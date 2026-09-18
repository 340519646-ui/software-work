"""采集层单测：URL 规则、列表页解析、归档与清单、翻页去重、登录自检。

全部离线（用 tests/conftest.py 的 ``fake_site`` 假门户），
真实门户相关的部分（登录表单字段名、实际选择器）标为「待核对」，不在此断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.crawler import crawler, detail_page, list_page, login_check, session
from src.contracts import (
    ArticleRef,
    ConfigError,
    FetchError,
    HttpResponse,
    LoginError,
    LoginStatus,
    ManifestEntry,
    PageFetcher,
    RawArticle,
)
from tests.conftest import DETAIL_IMAGE, DETAIL_PORTAL, DETAIL_TEXT, IMAGE_PNG, LIST_URL
from tests.conftest import FakeTransport


@pytest.fixture()
def ref() -> ArticleRef:
    return ArticleRef(detail_url=DETAIL_TEXT, list_url=LIST_URL, title="列表页标题")


@pytest.fixture()
def list_html() -> str:
    from tests.conftest import FIXTURES

    return (FIXTURES / "list_page.html").read_text(encoding="utf-8")


# ======================================================================
# 列表页
# ======================================================================


def test_list_page_url_page_one_returns_base(cfg) -> None:
    assert list_page.list_page_url(cfg, 1) == LIST_URL


def test_list_page_url_appends_page_param(cfg) -> None:
    url = list_page.list_page_url(cfg, 3)
    assert url.startswith(LIST_URL)
    assert f"{cfg.portal.page_param}=3" in url


def test_list_page_url_supports_template() -> None:
    from dataclasses import replace

    from src.config import PortalConfig

    class FakeCfg:
        portal = PortalConfig(base_url="https://x", list_url="https://x/list/{page}.htm")

    assert list_page.list_page_url(FakeCfg, 2) == "https://x/list/2.htm"


def test_list_page_url_requires_config(cfg) -> None:
    from dataclasses import replace

    bare = replace(cfg, portal=replace(cfg.portal, list_url=""))
    with pytest.raises(ConfigError):
        list_page.list_page_url(bare, 1)


def test_parse_list_uses_selector_and_absolutizes(cfg, list_html) -> None:
    refs = list_page.parse_list(list_html, LIST_URL, cfg)
    assert len(refs) == 4, "样本有 4 个条目（含一条重复链接）"
    urls = [item.detail_url for item in refs]
    assert DETAIL_TEXT in urls
    assert DETAIL_PORTAL in urls, "绝对链接与相对链接都要正确解析"
    assert all(item.list_url == LIST_URL for item in refs), "溯源要记录来自哪一页"
    assert refs[0].publish_date == "2025-06-12", "应从同一行解析出发布时间"
    assert refs[0].title


def test_parse_list_without_selector_returns_all_links(cfg, list_html) -> None:
    from dataclasses import replace

    loose = replace(cfg, portal=replace(cfg.portal, detail_link_selector=""))
    refs = list_page.parse_list(list_html, LIST_URL, loose)
    assert len(refs) > 4, "未配置选择器时会退化为「所有链接」，这是口径差异而非错误"


def test_parse_list_skips_noise_links(cfg) -> None:
    html = '<div class="news-list"><ul><li class="item">' \
           '<a href="javascript:void(0)">脚本</a>' \
           '<a href="#top">锚点</a>' \
           '<a href="mailto:a@b.c">邮箱</a>' \
           '<a href="/info/x.htm">正常文章</a>' \
           "</li></ul></div>"
    refs = list_page.parse_list(html, LIST_URL, cfg)
    assert [item.detail_url for item in refs] == ["https://example.edu.cn/info/x.htm"]


def test_fetch_list_raises_on_error_response(cfg) -> None:
    transport = FakeTransport(pages={})
    with pytest.raises(FetchError):
        list_page.fetch_list(transport, cfg, 1)


# ======================================================================
# 详情页：归档与清单
# ======================================================================


def test_archive_html_is_deterministic_and_idempotent(cfg, ref) -> None:
    first = detail_page.archive_html("<html>第一版</html>", ref, cfg)
    second = detail_page.archive_html("<html>第二版</html>", ref, cfg)

    assert first == second, "同一 URL 必须落到同一路径（幂等键）"
    assert first.endswith(".html")
    assert cfg.path(first).read_text(encoding="utf-8") == "<html>第二版</html>"
    assert not (cfg.path(first).parent / (cfg.path(first).name + ".tmp")).exists()


def test_load_archived_roundtrip(cfg, ref) -> None:
    detail_page.archive_html("<html>正文</html>", ref, cfg)
    raw = detail_page.load_archived(ref, cfg)
    assert raw.ok is True
    assert raw.html == "<html>正文</html>"
    assert raw.html_path.endswith(".html")


def test_load_archived_missing_file(cfg, ref) -> None:
    raw = detail_page.load_archived(ref, cfg)
    assert raw.ok is False
    assert "归档不存在" in raw.error


def test_manifest_roundtrip_and_dedup(cfg, ref) -> None:
    first = RawArticle.success(ref, "<html>1</html>", ref.raw_html_path())
    detail_page.append_manifest(first, cfg)

    other_ref = ArticleRef(detail_url=DETAIL_IMAGE, list_url=LIST_URL)
    second = RawArticle.success(other_ref, "<html>2</html>", other_ref.raw_html_path())
    detail_page.append_manifest(second, cfg)

    # 同一 URL 再采一次：清单按 detail_url 去重，保留最后一次
    detail_page.append_manifest(RawArticle.success(ref, "<html>1b</html>", "data/raw/html/x.html"), cfg)

    entries = detail_page.load_manifest(cfg)
    assert len(entries) == 2
    by_url = {entry.ref.detail_url: entry for entry in entries}
    assert by_url[DETAIL_TEXT].html_path == "data/raw/html/x.html"


def test_manifest_skips_damaged_lines(cfg, ref) -> None:
    from src.contracts import manifest_relpath

    target = cfg.path(manifest_relpath(cfg.extract.raw_html_dir))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"detail_url": "https://example.edu.cn/a"}\n坏行\n\n', encoding="utf-8")
    assert len(detail_page.load_manifest(cfg)) == 1


def test_load_manifest_without_file(cfg) -> None:
    assert detail_page.load_manifest(cfg) == []


def test_extract_image_urls_filters_and_dedups(cfg) -> None:
    html = (
        '<img src="/logo.png"><img src="/__local/a/1.png">'
        '<img src="/__local/a/1.png"><img src="data:image/png;base64,AAA">'
        '<img src="/__local/b/2.jpg">'
    )
    urls = detail_page.extract_image_urls(html, LIST_URL, cfg)
    assert urls == ["https://example.edu.cn/__local/a/1.png", "https://example.edu.cn/__local/b/2.jpg"]


# ======================================================================
# 采集门面
# ======================================================================


def test_iter_pages_respects_range(cfg) -> None:
    from dataclasses import replace

    ranged = replace(cfg, portal=replace(cfg.portal, page_start=2, page_end=4))
    assert list(crawler.iter_pages(ranged)) == [2, 3, 4]


def test_iter_refs_dedups_across_pages(cfg) -> None:
    from dataclasses import replace

    site = FakeTransport()
    from tests.conftest import FIXTURES

    list_html = (FIXTURES / "list_page.html").read_text(encoding="utf-8")
    site.pages = {LIST_URL: list_html, LIST_URL + "?page=2": list_html}
    ranged = replace(cfg, portal=replace(cfg.portal, page_start=1, page_end=2))

    refs = list(crawler.iter_refs(crawler.PortalPageFetcher(ranged, site), ranged))
    assert len(refs) == 3, "两页都返回同样 4 条（含重复），跨页去重后应为 3 条"
    assert crawler.collect_source_urls(refs) == {DETAIL_TEXT, DETAIL_IMAGE, DETAIL_PORTAL}


def test_iter_refs_skips_failing_page(cfg) -> None:
    site = FakeTransport(pages={LIST_URL: "<html><body>无链接</body></html>"})
    refs = list(crawler.iter_refs(crawler.PortalPageFetcher(cfg, site), cfg))
    assert refs == [], "页面没有链接时返回空，不抛异常"


def test_fetch_detail_archives_and_collects_images(cfg) -> None:
    from tests.conftest import FIXTURES

    site = FakeTransport(
        pages={DETAIL_IMAGE: (FIXTURES / "article_image.html").read_text(encoding="utf-8")},
        images={IMAGE_PNG: b"\x89PNG-data"},
    )
    fetcher = crawler.PortalPageFetcher(cfg, site)
    raw = fetcher.fetch_detail(ArticleRef(detail_url=DETAIL_IMAGE, list_url=LIST_URL))

    assert raw.ok is True
    assert cfg.path(raw.html_path).exists()
    assert len(raw.images) == 1, "只归档能下载到的图片（另有一张未提供 → 记为失败）"
    asset = raw.images[0]
    assert asset.sha1 and cfg.path(asset.image_path).exists()
    assert asset.ocr_cache_path.endswith(".txt"), "图片必须同时算出 OCR 缓存路径（内容寻址）"


def test_fetch_detail_returns_failure_on_404(cfg) -> None:
    fetcher = crawler.PortalPageFetcher(cfg, FakeTransport(pages={}))
    raw = fetcher.fetch_detail(ArticleRef(detail_url="https://example.edu.cn/missing.htm", list_url=LIST_URL))
    assert raw.ok is False
    assert raw.error


def test_fetch_all_skips_existing_and_writes_manifest(cfg, fake_site) -> None:
    fetcher = crawler.PortalPageFetcher(cfg, fake_site)
    first_pass = list(crawler.fetch_all(fetcher, cfg))
    assert len(first_pass) == 3
    assert all(item.ok for item in first_pass)
    manifest_path = cfg.path("data/raw/manifest.jsonl")
    assert manifest_path.exists() and len(manifest_path.read_text(encoding="utf-8").strip().splitlines()) == 3

    # 断点续跑：把已入库 URL 传进去，应当一条都不再抓
    existing = sorted(crawler.collect_source_urls([item.ref for item in first_pass]))
    second_pass = list(crawler.fetch_all(fetcher, cfg, skip_source_urls=existing))
    assert second_pass == []


def test_build_fetcher_satisfies_contract(cfg) -> None:
    fetcher = crawler.build_fetcher(cfg)
    assert isinstance(fetcher, PageFetcher)
    assert isinstance(fetcher, crawler.PortalPageFetcher)
    fetcher.close()


def test_portal_page_fetcher_supports_context_manager(cfg) -> None:
    site = FakeTransport()
    with crawler.PortalPageFetcher(cfg, site) as fetcher:
        assert isinstance(fetcher, crawler.PortalPageFetcher)
    assert site.closed is True, "退出上下文必须关闭传输层"


# ======================================================================
# 登录自检（离线路径）
# ======================================================================


def test_check_login_without_credentials(cfg) -> None:
    status = login_check.check_login(cfg)
    assert status.authenticated is False
    assert "凭据" in status.message


def test_check_login_account_mode_needs_playwright(cfg, tmp_path) -> None:
    (cfg.project_root / ".env").write_text("AUTH_STUDENT_ID=2021001\nAUTH_PASSWORD=secret\n", encoding="utf-8")
    from src.config import load_config

    reloaded = load_config(config_path=cfg.config_path, project_root=cfg.project_root)
    status = login_check.check_login(reloaded)
    assert status.authenticated is False
    assert "playwright" in status.message, "账号登录必须明确提示需要 playwright 或改用 cookie"


def test_require_login_raises(cfg) -> None:
    with pytest.raises(LoginError):
        login_check.require_login(cfg)


def test_clear_session(cfg) -> None:
    session_file = cfg.path(cfg.auth.session_file)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("{}", encoding="utf-8")

    assert login_check.clear_session(cfg) is True
    assert not session_file.exists()
    assert login_check.clear_session(cfg) is False, "文件不存在也算清理完成"


# ======================================================================
# 真实门户实测场景：统一身份认证（SSO）重定向
#
# 实测（scripts/probe_portal.py）：未登录访问 https://my.muc.edu.cn/page/11
# 会返回 HTTP 200，但落点是
# https://ca.muc.edu.cn/zfca/login?service=http%3A%2F%2Fmy.muc.edu.cn%2Fuser%2FsimpleSSOLogin
# ——只看状态码会把登录页当成正常页面，必须看重定向落点。
# ======================================================================

MUC_PROBE = "https://my.muc.edu.cn/page/11"
MUC_CAS = (
    "https://ca.muc.edu.cn/zfca/login?service=http%3A%2F%2Fmy.muc.edu.cn%2Fuser%2FsimpleSSOLogin"
)


@pytest.mark.parametrize(
    ("requested", "final", "expected"),
    [
        (MUC_PROBE, MUC_CAS, True),
        (MUC_PROBE, MUC_PROBE, False),
        (MUC_PROBE, "", False),
        (MUC_PROBE, "https://my.muc.edu.cn/page/12", False),
        (MUC_PROBE, "https://sso.example.edu.cn/passport/login", True),
        ("https://x.edu.cn/a", "https://x.edu.cn/authenticate/way", True),
    ],
)
def test_looks_like_login_redirect(requested: str, final: str, expected: bool) -> None:
    assert session.looks_like_login_redirect(requested, final) is expected


class _SsoTransport(FakeTransport):
    """模拟真实门户：200 + 落点在统一身份认证。"""

    def __init__(self, final_url: str, body: str = "<html>统一身份认证</html>") -> None:
        super().__init__()
        self._final_url = final_url
        self._body = body

    def get(self, url: str, *, referer: str = "", binary: bool = False) -> HttpResponse:
        self.requests.append(url)
        return HttpResponse(
            url=url, status_code=200, text=self._body, final_url=self._final_url or url
        )


def _reloaded_with_credentials(cfg, monkeypatch, transport):
    """写一份带凭据的 .env、开启 playwright，并把传输层替换成假门户。"""
    from dataclasses import replace

    from src.config import load_config
    from src.crawler import session as session_module

    (cfg.project_root / ".env").write_text(
        "AUTH_STUDENT_ID=2021001\nAUTH_PASSWORD=secret\n", encoding="utf-8"
    )
    reloaded = load_config(config_path=cfg.config_path, project_root=cfg.project_root)
    reloaded = replace(reloaded, request=replace(reloaded.request, use_playwright=True))
    monkeypatch.setattr(session_module, "build_transport", lambda config: transport)
    return reloaded


def test_check_login_detects_sso_redirect(cfg, monkeypatch) -> None:
    reloaded = _reloaded_with_credentials(cfg, monkeypatch, _SsoTransport(MUC_CAS))

    status = login_check.check_login(reloaded)

    assert status.authenticated is False
    assert "统一身份认证" in status.message
    assert status.final_url == MUC_CAS, "必须把落点带出来，方便排查"


def test_check_login_succeeds_when_no_redirect(cfg, monkeypatch) -> None:
    reloaded = _reloaded_with_credentials(
        cfg, monkeypatch, _SsoTransport("", body="<html>就业信息列表</html>")
    )

    status = login_check.check_login(reloaded)

    assert status.authenticated is True
    assert status.final_url == status.probe_url, "没有重定向时，落点就等于探测地址"


def test_check_login_treats_login_form_as_not_logged_in(cfg, monkeypatch) -> None:
    """落点没变但页面里出现登录表单：仍然判未登录（兜底判据）。"""
    transport = _SsoTransport("", body='<form><input type="password" name="pwd"></form>')
    reloaded = _reloaded_with_credentials(cfg, monkeypatch, transport)

    status = login_check.check_login(reloaded)

    assert status.authenticated is False
    assert "登录表单" in status.message


def test_check_login_account_mode_mentions_sm2(cfg, monkeypatch) -> None:
    """账号登录未开 playwright 时，提示必须点明 sm2 前端加密这个真实原因。"""
    from src.config import load_config

    (cfg.project_root / ".env").write_text(
        "AUTH_STUDENT_ID=2021001\nAUTH_PASSWORD=secret\n", encoding="utf-8"
    )
    reloaded = load_config(config_path=cfg.config_path, project_root=cfg.project_root)

    status = login_check.check_login(reloaded)

    assert status.authenticated is False
    assert "playwright" in status.message
    assert "sm2" in status.message
