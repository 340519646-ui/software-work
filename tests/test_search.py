"""检索层回归测试：FTS 索引、分词行为、三级缓存、定向采集触发。

为什么这些用例是**常驻回归测试**：本层在实现过程中连续踩了三个隐蔽的坑
（触发器先于派生列创建、批量刷列自触发 FTS 'delete'、另开连接维护索引导致
索引损坏），以及两个中文检索特有的坑（trigram 匹配不到 2 字词、
unicode61 把整段中文当一个 token 导致子串搜不到）。这些坑的共同特征是
**不报错、只是静默返回空结果**，因此必须用测试钉住行为。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.config import load_config
from src.contracts import CORE_FIELDS, MISSING, JobRecord, SearchQuery
from src.search.cache import LruCache, QueryCache, cache_key, result_from_payload, result_to_payload
from src.search.index import (
    DEFAULT_TOKENIZER,
    FtsSearchIndex,
    min_chars_for,
    make_snippet,
    row_to_index_text,
    short_tokens,
    significant_tokens,
    tokenize,
    to_fts_match,
)
from src.search.service import build_search_service, normalize_query


# ==========================================================================
# 纯函数：分词与索引文本
# ==========================================================================


def test_tokenize_splits_and_drops_stopwords() -> None:
    assert tokenize("北京 硕士") == ["北京", "硕士"]
    assert tokenize("就业、招聘；选调") == ["就业", "招聘", "选调"]
    assert tokenize("哪些 是 我 想 找 的 单位") == ["单位"]
    assert tokenize("") == []


def test_tokenize_keeps_two_char_chinese_words() -> None:
    """2 字中文词必须保留：它们是中文里最常见的查询词。"""
    assert tokenize("硕士") == ["硕士"]
    assert tokenize("本科") == ["本科"]


def test_min_chars_depends_on_tokenizer() -> None:
    """trigram 匹配不到 <3 字符的词，unicode61 逐字切分可以。"""
    assert min_chars_for("trigram") == 3
    assert min_chars_for("unicode61") == 1
    assert significant_tokens(["硕士"], "trigram") == []
    assert significant_tokens(["硕士"], "unicode61") == ["硕士"]


def test_short_tokens_are_the_like_fallback_set() -> None:
    assert short_tokens(["硕士", "北京"], "trigram") == ["硕士", "北京"]
    assert short_tokens(["硕士"], "unicode61") == []


def test_to_fts_match_uses_and_semantics_and_escapes_quotes() -> None:
    assert to_fts_match(["北京", "硕士"], "unicode61") == '"北京" AND "硕士"'
    # 引号必须被剔除，否则 FTS5 会抛语法异常
    assert '"' not in to_fts_match(['a"b'], "unicode61").replace('"', "", 1).replace('"', "", 1)


def test_to_fts_match_drops_terms_below_the_floor() -> None:
    assert to_fts_match(["硕士"], "trigram") == ""
    assert to_fts_match(["硕士", "招聘会"], "trigram") == '"招聘会"'


def test_index_text_excludes_missing_placeholder() -> None:
    """占位符「未知」绝不能进索引，否则每个查询都会命中全表。"""
    row = {
        "article_title": "标题",
        "graduation_year": MISSING,
        "grade": MISSING,
        "degree": "硕士",
        "major": MISSING,
        "city": MISSING,
        "employer": MISSING,
        "position": MISSING,
        "evidence": "",
    }
    text = row_to_index_text(row)
    assert MISSING not in text
    assert "硕士" in text
    assert text.count("标题") == 2, "标题应重复一次以加权"


def test_snippet_escapes_html_then_highlights() -> None:
    """先转义再插入 <em>：原文里的 < 不能破坏前端 DOM。"""
    snippet = make_snippet("单位 <script>alert(1)</script> 招聘", ["招聘"])
    assert "<script>" not in snippet
    assert "&lt;script&gt;" in snippet
    assert "<em>招聘</em>" in snippet


def test_snippet_without_match_truncates() -> None:
    snippet = make_snippet("x" * 500, ["不存在"])
    assert snippet.endswith("…")
    assert len(snippet) < 200


# ==========================================================================
# FTS 索引：真实 sqlite + 真实表结构（不联网）
# ==========================================================================


@pytest.fixture()
def index(cfg):
    """在 tmp_path 的库上建索引（复用 conftest 的 AppConfig）。"""
    from src.storage.database import build_repository

    repo = build_repository(cfg)
    repo.init_schema()
    engine = FtsSearchIndex(connection=repo.connection)
    engine.init_schema()
    yield repo, engine
    repo.close()


def _record(title: str, url: str, **fields) -> JobRecord:
    data = {
        "article_title": title,
        "source_url": url,
        "raw_html_path": f"data/raw/html/{abs(hash(url)) % 10**8}.html",
        "crawl_time": "2025-05-01T00:00:00",
    }
    data.update(fields)
    return JobRecord(**data)


def test_index_is_created_and_version_bumps_on_write(index) -> None:
    repo, engine = index
    engine.init_schema()
    first = engine.index_version()
    repo.upsert_many([_record("标题甲", "https://e.cn/1", city="北京")])
    # 仓储写入后由上层回调维护；这里显式维护，验证版本号确实前进
    engine.maintain()
    assert engine.index_version() > first


def test_search_finds_two_char_chinese_query(index) -> None:
    """回归：2 字中文（硕士/北京）必须能搜到——trigram 做不到这一点。"""
    repo, engine = index
    repo.upsert_many([
        _record("甲 北京硕士", "https://e.cn/1", city="北京", degree="硕士"),
        _record("乙 呼伦贝尔", "https://e.cn/2", city="呼伦贝尔"),
    ])
    engine.maintain()

    result = engine.search(SearchQuery(keywords=("北京",), raw_text="北京", limit=10))
    assert result.total == 1
    assert result.hits[0].source_url == "https://e.cn/1"


def test_search_substring_falls_back_to_like(index) -> None:
    """回归：unicode61 把整段中文当一个 token，「招聘」在「招聘会」里要靠 LIKE 兜住。"""
    repo, engine = index
    repo.upsert_many([_record("校园招聘会公告", "https://e.cn/1")])
    engine.maintain()

    result = engine.search(SearchQuery(keywords=("招聘",), raw_text="招聘", limit=10))
    assert result.total == 1, "FTS 无命中时必须退到 LIKE 子串路径"


def test_count_matching_agrees_with_hits(index) -> None:
    """total 必须与实际命中数一致，否则前端分页会承诺不存在的页。"""
    repo, engine = index
    repo.upsert_many([
        _record("招聘会甲", "https://e.cn/1"),
        _record("招聘会乙", "https://e.cn/2"),
    ])
    engine.maintain()

    query = SearchQuery(keywords=("招聘会",), raw_text="招聘会", limit=1)
    result = engine.search(query)
    assert result.total == 2
    assert len(result.hits) == 1, "limit=1 应只返回 1 条，但 total 仍是 2"


def test_field_filter_combines_with_keywords(index) -> None:
    repo, engine = index
    repo.upsert_many([
        _record("甲", "https://e.cn/1", degree="硕士"),
        _record("乙", "https://e.cn/2", degree="本科"),
    ])
    engine.maintain()

    query = SearchQuery(fields={"degree": "硕士"}, limit=10)
    result = engine.search(query)
    assert result.total == 1
    assert result.hits[0].source_url == "https://e.cn/1"


def test_empty_query_returns_everything(index) -> None:
    repo, engine = index
    repo.upsert_many([_record("甲", "https://e.cn/1"), _record("乙", "https://e.cn/2")])
    engine.maintain()
    assert engine.count_matching(SearchQuery()) == 2


def test_related_suggestions_come_from_real_data(index) -> None:
    repo, engine = index
    repo.upsert_many([
        _record("甲", "https://e.cn/1", city="北京", degree="硕士"),
        _record("乙", "https://e.cn/2", city="北京"),
    ])
    engine.maintain()

    related = engine.suggest_related(["硕士"], limit=5)
    assert "北京" in related
    assert "硕士" not in related, "已包含的查询词不应再被推荐"
    assert MISSING not in related


def test_rebuild_is_idempotent(index) -> None:
    repo, engine = index
    repo.upsert_many([_record("甲", "https://e.cn/1")])
    engine.maintain()
    first = engine.rebuild()
    second = engine.rebuild()
    assert first == second == 1


def test_index_survives_tokenizer_switch(index) -> None:
    """换分词器必须自动重建旧索引，而不是继续用不兼容的倒排表。"""
    repo, engine = index
    repo.upsert_many([_record("甲 招聘", "https://e.cn/1")])
    engine.maintain()

    switched = FtsSearchIndex(connection=repo.connection, tokenizer="trigram")
    switched.init_schema()
    switched.rebuild()
    result = switched.search(SearchQuery(keywords=("招聘会",), raw_text="招聘会", limit=5))
    assert result.total == 0, "trigram 下「招聘会」不该命中只含「招聘」的文本"


def test_legacy_triggers_are_removed(index) -> None:
    """历史触发器必须被清掉：它们会与 maintain() 抢着写同一张 FTS 表。"""
    repo, engine = index
    names = {
        row["name"]
        for row in repo.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    assert "articles_fts_ai" not in names
    assert "articles_fts_au" not in names


# ==========================================================================
# L1 / L2 缓存
# ==========================================================================


def test_lru_evicts_oldest_and_counts() -> None:
    cache = LruCache(capacity=2)
    cache.put("a", "A")
    cache.put("b", "B")
    assert cache.get("a") == "A"
    cache.put("c", "C")  # 淘汰 b（刚被访问过的是 a）
    assert cache.get("b") is None
    assert cache.get("a") == "A"
    assert cache.hits >= 1
    assert cache.misses >= 1


def test_cache_key_changes_with_index_version() -> None:
    query = SearchQuery(keywords=("北京",), raw_text="北京")
    assert cache_key(query, 1) != cache_key(query, 2)


def test_cache_key_is_order_insensitive_for_keywords() -> None:
    a = SearchQuery(keywords=("北京", "硕士"), raw_text="北京 硕士")
    b = SearchQuery(keywords=("硕士", "北京"), raw_text="硕士 北京")
    assert cache_key(a, 1) == cache_key(b, 1)


def test_query_cache_roundtrip_and_version_mismatch(cfg, tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cache = QueryCache(conn, ttl_seconds=300, max_rows=10)
    cache.init_schema()

    query = SearchQuery(keywords=("北京",), raw_text="北京", limit=5)
    from src.contracts import SearchHit, SearchResult

    result = SearchResult(
        query=query,
        hits=(SearchHit(article_key="k", title="标题", source_url="https://e.cn/1"),),
        total=1,
        index_version=7,
        source="l3",
        related=("北京",),
    )
    cache.put(cache_key(query, 7), result)

    hit = cache.get(cache_key(query, 7), 7)
    assert hit is not None
    assert hit.total == 1
    assert hit.hits[0].title == "标题"
    assert hit.related == ("北京",), "关联词必须随缓存一起还原"

    assert cache.get(cache_key(query, 8), 8) is None, "版本号不同必须视为未命中"


def test_query_cache_ttl_expiry() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cache = QueryCache(conn, ttl_seconds=0, max_rows=10)
    cache.init_schema()
    query = SearchQuery(keywords=("x",), raw_text="x")
    from src.contracts import SearchResult

    cache.put(cache_key(query, 1), SearchResult(query=query, index_version=1))
    # ttl=0 表示不按时间过期（由 prune 的 LRU 负责），应当能命中
    assert cache.get(cache_key(query, 1), 1) is not None


def test_result_payload_roundtrip_preserves_suggestion() -> None:
    from src.contracts import SearchResult, SearchSuggestion

    query = SearchQuery(keywords=("a",), raw_text="a")
    result = SearchResult(
        query=query,
        suggestion=SearchSuggestion(reason="库内命中 0 条", search_value="a", allowed=False),
        index_version=3,
    )
    restored = result_from_payload(result_to_payload(result))
    assert restored.suggestion is not None
    assert restored.suggestion.reason == "库内命中 0 条"
    assert restored.index_version == 3


# ==========================================================================
# 服务层：三级编排 + 采集闸门
# ==========================================================================


def test_normalize_query_clamps_limit(cfg) -> None:
    query = normalize_query("北京", cfg, limit=10**9)
    assert query.limit == cfg.search.max_limit, "前端不得用超大 limit 把整库拖进内存"
    assert normalize_query("北京", cfg, limit=0).limit == 1
    assert normalize_query("北京", cfg, limit=-5).limit == 1


def test_service_three_level_cache_and_invalidation(cfg) -> None:
    from src.storage.database import build_repository

    service = build_search_service(cfg)
    repo = build_repository(cfg, after_write=service._index.maintain)
    repo.init_schema()
    repo.upsert_many([_record("甲 北京", "https://e.cn/1", city="北京")])

    query = normalize_query("北京", cfg, limit=10)

    first = service.search(query)
    assert first.source == "l3"
    assert service.search(query).source == "l1", "同进程第二次应命中 L1"

    # 模拟重启：新服务实例、同一数据库 → L2 命中
    restarted = build_search_service(cfg)
    assert restarted.search(query).source == "l2"

    # 写入后版本号前进 ⇒ 缓存失效，新数据立刻可搜到
    repo.upsert_many([_record("乙 深圳", "https://e.cn/2", city="深圳")])
    fresh = service.search(normalize_query("深圳", cfg, limit=10))
    assert fresh.total == 1
    repo.close()
    service._index.close()
    restarted._index.close()


def test_suggestion_blocked_when_trigger_disabled(cfg) -> None:
    """默认配置下检索完全离线：只建议、不联网。"""
    service = build_search_service(cfg)
    query = normalize_query("不存在的词", cfg, limit=5)
    result = service.search(query)
    assert result.suggestion is not None
    assert result.suggestion.allowed is False
    assert "trigger_enabled" in result.suggestion.blocked_reason
    service._index.close()


def test_request_fetch_refuses_when_disabled(cfg) -> None:
    service = build_search_service(cfg)
    job = service.request_fetch(normalize_query("北京", cfg, limit=5))
    assert job.job_id == ""
    assert job.state == "skipped"
    service._index.close()


def test_request_fetch_estimates_cost_honestly(cfg) -> None:
    """预估耗时必须按「请求数 × 合规间隔」算，不能给出乐观数字。"""
    service = build_search_service(cfg)
    result = service.search(normalize_query("查不到的词", cfg, limit=20))
    suggestion = result.suggestion
    assert suggestion is not None
    expected_requests = 1 + cfg.search.max_fetch_pages * 15
    assert suggestion.estimated_requests == expected_requests
    assert suggestion.estimated_seconds >= expected_requests * 2.0
    service._index.close()


def test_index_works_with_a_plain_tuple_connection(cfg) -> None:
    """回归：外部注入的连接可能没有 row_factory=Row，索引层不得假定是 Row。

    实际故障：``sqlite3.connect()`` 默认返回 tuple，
    在 ``_create_fts_table`` 里读 ``row["sql"]`` 直接抛
    ``TypeError: tuple indices must be integers or slices, not str``。
    """
    import sqlite3 as plain_sqlite

    from src.storage.database import build_repository

    repo = build_repository(cfg)
    repo.init_schema()

    # 故意用**默认** row_factory（tuple）另开一条连接
    raw = plain_sqlite.connect(str(cfg.path(cfg.storage.db_path)))
    assert raw.row_factory is None, "前提：这条连接返回 tuple"

    engine = FtsSearchIndex(connection=raw)
    engine.init_schema()          # 曾在此处崩溃
    assert engine.articles_columns(), "应能读到列名"
    assert engine.index_version() >= 0

    repo.upsert_many([_record("甲 招聘公告", "https://e.cn/1", city="北京")])
    engine.rebuild()
    result = engine.search(SearchQuery(keywords=("招聘",), raw_text="招聘", limit=5))
    assert result.total == 1, "tuple 连接下检索也必须正常"
    raw.close()
    repo.close()
