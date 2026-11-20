"""双人同时对同一 window_id 物化：须占物化锁，后到只记争用。

夹具复现：物化锁在事务内即插即删、持锁期间从未对他人可见，
两人同时物化同窗不互斥（本文件红测运行输出即复现证据）。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import materialize_svc
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "contention.db")
    init_db()
    yield


def _auth(c: TestClient, username: str, password: str) -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_concurrent_materialize_same_window_latecomer_only_contends(monkeypatch):
    c_admin = TestClient(app)
    c_closer = TestClient(app)
    h_admin = _auth(c_admin, "admin", "123456")
    h_closer = _auth(c_closer, "closer", "close123")
    seal = c_admin.post("/api/windows/2/close", headers=h_admin).json()["seal_token"]
    mats_before = len(c_admin.get("/api/materializations", headers=h_admin).json()["items"])

    a_acquired = threading.Event()
    b_attempted = threading.Event()
    orig_require_lock = materialize_svc.require_lock

    def gated_require_lock(conn, lock_key, holder):
        """把两人压进同一争用窗口：A 占锁后等 B 抢锁结论再放行。"""
        if lock_key != "materialize:2":
            return orig_require_lock(conn, lock_key, holder)
        if holder == "admin":
            orig_require_lock(conn, lock_key, holder)
            a_acquired.set()
            b_attempted.wait(1.5)  # 持锁等后到者撞锁
        else:
            a_acquired.wait(5)  # 后到者确保在持锁窗口内发起
            try:
                orig_require_lock(conn, lock_key, holder)
            finally:
                b_attempted.set()

    monkeypatch.setattr(materialize_svc, "require_lock", gated_require_lock)

    results: dict[str, int] = {}

    def run(tag: str, client: TestClient, headers: dict) -> None:
        r = client.post(
            "/api/windows/2/materialize", headers=headers, json={"seal_token": seal}
        )
        results[tag] = r.status_code

    ta = threading.Thread(target=run, args=("admin", c_admin, h_admin))
    tb = threading.Thread(target=run, args=("closer", c_closer, h_closer))
    ta.start()
    tb.start()
    ta.join(15)
    tb.join(15)
    assert not ta.is_alive() and not tb.is_alive()

    assert sorted(results.values()) == [200, 409]  # 一人落库，后到只记争用
    mats = c_admin.get("/api/materializations", headers=h_admin).json()["items"]
    assert len(mats) - mats_before == 1  # 同窗同时物化只落一笔
    cont = c_admin.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == "materialize:2" and x["actor"] == "closer" for x in cont
    )  # 争用登记在册
