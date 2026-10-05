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
}
# nama konektor Secure Vault (fallback terakhir, khusus lingkungan Muse)
# custom.telegram_otp dipakai, bukan custom.telegram, karena yang terakhir
# sudah terisi token bot GrabFood (@grabnotifsy_bot) — jangan ditimpa.
VAULT_CONNECTORS = {
    "bot_token": "custom.telegram_otp",
    "litensi": "custom.litensi",
    "otpinstan": "custom.otpinstan",
    "ninjatop": "custom.ninjatop",
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


# ---------------------------------------------------------------- menu & wizard
def provider_rows():
    rows = []
    for p in ("litensi", "otpinstan", "ninjatop"):
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


def do_services(chat_id, msg_id, p):
    prov = get_provider(p)
    if not prov:
        edit_message(chat_id, msg_id, need_key_text(p),
                     kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        return
    r = prov.get_services()
    title = PROVIDER_TITLES.get(p, p)
    if not r["ok"]:
        edit_message(chat_id, msg_id,
                     f"❌ Gagal ambil layanan {esc(title)}:\n{r['error']}",
                     kb([[btn("◀️ Kembali", f"prov:{p}")]]))
        return
    svcs = r["data"]["services"]
    lines = [f"📋 <b>Layanan {esc(title)}</b> ({len(svcs)})"]
    for s in svcs[:60]:
        extra = ""
        if s.get("price") is not None:
            extra = f" — {rupiah(s['price'])}"
            if s.get("stock") is not None:
                extra += f" (stok {s['stock']})"
        lines.append(f"• {esc(s['name'])} <code>{esc(s['code'])}</code>{extra}")
    if len(svcs) > 60:
        lines.append(f"<i>…dan {len(svcs) - 60} lainnya, pakai /order untuk pilih via tombol.</i>")
    edit_message(chat_id, msg_id, "\n".join(lines),
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
    """Langkah pilih operator (khusus Litensi)."""
    prov = get_provider(p)
    ops = []
    if prov:
        r = prov.get_operators(cid)
        if r["ok"]:
            ops = r["data"]["operators"]
    rows = [[btn("🌐 Bebas (termurah)", "wo:any")]]
    for i in range(0, len(ops), 2):
        row = [btn(f"📶 {ops[i].capitalize()}", f"wo:{ops[i]}")]
        if i + 1 < len(ops):
            row.append(btn(f"📶 {ops[i + 1].capitalize()}", f"wo:{ops[i + 1]}"))
        rows.append(row)
    rows.append([btn("❌ Batal", "wx")])
    edit_message(chat_id, msg_id,
                 f"Pilih operator nomor untuk <b>{esc(svc)}</b>:\n"
                 f"<i>\"Bebas\" = dipilihkan yang termurah.</i>",
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
    if w.get("operator"):
        op = w["operator"]
        lines.append(f"Operator: <b>{esc('Bebas (termurah)' if op == 'any' else op.capitalize())}</b>")
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
    _wiz[chat_id] = {"p": p, "service": None, "service_name": None,
                     "country": None, "country_name": None,
                     "operator": None, "price": None}
    if p == "otpinstan":
        wiz_oi_countries(chat_id, None, 0, edit=False)
    else:
        wiz_services_page(chat_id, None, p, 0, edit=False)


# -- wizard khusus OTP Instan: negara dulu, baru layanan --
def wiz_oi_countries(chat_id, msg_id, pg, edit=True):
    prov = get_provider("otpinstan")
    r = prov.get_countries() if prov else {"ok": False}
    if not r["ok"]:
        txt = need_key_text("otpinstan") if not prov else f"❌ {r.get('error')}"
        if edit:
            edit_message(chat_id, msg_id, txt,
                         kb([[btn("◀️ Kembali", "prov:otpinstan")]]))
        else:
            send_message(chat_id, txt)
        return
    ctys = r["data"]["countries"]
    total = len(ctys)
    pg = max(0, min(pg, (total - 1) // PAGE if total else 0))
    chunk = ctys[pg * PAGE:(pg + 1) * PAGE]
    rows = []
    for i in range(0, len(chunk), 2):
        row = [btn(f"{chunk[i]['name'][:18]}", f"wc1c:{chunk[i]['id']}")]
        if i + 1 < len(chunk):
            row.append(btn(f"{chunk[i + 1]['name'][:18]}",
                           f"wc1c:{chunk[i + 1]['id']}"))
        rows.append(row)
    nav = _page_nav("wc1", pg, total)
    if nav:
        rows.append(nav)
    rows.append([btn("❌ Batal", "wx")])
    txt = (f"🛒 <b>Order — OTP Instan</b>\n"
           f"Pilih negara (hal. {pg + 1}/{max(1, (total + PAGE - 1) // PAGE)}):")
    if edit:
        edit_message(chat_id, msg_id, txt, kb(rows))
    else:
        send_message(chat_id, txt, kb(rows))


def wiz_oi_services(chat_id, msg_id, cid, pg):
    prov = get_provider("otpinstan")
    r = prov.get_services(cid) if prov else {"ok": False}
    if not r["ok"]:
        edit_message(chat_id, msg_id,
                     f"❌ {r.get('error', 'gagal ambil layanan')}",
                     kb([[btn("◀️ Kembali", "prov:otpinstan")]]))
        return
    svcs = r["data"]["services"]
    total = len(svcs)
    if not total:
        edit_message(chat_id, msg_id, "📵 Tidak ada layanan di negara ini.",
                     kb([[btn("◀️ Kembali", "prov:otpinstan")]]))
        return
    pg = max(0, min(pg, (total - 1) // PAGE))
    chunk = svcs[pg * PAGE:(pg + 1) * PAGE]
    rows = []
    for s in chunk:
        label = s["name"][:22]
        if s.get("price") is not None:
            label += f" {rupiah(s['price'])}"
        rows.append([btn(label, f"ws1:{cid}:{pg}:{s['code']}")])
    nav = _page_nav(f"wsp1:{cid}", pg, total)
    if nav:
        rows.append(nav)
    rows.append([btn("❌ Batal", "wx")])
    edit_message(chat_id, msg_id,
                 f"Pilih layanan (hal. {pg + 1}/{(total + PAGE - 1) // PAGE}):",
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
    timeout_at = now + ORDER_TIMEOUT
    if p == "ninjatop":
        exp = parse_expire(d.get("expire_at"))
        if exp and exp > now:
            timeout_at = exp
    okey = f"{p}:{d['order_id']}"
    add_order(okey, {
        "provider": p, "order_id": str(d["order_id"]),
        "phone": str(d.get("phone") or ""),
        "service": w["service"],
        "service_name": w.get("service_name") or w["service"],
        "country": w.get("country"), "country_name": w.get("country_name"),
        "created_at": now, "timeout_at": timeout_at,
        "last_code": None, "notified_no_code": False,
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
        kb([[btn("❌ Batalkan Order", f"ocx:{okey}"),
             btn("◀️ Menu", f"prov:{p}")]]))
    log.info("order %s (%s) phone=%s", okey, p, d.get("phone"))


# ---------------------------------------------------------------- aksi order aktif
def send_otp_message(okey, rec, code):
    send_message(
        OWNER,
        f"🔑 <b>Kode OTP masuk!</b>\n\n<code>{esc(code)}</code>\n\n"
        f"<i>Ketuk kode untuk menyalin.</i>\n"
        f"Order: <code>{esc(rec['order_id'])}</code> • "
        f"{esc(rec.get('service_name') or '')}",
        kb([[btn("✅ Selesai", f"of:{okey}"),
             btn("🔁 Minta Ulang", f"ors:{okey}")],
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
/setkey &lt;litensi|otpinstan|ninjatop&gt; — simpan API key
/bantuan — pesan ini

<b>Alur order:</b> pilih layanan → (pilih negara) → konfirmasi harga →
nomor keluar → bot memantau SMS tiap 5 detik → kode OTP dikirim otomatis.

<i>Contoh: /setkey litensi lalu kirim key-nya.
Contoh: /order ninjatop</i>
"""


def cmd_balance(chat_id, p):
    if not p:
        lines = ["💰 <b>Saldo semua provider</b>"]
        for name in ("litensi", "otpinstan", "ninjatop"):
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
    r = prov.get_services()
    if not r["ok"]:
        send_message(chat_id, f"❌ {r['error']}")
        return
    svcs = r["data"]["services"]
    lines = [f"📋 <b>Layanan {esc(PROVIDER_TITLES[p])}</b> ({len(svcs)})"]
    for s in svcs[:60]:
        extra = ""
        if s.get("price") is not None:
            extra = f" — {rupiah(s['price'])}"
            if s.get("stock") is not None:
                extra += f" (stok {s['stock']})"
        lines.append(f"• {esc(s['name'])} <code>{esc(s['code'])}</code>{extra}")
    if len(svcs) > 60:
        lines.append(f"<i>…{len(svcs) - 60} lainnya.</i>")
    send_message(chat_id, "\n".join(lines),
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

    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else ""
    rest = " ".join(parts[1:])

    if cmd == "/start":
        _pending_key.pop(chat_id, None)
        send_start(chat_id)
    elif cmd == "/setkey":
        if arg not in PROVIDER_CLASSES:
            send_message(chat_id,
                         "Pakai: <code>/setkey litensi|otpinstan|ninjatop</code>")
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
def _svc_name(prov, code):
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
                wiz_countries_page(chat_id, msg_id, p, code, 0)
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
            wiz_confirm(chat_id, msg_id, w)
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
                      "service_name": _svc_name(prov, pid)})
            wiz_confirm(chat_id, msg_id, w)
        elif cmd == "wgo":
            do_order(chat_id, msg_id)
        elif cmd == "wx":
            _wiz.pop(chat_id, None)
            edit_message(chat_id, msg_id, "❌ Order dibatalkan.",
                         kb([[btn("◀️ Menu", "menu")]]))
        # -- aksi order aktif --
        elif cmd in ("of", "ors", "ocx", "ock"):
            okey = ":".join(parts[1:])
            if cmd == "of":
                do_finish(chat_id, okey, via_edit=True, msg_id=msg_id)
            elif cmd == "ors":
                do_resend(chat_id, okey)
            elif cmd == "ocx":
                do_cancel(chat_id, okey, via_edit=True, msg_id=msg_id)
            elif cmd == "ock":
                manual_check(chat_id, okey)
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
