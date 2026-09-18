"""传输层：会话、限速、重试、UA / Cookie / 可选代理。

实现 ``src.contracts.Transport``。

职责
----
只把 URL 变成 ``HttpResponse``，**不做任何 HTML 解析**、不写文件、不碰数据库。

硬性要求（由 docs/architecture.md 与契约测试共同约束）
--------------------------------------------------
1. **限速**：同一实例内相邻两次请求的间隔不得小于
   ``cfg.request.interval_seconds``（默认且下限 2 秒），重试之间同样要间隔。
   禁止通过并发/线程池绕过限速——本实验是低频只读访问。
2. **凭据**：只在内存中持有会话，禁止把学号/密码/Cookie 写入日志或抛进异常文本。
3. **错误**：网络异常、超时统一转换为 ``FetchError``；
   非 2xx 状态不抛异常，而是填入 ``HttpResponse.status_code`` 交给上层判定。
4. **配置**：全部来自构造函数注入的 ``AppConfig``，不得在本模块读 yaml/.env。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from src.config import AppConfig
from src.contracts import ConfigError, FetchError, HttpResponse, PipelineError, Transport

DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
LOGIN_FORM_MARKERS: tuple = ('type="password"', 'name="password"', "name='password'", "用户名", "密码")
"""判定「页面其实是登录页」的特征串——登录自检与采集门禁共用。"""

SSO_URL_TOKENS: tuple = ("login", "sso", "cas", "zfca", "auth", "signin", "passport")
"""统一身份认证落点的 URL 特征。

实测：未登录访问 ``https://my.muc.edu.cn/page/11`` 会被 302 到
``https://ca.muc.edu.cn/zfca/login?service=...``；这类落点含有 login / zfca / cas 等片段。
"""


def looks_like_login_redirect(requested_url: str, final_url: str) -> bool:
    """是否被重定向到了统一身份认证（纯函数，便于单测）。

    这是登录态判定里**最强**的信号：requests/playwright 都会自动跟随重定向，
    如果不看落点，返回码仍是 200，登录页会被误判成目标页面。
    """
    if not final_url or final_url == requested_url:
        return False
    lowered = final_url.lower()
    return any(token in lowered for token in SSO_URL_TOKENS)

IMAGE_ACCEPT = "image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5"
"""图片请求的 Accept 头。"""


SPA_MIN_CONTENT_CHARS = 200
"""判定「页面正文已渲染」的最小可见文本长度。

实测：门户的 ``#/print`` 详情页在 DOMContentLoaded 时还是一个空壳，
只有「加载通知公告数据中…」约 60 余字；正文是之后异步取的。
"""

SPA_RENDER_TIMEOUT_SECONDS = 20.0
"""等待 SPA 渲染的最长时间；超时后仍返回当前 HTML（不当成失败，但要如实记录）。"""


LOGIN_TYPE_USERNAME_PASSWORD = "username_password"
"""统一身份认证表单里 ``loginType`` 的取值（实测 本校门户默认值）。"""


def _decode_json(response: Any, cfg: AppConfig) -> str:
    """解码 JSON 响应：按 RFC 8259 以 UTF-8 为准，失败再退 gb18030。

    **为什么不能用 _decode**：实测门户的接口响应**没有 Content-Type 头**
    （浏览器里看到 ``headers: Headers {}``），因此没有 charset 可依；
    而 _decode 会退回 ``apparent_encoding`` 的字符集推断——
    对短响应（例如只有一两条记录）可能误判成 gbk/latin-1，把中文变成乱码。
    JSON 的报文编码由规范固定为 UTF-8，这里就按规范来。
    """
    if cfg.request.encoding:
        return response.content.decode(cfg.request.encoding, errors="replace")
    for encoding in ("utf-8", "gb18030"):
        try:
            return response.content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return response.content.decode("utf-8", errors="replace")


def _load_requests():
    """惰性导入 requests：未安装时给出可执行的修复提示，而不是 ImportError 堆栈。"""
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise FetchError(
            "缺少依赖 requests：请执行 pip install -r requirements.txt", detail=str(exc)
        ) from exc
    return requests


def _decode(response: Any, cfg: AppConfig) -> str:
    """按「配置指定编码 → 响应头声明 → 自动探测 → utf-8」的顺序解码页面。"""
    if cfg.request.encoding:
        return response.content.decode(cfg.request.encoding, errors="replace")
    declared = (response.encoding or "").lower()
    if declared and declared not in ("iso-8859-1", "ascii"):
        return response.text
    try:
        apparent = response.apparent_encoding or "utf-8"
    except Exception:  # pragma: no cover - 探测失败时退回 utf-8
        apparent = "utf-8"
    return response.content.decode(apparent, errors="replace")



class HttpTransport:
    """基于 requests（或 playwright）的 Transport 实现。

    参考实现要点
    ------------
    * ``__init__``：按 ``cfg.auth`` 准备请求头与 Cookie（account 方式需先登录，
      cookie 方式直接用 ``cfg.auth.cookie``）；初始化 ``_last_request_at`` 时间戳。
    * ``get()``：进入前先 ``_throttle()`` 补齐间隔，再发请求；失败按
      ``cfg.request.retries`` 重试并退避，仍失败则返回带 ``error`` 的 HttpResponse。
    * ``close()``：关闭底层会话与（若启用）playwright browser context。
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._session = None
        self._last_request_at = 0.0

    def get(self, url: str, *, referer: str = "", binary: bool = False) -> HttpResponse:
        requests = _load_requests()
        session = self._ensure_session(requests)
        cfg = self._cfg

        started = time.monotonic()
        last = HttpResponse(url=url)
        attempts = max(1, int(cfg.request.retries) + 1)

        for attempt in range(attempts):
            self._sleep_before_request()
            headers = {"Referer": referer} if referer else {}
            if binary:
                headers["Accept"] = IMAGE_ACCEPT
            try:
                response = session.get(
                    url,
                    timeout=cfg.request.timeout,
                    verify=cfg.request.verify_tls,
                    headers=headers or None,
                )
            except Exception as exc:
                last = HttpResponse(
                    url=url,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            else:
                elapsed = int((time.monotonic() - started) * 1000)
                final_url = str(getattr(response, "url", "") or url)
                if binary:
                    return HttpResponse(
                        url=url,
                        status_code=response.status_code,
                        content=response.content,
                        final_url=final_url,
                        elapsed_ms=elapsed,
                    )
                return HttpResponse(
                    url=url,
                    status_code=response.status_code,
                    text=_decode(response, cfg),
                    final_url=final_url,
                    elapsed_ms=elapsed,
                )
            finally:
                # 失败也要记账：重试之间的间隔同样受限于合规红线
                self._last_request_at = time.monotonic()

            if attempt < attempts - 1:
                continue
        return last

    def post_json(self, url: str, payload: Mapping[str, Any], *, referer: str = "") -> HttpResponse:
        """以 JSON 形式 POST（列表接口）。与 get 共用同一会话与限速。"""
        requests = _load_requests()
        session = self._ensure_session(requests)
        cfg = self._cfg

        headers = {"Content-Type": "application/json;charset=UTF-8"}
        if referer:
            headers["Referer"] = referer

        started = time.monotonic()
        last = HttpResponse(url=url)
        attempts = max(1, int(cfg.request.retries) + 1)

        for attempt in range(attempts):
            self._sleep_before_request()
            try:
                response = session.post(
                    url,
                    json=dict(payload),
                    timeout=cfg.request.timeout,
                    verify=cfg.request.verify_tls,
                    headers=headers,
                )
            except Exception as exc:
                last = HttpResponse(
                    url=url,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            else:
                return HttpResponse(
                    url=url,
                    status_code=response.status_code,
                    text=_decode_json(response, cfg),
                    final_url=str(getattr(response, "url", "") or url),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            finally:
                self._last_request_at = time.monotonic()
        return last

    def _sleep_before_request(self) -> None:
        """按合规红线补齐间隔（≥ cfg.request.interval_seconds）后再发请求。"""
        wait = throttle(self._cfg.request.interval_seconds, self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _ensure_session(self, requests: Any):
        """首次使用时构造会话：UA、代理、Cookie 只在这里设置一次。"""
        if self._session is not None:
            return self._session
        cfg = self._cfg
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": cfg.request.user_agent,
                "Accept": DEFAULT_ACCEPT,
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        if cfg.request.proxy:
            session.proxies = {"http": cfg.request.proxy, "https": cfg.request.proxy}
        if cfg.auth.method == "cookie" and cfg.auth.cookie.strip():
            session.headers["Cookie"] = cfg.auth.cookie.strip()
        self._session = session
        return session

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def __enter__(self) -> "HttpTransport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def build_transport(cfg: AppConfig) -> Transport:
    """工厂：按 ``cfg.request.use_playwright`` 选择 requests 版或 playwright 版实现。

    调用方（crawler.py）只依赖返回的 ``Transport`` 协议，不感知具体实现，
    这样「门户需要 JS 渲染」这件事不会扩散到其他模块。
    """
    if cfg.request.use_playwright:
        return PlaywrightTransport(cfg)
    return HttpTransport(cfg)


class PlaywrightTransport:
    """基于 playwright 的 Transport：门户需要 JS 渲染或必须脚本化登录时启用。

    **待核对（门户相关，必须按本校实际结构确认后再启用）**：

    * 登录表单的字段选择器与提交按钮（见 :meth:`login` 里的选择器列表）；
    * 登录成功的判定条件（跳转 URL 还是特定元素出现）；
    * 是否需要 ``wait_for_selector`` 等待异步渲染的正文。
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._last_request_at = 0.0

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        cfg = self._cfg
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise FetchError(
                "缺少依赖 playwright：请执行 pip install playwright && playwright install",
                detail=str(exc),
            ) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        context_args: Dict[str, Any] = {"user_agent": cfg.request.user_agent}
        session_file = cfg.path(cfg.auth.session_file) if cfg.auth.session_file else None
        if session_file is not None and session_file.exists():
            context_args["storage_state"] = str(session_file)
        self._context = self._browser.new_context(**context_args)
        self._page = self._context.new_page()
        return self._page

    def login(self) -> None:
        """脚本化登录：**走页面自带的 doLogin**（由它完成 sm2 加密与提交）。

        实测流程（本校统一身份认证）：

        1. 先访问门户页面 → 被 302 到 ``ca.muc.edu.cn/zfca/login``，
           URL 上自动带上正确的 ``service`` 参数（这样登录后才会被带回门户）；
        2. 登录页自带 ::

               function doLogin(params) {
                   params.password = sm2.encrypt(params.password, config.sm2.publicKey);
                   $("#loginForm input[name='password']").val(params.password);
                   $("#loginForm input[name='loginType']").val(params.type);   // "username_password"
                   $("#loginForm input[name='submit']").click();
               }

           密码在浏览器里被 SM2 加密后提交，因此**必须**调它，
           手动 ``page.fill`` + ``click`` 会提交明文密码而失败。
        3. 登录成功后回到门户，把会话（storage_state）落盘复用。
        """
        cfg = self._cfg
        page = self._ensure_page()
        entry = cfg.portal.login_check_url or cfg.portal.base_url or cfg.portal.login_url
        if not entry:
            raise ConfigError("portal.base_url / login_check_url / login_url 均为空：无法确定登录入口")

        page.goto(entry, wait_until="domcontentloaded", timeout=cfg.request.timeout * 1000)

        password_input = page.locator("#loginForm input[name='password']")
        if password_input.count() > 0:
            params = {
                "username": cfg.auth.student_id,
                "password": cfg.auth.password,
                "type": LOGIN_TYPE_USERNAME_PASSWORD,
            }
            try:
                page.evaluate("(p) => doLogin(p)", params)
            except Exception as exc:
                raise FetchError(
                    "登录页未提供 doLogin（门户改版？）：请在浏览器手动登录后改用 auth.method=cookie",
                    detail=str(exc),
                ) from exc

            # 等待 CAS 回跳门户
            try:
                page.wait_for_load_state("networkidle", timeout=cfg.request.timeout * 1000)
            except Exception:  # pragma: no cover - 网络空闲判定失败不算致命
                pass
            page.wait_for_timeout(1500)

        if cfg.auth.session_file:
            target = cfg.path(cfg.auth.session_file)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(target))

    def get(self, url: str, *, referer: str = "", binary: bool = False) -> HttpResponse:
        cfg = self._cfg
        page = self._ensure_page()
        wait = throttle(cfg.request.interval_seconds, self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=cfg.request.timeout * 1000)
            status = int(getattr(response, "status", 0) or 0)
            final_url = str(getattr(page, "url", "") or url)
            if binary:
                body = response.body() if response is not None else b""
                return HttpResponse(
                    url=url, status_code=status, content=bytes(body or b""), final_url=final_url
                )

            # hash 路由的 SPA 页面（门户 #/print?…）在 DOMContentLoaded 之后才异步取正文，
            # 不等就会把「加载中…」的空壳当成正文归档（实测踩到过）。
            self._wait_for_render(page)
            return HttpResponse(url=url, status_code=status, text=page.content(), final_url=final_url)
        except Exception as exc:
            return HttpResponse(url=url, error=f"{type(exc).__name__}: {exc}")
        finally:
            self._last_request_at = time.monotonic()

    def post_json(self, url: str, payload: Mapping[str, Any], *, referer: str = "") -> HttpResponse:
        """用浏览器的 APIRequestContext 发 POST：**与页面共享 Cookie**，登录态不丢。"""
        import json as _json

        cfg = self._cfg
        self._ensure_page()
        wait = throttle(cfg.request.interval_seconds, self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self._context.request.post(
                url,
                data=_json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json;charset=UTF-8"},
                timeout=cfg.request.timeout * 1000,
            )
            return HttpResponse(
                url=url,
                status_code=int(response.status),
                text=response.text(),
                final_url=str(getattr(response, "url", "") or url),
            )
        except Exception as exc:
            return HttpResponse(url=url, error=f"{type(exc).__name__}: {exc}")
        finally:
            self._last_request_at = time.monotonic()

    def _wait_for_render(self, page: Any) -> None:
        """等待页面正文渲染出来；超时即返回（不抛异常，避免因单个页面卡住整批）。

        判据简单而稳：``document.body.innerText`` 长度达到 ``SPA_MIN_CONTENT_CHARS``。
        微信文章页在首屏就有上千字，因此这一步对它们几乎不产生额外等待；
        只对"先出空壳、再异步填内容"的门户 SPA 生效。
        """
        deadline = time.monotonic() + SPA_RENDER_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                length = page.evaluate(
                    "() => (document.body && document.body.innerText ? document.body.innerText.length : 0)"
                )
            except Exception:  # pragma: no cover - 页面已关闭等异常
                return
            try:
                measured = int(length)
            except (TypeError, ValueError):
                return
            if measured >= SPA_MIN_CONTENT_CHARS:
                return
            try:
                page.wait_for_timeout(400)
            except Exception:  # pragma: no cover
                return

    def close(self) -> None:
        for closer in (self._context, self._browser, self._playwright):
            if closer is None:
                continue
            try:
                closer.close() if hasattr(closer, "close") else closer.stop()
            except Exception:  # pragma: no cover - 关闭失败不应影响主流程
                pass
        self._page = self._context = self._browser = self._playwright = None


def throttle(interval_seconds: float, last_request_at: float, *, clock: Optional[Sequence[float]] = None) -> float:
    """限速辅助（纯函数，便于单测）：返回本次请求前需要 sleep 的秒数。

    契约：返回值必须 ≥ 0，且 ``sleep + 已过时间 >= interval_seconds``。
    ``clock`` 仅用于测试注入的 ``[now]`` 时间源，默认使用 ``time.monotonic()``。
    """
    if not last_request_at:
        return 0.0
    if clock is None:
        current = time.monotonic()
    elif callable(clock):
        current = float(clock())  # type: ignore[operator]
    else:
        current = float(clock[0])

    elapsed = current - float(last_request_at)
    wait = float(interval_seconds) - elapsed
    return wait if wait > 0 else 0.0
