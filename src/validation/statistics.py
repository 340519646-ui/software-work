"""统计与抽检：字段完整率、抽取方式分布、可复现的人工抽检抽样。

职责边界
--------
纯统计：输入 ``Iterable[JobRecord]``，输出普通字典/文本，**不写数据库**、
不发请求。写文件仅限 ``write_summary``（把结果落到 ``docs/`` 或 ``data/``）。

抽样契约
--------
``sample_for_review`` 必须可复现：用 ``cfg.sampling.seed`` 固定随机序列，
同一批数据与同一 seed 必须抽到同一批样本——否则抽检结论无法复核。
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from src.config import AppConfig
from src.contracts import (
    CORE_FIELDS,
    CORE_FIELD_LABELS,
    MISSING,
    ContentKind,
    ExtractMethod,
    JobRecord,
)


def summarize(records: Iterable[JobRecord], cfg: AppConfig) -> Dict[str, Any]:
    """汇总统计（写入 docs/experiment.md 的数据基础）。

    至少包含：
      * ``total``：记录总数；
      * ``completeness``：七项字段各自的非「未知」占比（键为字段名）；
      * ``complete_records`` / ``manual_review_count``；
      * ``extract_method``：rule / llm / hybrid / none 的条数分布；
      * ``field_source``：每个字段由 rule 命中、由 llm 命中的条数；
      * ``review_rate`` 与 ``sample_size``。
    """
    records = list(records)
    total = len(records)
    sample = sample_for_review(records, cfg)

    summary: Dict[str, Any] = {
        "total": total,
        "completeness": field_completeness(records),
        "complete_records": sum(1 for record in records if record.is_complete()),
        "manual_review_count": sum(1 for record in records if record.needs_manual_review()),
        "unknown_total": sum(len(record.unknown_fields()) for record in records),
        "extract_method": method_distribution(records),
        "field_source": {field: {"rule": 0, "llm": 0} for field in CORE_FIELDS},
        "content_kind": {kind.value: 0 for kind in ContentKind},
        "review_rate": cfg.sampling.review_rate,
        "sample_size": len(sample),
    }

    for record in records:
        kind = record.content_kind or ContentKind.HTML.value
        summary["content_kind"][kind] = summary["content_kind"].get(kind, 0) + 1

        if record.hits:
            # 精确归因：内存里的记录带着每个字段的命中方式（extract 阶段刚跑完时）
            for field in CORE_FIELDS:
                hit = record.hits.get(field)
                if hit is None:
                    continue
                if hit.method is ExtractMethod.RULE:
                    summary["field_source"][field]["rule"] += 1
                elif hit.method is ExtractMethod.LLM:
                    summary["field_source"][field]["llm"] += 1
            continue

        # 保守归因：从 DB 读回的记录不带 hits（hits 不落库），只能按整条记录的
        # extract_method 推断；hybrid 无法逐字段区分，因此不计入（口径见 docs/experiment.md）。
        method = record.extract_method
        bucket = ""
        if method == ExtractMethod.RULE.value:
            bucket = "rule"
        elif method == ExtractMethod.LLM.value:
            bucket = "llm"
        if not bucket:
            continue
        for field in CORE_FIELDS:
            if record.field_value(field) != MISSING:
                summary["field_source"][field][bucket] += 1
    return summary


def sample_for_review(records: Sequence[JobRecord], cfg: AppConfig) -> List[JobRecord]:
    """按 ``cfg.sampling.review_rate`` 随机抽样（``random.Random(cfg.sampling.seed)``）。

    至少抽 1 条（当记录非空且 review_rate > 0），最多等于记录总数。
    """
    records = list(records)
    if not records:
        return []
    rate = float(cfg.sampling.review_rate)
    if rate <= 0:
        return []
    size = min(len(records), max(1, math.ceil(len(records) * rate)))
    rng = random.Random(cfg.sampling.seed)
    return rng.sample(records, size)


def render_console(summary: Mapping[str, Any]) -> str:
    """把统计结果渲染为终端可读的多行文本（run.py export 阶段的收尾输出）。"""
    total = summary.get("total", 0)
    lines = [
        f"记录总数        : {total}",
        f"七项字段完整    : {summary.get('complete_records', 0)}/{total}",
        f"待人工复核      : {summary.get('manual_review_count', 0)}",
        f"未知字段总数    : {summary.get('unknown_total', 0)}",
        "",
        "字段完整率：",
    ]
    for field, rate in (summary.get("completeness") or {}).items():
        label = CORE_FIELD_LABELS.get(field, field)
        lines.append(f"  {label:<6}{rate:>7.1%}   ({field})")

    lines.append("")
    lines.append("抽取方式分布：")
    for name, count in (summary.get("extract_method") or {}).items():
        lines.append(f"  {name:<8}{count:>5}")

    lines.append("")
    lines.append("内容来源分布：")
    for name, count in (summary.get("content_kind") or {}).items():
        lines.append(f"  {name:<8}{count:>5}")

    lines.append("")
    lines.append(
        f"人工抽检        : {summary.get('sample_size', 0)} 条"
        f"（比例 {float(summary.get('review_rate', 0)):.0%}，种子固定可复现）"
    )
    return "\n".join(lines)


def format_completeness(record: JobRecord) -> str:
    """单条记录的完整率描述，如 ``7/7`` 或 ``5/7（缺 单位、岗位）``。"""
    missing = record.unknown_fields()
    filled = len(CORE_FIELDS) - len(missing)
    if not missing:
        return f"{filled}/{len(CORE_FIELDS)}"
    labels = "、".join(CORE_FIELD_LABELS.get(field, field) for field in missing)
    return f"{filled}/{len(CORE_FIELDS)}（缺 {labels}）"


def write_summary(summary: Mapping[str, Any], path: str) -> str:
    """把统计结果写成 JSON（便于二次处理），返回实际写入路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return str(target)


def field_completeness(records: Iterable[JobRecord]) -> Dict[str, float]:
    """七项字段的完整率（纯函数，便于单测）。"""
    records = list(records)
    if not records:
        return {field: 0.0 for field in CORE_FIELDS}
    completeness: Dict[str, float] = {}
    for field in CORE_FIELDS:
        filled = sum(1 for record in records if record.field_value(field) != MISSING)
        completeness[field] = round(filled / len(records), 4)
    return completeness


def method_distribution(records: Iterable[JobRecord]) -> Dict[str, int]:
    """抽取方式分布（纯函数）；键取自 ``ExtractMethod`` 的取值。"""
    counts: Dict[str, int] = {method.value: 0 for method in ExtractMethod}
    for record in records:
        key = record.extract_method or ExtractMethod.NONE.value
        counts[key] = counts.get(key, 0) + 1
    return counts
