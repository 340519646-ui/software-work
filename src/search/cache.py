"""L1/L2 缓存层：进程内 LRU + SQLite 查询缓存。

三级缓存的定位
--------------
| 级 | 位置 | 命中成本 | 失效方式 |
| - | ---- | -------- | -------- |
| L1 | 进程内 ``LruCache`` | 微秒 | 随进程结束 |
| L2 | ``search_query_cache`` 表 | 毫秒 | **数据版本号**变化即失效 + TTL |
| L3 | ``articles`` + FTS5 索引 | 毫秒 | —— |

为什么用「版本号」而不是「触发器标脏」
------------------------------------
把**数据版本号**编进缓存键（``index_version``），任何写入递增版本号后，
旧键自然不再被查询命中——无需清表、无脏读、不怕并发写。
相比之下"写完再回头标记哪些缓存行脏了"既慢又容易漏。
代价是旧行会暂时占空间，由 ``prune`` 按 LRU + TTL 回收。

缓存里存什么
------------
只存**渲染所需的 JSON**（与 API 应答同构），不存 ``JobRecord`` 对象：
一是入库记录本就能从 L3 重查，二是这样缓存层与存储层解耦、不必做行映射。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from src.contracts import (
    SearchHit,
    SearchQuery,
    SearchResult,
    SearchSuggestion,
    StorageError,
)

CACHE_TABLE = "search_query_cache"


class LruCache:
    """L1：线程安全的定容 LRU（微秒级命中）。

    只缓存**已完成**的检索结果。容量很小（默认 128 条）是刻意的：
    它只是挡住"同一秒内重复请求"，真正的持久化交给 L2。
    """

    def __init__(self, capacity: int = 128) -> None:
        self._capacity = max(1, int(capacity))
        self._data: "OrderedDict[str, SearchResult]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[SearchResult]:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return self._data[key]
            self.misses += 1
            return None

    def put(self, key: str, value: SearchResult) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


def cache_key(query: SearchQuery, index_version: int) -> str:
    """缓存键 = 规范化查询 + 数据版本号。

    版本号进键 ⇒ 数据一变，旧键永不再命中，等效"自动失效"。
    """
    raw = f"{query.normalized_key()}|v{int(index_version)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class QueryCache:
    """L2：SQLite 查询缓存（跨进程存活，毫秒级命中）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        ttl_seconds: int = 300,
        max_rows: int = 2000,
    ) -> None:
        self._conn = conn
        self._ttl = max(0, int(ttl_seconds))
        self._max_rows = max(1, int(max_rows))

    def init_schema(self) -> None:
        self._conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{CACHE_TABLE}" ('
            f'  "cache_key" TEXT PRIMARY KEY,'
            f'  "query_text" TEXT NOT NULL,'
            f'  "index_version" INTEGER NOT NULL,'
            f'  "payload" TEXT NOT NULL,'
            f'  "created_at" REAL NOT NULL,'
            f'  "last_hit_at" REAL NOT NULL)'
        )
        self._conn.execute(
            f'CREATE INDEX IF NOT EXISTS "idx_{CACHE_TABLE}_created" '
            f'ON "{CACHE_TABLE}" ("created_at")'
        )
        self._conn.commit()

    def get(self, key: str, index_version: int) -> Optional[SearchResult]:
        """取缓存；键不匹配版本、或超过 TTL 一律视为未命中。"""
        try:
            row = self._conn.execute(
                f'SELECT "payload", "index_version", "created_at" FROM "{CACHE_TABLE}" '
                f'WHERE "cache_key" = ?',
                (key,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None

        # 双保险：键里已含版本号，这里再校验一次，防止手工写库造成脏命中
        if int(row["index_version"]) != int(index_version):
            return None
        if self._ttl and (time.time() - float(row["created_at"])) > self._ttl:
            return None

        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            return None

        self._conn.execute(
            f'UPDATE "{CACHE_TABLE}" SET "last_hit_at" = ? WHERE "cache_key" = ?',
            (time.time(), key),
        )
        self._conn.commit()
        return result_from_payload(payload)

    def put(self, key: str, result: SearchResult) -> None:
        now = time.time()
        payload = json.dumps(result_to_payload(result), ensure_ascii=False)
        try:
            self._conn.execute(
                f'INSERT INTO "{CACHE_TABLE}" '
                f'("cache_key", "query_text", "index_version", "payload", "created_at", "last_hit_at") '
                f"VALUES (?, ?, ?, ?, ?, ?) "
                f'ON CONFLICT("cache_key") DO UPDATE SET '
                f'  "payload" = excluded."payload",'
                f'  "index_version" = excluded."index_version",'
                f'  "created_at" = excluded."created_at",'
                f'  "last_hit_at" = excluded."last_hit_at"',
                (
                    key,
                    result.query.raw_text or " ".join(result.query.keywords),
                    int(result.index_version),
                    payload,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            raise StorageError(f"写查询缓存失败：{exc}") from exc

    def prune(self) -> int:
        """回收过期行 + 按 LRU 裁到上限，返回删除行数。"""
        removed = 0
        if self._ttl:
            cutoff = time.time() - self._ttl
            removed += self._conn.execute(
                f'DELETE FROM "{CACHE_TABLE}" WHERE "created_at" < ?', (cutoff,)
            ).rowcount
        total = int(self._conn.execute(f'SELECT COUNT(*) FROM "{CACHE_TABLE}"').fetchone()[0])
        if total > self._max_rows:
            self._conn.execute(
                f'DELETE FROM "{CACHE_TABLE}" WHERE "cache_key" IN ('
                f'  SELECT "cache_key" FROM "{CACHE_TABLE}" '
                f'  ORDER BY "last_hit_at" ASC LIMIT ?)',
                (total - self._max_rows,),
            )
            removed += total - self._max_rows
        self._conn.commit()
        return max(0, removed)

    def invalidate_all(self) -> int:
        """清空缓存（版本号机制下通常不需要，留给运维与测试用）。"""
        removed = int(self._conn.execute(f'DELETE FROM "{CACHE_TABLE}"').rowcount)
        self._conn.commit()
        return max(0, removed)

    def count(self) -> int:
        try:
            return int(self._conn.execute(f'SELECT COUNT(*) FROM "{CACHE_TABLE}"').fetchone()[0])
        except sqlite3.OperationalError:
            return 0


# --------------------------------------------------------------------------
# 序列化（缓存与 API 共用同一结构，避免两套口径）
# --------------------------------------------------------------------------


def result_to_payload(result: SearchResult) -> Dict[str, Any]:
    """``SearchResult`` → 可 JSON 化的 dict（与 API 应答同构）。

    显式补齐「重建 DTO 所需的全部字段」，而不是依赖 ``to_json()`` 的实现细节：
    ``to_json()`` 是**前端契约**（面向页面渲染），这里是**缓存契约**（面向反序列化）。
    两者目前重合，但改前端展示字段不该悄悄破坏缓存反序列化。
    """
    payload = result.to_json()
    payload["query"] = {
        "keywords": list(result.query.keywords),
        "fields": {str(k): str(v) for k, v in result.query.fields.items()},
        "limit": int(result.query.limit),
        "offset": int(result.query.offset),
        "raw_text": result.query.raw_text,
    }
    payload.setdefault("related", list(result.related))
    return payload


def result_from_payload(payload: Dict[str, Any]) -> SearchResult:
    """dict → ``SearchResult``（从 L2 取回时重建 DTO）。"""
    raw_query = payload.get("query") or {}
    query = SearchQuery(
        keywords=tuple(raw_query.get("keywords") or ()),
        fields=dict(raw_query.get("fields") or {}),
        limit=int(raw_query.get("limit") or 20),
        offset=int(raw_query.get("offset") or 0),
        raw_text=str(raw_query.get("raw_text") or ""),
    )

    hits: List[SearchHit] = []
    for item in payload.get("hits") or []:
        hits.append(
            SearchHit(
                article_key=str(item.get("article_key") or ""),
                title=str(item.get("title") or ""),
                source_url=str(item.get("source_url") or ""),
                raw_html_path=str(item.get("raw_html_path") or ""),
                publish_date=str(item.get("publish_date") or ""),
                score=float(item.get("score") or 0.0),
                snippet=str(item.get("snippet") or ""),
                highlights=tuple(item.get("highlights") or ()),
                fields=dict(item.get("fields") or {}),
            )
        )

    suggestion = None
    raw_suggestion = payload.get("suggestion")
    if isinstance(raw_suggestion, dict):
        suggestion = SearchSuggestion(
            reason=str(raw_suggestion.get("reason") or ""),
            search_value=str(raw_suggestion.get("search_value") or ""),
            estimated_requests=int(raw_suggestion.get("estimated_requests") or 0),
            estimated_seconds=float(raw_suggestion.get("estimated_seconds") or 0.0),
            allowed=bool(raw_suggestion.get("allowed")),
            blocked_reason=str(raw_suggestion.get("blocked_reason") or ""),
        )

    return SearchResult(
        query=query,
        hits=tuple(hits),
        total=int(payload.get("total") or 0),
        index_version=int(payload.get("index_version") or 0),
        source=str(payload.get("source") or "l2"),
        elapsed_ms=float(payload.get("elapsed_ms") or 0.0),
        suggestion=suggestion,
        related=tuple(str(item) for item in (payload.get("related") or ())),
    )
