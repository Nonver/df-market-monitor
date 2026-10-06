"""对外 API：行情 / 信号 / 持仓 / 用户"""
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from ..core import config
from ..core.database import query, query_one, execute, insert_return_id
from ..engine import strategy

router = APIRouter(prefix="/api", tags=["public"])


def _change_info(propid: str) -> dict:
    """自适应涨跌幅：根据实际数据量返回最佳可用窗口的涨跌幅%。
    返回 {"pct": float|None, "window_hours": int, "data_points": int}
    """
    rows = query(
        """SELECT stat_hour, avg_buyprice FROM price_hourly
           WHERE propid = :p AND avg_buyprice IS NOT NULL
           ORDER BY stat_hour DESC LIMIT 50""",
        {"p": propid},
    )
    if len(rows) < 2:
        return {"pct": None, "window_hours": 0, "data_points": len(rows)}

    cur = float(rows[0]["avg_buyprice"])
    # 从近到远尝试各个窗口，返回第一个有数据的最长窗口
    for wh in [24, 12, 8, 4, 2, 1]:
        target = (datetime.now() - timedelta(hours=wh)).strftime("%Y%m%d%H")
        for r in rows:
            if r["stat_hour"] <= target:
                old = float(r["avg_buyprice"])
                if old > 0:
                    return {
                        "pct": round((cur - old) / old * 100, 2),
                        "window_hours": wh,
                        "data_points": len(rows),
                    }
                break

    # 兜底：用最老的一条
    old = float(rows[-1]["avg_buyprice"])
    if old > 0:
        return {
            "pct": round((cur - old) / old * 100, 2),
            "window_hours": -1,  # 表示"全部数据"
            "data_points": len(rows),
        }
    return {"pct": None, "window_hours": 0, "data_points": len(rows)}


@router.get("/market/list")
def market_list(keyword: str = ""):
    """子弹列表：当前价 + 自适应涨跌幅"""
    sql = "SELECT propid, name, caliber, grade, second_class FROM items WHERE watch_enabled = 1"
    params = {}
    if keyword:
        sql += " AND (name LIKE :k OR caliber LIKE :k OR grade LIKE :k)"
        params["k"] = f"%{keyword}%"
    sql += " ORDER BY sort_order, name"
    items = query(sql, params)

    prices = strategy.latest_prices()
    signals = {s["propid"]: s for s in strategy.latest_signals()}

    out = []
    for it in items:
        p = prices.get(it["propid"])
        sig = signals.get(it["propid"], {})
        chg = _change_info(it["propid"])
        out.append({
            "propid": it["propid"],
            "name": it["name"],
            "caliber": it["caliber"],
            "grade": it["grade"],
            "price": round(float(p["avg_buyprice"]), 2) if p else None,
            "stat_hour": p["stat_hour"] if p else None,
            "change_pct": chg["pct"],
            "change_window": chg["window_hours"],
            "data_points": chg["data_points"],
            "action": sig.get("action", "HOLD"),
            "score": sig.get("score", 0),
            "expected_gain": sig.get("expected_gain"),
        })
    return {"ok": True, "count": len(out), "data": out}


@router.get("/market/{propid}/history")
def market_history(propid: str, hours: int = 168):
    hs = strategy.history(propid, hours)
    return {
        "ok": True,
        "propid": propid,
        "data": [
            {"stat_hour": h["stat_hour"], "price": round(float(h["avg_buyprice"]), 2)}
            for h in hs
        ],
    }


@router.get("/market/{propid}/baseline")
def market_baseline(propid: str):
    rows = query(
        "SELECT slot, p20, p50, p80 FROM baseline WHERE propid = :p ORDER BY slot",
        {"p": propid},
    )
    return {"ok": True, "propid": propid, "data": rows}


@router.get("/market/{propid}/suggest")
def market_suggest(propid: str, duration: int = 12):
    s = strategy.suggest_list_price(propid, duration)
    if not s:
        raise HTTPException(404, "该物品暂无价格数据")
    return {"ok": True, **s}


@router.get("/signal/opportunities")
def opportunities(min_score: int = 0):
    rows = strategy.latest_signals()
    data = [r for r in rows if (r.get("score") or 0) >= min_score]
    return {"ok": True, "count": len(data), "data": data}


# 注意：手动触发信号已移至管理后台 (/api/admin/signal/run)，
# 用户端不再暴露此接口，避免未授权触发。


@router.get("/stats")
def stats():
    latest = query_one(
        "SELECT MAX(stat_hour) AS mh, MAX(collected_at) AS mc FROM price_hourly"
    )
    counts = query_one(
        """SELECT
             (SELECT COUNT(*) FROM items WHERE watch_enabled=1) AS items,
             (SELECT COUNT(*) FROM price_hourly) AS ticks,
             (SELECT COUNT(*) FROM credential WHERE status='active') AS creds,
             (SELECT COUNT(*) FROM signal_record WHERE action='BUY') AS buy_signals
           """
    )
    return {
        "ok": True,
        "latest_hour": latest["mh"] if latest else None,
        "latest_collected": str(latest["mc"])[:19] if latest and latest["mc"] else None,
        "watch_items": counts["items"],
        "total_ticks": counts["ticks"],
        "active_credentials": counts["creds"],
        "min_gain_threshold": config.min_gain(),
        "breakeven_12h": strategy.breakeven_gain(12),
        "breakeven_24h": strategy.breakeven_gain(24),
    }


# ---------------- 用户与持仓 ----------------
@router.post("/auth/login")
def login(payload: dict):
    """微信 code 换 openid；演示环境可直接传 openid"""
    openid = payload.get("openid") or (payload.get("code") or "")
    if not openid:
        raise HTTPException(400, "缺少 openid 或 code")
    openid = str(openid)[:64]
    row = query_one("SELECT * FROM user WHERE openid = :o", {"o": openid})
    if not row:
        uid = insert_return_id(
            "INSERT INTO user (openid, nickname, created_at, last_login_at)"
            " VALUES (:o, :n, NOW(), NOW())",
            {"o": openid, "n": payload.get("nickname") or f"干员{openid[-4:]}"},
        )
    else:
        uid = row["id"]
        execute("UPDATE user SET last_login_at = NOW() WHERE id = :i", {"i": uid})
    return {"ok": True, "user_id": uid, "openid": openid}


@router.get("/holding")
def list_holding(user_id: int):
    rows = query(
        """SELECT h.*, i.name, i.caliber, i.grade FROM holding h
           JOIN items i ON i.propid = h.propid
           WHERE h.user_id = :u ORDER BY h.created_at DESC""",
        {"u": user_id},
    )
    prices = strategy.latest_prices()
    for r in rows:
        p = prices.get(r["propid"])
        cur = float(p["avg_buyprice"]) if p else None
        r["current_price"] = round(cur, 2) if cur else None
        if cur and r.get("buy_price"):
            r["gross_profit_pct"] = round(
                (cur - float(r["buy_price"])) / float(r["buy_price"]) * 100, 2
            )
            dur = r.get("list_duration") or 12
            r["net_proceeds"] = round(strategy.net_proceeds(cur, dur), 2)
            r["net_profit_if_sell"] = round(
                strategy.net_proceeds(cur, dur) - float(r["buy_price"]), 2
            )
    return {"ok": True, "data": rows}


@router.post("/holding")
def add_holding(payload: dict):
    required = {"user_id", "propid", "buy_price", "quantity"}
    if not required.issubset(payload.keys()):
        raise HTTPException(400, f"缺少字段: {required}")
    hid = insert_return_id(
        """INSERT INTO holding (user_id, propid, buy_price, buy_time, quantity,
                                status, created_at)
           VALUES (:u, :p, :b, NOW(), :q, 'holding', NOW())""",
        {
            "u": int(payload["user_id"]),
            "p": str(payload["propid"]),
            "b": float(payload["buy_price"]),
            "q": int(payload.get("quantity", 1)),
        },
    )
    return {"ok": True, "id": hid}


@router.post("/holding/{hid}/list")
def list_holding_item(hid: int, payload: dict):
    duration = int(payload.get("duration", 12))
    price = payload.get("list_price")
    if price is None:
        s = strategy.suggest_list_price(
            query_one("SELECT propid FROM holding WHERE id = :i", {"i": hid})["propid"],
            duration,
        )
        price = s["suggest_price"] if s else 0
    execute(
        """UPDATE holding SET status='listed', list_price=:p, list_at=NOW(),
           list_duration=:d WHERE id=:i""",
        {"p": float(price), "d": duration, "i": hid},
    )
    return {"ok": True, "list_price": price, "duration": duration}


@router.post("/holding/{hid}/sell")
def sell_holding(hid: int, payload: dict):
    price = float(payload.get("sell_price") or 0)
    row = query_one("SELECT * FROM holding WHERE id = :i", {"i": hid})
    if not row:
        raise HTTPException(404, "持仓不存在")
    dur = int(payload.get("duration") or row.get("list_duration") or 12)
    net = strategy.net_proceeds(price, dur) * float(row["quantity"])
    cost = float(row["buy_price"]) * float(row["quantity"])
    execute(
        """UPDATE holding SET status='sold', sell_price=:p, sell_at=NOW(),
           net_profit=:n WHERE id=:i""",
        {"p": price, "n": round(net - cost, 2), "i": hid},
    )
    return {"ok": True, "net_profit": round(net - cost, 2)}


@router.delete("/holding/{hid}")
def delete_holding(hid: int):
    execute("DELETE FROM holding WHERE id = :i", {"i": hid})
    return {"ok": True}


@router.get("/watch")
def list_watch(user_id: int):
    rows = query(
        """SELECT w.propid, i.name FROM user_watch w
           JOIN items i ON i.propid = w.propid WHERE w.user_id = :u""",
        {"u": user_id},
    )
    return {"ok": True, "data": rows}


@router.post("/watch")
def add_watch(payload: dict):
    execute(
        """INSERT IGNORE INTO user_watch (user_id, propid, created_at)
           VALUES (:u, :p, NOW())""",
        {"u": int(payload["user_id"]), "p": str(payload["propid"])},
    )
    return {"ok": True}


@router.delete("/watch")
def del_watch(user_id: int, propid: str):
    execute(
        "DELETE FROM user_watch WHERE user_id = :u AND propid = :p",
        {"u": user_id, "p": propid},
    )
    return {"ok": True}
