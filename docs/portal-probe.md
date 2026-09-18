# 门户可达性探测报告（portal-probe.md）

> 探测对象：`https://my.muc.edu.cn/page/11#/notice/noticeList?lo=10&num=0`（中央民族大学信息门户·通知公告）
> 探测时间：本次开发会话内；探测方式：只读、低频、**不登录、不提交任何表单**
> 复现命令：`python3 scripts/probe_portal.py`

---

## 一、结论摘要

**"能不能外部访问"要分三层看，答案是「站点能、数据不能、账号未知」：**

| 层次 | 结论 | 依据 |
| --- | --- | --- |
| ① 站点可达性 | ✅ **校外可访问** | 域名解析到**公网地址** `210.31.15.39`（非 10./192.168./172.16-31. 内网段）；TLS 握手成功，证书 `*.muc.edu.cn`（DigiCert，有效期至 2026-12-22）；直连与经本机代理两种方式都返回响应 |
| ② 业务数据 | 🔒 **必须登录** | 未登录 `GET /page/11` 返回 HTTP **200**，但落点被 302 到统一身份认证：`https://ca.muc.edu.cn/zfca/login?service=http%3A%2F%2Fmy.muc.edu.cn%2Fuser%2FsimpleSSOLogin` |
| ③ 账号可用性 | ❓ **无法验证** | 校外能否用本人账号成功登录，取决于学校的 SSO 策略，只有你本人登录才能确认。**本探测不尝试任何凭据** |

> ⚠️ 注意第 ② 层的关键细节：**未登录时返回的是 HTTP 200**，不是 401/403。
> 只检查状态码会把登录页当成正常页面——这正是本次要修的代码缺口。

---

## 二、探测过程（原始输出）

```
===== 1) DNS：my.muc.edu.cn =====
  ✓ 210.31.15.39  —— 公网地址

===== 2) TLS 握手 =====
  ✓ 握手成功：TLSv1.2
    证书 CN：*.muc.edu.cn　颁发者：DigiCert, Inc.
    有效期至：Dec 22 23:59:59 2026 GMT

===== 3) 发起一次 GET：https://my.muc.edu.cn/page/11 =====
  HTTP 200
    server: nginx
    content-type: text/html;charset=UTF-8
    set-cookie: JSESSIONID=...; Path=/; HttpOnly

===== 4) 判定结论 =====
  ⚠ 未登录：被重定向到统一身份认证
    实际落点：https://ca.muc.edu.cn/zfca/login?service=http%3A%2F%2Fmy.muc.edu.cn%2Fuser%2FsimpleSSOLogin
  ⚠ 页面含登录表单特征：是
  ⚠ 检测到前端加密脚本：['/js/sm2.min.js']
  ✓ 前端形态：未检出 SPA 外壳
  ℹ 正文长度：11393 字节；外链脚本 3 个
      /js/respond.min.js
      /js/jquery-3.7.1.min.js
      /js/sm2.min.js
```

---

## 三、四条对项目有实质影响的发现

### 1. 登录页使用 **SM2 前端加密** → 纯 HTTP 明文提交不可行

登录页加载 `js/sm2.min.js`（国密 SM2），意味着密码在**浏览器里加密后**才提交。
因此 `requests` + 表单明文 POST 的方案在这套门户上**不成立**，只有两条路：

* **playwright**（推荐）：在真实浏览器里填表提交，JS 加密自动完成；
* **手动 Cookie**：在浏览器登录后把 Cookie 复制进 `.env`（`auth.method=cookie`）。

这正是 `config/config.yaml` 里 `request.use_playwright` 被设为 `true` 的原因，
也是 `login_check` 提示语里点明 "sm2" 的原因。

### 2. **必须识别 SSO 重定向**（本次已修复）

`requests`/`playwright` 都会**自动跟随重定向**，因此登录页会以 HTTP 200 的形式返回。
原来的实现只检查状态码与 HTML 特征串，存在把登录页误判成正常页面的风险。
本次补强（契约 `1.2.0`）：

* `HttpResponse` 新增 `final_url`（重定向落点）与 `redirected` 属性；
* `LoginStatus` 新增 `final_url`；
* `session.looks_like_login_redirect()` 用落点判定是否被弹回统一身份认证；
* `login_check` 把这条判定放在 HTML 特征判断**之前**（更可靠）。

### 3. 列表页是 **hash 路由**（`#/notice/noticeList?lo=10&num=0`）→ 分页方式待确认

`#` 之后的部分不会发给服务器，说明该列表很可能由前端 XHR 拉取数据。
两种可能，需要登录后在浏览器「网络」面板确认是哪一种：

* **A. XHR JSON 接口**：那么更稳的方案是直接调用该接口并把响应当 JSON 解析（比解析 DOM 更抗改版）；
* **B. 前端渲染 DOM**：那么必须用 playwright 等待渲染完成后，再用 CSS 选择器抓链接。

当前实现按 B（`use_playwright: true`）准备，A 属于待确认的优化项。

### 4. 其他观察（供你核对，未下结论）

* SSO 的 `service=` 参数指向 **`http://`**（非 https）的 `my.muc.edu.cn/user/simpleSSOLogin`：
  这是学校侧配置，**不影响校外可达性**，但意味着登录回跳可能经过一次 http → https 跳转，
  抓取时要注意 Cookie 作用域与重定向链是否完整。
* 登录页外链脚本只有 `respond.min.js` / `jquery-3.7.1.min.js` / `sm2.min.js`，
  **未检出验证码组件**；但不能排除登录失败若干次后才触发验证码。
* 未登录页面标题与正文为统一身份认证页，**不含任何通知列表数据**（符合预期）。

---

## 四、本次据此完成的项目改动

| 改动 | 文件 | 说明 |
| --- | --- | --- |
| 契约升到 `1.2.0` | `src/contracts.py` | `HttpResponse.final_url` / `redirected`；`LoginStatus.final_url` |
| SSO 落点判定 | `src/crawler/session.py` | `SSO_URL_TOKENS` + `looks_like_login_redirect()` |
| 登录自检用落点判定 | `src/crawler/login_check.py` | 优先判 SSO 重定向；提示语点明 sm2；输出实际落点 |
| 门户实测信息填入配置 | `config/config.yaml` | `base_url` / `list_url` / `login_url` / `login_check_url` / `use_playwright: true` |
| 探测脚本固化 | `scripts/probe_portal.py` | 门户改版时可一键重跑 |
| 新增测试 | `tests/test_crawler.py`、`tests/test_contracts.py` | SSO 判定 6 组参数化用例 + 登录态三条路径 + 契约 2 项 |

---

## 五、仍然「待核对」的事项（⚠️ 已被第八节取代，保留以记录当时的未知项）

| # | 待核对项 | 怎么拿 | 填到哪 |
| - | -------- | ------ | ------ |
| 1 | 列表页**详情链接选择器**（或 XHR 接口地址） | 登录后打开通知列表 → 开发者工具 Elements / Network | `portal.detail_link_selector` |
| 2 | **翻页方式**（hash 参数 / 接口分页 / 滚动加载） | 点第 2 页，观察 URL 与 Network 请求 | `portal.page_param` + 采集逻辑 |
| 3 | **详情页正文容器选择器** | 打开一条通知，取正文外层元素的 class | 解析层清洗容器（`cleaner.CONTAINER_SELECTORS`） |
| 4 | 登录是否**必须人工验证码** | 用浏览器手动登录一次看有无验证码 | 决定 `auth.method` 用 account 还是 cookie |
| 5 | 词表白名单（院系/专业/单位后缀/岗位） | 对照真实文章人工确认 | `config/aliases.yaml` |

> ⚠️ 请**不要**把学号、密码、Cookie 贴进对话或提交到仓库：
> 只把「选择器字符串 / 接口路径 / 翻页规则」告诉我即可，凭据你自己写进 `.env`。

---

## 六、复现与合规

```bash
# 重新探测（只读、低频、不登录）
python3 scripts/probe_portal.py
python3 scripts/probe_portal.py --use-proxy "https://my.muc.edu.cn/page/11"

# 登录自检（未配置凭据时会明确告诉你缺什么，不会尝试登录）
python -m src.crawler.login_check
python -m src.crawler.login_check --check-config-only
```

**合规声明**：本探测仅访问公开的登录页/首页各一次，未尝试任何凭据、未提交任何表单、
未遍历目录、未并发请求。项目其余部分的合规红线见 `README.md` 第九节
（本人账号、间隔 ≥2 秒、只读低频、不公开不外传、实验结束清会话）。

---

## 七、接口细节（由本人从浏览器 Network 面板提供，已用于实现）

登录后抓取到的真实请求，取代了第三节里「A/B 两种可能」的猜测——**是 A：JSON 接口**。

| 项 | 实测值 |
| --- | --- |
| 列表接口 | `POST https://my.muc.edu.cn/comsys-portal-notice-web/getNoticeByPage` |
| 请求头 | `Content-Type: application/json;charset=UTF-8` |
| 翻页 | **URL 不变**，靠请求体里的 `currentPage` 翻页 |
| 防缓存/令牌 | 请求体里的 `comsys_random_t` 每次请求都变 |
| 详情链接 | 由 `notice_id` + `organization_id` 两个参数构成 |
| 详情正文容器 | `<div id="content" class="content_all" style="min-width: 1260px;">` |
| 登录验证码 | **无**（因此 account 方式用 playwright 自动登录可行） |

### 据此实现的「API 模式」

列表采集不再解析 DOM，而是 POST JSON 并解析响应；**字段名、取值路径、详情链接模板全部走配置**，
门户改版时只改 `config/config.yaml`，不改代码：

| 配置项 | 作用 | 实测值 |
| --- | --- | --- |
| `portal.mode` | `html`（解析 DOM）/ `api`（POST JSON） | `api` |
| `portal.api_url` | 列表接口地址 | `…/getNoticeByPage` |
| `portal.api_page_field` | 页码字段名 | `currentPage` |
| `portal.api_token_field` | 随机令牌字段名 | `comsys_random_t` |
| `portal.api_body` | 请求体模板（其余字段原样发送） | 🔎 待补全 |
| `portal.api_list_path` | 响应里列表数组的路径 | 🔎 待确认 |
| `portal.api_title_field` / `api_date_field` | 列表项标题/时间字段名 | 🔎 待确认 |
| `portal.detail_url_template` | 详情链接模板，占位名取自接口字段 | 🔎 待确认 |

代码位置：`src/crawler/list_page.py`（`build_api_body` / `dig` / `parse_list_json` /
`fetch_list_api`），分发在 `src/crawler/crawler.py::PortalPageFetcher.fetch_list`。

---

## 八、当前的「待核对」清单（更新版）

### 8.1 必须你确认（否则列表页数据拿不到）

**已由本人提供（不再需要）**：完整 Request Payload 字段、`pageSize=15`、`type="10"`、
`orgId=bks1044104407202525rg2`、`total=1040`、`totalCounts=70`、`comsys_random_t` 的真实格式、
详情链接由 `notice_id`+`organization_id` 构成、正文容器 `div.content_all`、登录无验证码。

由此**已实现**：请求体按实填写、`start/end` 由页码推导、令牌按 `Math.random()` 格式生成、
总页数解析（`page.totalCounts`）、组织 ID 兜底、`page_end=0` 自动翻页。

| # | 仍需确认 | 怎么拿 | 影响 |
| - | -------- | ------ | ---- |
| 1 | **响应里"通知数组"的键名** | Response 标签页：你已给出 `page` 对象，但还差**存放通知数组的那个键**（`data.list`？`notices`？`rows`？） | 不填也能跑——程序会按常见命名**自动识别**并在日志里打出实际路径，届时写回 `api_list_path` 即可 |
| 2 | 列表项里**标题与时间的字段名** | Response 里看列表项对象 | 同上，已支持自动识别（`title/noticeTitle`、`publishTime/releaseTime` 等） |
| 3 | **`comsys_random_t` 是否被服务端校验** | 重放请求并把该值改成任意字符串，看是否报错 | 决定随机值是否够用；若校验则必须从页面取 |
| 4 | ~~详情页真实路由~~ | ✅ 已由本人提供：`https://my.muc.edu.cn/page/11#/print?notice_id=352209&show_type=1&type=10` | 只需 `notice_id`，不需要 `organization_id`；已写入配置 |
| 5 | `state` / `ggState` / `role` 等**回显字段是否必需** | 保持现状跑一次，若接口报参数缺失再补 | 影响 `api_body` 完整性 |
| 6 | 通知是否有**纯附件（PDF）** | 抽查几条 | 若有，附件需单独下载 + OCR |

### 8.2 仍需人工判断

| # | 待核对 | 填到哪 |
| - | ------ | ------ |
| 5 | 词表白名单（院系 / 专业 / 单位后缀 / 岗位） | `config/aliases.yaml`（标了「【本人确认】」的分组） |
| 6 | 登录用 account（playwright 自动填表）还是 cookie（手动复制） | `auth.method` |
| 7 | 详情正文是否也在 `#content` 内（部分通知可能是附件 PDF） | 附件需要单独下载与 OCR |

> 💡 有了 8.1 的第 1、2 项，我可以把 `config.yaml` 一次填好并加上**基于真实响应的夹具测试**；
> 有了第 3 项，才能确定翻页是否稳定。

---

## 九、实测响应结构（tables）与站外链接 —— 采集策略由此确定

本人从 Response 里提供的真实列表项，解决了第八节的第 1、2 项，并暴露出一个**影响采集策略**的事实。

### 9.1 响应结构

```jsonc
{
  "page": { "currentPage": 2, "pageSize": 15, "total": 1040, "totalCounts": 70, ... },
  "tables": [ { /* 通知对象 */ }, ... ]        // ← 列表数组的键是 tables
}
```

单条通知的真实字段（节选）：

| 字段 | 示例 | 用途 |
| --- | --- | --- |
| `notice_title` | `体制转外贸｜"职"点迷津校友分享会（第21期）` | **标题** |
| `notice_id` | `358715`（**整数**） | 拼详情链接 |
| `organization_id` | `"11027"`（**与 page.orgId 不是同一套 ID**） | 拼详情链接 |
| `notice_release_time` | `"2026-09-16 10:15"` | **发布时间**（另有 `notice_first_time`） |
| `notice_link_state` | `1` | **是否站外** |
| `notice_link` | `https://mp.weixin.qq.com/s/...` | **正文真实地址** |
| `notice_type` / `notice_type_name` | `10` / `就业信息` | 栏目（与 `type: "10"` 对应） |
| `notice_content` | `""` | 列表接口**不返回正文** |

已据此把 `config.yaml` 填死：`api_list_path: tables`、`api_title_field: notice_title`、
`api_date_field: notice_release_time`；并把 `detail_org_id` **清空**——
因为记录自带的 `organization_id`（11027）与 `page.orgId`（bks…）不是同一套 ID，
混用会静默生成打不开的链接。

### 9.2 ⚠️ 关键发现：大量通知的正文**不在门户上**

样两条通知的 `notice_link_state` 都是 1，`notice_link` 分别指向
**微信公众号文章** 与 **腾讯文档**；`notice_content` 为空。也就是说：

* 门户详情页对这些通知很可能只是「跳转壳」；
* 真正要抽取的正文在微信/腾讯文档的页面上。

因此新增 `portal.external_link_policy`，把选择权交给本人：

| 取值 | 行为 | 适用 |
| --- | --- | --- |
| `fetch`（默认） | 用同一个浏览器会话去抓 `notice_link` 当正文 | 想真正拿到内容；微信文章需 playwright 渲染 |
| `portal` | 只抓门户详情页（站外链接仅记录在清单里） | 只关心门户内信息 |
| `skip` | 跳过站外通知 | 先跑通门户内的那部分 |

**幂等键仍取门户详情页 URL**（`article_key = sha1(portal_detail_url)`），
因此无论选哪种策略、正文来自哪里，同一篇通知的主键与归档位置都不变——
这一条由 `ArticleRef.content_url` / `fetch_url` 两个概念实现（契约 1.4.0）。

### 9.3 第八节清单的现状

| 原待核对项 | 现状 |
| --- | --- |
| 1. 列表数组键名 | ✅ 已确认 `tables`，已写入配置 |
| 2. 标题/时间字段名 | ✅ 已确认 `notice_title` / `notice_release_time` |
| 3. `comsys_random_t` 是否被校验 | ✅ **已确认不被校验**：本人重放请求并把该值改成任意随机数，接口仍回复成功 → 随机值即可（当前实现正确） |
| 4. 详情页真实路由 | ⏳ 仍需你点开一条通知复制地址栏 |
| 5. `state`/`ggState` 等回显字段 | ⏳ 跑一次看是否报参数缺失 |
| 6. 是否有纯附件通知 | ⏳ 抽查；现已确认「站外链接」是更常见的情况 |

### 9.4 接口是 fetch() 调的，且**响应头为空**

本人从控制台给出的 Response 对象：

```
ok: true / status: 200 / redirected: false / type: "basic"
headers: Headers {}          ← 没有任何响应头（包括 Content-Type）
url: "https://my.muc.edu.cn/comsys-portal-notice-web/getNoticeByPage"
```

| 观察 | 结论 / 处置 |
| --- | --- |
| `headers: Headers {}` | **不能依赖 charset 声明解码**：JSON 按 RFC 8259 固定 UTF-8，`post_json` 改为严格 UTF-8（失败再退 gb18030），避免短响应被字符集推断误判成乱码 |
| 由 `fetch()` 发起、无自定义鉴权头 | 接口鉴权**只靠 Cookie** → 说明**列表接口不必走浏览器**，只要有登录后的 Cookie 用普通 HTTP 即可（为「列表走 requests、详情走浏览器」的混合模式留了路） |
| `redirected: false`、`status: 200` | 登录态下接口不重定向，`final_url` 逻辑无需特殊处理 |

### 9.5 ⚠️ 接口有业务应答外壳：失败也是 HTTP 200

本人实测「改随机数后 msg 回复成功」，同时说明响应外壳里带 **`msg`**（大概率还有 `code`、
`success` 之类）。这带来一个必须防的坑：

> **会话过期时，接口会以 HTTP 200 返回失败外壳**（例如 `{"code":401,"msg":"未登录"}`），
> 而不是 4xx。若翻页逻辑只看"这一页有没有数据"，就会把**失败**当成**已到最后一页**，
> 采集静默提前结束——数据无声变少，最难以察觉的一类故障。

已做的两处加固：

1. **错误信息带外壳摘要**：找不到列表数组时，报错里直接给出 `code`/`msg`
   （提示优先怀疑「登录态失效」而不是「配置写错」）；
2. **区分「抓取失败」与「本来就空」**：自动翻页遇到失败会**停下来并记 ERROR 日志**
   （含已收集条数），不再当作正常结束；显式范围下仍是「单页失败只跳过该页」。

### 9.6 详情页 URL（已确认）

```
https://my.muc.edu.cn/page/11#/print?notice_id=352209&show_type=1&type=10
```

| 观察 | 含义 |
| --- | --- |
| 路由是 `#/print` 而非 `#/notice/noticeDetail` | 这是**可打印视图**：正文更完整、导航干扰更少，适合抽取（但仍属 SPA 的 hash 路由，需 playwright 渲染后才能拿到 DOM） |
| 只需 `notice_id` | `organization_id` **不需要**——因此 `detail_org_id` 兜底置空；`page.orgId` 那套 ID 与本链接无关 |
| `show_type=1` / `type=10` 是固定字面量 | `type=10` 即「就业信息」栏目，与列表接口的 `type: "10"` 一致 |
| 正文容器 | 仍是 `<div id="content" class="content_all">`（见第三节） |

> 💡 `#/print` 视图的存在提示：门户很可能还有一个「按 ID 取通知正文」的接口
> （前端渲染打印页时必然要取数据）。若能拿到该接口，详情抓取可以省掉浏览器渲染、
> 直接拿结构化正文 —— 那会让整条链路更快也更稳（可选优化，非必需）。
