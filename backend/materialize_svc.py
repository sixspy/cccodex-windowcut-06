"""关窗后物化。空桩预留：分区失败回滚 vs 保留成功分区。

正确性口径（已落 seal_registry 册，当天禁止改口，两边不得混用）：
甲路径＝印记门禁拒绝——请求未带匹配 seal_token 则整笔拒绝、materializations
零增；带对则与台账行同库提交，回执与印记同源。偏离册拒写并记「印记偏了」。
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import SEAL_PATH, SEAL_PATH_NOTE, connect, write_audit
from locks import release, require_lock
from models import MaterializeIn

router = APIRouter(tags=["materialize"])


def declare_seal_path(conn, actor: str, path: str) -> bool:
    """封口印记册：选定即落册；当天禁止改口，偏离拒写并记「印记偏了」。"""
    row = conn.execute("SELECT * FROM seal_registry WHERE id=1").fetchone()
    if row is None:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO seal_registry(id,path,decided_on,created_at) VALUES (1,?,?,?)",
            (path, date.today().isoformat(), now),
        )
        return True
    if row["path"] == path:
        return True
    write_audit(
        conn,
        actor,
        "印记偏了",
        "seal_registry",
        {
            "registered": row["path"],
            "attempted": path,
            "decided_on": row["decided_on"],
        },
    )
    return False


@router.get("/api/seal-registry")
def seal_registry(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        declare_seal_path(conn, "system", SEAL_PATH)
        conn.commit()
        row = conn.execute("SELECT * FROM seal_registry WHERE id=1").fetchone()
        return {"registry": dict(row), "path_note": SEAL_PATH_NOTE}


@router.post("/api/windows/{wid}/materialize")
def materialize(
    wid: int,
    authorization: AuthHeader = None,
    body: MaterializeIn = MaterializeIn(),
):
    user = require_user(authorization)
    lock_key = f"materialize:{wid}"
    with connect() as conn:
        require_lock(conn, lock_key, user)  # 两人同窗：后到只记争用
        conn.commit()  # 物化锁落定，他人可见
        try:
            if not declare_seal_path(conn, user, SEAL_PATH):
                conn.commit()  # 「印记偏了」审计落库
                raise HTTPException(409, "印记偏了：偏离封口印记册，拒写")
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            if w["status"] != "已关闭":
                raise HTTPException(409, "未关窗不能物化")
            expected = w["seal_token"]
            presented = body.seal_token
            if not expected or not presented or presented != expected:
                reason = (
                    "窗口无印记" if not expected else ("缺印记" if not presented else "错印记")
                )
                write_audit(
                    conn,
                    user,
                    "印记拒绝",
                    str(wid),
                    {"reason": reason, "presented": presented, "expected": expected},
                )
                conn.commit()  # 拒痕落库；materializations 零增
                raise HTTPException(409, f"印记门禁拒绝（{reason}）：整笔拒绝")
            now = datetime.now().isoformat(timespec="seconds")
            rows = conn.execute(
                """SELECT COUNT(*) c, COALESCE(SUM(amount),0) s
                   FROM events WHERE window_id=? AND disposition='窗内'""",
                (wid,),
            ).fetchone()
            partition = body.partition_key or f"{w['name']}-g{w['generation']}"
            if body.fail_simulate:
                write_audit(
                    conn,
                    user,
                    "materialize_fail_sim",
                    str(wid),
                    {"partition": partition},
                )
                conn.commit()
                raise HTTPException(500, "模拟物化失败（夹具用）")
            conn.execute(
                """INSERT INTO materializations(window_id,status,row_count,total_amount,partition_key,seal_token,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (wid, "成功", rows["c"], rows["s"], partition, expected, now),
            )
            write_audit(
                conn,
                user,
                "materialize_ok",
                str(wid),
                {
                    "row_count": rows["c"],
                    "total_amount": rows["s"],
                    "partition": partition,
                    "seal_token": expected,
                },
            )
            conn.commit()  # 台账行与印记同库提交
            return {
                "row_count": rows["c"],
                "total_amount": rows["s"],
                "partition_key": partition,
                "seal_token": expected,  # 回执与印记同源
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/materializations")
def list_mats(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.*, w.name AS window_name
               FROM materializations m
               JOIN windows w ON w.id=m.window_id
               ORDER BY m.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/materializations/partition-stub")
def partition_stub(authorization: AuthHeader = None):
    """空桩模块：部分物化失败回滚策略待题面落地。"""
    user = require_user(authorization)
    with connect() as conn:
        write_audit(
            conn,
            user,
            "partition_mat_stub",
            None,
            {
                "paths": [
                    "整窗回滚",
                    "保留成功分区",
                ],
                "note": "正确性口径须择一，母仓不实现",
            },
        )
        conn.commit()
    return {"ok": True, "stub": True, "module": "partition_mat"}
