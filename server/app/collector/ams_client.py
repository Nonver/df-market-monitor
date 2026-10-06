"""腾讯官方 AMS 接口客户端 + 凭证池"""
import time
from datetime import datetime, date
from typing import Dict, List, Optional

import requests

from ..core import config
from ..core.database import query, query_one, execute
from ..core.security import encrypt_token, decrypt_token

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "content-type": "application/x-www-form-urlencoded",
}


# ---------------- 凭证池 ----------------
def list_credentials() -> List[Dict]:
    rows = query(
        "SELECT id, name, openid, acctype, appid, status, success_count, fail_count,"
        " last_success_at, last_fail_at, last_error, daily_quota, used_today,"
        " quota_date, remark, created_at FROM credential ORDER BY id DESC"
    )
    for r in rows:
        r["openid_masked"] = (
            (r["openid"][:6] + "***" + r["openid"][-4:]) if r.get("openid") else ""
        )
    return rows


def add_credential(name: str, openid: str, access_token: str, acctype: str,
                   appid: str = None, remark: str = "") -> int:
    from ..core.database import insert_return_id
    return insert_return_id(
        """INSERT INTO credential
           (name, openid, access_token, acctype, appid, status, remark, created_at)
           VALUES (:n, :o, :t, :a, :ap, 'active', :r, NOW())""",
        {
            "n": name,
            "o": openid,
            "t": encrypt_token(access_token),
            "a": acctype,
            "ap": appid or config.get_str("ams_appid", "101491592"),
            "r": remark,
        },
    )


def update_credential(cid: int, **fields) -> int:
    allowed = {"name", "openid", "acctype", "appid", "status", "daily_quota", "remark"}
    if "access_token" in fields:
        fields["access_token"] = encrypt_token(fields.pop("access_token"))
    sets, params = [], {"id": cid}
    for k, v in fields.items():
        if k in allowed or k == "access_token":
            sets.append(f"{k} = :{k}")
            params[k] = v
    if not sets:
        return 0
    return execute(f"UPDATE credential SET {', '.join(sets)} WHERE id = :id", params)


def delete_credential(cid: int) -> int:
    return execute("DELETE FROM credential WHERE id = :id", {"id": cid})


def pick_credential() -> Optional[Dict]:
    """选一个可用凭证：按当日已用次数升序，避开失败的"""
    today = date.today().isoformat()
    execute("UPDATE credential SET used_today = 0, quota_date = :d "
            "WHERE quota_date IS NULL OR quota_date <> :d", {"d": today})
    rows = query(
        """SELECT * FROM credential
           WHERE status = 'active' AND used_today < daily_quota
           ORDER BY used_today ASC, fail_count ASC, id ASC LIMIT 1"""
    )
    if not rows:
        return None
    cred = rows[0]
    cred["access_token"] = decrypt_token(cred.get("access_token") or "")
    return cred


def mark_result(cid: int, ok: bool, error: str = ""):
    if ok:
        execute(
            """UPDATE credential SET success_count = success_count + 1,
               used_today = used_today + 1, quota_date = CURDATE(),
               last_success_at = NOW(), last_error = NULL WHERE id = :id""",
            {"id": cid},
        )
    else:
        execute(
            """UPDATE credential SET fail_count = fail_count + 1,
               used_today = used_today + 1, quota_date = CURDATE(),
               last_fail_at = NOW(), last_error = :e WHERE id = :id""",
            {"id": cid, "e": (error or "")[:250]},
        )


# ---------------- AMS 接口 ----------------
class AmsClient:
    """官方小时均价接口封装"""

    def __init__(self, credential: Dict):
        self.cred = credential
        self.base_url = config.get_str(
            "ams_base_url", "https://comm.ams.game.qq.com/ide/"
        )
        self.chart_id = config.get_str("ams_chart_id", "329101")
        self.sub_chart_id = config.get_str("ams_sub_chart_id", "329101")
        self.ide_token = config.get_str("ams_ide_token", "qkJMkB")
        self.appid = credential.get("appid") or config.get_str("ams_appid", "101491592")
        self.session = requests.Session()

    def _cookie(self) -> str:
        return (
            f"openid={self.cred.get('openid','')}; "
            f"acctype={self.cred.get('acctype','qc')}; "
            f"appid={self.appid}; "
            f"access_token={self.cred.get('access_token','')}"
        )

    def fetch_price(self, propid: str, stat_hour: str) -> Dict:
        """返回 {'ok':bool, 'price':float|None, 'ret':int, 'msg':str}"""
        url = (
            f"{self.base_url}?iChartId={self.chart_id}"
            f"&iSubChartId={self.sub_chart_id}&sIdeToken={self.ide_token}"
            f"&propid={propid}&stat_hour={stat_hour}"
        )
        headers = dict(DEFAULT_HEADERS)
        headers["Cookie"] = self._cookie()
        try:
            resp = self.session.post(url, headers=headers, timeout=20)
            data = resp.json()
        except Exception as e:
            return {"ok": False, "price": None, "ret": -1, "msg": f"请求异常: {e}"}

        ret = data.get("ret", -1)
        if ret != 0:
            return {"ok": False, "price": None, "ret": ret,
                    "msg": data.get("sMsg", "未知错误")}

        try:
            jd = data.get("jData", {}).get("data", {})
            # 只认当前 propid 对应的节点，避免误用其它物品的报价
            node = jd.get(propid)
            price = node.get("avg_buyprice") if isinstance(node, dict) else None
        except Exception:
            price = None

        if price is None:
            return {"ok": False, "price": None, "ret": ret, "msg": "返回中无价格字段"}
        return {"ok": True, "price": float(price), "ret": 0, "msg": "ok"}


def test_credential(cid: int, propid: str = None, stat_hour: str = None) -> Dict:
    """后台测试某个凭证是否可用"""
    row = query_one("SELECT * FROM credential WHERE id = :id", {"id": cid})
    if not row:
        return {"ok": False, "msg": "凭证不存在"}
    row["access_token"] = decrypt_token(row.get("access_token") or "")
    if not propid:
        r = query_one("SELECT propid FROM items WHERE watch_enabled=1 LIMIT 1")
        propid = r["propid"] if r else "15010050001"
    if not stat_hour:
        stat_hour = datetime.now().strftime("%Y%m%d%H")
    client = AmsClient(row)
    res = client.fetch_price(propid, stat_hour)
    mark_result(cid, res["ok"], res.get("msg", ""))
    res["propid"] = propid
    res["stat_hour"] = stat_hour
    return res


def current_stat_hour(offset_hours: int = 0) -> str:
    t = datetime.now()
    if offset_hours:
        from datetime import timedelta
        t = t - timedelta(hours=offset_hours)
    return t.strftime("%Y%m%d%H")
