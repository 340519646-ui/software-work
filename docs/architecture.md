# 模块接口与契约（architecture.md）

本文档定义**模块之间怎么打交道**。它是接口的唯一权威说明；代码里每个模块的
docstring 是它的展开，`src/contracts.py` 是它的可执行形式，`tests/test_contracts.py`
是它的自动检查。三者不一致时以本文档为准，并须同步修改另外两处。

当前契约版本：`SCHEMA_VERSION = 1.5.0`（见 `src/contracts.py`）。

> **1.1.0 变更**：图片型数据成为一等公民——新增 `ImageAsset` / `ContentKind` / `OcrStatus`，
> `RawArticle` 与 `ManifestEntry` 增加 `images`，`CleanArticle.source_kind`、`JobRecord.content_kind`，
> 新增内容寻址的图片归档与 OCR 缓存路径规则，`PageFetcher` 增加 `fetch_image`，
> 计数键新增 `images_fetched` / `images_failed` / `ocr_cached` / `ocr_computed` / `ocr_failed`。
> 动机见 `docs/storage-decision.md`：门户多数文章正文是截图，必须「算一次 OCR、长期复用」。

---

## 一、分层与依赖方向

```
        ┌──────────────────────────────────────────────┐
        │  contracts  纯数据契约 / Protocol（仅标准库） │  ← 第 0 层，谁都能依赖
        └──────────────────────────────────────────────┘
                 ▲            ▲            ▲
        ┌────────┴───┐  ┌─────┴─────┐  ┌───┴──────────┐
        │  config    │  │  crawler  │  │  parser      │
        │（配置注入） │  │（采集层）  │  │（解析层）     │
        └────────────┘  └───────────┘  └──────────────┘
                 ▲            ▲            ▲
        ┌────────┴───┐  ┌─────┴──────┐ ┌───┴──────────────┐
        │ storage    │  │ validation │ │  （以上均无相互依赖）│
        │（存储层）   │  │（校验层）   │ │                    │
        └────────────┘  └────────────┘ └────────────────────┘
                 ▲            ▲            ▲
        ┌────────┴────────────┴────────────┴───────────┐
        │  pipeline  三段式编排（唯一装配点）             │
        └──────────────────────────────────────────────┘
                              ▲
                        ┌─────┴─────┐
                        │   main    │ （仅转交 CLI）
                        └───────────┘
```

**允许的 import 方向**（`tests/test_contracts.py::ALLOWED_PROJECT_IMPORTS` 强制）：

| 层 | 允许依赖的项目模块 | 允许的第三方库 |
| --- | --- | --- |
| `src.contracts` | 无 | 无（只标准库） |
| `src.config` | `contracts` | `yaml`、`dotenv` |
| `src.crawler` | `contracts`、`config` | `requests`、`bs4`、`lxml`、`playwright`、`tenacity` |
| `src.parser` | `contracts`、`config` | `bs4`、`lxml`、`openai`、`ollama`、`pytesseract`、`PIL` |
| `src.storage` | `contracts`、`config` | `pandas`、`openpyxl` |
| `src.validation` | `contracts`、`config` | `yaml`、`pandas` |
| `src.pipeline` | 上面全部层 | 无 |
| `src.search` | `contracts`、`config`、`crawler`、`parser`、`storage`、`validation` | 无（含 Web 层） |
| `src.main` | `contracts`、`config`、`pipeline` | 无 |

**四条硬性禁令**（违反即测试失败）：

1. `parser` 不得 import `storage` / `crawler`；`crawler` 不得 import `parser` / `storage`；
   采集与解析必须能各自独立测试。
2. 任何层不得 import `pipeline` / `main`（依赖只能向下，不能向上）。
3. `contracts` 不得 import 项目内任何模块，也不得 import 第三方库。
4. 除 `src/pipeline/run.py`、`src/crawler/login_check.py` 与 `src/search/server.py`
   （均为 CLI/服务入口）外，**任何模块不得调用 `load_config`**——
   配置必须由参数注入（`AppConfig`）。
5. **检索层与 pipeline 同级，且不得 import pipeline**：定向采集需要复用
   crawler/parser/storage/validation，因此允许集合与 pipeline 相同；
   但必须保持"依赖只能向下"这条红线。
6. **storage 不得反向依赖 search**：索引怎么建、什么时候重建由检索层决定，
   存储层只把连接交出去（`SqliteRepository.connection`），并通过可注入回调
   `after_write` 通知"写完该维护索引了"。

---

## 二、每层对外的「门面」

pipeline 只认门面，不 import 层的内部文件；层内部文件之间可以自由协作。

| 层 | 门面函数（唯一入口） | 返回的契约类型 | 实现文件 |
| --- | --- | --- | --- |
| 采集 | `crawler.build_fetcher(cfg)` | `contracts.PageFetcher` | `src/crawler/crawler.py` |
| 解析 | `parser.build_extractor(cfg)` | `contracts.ArticleExtractor` | `src/parser/extractor.py` |
| 检索 | `search.build_search_service(cfg)` | `search.SearchService`（`contracts.SearchIndex`） | `src/search/service.py` |
| 校验 | `validation.build_validator(cfg)` | `contracts.RecordValidator` | `src/validation/validator.py` |
| 存储 | `storage.build_repository(cfg)` | `contracts.RecordRepository` | `src/storage/database.py` |
| 编排 | `pipeline.build_pipeline(cfg)` | `contracts.StageRunner` | `src/pipeline/pipeline.py` |

因此：`pipeline` 不得出现 `from src.parser.rules import ...` 这类语句，
`parser` 也不得直接使用 `storage.models`。

### 采集层内部划分（对 pipeline 不可见）

| 文件 | 职责 | 关键约定 |
| --- | --- | --- |
| `session.py` | URL → `HttpResponse` | 限速 ≥ `interval_seconds`（≥2s）、重试、UA/Cookie；**不解析 HTML** |
| `list_page.py` | 列表页 → `ArticleRef` | `parse_list` 是纯函数，可用样本 HTML 单测 |
| `detail_page.py` | 详情页 → `RawArticle` + 归档 + 清单 | 归档路径确定、幂等覆盖写 |
| `crawler.py` | 组合上述三者，实现 `PageFetcher` | 单篇失败返回 failure 记录，不抛异常 |
| `login_check.py` | 登录态自检、清会话 | 命令行入口，退出码 0/2/3 |

---

## 三、数据契约（DTO）

跨模块传递只允许下面这些类型；禁止传 `BeautifulSoup`、`requests.Response`、
`DataFrame`、裸 `dict`。

| DTO | 生产者 → 消费者 | 关键字段与不变量 |
| --- | --- | --- |
| `ArticleRef` | 列表页 → 详情页 | `detail_url` 非空；`article_key` 由 URL 推导；`list_url` 必填（溯源） |
| `HttpResponse` | Transport → 列表/详情页 | `ok` = 2xx 且正文非空且无 error |
| `RawArticle` | 详情页 → 清洗 | `ok` = 2xx 且 `html` 非空；失败记录 `html=""`、`error` 非空 |
| `ImageAsset` | 详情页 → 清单 → OCR → 抽取 | `sha1` = 图片内容哈希（去重键 + OCR 缓存键）；`image_path`/`ocr_cache_path` 由内容决定，可重建 |
| `ManifestEntry` | fetch 阶段 → extract 阶段 | 每行 JSON；`detail_url ↔ html_path` 映射 + `images` 清单（sha1 不可逆，必须显式记录） |
| `CleanArticle` | 清洗 → 抽取 | `segments` 的键固定取自 `SEGMENT_KEYS` |
| `FieldHit` | 规则 / LLM / OCR → 组装 | `field_name` ∈ `CORE_FIELDS`；`value` 非空（缺失不得产生命中） |
| `JobRecord` | 抽取 → 校验 → 存储 → 导出 | 核心字段默认「未知」；`apply_hits` 累计归并 `extract_method` |
| `ValidationIssue` / `ValidationResult` | 校验 → 存储 / 编排 | `needs_manual_review` = 校验失败 或 存在「未知」字段 |
| `StageResult` | 各阶段 → CLI | `counters` 的键必须在 `COUNTER_KEYS` 内，否则 `add()` 抛 `KeyError` |
| `LoginStatus` | 登录自检 → 编排 | `require()` 未登录时抛 `LoginError` |

### 七项核心字段（顺序固定）

`graduation_year`（届别）、`grade`（年级）、`degree`（学历）、`major`（专业）、
`city`（城市）、`employer`（单位）、`position`（岗位）。
中文名映射见 `contracts.CORE_FIELD_LABELS`，列序与必填口径见 `config/fields.yaml`。

### 缺失与未知

* 唯一占位符：`contracts.MISSING = "未知"`；空串、`None`、`-`、`无`、`N/A` 等
  一律由 `contracts.normalize_missing()` 折叠为「未知」。
* 七项核心字段在 `fields.yaml` 中一律 `required: false`：缺失**不阻塞入库**，
  但会进 `data/processed/manual_review.csv`。
* `required: true` 只用于**溯源红线字段**：`source_url`、`raw_html_path`、`crawl_time`。

---

## 四、幂等键与路径规则

| 规则 | 值 | 唯一实现 |
| --- | --- | --- |
| 记录主键 | `article_key` = sha1(detail_url) 前 16 位 | `contracts.article_key_of` |
| 归档路径 | `data/raw/html/<article_key>.html` | `contracts.raw_html_relpath` |
| 采集清单 | `data/raw/manifest.jsonl`（每行一个 `ManifestEntry`） | `contracts.manifest_relpath` |
| 图片归档路径 | `data/raw/images/<article_key>/<image_sha1>.png` | `contracts.archived_image_relpath` |
| **OCR 缓存路径** | `data/raw/ocr/<image_sha1>.txt`（命中即跳过识别） | `contracts.ocr_cache_relpath` |
| 去重键 | `source_url`（数据库唯一索引） | `storage.models.SOURCE_URL_COLUMN` |
| 导出列序 | `config/fields.yaml` 的书写顺序 | `config.load_fields` → `AppConfig.columns` |

**断点续跑**：fetch 阶段用 `Repository.existing_source_urls()` 跳过已归档 URL；
extract 阶段按 `source_url` UPSERT；export 阶段全量重建报表。三个阶段都可重复执行，
结果一致。

---

## 五、阶段语义与产物

| 阶段 | 命令 | 输入 | 产物 | 联网 |
| --- | --- | --- | --- | --- |
| fetch | `python -m src.pipeline.run --stage fetch` | 门户列表页/详情页 | `data/raw/html/*.html`、`data/raw/manifest.jsonl` | 是 |
| extract | `python -m src.pipeline.run --stage extract` | 清单 + 归档 HTML | `articles` 表、`data/processed/manual_review.csv` | 否（离线回放） |
| export | `python -m src.pipeline.run --stage export` | `articles` 表 | `jobs.csv`、`jobs.xlsx`、统计 JSON | 否 |

> fetch 不写业务表、extract 不联网，是刻意的设计：调整正则/选择器后可以完全离线重跑，
> 既省时间，也减少对门户的访问（合规要求）。

---

## 六、错误与降级矩阵

| 情形 | 实现方动作 | 是否中断整批 | 计入 |
| --- | --- | --- | --- |
| 列表页请求失败 | `list_page.fetch_list` 抛 `FetchError` | 否（跳过该页） | `errors` |
| 详情页请求失败 | 返回 `RawArticle.failure(...)` | 否 | `details_failed` |
| 归档写盘失败 | `detail_page` 抛 `StorageError` | 否 | `errors` |
| 页面结构变化、解析异常 | 抛 `ParseError` | 否 | `errors` |
| LLM 未启用 | 缺失字段保持「未知」 | 否 | `unknown_fields` |
| LLM 网络/鉴权失败 | 降级为「保持未知」 | 否 | `llm_failed` |
| LLM 返回非 JSON / 无证据 | 丢弃该字段（防幻觉） | 否 | `llm_failed` |
| 图片下载失败 | 该图 `ocr_status=failed`，文章继续解析 | 否 | `images_failed` |
| OCR 环境缺失 | 跳过 OCR，记录进人工清单 | 否 | `ocr_failed` |
| OCR 缓存命中 | 直接读缓存文本，不重复识别 | 否 | `ocr_cached` |
| 字段缺失（两级都没拿到） | 保持 `MISSING` | 否 | `unknown_fields` |
| 校验不通过（红线字段为空、届别非法） | 不入库，记诊断 | 否 | `invalid` |
| 重复 `source_url` | UPSERT 覆盖，不新增 | 否 | `duplicates` |
| 登录态失效 | `login_check.require_login` 抛 `LoginError` | **是**（阶段终止，退出码 4） | — |
| 配置非法（间隔 <2s、缺门户 BASE） | `load_config` 抛 `ConfigError` | **是**（启动即失败，退出码 3） | — |

**异常边界规则**：模块内部异常（requests/解析/sqlite 异常）不得穿过模块边界，
必须转换为 `PipelineError` 子类或 DTO 的 `error` 字段。
所有 `PipelineError` 子类都带 `code`（取值见 `contracts.ErrorCode`）、`message`、
`detail`、`url`，可直接序列化进日志。

### 计数键（`StageResult.counters`）

`pages_listed`、`refs_found`、`details_fetched`、`details_failed`、`html_archived`、
`images_fetched`、`images_failed`、`records_extracted`、`rule_hits`、`llm_calls`、`llm_failed`、
`ocr_calls`、`ocr_cached`、`ocr_computed`、`ocr_failed`、
`unknown_fields`、`duplicates`、`validated`、`invalid`、`inserted`、`manual_review`、
`exported_rows`、`skipped_existing`、`errors`。

新增计数项必须先改 `contracts.COUNTER_KEYS`（并同步本文档），否则 `add()` 报错——
这是为了防止各阶段临时造键、导致统计口径漂移。

---

## 七、配置注入

* 唯一配置对象 `AppConfig`（`src/config.py`），由 CLI 构造后**逐层传参**注入；
  各模块构造函数第一个参数都是 `cfg: AppConfig`。
* 禁止模块级全局配置、禁止各模块自行读 `config.yaml` / `.env`（测试强制）。
* `AppConfig.path(rel)` 是相对路径 → 绝对路径的唯一转换入口。
* `AppConfig.require_auth()` / `require_llm()` 是启动前门禁；`AppConfig.redacted()`
  返回打码后的配置摘要，**日志与异常里只能出现这个版本**（学号、密码、Cookie、API Key 均打码）。

### 配置校验红线（`load_config` 阶段直接报错）

| 校验项 | 规则 |
| --- | --- |
| `request.interval_seconds` | **≥ 2.0**，低于即 `ConfigError` |
| `portal.base_url` | 必须 `http(s)://` 开头 |
| `portal.page_start ≤ page_end`，`page_start ≥ 1` | 分页范围合法 |
| `auth.method` | 只能是 `account` / `cookie` |
| `extract.rule_first` | 必须为 `true`（规则优先的路线不允许关闭） |
| `extract.missing_placeholder` | 必须为「未知」 |
| `llm.provider` | 只能是 `openai` / `ollama`；启用时必须给全 `api_key`/`base_url`/`model` |
| `sampling.review_rate` | 必须落在 (0, 1] |

---

## 八、契约变更流程

接口不是「写完就冻住」，但每一次变更都要走同样的四步：

1. 递增 `contracts.SCHEMA_VERSION`（字段增删改、枚举取值变化、路径规则变化）；
2. 改 `src/contracts.py`（DTO / Protocol / 常量）；
3. 改 `tests/test_contracts.py`（新增或修正契约断言）；
4. 更新本文档对应表格，并在 `docs/experiment.md` 记录「何时、为何改」。

任何一步没做，`pytest tests/test_contracts.py` 都会失败——接口因此始终可控。

---

## 九、实现顺序建议（骨架 → 可运行）

| 顺序 | 目标 | 需要填的文件 | 验收 |
| --- | --- | --- | --- |
| 1 | 能登录、能确认会话 | `crawler/session.py`、`crawler/login_check.py` | `python -m src.crawler.login_check` 退出码 0 |
| 2 | 能翻页、能抓详情并归档 | `crawler/list_page.py`、`crawler/detail_page.py`、`crawler/crawler.py` | `--stage fetch` 产出 `manifest.jsonl` 与 HTML |
| 3 | 能离线回放并抽取 | `parser/cleaner.py`、`parser/rules.py`、`parser/extractor.py` | 样本文章的七项字段有值，`tests/test_rules.py` 通过 |
| 4 | 能入库与导出 | `storage/models.py`、`storage/database.py`、`validation/validator.py` | `--stage extract` 后 `--stage export` 产出 CSV/XLSX |
| 5 | 补兜底与统计 | `parser/llm.py`、`parser/ocr.py`、`validation/statistics.py` | 缺失字段减少，人工清单与统计可复核 |

各步骤之间的接口**已经固定**（本文档第二、三节），因此可以按任意顺序并行实现，
不会互相返工。

---

## 十、实施期接口补记（1.1.0 实施记录）

实现 84 处待实现函数的过程中，为了让接口真正"可控"，对契约做了以下补充。
每一项都同步改了 `src/contracts.py`、`tests/test_contracts.py` 与本文档（契约变更四步）。

| 变更 | 内容 | 为什么必须改 |
| --- | --- | --- |
| `JobRecord.evidence_text` | 新增字段；`evidence()` 优先返回它，`apply_hits` 自动刷新 | 原来 `evidence` 是**方法**，而 `fields.yaml` 里它是**列**——入库后读不回，数据库不自描述 |
| `ErrorCode.OCR_NEEDS_REVIEW` | 新增错误码 | OCR 来源必须人工复核（合规红线 5），需要机器可判定的标记，而不是靠字符串约定 |
| `ValidationResult.needs_manual_review` | 由 2 个条件扩为 3 个（校验失败 / 未知字段 / OCR 来源） | 全项目唯一判定口径，禁止各层自行判断 |
| `HttpResponse.content` + `Transport.get(binary=)` | 响应结构支持二进制，`ok` 判定改为「文本或字节非空」 | 图片不能走文本解码，否则字节被破坏 |
| `AppConfig.lexicon` + `config.load_yaml/load_lexicon` | 词表在配置阶段读一次并注入 | ① `parser` 层不允许依赖 `yaml`（契约测试拦下）；② 按文章重读 YAML 是纯浪费 |
| `COUNTER_KEYS` 新增 5 项 | `images_fetched` / `images_failed` / `ocr_cached` / `ocr_computed` / `ocr_failed` | 图片与缓存命中必须可量化，否则"省了多少时间"无法证明 |
| 采集层门面再导出 | `crawler.py` 再导出 `load_archived` / `load_manifest` / `append_manifest` | 编排层只依赖采集层的**门面模块**，不 import 层内文件 |
| `Pipeline` 可注入 | 四个 `*_factory` + `login_checker` + `last_summary` | 让三阶段端到端能在**完全离线**下被测试覆盖（`tests/test_pipeline.py`） |

### 实施期被测试/契约拦下的 8 个真实缺陷

按发现顺序（全部已修复，且都有回归测试兜住）：

| # | 缺陷 | 怎么暴露的 | 后果（若不修） |
| - | ---- | ---------- | -------------- |
| 1 | `apply_hits` 只按"本次调用"归并 `extract_method` | 契约测试断言 hybrid 时失败 | 先跑规则再跑 LLM 会把 `rule` 覆盖成 `llm`，`hybrid` 永远统计不出来 |
| 2 | `evidence` 是方法、却是数据库列 | 存储层单测 | 从库里读回的记录丢失"命中依据"，人工复核无从下手 |
| 3 | DB 列名 `evidence` 未映射到 DTO `evidence_text` | 存储层单测 | 同上，且导出报表的依据列为空 |
| 4 | `parser/rules.py` import 了 `yaml` | 契约测试的分层白名单 | 解析层偷偷依赖配置文件，且每篇文章重读一次 YAML |
| 5 | `summarize` 的 `field_source` 从 DB 读回后全为 0 | 端到端验收 | 统计报表给出"规则命中 0 次"的错误结论 |
| 6 | `HttpTransport.get` 签名漏了 `binary` 参数，函数体却在使用 | **传输层本地集成测试**（首个真实 HTTP 调用） | **任何真实采集都会在第一次请求就抛 `NameError`**；假对象测试永远发现不了——这条是靠"用真实 HTTP 打一遍"抓出来的 |
| 7 | JSON 响应无 `Content-Type` 时走字符集推断（`apparent_encoding`） | 控制台 Response dump（`headers: Headers {}`） | 短响应可能被推断成 gbk/latin-1，**中文变乱码**且不报错；已改为 RFC 8259 规定的严格 UTF-8（失败退 gb18030） |
| 8 | 自动翻页把「这一页抓取失败」与「这一页本来就空」都当成 `[]` | 由本人实测推导（接口失败也返回 **HTTP 200 + 失败外壳**） | **会话过期会被误判成"已到最后一页"**，采集静默提前结束、数据无声变少；已区分 `(refs, ok)`：失败即停并记 ERROR 日志（含已收集条数） |

### 一条统计口径的补充说明

`field_source`（每字段由规则/LLM 命中的次数）有**两段口径**：

* 记录带着 `hits`（extract 阶段刚跑完、内存态）→ **精确归因**到每个字段；
* 记录从数据库读回（export 阶段）→ `hits` 不落库，只能按整条记录的 `extract_method`
  **保守归因**；`hybrid` 记录无法区分字段归属，因此不计入该项统计。

这条差异写在 `src/validation/statistics.py::summarize` 的注释里，避免后人误读报表。

### 契约 1.2.0：接入真实门户后的补强

在 `docs/portal-probe.md` 的实测基础上（未登录访问 `my.muc.edu.cn` 会以 **HTTP 200** 的形式
跳到统一身份认证），补了以下接口能力：

| 变更 | 内容 | 为什么必须改 |
| --- | --- | --- |
| `HttpResponse.final_url` | 记录重定向后的落点 | `requests`/`playwright` 都自动跟随重定向，不记录落点就会**把登录页当成正常页面**（状态码仍是 200） |
| `HttpResponse.redirected` | 判断是否发生重定向 | 登录态判定需要一眼可读的判据 |
| `LoginStatus.final_url` | 登录自检结果带上落点 | 排查「为什么说我没登录」时能直接看到被弹到哪里 |
| `session.looks_like_login_redirect()` | 落点是否命中 SSO 特征（login/sso/cas/zfca/auth…） | 比 HTML 特征串更可靠，且可单测（6 组参数化用例） |

判定顺序也固定下来：**先看落点，再看 HTML 特征**。两条都命中才判未登录的旧实现，
在「登录页恰好不含 password 字段」的门户上会漏判。

### 契约 1.3.0：接口（JSON）模式的列表采集

实测确认：门户通知列表**不是 DOM 页面**，而是
`POST /comsys-portal-notice-web/getNoticeByPage`（`Content-Type: application/json`），
URL 不变、靠请求体 `currentPage` 翻页，另有每次变化的 `comsys_random_t`。
解析 DOM 在这套门户上既不可行也不抗改版，因此新增接口模式：

| 变更 | 内容 | 说明 |
| --- | --- | --- |
| `Transport.post_json` | 协议新增方法；`HttpTransport` 与 `PlaywrightTransport` 各自实现 | Playwright 版走 `context.request`，**与页面共享 Cookie**，登录态不丢 |
| `PortalConfig.mode` | `html` / `api` 二选一（启动时校验） | `api` 模式强制要求 `api_url` 与 `detail_url_template` |
| `PortalConfig` 接口字段 | `api_url` / `api_body` / `api_page_field` / `api_token_field` / `api_list_path` / `api_total_path` / `api_title_field` / `api_date_field` / `detail_url_template` | **字段名与路径全部可配**，门户改版只改配置 |
| `list_page` 新函数 | `random_token` / `build_api_body` / `dig` / `build_detail_url` / `parse_list_json` / `fetch_list_api` | 详情链接用自实现的占位替换，支持 `{notice_id}` 与 `{data.noticeId}` 两种写法；缺占位字段的记录**跳过而不编造** |
| `cleaner` 容器 | 新增 `div.content_all`（实测选择器） | 正文容器优先级表更新 |

分发点只有一个：`PortalPageFetcher.fetch_list` 按 `portal.mode` 选择接口或 DOM 路径——
这样「门户是哪种形态」这件事不会扩散到解析层与编排层。
