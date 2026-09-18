# 实施计划（implementation-plan.md）

> 用途：把"待实现 84 处"变成**可执行、可验收、不越界**的排期。
> 配套文档：`architecture.md`（接口契约）、`storage-decision.md`（存储与缓存方案）、`overview.md`（新人导读）。

---

## 一、现状快照（实测，非估算）

| 部分 | 规模 | 状态 |
| --- | --- | --- |
| `src/contracts.py`（契约层：DTO + Protocol + 幂等键） | 858 行 | ✅ 已实现 |
| `src/config.py`（配置层：加载 + 强类型 + 启动校验 + 打码） | 603 行 | ✅ 已实现 |
| `src/main.py`（转交 CLI） | 27 行 | ✅ 已实现 |
| CLI 外壳：`login_check.main` / `run.main` / `apply_overrides` | — | ✅ 已实现（退出码、参数覆盖、错误提示） |
| `tests/test_contracts.py`（接口契约测试） | 509 行 / 78 项 | ✅ **78 passed** |
| 业务代码（采集/解析/存储/校验/编排） | 20 个文件 | ⏳ **84 处待实现** |
| 业务单测 `test_cleaner / test_rules / test_parser / test_database` | 4 个文件 | ⏳ 全为 0 字节 |
| 文档：`overview` / `architecture` / `storage-decision` | — | ✅ 已写 |
| 文档：`experiment.md` | 0 字节 | ⏳ 待写（需真实数据） |
| `data/**` 全部产物 | 均 0 字节 | ⏳ **说明从未跑过真实数据** |

**按文件的待实现分布**（数字 = 待实现函数个数）：

```
crawler/  session 4 · list_page 3 · detail_page 5 · crawler 8 · login_check 3
parser/   cleaner 5 · rules 5 · llm 4 · ocr 3 · extractor 5
storage/  models 5 · database 12
validation/ validator 8 · statistics 7
pipeline/ pipeline 7
```

> 说明：`grep NotImplementedError` 会数到 87，其中 2 处是已实现的 CLI 里的
> `except NotImplementedError` 异常分支，**真实待实现 84 处**。

---

## 二、关键分类：哪些能现在做，哪些必须等门户信息

这是排期的分界线。**不是所有 84 处都同样卡住**：

### A 类：门户无关，可离线实现 + 可自测（约 70 处）

| 文件 | 待实现 | 验证方式 |
| --- | --- | --- |
| `storage/models.py` | 5（DDL / UPSERT / 行映射） | 内存 SQLite 单测 |
| `storage/database.py` | 12（建表、批量写、查询、导出、待人工清单） | 临时库单测 |
| `validation/validator.py` | 8（必填/格式/区间/未知判定、去重） | 构造 `JobRecord` 单测 |
| `validation/statistics.py` | 7（完整率、分布、可复现抽样） | 固定 seed 断言 |
| `parser/cleaner.py` | 5（去标签、全角转半角、分段） | 样本 HTML 单测 |
| `parser/rules.py` | 5（词表匹配、正则匹配、一级抽取） | 样本文章 + 词表单测 |
| `parser/extractor.py` | 5（两级组装、触发判定） | 注入假 LLM 单测 |
| `parser/llm.py` | 4（提示词、JSON 解析与证据校验） | 纯函数单测（不发请求） |
| `parser/ocr.py` | 3（含 OCR 缓存机制） | 缓存命中单测（跳过真实识别） |
| `crawler/crawler.py` | 5（翻页、去重、整批遍历） | 注入 `FakeFetcher` |
| `crawler/detail_page.py` | 4（归档、回放、清单读写） | 临时目录单测 |
| `crawler/list_page.py` | 2（URL 拼接、单页抓取） | 注入 `FakeTransport` |
| `crawler/login_check.py` | 3（门禁、清会话） | 文件系统断言 |
| `pipeline/pipeline.py` | 7（三段编排、装配工厂） | 全链路离线跑 |

### B 类：依赖门户真实结构（约 10 处）——**这部分需要你提供输入**

| 文件 / 函数 | 为什么必须等 |
| --- | --- |
| `crawler/session.py`：`HttpTransport.get/close`、`build_transport` | 登录表单字段名、Cookie 校验地址、是否有反爬跳转 |
| `crawler/list_page.py`：`parse_list` | 详情链接的 CSS 选择器只有真实 DOM 才能定 |
| `crawler/detail_page.py`：`fetch_detail` | 正文容器选择器 |
| `crawler/login_check.py`：`check_login` | 登录页特征（如 `name="password"`） |
| `parser/rules.py`：词表与正则的**最终取值** | 本校专业名、单位后缀、岗位关键词需人工确认 |

### C 类：依赖外部服务 / 真实样本（4 处）

| 函数 | 卡点 |
| --- | --- |
| `parser/ocr.py`：`recognize` | 需装 tesseract + 真实截图，**实测值决定排期是否准确**（现为 1~3 秒/张的估算） |
| `parser/llm.py`：`extract_by_llm` | 需 API Key 或本地 ollama |
| `crawler/session.py`：`throttle` | 纯函数，其实属 A 类（可单测） |

---

## 三、依赖顺序（谁先谁后）

```
                     ┌─ contracts 1.1.0（图片/OCR 一等公民）← P0 前提，先做
                     │
      ┌──────────────┼───────────────┬─────────────────┐
      ▼              ▼               ▼                 ▼
 storage/models  validation/    parser/cleaner    crawler/*（B 类，需门户）
      ▼          validator          ▼
 storage/database   │         parser/rules ──→ parser/extractor
      │              │               ▲              ▲
      │              │        parser/llm(C)   parser/ocr(C)
      └──────────────┴───────────────┴──────────────┘
                     ▼
              pipeline（唯一装配点）→ 三段式 fetch / extract / export
```

**并行度**：`storage`、`validation`、`parser/cleaner+rules`、`crawler(B 类)` 四条线互不依赖，可并行。
**唯一强前置**：`contracts 1.1.0`——所有层都依赖它，所以必须先做（也避免了后面返工）。

---

## 四、分阶段计划与验收门

| 阶段 | 内容 | 依赖 | 验收门（可执行） | 预计 |
| --- | --- | --- | --- | --- |
| **P0** | 契约 1.1.0：`ImageAsset`、`ManifestEntry.images`、`CleanArticle.source_kind`、OCR 缓存路径规则、4 个 OCR 计数键、OCR 来源强制 pending | 仅需决策 | `pytest tests/test_contracts.py -q` 全绿（新增契约断言同步更新） | 半天 |
| **P1** | A 类主干：`storage`(17) + `validation`(15) + `cleaner`(5) + `rules`(通用骨架 5) + `extractor`(5) + `pipeline`(7) | P0 | 用 `tests/fixtures/` 的样本文章，**离线**跑通 `--stage extract` → `--stage export`，产出 `jobs.csv` 非空 | 1~1.5 天 |
| **P2** | 采集层：`session` + `list_page` + `detail_page` + `login_check` | 门户样本 | `python -m src.crawler.login_check` 退出码 0；`--stage fetch` 产出 `manifest.jsonl` + 归档文件 | 0.5 天（信息齐则更快） |
| **P3** | C 类：OCR 缓存落地 + `recognize` 实测 + LLM 兜底 | 真实截图 / API Key | 图片型文章七字段有值；重跑 extract ≤ 秒级（缓存命中） | 0.5 天 |
| **P4** | 业务单测 4 个文件 + `docs/experiment.md` + README 进度回填 | P1~P3 | `pytest -v --cov=src` 全绿且覆盖率 ≥ 70% | 0.5 天 |

**总计约 3~3.5 天**（并行可压缩；P2/P3 的时长取决于你提供门户信息的速度）。

---

## 五、开工前需要你提供的 5 样东西（阻塞项）

| # | 需要什么 | 用在哪 | 没有它怎么办 |
| - | -------- | ------ | ------------ |
| 1 | 门户 BASE + 就业信息列表页地址（可脱敏） | `config.yaml` | B 类无法验收，只能用合成 fixtures 验证链路 |
| 2 | **一份真实列表页 HTML + 一篇文字型详情页 + 一篇图片型详情页**（存 `tests/fixtures/`） | `parse_list` / 选择器 / cleaner 单测 | 同上传，规则准确率无法评估 |
| 3 | 登录方式：账号表单字段名 **或** 浏览器 Cookie | `session` / `login_check` | 采集阶段无法开始 |
| 4 | 词表白名单确认（专业 / 单位后缀 / 岗位关键词） | `config/aliases.yaml` | 误召率高，抽检结论不可信 |
| 5 | OCR 与 LLM 是否参与本次实验（有无 API Key / 能否装 tesseract） | P3 是否做 | 缺失字段保持「未知」，进人工清单 |

> 1~3 项可以先给"脱敏版"（例如把学校域名换成 `example.edu.cn`），我先把代码结构写好，
> 你再把真实值填进 `config.yaml` 与 `.env` —— 这不会让我接触你的账号与密码。

---

## 六、风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 门户真实结构未知 | B 类只能先留插槽 | 用合成 fixtures 保证链路可跑；B 类实现留清晰 TODO 与参数位置 |
| **代写了本该你手写的部分**（登录/选择器/正则） | 违反课程对个人工作的要求 | 见第七节分工边界；开工前确认 |
| OCR 成本估算不准（1~3 秒/张是经验值） | 排期失真 | P3 第一步先用 5 张真实截图实测，再修正 `storage-decision.md` |
| 契约 1.1.0 变更波及所有层 | 后改成本高 | 放在 P0 先做；严格遵守"递增版本→改 contracts→改契约测试→改文档"四步 |
| 图片体积（1000 篇约 600MB） | 磁盘与备份 | 本机可用 912GB，无风险；归档目录已在 `.gitignore` 中 |
| 合规红线被误改（间隔 <2s、并发抓取） | 违规 | 配置校验强制拦截（已实现，且 CLI 覆盖后仍会重新校验） |

---

## 七、分工边界（**开工前需你确认**）

三种可选分工，选一个即可：

| 方案 | 我做什么 | 你做什么 |
| --- | --- | --- |
| **① 保守**（贴合 README 里的声明） | A 类全部 + 测试 + 文档；B 类只留插槽（`TODO(本人实现)` 保留） | 登录、选择器、正则词表最终确认 |
| **② 推荐** | A 类全部 + B 类写成**参数化通用实现**（URL/选择器/字段名全部走配置） | 只填 `config.yaml` 里的地址与选择器字符串，并核对词表 |
| **③ 全包** | 84 处全实现，B 类按常见门户结构猜测并标注"待核对" | 只需提供样本与验收 |

> 方案 ② 的额外价值：登录与选择器一旦参数化，门户改版时不需要改代码，只改配置——
> 这也符合本项目"配置注入、接口可控"的整体设计。

---

## 八、Round 1 的交付物（确认后立刻开工）

**范围**：P0 + P1 的第一半（契约 + storage + validation）

| 产出 | 内容 |
| --- | --- |
| `src/contracts.py` | 升级到 1.1.0（6 项变更，见 `storage-decision.md` 第六节） |
| `tests/test_contracts.py` | 新增契约断言（图片资产、缓存路径、source_kind、计数键） |
| `src/storage/models.py`、`src/storage/database.py` | 17 处实现（标准库 sqlite3，无新依赖） |
| `src/validation/validator.py`、`statistics.py` | 15 处实现 |
| `tests/test_database.py` | 真实单测（临时库，不碰 `data/employment.db`） |
| `docs/architecture.md` | 同步 1.1.0 的接口变更 |

**Round 1 验收命令**：

```bash
cd ~/software-work
.venv/bin/python -m pytest -q                     # 契约测试 + 新增单测全绿
.venv/bin/python -c "import src.storage.database"  # 导入无副作用
```

---

## 附录 A：84 处待实现清单（按文件）

| 文件 | 待实现函数 |
| --- | --- |
| `crawler/session.py` | `HttpTransport.get`、`HttpTransport.close`、`build_transport`、`throttle` |
| `crawler/list_page.py` | `list_page_url`、`parse_list`、`fetch_list` |
| `crawler/detail_page.py` | `fetch_detail`、`archive_html`、`load_archived`、`append_manifest`、`load_manifest` |
| `crawler/crawler.py` | `PortalPageFetcher.fetch_list/fetch_detail/close`、`build_fetcher`、`iter_pages`、`iter_refs`、`fetch_all`、`collect_source_urls` |
| `crawler/login_check.py` | `check_login`、`require_login`、`clear_session` |
| `parser/cleaner.py` | `clean_html`、`strip_tags`、`normalize_text`、`to_halfwidth`、`split_segments` |
| `parser/rules.py` | `load_lexicon`、`build_patterns`、`extract_by_rules`、`match_first`、`match_lexicon` |
| `parser/llm.py` | `is_available`、`build_prompt`、`parse_llm_response`、`extract_by_llm` |
| `parser/ocr.py` | `is_available`、`recognize`、`extract_by_ocr` |
| `parser/extractor.py` | `TwoStageExtractor.extract/extract_from_html/should_call_llm/missing_fields_for_llm`、`build_extractor` |
| `storage/models.py` | `create_table_sql`、`create_index_sql`、`upsert_sql`、`record_to_params`、`row_to_record` |
| `storage/database.py` | `SqliteRepository` 的 11 个方法 + `build_repository` |
| `validation/validator.py` | `DefaultValidator.validate/check_required/check_formats/check_unknowns`、`validate_batch`、`deduplicate`、`needs_manual_review`、`build_validator` |
| `validation/statistics.py` | `summarize`、`sample_for_review`、`render_console`、`format_completeness`、`write_summary`、`field_completeness`、`method_distribution` |
| `pipeline/pipeline.py` | `Pipeline.run_fetch/run_extract/run_export`、3 个内部协作点、`build_pipeline` |
