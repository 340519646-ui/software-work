"""编排：三段式管线的唯一实现。实现 ``contracts.StageRunner``。

阶段语义（命令与阶段一一对应，见 README 第六节）
--------------------------------------------
``fetch``
    ① 校验登录态（``crawler.login_check.require_login``）
    ② 建库建表、取已入库 URL 作为幂等键集合
    ③ 翻页 → 抓详情 → 归档 HTML → 追加清单 ``data/raw/manifest.jsonl``
    产出：归档文件 + 清单；**不写业务表**（解析与入库分开，便于规则迭代时离线重跑）

``extract``
    ① 读清单（**不联网**）→ 逐条离线回放归档 HTML
    ② 清洗 → 一级规则 → 二级 LLM 兜底 → 校验 → 去重
    ③ UPSERT 入库；仍为「未知」的记录同时写入待人工清单
    产出：``articles`` 表 + ``manual_review.csv``

``export``
    全量重建 ``jobs.csv`` / ``jobs.xlsx``，输出统计摘要
    产出：两个报表 + 统计 JSON

横切约定
--------
* **幂等**：三个阶段都可重复执行；fetch 靠 source_url 跳过已归档，extract 靠
  source_url UPSERT，export 全量重建。
* **断点续跑**：失败不中断整批，逐条记 ``StageResult.diagnostics``，最后统一汇总。
* **错误隔离**：模块边界只允许 ``PipelineError``；本层捕获后记入
  ``StageResult.errors`` 与 ``errors`` 计数，默认继续处理下一条。
* **依赖注入**：本层只通过各层的 ``build_*`` 工厂获取实现，便于测试替换。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from src.config import AppConfig
from src.contracts import (
    ArticleExtractor,
    Diagnostic,
    ErrorCode,
    ExtractMethod,
    PageFetcher,
    PipelineError,
    RecordRepository,
    RecordValidator,
    Severity,
    Stage,
    StageResult,
    StageRunner,
    StorageError,
    manifest_relpath,
)
from src.crawler.crawler import build_fetcher, fetch_all, load_archived, load_manifest
from src.crawler.login_check import require_login
from src.parser.extractor import build_extractor
from src.storage.database import build_repository
from src.validation.statistics import render_console, summarize, write_summary
from src.validation.validator import build_validator, deduplicate

logger = logging.getLogger(__name__)

SUMMARY_RELPATH = "data/processed/summary.json"
"""导出阶段的统计摘要落盘位置（供 docs/experiment.md 引用）。"""


class Pipeline:
    """三段式管线编排器。"""

    def __init__(
        self,
        cfg: AppConfig,
        fetcher_factory: Optional[Callable[[AppConfig], PageFetcher]] = None,
        extractor_factory: Optional[Callable[[AppConfig], ArticleExtractor]] = None,
        validator_factory: Optional[Callable[[AppConfig], RecordValidator]] = None,
        repository_factory: Optional[Callable[[AppConfig], RecordRepository]] = None,
        login_checker: Optional[Callable[[AppConfig], None]] = None,
    ) -> None:
        self._cfg = cfg
        self._fetcher_factory = fetcher_factory
        self._extractor_factory = extractor_factory
        self._validator_factory = validator_factory
        self._repository_factory = repository_factory
        self._login_checker = login_checker
        self._last_summary: Dict[str, Any] = {}

    @property
    def last_summary(self) -> Dict[str, Any]:
        """最近一次 export 阶段的统计摘要（CLI 用它打印可读报表）。"""
        return dict(self._last_summary)

    # ---------- 对外唯一入口 ----------

    def run(self, stage: Stage) -> StageResult:
        """按阶段分发；未知阶段抛 ``ValueError``（由 CLI 层转为退出码 2）。"""
        if stage is Stage.FETCH:
            return self.run_fetch()
        if stage is Stage.EXTRACT:
            return self.run_extract()
        if stage is Stage.EXPORT:
            return self.run_export()
        raise ValueError(f"未知阶段：{stage!r}")

    # ---------- 各阶段 ----------

    def run_fetch(self) -> StageResult:
        """采集阶段：登录门禁 → 翻页 → 抓详情 → 归档 → 写清单。"""
        cfg = self._cfg
        result = StageResult.start(Stage.FETCH)

        # ① 登录门禁：这是**阶段前置条件**，失败必须上抛给 CLI（退出码 3/4），
        #    不能降级成「阶段跑完但有错误」，否则 `--stage fetch` 会返回 1，
        #    与文档里「4 = 登录态无效」的口径不符。
        (self._login_checker or require_login)(cfg)

        repository: Optional[RecordRepository] = None
        fetcher: Optional[PageFetcher] = None
        try:
            # ② 断点续跑：已入库的 source_url 直接跳过
            repository = self._open_repository()
            existing = self._existing_source_urls(repository)
            result.counters["skipped_existing"] = len(existing)

            fetcher = (self._fetcher_factory or build_fetcher)(cfg)
            for raw in fetch_all(fetcher, cfg, skip_source_urls=existing):
                result.add("refs_found")
                if raw.ok:
                    result.add("details_fetched")
                    result.add("html_archived")
                    result.add("images_fetched", len(raw.images))
                else:
                    result.add("details_failed")
                    self._record_warning(
                        result,
                        f"详情页采集失败：{raw.ref.detail_url}（{raw.error or 'HTTP ' + str(raw.status_code)}）",
                        ErrorCode.HTTP_ERROR,
                    )
        except PipelineError as exc:
            self._record_failure(result, f"采集阶段失败：{exc}", exc.code)
        finally:
            self._close_quietly(fetcher)
            self._close_quietly(repository)

        result.artifacts.append(cfg.extract.raw_html_dir)
        result.artifacts.append(manifest_relpath(cfg.extract.raw_html_dir))
        return result.finish()

    def run_extract(self) -> StageResult:
        """解析阶段：读清单离线回放 → 两级抽取 → 校验去重 → 入库 → 待人工清单。"""
        cfg = self._cfg
        result = StageResult.start(Stage.EXTRACT)
        repository: Optional[RecordRepository] = None

        try:
            repository = self._open_repository()
            entries = load_manifest(cfg)
            if not entries:
                self._record_warning(
                    result,
                    f"采集清单为空（{manifest_relpath(cfg.extract.raw_html_dir)}）：请先执行 --stage fetch",
                    ErrorCode.HTTP_ERROR,
                )
                return result.finish()

            extractor = (self._extractor_factory or build_extractor)(cfg)
            validator = (self._validator_factory or build_validator)(cfg)

            accepted = []
            invalid = []
            for entry in entries:
                # OCR 记账只在启用 OCR 时进行：
                # 否则"未命中缓存"毫无意义，反而会把统计误导成「跑了 N 次 OCR」。
                if cfg.ocr.enabled:
                    for asset in entry.images:
                        cache = cfg.path(asset.ocr_cache_path) if asset.ocr_cache_path else None
                        result.add("ocr_cached" if (cache is not None and cache.exists()) else "ocr_computed")

                if not entry.ok:
                    self._record_warning(
                        result,
                        f"清单条目采集失败，跳过：{entry.ref.detail_url}（{entry.error}）",
                        ErrorCode.HTTP_ERROR,
                    )
                    continue

                # 离线回放：完全不联网（规则/选择器调整后可反复重跑）
                raw = load_archived(entry.ref, cfg)
                if not raw.ok:
                    self._record_warning(result, f"归档不可用：{entry.ref.detail_url}（{raw.error}）", ErrorCode.STORAGE_ERROR)
                    continue
                raw = replace(raw, images=entry.images)

                try:
                    record = extractor.extract_from_html(raw)
                except PipelineError as exc:
                    self._record_warning(result, f"解析失败：{entry.ref.detail_url}（{exc}）", exc.code)
                    continue

                result.add("records_extracted")
                for hit in record.hits.values():
                    if hit.method is ExtractMethod.RULE:
                        result.add("rule_hits")
                    elif hit.method is ExtractMethod.LLM:
                        result.add("llm_calls")
                result.add("unknown_fields", len(record.unknown_fields()))

                validation = validator.validate(record)
                result.add("validated")
                if validation.ok:
                    accepted.append(record)
                else:
                    result.add("invalid")
                    invalid.append(record)
                    self._record_warning(
                        result,
                        f"校验未通过：{record.source_url}（{'；'.join(validation.error_messages)}）",
                        ErrorCode.FIELD_INVALID,
                    )

            # 去重 → 入库 → 待人工清单（三步都幂等）
            unique = deduplicate(accepted)
            result.add("duplicates", max(0, len(accepted) - len(unique)))

            if unique:
                result.add("inserted", repository.upsert_many(unique))

            pending = deduplicate([r for r in unique if r.needs_manual_review()] + invalid)
            result.add("manual_review", len(pending))
            if pending:
                repository.write_manual_review(pending)
        except PipelineError as exc:
            self._record_failure(result, f"解析阶段失败：{exc}", exc.code)
        finally:
            self._close_quietly(repository)

        result.artifacts.extend([cfg.storage.db_path, cfg.extract.manual_review_output])
        return result.finish()

    def run_export(self) -> StageResult:
        """导出阶段：全量重建 CSV/XLSX，输出统计摘要。"""
        cfg = self._cfg
        result = StageResult.start(Stage.EXPORT)
        repository: Optional[RecordRepository] = None

        try:
            repository = self._open_repository()
            total = repository.count()
            if total == 0:
                self._record_warning(result, "结果库为空：请先执行 --stage extract", ErrorCode.STORAGE_ERROR)

            exported = repository.export_csv()
            result.add("exported_rows", exported)
            try:
                repository.export_xlsx()
            except StorageError as exc:
                # pandas/openpyxl 缺失时降级为只出 CSV，不视为阶段失败
                self._record_warning(result, f"xlsx 导出跳过：{exc}", ErrorCode.STORAGE_ERROR)

            summary = summarize(repository.fetch_all(), cfg)
            summary_path = write_summary(summary, str(cfg.path(SUMMARY_RELPATH)))
            self._last_summary = summary
            result.counters["manual_review"] = len(repository.fetch_manual_review())
            result.artifacts.extend([cfg.output.csv_path, cfg.output.xlsx_path, summary_path])
        except PipelineError as exc:
            self._record_failure(result, f"导出阶段失败：{exc}", exc.code)
        finally:
            self._close_quietly(repository)

        return result.finish()

    # ---------- 内部协作点（供实现时复用，勿在外部调用） ----------

    def _open_repository(self) -> RecordRepository:
        repository = (self._repository_factory or build_repository)(self._cfg)
        repository.init_schema()
        return repository

    def _existing_source_urls(self, repository: RecordRepository) -> List[str]:
        """断点续跑用的已入库 URL（读取失败不阻断采集，只是退化为全量重抓）。"""
        try:
            return sorted(repository.existing_source_urls())
        except PipelineError as exc:
            logger.warning("读取已入库 URL 失败，本轮将全量采集：%s", exc)
            return []

    def _record_failure(self, result: StageResult, message: str, code=None) -> None:
        """阶段级失败：记入 errors、置 ok=False（用于系统性错误）。"""
        result.fail(message, code=code or ErrorCode.INTERNAL)
        result.add("errors")

    def _record_warning(self, result: StageResult, message: str, code=None) -> None:
        """条目级问题：记入 errors 便于排查，但**不**让整个阶段失败。"""
        result.errors.append(message)
        result.note(
            Diagnostic(
                code=code or ErrorCode.INTERNAL,
                message=message,
                severity=Severity.WARNING,
            )
        )

    @staticmethod
    def _close_quietly(resource) -> None:
        if resource is None:
            return
        try:
            resource.close()
        except Exception as exc:  # pragma: no cover - 关闭失败不影响结果
            logger.warning("释放资源失败：%s", exc)


def render_summary(summary: Dict[str, Any]) -> str:
    """把统计摘要渲染成可读文本（CLI 与文档共用同一渲染实现）。"""
    return render_console(summary)


def build_pipeline(cfg: AppConfig) -> StageRunner:
    """工厂：把各层的工厂函数注入 ``Pipeline``（唯一装配点）。

    装配关系（唯一允许的层间依赖，与 docs/architecture.md 的依赖图一致）::

        crawler.build_fetcher ─┐
        parser.build_extractor ─┼─→ Pipeline ─→ contracts.StageRunner
        validation.build_validator ─┤
        storage.build_repository ─┘
    """
    return Pipeline(
        cfg,
        fetcher_factory=build_fetcher,
        extractor_factory=build_extractor,
        validator_factory=build_validator,
        repository_factory=build_repository,
    )
