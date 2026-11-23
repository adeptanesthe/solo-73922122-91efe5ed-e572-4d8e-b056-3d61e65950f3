"""口述史馆检索 API

- 编辑按稿号刊发分词后的转写稿；在刊稿号拒绝重复刊发，撤稿后可同号重刊。
- 读者首查用当时全部在刊稿计算 BM25，凭游标续页。
- 游标只保存查询与首查时的语料统计（N / avgdl / 各词 df），不保存整张榜单；
  续页时用冻结的统计重算分数，跳过当前已撤稿的版本，从后续命中补足当页。
- 同号重刊得到新的版本序号，只进入重刊后新开的检索。
- SQLite 开 WAL、每请求独立连接、写事务尽量短，撤稿不必等正在计分的请求结束。

索引加速（/search/next）：
- `term_index(term, version)` 是语料级倒排索引，不按游标保存榜单。
- 索引覆盖到某游标首查时的版本高水位后，续页只按查询词从索引定位候选版本，
  再只为这些候选读取 tf / length；语料统计仍用首查冻结的 N、avgdl、df。
- 旧库在后台按版本分批、每批独立事务补齐索引；进度写入 index_meta，
  重启后从上次提交处继续。补齐未完成或出错时，续页自动回退到全量重算。
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "oral_history.db")

# Okapi BM25 参数
K1 = 1.2
B = 0.75

MAX_PAGE_SIZE = 100

# 索引：语料级倒排（term -> version），与游标无关；旧库由后台按版本分批补齐
INDEX_ENABLED = os.environ.get("INDEX_ENABLED", "1").lower() not in ("0", "false", "no")
INDEX_BACKFILL_ENABLED = (
    INDEX_ENABLED
    and os.environ.get("INDEX_BACKFILL_ENABLED", "1").lower() not in ("0", "false", "no")
)
INDEX_BATCH_SIZE = max(1, int(os.environ.get("INDEX_BATCH_SIZE", "200")))
INDEX_IDLE_SECONDS = max(0.01, float(os.environ.get("INDEX_IDLE_SECONDS", "0.05")))
_INDEX_FETCH_CHUNK = 500  # IN (...) 的参数上限留足余量

# 后台补齐可暂停（运维/演示用）：暂停期间 publish 仍会写索引，只是高水位不推进
_BACKFILL_PAUSED = threading.Event()
# 串行化补齐批次：同步接口与后台线程不会同时写 index_meta，测试/运维也用它暂停
_BACKFILL_LOCK = threading.Lock()

log = logging.getLogger("oral_history.index")


# ------------------------------------------------------------------ 数据库

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL：读（计分）不阻塞写（撤稿），写也不必等读结束
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS manuscripts (
                version      INTEGER PRIMARY KEY AUTOINCREMENT,  -- 版本序号，单调递增
                doc_id       TEXT NOT NULL,                      -- 稿号
                tokens       TEXT NOT NULL,                      -- 分词结果(JSON 数组)
                tf           TEXT NOT NULL,                      -- 词频(JSON 对象)
                length       INTEGER NOT NULL,                   -- 文档长度(词数)
                status       TEXT NOT NULL DEFAULT 'published'
                             CHECK (status IN ('published', 'withdrawn')),
                published_at TEXT NOT NULL,
                withdrawn_at TEXT
            );
            -- 同一稿号任一时刻至多一个在刊版本：重复刊发被唯一索引拒绝
            CREATE UNIQUE INDEX IF NOT EXISTS uq_manuscripts_published_doc
                ON manuscripts(doc_id) WHERE status = 'published';
            CREATE INDEX IF NOT EXISTS ix_manuscripts_status_version
                ON manuscripts(status, version);

            -- 每个检索游标只登记查询与首查时的语料统计，不保存整张榜单
            CREATE TABLE IF NOT EXISTS searches (
                id           TEXT PRIMARY KEY,
                terms        TEXT NOT NULL,        -- 查询词(JSON 数组)
                size         INTEGER NOT NULL,     -- 首查页长
                snapshot_seq INTEGER NOT NULL,     -- 首查时的版本高水位
                n_docs       INTEGER NOT NULL,     -- 首查时在刊文档数 N
                avgdl        REAL NOT NULL,        -- 首查时平均文档长度
                dfs          TEXT NOT NULL,        -- 各查询词的 df(JSON 对象)
                created_at   TEXT NOT NULL
            );

            -- 语料级倒排索引：每个 (版本, 词) 一行，与游标/榜单无关。
            -- 续页从这里定位候选版本，再只读取这些版本的 tf / length。
            CREATE TABLE IF NOT EXISTS term_index (
                term    TEXT NOT NULL,
                version INTEGER NOT NULL,
                PRIMARY KEY (term, version)
            ) WITHOUT ROWID;

            -- 索引补齐进度：indexed_seq 之前的版本已全部写入 term_index。
            -- 后台每批独立提交；重启后从此处继续。
            CREATE TABLE IF NOT EXISTS index_meta (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                indexed_seq INTEGER NOT NULL,   -- 已补齐到的高水位
                updated_at  TEXT NOT NULL,
                last_error  TEXT
            );
            INSERT OR IGNORE INTO index_meta (id, indexed_seq, updated_at)
                VALUES (1, 0, '');
            """
        )
    finally:
        conn.close()


# ------------------------------------------------------------------ 倒排索引

def _index_entry(conn: sqlite3.Connection, version: int, tf: dict[str, int]) -> None:
    """把一个版本的词频表写入倒排索引；只收非空、计数为正的词（与计分一致）。"""
    conn.executemany(
        "INSERT OR IGNORE INTO term_index (term, version) VALUES (?, ?)",
        [(t, version) for t, f in tf.items() if t and f],
    )


def _candidate_versions(
    conn: sqlite3.Connection, terms: list[str], max_version: int
) -> Optional[set[int]]:
    """按查询词从索引取候选版本（<= max_version）。索引未就绪时返回 None。

    调用方负责开启/提交读事务，使索引查询与在刊状态读取处于同一快照。
    """
    if not INDEX_ENABLED:
        return None
    row = conn.execute("SELECT indexed_seq FROM index_meta WHERE id = 1").fetchone()
    if row is None or row["indexed_seq"] < max_version:
        return None
    versions: set[int] = set()
    for chunk_start in range(0, len(terms), _INDEX_FETCH_CHUNK):
        chunk = terms[chunk_start:chunk_start + _INDEX_FETCH_CHUNK]
        ph = ",".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT DISTINCT version FROM term_index"
            f" WHERE term IN ({ph}) AND version <= ?",
            (*chunk, max_version),
        ):
            versions.add(r["version"])
    return versions


def _fetch_published_by_version(
    conn: sqlite3.Connection, versions: set[int]
) -> list[tuple[str, int, dict, int]]:
    """只为索引命中的候选版本读取 doc_id / tf / length，并按同一快照过滤在刊状态。"""
    docs = []
    ordered = sorted(versions)
    for start in range(0, len(ordered), _INDEX_FETCH_CHUNK):
        chunk = ordered[start:start + _INDEX_FETCH_CHUNK]
        ph = ",".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT version, doc_id, tf, length FROM manuscripts"
            f" WHERE version IN ({ph}) AND status = 'published'",
            chunk,
        ):
            docs.append((r["doc_id"], r["version"], json.loads(r["tf"]), r["length"]))
    return docs


def _index_meta(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT indexed_seq, updated_at, last_error FROM index_meta WHERE id = 1"
    ).fetchone()
    if row is None:
        return {"indexed_seq": 0, "updated_at": None, "last_error": None}
    return dict(row)


def _backfill_once(
    conn: sqlite3.Connection, batch_size: Optional[int] = None
) -> tuple[int, Optional[str]]:
    """补齐一批版本（<= batch_size），单个写事务提交；返回 (写入数, 错误)。

    只推进到本批最后一个**成功写入**的版本，因此中断后从该处接着补，
    不会留下空洞。新增版本即使大于当前进度也不会漏补：进度按版本升序推进。
    已追上最新版本时只做一次读检查，不取写锁，避免空闲时持续阻塞刊发/撤稿。
    """
    if batch_size is None:
        batch_size = INDEX_BATCH_SIZE
    meta = conn.execute(
        "SELECT indexed_seq FROM index_meta WHERE id = 1"
    ).fetchone()
    indexed_seq = meta["indexed_seq"] if meta else 0
    latest = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM manuscripts"
    ).fetchone()["v"]
    if latest <= indexed_seq:
        return 0, None

    connected = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        connected = True
        meta = conn.execute(
            "SELECT indexed_seq FROM index_meta WHERE id = 1"
        ).fetchone()
        indexed_seq = meta["indexed_seq"] if meta else 0
        rows = conn.execute(
            "SELECT version, tf FROM manuscripts"
            " WHERE version > ? ORDER BY version LIMIT ?",
            (indexed_seq, batch_size),
        ).fetchall()
        if not rows:
            conn.execute("COMMIT")
            return 0, None

        inserted = 0
        for r in rows:
            before = conn.total_changes
            _index_entry(conn, r["version"], json.loads(r["tf"]))
            inserted += conn.total_changes - before
        new_seq = rows[-1]["version"]
        conn.execute(
            "UPDATE index_meta SET indexed_seq = ?, updated_at = ?, last_error = NULL"
            " WHERE id = 1",
            (new_seq, _utcnow()),
        )
        conn.execute("COMMIT")
        return inserted, None
    except Exception as exc:  # 记录错误但保留已提交进度，续页仍可走原路径
        if connected:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        msg = f"{type(exc).__name__}: {exc}"
        try:
            conn.execute(
                "UPDATE index_meta SET last_error = ?, updated_at = ? WHERE id = 1",
                (msg, _utcnow()),
            )
        except sqlite3.Error:
            pass  # 数据库暂时不可写：错误由返回值/后台循环日志体现
        return 0, msg


def _backfill_worker(stop: threading.Event) -> None:
    """后台补齐循环：一批一提交，直到追上最新版本；出错后退避重试。"""
    while not stop.is_set():
        if _BACKFILL_PAUSED.is_set():
            stop.wait(INDEX_IDLE_SECONDS)
            continue
        try:
            conn = _connect()
            try:
                with _BACKFILL_LOCK:
                    inserted, err = _backfill_once(conn)
            finally:
                conn.close()
            if err:
                log.warning("索引补齐出错，稍后重试: %s", err)
                stop.wait(max(1.0, INDEX_IDLE_SECONDS * 20))
            elif inserted == 0:
                stop.wait(INDEX_IDLE_SECONDS)
            else:
                stop.wait(min(0.2, INDEX_IDLE_SECONDS))
        except Exception as exc:  # 数据库暂时不可用等：不退出补齐线程
            log.warning("索引补齐循环异常: %s", exc)
            stop.wait(1.0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_db()
    stop = threading.Event()
    worker: Optional[threading.Thread] = None
    if INDEX_BACKFILL_ENABLED:
        worker = threading.Thread(
            target=_backfill_worker, args=(stop,), name="index-backfill", daemon=True
        )
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=5.0)


app = FastAPI(title="口述史馆检索 API", version="1.1.0", lifespan=lifespan)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ BM25

def _bm25_idf(n_docs: int, df: int) -> float:
    # Robertson idf 变体，+1 保证为正：命中至少一个查询词的文档得分必为正
    return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))


def _load_published_docs(conn: sqlite3.Connection, max_version: Optional[int] = None):
    """读当前在刊版本；续页时按首查的版本高水位截断，排除重刊/新刊版本。"""
    sql = ("SELECT version, doc_id, tf, length FROM manuscripts "
           "WHERE status = 'published'")
    params: tuple = ()
    if max_version is not None:
        sql += " AND version <= ?"
        params = (max_version,)
    docs = []
    for row in conn.execute(sql, params):
        docs.append((row["doc_id"], row["version"], json.loads(row["tf"]), row["length"]))
    return docs


def _score_hits(docs, terms, dfs, n_docs, avgdl):
    """用（首查冻结的）语料统计对文档计分，返回按 (分数降序, 稿号升序) 排列的命中。

    相同输入下浮点结果逐位一致，因此续页重算的分数与首查相同。
    """
    idfs = {t: _bm25_idf(n_docs, dfs[t]) for t in terms if dfs.get(t, 0) > 0}
    hits = []
    for doc_id, version, tf, dl in docs:
        score = 0.0
        for t in terms:  # 固定词序累加，保证浮点结果确定
            idf = idfs.get(t)
            if idf is None:
                continue
            f = tf.get(t)
            if f:
                score += idf * (f * (K1 + 1.0)) / (f + K1 * (1.0 - B + B * dl / avgdl))
        if score > 0.0:
            hits.append({"doc_id": doc_id, "version": version, "score": score})
    hits.sort(key=lambda h: (-h["score"], h["doc_id"]))
    return hits


# ------------------------------------------------------------------ 游标

def _encode_cursor(search_id: str, score: float, doc_id: str) -> str:
    payload = json.dumps({"sid": search_id, "after": [score, doc_id]}, ensure_ascii=False)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(token: str) -> tuple[str, float, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
        score, doc_id = payload["after"]
        return str(payload["sid"]), float(score), str(doc_id)
    except Exception:
        raise HTTPException(status_code=400, detail="无法识别的游标")


# ------------------------------------------------------------------ 请求模型

class PublishRequest(BaseModel):
    doc_id: str = Field(min_length=1, max_length=128, description="稿号")
    tokens: list[str] = Field(min_length=1, description="分词后的转写稿")


class SearchRequest(BaseModel):
    terms: Optional[list[str]] = Field(default=None, description="查询词（已分词）")
    query: Optional[str] = Field(default=None, description="空白分隔的查询串，与 terms 二选一")
    size: int = Field(default=10, ge=1, le=MAX_PAGE_SIZE, description="页长")
    debug_delay_seconds: float = Field(
        default=0.0, ge=0.0, le=30.0,
        description="测试钩子：人为拉长计分耗时，用于演示撤稿不阻塞计分",
    )


class ContinueRequest(BaseModel):
    cursor: str = Field(description="上一页返回的 next_cursor")
    size: Optional[int] = Field(default=None, ge=1, le=MAX_PAGE_SIZE,
                                description="缺省沿用首查页长")
    debug: bool = Field(default=False,
                        description="附带读取范围诊断字段（候选数 / 快照在刊总数）")


def _resolve_terms(req: SearchRequest) -> list[str]:
    raw = req.terms if req.terms else (req.query.split() if req.query else [])
    terms = list(dict.fromkeys(t for t in raw if t))  # 去重保序
    if not terms:
        raise HTTPException(status_code=422, detail="请通过 terms 或 query 提供至少一个查询词")
    return terms


# ------------------------------------------------------------------ 刊发 / 撤稿

@app.post("/manuscripts", status_code=201)
def publish_manuscript(req: PublishRequest):
    """刊发分词后的转写稿。在刊稿号重复刊发返回 409；撤稿后可同号重刊。"""
    tokens = [t for t in req.tokens if t]
    if not tokens:
        raise HTTPException(status_code=422, detail="tokens 不能为空")
    tf: dict[str, int] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO manuscripts (doc_id, tokens, tf, length, status, published_at)"
                " VALUES (?,?,?,?, 'published', ?)",
                (req.doc_id, json.dumps(tokens, ensure_ascii=False),
                 json.dumps(tf, ensure_ascii=False), len(tokens), _utcnow()),
            )
            version = cur.lastrowid
            # 新刊版本在同一写事务内写入倒排索引。若此前索引恰好补齐到
            # 上一个版本（稳态），可原子地把高水位推进到本版本；若补齐仍在
            # 追赶，则保持高水位不动，由后台按版本顺序补齐。
            # （旧实例不写索引也不会被跳过：它们的版本会让高水位条件不满足，
            #   由补齐扫描补上。）
            if INDEX_ENABLED:
                _index_entry(conn, version, tf)
                row = conn.execute(
                    "SELECT indexed_seq FROM index_meta WHERE id = 1"
                ).fetchone()
                if row is not None and row["indexed_seq"] == version - 1:
                    conn.execute(
                        "UPDATE index_meta SET indexed_seq = ?, updated_at = ?"
                        " WHERE id = 1",
                        (version, _utcnow()),
                    )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409,
                                detail=f"稿号 {req.doc_id} 已有在刊版本，请先撤稿再重刊")
    finally:
        conn.close()
    return {"doc_id": req.doc_id, "version": version, "status": "published",
            "length": len(tokens)}


@app.post("/manuscripts/{doc_id}/withdraw")
def withdraw_manuscript(doc_id: str):
    """撤稿：立即生效，后续检索请求（含续页）不再返回该版本。"""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "UPDATE manuscripts SET status = 'withdrawn', withdrawn_at = ?"
            " WHERE doc_id = ? AND status = 'published' RETURNING version",
            (_utcnow(), doc_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=404, detail=f"稿号 {doc_id} 没有在刊版本")
        conn.execute("COMMIT")
        version = row["version"]
    finally:
        conn.close()
    return {"doc_id": doc_id, "version": version, "status": "withdrawn"}


@app.get("/manuscripts")
def list_manuscripts():
    """列出全部版本（含已撤稿），便于观察版本演变。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT version, doc_id, tokens, length, status, published_at, withdrawn_at"
            " FROM manuscripts ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    return {"manuscripts": [
        {**dict(r), "tokens": json.loads(r["tokens"])} for r in rows
    ]}


# ------------------------------------------------------------------ 检索

@app.post("/search")
def first_search(req: SearchRequest):
    """首查：用当前全部在刊稿计算 BM25，返回第一页与续页游标。"""
    terms = _resolve_terms(req)

    # 1) 单个读事务内取一致快照：在刊文档 + 版本高水位
    conn = _connect()
    try:
        conn.execute("BEGIN")
        docs = _load_published_docs(conn)
        snapshot_seq = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM manuscripts"
        ).fetchone()["v"]
        conn.execute("COMMIT")
    finally:
        conn.close()

    # 2) 语料统计（冻结进游标，供续页重算）
    n_docs = len(docs)
    avgdl = sum(d[3] for d in docs) / n_docs if n_docs else 0.0
    dfs = {t: sum(1 for d in docs if t in d[2]) for t in terms}

    if req.debug_delay_seconds > 0:
        time.sleep(req.debug_delay_seconds)  # 测试钩子：模拟长耗时计分

    # 3) 计分、排序、取首页
    hits = _score_hits(docs, terms, dfs, n_docs, avgdl)
    page = hits[: req.size]

    # 4) 还有后续命中时登记检索（只存统计，不存榜单），签发游标
    next_cursor = None
    if len(hits) > req.size:
        search_id = uuid.uuid4().hex
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO searches (id, terms, size, snapshot_seq, n_docs, avgdl, dfs, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (search_id, json.dumps(terms, ensure_ascii=False), req.size, snapshot_seq,
                 n_docs, avgdl, json.dumps(dfs, ensure_ascii=False), _utcnow()),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        next_cursor = _encode_cursor(search_id, page[-1]["score"], page[-1]["doc_id"])

    return {
        "terms": terms,
        "size": req.size,
        "snapshot_seq": snapshot_seq,
        "total_hits": len(hits),
        "hits": page,
        "next_cursor": next_cursor,
    }


@app.post("/search/next")
def continue_search(req: ContinueRequest):
    """续页：沿用首查的分数与顺序，跳过已撤稿版本，从后续命中补足当页。

    索引就绪（indexed_seq >= 首查 snapshot_seq）时，只按查询词从 term_index
    定位候选版本，再读取这些版本的 tf / length；否则回退到原来的全量路径。
    """
    search_id, after_score, after_doc_id = _decode_cursor(req.cursor)

    conn = _connect()
    try:
        conn.execute("BEGIN")
        srow = conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
        if srow is None:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=404, detail="游标不存在或已过期")
        terms = json.loads(srow["terms"])
        # 索引与在刊状态在同一读事务里读取：候选版本和状态过滤出自同一快照。
        # 只取首查快照内的版本；本次请求前已撤稿的版本被跳过；
        # 重刊版本 version > snapshot_seq，不会进入本次检索。
        candidate_versions = _candidate_versions(
            conn, terms, srow["snapshot_seq"]
        )
        if candidate_versions is None:
            docs = _load_published_docs(conn, max_version=srow["snapshot_seq"])
            path = "scan"
            index_hits = 0
        else:
            docs = _fetch_published_by_version(conn, candidate_versions)
            path = "index"
            index_hits = len(candidate_versions)
        conn.execute("COMMIT")
    finally:
        conn.close()

    dfs = json.loads(srow["dfs"])
    size = req.size if req.size is not None else srow["size"]

    # 用首查冻结的统计重算：分数与顺序与首查一致
    hits = _score_hits(docs, terms, dfs, srow["n_docs"], srow["avgdl"])
    remaining = [
        h for h in hits
        if h["score"] < after_score
        or (h["score"] == after_score and h["doc_id"] > after_doc_id)
    ]
    page = remaining[:size]
    next_cursor = (
        _encode_cursor(search_id, page[-1]["score"], page[-1]["doc_id"])
        if len(remaining) > size else None
    )
    result = {
        "terms": terms,
        "size": size,
        "remaining_hits": len(remaining),
        "hits": page,
        "next_cursor": next_cursor,
    }
    if req.debug:
        # 读取范围诊断：index 路径只读候选版本；scan 路径读全部在刊快照。
        result["read_scope"] = {
            "path": path,
            "index_candidates": index_hits,      # 索引定位到的候选版本数
            "docs_scored": len(docs),            # 实际读取 tf/length 并计分的版本数
            "snapshot_published": srow["n_docs"],  # 首查时在刊稿总数
            "snapshot_seq": srow["snapshot_seq"],
        }
    return result


# ------------------------------------------------------------------ 其他

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    return {
        "service": "口述史馆检索 API",
        "endpoints": {
            "publish": "POST /manuscripts",
            "withdraw": "POST /manuscripts/{doc_id}/withdraw",
            "list": "GET /manuscripts",
            "first_search": "POST /search",
            "continue_search": "POST /search/next",
            "index_status": "GET /index/status",
            "index_backfill": "POST /index/backfill",
            "index_pause": "POST /index/pause",
            "index_resume": "POST /index/resume",
            "index_rebuild": "POST /index/rebuild",
        },
    }


# ------------------------------------------------------------------ 索引运维

@app.get("/index/status")
def index_status():
    """观察索引补齐进度：indexed_seq 追上最新版本即"索引就绪"。"""
    conn = _connect()
    try:
        meta = _index_meta(conn)
        latest = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM manuscripts"
        ).fetchone()["v"]
        rows = conn.execute("SELECT COUNT(*) AS c FROM term_index").fetchone()["c"]
    finally:
        conn.close()
    return {
        "enabled": INDEX_ENABLED,
        "indexed_seq": meta["indexed_seq"],
        "latest_version": latest,
        "ready": INDEX_ENABLED and meta["indexed_seq"] >= latest,
        "term_entries": rows,
        "backfill_error": meta["last_error"],
        "updated_at": meta["updated_at"],
    }


@app.post("/index/backfill")
def index_backfill(batches: int = 1):
    """同步补齐若干批（便于测试/运维推进）；后台线程仍会继续补齐。"""
    if not INDEX_ENABLED:
        raise HTTPException(status_code=409, detail="索引已禁用")
    done = 0
    last_err = None
    conn = _connect()
    try:
        with _BACKFILL_LOCK:
            for _ in range(max(1, min(batches, 1000))):
                inserted, last_err = _backfill_once(conn)
                done += inserted
                if last_err or inserted == 0:
                    break
        meta = _index_meta(conn)
    finally:
        conn.close()
    return {"inserted": done, "indexed_seq": meta["indexed_seq"],
            "backfill_error": last_err}


@app.post("/index/pause")
def index_pause():
    """暂停后台补齐（同步接口不受影响），用于演示/测试"补齐未完成"状态。"""
    _BACKFILL_PAUSED.set()
    return {"paused": True}


@app.post("/index/resume")
def index_resume():
    _BACKFILL_PAUSED.clear()
    return {"paused": False}


@app.post("/index/rebuild")
def index_rebuild(pause: bool = False):
    """重建语料索引：清空 term_index、进度归零，由后台重新补齐。

    用于演示/运维（例如怀疑索引损坏）。pause=true 时重建后保持"未就绪"状态，
    便于复现升级前旧库（游标签发时索引尚未补齐）的行为。
    """
    if not INDEX_ENABLED:
        raise HTTPException(status_code=409, detail="索引已禁用")
    with _BACKFILL_LOCK:
        if pause:
            _BACKFILL_PAUSED.set()
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM term_index")
            conn.execute(
                "UPDATE index_meta SET indexed_seq = 0, last_error = NULL, updated_at = ?"
                " WHERE id = 1",
                (_utcnow(),),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
    return {"rebuilt": True, "paused": _BACKFILL_PAUSED.is_set()}
