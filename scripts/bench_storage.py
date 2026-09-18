#!/usr/bin/env python3
"""存储方案基准测试：文件直读 / 内容寻址缓存 / SQLite。

用途：为 docs/storage-decision.md 的结论提供可复现的实测数据。
只测底层操作吞吐（读文件、解析 HTML、读图片、哈希、读写 SQLite），
不测尚未实现的业务代码，避免拿估算冒充实测。

用法::

    python3 scripts/bench_storage.py
    python3 scripts/bench_storage.py --articles 1000 --images 2000
    python3 scripts/bench_storage.py --workdir /tmp/edp_bench --repeat 5

依赖：beautifulsoup4 + lxml（requirements.txt 中的必选依赖）。
样本全部写在 --workdir（默认 /tmp/edp_bench），不会污染项目 data/ 目录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Callable, List, Tuple

CHARS = (
    "计算机学院软件工程专业本科毕业生签约北京科技有限公司算法工程师岗位"
    "我是二零二五届大四参加秋招最终入职杭州某互联网公司从事数据分析工作"
    "在校期间担任学生会干部获得国家奖学金积累了丰富的项目实践经历"
)

DEFAULT_HTML_KB = 40
DEFAULT_IMAGE_KB = 300


def build_corpus(workdir: Path, n_articles: int, n_images: int, html_kb: int, image_kb: int) -> None:
    """生成接近真实体量的样本（HTML 中文正文 / 随机字节模拟图片）。"""
    if workdir.exists():
        shutil.rmtree(workdir)
    html_dir = workdir / "html"
    img_dir = workdir / "images"
    cache_dir = workdir / "ocr_cache"
    for d in (html_dir, img_dir, cache_dir):
        d.mkdir(parents=True)

    rng = random.Random(42)
    chars_per_article = html_kb * 1024 // 3  # 中文 UTF-8 约 3 字节
    for i in range(n_articles):
        body = "".join(rng.choice(CHARS) for _ in range(chars_per_article))
        html = (
            f"<html><head><title>就业分享 {i}</title></head><body>"
            f"<div class='article'><h1>第 {i} 篇就业分享</h1><p>{body}</p></div></body></html>"
        )
        (html_dir / f"art-{i:05d}.html").write_text(html, encoding="utf-8")

    blob = os.urandom(image_kb * 1024)
    for i in range(n_images):
        (img_dir / f"img-{i:06d}.png").write_bytes(blob)


def timed(fn: Callable[[], None], repeat: int) -> Tuple[float, float]:
    """跑 repeat 次，返回 (最好一次 ms, 中位数 ms)。"""
    samples: List[float] = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return min(samples), statistics.median(samples)


def make_html_read(files: List[Path]) -> Callable[[], None]:
    def run() -> None:
        total = 0
        for path in files:
            with open(path, encoding="utf-8") as fh:
                total += len(fh.read())
        _ = total

    return run


def make_parse(files: List[Path], parser: str) -> Callable[[], None]:
    from bs4 import BeautifulSoup

    def run() -> None:
        for path in files:
            soup = BeautifulSoup(path.read_text(encoding="utf-8"), parser)
            _ = soup.get_text(" ", strip=True)

    return run


def make_read_bytes(files: List[Path]) -> Callable[[], None]:
    def run() -> None:
        for path in files:
            path.read_bytes()

    return run


def make_hash_bytes(files: List[Path]) -> Callable[[], None]:
    def run() -> None:
        for path in files:
            hashlib.sha1(path.read_bytes()).hexdigest()

    return run


def make_read_cache(cache_dir: Path, n: int) -> Callable[[], None]:
    for i in range(n):
        (cache_dir / f"img-{i:06d}.txt").write_text("识别文本" * 500, encoding="utf-8")
    files = sorted(cache_dir.iterdir())

    def run() -> None:
        for path in files:
            path.read_text(encoding="utf-8")

    return run


def make_read_manifest(workdir: Path, n: int) -> Callable[[], None]:
    manifest = workdir / "manifest.jsonl"
    with open(manifest, "w", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(
                json.dumps(
                    {"detail_url": f"https://x.edu/{i}", "html_path": f"html/art-{i:05d}.html"},
                    ensure_ascii=False,
                )
                + "\n"
            )

    def run() -> None:
        with open(manifest, encoding="utf-8") as fh:
            for line in fh:
                json.loads(line)

    return run


def make_sqlite_write(db_path: Path, n: int) -> Callable[[], None]:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE articles (article_key TEXT PRIMARY KEY, source_url TEXT UNIQUE, "
        "graduation_year TEXT, grade TEXT, degree TEXT, major TEXT, city TEXT, "
        "employer TEXT, position TEXT, evidence TEXT, extract_method TEXT)"
    )
    rows = [
        (
            f"key{i:06d}", f"https://x.edu/{i}", "2025", "大四", "本科", "软件工程",
            "北京", "某科技有限公司", "算法工程师", "证据" * 20, "hybrid",
        )
        for i in range(n)
    ]

    def run() -> None:
        conn.execute("DELETE FROM articles")
        conn.executemany("INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()

    return run


def make_sqlite_read(db_path: Path) -> Callable[[], None]:
    conn = sqlite3.connect(db_path)

    def run() -> None:
        cur = conn.execute("SELECT * FROM articles")
        columns = [c[0] for c in cur.description]
        _ = [dict(zip(columns, row)) for row in cur.fetchall()]

    return run


def main() -> int:
    parser = argparse.ArgumentParser(description="存储方案基准测试")
    parser.add_argument("--articles", type=int, default=300, help="样本文章数（默认 300）")
    parser.add_argument("--images", type=int, default=600, help="样本图片数（默认 600）")
    parser.add_argument("--html-kb", type=int, default=DEFAULT_HTML_KB, help="单篇 HTML 大小 KB")
    parser.add_argument("--image-kb", type=int, default=DEFAULT_IMAGE_KB, help="单张图片大小 KB")
    parser.add_argument("--workdir", default="/tmp/edp_bench", help="样本目录（默认 /tmp/edp_bench）")
    parser.add_argument("--repeat", type=int, default=3, help="每项测量的重复次数（取最好/中位）")
    parser.add_argument(
        "--image-ratio", type=float, default=0.7, help="图片型文章占比，用于耗时推算（默认 0.7）"
    )
    parser.add_argument("--images-per-article", type=int, default=2, help="每篇图片型文章的图片数")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    print(f"生成样本：{args.articles} 篇 HTML（约 {args.html_kb}KB/篇）、"
          f"{args.images} 张图片（{args.image_kb}KB/张）→ {workdir}")
    build_corpus(workdir, args.articles, args.images, args.html_kb, args.image_kb)

    html_files = sorted((workdir / "html").iterdir())
    img_files = sorted((workdir / "images").iterdir())
    db_path = workdir / "bench.db"

    cases: List[Tuple[str, str, Callable[[], None], str]] = [
        ("A 纯读 HTML 文件", "read_html", make_html_read(html_files), f"{len(html_files)} 篇"),
        ("B 读+解析 BeautifulSoup(lxml)", "parse_lxml", make_parse(html_files, "lxml"), f"{len(html_files)} 篇"),
        ("C 读+解析 BeautifulSoup(html.parser)", "parse_html", make_parse(html_files, "html.parser"), f"{len(html_files)} 篇"),
        ("D 读图片字节（不解析）", "read_img", make_read_bytes(img_files), f"{len(img_files)} 张"),
        ("E 读图片 + sha1 哈希", "hash_img", make_hash_bytes(img_files), f"{len(img_files)} 张"),
        ("F 读 OCR 缓存文本", "read_cache", make_read_cache(workdir / "ocr_cache", args.images), f"{len(img_files)} 个"),
        ("G 读 manifest.jsonl", "read_manifest", make_read_manifest(workdir, args.articles), f"{len(html_files)} 行"),
        ("H SQLite 批量写", "sqlite_write", make_sqlite_write(db_path, args.articles), f"{len(html_files)} 行"),
        ("I SQLite 全量读", "sqlite_read", make_sqlite_read(db_path), f"{len(html_files)} 行"),
    ]

    print()
    header = f"{'操作':<42}{'最好(ms)':>11}{'中位(ms)':>11}{'规模':>12}"
    print(header)
    print("-" * len(header))

    results: dict = {}
    for label, key, fn, scope in cases:
        best, med = timed(fn, args.repeat)
        results[key] = (best, med)
        print(f"{label:<42}{best:>11.1f}{med:>11.1f}{scope:>12}")

    n_articles, n_images = len(html_files), len(img_files)
    print()
    print(f"SQLite 文件大小：{db_path.stat().st_size / 1024:.1f} KB")
    print(f"HTML 归档总大小：{sum(p.stat().st_size for p in html_files) / 1024 / 1024:.1f} MB")
    print(f"图片归档总大小：{sum(p.stat().st_size for p in img_files) / 1024 / 1024:.1f} MB")

    parse_ms = results["parse_lxml"][0]
    img_ms = results["hash_img"][0]
    cache_ms = results["read_cache"][0]
    db_ms = results["sqlite_read"][0]

    print()
    print("=== 单条成本 ===")
    print(f"  读+解析一篇 HTML      : {parse_ms / n_articles:.3f} ms")
    print(f"  读+哈希一张图片        : {img_ms / max(n_images, 1):.3f} ms")
    print(f"  读一份 OCR 缓存        : {cache_ms / max(n_images, 1):.3f} ms")
    print(f"  读一行 SQLite          : {db_ms / n_articles:.4f} ms")

    image_articles = int(n_articles * args.image_ratio)
    ocr_images = image_articles * args.images_per_article
    print()
    print(f"=== 一轮 extract 推算（{n_articles} 篇，其中 {image_articles} 篇图片型 × "
          f"{args.images_per_article} 张 = {ocr_images} 张需 OCR）===")
    hit = parse_ms + cache_ms * ocr_images / max(n_images, 1) + db_ms
    print(f"  命中 OCR 缓存：约 {hit / 1000:.2f} s")
    print(f"  未命中缓存（需真跑 OCR）：{ocr_images} 张 × 1~3 s = "
          f"{ocr_images * 1 / 60:.0f}~{ocr_images * 3 / 60:.0f} 分钟（估算，见 storage-decision.md）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
