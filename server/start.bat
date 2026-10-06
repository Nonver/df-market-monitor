@echo off
chcp 65001 >nul
title 三角洲子弹价格监控 - 后端服务

echo ============================================
echo   三角洲子弹价格监控平台
echo   后端服务启动中...
echo ============================================
echo.

set PY=python
cd /d %~dp0

echo [1/2] 检查依赖...
"%PY%" -c "import fastapi,uvicorn,pymysql,sqlalchemy,apscheduler,requests" 2>nul
if errorlevel 1 (
    echo 正在安装依赖...
    "%PY%" -m pip install -q fastapi uvicorn pymysql sqlalchemy apscheduler cryptography pyjwt passlib bcrypt python-multipart requests
)

echo [2/2] 启动服务...
echo.
echo   网站首页:  http://127.0.0.1:8765/
echo   管理后台:  http://127.0.0.1:8765/admin
echo   API 文档:  http://127.0.0.1:8765/docs
echo.
echo   管理后台: 首次登录时自行设置账号与密码
echo   (第一个登录的用户名/密码即成为管理员)
echo.
echo 按 Ctrl+C 停止服务
echo ============================================
echo.

"%PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8765

pause
