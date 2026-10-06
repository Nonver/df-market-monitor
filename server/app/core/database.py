"""数据库连接与基础操作"""
import os
from typing import Any, Dict, List, Optional
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS", "")
DB_NAME = os.getenv("DB_NAME", "df_market")

DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    f"?charset=utf8mb4"
)

_engine: Optional[Engine] = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=10,
            max_overflow=20,
            future=True,
        )
    return _engine


def query(sql: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """查询，返回字典列表"""
    with get_engine().connect() as conn:
        rs = conn.execute(text(sql), params or {})
        cols = list(rs.keys())
        return [dict(zip(cols, row)) for row in rs.fetchall()]


def query_one(sql: str, params: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Optional[Dict] = None) -> int:
    """执行写操作，返回影响行数"""
    with get_engine().begin() as conn:
        rs = conn.execute(text(sql), params or {})
        return rs.rowcount or 0


def execute_many(sql: str, params_list: List[Dict]) -> int:
    if not params_list:
        return 0
    total = 0
    with get_engine().begin() as conn:
        for p in params_list:
            rs = conn.execute(text(sql), p)
            total += rs.rowcount or 0
    return total


def insert_return_id(sql: str, params: Optional[Dict] = None) -> int:
    with get_engine().begin() as conn:
        rs = conn.execute(text(sql), params or {})
        return rs.lastrowid or 0
