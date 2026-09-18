"""编排层单测：三阶段端到端（全程离线）、幂等、CLI 退出码。

这是「extract→export 离线跑通」的**常驻回归测试**版本：
用 ``fake_site`` 假门户跑真实的 fetch → extract → export 代码路径，
所有产物落在 tmp_path，不联网、不需要凭据。
"""

from __future__ import annotations

import csv
import json

import pytest

from src.contracts import LoginError, Stage, StageRunner
from src.crawler.crawler import PortalPageFetcher
from src.pipeline import run as cli
from src.pipeline.pipeline import Pipeline, build_pipeline, render_summary

SUMMARY_RELPATH = "data/processed/summary.json"


@pytest.fixture()
def pipeline(cfg, fake_site) -> Pipeline:
    """注入了假门户与「跳过登录探测」的编排器（离线可用）。"""
    return Pipeline(
        cfg,
        fetcher_factory=lambda config: PortalPageFetcher(config, fake_site),
        login_checker=lambda config: None,
    )


# ======================================================================
# 三阶段
# ======================================================================


def test_fetch_stage(pipeline: Pipeline, cfg) -> None:
    result = pipeline.run_fetch()

    assert result.ok is True
    assert result.stage is Stage.FETCH
    assert result.counters["refs_found"] == 3, "样本 4 条链接含 1 条重复，去重后 3 条"
    assert result.counters["details_fetched"] == 3
    assert result.counters["html_archived"] == 3
    assert result.counters["images_fetched"] == 2
    assert result.counters.get("details_failed", 0) == 0, "counters 只记录发生过的计数项，故用 get"
    assert cfg.path("data/raw/manifest.jsonl").exists()
    assert "data/raw/manifest.jsonl" in result.artifacts


def test_extract_stage_is_offline(pipeline: Pipeline, cfg, fake_site) -> None:
    pipeline.run_fetch()
    requests_after_fetch = len(fake_site.requests)

    result = pipeline.run_extract()

    assert result.ok is True
    assert len(fake_site.requests) == requests_after_fetch, "extract 阶段必须完全离线（靠归档回放）"
    assert result.counters["records_extracted"] == 3
    assert result.counters["inserted"] == 3
    assert result.counters["validated"] == 3
    assert result.counters["rule_hits"] == 15, "2 篇文字型各 7 项 + 1 篇图片型 1 项"
    assert result.counters["unknown_fields"] == 6
    assert result.counters["manual_review"] == 1, "图片型文章缺 6 项 → 进待人工清单"
    assert cfg.path(cfg.extract.manual_review_output).exists()


def test_export_stage_creates_real_artifacts(pipeline: Pipeline, cfg) -> None:
    pipeline.run_fetch()
    pipeline.run_extract()

    result = pipeline.run_export()

    assert result.ok is True
    assert result.counters["exported_rows"] == 3

    csv_path = cfg.path(cfg.output.csv_path)
    with open(csv_path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3, "jobs.csv 必须非空"
    assert set(rows[0]) == set(cfg.columns), "列集合必须等于 fields.yaml 的声明"
    assert rows[0]["graduation_year"] == "2025"
    assert rows[0]["city"] == "北京"
    assert rows[0]["extract_method"] == "rule"
    assert rows[0]["evidence"]

    pytest.importorskip("pandas")
    assert cfg.path(cfg.output.xlsx_path).exists(), "xlsx 必须一并产出"

    summary = json.loads(cfg.path(SUMMARY_RELPATH).read_text(encoding="utf-8"))
    assert summary["total"] == 3
    assert summary["complete_records"] == 2
    assert summary["content_kind"]["ocr"] == 1
    assert summary["field_source"]["graduation_year"]["rule"] == 3
    assert pipeline.last_summary["total"] == 3


def test_full_chain_then_fetch_is_incremental(pipeline: Pipeline, cfg) -> None:
    pipeline.run_fetch()
    pipeline.run_extract()

    second = pipeline.run_fetch()

    assert second.ok is True
    assert second.counters["skipped_existing"] == 3, "已入库的 URL 必须被跳过"
    assert second.counters.get("details_fetched", 0) == 0, "断点续跑不应重复抓取"


# ======================================================================
# 边界与降级
# ======================================================================


def test_extract_without_manifest_warns_but_succeeds(cfg) -> None:
    pipeline = Pipeline(cfg, login_checker=lambda config: None)
    result = pipeline.run_extract()

    assert result.ok is True, "没有清单只是「没活可干」，不是失败"
    assert result.counters.get("records_extracted", 0) == 0
    assert any("采集清单为空" in message for message in result.errors)


def test_export_with_empty_database_warns(cfg) -> None:
    pipeline = Pipeline(cfg, login_checker=lambda config: None)
    result = pipeline.run_export()

    assert result.ok is True
    assert result.counters["exported_rows"] == 0
    assert any("结果库为空" in message for message in result.errors)


def test_login_gate_blocks_fetch(cfg) -> None:
    def deny(config):
        raise LoginError("未登录")

    pipeline = Pipeline(cfg, login_checker=deny)

    with pytest.raises(LoginError):
        # 登录门禁失败必须上抛，CLI 才能返回退出码 4
        pipeline.run_fetch()


def test_run_dispatch(pipeline: Pipeline) -> None:
    assert pipeline.run(Stage.EXPORT).stage is Stage.EXPORT

    with pytest.raises(ValueError):
        Stage.from_cli("transform")


def test_build_pipeline_returns_stage_runner(cfg) -> None:
    assert isinstance(build_pipeline(cfg), StageRunner)


def test_render_summary_text(pipeline: Pipeline) -> None:
    text = render_summary(pipeline.last_summary)
    assert "记录总数" in text or text == ""


# ======================================================================
# CLI 退出码
# ======================================================================


def test_cli_extract_succeeds_on_empty_then_fills(cfg) -> None:
    code = cli.main(["--stage", "extract", "--config", str(cfg.config_path), "--json"])
    assert code == cli.EXIT_OK

    code = cli.main(["--stage", "export", "--config", str(cfg.config_path), "--json"])
    assert code == cli.EXIT_OK


def test_cli_login_required_exit_code(cfg) -> None:
    """未提供凭据时，--stage fetch 必须返回「登录态无效」专用退出码。"""
    code = cli.main(["--stage", "fetch", "--config", str(cfg.config_path), "--json"])
    assert code == cli.EXIT_LOGIN_REQUIRED


def test_cli_config_error_exit_code(cfg) -> None:
    code = cli.main(["--stage", "fetch", "--config", "/nonexistent/config.yaml"])
    assert code == cli.EXIT_CONFIG_ERROR


def test_cli_rejects_unknown_stage(cfg) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--stage", "transform", "--config", str(cfg.config_path)])
    assert excinfo.value.code == cli.EXIT_USAGE


def test_cli_stage_failure_exit_code(cfg, monkeypatch) -> None:
    """阶段内部报错时，CLI 必须返回「有错误」的退出码，而不是 0。"""

    def broken_run(self, stage):  # noqa: ANN001
        from src.contracts import StageResult

        result = StageResult.start(stage)
        return result.fail("模拟阶段失败").finish()

    monkeypatch.setattr(Pipeline, "run", broken_run)
    code = cli.main(["--stage", "export", "--config", str(cfg.config_path), "--json"])
    assert code == cli.EXIT_STAGE_FAILED
