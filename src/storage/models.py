"""数据模型：表结构、行映射（纯数据，无第三方依赖）。

设计选择
--------
本项目用**标准库 sqlite3** 而非 ORM，避免为单表存储引入额外依赖；
因此本模块只提供「列定义 + DDL + 行 ↔ 记录映射」三件事，全部是纯函数，
保证存储层可以在没有网络、没有 ORM 的环境里被完整测试。

表结构契约
----------
* 表名固定 ``articles``；
* 列 = ``cfg.columns``（来自 config/fields.yaml，顺序一致），即导出 CSV/XLSX 的表头；
* **唯一键 = source_url**：同一篇文章重复入库是更新（UPSERT），不是新增；
* ``article_key`` 若在 fields.yaml 中声明为列，则取 ``JobRecord.article_key``，
  与归档文件名同源，便于「数据库记录 ↔ 归档文件」双向定位。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence, Tuple

from src.contracts import CORE_FIELDS, MISSING, JobRecord, normalize_missing

TABLE_NAME = "articles"

SOURCE_URL_COLUMN = "source_url"
"""唯一键列名。去重、UPSERT、断点续跑都以此为准。"""

SQLITE_TYPE_BY_FIELD_TYPE: Mapping[str, str] = {
    "str": "TEXT",
    "date": "TEXT",
    "int": "INTEGER",
    "float": "REAL",
    "list": "TEXT",
}


def create_table_sql(columns: Sequence[str], field_types: Mapping[str, str] | None = None) -> str:
    """生成 ``CREATE TABLE IF NOT EXISTS`` 语句（列序与入参一致，source_url 唯一）。"""
    if not columns:
        raise ValueError("create_table_sql: columns 不能为空")
    if SOURCE_URL_COLUMN not in columns:
        raise ValueError(f"create_table_sql: columns 必须包含唯一键列 {SOURCE_URL_COLUMN}")

    specs = field_types or {}
    definitions = []
    for column in columns:
        sql_type = SQLITE_TYPE_BY_FIELD_TYPE.get(str(specs.get(column, "str")), "TEXT")
        if column == SOURCE_URL_COLUMN:
            definitions.append(f'  "{column}" {sql_type} NOT NULL UNIQUE')
        else:
            definitions.append(f'  "{column}" {sql_type}')
    body = ",\n".join(definitions)
    return f'CREATE TABLE IF NOT EXISTS "{TABLE_NAME}" (\n{body}\n)'



def create_index_sql() -> Tuple[str, ...]:
    """返回需要创建的索引语句（至少：source_url 唯一索引、crawl_time 普通索引）。"""
    return (
        f'CREATE INDEX IF NOT EXISTS "idx_{TABLE_NAME}_source_url" '
        f'ON "{TABLE_NAME}" ("{SOURCE_URL_COLUMN}")',
        f'CREATE INDEX IF NOT EXISTS "idx_{TABLE_NAME}_crawl_time" '
        f'ON "{TABLE_NAME}" ("crawl_time")',
    )


def upsert_sql(columns: Sequence[str]) -> str:
    """生成 UPSERT 语句：``INSERT ... ON CONFLICT(source_url) DO UPDATE SET ...``。

    必须保证「同 source_url 重复写入 = 更新」，这是幂等入库的契约。
    """
    if SOURCE_URL_COLUMN not in columns:
        raise ValueError(f"upsert_sql: columns 必须包含唯一键列 {SOURCE_URL_COLUMN}")
    placeholders = ", ".join("?" for _ in columns)
    target = ", ".join(f'"{column}"' for column in columns)
    updates = ", ".join(
        f'"{column}" = excluded."{column}"' for column in columns if column != SOURCE_URL_COLUMN
    )
    conflict = f'ON CONFLICT("{SOURCE_URL_COLUMN}") DO NOTHING' if not updates else (
        f'ON CONFLICT("{SOURCE_URL_COLUMN}") DO UPDATE SET {updates}'
    )
    return f'INSERT INTO "{TABLE_NAME}" ({target}) VALUES ({placeholders}) {conflict}'



def record_to_params(record: JobRecord, columns: Sequence[str]) -> Tuple[Any, ...]:
    """把记录按列序转成 sqlite 参数元组（与 ``JobRecord.to_row`` 口径一致）。"""
    row = record.to_row(columns)
    return tuple(row.get(column, "") for column in columns)


def row_to_record(row: Mapping[str, Any], columns: Sequence[str]) -> JobRecord:
    """把数据库行还原成 ``JobRecord``（缺失值为「未知」，未知列忽略）。"""
    if hasattr(row, "keys"):
        available = {column: row[column] for column in row.keys()}  # type: ignore[attr-defined]
    else:
        available = dict(row)

    kwargs: dict = {}
    for field_name in CORE_FIELDS:
        if field_name in available:
            kwargs[field_name] = normalize_missing(available[field_name])

    textual = (
        "article_title",
        "publish_date",
        "source_url",
        "list_url",
        "raw_html_path",
        "crawl_time",
        "extract_method",
        "review_status",
        "content_kind",
        "evidence_text",
    )
    for field_name in textual:
        if field_name in available:
            value = available[field_name]
            kwargs[field_name] = "" if value is None else str(value)

    # 数据库列名取自 fields.yaml（evidence），DTO 字段名是 evidence_text：
    # 这层映射必须显式写出来，否则从库里读回的记录会丢失「命中依据」。
    if "evidence" in available:
        value = available["evidence"]
        kwargs["evidence_text"] = "" if value is None else str(value)
    elif "evidence_text" in available:
        kwargs["evidence_text"] = str(available["evidence_text"] or "")

    return JobRecord(**kwargs)
