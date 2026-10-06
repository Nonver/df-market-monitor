"""三角洲子弹价格监控平台 · 后端入口"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api import public, admin
from app.core import config
from app.collector import scheduler

BASE_DIR = Path(__file__).resolve().parent.parent
ADMIN_DIR = BASE_DIR / "admin"
H5_DIR = BASE_DIR / "h5"

app = FastAPI(
    title="三角洲子弹价格监控平台",
    description="交易行子弹价格采集、监控与择时信号服务",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.get_str("cors_origins", "").split(",") if config.get_str("cors_origins", "") else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(public.router)
app.include_router(admin.router)


@app.on_event("startup")
def on_startup():
    try:
        if config.get_bool("collect_enabled", True):
            scheduler.start_scheduler()
            print("[启动] 定时采集已开启")
        else:
            print("[启动] 定时采集未开启（可在后台开启）")
    except Exception as e:
        print(f"[启动] 调度器启动失败: {e}")


@app.get("/", include_in_schema=False)
def root():
    """用户端网站"""
    h5 = H5_DIR / "index.html"
    if h5.exists():
        return FileResponse(h5)
    return {"name": "三角洲子弹价格监控平台", "docs": "/docs", "admin": "/admin"}


@app.get("/api/info")
def api_info():
    return {
        "name": "三角洲子弹价格监控平台",
        "version": "1.0.0",
        "api": {
            "行情列表": "/api/market/list",
            "单个走势": "/api/market/{propid}/history",
            "日内基准": "/api/market/{propid}/baseline",
            "挂单建议": "/api/market/{propid}/suggest",
            "买入机会": "/api/signal/opportunities",
            "系统统计": "/api/stats",
        },
    }


@app.get("/health")
def health():
    from app.core.database import query_one
    try:
        query_one("SELECT 1 AS ok")
        return {"ok": True, "database": "up"}
    except Exception as e:
        return JSONResponse({"ok": False, "database": str(e)}, status_code=500)


# ---- 后台管理界面 ----
@app.get("/admin", include_in_schema=False)
def admin_index():
    return FileResponse(ADMIN_DIR / "index.html")


@app.get("/admin/{path:path}", include_in_schema=False)
def admin_static(path: str):
    fp = ADMIN_DIR / path
    if fp.exists() and fp.is_file():
        return FileResponse(fp)
    return FileResponse(ADMIN_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
