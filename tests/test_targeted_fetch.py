"""定向采集端到端集成测试（离线假门户，覆盖 fetch → extract → 入库 → 可搜）。

为什么单独立一个文件
--------------------
`tests/test_search.py` 只覆盖了采集的**闸门与计费**（开关/冷却/预算），
而 `fetcher.run_targeted_fetch` 的**真实链路**（门户检索 → 过滤 → 取详情 →
归档清单 → 抽取 → 校验 → 入库 → 索引失效）此前完全没有被测过。
这一层恰恰是最容易"看起来对、实际没生效"的地方：
`searchValue` 有没有真的下发、标题过滤有没有误杀、
新数据入库后能不能立刻被搜到——三者都必须用测试钉住。

全程离线：用 `FakeTransport` 冒充门户，不发任何真实请求。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.contracts import ArticleRef, HttpResponse, RawArticle
from src.crawler.crawler import PortalPageFetcher
from src.search.index import FtsSearchIndex
from src.search.service import normalize_query
from src.storage.database import build_repository
from tests.conftest import FIXTURES, MUC_API_URL, MUC_DETAIL_TEMPLATE, FakeTransport

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


@pytest.fixture()
def api_cfg(cfg):
    """接口模式配置，参数表与生产 config.yaml 一致。"""
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
            api_total_path="page.total",
            api_total_pages_path="page.totalCounts",
            api_title_field="notice_title",
            api_date_field="notice_release_time",
            external_link_policy="portal",
        ),
    )


class TargetedTransport(FakeTransport):
    """假门户：列表接口按查询串返回候选，详情页一律返回样本正文。

    额外记录**每次列表请求的查询串**，用于断言 searchValue 真的下发了
    （只断言返回结果是不够的：服务端忽略参数时结果看起来也可能"像对的"）。
    """

    def __init__(self, records):
        super().__init__()
        self._records = list(records)
        self.list_queries: list = []

    def post_json(self, url, payload, *, referer: str = ""):
        from urllib.parse import parse_qs, urlparse

        self.requests.append(url)
        self.posts.append(dict(payload))
        query = parse_qs(urlparse(url).query)
        self.list_queries.append(query)

        page = int((query.get("currentPage") or ["1"])[0])
        window = self._records if page == 1 else []
        body = {
            "datas": {"tables": window},
            "page": {"total": len(self._records), "totalCounts": 1},
            "msg": "success",
            "state": True,
        }
        return HttpResponse(url=url, status_code=200, text=json.dumps(body, ensure_ascii=False))

    def get(self, url, *, referer: str = "", binary: bool = False):
        self.requests.append(url)
        if binary:
            return HttpResponse(url=url, status_code=404, error="no images in this fake")
        html = (FIXTURES / "article_text.html").read_text(encoding="utf-8")
        return HttpResponse(url=url, status_code=200, text=html)


def _record(notice_id: str, title: str) -> dict:
    return {
        "notice_id": notice_id,
        "notice_title": title,
        "notice_release_time": "2026-09-16 10:15",
        "notice_link": "",
        "notice_link_state": 0,
    }


@pytest.fixture()
def portal(api_cfg):
    """一个候选：标题命中查询词一条 + 不命中一条（用于验证兜底过滤）。"""
    return TargetedTransport([
        _record("1001", "职点迷津校友分享会第十八期"),
        _record("1002", "关于校园网络维护的通知"),  # 不含查询词，必须被过滤掉
    ])


def _fetcher_factory(transport):
    return lambda cfg: PortalPageFetcher(cfg, transport)


def _run(api_cfg, transport, query_text, extra=None):
    """跑一次定向采集，返回 (counters, repository, index)。"""
    from src.search.fetcher import run_targeted_fetch
    from src.parser.extractor import build_extractor
    from src.validation.validator import build_validator

    repo_holder = {}

    def repo_factory(cfg, after_write=None):
        repo = build_repository(cfg, after_write=after_write)
        repo_holder["repo"] = repo
        return repo

    cfg = api_cfg
    if extra:
        cfg = replace(cfg, search=replace(cfg.search, **extra))

    db_path = cfg.path(cfg.storage.db_path)
    repo = build_repository(cfg)
    repo.init_schema()
    index = FtsSearchIndex(connection=repo.connection)
    index.init_schema()

    counters = run_targeted_fetch(
        cfg,
        normalize_query(query_text, cfg, limit=10),
        index=index,
        fetcher_factory=_fetcher_factory(transport),
        extractor_factory=build_extractor,
        validator_factory=build_validator,
        repository_factory=repo_factory,
    )
    return counters, repo, index


def test_search_value_is_sent_to_the_portal(api_cfg, portal) -> None:
    """核心前提：关键词必须作为 searchValue 下发到服务端（否则就是全量采集）。"""
    _run(api_cfg, portal, "职点迷津")

    assert portal.list_queries, "列表接口必须被请求过"
    first = portal.list_queries[0]
    assert first.get("searchValue") == ["职点迷津"], (
        f"searchValue 没有下发或值不对：{first.get('searchValue')}"
    )
    assert first.get("type") == ["10"], "栏目过滤必须保留"


def test_only_title_matching_records_are_ingested(api_cfg, portal) -> None:
    """标题兜底过滤：门户关键词检索是宽松匹配，擦边结果不入库。

    假门户返回 2 条，其中只有 1 条标题含查询词 → 只应入库 1 条。
    """
    counters, repo, index = _run(api_cfg, portal, "职点迷津")

    assert counters["refs_found"] == 2, "候选应有 2 条"
    assert counters["records_inserted"] == 1, "不匹配标题的擦边结果不得入库"
    assert repo.count() == 1

    stored = repo.fetch_all()
    assert "职点迷津" in stored[0].article_title


def test_ingested_record_is_extracted_and_immediately_searchable(api_cfg, portal) -> None:
    """采集完成后必须立刻能搜到（版本号失效缓存这条链路要真的通）。"""
    counters, repo, index = _run(api_cfg, portal, "职点迷津")

    assert counters["details_ok"] == 1
    assert counters["records_inserted"] == 1

    # 样本正文含全部七项 → 抽取与入库都应生效
    record = repo.fetch_all()[0]
    assert record.degree == "本科"
    assert record.city == "北京"
    assert record.employer == "某某科技有限公司"
    assert record.position == "算法工程师"

    # 索引：按采集进来的字段值检索，应当命中
    from src.contracts import SearchQuery

    result = index.search(SearchQuery(keywords=("北京",), raw_text="北京", limit=5))
    assert result.total == 1, "定向采集入库的数据必须立刻进入索引"
    assert result.hits[0].title == record.article_title


def test_repeat_fetch_is_idempotent(api_cfg) -> None:
    """同一篇重复采集不得产生第二条记录（既有幂等红线）。"""
    transport = TargetedTransport([_record("1001", "职点迷津校友分享会")])
    first, repo, index = _run(api_cfg, transport, "职点迷津")
    assert first["records_inserted"] == 1

    second, repo2, _ = _run(api_cfg, transport, "职点迷津")
    assert repo2.count() == 1, "重复采集必须按 source_url 幂等，不得新增"
    assert second["records_inserted"] == 0, "已入库的 URL 应被识别为非新增"


def test_max_fetch_pages_bounds_the_number_of_list_requests(api_cfg, portal) -> None:
    """预算闸门的落地形式：单次采集最多翻 max_fetch_pages 页。"""
    counters, _, _ = _run(api_cfg, portal, "职点迷津", extra={"max_fetch_pages": 1})
    assert counters["pages_fetched"] == 1

    transport = TargetedTransport([_record("1001", "职点迷津校友分享会")])
    counters2, _, _ = _run(api_cfg, transport, "职点迷津", extra={"max_fetch_pages": 2})
    # 第 2 页返回空 → 提前结束，但确实请求过 2 页
    assert counters2["pages_fetched"] == 2


def test_archive_and_manifest_written_for_offline_replay(api_cfg, portal) -> None:
    """定向采集也必须写归档与清单，否则无法离线复盘与重跑。"""
    _run(api_cfg, portal, "职点迷津")

    raw_dir = api_cfg.path(api_cfg.extract.raw_html_dir)
    archived = list(raw_dir.glob("*.html"))
    assert archived, "详情页必须归档"

    manifest = api_cfg.path("data/raw/manifest.jsonl")
    assert manifest.exists(), "必须写采集清单（sha1 归档名不可逆，靠清单还原 URL）"
    lines = [line for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "清单不能为空"
    entry = json.loads(lines[0])
    assert entry["detail_url"].startswith("https://my.muc.edu.cn/")
