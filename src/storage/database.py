"""存储层：SQLite 读写、CSV/XLSX 导出、待人工清单。实现 ``contracts.RecordRepository``。

这是存储层对 pipeline 暴露的**唯一门面**。pipeline 不允许直接 import ``models``。

幂等约定（全部由契约测试与实现共同保证）
------------------------------------
1. ``init_schema()`` 可重复调用（``CREATE TABLE IF NOT EXISTS``）；
2. ``upsert_many()`` 以 ``source_url`` 为唯一键：重复 URL 是**更新**，不是新增，
   返回值是「写入（含更新）的行数」；
3. ``export_csv()`` / ``export_xlsx()`` 全量重建目标文件：先写临时文件再替换，
   保证中途失败不会留下半截文件；
4. ``write_manual_review()`` 只写「未知字段或校验不通过」的记录，
   列序固定 = ``cfg.columns``。

职责边界
--------
不解析 HTML、不调用 LLM、不判定业务规则；**入库前的校验由 validation 层完成**，
本层只做「写」。数据库路径全部来自 ``cfg.path(cfg.storage.db_path)``。
"""

from __future__ import annotations

import csv
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from src.config import AppConfig
from src.contracts import (
    CORE_FIELDS,
    MISSING,
    JobRecord,
    RecordRepository,
    ReviewStatus,
    StorageError,
)
from src.storage import models


class SqliteRepository:
    """基于标准库 sqlite3 的仓库实现。"""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._conn: Optional[sqlite3.Connection] = None
        self._columns: Sequence[str] = tuple(cfg.columns)
        self._field_types: Dict[str, str] = {spec.name: spec.type for spec in cfg.fields}

    # ---------- 内部工具 ----------

    def _connect(self) -> sqlite3.Connection:
        """建立（或复用）连接；目录不存在时自动创建。"""
        if self._conn is None:
            path = self._cfg.path(self._cfg.storage.db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode = WAL")
        return self._conn

    def _rows_to_records(self, rows: Iterable[sqlite3.Row]) -> List[JobRecord]:
        return [models.row_to_record(row, self._columns) for row in rows]

    @staticmethod
    def _write_atomically(target, writer) -> None:
        """先写临时文件再原子替换：中途失败不留半截文件（导出幂等的前提）。"""
        tmp = target.parent / (target.name + ".tmp")
        try:
            writer(tmp)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _manual_review_columns(self) -> List[str]:
        """待人工清单列序：主键 + 七项核心字段 + 溯源与依据。"""
        wanted = [
            "article_key",
            *CORE_FIELDS,
            "source_url",
            "content_kind",
            "extract_method",
            "evidence",
            "review_status",
        ]
        return [column for column in wanted if column in self._columns or column == "evidence"]

    # ---------- 生命周期 ----------

    def init_schema(self) -> None:
        """建库建表建索引（幂等）。父目录不存在时自动创建。"""
        conn = self._connect()
        conn.execute(models.create_table_sql(self._columns, self._field_types))
        for statement in models.create_index_sql():
            conn.execute(statement)
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SqliteRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------- 写 ----------

    def upsert_many(self, records: Iterable[JobRecord]) -> int:
        """批量 UPSERT（按 ``cfg.storage.batch_size`` 分批提交），返回写入行数。"""
        conn = self._connect()
        sql = models.upsert_sql(self._columns)
        batch_size = max(1, int(self._cfg.storage.batch_size))
        written = 0
        pending: List[tuple] = []
        for record in records:
            pending.append(models.record_to_params(record, self._columns))
            if len(pending) >= batch_size:
                conn.executemany(sql, pending)
                conn.commit()
                written += len(pending)
                pending.clear()
        if pending:
            conn.executemany(sql, pending)
            conn.commit()
            written += len(pending)
        return written

    def update_review_status(self, article_key: str, status: str) -> int:
        """人工复核回写：按 ``article_key`` 更新 ``review_status`` 列，返回受影响行数。"""
        allowed = {status.value for status in ReviewStatus}
        if status not in allowed:
            raise StorageError(f"review_status 非法取值 {status!r}，合法取值：{sorted(allowed)}")
        conn = self._connect()
        cursor = conn.execute(
            f'UPDATE "{models.TABLE_NAME}" SET "review_status" = ? WHERE "article_key" = ?',
            (status, article_key),
        )
        conn.commit()
        return cursor.rowcount

    # ---------- 读 ----------

    def existing_source_urls(self) -> Set[str]:
        """已入库的 source_url 集合，供 fetch 阶段断点续跑跳过。"""
        conn = self._connect()
        rows = conn.execute(
            f'SELECT "{models.SOURCE_URL_COLUMN}" FROM "{models.TABLE_NAME}"'
        ).fetchall()
        return {str(row[0]) for row in rows if row[0]}

    def fetch_all(self) -> List[JobRecord]:
        """按 ``crawl_time`` 倒序取全部记录（导出与统计的数据源）。"""
        conn = self._connect()
        rows = conn.execute(
            f'SELECT * FROM "{models.TABLE_NAME}" ORDER BY "crawl_time" DESC, "source_url" ASC'
        ).fetchall()
        return self._rows_to_records(rows)

    def fetch_manual_review(self) -> List[JobRecord]:
        """取待人工清单记录：任一核心字段为「未知」的记录。"""
        conn = self._connect()
        conditions = " OR ".join(f'"{field}" IS NULL OR "{field}" = ?' for field in CORE_FIELDS)
        params = [MISSING] * len(CORE_FIELDS)
        rows = conn.execute(
            f'SELECT * FROM "{models.TABLE_NAME}" WHERE {conditions} '
            f'ORDER BY "crawl_time" DESC, "source_url" ASC',
            params,
        ).fetchall()
        return self._rows_to_records(rows)

    def count(self) -> int:
        conn = self._connect()
        return int(conn.execute(f'SELECT COUNT(*) FROM "{models.TABLE_NAME}"').fetchone()[0])

    # ---------- 导出 ----------

    def export_csv(self) -> int:
        """导出 ``cfg.output.csv_path``（UTF-8 with BOM，便于 Excel 直接打开），返回行数。"""
        records = self.fetch_all()
        target = self._cfg.path(self._cfg.output.csv_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        columns = list(self._columns)

        def write(path) -> None:
            # utf-8-sig：Excel 直接双击打开不乱码
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                for record in records:
                    writer.writerow({column: record.to_row(columns)[column] for column in columns})

        self._write_atomically(target, write)
        return len(records)

    def export_xlsx(self) -> int:
        """导出 ``cfg.output.xlsx_path``（用 pandas/openpyxl），返回行数。"""
        try:
            import pandas as pd  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的可读提示
            raise StorageError(
                "导出 xlsx 需要 pandas 与 openpyxl，请执行 pip install -r requirements.txt"
            ) from exc

        records = self.fetch_all()
        columns = list(self._columns)
        frame = pd.DataFrame([record.to_row(columns) for record in records], columns=columns)
        target = self._cfg.path(self._cfg.output.xlsx_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomically(target, lambda path: frame.to_excel(path, index=False, sheet_name="jobs"))
        return len(records)

    def write_manual_review(self, records: Iterable[JobRecord]) -> int:
        """写出待人工清单（默认 ``cfg.extract.manual_review_output``），返回行数。

        清单必须包含：article_key、七项字段、source_url、evidence、review_status，
        便于人工逐条打开原文核对后回填。
        """
        records = list(records)
        columns = self._manual_review_columns()
        target = self._cfg.path(self._cfg.extract.manual_review_output)
        target.parent.mkdir(parents=True, exist_ok=True)

        def write(path) -> None:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                for record in records:
                    writer.writerow({column: record.to_row(columns)[column] for column in columns})

        self._write_atomically(target, write)
        return len(records)


def build_repository(cfg: AppConfig) -> RecordRepository:
    """工厂：pipeline 的唯一入口，返回 ``SqliteRepository(cfg)``。"""
    return SqliteRepository(cfg)
