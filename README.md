# 🤖 OTP Bot

Bot Telegram **privat** untuk beli OTP online dari beberapa provider tanpa
buka website satu-satu. Pilih provider → cek saldo → pilih layanan → order
nomor → kode OTP otomatis dikirim ke chat saat SMS masuk.

Python 3, **stdlib only** (tanpa `pip install`), long-polling.

## Provider yang didukung

| Provider | Auth | Negara | Catatan |
|---|---|---|---|
| **Litensi** | `api_key` di query | Ya (pilih negara) | API gaya sms-activate |
| **OTP Instan** | Header `X-Api-Key` | Ya (negara dulu, baru layanan) | 5 server (s1–s5), bisa ganti via /server atau menu ⚙️ |
| **NinjaOTP** | Header `Authorization: Bearer nk_xxxx` | Tidak | Harga+stok langsung per layanan |

## Cara pakai

### 1. Buat bot Telegram
1. Chat ke [@BotFather](https://t.me/BotFather) → `/newbot` → ikuti langkahnya.
2. Salin **token** yang diberikan.

### 2. Ambil API key tiap provider
- **Litensi**: dashboard litensi.id → API key.
- **OTP Instan**: halaman API Key di dashboard → generate key.
- **NinjaOTP**: dashboard → menu "API Keys" (format `nk_xxxx`).

### 3. Isi secrets.json
```bash
cp secrets.example.json secrets.json
nano secrets.json   # isi bot_token, owner_chat_id, api_keys
chmod 600 secrets.json
```
`owner_chat_id` = ID Telegram kamu (chat ke [@userinfobot](https://t.me/userinfobot)
untuk tahu ID-mu). Bot **hanya** melayani chat ini — chat lain diabaikan.

> Alternatif: token & owner bisa via env `TELEGRAM_BOT_TOKEN` dan
> `OWNER_CHAT_ID` (env diprioritaskan). API key juga bisa diisi dari dalam
> bot dengan perintah `/setkey` — pesan berisi key otomatis dihapus setelah
> tersimpan.

### 4. Jalankan
```bash
./run.sh
# atau: python3 otpbot.py
```

### 5. Watchdog (biar hidup terus)
Tambahkan ke cron (`crontab -e`), tiap 5 menit:
```
*/5 * * * * /home/hatch/workspace/bot-telegram-otp/watchdog.sh
```
Menyalakan ulang bot bila proses mati (mis. VM restart). Order yang sedang
dipolling tersimpan di `state.json` dan dilanjutkan setelah restart.

## Perintah bot

| Perintah | Fungsi |
|---|---|
| `/start` | Pilih provider (tombol) |
| `/saldo [provider]` | Cek saldo (semua / satu provider) |
| `/layanan [provider]` | Daftar layanan + harga |
| `/order [provider]` | Mulai wizard order nomor |
| `/batal <order_id>` | Batalkan order aktif |
| `/server` | Ganti server OTP Instan (s1–s5) |
| `/setkey <litensi\|otpinstan\|ninjatop>` | Simpan API key via chat |
| `/bantuan` | Bantuan |

## Alur order

1. `/order` → pilih layanan (tombol, ada halaman bila banyak).
2. Pilih negara → tampil harga termurah + stok → ✅ Order.
   - *NinjaOTP*: langkah negara dilewati (tidak ada konsep negara).
   - *OTP Instan*: urutan dibalik — pilih negara dulu, baru layanan.
3. Nomor HP + Order ID dikirim (nomor bisa diketuk untuk salin).
4. Bot polling tiap 5 detik. Saat OTP masuk → kode dikirim **besar** +
   tombol **✅ Selesai** / **🔁 Minta Ulang** / **❌ Batalkan**.
5. Polling berhenti setelah 20 menit (order kedaluwarsa) atau setelah
   selesai/dibatalkan.

Catatan perilaku per provider:
- **Litensi**: Selesai = `setStatus 6`, Batal = `setStatus 8` (refund),
  Minta ulang = `setStatus 3`.
- **OTP Instan**: tidak ada endpoint finish → selesai = stop polling lokal.
  Cancel **baru bisa setelah 2 menit** (aturan anti-abuse) — bot menolak
  dengan hitung mundur bila terlalu cepat.
- **NinjaOTP**: Selesai = `POST /orders/{id}/ack`, Batal = `POST …/cancel`
  (refund penuh), Minta ulang = `POST …/resend` (gratis).

## File

```
bot-telegram-otp/
├── otpbot.py            # main loop long-polling + wizard + poller
├── providers/
│   ├── base.py          # interface + HTTP helper (retry/backoff)
│   ├── litensi.py
│   ├── otpinstan.py     # server s1–s5 (s1 penuh, s2–s5 best-effort)
│   └── ninjatop.py
├── secrets.json         # token, owner, API key (GITAIGNORE, chmod 600)
├── secrets.example.json # contoh format
├── state.json           # order aktif (GITAIGNORE) — lanjut setelah restart
├── bot.log              # log (GITAIGNORE)
├── run.sh / watchdog.sh
└── README.md
```

## Keamanan

- Bot dikunci ke satu `owner_chat_id`.
- API key tidak pernah dikirim lewat URL (OTP Instan & NinjaOTP pakai
  header), tidak pernah di-log, dan pesan berisi key dihapus dari chat.
- `secrets.json` / `state.json` / `bot.log` di-gitignore.

## Keterbatasan yang diketahui

- Pemetaan parameter order server OTP Instan **s2–s5** bersifat best-effort
  (identifier layanan beda per server); bila order gagal dengan
  `BAD_SERVICE`, pakai server s1 atau sesuaikan `order_style` di
  `providers/otpinstan.py`.
- Format respons `check.php` OTP Instan di-parse defensif; bila provider
  mengubah format, bot mencatat respons mentah di `bot.log`.
