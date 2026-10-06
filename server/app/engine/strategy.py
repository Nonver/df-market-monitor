"""择时引擎：日内分位 + 周内位置 + 费率门槛 + 持仓约束 + 突变检测"""
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ..core import config
from ..core.database import query, query_one, execute


# ---------------- 统计工具 ----------------
def percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def rank_percentile(vals: List[float], v: float) -> float:
    """v 在样本中的百分位 0~100"""
    if not vals:
        return 50.0
    below = sum(1 for x in vals if x < v)
    return round(below / len(vals) * 100.0, 2)


# ---------------- 日内基准 ----------------
def rebuild_baseline(days: int = None) -> Dict:
    """重建每个物品每个小时(0~23)的 P20/P50/P80 基准"""
    days = days or config.baseline_days()
    rows = query(
        """SELECT propid, stat_hour, avg_buyprice FROM price_hourly
           WHERE avg_buyprice IS NOT NULL
             AND collected_at >= DATE_SUB(NOW(), INTERVAL :d DAY)
             AND propid IN (SELECT propid FROM items)
           ORDER BY propid, stat_hour""",
        {"d": days},
    )
    buckets: Dict[str, List[float]] = {}
    for r in rows:
        slot = int(str(r["stat_hour"])[-2:])  # HH
        buckets.setdefault(f"{r['propid']}|{slot}", []).append(float(r["avg_buyprice"]))

    count = 0
    for key, vals in buckets.items():
        propid, slot = key.split("|")
        vals.sort()
        p20 = percentile(vals, 20)
        p50 = percentile(vals, 50)
        p80 = percentile(vals, 80)
        execute(
            """INSERT INTO baseline (propid, slot, p20, p50, p80, sample_days, updated_at)
               VALUES (:p, :s, :p20, :p50, :p80, :d, NOW())
               ON DUPLICATE KEY UPDATE p20=:p20, p50=:p50, p80=:p80,
                 sample_days=:d, updated_at=NOW()""",
            {"p": propid, "s": int(slot), "p20": p20, "p50": p50, "p80": p80, "d": days},
        )
        count += 1
    return {"ok": True, "baseline_rows": count, "days": days}


# ---------------- 数据读取 ----------------
def latest_prices() -> Dict[str, Dict]:
    """每个物品最新一条价格"""
    rows = query(
        """SELECT p.propid, p.stat_hour, p.avg_buyprice, i.name, i.caliber, i.grade
           FROM price_hourly p
           JOIN items i ON i.propid = p.propid
           JOIN (SELECT propid, MAX(stat_hour) AS mh FROM price_hourly
                 WHERE avg_buyprice IS NOT NULL GROUP BY propid) t
             ON t.propid = p.propid AND t.mh = p.stat_hour
           WHERE i.watch_enabled = 1"""
    )
    out = {}
    for r in rows:
        out[r["propid"]] = r
    return out


def history(propid: str, hours: int = 24) -> List[Dict]:
    return query(
        """SELECT stat_hour, avg_buyprice FROM price_hourly
           WHERE propid = :p AND avg_buyprice IS NOT NULL
           ORDER BY stat_hour DESC LIMIT :n""",
        {"p": propid, "n": hours},
    )[::-1]


def recent_range(propid: str, days: int = 7) -> Dict:
    """近 N 天最高/最低"""
    row = query_one(
        """SELECT MIN(avg_buyprice) AS lo, MAX(avg_buyprice) AS hi, COUNT(*) AS cnt
           FROM price_hourly WHERE propid = :p AND avg_buyprice IS NOT NULL
             AND collected_at >= DATE_SUB(NOW(), INTERVAL :d DAY)""",
        {"p": propid, "d": days},
    )
    return row or {"lo": None, "hi": None, "cnt": 0}


def baseline_of(propid: str, slot: int) -> Optional[Dict]:
    return query_one(
        "SELECT p20, p50, p80, sample_days FROM baseline WHERE propid = :p AND slot = :s",
        {"p": propid, "s": slot},
    )


# ---------------- 成本与建议 ----------------
def net_proceeds(sell_price: float, duration: int = 12) -> float:
    """卖出到手金额（扣保证金与手续费）"""
    deposit = config.fee_deposit_rate()
    fee = config.fee_rate_12h() if duration == 12 else config.fee_rate_24h()
    return sell_price * (1 - (deposit + fee) / 100.0)


def breakeven_gain(duration: int = 12) -> float:
    """盈亏平衡所需涨幅%"""
    deposit = config.fee_deposit_rate()
    fee = config.fee_rate_12h() if duration == 12 else config.fee_rate_24h()
    total = (deposit + fee) / 100.0
    return round(total / (1 - total) * 100, 2)


def suggest_list_price(propid: str, duration: int = 12) -> Optional[Dict]:
    """挂单价建议：偏向快成交，不超过 P80 的 98%"""
    latest = latest_prices().get(propid)
    if not latest:
        return None
    cur = float(latest["avg_buyprice"])
    slot = int(str(latest["stat_hour"])[-2:])
    bl = baseline_of(propid, slot)
    target = float(bl["p80"]) if bl and bl.get("p80") else cur * 1.2
    price = min(target * 0.98, cur * 1.5)
    return {
        "suggest_price": round(price),
        "current": round(cur, 2),
        "target_p80": round(target, 2),
        "net_proceeds": round(net_proceeds(price, duration), 2),
        "breakeven_gain": breakeven_gain(duration),
        "duration": duration,
    }


# ---------------- 突变检测 ----------------
def has_recent_shock(propid: str, hours: int = 3, threshold: float = 40.0) -> bool:
    """连续 N 小时同向大幅变动视为事件冲击"""
    hs = history(propid, hours + 1)
    if len(hs) < hours + 1:
        return False
    vals = [h["avg_buyprice"] for h in hs]
    ups = downs = 0
    for i in range(1, len(vals)):
        prev, cur = vals[i - 1], vals[i]
        if prev and prev > 0:
            chg = (cur - prev) / prev * 100
            if chg > threshold:
                ups += 1
            elif chg < -threshold:
                downs += 1
    return ups >= hours or downs >= hours


# ---------------- 信号生成 ----------------
def generate_signals() -> Dict:
    """生成买入机会信号，写入 signal_record"""
    min_gain = config.min_gain()
    buy_pct = config.buy_percentile()
    sell_pos_th = config.sell_pos()
    now = datetime.now()

    prices = latest_prices()
    holdings = query("SELECT propid, status FROM holding WHERE status IN ('holding','listed')")
    holding_map = {h["propid"]: h["status"] for h in holdings}

    used_slots = query_one(
        "SELECT COUNT(*) AS c FROM holding WHERE status = 'listed'"
    )["c"] or 0
    free_slots = max(0, config.slot_count() - used_slots)

    results = []
    for propid, p in prices.items():
        try:
            cur = float(p["avg_buyprice"])
            if cur <= 0:
                continue
            slot = int(str(p["stat_hour"])[-2:])
            bl = baseline_of(propid, slot)
            rng = recent_range(propid, 7)

            rk = None
            if bl and bl.get("p20") is not None:
                vals = [v for v in (bl["p20"], bl["p50"], bl["p80"]) if v]
                rk = rank_percentile(vals, cur)

            lo, hi = rng.get("lo"), rng.get("hi")
            pos = None
            if lo is not None and hi is not None and hi > lo:
                pos = (cur - lo) / (hi - lo) * 100

            target = float(bl["p80"]) if bl and bl.get("p80") else cur * 1.2
            expected_gain = (target * 0.98 - cur) / cur * 100 if cur else 0

            reasons = []
            action, score = "HOLD", 0

            shock = has_recent_shock(propid)

            # 买入判定
            if propid in holding_map:
                reasons.append("已在持仓中，不再重复买入")
            elif free_slots <= 0:
                reasons.append("寄售槽位已满")
            elif shock:
                reasons.append("近期存在价格突变，暂停信号")
            # 数据不足判断
            elif bl is None:
                reasons.append("价格数据积累中，暂无足够历史波动生成信号")
            elif bl.get("p20") is not None and abs((bl["p20"] or 0) - (bl["p80"] or 0)) < 0.01:
                reasons.append("价格数据积累中，历史波动不足，暂时无法生成信号")
            elif rk is not None and rk <= buy_pct and expected_gain >= min_gain:
                action = "BUY"
                s_rank = max(0.0, (buy_pct - rk) / buy_pct) * 100 if buy_pct else 50
                s_pos = max(0.0, (30 - (pos if pos is not None else 50)) / 30) * 100
                s_gain = min(100.0, expected_gain / (min_gain * 2) * 100)
                score = round(0.35 * s_rank + 0.25 * max(0, s_pos) + 0.40 * s_gain)
                reasons.append(
                    f"当前价处于近 {config.baseline_days()} 天同时段的 {rk}% 分位"
                )
                if pos is not None:
                    reasons.append(f"周内位置 {pos:.0f}%")
                reasons.append(f"预期收益 {expected_gain:.1f}%（门槛 {min_gain}%）")
            else:
                if rk is not None and rk > buy_pct:
                    reasons.append(f"分位 {rk}% 高于买入阈值 {buy_pct}%")
                if expected_gain < min_gain:
                    reasons.append(
                        f"预期收益 {expected_gain:.1f}% 低于门槛 {min_gain}%，"
                        f"扣费后无利可图"
                    )

            # 卖出判定（针对持仓）
            if propid in holding_map:
                if pos is not None and pos >= sell_pos_th:
                    action = "SELL"
                    score = max(score, round(pos))
                    reasons.append(f"周内位置 {pos:.0f}%，达到卖出阈值 {sell_pos_th}%")
                elif pos is not None:
                    reasons.append(f"周内位置 {pos:.0f}%，未达卖出阈值")

            if action == "HOLD":
                score = 0

            execute(
                """INSERT INTO signal_record
                   (propid, ts, action, score, price, expected_gain, reason, created_at)
                   VALUES (:p, :t, :a, :s, :pr, :g, :r, NOW())""",
                {
                    "p": propid, "t": now, "a": action, "s": int(score),
                    "pr": cur, "g": round(expected_gain, 2),
                    "r": "；".join(reasons)[:500],
                },
            )
            results.append({
                "propid": propid, "name": p["name"], "price": cur,
                "action": action, "score": int(score),
                "expected_gain": round(expected_gain, 2),
                "percentile": rk, "week_pos": (round(pos, 1) if pos is not None else None),
                "reason": "；".join(reasons),
            })
        except Exception as e:
            results.append({"propid": propid, "action": "HOLD", "reason": f"计算异常: {e}"})

    results.sort(key=lambda x: (x.get("score") or 0), reverse=True)
    return {
        "ok": True,
        "count": len(results),
        "buy_count": sum(1 for r in results if r.get("action") == "BUY"),
        "sell_count": sum(1 for r in results if r.get("action") == "SELL"),
        "free_slots": free_slots,
        "results": results,
    }


def latest_signals() -> List[Dict]:
    """取每个物品最新一条信号"""
    rows = query(
        """SELECT s.propid, s.ts, s.action, s.score, s.price, s.expected_gain, s.reason,
                  i.name, i.caliber, i.grade
           FROM signal_record s
           JOIN items i ON i.propid = s.propid
           JOIN (SELECT propid, MAX(ts) AS mt FROM signal_record GROUP BY propid) t
             ON t.propid = s.propid AND t.mt = s.ts
           WHERE i.watch_enabled = 1
           ORDER BY s.score DESC"""
    )
    return rows
