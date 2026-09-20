"""定向采集：把一次检索意图变成「只拉相关的那几页」。

与 ``pipeline --stage fetch`` 的区别
------------------------------------
| | 全量采集 | 定向采集（本模块） |
| - | -------- | ------------------ |
| 入口 | 遍历 ``page_start..page_end`` | 用户查询词 → 门户服务端检索 |
| 参数 | ``searchValue=""`` 取全部 | ``searchValue=<查询词>`` |
| 规模 | 1040 条 ≈ 70 页 ≈ 4~5 小时 | 上限 ``max_fetch_pages`` 页 ≈ 数十秒 |
| 触发 | 人工执行脚本 | 检索命中不足时由用户在页面上确认 |

关键前提（已实测，见 docs/portal-probe.md）
------------------------------------------
门户列表接口 ``getNoticeByPage`` 的参数**必须走查询串**，
且它本身就支持 ``searchValue`` 服务端检索。
因此"有倾向性"不是本地过滤出来的，而是**让服务端只返回相关的**——
这既省请求（合规红线：≥2 秒/请求）也省时间。

留一条本地标题过滤作为兜底：实测门户的关键词检索是宽松匹配，
可能返回标题里并不含查询词的条目；这类"擦边"结果不入库，
避免把一次定向采集变成一次小规模全量灌库。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.config import AppConfig
from src.contracts import (
    ArticleRef,
    JobRecord,
    SearchQuery,
    StorageError,
    now_iso,
)

MAX_REF_OVERFETCH = 3
"""每页最多多取 3 倍于 15 条的候选，避免关键词检索异常时失控翻页。"""


def run_targeted_fetch(
    cfg: AppConfig,
    query: SearchQuery,
    index: Any = None,
    fetcher_factory: Optional[Callable[[AppConfig], Any]] = None,
    extractor_factory: Optional[Callable[[AppConfig], Any]] = None,
    validator_factory: Optional[Callable[[AppConfig], Any]] = None,
    repository_factory: Optional[Callable[[AppConfig], Any]] = None,
    login_checker: Optional[Callable[[AppConfig], None]] = None,
) -> Dict[str, Any]:
    """执行一次定向采集，返回 counters（可直接喂给 ``FetchJob``）。

    流程：**登录门禁** → 门户检索 → 逐页取详情（含归档与清单）→ 离线抽取 →
    校验 → 入库 → 索引失效。单篇失败不中断整批（与全量采集一致：失败只计入计数）。

    登录门禁是**必须的第一步**：未登录时门户对接口请求返回 HTTP 200 + 登录页 HTML
    （不是 401），若不放行前检查，错误会伪装成"接口返回不是合法 JSON"，
    很容易被误判成接口地址配错。
    """
    from src.crawler.crawler import PortalPageFetcher
    from src.crawler.detail_page import load_archived
    from src.parser.extractor import build_extractor
    from src.storage.database import build_repository
    from src.validation.validator import build_validator, deduplicate

    # ① 登录门禁：与 pipeline --stage fetch 同一道门（crawler.login_check.require_login）
    checker = login_checker
    if checker is None:
        from src.crawler.login_check import require_login

        checker = require_login
    checker(cfg)

    # 关键词候选逐个试：语料里的标点无法预知，任何"算出来"的词都可能在猜标点位置
    candidate_keywords = portal_candidates_of(query)
    counters: Dict[str, Any] = {
        "pages_fetched": 0,
        "refs_found": 0,
        "details_ok": 0,
        "details_failed": 0,
        "records_inserted": 0,
        "message": "",
    }

    fetcher = (fetcher_factory or PortalPageFetcher)(cfg)
    repository = None
    try:
        repository = (repository_factory or build_repository)(cfg)
        repository.init_schema()
        existing = set()
        try:
            existing = set(repository.existing_source_urls())
        except StorageError:
            existing = set()

        extractor = (extractor_factory or build_extractor)(cfg)
        validator = (validator_factory or build_validator)(cfg)

        refs, search_value, pages = _search_with_candidates(fetcher, cfg, query)
        # 如实计入候选重试的请求数：这个数字会展示给用户，不能美化
        counters["pages_fetched"] = pages

        counters["refs_found"] = len(refs)
        matched = [ref for ref in refs if _title_matches(ref.title, fallback_terms_of(query, search_value))]

        records: List[JobRecord] = []
        for ref in matched:
            raw = fetcher.fetch_detail(ref)
            if not raw.ok:
                counters["details_failed"] += 1
                continue
            counters["details_ok"] += 1
            try:
                from src.crawler.detail_page import append_manifest

                append_manifest(raw, cfg)
            except StorageError:
                pass
            archived = load_archived(ref, cfg)
            if not archived.ok:
                counters["details_failed"] += 1
                continue
            resource = replace(archived, images=raw.images)
            try:
                record = extractor.extract_from_html(resource)
            except Exception:  # noqa: BLE001 - 单篇解析失败不影响整批
                counters["details_failed"] += 1
                continue
            if validator.validate(record).ok:
                records.append(record)

        unique = deduplicate(records)
        fresh = [record for record in unique if record.source_url not in existing]
        if fresh:
            counters["records_inserted"] = repository.upsert_many(fresh)

        if not refs and candidate_keywords:
            counters["message"] = (
                f"门户用关键词 {candidate_keywords[:MAX_KEYWORD_CANDIDATES]} 均未召回"
                f"（用户查询：{query.raw_text or search_value}）。"
                "可换更短的词试试，或确认该主题确实在「就业信息」栏目里。"
            )
            return counters

        counters["message"] = (
            f"检索词「{search_value}」（用户查询：{query.raw_text or search_value}）："
            f"候选 {counters['refs_found']} 条，"
            f"标题命中 {len(matched)} 条，成功 {counters['details_ok']} 条，"
            f"新增入库 {counters['records_inserted']} 条"
        )
    finally:
        close = getattr(fetcher, "close", None)
        if callable(close):
            close()
        close_repo = getattr(repository, "close", None)
        if callable(close_repo):
            close_repo()

    # 入库后让索引版本号递增 → L1/L2 缓存自动失效，下一次检索立刻看到新数据
    if index is not None and counters.get("records_inserted"):
        try:
            index.bump_version()
        except Exception:  # noqa: BLE001 - 失效失败不应让作业变失败
            pass
    return counters


def _title_matches(title: str, terms: Sequence[str]) -> bool:
    """标题是否含任一过滤词（宽松兜底过滤）。

    没有过滤词时不设限（相当于取该页全部）。**只按标题过滤**：
    标题是唯一在列表阶段就可靠可得的文本；详情页正文的匹配交给抽取与检索环节。
    """
    wanted = [str(t).strip() for t in terms if str(t).strip()]
    if not wanted:
        return True
    text = str(title or "")
    return any(term in text for term in wanted)

MIN_PORTAL_KEYWORD_CHARS = 2
"""下发给门户的关键词长度下限。

实测单字查询（如「职」）会返回整页与查询无关的结果，等于白花一次请求；
但它仍胜于完全不过滤，因此仅在没有任何更长候选时才使用。
"""

_CJK_RUN_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]+")


def portal_candidates_of(query: SearchQuery) -> List[str]:
    """按"靠谱程度"降序的门户关键词候选（**按顺序试，取第一个有结果的**）。

    为什么不"算出一个正确答案"：门户是 LIKE 子串匹配，而语料里的标点无法预知。
    实测「职点迷津」在标题里写作「“职”点迷津」——引号把连续子串切断了，
    所以用户打在搜索框里的「职点迷津」**永远匹配不到**。
    任何"聪明"的截断都只是在猜标点位置（我猜错过：截成「职点迷」，
    恰好跨在引号上，0 召回，比不截还糟）。

    因此改为**枚举候选 + 逐个试**：
      1. 按标点切出的整段（标点位置正是语料里可能存在的断点）；
      2. 该段的短后缀（「职点迷津」→「点迷津」「迷津」）——
         后缀能绕过"词内部夹标点"这一最常见情况；
      3. 去重后按长度降序，先试长候选（长词更精确；「迷津」会顺带召回别的）。
    """
    raw = str(query.raw_text or "").strip()
    source = raw or " ".join(str(t) for t in query.keywords)

    candidates: List[str] = []
    for run in sorted(_CJK_RUN_RE.findall(source), key=len, reverse=True):
        if len(run) >= MIN_PORTAL_KEYWORD_CHARS:
            candidates.append(run)
            for start in range(1, len(run) - MIN_PORTAL_KEYWORD_CHARS + 1):
                suffix = run[start:]
                if len(suffix) >= MIN_PORTAL_KEYWORD_CHARS:
                    candidates.append(suffix)
        else:
            candidates.append(run)  # 单字兜底：胜于完全不过滤

    seen: List[str] = []
    for term in candidates:
        if term and term not in seen:
            seen.append(term)
    return seen


def portal_query_of(query: SearchQuery) -> str:
    """首选关键词（候选列表的第一个）；供展示与测试使用。"""
    candidates = portal_candidates_of(query)
    return candidates[0] if candidates else ""


def fallback_terms_of(query: SearchQuery, keyword: str = "") -> List[str]:
    """标题兜底过滤使用的词集合。

    **必须与"门户实际生效的那个关键词"同口径**，否则会出现
    "门户明明返回了结果、我们却全丢掉"（用户最初踩到的就是这种空转）。
    因此调用方把实际生效的 keyword 传进来；未传时退化为首选候选。
    """
    if not keyword:
        keyword = portal_query_of(query)
    return [keyword] if keyword else ""


MAX_KEYWORD_CANDIDATES = 4
"""最多尝试几个候选关键词。

每次尝试是 1 次列表请求（轻量、不抓详情）。4 个足够覆盖"整段 + 2~3 个后缀"，
同时把额外成本钉死在可预期范围内——不能为了召回把请求数放开。
"""


def _search_with_candidates(
    fetcher: Any, cfg: AppConfig, query: SearchQuery
) -> "tuple[List[ArticleRef], str, int]":
    """逐个候选调门户检索，返回 ``(refs, 实际生效的关键词, 真实翻页数)``。

    取**第一个有结果**的候选即停止，避免继续白花请求。
    所有候选都无结果时，返回空列表与首选候选（调用方据此给出准确提示）。
    """
    candidates = portal_candidates_of(query)
    if not candidates:
        return [], "", 0

    last_keyword = candidates[0]
    pages_total = 0
    for keyword in candidates[:MAX_KEYWORD_CANDIDATES]:
        last_keyword = keyword
        refs, pages = _safe_fetch_pages(fetcher, cfg, keyword)
        pages_total += pages
        if refs:
            return refs, keyword, pages_total

    return [], last_keyword, pages_total


def _safe_fetch_pages(
    fetcher: Any, cfg: AppConfig, keyword: str
) -> "tuple[List[ArticleRef], int]":
    """按 ``max_fetch_pages`` 抓若干页，返回 ``(refs, pages)``。

    页失败（FetchError/ParseError）不抛出而是当作"该候选无结果"，
    这样单个候选失败不会中断候选枚举；真正系统性的错误（如登录失效）
    会由每个候选一致地失败，最终表现为空结果并在消息里说明。
    """
    collected: List[ArticleRef] = []
    pages = 0
    for page in range(1, int(cfg.search.max_fetch_pages) + 1):
        try:
            page_refs = fetcher.fetch_list(page, keyword)
        except Exception:  # noqa: BLE001 - 单个候选失败不应中断枚举
            break
        pages += 1
        if not page_refs:
            break
        collected.extend(page_refs[: PAGE_SIZE * MAX_REF_OVERFETCH])
    return collected, pages



    """标题兜底过滤使用的词集合。

    **关键约束：过滤条件不能比门户召回条件更严**，否则会出现
    "门户明明返回了结果、我们却全丢掉"（用户实际踩到的就是这种空转）。

    因此这里**只使用下发给门户的那个关键词**。不再并入整串查询——
    整串往往因为引号而根本不可能出现在标题里，并进来只会把结果误杀。
    （标题兜底过滤的角色是"防门户宽松匹配带回无关条目"，
      而不是做第二次严格匹配。）
    """
    portal_keyword = portal_query_of(query)
    return [portal_keyword] if portal_keyword else []



PAGE_SIZE = 15
"""门户每页条数（实测），用于限制单页候选上限。"""
