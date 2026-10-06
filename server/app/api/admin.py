"""后台管理 API：配置 / 凭证 / 物品 / 采集 / 调度"""
from datetime import datetime, timedelta
from typing import Dict, List

from fastapi import APIRouter, Depends, Header, HTTPException

from ..core import config
from ..core.database import query, query_one, execute, insert_return_id, get_engine
from ..core.security import (
    hash_password, verify_password, create_access_token, decode_access_token,
    mask_token, encrypt_token,
)
from ..collector import ams_client, scheduler, qrlogin
from ..engine import strategy, demo
from ..engine.seed_ammo import AMMO_SEED

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- 认证 ----------------
def require_admin(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    payload = decode_access_token(authorization[7:])
    if not payload or payload.get("role") != "admin":
        raise HTTPException(401, "登录已失效")
    return payload


@router.post("/login")
def login(payload: dict):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    row = query_one("SELECT * FROM admin_user WHERE username = :u", {"u": username})
    if not row:
        # 首次使用：自动创建默认管理员
        cnt = query_one("SELECT COUNT(*) AS c FROM admin_user")["c"]
        if cnt == 0 and username and password:
            insert_return_id(
                """INSERT INTO admin_user (username, password_hash, role, created_at)
                   VALUES (:u, :p, 'admin', NOW())""",
                {"u": username, "p": hash_password(password)},
            )
            row = query_one("SELECT * FROM admin_user WHERE username = :u", {"u": username})
        else:
            raise HTTPException(401, "用户名或密码错误")
    if not verify_password(password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    execute("UPDATE admin_user SET last_login_at = NOW() WHERE id = :i", {"i": row["id"]})
    token = create_access_token({"uid": row["id"], "role": "admin",
                                 "username": row["username"]})
    return {"ok": True, "token": token, "username": row["username"]}


@router.get("/profile")
def profile(admin=Depends(require_admin)):
    return {"ok": True, "username": admin.get("username")}


@router.post("/password")
def change_password(payload: dict, admin=Depends(require_admin)):
    pwd = str(payload.get("password", "") or "")
    if len(pwd) < 6:
        raise HTTPException(400, "密码至少 6 位，且不能为空")
    if len(pwd) > 64:
        raise HTTPException(400, "密码最长 64 位")
    execute(
        "UPDATE admin_user SET password_hash = :p WHERE id = :i",
        {"p": hash_password(pwd), "i": admin["uid"]},
    )
    return {"ok": True, "msg": "密码已修改"}


# ---------------- 系统配置 ----------------
@router.get("/config")
def get_config(admin=Depends(require_admin)):
    rows = query("SELECT config_key, config_value, config_type, description,"
                 " updated_at FROM sys_config ORDER BY config_key")
    return {"ok": True, "data": rows}


@router.put("/config")
def update_config(payload: dict, admin=Depends(require_admin)):
    """payload: {"fee_deposit_rate": 3, "strategy_min_gain": 15, ...}"""
    for k, v in payload.items():
        if k in ("_", "token"):
            continue
        config.set_value(k, v)
    config.reload()
    if "collect_interval" in payload or "collect_enabled" in payload:
        scheduler.restart_scheduler()
    return {"ok": True, "updated": list(payload.keys())}


@router.post("/config/reset")
def reset_config(admin=Depends(require_admin)):
    config.reload()
    return {"ok": True}


# ---------------- 凭证管理 ----------------
@router.get("/credentials")
def credentials(admin=Depends(require_admin)):
    rows = ams_client.list_credentials()
    for r in rows:
        r.pop("access_token", None)
        r["openid"] = mask_token(r.get("openid") or "")
    return {"ok": True, "data": rows}


@router.post("/credentials")
def add_cred(payload: dict, admin=Depends(require_admin)):
    openid = str(payload.get("openid", "")).strip()
    token = str(payload.get("access_token", "")).strip()
    if not openid or not token:
        raise HTTPException(400, "openid 和 access_token 不能为空")
    cid = insert_return_id(
        """INSERT INTO credential (name, openid, access_token, acctype, appid,
           status, remark, created_at) VALUES (:n, :o, :t, :a, :ap, 'active', :r, NOW())""",
        {
            "n": payload.get("name") or f"凭证-{datetime.now():%m%d%H%M}",
            "o": openid,
            # 必须加密：读取侧一律走 decrypt_token，存明文会导致解密得到空串
            "t": encrypt_token(token),
            "a": payload.get("acctype") or "qc",
            "ap": payload.get("appid") or config.get_str("ams_appid", "101491592"),
            "r": payload.get("remark") or "",
        },
    )
    return {"ok": True, "id": cid}


@router.put("/credentials/{cid}")
def update_cred(cid: int, payload: dict, admin=Depends(require_admin)):
    payload.pop("id", None)
    ams_client.update_credential(cid, **payload)
    return {"ok": True}


@router.delete("/credentials/{cid}")
def del_cred(cid: int, admin=Depends(require_admin)):
    ams_client.delete_credential(cid)
    return {"ok": True}


@router.post("/credentials/{cid}/test")
def test_cred(cid: int, payload: dict = None, admin=Depends(require_admin)):
    payload = payload or {}
    return ams_client.test_credential(
        cid, payload.get("propid"), payload.get("stat_hour")
    )


@router.post("/credentials/{cid}/toggle")
def toggle_cred(cid: int, admin=Depends(require_admin)):
    row = query_one("SELECT status FROM credential WHERE id = :i", {"i": cid})
    if not row:
        raise HTTPException(404, "凭证不存在")
    new = "disabled" if row["status"] == "active" else "active"
    execute("UPDATE credential SET status = :s WHERE id = :i", {"s": new, "i": cid})
    return {"ok": True, "status": new}


# ---------------- 扫码获取凭证 ----------------
# 纯 HTTP 走腾讯官方 PT 登录二维码 + QQ 互联 OAuth，不经过任何第三方平台。
# 二维码与 token 只存在于服务端内存，不落库、不写日志。
@router.post("/credential/qr/start")
def qr_start(admin=Depends(require_admin)):
    s = qrlogin.create_session()
    return s._view()


@router.get("/credential/qr/poll")
def qr_poll(sid: str, admin=Depends(require_admin)):
    if not sid:
        raise HTTPException(400, "缺少 sid")
    return qrlogin.poll_session(sid)


@router.post("/credential/qr/cancel")
def qr_cancel(payload: dict = None, admin=Depends(require_admin)):
    sid = str((payload or {}).get("sid", ""))
    if sid:
        qrlogin.cancel_session(sid)
    return {"ok": True}


# ---------------- 物品管理 ----------------
@router.get("/items")
def items(keyword: str = "", enabled: int = -1, admin=Depends(require_admin)):
    sql = """SELECT i.*, (SELECT COUNT(*) FROM price_hourly p WHERE p.propid = i.propid)
             AS tick_count FROM items i WHERE 1=1"""
    params = {}
    if keyword:
        sql += " AND (i.name LIKE :k OR i.caliber LIKE :k OR i.grade LIKE :k)"
        params["k"] = f"%{keyword}%"
    if enabled >= 0:
        sql += " AND i.watch_enabled = :e"
        params["e"] = enabled
    sql += " ORDER BY i.sort_order, i.name"
    return {"ok": True, "data": query(sql, params)}


@router.post("/items")
def add_item(payload: dict, admin=Depends(require_admin)):
    propid = str(payload.get("propid", "")).strip()
    name = str(payload.get("name", "")).strip()
    if not propid or not name:
        raise HTTPException(400, "propid 和 name 不能为空")
    exists = query_one("SELECT propid FROM items WHERE propid = :p", {"p": propid})
    if exists:
        raise HTTPException(400, "该 propid 已存在")
    execute(
        """INSERT INTO items (propid, name, caliber, grade, second_class,
           watch_enabled, sort_order, created_at, updated_at)
           VALUES (:p, :n, :c, :g, :s, :w, :o, NOW(), NOW())""",
        {
            "p": propid, "n": name,
            "c": payload.get("caliber") or "",
            "g": payload.get("grade") or "",
            "s": payload.get("second_class") or "子弹",
            "w": int(payload.get("watch_enabled", 1)),
            "o": int(payload.get("sort_order", 0)),
        },
    )
    return {"ok": True}


@router.put("/items/{propid}")
def update_item(propid: str, payload: dict, admin=Depends(require_admin)):
    allowed = {"name", "caliber", "grade", "second_class", "watch_enabled", "sort_order"}
    sets, params = [], {"p": propid}
    for k, v in payload.items():
        if k in allowed:
            sets.append(f"{k} = :{k}")
            params[k] = v
    if not sets:
        return {"ok": True}
    sets.append("updated_at = NOW()")
    execute(f"UPDATE items SET {', '.join(sets)} WHERE propid = :p", params)
    return {"ok": True}


@router.put("/items/{propid}/propid")
def change_propid(propid: str, payload: dict, admin=Depends(require_admin)):
    """修改物品ID（会级联更新所有关联表）"""
    new_id = str(payload.get("new_propid", "")).strip()
    if not new_id:
        raise HTTPException(400, "新 propid 不能为空")
    if new_id == propid:
        return {"ok": True}
    exists = query_one("SELECT propid FROM items WHERE propid = :p", {"p": new_id})
    if exists:
        raise HTTPException(400, "新 propid 已存在")
    eng = get_engine()
    with eng.begin() as conn:
        from sqlalchemy import text as _t
        conn.execute(_t("UPDATE items SET propid = :n WHERE propid = :o"),
                     {"n": new_id, "o": propid})
        for tbl in ("price_hourly", "baseline", "signal_record", "holding", "user_watch"):
            conn.execute(_t(f"UPDATE {tbl} SET propid = :n WHERE propid = :o"),
                         {"n": new_id, "o": propid})
    return {"ok": True, "new_propid": new_id}


@router.delete("/items/{propid}")
def del_item(propid: str, admin=Depends(require_admin)):
    eng = get_engine()
    with eng.begin() as conn:
        from sqlalchemy import text as _t
        for tbl in ("price_hourly", "baseline", "signal_record", "holding", "user_watch"):
            conn.execute(_t(f"DELETE FROM {tbl} WHERE propid = :o"), {"o": propid})
        conn.execute(_t("DELETE FROM items WHERE propid = :o"), {"o": propid})
    return {"ok": True}


@router.post("/items/seed")
def seed_items(admin=Depends(require_admin)):
    """导入弹药种子数据（propid 为占位，需后台补充真实ID）"""
    added = 0
    for idx, row in enumerate(AMMO_SEED, 1):
        name, cal, grade = row[0], row[1], row[2]
        base_price = row[3] if len(row) > 3 else 0
        propid = f"AUTO{idx:03d}"
        # 已存在则只补基准价
        exists = query_one("SELECT propid FROM items WHERE name = :n", {"n": name})
        if exists:
            execute("UPDATE items SET base_price = :b WHERE name = :n",
                    {"b": base_price, "n": name})
            continue
        n = execute(
            """INSERT INTO items (propid, name, caliber, grade, base_price, second_class,
               watch_enabled, sort_order, created_at, updated_at)
               VALUES (:p, :n, :c, :g, :b, '子弹', 1, :o, NOW(), NOW())""",
            {"p": propid, "n": name, "c": cal, "g": grade,
             "b": base_price, "o": idx},
        )
        added += n
    return {"ok": True, "added": added, "total": len(AMMO_SEED),
            "note": "propid 为 AUTO### 占位符，请抓包后在后台替换为官方11位ID"}


@router.post("/items/propid/batch")
def batch_propid(payload: dict, admin=Depends(require_admin)):
    """批量导入 propid 映射：{"mapping":[{"name":"5.45x39mm BS","propid":"15010050001"}]}"""
    mapping: List[Dict] = payload.get("mapping") or []
    ok, fail = 0, []
    eng = get_engine()
    for m in mapping:
        name = str(m.get("name", "")).strip()
        new_id = str(m.get("propid", "")).strip()
        if not name or not new_id:
            fail.append(f"{name or '?'}: 字段不全")
            continue
        row = query_one("SELECT propid FROM items WHERE name = :n", {"n": name})
        if not row:
            fail.append(f"{name}: 物品不存在")
            continue
        old = row["propid"]
        if old == new_id:
            ok += 1
            continue
        if query_one("SELECT propid FROM items WHERE propid = :p", {"p": new_id}):
            fail.append(f"{name}: 目标ID已存在")
            continue
        try:
            from sqlalchemy import text as _t
            with eng.begin() as conn:
                conn.execute(_t("UPDATE items SET propid = :n WHERE propid = :o"),
                             {"n": new_id, "o": old})
                for tbl in ("price_hourly", "baseline", "signal_record",
                            "holding", "user_watch"):
                    conn.execute(_t(f"UPDATE {tbl} SET propid = :n WHERE propid = :o"),
                                 {"n": new_id, "o": old})
            ok += 1
        except Exception as e:
            fail.append(f"{name}: {e}")
    return {"ok": True, "success": ok, "failed": fail}


# ---------------- 采集控制 ----------------
@router.post("/collect/now")
def collect_now(payload: dict = None, admin=Depends(require_admin)):
    payload = payload or {}
    return scheduler.collect_round(
        payload.get("stat_hour"), payload.get("cred_id")
    )


@router.post("/collect/backfill")
def backfill(payload: dict = None, admin=Depends(require_admin)):
    days = int((payload or {}).get("days", 14))
    return scheduler.start_backfill(days)


@router.get("/collect/backfill/status")
def backfill_st(admin=Depends(require_admin)):
    return scheduler.backfill_status()


@router.get("/collect/logs")
def collect_logs(limit: int = 30, admin=Depends(require_admin)):
    rows = query(
        """SELECT l.*, c.name AS cred_name FROM collect_log l
           LEFT JOIN credential c ON c.id = l.cred_id
           ORDER BY l.id DESC LIMIT :n""",
        {"n": limit},
    )
    for r in rows:
        for k in ("started_at", "finished_at"):
            if r.get(k):
                r[k] = str(r[k])[:19]
    return {"ok": True, "data": rows}


# ---------------- 调度 ----------------
@router.get("/scheduler")
def sched_status(admin=Depends(require_admin)):
    return {"ok": True, **scheduler.scheduler_status(),
            "enabled": config.get_bool("collect_enabled", True),
            "interval": config.get_int("collect_interval", 60)}


@router.post("/scheduler/start")
def sched_start(admin=Depends(require_admin)):
    config.set_value("collect_enabled", "1")
    config.reload()
    scheduler.restart_scheduler()
    return {"ok": True, **scheduler.scheduler_status()}


@router.post("/scheduler/stop")
def sched_stop(admin=Depends(require_admin)):
    config.set_value("collect_enabled", "0")
    config.reload()
    scheduler.stop_scheduler()
    return {"ok": True, **scheduler.scheduler_status()}


# ---------------- 数据与引擎 ----------------
@router.post("/engine/baseline")
def run_baseline(admin=Depends(require_admin)):
    return strategy.rebuild_baseline()


@router.post("/engine/signal")
def run_signals(admin=Depends(require_admin)):
    return strategy.generate_signals()


@router.get("/prices")
def prices(propid: str = "", limit: int = 100, admin=Depends(require_admin)):
    sql = """SELECT p.propid, i.name, p.stat_hour, p.avg_buyprice, p.source,
             p.collected_at FROM price_hourly p
             LEFT JOIN items i ON i.propid = p.propid WHERE 1=1"""
    params = {"n": limit}
    if propid:
        sql += " AND p.propid = :p"
        params["p"] = propid
    sql += " ORDER BY p.stat_hour DESC, p.propid LIMIT :n"
    rows = query(sql, params)
    for r in rows:
        if r.get("collected_at"):
            r["collected_at"] = str(r["collected_at"])[:19]
    return {"ok": True, "data": rows}


@router.delete("/prices")
def clear_prices(payload: dict = None, admin=Depends(require_admin)):
    before = (payload or {}).get("before")
    if before:
        n = execute("DELETE FROM price_hourly WHERE stat_hour < :b", {"b": before})
    else:
        n = execute("DELETE FROM price_hourly")
    return {"ok": True, "deleted": n}


# ---------------- 演示数据 ----------------
@router.post("/demo/generate")
def demo_generate(payload: dict = None, admin=Depends(require_admin)):
    """生成演示用价格数据（无凭证时用于验证全链路）"""
    payload = payload or {}
    return demo.generate(int(payload.get("days", 14)),
                         bool(payload.get("clear", True)))


@router.delete("/demo")
def demo_clear(admin=Depends(require_admin)):
    return demo.clear()


@router.get("/demo/count")
def demo_count(admin=Depends(require_admin)):
    return {"ok": True, "count": demo.count()}


@router.get("/dashboard")
def dashboard(admin=Depends(require_admin)):
    latest = query_one(
        "SELECT MAX(stat_hour) AS mh, MAX(collected_at) AS mc FROM price_hourly"
    )
    c = query_one(
        """SELECT
             (SELECT COUNT(*) FROM items WHERE watch_enabled=1) AS items,
             (SELECT COUNT(*) FROM items) AS items_all,
             (SELECT COUNT(*) FROM price_hourly) AS ticks,
             (SELECT COUNT(*) FROM credential WHERE status='active') AS creds,
             (SELECT COUNT(*) FROM signal_record) AS signals,
             (SELECT COUNT(*) FROM holding WHERE status IN ('holding','listed')) AS holdings,
             (SELECT COUNT(*) FROM user) AS users"""
    )
    last_log = query_one(
        "SELECT status, item_count, success_count, error_msg, started_at"
        " FROM collect_log ORDER BY id DESC LIMIT 1"
    )
    if last_log and last_log.get("started_at"):
        last_log["started_at"] = str(last_log["started_at"])[:19]

    # 数据新鲜度
    fresh_min = None
    if latest and latest.get("mc"):
        delta = (datetime.now() - latest["mc"]).total_seconds() / 60
        fresh_min = round(delta, 1)

    # 实时涨跌榜（自适应窗口：有多少数据就算多少，不死等 24h）
    movers = {"top_gainers": [], "top_losers": []}
    try:
        items_map = {i["propid"]: i["name"] for i in (query("SELECT propid, name FROM items") or [])}
        # 取每条弹药的最新价 + 最老价（自适应对比）
        all_rows = query(
            """SELECT propid, stat_hour, avg_buyprice AS price FROM price_hourly
               WHERE avg_buyprice IS NOT NULL ORDER BY propid, stat_hour"""
        ) or []
        from collections import defaultdict
        by_prop = defaultdict(list)
        for r in all_rows:
            by_prop[r["propid"]].append(r)

        changed = []
        for pid, rows in by_prop.items():
            if len(rows) < 2:
                continue
            cur = float(rows[-1]["price"])
            old = float(rows[0]["price"])
            if old <= 0:
                continue
            # 计算窗口小时数（最新 vs 最老）
            try:
                latest_h = datetime.strptime(str(rows[-1]["stat_hour"]), "%Y%m%d%H")
                oldest_h = datetime.strptime(str(rows[0]["stat_hour"]), "%Y%m%d%H")
                window_h = int((latest_h - oldest_h).total_seconds() / 3600)
            except Exception:
                window_h = -1
            changed.append({
                "name": items_map.get(pid, pid),
                "propid": pid,
                "price": round(cur, 2),
                "change_pct": round((cur - old) * 100.0 / old, 2),
                "window_hours": window_h,
            })
        changed.sort(key=lambda x: x["change_pct"], reverse=True)
        movers["top_gainers"] = [x for x in changed[:8] if x["change_pct"] > 0]
        movers["top_losers"] = [x for x in changed[-8:][::-1] if x["change_pct"] < 0]
    except Exception:
        pass

    return {
        "ok": True,
        "latest_hour": latest["mh"] if latest else None,
        "freshness_minutes": fresh_min,
        "top_gainers": movers["top_gainers"],
        "top_losers": movers["top_losers"],
        "watch_items": c["items"],
        "total_items": c["items_all"],
        "total_ticks": c["ticks"],
        "active_credentials": c["creds"],
        "total_signals": c["signals"],
        "holdings": c["holdings"],
        "users": c["users"],
        "last_collect": last_log,
        "scheduler": scheduler.scheduler_status(),
        "min_gain": config.min_gain(),
        "breakeven_12h": strategy.breakeven_gain(12),
    }
