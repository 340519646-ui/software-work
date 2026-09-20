"""L1/L2/L3 编排 + 「有倾向性采集」的触发判定（检索层唯一门面）。

一次检索的完整流程
------------------
1. 规范化查询意图（检索词 + 字段过滤 + 分页）；
2. **L1** 进程内 LRU → 命中即返回（微秒）；
3. **L2** SQLite 查询缓存（键含数据版本号）→ 命中即返回（毫秒）；
4. **L3** FTS5 检索 → 回填 L2、L1；
5. 命中不足时生成 ``SearchSuggestion``：**只建议、不擅自联网**。

「有倾向性采集」的三道闸门
--------------------------
采集是联网且受「≥2 秒/请求」红线约束的动作，因此触发必须被三重约束：

* ``search.trigger_enabled`` —— 总开关，默认 **False**；
* 冷却时间 —— 同一查询串 ``fetch_cooldown_seconds`` 内只允许一次；
* 预算闸门 —— 预估耗时超过 ``max_estimated_seconds`` 直接拒绝。

预估耗时按 ``请求数 × request.interval_seconds`` 计算，且请求数取"最坏情况"
（列表页 + 每页 15 条详情），宁可高估也不要让用户以为 10 秒就好。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.config import AppConfig
from src.contracts import (
    CORE_FIELDS,
    FetchJob,
    PipelineError,
    SearchQuery,
    SearchResult,
    SearchSuggestion,
    StorageError,
)
from src.search.cache import LruCache, QueryCache, cache_key
from src.search.index import FtsSearchIndex, tokenize, to_fts_match

logger = logging.getLogger(__name__)

COOLDOWN_TABLE = "search_fetch_cooldown"
JOBS_TABLE = "search_fetch_jobs"
PAGE_SIZE_ASSUMED = 15
"""门户每页条数（实测 15）。仅用于估算请求数与耗时，不参与实际翻页。"""


def normalize_query(
    raw_text: str,
    cfg: AppConfig,
    fields: Optional[Dict[str, str]] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    want_fetch: bool = False,
) -> SearchQuery:
    """把原始输入变成受配置约束的 ``SearchQuery``（纯函数）。

    ``limit`` 会被夹到 ``[1, search.max_limit]``：否则前端一个 ``limit=999999``
    就能把整个库拖进内存。
    """
    tokens = tokenize(raw_text)
    resolved_limit = cfg.search.default_limit if limit is None else int(limit)
    resolved_limit = max(1, min(resolved_limit, int(cfg.search.max_limit)))
    return SearchQuery(
        keywords=tuple(tokens),
        fields={str(k): str(v) for k, v in (fields or {}).items() if str(v or "").strip()},
        limit=resolved_limit,
        offset=max(0, int(offset)),
        want_fetch=bool(want_fetch),
        raw_text=str(raw_text or "").strip(),
    )


class SearchService:
    """检索服务：三级缓存编排 + 定向采集判定。"""

    def __init__(
        self,
        cfg: AppConfig,
        index: FtsSearchIndex,
        conn: sqlite3.Connection,
        fetcher_factory: Optional[Callable[[AppConfig], Any]] = None,
        extractor_factory: Optional[Callable[[AppConfig], Any]] = None,
        validator_factory: Optional[Callable[[AppConfig], Any]] = None,
        repository_factory: Optional[Callable[[AppConfig], Any]] = None,
        login_checker: Optional[Callable[[AppConfig], None]] = None,
    ) -> None:
        self._cfg = cfg
        self._index = index
        self._conn = conn
        self._l1 = LruCache(capacity=cfg.search.l1_size)
        self._l2 = QueryCache(
            conn,
            ttl_seconds=cfg.search.cache_ttl_seconds,
            max_rows=cfg.search.cache_max_rows,
        )
        self._fetcher_factory = fetcher_factory
        self._extractor_factory = extractor_factory
        self._validator_factory = validator_factory
        self._repository_factory = repository_factory
        self._login_checker = login_checker
        self._jobs: Dict[str, FetchJob] = {}
        self._jobs_lock = threading.Lock()

    # ---------- 初始化 ----------

    def init_schema(self) -> None:
        """建缓存表、冷却表与作业表（幂等）。"""
        self._l2.init_schema()
        conn = self._conn
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{COOLDOWN_TABLE}" ('
            f'  "search_value" TEXT PRIMARY KEY,'
            f'  "query_key" TEXT NOT NULL,'
            f'  "last_fetch_at" REAL NOT NULL)'
        )
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{JOBS_TABLE}" ('
            f'  "job_id" TEXT PRIMARY KEY,'
            f'  "state" TEXT NOT NULL,'
            f'  "payload" TEXT NOT NULL,'
            f'  "created_at" REAL NOT NULL)'
        )
        conn.commit()

    # ---------- 检索主流程 ----------

    def search(self, query: SearchQuery) -> SearchResult:
        """三级缓存编排：L1 → L2 → L3，并回填上层。"""
        started = time.perf_counter()
        version = self._index.index_version()
        key = cache_key(query, version)

        cached = self._l1.get(key)
        if cached is not None:
            return self._with_meta(cached, "l1", started)

        cached = self._l2.get(key, version)
        if cached is not None:
            self._l1.put(key, cached)
            return self._with_meta(cached, "l2", started)

        result = self._index.search(query)
        result = self._with_result_meta(result, started)
        self._l2.put(key, result)
        self._l1.put(key, result)
        return result

    def _with_meta(self, result: SearchResult, source: str, started: float) -> SearchResult:
        """命中缓存时改写来源与耗时（``elapsed_ms`` 反映**本次**实际耗时）。"""
        from dataclasses import replace

        return replace(
            result,
            source=source,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    def _with_result_meta(self, result: SearchResult, started: float) -> SearchResult:
        """给 L3 结果补上关联词建议与采集建议。"""
        from dataclasses import replace

        suggestion = self.build_suggestion(result)
        related = (
            tuple(self._index.suggest_related(result.query.keywords, limit=8))
            if result.query.keywords
            else ()
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        return replace(
            result,
            suggestion=suggestion,
            related=related,
            elapsed_ms=elapsed,
        )

    # ---------- 采集建议 ----------

    def build_suggestion(self, result: SearchResult) -> Optional[SearchSuggestion]:
        """判断"库内是否不够用"，返回采集建议（不发起任何请求）。"""
        cfg = self._cfg
        if result.query.is_empty():
            return None

        # 命中数已达请求量 → 库内够用，不必采集
        if result.total >= result.query.limit:
            return None

        search_value = result.query.raw_text or " ".join(result.query.keywords)
        requests = 1 + cfg.search.max_fetch_pages * PAGE_SIZE_ASSUMED
        estimated = requests * float(cfg.request.interval_seconds)

        if not cfg.search.trigger_enabled:
            return SearchSuggestion(
                reason=f"库内命中 {result.total} 条，少于请求的 {result.query.limit} 条",
                search_value=search_value,
                estimated_requests=requests,
                estimated_seconds=estimated,
                allowed=False,
                blocked_reason="search.trigger_enabled=false（采集总开关关闭）",
            )

        if estimated > float(cfg.search.max_estimated_seconds):
            return SearchSuggestion(
                reason=f"库内命中 {result.total} 条，少于请求的 {result.query.limit} 条",
                search_value=search_value,
                estimated_requests=requests,
                estimated_seconds=estimated,
                allowed=False,
                blocked_reason=(
                    f"预估 {estimated:.0f} 秒超过预算上限 "
                    f"{cfg.search.max_estimated_seconds:.0f} 秒"
                ),
            )

        remaining = self.cooldown_remaining(search_value)
        if remaining > 0:
            return SearchSuggestion(
                reason=f"库内命中 {result.total} 条，少于请求的 {result.query.limit} 条",
                search_value=search_value,
                estimated_requests=requests,
                estimated_seconds=estimated,
                allowed=False,
                blocked_reason=f"该查询刚采集过，冷却中（还需 {remaining // 60 + 1} 分钟）",
            )

        return SearchSuggestion(
            reason=f"库内命中 {result.total} 条，少于请求的 {result.query.limit} 条",
            search_value=search_value,
            estimated_requests=requests,
            estimated_seconds=estimated,
            allowed=True,
        )

    # ---------- 冷却 ----------

    def cooldown_remaining(self, search_value: str) -> int:
        """距离下次可采集还剩多少秒（0 表示可以采集）。"""
        cooldown = int(self._cfg.search.fetch_cooldown_seconds)
        if cooldown <= 0:
            return 0
        row = self._conn.execute(
            f'SELECT "last_fetch_at" FROM "{COOLDOWN_TABLE}" WHERE "search_value" = ?',
            (str(search_value),),
        ).fetchone()
        if row is None:
            return 0
        elapsed = time.time() - float(row["last_fetch_at"])
        return max(0, int(cooldown - elapsed))

    def _mark_fetched(self, search_value: str, query_key: str) -> None:
        self._conn.execute(
            f'INSERT INTO "{COOLDOWN_TABLE}"("search_value", "query_key", "last_fetch_at") '
            f"VALUES (?, ?, ?) "
            f'ON CONFLICT("search_value") DO UPDATE SET '
            f'  "last_fetch_at" = excluded."last_fetch_at",'
            f'  "query_key" = excluded."query_key"',
            (str(search_value), str(query_key), time.time()),
        )
        self._conn.commit()

    # ---------- 关联词 ----------

    def suggest_related(self, keywords: Sequence[str], limit: int = 8) -> List[str]:
        return self._index.suggest_related(keywords, limit=limit)

    # ---------- 定向采集作业 ----------

    def request_fetch(self, query: SearchQuery) -> FetchJob:
        """受理一次定向采集请求，返回作业（**在后台线程执行**）。

        为什么必须异步：一页 15 条 = 15 次详情请求，按 ≥2 秒/请求计约 32 秒，
        同步执行会让 HTTP 请求挂死、前端以为页面崩了。作业状态由前端轮询。
        """
        cfg = self._cfg
        search_value = query.raw_text or " ".join(query.keywords)

        if not cfg.search.trigger_enabled:
            return FetchJob(
                job_id="",
                search_value=search_value,
                state="skipped",
                message="采集总开关关闭（search.trigger_enabled=false）",
            )
        if not search_value.strip():
            return FetchJob(job_id="", state="skipped", message="空查询不触发采集")

        # 登录门禁放在**后台线程之前、冷却计时之前**：
        # 没有登录态就该立刻告诉用户（并引导去跑 login_check），
        # 而不是让用户等一分钟拿到一个后台失败，还把冷却额度白白用掉。
        try:
            (self._login_checker or self._default_login_check)(cfg)
        except PipelineError as exc:
            logger.warning("定向采集被登录门禁拦下：%s", exc)
            return FetchJob(
                job_id="",
                search_value=search_value,
                state="skipped",
                message=f"登录态无效：{exc}；请先执行 python -m src.crawler.login_check",
            )

        remaining = self.cooldown_remaining(search_value)
        if remaining > 0:
            return FetchJob(
                job_id="",
                search_value=search_value,
                state="skipped",
                message=f"冷却中，还需 {remaining // 60 + 1} 分钟",
            )

        job = FetchJob(
            job_id=uuid.uuid4().hex[:12],
            query_key=query.normalized_key(),
            search_value=search_value,
            state="pending",
        )
        self._store_job(job)
        self._mark_fetched(search_value, job.query_key)
        thread = threading.Thread(
            target=self._run_fetch_job,
            args=(job.job_id, query),
            name=f"search-fetch-{job.job_id}",
            daemon=True,
        )
        thread.start()
        return job

    def get_job(self, job_id: str) -> Optional[FetchJob]:
        with self._jobs_lock:
            job = self._jobs.get(str(job_id))
        if job is not None:
            return job
        # 进程重启后从库里恢复（前端可能还在轮询旧作业）
        row = self._conn.execute(
            f'SELECT "payload" FROM "{JOBS_TABLE}" WHERE "job_id" = ?', (str(job_id),)
        ).fetchone()
        if row is None:
            return None
        import json

        try:
            return _job_from_payload(json.loads(row["payload"]))
        except (TypeError, ValueError):
            return None

    def latest_job(self) -> Optional[FetchJob]:
        """最近一次采集作业（前端打开页面时恢复进度条用）。"""
        with self._jobs_lock:
            if self._jobs:
                return max(self._jobs.values(), key=lambda job: job.started_at or "")
        row = self._conn.execute(
            f'SELECT "payload" FROM "{JOBS_TABLE}" ORDER BY "created_at" DESC LIMIT 1'
        ).fetchone()
        if row is None:
            return None
        import json

        try:
            return _job_from_payload(json.loads(row["payload"]))
        except (TypeError, ValueError):
            return None

    def _store_job(self, job: FetchJob) -> None:
        import json

        with self._jobs_lock:
            self._jobs[job.job_id] = job
        self._conn.execute(
            f'INSERT INTO "{JOBS_TABLE}"("job_id", "state", "payload", "created_at") '
            f"VALUES (?, ?, ?, ?) "
            f'ON CONFLICT("job_id") DO UPDATE SET '
            f'  "state" = excluded."state", "payload" = excluded."payload"',
            (job.job_id, job.state, json.dumps(job.to_json(), ensure_ascii=False), time.time()),
        )
        self._conn.commit()

    def _run_fetch_job(self, job_id: str, query: SearchQuery) -> None:
        """后台执行：定向采集 → 解析入库 → 索引版本号递增（缓存自动失效）。"""
        from dataclasses import replace

        job = self.get_job(job_id)
        if job is None:
            return
        job = replace(job, state="running", started_at=_now_iso())
        self._store_job(job)

        try:
            outcome = self._execute_fetch(query)
            job = replace(
                job,
                state="done",
                finished_at=_now_iso(),
                **outcome,
            )
        except Exception as exc:  # noqa: BLE001 - 后台线程必须吞掉异常，否则静默死亡
            logger.exception("定向采集作业失败：%s", exc)
            job = replace(
                job,
                state="failed",
                finished_at=_now_iso(),
                message=f"{type(exc).__name__}: {exc}",
            )
        self._store_job(job)

    def _execute_fetch(self, query: SearchQuery) -> Dict[str, Any]:
        """真正执行采集（延迟导入，避免未装 playwright 时检索服务起不来）。"""
        from src.search.fetcher import run_targeted_fetch

        return run_targeted_fetch(
            self._cfg,
            query,
            index=self._index,
            fetcher_factory=self._fetcher_factory,
            extractor_factory=self._extractor_factory,
            validator_factory=self._validator_factory,
            repository_factory=self._repository_factory,
            login_checker=self._login_checker,
        )

    def _default_login_check(self, cfg: AppConfig) -> None:
        """默认登录门禁：延迟导入，避免未装 playwright 时服务起不来。

        ``SearchService._default_login_check`` 本身是**同步**调用（会真实登录并落盘会话），
        但只在 request_fetch 里被调用一次；真实采集仍在后台线程跑。
        """
        from src.crawler.login_check import require_login

        require_login(cfg)

    # ---------- 运维视图 ----------

    def stats(self) -> Dict[str, Any]:
        """缓存与索引的可观测指标（页面底部显示）。"""
        return {
            "index_version": self._index.index_version(),
            "indexed_rows": self._count("articles"),
            "query_cache_rows": self._l2.count(),
            "l1_entries": len(self._l1),
            "l1_hits": self._l1.hits,
            "l1_misses": self._l1.misses,
            "trigger_enabled": self._cfg.search.trigger_enabled,
            "db_path": str(self._cfg.path(self._cfg.storage.db_path)),
        }

    def _count(self, table: str) -> int:
        try:
            return int(self._conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        except sqlite3.OperationalError:
            return 0


def _now_iso() -> str:
    from src.contracts import now_iso

    return now_iso()


def _job_from_payload(payload: Dict[str, Any]) -> FetchJob:
    return FetchJob(
        job_id=str(payload.get("job_id") or ""),
        search_value=str(payload.get("search_value") or ""),
        state=str(payload.get("state") or "pending"),
        pages_fetched=int(payload.get("pages_fetched") or 0),
        refs_found=int(payload.get("refs_found") or 0),
        details_ok=int(payload.get("details_ok") or 0),
        details_failed=int(payload.get("details_failed") or 0),
        records_inserted=int(payload.get("records_inserted") or 0),
        message=str(payload.get("message") or ""),
        started_at=str(payload.get("started_at") or ""),
        finished_at=str(payload.get("finished_at") or ""),
    )


def build_search_service(cfg: AppConfig) -> SearchService:
    """工厂：打开数据库连接、建好各级索引与缓存（Web 层只调它）。"""
    from src.search.index import build_search_index

    db_path = cfg.path(cfg.storage.db_path)
    index = build_search_index(db_path, tokenizer=cfg.search.fts_tokenizer)
    index.init_schema()

    service = SearchService(cfg, index, index.conn)
    service.init_schema()
    return service
