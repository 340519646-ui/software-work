"""两级抽取组装：实现 ``contracts.ArticleExtractor``。

这是解析层对 pipeline 暴露的**唯一门面**。pipeline 不允许直接 import
``cleaner`` / ``rules`` / ``llm`` / ``ocr``。

抽取流程（顺序固定）
------------------
1. ``JobRecord.empty(ref, raw.html_path)`` —— 先把溯源字段填好（来源 URL、列表页
   URL、归档路径、采集时间），保证「即使一条字段都没抽到，记录也可溯源」；
2. 一级：``rules.extract_by_rules`` → ``record.apply_hits(hits)``；
3. 判定是否需要二级：``cfg.llm.enabled`` 且规则命中数 < ``cfg.extract.llm_trigger_below``；
   需要则对 ``record.unknown_fields()`` 调 ``llm.extract_by_llm``，再 ``apply_hits``；
4. 可选 OCR：``cfg.ocr.enabled`` 时对图片型文章补一次；
5. 收尾：仍为「未知」的字段保持 ``MISSING``（**不写成空串**），记录进待人工清单的
   判定由 ``JobRecord.needs_manual_review()`` 统一给出，本模块不得自行改判。

降级约定
--------
LLM/OCR 的任何异常都不得让单篇解析失败：捕获后降级为「保持未知」，
并把原因通过 ``ExtractError`` 的计数/诊断上报（由 pipeline 记录 ``llm_failed``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

from src.config import AppConfig
from src.contracts import (
    ArticleExtractor,
    CleanArticle,
    ContentKind,
    JobRecord,
    PipelineError,
    RawArticle,
)
from src.parser import cleaner, llm, ocr, rules

MIN_TEXT_LENGTH_FOR_HTML = 200
"""清洗后正文短于该长度、且文章带图片时，判定内容主要来自图片（需要 OCR）。"""



class TwoStageExtractor:
    """规则优先 + LLM 兜底的两级抽取器。"""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg

    def extract(self, raw: RawArticle, clean: CleanArticle) -> JobRecord:
        """主接口：``RawArticle`` + ``CleanArticle`` → ``JobRecord``。"""
        cfg = self._cfg
        record = JobRecord.empty(
            raw.ref,
            raw.html_path or raw.ref.raw_html_path(cfg.extract.raw_html_dir),
        )
        record.content_kind = self._resolve_content_kind(raw, clean)

        # ① 一级：规则优先
        rule_hits = rules.extract_by_rules(clean, cfg)
        record.apply_hits(rule_hits)

        # ② 二级：LLM 兜底（只补「未知」字段，且失败即降级，绝不中断整批）
        if self.should_call_llm(record, len(rule_hits)):
            missing = self.missing_fields_for_llm(record)
            try:
                record.apply_hits(llm.extract_by_llm(clean, missing, cfg))
            except PipelineError:
                pass

        # ③ 可选：图片型文章的 OCR 兜底（走一级规则，置信度封顶且强制人工复核）
        if cfg.ocr.enabled and record.unknown_fields():
            image_paths = self.image_paths(raw, cfg)
            if image_paths and ocr.is_available(cfg):
                try:
                    ocr_hits = ocr.extract_by_ocr(clean, image_paths, cfg)
                except PipelineError:
                    ocr_hits = {}
                if ocr_hits:
                    record.apply_hits(ocr_hits)
                    record.content_kind = ContentKind.OCR.value

        # ④ 收尾：仍为「未知」的字段保持 MISSING，交由校验层与待人工清单处理
        return record

    def extract_from_html(self, raw: RawArticle) -> JobRecord:
        """便捷接口：内部先 ``cleaner.clean_html`` 再 ``extract``，供 pipeline 使用。"""
        return self.extract(raw, cleaner.clean_html(raw.html, raw.ref, self._cfg))

    def should_call_llm(self, record: JobRecord, rule_hit_count: int) -> bool:
        """二级抽取触发判定（唯一实现，禁止在其他地方重复判断）。

        规则：``cfg.llm.enabled`` 为真 **且** 规则命中数 < ``cfg.extract.llm_trigger_below``
        **且** 仍存在「未知」字段。
        """
        cfg = self._cfg
        if not cfg.llm.enabled:
            return False
        if not record.unknown_fields():
            return False
        return rule_hit_count < cfg.extract.llm_trigger_below

    def missing_fields_for_llm(self, record: JobRecord) -> Sequence[str]:
        """交给 LLM 的待补字段清单（= ``record.unknown_fields()``，顺序与 CORE_FIELDS 一致）。"""
        return record.unknown_fields()

    def image_paths(self, raw: RawArticle, cfg: AppConfig) -> List[str]:
        """待 OCR 的本地图片：优先用清单登记的 ImageAsset，其次扫描归档目录。"""
        from_manifest: List[str] = []
        for asset in raw.images:
            if not asset.image_path:
                continue
            candidate = cfg.path(asset.image_path)
            if candidate.exists():
                from_manifest.append(str(candidate))
        if from_manifest:
            return from_manifest
        return ocr.collect_archived_images(raw.ref.detail_url, cfg)

    def _resolve_content_kind(self, raw: RawArticle, clean: CleanArticle) -> str:
        """判定内容来源：正文够长算 html；正文很短且带图片算 ocr（需要人工复核）。"""
        declared = str(clean.source_kind or "")
        if declared and declared != ContentKind.HTML.value:
            return declared
        if len((clean.text or "").strip()) >= MIN_TEXT_LENGTH_FOR_HTML:
            return ContentKind.HTML.value
        if raw.images:
            return ContentKind.OCR.value
        return ContentKind.HTML.value


def build_extractor(cfg: AppConfig) -> ArticleExtractor:
    """工厂：pipeline 的唯一入口，返回 ``TwoStageExtractor(cfg)``。"""
    return TwoStageExtractor(cfg)
