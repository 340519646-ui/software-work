# 运行手册（runbook.md）

> 给**自己**看的操作说明：怎么跑、看哪里、改什么、出问题怎么查。
> 项目结构见 `README.md`；接口细节见 `docs/architecture.md`；实测结论见 `docs/experiment.md`。

---

## 0. 一句话

**在 WSL 里进项目目录，用项目自带的 `.venv` 跑三段式管线，然后用浏览器打开看板。**

```bash
cd ~/software-work
bash scripts/run_all.sh            # 采集 → 解析 → 导出 → 生成看板
```

---

## 1. 环境：哪些已经就绪，哪些要重装

| 项 | 状态 | 说明 |
| --- | --- | --- |
| `.venv/` | ✅ 已就绪 | 已装 requests / beautifulsoup4 / lxml / pandas / openpyxl / playwright + chromium 内核 / pytest |
| `.env` | ✅ 已填 | 学号与密码（**只在本地，已 gitignore**） |
| `config/config.yaml` | ✅ 已按实测填好 | 接口地址、`datas.tables`、查询串参数、详情模板、`use_playwright: true` |
| 会话 `.session.json` | ✅ 已生成 | 首次登录后落盘，后续运行直接复用 |

⚠️ **必须用 `.venv/bin/python`**（系统 `python3` 没装这些依赖）。两种写法等价：

```bash
.venv/bin/python -m src.pipeline.run --stage extract     # 直接用 venv 里的解释器
source .venv/bin/activate && python -m src.pipeline.run --stage extract   # 或先激活
```

重装环境（换机器/清空后）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium     # 约 150MB
cp .env.example .env                                # 然后填学号密码
```

---

## 2. 怎么跑：三段式

管线分三段，可以分开跑，也可以一键跑完：

```bash
cd ~/software-work

# ① 采集（联网；会登录门户、调列表接口、抓详情页与图片、写归档与清单）
.venv/bin/python -m src.pipeline.run --stage fetch

# ② 解析（**离线**；读归档 HTML → 清洗 → 规则抽取 → LLM 兜底 → 校验去重 → 入库）
.venv/bin/python -m src.pipeline.run --stage extract

# ③ 导出（生成 CSV / Excel / 待人工清单 / 统计摘要）
.venv/bin/python -m src.pipeline.run --stage export

# ④ 看板（把上面产物汇总成一个网页）
.venv/bin/python scripts/make_report.py
```

**一键跑**（等价于上面四步，失败即停）：

```bash
bash scripts/run_all.sh                      # 完整流程
bash scripts/run_all.sh --pages 1 2          # 只采集第 1~2 页
bash scripts/run_all.sh --stages extract,export   # 只重解析+导出（秒级，不联网）
```

**登录自检**（改过密码、或怀疑会话过期时先跑它）：

```bash
.venv/bin/python -m src.crawler.login_check
# 期望输出：登录态有效（方式：account，探测：…）
# 退出码 0=有效  2=未登录  3=配置错误
```

---

## 3. 怎么查看成果

| 想看什么 | 打开什么 | 路径 |
| --- | --- | --- |
| **一眼看全貌（推荐）** | 成果看板（网页） | `data/processed/report.html` |
| 结构化表格 | Excel | `data/processed/jobs.xlsx` |
| 纯文本表格 | CSV | `data/processed/jobs.csv` |
| 需要人工复核的条目 | CSV | `data/processed/manual_review.csv` |
| 统计摘要 | JSON | `data/processed/summary.json` |
| 抓取当时的原始页面 | HTML（本地归档） | `data/raw/html/<key>.html` |
| 采集清单（URL ↔ 归档） | JSONL | `data/raw/manifest.jsonl` |

**在 Windows 里打开**（WSL 的文件路径可以直接访问）：

- 资源管理器地址栏粘：`\\wsl.localhost\Ubuntu\home\user_01\software-work\data\processed\`
  然后双击 `report.html`（看板）或 `jobs.xlsx`（Excel）
- 或在 WSL 里执行 `explorer.exe /home/user_01/software-work/data/processed` 弹出资源管理器

**看板里能看到什么**：概览卡片（记录数/归档数/图片数/七项齐备数）、七项字段命中率、每条通知的七项结果 +
命中依据（原文片段）+ 门户链接 + 本地归档链接，以及待人工清单；顶部还能按标题筛选、勾选「只看有命中的」。

> 注：DSH Web GUI 右侧的文件/预览面板绑定的是**当前会话的工作区**（`~/test_app`），
> 不会显示 `~/software-work`。要看这个项目，用上面的 UNC 路径打开，或把会话工作区切到该项目再开。

---

## 4. 控制采集范围与时间成本

在 `config/config.yaml` 的 `portal` 段：

| 配置 | 含义 | 当前值 |
| --- | --- | --- |
| `page_start` / `page_end` | 采集第几页到第几页 | `1` ~ `2`（试跑） |
| `page_end: 0` | **自动翻到最后一页**（翻到空页即停） | —— |
| `api_body.pageSize` | 每页条数 | `15` |
| `api_body.type` | 栏目：`"10"` = 就业信息 | `"10"` |

也可以不改配置、直接用命令行覆盖：

```bash
.venv/bin/python -m src.pipeline.run --stage fetch --page-start 3 --page-end 5
```

**时间成本（实测）**：约 **16 秒/条**（浏览器渲染 + 每次请求 ≥2 秒的合规限速 + 图片下载），
30 条约 8 分钟。栏目共约 **1040 条 / 70 页**，全量约 **4~5 小时** —— 建议**分批采集**
（例如每次 10 页），`fetch` 会自动跳过已入库的 URL（`skipped_existing` 计数可验证）。

---

## 5. 改了规则或词表之后，怎么重跑

**不需要重新联网**——解析阶段完全离线，几秒钟就能重跑：

```bash
# 1) 改 config/aliases.yaml（词表）或 src/parser/rules.py（正则）
# 2) 重跑解析与导出（读本地归档，不联网）
bash scripts/run_all.sh --stages extract,export
# 3) 重新生成看板
.venv/bin/python scripts/make_report.py
```

实测：30 篇的解析 + 导出约 **2 秒**。这就是「归档 + 清单」设计的意义所在。

---

## 6. 常用参数与开关

`config/config.yaml` 里改：

| 想做的事 | 改哪里 |
| --- | --- |
| 开启 LLM 兜底（长尾专业名等） | `llm.enabled: true` + `.env` 的 `LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY` |
| 开启图片 OCR | `ocr.enabled: true`（需先装 `tesseract-ocr` 与中文语言包） |
| 站外通知怎么处理 | `external_link_policy`: `fetch`（抓微信/文档正文）/ `portal`（只抓门户页）/ `skip` |
| 加快/放慢试跑 | `request.interval_seconds`（**不得小于 2**，配置校验会直接报错） |
| 抽检比例 | `sampling.review_rate`（默认 10%）、`sampling.seed`（固定种子，可复现） |

命令行临时覆盖：`--use-llm` / `--no-llm` / `--use-ocr` / `--no-ocr` / `--json`。
其中覆盖后的配置**会重新校验**，因此命令行也无法绕过「间隔 ≥2 秒」这条红线。

---

## 6.5 检索服务（类百度页面 + 三级缓存）

成果不必只看静态文件，也可以起一个**只监听本机**的检索服务：

```bash
# 启动（默认 http://127.0.0.1:8765/），加 --open 会自动开浏览器
.venv/bin/python -m src.search.server
.venv/bin/python -m src.search.server --port 8790 --open

# 直接调 API（便于脚本化验证）
curl -s "http://127.0.0.1:8765/api/stats"
curl -s --get --data-urlencode "q=北京 硕士" "http://127.0.0.1:8765/api/search"
curl -s "http://127.0.0.1:8765/api/search?q=%E6%8B%9B%E8%81%98&limit=5"
```

| 项 | 说明 |
| --- | --- |
| 监听地址 | **只允许 127.0.0.1 / localhost / ::1**（配置校验强制），对外监听会配置报错 |
| 数据来源 | 现成的 `data/employment.db`（先跑 `--stages extract,export` 才有数据） |
| 三级缓存 | L1 进程内 → L2 查询缓存表 → L3 FTS5；写入后靠**数据版本号**自动失效 |
| 采集开关 | `search.trigger_enabled` **默认 false**：默认完全离线，不会发任何请求 |
| 分词器 | 默认 `unicode61`（两字中文可搜）；改 `trigram` 可做子串匹配 |
| 测试 | 检索层专项：`.venv/bin/python -m pytest tests/test_search.py -q` |

**开启「缓存未命中就采集」前请先确认**（默认关闭，属于会真实发请求的动作）：

```yaml
search:
  trigger_enabled: true      # 总开关
  max_fetch_pages: 2         # 每页 15 条 → 31 次请求 ≈ 62 秒（按 ≥2 秒/请求算）
  fetch_cooldown_seconds: 900
```

页面会把「预估请求数 / 预估秒数」显示出来再由用户确认，采集在**后台线程**跑
（前端轮询进度），不会卡住页面。详见 `docs/search.md`。

检索层排查：

| 现象 | 处理 |
| --- | --- |
| 页面能开但搜不到东西 | `/api/stats` 看 `indexed_rows`；为 0 说明还没跑 extract |
| 明明存在却搜不到（2 字词） | 检查 `search.fts_tokenizer` 是否被改成 `trigram` |
| 改了匹配逻辑结果没变 | L2 缓存不会因改代码失效；清一次 `search_query_cache` 表即可 |
| 采集按钮点不了 | `trigger_enabled=false` 或冷却中，页面会显示 `blocked_reason` |

---

## 7. 退出码

| 退出码 | 含义 | 怎么办 |
| --- | --- | --- |
| 0 | 阶段成功 | —— |
| 1 | 阶段跑完但有错误（详见输出里的 counters/errors 与日志） | 看输出里的 `errors`；常见是单条详情页失败，不影响整批 |
| 2 | 参数用法错误 | 检查 `--stage` 取值等 |
| 3 | 配置错误 | 按报错提示改 `config/config.yaml`（例如间隔 < 2、`base_url` 没填） |
| 4 | 登录态无效 | 检查 `.env` 里的学号密码；或把 `auth.method` 改成 `cookie` |

---

## 8. 跑测试

```bash
.venv/bin/python -m pytest -q                       # 全量（335 项，约 12 秒）
.venv/bin/python -m pytest -q --cov=src             # 带覆盖率（当前 85%）
.venv/bin/python -m pytest tests/test_portal_api.py -q   # 只跑接口模式相关
```

改代码前后都跑一下：`tests/test_contracts.py` 会检查「层间依赖方向、禁止各模块自行读配置」等接口约束。

---

## 9. 常见问题排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `ModuleNotFoundError` | 用了系统 `python3`；改用 `.venv/bin/python` |
| `登录态无效：未提供登录凭据` | `.env` 没填或没在项目根目录；`AUTH_STUDENT_ID` / `AUTH_PASSWORD` |
| 被重定向到统一身份认证 | 会话过期 → 重跑 `login_check`（会自动重新登录并落盘会话） |
| 翻页只回 10 条、`total` 是 44944 | 参数没走到查询串（`api_param_style` 应为 `query`）——见 `docs/experiment.md` 11.3 第 2 条 |
| 归档 HTML 里只有「加载通知公告数据中…」 | 页面还没渲染完就抓了（`session.py` 的 `_wait_for_render` 负责等待） |
| `找不到列表数组` | 响应结构变了；日志里会打印实际路径，把它写进 `portal.api_list_path` |
| 字段大多为「未知」 | 见 `docs/experiment.md` 第十一节：当前栏目的通知本身不含七项信息，需换语料或开 LLM/OCR |
| 想看某条到底抽到了什么 | 打开看板的「依据」列，或在 `manual_review.csv` 里看 evidence |

---

## 10. 合规提醒（别忘）

1. **只用自己的账号**，不共享、不代采集；
2. **间隔 ≥2 秒**是硬约束，不要改小、不要并发；
3. 数据与登录态只留本地，**不公开、不外传**；
4. **实验结束清理会话**：`.venv/bin/python -m src.crawler.login_check --clear-session`；
5. OCR 结果必须人工复核后才算数。
