"""采集调度：实现 ``contracts.PageFetcher``，并对外提供翻页/整批采集的生成器。

这是采集层对 pipeline 暴露的**唯一门面**。pipeline 不允许直接 import
``session`` / ``list_page`` / ``detail_page``，只能通过本模块的
``build_fetcher`` 拿到 ``PageFetcher``。

幂等与失败约定
--------------
* 同一 ``detail_url`` 重复采集 → 覆盖同一归档文件，不产生重复记录；
* 单篇失败不抛异常，返回 ``RawArticle.failure(...)``，由调用方统计 ``details_failed``；
* 列表页整页失败抛 ``FetchError``，由 pipeline 记入 StageResult 并继续下一页。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set
from urllib.parse import urlsplit

from src.config import AppConfig
from src.contracts import (
    ArticleRef,
    ImageAsset,
    PageFetcher,
    PipelineError,
    RawArticle,
    StorageError,
    Transport,
)
from src.crawler import detail_page, list_page, session

logger = logging.getLogger(__name__)

# 门面再导出：编排层只从本模块取采集层能力（保持「层内文件对外不可见」的约定）
from src.crawler.detail_page import (  # noqa: E402
    append_manifest,
    load_archived,
    load_manifest,
)

# 待核对（门户相关）：以下按「常见高校门户」结构实现，需按本校实际页面核对——
#   1) portal.detail_link_selector 的取值；
#   2) 分页参数名 portal.page_param 与页数范围；
#   3) 图片是否放在 __local / uploadfile 目录下（影响图标过滤规则）。


class PortalPageFetcher:
    """门户采集器：组合 Transport + 列表页 + 详情页。

    生命周期：``build_fetcher(cfg)`` → 用 with 或 finally 调 ``close()``。
    """

    def __init__(self, cfg: AppConfig, transport: Optional[Transport] = None) -> None:
        self._cfg = cfg
        self._transport = transport if transport is not None else session.build_transport(cfg)

    def fetch_list(self, page: int) -> List[ArticleRef]:
        """抓取第 ``page`` 页列表。

        两种模式（由 ``portal.mode`` 决定）：
          * ``api``  —— POST JSON 接口（实测本校门户属于这种）；
          * ``html`` —— 解析列表页 DOM。
        """
        if self._cfg.portal.mode == "api":
            return list_page.fetch_list_api(self._transport, self._cfg, page)
        return list_page.fetch_list(self._transport, self._cfg, page)

    def fetch_detail(self, ref: ArticleRef) -> RawArticle:
        """抓取单篇详情页（失败返回 failure 记录，不抛异常）。"""
        raw = detail_page.fetch_detail(self._transport, self._cfg, ref)
        if not raw.ok:
            return raw

        assets: List[ImageAsset] = []
        for url in detail_page.extract_image_urls(raw.html, ref.detail_url, self._cfg):
            asset = self.fetch_image(url, ref)
            if asset is not None:
                assets.append(asset)
        if not assets:
            return raw
        return replace(raw, images=tuple(assets))

    def fetch_image(self, url: str, ref: ArticleRef) -> Optional[ImageAsset]:
        """抓取并归档一张图片；失败返回 None（由调用方计入 images_failed）。"""
        response = self._transport.get(url, referer=ref.detail_url, binary=True)
        if not response.ok or not response.content:
            logger.warning("图片抓取失败：%s", url)
            return None

        try:
            asset = ImageAsset.from_bytes(
                response.content,
                source_url=url,
                detail_url=ref.detail_url,
                suffix=_image_suffix(url),
                images_dir=self._cfg.extract.ocr_images_dir,
                cache_dir=self._cfg.extract.ocr_cache_dir,
            )
            target = self._cfg.path(asset.image_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
        except OSError as exc:
            logger.warning("图片归档失败：%s（%s）", url, exc)
            return None
        return asset

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def __enter__(self) -> "PortalPageFetcher":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def build_fetcher(cfg: AppConfig) -> PageFetcher:
    """工厂：构造采集器并传入配置（pipeline 的唯一入口）。"""
    return PortalPageFetcher(cfg)


def iter_pages(cfg: AppConfig) -> Iterator[int]:
    """按 ``portal.page_start`` → ``portal.page_end`` 顺序产出页码（含两端）。

    分页范围的唯一来源，禁止在其他模块重复计算范围。
    """
    for page in range(int(cfg.portal.page_start), int(cfg.portal.page_end) + 1):
        yield page


AUTO_PAGE_LIMIT = 500
"""``page_end=0``（自动翻页）时的安全上限，防止接口异常导致无限翻页。"""


def _safe_fetch_list(fetcher: PageFetcher, page: int) -> "tuple[List[ArticleRef], bool]":
    """抓一页，返回 ``(refs, ok)``；``ok=False`` 表示这一页**失败**。

    失败与"这页本来就空"必须区分：接口在**会话过期**时会以 HTTP 200 返回失败外壳
    （例如 ``{"code":401,"msg":"未登录"}``），此时没有列表数组 → 抛 ``ParseError``。
    若不区分，自动翻页会把「失败」当成「已到最后一页」而静默提前结束。
    """
    try:
        return fetcher.fetch_list(page), True
    except PipelineError as exc:
        logger.warning("第 %s 页抓取失败：%s", page, exc)
        return [], False


def iter_refs(fetcher: PageFetcher, cfg: AppConfig) -> Iterator[ArticleRef]:
    """翻页产出 ``ArticleRef``，并做**跨页去重**（按 ``detail_url``，保持首次出现顺序）。

    两种翻页方式：

    * 显式范围：``page_start``..``page_end``；
    * **自动翻页**（``page_end = 0``）：一直翻到某页返回 0 条为止——
      实测门户的通知共 1040 条、每页 15 条，用它省去手工维护末页。
    """
    seen: Set[str] = set()

    def emit(refs: "List[ArticleRef]") -> Iterator[ArticleRef]:
        for ref in refs:
            if ref.detail_url in seen:
                continue
            seen.add(ref.detail_url)
            yield ref

    if cfg.portal.page_end <= 0:
        start = int(cfg.portal.page_start)
        for page in range(start, start + AUTO_PAGE_LIMIT):
            refs, ok = _safe_fetch_list(fetcher, page)
            if not ok:
                # 失败 ≠ 结束：宁可停下来报错，也不能把「会话过期」当成「翻到最后一页」
                logger.error(
                    "第 %s 页抓取失败，自动翻页提前结束（已收集 %s 条）；"
                    "请检查登录态是否过期、或接口参数是否已变化",
                    page,
                    len(seen),
                )
                break
            if not refs:
                logger.info("第 %s 页没有数据，自动翻页结束（共 %s 页）", page, page - start)
                break
            yield from emit(refs)
        return

    for page in iter_pages(cfg):
        refs, _ok = _safe_fetch_list(fetcher, page)
        yield from emit(refs)


def fetch_all(
    fetcher: PageFetcher,
    cfg: AppConfig,
    skip_source_urls: Optional[Iterable[str]] = None,
) -> Iterator[RawArticle]:
    """整批采集：逐个产出 ``RawArticle``（成功与失败都产出，由调用方按 ``ok`` 分流）。

    ``skip_source_urls`` 是断点续跑用的幂等键集合（来自
    ``Repository.existing_source_urls()``），命中的 URL 直接跳过并计入
    ``skipped_existing``。
    """
    skip = set(skip_source_urls or ())
    for ref in iter_refs(fetcher, cfg):
        if ref.detail_url in skip:
            continue
        raw = fetcher.fetch_detail(ref)
        try:
            detail_page.append_manifest(raw, cfg)
        except StorageError as exc:
            logger.warning("写入采集清单失败：%s", exc)
        yield raw


def collect_source_urls(refs: Iterable[ArticleRef]) -> Set[str]:
    """把 ``ArticleRef`` 集合转成 detail_url 集合（纯函数，供调度与测试使用）。"""
    return {ref.detail_url for ref in refs}


def _image_suffix(url: str) -> str:
    """从 URL 推断图片扩展名（未知或不在白名单内时按 .png 归档）。"""
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in detail_page.IMAGE_EXTENSIONS else ".png"
