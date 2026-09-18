"""接口（JSON）模式列表采集测试。

实测事实（由本人在浏览器 Network 面板提供）：
  * 接口：``POST https://my.muc.edu.cn/comsys-portal-notice-web/getNoticeByPage``，
    ``Content-Type: application/json;charset=UTF-8``；
  * 翻页：**URL 不变**，请求体里的 ``currentPage`` 与 ``comsys_random_t`` 变化；
  * 详情链接由 ``notice_id`` + ``organization_id`` 构成；
  * 详情正文容器：``<div id="content" class="content_all">``。

本文件全部离线（用内存假接口），验证"配好即能跑通"：
字段名、取值路径、详情链接模板全部走配置，门户改版不必改代码。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.config import ConfigError, load_config
from src.contracts import FetchError, HttpResponse, ParseError
from src.crawler import crawler, list_page
from src.crawler.crawler import PortalPageFetcher
from src.parser.cleaner import clean_html
from src.contracts import ArticleRef
from tests.conftest import FIXTURES, MUC_API_URL, MUC_DETAIL_TEMPLATE, FakeTransport

API_RESPONSE = {
    "code": 0,
    "message": "success",
    "data": {
        "total": 3,
        "records": [
            {
                "notice_id": "1001",
                "organization_id": "org-01",
                "title": "关于开展2025届毕业生就业情况统计的通知",
                "publishTime": "2025-06-12 10:30:00",
            },
            {
                "notice_id": "1002",
                "organization_id": "org-02",
                "title": "就业分享会报名通知",
                "publishTime": "2025-06-10 09:00:00",
            },
            {"title": "脏数据：缺少 notice_id，应被跳过"},
        ],
    },
}


REAL_API_BODY = {
    "pageSize": 15,
    "type": "10",
    "is_add": "0",
    "system_show": 1,
    "select_all": False,
    "select_notice": "",
    "searchValue": "",
    "searchDepartment": "",
    "start_date": "",
    "end_date": "",
    "start": 0,
    "end": 15,
}
"""实测参数表（type=10 即「就业信息」栏目）：参数走查询串，因此这些字段会出现在 URL 上。"""


@pytest.fixture()
def api_cfg(cfg):
    """在共享夹具基础上切到接口模式（参数表与生产配置一致）。"""
    return replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            api_url=MUC_API_URL,
            detail_url_template=MUC_DETAIL_TEMPLATE,
            api_body=dict(REAL_API_BODY),
            api_param_style="query",
            api_list_path="data.records",
            api_total_path="data.total",
            api_title_field="title",
            api_date_field="publishTime",
        ),
    )


@pytest.fixture()
def api_site() -> FakeTransport:
    return FakeTransport(json_routes={MUC_API_URL: API_RESPONSE})


# ======================================================================
# 请求体与令牌
# ======================================================================


def test_random_token_default_matches_math_random() -> None:
    """实测 comsys_random_t 形如 0.9887865334032206，即浏览器 Math.random() 的格式。"""
    first = list_page.random_token()
    second = list_page.random_token()

    assert first.startswith("0.") and first.count(".") == 1
    assert first[2:].isdigit(), "小数部分必须是数字"
    assert 15 <= len(first) <= 20, f"长度应接近 JS 浮点输出，实际 {len(first)}"
    assert float(first) == float(first) and 0 <= float(first) < 1
    assert first != second, "每次请求都应重新生成"


def test_random_token_hex_style() -> None:
    token = list_page.random_token(style="hex32", length=32)
    assert len(token) == 32
    assert all(char in "0123456789abcdef" for char in token)


def test_build_api_body_injects_page_and_token(api_cfg) -> None:
    body1 = list_page.build_api_body(api_cfg, 1)
    body2 = list_page.build_api_body(api_cfg, 2)

    assert body1[api_cfg.portal.api_page_field] == 1
    assert body2[api_cfg.portal.api_page_field] == 2
    token_field = api_cfg.portal.api_token_field
    assert body1[token_field] and body1[token_field] != body2[token_field]


def test_build_api_body_preserves_extra_fields(cfg) -> None:
    with_extra = replace(
        cfg,
        portal=replace(cfg.portal, mode="api", api_body={"pageSize": 15, "noticeType": "1"}),
    )
    body = list_page.build_api_body(with_extra, 3)
    assert body["pageSize"] == 15 and body["noticeType"] == "1"
    assert body["currentPage"] == 3


def test_build_api_body_can_use_fixed_token(cfg) -> None:
    frozen = replace(cfg, portal=replace(cfg.portal, mode="api", api_token_field="token"))
    assert list_page.build_api_body(frozen, 1, token="FIXED")["token"] == "FIXED"


# ======================================================================
# 取值路径与详情链接
# ======================================================================


def test_dig_reads_nested_paths() -> None:
    payload = {"data": {"records": [{"id": 1}], "total": 3}}
    assert list_page.dig(payload, "data.total") == 3
    assert list_page.dig(payload, "data.records.0.id") == 1
    assert list_page.dig(payload, "data.missing") is None
    assert list_page.dig(payload, "data.missing", "默认") == "默认"
    assert list_page.dig(payload, "") is None


def test_build_detail_url_fills_placeholders(api_cfg) -> None:
    """实测模板：https://my.muc.edu.cn/page/11#/print?notice_id=352209&show_type=1&type=10"""
    record = {"notice_id": "352209", "organization_id": "11027", "notice_title": "x"}
    url = list_page.build_detail_url(record, api_cfg)

    assert url == "https://my.muc.edu.cn/page/11#/print?notice_id=352209&show_type=1&type=10"
    assert "organization_id" not in url, "实测模板不需要 organization_id"


def test_build_detail_url_returns_empty_when_field_missing(api_cfg) -> None:
    # 实测模板只用到 notice_id：缺它就必须跳过（不能编造链接）
    assert list_page.build_detail_url({"organization_id": "11027"}, api_cfg) == ""
    assert list_page.build_detail_url({"notice_id": "1"}, replace(api_cfg, portal=replace(api_cfg.portal, detail_url_template=""))) == ""


def test_build_detail_url_supports_nested_fields(cfg) -> None:
    nested = replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            detail_url_template="https://x/detail?id={inner.noticeId}",
        ),
    )
    assert list_page.build_detail_url({"inner": {"noticeId": "7"}}, nested) == "https://x/detail?id=7"


# ======================================================================
# 解析接口响应
# ======================================================================


def test_parse_list_json_extracts_refs(api_cfg) -> None:
    import json

    refs = list_page.parse_list_json(json.dumps(API_RESPONSE, ensure_ascii=False), MUC_API_URL, api_cfg)

    assert len(refs) == 2, "第 3 条缺 notice_id，必须被跳过而不是编造链接"
    assert refs[0].title == "关于开展2025届毕业生就业情况统计的通知"
    assert refs[0].publish_date == "2025-06-12", "时间要归一成 YYYY-MM-DD"
    assert refs[0].list_url == MUC_API_URL, "溯源要记录来自哪个接口"
    assert refs[0].detail_url.endswith("notice_id=1001&show_type=1&type=10")


def test_parse_list_json_rejects_bad_payload(api_cfg) -> None:
    with pytest.raises(ParseError):
        list_page.parse_list_json("<html>这不是 JSON</html>", MUC_API_URL, api_cfg)
    with pytest.raises(ParseError):
        list_page.parse_list_json('{"data": {"records": {}}}', MUC_API_URL, api_cfg), "records 不是数组"
    with pytest.raises(ParseError):
        list_page.parse_list_json('{"code": 0}', MUC_API_URL, api_cfg), "找不到列表路径"


def test_parse_list_json_honours_configured_paths(cfg) -> None:
    import json

    other = replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            detail_url_template="https://x/d?id={noticeId}",
            api_list_path="rows",
            api_title_field="noticeTitle",
            api_date_field="createTime",
        ),
    )
    payload = {"rows": [{"noticeId": "9", "noticeTitle": "另一种字段命名", "createTime": "2025/06/01"}]}
    refs = list_page.parse_list_json(json.dumps(payload, ensure_ascii=False), MUC_API_URL, other)

    assert len(refs) == 1
    assert refs[0].title == "另一种字段命名"
    assert refs[0].publish_date == "2025-06-01"
    assert refs[0].detail_url == "https://x/d?id=9"


# ======================================================================
# 走传输层
# ======================================================================


def test_fetch_list_api_posts_json(api_cfg, api_site) -> None:
    refs = list_page.fetch_list_api(api_site, api_cfg, 2)

    assert len(refs) == 2
    assert len(api_site.requests) == 1
    assert api_site.requests[0].startswith(MUC_API_URL)
    assert "currentPage=2" in api_site.requests[0], "实测：参数必须走查询串（URL 会随页码变化）"
    assert len(api_site.posts) == 1
    payload = api_site.posts[0]
    assert payload[api_cfg.portal.api_page_field] == 2
    assert payload[api_cfg.portal.api_token_field]


def test_fetch_list_api_raises_on_error_status(api_cfg) -> None:
    site = FakeTransport(json_routes={})
    with pytest.raises(FetchError):
        list_page.fetch_list_api(site, api_cfg, 1)


def test_fetch_list_api_requires_api_url(cfg) -> None:
    broken = replace(cfg, portal=replace(cfg.portal, mode="api", api_url=""))
    with pytest.raises(ConfigError):
        list_page.fetch_list_api(FakeTransport(), broken, 1)


def test_portal_page_fetcher_dispatches_by_mode(api_cfg, api_site, cfg) -> None:
    api_fetcher = PortalPageFetcher(api_cfg, api_site)
    assert len(api_fetcher.fetch_list(1)) == 2
    assert api_site.posts, "api 模式必须走 POST JSON"

    html_site = FakeTransport(pages={cfg.portal.list_url: "<html><body>无链接</body></html>"})
    html_fetcher = PortalPageFetcher(cfg, html_site)
    assert html_fetcher.fetch_list(1) == []
    assert html_site.posts == [], "html 模式不应发 POST"


# ======================================================================
# 配置校验
# ======================================================================


def _write_api_config(root, **portal_overrides):
    from tests.conftest import BASE_CONFIG, patched_config, write_config

    config = patched_config(portal=portal_overrides)
    write_config(root, config)
    return load_config(config_path=root / "config/config.yaml", project_root=root)


def test_api_mode_requires_api_url(cfg_root) -> None:
    with pytest.raises(ConfigError):
        _write_api_config(cfg_root, mode="api", api_url="", detail_url_template="https://x/{a}")


def test_api_mode_requires_detail_template(cfg_root) -> None:
    with pytest.raises(ConfigError):
        _write_api_config(cfg_root, mode="api", api_url=MUC_API_URL, detail_url_template="")


def test_api_mode_accepts_complete_config(cfg_root) -> None:
    loaded = _write_api_config(
        cfg_root, mode="api", api_url=MUC_API_URL, detail_url_template=MUC_DETAIL_TEMPLATE
    )
    assert loaded.portal.mode == "api"


def test_invalid_mode_rejected(cfg_root) -> None:
    with pytest.raises(ConfigError):
        _write_api_config(cfg_root, mode="dom")


# ======================================================================
# 详情页：实测正文容器
# ======================================================================


def test_cleaner_uses_portal_content_container(cfg) -> None:
    """实测容器：<div id="content" class="content_all">。"""
    html = (FIXTURES / "muc_notice_detail.html").read_text(encoding="utf-8")
    ref = ArticleRef(detail_url="https://my.muc.edu.cn/page/11#/notice/noticeDetail?notice_id=1001")

    cleaned = clean_html(html, ref, cfg)

    assert cleaned.title == "关于开展2025届毕业生就业情况统计的通知"
    assert "就业情况统计" in cleaned.text
    assert "签约北京某某科技有限公司" in cleaned.text
    assert "发布单位" not in cleaned.segments.get("body", ""), "导航/元信息不应混进正文段"

# ======================================================================
# 实测请求体/响应带来的补强
# ======================================================================


def test_api_body_derives_start_end(api_cfg) -> None:
    """实测：第 2 页、pageSize=15 → start=15、end=30。"""
    with_range = replace(
        api_cfg, portal=replace(api_cfg.portal, api_body={"pageSize": 15, "start": 0, "end": 15})
    )

    assert list_page.build_api_body(with_range, 1)["start"] == 0
    assert list_page.build_api_body(with_range, 1)["end"] == 15
    assert list_page.build_api_body(with_range, 2)["start"] == 15
    assert list_page.build_api_body(with_range, 2)["end"] == 30
    assert list_page.build_api_body(with_range, 3)["end"] == 45


def test_api_body_omits_start_end_when_not_configured(cfg) -> None:
    plain = replace(cfg, portal=replace(cfg.portal, mode="api", api_body={"pageSize": 15}))
    body = list_page.build_api_body(plain, 2)
    assert "start" not in body and "end" not in body, "模板里没有这两个键就不应多发字段"


def test_locate_records_uses_configured_path(api_cfg) -> None:
    payload = {"data": {"records": [{"a": 1}]}}
    records, path = list_page.locate_records(payload, api_cfg)
    assert records == [{"a": 1}] and path == "data.records"


def test_locate_records_auto_detects_common_names(api_cfg) -> None:
    payload = {"page": {"totalCounts": 70}, "data": {"list": [{"notice_id": "1"}]}}
    records, path = list_page.locate_records(payload, api_cfg)

    assert records == [{"notice_id": "1"}]
    assert path == "data.list", "配错路径时应自动识别，避免门户改版即挂"


def test_locate_records_falls_back_to_top_level_array(api_cfg) -> None:
    payload = {"page": {}, "notices": [{"notice_id": "2"}]}
    records, path = list_page.locate_records(payload, api_cfg)
    assert records == [{"notice_id": "2"}] and path == "notices"


def test_build_api_query_carries_paging_and_filters(tables_cfg) -> None:
    """实测结论：参数必须出现在查询串里，否则服务端忽略（翻页与栏目过滤都会失效）。"""
    params = list_page.build_api_query(tables_cfg, 2)

    assert params["currentPage"] == "2"
    assert params["pageSize"] == "15"
    assert params["type"] == "10", "栏目过滤必须带上，否则 total 会变成全站 44944"
    assert params["start"] == "15" and params["end"] == "30"
    assert params["select_all"] == "false", "布尔值要小写成 false，避免被当成真值"
    assert params[tables_cfg.portal.api_token_field]


def test_build_api_url_appends_query(api_cfg) -> None:
    url = list_page.build_api_url(api_cfg, 3)
    assert url.startswith(MUC_API_URL)
    assert "currentPage=3" in url and "?" in url


def test_build_api_url_respects_json_style(api_cfg) -> None:
    json_style = replace(api_cfg, portal=replace(api_cfg.portal, api_param_style="json"))
    assert list_page.build_api_url(json_style, 3) == MUC_API_URL, "json 形态不应改 URL"


def test_fetch_list_api_sends_params_in_query(api_cfg, api_site) -> None:
    refs = list_page.fetch_list_api(api_site, api_cfg, 2)

    sent = api_site.requests[0]
    assert "currentPage=2" in sent, "翻页参数必须出现在请求 URL 上"
    assert "type=" in sent
    assert refs and "currentPage=2" in refs[0].list_url, "清单要能还原来自哪一页查询"


def test_config_rejects_bad_param_style(cfg_root) -> None:
    with pytest.raises(ConfigError):
        _write_api_config(
            cfg_root,
            mode="api",
            api_url=MUC_API_URL,
            detail_url_template=MUC_DETAIL_TEMPLATE,
            api_param_style="body",
        )


def test_locate_records_digs_into_nested_envelope(api_cfg) -> None:
    """实测响应：{"datas": {...}, "msg": "success", "state": true} —— 需递归下钻。"""
    payload = {
        "datas": {"tables": [{"notice_id": 358715}], "page": {"totalCounts": 70}},
        "msg": "success",
        "state": True,
    }
    records, path = list_page.locate_records(payload, api_cfg)

    assert records == [{"notice_id": 358715}]
    assert path == "datas.tables", "必须报出实际路径，方便写回配置"


def test_locate_records_handles_datas_at_top_level(api_cfg) -> None:
    payload = {"datas": [{"notice_id": 1}], "msg": "success", "state": True}
    records, path = list_page.locate_records(payload, api_cfg)
    assert records == [{"notice_id": 1}] and path == "datas"


def test_parse_list_json_works_with_datas_envelope(api_cfg) -> None:
    payload = {
        "datas": {
            "tables": [
                {"notice_id": 358715, "notice_title": "通知一", "notice_release_time": "2026-09-16 10:15"},
                {"notice_id": 358700, "notice_title": "通知二", "notice_release_time": "2026-09-15 10:51"},
            ]
        },
        "msg": "success",
        "state": True,
    }
    refs = list_page.parse_list_json(json.dumps(payload, ensure_ascii=False), MUC_API_URL, api_cfg)

    assert [ref.title for ref in refs] == ["通知一", "通知二"]
    assert refs[0].detail_url.endswith("notice_id=358715&show_type=1&type=10")


def test_locate_records_returns_none_when_absent(api_cfg) -> None:
    records, path = list_page.locate_records({"page": {"totalCounts": 0}}, api_cfg)
    assert records is None and path == ""


def test_parse_list_json_auto_detects_and_reports(api_cfg) -> None:
    payload = {
        "page": {"totalCounts": 70},
        "data": {"list": [{"notice_id": "7", "organization_id": "org-9", "title": "通知", "publishTime": "2025-06-12 08:00:00"}]},
    }
    refs = list_page.parse_list_json(json.dumps(payload, ensure_ascii=False), MUC_API_URL, api_cfg)

    assert len(refs) == 1
    assert refs[0].title == "通知"
    assert refs[0].publish_date == "2025-06-12"


def test_parse_list_json_auto_detects_title_and_date(api_cfg) -> None:
    """列表项字段名各家不同：配错也应能识别，而不是留空。"""
    payload = {
        "data": {"list": [{"notice_id": "1", "organization_id": "o", "noticeTitle": "另一种标题字段", "releaseDate": "2025/06/09"}]}
    }
    refs = list_page.parse_list_json(json.dumps(payload, ensure_ascii=False), MUC_API_URL, api_cfg)

    assert refs[0].title == "另一种标题字段"
    assert refs[0].publish_date == "2025-06-09"


def test_parse_list_json_keeps_empty_when_no_field_matches(api_cfg) -> None:
    payload = {"data": {"list": [{"notice_id": "1", "organization_id": "o", "whatever": "x"}]}}
    refs = list_page.parse_list_json(json.dumps(payload, ensure_ascii=False), MUC_API_URL, api_cfg)

    assert refs[0].title == "" and refs[0].publish_date == "", "识别不出就留空，不编造"


def test_parse_total_pages_from_total_counts(api_cfg) -> None:
    payload = json.dumps({"page": {"total": 1040, "totalCounts": 70, "pageSize": 15}}, ensure_ascii=False)
    assert list_page.parse_total_pages(payload, api_cfg) == 70


def test_parse_total_pages_falls_back_to_division(api_cfg) -> None:
    body_with_size = replace(
        api_cfg,
        portal=replace(api_cfg.portal, api_body={"pageSize": 15}, api_total_path="page.total"),
    )
    payload = json.dumps({"page": {"total": 1040}}, ensure_ascii=False)
    assert list_page.parse_total_pages(payload, body_with_size) == 70, "总页数取不到时按 total/pageSize 估算"


def test_parse_total_pages_returns_zero_on_bad_payload(api_cfg) -> None:
    assert list_page.parse_total_pages("<html>", api_cfg) == 0
    assert list_page.parse_total_pages("{}", api_cfg) == 0


def test_detail_url_uses_org_id_fallback(cfg) -> None:
    """通用的占位兜底机制（本校门户的实测模板已不需要它，但保留以备换门户）。"""
    with_org = replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            detail_url_template="https://x/d?n={notice_id}&o={organization_id}",
            detail_org_id="bks1044104407202525rg2",
        ),
    )
    assert list_page.build_detail_url({"notice_id": "1001"}, with_org) == (
        "https://x/d?n=1001&o=bks1044104407202525rg2"
    )

    # 记录自带的值优先，不被兜底覆盖
    assert list_page.build_detail_url({"notice_id": "1", "organization_id": "self"}, with_org) == (
        "https://x/d?n=1&o=self"
    )


def test_detail_url_without_org_still_skips_when_missing(cfg) -> None:
    no_org = replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            detail_url_template="https://x/d?n={notice_id}&o={organization_id}",
            detail_org_id="",
        ),
    )
    assert list_page.build_detail_url({"notice_id": "1"}, no_org) == ""


# ======================================================================
# 自动翻页（page_end = 0）
# ======================================================================


class _PagedApiTransport(FakeTransport):
    """按 currentPage 返回不同页的数据；末页之后返回空数组。"""

    def __init__(self, url: str, pages: dict) -> None:
        super().__init__()
        self._api_url = url
        self._pages = pages

    def post_json(self, url: str, payload, *, referer: str = "") -> HttpResponse:
        self.requests.append(url)
        self.posts.append(dict(payload))
        if url.split("?", 1)[0] != self._api_url.split("?", 1)[0]:
            return HttpResponse(url=url, status_code=404, error="no json route")
        page = int(payload.get("currentPage", 1))
        records = self._pages.get(page, [])
        body = {
            "page": {"currentPage": page, "totalCounts": len(self._pages), "pageSize": 15},
            "data": {"list": records},
        }
        return HttpResponse(
            url=url, status_code=200, text=json.dumps(body, ensure_ascii=False), final_url=url
        )


def test_iter_refs_auto_pages_until_empty(cfg) -> None:
    pages = {
        1: [{"notice_id": "1", "organization_id": "o", "title": "A", "publishTime": "2025-06-12"}],
        2: [{"notice_id": "2", "organization_id": "o", "title": "B", "publishTime": "2025-06-11"}],
    }
    site = _PagedApiTransport(MUC_API_URL, pages)
    auto = replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            api_url=MUC_API_URL,
            detail_url_template=MUC_DETAIL_TEMPLATE,
            page_start=1,
            page_end=0,  # 自动翻页
        ),
    )

    refs = list(crawler.iter_refs(PortalPageFetcher(auto, site), auto))

    assert [ref.title for ref in refs] == ["A", "B"]
    assert [post["currentPage"] for post in site.posts] == [1, 2, 3], "第 3 页为空即停"
    assert len(site.requests) == 3


def test_iter_refs_explicit_range_does_not_overrun(cfg) -> None:
    pages = {1: [{"notice_id": "1", "organization_id": "o", "title": "A", "publishTime": ""}]}
    site = _PagedApiTransport(MUC_API_URL, pages)
    ranged = replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            api_url=MUC_API_URL,
            detail_url_template=MUC_DETAIL_TEMPLATE,
            page_start=1,
            page_end=1,
        ),
    )

    refs = list(crawler.iter_refs(PortalPageFetcher(ranged, site), ranged))

    assert len(refs) == 1
    assert [post["currentPage"] for post in site.posts] == [1], "显式范围不应多请求一页"


def test_auto_page_limit_guards_against_infinite_loop(cfg, monkeypatch) -> None:
    from src.crawler import crawler as crawler_module

    class _EndlessTransport(FakeTransport):
        def post_json(self, url: str, payload, *, referer: str = "") -> HttpResponse:
            self.requests.append(url)
            record = {
                "notice_id": str(payload.get("currentPage")),
                "organization_id": "o",
                "title": "X",
                "publishTime": "",
            }
            body = {"page": {}, "data": {"list": [record]}}
            return HttpResponse(url=url, status_code=200, text=json.dumps(body, ensure_ascii=False))

    monkeypatch.setattr(crawler_module, "AUTO_PAGE_LIMIT", 3)
    auto = replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            api_url=MUC_API_URL,
            detail_url_template=MUC_DETAIL_TEMPLATE,
            page_start=1,
            page_end=0,
        ),
    )
    refs = list(crawler.iter_refs(PortalPageFetcher(auto, _EndlessTransport()), auto))

    assert len(refs) == 3, "接口异常时必须被安全上限截断"


# ======================================================================
# 实测的 tables 结构（含站外链接）
# ======================================================================

REAL_RECORD = {
    "notice_title": "体制转外贸｜ “职”点迷津校友分享会（第21期）",
    "notice_stick": 0,
    "notice_state": 1,
    "notice_source": 1,
    "notice_link_state": 1,
    "notice_content": "",
    "notice_type_name": "就业信息",
    "notice_first_time": "2026-09-16 10:15",
    "organization_name": "中国少数民族语言文学学院",
    "notice_link": "https://mp.weixin.qq.com/s/2bZPjAOMnp2rE8PeBq5m0w",
    "notice_id": 358715,
    "notice_type": 10,
    "notice_release_time": "2026-09-16 10:15",
    "organization_id": "11027",
}
PORTAL_RECORD = {
    "notice_title": "关于开展2025届毕业生就业情况统计的通知",
    "notice_link_state": 0,
    "notice_link": "",
    "notice_id": 358700,
    "notice_release_time": "2026-09-15 10:51",
    "organization_id": "00014",
}


@pytest.fixture()
def tables_cfg(cfg):
    """按实测配置：tables / notice_title / notice_release_time / 站外抓取。"""
    return replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            api_url=MUC_API_URL,
            detail_url_template=MUC_DETAIL_TEMPLATE,
            api_body=dict(REAL_API_BODY),
            api_param_style="query",
            api_list_path="datas.tables",
            api_title_field="notice_title",
            api_date_field="notice_release_time",
            api_link_field="notice_link",
            api_link_state_field="notice_link_state",
            api_link_state_external=1,
            external_link_policy="fetch",
            detail_org_id="",
        ),
    )


def _tables_payload(*records) -> str:
    return json.dumps(
        {"page": {"totalCounts": 70, "total": 1040}, "tables": list(records)}, ensure_ascii=False
    )


def test_real_tables_structure_is_parsed(tables_cfg) -> None:
    refs = list_page.parse_list_json(_tables_payload(REAL_RECORD, PORTAL_RECORD), MUC_API_URL, tables_cfg)

    assert len(refs) == 2
    first = refs[0]
    assert first.title == REAL_RECORD["notice_title"]
    assert first.publish_date == "2026-09-16"
    assert first.detail_url == (
        "https://my.muc.edu.cn/page/11#/print?notice_id=358715&show_type=1&type=10"
    ), "notice_id 是整数，也要正确拼进实测模板"
    assert first.content_url == REAL_RECORD["notice_link"], "站外通知的正文地址应记下来"
    assert first.is_external is True
    assert first.fetch_url == REAL_RECORD["notice_link"], "采集应去抓站外正文"


def test_portal_hosted_notice_has_no_content_url(tables_cfg) -> None:
    refs = list_page.parse_list_json(_tables_payload(PORTAL_RECORD), MUC_API_URL, tables_cfg)

    assert refs[0].content_url == ""
    assert refs[0].is_external is False
    assert refs[0].fetch_url == refs[0].detail_url


def test_external_link_policy_portal_ignores_link(tables_cfg) -> None:
    portal_only = replace(tables_cfg, portal=replace(tables_cfg.portal, external_link_policy="portal"))
    refs = list_page.parse_list_json(_tables_payload(REAL_RECORD), MUC_API_URL, portal_only)

    assert len(refs) == 1, "portal 策略不跳过，但要抓门户页"
    assert refs[0].content_url == ""
    assert refs[0].fetch_url == refs[0].detail_url


def test_external_link_policy_skip_drops_notice(tables_cfg) -> None:
    skipping = replace(tables_cfg, portal=replace(tables_cfg.portal, external_link_policy="skip"))
    refs = list_page.parse_list_json(_tables_payload(REAL_RECORD, PORTAL_RECORD), MUC_API_URL, skipping)

    assert len(refs) == 1
    assert refs[0].title == PORTAL_RECORD["notice_title"], "只保留门户内的那条"


def test_detail_fetch_uses_content_url(cfg, tables_cfg) -> None:
    """站外通知：采集应请求 notice_link，而不是门户详情页。"""
    ref = list_page.parse_list_json(_tables_payload(REAL_RECORD), MUC_API_URL, tables_cfg)[0]
    site = FakeTransport(
        pages={REAL_RECORD["notice_link"]: "<html><div id='content' class='content_all'>微信文章正文</div></html>"}
    )

    raw = PortalPageFetcher(cfg, site).fetch_detail(ref)

    assert raw.ok is True
    assert site.requests == [REAL_RECORD["notice_link"]]
    assert raw.html_path.startswith("data/raw/html/"), "归档路径仍按门户 detail_url 命名（幂等键稳定）"


def test_manifest_keeps_content_url(cfg, tables_cfg) -> None:
    from src.crawler.detail_page import append_manifest, load_manifest

    ref = list_page.parse_list_json(_tables_payload(REAL_RECORD), MUC_API_URL, tables_cfg)[0]
    from src.contracts import RawArticle

    raw = RawArticle.success(ref, "<html>外部正文</html>", ref.raw_html_path())
    append_manifest(raw, cfg)

    entries = load_manifest(cfg)
    assert entries[0].ref.content_url == REAL_RECORD["notice_link"], "清单要带回站外地址"


# ======================================================================
# 业务应答外壳（会话过期会以 HTTP 200 返回失败外壳）
# ======================================================================


def test_parse_list_json_error_carries_envelope(api_cfg) -> None:
    """接口用 HTTP 200 回失败外壳时，错误信息必须直接给出 code/msg。"""
    payload = json.dumps({"code": 401, "msg": "未登录", "success": False}, ensure_ascii=False)

    with pytest.raises(ParseError) as excinfo:
        list_page.parse_list_json(payload, MUC_API_URL, api_cfg)

    message = str(excinfo.value)
    assert "登录态失效" in message
    assert "未登录" in message, "必须把外壳里的 msg 带出来，否则排查会跑到错误方向"


def test_envelope_summary_extracts_scalars() -> None:
    summary = list_page.envelope_summary({"code": 0, "msg": "success", "tables": [1], "page": {}})
    assert "code" in summary and "success" in summary and "tables" in summary
    assert list_page.envelope_summary("not a dict").startswith("响应类型")


def test_iter_refs_auto_stops_on_fetch_failure(tables_cfg, caplog) -> None:
    """会话过期时必须**停下来报错**，而不是当成"翻到最后一页"静默结束。"""

    class _FailsOnPage2(FakeTransport):
        def post_json(self, url: str, payload, *, referer: str = "") -> HttpResponse:
            self.requests.append(url)
            self.posts.append(dict(payload))
            page = int(payload.get("currentPage", 1))
            if page >= 2:
                # 模拟会话过期：HTTP 200 + 失败外壳
                return HttpResponse(
                    url=url,
                    status_code=200,
                    text=json.dumps({"code": 401, "msg": "未登录"}, ensure_ascii=False),
                )
            record = {"notice_id": "1", "notice_title": "A", "notice_release_time": "2026-09-16 10:15"}
            return HttpResponse(url=url, status_code=200, text=json.dumps({"tables": [record]}, ensure_ascii=False))

    site = _FailsOnPage2()
    auto = replace(tables_cfg, portal=replace(tables_cfg.portal, page_start=1, page_end=0))

    with caplog.at_level("ERROR"):
        refs = list(crawler.iter_refs(PortalPageFetcher(auto, site), auto))

    assert len(refs) == 1, "第 1 页的数据要保留"
    assert "抓取失败" in caplog.text, "必须留下明确的错误日志"
    assert "登录态" in caplog.text, "日志要点明最可能的原因"
    assert [post["currentPage"] for post in site.posts] == [1, 2], "失败后不应继续翻页"


def test_iter_refs_explicit_range_skips_failed_page(cfg) -> None:
    """显式范围下，单页失败只跳过该页，不影响其余页。"""

    class _FailsOnPage2(FakeTransport):
        def post_json(self, url: str, payload, *, referer: str = "") -> HttpResponse:
            self.requests.append(url)
            page = int(payload.get("currentPage", 1))
            if page == 2:
                return HttpResponse(url=url, status_code=200, text='{"code": 500, "msg": "boom"}')
            record = {"notice_id": str(page), "notice_title": f"第{page}页通知", "notice_release_time": ""}
            return HttpResponse(url=url, status_code=200, text=json.dumps({"tables": [record]}, ensure_ascii=False))

    site = _FailsOnPage2()
    ranged = replace(
        cfg,
        portal=replace(
            cfg.portal,
            mode="api",
            api_url=MUC_API_URL,
            detail_url_template=MUC_DETAIL_TEMPLATE,
            api_list_path="tables",
            api_title_field="notice_title",
            api_date_field="notice_release_time",
            page_start=1,
            page_end=3,
        ),
    )

    refs = list(crawler.iter_refs(PortalPageFetcher(ranged, site), ranged))

    assert [ref.title for ref in refs] == ["第1页通知", "第3页通知"], "第 2 页失败只跳过它"


def test_config_rejects_bad_external_policy(cfg_root) -> None:
    with pytest.raises(ConfigError):
        _write_api_config(
            cfg_root,
            mode="api",
            api_url=MUC_API_URL,
            detail_url_template=MUC_DETAIL_TEMPLATE,
            external_link_policy="whatever",
        )


def test_config_accepts_auto_page_range(cfg_root) -> None:
    loaded = _write_api_config(
        cfg_root,
        mode="api",
        api_url=MUC_API_URL,
        detail_url_template=MUC_DETAIL_TEMPLATE,
        page_end=0,
    )
    assert loaded.portal.page_end == 0, "page_end=0 表示自动翻到最后一页，必须合法"


def test_config_still_rejects_inverted_range(cfg_root) -> None:
    with pytest.raises(ConfigError):
        _write_api_config(
            cfg_root,
            mode="api",
            api_url=MUC_API_URL,
            detail_url_template=MUC_DETAIL_TEMPLATE,
            page_start=5,
            page_end=2,
        )
