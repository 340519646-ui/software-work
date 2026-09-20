"""列表页：URL 拼接、HTML 解析（纯函数）与单页抓取。

职责边界
--------
* ``parse_list`` 是**纯函数**：只吃 HTML 字符串，吐 ``ArticleRef`` 列表，
  不发请求、不读文件，因此可以用 ``tests/`` 里的样本 HTML 直接单测。
* ``fetch_list`` 负责 IO：调 Transport 拿 HTML，再交给 ``parse_list``。

输出契约
--------
返回的每个 ``ArticleRef`` 必须满足：

* ``detail_url`` 为**绝对 URL**（相对链接需用页面 URL 补全）；
* ``list_url`` 记录该链接来自哪一页（溯源用，必须填）；
* 同一页内按出现顺序返回，**不得去重**（跨页去重由 crawler.iter_refs 负责）；
* 解析不到任何链接时返回空列表，**不要**抛异常——翻页结束由上层判断。
"""

from __future__ import annotations

import json
import logging
import random
import re
import secrets
from typing import Any, Dict, List, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from src.config import AppConfig
from src.contracts import ArticleRef, ConfigError, FetchError, ParseError, Transport

DATE_PATTERN = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
NOISE_PREFIXES: tuple = ("javascript:", "mailto:", "tel:", "#")
NOISE_URL_TOKENS: tuple = ("/login", "/logout", "javascript", "void(0)")
"""明显不是详情页的链接特征（登录、登出、脚本跳转）。

**待核对**：不同门户的导航/分页链接形态不同，必要时按实际页面增删这里的过滤词。
"""


def _make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:  # pragma: no cover - 解析后端缺失时降级
        return BeautifulSoup(html or "", "html.parser")


def _nearby_date(node) -> str:
    """从链接所在的行（li/tr/div）里找形如 2025-06-12 的日期，找不到返回空串。"""
    parent = node
    for _ in range(3):
        parent = getattr(parent, "parent", None)
        if parent is None:
            break
        matched = DATE_PATTERN.search(parent.get_text(" ", strip=True))
        if matched:
            return f"{matched.group(1)}-{int(matched.group(2)):02d}-{int(matched.group(3)):02d}"
        if getattr(parent, "name", "") in ("li", "tr", "div"):
            break
    return ""



def list_page_url(cfg: AppConfig, page: int) -> str:
    """按门户配置生成第 ``page`` 页的列表页 URL（唯一实现，禁止各处手拼 URL）。

    约定：
      * ``page == 1`` 时可返回 ``portal.list_url`` 原样；
      * 其余页使用 ``portal.page_param`` 与 ``page`` 拼参数；
      * 已有的 query 参数必须保留。
    """
    base = str(cfg.portal.list_url or "").strip()
    if not base:
        raise ConfigError("portal.list_url 未配置：请在 config/config.yaml 填写就业信息栏目列表页地址")

    if "{page}" in base:
        return base.replace("{page}", str(page))
    if page <= 1:
        return base

    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[cfg.portal.page_param or "page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def parse_list(html: str, page_url: str, cfg: AppConfig) -> List[ArticleRef]:
    """纯函数：从列表页 HTML 中解析出 ``ArticleRef`` 列表。

    实现要点：用 ``cfg.portal.detail_link_selector`` 定位详情页链接；
    标题与发布时间能取到就填，取不到留空字符串（不要填「未知」——「未知」只用于七项核心字段）。
    """
    soup = _make_soup(html)
    selector = str(cfg.portal.detail_link_selector or "").strip()
    nodes = soup.select(selector) if selector else soup.select("a[href]")

    refs: List[ArticleRef] = []
    for node in nodes:
        href = str(node.get("href") or "").strip()
        if not href or href.startswith(NOISE_PREFIXES):
            continue
        detail_url = urljoin(page_url, href)
        if not detail_url.startswith(("http://", "https://")):
            continue
        lowered = detail_url.lower()
        if any(token in lowered for token in NOISE_URL_TOKENS):
            continue
        title = str(node.get("title") or node.get_text(" ", strip=True) or "").strip()
        refs.append(
            ArticleRef(
                detail_url=detail_url,
                list_url=page_url,
                title=title,
                publish_date=_nearby_date(node),
            )
        )
    return refs


def fetch_list(transport: Transport, cfg: AppConfig, page: int) -> List[ArticleRef]:
    """抓取并解析第 ``page`` 页。

    请求失败（HttpResponse 非 2xx 或 error 非空）时抛 ``FetchError``；
    页面结构变化导致解析异常时抛 ``ParseError``——两者都由 pipeline 记录进
    ``StageResult.errors`` 并计入 ``errors`` 计数，不中断其余页。
    """
    url = list_page_url(cfg, page)
    response = transport.get(url)
    if not response.ok:
        raise FetchError(
            f"列表页请求失败（HTTP {response.status_code}，第 {page} 页）",
            url=url,
            detail=response.error,
        )
    try:
        return parse_list(response.text, url, cfg)
    except Exception as exc:
        raise ParseError(
            f"列表页解析失败（第 {page} 页）：请核对 portal.detail_link_selector 是否匹配门户当前 DOM",
            url=url,
            detail=str(exc),
        ) from exc


# ======================================================================
# 接口（JSON）模式的列表采集
#
# 实测：本校门户通知列表不是 DOM 页面，而是
#   POST /comsys-portal-notice-web/getNoticeByPage
#   Content-Type: application/json;charset=UTF-8
#   翻页：URL 不变，请求体里的 currentPage（页码）与 comsys_random_t（随机令牌）变化
# 因此这里 POST JSON 并解析响应，字段名/路径/详情链接模板全部走配置。
# ======================================================================


logger = logging.getLogger(__name__)

LIST_PATH_CANDIDATES: tuple = (
    "datas.tables",
    "datas.records",
    "datas.list",
    "datas.rows",
    "datas.items",
    "datas",
    "data.tables",
    "data.records",
    "data.list",
    "data.notices",
    "data.rows",
    "data.items",
    "data",
    "tables",
    "records",
    "notices",
    "list",
    "rows",
    "items",
    "result.records",
    "result.list",
)

LIST_SEARCH_MAX_DEPTH = 3
"""递归查找列表数组的最大深度（防止在超大响应里无谓遍历）。"""
"""列表数组的常见命名。配置里的 ``api_list_path`` 优先；取不到时按这里顺序自动识别。"""

TITLE_FIELD_CANDIDATES: tuple = ("title", "noticeTitle", "notice_title", "subject", "name", "newsTitle")
DATE_FIELD_CANDIDATES: tuple = (
    "publishTime",
    "publishDate",
    "publish_time",
    "pubDate",
    "createTime",
    "create_time",
    "noticeTime",
    "releaseTime",
    "releaseDate",
    "date",
)
"""列表项里标题/时间的常见字段名：配置优先，配错时自动识别（并提示写回配置）。"""

TOKEN_STYLE_JS_RANDOM = "js_random"
TOKEN_STYLE_HEX = "hex32"


def random_token(style: str = TOKEN_STYLE_JS_RANDOM, length: int = 32) -> str:
    """生成接口令牌（``comsys_random_t`` 这类字段）。

    实测本校门户的该字段形如 ``0.9887865334032206`` —— 即浏览器 ``Math.random()`` 的
    输出格式（0 开头、约 16 位小数），因此默认按同样的格式生成；
    若某门户要求定长十六进制串，把 ``portal.api_token_style`` 改成 ``hex32``。

    **待核对**：若服务端校验该令牌（例如参与签名），必须改为从页面元素/接口获取，
    仅靠随机值会被拒；判定方法见 docs/portal-probe.md 第八节第 3 项。
    """
    if (style or "").strip().lower() != TOKEN_STYLE_HEX:
        # repr(random.random()) 与 JS Math.random() 同形：0.xxxxxxxxxxxxxxxx
        return repr(random.random())
    size = max(8, int(length))
    return secrets.token_hex((size + 1) // 2)[:size]


def envelope_summary(payload: Any) -> str:
    """摘出应答外壳里的标量字段（``code`` / ``msg`` / ``success`` 等），用于错误信息。

    接口出错时通常仍是 HTTP 200，真正的失败原因只在外壳里
    （实测该门户的响应含 ``msg``；把 ``comsys_random_t`` 改成任意值也照样成功，
    说明它不被校验，因此"失败"基本只可能来自登录态或参数）。
    """
    if not isinstance(payload, Mapping):
        return f"响应类型：{type(payload).__name__}"

    scalars = {
        str(key): (str(value)[:80] if not isinstance(value, (int, float, bool)) else value)
        for key, value in payload.items()
        if value is None or isinstance(value, (str, int, float, bool))
    }
    keys = [str(key) for key in list(payload)[:12]]
    return f"顶层键：{keys}；标量字段：{scalars}"


def locate_records(payload: Any, cfg: AppConfig) -> "tuple[Any, str]":
    """定位响应里的列表数组，返回 ``(records, path)``；找不到返回 ``(None, "")``。

    优先用配置的 ``api_list_path``；配错/路径变了时按常见命名自动识别（并记日志），
    这样门户改版后不必先改代码就能跑起来。
    """
    configured = dig(payload, cfg.portal.api_list_path)
    if isinstance(configured, (list, tuple)):
        return configured, cfg.portal.api_list_path

    def looks_like_records(value: Any) -> bool:
        return isinstance(value, (list, tuple)) and any(isinstance(item, Mapping) for item in value)

    for candidate in LIST_PATH_CANDIDATES:
        value = dig(payload, candidate)
        if looks_like_records(value):
            logger.warning(
                "配置的 api_list_path=%r 没取到列表，自动识别为 %r；建议把它写进 config.yaml",
                cfg.portal.api_list_path,
                candidate,
            )
            return value, candidate

    # 最后：递归下钻（限深），解决 datas.tables 这类嵌套命名
    found, found_path = _search_records(payload)
    if found is not None:
        logger.warning(
            "未匹配 portal.api_list_path=%r，已自动定位到 %r；建议把它写进 config.yaml",
            cfg.portal.api_list_path,
            found_path,
        )
        return found, found_path
    return None, ""


def _search_records(value: Any, depth: int = 0, path: str = "") -> "tuple[Any, str]":
    """在响应里递归找第一个「对象数组」，返回 ``(records, path)``。

    实测该门户的列表是嵌套在外壳里的（形如 ``{"datas": {...}, "msg": ..., "state": ...}``），
    因此不能只靠固定的候选路径；这里按层下钻，找到即返回，并把路径打进日志。
    """
    if depth > LIST_SEARCH_MAX_DEPTH:
        return None, ""

    if isinstance(value, (list, tuple)):
        if any(isinstance(item, Mapping) for item in value):
            return value, path
        return None, ""

    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            found, found_path = _search_records(child, depth + 1, child_path)
            if found is not None:
                return found, found_path
    return None, ""


def detect_external_link(record: Mapping[str, Any], cfg: AppConfig) -> "tuple[str, bool]":
    """判断这条通知的正文是否在站外，返回 ``(站外URL, 是否站外)``。

    实测字段：``notice_link_state`` 为 1 且 ``notice_link`` 非空 → 站外
    （微信公众号文章 / 腾讯文档等）。状态字段缺失时，只要链接存在就按站外处理。
    """
    link = str(dig(record, cfg.portal.api_link_field, "") or "").strip()
    if not link:
        return "", False

    state_field = cfg.portal.api_link_state_field
    state = dig(record, state_field, None) if state_field else None
    if state is None:
        return link, True
    try:
        return link, int(state) == int(cfg.portal.api_link_state_external)
    except (TypeError, ValueError):
        return link, False


def detect_field(record: Mapping[str, Any], configured: str, candidates: "tuple", label: str) -> str:
    """取列表项里的一个字段：先按配置，取不到再按常见命名识别（返回空串表示都没有）。"""
    value = dig(record, configured, "")
    if str(value or "").strip():
        return str(value).strip()

    for name in candidates:
        value = dig(record, name, "")
        if str(value or "").strip():
            logger.warning(
                "列表项没有 %s（配置 %r），自动识别为 %r；建议把它写进 config.yaml",
                label,
                configured,
                name,
            )
            return str(value).strip()
    return ""


def parse_total_pages(payload_text: str, cfg: AppConfig) -> int:
    """从响应里取**总页数**（实测 ``page.totalCounts`` = 70，而 total=1040、pageSize=15）。

    取不到时按 ``total / pageSize`` 估算；都取不到返回 0（表示未知）。
    """
    try:
        payload = json.loads(payload_text or "")
    except (ValueError, TypeError):
        return 0

    pages = dig(payload, cfg.portal.api_total_pages_path)
    try:
        if pages not in (None, ""):
            return max(0, int(pages))
    except (TypeError, ValueError):
        pass

    total = dig(payload, cfg.portal.api_total_path)
    page_size = int((cfg.portal.api_body or {}).get("pageSize") or 0)
    try:
        if total is not None and page_size > 0:
            return max(1, (int(total) + page_size - 1) // page_size)
    except (TypeError, ValueError):
        pass
    return 0


def build_api_body(cfg: AppConfig, page: int, token: str = "") -> Dict[str, Any]:
    """按配置拼装请求体：注入页码、令牌，并**按页码推导 start/end**，其余字段原样保留。

    实测请求体里 ``start`` / ``end`` 是分页窗口（第 2 页、pageSize=15 → start=15、end=30），
    由页码算得而非人工维护；只有模板里出现这两个键时才写入，避免给不需要的门户多发字段。
    """
    body: Dict[str, Any] = dict(cfg.portal.api_body or {})
    body[cfg.portal.api_page_field or "currentPage"] = page

    token_field = cfg.portal.api_token_field
    if token_field:
        body[token_field] = token or random_token(cfg.portal.api_token_style, cfg.portal.api_token_length)

    if "start" in body or "end" in body:
        try:
            page_size = int(body.get("pageSize") or 0)
        except (TypeError, ValueError):
            page_size = 0
        if page_size > 0:
            body["start"] = (int(page) - 1) * page_size
            body["end"] = int(page) * page_size
    return body


def build_api_query(
    cfg: AppConfig,
    page: int,
    token: str = "",
    *,
    search_value: str = "",
    start_date: str = "",
    end_date: str = "",
) -> Dict[str, str]:
    """按实测把分页与过滤参数摊平成**查询串参数**。

    服务端只认查询串（JSON 体被忽略），因此这里是让翻页与栏目过滤生效的关键。
    布尔值按小写字符串发送（``false``），避免被当成真值。

    ``search_value`` 是**门户自带的服务端关键词检索**（配置里的 ``searchValue``），
    定向采集靠它做到"只拉相关的几页"；留空则维持全量语义（向后兼容）。
    """
    params: Dict[str, Any] = dict(cfg.portal.api_body or {})
    if search_value:
        params["searchValue"] = str(search_value)
    if start_date:
        params["start_date"] = str(start_date)
    if end_date:
        params["end_date"] = str(end_date)
    params[cfg.portal.api_page_field or "currentPage"] = page

    if "start" in params or "end" in params:
        try:
            size = int(params.get("pageSize") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            params["start"] = (int(page) - 1) * size
            params["end"] = int(page) * size

    token_field = cfg.portal.api_token_field
    if token_field:
        params[token_field] = token or random_token(cfg.portal.api_token_style, cfg.portal.api_token_length)

    rendered: Dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            rendered[str(key)] = ""
        elif isinstance(value, bool):
            rendered[str(key)] = "true" if value else "false"
        else:
            rendered[str(key)] = str(value)
    return rendered


def build_api_url(
    cfg: AppConfig,
    page: int,
    token: str = "",
    *,
    search_value: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """按 ``api_param_style`` 生成请求 URL（query 模式下把参数拼进查询串）。"""
    url = str(cfg.portal.api_url or "").strip()
    if not url:
        return url
    if cfg.portal.api_param_style != "query":
        return url
    query = urlencode(
        build_api_query(
            cfg,
            page,
            token,
            search_value=search_value,
            start_date=start_date,
            end_date=end_date,
        )
    )
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def dig(data: Any, path: str, default: Any = None) -> Any:
    """按点分路径取值，如 ``dig(payload, "data.records")``；取不到返回 default。"""
    if not path:
        return default
    current = data
    for part in str(path).split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def _flatten(record: Mapping[str, Any], prefix: str = "") -> Dict[str, str]:
    """把记录摊平成 ``{占位名: 字符串}``，支持 ``{notice_id}`` 与 ``{data.noticeId}`` 两种写法。"""
    flat: Dict[str, str] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(_flatten(value, prefix=f"{name}."))
        elif value is None:
            flat[name] = ""
        else:
            flat[name] = str(value)
    return flat


PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")


def build_detail_url(record: Mapping[str, Any], cfg: AppConfig) -> str:
    """用详情模板 + 记录字段拼详情链接；**缺任一占位字段时返回空串**（该条跳过）。

    不用 ``str.format``：它会把 ``{a.b}`` 当成属性访问，无法表达嵌套字段；
    这里自己替换，既能写 ``{notice_id}``，也能写 ``{data.noticeId}``。
    """
    template = str(cfg.portal.detail_url_template or "").strip()
    if not template:
        return ""

    flat = _flatten(record)

    # 组织 ID 兜底：列表项通常不带 organization_id，但它是详情链接的必需参数
    # （实测门户的 orgId 是固定值）。命中占位名才回填，不会覆盖记录自带的值。
    org_id = str(cfg.portal.detail_org_id or "").strip()
    if org_id:
        for name in ("organization_id", "organizationId", "orgId"):
            if not flat.get(name):
                flat[name] = org_id

    missing: List[str] = []

    def substitute(match: "re.Match[str]") -> str:
        name = match.group(1).strip()
        if name in flat and flat[name] != "":
            return flat[name]
        missing.append(name)
        return match.group(0)

    url = PLACEHOLDER_PATTERN.sub(substitute, template)
    return "" if missing else url


def looks_like_html(text: str) -> bool:
    """响应体是否明显是 HTML 文档（而不是 JSON）。

    这是判断"登录态失效"的关键信号：未登录时门户对接口请求返回
    **HTTP 200 + 统一身份认证登录页**，而不是 401/302。
    仅凭状态码无法发现，只能看响应体。
    """
    head = str(text or "").lstrip()[:200].lower()
    return head.startswith(("<!doctype html", "<html", "<?xml")) or "</html>" in str(text or "").lower()


def parse_list_json(payload_text: str, api_url: str, cfg: AppConfig) -> List[ArticleRef]:
    """解析接口返回的 JSON 列表（纯函数，便于单测）。

    失败时区分两种原因，因为处理方式完全不同：

    * **响应是 HTML** → 基本可以断定登录态失效（门户返回登录页而不是 JSON），
      错误信息直接把这一条放在最前面，并附上命中的登录页特征词；
    * 响应既不是 HTML 也不是合法 JSON → 才是接口结构/地址问题。
    """
    from src.crawler.session import LOGIN_FORM_MARKERS  # 登录页特征词（同一层内的常量）

    if looks_like_html(payload_text):
        lowered = str(payload_text or "").lower()
        matched = [m for m in LOGIN_FORM_MARKERS if m.lower() in lowered]
        hint = (
            "门户返回的是**登录页 HTML**（HTTP 200），说明登录态已失效或从未登录"
            if matched
            else "门户返回的是 HTML 而不是 JSON"
        )
        raise ParseError(
            f"列表接口未返回 JSON：{hint}。"
            "请先执行 `python -m src.crawler.login_check` 重新登录（会自动落盘会话），"
            "再重试采集。",
            url=api_url,
            detail=f"登录页特征={matched[:3]}；payload 开头={str(payload_text)[:120]!r}",
        )

    try:
        payload = json.loads(payload_text or "")
    except (ValueError, TypeError) as exc:
        raise ParseError(
            "列表接口返回的不是合法 JSON：请核对 api_url 是否正确、"
            "以及接口参数是否仍与门户一致（若登录态刚过期，请先跑 login_check）",
            url=api_url,
            detail=f"{exc}; payload={str(payload_text)[:200]!r}",
        ) from exc

    records, path = locate_records(payload, cfg)
    if records is None:
        raise ParseError(
            "响应里没有通知列表数组：优先怀疑登录态失效或接口参数变化"
            "（若确认是结构变化，请把实际路径写进 portal.api_list_path）",
            url=api_url,
            detail=envelope_summary(payload),
        )
    logger.debug("列表数组路径：%s", path)

    refs: List[ArticleRef] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        detail_url = build_detail_url(record, cfg)
        if not detail_url:
            continue
        external_url, is_external = detect_external_link(record, cfg)
        policy = cfg.portal.external_link_policy
        if is_external and policy == "skip":
            logger.debug("按 external_link_policy=skip 跳过站外通知：%s", external_url)
            continue
        # content_url 为「正文实际所在地址」：portal 策略下强制留空（只抓门户页）
        content_url = external_url if (is_external and policy == "fetch") else ""

        title = detect_field(record, cfg.portal.api_title_field, TITLE_FIELD_CANDIDATES, "标题")
        date_value = detect_field(record, cfg.portal.api_date_field, DATE_FIELD_CANDIDATES, "发布时间")
        refs.append(
            ArticleRef(
                detail_url=detail_url,
                list_url=api_url,
                title=title,
                publish_date=_normalize_date(date_value),
                content_url=content_url,
            )
        )
    return refs


def _normalize_date(value: str) -> str:
    """把接口返回的时间统一成 ``YYYY-MM-DD``；识别不了就留空（不编造）。"""
    matched = DATE_PATTERN.search(value or "")
    if not matched:
        return ""
    return f"{matched.group(1)}-{int(matched.group(2)):02d}-{int(matched.group(3)):02d}"


def fetch_list_api(
    transport: Transport,
    cfg: AppConfig,
    page: int,
    *,
    search_value: str = "",
    start_date: str = "",
    end_date: str = "",
) -> List[ArticleRef]:
    """采集第 ``page`` 页：POST JSON → 解析 → ``ArticleRef`` 列表。

    分页口径与门户一致：**URL 不变**，靠请求体里的页码字段翻页。
    ``search_value`` 非空时走门户的**服务端关键词检索**（定向采集用）。
    """
    url = str(cfg.portal.api_url or "").strip()
    if not url:
        raise ConfigError("portal.api_url 未配置：请填写列表接口地址（config/config.yaml 的 portal.api_url）")

    request_url = build_api_url(
        cfg,
        page,
        search_value=search_value,
        start_date=start_date,
        end_date=end_date,
    )
    body = build_api_body(cfg, page)
    # 实测：参数必须走查询串（request_url 里已带）；JSON 体仍发送但服务端忽略，
    # 保留它是为了换门户时无需改代码（api_param_style=json 时就用它）。
    response = transport.post_json(request_url, body, referer=cfg.portal.list_url or cfg.portal.base_url)
    if not response.ok:
        raise FetchError(
            f"列表接口请求失败（HTTP {response.status_code}，第 {page} 页）",
            url=request_url,
            detail=response.error,
        )
    # list_url 记录**带参数的完整 URL**，让清单能还原"这条记录来自哪一页、哪个查询"
    return parse_list_json(response.text, request_url, cfg)
