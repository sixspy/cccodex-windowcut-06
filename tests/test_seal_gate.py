"""印记门禁（甲路径）失败测：缺印记/错印记必须整笔拒绝，带对与台账同库提交。

夹具复现：关窗后 windows.seal_token 已签发，但 materialize_svc 不核印——
缺印记或错印记仍物化成功（本文件红测运行输出即复现证据）。
正确性口径只走甲路径（印记门禁拒绝），落 seal_registry 册，当天禁止改口。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "seal.db")
    init_db()
    yield


def _auth(c: TestClient, username="admin", password="123456") -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _closed_window_with_seal(c: TestClient, h: dict, wid: int = 2) -> str:
    """夹具：入账一笔 → 关窗 → 拿到本窗最新封口印记。"""
    c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "SEAL-1",
            "event_ts": "2026-11-02T11:00:00",
            "amount": 7.5,
        },
    )
    r = c.post(f"/api/windows/{wid}/close", headers=h)
    assert r.status_code == 200
    seal = r.json().get("seal_token")
    assert seal
    return seal


def _mat_rows(c: TestClient, h: dict) -> list:
    return c.get("/api/materializations", headers=h).json()["items"]


def _audit_items(c: TestClient, h: dict) -> list:
    return c.get("/api/audit?limit=200", headers=h).json()["items"]


def test_missing_seal_rejected_zero_growth():
    c = TestClient(app)
    h = _auth(c)
    _closed_window_with_seal(c, h)
    before = len(_mat_rows(c, h))
    r = c.post("/api/windows/2/materialize", headers=h, json={})
    assert r.status_code == 409  # 缺印记必须整笔拒绝
    assert len(_mat_rows(c, h)) == before  # materializations 零增
    assert any(a["action"] == "印记拒绝" for a in _audit_items(c, h))


def test_wrong_seal_rejected_zero_growth():
    c = TestClient(app)
    h = _auth(c)
    _closed_window_with_seal(c, h)
    before = len(_mat_rows(c, h))
    r = c.post(
        "/api/windows/2/materialize", headers=h, json={"seal_token": "seal-伪造-0"}
    )
    assert r.status_code == 409  # 错印记必须整笔拒绝
    assert len(_mat_rows(c, h)) == before
    assert any(a["action"] == "印记拒绝" for a in _audit_items(c, h))


def test_correct_seal_commits_receipt_same_source():
    c = TestClient(app)
    h = _auth(c)
    seal = _closed_window_with_seal(c, h)
    before = len(_mat_rows(c, h))
    r = c.post("/api/windows/2/materialize", headers=h, json={"seal_token": seal})
    assert r.status_code == 200
    receipt = r.json()
    assert receipt["seal_token"] == seal  # 物化回执与印记同源
    rows = _mat_rows(c, h)
    assert len(rows) == before + 1
    latest = [m for m in rows if m["window_id"] == 2][0]
    assert latest["seal_token"] == seal  # 台账行与窗口印记同库同源
    recon = c.get("/api/reconcile/2", headers=h).json()
    assert recon["seal_aligned"] is True  # 对账页同态
    assert recon["aligned"] is True
    ok = [a for a in _audit_items(c, h) if a["action"] == "materialize_ok"]
    assert ok and seal in (ok[0]["payload"] or "")  # 审计流水同一印记


def test_legacy_window_without_seal_rejected():
    c = TestClient(app)
    h = _auth(c)
    before = len(_mat_rows(c, h))
    r = c.post("/api/windows/1/materialize", headers=h, json={"seal_token": "x"})
    assert r.status_code == 409  # 窗口无印记同样整笔拒绝
    assert len(_mat_rows(c, h)) == before


def test_seal_registry_records_path_jia():
    c = TestClient(app)
    h = _auth(c)
    r = c.get("/api/seal-registry", headers=h)
    assert r.status_code == 200
    reg = r.json()["registry"]
    assert reg["path"] == "甲"  # 选定甲路径（印记门禁拒绝）落册
    assert reg["decided_on"]  # 落册日期在案，当天禁止改口


def test_registry_deviation_refuses_with_pianle():
    import db

    c = TestClient(app)
    h = _auth(c)
    seal = _closed_window_with_seal(c, h)
    with db.connect() as conn:  # 模拟改口：册被改成乙路径
        conn.execute("UPDATE seal_registry SET path='乙' WHERE id=1")
        conn.commit()
    before = len(_mat_rows(c, h))
    r = c.post("/api/windows/2/materialize", headers=h, json={"seal_token": seal})
    assert r.status_code == 409
    assert "印记偏了" in r.json()["detail"]  # 偏离封口印记册拒写
    assert len(_mat_rows(c, h)) == before
    assert any(a["action"] == "印记偏了" for a in _audit_items(c, h))


def test_registry_same_day_redeclaration_refused():
    import db
    from materialize_svc import declare_seal_path

    with db.connect() as conn:
        assert declare_seal_path(conn, "admin", "甲") is True  # 同口径幂等
        assert declare_seal_path(conn, "admin", "乙") is False  # 当天禁止改口
        conn.commit()
        row = conn.execute("SELECT path FROM seal_registry WHERE id=1").fetchone()
        assert row["path"] == "甲"  # 册不被翻动
