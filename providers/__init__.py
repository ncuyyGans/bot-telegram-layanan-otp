"""Paket konektor provider OTP untuk otpbot."""
from .litensi import LitensiProvider
from .otpinstan import OTPInstanProvider
from .ninjatop import NinjaTopProvider

PROVIDER_CLASSES = {
    "litensi": LitensiProvider,
    "otpinstan": OTPInstanProvider,
    "ninjatop": NinjaTopProvider,
}

PROVIDER_TITLES = {
    "litensi": "Litensi",
    "otpinstan": "OTP Instan",
    "ninjatop": "NinjaOTP",
}

__all__ = ["LitensiProvider", "OTPInstanProvider", "NinjaTopProvider",
           "PROVIDER_CLASSES", "PROVIDER_TITLES"]
