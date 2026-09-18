"""跨模块接口契约层（第 0 层，Layer 0）。

本模块是本项目**唯一**允许被所有其他模块 import 的模块，它只依赖 Python 标准库。

三条不可违反的规则（由 tests/test_contracts.py 自动检查）：

1. 本模块**不得** import 任何项目内其他模块，也不得 import 第三方库
   （requests / bs4 / pandas / openai / pytesseract ...）。
2. 任何跨模块传递的数据都必须是本模块定义的 DTO，禁止跨边界传
   BeautifulSoup 对象、requests.Response、裸 ``dict[str, Any]``、DataFrame。
3. 契约变更必须同时：改 SCHEMA_VERSION → 改本文件 → 改 tests/test_contracts.py
   → 改 docs/architecture.md。缺一不可。

变更记录
--------
* **1.4.0**：区分「门户详情页」与「正文实际所在 URL」。实测门户的就业信息通知里，
  相当一部分是**站外链接**（微信公众号文章、腾讯文档），正文不在门户上：
  于是 ``ArticleRef`` 增加 ``content_url`` 与 ``fetch_url``，``ManifestEntry`` 序列化该字段。
  **幂等键仍取门户详情页 URL**，因此同一篇通知无论正文在哪，主键都稳定不变。
* **1.3.0**：支持**接口（JSON）模式的列表采集**。``Transport`` 新增 ``post_json``。
  原因：实测学校门户的通知列表不是 DOM 页面，而是
  ``POST /comsys-portal-notice-web/getNoticeByPage``（``Content-Type: application/json``），
  翻页靠请求体里的 ``currentPage``；解析 DOM 既不可行也不抗改版。
* **1.2.0**：接入真实门户后的补强。``HttpResponse`` 增加 ``final_url``（重定向落点）、
  ``LoginStatus`` 增加 ``final_url``。原因：实测未登录访问 ``my.muc.edu.cn`` 会被 302 到
  ``ca.muc.edu.cn/zfca/login``（统一身份认证），而 requests 会自动跟随重定向、
  返回 200 —— 若不记录落点，就会把**登录页当成正常页面**，登录自检形同虚设。
* **1.1.0**：图片型数据成为一等公民。新增 ``ImageAsset`` / ``ContentKind`` / ``OcrStatus``、
  ``RawArticle.images``、``ManifestEntry.images``、``CleanArticle.source_kind``、
  ``JobRecord.content_kind``、``archived_image_relpath`` / ``ocr_cache_relpath`` /
  ``sha1_of_bytes``，``PageFetcher`` 增加 ``fetch_image``，计数键新增
  ``images_fetched`` / ``images_failed`` / ``ocr_cached`` / ``ocr_computed`` / ``ocr_failed``。
  原因：门户上多数文章正文是截图，需要「算一次 OCR、一直复用」的内容寻址缓存。
* **1.0.0**：初版契约（七项核心字段 + 溯源字段 + 三层管线 DTO 与 Protocol）。

分层与依赖方向（箭头 = 允许 import 的方向）::

    contracts  ←  config
        ↑            ↑
        ├── crawler  │
        ├── parser   │
        ├── storage  │
        ├── validation
        └── pipeline ──→ 允许 import 上面全部层
              ↑
             main

    禁止：parser → storage / crawler；crawler → parser / storage；
          storage → parser / crawler；任何层 → pipeline；任何层 → main。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set, Tuple, runtime_checkable

# --------------------------------------------------------------------------
# 版本与全局常量
# --------------------------------------------------------------------------

SCHEMA_VERSION = "1.4.0"
"""契约版本。DTO 字段增删改时必须递增，并在 docs/architecture.md 记录变更。"""

MISSING = "未知"
"""缺失值占位符。任何一层都不得用空字符串、None、'-' 表示缺失。"""


class Stage(str, Enum):
    """三段式管线的阶段名，同时也是命令行 ``--stage`` 的合法取值。"""

    FETCH = "fetch"
    EXTRACT = "extract"
    EXPORT = "export"

    @classmethod
    def from_cli(cls, value: str) -> "Stage":
        """把命令行字符串转为 Stage；非法取值抛出 ValueError（附合法清单）。"""
        normalized = (value or "").strip().lower()
        for stage in cls:
            if stage.value == normalized:
                return stage
        allowed = ", ".join(s.value for s in cls)
        raise ValueError(f"未知阶段 {value!r}，合法取值为：{allowed}")


class ExtractMethod(str, Enum):
    """字段的抽取方式，写入 JobRecord.extract_method 与 FieldHit.method。"""

    RULE = "rule"
    LLM = "llm"
    HYBRID = "hybrid"
    NONE = "none"


class ContentKind(str, Enum):
    """记录的内容来源：正文在 HTML 里，还是必须 OCR 才有文字。

    这个区分不是装饰：``ocr`` 来源的记录必须人工复核（合规红线 5），
    准确率统计也要按来源分开看，否则图片型文章的抽取质量会被文字型拉平掩盖。
    """

    HTML = "html"
    OCR = "ocr"
    MIXED = "mixed"


class OcrStatus(str, Enum):
    """单张图片的 OCR 处理状态（失败也要如实记录，不得静默跳过）。"""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class ReviewStatus(str, Enum):
    """人工复核状态（人工抽检环节写回）。"""

    PENDING = "pending"
    APPROVED = "approved"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ErrorCode(str, Enum):
    """错误码。所有 Diagnostic / Issue 与 PipelineError 必须使用这里的取值。"""

    CONFIG_INVALID = "config_invalid"
    LOGIN_REQUIRED = "login_required"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"
    EXTRACT_INCOMPLETE = "extract_incomplete"
    FIELD_INVALID = "field_invalid"
    FIELD_OUT_OF_RANGE = "field_out_of_range"
    DUPLICATE = "duplicate"
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_BAD_RESPONSE = "llm_bad_response"
    OCR_UNAVAILABLE = "ocr_unavailable"
    OCR_NEEDS_REVIEW = "ocr_needs_review"
    STORAGE_ERROR = "storage_error"
    INTERNAL = "internal"


CORE_FIELDS: Tuple[str, ...] = (
    "graduation_year",
    "grade",
    "degree",
    "major",
    "city",
    "employer",
    "position",
)
"""七项核心字段，顺序即报表展示顺序。契约测试会与 config/fields.yaml 核对。"""

CORE_FIELD_LABELS: Mapping[str, str] = {
    "graduation_year": "届别",
    "grade": "年级",
    "degree": "学历",
    "major": "专业",
    "city": "城市",
    "employer": "单位",
    "position": "岗位",
}

PROVENANCE_FIELDS: Tuple[str, ...] = (
    "article_title",
    "publish_date",
    "source_url",
    "list_url",
    "raw_html_path",
    "crawl_time",
    "extract_method",
    "evidence",
    "review_status",
)
"""溯源字段。每条记录必须能沿这些字段回溯到原始页面与命中片段。"""

COUNTER_KEYS: Tuple[str, ...] = (
    "pages_listed",
    "refs_found",
    "details_fetched",
    "details_failed",
    "html_archived",
    "images_fetched",
    "images_failed",
    "records_extracted",
    "rule_hits",
    "llm_calls",
    "llm_failed",
    "ocr_calls",
    "ocr_cached",
    "ocr_computed",
    "ocr_failed",
    "unknown_fields",
    "duplicates",
    "validated",
    "invalid",
    "inserted",
    "manual_review",
    "exported_rows",
    "skipped_existing",
    "errors",
)
"""StageResult.counters 的合法键。新增计数项必须先改这里，禁止就地造键。"""

DEFAULT_RAW_HTML_DIR = "data/raw/html"
DEFAULT_IMAGE_DIR = "data/raw/images"
DEFAULT_OCR_CACHE_DIR = "data/raw/ocr"
DEFAULT_MANUAL_REVIEW_PATH = "data/processed/manual_review.csv"
DEFAULT_CSV_PATH = "data/processed/jobs.csv"
DEFAULT_XLSX_PATH = "data/processed/jobs.xlsx"
DEFAULT_DB_PATH = "data/employment.db"


# --------------------------------------------------------------------------
# 通用工具（纯函数，禁止 IO）
# --------------------------------------------------------------------------


def now() -> datetime:
    """统一取当前本地时间（带时区），所有 DTO 的时间字段都从这里取。"""
    return datetime.now(timezone.utc).astimezone()


def now_iso() -> str:
    """crawl_time 等字符串时间字段的统一格式：ISO 8601，秒级精度。"""
    return now().replace(microsecond=0).isoformat()


def article_key_of(detail_url: str) -> str:
    """由详情页 URL 推导稳定主键（sha1 前 16 位）。

    这是**幂等键**：同一 URL 在采集、解析、入库、导出各层得到同一 key，
    断点续跑与去重都依赖它，禁止改用自增序号或时间戳。
    """
    url = (detail_url or "").strip()
    if not url:
        raise ValueError("article_key_of: detail_url 不能为空")
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


MANIFEST_NAME = "manifest.jsonl"
"""采集清单文件名。fetch 阶段写、extract 阶段读，是两阶段之间唯一的数据交换格式。"""


def manifest_relpath(raw_dir: str = DEFAULT_RAW_HTML_DIR) -> str:
    """清单路径规则：``<raw_dir 的父目录>/manifest.jsonl``（默认 ``data/raw/manifest.jsonl``）。"""
    parent = posixpath.dirname(raw_dir.rstrip("/")) or "."
    return f"{parent}/{MANIFEST_NAME}"


def raw_html_relpath(detail_url: str, raw_dir: str = DEFAULT_RAW_HTML_DIR) -> str:
    """归档路径规则：``<raw_dir>/<article_key>.html``，确定性、可重建、天然去重。"""
    return f"{raw_dir.rstrip('/')}/{article_key_of(detail_url)}.html"


def sha1_of_bytes(data: bytes) -> str:
    """内容哈希（40 位十六进制）。

    图片的归档命名、去重键与 OCR 缓存键都取自它——**同一张图只算一次 OCR**，
    这是本项目在图片型数据上最主要的性能设计（见 docs/storage-decision.md）。
    """
    return hashlib.sha1(data).hexdigest()


def archived_image_relpath(
    detail_url: str,
    image_sha1: str,
    suffix: str = ".png",
    images_dir: str = DEFAULT_IMAGE_DIR,
) -> str:
    """图片归档路径：``<images_dir>/<article_key>/<image_sha1><suffix>``。

    按内容命名而不是按序号命名：同一张图在同一篇文章里重复出现只存一份，
    页面图片顺序变化也不会导致归档错位。
    """
    normalized = suffix if suffix.startswith(".") else f".{suffix}"
    return f"{images_dir.rstrip('/')}/{article_key_of(detail_url)}/{image_sha1}{normalized}"


def ocr_cache_relpath(image_sha1: str, cache_dir: str = DEFAULT_OCR_CACHE_DIR) -> str:
    """OCR 结果缓存路径：``<cache_dir>/<image_sha1>.txt``。

    命中即跳过识别，是「一轮 extract 从十几分钟降到约 1 秒」的唯一原因。
    """
    return f"{cache_dir.rstrip('/')}/{image_sha1}.txt"


def normalize_missing(value: Any, placeholder: str = MISSING) -> str:
    """把空值统一折叠为「未知」。

    ``None`` / 空串 / 纯空白 / ``-`` / ``—`` / ``无`` / ``N/A`` / ``null`` 都视为缺失。
    """
    if value is None:
        return placeholder
    text = str(value).strip()
    if not text or text in {"-", "—", "–", "－", "无", "N/A", "n/a", "NA", "null", "None"}:
        return placeholder
    return text


def is_missing(value: Any) -> bool:
    """判断某字段值是否表示缺失（与 normalize_missing 口径一致）。"""
    return normalize_missing(value) == MISSING


# --------------------------------------------------------------------------
# 异常：模块边界只允许抛出 PipelineError 的子类
# --------------------------------------------------------------------------


class PipelineError(Exception):
    """所有可预期错误的基类。

    约定：模块内部异常（requests 异常、解析异常、sqlite 异常等）**不得**穿过
    模块边界；实现方必须在边界处捕获并转换为本类子类，或转换为 DTO 中的
    ``error`` 字段。pipeline 阶段捕获本类并记入 StageResult.errors，默认不中断整批。
    """

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str, *, detail: str = "", url: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.url = url

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code.value, "message": self.message, "detail": self.detail, "url": self.url}

    def __str__(self) -> str:  # pragma: no cover - 便于日志阅读
        parts = [f"[{self.code.value}] {self.message}"]
        if self.url:
            parts.append(f"url={self.url}")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)


class ConfigError(PipelineError):
    code = ErrorCode.CONFIG_INVALID


class LoginError(PipelineError):
    code = ErrorCode.LOGIN_REQUIRED


class FetchError(PipelineError):
    code = ErrorCode.HTTP_ERROR


class ParseError(PipelineError):
    code = ErrorCode.PARSE_ERROR


class ExtractError(PipelineError):
    code = ErrorCode.EXTRACT_INCOMPLETE


class LlmError(PipelineError):
    code = ErrorCode.LLM_UNAVAILABLE


class OcrError(PipelineError):
    code = ErrorCode.OCR_UNAVAILABLE


class StorageError(PipelineError):
    code = ErrorCode.STORAGE_ERROR


# --------------------------------------------------------------------------
# DTO：跨模块传递的唯一数据形态
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Diagnostic:
    """结构化告警/错误项，供 Issue 与 StageResult 复用。"""

    code: ErrorCode
    message: str
    severity: Severity = Severity.ERROR
    field_name: str = ""
    article_key: str = ""


@dataclass(frozen=True)
class ArticleRef:
    """列表页解析产物 → 详情页采集入参（列表层与采集层之间唯一的输入类型）。

    ``detail_url`` 必填且必须是非空字符串；``article_key`` 与 ``raw_html_path``
    由 detail_url 推导，不单独存储，保证同一 URL 在任何一层得到同一结果。
    """

    detail_url: str
    list_url: str = ""
    title: str = ""
    publish_date: str = ""
    content_url: str = ""
    """正文实际所在 URL：通知指向站外（微信公众号 / 腾讯文档）时与 ``detail_url`` 不同。

    为空表示正文就在门户详情页上。
    """

    def __post_init__(self) -> None:
        if not str(self.detail_url or "").strip():
            raise ValueError("ArticleRef.detail_url 不能为空")

    @property
    def fetch_url(self) -> str:
        """采集层应当抓取的 URL：优先站外正文，其次门户详情页。"""
        return str(self.content_url or "").strip() or self.detail_url

    @property
    def is_external(self) -> bool:
        return self.fetch_url != self.detail_url

    @property
    def article_key(self) -> str:
        return article_key_of(self.detail_url)

    def raw_html_path(self, raw_dir: str = DEFAULT_RAW_HTML_DIR) -> str:
        return raw_html_relpath(self.detail_url, raw_dir)


@dataclass(frozen=True)
class RawArticle:
    """详情页采集产物 → 清洗/解析入参。

    ``html`` 是已解码的页面原文；采集失败时 ``html=""`` 且 ``error`` 非空，
    此时该记录不得进入解析层（由 pipeline 按 ``ok`` 过滤）。
    """

    ref: ArticleRef
    html: str = ""
    html_path: str = ""
    status_code: int = 0
    fetched_at: datetime = field(default_factory=now)
    error: str = ""
    images: Tuple["ImageAsset", ...] = ()

    @property
    def ok(self) -> bool:
        """采集是否成功：状态码 2xx、正文非空、无错误。"""
        return 200 <= self.status_code < 300 and bool(self.html) and not self.error

    @property
    def article_key(self) -> str:
        return self.ref.article_key

    @classmethod
    def success(
        cls,
        ref: ArticleRef,
        html: str,
        html_path: str,
        status_code: int = 200,
        images: Tuple["ImageAsset", ...] = (),
    ) -> "RawArticle":
        return cls(
            ref=ref,
            html=html,
            html_path=html_path,
            status_code=status_code,
            fetched_at=now(),
            images=tuple(images),
        )

    @classmethod
    def failure(cls, ref: ArticleRef, status_code: int, error: str) -> "RawArticle":
        return cls(ref=ref, status_code=status_code, fetched_at=now(), error=error)


@dataclass(frozen=True)
class HttpResponse:
    """传输层返回值（session.py → list_page/detail_page）。

    传输层只负责「把 URL 变成内容」，不做任何 HTML 解析。

    文本与二进制共用同一个结构：页面填 ``text``，图片等二进制资源填 ``content``
    （``binary=True`` 请求时不做文本解码，避免二进制被破坏）。
    """

    url: str
    status_code: int = 0
    text: str = ""
    content: bytes = b""
    final_url: str = ""
    elapsed_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        """成功判定：2xx 且无错误，且**文本或字节二者之一非空**。"""
        return 200 <= self.status_code < 300 and not self.error and (bool(self.text) or bool(self.content))

    @property
    def size(self) -> int:
        return len(self.content) if self.content else len(self.text)

    @property
    def redirected(self) -> bool:
        """是否发生了重定向（final_url 与请求 URL 不同）。

        真实门户上最常见的重定向就是「未登录 → 弹回统一身份认证」，
        因此它是比 HTML 特征串更可靠的登录态判据。
        """
        return bool(self.final_url) and self.final_url != self.url


@dataclass(frozen=True)
class LoginStatus:
    """登录自检结果（login_check.py → 命令行输出 / pipeline 前置门禁）。"""

    authenticated: bool
    method: str = "account"
    probe_url: str = ""
    final_url: str = ""
    message: str = ""
    checked_at: datetime = field(default_factory=now)

    def require(self) -> None:
        """未登录时抛 LoginError（附探测 URL 与原因，不回显凭据）。"""
        if not self.authenticated:
            raise LoginError(self.message or "登录态无效，请检查凭据或 Cookie 是否已过期", url=self.probe_url)


@dataclass(frozen=True)
class CleanArticle:
    """清洗产物 → 抽取入参（解析层内部流转，但作为接口类型固定下来便于单测）。

    ``segments`` 是带语义的分段文本，键取自 SEGMENT_KEYS；抽取规则按段取词，
    避免在整篇正文上做无差别正则。
    """

    ref: ArticleRef
    title: str = ""
    text: str = ""
    segments: Mapping[str, str] = field(default_factory=dict)
    source_kind: str = ContentKind.HTML.value
    cleaned_at: datetime = field(default_factory=now)

    @property
    def article_key(self) -> str:
        return self.ref.article_key


SEGMENT_KEYS: Tuple[str, ...] = ("headline", "meta", "body", "tail", "raw")
"""CleanArticle.segments 的合法键：标题 / 元信息 / 正文 / 结尾 / 兜底全文。"""


@dataclass(frozen=True)
class ImageAsset:
    """图片资产：正文以图片形式发布的文章，其内容以「图片」为单位被追踪。

    为什么需要它：门户上大量就业分享是截图/长图，正文不在 HTML 里而在图里。
    于是「内容从哪来」必须可追踪：

      * ``sha1`` 是**图片内容哈希**，同时充当去重键与 OCR 缓存键；
      * ``image_path`` 与 ``ocr_cache_path`` 都由内容决定，可重建、可幂等；
      * ``ocr_status`` 记录识别状态，失败如实记录并进待人工清单。
    """

    source_url: str
    sha1: str
    image_path: str = ""
    byte_size: int = 0
    ocr_cache_path: str = ""
    ocr_status: str = OcrStatus.PENDING.value

    def __post_init__(self) -> None:
        if not str(self.source_url or "").strip():
            raise ValueError("ImageAsset.source_url 不能为空")
        if len(str(self.sha1 or "")) < 8:
            raise ValueError("ImageAsset.sha1 必须是图片内容的哈希（至少 8 位）")

    @property
    def ocr_ready(self) -> bool:
        """是否已有可用的 OCR 结果（命中缓存或已识别）。"""
        return self.ocr_status == OcrStatus.DONE.value and bool(self.ocr_cache_path)

    def with_ocr(self, status: OcrStatus, cache_path: str = "") -> "ImageAsset":
        """返回带上识别状态的新资产（保持不可变）。"""
        return replace(self, ocr_status=status.value, ocr_cache_path=cache_path or self.ocr_cache_path)

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        source_url: str,
        detail_url: str,
        suffix: str = ".png",
        images_dir: str = DEFAULT_IMAGE_DIR,
        cache_dir: str = DEFAULT_OCR_CACHE_DIR,
    ) -> "ImageAsset":
        """由图片字节构造：哈希、归档路径、缓存路径一次算好，保证三层同源。"""
        digest = sha1_of_bytes(data)
        return cls(
            source_url=source_url,
            sha1=digest,
            image_path=archived_image_relpath(detail_url, digest, suffix, images_dir),
            byte_size=len(data),
            ocr_cache_path=ocr_cache_relpath(digest, cache_dir),
        )


@dataclass(frozen=True)
class ManifestEntry:
    """采集清单条目：fetch 阶段的产物凭证，也是 extract 阶段的输入。

    为什么需要它：归档文件名是 ``sha1(detail_url)``，**不可逆**。
    因此「URL ↔ 归档文件」的对应关系必须由清单显式记录，
    extract 阶段才能在离线状态下重建 ``RawArticle``（不必重新联网）。

    格式：每行一个 JSON 对象，字段固定为下面这些（新增字段需递增 SCHEMA_VERSION）。
    """

    ref: ArticleRef
    html_path: str
    status_code: int = 200
    crawl_time: str = ""
    error: str = ""
    images: Tuple["ImageAsset", ...] = ()

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 and bool(self.html_path) and not self.error

    @classmethod
    def from_raw(cls, raw: "RawArticle") -> "ManifestEntry":
        return cls(
            ref=raw.ref,
            html_path=raw.html_path,
            status_code=raw.status_code,
            crawl_time=raw.fetched_at.replace(microsecond=0).isoformat(),
            error=raw.error,
            images=tuple(raw.images),
        )

    def to_line(self) -> str:
        """序列化为一行 JSON（紧凑、UTF-8、不转义中文）。"""
        payload = {
            "detail_url": self.ref.detail_url,
            "list_url": self.ref.list_url,
            "title": self.ref.title,
            "publish_date": self.ref.publish_date,
            "content_url": self.ref.content_url,
            "html_path": self.html_path,
            "status_code": self.status_code,
            "crawl_time": self.crawl_time,
            "error": self.error,
            "images": [
                {
                    "source_url": asset.source_url,
                    "sha1": asset.sha1,
                    "image_path": asset.image_path,
                    "byte_size": asset.byte_size,
                    "ocr_cache_path": asset.ocr_cache_path,
                    "ocr_status": asset.ocr_status,
                }
                for asset in self.images
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_line(cls, line: str) -> Optional["ManifestEntry"]:
        """反序列化；空行或损坏行返回 ``None``（调用方跳过并计数，不抛异常）。"""
        text = (line or "").strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, Mapping) or not payload.get("detail_url"):
            return None
        ref = ArticleRef(
            detail_url=str(payload["detail_url"]),
            list_url=str(payload.get("list_url", "")),
            title=str(payload.get("title", "")),
            publish_date=str(payload.get("publish_date", "")),
            content_url=str(payload.get("content_url", "")),
        )
        assets: List[ImageAsset] = []
        for item in payload.get("images") or []:
            if not isinstance(item, Mapping) or not item.get("sha1"):
                continue
            assets.append(
                ImageAsset(
                    source_url=str(item.get("source_url", "")),
                    sha1=str(item["sha1"]),
                    image_path=str(item.get("image_path", "")),
                    byte_size=int(item.get("byte_size", 0)),
                    ocr_cache_path=str(item.get("ocr_cache_path", "")),
                    ocr_status=str(item.get("ocr_status", OcrStatus.PENDING.value)),
                )
            )
        return cls(
            ref=ref,
            html_path=str(payload.get("html_path", "")),
            status_code=int(payload.get("status_code", 0)),
            crawl_time=str(payload.get("crawl_time", "")),
            error=str(payload.get("error", "")),
            images=tuple(assets),
        )


@dataclass(frozen=True)
class FieldHit:
    """单字段命中结果：值 + 方式 + 依据原文片段。

    一级（规则）与二级（LLM）抽取都必须返回本类型，这是「可解释、可溯源」的最小单位。
    """

    field_name: str
    value: str
    method: ExtractMethod
    evidence: str = ""
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.field_name not in CORE_FIELDS:
            raise ValueError(f"FieldHit.field_name 必须是七项核心字段之一，收到 {self.field_name!r}")
        if not str(self.value or "").strip():
            raise ValueError("FieldHit.value 不能为空；缺失字段不要产生 FieldHit")


@dataclass
class JobRecord:
    """**跨模块传递的唯一记录类型**（抽取层产出 → 校验 → 存储 → 导出）。

    字段口径：
      * 七项核心字段默认值为 ``MISSING``（「未知」），不允许空串；
      * 溯源字段由抽取/存储两阶段补全，导出时必须完整；
      * ``hits`` 保存每个字段的 FieldHit，用于生成 evidence 与统计命中率。
    """

    # ---- 七项核心字段（顺序与 CORE_FIELDS 一致）----
    graduation_year: str = MISSING
    grade: str = MISSING
    degree: str = MISSING
    major: str = MISSING
    city: str = MISSING
    employer: str = MISSING
    position: str = MISSING

    # ---- 溯源字段 ----
    article_title: str = ""
    publish_date: str = ""
    source_url: str = ""
    list_url: str = ""
    raw_html_path: str = ""
    crawl_time: str = ""
    extract_method: str = ExtractMethod.NONE.value
    review_status: str = ReviewStatus.PENDING.value
    content_kind: str = ContentKind.HTML.value

    # ---- 命中依据（由 apply_hits 汇总；入库后可直接读回，便于人工复核）----
    evidence_text: str = ""

    # ---- 依据（不导出为独立列，但可展开写入 evidence 汇总列）----
    hits: Dict[str, FieldHit] = field(default_factory=dict)

    # ---------- 查询 ----------

    @property
    def article_key(self) -> str:
        return article_key_of(self.source_url) if self.source_url else ""

    def field_value(self, field_name: str) -> str:
        if field_name not in CORE_FIELDS:
            raise KeyError(f"未知核心字段：{field_name!r}")
        return normalize_missing(getattr(self, field_name))

    def unknown_fields(self) -> Tuple[str, ...]:
        """返回仍为「未知」的核心字段。"""
        return tuple(f for f in CORE_FIELDS if self.field_value(f) == MISSING)

    def is_complete(self) -> bool:
        return not self.unknown_fields()

    def needs_manual_review(self) -> bool:
        """只要存在未知字段，就必须进待人工清单（口径固定，不允许各层自行判断）。"""
        return bool(self.unknown_fields())

    def evidence(self) -> str:
        """返回命中依据字符串（导出报表的 evidence 列）。

        优先返回已落库的 ``evidence_text``（从数据库读回时用它，保证报表可离线复现）；
        若为空则用当前内存中的 hits 现场拼接。
        """
        return self.evidence_text or self._compose_evidence()

    def _compose_evidence(self) -> str:
        """由 hits 现场拼接依据（纯函数，不依赖 DB）。"""
        parts = []
        for name in CORE_FIELDS:
            hit = self.hits.get(name)
            if hit and hit.evidence:
                parts.append(f"{name}={hit.value} ← {hit.evidence}")
        return " | ".join(parts)

    # ---------- 写入 ----------

    def apply_hits(self, hits: Mapping[str, FieldHit]) -> "JobRecord":
        """用一批 FieldHit 更新记录，并统一归并 extract_method。

        归并规则（全项目唯一实现，禁止各层重复判断）：
          * 有规则命中且无 LLM 命中 → ``rule``
          * 只有 LLM 命中            → ``llm``
          * 两者都有                → ``hybrid``
          * 都没有                  → 保持原值（不覆盖）

        注意：归并是**累计**语义——判定基于「记录中已有的全部命中 + 本次传入的命中」，
        因此先调 ``apply_hits(规则命中)`` 再调 ``apply_hits(LLM 命中)`` 必须得到
        ``hybrid``，而不是被后者覆盖成 ``llm``。

        仅当命中值非缺失时才覆盖已有字段值。
        """
        if not hits:
            return self
        methods: Set[ExtractMethod] = {hit.method for hit in self.hits.values()}
        for name, hit in hits.items():
            if name not in CORE_FIELDS:
                raise KeyError(f"apply_hits 收到非法字段：{name!r}")
            if is_missing(hit.value):
                continue
            self.hits[name] = hit
            setattr(self, name, hit.value)
            methods.add(hit.method)
        if methods:
            self.extract_method = _merge_methods(methods).value
        self.evidence_text = self._compose_evidence()
        return self

    def to_row(self, columns: Sequence[str]) -> Dict[str, str]:
        """按给定列序（通常来自 config/fields.yaml）导出为字符串行。

        未在本记录中定义的列输出空串，保证 CSV/XLSX 列序稳定、不漂移。
        """
        row: Dict[str, str] = {}
        for column in columns:
            if column == "evidence":
                row[column] = self.evidence()
                continue
            if column == "article_key":
                row[column] = self.article_key
                continue
            value = getattr(self, column, "")
            row[column] = "" if value is None else str(value)
        return row

    @classmethod
    def empty(cls, ref: ArticleRef, raw_html_path: str = "") -> "JobRecord":
        """按 ArticleRef 建立一条空白记录（所有溯源信息已就位，字段为「未知」）。"""
        return cls(
            source_url=ref.detail_url,
            list_url=ref.list_url,
            article_title=ref.title,
            publish_date=ref.publish_date,
            raw_html_path=raw_html_path or ref.raw_html_path(),
            crawl_time=now_iso(),
        )


def _merge_methods(methods: Iterable[ExtractMethod]) -> ExtractMethod:
    """extract_method 归并规则的唯一实现（供 JobRecord.apply_hits 调用）。"""
    unique = set(methods)
    if not unique:
        return ExtractMethod.NONE
    has_rule = ExtractMethod.RULE in unique
    has_llm = ExtractMethod.LLM in unique
    if has_rule and has_llm:
        return ExtractMethod.HYBRID
    if has_llm:
        return ExtractMethod.LLM
    return ExtractMethod.RULE


@dataclass(frozen=True)
class ValidationIssue:
    """一条校验问题。field_name 为空表示记录级问题（如重复）。"""

    code: ErrorCode
    message: str
    severity: Severity = Severity.ERROR
    field_name: str = ""


@dataclass(frozen=True)
class ValidationResult:
    """校验层 → 存储层的唯一结果类型。"""

    record: JobRecord
    ok: bool
    issues: Tuple[ValidationIssue, ...] = ()

    @property
    def needs_manual_review(self) -> bool:
        """进待人工清单的三个条件（口径唯一，各层不得自行判定）：

        1. 校验不通过（存在 ERROR 级问题）；
        2. 存在「未知」核心字段（七项没抽全）；
        3. 内容来自图片 OCR —— 合规红线 5 要求 OCR 结果一律人工复核。
        """
        if (not self.ok) or self.record.needs_manual_review():
            return True
        return any(issue.code is ErrorCode.OCR_NEEDS_REVIEW for issue in self.issues)

    @property
    def error_messages(self) -> Tuple[str, ...]:
        return tuple(i.message for i in self.issues if i.severity is Severity.ERROR)


@dataclass
class StageResult:
    """每个阶段（fetch / extract / export）的统一返回值。

    字段固定：pipeline 与 run.py 只依赖本结构，不解析各阶段的自定义输出。
    counters 的键必须取自 COUNTER_KEYS，越界即报错（由 add 强制）。
    """

    stage: Stage
    ok: bool = True
    started_at: datetime = field(default_factory=now)
    finished_at: Optional[datetime] = None
    counters: Dict[str, int] = field(default_factory=dict)
    artifacts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @classmethod
    def start(cls, stage: Stage) -> "StageResult":
        return cls(stage=stage, started_at=now())

    def add(self, key: str, count: int = 1) -> "StageResult":
        """累加计数；key 必须是 COUNTER_KEYS 中的合法项。"""
        if key not in COUNTER_KEYS:
            raise KeyError(f"非法计数键 {key!r}；如需新增请先更新 contracts.COUNTER_KEYS")
        self.counters[key] = self.counters.get(key, 0) + count
        return self

    def note(self, diagnostic: Diagnostic) -> "StageResult":
        """记录一条结构化诊断；ERROR 级别的诊断会同时置 ok=False。"""
        self.diagnostics.append(diagnostic)
        if diagnostic.severity is Severity.ERROR:
            self.ok = False
        return self

    def fail(self, message: str, code: ErrorCode = ErrorCode.INTERNAL) -> "StageResult":
        """记录错误信息并标记阶段失败。"""
        self.errors.append(message)
        self.diagnostics.append(Diagnostic(code=code, message=message))
        self.ok = False
        return self

    def finish(self) -> "StageResult":
        self.finished_at = now()
        return self

    def to_dict(self) -> Dict[str, Any]:
        """供日志与 run.py 输出（保持稳定，便于外部脚本消费）。"""
        return {
            "stage": self.stage.value,
            "ok": self.ok,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else "",
            "counters": dict(sorted(self.counters.items())),
            "artifacts": list(self.artifacts),
            "errors": list(self.errors),
            "diagnostics": [
                {
                    "code": d.code.value,
                    "message": d.message,
                    "severity": d.severity.value,
                    "field_name": d.field_name,
                    "article_key": d.article_key,
                }
                for d in self.diagnostics
            ],
        }


# --------------------------------------------------------------------------
# Protocol：模块之间的「插座」，实现方必须逐一对上
# --------------------------------------------------------------------------


@runtime_checkable
class Transport(Protocol):
    """传输层接口，由 ``src/crawler/session.py`` 实现。

    职责边界：只负责 URL → HttpResponse（限速 ≥2s、重试、UA、Cookie、可选代理）。
    禁止在实现中解析 HTML，禁止直接读配置文件（配置由构造函数注入）。

    ``binary=True`` 用于图片等二进制资源：实现方只填 ``HttpResponse.content``，
    不尝试文本解码（否则图片字节会被破坏）。
    """

    def get(self, url: str, *, referer: str = "", binary: bool = False) -> HttpResponse:
        ...

    def post_json(
        self, url: str, payload: Mapping[str, Any], *, referer: str = ""
    ) -> HttpResponse:
        """以 JSON 形式 POST 并返回响应（门户列表接口的通用形态）。

        约定与 ``get`` 一致：响应文本放在 ``HttpResponse.text``；
        非 2xx **不抛异常**，由调用方按 ``ok`` 判定。
        实现方必须复用同一会话（Cookie 共享），否则登录态会丢。
        """
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class PageFetcher(Protocol):
    """采集层对外接口，由 ``src/crawler/crawler.py`` 实现（pipeline 只认这个）。

    幂等约定：同一 detail_url 重复 fetch 必须得到相同的 html_path 并覆盖写入，
    已归档的 URL 由 pipeline 通过 Repository.existing_source_urls() 跳过。
    """

    def fetch_list(self, page: int) -> List[ArticleRef]:
        ...

    def fetch_detail(self, ref: ArticleRef) -> RawArticle:
        ...

    def fetch_image(self, url: str, ref: ArticleRef) -> Optional["ImageAsset"]:
        """抓取并归档一张图片（按内容哈希命名，幂等覆盖）。

        失败返回 ``None``（由调用方计入 ``images_failed``），不得抛异常中断整篇采集。
        """
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class ArticleExtractor(Protocol):
    """解析层对外接口，由 ``src/parser/extractor.py`` 实现。

    职责边界：输入 RawArticle + CleanArticle，输出 JobRecord；
    必须完成七项字段的两级抽取与溯源字段填充，不得返回裸 dict。
    """

    def extract(self, raw: RawArticle, clean: CleanArticle) -> JobRecord:
        ...


@runtime_checkable
class RecordValidator(Protocol):
    """校验层对外接口，由 ``src/validation/validator.py`` 实现。

    职责边界：只做校验与判定，不修改入库结果、不写文件。
    """

    def validate(self, record: JobRecord) -> ValidationResult:
        ...


@runtime_checkable
class RecordRepository(Protocol):
    """存储层对外接口，由 ``src/storage/database.py`` 实现。

    幂等约定：
      * ``upsert_many`` 以 source_url 为唯一键，重复写入为更新而非新增；
      * ``export_*`` 全量重建目标文件，重复执行结果一致。
    """

    def init_schema(self) -> None:
        ...

    def existing_source_urls(self) -> Set[str]:
        ...

    def upsert_many(self, records: Iterable[JobRecord]) -> int:
        ...

    def fetch_all(self) -> List[JobRecord]:
        ...

    def export_csv(self) -> int:
        ...

    def export_xlsx(self) -> int:
        ...

    def write_manual_review(self, records: Iterable[JobRecord]) -> int:
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class StageRunner(Protocol):
    """编排接口，由 ``src/pipeline/pipeline.py`` 实现（run.py 只调用它）。"""

    def run(self, stage: Stage) -> StageResult:
        ...
