"""校验层与统计层单测。

覆盖 README/docs/architecture.md 里写明的判定口径：
未知字段是 WARNING（进人工清单，不阻塞入库）、溯源红线是 ERROR、
OCR 来源强制人工复核、去重按 source_url、抽检随机可复现。
"""

from __future__ import annotations

import json

import pytest

from src.contracts import (
    CORE_FIELDS,
    ArticleRef,
    ContentKind,
    ErrorCode,
    ExtractMethod,
    FieldHit,
    JobRecord,
    RecordValidator,
    Severity,
)
from src.validation.statistics import (
    field_completeness,
    format_completeness,
    method_distribution,
    render_console,
    sample_for_review,
    summarize,
    write_summary,
)
from src.validation.validator import (
    DefaultValidator,
    build_validator,
    deduplicate,
    needs_manual_review,
    validate_batch,
)

LIST_URL = "https://example.edu.cn/jobs?page=1"

DEFAULT_VALUES = {
    "graduation_year": "2025",
    "grade": "大四",
    "degree": "本科",
    "major": "软件工程",
    "city": "北京",
    "employer": "某某科技有限公司",
    "position": "算法工程师",
}


def make_record(url: str = "https://example.edu.cn/a/1", **overrides: str) -> JobRecord:
    """构造记录；传 ``值`` 为 SKIP 表示该字段保持「未知」。"""
    record = JobRecord.empty(ArticleRef(detail_url=url, list_url=LIST_URL))
    hits = {}
    for field in CORE_FIELDS:
        value = overrides.get(field, DEFAULT_VALUES[field])
        if value == "SKIP":
            continue
        hits[field] = FieldHit(field, value, ExtractMethod.RULE, f"证据-{field}")
    record.apply_hits(hits)
    return record


@pytest.fixture()
def validator(cfg) -> DefaultValidator:
    return DefaultValidator(cfg)


# ---------------------------------------------------------------- 校验口径


def test_complete_record_passes(validator) -> None:
    result = validator.validate(make_record())
    assert result.ok is True
    assert result.needs_manual_review is False
    assert result.error_messages == ()


def test_unknown_field_is_warning_not_error(validator) -> None:
    result = validator.validate(make_record(position="SKIP"))
    assert result.ok is True, "缺失字段不阻塞入库（数据集价值在于可追溯）"
    assert result.needs_manual_review is True
    assert any(issue.code is ErrorCode.EXTRACT_INCOMPLETE for issue in result.issues)
    assert all(issue.severity is Severity.WARNING for issue in result.issues)


def test_missing_required_column_is_error(validator) -> None:
    record = make_record()
    record.source_url = ""
    result = validator.validate(record)
    assert result.ok is False
    assert any(issue.field_name == "source_url" and issue.severity is Severity.ERROR for issue in result.issues)


def test_bad_graduation_year_format(validator) -> None:
    result = validator.validate(make_record(graduation_year="25届"))
    assert result.ok is False
    assert any(issue.code is ErrorCode.FIELD_OUT_OF_RANGE for issue in result.issues)


def test_out_of_range_graduation_year(validator) -> None:
    result = validator.validate(make_record(graduation_year="1900"))
    assert result.ok is False
    assert any(issue.field_name == "graduation_year" for issue in result.issues)


def test_non_http_source_url_is_error(validator) -> None:
    record = make_record()
    record.source_url = "/info/2025/1001.htm"
    result = validator.validate(record)
    assert result.ok is False, "不可溯源的记录不允许入库"
    assert any(issue.field_name == "source_url" for issue in result.issues)


def test_value_outside_lexicon_warns(validator) -> None:
    result = validator.validate(make_record(degree="中专", city="雄安新区"))
    assert result.ok is True, "不在词表内只提示、不阻塞"
    warned = {issue.field_name for issue in result.issues if issue.severity is Severity.WARNING}
    assert {"degree", "city"} <= warned


def test_ocr_content_requires_manual_review(validator) -> None:
    record = make_record()
    record.content_kind = ContentKind.OCR.value
    result = validator.validate(record)
    assert result.ok is True
    assert result.needs_manual_review is True, "OCR 来源必须人工复核（合规红线 5）"
    assert any(issue.code is ErrorCode.OCR_NEEDS_REVIEW for issue in result.issues)


def test_content_kind_html_does_not_trigger_ocr_rule(validator) -> None:
    result = validator.validate(make_record())
    assert not any(issue.code is ErrorCode.OCR_NEEDS_REVIEW for issue in result.issues)


# ------------------------------------------------------------ 批量与去重


def test_validate_batch_preserves_order(cfg) -> None:
    records = [make_record("https://example.edu.cn/a/1"), make_record("https://example.edu.cn/a/2", city="SKIP")]
    results = validate_batch(records, cfg)
    assert [item.record.source_url for item in results] == [
        "https://example.edu.cn/a/1",
        "https://example.edu.cn/a/2",
    ]
    assert [item.ok for item in results] == [True, True]
    assert [item.needs_manual_review for item in results] == [False, True]


def test_deduplicate_keeps_first_and_drops_empty_url() -> None:
    first = make_record("https://example.edu.cn/a/1")
    duplicate = make_record("https://example.edu.cn/a/1", city="上海")
    other = make_record("https://example.edu.cn/a/2")
    orphan = make_record("https://example.edu.cn/a/3")
    orphan.source_url = ""

    unique = deduplicate([first, duplicate, other, orphan])
    assert [record.source_url for record in unique] == [
        "https://example.edu.cn/a/1",
        "https://example.edu.cn/a/2",
    ]
    assert unique[0].city == "北京", "去重保留首次出现的那条"


def test_needs_manual_review_delegates(cfg) -> None:
    validator = build_validator(cfg)
    assert isinstance(validator, RecordValidator)
    assert needs_manual_review(validator.validate(make_record(position="SKIP"))) is True
    assert needs_manual_review(validator.validate(make_record())) is False


# ------------------------------------------------------------------ 统计


def test_field_completeness_and_method_distribution() -> None:
    records = [make_record("https://example.edu.cn/a/1"), make_record("https://example.edu.cn/a/2", city="SKIP")]
    completeness = field_completeness(records)
    assert completeness["city"] == 0.5
    assert completeness["degree"] == 1.0
    assert method_distribution(records) == {"rule": 2, "llm": 0, "hybrid": 0, "none": 0}
    assert field_completeness([])["city"] == 0.0


def test_sample_for_review_is_reproducible(cfg) -> None:
    records = [make_record(f"https://example.edu.cn/a/{index}") for index in range(10)]
    first = sample_for_review(records, cfg)
    second = sample_for_review(records, cfg)
    assert [record.source_url for record in first] == [record.source_url for record in second], (
        "同一 seed 必须抽到同一批样本，否则抽检结论无法复核"
    )
    assert len(first) == 1, "10 条 × 10% 向上取整 = 1 条"
    assert sample_for_review([], cfg) == []


def test_format_completeness_text() -> None:
    assert format_completeness(make_record()) == "7/7"
    text = format_completeness(make_record(position="SKIP", employer="SKIP"))
    assert text.startswith("5/7")
    assert "单位" in text and "岗位" in text


def test_summarize_and_render(cfg) -> None:
    ocr_record = make_record("https://example.edu.cn/a/3", position="SKIP")
    ocr_record.content_kind = ContentKind.OCR.value
    records = [make_record("https://example.edu.cn/a/1"), make_record("https://example.edu.cn/a/2"), ocr_record]

    summary = summarize(records, cfg)
    assert summary["total"] == 3
    assert summary["complete_records"] == 2
    assert summary["manual_review_count"] == 1
    assert summary["content_kind"]["ocr"] == 1
    assert summary["content_kind"]["html"] == 2
    assert summary["extract_method"]["rule"] == 3
    assert summary["unknown_total"] == 1, "只有一条记录缺一个字段"
    assert summary["sample_size"] == 1
    assert summary["field_source"]["city"]["rule"] == 3

    text = render_console(summary)
    assert "记录总数" in text and "字段完整率" in text and "人工抽检" in text


def test_write_summary(tmp_path) -> None:
    target = tmp_path / "reports" / "summary.json"
    written = write_summary({"total": 1, "completeness": {"city": 1.0}}, str(target))
    assert written == str(target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["total"] == 1
