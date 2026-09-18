"""存储层单测：幂等入库、去重、导出与待人工清单。

全部使用 tmp_path 与临时库（夹具见 tests/conftest.py），不触碰项目的 data/employment.db。
"""

from __future__ import annotations

import csv

import pytest

from src.contracts import (
    CORE_FIELDS,
    MISSING,
    ArticleRef,
    ContentKind,
    ExtractMethod,
    FieldHit,
    JobRecord,
    RecordRepository,
    ReviewStatus,
    StorageError,
)
from src.storage.database import SqliteRepository, build_repository

LIST_URL = "https://example.edu.cn/jobs?page=1"


def make_record(url: str, **overrides: str) -> JobRecord:
    """构造一条七项齐备的记录；overrides 可覆盖任意核心字段的值。"""
    record = JobRecord.empty(ArticleRef(detail_url=url, list_url=LIST_URL))
    hits = {
        name: FieldHit(name, overrides.get(name, f"值-{name}"), ExtractMethod.RULE, f"证据-{name}")
        for name in CORE_FIELDS
    }
    record.apply_hits(hits)
    return record


def make_incomplete_record(url: str) -> JobRecord:
    """只抽到城市，其余保持「未知」（用于验证待人工清单）。"""
    record = JobRecord.empty(ArticleRef(detail_url=url, list_url=LIST_URL))
    record.apply_hits({"city": FieldHit("city", "北京", ExtractMethod.RULE, "最终签约北京一家科技公司")})
    return record


@pytest.fixture()
def repo(cfg):
    repository = SqliteRepository(cfg)
    repository.init_schema()
    yield repository
    repository.close()


# ------------------------------------------------------------------ 建表与幂等


def test_init_schema_is_idempotent(repo) -> None:
    repo.init_schema()
    assert repo.count() == 0


def test_upsert_is_idempotent_by_source_url(repo) -> None:
    assert repo.upsert_many([make_record("https://example.edu.cn/a/1"), make_record("https://example.edu.cn/a/2")]) == 2
    assert repo.count() == 2

    repo.upsert_many([make_record("https://example.edu.cn/a/1", city="上海")])
    assert repo.count() == 2, "同一 source_url 重复写入必须是更新，不是新增"
    stored = {record.source_url: record for record in repo.fetch_all()}
    assert stored["https://example.edu.cn/a/1"].city == "上海"


def test_existing_source_urls(repo) -> None:
    repo.upsert_many([make_record("https://example.edu.cn/a/1")])
    assert repo.existing_source_urls() == {"https://example.edu.cn/a/1"}


def test_upsert_respects_batch_size(repo) -> None:
    # 夹具里 batch_size = 2，用 5 条记录逼出分批提交路径
    records = [make_record(f"https://example.edu.cn/a/{index}") for index in range(5)]
    assert repo.upsert_many(records) == 5
    assert repo.count() == 5


# ---------------------------------------------------------------- 读回与溯源


def test_fetch_all_roundtrip_keeps_fields_and_evidence(repo) -> None:
    repo.upsert_many([make_record("https://example.edu.cn/a/1", employer="某科技有限公司")])
    record = repo.fetch_all()[0]

    assert record.employer == "某科技有限公司"
    assert record.source_url == "https://example.edu.cn/a/1"
    assert record.list_url == LIST_URL
    assert record.raw_html_path.endswith(".html")
    assert record.crawl_time
    assert record.extract_method == ExtractMethod.RULE.value
    assert record.article_key, "article_key 由 source_url 推导，读回后仍应可计算"
    assert record.evidence(), "evidence 必须落库后读回，否则数据库不自描述"
    assert "employer=某科技有限公司" in record.evidence()


def test_unknown_fields_are_preserved(repo) -> None:
    repo.upsert_many([make_incomplete_record("https://example.edu.cn/a/2")])
    record = repo.fetch_all()[0]
    assert record.city == "北京"
    assert record.employer == MISSING
    assert record.needs_manual_review() is True


def test_content_kind_roundtrip(repo) -> None:
    record = make_record("https://example.edu.cn/a/3")
    record.content_kind = ContentKind.OCR.value
    repo.upsert_many([record])
    assert repo.fetch_all()[0].content_kind == ContentKind.OCR.value


# ------------------------------------------------------------- 待人工清单


def test_fetch_manual_review_only_returns_incomplete(repo) -> None:
    repo.upsert_many(
        [make_record("https://example.edu.cn/a/1"), make_incomplete_record("https://example.edu.cn/a/2")]
    )
    pending = repo.fetch_manual_review()
    assert len(pending) == 1
    assert pending[0].source_url == "https://example.edu.cn/a/2"


def test_write_manual_review_file(repo, cfg) -> None:
    repo.upsert_many([make_incomplete_record("https://example.edu.cn/a/2")])
    assert repo.write_manual_review(repo.fetch_manual_review()) == 1

    target = cfg.path(cfg.extract.manual_review_output)
    assert target.exists()
    with open(target, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["source_url"] == "https://example.edu.cn/a/2"
    assert rows[0]["city"] == "北京"
    assert rows[0]["employer"] == MISSING
    assert "city=北京" in rows[0]["evidence"]


# ------------------------------------------------------------------- 导出


def test_export_csv_header_follows_config_columns(repo, cfg) -> None:
    repo.upsert_many([make_record("https://example.edu.cn/a/1")])
    assert repo.export_csv() == 1

    target = cfg.path(cfg.output.csv_path)
    with open(target, encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = list(reader)
    assert tuple(header) == tuple(cfg.columns), "CSV 表头必须严格等于 fields.yaml 的列序"
    assert len(rows) == 1
    assert not (target.parent / (target.name + ".tmp")).exists(), "临时文件必须被清理"


def test_export_csv_is_idempotent(repo, cfg) -> None:
    repo.upsert_many([make_record("https://example.edu.cn/a/1")])
    repo.export_csv()
    first = cfg.path(cfg.output.csv_path).read_text(encoding="utf-8-sig")
    repo.export_csv()
    second = cfg.path(cfg.output.csv_path).read_text(encoding="utf-8-sig")
    assert first == second, "全量重建导出必须幂等"


def test_export_xlsx(repo, cfg) -> None:
    pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    repo.upsert_many([make_record("https://example.edu.cn/a/1")])
    assert repo.export_xlsx() == 1
    assert cfg.path(cfg.output.xlsx_path).exists()


# --------------------------------------------------------------- 复核回写


def test_update_review_status(repo) -> None:
    record = make_record("https://example.edu.cn/a/1")
    repo.upsert_many([record])
    assert repo.update_review_status(record.article_key, ReviewStatus.APPROVED.value) == 1
    assert repo.fetch_all()[0].review_status == ReviewStatus.APPROVED.value


def test_update_review_status_rejects_illegal_value(repo) -> None:
    with pytest.raises(StorageError):
        repo.update_review_status("whatever", "已阅")


# ------------------------------------------------------------- 接口一致性


def test_build_repository_satisfies_contract(cfg) -> None:
    repository = build_repository(cfg)
    assert isinstance(repository, RecordRepository), "必须满足 contracts.RecordRepository 协议"
    repository.init_schema()
    repository.close()


def test_article_key_column_is_stored(repo) -> None:
    record = make_record("https://example.edu.cn/a/1")
    repo.upsert_many([record])
    connection = repo._connect()
    value = connection.execute(
        'SELECT "article_key" FROM articles WHERE source_url = ?', (record.source_url,)
    ).fetchone()[0]
    assert value == record.article_key
