"""清洗：HTML → 纯文本 → 分段（解析层第一步）。

职责边界
--------
**纯函数模块**：输入 HTML 字符串，输出 ``CleanArticle``。不读文件、不发请求、
不碰配置里的凭据。唯一从配置取的是 ``cfg.extract`` 下的归一化开关。

分段契约
--------
``CleanArticle.segments`` 的键固定取自 ``contracts.SEGMENT_KEYS``：
``headline``（标题）/ ``meta``（作者、届别等元信息）/ ``body``（正文）/
``tail``（结尾、联系方式）/ ``raw``（兜底全文）。
抽取规则按段取词，避免在整篇正文上做无差别正则。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Mapping, Optional

from bs4 import BeautifulSoup

from src.config import AppConfig
from src.contracts import SEGMENT_KEYS, ArticleRef, CleanArticle

NOISE_TAGS: tuple = ("script", "style", "noscript", "iframe", "form", "svg")

CONTAINER_SELECTORS: tuple = (
    # 微信公众号文章正文——实测站外通知大量指向 mp.weixin.qq.com，
    # 正文在 <div id="js_content" class="rich_media_content"> 里；
    # 不加这个选择器就会退回整页，把菜单/阅读器提示等噪声一起当成正文。
    "div#js_content",
    "div.rich_media_content",
    # 实测本校门户的正文容器：<div id="content" class="content_all">
    "div.content_all",
    "div#content",
    "div.content",
    "div.article-content",
    "div.v_news_content",
    "div.article",
    "article",
    "div.main",
)

TITLE_SELECTORS: tuple = (
    # 微信文章的标题在 <h1 id="activity-name"> / .rich_media_title 里
    "h1#activity-name",
    "h1.rich_media_title",
    "h1",
    "h2.title",
    "div.title",
    ".article-title",
    "title",
)

# 中英文标点语义不同：这些全角标点在转半角时保留原样
KEEP_FULLWIDTH_PUNCTUATION = set("，。？！；：、（）【】“”‘’《》")



def clean_html(html: str, ref: ArticleRef, cfg: AppConfig) -> CleanArticle:
    """主入口：HTML 原文 → ``CleanArticle``。

    要求：
      * 去掉 script/style/注释等噪声，保留段落换行；
      * 全角字母数字与标点转半角，连续空白折叠为单个空格；
      * 标题单独抽出填入 ``title``，同时进入 ``segments["headline"]``；
      * 正文分不出段落时，``segments["body"]`` 与 ``segments["raw"]`` 相同即可。
    """
    soup = _make_soup(html)
    _strip_noise(soup)

    title = _extract_title(soup) or str(ref.title or "")
    container = _find_container(soup)
    text = _text_of(container)
    segments = split_segments(title, text)
    return CleanArticle(ref=ref, title=title, text=text, segments=segments)


def strip_tags(html: str) -> str:
    """去标签：返回可见文本，保留块级元素的换行语义。"""
    soup = _make_soup(html)
    _strip_noise(soup)
    return _text_of(soup.body or soup)


def normalize_text(text: str) -> str:
    """文本归一化（纯函数）：全角转半角、去零宽字符、折叠空白、去首尾空格。

    注意：**不做**缺失值判定（那是 ``contracts.normalize_missing`` 的职责），
    也不得把空文本变成「未知」。
    """
    if text is None:
        return ""
    value = to_halfwidth(str(text))
    value = value.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{2,}", "\n", value)
    return value.strip()


def to_halfwidth(text: str) -> str:
    """全角字符转半角（纯函数），保留中文标点语义。"""
    if not text:
        return ""
    converted: List[str] = []
    for char in text:
        code = ord(char)
        if char in KEEP_FULLWIDTH_PUNCTUATION:
            converted.append(char)
        elif code == 0x3000:
            converted.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            converted.append(chr(code - 0xFEE0))
        else:
            converted.append(char)
    return "".join(converted)


def split_segments(title: str, text: str) -> Mapping[str, str]:
    """把标题与正文切成 ``SEGMENT_KEYS`` 定义的分段字典（纯函数）。

    切分口径（固定，便于单测）：
      * 段落数 ≤ 2 时：首段进 ``meta``，其余全部进 ``body``；
      * 段落数 ≥ 3 时：前 2 段进 ``meta``，末 1 段进 ``tail``，中间进 ``body``；
      * ``raw`` 始终是完整正文，任何分段为空都不会导致信息丢失。
    """
    paragraphs = [line.strip() for line in (text or "").split("\n") if line.strip()]
    segments: Dict[str, str] = {key: "" for key in SEGMENT_KEYS}
    segments["headline"] = title or ""
    segments["raw"] = text or ""

    if not paragraphs:
        segments["body"] = text or ""
        return segments

    if len(paragraphs) <= 2:
        segments["meta"] = paragraphs[0]
        body = paragraphs if len(paragraphs) == 1 else paragraphs
        segments["body"] = " ".join(body)
    else:
        segments["meta"] = " ".join(paragraphs[:2])
        segments["tail"] = paragraphs[-1]
        segments["body"] = " ".join(paragraphs[2:-1]) or " ".join(paragraphs)
    return segments


# --------------------------------------------------------------------------
# 内部工具（不对外暴露，均无副作用）
# --------------------------------------------------------------------------


def _make_soup(html: str) -> BeautifulSoup:
    """构造解析树；lxml 不可用时自动退回标准库解析器。"""
    source = html or ""
    try:
        return BeautifulSoup(source, "lxml")
    except Exception:  # pragma: no cover - 解析后端缺失时降级
        return BeautifulSoup(source, "html.parser")


def _strip_noise(soup: BeautifulSoup) -> None:
    """去掉脚本、样式、表单等噪声节点与注释。"""
    for node in soup.find_all(list(NOISE_TAGS)):
        node.decompose()
    for comment in soup.find_all(string=lambda value: isinstance(value, str) and value.strip().startswith("<!--")):
        comment.extract()


def _extract_title(soup: BeautifulSoup) -> str:
    """按「h1 → 常见标题类 → <title>」优先级抽标题。"""
    for selector in TITLE_SELECTORS:
        node = soup.select_one(selector)
        if node is None:
            continue
        value = normalize_text(node.get_text(" ", strip=True))
        if value:
            return value
    return ""


def _find_container(soup: BeautifulSoup):
    """定位正文容器：命中常见选择器就用它，否则退回 <body>。"""
    for selector in CONTAINER_SELECTORS:
        node = soup.select_one(selector)
        if node is not None:
            return node
    return soup.body or soup


def _text_of(node) -> str:
    """取出可见文本：块级元素之间保留换行，再做归一化。"""
    if node is None:
        return ""
    return normalize_text(node.get_text("\n", strip=True))
