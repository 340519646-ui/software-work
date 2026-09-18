"""命令行入口：``python -m src.pipeline.run --stage fetch|extract|export``。

本模块是**唯一的 CLI 层**：负责参数解析、配置加载、错误到退出码的映射、
结果输出。它不包含任何业务逻辑——业务全部在 ``pipeline.Pipeline`` 内。

退出码约定
----------
======  ================================================
退出码  含义
======  ================================================
0       阶段成功（``StageResult.ok`` 为真）
1       阶段执行完成但存在错误（见输出中的 errors / diagnostics）
2       参数用法错误（argparse 默认行为）
3       配置错误（``ConfigError``，如请求间隔 < 2s、缺门户 BASE）
4       登录态无效（``LoginError``）
130     用户中断（Ctrl-C）
======  ================================================

输出约定：结束时向 stdout 打印一行 JSON 的 ``StageResult.to_dict()``，
便于外部脚本消费；同时把人类可读摘要打印到 stderr。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from typing import Dict, Optional, Sequence

from src.config import AppConfig, load_config, validate_config
from src.contracts import ConfigError, LoginError, PipelineError, Stage
from src.pipeline.pipeline import Pipeline, build_pipeline, render_summary

EXIT_OK = 0
EXIT_STAGE_FAILED = 1
EXIT_USAGE = 2
EXIT_CONFIG_ERROR = 3
EXIT_LOGIN_REQUIRED = 4
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器（接口固定，便于测试与文档同步）。

    参数：
      ``--stage``       必填，fetch / extract / export
      ``--config``      配置文件路径，默认 ``config/config.yaml``
      ``--page-start`` / ``--page-end``  仅 fetch 生效，覆盖配置中的分页范围
      ``--use-llm`` / ``--no-llm``      覆盖 ``llm.enabled``
      ``--use-ocr`` / ``--no-ocr``      覆盖 ``ocr.enabled``
      ``--from-cache``  仅 extract 生效：强制离线（不发起任何请求）
      ``--json``        只输出 JSON 结果（便于脚本管道）
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.pipeline.run",
        description="门户就业信息结构化采集与抽取流水线（三段式）",
    )
    parser.add_argument("--stage", required=True, choices=[s.value for s in Stage], help="要执行的阶段")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 config/config.yaml")
    parser.add_argument("--page-start", type=int, default=None, help="起始页码（覆盖配置）")
    parser.add_argument("--page-end", type=int, default=None, help="结束页码（覆盖配置）")
    parser.add_argument("--use-llm", dest="use_llm", action="store_true", default=None, help="强制开启 LLM 兜底")
    parser.add_argument("--no-llm", dest="use_llm", action="store_false", help="强制关闭 LLM 兜底")
    parser.add_argument("--use-ocr", dest="use_ocr", action="store_true", default=None, help="强制开启 OCR")
    parser.add_argument("--no-ocr", dest="use_ocr", action="store_false", help="强制关闭 OCR")
    parser.add_argument("--from-cache", action="store_true", help="extract 阶段强制离线，不发起请求")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    return parser


def apply_overrides(cfg: AppConfig, args: argparse.Namespace) -> AppConfig:
    """把命令行覆盖项合并进配置（保持 ``AppConfig`` 不可变，返回副本）。

    约定：
      * 没有任何覆盖参数时**原样返回** ``cfg``（不复制、不修改）；
      * 覆盖后必须重新跑 ``validate_config``——命令行不得成为绕过合规红线的后门
        （例如用参数把间隔调到 2 秒以下会直接被拒绝）。
    """
    overrides: Dict[str, object] = {}

    portal = cfg.portal
    if args.page_start is not None:
        portal = replace(portal, page_start=args.page_start)
    if args.page_end is not None:
        portal = replace(portal, page_end=args.page_end)
    if portal is not cfg.portal:
        overrides["portal"] = portal

    if args.use_llm is not None:
        overrides["llm"] = replace(cfg.llm, enabled=bool(args.use_llm))
    if args.use_ocr is not None:
        overrides["ocr"] = replace(cfg.ocr, enabled=bool(args.use_ocr))

    if not overrides:
        return cfg

    updated = replace(cfg, **overrides)
    validate_config(updated)
    return updated


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 主流程：解析参数 → 加载配置 → 跑阶段 → 输出结果 → 返回退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(config_path=args.config)
        cfg = apply_overrides(cfg, args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except NotImplementedError as exc:
        print(
            f"命令行参数覆盖尚未实现：{exc}\n"
            "可先不传覆盖参数（--page-start/--page-end/--use-llm/--use-ocr），"
            "或按 docs/architecture.md 第九节实现 apply_overrides。",
            file=sys.stderr,
        )
        return EXIT_STAGE_FAILED

    stage = Stage.from_cli(args.stage)
    try:
        pipeline: Pipeline = build_pipeline(cfg)  # type: ignore[assignment]
        result = pipeline.run(stage)
    except LoginError as exc:
        print(f"登录态无效：{exc}", file=sys.stderr)
        return EXIT_LOGIN_REQUIRED
    except NotImplementedError as exc:
        print(
            f"阶段 {stage.value} 尚未实现：{exc}\n"
            "接口与签名已冻结，实现顺序见 docs/architecture.md 第九节。",
            file=sys.stderr,
        )
        return EXIT_STAGE_FAILED
    except PipelineError as exc:
        print(f"阶段失败：{exc}", file=sys.stderr)
        return EXIT_STAGE_FAILED
    except KeyboardInterrupt:  # pragma: no cover
        print("已中断：注意按合规要求清理会话（python -m src.crawler.login_check --clear-session）", file=sys.stderr)
        return EXIT_INTERRUPTED

    print(json.dumps(result.to_dict(), ensure_ascii=False))
    if not args.json:
        print(f"阶段 {result.stage.value} 完成：ok={result.ok} counters={result.counters}", file=sys.stderr)
        summary = getattr(pipeline, "last_summary", None)
        if summary:
            print(render_summary(summary), file=sys.stderr)
        for message in result.errors:
            print(f"  - {message}", file=sys.stderr)
    return EXIT_OK if result.ok else EXIT_STAGE_FAILED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
