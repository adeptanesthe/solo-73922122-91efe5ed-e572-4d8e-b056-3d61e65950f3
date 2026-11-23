"""关键操作测试：刊发/撤稿/重刊、首查与续页、快照语义、撤稿不阻塞计分。"""

import base64
import json
import os
import sys
import tempfile
import threading
import time

import pytest

_fd, _DB = tempfile.mkstemp(suffix=".sqlite3")
os.close(_fd)
os.unlink(_DB)  # 交给应用重建
os.environ["DB_PATH"] = _DB
# 测试里手动推进补齐，避免后台线程与每个用例的临时库赛跑
os.environ["INDEX_BACKFILL_ENABLED"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """每个测试一个独立的 SQLite 库。"""
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.sqlite3"))
    main._init_db()
    yield


def _full_backfill():
    """同步补齐到最新版本（测试里 INDEX_BACKFILL_ENABLED=0 时使用）。"""
    conn = main._connect()
    try:
        with main._BACKFILL_LOCK:
            while True:
                inserted, err = main._backfill_once(conn)
                assert err is None
                if inserted == 0:
                    break
        return main._index_meta(conn)
    finally:
        conn.close()


# 语料：7 篇分词转写稿。查询词 ["抗战", "北平"] 时的预期榜序见 test_walkthrough。
DOCS = {
    "MS-001": ["抗战", "北平", "胡同", "童年", "粮票"],
    "MS-002": ["抗战", "上海", "弄堂", "工厂", "夜校"],
    "MS-003": ["北平", "茶馆", "曲艺", "抗战", "胜利", "广播"],
    "MS-004": ["知青", "下乡", "北大荒", "麦田"],
    "MS-005": ["抗战", "西南联大", "昆明", "北平", "师生"],
    "MS-006": ["北平", "城墙", "骆驼", "运输队"],
    "MS-007": ["抗战", "伤兵", "医院", "护士"],
}
TERMS = ["抗战", "北平"]


def _publish(client, doc_id, tokens):
    return client.post("/manuscripts", json={"doc_id": doc_id, "tokens": tokens})


def test_publish_withdraw_republish(client, fresh_db):
    """在刊稿号拒绝重复刊发；撤稿后可同号重刊，版本号递增。"""
    r = _publish(client, "T-100", ["抗战", "重庆", "防空洞"])
    assert r.status_code == 201, r.text
    v1 = r.json()["version"]

    r = _publish(client, "T-100", ["抗战", "重庆"])
    assert r.status_code == 409  # 在刊稿号重复刊发被拒

    r = client.post("/manuscripts/T-100/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"

    r = client.post("/manuscripts/T-100/withdraw")
    assert r.status_code == 404  # 已撤稿，无在刊版本

    r = _publish(client, "T-100", ["抗战", "重庆", "重逢"])
    assert r.status_code == 201  # 撤稿后可同号重刊
    assert r.json()["version"] > v1  # 新版本序号

    r = client.post("/manuscripts/NO-SUCH/withdraw")
    assert r.status_code == 404


def test_walkthrough(client, fresh_db):
    """首查→撤稿→续页跳过并补足→重刊→旧游标不可见、新检索可见。"""
    for doc_id, tokens in DOCS.items():
        r = _publish(client, doc_id, tokens)
        assert r.status_code == 201, r.text

    # 重复刊发在刊稿号被拒
    assert _publish(client, "MS-001", ["抗战"]).status_code == 409

    # 全量探针：记录首查时刻的完整榜序与分数
    probe = client.post("/search", json={"terms": TERMS, "size": 100}).json()
    assert probe["next_cursor"] is None  # 5 篇命中 <= 页长，无续页
    order = [h["doc_id"] for h in probe["hits"]]
    assert order == ["MS-001", "MS-005", "MS-003", "MS-006", "MS-007", "MS-002"]
    # MS-001 与 MS-005 同分，按稿号升序
    assert probe["hits"][0]["score"] == probe["hits"][1]["score"]
    # 命中至少一个查询词：MS-004 不含查询词，不在榜上
    assert "MS-004" not in order
    scores = {h["doc_id"]: h["score"] for h in probe["hits"]}

    # query 字符串形式与 terms 等价
    probe2 = client.post("/search", json={"query": "抗战 北平", "size": 100}).json()
    assert [h["doc_id"] for h in probe2["hits"]] == order

    # 首查：页长 2
    first = client.post("/search", json={"terms": TERMS, "size": 2}).json()
    assert [h["doc_id"] for h in first["hits"]] == ["MS-001", "MS-005"]
    assert first["hits"][0]["score"] == scores["MS-001"]
    assert first["next_cursor"]

    # 首查与续页之间：馆方撤掉 MS-003（原本第 3 名）
    r = client.post("/manuscripts/MS-003/withdraw")
    assert r.status_code == 200

    # 续页：跳过已撤的 MS-003，从后续命中补足当页
    page2 = client.post("/search/next", json={"cursor": first["next_cursor"]}).json()
    assert [h["doc_id"] for h in page2["hits"]] == ["MS-006", "MS-007"]
    # 沿用首查的分数（与探针逐位一致）
    assert page2["hits"][0]["score"] == scores["MS-006"]
    assert page2["hits"][1]["score"] == scores["MS-007"]
    assert page2["next_cursor"]

    # 同号重刊 MS-003：新版本序号
    r = _publish(client, "MS-003", ["北平", "抗战", "重逢", "口述", "整理"])
    assert r.status_code == 201
    new_version = r.json()["version"]
    assert new_version > 7

    # 旧游标继续翻页：重刊版本不进入首查前已开的检索
    page3 = client.post("/search/next", json={"cursor": page2["next_cursor"]}).json()
    assert [h["doc_id"] for h in page3["hits"]] == ["MS-002"]
    assert "MS-003" not in [h["doc_id"] for h in page3["hits"]]
    assert page3["next_cursor"] is None

    # 重刊后新开的检索：能看到新版本
    fresh = client.post("/search", json={"terms": TERMS, "size": 100}).json()
    by_id = {h["doc_id"]: h for h in fresh["hits"]}
    assert by_id["MS-003"]["version"] == new_version


def test_continue_size_override_and_cursor_reuse(client, fresh_db):
    """续页可临时改页长；游标可重复使用（幂等）。"""
    for i in range(1, 5):
        _publish(client, f"S-{i}", ["抗战", f"词{i}"])
    first = client.post("/search", json={"terms": ["抗战"], "size": 2}).json()
    assert len(first["hits"]) == 2
    # 同一游标用两次，结果一致
    p2a = client.post("/search/next", json={"cursor": first["next_cursor"], "size": 1}).json()
    p2b = client.post("/search/next", json={"cursor": first["next_cursor"], "size": 1}).json()
    assert p2a["hits"] == p2b["hits"] and len(p2a["hits"]) == 1
    # 坏游标 / 未知游标
    assert client.post("/search/next", json={"cursor": "not-a-cursor"}).status_code == 400
    ghost = base64.urlsafe_b64encode(
        json.dumps({"sid": "no-such-search", "after": [1.0, "A"]}).encode()
    ).decode()
    assert client.post("/search/next", json={"cursor": ghost}).status_code == 404


def test_withdraw_does_not_wait_for_scoring(client, fresh_db):
    """撤稿提交不必等正在计分的请求结束；计分请求仍按首查时刻快照返回。"""
    _publish(client, "NB-200", ["抗战", "南京", "渡口"])
    _publish(client, "NB-201", ["抗战", "武汉", "轮渡"])

    started = threading.Event()
    outcome = {}

    def slow_search():
        started.set()
        r = client.post("/search", json={
            "terms": ["抗战"], "size": 100, "debug_delay_seconds": 3.0,
        })
        outcome["search"] = r

    t = threading.Thread(target=slow_search)
    t.start()
    assert started.wait(timeout=5)
    time.sleep(0.3)  # 确认对方进入计分阶段

    t0 = time.monotonic()
    r = client.post("/manuscripts/NB-200/withdraw")
    elapsed = time.monotonic() - t0
    t.join(timeout=10)

    assert r.status_code == 200
    assert elapsed < 2.0, f"撤稿耗时 {elapsed:.2f}s，被计分请求阻塞"
    assert outcome["search"].status_code == 200
    # 慢检索取的是撤稿前的快照，NB-200 仍在其结果中
    assert "NB-200" in [h["doc_id"] for h in outcome["search"].json()["hits"]]


# ------------------------------------------------------------------ 索引续页

def _score_only(page):
    return [(h["doc_id"], h["version"], h["score"]) for h in page["hits"]]


def test_index_continue_matches_scan_and_old_cursor(client, fresh_db):
    """索引续页与全量续页逐位一致；升级前签发的游标可直接使用索引续页。"""
    for doc_id, tokens in DOCS.items():
        assert _publish(client, doc_id, tokens).status_code == 201

    # 模拟升级前的旧库：清空索引，游标签发时索引尚未就绪
    conn = main._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM term_index")
        conn.execute("UPDATE index_meta SET indexed_seq = 0 WHERE id = 1")
        conn.execute("COMMIT")
    finally:
        conn.close()

    first = client.post("/search", json={"terms": TERMS, "size": 2}).json()
    assert first["next_cursor"]
    # 索引尚未补齐：续页走原路径
    p2 = client.post("/search/next", json={"cursor": first["next_cursor"],
                                           "debug": True}).json()
    assert p2["read_scope"]["path"] == "scan"
    assert p2["read_scope"]["docs_scored"] == 7  # 全量路径读取快照内全部在刊稿
    assert [h["doc_id"] for h in p2["hits"]] == ["MS-003", "MS-006"]

    # 首查之后撤稿 + 同号重刊，再补齐索引；补齐期间的刊撤不改变旧游标规则
    assert client.post("/manuscripts/MS-003/withdraw").status_code == 200
    r = _publish(client, "MS-003", ["北平", "抗战", "重逢", "口述", "整理"])
    assert r.status_code == 201
    new_version = r.json()["version"]

    # 同一状态下，先强制走原路径取一份参考结果
    def scan_mode(flag):
        main.INDEX_ENABLED = flag

    scan_mode(False)
    try:
        scan_ref = client.post("/search/next", json={"cursor": first["next_cursor"],
                                                     "debug": True}).json()
    finally:
        scan_mode(True)
    assert scan_ref["read_scope"]["path"] == "scan"

    _full_backfill()

    # 升级前签发（索引未就绪时）的游标，现在直接走索引续页
    p2b = client.post("/search/next", json={"cursor": first["next_cursor"],
                                            "debug": True}).json()
    assert p2b["read_scope"]["path"] == "index"
    # 只读取候选版本（含查询词的版本），而不是整份在刊快照
    assert p2b["read_scope"]["docs_scored"] < scan_ref["read_scope"]["docs_scored"]
    assert p2b["read_scope"]["docs_scored"] >= len(p2b["hits"])
    # 分数、同分顺序、剩余命中数、当页内容与全量路径一致
    assert _score_only(p2b) == _score_only(scan_ref)
    assert p2b["remaining_hits"] == scan_ref["remaining_hits"]
    assert [h["doc_id"] for h in p2b["hits"]] == ["MS-006", "MS-007"]
    assert "MS-003" not in [h["doc_id"] for h in p2b["hits"]]  # 已撤/重刊都不进旧榜

    # 后续页同样一致
    scan_mode(False)
    try:
        p3_scan = client.post("/search/next", json={"cursor": p2b["next_cursor"],
                                                    "debug": True}).json()
    finally:
        scan_mode(True)
    p3_idx = client.post("/search/next", json={"cursor": p2b["next_cursor"],
                                               "debug": True}).json()
    assert p3_idx["read_scope"]["path"] == "index"
    assert _score_only(p3_idx) == _score_only(p3_scan)
    assert p3_idx["remaining_hits"] == p3_scan["remaining_hits"]
    assert new_version not in [h["version"] for h in p3_idx["hits"]]

    # 重刊后的新检索能看到新版本；其游标从第一页起也走索引
    fresh = client.post("/search", json={"terms": TERMS, "size": 2}).json()
    assert fresh["snapshot_seq"] == new_version
    fn = client.post("/search/next", json={"cursor": fresh["next_cursor"],
                                           "debug": True}).json()
    assert fn["read_scope"]["path"] == "index"
    assert new_version in [h["version"] for h in
                           client.post("/search", json={"terms": TERMS, "size": 100})
                           .json()["hits"]]


def test_index_disabled_falls_back(client, fresh_db, monkeypatch):
    """INDEX_ENABLED=0 时始终走原路径（索引未就绪的回退语义）。"""
    for doc_id, tokens in DOCS.items():
        assert _publish(client, doc_id, tokens).status_code == 201
    first = client.post("/search", json={"terms": TERMS, "size": 2}).json()
    monkeypatch.setattr(main, "INDEX_ENABLED", False)
    nxt = client.post("/search/next", json={"cursor": first["next_cursor"],
                                            "debug": True}).json()
    assert nxt["read_scope"]["path"] == "scan"
    assert [h["doc_id"] for h in nxt["hits"]] == ["MS-003", "MS-006"]


def test_index_switches_exactly_at_snapshot_boundary(client, fresh_db):
    """就绪边界：补到 snapshot_seq-1 仍走原路径，补到 snapshot_seq 才切索引。"""
    for doc_id, tokens in DOCS.items():
        assert _publish(client, doc_id, tokens).status_code == 201
    first = client.post("/search", json={"terms": TERMS, "size": 2}).json()
    snap = first["snapshot_seq"]
    cursor = first["next_cursor"]

    conn = main._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM term_index")
        conn.execute("UPDATE index_meta SET indexed_seq = 0 WHERE id = 1")
        conn.execute("COMMIT")
    finally:
        conn.close()

    def advance_to(target):
        conn = main._connect()
        try:
            with main._BACKFILL_LOCK:
                while main._index_meta(conn)["indexed_seq"] < target:
                    _, err = main._backfill_once(conn, batch_size=1)
                    assert err is None
            return main._index_meta(conn)["indexed_seq"]
        finally:
            conn.close()

    assert advance_to(snap - 1) == snap - 1
    before = client.post("/search/next", json={"cursor": cursor, "debug": True}).json()
    assert before["read_scope"]["path"] == "scan"

    assert advance_to(snap) == snap
    after = client.post("/search/next", json={"cursor": cursor, "debug": True}).json()
    assert after["read_scope"]["path"] == "index"
    assert _score_only(after) == _score_only(before)
    assert after["remaining_hits"] == before["remaining_hits"]


def test_backfill_resumes_after_error_and_restart(client, fresh_db, monkeypatch):
    """旧库分批补齐：出错保留已提交进度，续页仍可用，重启后接着补。"""
    monkeypatch.setattr(main, "INDEX_ENABLED", False)
    for doc_id, tokens in DOCS.items():
        assert _publish(client, doc_id, tokens).status_code == 201
    # 模拟旧库：索引全空
    conn = main._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM term_index")
        conn.execute("UPDATE index_meta SET indexed_seq = 0, last_error = NULL WHERE id = 1")
        conn.execute("COMMIT")
    finally:
        conn.close()

    first = client.post("/search", json={"terms": TERMS, "size": 2}).json()
    # 补齐期间撤稿/重刊照常
    assert client.post("/manuscripts/MS-003/withdraw").status_code == 200
    r = _publish(client, "MS-003", ["北平", "抗战", "重逢"])
    assert r.status_code == 201
    assert client.post("/search/next", json={"cursor": first["next_cursor"],
                                             "debug": True}).json()[
        "read_scope"]["path"] == "scan"

    monkeypatch.setattr(main, "INDEX_ENABLED", True)
    # 一批一批补，中途人为失败（模拟重启/异常），已提交的进度保留
    conn = main._connect()
    try:
        with main._BACKFILL_LOCK:
            ins, err = main._backfill_once(conn, batch_size=2)
        assert err is None and ins > 0
        meta = main._index_meta(conn)
        assert meta["indexed_seq"] == 2
    finally:
        conn.close()

    orig = main._index_entry
    calls = {"n": 0}

    def flaky(conn, version, tf):
        calls["n"] += 1
        if version == 4:
            raise RuntimeError("boom")
        return orig(conn, version, tf)

    monkeypatch.setattr(main, "_index_entry", flaky)
    conn = main._connect()
    try:
        with main._BACKFILL_LOCK:
            ins, err = main._backfill_once(conn, batch_size=2)
        assert ins == 0 and "boom" in err
        meta = main._index_meta(conn)
        assert meta["indexed_seq"] == 2  # 失败批次回滚，进度不倒退也不跳洞
        assert "boom" in meta["last_error"]
    finally:
        conn.close()
    monkeypatch.setattr(main, "_index_entry", orig)

    # 出错后索引未就绪 -> 续页仍走原路径，且结果正确
    during = client.post("/search/next", json={"cursor": first["next_cursor"],
                                               "debug": True}).json()
    assert during["read_scope"]["path"] == "scan"
    assert [h["doc_id"] for h in during["hits"]] == ["MS-006", "MS-007"]

    # 重新启动补齐：从 indexed_seq=2 接着补，不重扫已完成的版本
    status = client.get("/index/status").json()
    assert status["indexed_seq"] == 2 and status["latest_version"] == 8
    resumed = client.post("/index/backfill?batches=100").json()
    assert resumed["backfill_error"] is None
    assert resumed["indexed_seq"] == 8
    status = client.get("/index/status").json()
    assert status["ready"] is True and status["backfill_error"] is None

    # 切到索引后，补齐期间的撤稿仍按旧游标规则生效（MS-003 旧版被撤、重刊版不入旧榜）
    after = client.post("/search/next", json={"cursor": first["next_cursor"],
                                              "debug": True}).json()
    assert after["read_scope"]["path"] == "index"
    assert _score_only(after) == _score_only(during)
    assert after["remaining_hits"] == during["remaining_hits"]
    assert [h["doc_id"] for h in after["hits"]] == ["MS-006", "MS-007"]


def test_index_ready_stable_state_uses_index(client, fresh_db):
    """索引就绪后，续页只读命中查询词的候选版本，不随在刊总量线性增长。"""
    for i in range(1, 6):
        assert _publish(client, f"R-{i}", ["抗战", f"词{i}"]).status_code == 201
    # 20 篇与查询无关的稿件：全量路径会全部读出，索引路径应完全跳过
    for i in range(20):
        assert _publish(client, f"N-{i:02d}", ["知青", "下乡", f"麦田{i}"]).status_code == 201
    _full_backfill()
    assert client.get("/index/status").json()["ready"] is True

    # 索引就绪时新刊发：条目随写事务落库；高水位由补齐逻辑推进
    assert _publish(client, "R-6", ["抗战", "词6"]).status_code == 201
    _full_backfill()
    assert client.get("/index/status").json()["ready"] is True

    first = client.post("/search", json={"terms": ["抗战"], "size": 2}).json()
    assert first["total_hits"] == 6
    nxt = client.post("/search/next", json={"cursor": first["next_cursor"],
                                            "debug": True}).json()
    assert nxt["read_scope"]["path"] == "index"
    assert nxt["read_scope"]["index_candidates"] == 6   # 含"抗战"的版本
    assert nxt["read_scope"]["docs_scored"] == 6        # 只读这 6 个版本
    assert nxt["read_scope"]["snapshot_published"] == 26  # 首查时在刊 26 篇
    assert [h["doc_id"] for h in nxt["hits"]] == ["R-3", "R-4"]


def test_publish_writes_index_entries(client, fresh_db):
    """刊发即在同一写事务写索引；空 token 不建索引项。"""
    r = client.post("/manuscripts", json={"doc_id": "E-1",
                                          "tokens": ["", "抗战", " ,", "抗战"]})
    assert r.status_code == 201
    version = r.json()["version"]
    conn = main._connect()
    try:
        terms = sorted(row["term"] for row in conn.execute(
            "SELECT term FROM term_index WHERE version = ?", (version,)))
    finally:
        conn.close()
    assert terms == [" ,", "抗战"]  # 只有空串被过滤，其余按原样入索引


def test_index_and_scan_equivalent_randomized(client, fresh_db):
    """随机语料 + 随机刊撤/重刊：索引续页与全量续页逐页逐位一致。"""
    import random
    rng = random.Random(20261123)
    vocab = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]

    def rand_tokens():
        return [rng.choice(vocab) for _ in range(rng.randint(1, 9))]

    for i in range(40):
        assert _publish(client, f"Z-{i:03d}", rand_tokens()).status_code == 201
    _full_backfill()

    for _round in range(5):
        terms = rng.sample(vocab, rng.randint(1, 3))
        first = client.post("/search", json={"terms": terms, "size": 3}).json()
        cursors = [first["next_cursor"]]
        scan_pages, idx_pages = [], []
        while cursors and cursors[-1]:
            cur = cursors[-1]
            main.INDEX_ENABLED = False
            try:
                scan = client.post("/search/next", json={"cursor": cur}).json()
            finally:
                main.INDEX_ENABLED = True
            idx = client.post("/search/next", json={"cursor": cur, "debug": True}).json()
            assert idx["read_scope"]["path"] == "index"
            scan_pages.append(scan)
            idx_pages.append(idx)
            cursors.append(idx["next_cursor"])

            # 翻页之间随机撤稿 / 同号重刊
            if rng.random() < 0.5:
                live = [m["doc_id"] for m in client.get("/manuscripts").json()["manuscripts"]
                        if m["status"] == "published"]
                victim = rng.choice(live)
                assert client.post(f"/manuscripts/{victim}/withdraw").status_code == 200
                if rng.random() < 0.6:
                    _publish(client, victim, rand_tokens())
                _full_backfill()

        assert len(scan_pages) == len(idx_pages)
        for s, i in zip(scan_pages, idx_pages):
            assert _score_only(i) == _score_only(s)
            assert i["remaining_hits"] == s["remaining_hits"]
            assert (i["next_cursor"] is None) == (s["next_cursor"] is None)
