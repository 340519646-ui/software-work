"""一级规则抽取单测（正则 + 词表）。

重点是三件事：**抽得准**（样本七项全中）、**不编造**（图片型样本不乱给值）、
**可解释**（每条命中都带原文证据）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.contracts import CORE_FIELDS, ArticleRef, ExtractMethod
from src.parser import rules
from src.parser.cleaner import clean_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIST_URL = "https://example.edu.cn/jobs?page=1"

EXPECTED_TEXT_ARTICLE = {
    "graduation_year": "2025",
    "grade": "大四",
    "degree": "本科",
    "major": "软件工程",
    "city": "北京",
    "employer": "某某科技有限公司",
    "position": "算法工程师",
}


@pytest.fixture()
def ref() -> ArticleRef:
    return ArticleRef(detail_url="https://example.edu.cn/info/2025/1001.htm", list_url=LIST_URL)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture()
def text_article(ref: ArticleRef, cfg):
    return clean_html(read_fixture("article_text.html"), ref, cfg)


@pytest.fixture()
def image_article(ref: ArticleRef, cfg):
    return clean_html(read_fixture("article_image.html"), ref, cfg)


# ------------------------------------------------------------------ 词表


def test_load_lexicon_reads_both_shapes(cfg) -> None:
    lexicon = rules.load_lexicon(str(cfg.path(cfg.extract.lexicon_file)))

    assert "cities" in lexicon and "北京" in lexicon["cities"]
    assert "北京市" in lexicon["cities"]["北京"], "字典形态：标准值 → 别名元组"
    assert "employer_suffix" in lexicon and "有限公司" in lexicon["employer_suffix"], (
        "列表形态：词 → 空别名元组"
    )
    assert lexicon["position_keywords"]["算法工程师"] == ()


def test_load_lexicon_tolerates_missing_file(tmp_path) -> None:
    assert rules.load_lexicon(str(tmp_path / "nope.yaml")) == {}


def test_lexicon_is_injected_into_config(cfg) -> None:
    assert cfg.lexicon, "词表应在配置阶段读好并注入，解析层不再按文章重读文件"
    assert "cities" in cfg.lexicon


# ------------------------------------------------------------------ 正则


def test_build_patterns_covers_core_fields(cfg) -> None:
    patterns = rules.build_patterns(cfg)
    for field in ("graduation_year", "grade", "degree", "employer", "position"):
        assert patterns[field], f"{field} 必须有一级正则"
        assert all(hasattr(item, "search") for item in patterns[field])


def test_match_first_returns_empty_when_no_hit() -> None:
    assert rules.match_first("完全无关的文本", ()) == ("", "")
    assert rules.match_first("", ()) == ("", "")


def test_match_first_returns_capture_and_evidence(cfg) -> None:
    patterns = rules.build_patterns(cfg)
    matched, evidence = rules.match_first("我是2025届毕业生", patterns["graduation_year"])
    assert matched == "2025"
    assert "2025届" in evidence


# ------------------------------------------------------------ 词表匹配


def test_match_lexicon_prefers_longer_alias() -> None:
    lexicon = {"北京": ("北京市", "京")}
    found = rules.match_lexicon("签约北京市朝阳区某公司", lexicon)
    assert list(found) == ["北京"]


def test_match_lexicon_prefers_specific_major() -> None:
    lexicon = {
        "计算机科学与技术": ("计算机", "计科"),
        "软件工程": ("软工",),
    }
    found = rules.match_lexicon("我是计算机科学与技术专业的", lexicon)
    assert "计算机科学与技术" in found, "长专业名不能被短别名抢走"


def test_match_lexicon_returns_empty_for_empty_input() -> None:
    assert rules.match_lexicon("", {"北京": ("北京市",)}) == {}
    assert rules.match_lexicon("北京", {}) == {}


# ------------------------------------------------------------ 一级抽取


def test_extract_by_rules_gets_all_seven_on_text_article(text_article, cfg) -> None:
    hits = rules.extract_by_rules(text_article, cfg)

    assert set(hits) == set(CORE_FIELDS), "文字型样本应当七项全中"
    for field, expected in EXPECTED_TEXT_ARTICLE.items():
        assert hits[field].value == expected, f"{field} 抽取结果不符合预期"


def test_every_hit_is_explainable(text_article, cfg) -> None:
    hits = rules.extract_by_rules(text_article, cfg)
    for field, hit in hits.items():
        assert hit.method is ExtractMethod.RULE
        assert hit.evidence.strip(), f"{field} 的命中必须带原文证据（可溯源的前提）"
        assert hit.confidence == 1.0


def test_extract_by_rules_does_not_fabricate_on_image_article(image_article, cfg) -> None:
    hits = rules.extract_by_rules(image_article, cfg)
    assert set(hits) <= {"graduation_year"}, "图片型文章正文里没有信息，绝不能凭空抽取"
    if "graduation_year" in hits:
        assert hits["graduation_year"].value == "2024"


def test_employer_strips_city_and_particle_prefix(text_article, cfg) -> None:
    hits = rules.extract_by_rules(text_article, cfg)
    assert hits["employer"].value == "某某科技有限公司"
    assert not hits["employer"].value.startswith("北京")


def test_city_not_matched_inside_organization_name(cfg) -> None:
    """实测假命中：「北京大学光华管理学院」里的「北京」不是城市。"""
    article_text = "主讲人介绍：孙志超老师，北京大学光华管理学院硕士研究生。"

    hits = rules.extract_by_rules(_clean_ref_article(article_text), cfg)

    assert "city" not in hits, "机构名里的城市字样不能被当成工作城市"


def test_city_still_matched_in_real_context(cfg) -> None:
    article = "我最终签约北京的一家科技公司，担任算法工程师。"
    hits = rules.extract_by_rules(_clean_ref_article(article), cfg)
    assert hits["city"].value == "北京"


def test_position_requires_cue_phrase(cfg) -> None:
    """实测假命中：「教师资格培训」→ 岗位=教师。没有线索词就不该产出岗位。"""
    noisy = "关于2025年下半年国家教师资格笔试培训课程的通知，请同学们按时参加。"
    hits = rules.extract_by_rules(_clean_ref_article(noisy), cfg)
    assert "position" not in hits, "无线索词时不得全文匹配岗位关键词"

    clear = "签约后担任算法工程师，负责推荐系统召回。"
    hits2 = rules.extract_by_rules(_clean_ref_article(clear), cfg)
    assert hits2["position"].value == "算法工程师"


def _clean_ref_article(text: str):
    from src.contracts import ArticleRef as _Ref
    from src.contracts import CleanArticle

    ref = _Ref(detail_url="https://example.edu.cn/a/x", list_url="https://example.edu.cn/jobs")
    return CleanArticle(ref=ref, title="", text=text, segments={"raw": text, "body": text})


def test_real_alumni_article_phrasing(cfg) -> None:
    """真实语料（站外校友分享）的表述：2001级…现任XX公司总经理。"""
    text = (
        "本次活动特邀学院2001级中国少数民族语言文学（朝鲜语言文学）专业优秀校友、"
        "现任北京满分进出口贸易有限公司总经理李香兰学姐担任“职场领航员”。"
    )
    hits = rules.extract_by_rules(_article(text), cfg)

    assert hits["graduation_year"].value == "2001", "「2001级」也是届别（原来只认「届」）"
    assert hits["employer"].value == "北京满分进出口贸易有限公司", (
        "城市是公司名的一部分时必须保留（不能被剥成「满分…公司」）"
    )
    assert hits["position"].value == "总经理", "管理层职务要在岗位词表里"


def test_city_prefix_still_stripped_when_modifier(cfg) -> None:
    """「签约北京一家科技公司」里的城市是修饰语，仍应剥掉。"""
    hits = rules.extract_by_rules(_article("最终签约北京一家科技有限公司，担任算法工程师。"), cfg)
    assert hits["city"].value == "北京"
    assert hits["employer"].value in ("科技有限公司", "一家科技有限公司")


def _article(text: str):
    from src.contracts import ArticleRef as _Ref
    from src.contracts import CleanArticle

    ref = _Ref(detail_url="https://example.edu.cn/a/real", list_url="https://example.edu.cn/jobs")
    return CleanArticle(ref=ref, title="", text=text, segments={"raw": text, "body": text})


def test_extract_by_rules_on_empty_text(ref: ArticleRef, cfg) -> None:
    from src.contracts import CleanArticle

    empty = CleanArticle(ref=ref, title="", text="", segments={})
    assert rules.extract_by_rules(empty, cfg) == {}


def test_hit_evidence_contains_context(text_article, cfg) -> None:
    hits = rules.extract_by_rules(text_article, cfg)
    assert "签约" in hits["employer"].evidence
