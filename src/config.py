"""配置加载与强类型化（第 1 层，Layer 1）。

**唯一**允许读取 ``config/config.yaml``、``config/fields.yaml`` 与 ``.env`` 的模块。
其他模块一律通过函数参数接收 ``AppConfig``（依赖注入），禁止自行读文件、禁止
使用模块级全局配置对象——这是「接口可控」的前提。

职责
----
1. 把 ``config/config.yaml`` + ``config/fields.yaml`` + ``.env`` 合成一个不可变的
   ``AppConfig``；
2. 在加载阶段完成**所有可判定的校验**（合规红线、枚举取值、区间、字段口径），
   把配置错误挡在管线启动之前，而不是运行到一半才炸；
3. 提供 ``AppConfig.redacted()``，保证日志/异常里永不出现密码与 API Key。

``.env`` 变量到配置项的映射（.env 优先级最高）::

    PORTAL_BASE_URL    -> portal.base_url
    AUTH_STUDENT_ID    -> auth.student_id       （本人学号）
    AUTH_PASSWORD      -> auth.password         （本人密码，禁止写入 yaml）
    AUTH_COOKIE        -> auth.cookie           （手动 Cookie 登录时使用）
    CRAWLER_PROXY      -> request.proxy
    LLM_ENABLED        -> llm.enabled
    LLM_API_KEY        -> llm.api_key           （禁止写入 yaml）
    LLM_BASE_URL       -> llm.base_url
    LLM_MODEL          -> llm.model
    OCR_ENABLED        -> ocr.enabled
    TESSERACT_CMD      -> ocr.tesseract_cmd
    DB_PATH            -> storage.db_path
    LOG_LEVEL          -> logging.level
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from src import contracts
from src.contracts import (
    CORE_FIELDS,
    DEFAULT_CSV_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_IMAGE_DIR,
    DEFAULT_MANUAL_REVIEW_PATH,
    DEFAULT_OCR_CACHE_DIR,
    DEFAULT_RAW_HTML_DIR,
    DEFAULT_XLSX_PATH,
    MISSING,
    ConfigError,
    LoginError,
)

PathLike = Union[str, Path]

CONFIG_RELATIVE_PATH = "config/config.yaml"
FIELDS_RELATIVE_PATH = "config/fields.yaml"
ENV_RELATIVE_PATH = ".env"

AUTH_METHODS: Tuple[str, ...] = ("account", "cookie")
LLM_PROVIDERS: Tuple[str, ...] = ("openai", "ollama")

MIN_REQUEST_INTERVAL = 2.0
"""合规红线：请求间隔下限（秒）。任何配置都不允许低于该值。"""


# --------------------------------------------------------------------------
# 字段口径
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """``config/fields.yaml`` 中单条字段定义（列序、中文名、必填、归一化动作）。"""

    name: str
    label: str
    type: str = "str"
    required: bool = False
    normalize: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()

    @property
    def is_core(self) -> bool:
        return self.name in CORE_FIELDS


# --------------------------------------------------------------------------
# 各配置段
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PortalConfig:
    base_url: str = ""
    list_url: str = ""
    page_param: str = "page"
    page_start: int = 1
    page_end: int = 1
    detail_link_selector: str = ""
    login_url: str = ""
    login_check_url: str = ""

    # ---------- 列表采集模式 ----------
    mode: str = "html"
    """``html`` = 解析列表页 DOM；``api`` = POST JSON 接口（实测本校门户属于后者）。"""

    # 接口模式的字段名与取值路径（全部可配，门户改版不必改代码）
    api_url: str = ""
    api_body: Dict[str, Any] = field(default_factory=dict)
    api_param_style: str = "query"
    """参数提交方式：``query``（查询串，实测本校门户只认这个）/ ``json``（请求体）。

    实测：把参数放 JSON 体里时服务端**完全不解析**——回显的 currentPage 恒为 1、pageSize 恒为 10，
    连栏目过滤 type 都不生效（total 44944 而非 1040）；改用查询串后一切正常。
    """
    api_page_field: str = "currentPage"
    api_token_field: str = "comsys_random_t"
    api_token_style: str = "js_random"
    """令牌格式：``js_random``（等价于浏览器 Math.random()，实测值形如 0.9887865334032206）
    或 ``hex32``。"""

    api_token_length: int = 32
    api_list_path: str = "data.records"
    """响应里**列表数组**的路径；填错时会自动识别常见命名，并在日志里提示。"""
    api_total_path: str = "page.total"
    api_total_pages_path: str = "page.totalCounts"
    """记录总数与**总页数**（实测 totalCounts=70 而 total=1040、pageSize=15 → 70 页）。"""
    api_title_field: str = "title"
    api_date_field: str = "publishTime"

    # 站外链接（实测：就业信息里大量通知指向微信公众号 / 腾讯文档）
    api_link_field: str = "notice_link"
    api_link_state_field: str = "notice_link_state"
    api_link_state_external: int = 1
    external_link_policy: str = "fetch"
    """站外通知怎么处理：

    * ``fetch``  —— 抓站外链接当正文（微信公众号/腾讯文档等，需 playwright）；
    * ``portal`` —— 只抓门户详情页（站外链接仅记录，不抓取）；
    * ``skip``   —— 直接跳过站外通知。

    **幂等键始终取门户详情页 URL**，与选哪种策略无关。
    """

    detail_org_id: str = ""
    """组织 ID 兜底值：列表项不含 organization_id 时用它拼详情链接（实测 orgId）。"""

    # 详情链接模板：用接口记录里的字段做占位，如
    # "https://my.muc.edu.cn/page/11#/notice/noticeDetail?notice_id={notice_id}&organization_id={organization_id}"
    detail_url_template: str = ""


@dataclass(frozen=True)
class AuthConfig:
    method: str = "account"
    student_id: str = ""
    password: str = ""
    cookie: str = ""
    session_file: str = "data/.session.json"

    def credentials_present(self) -> bool:
        if self.method == "cookie":
            return bool(self.cookie.strip())
        return bool(self.student_id.strip()) and bool(self.password.strip())


@dataclass(frozen=True)
class RequestConfig:
    interval_seconds: float = MIN_REQUEST_INTERVAL
    timeout: float = 20.0
    retries: int = 2
    user_agent: str = "Mozilla/5.0 (compatible; EmploymentPipeline/1.0)"
    proxy: str = ""
    use_playwright: bool = False
    verify_tls: bool = True
    encoding: str = ""


@dataclass(frozen=True)
class ExtractConfig:
    rule_first: bool = True
    missing_placeholder: str = MISSING
    manual_review_output: str = DEFAULT_MANUAL_REVIEW_PATH
    raw_html_dir: str = DEFAULT_RAW_HTML_DIR
    regex_file: str = ""
    lexicon_file: str = "config/aliases.yaml"
    ocr_images_dir: str = DEFAULT_IMAGE_DIR
    ocr_cache_dir: str = DEFAULT_OCR_CACHE_DIR
    llm_trigger_below: int = len(CORE_FIELDS)


@dataclass(frozen=True)
class LlmConfig:
    enabled: bool = False
    provider: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 60.0
    max_retries: int = 1
    temperature: float = 0.0


@dataclass(frozen=True)
class OcrConfig:
    enabled: bool = False
    lang: str = "chi_sim+eng"
    tesseract_cmd: str = ""


@dataclass(frozen=True)
class StorageConfig:
    db_path: str = DEFAULT_DB_PATH
    batch_size: int = 200


@dataclass(frozen=True)
class SearchConfig:
    """检索与「有倾向性采集」配置（SCHEMA_VERSION 1.5.0 新增）。

    设计取舍：``trigger_enabled`` **默认 False**。检索本身完全离线，
    但"缓存未命中就联网采集"会消耗真实请求，属于必须显式开启的动作；
    默认关闭可保证「装好就能搜、绝不偷偷发请求」。
    """

    enabled: bool = True
    """是否启用 Web 检索服务（关闭后 ``python -m src.search.server`` 拒绝启动）。"""

    host: str = "127.0.0.1"
    """**只能绑本机回环地址。** 该服务会返回真实抓取内容，不得对外监听。"""

    port: int = 8765

    default_limit: int = 20
    max_limit: int = 100
    """单页上限：防止前端构造 limit=100000 把整个库拖进内存。"""

    cache_ttl_seconds: int = 300
    """L2 查询缓存有效期（秒）。数据版本号变化会立即失效，不依赖这个 TTL。"""

    cache_max_rows: int = 2000
    """L2 缓存最多保留多少条查询（LRU 淘汰，防止无限增长）。"""

    l1_size: int = 128
    """L1 进程内缓存条数。"""

    trigger_enabled: bool = False
    """缓存未命中时是否允许触发定向采集（联网）。"""

    max_fetch_pages: int = 2
    """单次采集最多翻几页。每页 15 条 = 15 次详情请求，按 ≥2 秒计约 32 秒。"""

    fetch_cooldown_seconds: int = 900
    """同一查询串的采集冷却时间（秒），防止重复点击把请求放大。"""

    max_estimated_seconds: float = 120.0
    """预估耗时超过该值就拒绝触发（预算闸门）。"""

    fts_tokenizer: str = "unicode61"
    """FTS5 分词器。``trigram`` 让中文无需分词即可子串匹配（SQLite 内置，无新依赖）。"""


@dataclass(frozen=True)
class OutputConfig:
    csv_path: str = DEFAULT_CSV_PATH
    xlsx_path: str = DEFAULT_XLSX_PATH


@dataclass(frozen=True)
class SamplingConfig:
    review_rate: float = 0.1
    seed: int = 42


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file_name: str = "pipeline.log"
    console: bool = True


@dataclass(frozen=True)
class AppConfig:
    """全项目唯一的配置对象，由 pipeline 构造并注入各模块。

    ``columns`` 是导出列序（来自 config/fields.yaml 的顺序），存储层的 CSV/XLSX
    表头必须直接使用它，不得自行排序或增删。
    """

    project_root: Path
    config_path: Path
    fields: Tuple[FieldSpec, ...]
    columns: Tuple[str, ...]
    portal: PortalConfig
    auth: AuthConfig
    request: RequestConfig
    extract: ExtractConfig
    llm: LlmConfig
    ocr: OcrConfig
    storage: StorageConfig
    search: SearchConfig
    output: OutputConfig
    sampling: SamplingConfig
    logging: LoggingConfig
    lexicon: Mapping[str, Mapping[str, Tuple[str, ...]]] = field(default_factory=dict)

    # ---------- 派生访问器（各模块唯一允许的路径来源）----------

    def path(self, relative: str) -> Path:
        """把配置里的相对路径解析为绝对路径（相对项目根）。"""
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else (self.project_root / candidate)

    @property
    def raw_html_dir(self) -> str:
        return self.extract.raw_html_dir

    @property
    def core_fields(self) -> Tuple[str, ...]:
        """按 CORE_FIELDS 顺序返回导出列中出现的核心字段（缺失即配置错误）。"""
        missing = [f for f in CORE_FIELDS if f not in self.columns]
        if missing:
            raise ConfigError(f"config/fields.yaml 缺少核心字段：{missing}")
        return CORE_FIELDS

    @property
    def required_columns(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    def require_auth(self) -> None:
        """采集阶段调用：凭据不足时抛 LoginError（提示缺哪一项，不回显值）。"""
        if self.auth.credentials_present():
            return
        if self.auth.method == "cookie":
            raise LoginError("缺少手动 Cookie：请在 .env 中填写 AUTH_COOKIE")
        missing_keys = []
        if not self.auth.student_id.strip():
            missing_keys.append("AUTH_STUDENT_ID")
        if not self.auth.password.strip():
            missing_keys.append("AUTH_PASSWORD")
        raise LoginError(f"缺少登录凭据：请在 .env 中填写 {', '.join(missing_keys)}")

    def require_llm(self) -> None:
        """二级抽取调用：LLM 已启用但配置不全时抛 ConfigError。"""
        if not self.llm.enabled:
            return
        if self.llm.provider == "openai" and not self.llm.api_key.strip():
            raise ConfigError("llm.enabled=true 且 provider=openai 时必须在 .env 中填写 LLM_API_KEY")
        if not self.llm.base_url.strip():
            raise ConfigError("llm.enabled=true 时必须填写 llm.base_url")
        if not self.llm.model.strip():
            raise ConfigError("llm.enabled=true 时必须填写 llm.model")

    def redacted(self) -> Dict[str, Any]:
        """配置摘要（凭据打码），用于启动日志与错误报告。"""
        return {
            "schema_version": contracts.SCHEMA_VERSION,
            "project_root": str(self.project_root),
            "config_path": str(self.config_path),
            "portal": {"base_url": self.portal.base_url, "pages": f"{self.portal.page_start}-{self.portal.page_end}"},
            "auth": {
                "method": self.auth.method,
                "student_id": _mask_secret(self.auth.student_id),
                "password": _mask_secret(self.auth.password),
                "cookie": _mask_secret(self.auth.cookie),
            },
            "request": {
                "interval_seconds": self.request.interval_seconds,
                "timeout": self.request.timeout,
                "retries": self.request.retries,
                "use_playwright": self.request.use_playwright,
                "proxy": bool(self.request.proxy),
            },
            "extract": {
                "rule_first": self.extract.rule_first,
                "missing_placeholder": self.extract.missing_placeholder,
                "llm_trigger_below": self.extract.llm_trigger_below,
                "ocr_images_dir": self.extract.ocr_images_dir,
                "ocr_cache_dir": self.extract.ocr_cache_dir,
            },
            "llm": {
                "enabled": self.llm.enabled,
                "provider": self.llm.provider,
                "base_url": self.llm.base_url,
                "model": self.llm.model,
                "api_key": _mask_secret(self.llm.api_key),
            },
            "ocr": {"enabled": self.ocr.enabled, "lang": self.ocr.lang},
            "storage": {"db_path": self.storage.db_path},
            "search": {
                "enabled": self.search.enabled,
                "host": self.search.host,
                "port": self.search.port,
                "trigger_enabled": self.search.trigger_enabled,
                "max_fetch_pages": self.search.max_fetch_pages,
                "fts_tokenizer": self.search.fts_tokenizer,
            },
            "output": {"csv_path": self.output.csv_path, "xlsx_path": self.output.xlsx_path},
            "sampling": {"review_rate": self.sampling.review_rate, "seed": self.sampling.seed},
            "columns": list(self.columns),
            "lexicon_groups": {group: len(values) for group, values in self.lexicon.items()},
        }


def _mask_secret(value: str) -> str:
    """凭据类字段只报"是否已填 + 长度"，**一个字符都不露出**。

    原来的 _mask 会保留首尾各 2 个字符——对 9 位密码等于泄露 4 位，
    在任何日志、截图、报错里都不该出现。
    """
    text = (value or "").strip()
    return f"已填（{len(text)} 字符）" if text else ""


def _mask(value: str, keep: int = 2) -> str:
    """凭据打码：只保留首尾少量字符，空值显示为空。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}{'*' * (len(text) - keep * 2)}{text[-keep:]}"


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------


def project_root_of(module_file: Union[str, Path] = __file__) -> Path:
    """项目根 = src/config.py 的上一级目录，与工作目录无关。"""
    return Path(module_file).resolve().parents[1]


def load_env_file(path: Optional[Path] = None) -> Dict[str, str]:
    """读取 ``.env``；优先使用 python-dotenv，未安装则退回内置简易解析。"""
    env_path = Path(path) if path else None
    if env_path is None or not env_path.exists():
        return {}
    try:
        from dotenv import dotenv_values  # type: ignore
    except ImportError:  # pragma: no cover - 允许在没有 dotenv 的环境跑
        values: Dict[str, str] = {}
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values
    return {k: (v or "") for k, v in dotenv_values(env_path).items()}


def load_yaml(path: PathLike) -> Any:
    """读取 YAML 文件（配置层是全项目**唯一**的配置读取入口）。

    文件不存在或解析失败一律返回空字典，不抛异常：调用方按「缺配置 → 用内置兜底」处理。
    """
    target = Path(path)
    if not target.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover
        return {}
    try:
        return yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception:  # pragma: no cover - 配置损坏时降级
        return {}


def load_lexicon(path: PathLike) -> Dict[str, Dict[str, Tuple[str, ...]]]:
    """读取别名/词表文件，规范化为 ``{分组: {标准值: (别名, ...)}}``。

    同时兼容两种写法：

    * ``{标准值: [别名...]}``（如 cities / degree / grade / major）
    * ``[词, 词, ...]``（如 employer_suffix / position_keywords / employer_cues）

    在配置阶段读一次并放进 ``AppConfig.lexicon``，解析层因此不必按文章重读文件。
    """
    loaded = load_yaml(path)
    if not isinstance(loaded, Mapping):
        return {}

    result: Dict[str, Dict[str, Tuple[str, ...]]] = {}
    for group, value in loaded.items():
        if isinstance(value, Mapping):
            normalized: Dict[str, Tuple[str, ...]] = {}
            for standard, aliases in value.items():
                if aliases is None:
                    normalized[str(standard)] = ()
                elif isinstance(aliases, (list, tuple)):
                    normalized[str(standard)] = tuple(str(item) for item in aliases)
                else:
                    normalized[str(standard)] = (str(aliases),)
            result[str(group)] = normalized
        elif isinstance(value, (list, tuple)):
            result[str(group)] = {str(item): () for item in value}
    return result


def load_fields(fields_path: PathLike) -> Tuple[Tuple[FieldSpec, ...], Tuple[str, ...]]:
    """读取 ``config/fields.yaml``，返回 (字段定义, 导出列序)。"""
    path = Path(fields_path)
    if not path.exists():
        raise ConfigError(f"字段口径文件不存在：{path}")
    raw = _read_yaml(path)
    entries = raw.get("fields")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path} 必须包含非空的 fields 列表")

    specs: List[FieldSpec] = []
    seen: set = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not entry.get("name"):
            raise ConfigError(f"{path} 第 {index + 1} 条字段缺少 name")
        name = str(entry["name"]).strip()
        if name in seen:
            raise ConfigError(f"{path} 字段名重复：{name}")
        seen.add(name)
        specs.append(
            FieldSpec(
                name=name,
                label=str(entry.get("label", name)),
                type=str(entry.get("type", "str")),
                required=bool(entry.get("required", False)),
                normalize=tuple(entry.get("normalize") or ()),
                aliases=tuple(entry.get("aliases") or ()),
            )
        )

    columns = tuple(spec.name for spec in specs)
    missing_core = [f for f in CORE_FIELDS if f not in columns]
    if missing_core:
        raise ConfigError(f"{path} 缺少七项核心字段：{missing_core}")
    return tuple(specs), columns


def load_config(
    config_path: Optional[PathLike] = None,
    project_root: Optional[PathLike] = None,
    env_path: Optional[PathLike] = None,
) -> AppConfig:
    """加载并校验全部配置，返回不可变 ``AppConfig``。

    优先级：``.env`` > ``config.yaml`` > 代码内置默认值。
    任何不合法的取值都会在这里抛 ``ConfigError``，管线不会带着坏配置开跑。
    """
    root = Path(project_root) if project_root else project_root_of()
    cfg_path = Path(config_path) if config_path else (root / CONFIG_RELATIVE_PATH)
    if not cfg_path.exists():
        raise ConfigError(
            f"找不到配置文件 {cfg_path}；请从模板创建 config/config.yaml 并填写门户 BASE、"
            "登录方式、请求间隔（≥2）、分页范围与 LLM/OCR 开关"
        )

    raw = _read_yaml(cfg_path)
    env = load_env_file(Path(env_path) if env_path else (root / ENV_RELATIVE_PATH))

    portal_raw = _section(raw, "portal")
    auth_raw = _section(raw, "auth")
    request_raw = _section(raw, "request")
    extract_raw = _section(raw, "extract")
    llm_raw = _section(raw, "llm")
    ocr_raw = _section(raw, "ocr")
    storage_raw = _section(raw, "storage")
    search_raw = _section(raw, "search")
    output_raw = _section(raw, "output")
    sampling_raw = _section(raw, "sampling")
    logging_raw = _section(raw, "logging")

    fields_specs, columns = load_fields(root / str(raw.get("fields_file", FIELDS_RELATIVE_PATH)))
    lexicon = load_lexicon(root / str(extract_raw.get("lexicon_file", "config/aliases.yaml")))

    portal = PortalConfig(
        base_url=_pick(env, "PORTAL_BASE_URL", portal_raw.get("base_url", "")),
        list_url=str(portal_raw.get("list_url", "")),
        page_param=str(portal_raw.get("page_param", "page")),
        page_start=int(portal_raw.get("page_start", 1)),
        page_end=int(portal_raw.get("page_end", portal_raw.get("page_start", 1))),
        detail_link_selector=str(portal_raw.get("detail_link_selector", "")),
        login_url=str(portal_raw.get("login_url", "")),
        login_check_url=str(portal_raw.get("login_check_url", "")),
        mode=str(portal_raw.get("mode", "html")).strip().lower(),
        api_url=str(portal_raw.get("api_url", "")),
        api_body=dict(portal_raw.get("api_body") or {}),
        api_param_style=str(portal_raw.get("api_param_style", "query")).strip().lower(),
        api_page_field=str(portal_raw.get("api_page_field", "currentPage")),
        api_token_field=str(portal_raw.get("api_token_field", "comsys_random_t")),
        api_token_style=str(portal_raw.get("api_token_style", "js_random")),
        api_token_length=int(portal_raw.get("api_token_length", 32)),
        api_list_path=str(portal_raw.get("api_list_path", "data.records")),
        api_total_path=str(portal_raw.get("api_total_path", "page.total")),
        api_total_pages_path=str(portal_raw.get("api_total_pages_path", "page.totalCounts")),
        api_title_field=str(portal_raw.get("api_title_field", "title")),
        api_date_field=str(portal_raw.get("api_date_field", "publishTime")),
        api_link_field=str(portal_raw.get("api_link_field", "notice_link")),
        api_link_state_field=str(portal_raw.get("api_link_state_field", "notice_link_state")),
        api_link_state_external=int(portal_raw.get("api_link_state_external", 1)),
        external_link_policy=str(portal_raw.get("external_link_policy", "fetch")).strip().lower(),
        detail_org_id=str(portal_raw.get("detail_org_id", "")),
        detail_url_template=str(portal_raw.get("detail_url_template", "")),
    )

    auth = AuthConfig(
        method=str(auth_raw.get("method", "account")).strip().lower(),
        student_id=_pick(env, "AUTH_STUDENT_ID", auth_raw.get("student_id", "")),
        password=_pick(env, "AUTH_PASSWORD", auth_raw.get("password", "")),
        cookie=_pick(env, "AUTH_COOKIE", auth_raw.get("cookie", "")),
        session_file=str(auth_raw.get("session_file", "data/.session.json")),
    )

    request = RequestConfig(
        interval_seconds=float(request_raw.get("interval_seconds", MIN_REQUEST_INTERVAL)),
        timeout=float(request_raw.get("timeout", 20.0)),
        retries=int(request_raw.get("retries", 2)),
        user_agent=str(request_raw.get("user_agent", RequestConfig.user_agent)),
        proxy=_pick(env, "CRAWLER_PROXY", request_raw.get("proxy", "")),
        use_playwright=_as_bool(request_raw.get("use_playwright", False)),
        verify_tls=_as_bool(request_raw.get("verify_tls", True)),
        encoding=str(request_raw.get("encoding", "")),
    )

    extract = ExtractConfig(
        rule_first=_as_bool(extract_raw.get("rule_first", True)),
        missing_placeholder=str(extract_raw.get("missing_placeholder", MISSING)),
        manual_review_output=str(extract_raw.get("manual_review_output", DEFAULT_MANUAL_REVIEW_PATH)),
        raw_html_dir=str(extract_raw.get("raw_html_dir", DEFAULT_RAW_HTML_DIR)),
        regex_file=str(extract_raw.get("regex_file", "")),
        lexicon_file=str(extract_raw.get("lexicon_file", "config/aliases.yaml")),
        ocr_images_dir=str(extract_raw.get("ocr_images_dir", DEFAULT_IMAGE_DIR)),
        ocr_cache_dir=str(extract_raw.get("ocr_cache_dir", DEFAULT_OCR_CACHE_DIR)),
        llm_trigger_below=int(extract_raw.get("llm_trigger_below", len(CORE_FIELDS))),
    )

    llm = LlmConfig(
        enabled=_as_bool(_pick(env, "LLM_ENABLED", llm_raw.get("enabled", False))),
        provider=str(llm_raw.get("provider", "openai")).strip().lower(),
        base_url=_pick(env, "LLM_BASE_URL", llm_raw.get("base_url", "")),
        model=_pick(env, "LLM_MODEL", llm_raw.get("model", "")),
        api_key=_pick(env, "LLM_API_KEY", llm_raw.get("api_key", "")),
        timeout=float(llm_raw.get("timeout", 60.0)),
        max_retries=int(llm_raw.get("max_retries", 1)),
        temperature=float(llm_raw.get("temperature", 0.0)),
    )

    ocr = OcrConfig(
        enabled=_as_bool(_pick(env, "OCR_ENABLED", ocr_raw.get("enabled", False))),
        lang=str(ocr_raw.get("lang", "chi_sim+eng")),
        tesseract_cmd=_pick(env, "TESSERACT_CMD", ocr_raw.get("tesseract_cmd", "")),
    )

    storage = StorageConfig(
        db_path=_pick(env, "DB_PATH", storage_raw.get("db_path", DEFAULT_DB_PATH)),
        batch_size=int(storage_raw.get("batch_size", 200)),
    )

    search = SearchConfig(
        enabled=_as_bool(search_raw.get("enabled", True)),
        host=str(search_raw.get("host", "127.0.0.1")),
        port=int(search_raw.get("port", 8765)),
        default_limit=int(search_raw.get("default_limit", 20)),
        max_limit=int(search_raw.get("max_limit", 100)),
        cache_ttl_seconds=int(search_raw.get("cache_ttl_seconds", 300)),
        cache_max_rows=int(search_raw.get("cache_max_rows", 2000)),
        l1_size=int(search_raw.get("l1_size", 128)),
        trigger_enabled=_as_bool(search_raw.get("trigger_enabled", False)),
        max_fetch_pages=int(search_raw.get("max_fetch_pages", 2)),
        fetch_cooldown_seconds=int(search_raw.get("fetch_cooldown_seconds", 900)),
        max_estimated_seconds=float(search_raw.get("max_estimated_seconds", 120.0)),
        fts_tokenizer=str(search_raw.get("fts_tokenizer", "unicode61")),
    )

    output = OutputConfig(
        csv_path=str(output_raw.get("csv_path", DEFAULT_CSV_PATH)),
        xlsx_path=str(output_raw.get("xlsx_path", DEFAULT_XLSX_PATH)),
    )

    sampling = SamplingConfig(
        review_rate=float(sampling_raw.get("review_rate", 0.1)),
        seed=int(sampling_raw.get("seed", 42)),
    )

    logging_cfg = LoggingConfig(
        level=str(_pick(env, "LOG_LEVEL", logging_raw.get("level", "INFO"))).upper(),
        file_name=str(logging_raw.get("file_name", "pipeline.log")),
        console=_as_bool(logging_raw.get("console", True)),
    )

    config = AppConfig(
        project_root=root,
        config_path=cfg_path,
        fields=fields_specs,
        columns=columns,
        portal=portal,
        auth=auth,
        request=request,
        extract=extract,
        llm=llm,
        ocr=ocr,
        storage=storage,
        search=search,
        output=output,
        sampling=sampling,
        logging=logging_cfg,
        lexicon=lexicon,
    )
    validate_config(config)
    return config


def validate_config(cfg: AppConfig) -> AppConfig:
    """对配置做启动前校验；任何一项不合法即抛 ``ConfigError``。"""
    problems: List[str] = []

    if not cfg.portal.base_url.startswith(("http://", "https://")):
        problems.append("portal.base_url 必须是以 http:// 或 https:// 开头的门户地址")

    if cfg.portal.mode not in ("html", "api"):
        problems.append(f"portal.mode 只能是 html 或 api，收到 {cfg.portal.mode!r}")
    if cfg.portal.api_param_style not in ("query", "json"):
        problems.append(
            f"portal.api_param_style 只能是 query 或 json，收到 {cfg.portal.api_param_style!r}"
        )
    if cfg.portal.external_link_policy not in ("fetch", "portal", "skip"):
        problems.append(
            f"portal.external_link_policy 只能是 fetch/portal/skip，收到 {cfg.portal.external_link_policy!r}"
        )
    if cfg.portal.mode == "api":
        if not cfg.portal.api_url.startswith(("http://", "https://")):
            problems.append("portal.mode=api 时 portal.api_url 必填，且需为 http(s) 地址")
        if not cfg.portal.detail_url_template:
            problems.append(
                "portal.mode=api 时 portal.detail_url_template 必填"
                "（用接口返回的 notice_id / organization_id 拼详情链接）"
            )

    if cfg.portal.page_start < 1:
        problems.append("portal.page_start 必须 ≥ 1")
    if cfg.portal.page_end != 0 and cfg.portal.page_end < cfg.portal.page_start:
        problems.append(
            f"分页范围非法：page_start={cfg.portal.page_start} > page_end={cfg.portal.page_end}"
            "（page_end=0 表示自动翻到最后一页）"
        )

    # 合规红线：只读低频
    if cfg.request.interval_seconds < MIN_REQUEST_INTERVAL:
        problems.append(
            f"request.interval_seconds={cfg.request.interval_seconds} 低于合规下限 "
            f"{MIN_REQUEST_INTERVAL}s（红线：只读低频，不得调小）"
        )

    if cfg.auth.method not in AUTH_METHODS:
        problems.append(f"auth.method 只能是 {AUTH_METHODS} 之一，收到 {cfg.auth.method!r}")

    if not cfg.extract.rule_first:
        problems.append("extract.rule_first 必须为 true（技术路线：规则优先 + LLM 兜底）")

    if cfg.extract.missing_placeholder != MISSING:
        problems.append(f"extract.missing_placeholder 必须为「{MISSING}」")

    if not 0 < cfg.sampling.review_rate <= 1:
        problems.append(f"sampling.review_rate 必须在 (0, 1] 区间，收到 {cfg.sampling.review_rate}")

    if cfg.llm.enabled and cfg.llm.provider not in LLM_PROVIDERS:
        problems.append(f"llm.provider 只能是 {LLM_PROVIDERS} 之一，收到 {cfg.llm.provider!r}")

    if cfg.ocr.enabled and not cfg.ocr.lang.strip():
        problems.append("ocr.enabled=true 时必须填写 ocr.lang（如 chi_sim+eng）")

    if cfg.storage.batch_size < 1:
        problems.append("storage.batch_size 必须 ≥ 1")

    # 检索服务对外暴露真实抓取内容：只允许回环地址（安全红线）
    if cfg.search.host not in ("127.0.0.1", "localhost", "::1"):
        problems.append(
            f"search.host={cfg.search.host!r} 非法：检索服务返回真实门户内容，"
            "只允许绑定本机回环地址（127.0.0.1 / localhost / ::1）"
        )

    if not 1 <= cfg.search.port <= 65535:
        problems.append(f"search.port 必须在 1~65535 之间，收到 {cfg.search.port}")

    if cfg.search.default_limit < 1:
        problems.append("search.default_limit 必须 ≥ 1")
    if cfg.search.max_limit < cfg.search.default_limit:
        problems.append(
            f"search.max_limit={cfg.search.max_limit} 不得小于 "
            f"search.default_limit={cfg.search.default_limit}"
        )
    if cfg.search.max_fetch_pages < 1:
        problems.append("search.max_fetch_pages 必须 ≥ 1（每页 15 条，按 ≥2 秒/请求计）")
    if cfg.search.fetch_cooldown_seconds < 0:
        problems.append("search.fetch_cooldown_seconds 不得为负")
    if cfg.search.max_estimated_seconds <= 0:
        problems.append("search.max_estimated_seconds 必须为正数（预算闸门）")
    if cfg.search.fts_tokenizer not in ("trigram", "unicode61"):
        problems.append(
            f"search.fts_tokenizer 只能是 trigram 或 unicode61，收到 {cfg.search.fts_tokenizer!r}"
            "（trigram 才能让中文子串匹配）"
        )

    if not cfg.extract.llm_trigger_below:
        problems.append("extract.llm_trigger_below 必须 ≥ 1（= 七项字段数时表示「缺一即触发兜底」）")

    if problems:
        raise ConfigError("配置校验未通过：" + "；".join(problems))

    # LLM 完整性单独校验（只有在启用时才严格）
    cfg.require_llm()
    return cfg


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("缺少依赖 PyYAML，请先执行 pip install -r requirements.txt") from exc
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"YAML 解析失败：{path}：{exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise ConfigError(f"{path} 顶层必须是映射（键值对）")
    return dict(loaded)


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"配置段 {name} 必须是映射（键值对）")
    return value


def _pick(env: Mapping[str, str], key: str, default: Any) -> Any:
    """.env 覆盖 yaml；空字符串视为「未设置」。"""
    value = env.get(key)
    if value is None or str(value).strip() == "":
        return default
    return value


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def with_overrides(cfg: AppConfig, **overrides: Any) -> AppConfig:
    """返回覆盖了顶层配置段的副本（如 ``with_overrides(cfg, request=new_request)``）。

    供 CLI 参数覆写配置使用，保持 ``AppConfig`` 不可变。
    """
    return replace(cfg, **overrides) if overrides else cfg
