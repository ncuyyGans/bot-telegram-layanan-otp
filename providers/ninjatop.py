"""Konektor NinjaOTP — https://app.ninjatop.cloud

Base: https://app.ninjatop.cloud/api/public/v1
Auth: header "Authorization: Bearer <api_key>" (key format nk_xxxx).
Semua request/response JSON. Tidak ada konsep negara — layanan
langsung punya harga & stok, jadi has_countries = False (bot melewati
langkah pilih negara di wizard order).

Rate limit default 60 req/menit per key; polling 5 detik = 12 req/menit.
"""
import re
import uuid

from .base import OTPProvider, http_request, ok, fail

BASE_URL = "https://app.ninjatop.cloud/api/public/v1"


class NinjaTopProvider(OTPProvider):
    name = "ninjatop"
    title = "NinjaOTP"
    has_countries = False

    ERROR_MAP = {
        "UNAUTHENTICATED": "🔑 API key salah/hilang — cek lagi dengan /setkey ninjatop.",
        "FORBIDDEN": "⛔ API key tidak punya izin untuk endpoint ini.",
        "INSUFFICIENT_BALANCE": "💸 Saldo kurang — deposit dulu di dashboard NinjaOTP.",
        "OUT_OF_STOCK": "📵 Stok habis untuk layanan ini.",
        "NOT_FOUND": "Order/layanan tidak ditemukan.",
        "CONFLICT": "Order sudah tidak pending — tidak bisa diproses.",
        "VALIDATION": "Parameter tidak valid.",
        "RATE_LIMITED": "⏳ Kena rate limit — tunggu sebentar.",
        "API_DISABLED": "🚧 API NinjaOTP sedang nonaktif.",
    }

    # -- internal --
    # NinjaOTP memakai Cloudflare Browser Integrity Check: request tanpa
    # User-Agent browser diblokir (error 1010). Wajib kirim UA browser.
    BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36")

    def _headers(self):
        return {"Authorization": "Bearer " + self.api_key,
                "User-Agent": self.BROWSER_UA,
                "Accept": "application/json"}

    def _req(self, method, path, params=None, json_body=None,
             extra_headers=None):
        url = BASE_URL + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        h = self._headers()
        if extra_headers:
            h.update(extra_headers)
        r = http_request(method, url, headers=h, json_body=json_body)
        if r["error"]:
            return None, "🌐 Gangguan jaringan: " + r["error"]
        if r["http"] == 401:
            return None, self.translate("UNAUTHENTICATED")
        if r["http"] == 403:
            # bedakan blokir Cloudflare (WAF) vs FORBIDDEN dari API-nya
            body = (r["text"] or "").lower()
            if "cloudflare" in body or "error code:" in body:
                return None, ("🛡️ Diblokir proteksi Cloudflare NinjaOTP — "
                              "coba lagi sebentar.")
            return None, self.translate("FORBIDDEN")
        if r["http"] == 429:
            return None, self.translate("RATE_LIMITED")
        return r, None

    def _err(self, r):
        """Ambil pesan error dari format {"error":{"code":..,"message":..}}."""
        d = r["json"] if isinstance(r["json"], dict) else {}
        e = d.get("error") if isinstance(d.get("error"), dict) else {}
        code = str(e.get("code") or d.get("code") or "")
        msg = str(e.get("message") or d.get("message") or "")
        if r["http"] == 402:
            code = "INSUFFICIENT_BALANCE"
        elif r["http"] == 404 and not code:
            code = "OUT_OF_STOCK"
        return self.translate(code, msg or f"HTTP {r['http']}")

    @staticmethod
    def _order_obj(d):
        """Ambil objek order dari berbagai bentuk respons."""
        if not isinstance(d, dict):
            return {}
        for key in ("data", "order"):
            v = d.get(key)
            if isinstance(v, dict):
                return v
        return d

    # -- API --
    def get_balance(self):
        r, err = self._req("GET", "/balance")
        if err:
            return fail(err)
        d = self._order_obj(r["json"])
        if "balance" in d:
            try:
                return ok({"balance": int(d["balance"])})
            except (ValueError, TypeError):
                pass
        return fail(self._err(r))

    def get_services(self, country=None):
        r, err = self._req("GET", "/services")
        if err:
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        items = d.get("data") if isinstance(d.get("data"), list) else []
        out = []
        for s in items:
            if not isinstance(s, dict) or s.get("id") is None:
                continue
            out.append({
                "code": str(s.get("id")),
                "name": s.get("name") or str(s.get("code") or s.get("id")),
                "price": self._num(s.get("price")),
                "stock": self._num(s.get("available_count")),
            })
        if not out:
            return fail(self._err(r))
        return ok({"services": out})

    def get_countries(self):
        # NinjaOTP tidak punya konsep negara.
        return ok({"countries": []})

    def get_price(self, service, country=None):
        res = self.get_services()
        if not res["ok"]:
            return res
        for s in res["data"]["services"]:
            if s["code"] == str(service):
                return ok({"options": [{"price": s["price"],
                                        "stock": s["stock"],
                                        "country_id": ""}]})
        return fail("Layanan tidak ditemukan.")

    def order(self, service, country=None):
        try:
            service_id = int(service)
        except (ValueError, TypeError):
            return fail("ID layanan tidak valid.")
        r, err = self._req(
            "POST", "/orders",
            json_body={"service_id": service_id, "qty": 1},
            extra_headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        if err:
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        orders = d.get("orders") if isinstance(d.get("orders"), list) else []
        o = orders[0] if orders else self._order_obj(d)
        if o.get("id") and o.get("phone_number"):
            return ok({"order_id": str(o["id"]),
                       "phone": str(o["phone_number"]),
                       "price": self._num(o.get("price")),
                       "expire_at": o.get("expire_at")})
        return fail(self._err(r))

    def check(self, order_id):
        r, err = self._req("GET", f"/orders/{order_id}")
        if err:
            return fail(err)
        o = self._order_obj(r["json"])
        status = str(o.get("status") or "").lower()
        code = o.get("otp_code")
        otp_text = o.get("otp_text") or ""
        if not code and otp_text:
            m = re.search(r"\d{4,8}", str(otp_text))
            if m:
                code = m.group(0)
        if status == "completed" or code:
            return ok({"state": "ok",
                       "code": str(code) if code else None,
                       "sms_text": str(otp_text or "")})
        if status == "cancelled":
            return ok({"state": "cancel", "code": None})
        if status in ("expired",):
            return ok({"state": "expired", "code": None})
        if status == "pending" or not status:
            return ok({"state": "wait", "code": None,
                       "raw_status": status})
        return ok({"state": "wait", "code": None, "raw_status": status})

    def cancel(self, order_id):
        r, err = self._req("POST", f"/orders/{order_id}/cancel")
        if err:
            return fail(err)
        if r["http"] in (200, 201, 202):
            return ok({"raw": r["json"]})
        return fail(self._err(r))

    def resend(self, order_id):
        r, err = self._req("POST", f"/orders/{order_id}/resend")
        if err:
            return fail(err)
        if r["http"] in (200, 201, 202):
            return ok({"raw": r["json"]})
        return fail(self._err(r))

    def finish(self, order_id):
        r, err = self._req("POST", f"/orders/{order_id}/ack")
        if err:
            return fail(err)
        if r["http"] in (200, 201, 202):
            return ok({"raw": r["json"]})
        return fail(self._err(r))

    def reactivate(self, order_id):
        """Tambahan: sewa ulang nomor yang sama (kena biaya lagi)."""
        r, err = self._req("POST", "/orders/reactivate",
                           json_body={"order_id": order_id})
        if err:
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        if r["http"] in (200, 201, 202):
            return ok({"raw": d})
        return fail(self._err(r))

    def get_active(self):
        """Tambahan: daftar order yang masih berjalan."""
        r, err = self._req("GET", "/orders/active")
        if err:
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        items = d.get("data") if isinstance(d.get("data"), list) else []
        return ok({"orders": items})

    # -- helper --
    @staticmethod
    def _num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
