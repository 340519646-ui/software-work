"""两级抽取与 OCR 单测。

重点验证三条口径：
1. **LLM 只能补空格**，且必须给出能在正文里找到的证据，否则丢弃（防幻觉）；
2. **OCR 结果一律标 [OCR] 且置信度封顶**，记录内容来源为 ocr（强制人工复核）；
3. **OCR 缓存按图片内容哈希命中**，命中时不再调用识别程序。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.contracts import (
    CORE_FIELDS,
    MISSING,
    ArticleRef,
    CleanArticle,
    ContentKind,
    ExtractMethod,
    FieldHit,
    ImageAsset,
    JobRecord,
    LlmError,
    OcrError,
    RawArticle,
)
from src.parser import llm, ocr
from src.parser.cleaner import clean_html, split_segments
from src.parser.extractor import TwoStageExtractor, build_extractor

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIST_URL = "https://example.edu.cn/jobs?page=1"


@pytest.fixture()
def ref() -> ArticleRef:
    return ArticleRef(detail_url="https://example.edu.cn/info/2025/1001.htm", list_url=LIST_URL)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture()
def text_article(ref: ArticleRef, cfg) -> CleanArticle:
    return clean_html(read_fixture("article_text.html"), ref, cfg)


# ======================================================================
# 二级抽取：LLM（全部离线，不发请求）
# ======================================================================


def test_is_available_false_when_disabled(cfg) -> None:
    assert llm.is_available(cfg) is False


def test_is_available_false_without_sdk(cfg) -> None:
    enabled = replace(cfg, llm=replace(cfg.llm, enabled=True, provider="openai", api_key="sk-x", base_url="https://api.example/v1", model="demo"))
    # openai SDK 未安装 → 不可用（而不是抛异常）
    assert llm.is_available(enabled) is False


def test_build_prompt_contains_field_labels_and_evidence_rule(text_article, cfg) -> None:
    system, user = llm.build_prompt(text_article, ["employer", "position"], cfg)
    assert "JSON" in system
    assert "单位" in user and "岗位" in user
    assert "evidence" in user
    assert "未知" in user
    # 正文会被折叠空白后放进提示词，因此比对去空白后的片段
    assert "软件工程" in user and "算法工程师" in user
    assert "\n" not in user.split("文章正文：")[-1], "提示词里的正文必须是单行（空白已折叠）"


def test_parse_llm_response_accepts_fenced_json(text_article) -> None:
    payload = (
        "```json\n"
        '{"employer": {"value": "某某科技有限公司", "evidence": "签约北京的某某科技有限公司"},'
        ' "position": {"value": "算法工程师", "evidence": "担任算法工程师"}}\n'
        "```"
    )
    hits = llm.parse_llm_response(payload, text_article)
    assert set(hits) == {"employer", "position"}
    assert hits["employer"].value == "某某科技有限公司"
    assert hits["employer"].method is ExtractMethod.LLM


def test_parse_llm_response_drops_fields_without_valid_evidence(text_article) -> None:
    payload = (
        '{"employer": {"value": "某互联网大厂", "evidence": "这段原文根本不存在"},'
        ' "city": {"value": "北京", "evidence": "签约北京"}}'
    )
    hits = llm.parse_llm_response(payload, text_article)
    assert "employer" not in hits, "证据在正文里找不到 → 视为幻觉，必须丢弃"
    assert hits["city"].value == "北京"


def test_parse_llm_response_skips_unknown_and_illegal_fields(text_article) -> None:
    payload = (
        '{"employer": {"value": "未知", "evidence": "签约"},'
        ' "salary": {"value": "20k", "evidence": "签约"},'
        ' "position": {"value": "算法工程师", "evidence": "担任算法工程师"}}'
    )
    hits = llm.parse_llm_response(payload, text_article)
    assert set(hits) == {"position"}


def test_parse_llm_response_rejects_bad_payload(text_article) -> None:
    with pytest.raises(LlmError):
        llm.parse_llm_response("这不是 JSON", text_article)
    with pytest.raises(LlmError):
        llm.parse_llm_response("[1, 2, 3]", text_article)
    with pytest.raises(LlmError):
        llm.parse_llm_response("", text_article)


def test_extract_by_llm_raises_when_dependencies_missing(text_article, cfg) -> None:
    enabled = replace(cfg, llm=replace(cfg.llm, enabled=True, provider="openai", api_key="sk-x", base_url="https://api.example/v1", model="demo"))
    with pytest.raises(LlmError):
        llm.extract_by_llm(text_article, ["employer"], enabled)
    assert llm.extract_by_llm(text_article, [], cfg) == {}, "没有待补字段时不应发起调用"


# ======================================================================
# OCR：缓存与打标（不依赖 tesseract）
# ======================================================================


@pytest.fixture()
def image_file(tmp_path: Path) -> Path:
    path = tmp_path / "long-image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload" * 50)
    return path


def test_cache_path_is_content_addressed(image_file: Path, cfg) -> None:
    first = ocr.cache_path_for(str(image_file), cfg)
    second = ocr.cache_path_for(str(image_file), cfg)
    assert first == second
    assert first.suffix == ".txt"

    copy = image_file.parent / "renamed-image.png"
    copy.write_bytes(image_file.read_bytes())
    assert ocr.cache_path_for(str(copy), cfg) == first, "同一内容不同文件名必须命中同一缓存"


def test_recognize_requires_enabled_flag(image_file: Path, cfg) -> None:
    with pytest.raises(OcrError):
        ocr.recognize(str(image_file), cfg)


def test_recognize_with_cache_skips_recognition_on_hit(image_file: Path, cfg, monkeypatch) -> None:
    calls = {"count": 0}

    def fake_recognize(path: str, config) -> str:
        calls["count"] += 1
        return "2025届 本科 软件工程 签约北京某某科技有限公司 算法工程师"

    monkeypatch.setattr(ocr, "recognize", fake_recognize)

    text, hit = ocr.recognize_with_cache(str(image_file), cfg)
    assert hit is False and calls["count"] == 1
    assert "算法工程师" in text

    text_again, hit_again = ocr.recognize_with_cache(str(image_file), cfg)
    assert hit_again is True
    assert calls["count"] == 1, "第二次必须直接读缓存，不再调用识别"
    assert text_again == text

    stats = ocr.cached_image_stats([str(image_file)], cfg)
    assert stats == {"ocr_cached": 1, "ocr_computed": 0}


def test_extract_by_ocr_marks_evidence_and_caps_confidence(ref: ArticleRef, cfg, image_file: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        ocr,
        "recognize",
        lambda path, config: "2025届 大四 本科 软件工程 签约北京的某某科技有限公司 担任算法工程师",
    )
    article = CleanArticle(ref=ref, title="图片型分享", text="", segments=split_segments("图片型分享", ""))

    hits = ocr.extract_by_ocr(article, [str(image_file)], cfg)

    assert hits, "OCR 文本必须能走一级规则抽出字段"
    for field, hit in hits.items():
        assert hit.evidence.startswith("[OCR]"), f"{field} 的证据必须带 OCR 标记"
        assert hit.confidence <= ocr.OCR_CONFIDENCE_CAP, "OCR 命中置信度必须封顶"
        assert hit.method is ExtractMethod.RULE


def test_extract_by_ocr_returns_empty_without_images(ref: ArticleRef, cfg) -> None:
    article = CleanArticle(ref=ref, title="t", text="", segments={})
    assert ocr.extract_by_ocr(article, [], cfg) == {}


# ======================================================================
# 两级组装
# ======================================================================


def test_two_stage_extractor_rules_only_on_text_article(ref: ArticleRef, cfg) -> None:
    raw = RawArticle.success(ref, read_fixture("article_text.html"), ref.raw_html_path())
    record = build_extractor(cfg).extract_from_html(raw)

    assert record.is_complete(), "文字型样本应七项齐备"
    assert record.extract_method == ExtractMethod.RULE.value
    assert record.content_kind == ContentKind.HTML.value
    assert record.unknown_fields() == ()
    assert record.evidence()


def test_two_stage_extractor_keeps_unknowns_for_image_article(ref: ArticleRef, cfg) -> None:
    raw = RawArticle.success(ref, read_fixture("article_image.html"), ref.raw_html_path())
    record = build_extractor(cfg).extract_from_html(raw)

    assert record.graduation_year == "2024"
    assert record.employer == MISSING
    assert record.needs_manual_review() is True
    assert set(record.unknown_fields()) == {"grade", "degree", "major", "city", "employer", "position"}


def test_should_call_llm_conditions(ref: ArticleRef, cfg) -> None:
    raw = RawArticle.success(ref, read_fixture("article_image.html"), ref.raw_html_path())
    extractor = TwoStageExtractor(cfg)
    record = extractor.extract_from_html(raw)

    assert extractor.should_call_llm(record, 1) is False, "LLM 未启用时不得触发"
    assert extractor.missing_fields_for_llm(record) == record.unknown_fields()

    enabled_cfg = replace(cfg, llm=replace(cfg.llm, enabled=True))
    enabled_extractor = TwoStageExtractor(enabled_cfg)
    assert enabled_extractor.should_call_llm(record, 1) is True
    assert enabled_extractor.should_call_llm(record, len(CORE_FIELDS)) is False, "命中数达标即不触发兜底"

    complete = two_stage_complete_record(enabled_extractor, ref)
    assert enabled_extractor.should_call_llm(complete, 0) is False, "字段齐备时不必调用 LLM"


def two_stage_complete_record(extractor: TwoStageExtractor, ref: ArticleRef) -> JobRecord:
    record = JobRecord.empty(ref)
    record.apply_hits(
        {field: FieldHit(field, f"值-{field}", ExtractMethod.RULE, "证据") for field in CORE_FIELDS}
    )
    return record


def test_llm_fallback_merges_as_hybrid(ref: ArticleRef, cfg, monkeypatch) -> None:
    enabled_cfg = replace(cfg, llm=replace(cfg.llm, enabled=True))
    extractor = TwoStageExtractor(enabled_cfg)

    def fake_extract_by_llm(article, missing_fields, config):
        return {
            field: FieldHit(field, f"LLM补-{field}", ExtractMethod.LLM, "正文里的证据片段")
            for field in missing_fields
        }

    monkeypatch.setattr("src.parser.extractor.llm.extract_by_llm", fake_extract_by_llm)

    raw = RawArticle.success(ref, read_fixture("article_image.html"), ref.raw_html_path())
    # 带上图片资产：正文很短 + 有图片 → 判定内容来源为 OCR（需人工复核）
    raw = replace(
        raw,
        images=(ImageAsset.from_bytes(b"fake-image", "https://example.edu.cn/i.png", ref.detail_url),),
    )
    record = extractor.extract_from_html(raw)

    assert record.extract_method == ExtractMethod.HYBRID.value, "规则 + LLM 同时命中必须归并为 hybrid"
    assert record.content_kind == ContentKind.OCR.value
    assert record.employer == "LLM补-employer"


def test_llm_failure_degrades_without_raising(ref: ArticleRef, cfg, monkeypatch) -> None:
    enabled_cfg = replace(cfg, llm=replace(cfg.llm, enabled=True))
    extractor = TwoStageExtractor(enabled_cfg)

    def boom(article, missing_fields, config):
        raise LlmError("接口挂了")

    monkeypatch.setattr("src.parser.extractor.llm.extract_by_llm", boom)

    raw = RawArticle.success(ref, read_fixture("article_image.html"), ref.raw_html_path())
    record = extractor.extract_from_html(raw)

    assert record.graduation_year == "2024", "LLM 失败必须降级为「保持未知」，不能中断解析"
    assert record.employer == MISSING
