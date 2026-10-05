"""Konektor OTP Instan — https://otpinstan.com

Autentikasi via HEADER "X-Api-Key" (JANGAN pernah taruh key di URL).
Rate limit 300 req/menit per key. Order aktif 20 menit; cancel baru
boleh setelah 2 menit (CANCEL_TOO_EARLY).

Server s1 diimplementasikan penuh. Server s2..s5 memakai endpoint yang
sama dengan pemetaan parameter best-effort (identifier layanan berbeda
per server) — sesuaikan bila provider mengubah formatnya.
"""
import re

from .base import OTPProvider, http_request, ok, fail

SERVERS = {
    # base, prefix order id, gaya identifier layanan untuk order
    "s1": {"base": "https://otpinstan.com/api/reseller/s1/",
           "prefix": "S1-", "order_style": "platform"},
    "s2": {"base": "https://otpinstan.com/api/reseller/",
           "prefix": "S6-", "order_style": "service_kode"},
    "s3": {"base": "https://otpinstan.com/api/reseller/s3/",
           "prefix": "S3-", "order_style": "service_angka"},
    "s4": {"base": "https://otpinstan.com/api/reseller/s4/",
           "prefix": "S9-", "order_style": "service_angka"},
    "s5": {"base": "https://otpinstan.com/api/reseller/s5/",
           "prefix": "S5-", "order_style": "service_kode"},
}

CANCEL_MIN_AGE = 120  # detik; aturan anti-abuse OTP Instan

# balance.php & history.php adalah endpoint SHARED — selalu di base ini,
# tidak mengikuti base server (s1..s5).
SHARED_BASE = "https://otpinstan.com/api/reseller/"


class OTPInstanProvider(OTPProvider):
    name = "otpinstan"
    title = "OTP Instan"

    ERROR_MAP = {
        "PRODUCT_NOT_FOUND": "Produk tidak ditemukan (pakai platform_id + country_id, jangan hardcode product_id).",
        "OUT_OF_STOCK": "📵 Stok habis untuk layanan ini.",
        "NO_NUMBERS": "📵 Stok nomor habis.",
        "NO_PRICE": "Layanan tidak tersedia / harga tidak ada.",
        "LOW_BALANCE": "💸 Saldo kurang — deposit dulu di web OTP Instan.",
        "CANCEL_TOO_EARLY": "⏳ Order baru bisa dibatalkan setelah 2 menit.",
        "CONFLICT": "SMS sudah masuk — tidak bisa minta ulang.",
        "BAD_SERVICE": "Layanan tidak valid.",
        "BAD_COUNTRY": "Negara tidak valid.",
    }

    def __init__(self, api_key, server="s1"):
        super().__init__(api_key)
        self.server = server if server in SERVERS else "s1"
        self.base = SERVERS[self.server]["base"]

    # -- internal --
    def _headers(self):
        return {"X-Api-Key": self.api_key}

    def _req(self, method, path, params=None, json_body=None):
        url = self.base + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        r = http_request(method, url, headers=self._headers(),
                         json_body=json_body)
        if r["error"]:
            return None, "🌐 Gangguan jaringan: " + r["error"]
        if r["http"] == 401:
            return None, "🔑 API key salah — cek lagi dengan /setkey otpinstan."
        if r["http"] == 403:
            return None, "⛔ Akun dinonaktifkan — hubungi OTP Instan."
        if r["http"] == 429:
            return None, "⏳ Kena rate limit — tunggu sebentar."
        return r, None

    def _err(self, d):
        code = ""
        msg = ""
        if isinstance(d, dict):
            code = str(d.get("error") or d.get("code") or "")
            msg = str(d.get("message") or "")
        return self.translate(code, msg or f"Error: {code or 'unknown'}")

    # -- API --
    def get_balance(self):
        # endpoint shared — selalu pakai SHARED_BASE, bukan base server
        r = http_request("GET", SHARED_BASE + "balance.php",
                         headers=self._headers())
        if r["error"]:
            return fail("🌐 Gangguan jaringan: " + r["error"])
        if r["http"] == 401:
            return fail("🔑 API key salah — cek lagi dengan /setkey otpinstan.")
        if r["http"] == 403:
            return fail("⛔ Akun dinonaktifkan — hubungi OTP Instan.")
        d = r["json"] if isinstance(r["json"], dict) else {}
        if d.get("success") and "balance" in d:
            try:
                return ok({"balance": int(d["balance"])})
            except (ValueError, TypeError):
                pass
        return fail(self._err(d))

    def get_countries(self):
        r, err = self._req("GET", "countries.php")
        if err:
            return fail(err)
        d = r["json"]
        items = self._as_list(d)
        out = []
        for c in items:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or c.get("country_id") or c.get("code") or "")
            name = c.get("name") or c.get("country") or cid
            if cid:
                out.append({"id": cid, "name": name})
        if not out:
            return fail("Gagal mengambil daftar negara.")
        return ok({"countries": out})

    def get_services(self, country=None):
        params = {"country_id_": country} if country else {}
        r, err = self._req("GET", "services.php", params=params)
        if err:
            return fail(err)
        items = self._as_list(r["json"])
        out = []
        for s in items:
            if not isinstance(s, dict):
                continue
            code = str(s.get("platform_id") or s.get("service_kode")
                       or s.get("service_angka") or s.get("product_id")
                       or s.get("id") or "")
            name = s.get("platform_name") or s.get("name") or code
            price = self._num(s.get("price") or s.get("harga"))
            stock = self._num(s.get("stock") or s.get("stok")
                              or s.get("available"))
            if code:
                out.append({"code": code, "name": name,
                            "price": price, "stock": stock})
        if not out:
            return fail(self._err(r["json"] if isinstance(r["json"], dict)
                                  else {}))
        return ok({"services": out})

    def get_price(self, service, country=None):
        res = self.get_services(country)
        if not res["ok"]:
            return res
        for s in res["data"]["services"]:
            if s["code"] == str(service):
                return ok({"options": [{"price": s["price"],
                                        "stock": s["stock"],
                                        "country_id": str(country or "")}]})
        return fail("Layanan tidak ditemukan di negara ini.")

    def order(self, service, country=None):
        if not country:
            return fail("Negara wajib dipilih untuk OTP Instan.")
        style = SERVERS[self.server]["order_style"]
        # REKOMENDASI docs: platform_id + country_id (s1). Server lain
        # memakai identifier-nya masing-masing (best-effort).
        if style == "platform":
            payload = {"platform_id": str(service),
                       "country_id": str(country)}
        else:
            payload = {style: str(service), "country_id": str(country)}
        r, err = self._req("POST", "order.php", json_body=payload)
        if err:
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        if d.get("success"):
            return ok({"order_id": d.get("order_id"),
                       "phone": d.get("phone"),
                       "price": self._num(d.get("price"))})
        return fail(self._err(d))

    def check(self, order_id):
        r, err = self._req("GET", "check.php",
                           params={"order_id": order_id})
        if err:
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        if not d.get("success", True):
            return fail(self._err(d))
        status = str(d.get("status") or d.get("state") or "").lower()
        otp = d.get("otp") or d.get("sms_code") or d.get("code")
        sms_text = d.get("sms_text") or d.get("sms") or ""
        if not otp and sms_text:
            m = re.search(r"\d{4,8}", str(sms_text))
            if m:
                otp = m.group(0)
        if otp:
            return ok({"state": "ok", "code": str(otp),
                       "sms_text": str(sms_text or "")})
        if status in ("received", "done", "success", "completed"):
            return ok({"state": "ok", "code": None,
                       "sms_text": str(sms_text or "")})
        if status in ("cancelled", "canceled"):
            return ok({"state": "cancel", "code": None})
        if status in ("expired",):
            return ok({"state": "expired", "code": None})
        return ok({"state": "wait", "code": None, "raw_status": status})

    def cancel(self, order_id):
        r, err = self._req("POST", "cancel.php",
                           json_body={"order_id": order_id})
        if err:
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        if d.get("success"):
            return ok({"raw": d})
        return fail(self._err(d))

    def resend(self, order_id):
        r, err = self._req("POST", "resend.php",
                           json_body={"order_id": order_id})
        if err:
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        if d.get("success"):
            return ok({"raw": d})
        return fail(self._err(d))

    def finish(self, order_id):
        # OTP Instan tidak punya endpoint finish eksplisit — bot cukup
        # menghentikan polling & mengarsipkan order secara lokal.
        return ok({"local": True})

    # -- helper --
    @staticmethod
    def _as_list(d):
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for key in ("services", "countries", "data", "items", "result"):
                if isinstance(d.get(key), list):
                    return d[key]
        return []

    @staticmethod
    def _num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
