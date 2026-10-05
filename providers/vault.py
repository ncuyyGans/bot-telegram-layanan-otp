"""Fallback secret dari Secure Vault (lingkungan Muse).

Urutan resolusi secret di bot:
  1. environment variable,
  2. secrets.json,
  3. konektor custom.* di Secure Vault.

Nilai dari vault adalah *surrogate* (mis. "hsurr:...") yang hanya dikenali
oleh egress proxy — JANGAN pernah di-percent-encode (lihat quirk Telegram:
surrogate yang ter-encode tidak diganti sehingga request gagal).
Modul ini stdlib-only dan aman diimpor di luar lingkungan Muse
(gagal diam-diam -> string kosong).
"""
import sys
import time

_cache = {}  # connector -> (value, timestamp)


def vault_surrogate(connector):
    """Kembalikan surrogate untuk konektor custom.*, atau '' bila tak ada."""
    now = time.time()
    hit = _cache.get(connector)
    if hit and hit[0] and now - hit[1] < 600:
        return hit[0]
    val = ""
    try:
        bin_path = "/opt/hatch/skills/skill-creator/bin"
        if bin_path not in sys.path:
            sys.path.insert(0, bin_path)
        from dynamic_credentials import dynamic_credential_entry
        entry = dynamic_credential_entry(connector) or {}
        val = (entry.get("surrogate") or "").strip()
    except Exception:
        val = ""
    _cache[connector] = (val, now)
    return val


def vault_connected(connector):
    """True bila konektor vault punya nilai (tanpa mengekspos nilainya)."""
    return bool(vault_surrogate(connector))
