#!/usr/bin/env python3
"""生成实验成果看板：单文件 HTML，双击即可查看（无外部依赖、不联网）。

用法::

    python3 scripts/make_report.py                # 默认读 data/ 下的产物
    python3 scripts/make_report.py --open          # 生成后尝试用系统默认浏览器打开

看点：
  * 顶部概览：记录数、归档数、图片数、站外条数、字段完整率；
  * 字段命中表：七项各自抽到多少条；
  * 记录表格：每条通知的七项结果 + 命中依据（evidence）+ 原文链接 + 本地归档链接；
  * 待人工清单：需要人工复核的条目单独列出。

数据来源（全部是流水线自己的产物）：
  * ``data/processed/jobs.csv``        结构化结果
  * ``data/raw/manifest.jsonl``        采集清单（含站外正文地址）
  * ``data/processed/summary.json``    统计摘要（若有）
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.config import load_config  # noqa: E402
from src.contracts import CORE_FIELDS, CORE_FIELD_LABELS, MISSING, article_key_of  # noqa: E402

CSV_PATH = REPO / "data/processed/jobs.csv"
MANIFEST_PATH = REPO / "data/raw/manifest.jsonl"
SUMMARY_PATH = REPO / "data/processed/summary.json"
OUTPUT_PATH = REPO / "data/processed/report.html"

CSS = """
:root { --fg:#1f2328; --muted:#6b7280; --line:#e5e7eb; --bg:#f7f8fa; --card:#fff;
        --ok:#1a7f37; --warn:#b45309; --bad:#b91c1c; --accent:#1f6feb; }
* { box-sizing: border-box; }
body { margin:0; padding:28px 22px 60px; background:var(--bg); color:var(--fg);
       font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
h1 { font-size:22px; margin:0 0 6px; }
h2 { font-size:16px; margin:30px 0 10px; padding-bottom:6px; border-bottom:1px solid var(--line); }
.sub { color:var(--muted); margin-bottom:18px; }
.cards { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:8px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:12px 16px; min-width:132px; }
.card .k { color:var(--muted); font-size:12px; }
.card .v { font-size:22px; font-weight:600; margin-top:2px; }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--line); border-radius:10px; overflow:hidden; }
th,td { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
th { background:#fafbfc; font-weight:600; font-size:13px; color:#374151; white-space:nowrap; }
tr:last-child td { border-bottom:none; }
tr:hover td { background:#fcfcfd; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.unknown { color:#c2c7cd; }
.title-cell { min-width:260px; }
.title-cell a { color:var(--fg); text-decoration:none; font-weight:600; }
.title-cell a:hover { color:var(--accent); text-decoration:underline; }
.tag { display:inline-block; padding:1px 7px; border-radius:999px; font-size:12px;
       border:1px solid var(--line); background:#f3f4f6; color:#374151; margin-right:6px; }
.tag.external { background:#fff7ed; border-color:#fed7aa; color:#9a3412; }
.tag.rule { background:#eff6ff; border-color:#bfdbfe; color:#1d4ed8; }
.tag.llm { background:#f5f3ff; border-color:#ddd6fe; color:#6d28d9; }
.tag.hybrid { background:#ecfdf5; border-color:#a7f3d0; color:#047857; }
.bar { height:8px; background:#eef1f4; border-radius:999px; overflow:hidden; min-width:90px; }
.bar > i { display:block; height:100%; background:var(--accent); }
.ev { color:var(--muted); font-size:12.5px; max-width:520px; }
.mini { font-size:12.5px; color:var(--muted); }
.toolbar { margin:12px 0; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
input[type=search] { padding:7px 10px; border:1px solid var(--line); border-radius:8px; min-width:240px; }
code { background:#f3f4f6; padding:1px 5px; border-radius:5px; font-size:12.5px; }
.note { background:#fffbeb; border:1px solid #fde68a; color:#92400e; padding:10px 14px;
        border-radius:10px; margin:10px 0; }
"""


def load_manifest() -> Dict[str, Dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return {}
    by_key: Dict[str, Dict[str, Any]] = {}
    for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        url = entry.get("detail_url")
        if url:
            by_key[article_key_of(url)] = entry
    return by_key


def load_rows() -> List[Dict[str, str]]:
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def cell(value: str) -> str:
    value = (value or "").strip()
    if not value or value == MISSING:
        return f'<span class="unknown">{MISSING}</span>'
    return esc(value)


def build_html(rows: List[Dict[str, str]], manifest: Dict[str, Dict[str, Any]]) -> str:
    total = len(rows)
    field_hits = {f: sum(1 for r in rows if (r.get(f) or "") not in ("", MISSING)) for f in CORE_FIELDS}
    complete = sum(1 for r in rows if all((r.get(f) or "") not in ("", MISSING) for f in CORE_FIELDS))
    manual = [r for r in rows if any((r.get(f) or "") in ("", MISSING) for f in CORE_FIELDS)]
    methods = {}
    kinds = {}
    external = 0
    for row in rows:
        methods[row.get("extract_method", "")] = methods.get(row.get("extract_method", ""), 0) + 1
        kinds[row.get("content_kind", "")] = kinds.get(row.get("content_kind", ""), 0) + 1
        entry = manifest.get(row.get("article_key", ""), {})
        if entry.get("content_url"):
            external += 1

    images = sum(len(entry.get("images") or []) for entry in manifest.values())
    archived = len([e for e in manifest.values() if e.get("html_path")])

    parts: List[str] = []
    parts.append("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    parts.append("<title>就业信息抽取实验 · 成果看板</title>")
    parts.append(f"<style>{CSS}</style></head><body>")
    parts.append("<h1>门户就业信息结构化抽取 · 成果看板</h1>")
    parts.append(
        f"<div class='sub'>生成时间 {esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}"
        f" · 数据源 <code>data/processed/jobs.csv</code> + <code>data/raw/manifest.jsonl</code></div>"
    )

    # 概览卡片
    parts.append("<div class='cards'>")
    for label, value in (
        ("结构化记录", total),
        ("归档原文", archived),
        ("归档图片", images),
        ("站外正文", external),
        ("七项齐备", complete),
        ("待人工复核", len(manual)),
    ):
        parts.append(f"<div class='card'><div class='k'>{esc(label)}</div><div class='v'>{value}</div></div>")
    parts.append("</div>")

    parts.append(
        "<div class='note'><b>怎么读这份看板：</b>"
        "「未知」= 规则与 LLM 都没抽到（进待人工清单）；"
        "「依据」列是该字段命中的<b>原文片段</b>——点标题可打开门户原文，"
        "点「本地归档」可打开抓取当时保存的页面副本（门户改版或删稿后仍可核对）。</div>"
    )

    # 字段命中率
    parts.append("<h2>字段命中率</h2><table><thead><tr>")
    for head in ("字段", "命中", "完整率", "分布"):
        parts.append(f"<th>{head}</th>")
    parts.append("</tr></thead><tbody>")
    for field in CORE_FIELDS:
        hit = field_hits[field]
        rate = (hit / total * 100) if total else 0.0
        parts.append(
            "<tr>"
            f"<td>{esc(CORE_FIELD_LABELS.get(field, field))} <span class='mini'>{esc(field)}</span></td>"
            f"<td class='num'>{hit}/{total}</td>"
            f"<td class='num'>{rate:.1f}%</td>"
            f"<td><div class='bar'><i style='width:{rate:.1f}%'></i></div></td>"
            "</tr>"
        )
    parts.append("</tbody></table>")

    # 抽取方式 / 内容来源
    parts.append("<h2>抽取方式与内容来源</h2><table><thead><tr><th>维度</th><th>取值</th></tr></thead><tbody>")
    parts.append("<tr><td>抽取方式</td><td>" + " ".join(
        f"<span class='tag {esc(k)}'>{esc(k or '空')}：{v}</span>" for k, v in sorted(methods.items())
    ) + "</td></tr>")
    parts.append("<tr><td>内容来源</td><td>" + " ".join(
        f"<span class='tag'>{esc(k or '空')}：{v}</span>" for k, v in sorted(kinds.items())
    ) + "</td></tr>")
    parts.append("</tbody></table>")

    # 记录明细
    parts.append("<h2>记录明细</h2>")
    parts.append(
        "<div class='toolbar'><input type='search' id='q' placeholder='按标题/单位/城市筛选…'>"
        "<label class='mini'><input type='checkbox' id='onlyHit'> 只看有命中的</label>"
        f"<span class='mini'>共 {total} 条</span></div>"
    )
    parts.append("<table id='jobs'><thead><tr>")
    parts.append("<th>#</th><th>标题 / 来源</th>")
    for field in CORE_FIELDS:
        parts.append(f"<th>{esc(CORE_FIELD_LABELS.get(field, field))}</th>")
    for head in ("方式", "依据", "溯源"):
        parts.append(f"<th>{head}</th>")
    parts.append("</tr></thead><tbody>")

    ordered = sorted(rows, key=lambda r: (-sum(1 for f in CORE_FIELDS if (r.get(f) or "") not in ("", MISSING)), r.get("publish_date", "")))
    for index, row in enumerate(ordered, 1):
        entry = manifest.get(row.get("article_key", ""), {})
        is_external = bool(entry.get("content_url"))
        hits = sum(1 for f in CORE_FIELDS if (row.get(f) or "") not in ("", MISSING))
        title = row.get("article_title") or "(无标题)"
        source = entry.get("content_url") or row.get("source_url") or ""
        archive = row.get("raw_html_path") or ""
        rel_archive = "../raw/" + archive.split("data/raw/", 1)[-1] if archive else ""

        parts.append(f"<tr data-hits='{hits}' data-text='{esc((title + ' ' + ' '.join(row.get(f,'') for f in CORE_FIELDS)).lower())}'>")
        parts.append(f"<td class='num'>{index}</td>")
        parts.append("<td class='title-cell'>")
        parts.append(f"<a href='{esc(source)}' target='_blank' rel='noreferrer'>{esc(title[:60])}</a><br>")
        parts.append(f"<span class='tag {'external' if is_external else ''}'>"
                     f"{'站外·微信/文档' if is_external else '门户内'}</span>")
        parts.append(f"<span class='mini'>{esc(row.get('publish_date') or '')}</span>")
        parts.append("</td>")
        for field in CORE_FIELDS:
            parts.append(f"<td>{cell(row.get(field, ''))}</td>")
        parts.append(f"<td><span class='tag {esc(row.get('extract_method',''))}'>{esc(row.get('extract_method',''))}</span></td>")
        parts.append(f"<td class='ev'>{esc((row.get('evidence') or '')[:220])}</td>")
        parts.append("<td class='mini'>")
        parts.append(f"<a href='{esc(row.get('source_url',''))}' target='_blank' rel='noreferrer'>门户页</a>")
        if rel_archive:
            parts.append(f" · <a href='{esc(rel_archive)}' target='_blank'>本地归档</a>")
        if is_external:
            parts.append(f" · <a href='{esc(entry.get('content_url'))}' target='_blank' rel='noreferrer'>站外正文</a>")
        parts.append("</td></tr>")
    parts.append("</tbody></table>")

    # 待人工清单
    parts.append(f"<h2>待人工复核（{len(manual)} 条）</h2>")
    if manual:
        parts.append("<table><thead><tr><th>标题</th><th>缺失字段</th><th>门户页</th></tr></thead><tbody>")
        for row in manual:
            missing = [CORE_FIELD_LABELS.get(f, f) for f in CORE_FIELDS if (row.get(f) or "") in ("", MISSING)]
            parts.append(
                "<tr>"
                f"<td class='title-cell'>{esc((row.get('article_title') or '')[:56])}</td>"
                f"<td class='mini'>{esc('、'.join(missing))}</td>"
                f"<td class='mini'><a href='{esc(row.get('source_url',''))}' target='_blank' rel='noreferrer'>打开</a></td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
        parts.append("<div class='mini' style='margin-top:8px'>"
                     "完整清单见 <code>data/processed/manual_review.csv</code>；"
                     "补齐后回填 <code>review_status</code> 列。</div>")
    else:
        parts.append("<div class='mini'>无</div>")

    parts.append("<h2>复现命令</h2><pre class='mini'>"
                 "python -m src.pipeline.run --stage fetch    # 采集（联网，≥2 秒/请求）\n"
                 "python -m src.pipeline.run --stage extract  # 解析（离线，读归档）\n"
                 "python -m src.pipeline.run --stage export   # 导出 CSV/XLSX + 统计\n"
                 "python3 scripts/make_report.py              # 重新生成本看板"
                 "</pre>")

    # 一点点 JS：筛选与只看命中
    parts.append(
        "<script>"
        "const q=document.getElementById('q'),only=document.getElementById('onlyHit');"
        "function apply(){const s=(q.value||'').trim().toLowerCase();"
        "document.querySelectorAll('#jobs tbody tr').forEach(tr=>{"
        "const hit=parseInt(tr.dataset.hits||'0',10);"
        "const okText=!s||(tr.dataset.text||'').includes(s);"
        "const okOnly=!only.checked||hit>0;"
        "tr.style.display=(okText&&okOnly)?'':'none';});}"
        "q.addEventListener('input',apply);only.addEventListener('change',apply);apply();"
        "</script>"
    )
    parts.append("</body></html>")
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成实验成果看板（单文件 HTML）")
    parser.add_argument("--out", default=str(OUTPUT_PATH), help="输出路径")
    parser.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    args = parser.parse_args()

    rows = load_rows()
    if not rows:
        print(f"[FAIL] 找不到结构化结果：{CSV_PATH}")
        print("        请先运行：python -m src.pipeline.run --stage export")
        return 1

    manifest = load_manifest()
    html_text = build_html(rows, manifest)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html_text, encoding="utf-8")

    print(f"[OK] 看板已生成：{target}（{len(html_text) / 1024:.1f} KB，{len(rows)} 条记录）")
    print(f"     Windows 打开方式：{target}")
    if args.open:
        try:
            webbrowser.open(target.as_uri())
            print("[OK] 已尝试调用默认浏览器")
        except Exception as exc:  # pragma: no cover
            print(f"[WARN] 自动打开失败：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
