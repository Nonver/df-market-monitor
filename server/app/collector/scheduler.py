"""采集调度：实时采集、历史回填、定时任务"""
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core import config
from ..core.database import query, query_one, execute, execute_many
from .ams_client import (
    AmsClient, pick_credential, mark_result, current_stat_hour,
)

_backfill_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "success": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "message": "",
}
_backfill_lock = threading.Lock()


# ---------------- 单轮采集 ----------------
def collect_round(stat_hour: str = None, cred_id: int = None) -> Dict:
    """采集一轮：所有启用监控的物品在指定小时的均价"""
    started = datetime.now()
    stat_hour = stat_hour or current_stat_hour()
    delay_ms = config.get_int("collect_delay_ms", 1000)

    items = query(
        "SELECT propid, name FROM items WHERE watch_enabled = 1 ORDER BY sort_order, propid"
    )
    if not items:
        return {"ok": False, "msg": "没有启用监控的物品，请先在后台添加子弹",
                "total": 0, "success": 0}

    if cred_id:
        row = query_one("SELECT * FROM credential WHERE id = :id", {"id": cred_id})
        if not row:
            return {"ok": False, "msg": "指定凭证不存在", "total": 0, "success": 0}
        from ..core.security import decrypt_token
        row["access_token"] = decrypt_token(row.get("access_token") or "")
        cred = row
    else:
        cred = pick_credential()

    if not cred:
        execute(
            """INSERT INTO collect_log (started_at, finished_at, item_count,
               success_count, status, error_msg) VALUES (NOW(), NOW(), 0, 0, 'failed', :e)""",
            {"e": "无可用的凭证，请在后台添加有效凭证"},
        )
        return {"ok": False, "msg": "无可用的凭证，请先在后台添加凭证",
                "total": 0, "success": 0}

    client = AmsClient(cred)
    ok_count = 0
    unchanged = 0
    fail_count = 0
    first_error = ""

    # 预取每个物品的最新价格，用于去重
    last_prices = {}
    for r in query(
        "SELECT propid, avg_buyprice FROM price_hourly ph1 "
        "WHERE stat_hour = (SELECT MAX(ph2.stat_hour) FROM price_hourly ph2 "
        "                  WHERE ph2.propid = ph1.propid)"
    ):
        last_prices[r["propid"]] = float(r["avg_buyprice"])

    for idx, it in enumerate(items):
        res = client.fetch_price(it["propid"], stat_hour)
        if res["ok"]:
            price = res["price"]
            last_p = last_prices.get(it["propid"])
            # 去重：价格与上次相同则跳过（AMS 对任意 stat_hour 返回同一值）
            if last_p is not None and abs(price - last_p) < 0.01:
                unchanged += 1
            else:
                execute(
                    """INSERT INTO price_hourly (propid, stat_hour, avg_buyprice, source, collected_at)
                       VALUES (:p, :h, :v, 'official', NOW())
                       ON DUPLICATE KEY UPDATE avg_buyprice = :v, collected_at = NOW()""",
                    {"p": it["propid"], "h": stat_hour, "v": price},
                )
                ok_count += 1
        else:
            fail_count += 1
            if not first_error:
                first_error = f"{it['name']}: {res['msg']}"
        if idx < len(items) - 1:
            time.sleep(delay_ms / 1000.0)

    mark_result(cred["id"], ok_count > 0, first_error)
    status = "success" if fail_count == 0 else ("partial" if ok_count else "failed")
    execute(
        """INSERT INTO collect_log (started_at, finished_at, item_count, success_count,
           cred_id, status, error_msg) VALUES (:s, NOW(), :t, :ok, :c, :st, :e)""",
        {
            "s": started,
            "t": len(items),
            "ok": ok_count,
            "c": cred["id"],
            "st": status,
            "e": first_error[:500],
        },
    )
    return {
        "ok": ok_count > 0,
        "msg": first_error or f"采集成功（新增 {ok_count}，未变 {unchanged}）",
        "total": len(items),
        "success": ok_count,
        "unchanged": unchanged,
        "failed": fail_count,
        "stat_hour": stat_hour,
    }


# ---------------- 历史回填 ----------------
def start_backfill(days: int = 14) -> Dict:
    """异步回填历史数据（限速，避免风控）"""
    with _backfill_lock:
        if _backfill_state["running"]:
            return {"ok": False, "msg": "已有回填任务在运行"}
        _backfill_state.update({
            "running": True, "total": 0, "done": 0, "success": 0,
            "failed": 0, "started_at": datetime.now(), "finished_at": None,
            "message": "任务启动",
        })

    t = threading.Thread(target=_backfill_worker, args=(days,), daemon=True)
    t.start()
    return {"ok": True, "msg": f"回填任务已启动，共 {days} 天"}


def _backfill_worker(days: int):
    """并发回填历史数据，支持断点续跑（跳过已采集的对）。"""
    try:
        items = query(
            "SELECT propid FROM items WHERE watch_enabled = 1 ORDER BY propid"
        )
        if not items:
            with _backfill_lock:
                _backfill_state["message"] = "没有启用监控的物品"
            return

        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        hours: List[str] = [
            (now - timedelta(hours=i)).strftime("%Y%m%d%H")
            for i in range(days * 24)
        ]
        propids = [it["propid"] for it in items]

        # 断点续跑：跳过 price_hourly 中已存在的 (propid, stat_hour)
        existing = set()
        for r in query("SELECT propid, stat_hour FROM price_hourly"):
            existing.add((r["propid"], r["stat_hour"]))
        tasks = [
            (p, h) for h in hours for p in propids
            if (p, h) not in existing
        ]

        with _backfill_lock:
            _backfill_state["total"] = len(tasks)
            _backfill_state["done"] = 0
            _backfill_state["success"] = 0
            _backfill_state["failed"] = 0
        if not tasks:
            with _backfill_lock:
                _backfill_state["message"] = "无需回填（数据已存在）"
            return

        cred = pick_credential()
        if not cred:
            with _backfill_lock:
                _backfill_state["message"] = "无可用凭证"
            return

        client = AmsClient(cred)
        concurrency = max(1, config.get_int("backfill_concurrency", 8))
        pace_ms = max(0, config.get_int("backfill_delay_ms", 80))
        batch_size = max(1, config.get_int("backfill_batch", 200))

        buffer: List[tuple] = []
        buffer_lock = threading.Lock()
        _local = threading.local()

        def get_client() -> AmsClient:
            if not hasattr(_local, "client"):
                _local.client = AmsClient(cred)
            return _local.client

        def flush(buf):
            if not buf:
                return
            execute_many(
                """INSERT INTO price_hourly
                   (propid, stat_hour, avg_buyprice, source, collected_at)
                   VALUES (:p, :h, :v, 'official', NOW())
                   ON DUPLICATE KEY UPDATE avg_buyprice = :v, collected_at = NOW()""",
                [{"p": p, "h": h, "v": v} for (p, h, v) in buf],
            )

        def worker(task):
            p, h = task
            return task, get_client().fetch_price(p, h)

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, t) for t in tasks]
            for fut in as_completed(futures):
                task, res = fut.result()
                with _backfill_lock:
                    _backfill_state["done"] += 1
                    if res["ok"]:
                        _backfill_state["success"] += 1
                        buffer.append((task[0], task[1], res["price"]))
                    else:
                        _backfill_state["failed"] += 1
                if pace_ms:
                    time.sleep(pace_ms / 1000.0)
                with buffer_lock:
                    if len(buffer) >= batch_size:
                        flush(buffer)
                        buffer.clear()

        with buffer_lock:
            flush(buffer)

        _backfill_state["message"] = "回填完成"
    except Exception as e:
        _backfill_state["message"] = f"回填异常: {e}"
    finally:
        with _backfill_lock:
            _backfill_state["running"] = False
            _backfill_state["finished_at"] = datetime.now()


def backfill_status() -> Dict:
    with _backfill_lock:
        st = dict(_backfill_state)
    if st["started_at"]:
        st["started_at"] = st["started_at"].strftime("%Y-%m-%d %H:%M:%S")
    if st["finished_at"]:
        st["finished_at"] = st["finished_at"].strftime("%Y-%m-%d %H:%M:%S")
    return st


# ---------------- 定时调度 ----------------
_scheduler = None


def start_scheduler():
    """启动定时采集"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    if not config.get_bool("collect_enabled", True):
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    interval = max(1, config.get_int("collect_interval", 60))
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        _job_collect, "interval", minutes=interval, id="collect",
        replace_existing=True, misfire_grace_time=300,
    )
    _scheduler.add_job(_job_baseline, "cron", hour=3, minute=10,
                       id="baseline", replace_existing=True)
    _scheduler.add_job(_job_signal, "interval", minutes=30, id="signal",
                       replace_existing=True)
    _scheduler.start()
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None


def restart_scheduler():
    stop_scheduler()
    return start_scheduler()


def scheduler_status() -> Dict:
    jobs = []
    if _scheduler:
        for j in _scheduler.get_jobs():
            jobs.append({
                "id": j.id,
                "next_run": str(j.next_run_time)[:19] if j.next_run_time else None,
            })
    return {"running": _scheduler is not None, "jobs": jobs}


def _job_collect():
    try:
        collect_round()
    except Exception:
        pass


def _job_baseline():
    try:
        from ..engine.strategy import rebuild_baseline
        rebuild_baseline()
    except Exception:
        pass


def _job_signal():
    try:
        from ..engine.strategy import generate_signals
        generate_signals()
    except Exception:
        pass
