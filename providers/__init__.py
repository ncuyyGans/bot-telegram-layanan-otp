"""Paket konektor provider OTP untuk otpbot."""
from .litensi import LitensiProvider
from .otpinstan import OTPInstanProvider
from .ninjatop import NinjaTopProvider
from .otpcepat import OTPCepatProvider
# DehuyOTPWA DIARSIPKAN 2026-10-09: admin provider memblokir pemakaian
# API via bot Telegram. Adapter tetap tersimpan di providers/dehuy.py —
# daftarkan lagi di bawah bila blokir dibuka.
# from .dehuy import DehuyProvider

PROVIDER_CLASSES = {
    "litensi": LitensiProvider,
    "otpinstan": OTPInstanProvider,
    "ninjatop": NinjaTopProvider,
    "otpcepat": OTPCepatProvider,
    # "dehuy": DehuyProvider,  # diarsipkan
}

PROVIDER_TITLES = {
    "litensi": "Litensi",
    "otpinstan": "OTP Instan",
    "ninjatop": "NinjaOTPWA",
    "otpcepat": "OTPCepat",
    # "dehuy": "DehuyOTPWA",  # diarsipkan
}

__all__ = ["LitensiProvider", "OTPInstanProvider", "NinjaTopProvider",
           "OTPCepatProvider",
           "PROVIDER_CLASSES", "PROVIDER_TITLES"]
