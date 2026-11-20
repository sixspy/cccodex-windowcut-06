"""窗口锁与争用登记（公共并发面，题面争用句应对得上）。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import connect, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def _contention(conn, lock_key: str, holder: str, holder_now: str, now: str) -> None:
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={holder_now}", now),
    )


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    """占锁原子化：INSERT 先行；busy_timeout 压 0，撞锁即判争用。

    锁行须由调用方及时 commit 方对他人可见。撞已提交锁行（主键冲突）或
    撞持锁方未放行的写锁（database is locked）都立即判争用——后到者
    只记争用，绝不等锁滑行进入临界区。

    注意：失败语句的残留事务会持锁（RESERVED/SHARED）碍了持锁方的提交，
    故失败路径先 rollback 清场再查持有者、记争用——try_acquire 须为
    调用方连接上的首个写操作。
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("PRAGMA busy_timeout=0")
    try:
        conn.execute(
            "INSERT INTO window_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now),
        )
        acquired = True
    except (sqlite3.IntegrityError, sqlite3.OperationalError):
        acquired = False
    finally:
        conn.execute("PRAGMA busy_timeout=30000")
    if acquired:
        return True
    conn.rollback()  # 清掉失败语句的残留事务，避免自持锁与持锁方互等成死锁
    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is not None and row["holder"] == holder:
        return True
    _contention(conn, lock_key, holder, row["holder"] if row else "未知", now)
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM window_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        conn.commit()  # 争用登记落库：后到只记争用，不写业务
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire" if ok else "lock_contention",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：后到只记争用")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
    return {"ok": True}
