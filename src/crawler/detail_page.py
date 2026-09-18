"""详情页：抓取、原始 HTML 归档、离线回放。

归档契约（朔源与断点续跑的基石）
--------------------------------
* 归档路径由 ``detail_url`` 决定：``<cfg.extract.raw_html_dir>/<article_key>.html``
  （``article_key`` = sha1(detail_url) 前 16 位，见 ``contracts.raw_html_relpath``）。
  同一 URL 重复采集得到同一路径，天然幂等、天然去重。
* 文件以 UTF-8 写入，内容为**解码后的页面 HTML 原文**，不做任何清洗；
  清洗属于解析层职责，这样规则调整后可以完全离线重解析。
* 归档失败不得静默：抛 ``StorageError``，且不返回 ``RawArticle``。
"""

from __future__ import annotations

import os
from typing import Dict, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.config import AppConfig
from src.contracts import (
    ArticleRef,
    ManifestEntry,
    RawArticle,
    StorageError,
    Transport,
    manifest_relpath,
)

IMAGE_EXTENSIONS: tuple = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
IMAGE_URL_SKIP_TOKENS: tuple = ("logo", "icon", "btn_", "arrow", "bg_", "avatar", "qrcode", "weixin", "weibo")

# 待核对：图片所在目录与图标命名因门户而异，必要时按实际页面调整上面的过滤词。


def fetch_detail(transport: Transport, cfg: AppConfig, ref: ArticleRef) -> RawArticle:
    """抓取单篇详情页并归档。

    返回约定：
      * 成功 → ``RawArticle.success(...)``，``html_path`` 为归档相对路径；
      * 失败 → ``RawArticle.failure(...)``（**不抛异常**），由 pipeline 统一过滤，
        这样单篇失败不影响整批采集；
      * 仅在归档写盘失败时抛 ``StorageError``。
    """
    # 正文可能不在门户上（站外通知）：抓 fetch_url，但归档路径仍按门户 detail_url 取名，
    # 保证同一篇通知的幂等键与归档位置不随策略变化。
    response = transport.get(ref.fetch_url, referer=ref.list_url)
    if not response.ok:
        return RawArticle.failure(
            ref, response.status_code, response.error or f"HTTP {response.status_code}"
        )
    html_path = archive_html(response.text, ref, cfg)
    return RawArticle.success(ref, response.text, html_path, response.status_code)


def archive_html(html: str, ref: ArticleRef, cfg: AppConfig) -> str:
    """把页面原文写入归档目录，返回**相对项目根**的路径字符串（写入数据库的形态）。

    幂等要求：同 ``ref.detail_url`` 重复调用，结果文件路径与内容均一致（覆盖写）。
    """
    relative = ref.raw_html_path(cfg.extract.raw_html_dir)
    target = cfg.path(relative)
    tmp = target.parent / (target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(html or "", encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        raise StorageError(f"归档失败：{relative}", url=ref.detail_url, detail=str(exc)) from exc
    return relative


def load_archived(ref: ArticleRef, cfg: AppConfig) -> RawArticle:
    """离线回放：从归档文件读回 ``RawArticle``，不发起任何网络请求。

    用于「调整正则/选择器后不必重新联网」的迭代方式；文件不存在时返回
    ``RawArticle.failure(ref, 0, "归档不存在")``。
    """
    relative = ref.raw_html_path(cfg.extract.raw_html_dir)
    target = cfg.path(relative)
    if not target.exists():
        return RawArticle.failure(ref, 0, f"归档不存在：{relative}")
    try:
        html = target.read_text(encoding="utf-8")
    except OSError as exc:
        return RawArticle.failure(ref, 0, f"归档读取失败：{exc}")
    return RawArticle.success(ref, html, relative)


def append_manifest(raw: RawArticle, cfg: AppConfig) -> None:
    """把一条采集结果追加到清单（``contracts.manifest_relpath``）。

    清单是 fetch → extract 两阶段之间**唯一**的数据交换格式：
    归档文件名是 sha1 不可逆，必须靠清单才能把 URL 还原出来。
    以追加方式写入（同一 URL 重复采集时后写的条目覆盖前者的语义由
    ``load_manifest`` 按 detail_url 去重、保留最后一条来保证）。
    """
    entry = ManifestEntry.from_raw(raw)
    target = cfg.path(manifest_relpath(cfg.extract.raw_html_dir))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(entry.to_line() + "\n")
    except OSError as exc:
        raise StorageError(f"写入采集清单失败：{target.name}", url=raw.ref.detail_url, detail=str(exc)) from exc


def load_manifest(cfg: AppConfig) -> List[ManifestEntry]:
    """读取清单：跳过损坏行（并计数告警），按 detail_url 去重（保留最后一次采集）。"""
    target = cfg.path(manifest_relpath(cfg.extract.raw_html_dir))
    if not target.exists():
        return []

    entries: Dict[str, ManifestEntry] = {}
    try:
        with open(target, encoding="utf-8") as handle:
            for line in handle:
                entry = ManifestEntry.from_line(line)
                if entry is None:
                    continue
                entries[entry.ref.detail_url] = entry
    except OSError as exc:
        raise StorageError(f"读取采集清单失败：{exc}") from exc
    return list(entries.values())


def extract_image_urls(html: str, page_url: str, cfg: AppConfig, limit: int = 20) -> List[str]:
    """抽取正文里的图片 URL（图片型文章走 OCR 的入口）。

    通用处理：相对链接绝对化、去重、按文件名粗筛掉图标/二维码；
    每篇文章最多取 ``limit`` 张，避免把整站图片都拉下来。
    """
    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:  # pragma: no cover
        soup = BeautifulSoup(html or "", "html.parser")

    urls: List[str] = []
    seen = set()
    for node in soup.select("img[src]"):
        src = str(node.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(page_url, src)
        lowered = absolute.lower()
        if any(token in lowered for token in IMAGE_URL_SKIP_TOKENS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
        if len(urls) >= limit:
            break
    return urls
