"""Web 服务：类百度检索页面 + JSON API（**只用标准库**，零新增依赖）。

为什么用 ``http.server`` 而不是 FastAPI/Flask
-------------------------------------------
项目一贯取舍是"不为单职责引入依赖"（sqlite3 而非 ORM、FTS5 而非 Whoosh）。
检索服务只做「只读查询 + 一个后台采集作业」，路由总数不到 10 条，
标准库完全够用；引入 Web 框架会让 requirements 多出十几个包，
却换不来这个规模下的实际收益。代价是路由与 JSON 要手写——本文件就是那份代价。

安全边界（两条硬性约束）
----------------------
1. **只监听回环地址**：``config.search.host`` 被校验强制为 127.0.0.1/localhost/::1。
   该服务会返回真实抓取内容，绝不允许对外监听。
2. **不信任查询参数**：``limit`` 一律夹到配置上限，``offset`` 不得为负，
   字段过滤只接受配置里存在的列名——避免前端构造
   ``limit=999999`` 或注入式列名把整库拖进内存 / 触发异常 SQL。

端点
----
* ``GET  /api/search?q=&limit=&offset=&degree=&city=...``  检索（三级缓存）
* ``GET  /api/related?q=``                                关联词建议
* ``POST /api/fetch``                                     触发定向采集（后台作业）
* ``GET  /api/fetch/<job_id>``                            查询采集作业状态
* ``GET  /api/stats``                                     缓存与索引可观测指标
* ``GET  /``                                              前端页面
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from src.config import AppConfig, load_config
from src.contracts import CORE_FIELDS, PipelineError
from src.search.service import SearchService, build_search_service, normalize_query

logger = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).resolve().parent / "web"
MAX_BODY_BYTES = 64 * 1024
"""请求体上限：本地单用户场景没必要接受大体积 POST。"""

FIELD_FILTER_NAMES = tuple(CORE_FIELDS)
"""允许作为字段过滤的列（只接受七项核心字段，杜绝任意列名进 SQL）。"""


class SearchRequestHandler(BaseHTTPRequestHandler):
    """把 HTTP 请求映射到 ``SearchService``（服务端每次请求复用同一个实例）。"""

    server_version = "EmploymentSearch/1.0"
    service: SearchService  # 由 build_server 注入
    config: AppConfig

    # ---------- 日志：默认实现会把每个请求打到 stderr，压到 DEBUG ----------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802 - http.server 的约定命名
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)

        if route == "/api/search":
            self._handle_search(query)
        elif route == "/api/related":
            self._handle_related(query)
        elif route == "/api/stats":
            self._send_json(HTTPStatus.OK, self.service.stats())
        elif route.startswith("/api/fetch/"):
            self._handle_fetch_status(route.rsplit("/", 1)[-1])
        elif route == "/api/fetch":
            # 允许用 GET 查"最近一次作业"，方便前端刷新后恢复进度条
            job = self.service.latest_job()
            self._send_json(HTTPStatus.OK, {"job": job.to_json() if job else None})
        elif route.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "未知接口"})
        else:
            self._serve_static(route)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route != "/api/fetch":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "未知接口"})
            return

        payload = self._read_json_body()
        if payload is None:
            return
        text = str(payload.get("q") or "").strip()
        limit = self._coerce_int(payload.get("limit"), self.config.search.default_limit)
        query = normalize_query(text, self.config, limit=limit, want_fetch=True)
        if query.is_empty():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "空查询不触发采集"})
            return

        job = self.service.request_fetch(query)
        status = HTTPStatus.ACCEPTED if job.job_id else HTTPStatus.CONFLICT
        self._send_json(status, {"job": job.to_json()})

    # ---------- 处理函数 ----------

    def _handle_search(self, query: Dict[str, List[str]]) -> None:
        text = (query.get("q") or [""])[0]
        limit = self._coerce_int(
            (query.get("limit") or [None])[0], self.config.search.default_limit
        )
        offset = max(0, self._coerce_int((query.get("offset") or [None])[0], 0))
        fields = {
            name: (query.get(name) or [""])[0]
            for name in FIELD_FILTER_NAMES
            if (query.get(name) or [""])[0].strip()
        }
        search_query = normalize_query(text, self.config, fields=fields, limit=limit, offset=offset)
        result = self.service.search(search_query)
        self._send_json(HTTPStatus.OK, result.to_json())

    def _handle_related(self, query: Dict[str, List[str]]) -> None:
        text = (query.get("q") or [""])[0]
        search_query = normalize_query(text, self.config)
        related = self.service.suggest_related(search_query.keywords, limit=10)
        self._send_json(HTTPStatus.OK, {"related": related})

    def _handle_fetch_status(self, job_id: str) -> None:
        job = self.service.get_job(job_id)
        if job is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "作业不存在"})
            return
        self._send_json(HTTPStatus.OK, {"job": job.to_json()})

    # ---------- 静态文件 ----------

    def _serve_static(self, route: str) -> None:
        """只从 ``src/search/web/`` 取文件，并做路径穿越防护。"""
        relative = "index.html" if route == "/" else route.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "非法路径"})
            return

        if not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "文件不存在"})
            return

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 本地单用户：禁用缓存，避免改了前端还要手动清浏览器缓存
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 工具 ----------

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "请求体过大"})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求体不是合法 JSON"})
            return None
        return payload if isinstance(payload, dict) else {}

    def _send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        try:
            if value is None or str(value).strip() == "":
                return int(default)
            return int(str(value).strip())
        except (TypeError, ValueError):
            return int(default)


def build_server(cfg: AppConfig, service: Optional[SearchService] = None) -> ThreadingHTTPServer:
    """构造 HTTP 服务器（不启动）。

    注入 ``service`` 便于测试用替身；生产路径由 ``build_search_service`` 构造。
    """
    if not cfg.search.enabled:
        raise PipelineError("search.enabled=false：检索服务已关闭（改 config.yaml 的 search.enabled）")

    handler = type(
        "BoundSearchHandler",
        (SearchRequestHandler,),
        {"service": service if service is not None else build_search_service(cfg), "config": cfg},
    )
    server = ThreadingHTTPServer((cfg.search.host, cfg.search.port), handler)
    server.daemon_threads = True
    return server


def main(argv: Optional[List[str]] = None) -> int:
    """CLI：``python -m src.search.server [--port N] [--no-browser]``。"""
    parser = argparse.ArgumentParser(description="就业信息检索服务（仅监听本机）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/config.yaml）")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听端口")
    parser.add_argument("--open", action="store_true", help="启动后尝试打开浏览器")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(config_path=Path(args.config) if args.config else None)
    if args.port:
        from dataclasses import replace

        cfg = replace(cfg, search=replace(cfg.search, port=int(args.port)))

    service = build_search_service(cfg)
    indexed = service.stats().get("indexed_rows", 0)
    if not indexed:
        logger.warning(
            "索引里没有数据（articles 表为空）：请先跑 "
            "`bash scripts/run_all.sh --stages extract,export` 或触发一次定向采集"
        )
    else:
        # 启动即对齐索引：保证 FTS 与 articles 一致（幂等，成本很低）
        try:
            service._index.maintain()  # noqa: SLF001 - 服务自有的索引实例
        except Exception as exc:  # noqa: BLE001 - 索引维护失败不影响启动
            logger.warning("启动时重建索引失败：%s", exc)

    server = build_server(cfg, service=service)
    url = f"http://{cfg.search.host}:{cfg.search.port}/"
    print(f"检索服务已启动：{url}")
    print(f"  数据版本号：{service._index.index_version()}  已索引：{indexed} 条")  # noqa: SLF001
    print(f"  采集开关：{'开启' if cfg.search.trigger_enabled else '关闭'}（search.trigger_enabled）")
    print("  只监听本机回环地址；Ctrl+C 退出")

    if args.open:
        import webbrowser

        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
        service._index.close()  # noqa: SLF001
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
