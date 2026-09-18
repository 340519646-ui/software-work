"""清洗层单测（纯函数为主，样本见 tests/fixtures/）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.contracts import SEGMENT_KEYS, ArticleRef
from src.parser.cleaner import (
    clean_html,
    normalize_text,
    split_segments,
    strip_tags,
    to_halfwidth,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIST_URL = "https://example.edu.cn/jobs?page=1"


@pytest.fixture()
def ref() -> ArticleRef:
    return ArticleRef(detail_url="https://example.edu.cn/info/2025/1001.htm", list_url=LIST_URL)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 归一化


def test_to_halfwidth_converts_ascii_but_keeps_chinese_punctuation() -> None:
    assert to_halfwidth("ＡＢＣ１２３") == "ABC123"
    assert to_halfwidth("薪资８０００元") == "薪资8000元"
    converted = to_halfwidth("好的，就这些。")
    assert "，" in converted and "。" in converted, "中文句读必须保留，否则分词与正则都会被破坏"
    assert to_halfwidth("") == ""


def test_normalize_text_collapses_whitespace_and_zero_width() -> None:
    assert normalize_text("  北京\u200b   大学 \n\n 计算机 ") == "北京 大学\n计算机"
    assert normalize_text("") == ""
    assert normalize_text(None) == ""
    assert normalize_text("全角ＡＢＣ") == "全角ABC"


# ---------------------------------------------------------------- 去标签


def test_strip_tags_keeps_visible_text() -> None:
    html = "<div><p>第一段</p><script>var a=1;</script><p>第二段</p></div>"
    text = strip_tags(html)
    assert "第一段" in text and "第二段" in text
    assert "var a=1" not in text, "script 内容必须被清掉"


def test_clean_html_extracts_title_and_segments(ref: ArticleRef, cfg) -> None:
    clean = clean_html(read_fixture("article_text.html"), ref, cfg)

    assert clean.title == "2025届毕业生张三的求职分享", "标题优先取 h1，而不是带站名后缀的 <title>"
    assert "软件工程" in clean.text
    assert set(clean.segments.keys()) == set(SEGMENT_KEYS), "分段键必须严格等于契约规定的 SEGMENT_KEYS"
    assert clean.segments["raw"] == clean.text
    assert clean.segments["headline"] == clean.title
    assert clean.source_kind == "html"
    assert clean.article_key == ref.article_key


def test_clean_html_on_image_article(ref: ArticleRef, cfg) -> None:
    clean = clean_html(read_fixture("article_image.html"), ref, cfg)
    assert clean.title == "2024届毕业生李四的就业分享"
    assert len(clean.text) < 200, "图片型文章正文很短——这正是需要 OCR 的信号"
    assert "图片形式" in clean.text


def test_clean_html_tolerates_empty_and_broken_html(ref: ArticleRef, cfg) -> None:
    for html in ("", "<div>未闭合", None):
        clean = clean_html(html or "", ref, cfg)
        assert clean.text == "" or isinstance(clean.text, str)
        assert set(clean.segments.keys()) == set(SEGMENT_KEYS)


def test_clean_html_falls_back_to_ref_title(ref: ArticleRef, cfg) -> None:
    titled_ref = ArticleRef(
        detail_url=ref.detail_url, list_url=LIST_URL, title="来自列表页的标题"
    )
    clean = clean_html("<html><body><p>没有任何标题标签</p></body></html>", titled_ref, cfg)
    assert clean.title == "来自列表页的标题"


# ---------------------------------------------------------------- 分段


def test_split_segments_single_paragraph() -> None:
    segments = split_segments("标题", "只有一个段落")
    assert segments["meta"] == "只有一个段落"
    assert segments["body"] == "只有一个段落"
    assert segments["tail"] == ""
    assert segments["raw"] == "只有一个段落"


def test_split_segments_three_or_more_paragraphs() -> None:
    text = "\n".join(["第一段", "第二段", "第三段", "第四段"])
    segments = split_segments("标题", text)
    assert segments["meta"] == "第一段 第二段"
    assert segments["body"] == "第三段"
    assert segments["tail"] == "第四段"


def test_split_segments_empty_text() -> None:
    segments = split_segments("标题", "")
    assert segments["headline"] == "标题"
    assert segments["body"] == ""
    assert segments["raw"] == ""
