"""演示数据生成器

用途：在没有可用凭证时，生成一份带日内周期特征的模拟价格数据，
让看板、走势图、择时信号等功能可以先跑起来看效果。

数据基于游戏内真实基准价，叠加：
  1. 日内周期（正弦，峰值约 21 点，谷值约 9 点，振幅 ±10%）
  2. 日间趋势（随机游走，限制在 ±15%）
  3. 随机噪声（±2.5%）

所有数据标记为 source='demo'，真实采集（source='official'）会自动覆盖。
"""
import math
import random
from datetime import datetime, timedelta

from ..core.database import query, execute, get_engine
from sqlalchemy import text as _t


def generate(days: int = 14, clear_old: bool = True) -> dict:
    items = query(
        """SELECT propid, name, base_price FROM items
           WHERE watch_enabled = 1 ORDER BY propid"""
    )
    if not items:
        return {"ok": False, "msg": "没有启用监控的物品，请先导入弹药种子"}

    if clear_old:
        execute("DELETE FROM price_hourly WHERE source = 'demo'")

    random.seed(20260830)
    now = datetime.now().replace(minute=0, second=0, microsecond=0)

    # 每个物品一条独立的趋势轨迹
    trends = {}
    for it in items:
        base = float(it.get("base_price") or 0) or random.uniform(800, 40000)
        drift = 0.0
        series = []
        for i in range(days * 24):
            drift += random.uniform(-1.1, 1.14)          # 每日随机游走
            drift = max(-28.0, min(28.0, drift))          # 限制漂移幅度
            series.append(drift)
        trends[it["propid"]] = (base, series)

    rows = []
    for idx in range(days * 24):
        ts = now - timedelta(hours=idx)
        stat_hour = ts.strftime("%Y%m%d%H")
        hour = ts.hour
        # 日内周期：峰值 21 点，谷值 9 点，振幅 ±16%（贴近真实波动）
        intraday = 1 + 0.16 * math.sin((hour - 15) / 24.0 * 2 * math.pi)
        for it in items:
            base, series = trends[it["propid"]]
            pos = days * 24 - 1 - idx          # 越早的数据越靠前
            trend = 1 + series[pos] / 100.0
            noise = 1 + random.uniform(-0.025, 0.025)
            price = base * intraday * trend * noise
            rows.append({
                "p": it["propid"],
                "h": stat_hour,
                "v": round(max(1.0, price), 2),
                "c": ts,
            })

    eng = get_engine()
    sql = _t(
        """INSERT INTO price_hourly (propid, stat_hour, avg_buyprice, source, collected_at)
           VALUES (:p, :h, :v, 'demo', :c)
           ON DUPLICATE KEY UPDATE avg_buyprice = :v, source = 'demo'
           WHERE source <> 'official'"""
    )
    # MySQL 不支持上述 WHERE，改用显式判断
    sql = _t(
        """INSERT INTO price_hourly (propid, stat_hour, avg_buyprice, source, collected_at)
           VALUES (:p, :h, :v, 'demo', :c)
           ON DUPLICATE KEY UPDATE
             avg_buyprice = IF(source = 'official', avg_buyprice, :v),
             source = IF(source = 'official', 'official', 'demo')"""
    )
    total = 0
    with eng.begin() as conn:
        for i in range(0, len(rows), 1000):
            batch = rows[i:i + 1000]
            conn.execute(sql, batch)
            total += len(batch)

    return {
        "ok": True,
        "generated": total,
        "days": days,
        "items": len(items),
        "note": "演示数据（source=demo），真实采集后会自动保留官方数据",
    }


def clear() -> dict:
    n = execute("DELETE FROM price_hourly WHERE source = 'demo'")
    return {"ok": True, "deleted": n}


def count() -> int:
    r = query("SELECT COUNT(*) AS c FROM price_hourly WHERE source = 'demo'")
    return r[0]["c"] if r else 0
