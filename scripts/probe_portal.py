#!/usr/bin/env python3
"""门户可达性探测（只读、低频、不登录、不提交任何表单）。

用途：门户改版、换校区、或怀疑"校外能不能访问"时，先用本脚本拿到**事实**，
再决定采集层怎么配。它只做四件事：

1. DNS 解析（判断域名指向公网还是内网地址段）；
2. TLS 握手与证书信息；
3. 发起**一次** GET，看是否被重定向到统一身份认证（SSO/CAS）；
4. 判定页面形态：服务端渲染 / SPA 外壳 / 是否已含列表数据，并列出外链脚本。

用法::

    python3 scripts/probe_portal.py
    python3 scripts/probe_portal.py "https://my.muc.edu.cn/page/11#/notice/noticeList?lo=10&num=0"
    python3 scripts/probe_portal.py --use-proxy "https://news.muc.edu.cn"

**合规**：本脚本只访问公开的登录页/首页，不尝试任何凭据，不提交表单。
若目标要求登录，它会明确告诉你"需要登录"，而不是替你登录。
"""

from __future__ import annotations

import argparse
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

TIMEOUT = 15
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 统一身份认证常见的 URL 特征（命中说明"未登录被弹回登录页"）
SSO_URL_TOKENS: Tuple[str, ...] = ("login", "sso", "cas", "zfca", "auth", "signin", "passport")

# 登录表单/加密脚本特征
LOGIN_HTML_TOKENS: Tuple[str, ...] = ("type=\"password\"", "name=\"password\"", "统一身份认证", "用户名", "密码")

# 前端加密脚本（SM2/RSA）：出现它意味着**纯 HTTP 明文提交不可行**
FRONTEND_CRYPTO_TOKENS: Tuple[str, ...] = ("sm2", "sm4", "jsencrypt", "rsa")


def banner(text: str) -> None:
    print()
    print(f"===== {text} =====")


def is_private_ip(address: str) -> bool:
    if address.startswith(("10.", "127.", "192.168.", "169.254.", "::1")):
        return True
    if address.startswith("172."):
        try:
            second = int(address.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


def check_dns(host: str) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        print(f"  ✗ DNS 解析失败：{type(exc).__name__}: {exc}")
        return []
    addresses = sorted({info[4][0] for info in infos})
    for address in addresses:
        scope = "内网地址段（校外可能访问不到）" if is_private_ip(address) else "公网地址"
        print(f"  ✓ {address}  —— {scope}")
    return addresses


def check_tls(host: str, port: int = 443) -> bool:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=TIMEOUT) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                subject = dict(item[0] for item in cert.get("subject", ()))
                issuer = dict(item[0] for item in cert.get("issuer", ()))
                print(f"  ✓ 握手成功：{tls.version()}")
                print(f"    证书 CN：{subject.get('commonName')}　颁发者：{issuer.get('organizationName')}")
                print(f"    有效期至：{cert.get('notAfter')}")
        return True
    except Exception as exc:
        print(f"  ✗ TLS 失败：{type(exc).__name__}: {exc}")
        return False


def http_get(url: str, use_proxy: bool, proxy: str) -> Optional[Dict[str, object]]:
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if use_proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return {
                "status": response.status,
                "final_url": response.geturl(),
                "headers": {k.lower(): v for k, v in response.headers.items()},
                "body": response.read(),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": exc.code,
            "final_url": getattr(exc, "url", url),
            "headers": {k.lower(): v for k, v in (exc.headers or {}).items()},
            "body": exc.read() or b"",
        }
    except Exception as exc:
        print(f"  ✗ 请求失败：{type(exc).__name__}: {exc}")
        return None


def analyse(body: bytes, requested_url: str, final_url: str) -> None:
    text = body.decode("utf-8", errors="replace")
    lowered = text.lower()

    banner("4) 判定结论")

    redirected = bool(final_url) and final_url != requested_url
    sso_hit = redirected and any(token in final_url.lower() for token in SSO_URL_TOKENS)
    if sso_hit:
        print(f"  ⚠ 未登录：被重定向到统一身份认证")
        print(f"    实际落点：{final_url}")
        print("    → 说明站点可访问，但业务数据必须登录后才能取（本脚本不会尝试登录）")
    elif redirected:
        print(f"  ⚠ 发生重定向（非 SSO 特征）：{final_url}")
    else:
        print("  ✓ 未发生重定向，直接返回了目标页面")

    has_login_form = any(token.lower() in lowered for token in LOGIN_HTML_TOKENS)
    print(f"  {'⚠' if has_login_form else '✓'} 页面含登录表单特征：{'是' if has_login_form else '否'}")

    crypto_scripts = []
    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', text, flags=re.I)
    for src in scripts:
        if any(token in src.lower() for token in FRONTEND_CRYPTO_TOKENS):
            crypto_scripts.append(src)
    if crypto_scripts:
        print(f"  ⚠ 检测到前端加密脚本：{crypto_scripts}")
        print("    → 密码在浏览器里被 SM2/RSA 加密后提交，纯 requests 明文表单提交**不可行**，")
        print("      必须用 playwright（在真实浏览器里完成加密）或改用 Cookie 登录方式。")

    is_spa = ("id=\"app\"" in text) or ("id=app" in text) or ("__vite" in text) or ("webpack" in text)
    has_hash_route = "#/" in requested_url
    print(f"  {'ℹ' if is_spa or has_hash_route else '✓'} 前端形态："
          f"{'SPA 外壳' if is_spa else '未检出 SPA 外壳'}"
          f"{'；URL 使用 hash 路由（数据很可能来自 XHR JSON 接口）' if has_hash_route else ''}")

    print(f"  ℹ 正文长度：{len(body)} 字节；外链脚本 {len(scripts)} 个")
    for src in scripts[:6]:
        print(f"      {src}")


def main() -> int:
    parser = argparse.ArgumentParser(description="门户可达性探测（只读、不登录）")
    parser.add_argument(
        "url",
        nargs="?",
        default="https://my.muc.edu.cn/page/11#/notice/noticeList?lo=10&num=0",
        help="要探测的页面 URL（hash 路由部分会被忽略，只请求 # 之前的部分）",
    )
    parser.add_argument("--use-proxy", action="store_true", help="通过本机代理访问")
    parser.add_argument("--proxy", default="http://172.29.224.1:7897", help="代理地址")
    args = parser.parse_args()

    parts = urlsplit(args.url)
    host = parts.netloc or parts.path
    request_url = f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"

    banner(f"1) DNS：{host}")
    check_dns(host)

    banner("2) TLS 握手")
    check_tls(host)

    banner(f"3) 发起一次 GET：{request_url}" + ("（经代理）" if args.use_proxy else ""))
    result = http_get(request_url, args.use_proxy, args.proxy)
    if result is None:
        print("  ✗ 未取得响应，无法继续判定")
        return 2

    print(f"  HTTP {result['status']}")
    for key in ("server", "content-type", "set-cookie", "location"):
        value = result["headers"].get(key)  # type: ignore[union-attr]
        if value:
            print(f"    {key}: {str(value)[:110]}")

    analyse(result["body"], request_url, str(result["final_url"]))  # type: ignore[arg-type]
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
