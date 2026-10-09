"""Konektor DehuyOTPWA (WAHub OTP Platform) — https://dehuyzotp.shop

Auth: header "Authorization: Bearer <api_key>" (key format wh_live_...).
Base: https://dehuyzotp.shop/api — semua request/response JSON.

Pay-per-Success: saldo hanya terpotong permanen bila OTP berhasil
diterima (saat order, saldo hanya DITAHAN).

Tidak ada konsep negara (layanan global) -> has_countries = False,
wizard langsung pilih layanan seperti NinjaOTP.

Endpoint yang dipakai bot:
  GET  /api/services            -> [{id,name,price,stock,allow_retry}]
  GET  /api/balance             -> {balance,reserved,available}
  POST /api/rent {service_id}   -> {order_id,token,phone,expires_at,allow_retry}
  GET  /api/order/{id}          -> status (id = order_id ATAU token)
  POST /api/order/{id} {action} -> action: cancel | done
  POST /api/rent/{token}/retry  -> Re-OTP gratis (maks 5x; 409 bila
                                   layanan sekali-pakai / batas tercapai)

Polling bot memakai GET /api/order/{id} (tanpa blocking), BUKAN
/api/sms/{token} yang long-polling (blocking s.d. 120 detik).
"""
import re

from .base import OTPProvider, http_request, ok, fail

BASE_URL = "https://dehuyzotp.shop/api"

RENT_TTL = 20 * 60  # detik; sewa kedaluwarsa 20 menit (per docs 409)


class DehuyProvider(OTPProvider):
    name = "dehuy"
    title = "DehuyOTPWA"
    has_countries = False

    # allow_retry=False -> layanan sekali-pakai; API menolak retry (409).
    # Bot menyembunyikan tombol "🔁 Minta Ulang" untuk order ini
    # (lihat send_otp_message di otpbot.py).

    # -- internal --
    def _headers(self):
        return {"Authorization": "Bearer " + self.api_key,
                "Accept": "application/json"}

    def _req(self, method, path, json_body=None):
        r = http_request(method, BASE_URL + path, headers=self._headers(),
                         json_body=json_body)
        if r["error"]:
            return None, "🌐 Gangguan jaringan: " + r["error"]
        if r["http"] == 401:
            return None, "🔑 API key salah — cek lagi dengan /setkey dehuy."
        if r["http"] == 403:
            return None, "⛔ Akun diblokir admin — hubungi DehuyOTPWA."
        if r["http"] == 429:
            return None, ("⏳ Batas sewa bersamaan tercapai — selesaikan/"
                          "batalkan order lama dulu.")
        if r["http"] == 503:
            return None, "📵 Stok habis untuk layanan ini."
        if r["http"] == 404:
            return None, "NOT_FOUND"
        return r, None

    def _err(self, r, default):
        d = r["json"] if isinstance(r["json"], dict) else {}
        msg = str(d.get("message") or d.get("error") or "").strip()
        return msg or default

    @staticmethod
    def _num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _norm_phone(phone):
        """'85196495750' -> '6285196495750' (format webhook)."""
        s = re.sub(r"\D", "", str(phone or ""))
        if s.startswith("62"):
            return s
        if s.startswith("0"):
            return "62" + s[1:]
        if s.startswith("8"):
            return "62" + s
        return s

    # -- API --
    def get_balance(self):
        r, err = self._req("GET", "/balance")
        if err:
            return fail(err if err != "NOT_FOUND" else "Endpoint tak dikenal.")
        d = r["json"] if isinstance(r["json"], dict) else {}
        # "available" = saldo bersih siap pakai (balance - reserved)
        bal = self._num(d.get("available"))
        if bal is None:
            bal = self._num(d.get("balance"))
        if bal is None:
            return fail(self._err(r, "Format saldo tak dikenal."))
        return ok({"balance": bal, "reserved": self._num(d.get("reserved"))})

    def get_services(self, country=None):
        r, err = self._req("GET", "/services")
        if err:
            return fail(err if err != "NOT_FOUND" else "Endpoint tak dikenal.")
        items = r["json"] if isinstance(r["json"], list) else []
        out = []
        for s in items:
            if not isinstance(s, dict) or s.get("id") is None:
                continue
            out.append({
                "code": str(s.get("id")),
                "name": s.get("name") or str(s.get("id")),
                "price": self._num(s.get("price")),
                "stock": self._num(s.get("stock")),
                "allow_retry": bool(s.get("allow_retry", True)),
            })
        if not out:
            return fail(self._err(r, "Daftar layanan kosong."))
        return ok({"services": out})

    def get_countries(self):
        # DehuyOTPWA tidak punya konsep negara.
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
        r, err = self._req("POST", "/rent",
                           json_body={"service_id": service_id})
        if err:
            return fail(err if err != "NOT_FOUND" else "Endpoint tak dikenal.")
        d = r["json"] if isinstance(r["json"], dict) else {}
        if not d.get("order_id") or not d.get("token"):
            return fail(self._err(r, "Respons order tak dikenal."))
        return ok({"order_id": str(d["order_id"]),
                   "token": str(d["token"]),
                   "phone": self._norm_phone(d.get("phone")),
                   "price": None,  # harga dari daftar layanan (wizard)
                   "allow_retry": bool(d.get("allow_retry", True)),
                   "expires_at": self._num(d.get("expires_at"))})

    def check(self, order_id):
        r, err = self._req("GET", f"/order/{order_id}")
        if err:
            if err == "NOT_FOUND":
                return ok({"state": "notfound", "code": None})
            return fail(err)
        if r["http"] == 410:
            # order sudah terminal (success/cancelled/expired)
            return ok({"state": "notfound", "code": None})
        d = r["json"] if isinstance(r["json"], dict) else {}
        state = str(d.get("state") or "").lower()
        otp = d.get("otp")
        if not otp:
            m = re.search(r"\d{4,8}", str(d.get("full_sms") or ""))
            if m:
                otp = m.group(0)
        if state == "success":
            return ok({"state": "ok",
                       "code": str(otp) if otp else None})
        if state in ("cancelled", "canceled"):
            return ok({"state": "cancel", "code": None})
        if state in ("expired",):
            return ok({"state": "expired", "code": None})
        # pending/waiting/dll -> masih menunggu SMS
        return ok({"state": "wait", "code": None, "raw_state": state})

    def cancel(self, order_id):
        r, err = self._req("POST", f"/order/{order_id}",
                           json_body={"action": "cancel"})
        if err:
            if err == "NOT_FOUND":
                return fail("Order tidak ditemukan / sudah terminal.")
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        return ok({"refunded": self._num(d.get("refunded"))})

    def resend(self, order_id):
        # Retry butuh TOKEN sewa: ambil dulu dari status order.
        r, err = self._req("GET", f"/order/{order_id}")
        if err:
            if err == "NOT_FOUND":
                return fail("Order tidak ditemukan / sudah terminal.")
            return fail(err)
        d = r["json"] if isinstance(r["json"], dict) else {}
        token = str(d.get("token") or "")
        if not token:
            return fail("Token sewa tidak ditemukan.")
        r2, err2 = self._req("POST", f"/rent/{token}/retry")
        if err2:
            return fail(err2)
        if r2["http"] == 409:
            return fail("Layanan ini sekali-pakai / batas Re-OTP tercapai.")
        d2 = r2["json"] if isinstance(r2["json"], dict) else {}
        if d2.get("order_id") or r2["http"] in (200, 201):
            return ok({"raw": d2})
        return fail(self._err(r2, "Gagal meminta OTP ulang."))

    def finish(self, order_id):
        r, err = self._req("POST", f"/order/{order_id}",
                           json_body={"action": "done"})
        if err:
            if err == "NOT_FOUND":
                # order sudah terminal -> anggap selesai
                return ok({"local": True})
            return fail(err)
        return ok({"raw": r["json"]})
