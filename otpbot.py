#!/usr/bin/env python3
"""otpbot — bot Telegram privat untuk beli OTP dari beberapa provider.

Alur: /start -> pilih provider -> cek saldo / daftar layanan /
order nomor -> bot polling OTP otomatis tiap 5 detik.

Hanya melayani OWNER_CHAT_ID; chat lain diabaikan diam-diam.
Stdlib only. Jalankan:  python3 otpbot.py
"""
import html
import json
import logging
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

from providers import PROVIDER_CLASSES, PROVIDER_TITLES
from providers.vault import vault_surrogate, vault_connected

# env var per secret (prioritas tertinggi, untuk portabilitas)
ENV_KEYS = {
    "bot_token": "TELEGRAM_BOT_TOKEN",
    "litensi": "LITENSI_API_KEY",
    "otpinstan": "OTPINSTAN_API_KEY",
    "ninjatop": "NINJATOP_API_KEY",
    "otpcepat": "OTPCEPAT_API_KEY",
    "dehuy": "DEHUY_API_KEY",
}
# nama konektor Secure Vault (fallback terakhir, khusus lingkungan Muse)
# custom.telegram_otp dipakai, bukan custom.telegram, karena yang terakhir
# sudah terisi token bot GrabFood (@grabnotifsy_bot) — jangan ditimpa.
VAULT_CONNECTORS = {
    "bot_token": "custom.telegram_otp",
    "litensi": "custom.litensi",
    "otpinstan": "custom.otpinstan",
    "ninjatop": "custom.ninjatop",
    "otpcepat": "custom.otpcepat",
    "dehuy": "custom.dehuy",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_PATH = os.path.join(BASE_DIR, "secrets.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")

ORDER_TIMEOUT = 20 * 60   # detik; polling berhenti setelah ini
POLL_INTERVAL = 5         # detik antar putaran polling
OTPINSTAN_CANCEL_MIN = 120  # detik; aturan anti-abuse OTP Instan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("otpbot")


# ---------------------------------------------------------------- secrets
def load_secrets():
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_secrets(s):
    tmp = SECRETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SECRETS_PATH)


def resolve_secret(name):
    """Ambil secret: env var -> secrets.json -> Secure Vault. '' bila kosong.

    Nilai dari vault adalah surrogate yang hanya valid di lingkungan Muse
    (diganti nilai asli oleh egress proxy saat request keluar).
    """
    env_name = ENV_KEYS.get(name)
    if env_name:
        v = os.environ.get(env_name, "").strip()
        if v:
            return v
    if name == "bot_token":
        v = (load_secrets().get("bot_token") or "").strip()
    else:
        v = ((load_secrets().get("api_keys") or {}).get(name) or "").strip()
    if v:
        return v
    connector = VAULT_CONNECTORS.get(name)
    return vault_surrogate(connector) if connector else ""


def has_secret(name):
    if resolve_secret(name):
        return True
    # vault_connected dipanggil ulang agar status segar bila user baru
    # menyelesaikan kartu Secure Vault setelah bot berjalan
    connector = VAULT_CONNECTORS.get(name)
    return bool(connector and vault_connected(connector))


def get_token():
    return resolve_secret("bot_token")


def get_owner():
    raw = os.environ.get("OWNER_CHAT_ID", "").strip()
    if not raw:
        raw = str(load_secrets().get("owner_chat_id") or "")
    try:
        return int(raw)
    except ValueError:
        return 0


TOKEN = ""
OWNER = 0


# ---------------------------------------------------------------- Telegram API
def tg_api(method, payload=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            body = ""
        return {"ok": False, "error_code": e.code, "description": body}
    except Exception as e:
        return {"ok": False, "error_code": -1, "description": str(e)[:200]}


def send_message(chat_id, text, markup=None, parse_mode="HTML"):
    p = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
         "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    return tg_api("sendMessage", p, timeout=20)


def edit_message(chat_id, msg_id, text, markup=None, parse_mode="HTML"):
    p = {"chat_id": chat_id, "message_id": msg_id, "text": text,
         "parse_mode": parse_mode, "disable_web_page_preview": True}
    if markup is not None:
        p["reply_markup"] = markup
    return tg_api("editMessageText", p, timeout=20)


def answer_callback(cb_id, text=""):
    return tg_api("answerCallbackQuery",
                  {"callback_query_id": cb_id, "text": text}, timeout=15)


def delete_message(chat_id, msg_id):
    return tg_api("deleteMessage",
                  {"chat_id": chat_id, "message_id": msg_id}, timeout=15)


# ---------------------------------------------------------------- helpers
def esc(s):
    return html.escape(str(s or ""))


def rupiah(n):
    try:
        return "Rp" + f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "-"


def btn(text, data):
    return {"text": text, "callback_data": data}


def kb(rows):
    return {"inline_keyboard": rows}


def mask_key(key):
    k = (key or "").strip()
    return "…" + k[-4:] if len(k) > 4 else "****"


def has_key(name):
    return has_secret(name)


def get_provider(name):
    """Bangun instance provider dari API key tersimpan (baca ulang tiap
    panggil agar /setkey langsung berlaku). Return None bila key kosong."""
    if name not in PROVIDER_CLASSES:
        return None
    key = resolve_secret(name)
    if not key:
        return None
    if name == "otpinstan":
        server = (load_secrets().get("otpinstan_server") or "s1")
        return PROVIDER_CLASSES[name](key, server)
    return PROVIDER_CLASSES[name](key)


# ---------------------------------------------------------------- state
_lock = threading.Lock()
_state = {"provider": "litensi", "otpinstan_server": "s1", "active_orders": {}}
_wiz = {}          # chat_id -> wizard order yang sedang berjalan
_pending_key = {}  # chat_id -> nama provider yang menunggu API key
_pending_search = {}  # chat_id -> {"p": provider, "cid": country|None} (cari layanan)


def load_state():
    global _state
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        # buang order yang sudah kedaluwarsa saat restart
        now = time.time()
        orders = d.get("active_orders") or {}
        d["active_orders"] = {k: v for k, v in orders.items()
                              if v.get("timeout_at", 0) > now}
        # bersihkan flag "changing" sisa crash saat tukar nomor
        for v in d["active_orders"].values():
            v.pop("changing", None)
        d.setdefault("provider", "litensi")
        d.setdefault("otpinstan_server", "s1")
        _state = d
        log.info("state dimuat: %d order aktif", len(_state["active_orders"]))
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def save_state():
    tmp = STATE_PATH + ".tmp"
    with _lock:
        snapshot = json.dumps(_state, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(snapshot)
    os.replace(tmp, STATE_PATH)


def add_order(okey, rec):
    with _lock:
        _state["active_orders"][okey] = rec
    save_state()


def remove_order(okey):
    with _lock:
        _state["active_orders"].pop(okey, None)
    save_state()


def get_order(okey):
    with _lock:
        return dict(_state["active_orders"].get(okey) or {})


def find_order(order_id):
    """Cari order aktif berdasarkan order_id (boleh tanpa prefix)."""
    oid = str(order_id)
    with _lock:
        for okey, rec in _state["active_orders"].items():
            if rec.get("order_id") == oid or okey.endswith(":" + oid):
                return okey, dict(rec)
    return None, None


def parse_expire(expire_at):
    """Parse expire_at NinjaOTP (unix ts / ISO) -> epoch. None bila gagal."""
    if not expire_at:
        return None
    try:
        return int(float(expire_at))
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(expire_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _order_timeout(p, d, now):
    """timeout_at order: pakai expires_at provider bila valid,
    fallback ke ORDER_TIMEOUT (20 menit)."""
    if p in ("ninjatop", "dehuy"):
        exp = parse_expire(d.get("expires_at"))
        if exp and exp > now:
            return exp
    return now + ORDER_TIMEOUT


# ---------------------------------------------------------------- menu & wizard
def provider_rows():
    rows = []
    for p in PROVIDER_CLASSES:
        mark = "✅" if has_key(p) else "🔑"
        rows.append([btn(f"{mark} {PROVIDER_TITLES[p]}", f"prov:{p}")])
    return rows


def send_start(chat_id):
    send_message(
        chat_id,
        "👋 <b>OTP Bot</b>\nPilih provider dulu:\n"
        "<i>✅ = API key sudah terisi • 🔑 = belum, pakai /setkey</i>",
        kb(provider_rows()))


def show_provider_menu(chat_id, msg_id, p):
    title = PROVIDER_TITLES.get(p, p)
    rows = [
        [btn("💰 Cek Saldo", f"bal:{p}"),
         btn("📋 Daftar Layanan", f"svc:{p}")],
        [btn("🛒 Order Nomor", f"ord:{p}"),
         btn("📦 Order Aktif", f"act:{p}")],
    ]
    if p == "otpinstan":
        srv = (load_secrets().get("otpinstan_server") or "s1").upper()
        rows.append([btn(f"⚙️ Ganti Server (sekarang: {srv})", "srv")])
    rows.append([btn("🔑 Set API Key", f"key:{p}")])
    rows.append([btn("◀️ Kembali", "menu")])
    key_note = "" if has_key(p) else "\n⚠️ <i>API key belum diisi — /setkey " + p + "</i>"
    edit_message(chat_id, msg_id,
                 f"<b>{esc(title)}</b>\nMau apa?{key_note}", kb(rows))


def need_key_text(p):
    return (f"🔑 API key <b>{esc(PROVIDER_TITLES.get(p, p))}</b> belum diisi.\n"
            f"Ketik: <code>/setkey {esc(p)}</code> lalu kirim key-nya.")


def do_balance(chat_id, msg_id, p):
    prov = get_provider(p)
    if not prov:
        edit_message(chat_id, msg_id, need_key_text(p),
                     kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        return
    r = prov.get_balance()
    title = PROVIDER_TITLES.get(p, p)
    if r["ok"]:
        txt = f"💰 <b>Saldo {esc(title)}</b>\n\n{rupiah(r['data'].get('balance'))}"
    else:
        txt = f"❌ Gagal cek saldo {esc(title)}:\n{r['error']}"
    edit_message(chat_id, msg_id, txt,
                 kb([[btn("◀️ Kembali", f"prov:{p}")]]))


def _services_lines(p, svcs, cname=None):
    """Baris-baris daftar layanan (maks 60 + catatan sisa)."""
    title = PROVIDER_TITLES.get(p, p)
    head = f"📋 <b>Layanan {esc(title)}"
    if cname:
        head += f" — {esc(cname)}"
    head += f"</b> ({len(svcs)})"
    lines = [head]
    for s in svcs[:60]:
        extra = ""
        if s.get("price") is not None:
            extra = f" — {rupiah(s['price'])}"
            if s.get("stock") is not None:
                extra += f" (stok {s['stock']})"
        lines.append(f"• {esc(s['name'])} <code>{esc(s['code'])}</code>{extra}")
    if len(svcs) > 60:
        lines.append(f"<i>…dan {len(svcs) - 60} lainnya, pakai /order "
                     f"untuk pilih via tombol.</i>")
    return lines


def do_services(chat_id, msg_id, p):
    prov = get_provider(p)
    if not prov:
        edit_message(chat_id, msg_id, need_key_text(p),
                     kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        return
    if p in ("otpinstan", "otpcepat"):
        # services.php kedua provider ini wajib pakai country —
        # otomatis Indonesia, langkah pilih negara dilewati.
        cid, cname, err = _resolve_indonesia(p)
        if err or not cid:
            edit_message(chat_id, msg_id,
                         f"❌ {err or 'Gagal menemukan Indonesia.'}",
                         kb([[btn("◀️ Kembali", f"prov:{p}")]]))
            return
        cf_show_services(chat_id, msg_id, p, cid)
        return
    r = prov.get_services()
    title = PROVIDER_TITLES.get(p, p)
    if not r["ok"]:
        edit_message(chat_id, msg_id,
                     f"❌ Gagal ambil layanan {esc(title)}:\n{r['error']}",
                     kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        return
    svcs = r["data"]["services"]
    edit_message(chat_id, msg_id, "\n".join(_services_lines(p, svcs)),
                 kb([[btn("🛒 Order Nomor", f"ord:{p}"),
                      btn("◀️ Kembali", f"prov:{p}")]]))


# -- wizard order --
PAGE = 20


def _page_nav(prefix, pg, total):
    nav = []
    if pg > 0:
        nav.append(btn("◀", f"{prefix}:{pg - 1}"))
    if (pg + 1) * PAGE < total:
        nav.append(btn("▶", f"{prefix}:{pg + 1}"))
    return nav


def wiz_services_page(chat_id, msg_id, p, pg, edit=True):
    """Langkah pilih layanan (Litensi & NinjaOTP)."""
    prov = get_provider(p)
    r = prov.get_services() if prov else {"ok": False, "error": "x"}
    if not r["ok"]:
        txt = need_key_text(p) if not prov else f"❌ {r['error']}"
        rows = kb([[btn("◀️ Kembali", f"prov:{p}")]])
        if edit:
            edit_message(chat_id, msg_id, txt, rows)
        else:
            send_message(chat_id, txt, rows)
        return
    svcs = r["data"]["services"]
    total = len(svcs)
    pg = max(0, min(pg, (total - 1) // PAGE if total else 0))
    chunk = svcs[pg * PAGE:(pg + 1) * PAGE]
    rows = []
    for i in range(0, len(chunk), 2):
        row = [btn(f"{chunk[i]['name'][:18]}", f"ws:{p}:{pg}:{chunk[i]['code']}")]
        if i + 1 < len(chunk):
            row.append(btn(f"{chunk[i + 1]['name'][:18]}",
                           f"ws:{p}:{pg}:{chunk[i + 1]['code']}"))
        rows.append(row)
    nav = _page_nav(f"wsp:{p}", pg, total)
    if nav:
        rows.append(nav)
    rows.append([btn("🔍 Cari layanan", "wfind")])
    rows.append([btn("❌ Batal", "wx")])
    txt = (f"🛒 <b>Order — {esc(PROVIDER_TITLES[p])}</b>\n"
           f"Pilih layanan (hal. {pg + 1}/{max(1, (total + PAGE - 1) // PAGE)}):")
    if edit:
        edit_message(chat_id, msg_id, txt, kb(rows))
    else:
        send_message(chat_id, txt, kb(rows))


def wiz_countries_page(chat_id, msg_id, p, svc, pg):
    """Langkah pilih negara (Litensi)."""
    prov = get_provider(p)
    r = prov.get_countries() if prov else {"ok": False}
    if not r["ok"]:
        edit_message(chat_id, msg_id, f"❌ {r.get('error', '?')}",
                     kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        return
    ctys = r["data"]["countries"]
    total = len(ctys)
    pg = max(0, min(pg, (total - 1) // PAGE if total else 0))
    chunk = ctys[pg * PAGE:(pg + 1) * PAGE]
    rows = []
    for i in range(0, len(chunk), 2):
        row = [btn(f"{chunk[i]['name'][:18]}",
                   f"wc:{p}:{svc}:{pg}:{chunk[i]['id']}")]
        if i + 1 < len(chunk):
            row.append(btn(f"{chunk[i + 1]['name'][:18]}",
                           f"wc:{p}:{svc}:{pg}:{chunk[i + 1]['id']}"))
        rows.append(row)
    nav = _page_nav(f"wcp:{p}:{svc}", pg, total)
    if nav:
        rows.append(nav)
    rows.append([btn("❌ Batal", "wx")])
    edit_message(chat_id, msg_id,
                 f"Pilih negara untuk <b>{esc(svc)}</b> "
                 f"(hal. {pg + 1}/{max(1, (total + PAGE - 1) // PAGE)}):",
                 kb(rows))


def wiz_operators_page(chat_id, msg_id, p, svc, cid):
    """Langkah pilih operator (Litensi & OTPCepat)."""
    prov = get_provider(p)
    ops = []
    if prov:
        r = prov.get_operators(cid)
        if r["ok"]:
            ops = r["data"]["operators"]
    if p == "otpcepat":
        random_op, random_label = "random", "🎲 Acak"
    else:
        random_op, random_label = "any", "🌐 Bebas (termurah)"
    rows = [[btn(random_label, f"wo:{random_op}")]]
    for i in range(0, len(ops), 2):
        if ops[i] in ("any", "random"):
            continue
        row = [btn(f"📶 {ops[i].capitalize()}", f"wo:{ops[i]}")]
        if i + 1 < len(ops) and ops[i + 1] not in ("any", "random"):
            row.append(btn(f"📶 {ops[i + 1].capitalize()}", f"wo:{ops[i + 1]}"))
        rows.append(row)
    rows.append([btn("❌ Batal", "wx")])
    edit_message(chat_id, msg_id,
                 f"Pilih operator nomor untuk <b>{esc(svc)}</b>:",
                 kb(rows))


def wiz_confirm(chat_id, msg_id, w):
    """Ringkasan harga + tombol Order."""
    p, prov = w["p"], get_provider(w["p"])
    r = prov.get_price(w["service"], w.get("country"))
    if not r["ok"]:
        edit_message(chat_id, msg_id, f"❌ {r['error']}",
                     kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        return
    opts = r["data"]["options"]
    if not opts:
        edit_message(chat_id, msg_id,
                     "📵 Tidak ada harga/stok untuk pilihan ini.",
                     kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        return
    best = opts[0]
    w["price"] = best["price"]
    lines = [f"🧾 <b>Konfirmasi Order</b>",
             f"Provider: <b>{esc(PROVIDER_TITLES[p])}</b>",
             f"Layanan: <b>{esc(w.get('service_name') or w['service'])}</b>"]
    if w.get("country_name"):
        lines.append(f"Negara: <b>{esc(w['country_name'])}</b>")
    if w.get("operator_name"):
        lines.append(f"Operator: <b>{esc(w['operator_name'])}</b>")
    if best["price"] is not None:
        lines.append(f"Harga: <b>{rupiah(best['price'])}</b>")
    if best.get("stock") is not None:
        lines.append(f"Stok: {best['stock']}")
    if len(opts) > 1:
        lines.append(f"<i>{len(opts)} varian harga, diambil termurah.</i>")
    lines.append("\nLanjut order?")
    edit_message(chat_id, msg_id, "\n".join(lines),
                 kb([[btn("✅ Order", "wgo"), btn("❌ Batal", "wx")]]))


def start_order(chat_id, p):
    prov = get_provider(p)
    if not prov:
        send_message(chat_id, need_key_text(p))
        return
    _pending_search.pop(chat_id, None)
    _wiz[chat_id] = {"p": p, "service": None, "service_name": None,
                     "country": None, "country_name": None,
                     "operator": None, "price": None}
    if p in ("otpinstan", "otpcepat"):
        # Negara otomatis: Indonesia ("Wakanda (Indo)" di OTPCepat) —
        # langkah pilih negara dilewati, langsung ke pilih layanan.
        cid, cname, err = _resolve_indonesia(p)
        if err or not cid:
            _wiz.pop(chat_id, None)
            send_message(chat_id,
                         f"❌ {err or 'Gagal menemukan Indonesia.'}")
            return
        _wiz[chat_id].update({"country": cid, "country_name": cname})
        cf_services_page(chat_id, None, p, cid, 0, edit=False)
    else:
        wiz_services_page(chat_id, None, p, 0, edit=False)


# -- pilih negara untuk provider country-first (otpinstan, otpcepat) --
# dipakai wizard order (mode='order') & daftar layanan (mode='list')
CF_PREFIXES = {
    ("otpinstan", "order"): ("wc1c", "wc1"),
    ("otpinstan", "list"): ("sl1c", "sl1"),
    ("otpcepat", "order"): ("oc1c", "oc1"),
    ("otpcepat", "list"): ("sc1c", "sc1"),
}


def cf_countries_page(chat_id, msg_id, pg, p, edit=True, mode="order"):
    prov = get_provider(p)
    r = prov.get_countries() if prov else {"ok": False}
    if not r["ok"]:
        txt = need_key_text(p) if not prov else f"❌ {r.get('error')}"
        if edit:
            edit_message(chat_id, msg_id, txt,
                         kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        else:
            send_message(chat_id, txt)
        return
    cb_sel, cb_page = CF_PREFIXES[(p, mode)]
    title = PROVIDER_TITLES.get(p, p)
    if mode == "list":
        head = f"📋 <b>Layanan — {esc(title)}</b>"
        cancel = [[btn("◀️ Kembali", f"prov:{p}")]]
    else:
        head = f"🛒 <b>Order — {esc(title)}</b>"
        cancel = [[btn("❌ Batal", "wx")]]
    ctys = r["data"]["countries"]
    total = len(ctys)
    pg = max(0, min(pg, (total - 1) // PAGE if total else 0))
    chunk = ctys[pg * PAGE:(pg + 1) * PAGE]
    rows = []
    for i in range(0, len(chunk), 2):
        row = [btn(f"{chunk[i]['name'][:18]}", f"{cb_sel}:{chunk[i]['id']}")]
        if i + 1 < len(chunk):
            row.append(btn(f"{chunk[i + 1]['name'][:18]}",
                           f"{cb_sel}:{chunk[i + 1]['id']}"))
        rows.append(row)
    nav = _page_nav(cb_page, pg, total)
    if nav:
        rows.append(nav)
    rows.extend(cancel)
    txt = (f"{head}\n"
           f"Pilih negara (hal. {pg + 1}/{max(1, (total + PAGE - 1) // PAGE)}):")
    if edit:
        edit_message(chat_id, msg_id, txt, kb(rows))
    else:
        send_message(chat_id, txt, kb(rows))


def wiz_oi_countries(chat_id, msg_id, pg, edit=True):
    cf_countries_page(chat_id, msg_id, pg, "otpinstan", edit=edit,
                      mode="order")


def cf_show_services(chat_id, msg_id, p, cid):
    """Tampilkan daftar layanan satu negara (mode list)."""
    prov = get_provider(p)
    r = prov.get_services(cid) if prov else {"ok": False}
    title = PROVIDER_TITLES.get(p, p)
    if not r["ok"]:
        edit_message(chat_id, msg_id,
                     f"❌ Gagal ambil layanan {esc(title)}:\n{r.get('error', '?')}",
                     kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        return
    svcs = r["data"]["services"]
    cname = _cty_name(prov, cid)
    edit_message(chat_id, msg_id, "\n".join(_services_lines(p, svcs, cname)),
                 kb([[btn("🛒 Order Nomor", f"ord:{p}"),
                      btn("◀️ Kembali", f"prov:{p}")]]))


def show_oi_services(chat_id, msg_id, cid):
    cf_show_services(chat_id, msg_id, "otpinstan", cid)


# callback prefixes untuk halaman layanan (country-first, mode order)
CF_SVC_PREFIXES = {
    "otpinstan": ("ws1", "wsp1"),
    "otpcepat": ("os1", "osp1"),
}


def cf_services_page(chat_id, msg_id, p, cid, pg, edit=True):
    """Pilih layanan satu negara (country-first, mode order)."""
    prov = get_provider(p)
    r = prov.get_services(cid) if prov else {"ok": False}
    if not r["ok"]:
        txt = (f"❌ {r.get('error', 'gagal ambil layanan')}")
        rows = kb([[btn("◀️ Kembali", f"prov:{p}")]])
        if edit:
            edit_message(chat_id, msg_id, txt, rows)
        else:
            send_message(chat_id, txt, rows)
        return
    svcs = r["data"]["services"]
    total = len(svcs)
    if not total:
        txt = "📵 Tidak ada layanan di negara ini."
        rows = kb([[btn("◀️ Kembali", f"prov:{p}")]])
        if edit:
            edit_message(chat_id, msg_id, txt, rows)
        else:
            send_message(chat_id, txt, rows)
        return
    sel_cb, page_cb = CF_SVC_PREFIXES[p]
    pg = max(0, min(pg, (total - 1) // PAGE))
    chunk = svcs[pg * PAGE:(pg + 1) * PAGE]
    rows = []
    for s in chunk:
        label = s["name"][:22]
        if s.get("price") is not None:
            label += f" {rupiah(s['price'])}"
        rows.append([btn(label, f"{sel_cb}:{cid}:{pg}:{s['code']}")])
    nav = _page_nav(f"{page_cb}:{cid}", pg, total)
    if nav:
        rows.append(nav)
    rows.append([btn("🔍 Cari layanan", "wfind")])
    rows.append([btn("❌ Batal", "wx")])
    txt = (f"Pilih layanan (hal. {pg + 1}/{(total + PAGE - 1) // PAGE}):")
    if edit:
        edit_message(chat_id, msg_id, txt, kb(rows))
    else:
        send_message(chat_id, txt, kb(rows))


def wiz_oi_services(chat_id, msg_id, cid, pg, edit=True):
    cf_services_page(chat_id, msg_id, "otpinstan", cid, pg, edit=edit)


def do_search(chat_id, msg_id, query):
    """Cari layanan dari kata kunci yang diketik user di chat."""
    ctx = _pending_search.pop(chat_id, None)
    if not ctx:
        return
    p, cid = ctx["p"], ctx.get("cid")
    prov = get_provider(p)
    if not prov:
        send_message(chat_id, need_key_text(p))
        return
    q = query.strip().lower()
    if not q:
        send_message(chat_id, "Ketik nama layanannya dulu ya.",
                     kb([[btn("🔍 Cari lagi", "wfind"),
                          btn("❌ Batal", "wx")]]))
        return
    r = prov.get_services(cid)
    if not r["ok"]:
        send_message(chat_id, f"❌ {r['error']}",
                     kb([[btn("🔍 Cari lagi", "wfind"),
                          btn("❌ Batal", "wx")]]))
        return

    def _score(s):
        name = str(s.get("name") or "").lower()
        code = str(s.get("code") or "").lower()
        if q == name or q == code:
            return 0
        if name.startswith(q) or code.startswith(q):
            return 1
        if q in name or q in code:
            return 2
        return 9

    svcs = r["data"]["services"]
    hits = sorted((s for s in svcs if _score(s) < 9),
                  key=lambda s: (_score(s), str(s.get("name") or "")))[:15]
    if not hits:
        send_message(chat_id,
                     f"🔍 Tidak ketemu layanan <b>{esc(query.strip())}</b>.\n"
                     f"Coba kata kunci lain.",
                     kb([[btn("🔍 Cari lagi", "wfind"),
                          btn("❌ Batal", "wx")]]))
        return
    if p in ("otpinstan", "otpcepat"):
        sel_cb = f"{'ws1' if p == 'otpinstan' else 'os1'}:{cid}:0:"
    else:
        sel_cb = f"ws:{p}:0:"
    rows = []
    for s in hits:
        label = str(s["name"])[:24]
        if s.get("price") is not None:
            label += f" {rupiah(s['price'])}"
        rows.append([btn(label, f"{sel_cb}{s['code']}")])
    rows.append([btn("🔍 Cari lagi", "wfind"), btn("❌ Batal", "wx")])
    send_message(chat_id,
                 f"🔍 Hasil cari <b>{esc(query.strip())}</b> ({len(hits)}):",
                 kb(rows))


def do_order(chat_id, msg_id):
    w = _wiz.get(chat_id)
    if not w or not w.get("service"):
        return
    p, prov = w["p"], get_provider(w["p"])
    if not prov:
        edit_message(chat_id, msg_id, need_key_text(p))
        return
    edit_message(chat_id, msg_id, "⏳ Memesan nomor…")
    if p == "litensi":
        r = prov.order(w["service"], w.get("country"),
                       operator=w.get("operator") or "any")
    elif p == "otpcepat":
        r = prov.order(w["service"], w.get("country"),
                       operator=w.get("operator") or "random")
    elif prov.has_countries:
        r = prov.order(w["service"], w.get("country"))
    else:
        r = prov.order(w["service"])
    if not r["ok"]:
        edit_message(chat_id, msg_id,
                     f"❌ Order gagal:\n{r['error']}\n\n"
                     f"<i>Saldo mungkin tidak terpotong bila order gagal.</i>",
                     kb([[btn("🔁 Coba Lagi", f"ord:{p}"),
                          btn("◀️ Kembali", f"prov:{p}")]]))
        return
    d = r["data"]
    now = time.time()
    timeout_at = _order_timeout(p, d, now)
    okey = f"{p}:{d['order_id']}"
    add_order(okey, {
        "provider": p, "order_id": str(d["order_id"]),
        "phone": str(d.get("phone") or ""),
        "service": w["service"],
        "service_name": w.get("service_name") or w["service"],
        "country": w.get("country"), "country_name": w.get("country_name"),
        "operator": w.get("operator"), "operator_name": w.get("operator_name"),
        "created_at": now, "timeout_at": timeout_at,
        "last_code": None, "notified_no_code": False,
        # khusus provider tertentu (mis. DehuyOTPWA): token sewa &
        # flag allow_retry untuk tombol "🔁 Minta Ulang".
        "token": d.get("token"), "allow_retry": d.get("allow_retry"),
    })
    _wiz.pop(chat_id, None)
    price_note = f"\nHarga: {rupiah(w['price'])}" if w.get("price") else ""
    edit_message(
        chat_id, msg_id,
        f"✅ <b>Order berhasil!</b>\n\n"
        f"📱 Nomor: <code>{esc(d.get('phone'))}</code>\n"
        f"🆔 Order ID: <code>{esc(d.get('order_id'))}</code>\n"
        f"Layanan: {esc(w.get('service_name') or w['service'])}"
        f"{price_note}\n\n"
        f"⏳ Bot memantau SMS masuk tiap {POLL_INTERVAL} detik. "
        f"Kode OTP langsung dikirim ke sini.\n"
        f"<i>Tempel nomor di atas ke aplikasi, lalu tunggu.</i>",
        kb([[btn("🔄 Ganti Nomor", f"och:{okey}"),
             btn("❌ Batalkan Order", f"ocx:{okey}")],
            [btn("◀️ Menu", f"prov:{p}")]]))
    log.info("order %s (%s) phone=%s", okey, p, d.get("phone"))


# ---------------------------------------------------------------- aksi order aktif
def send_otp_message(okey, rec, code):
    # Litensi: "🔁 Minta Ulang" memanggil setStatus=3 (ganti nomor) — persis
    # sama dengan tombol "🔄 Ganti Nomor", jadi disembunyikan untuk Litensi
    # agar tidak ada dua tombol yang melakukan hal identik (fix 2026-10-07).
    # DehuyOTPWA: layanan dengan allow_retry=False adalah sekali-pakai —
    # API menolak retry (409), jadi tombolnya disembunyikan juga.
    row1 = [btn("✅ Selesai", f"of:{okey}")]
    show_resend = rec.get("provider") != "litensi"
    if rec.get("provider") == "dehuy" and rec.get("allow_retry") is False:
        show_resend = False
    if show_resend:
        row1.append(btn("🔁 Minta Ulang", f"ors:{okey}"))
    send_message(
        OWNER,
        f"🔑 <b>Kode OTP masuk!</b>\n\n<code>{esc(code)}</code>\n\n"
        f"<i>Ketuk kode untuk menyalin.</i>\n"
        f"Order: <code>{esc(rec['order_id'])}</code> • "
        f"{esc(rec.get('service_name') or '')}",
        kb([row1,
            [btn("❌ Batalkan", f"ocx:{okey}")]]))
    log.info("OTP diterima %s code=%s", okey, code)


def do_finish(chat_id, okey, via_edit=True, msg_id=None):
    rec = get_order(okey)
    if not rec:
        send_message(chat_id, "Order sudah tidak aktif.")
        return
    prov = get_provider(rec["provider"])
    r = prov.finish(rec["order_id"]) if prov else {"ok": True}
    remove_order(okey)
    txt = (f"✅ Order <code>{esc(rec['order_id'])}</code> selesai."
           if r["ok"] else f"❌ {r['error']}")
    if via_edit and msg_id:
        edit_message(chat_id, msg_id, txt,
                     kb([[btn("◀️ Menu", f"prov:{rec['provider']}")]]))
    else:
        send_message(chat_id, txt)
    log.info("finish %s ok=%s", okey, r["ok"])


def do_resend(chat_id, okey):
    rec = get_order(okey)
    if not rec:
        send_message(chat_id, "Order sudah tidak aktif.")
        return
    prov = get_provider(rec["provider"])
    if not prov:
        send_message(chat_id, need_key_text(rec["provider"]))
        return
    r = prov.resend(rec["order_id"])
    send_message(chat_id,
                 "🔁 Permintaan SMS ulang dikirim, tunggu…"
                 if r["ok"] else f"❌ {r['error']}")
    log.info("resend %s ok=%s", okey, r["ok"])


def _order_buttons(p, okey):
    """Tombol standar kartu order aktif."""
    return kb([[btn("🔄 Ganti Nomor", f"och:{okey}"),
                btn("❌ Batalkan Order", f"ocx:{okey}")],
               [btn("◀️ Menu", f"prov:{p}")]])


def do_change_number(chat_id, okey, msg_id=None):
    """Tombol 🔄 Ganti Nomor: tukar ke nomor lain dengan pilihan yang
    persis sama — tanpa mengulang wizard.

    - litensi: jalur native setStatus=3 (request another number); nomor
      baru langsung keluar di respons, order_id tetap sama.
    - otpinstan/ninjatop/otpcepat: cancel order lama (refund) -> order()
      lagi dengan service/country/operator yang tersimpan.
    """
    rec = get_order(okey)
    if not rec:
        send_message(chat_id, "Order sudah tidak aktif.")
        return
    p = rec["provider"]
    prov = get_provider(p)
    if not prov:
        send_message(chat_id, need_key_text(p))
        return
    if p == "otpinstan":
        age = time.time() - rec.get("created_at", 0)
        if age < OTPINSTAN_CANCEL_MIN:
            wait = int(OTPINSTAN_CANCEL_MIN - age)
            send_message(
                chat_id,
                f"⏳ Belum bisa ganti nomor — aturan anti-abuse OTP Instan: "
                f"cancel setelah 2 menit.\nTunggu <b>{wait} detik</b> lagi, "
                f"lalu tekan 🔄 Ganti Nomor lagi.")
            return
    # tandai agar poller tidak menyentuh order ini selama proses tukar
    with _lock:
        cur = _state["active_orders"].get(okey)
        if cur:
            cur["changing"] = True
    save_state()
    if msg_id:
        edit_message(chat_id, msg_id,
                     "🔄 <i>Menukar nomor… order lama dibatalkan, saldo kembali.</i>")
    if p == "litensi":
        _change_number_litensi(chat_id, okey, rec, prov, msg_id)
    else:
        _change_number_reorder(chat_id, okey, rec, prov, msg_id)


def _change_number_litensi(chat_id, okey, rec, prov, msg_id):
    p = rec["provider"]
    r = prov.resend(rec["order_id"])  # setStatus=3 = minta nomor lain
    new_phone = None
    if r["ok"]:
        raw = str((r["data"] or {}).get("raw") or "")
        # respons sukses: ACCESS_NUMBER:<id>:<nomor_baru>
        if raw.startswith("ACCESS_NUMBER:"):
            parts = raw.split(":")
            if len(parts) >= 3 and parts[2]:
                new_phone = parts[2]
    with _lock:
        cur = _state["active_orders"].get(okey)
        if cur:
            cur.pop("changing", None)
            if new_phone:
                cur["phone"] = new_phone
    save_state()
    if r["ok"] and new_phone:
        txt = (f"🔄 <b>Nomor berhasil diganti!</b>\n\n"
               f"📱 Nomor baru: <code>{esc(new_phone)}</code>\n"
               f"🆔 Order ID: <code>{esc(rec['order_id'])}</code> (tetap)\n"
               f"Layanan: {esc(rec.get('service_name') or rec.get('service'))}\n\n"
               f"<i>Nomor lama dibatalkan & saldonya kembali.</i>")
        if msg_id:
            edit_message(chat_id, msg_id, txt, _order_buttons(p, okey))
        else:
            send_message(chat_id, txt, _order_buttons(p, okey))
    else:
        err = r["error"] if not r["ok"] else \
            "Respons nomor baru tak dikenal dari Litensi."
        send_message(chat_id, f"❌ Gagal ganti nomor:\n{err}\n"
                              f"Nomor lama masih aktif & terpantau.")
    log.info("change_number litensi %s ok=%s new=%s",
             okey, r["ok"], bool(new_phone))


def _change_number_reorder(chat_id, okey, rec, prov, msg_id):
    p = rec["provider"]
    svc, cid, op = rec.get("service"), rec.get("country"), rec.get("operator")

    def _fail_local(msg):
        with _lock:
            cur = _state["active_orders"].get(okey)
            if cur:
                cur.pop("changing", None)
        save_state()
        send_message(chat_id, msg)

    rc = prov.cancel(rec["order_id"])
    if not rc["ok"]:
        _fail_local(f"❌ Gagal membatalkan order lama:\n{rc['error']}\n"
                    f"Nomor tidak jadi diganti.")
        log.info("change_number %s cancel gagal", okey)
        return
    # order baru dengan pilihan yang persis sama
    if p == "otpcepat":
        r = prov.order(svc, cid, operator=op or "random")
    elif prov.has_countries:
        r = prov.order(svc, cid)
    else:
        r = prov.order(svc)
    if not r["ok"]:
        # order lama sudah ter-cancel (refund); order baru gagal
        with _lock:
            _state["active_orders"].pop(okey, None)
        save_state()
        send_message(chat_id,
                     f"🔄 Order lama dibatalkan (saldo kembali), tapi order "
                     f"nomor baru gagal:\n{r['error']}\n"
                     f"Silakan order manual dari menu.")
        log.info("change_number %s reorder gagal", okey)
        return
    d = r["data"]
    now = time.time()
    timeout_at = _order_timeout(p, d, now)
    new_okey = f"{p}:{d['order_id']}"
    with _lock:
        _state["active_orders"].pop(okey, None)
        _state["active_orders"][new_okey] = {
            "provider": p, "order_id": str(d["order_id"]),
            "phone": str(d.get("phone") or ""),
            "service": svc,
            "service_name": rec.get("service_name") or svc,
            "country": cid, "country_name": rec.get("country_name"),
            "operator": op, "operator_name": rec.get("operator_name"),
            "created_at": now, "timeout_at": timeout_at,
            "last_code": None, "notified_no_code": False,
            "token": d.get("token"), "allow_retry": d.get("allow_retry"),
        }
    save_state()
    txt = (f"🔄 <b>Nomor berhasil diganti!</b>\n\n"
           f"📱 Nomor baru: <code>{esc(d.get('phone'))}</code>\n"
           f"🆔 Order ID baru: <code>{esc(d.get('order_id'))}</code>\n"
           f"Layanan: {esc(rec.get('service_name') or svc)}\n\n"
           f"<i>Order lama dibatalkan & saldonya kembali. "
           f"Bot lanjut memantau nomor baru.</i>")
    if msg_id:
        edit_message(chat_id, msg_id, txt, _order_buttons(p, new_okey))
    else:
        send_message(chat_id, txt, _order_buttons(p, new_okey))
    log.info("change_number %s -> %s phone=%s", okey, new_okey,
             d.get("phone"))


def do_cancel(chat_id, okey, via_edit=True, msg_id=None):
    rec = get_order(okey)
    if not rec:
        send_message(chat_id, "Order sudah tidak aktif.")
        return
    if rec["provider"] == "otpinstan":
        age = time.time() - rec.get("created_at", 0)
        if age < OTPINSTAN_CANCEL_MIN:
            wait = int(OTPINSTAN_CANCEL_MIN - age)
            send_message(
                chat_id,
                f"⏳ Belum bisa dibatalkan — aturan anti-abuse OTP Instan: "
                f"cancel setelah 2 menit.\nTunggu <b>{wait} detik</b> lagi ya.")
            return
    prov = get_provider(rec["provider"])
    if not prov:
        send_message(chat_id, need_key_text(rec["provider"]))
        return
    r = prov.cancel(rec["order_id"])
    if r["ok"]:
        remove_order(okey)
        txt = (f"❌ Order <code>{esc(rec['order_id'])}</code> dibatalkan.\n"
               f"Saldo dikembalikan oleh provider (refund).")
    else:
        txt = f"❌ Gagal membatalkan:\n{r['error']}"
    if via_edit and msg_id:
        edit_message(chat_id, msg_id, txt,
                     kb([[btn("◀️ Menu", f"prov:{rec['provider']}")]]))
    else:
        send_message(chat_id, txt)
    log.info("cancel %s ok=%s", okey, r["ok"])


def show_active(chat_id, msg_id, p, edit=True):
    with _lock:
        orders = [(k, dict(v)) for k, v in _state["active_orders"].items()
                  if v.get("provider") == p]
    title = PROVIDER_TITLES.get(p, p)
    if not orders:
        txt = f"📦 Tidak ada order aktif di <b>{esc(title)}</b>."
        rows = [[btn("◀️ Kembali", f"prov:{p}")]]
    else:
        lines = [f"📦 <b>Order aktif — {esc(title)}</b>"]
        rows = []
        for okey, rec in orders:
            age = int((time.time() - rec.get("created_at", 0)) / 60)
            lines.append(
                f"• <code>{esc(rec['phone'])}</code> "
                f"({esc(rec.get('service_name') or rec.get('service'))}) — "
                f"<code>{esc(rec['order_id'])}</code>, {age} mnt")
            rows.append([btn(f"🔑 Cek {rec['order_id'][-8:]}", f"ock:{okey}"),
                         btn("🔄 Ganti", f"och:{okey}"),
                         btn("❌ Batal", f"ocx:{okey}")])
        rows.append([btn("◀️ Kembali", f"prov:{p}")])
        txt = "\n".join(lines)
    if edit:
        edit_message(chat_id, msg_id, txt, kb(rows))
    else:
        send_message(chat_id, txt, kb(rows))


def manual_check(chat_id, okey):
    rec = get_order(okey)
    if not rec:
        send_message(chat_id, "Order sudah tidak aktif.")
        return
    prov = get_provider(rec["provider"])
    if not prov:
        send_message(chat_id, need_key_text(rec["provider"]))
        return
    r = prov.check(rec["order_id"])
    if not r["ok"]:
        send_message(chat_id, f"❌ {r['error']}")
        return
    st, code = r["data"].get("state"), r["data"].get("code")
    if st == "ok" and code:
        with _lock:
            cur = _state["active_orders"].get(okey)
            if cur:
                cur["last_code"] = code
        save_state()
        send_otp_message(okey, rec, code)
    elif st == "wait":
        send_message(chat_id,
                     f"⏳ Order <code>{esc(rec['order_id'])}</code> masih "
                     f"menunggu SMS…")
    else:
        send_message(chat_id, f"Status: {esc(st)}")


# ---------------------------------------------------------------- poller
def poll_loop():
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            poll_tick()
        except Exception:
            log.exception("poll_tick error")


def poll_tick():
    with _lock:
        snapshot = list(_state["active_orders"].items())
    now = time.time()
    for okey, rec in snapshot:
        try:
            if rec.get("changing"):
                continue  # sedang ditukar nomornya, jangan disentuh
            if now > rec.get("timeout_at", 0):
                remove_order(okey)
                send_message(
                    OWNER,
                    f"⌛ Order <code>{esc(rec['order_id'])}</code> "
                    f"({esc(rec.get('service_name') or '')}) kedaluwarsa "
                    f"(20 menit) — polling dihentikan.")
                log.info("expired %s", okey)
                continue
            prov = get_provider(rec["provider"])
            if not prov:
                continue
            r = prov.check(rec["order_id"])
            if not r["ok"]:
                log.warning("check %s gagal: %s", okey, r["error"])
                continue
            st = r["data"].get("state")
            code = r["data"].get("code")
            if st == "ok":
                cur = get_order(okey)
                if code and code != cur.get("last_code"):
                    send_otp_message(okey, rec, code)
                    with _lock:
                        if okey in _state["active_orders"]:
                            _state["active_orders"][okey]["last_code"] = code
                    save_state()
                elif not code and not cur.get("notified_no_code"):
                    send_message(
                        OWNER,
                        f"📩 SMS masuk untuk order "
                        f"<code>{esc(rec['order_id'])}</code> tapi kode belum "
                        f"terbaca otomatis — cek manual via 📦 Order Aktif.")
                    with _lock:
                        if okey in _state["active_orders"]:
                            _state["active_orders"][okey]["notified_no_code"] = True
                    save_state()
            elif st in ("cancel", "expired", "notfound"):
                remove_order(okey)
                send_message(
                    OWNER,
                    f"ℹ️ Order <code>{esc(rec['order_id'])}</code> "
                    f"berstatus <b>{esc(st)}</b> di provider — "
                    f"dihentikan dari pantauan.")
                log.info("order %s status %s, dihentikan", okey, st)
            elif st == "done":
                remove_order(okey)
                msg = (f"✅ Order <code>{esc(rec['order_id'])}</code> "
                       f"selesai di provider.")
                if code:
                    msg += f"\n📩 Kode OTP: <code>{esc(code)}</code>"
                send_message(OWNER, msg)
                log.info("order %s done, dihentikan", okey)
        except Exception:
            log.exception("poll order %s", okey)


# ---------------------------------------------------------------- commands
HELP_TEXT = """🤖 <b>OTP Bot — bantuan</b>

<b>Perintah:</b>
/start — pilih provider
/saldo [provider] — cek saldo
/layanan [provider] — daftar layanan
/order [provider] — order nomor baru
/batal &lt;order_id&gt; — batalkan order aktif
/server — ganti server OTP Instan (s1..s5)
/setkey &lt;provider&gt; — simpan API key
<i>provider: litensi|otpinstan|ninjatop|otpcepat|dehuy</i>
/bantuan — pesan ini

<b>Alur order:</b> pilih layanan → konfirmasi harga →
nomor keluar → bot memantau SMS tiap 5 detik → kode OTP dikirim otomatis.

🌏 Negara otomatis <b>Indonesia</b> di semua provider — langkah pilih
negara dilewati (di OTPCepat namanya "Wakanda (Indo)").

<i>Tips: di daftar layanan ada tombol 🔍 Cari — ketik saja namanya,
nggak perlu geser-geser halaman.</i>

<i>Contoh: /setkey litensi lalu kirim key-nya.
Contoh: /order ninjatop</i>
"""


def cmd_balance(chat_id, p):
    if not p:
        lines = ["💰 <b>Saldo semua provider</b>"]
        for name in PROVIDER_CLASSES:
            prov = get_provider(name)
            if not prov:
                lines.append(f"• {PROVIDER_TITLES[name]}: <i>key belum diisi</i>")
                continue
            r = prov.get_balance()
            lines.append(f"• {PROVIDER_TITLES[name]}: "
                         f"{rupiah(r['data'].get('balance')) if r['ok'] else '❌ ' + r['error']}")
        send_message(chat_id, "\n".join(lines))
        return
    prov = get_provider(p)
    if not prov:
        send_message(chat_id, need_key_text(p))
        return
    r = prov.get_balance()
    send_message(chat_id,
                 f"💰 <b>Saldo {esc(PROVIDER_TITLES[p])}</b>\n\n{rupiah(r['data'].get('balance'))}"
                 if r["ok"] else f"❌ {r['error']}")


def cmd_services(chat_id, p):
    p = p or _state.get("provider", "litensi")
    prov = get_provider(p)
    if not prov:
        send_message(chat_id, need_key_text(p))
        return
    if p in ("otpinstan", "otpcepat"):
        # daftar layanan kedua provider ini wajib pakai negara —
        # otomatis Indonesia, langkah pilih negara dilewati.
        cid, cname, err = _resolve_indonesia(p)
        if err or not cid:
            send_message(chat_id, f"❌ {err or 'Gagal menemukan Indonesia.'}")
            return
        r = prov.get_services(cid)
        if not r["ok"]:
            send_message(chat_id, f"❌ {r['error']}")
            return
        svcs = r["data"]["services"]
        send_message(chat_id, "\n".join(_services_lines(p, svcs, cname)),
                     kb([[btn("🛒 Order", f"ord:{p}")]]))
        return
    r = prov.get_services()
    if not r["ok"]:
        send_message(chat_id, f"❌ {r['error']}")
        return
    svcs = r["data"]["services"]
    send_message(chat_id, "\n".join(_services_lines(p, svcs)),
                 kb([[btn("🛒 Order", f"ord:{p}")]]))


def save_api_key(chat_id, msg_id, p, key):
    key = (key or "").strip()
    if not key or len(key) < 6:
        send_message(chat_id, "❌ Key-nya kependekan, coba lagi.")
        return
    s = load_secrets()
    s.setdefault("api_keys", {})[p] = key
    save_secrets(s)
    try:
        delete_message(chat_id, msg_id)  # hapus pesan berisi key
    except Exception:
        pass
    send_message(chat_id,
                 f"🔑 API key <b>{esc(PROVIDER_TITLES[p])}</b> tersimpan ✅ "
                 f"(<code>{esc(mask_key(key))}</code>)\n"
                 f"<i>Pesan berisi key sudah dihapus.</i>")
    log.info("api key %s disimpan (…%s)", p, mask_key(key)[-4:])


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    if chat_id != OWNER:
        log.info("abaikan pesan dari chat %s", chat_id)
        return
    text = (msg.get("text") or "").strip()
    msg_id = msg.get("message_id")

    # aliran /setkey: pesan berikutnya adalah API key
    if chat_id in _pending_key and not text.startswith("/"):
        p = _pending_key.pop(chat_id)
        save_api_key(chat_id, msg_id, p, text)
        return

    # aliran cari layanan: pesan teks berikutnya adalah kata kunci
    if chat_id in _pending_search and not text.startswith("/"):
        do_search(chat_id, msg_id, text)
        return

    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else ""
    rest = " ".join(parts[1:])

    if cmd == "/start":
        _pending_key.pop(chat_id, None)
        _pending_search.pop(chat_id, None)
        send_start(chat_id)
    elif cmd == "/setkey":
        if arg not in PROVIDER_CLASSES:
            send_message(chat_id,
                         "Pakai: <code>/setkey litensi|otpinstan|ninjatop|otpcepat</code>")
            return
        _pending_key[chat_id] = arg
        send_message(chat_id,
                     f"Kirim <b>API key {esc(PROVIDER_TITLES[arg])}</b> "
                     f"sebagai pesan berikutnya.\n"
                     f"⚠️ Pesanmu akan langsung kuhapus setelah tersimpan — "
                     f"jangan forward ke siapa pun.")
    elif cmd == "/saldo":
        cmd_balance(chat_id, arg if arg in PROVIDER_CLASSES else "")
    elif cmd == "/layanan":
        cmd_services(chat_id, arg if arg in PROVIDER_CLASSES else "")
    elif cmd == "/order":
        p = arg if arg in PROVIDER_CLASSES else _state.get("provider", "litensi")
        start_order(chat_id, p)
    elif cmd == "/batal":
        if not rest:
            send_message(chat_id, "Pakai: <code>/batal &lt;order_id&gt;</code>")
            return
        okey, _ = find_order(rest)
        if not okey:
            send_message(chat_id, "❌ Order tidak ditemukan di pantauan aktif.")
            return
        do_cancel(chat_id, okey, via_edit=False)
    elif cmd == "/server":
        show_servers(chat_id, None)
    elif cmd in ("/bantuan", "/help"):
        send_message(chat_id, HELP_TEXT)
    else:
        send_message(chat_id, "Perintah tidak dikenal. /bantuan")


def show_servers(chat_id, msg_id):
    cur = (load_secrets().get("otpinstan_server") or "s1")
    rows = []
    from providers.otpinstan import SERVERS
    for s, cfg in SERVERS.items():
        mark = "✅ " if s == cur else ""
        rows.append([btn(f"{mark}Server {s.upper()} ({cfg['prefix']}*)",
                         f"setserver:{s}")])
    rows.append([btn("◀️ Kembali", "prov:otpinstan")])
    txt = ("⚙️ <b>Server OTP Instan</b>\n"
           "Stok & harga beda tiap server. Default: S1.")
    if msg_id:
        edit_message(chat_id, msg_id, txt, kb(rows))
    else:
        send_message(chat_id, txt, kb(rows))


# ---------------------------------------------------------------- callbacks
def _svc_name(prov, code, country=None):
    try:
        r = prov.get_services(country) if country else prov.get_services()
    except TypeError:
        r = prov.get_services()
    if r["ok"]:
        for s in r["data"]["services"]:
            if str(s["code"]) == str(code):
                return s["name"]
    return code


def _cty_name(prov, cid):
    r = prov.get_countries()
    if r["ok"]:
        for c in r["data"]["countries"]:
            if str(c["id"]) == str(cid):
                return c["name"]
    return cid


def _resolve_indonesia(p):
    """Cari (country_id, country_name) Indonesia di provider p.

    Dipakai agar langkah "pilih negara" otomatis terisi Indonesia —
    user langsung ke pilih layanan. Return (cid, name, error);
    error None bila sukses. Untuk provider tanpa konsep negara
    (NinjaOTP, DehuyOTPWA) mengembalikan (None, None, None).
    """
    cls = PROVIDER_CLASSES.get(p)
    if cls and not cls.has_countries:
        return None, None, None
    prov = get_provider(p)
    if not prov:
        return None, None, need_key_text(p)
    keys = getattr(prov, "INDONESIA_KEYS", ("indonesia",))
    r = prov.get_countries()
    if not r["ok"]:
        return None, None, r["error"]
    best = None
    for c in r["data"]["countries"]:
        name = str(c.get("name") or "")
        nl = name.lower()
        for kw in keys:
            if nl == kw:
                return str(c["id"]), name, None
            if kw in nl and best is None:
                best = (str(c["id"]), name)
    if best:
        return best[0], best[1], None
    return None, None, (f"Daftar negara {PROVIDER_TITLES.get(p, p)} "
                        "tidak memuat Indonesia.")


def handle_callback(q):
    cb_id = q["id"]
    msg = q.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    data = q.get("data", "")
    answer_callback(cb_id)
    if chat_id != OWNER:
        log.info("abaikan callback dari chat %s", chat_id)
        return
    parts = data.split(":")
    cmd = parts[0]

    try:
        if cmd == "menu":
            edit_message(chat_id, msg_id,
                         "👋 <b>OTP Bot</b>\nPilih provider dulu:",
                         kb(provider_rows()))
        elif cmd == "prov":
            p = parts[1]
            with _lock:
                _state["provider"] = p
            save_state()
            show_provider_menu(chat_id, msg_id, p)
        elif cmd == "bal":
            do_balance(chat_id, msg_id, parts[1])
        elif cmd == "svc":
            do_services(chat_id, msg_id, parts[1])
        elif cmd == "ord":
            start_order(chat_id, parts[1])
            try:
                delete_message(chat_id, msg_id)
            except Exception:
                pass
        elif cmd == "act":
            show_active(chat_id, msg_id, parts[1])
        elif cmd == "srv":
            show_servers(chat_id, msg_id)
        elif cmd == "setserver":
            s = parts[1]
            sec = load_secrets()
            sec["otpinstan_server"] = s
            save_secrets(sec)
            with _lock:
                _state["otpinstan_server"] = s
            save_state()
            show_provider_menu(chat_id, msg_id, "otpinstan")
            send_message(chat_id, f"⚙️ Server OTP Instan → <b>{esc(s.upper())}</b>")
        elif cmd == "key":
            p = parts[1]
            _pending_key[chat_id] = p
            send_message(chat_id,
                         f"Kirim <b>API key {esc(PROVIDER_TITLES[p])}</b> "
                         f"sebagai pesan berikutnya.\n"
                         f"⚠️ Pesanmu akan langsung kuhapus setelah tersimpan.")
        # -- wizard umum (layanan dulu): litensi & ninjatop --
        elif cmd == "wsp":
            wiz_services_page(chat_id, msg_id, parts[1], int(parts[2]))
        elif cmd == "ws":
            p, pg, code = parts[1], int(parts[2]), ":".join(parts[3:])
            prov = get_provider(p)
            w = _wiz.setdefault(chat_id, {"p": p})
            w.update({"p": p, "service": code,
                      "service_name": _svc_name(prov, code),
                      "country": None, "country_name": None,
                      "operator": None})
            if prov.has_countries:
                # Negara otomatis: Indonesia — langkah pilih negara
                # dilewati, langsung ke pilih operator (Litensi).
                cid, cname, err = _resolve_indonesia(p)
                if err or not cid:
                    edit_message(
                        chat_id, msg_id,
                        f"❌ {err or 'Gagal menemukan Indonesia.'}",
                        kb([[btn("◀️ Kembali", f"prov:{p}")]]))
                else:
                    w.update({"country": cid, "country_name": cname})
                    if p == "litensi":
                        wiz_operators_page(chat_id, msg_id, p,
                                           w["service_name"] or code, cid)
                    else:
                        wiz_confirm(chat_id, msg_id, w)
            else:
                wiz_confirm(chat_id, msg_id, w)
        elif cmd == "wcp":
            p, svc, pg = parts[1], parts[2], int(parts[3])
            wiz_countries_page(chat_id, msg_id, p, svc, pg)
        elif cmd == "wc":
            p, svc, pg, cid = parts[1], parts[2], parts[3], ":".join(parts[4:])
            prov = get_provider(p)
            w = _wiz.setdefault(chat_id, {"p": p})
            w.update({"service": svc,
                      "service_name": _svc_name(prov, svc),
                      "country": cid, "country_name": _cty_name(prov, cid),
                      "operator": None})
            if p == "litensi":
                wiz_operators_page(chat_id, msg_id, p, w["service_name"]
                                   or svc, cid)
            else:
                wiz_confirm(chat_id, msg_id, w)
        elif cmd == "wo":
            op = ":".join(parts[1:]) or "any"
            w = _wiz.get(chat_id)
            if not w or not w.get("service"):
                return
            w["operator"] = op
            if op in ("any", "random"):
                w["operator_name"] = ("Bebas (termurah)"
                                     if w["p"] == "litensi" else "Acak")
            else:
                w["operator_name"] = op.capitalize()
            wiz_confirm(chat_id, msg_id, w)
        # -- daftar layanan OTP Instan: pilih negara dulu --
        elif cmd == "sl1":
            cf_countries_page(chat_id, msg_id, 0, "otpinstan", mode="list")
        elif cmd == "sl1c":
            cf_show_services(chat_id, msg_id, "otpinstan", ":".join(parts[1:]))
        # -- daftar layanan OTPCepat: pilih negara dulu --
        elif cmd == "sc1":
            cf_countries_page(chat_id, msg_id, int(parts[1]), "otpcepat",
                              mode="list")
        elif cmd == "sc1c":
            cf_show_services(chat_id, msg_id, "otpcepat", ":".join(parts[1:]))
        # -- wizard OTPCepat: negara -> layanan -> operator --
        elif cmd == "oc1":
            cf_countries_page(chat_id, msg_id, int(parts[1]), "otpcepat",
                              mode="order")
        elif cmd == "oc1c":
            cid = ":".join(parts[1:])
            prov = get_provider("otpcepat")
            w = _wiz.setdefault(chat_id, {"p": "otpcepat"})
            w.update({"p": "otpcepat", "country": cid,
                      "country_name": _cty_name(prov, cid),
                      "service": None, "service_name": None,
                      "operator": None})
            cf_services_page(chat_id, msg_id, "otpcepat", cid, 0)
        elif cmd == "osp1":
            cf_services_page(chat_id, msg_id, "otpcepat", parts[1],
                             int(parts[2]))
        elif cmd == "os1":
            cid, pg, sid = parts[1], int(parts[2]), ":".join(parts[3:])
            prov = get_provider("otpcepat")
            w = _wiz.setdefault(chat_id, {"p": "otpcepat"})
            w.update({"service": sid,
                      "service_name": _svc_name(prov, sid, cid),
                      "operator": None})
            wiz_operators_page(chat_id, msg_id, "otpcepat",
                               w["service_name"] or sid, cid)
        # -- wizard OTP Instan: negara dulu, baru layanan --
        elif cmd == "wc1":
            wiz_oi_countries(chat_id, msg_id, int(parts[1]))
        elif cmd == "wc1c":
            cid = ":".join(parts[1:])
            prov = get_provider("otpinstan")
            w = _wiz.setdefault(chat_id, {"p": "otpinstan"})
            w.update({"country": cid,
                      "country_name": _cty_name(prov, cid),
                      "service": None, "service_name": None})
            wiz_oi_services(chat_id, msg_id, cid, 0)
        elif cmd == "wsp1":
            wiz_oi_services(chat_id, msg_id, parts[1], int(parts[2]))
        elif cmd == "ws1":
            cid, pg, pid = parts[1], int(parts[2]), ":".join(parts[3:])
            prov = get_provider("otpinstan")
            w = _wiz.setdefault(chat_id, {"p": "otpinstan"})
            w.update({"service": pid,
                      "service_name": _svc_name(prov, pid, cid)})
            wiz_confirm(chat_id, msg_id, w)
        elif cmd == "wgo":
            do_order(chat_id, msg_id)
        elif cmd == "wfind":
            w = _wiz.get(chat_id)
            if not w:
                return
            _pending_search[chat_id] = {"p": w["p"],
                                       "cid": w.get("country")}
            send_message(chat_id,
                         "🔍 <b>Cari layanan</b>\n"
                         "Ketik nama layanannya, mis. <i>whatsapp</i>, "
                         "<i>telegram</i>, <i>dana</i>.",
                         kb([[btn("❌ Batal", "wxfind")]]))
        elif cmd == "wxfind":
            ctx = _pending_search.pop(chat_id, None)
            if not ctx:
                return
            p, cid = ctx["p"], ctx.get("cid")
            if cid:
                cf_services_page(chat_id, msg_id, p, cid, 0)
            else:
                wiz_services_page(chat_id, msg_id, p, 0)
        elif cmd == "wx":
            _wiz.pop(chat_id, None)
            edit_message(chat_id, msg_id, "❌ Order dibatalkan.",
                         kb([[btn("◀️ Menu", "menu")]]))
        # -- aksi order aktif --
        elif cmd in ("of", "ors", "ocx", "ock", "och"):
            okey = ":".join(parts[1:])
            if cmd == "of":
                do_finish(chat_id, okey, via_edit=True, msg_id=msg_id)
            elif cmd == "ors":
                do_resend(chat_id, okey)
            elif cmd == "ocx":
                do_cancel(chat_id, okey, via_edit=True, msg_id=msg_id)
            elif cmd == "ock":
                manual_check(chat_id, okey)
            elif cmd == "och":
                do_change_number(chat_id, okey, msg_id=msg_id)
        else:
            log.warning("callback tak dikenal: %s", data)
    except Exception:
        log.exception("callback %s", data)


def handle_update(u):
    if "message" in u:
        handle_message(u["message"])
    elif "callback_query" in u:
        handle_callback(u["callback_query"])


# ---------------------------------------------------------------- main
def main():
    global TOKEN, OWNER
    TOKEN = get_token()
    OWNER = get_owner()
    if not TOKEN:
        print("ERROR: isi TELEGRAM_BOT_TOKEN (env) atau bot_token di secrets.json",
              file=sys.stderr)
        sys.exit(1)
    if not OWNER:
        print("ERROR: isi OWNER_CHAT_ID (env) atau owner_chat_id di secrets.json",
              file=sys.stderr)
        sys.exit(1)
    me = tg_api("getMe")
    log.info("bot: @%s", (me.get("result") or {}).get("username", "?"))
    # buang update basi agar restart tidak memproses ulang perintah lama
    try:
        r = tg_api("getUpdates", {"timeout": 0})
        ids = [u.get("update_id", 0) for u in r.get("result", [])]
        offset = max(ids) + 1 if ids else 0
    except Exception:
        offset = 0
    load_state()
    threading.Thread(target=poll_loop, daemon=True).start()
    log.info("otpbot jalan. owner=%s", OWNER)
    while True:
        try:
            r = tg_api("getUpdates", {"timeout": 50, "offset": offset},
                       timeout=70)
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(3)
            continue
        if not r.get("ok"):
            log.warning("getUpdates gagal: %s", r)
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u.get("update_id", offset) + 1
            try:
                handle_update(u)
            except Exception:
                log.exception("handle_update")


if __name__ == "__main__":
    main()
