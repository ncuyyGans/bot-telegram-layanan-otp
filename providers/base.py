"""Interface & util bersama untuk semua provider OTP.

Kontrak: setiap method mengembalikan dict
    {"ok": bool, "data": <dict/list>, "error": <str|None>}
"error" adalah pesan yang sudah ramah dibaca user (Bahasa Indonesia).

Stdlib only.
"""
import json
import time
import urllib.request
import urllib.error

HTTP_TIMEOUT = 15


def _try_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


def http_request(method, url, headers=None, json_body=None,
                 timeout=HTTP_TIMEOUT, retries=3):
    """HTTP request dengan retry + backoff untuk network error.

    Return: {"http": int|None, "json": parsed|None, "text": str,
             "error": str|None}
    HTTPError (4xx/5xx) TIDAK di-retry — langsung dikembalikan agar
    caller bisa menerjemahkan kode error API-nya.
    """
    body = None
    h = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=h,
                                         method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return {"http": resp.status, "json": _try_json(raw),
                        "text": raw, "error": None}
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            return {"http": e.code, "json": _try_json(raw),
                    "text": raw, "error": None}
        except Exception as e:  # timeout / DNS / koneksi putus -> retry
            last_err = str(e)[:200]
            time.sleep(1 * (2 ** attempt))
    return {"http": None, "json": None, "text": "",
            "error": last_err or "network error"}


def ok(data=None):
    return {"ok": True, "data": data or {}, "error": None}


def fail(message):
    return {"ok": False, "data": {}, "error": message}


class OTPProvider:
    """Interface yang wajib dipenuhi setiap provider."""

    name = "base"
    title = "Base"
    # False untuk provider yang tidak punya konsep negara (mis. NinjaOTP):
    # bot akan melewati langkah "pilih negara" di wizard order.
    has_countries = True

    def __init__(self, api_key):
        self.api_key = api_key

    # -- dibaca bot --
    def get_balance(self):
        raise NotImplementedError

    def get_services(self, country=None):
        """Return {"ok","data":[{"code","name","price","stock"}]}.
        price/stock boleh None bila tidak tersedia di endpoint ini."""
        raise NotImplementedError

    def get_countries(self):
        """Return {"ok","data":[{"id","name"}]}."""
        raise NotImplementedError

    def get_price(self, service, country=None):
        """Return {"ok","data":{"options":[{"price","stock","country_id"}]}}."""
        raise NotImplementedError

    def order(self, service, country=None):
        """Return {"ok","data":{"order_id","phone","price"}}."""
        raise NotImplementedError

    def check(self, order_id):
        """Polling status. Return {"ok","data":{"state","code"}}.
        state: "wait" | "ok" | "cancel" | "expired" | "notfound".
        "code" = kode OTP bila sudah masuk (None bila belum)."""
        raise NotImplementedError

    def cancel(self, order_id):
        raise NotImplementedError

    def resend(self, order_id):
        raise NotImplementedError

    def finish(self, order_id):
        """Tandai order selesai (setelah OTP diterima)."""
        raise NotImplementedError

    # -- helper --
    def translate(self, code, default=None):
        return self.ERROR_MAP.get(code, default or f"Error: {code}")

    ERROR_MAP = {}
