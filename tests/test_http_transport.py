"""传输层本地集成测试：用 127.0.0.1 上的测试服务器跑**真实 HTTP 往返**。

为什么需要它：session.py 之前只能靠假对象覆盖，真实的分支（限速、重定向落点、
二进制响应、字符集解码）一直没被测到。这里起一个本地 HTTP 服务，把这些分支跑实。

**只访问 127.0.0.1**，不触碰任何外部站点。

放款复现实测环境的三件事：
  * 接口响应**不带 Content-Type**（门户实测 `Headers {}`）；
  * 未登录访问页面会被 **302 到登录页**（实测 302 到统一身份认证）；
  * 图片响应是二进制，不能走文本解码。
"""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import pytest

from src.contracts import ConfigError, FetchError
from src.crawler import session as session_module

CHINESE_TITLE = "体制转外贸｜“职”点迷津校友分享会（第21期）"


class _Handler(BaseHTTPRequestHandler):
    """最小测试服务：JSON 接口 / 重定向 / 图片 / 404。"""

    def log_message(self, *args) -> None:  # noqa: D102 - 静音
        return

    def _send(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path.startswith("/page/11"):
            # 实测行为：未登录访问门户页面 → 302 到统一身份认证
            self._send(302, b"", {"Location": "/ca/login?service=http%3A%2F%2Fmy.muc.edu.cn%2Fuser%2FsimpleSSOLogin"})
        elif self.path.startswith("/ca/login"):
            self._send(200, "<html><input type=\"password\" name=\"pwd\"></html>".encode("utf-8"))
        elif self.path.startswith("/img.png"):
            self._send(200, b"\x89PNG\r\n\x1a\n" + b"payload" * 10)  # 故意不带 Content-Type
        else:
            self._send(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.startswith("/api/getNoticeByPage"):
            body = json.dumps(
                {"page": {"currentPage": 1, "totalCounts": 70}, "tables": [{"notice_title": CHINESE_TITLE}]},
                ensure_ascii=False,
            ).encode("utf-8")
            # 关键：**不发送 Content-Type**，镜像门户的空响应头
            self._send(200, body)
        else:
            self._send(404, b"not found")


@pytest.fixture()
def server() -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def transport(cfg, server: str):
    """真实 HttpTransport；把「上次请求时刻」清零以免每条用例都等 2 秒。

    限速本身由 `throttle` 的独立单测覆盖，这里只验证真实 HTTP 行为。
    """
    ready = replace(cfg, request=replace(cfg.request, interval_seconds=2.0, timeout=5, retries=1))
    transport = session_module.HttpTransport(ready)
    transport._last_request_at = 0.0
    yield transport
    transport.close()


def _reset(transport) -> None:
    transport._last_request_at = 0.0


# ======================================================================
# JSON 接口（镜像门户的空响应头）
# ======================================================================


def test_post_json_decodes_chinese_without_content_type(transport, server: str) -> None:
    response = transport.post_json(f"{server}/api/getNoticeByPage", {"currentPage": 1})

    assert response.ok is True
    assert response.status_code == 200
    assert CHINESE_TITLE in response.text, "无 Content-Type 时中文必须仍能正确解码"
    assert response.final_url == f"{server}/api/getNoticeByPage"
    assert response.redirected is False


def test_post_json_payload_is_json(transport, server: str) -> None:
    response = transport.post_json(f"{server}/api/getNoticeByPage", {"currentPage": 2, "pageSize": 15})
    payload = json.loads(response.text)
    assert payload["page"]["totalCounts"] == 70


def test_post_json_reports_http_error_without_raising(transport, server: str) -> None:
    response = transport.post_json(f"{server}/api/unknown", {})
    assert response.ok is False
    assert response.status_code == 404


# ======================================================================
# 页面：真实 302 与 SSO 判定
# ======================================================================


def test_get_follows_redirect_and_records_final_url(transport, server: str) -> None:
    probe = f"{server}/page/11"
    response = transport.get(probe)

    assert response.status_code == 200
    assert response.final_url != probe, "必须记录重定向落点（requests 会自动跟随）"
    assert response.redirected is True
    assert session_module.looks_like_login_redirect(probe, response.final_url) is True, (
        "真实 302 → 登录页 的链路必须被判为「未登录」"
    )
    assert session_module.LOGIN_FORM_MARKERS[0] in response.text or "password" in response.text


def test_get_records_no_redirect(transport, server: str) -> None:
    response = transport.get(f"{server}/ca/login")
    assert response.redirected is False
    assert response.final_url == f"{server}/ca/login"


# ======================================================================
# 二进制资源
# ======================================================================


def test_get_binary_keeps_bytes(transport, server: str) -> None:
    response = transport.get(f"{server}/img.png", binary=True)

    assert response.ok is True
    assert response.content.startswith(b"\x89PNG"), "二进制必须原样保留，不能被文本解码破坏"
    assert response.text == "", "binary 模式不应尝试解码文本"
    assert response.size == len(response.content)


def test_get_text_returns_str(transport, server: str) -> None:
    response = transport.get(f"{server}/ca/login")
    assert isinstance(response.text, str) and response.text


# ======================================================================
# 限速：确实调用了 sleep
# ======================================================================


def test_second_request_waits_for_interval(cfg, server: str, monkeypatch) -> None:
    ready = replace(cfg, request=replace(cfg.request, interval_seconds=2.0, timeout=5, retries=1))
    transport = session_module.HttpTransport(ready)
    sleeps: list = []
    monkeypatch.setattr(session_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    transport.get(f"{server}/ca/login")   # 第一次：无上次记录，不等待
    transport.get(f"{server}/ca/login")   # 第二次：间隔不足 → 必须等待

    assert len(sleeps) == 1, "第二次请求必须等待限速间隔"
    assert 0 < sleeps[0] <= 2.0
    transport.close()


def test_spa_render_threshold_is_sane() -> None:
    """阈值要能区分"加载中空壳"（实测 60 余字）与真实正文（实测 900+ 字）。"""
    assert 80 <= session_module.SPA_MIN_CONTENT_CHARS <= 600
    assert session_module.SPA_RENDER_TIMEOUT_SECONDS >= 5


def test_login_type_constant_matches_portal() -> None:
    """实测 loginType 取值；写死成常量，避免各处各写一份。"""
    assert session_module.LOGIN_TYPE_USERNAME_PASSWORD == "username_password"


def test_transport_login_requires_entry_url(cfg) -> None:
    ready = replace(
        cfg,
        portal=replace(cfg.portal, base_url="", login_url="", login_check_url=""),
    )
    transport = session_module.PlaywrightTransport(ready)
    with pytest.raises(ConfigError):
        transport.login()


def test_missing_dependency_gives_actionable_error(cfg) -> None:
    """requests 未安装时应抛 FetchError 并提示安装命令，而不是 ImportError 堆栈。"""
    ready = replace(cfg, request=replace(cfg.request, interval_seconds=2.0))
    transport = session_module.HttpTransport(ready)
    import builtins

    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "requests":
            raise ImportError("No module named 'requests'")
        return original_import(name, *args, **kwargs)

    transport._last_request_at = 0.0
    import unittest.mock as mock

    with mock.patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(FetchError) as excinfo:
            transport.get("http://127.0.0.1:9/none")
    assert "requirements.txt" in str(excinfo.value)
