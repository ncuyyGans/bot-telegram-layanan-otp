"""Konektor OTPCepat — https://otpcepat.org

API: semua via GET dengan query param api_key, base:
    https://otpcepat.org/api/handler_api.php
Respons JSON {"status":"true"/true, ...} atau {"status":"false","msg":"..."}.
Alur order: negara -> layanan (per negara) -> operator -> get_order.
"""
import re
import urllib.parse

from .base import OTPProvider, http_request, ok, fail

BASE_URL = "https://otpcepat.org/api/handler_api.php"


class OTPCepatProvider(OTPProvider):
    name = "otpcepat"
    title = "OTPCepat"

    # Di OTPCepat, Indonesia TIDAK bernama "Indonesia" — namanya
    # "Wakanda (Indo)" (terverifikasi live 2026-10-09, country_id=6).
    INDONESIA_KEYS = ("wakanda (indo)", "wakanda", "indonesia")

    ERROR_MAP = {
        "You don't have Access!": "🔑 API key salah / tidak punya akses — cek lagi dengan /setkey otpcepat.",
        "BAD ACTION": "Action API tidak dikenal (bug konektor?).",
        "NO BALANCE": "💸 Saldo kurang — deposit dulu di web OTPCepat.",
        "INVALID OPERATOR": "Operator tidak valid.",
        "INVALID SERVICE ID": "Layanan tidak valid.",
        "WRONG SERVICE ID": "ID layanan salah.",
        "WRONG OPERATOR ID": "ID operator salah.",
        "WRONG COUNTRY ID": "ID negara salah.",
        "INVALID ORDER ID": "Order ID tidak valid.",
        "WRONG ORDER ID": "Order ID salah.",
        "INVALID STATUS": "Status tidak valid.",
    }

    # -- internal --
    # OTPCepat memakai Cloudflare Browser Integrity Check: request tanpa
    # User-Agent browser diblokir (error 1010). Wajib kirim UA browser.
    BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36")

    def _get(self, action, params=None):
        q = {"api_key": self.api_key, "action": action}
        q.update(params or {})
        # safe=":" agar surrogate vault (hsurr:...) tidak ter-encode
        url = BASE_URL + "?" + urllib.parse.urlencode(q, safe=":")
        r = http_request("GET", url,
                         headers={"User-Agent": self.BROWSER_UA,
                                  "Accept": "application/json"})
        if r["error"]:
            return None, "🌐 Gangguan jaringan: " + r["error"]
        if r["http"] == 403 and "error code:" in (r["text"] or "").lower():
            return None, ("🛡️ Diblokir proteksi Cloudflare OTPCepat — "
                          "coba lagi sebentar.")
        return r, None

    @staticmethod
    def _status_ok(d):
        return str(d.get("status")).lower() == "true"

    def _fail_msg(self, d, default="Gagal memproses respons OTPCepat."):
        msg = str(d.get("msg") or "")
        if msg:
            return fail(self.translate(msg, msg))
        return fail(default)

    def _data(self, r):
        d = r["json"] if isinstance(r["json"], dict) else {}
        if not self._status_ok(d):
            return None, d
        return d, None

    @staticmethod
    def _num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def extract_code(text):
        """Ambil kode OTP (4-8 digit) dari isi SMS."""
        m = re.findall(r"\d{4,8}", str(text or ""))
        return m[0] if m else None

    # -- API --
    def get_balance(self):
        r, err = self._get("getBalance")
        if err:
            return fail(err)
        d, derr = self._data(r)
        if derr is not None:
            return self._fail_msg(derr)
        data = d.get("data") or {}
        bal = self._num(data.get("saldo"))
        if bal is None:
            return fail("Format saldo tak dikenal.")
        return ok({"balance": bal})

    def get_countries(self):
        r, err = self._get("getCountries")
        if err:
            return fail(err)
        d, derr = self._data(r)
        if derr is not None:
            return self._fail_msg(derr)
        out = []
        for c in (d.get("data") or []):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("countryID") or "")
            if cid:
                out.append({"id": cid,
                            "name": c.get("countryName") or cid})
        if not out:
            return fail("Gagal mengambil daftar negara.")
        return ok({"countries": out})

    def get_operators(self, country):
        r, err = self._get("getOperators", {"country_id": country})
        if err:
            return fail(err)
        d, derr = self._data(r)
        if derr is not None:
            return self._fail_msg(derr)
        ops = []
        for o in (d.get("data") or []):
            # format docs: ["random", "operatorName", ...]
            if isinstance(o, str) and o:
                ops.append(o)
            elif isinstance(o, dict):
                oid = str(o.get("operatorID") or o.get("id") or "")
                if oid:
                    ops.append(oid)
        # "random" selalu tersedia sebagai pilihan acak termurah
        if "random" not in ops:
            ops.insert(0, "random")
        return ok({"operators": ops})

    def get_services(self, country=None):
        if not country:
            return fail("Negara wajib dipilih untuk OTPCepat.")
        r, err = self._get("getServices", {"country_id": country})
        if err:
            return fail(err)
        d, derr = self._data(r)
        if derr is not None:
            return self._fail_msg(derr)
        out = []
        for s in (d.get("data") or []):
            if not isinstance(s, dict):
                continue
            code = str(s.get("serviceID") or "")
            if not code:
                continue
            out.append({"code": code,
                        "name": s.get("serviceName") or code,
                        "price": self._num(s.get("price")),
                        "stock": None})
        if not out:
            return fail("Tidak ada layanan di negara ini.")
        return ok({"services": out})

    def get_special_services(self):
        r, err = self._get("getSpecialServices")
        if err:
            return fail(err)
        d, derr = self._data(r)
        if derr is not None:
            return self._fail_msg(derr)
        out = [{"code": str(s.get("serviceID") or ""),
                "name": s.get("serviceName") or "",
                "price": self._num(s.get("price")), "stock": None}
               for s in (d.get("data") or [])
               if isinstance(s, dict) and s.get("serviceID")]
        return ok({"services": out})

    def get_price(self, service, country=None):
        res = self.get_services(country)
        if not res["ok"]:
            return res
        for s in res["data"]["services"]:
            if s["code"] == str(service):
                return ok({"options": [{"price": s["price"], "stock": None,
                                        "country_id": country}]})
        return fail("Layanan tidak ditemukan di negara ini.")

    def order(self, service, country=None, operator="random"):
        if not country:
            return fail("Negara wajib dipilih untuk OTPCepat.")
        r, err = self._get("get_order", {
            "operator_id": operator or "random",
            "service_id": str(service),
            "country_id": str(country),
        })
        if err:
            return fail(err)
        d, derr = self._data(r)
        if derr is not None:
            return self._fail_msg(derr)
        data = d.get("data") or {}
        if not data.get("order_id"):
            return fail("Respons order tak dikenal.")
        return ok({"order_id": str(data["order_id"]),
                   "phone": str(data.get("number") or ""),
                   "price": self._num(data.get("price"))})

    def check(self, order_id):
        r, err = self._get("get_status", {"order_id": order_id})
        if err:
            return fail(err)
        d, derr = self._data(r)
        if derr is not None:
            return self._fail_msg(derr)
        data = d.get("data") or {}
        status = str(data.get("status") or "")
        slow = status.lower()
        if slow == "waiting sms":
            return ok({"state": "wait", "code": None})
        if slow == "recieved":  # sic: typo di dokumentasi API
            code = self.extract_code(data.get("sms"))
            return ok({"state": "ok", "code": code,
                       "sms": data.get("sms")})
        if slow == "cancel":
            return ok({"state": "cancel", "code": None})
        if slow == "done":
            code = self.extract_code(data.get("sms"))
            return ok({"state": "done", "code": code})
        return fail(f"Status tak dikenal: {status or '?'}")

    def _set_status(self, order_id, status):
        r, err = self._get("set_status",
                           {"order_id": order_id, "status": status})
        if err:
            return fail(err)
        d, derr = self._data(r)
        if derr is not None:
            return self._fail_msg(derr)
        return ok({"raw": d})

    def cancel(self, order_id):
        return self._set_status(order_id, 2)

    def resend(self, order_id):
        return self._set_status(order_id, 3)

    def finish(self, order_id):
        return self._set_status(order_id, 4)
