"""登录自检：确认会话有效后再开跑；实验结束清理会话（合规红线）。

命令行入口（README 第六节）::

    python -m src.crawler.login_check                  # 探测登录态
    python -m src.crawler.login_check --check-config-only   # 只校验配置，不联网
    python -m src.crawler.login_check --clear-session  # 清理会话文件（实验收尾）

输出约定
--------
* 已登录 → 打印 ``登录态有效`` 与探测 URL，退出码 ``0``；
* 未登录/凭据缺失 → 打印**不含凭据**的原因，退出码 ``2``；
* 配置错误 → 退出码 ``3``；功能尚未实现 → 退出码 ``1``。

任何情况下都不得打印学号、密码、Cookie 明文（配置摘要走 ``AppConfig.redacted()``）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from src.config import AppConfig, load_config
from src.contracts import ConfigError, LoginError, LoginStatus, PipelineError
from src.crawler import session

EXIT_OK = 0
EXIT_NOT_IMPLEMENTED = 1
EXIT_NOT_LOGGED_IN = 2
EXIT_CONFIG_ERROR = 3


def check_login(cfg: AppConfig) -> LoginStatus:
    """探测登录态：请求 ``portal.login_check_url``（未配置则用列表页第一页）判断。

    判定口径（实现时保持一致）：
      * HTTP 2xx 且页面中**不含**登录表单特征（如 ``name="password"``）→ 已登录；
      * 被 302 跳转到登录页、或出现登录表单特征 → 未登录；
      * 凭据缺失 → 直接返回 ``authenticated=False``（不要发起请求）。
    """
    method = cfg.auth.method

    if not cfg.auth.credentials_present():
        return LoginStatus(
            authenticated=False,
            method=method,
            message="未提供登录凭据：请在 .env 填写 AUTH_STUDENT_ID / AUTH_PASSWORD，或 AUTH_COOKIE 并在 config.yaml 设 auth.method=cookie",
        )

    probe = cfg.portal.login_check_url or cfg.portal.list_url or cfg.portal.base_url
    if not probe:
        return LoginStatus(
            authenticated=False,
            method=method,
            message="portal.login_check_url / list_url / base_url 均为空，无法探测登录态",
        )

    # 待核对：账号登录依赖门户登录表单的具体字段名，脚本化登录只在 playwright 模式下尝试；
    # 若门户无法脚本化登录，请改用 auth.method=cookie（从浏览器复制 Cookie）。
    if method == "account" and not cfg.request.use_playwright:
        return LoginStatus(
            authenticated=False,
            method=method,
            probe_url=probe,
            message=(
                "账号登录需要 request.use_playwright=true：门户登录页使用 sm2 前端加密，"
                "纯 HTTP 明文表单提交不可行；也可在浏览器登录后改用 auth.method=cookie"
            ),
        )

    transport = session.build_transport(cfg)
    try:
        if method == "account" and hasattr(transport, "login"):
            transport.login()
        response = transport.get(probe)
    except PipelineError as exc:
        return LoginStatus(
            authenticated=False, method=method, probe_url=probe, message=f"探测请求失败：{exc}"
        )
    finally:
        transport.close()

    if not response.ok:
        return LoginStatus(
            authenticated=False,
            method=method,
            probe_url=probe,
            final_url=response.final_url,
            message=f"探测页返回 HTTP {response.status_code}",
        )

    # 最强信号：被重定向到统一身份认证（实测 my.muc.edu.cn 未登录即如此）。
    # 必须放在 HTML 特征判断之前——登录页本身就是 HTTP 200，只看状态码会误判为已登录。
    if session.looks_like_login_redirect(probe, response.final_url):
        return LoginStatus(
            authenticated=False,
            method=method,
            probe_url=probe,
            final_url=response.final_url,
            message=f"被重定向到统一身份认证：{response.final_url}（未登录或会话已过期）",
        )

    if _looks_like_login_form(response.text):
        return LoginStatus(
            authenticated=False,
            method=method,
            probe_url=probe,
            final_url=response.final_url,
            message="探测页出现登录表单特征，判定为未登录（Cookie 可能已过期）",
        )
    return LoginStatus(
        authenticated=True,
        method=method,
        probe_url=probe,
        final_url=response.final_url or probe,
        message="登录态有效",
    )


def require_login(cfg: AppConfig) -> None:
    """采集前置门禁：未登录抛 ``LoginError``。fetch 阶段开始时由 pipeline 调用。"""
    check_login(cfg).require()


def clear_session(cfg: AppConfig) -> bool:
    """实验结束清理会话（合规红线 4）：删除 ``auth.session_file`` 与内存 Cookie。

    返回是否确实删除了文件；文件不存在也算清理成功（返回 ``False`` 但不算错误）。
    """
    if not cfg.auth.session_file:
        return False
    target = cfg.path(cfg.auth.session_file)
    if not target.exists():
        return False
    try:
        target.unlink()
    except OSError as exc:
        raise LoginError("会话文件删除失败", detail=str(exc)) from exc
    return True


def _looks_like_login_form(html: str) -> bool:
    """页面是否其实是登录页（用于判定登录态失效）。"""
    lowered = (html or "").lower()
    return any(marker.lower() in lowered for marker in session.LOGIN_FORM_MARKERS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.crawler.login_check",
        description="登录自检 / 会话清理（不会打印任何凭据）",
    )
    parser.add_argument("--config", default=None, help="配置文件路径，默认 config/config.yaml")
    parser.add_argument("--check-config-only", action="store_true", help="只校验配置，不发起任何请求")
    parser.add_argument("--clear-session", action="store_true", help="删除会话文件（实验收尾）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：加载配置 → 按开关执行自检 / 清理 → 打印状态。"""
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(config_path=args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.check_config_only:
        print(json.dumps(cfg.redacted(), ensure_ascii=False, indent=2))
        return EXIT_OK

    try:
        if args.clear_session:
            removed = clear_session(cfg)
            print(f"会话已清理（删除文件：{'是' if removed else '文件本就不存在'}）")
            return EXIT_OK

        status = check_login(cfg)
    except NotImplementedError as exc:
        print(f"尚未实现：{exc}（实现顺序见 docs/architecture.md 第九节）", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED

    if status.authenticated:
        print(f"登录态有效（方式：{status.method}，探测：{status.probe_url or '未设置'}）")
        print(f"实际落点：{status.final_url or status.probe_url or '未设置'}")
        return EXIT_OK
    print(f"登录态无效：{status.message or '原因未提供'}", file=sys.stderr)
    if status.final_url and status.final_url != status.probe_url:
        print(f"  （被重定向到：{status.final_url}）", file=sys.stderr)
    return EXIT_NOT_LOGGED_IN


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
