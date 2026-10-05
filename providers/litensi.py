"""Konektor Litensi — https://litensi.id

API gaya sms-activate: semua via GET dengan query param
    ?action=...&api_key=...
Respons sukses/error berupa teks biasa (mis. ACCESS_BALANCE:100000)
kecuali beberapa endpoint yang mengembalikan JSON.
"""
import urllib.parse

from .base import OTPProvider, http_request, ok, fail

BASE_URL = "https://litensi.id/api/sms/handler_api.php"


class LitensiProvider(OTPProvider):
    name = "litensi"
    title = "Litensi"

    ERROR_MAP = {
        "NO_KEY": "🔑 API key belum diisi.",
        "BAD_KEY": "🔑 API key salah — cek lagi dengan /setkey litensi.",
        "NO_NUMBERS": "📵 Stok nomor habis untuk layanan ini.",
        "NO_BALANCE": "💸 Saldo kurang — deposit dulu di web Litensi.",
        "BAD_SERVICE": "Kode layanan tidak dikenal.",
        "BAD_PHONE_EXCEPTION": "Filter prefix nomor tidak valid.",
        "TOO_MANY_ACTIVE_ACTIVATIONS": "Terlalu banyak aktivasi aktif — selesaikan/batalkan dulu.",
        "NO_ACTIVATION": "Aktivasi tidak ditemukan.",
        "BAD_STATUS": "Status tidak valid.",
        "BAD_ACTION": "Action API tidak dikenal (bug konektor?).",
        "TRY_AGAIN_LATER": "Server sibuk — coba lagi sebentar.",
        "NO_BALANCE_FORWARD": "💸 Saldo kurang.",
    }

    # -- internal --
    def _get(self, action, params=None):
        q = {"action": action, "api_key": self.api_key}
        q.update(params or {})
        # safe=":" -> surrogate vault (hsurr:...) tidak boleh ter-encode,
        # kalau tidak egress proxy tidak mengenalinya (quirk yang sama
        # seperti pada URL path Bot API Telegram).
        url = BASE_URL + "?" + urllib.parse.urlencode(q, safe=":")
        r = http_request("GET", url)
        if r["error"]:
            return None, "🌐 Gangguan jaringan: " + r["error"]
        if r["http"] == 401:
            return None, self.translate("BAD_KEY")
        return r, None

    def _parse_text(self, r):
        """Ambil payload teks; kembalikan (payload, error_code|None)."""
        text = (r["text"] or "").strip()
        # beberapa error dikirim sebagai JSON {"status":"error",...}
        if r["json"] and isinstance(r["json"], dict) and \
                r["json"].get("status") == "error":
            code = r["json"].get("error_code") or r["json"].get("message")
            return None, str(code)
        return text, None

    # -- API --
    def get_balance(self):
        r, err = self._get("getBalance")
        if err:
            return fail(err)
        text, code = self._parse_text(r)
        if code:
            return fail(self.translate(code))
        if text.startswith("ACCESS_BALANCE:"):
            try:
                return ok({"balance": int(text.split(":", 1)[1])})
            except ValueError:
                pass
        return fail(self.translate(text, "Respons saldo tak dikenal."))

    def get_services(self, country=None):
        r, err = self._get("getServicesList")
        if err:
            return fail(err)
        d = r["json"]
        if not isinstance(d, dict) or d.get("status") != "success":
            return fail(self.translate((r["text"] or "").strip(),
                                       "Gagal mengambil daftar layanan."))
        out = [{"code": s.get("code"), "name": s.get("name"),
                "price": None, "stock": None}
               for s in (d.get("services") or []) if s.get("code")]
        return ok({"services": out})

    def get_countries(self):
        r, err = self._get("getCountries")
        if err:
            return fail(err)
        d = r["json"]
        if not isinstance(d, list):
            return fail(self.translate((r["text"] or "").strip(),
                                       "Gagal mengambil daftar negara."))
        out = [{"id": str(c.get("id")), "name": c.get("eng") or c.get("rus")
                or str(c.get("id"))} for c in d]
        return ok({"countries": out})

    def get_price(self, service, country=None):
        params = {"service": service}
        if country:
            params["country"] = country
        r, err = self._get("getPrices", params)
        if err:
            return fail(err)
        d = r["json"]
        if not isinstance(d, dict) or "data" not in d:
            return fail(self.translate((r["text"] or "").strip(),
                                       "Gagal mengambil harga."))
        options = []
        svc_data = (d["data"] or {}).get(service, {})
        for cid, cdata in svc_data.items():
            for price, stock in ((cdata or {}).get("map") or {}).items():
                try:
                    options.append({"price": int(price), "stock": int(stock),
                                    "country_id": str(cid)})
                except (ValueError, TypeError):
                    continue
        options.sort(key=lambda o: o["price"])
        return ok({"options": options})

    def order(self, service, country=None, max_price=None):
        if not country:
            return fail("Negara wajib dipilih untuk Litensi.")
        params = {"service": service, "country": country, "operator": "any"}
        if max_price:
            params["maxPrice"] = max_price
        r, err = self._get("getNumber", params)
        if err:
            return fail(err)
        text, code = self._parse_text(r)
        if code:
            return fail(self.translate(code))
        # ACCESS_NUMBER:<activation_id>:<phone>
        if text.startswith("ACCESS_NUMBER:"):
            parts = text.split(":")
            if len(parts) >= 3:
                return ok({"order_id": parts[1], "phone": parts[2],
                           "price": None})
        return fail(self.translate(text, "Respons order tak dikenal."))

    def check(self, order_id):
        r, err = self._get("getStatus", {"id": order_id})
        if err:
            return fail(err)
        text, code = self._parse_text(r)
        if code:
            return fail(self.translate(code))
        if text.startswith("STATUS_OK:"):
            return ok({"state": "ok", "code": text.split(":", 1)[1]})
        if text.startswith("STATUS_WAIT_RETRY:"):
            return ok({"state": "ok", "code": text.split(":", 1)[1]})
        if text == "STATUS_WAIT_CODE":
            return ok({"state": "wait", "code": None})
        if text == "STATUS_CANCEL":
            return ok({"state": "cancel", "code": None})
        if text == "NO_ACTIVATION":
            return ok({"state": "notfound", "code": None})
        return fail(self.translate(text, "Status tak dikenal."))

    def _set_status(self, order_id, status):
        r, err = self._get("setStatus", {"id": order_id, "status": status})
        if err:
            return fail(err)
        text, code = self._parse_text(r)
        if code:
            return fail(self.translate(code))
        return ok({"raw": text})

    def cancel(self, order_id):
        r = self._set_status(order_id, 8)
        if r["ok"] and "ACCESS_CANCEL" not in str(r["data"].get("raw")):
            # tetap anggap sukses bila server tidak mengembalikan pola baku
            pass
        return r

    def resend(self, order_id):
        return self._set_status(order_id, 3)

    def finish(self, order_id):
        return self._set_status(order_id, 6)

    def get_active(self):
        """Tambahan: daftar aktivasi aktif (untuk menu 📦)."""
        r, err = self._get("getActiveActivations")
        if err:
            return fail(err)
        d = r["json"]
        if not isinstance(d, dict) or d.get("status") != "success":
            return fail("Gagal mengambil aktivasi aktif.")
        return ok({"orders": d.get("data") or []})
