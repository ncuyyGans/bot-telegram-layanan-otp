"""Paket konektor provider OTP untuk otpbot."""
from .litensi import LitensiProvider
from .otpinstan import OTPInstanProvider
from .ninjatop import NinjaTopProvider
from .otpcepat import OTPCepatProvider
from .dehuy import DehuyProvider

PROVIDER_CLASSES = {
    "litensi": LitensiProvider,
    "otpinstan": OTPInstanProvider,
    "ninjatop": NinjaTopProvider,
    "otpcepat": OTPCepatProvider,
    "dehuy": DehuyProvider,
}

PROVIDER_TITLES = {
    "litensi": "Litensi",
    "otpinstan": "OTP Instan",
    "ninjatop": "NinjaOTP",
    "otpcepat": "OTPCepat",
    "dehuy": "DehuyOTPWA",
}

__all__ = ["LitensiProvider", "OTPInstanProvider", "NinjaTopProvider",
           "OTPCepatProvider", "DehuyProvider",
           "PROVIDER_CLASSES", "PROVIDER_TITLES"]
