"""Encrypt small secrets (sign-in tokens) with Windows DPAPI for the current Windows user.

Only the same Windows user on the same laptop can decrypt them. Used by the Gmail and Outlook services.
"""
import ctypes
import sys
from ctypes import wintypes


class DPAPIError(Exception):
    """Encryption is unavailable (not Windows) or the data could not be decrypted."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise DPAPIError("Token encryption needs Windows (DPAPI).")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ui_forbidden = 0x1
    if not func(ctypes.byref(blob_in), None, None, None, None, ui_forbidden, ctypes.byref(blob_out)):
        raise DPAPIError("The data could not be encrypted or decrypted for this Windows user.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(data: bytes) -> bytes:
    return _crypt(data, protect=True)


def unprotect(data: bytes) -> bytes:
    return _crypt(data, protect=False)
