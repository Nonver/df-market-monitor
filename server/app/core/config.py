"""系统配置：全部存于 sys_config 表，后台可实时修改"""
import time
from typing import Any, Dict, Optional

from .database import query, query_one, execute

_CACHE: Dict[str, Any] = {}
_CACHE_TS: float = 0
_TTL = 30  # 秒


def _load_all() -> Dict[str, Any]:
    global _CACHE, _CACHE_TS
    now = time.time()
    if _CACHE and now - _CACHE_TS < _TTL:
        return _CACHE
    rows = query("SELECT config_key, config_value, config_type FROM sys_config")
    data: Dict[str, Any] = {}
    for r in rows:
        v = r["config_value"]
        t = r.get("config_type", "string")
        try:
            if t == "number":
                v = float(v) if "." in str(v) else int(v)
            elif t == "bool":
                v = str(v).lower() in ("1", "true", "yes", "on")
        except Exception:
            pass
        data[r["config_key"]] = v
    _CACHE = data
    _CACHE_TS = now
    return data


def get(key: str, default: Any = None) -> Any:
    return _load_all().get(key, default)


def get_str(key: str, default: str = "") -> str:
    v = get(key, default)
    return str(v)


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(float(get(key, default)))
    except Exception:
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get(key, default))
    except Exception:
        return default


def get_bool(key: str, default: bool = False) -> bool:
    v = get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on")


def set_value(key: str, value: Any, desc: str = None) -> None:
    """写入配置并立即失效缓存"""
    execute(
        """
        INSERT INTO sys_config (config_key, config_value, description, updated_at)
        VALUES (:k, :v, :d, NOW())
        ON DUPLICATE KEY UPDATE config_value = :v, updated_at = NOW()
        """,
        {"k": key, "v": str(value), "d": desc},
    )
    _CACHE.pop(key, None)
    _CACHE_TS = 0


def all_config() -> Dict[str, Any]:
    return dict(_load_all())


def reload() -> None:
    global _CACHE, _CACHE_TS
    _CACHE = {}
    _CACHE_TS = 0


# ---- 常用配置快捷方式 ----
def fee_deposit_rate() -> float:
    return get_float("fee_deposit_rate", 3.0)


def fee_rate_12h() -> float:
    return get_float("fee_rate_12h", 5.0)


def fee_rate_24h() -> float:
    return get_float("fee_rate_24h", 8.0)


def min_gain() -> float:
    return get_float("strategy_min_gain", 15.0)


def baseline_days() -> int:
    return get_int("strategy_baseline_days", 14)


def buy_percentile() -> float:
    return get_float("strategy_buy_percentile", 25.0)


def sell_pos() -> float:
    return get_float("strategy_sell_pos", 75.0)


def slot_count() -> int:
    return get_int("strategy_slot_count", 15)
