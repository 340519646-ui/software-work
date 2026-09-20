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
) -> Dict[str, Any]:
    """执行一次定向采集，返回 counters（可直接喂给 ``FetchJob``）。

    流程：门户检索 → 逐页取详情（含归档与清单）→ 离线抽取 → 校验 → 入库 → 索引失效。
    单篇失败不中断整批（与全量采集一致：失败只计入计数）。
    """
    from src.crawler.crawler import PortalPageFetcher
    from src.crawler.detail_page import load_archived
    from src.parser.extractor import build_extractor
    from src.storage.database import build_repository
    from src.validation.validator import build_validator, deduplicate

    search_value = query.raw_text or " ".join(query.keywords)
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

        refs: List[ArticleRef] = []
        for page in range(1, int(cfg.search.max_fetch_pages) + 1):
            page_refs = fetcher.fetch_list(page, search_value)
            counters["pages_fetched"] += 1
            if not page_refs:
                break
            refs.extend(page_refs[: PAGE_SIZE * MAX_REF_OVERFETCH])

        counters["refs_found"] = len(refs)
        matched = [ref for ref in refs if _title_matches(ref.title, query.keywords)]

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

        counters["message"] = (
            f"检索词「{search_value}」：候选 {counters['refs_found']} 条，"
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


def _title_matches(title: str, keywords: Sequence[str]) -> bool:
    """标题是否含任一检索词（宽松兜底过滤）。

    没有关键词时不设限（相当于取该页全部），否则要求命中。
    **只按标题过滤**：标题是唯一在列表阶段就可靠可得的文本；
    详情页正文的匹配交给抽取与检索环节，避免在这里过度丢数据。
    """
    terms = [str(k).strip() for k in keywords if str(k).strip()]
    if not terms:
        return True
    text = str(title or "")
    return any(term in text for term in terms)


PAGE_SIZE = 15
"""门户每页条数（实测），用于限制单页候选上限。"""
