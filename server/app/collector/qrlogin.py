# -*- coding: utf-8 -*-
"""
扫码登录拿凭证 —— 复用腾讯官方「我的行动周报」页的登录流程（Playwright 无头浏览器）。

为什么不用纯 HTTP 的 OAuth 隐式授权（graph.qq.com/oauth2.0/authorize?response_type=token）：
  周报页的登录模块 dfm-qq-app-login.min.js 根本不走那条路。它通过 qq-web.cdn-go.cn 的
  qqapi.js 走 **QQ SSO 桥接**，直接拿到 jData.openid + jData.access_token，并把凭证写进
  页面 URL 的 openid/acctype/appid/access_token 参数（request.js 就是这么读的）。
  纯服务端缺跨域 OAuth 会话态，拿不到可用 token；而复用官方页走 SSO，token 类型与 AMS 接口
  完全一致（appid 同为 101491592），且不经过任何第三方平台。

链路：
  启动无头 Chromium -> 打开周报页 -> 等 #qrlogin_img -> 截二维码给用户扫
  轮询：手机扫码确认后，页面 URL 带上 openid/access_token -> 读出 -> AES 入库 -> 打 AMS 验证

二维码与 token 只存在于浏览器内存与后端内存，不落库、不写日志。
"""
import asyncio
import base64
import concurrent.futures
import time
import threading
import uuid
from typing import Dict, Optional

from ..core import config
from . import ams_client


# 周报页（appid 与 AMS 价格接口同为 101491592，凭证通用）
WEEKLY_URL = "https://df.qq.com/cp/weekly20241129/index.html"

# 轮询状态码文案（沿用前端约定）
PT_CODE_TEXT = {
    "66": "等待扫码",
    "67": "已扫码，请在手机上确认",
    "65": "二维码已失效",
    "0": "登录成功",
}

# 会话状态
ST_INIT = "init"
ST_QRCODE = "qrcode"
ST_SCANNED = "scanned"
ST_PROCESSING = "processing"
ST_SUCCESS = "success"
ST_EXPIRED = "expired"
ST_FAILED = "failed"

SESSION_TTL = 300        # 会话 5 分钟无操作自动销毁
QR_TTL = 180             # 腾讯侧二维码约 3 分钟有效


def _cfg(key: str, default: str) -> str:
    try:
        v = config.get_str(key, default)
        return str(v) if v else default
    except Exception:
        return default


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("gbk", errors="ignore")


class QrLoginSession:
    """一次扫码登录会话。后端用无头浏览器驱动官方周报页，二维码与 token 只在内存。"""

    def __init__(self):
        self.sid = uuid.uuid4().hex
        self.status = ST_INIT
        self.message = "初始化中…"
        self.qr_b64: Optional[str] = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.processing = False
        self.last_raw = ""
        self.log: list = []
        self.result: Optional[Dict] = None

        self._pw = None
        self._browser = None
        self._page = None
        self._extra = []  # 登录过程可能新开的子页面（回调等）
        self._ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="qrlogin")  # 专用线程跑浏览器


    # ---------- 配置 ----------
    @property
    def client_id(self) -> str:
        return _cfg("ams_appid", "101491592")

    # ---------- 步骤 1：打开官方页 + 取二维码 ----------
    # 所有 Playwright 调用都跑在同一个专用线程（self._ex）里，并给该线程一个
    # 独立且未运行的 event loop，规避 uvicorn 线程池复用导致的
    # “Sync API inside asyncio loop” 冲突，也避免跨线程使用浏览器对象。
    def _ensure_loop(self):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_running():
            asyncio.set_event_loop(asyncio.new_event_loop())

    def start(self) -> Dict:
        if getattr(self, "_ex", None) is None:
            self._ex = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="qrlogin")
        try:
            self._ex.submit(self._browser_start).result(timeout=120)
        except Exception as e:
            self.status = ST_FAILED
            self.message = f"打开登录页失败: {e}"
            self._log(f"start err: {e}")
            try:
                self._close_impl()
            except Exception:
                pass
        return self._view()

    def _browser_start(self):
        self._ensure_loop()
        try:
            from playwright.sync_api import sync_playwright

            p = sync_playwright().start()
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            ctx = browser.new_context()
            extra_pages = []
            ctx.on("page", lambda pg: extra_pages.append(pg))
            page = ctx.new_page()
            page.goto(WEEKLY_URL, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # 周报页登录弹层默认隐藏，需要强制显示并真实点击 QQ 登录按钮
            # （真实 click 才会触发腾讯 ptlogin 弹层 / iframe）
            page.evaluate(
                """() => {
                    document.querySelectorAll('.pop,.pop-item,.pop-login,.login-box')
                        .forEach(e => { e.style.display = 'block'; });
                    const qq = document.querySelector('.btn-qq-login');
                    if (qq) { qq.style.display = 'inline-block'; qq.style.visibility = 'visible'; }
                }"""
            )
            try:
                page.click(".btn-qq-login", timeout=8000)
            except Exception:
                pass

            # 二维码在 ptlogin 的 iframe 内（#qrlogin_img），且 src 要等一会儿才赋上
            qr = None
            qr_frame = None
            qr_src = ""
            for _ in range(60):
                for pg in [page] + extra_pages:
                    for f in pg.frames:
                        try:
                            el = f.query_selector("#qrlogin_img")
                        except Exception:
                            el = None
                        if el:
                            try:
                                s = el.get_attribute("src") or ""
                            except Exception:
                                s = ""
                            if s:
                                qr = el
                                qr_frame = f
                                qr_src = s
                                break
                    if qr:
                        break
                if qr:
                    break
                time.sleep(0.5)

            if not qr:
                raise RuntimeError("未能在登录页中找到二维码")

            # 等图片真正加载完成
            try:
                qr_frame.wait_for_function(
                    "(e) => e.naturalWidth > 0", arg=qr, timeout=10000
                )
            except Exception:
                pass

            # 优先：同帧 fetch 取真实二维码字节（稳定、无需等渲染）
            png = None
            try:
                b64 = qr_frame.evaluate(
                    """(src) => {
                        return (async () => {
                            const r = await fetch(src, {credentials: 'include'});
                            const blob = await r.blob();
                            const buf = await blob.arrayBuffer();
                            let bin = '';
                            const bytes = new Uint8Array(buf);
                            for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                            return btoa(bin);
                        })();
                    }""",
                    qr_src,
                )
                if b64:
                    png = base64.b64decode(b64)
            except Exception as e:
                self._log(f"fetch 二维码失败，回退截图: {e}")
            # 兜底：直接对元素截图
            if not png or len(png) < 200:
                try:
                    png = qr.screenshot()
                except Exception:
                    pass
            if not png:
                raise RuntimeError("二维码图像获取失败")
            self.qr_b64 = base64.b64encode(png).decode()

            self._pw = p
            self._browser = browser
            self._page = page
            self._extra = extra_pages
            self.status = ST_QRCODE
            self.message = "请用手机 QQ 扫码"
            self.updated_at = time.time()
            self._log("登录页已打开，二维码就绪")
        except Exception as e:
            self.status = ST_FAILED
            self.message = f"打开登录页失败: {e}"
            self._log(f"start err: {e}")
            self._close_impl()

    # ---------- 步骤 2：轮询登录结果 ----------
    def poll(self) -> Dict:
        self.updated_at = time.time()

        if self.status in (ST_EXPIRED, ST_SUCCESS, ST_FAILED):
            return self._view()
        if self.processing:
            return self._view()
        if not self._page:
            self.status = ST_FAILED
            self.message = "浏览器会话丢失，请重新扫码"
            return self._view()

        if time.time() - self.created_at > QR_TTL and self.status == ST_QRCODE:
            # 二维码过期：自动重新打开页面换新码
            self._log("二维码可能已过期，自动换新")
            return self.restart()

        cred = self.read_credential()
        if cred:
            self.processing = True
            self.status = ST_PROCESSING
            self.message = "登录成功，正在获取凭证…"
            try:
                self._finish(cred)
                self._log("凭证已入库")
            except Exception as ex:
                self.status = ST_FAILED
                self.message = f"获取凭证失败: {ex}"
                self._log(f"取凭证异常: {ex}")
            finally:
                self.processing = False
                self.close()
        return self._view()

    def restart(self) -> Dict:
        self.close()
        self._pw = None
        self._browser = None
        self._page = None
        self._extra = []
        self.created_at = time.time()
        self.status = ST_INIT
        return self.start()

    # ---------- 步骤 3：从页面读出凭证 ----------
    _CRED_JS = """() => {
        const out = {};
        const href = location.href;
        const q = href.indexOf('?');
        const h = href.indexOf('#');
        const search = q >= 0 ? href.slice(q + 1, h >= 0 ? h : undefined) : '';
        const hash = h >= 0 ? href.slice(h + 1) : '';
        const params = new URLSearchParams(search + '&' + hash);
        ['openid', 'access_token', 'acctype', 'appid'].forEach(k => {
            const v = params.get(k); if (v) out[k] = v;
        });
        const ump = params.get('urlMiniParams');
        if (ump) { try {
            const o = JSON.parse(ump);
            ['openid','access_token','acctype','appid'].forEach(k => { if (o[k]) out[k] = o[k]; });
        } catch (e) {} }
        try { if (window.Milo && window.Milo.get) {
            ['openid','access_token','acctype','appid'].forEach(k => {
                const v = window.Milo.get(k); if (v) out[k] = v;
            });
        }} catch (e) {}
        try { for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (/openid|access_token|acctype|appid/i.test(k)) {
                const v = localStorage.getItem(k); if (v) out[k] = v;
            }
        }} catch (e) {}
        return out;
    }"""

    def read_credential(self) -> Optional[Dict]:
        try:
            return self._ex.submit(self._read_credential).result(timeout=30)
        except Exception as e:
            self._log(f"读凭证异常: {e}")
            return None

    def _read_credential(self) -> Optional[Dict]:
        self._ensure_loop()
        if not self._page:
            return None
        frames = list(self._page.frames)
        for pg in getattr(self, "_extra", []) or []:
            try:
                frames.extend(pg.frames)
            except Exception:
                pass
        for fr in frames:
            try:
                data = fr.evaluate(self._CRED_JS)
            except Exception:
                # 跨域 frame 可能抛安全异常，跳过
                continue
            openid = (data or {}).get("openid")
            token = (data or {}).get("access_token")
            if openid and token:
                return {
                    "openid": openid,
                    "access_token": token,
                    "acctype": (data.get("acctype") or "qc"),
                    "appid": (data.get("appid") or self.client_id),
                }
        return None

    # ---------- 步骤 4：入库 + 验证 ----------
    def _finish(self, cred: Dict):
        token = cred.get("access_token")
        openid = cred.get("openid")
        if not token or not openid:
            raise RuntimeError("凭证不完整（缺 openid 或 access_token）")

        appid = cred.get("appid") or self.client_id
        acctype = cred.get("acctype") or "qc"

        name = f"扫码登录 {time.strftime('%m-%d %H:%M')}"
        new_id = ams_client.add_credential(
            name=name, openid=openid, access_token=token,
            acctype=acctype, appid=appid, remark="扫码自动配置(浏览器)",
        )

        verify = self._verify(new_id)

        self.result = {
            "credential_id": new_id,
            "credential_name": name,
            "openid_masked": openid[:6] + "***" + openid[-4:],
            "appid": appid,
            "acctype": acctype,
            "verify": verify,
        }
        self.status = ST_SUCCESS
        self.message = (
            "凭证已保存，接口验证通过" if verify.get("ok")
            else f"凭证已保存，但接口验证未通过：{verify.get('msg', '')}"
        )

    def _verify(self, cid: int) -> Dict:
        res = ams_client.test_credential(cid)
        # 兜底：如果 cid 查不到（极端情况），按名称回查刚插入的那条
        if not res.get("ok") and res.get("msg") == "凭证不存在":
            try:
                from ..core.database import query_one
                row = query_one(
                    "SELECT id FROM credential WHERE name = :n ORDER BY id DESC LIMIT 1",
                    {"n": self.result.get("credential_name", "")} if hasattr(self, 'result') and self.result else {},
                )
                if row and row["id"] != cid:
                    res = ams_client.test_credential(row["id"])
            except Exception:
                pass
        if not res.get("ok"):
            try:
                prev = ams_client.current_stat_hour(1)
                res2 = ams_client.test_credential(cid, stat_hour=prev)
                if res2.get("ok"):
                    res2["note"] = f"当前小时无数据，已用 {prev} 验证"
                    return res2
            except Exception:
                pass
        return res

    def _close_impl(self):
        """真正释放浏览器资源（必须在专用线程 self._ex 里跑）。"""
        for pg in getattr(self, "_extra", []) or []:
            try:
                pg.close()
            except Exception:
                pass
        self._extra = []
        for attr in ("_page", "_browser", "_pw"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                if attr == "_page":
                    obj.close()
                elif attr == "_browser":
                    obj.close()
                else:
                    obj.stop()
            except Exception:
                pass
            setattr(self, attr, None)

    def close(self):
        ex = getattr(self, "_ex", None)
        if ex is not None:
            try:
                ex.submit(self._close_impl).result(timeout=20)
            except Exception:
                pass
            try:
                ex.shutdown(wait=False)
            except Exception:
                pass
            self._ex = None

    def _log(self, msg: str):
        t = time.strftime("%H:%M:%S")
        self.log.append(f"{t} {msg}")
        if len(self.log) > 30:
            self.log = self.log[-30:]
        print(f"[QR {self.sid[:8]}] {msg}", flush=True)

    # ---------- 输出 ----------
    def _view(self) -> Dict:
        left = int(QR_TTL - (time.time() - self.created_at))
        return {
            "sid": self.sid,
            "status": self.status,
            "message": self.message,
            "qr": self.qr_b64 if self.status in (ST_QRCODE, ST_SCANNED) else None,
            "expires_in": max(0, left),
            "result": self.result,
            "raw": self.last_raw,
            "log": self.log[-8:],
        }


# ---------------- 会话池 ----------------
_sessions: Dict[str, QrLoginSession] = {}
_lock = threading.Lock()


def _cleanup_locked():
    now = time.time()
    for sid, s in list(_sessions.items()):
        if now - s.updated_at > SESSION_TTL:
            try:
                s.close()
            except Exception:
                pass
            _sessions.pop(sid, None)


def create_session() -> QrLoginSession:
    with _lock:
        _cleanup_locked()
        s = QrLoginSession()
        _sessions[s.sid] = s
    s.start()
    return s


def get_session(sid: str) -> Optional[QrLoginSession]:
    with _lock:
        _cleanup_locked()
        return _sessions.get(sid)


def poll_session(sid: str) -> Dict:
    s = get_session(sid)
    if not s:
        return {"status": ST_EXPIRED, "message": "会话已过期，请重新扫码",
                "qr": None, "expires_in": 0, "result": None}
    return s.poll()


def cancel_session(sid: str) -> bool:
    with _lock:
        s = _sessions.pop(sid, None)
    if s:
        try:
            s.close()
        except Exception:
            pass
        return True
    return False


def session_count() -> int:
    with _lock:
        _cleanup_locked()
        return len(_sessions)
