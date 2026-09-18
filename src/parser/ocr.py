"""OCR：图片型就业分享的兜底识别。

职责边界与红线
--------------
1. OCR 结果一律视为**待复核**：``FieldHit.confidence`` 上限为
   ``OCR_CONFIDENCE_CAP``，且其所在记录必须进入待人工清单（合规红线 5）。
2. OCR 文本随后仍走**一级规则**（``parser.rules``）抽取，因此 ``method`` 记
   ``ExtractMethod.RULE``，用 ``confidence`` 区分证据强度，不新增抽取方式枚举。
3. 未安装 pytesseract / tesseract 时不得静默失败：``is_available`` 返回 False，
   抽取层跳过 OCR 并把该记录标为「未知」进人工清单。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from src.config import AppConfig
from src.contracts import (
    CleanArticle,
    ContentKind,
    ExtractMethod,
    FieldHit,
    OcrError,
    article_key_of,
    ocr_cache_relpath,
    sha1_of_bytes,
)
from src.parser import rules

IMAGE_SUFFIXES: tuple = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.bmp")
"""归档目录里视为正文图片的扩展名。"""


OCR_CONFIDENCE_CAP = 0.6
"""OCR 命中的置信度上限：低于规则精确匹配，提示需要人工复核。"""


def is_available(cfg: AppConfig) -> bool:
    """OCR 是否可用：``cfg.ocr.enabled`` 为真且 pytesseract/tesseract 可导入可执行。"""
    if not cfg.ocr.enabled:
        return False
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    if cfg.ocr.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = cfg.ocr.tesseract_cmd
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return False
    return True


def recognize(image_path: str, cfg: AppConfig) -> str:
    """对单张图片做 OCR，返回识别文本；失败抛 ``OcrError``。"""
    if not cfg.ocr.enabled:
        raise OcrError("OCR 未启用（config.yaml 的 ocr.enabled 为 false）")
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise OcrError(
            "缺少 OCR 依赖：请安装 pytesseract 与 Pillow，并安装系统组件 tesseract-ocr",
            detail=str(exc),
        ) from exc
    if cfg.ocr.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = cfg.ocr.tesseract_cmd

    target = Path(image_path)
    if not target.exists():
        raise OcrError(f"图片不存在：{target}")
    try:
        with Image.open(target) as image:
            return pytesseract.image_to_string(image, lang=cfg.ocr.lang)
    except Exception as exc:
        raise OcrError(f"OCR 识别失败：{target.name}", detail=str(exc)) from exc


# --------------------------------------------------------------------------
# 内容寻址缓存：算一次、一直用（docs/storage-decision.md 的核心结论）
# --------------------------------------------------------------------------


def cache_path_for(image_path: str, cfg: AppConfig) -> Path:
    """由图片**内容哈希**推导缓存路径：换文件名或换页面都不影响命中。"""
    data = Path(image_path).read_bytes()
    return cfg.path(ocr_cache_relpath(sha1_of_bytes(data), cfg.extract.ocr_cache_dir))


def recognize_with_cache(image_path: str, cfg: AppConfig) -> Tuple[str, bool]:
    """带缓存的识别：返回 ``(文本, 是否命中缓存)``；命中缓存时**不启动 tesseract**。"""
    cache = cache_path_for(image_path, cfg)
    if cache.exists():
        return cache.read_text(encoding="utf-8"), True

    text = recognize(str(image_path), cfg)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text, False


def cached_image_stats(image_paths: Sequence[str], cfg: AppConfig) -> Dict[str, int]:
    """只统计缓存命中情况（不触发任何识别），供 pipeline 记账。"""
    stats = {"ocr_cached": 0, "ocr_computed": 0}
    for path in image_paths:
        try:
            cache = cache_path_for(str(path), cfg)
        except OSError:
            continue
        stats["ocr_cached" if cache.exists() else "ocr_computed"] += 1
    return stats


def collect_archived_images(detail_url: str, cfg: AppConfig) -> List[str]:
    """扫描某篇文章的图片归档目录（清单里没有图片信息时的兜底入口）。"""
    directory = cfg.path(f"{cfg.extract.ocr_images_dir.rstrip('/')}/{article_key_of(detail_url)}")
    if not directory.is_dir():
        return []
    found: List[str] = []
    for suffix in IMAGE_SUFFIXES:
        found.extend(str(path) for path in sorted(directory.glob(suffix)))
    return found


def extract_by_ocr(
    article: CleanArticle,
    image_paths: Sequence[str],
    cfg: AppConfig,
) -> Dict[str, FieldHit]:
    """对一组图片做 OCR 并把识别文本并入正文，再交给一级规则抽取。

    约定：识别文本拼接后追加到 ``article.segments['raw']`` 的副本上再匹配；
    命中的 ``FieldHit.evidence`` 前缀加 ``[OCR]`` 标记，便于人工复核时定位。
    """
    if not image_paths:
        return {}

    texts: List[str] = []
    for path in image_paths:
        try:
            text, _hit = recognize_with_cache(str(path), cfg)
        except OcrError:
            continue
        if text.strip():
            texts.append(text.strip())
    if not texts:
        return {}

    merged = "\n".join(texts)
    segments = dict(article.segments or {})
    segments["body"] = f"{segments.get('body', '')}\n{merged}".strip()
    segments["raw"] = f"{segments.get('raw', '')}\n{merged}".strip()

    augmented = replace(
        article,
        text=f"{article.text or ''}\n{merged}".strip(),
        segments=segments,
        source_kind=ContentKind.OCR.value,
    )

    marked: Dict[str, FieldHit] = {}
    for field, hit in rules.extract_by_rules(augmented, cfg).items():
        marked[field] = FieldHit(
            field_name=field,
            value=hit.value,
            method=ExtractMethod.RULE,
            evidence=f"[OCR] {hit.evidence}",
            confidence=min(hit.confidence, OCR_CONFIDENCE_CAP),
        )
    return marked
