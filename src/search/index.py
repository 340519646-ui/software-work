"""L3 索引层：SQLite **FTS5** 全文索引与检索（无新增依赖）。

为什么用 FTS5，以及为什么默认分词器是 unicode61
--------------------------------------------
项目红线之一是「不为单表存储引入额外依赖」（见 docs/architecture.md）。
SQLite 自带 FTS5，中文不必引入 jieba 之类的分词库即可检索。

分词器的选择是**实测**出来的，不是猜的：

* ``trigram``（三字符窗口）：能做子串匹配，但 FTS5 对 **长度 < 3 的查询词直接匹配不到**
  （实测 ``MATCH '"硕士"'`` 返回 0 行）。而「硕士 / 北京 / 本科」正是中文里最常见的查询词，
  于是它们全部掉进 LIKE 降级路径，**bm25 相关性排序完全失效**（score 恒为 0）。
* ``unicode61``（默认）：把 CJK 逐字切分，``MATCH '"硕士"'`` 是"相邻短语"查询，
  能正确命中并给出 bm25 分数——所有常见查询都能走 FTS。

代价是 unicode61 要求整词命中（搜「招聘」匹配不到「招聘会」），
这是子串语义；需要子串时把 ``search.fts_tokenizer`` 设回 ``trigram``（长词场景），
或依赖 LIKE 降级路径。

为什么**不用触发器**维护索引（三次踩坑换来的结论）
------------------------------------------------
最初用 ``AFTER INSERT/UPDATE/DELETE`` 触发器同步 FTS，结果连续踩了三个坑：

1. 触发器在 ``index_text`` 派生列之前创建 → 触发器体引用不存在的列，
   SQLite 报的不是"列不存在"，而是极具误导性的
   ``database disk image is malformed``；
2. ``AFTER UPDATE`` 触发器里的 FTS ``'delete'`` 命令在"批量刷派生列"时自触发，
   把倒排索引写坏，后续所有语句都报同样的 malformed；
3. 最致命的一条：为了"写完后递增版本号"，在写事务未提交时**另开一条连接**
   操作同一个 WAL 数据库，两条连接交叉维护 FTS 表 → 索引损坏。

结论：**任何写入完成后，由写方在自己的连接上调用 ``maintain()`` 重建索引**。
不依赖触发器、不另开连接，只有一条代码路径，也就没有上述全部问题。
本模块的 ``init_schema`` 还会顺手清掉历史遗留的触发器（迁移用）。
"""

from __future__ import annotations

import html
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from src.contracts import (
    CORE_FIELDS,
    MISSING,
    SearchHit,
    SearchQuery,
    SearchResult,
    StorageError,
)

FTS_TABLE = "articles_fts"
META_TABLE = "search_meta"
VERSION_KEY = "index_version"

LEGACY_TRIGGERS: Tuple[str, ...] = ("articles_fts_ai", "articles_fts_ad", "articles_fts_au")
"""历史遗留的同步触发器名。现在不再使用，但建表时会被清掉（迁移安全）。"""

MIN_TRIGRAM_CHARS = 3
"""trigram 分词器的最小匹配长度（实测：短于 3 的词 MATCH 不到任何行）。"""

MIN_UNICODE61_CHARS = 1
"""unicode61 的最小匹配长度：CJK 逐字切分，单字也是合法 token。"""

DEFAULT_TOKENIZER = "unicode61"
"""默认分词器。理由见模块文档：trigram 会让 2 字中文查询拿不到 bm25 分数。"""

FIELD_LABELS: Dict[str, str] = {
    "graduation_year": "届别",
    "grade": "年级",
    "degree": "学历",
    "major": "专业",
    "city": "城市",
    "employer": "单位",
    "position": "岗位",
}

_TOKEN_SPLIT_RE = re.compile(r"[\s,，、;；/|+·。！？!?（）()\[\]【】\"'“”‘’]+")

_STOPWORDS = {
    "的", "了", "和", "与", "及", "或", "是", "在", "有", "我", "想", "找",
    "查", "搜", "搜索", "请问", "一下", "哪些", "什么", "怎么", "如何",
}


# --------------------------------------------------------------------------
# 纯函数：分词与索引文本
# --------------------------------------------------------------------------


def tokenize(text: str) -> List[str]:
    """把用户输入切成检索词。

    按空白与中英文标点切分 → 去首尾空白 → 丢弃停用词 → 保序去重。
    **保留 2 字中文词**（如「硕士」「北京」），它们走 LIKE 降级路径仍然有效。
    """
    pieces = _TOKEN_SPLIT_RE.split(str(text or "").strip())
    tokens: List[str] = []
    seen: Set[str] = set()
    for piece in pieces:
        token = piece.strip()
        if not token or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def min_chars_for(tokenizer: str) -> int:
    """该分词器能匹配的最短查询词长度。"""
    return MIN_TRIGRAM_CHARS if str(tokenizer).strip().lower() == "trigram" else MIN_UNICODE61_CHARS


def significant_tokens(tokens: Sequence[str], tokenizer: str = DEFAULT_TOKENIZER) -> List[str]:
    """能参与 FTS 匹配的词（长度达到该分词器的下限）。"""
    floor = min_chars_for(tokenizer)
    return [str(t).strip() for t in tokens if len(str(t).strip()) >= floor]


def short_tokens(tokens: Sequence[str], tokenizer: str = DEFAULT_TOKENIZER) -> List[str]:
    """只能走 LIKE 降级路径的短词（长度低于该分词器下限）。"""
    floor = min_chars_for(tokenizer)
    return [str(t).strip() for t in tokens if 0 < len(str(t).strip()) < floor]


def to_fts_match(tokens: Sequence[str], tokenizer: str = DEFAULT_TOKENIZER) -> str:
    """把检索词拼成 FTS5 MATCH 表达式：全部词用 AND 连接，短语加引号。

    用显式 AND 而不是空格：多词查询应"都要满足"，符合「北京 硕士」的直觉。
    加引号是"相邻短语"语义——unicode61 下 ``"硕士"`` 会要求这两个 token 相邻，
    这正是用户搜「硕士」时期望的（而不是"含硕字且含士字"）。
    含引号或短于下限的词一律剔除，否则 FTS5 会直接抛语法异常。
    """
    floor = min_chars_for(tokenizer)
    parts: List[str] = []
    for token in tokens:
        cleaned = str(token).replace('"', " ").strip()
        if len(cleaned) >= floor and cleaned:
            parts.append(f'"{cleaned}"')
    return " AND ".join(parts)


def row_to_index_text(row: Mapping[str, Any]) -> str:
    """把一行记录拼成**可检索文本**，并剔除占位符「未知」。

    不剔除「未知」会让每个查询都命中全表，相关性排序随之失效。
    标题重复一次以加权（FTS5 无字段权重，靠提词频）。
    """
    parts: List[str] = []
    title = str(row.get("article_title") or "").strip()
    if title and title != MISSING:
        parts.extend([title, title])

    for name in CORE_FIELDS:
        value = str(row.get(name) or "").strip()
        if value and value != MISSING:
            parts.append(value)

    evidence = str(row.get("evidence") or "").strip()
    if evidence and evidence != MISSING:
        parts.append(evidence)

    return " ".join(parts)


def _build_index_text_sql(prefix: str, suffix: str, columns: Optional[Sequence[str]]) -> str:
    """索引文本 SQL 的唯一实现（Python 与 SQL 两路口径必须一致）。"""

    def literal(name: str) -> str:
        if columns is not None and name not in columns:
            return "''"
        value = f"NULLIF(TRIM(COALESCE({prefix}{name}{suffix}, '')), '')"
        return f"CASE WHEN {value} IS NULL OR {value} = '{MISSING}' THEN '' ELSE {value} END"

    known = list(columns) if columns is not None else ["article_title", *CORE_FIELDS, "evidence"]
    parts: List[str] = []
    if "article_title" in known:
        parts.extend([literal("article_title"), literal("article_title")])
    for name in CORE_FIELDS:
        if name in known:
            parts.append(literal(name))
    if "evidence" in known:
        parts.append(literal("evidence"))

    if not parts:
        return "''"
    return "TRIM(" + " || ' ' || ".join(parts) + ")"


# --------------------------------------------------------------------------
# 片段与高亮（纯函数）
# --------------------------------------------------------------------------


def detect_highlights(
    title: str, fields: Mapping[str, str], evidence: str, keywords: Sequence[str]
) -> List[str]:
    """返回命中依据标签（中文字段名），前端直接显示成标签。

    这是"可溯源"在检索层的体现：用户能看到这条**为什么**被搜出来。
    """
    terms = [str(k).strip() for k in keywords if str(k).strip()]
    if not terms:
        return []

    haystacks: List[Tuple[str, str]] = [("标题", title)]
    for name, value in fields.items():
        haystacks.append((FIELD_LABELS.get(name, name), value))
    haystacks.append(("命中依据", evidence))

    hits: List[str] = []
    for label, text in haystacks:
        if not text or text == MISSING:
            continue
        if any(term in text for term in terms):
            hits.append(label)
    return hits


def make_snippet(corpus: str, keywords: Sequence[str], width: int = 120) -> str:
    """在语料里定位第一个命中词，截取片段并用 ``<em>`` 高亮（HTML 安全）。

    先 ``html.escape`` 再插入 ``<em>``：否则原文里的 ``<`` 会破坏前端 DOM，
    这是把可搜索内容渲染进网页时最容易忽略的一处注入面。
    """
    text = str(corpus or "").strip()
    if not text:
        return ""

    terms = [str(k).strip() for k in keywords if str(k).strip()]
    position, matched = -1, ""
    for term in terms:
        found = text.find(term)
        if found >= 0 and (position < 0 or found < position):
            position, matched = found, term

    if position < 0:
        return html.escape(text[:width]) + ("…" if len(text) > width else "")

    start = max(0, position - width // 3)
    end = min(len(text), start + width)
    escaped = html.escape(text[start:end])
    if matched:
        escaped_term = html.escape(matched)
        escaped = escaped.replace(escaped_term, f"<em>{escaped_term}</em>")
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{escaped}{suffix}"


# --------------------------------------------------------------------------
# 索引实现
# --------------------------------------------------------------------------


class FtsSearchIndex:
    """实现 ``contracts.SearchIndex``：基于 SQLite FTS5 的检索索引。

    连接策略：**可以由外部注入连接**（``connection=``）。
    存储层写完后就在自己那条连接上调用 ``maintain()``，
    避免"两条连接同时维护同一个 FTS 表"这种会写坏索引的用法。
    未注入时自建一条惰性连接（只读场景、独立脚本用）。
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        tokenizer: str = DEFAULT_TOKENIZER,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else None
        self._tokenizer = (
            tokenizer if tokenizer in ("trigram", "unicode61") else DEFAULT_TOKENIZER
        )
        self._conn = connection
        self._owns_connection = connection is None

    # ---------- 连接 ----------

    @property
    def conn(self) -> sqlite3.Connection:
        """惰性建连（WAL + Row 工厂），口径与存储层一致。"""
        if self._conn is None:
            if self._db_path is None:
                raise StorageError("FtsSearchIndex 既没有连接也没有数据库路径")
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode = WAL")
        return self._conn

    def close(self) -> None:
        """只关闭**自己创建**的连接；外部注入的连接由注入方负责。"""
        if self._owned_connection() and self._conn is not None:
            self._conn.close()
            self._conn = None

    def _owned_connection(self) -> bool:
        return self._owns_connection

    # ---------- 建表 ----------

    def init_schema(self) -> None:
        """建元数据表、派生列与 FTS 表，并清掉历史触发器（幂等）。"""
        conn = self.conn
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{META_TABLE}" ('
            f'  "key" TEXT PRIMARY KEY, "value" TEXT NOT NULL)'
        )
        self._create_fts_table()
        self._ensure_index_text_column()
        self._drop_legacy_triggers()
        conn.commit()

    def _create_fts_table(self) -> None:
        """建 external-content 的 FTS5 表。

        FTS5 语法要点（两个坑）：
        1. ``tokenize`` 是**表级选项**，必须写成 ``fts5(... tokenize='trigram')``；
           写成 ``"index_text" TOKENIZE="trigram"`` 会被当成列定义，直接 parse error。
        2. 用 ``content='articles'`` 之后，索引列必须写成 ``index_text``
           **取自** content 表的列；声明成普通列时 ``'rebuild'`` 会去查
           ``articles.index_text`` 而报 ``no such column``。
        因此 content 表需要有一个名为 ``index_text`` 的派生列（见
        ``_ensure_index_text_column`` 与 ``_refresh_index_text``）。
        """
        conn = self.conn
        sql = (
            f'CREATE VIRTUAL TABLE IF NOT EXISTS "{FTS_TABLE}" USING fts5('
            f"  index_text,"
            f"  tokenize='{self._tokenizer}',"
            f"  content='articles',"
            f"  content_rowid='rowid')"
        )
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (FTS_TABLE,)
        ).fetchone()
        if existing is not None and self._tokenizer not in str(existing["sql"] or ""):
            # 分词器换过（例如 unicode61 → trigram）：旧倒排索引无法复用，直接重建
            conn.execute(f'DROP TABLE IF EXISTS "{FTS_TABLE}"')
            conn.execute(sql)
            conn.execute(f'DELETE FROM "{META_TABLE}" WHERE "key" = ?', (VERSION_KEY,))
            return
        conn.execute(sql)

    def _ensure_index_text_column(self) -> bool:
        """确保 ``articles.index_text`` 派生列存在（external-content 需要读取它）。"""
        columns = self.articles_columns()
        if not columns or "index_text" in columns:
            return False
        self.conn.execute(
            'ALTER TABLE "articles" ADD COLUMN "index_text" TEXT NOT NULL DEFAULT \'\''
        )
        return True

    def _drop_legacy_triggers(self) -> None:
        """清掉历史遗留的同步触发器（已不再使用，留着会与 maintain 抢着写索引）。"""
        for name in LEGACY_TRIGGERS:
            self.conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')

    def articles_columns(self) -> List[str]:
        rows = self.conn.execute("PRAGMA table_info(articles)").fetchall()
        return [str(row["name"]) for row in rows]

    # ---------- 版本 ----------

    def index_version(self) -> int:
        try:
            row = self.conn.execute(
                f'SELECT "value" FROM "{META_TABLE}" WHERE "key" = ?', (VERSION_KEY,)
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def set_version(self, value: int) -> None:
        self.conn.execute(
            f'INSERT INTO "{META_TABLE}"("key", "value") VALUES (?, ?) '
            f'ON CONFLICT("key") DO UPDATE SET "value" = excluded."value"',
            (VERSION_KEY, str(int(value))),
        )
        self.conn.commit()

    def bump_version(self) -> int:
        """递增版本号并返回新值（**缓存失效的唯一依据**）。"""
        version = self.index_version() + 1
        self.set_version(version)
        return version

    # ---------- 索引维护 ----------

    def maintain(self) -> int:
        """写入之后调用：刷新派生列 + 重建倒排索引 + 递增版本号（返回索引行数）。

        **必须在写方自己的连接上调用**（见模块文档第 3 条坑）。
        """
        self.init_schema()
        if not self.articles_columns():
            return 0
        self._refresh_index_text()
        return self.rebuild_index_only()

    def rebuild_index_only(self) -> int:
        """只重建倒排索引（假定派生列已刷新），并递增版本号。"""
        conn = self.conn
        conn.execute(f'INSERT INTO "{FTS_TABLE}"("{FTS_TABLE}") VALUES (\'rebuild\')')
        count = int(conn.execute('SELECT COUNT(*) FROM "articles"').fetchone()[0])
        self.bump_version()
        return count

    def rebuild(self) -> int:
        """全量重建（刷新派生列 + 重建索引），返回 ``articles`` 行数。幂等。"""
        self.init_schema()
        if not self.articles_columns():
            return 0
        self._refresh_index_text()
        return self.rebuild_index_only()

    def _refresh_index_text(self) -> int:
        """刷新 ``articles.index_text`` 派生列，返回受影响行数。

        该列只服务于 FTS5 external-content 的 ``'rebuild'``：
        external-content 索引自己不存文本，官方 ``'rebuild'`` 命令要从 content 表读它。
        """
        conn = self.conn
        self._ensure_index_text_column()
        columns = self.articles_columns()
        if "index_text" not in columns:
            return 0
        expression = _build_index_text_sql(prefix="", suffix="", columns=columns)
        cursor = conn.execute(f'UPDATE "articles" SET "index_text" = {expression}')
        conn.commit()
        return int(cursor.rowcount or 0)

    # ---------- 检索 ----------

    def search(self, query: SearchQuery) -> SearchResult:
        started = time.perf_counter()
        rows = self.fetch_rows(query)
        hits = [self._row_to_hit(row, query) for row in rows]
        return SearchResult(
            query=query,
            hits=tuple(hits),
            total=self.count_matching(query),
            index_version=self.index_version(),
            source="l3",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    def count_matching(self, query: SearchQuery) -> int:
        """只数不取：用于判断"库内命中是否够用"（决定要不要联网采集）。"""
        conn = self.conn
        if not self.articles_columns():
            return 0
        field_criteria, field_params = self._field_criteria(query)
        fts_terms = significant_tokens(query.keywords, self._tokenizer)

        if fts_terms and not short_tokens(query.keywords, self._tokenizer):
            sql = (
                f'SELECT COUNT(*) FROM "{FTS_TABLE}" f JOIN "articles" a ON a.rowid = f.rowid '
                f'WHERE "{FTS_TABLE}" MATCH ?'
            )
            params: List[Any] = [to_fts_match(fts_terms, self._tokenizer)]
            if field_criteria:
                sql += " AND " + " AND ".join(field_criteria)
                params.extend(field_params)
            counted = self._scalar(sql, params)
            if counted:
                return counted
            # FTS 无命中 → 退到 LIKE 子串路径（与 fetch_rows 保持同一决策）
            like_criteria, like_params = self._text_like_criteria(query)
            if not like_criteria:
                return 0
            sql = 'SELECT COUNT(*) FROM "articles" a WHERE ' + " AND ".join(
                field_criteria + like_criteria
            )
            return self._scalar(sql, list(field_params) + list(like_params))

        like_criteria, like_params = self._text_like_criteria(query)
        if not field_criteria and not like_criteria:
            return self._scalar('SELECT COUNT(*) FROM "articles"', [])
        sql = 'SELECT COUNT(*) FROM "articles" a WHERE ' + " AND ".join(
            field_criteria + like_criteria
        )
        return self._scalar(sql, list(field_params) + list(like_params))

    def _scalar(self, sql: str, params: Sequence[Any]) -> int:
        try:
            return int(self.conn.execute(sql, list(params)).fetchone()[0])
        except sqlite3.OperationalError as exc:
            raise StorageError(f"检索计数失败（SQLite）：{exc}") from exc

    def fetch_rows(self, query: SearchQuery) -> List[Dict[str, Any]]:
        """取命中行（dict 形式，供服务层组装 DTO）。

        两条**互斥**路径：
          * 全部词 ≥3 字符 → FTS5 MATCH + bm25 排序（快、有相关性分）；
          * 含短词（<3 字符）或无词 → LIKE 扫描。
        bm25() 返回**负值，越小越相关**，所以按 ASC 排序。
        """
        conn = self.conn
        if not self.articles_columns():
            return []
        limit = max(1, int(query.limit))
        offset = max(0, int(query.offset))
        columns = ", ".join(f'a."{name}"' for name in self.articles_columns())

        fts_terms = significant_tokens(query.keywords, self._tokenizer)
        use_fts = bool(fts_terms) and not short_tokens(query.keywords, self._tokenizer)
        field_criteria, field_params = self._field_criteria(query)

        if use_fts:
            sql = (
                f'SELECT {columns}, bm25("{FTS_TABLE}") AS score '
                f'FROM "{FTS_TABLE}" f JOIN "articles" a ON a.rowid = f.rowid '
                f'WHERE "{FTS_TABLE}" MATCH ?'
            )
            params: List[Any] = [to_fts_match(fts_terms, self._tokenizer)]
            if field_criteria:
                sql += " AND " + " AND ".join(field_criteria)
                params.extend(field_params)
            sql += ' ORDER BY score ASC, a."crawl_time" DESC LIMIT ? OFFSET ?'
            params.extend([limit, offset])
            try:
                rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            except sqlite3.OperationalError as exc:
                raise StorageError(f"检索失败（SQLite）：{exc}") from exc
            if rows:
                return rows
            # FTS 无命中（例如查的是分词器切不出的子串）→ 退到 LIKE 子串路径。
            # 这一步很关键：unicode61 把整段中文当一个 token，
            # 「招聘」在「招聘会」里就找不到，只能靠 LIKE 兜住。
            return self._fetch_rows_like(query, columns, limit, offset, field_criteria, field_params)
        else:
            return self._fetch_rows_like(
                query, columns, limit, offset, field_criteria, field_params
            )

    def _fetch_rows_like(
        self,
        query: SearchQuery,
        columns: str,
        limit: int,
        offset: int,
        field_criteria: List[str],
        field_params: List[Any],
    ) -> List[Dict[str, Any]]:
        """LIKE 子串降级路径（无 bm25 分数，按采集时间倒序）。

        score 恒为 0 是**如实反映**：走这条路径时没有相关性排序，
        不假装有一个分数。
        """
        conn = self.conn
        like_criteria, like_params = self._text_like_criteria(query)
        sql = f'SELECT {columns}, 0.0 AS score FROM "articles" a'
        combined = list(field_criteria) + list(like_criteria)
        if combined:
            sql += " WHERE " + " AND ".join(combined)
        sql += ' ORDER BY a."crawl_time" DESC, a."source_url" ASC LIMIT ? OFFSET ?'
        params = list(field_params) + list(like_params) + [limit, offset]
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        except sqlite3.OperationalError as exc:
            raise StorageError(f"检索失败（SQLite）：{exc}") from exc

    def _field_criteria(self, query: SearchQuery) -> Tuple[List[str], List[Any]]:
        """字段过滤条件（如「城市=北京」），始终作为 AND 叠加在文本条件之上。"""
        criteria: List[str] = []
        params: List[Any] = []
        available = set(self.articles_columns())
        for name, value in (query.fields or {}).items():
            text = str(value or "").strip()
            if not text or name not in available:
                continue
            criteria.append(f'a."{name}" LIKE ?')
            params.append(f"%{text}%")
        return criteria, params

    def _text_like_criteria(self, query: SearchQuery) -> Tuple[List[str], List[Any]]:
        """关键词的 LIKE 降级条件（短词或无 FTS 可用时）。

        逐词在"标题 + 七项字段 + 命中依据"上做子串匹配，词之间是 **OR**
        （与 FTS 路径的 AND 语义不同：LIKE 路径召回本来就窄，
        再叠 AND 过于苛刻，实测容易一条都不返回）。
        """
        # 原始查询串优先：降级路径要的是**子串**语义（用户搜「招聘会」就该匹配
        # 标题里的「招聘会」），而 tokens 是切分结果，拼回去可能已改变原意。
        raw = str(query.raw_text or "").strip()
        terms = [raw] if raw else [str(t).strip() for t in query.keywords if str(t).strip()]
        if not terms:
            return [], []

        available = set(self.articles_columns())
        searchable = [
            name for name in ("article_title", *CORE_FIELDS, "evidence") if name in available
        ]
        if not searchable:
            return [], []

        conditions: List[str] = []
        params: List[Any] = []
        for term in terms:
            for column in searchable:
                conditions.append(f'a."{column}" LIKE ?')
                params.append(f"%{term}%")
        return ["(" + " OR ".join(conditions) + ")"], params

    def _row_to_hit(self, row: Mapping[str, Any], query: SearchQuery) -> SearchHit:
        fields = {name: str(row.get(name) or MISSING) for name in CORE_FIELDS}
        title = str(row.get("article_title") or "")
        evidence = str(row.get("evidence") or "")
        corpus = " ".join(
            [title, *fields.values(), evidence, str(row.get("publish_date") or "")]
        ).strip()

        return SearchHit(
            article_key=str(row.get("article_key") or ""),
            title=title or "(无标题)",
            source_url=str(row.get("source_url") or ""),
            raw_html_path=str(row.get("raw_html_path") or ""),
            publish_date=str(row.get("publish_date") or ""),
            score=float(row.get("score") or 0.0),
            snippet=make_snippet(corpus, query.keywords),
            highlights=tuple(detect_highlights(title, fields, evidence, query.keywords)),
            fields=fields,
        )

    def suggest_related(self, keywords: Sequence[str], limit: int = 8) -> List[str]:
        """关联词建议：从**已入库语料的真实字段值**里按频次召回。

        这样推荐的都是"本库真的有数据"的词，避免推荐了却搜不到（空结果）。
        """
        conn = self.conn
        if not self.articles_columns():
            return []
        wanted = {str(k).strip().lower() for k in keywords if str(k).strip()}
        counter: Dict[str, int] = {}
        available = set(self.articles_columns())

        for name in CORE_FIELDS:
            if name not in available:
                continue
            rows = conn.execute(
                f'SELECT "{name}" AS v, COUNT(*) AS n FROM "articles" '
                f'WHERE "{name}" IS NOT NULL AND "{name}" != ? AND TRIM("{name}") != \'\' '
                f'GROUP BY "{name}" ORDER BY n DESC LIMIT 50',
                (MISSING,),
            ).fetchall()
            for row in rows:
                value = str(row["v"]).strip()
                if not value or value == MISSING or value.lower() in wanted:
                    continue
                counter[value] = counter.get(value, 0) + int(row["n"])

        ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        return [value for value, _ in ranked[: max(1, int(limit))]]


def build_search_index(
    db_path: Optional[Path | str] = None,
    tokenizer: str = DEFAULT_TOKENIZER,
    connection: Optional[sqlite3.Connection] = None,
) -> FtsSearchIndex:
    """工厂：检索层唯一入口（与各层 ``build_*`` 约定一致）。"""
    return FtsSearchIndex(db_path, tokenizer=tokenizer, connection=connection)


def iter_index_texts(rows: Iterable[Mapping[str, Any]]) -> Iterable[str]:
    """便于测试：逐行产出索引文本。"""
    for row in rows:
        yield row_to_index_text(row)
