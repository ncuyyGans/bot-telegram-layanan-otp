"""Paket konektor provider OTP untuk otpbot."""
from .litensi import LitensiProvider
from .otpinstan import OTPInstanProvider
from .ninjatop import NinjaTopProvider
from .otpcepat import OTPCepatProvider

PROVIDER_CLASSES = {
    "litensi": LitensiProvider,
    "otpinstan": OTPInstanProvider,
    "ninjatop": NinjaTopProvider,
    "otpcepat": OTPCepatProvider,
}

PROVIDER_TITLES = {
    "litensi": "Litensi",
    "otpinstan": "OTP Instan",
    "ninjatop": "NinjaOTP",
    "otpcepat": "OTPCepat",
}

__all__ = ["LitensiProvider", "OTPInstanProvider", "NinjaTopProvider",
           "OTPCepatProvider", "PROVIDER_CLASSES", "PROVIDER_TITLES"]
