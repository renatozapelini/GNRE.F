"""Proteção da senha do certificado com o DPAPI do Windows.

A senha fica vinculada ao usuário/máquina que a salvou; em outro computador
basta atualizar o certificado novamente pelo aplicativo.
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes


class ErroSeguranca(RuntimeError):
    pass


_CRYPTPROTECT_UI_FORBIDDEN = 0x01


if sys.platform == "win32":

    class _Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def disponivel() -> bool:
    return sys.platform == "win32"


def _blob_entrada(dados: bytes) -> tuple["_Blob", ctypes.Array]:
    buffer = ctypes.create_string_buffer(dados, len(dados))
    blob = _Blob(len(dados), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _ler_blob(blob: "_Blob") -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def _erro(acao: str) -> ErroSeguranca:
    codigo = ctypes.get_last_error()
    return ErroSeguranca(f"{acao} falhou (Windows erro {codigo}).")


def proteger(texto: str) -> str:
    if not disponivel():
        raise ErroSeguranca("A proteção da senha está disponível apenas no Windows.")
    if not texto:
        raise ErroSeguranca("Senha vazia.")
    entrada, _buffer = _blob_entrada(texto.encode("utf-8"))
    saida = _Blob()
    if not _crypt32.CryptProtectData(
        ctypes.byref(entrada),
        "Emissor GNRE",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(saida),
    ):
        raise _erro("CryptProtectData")
    return base64.b64encode(_ler_blob(saida)).decode("ascii")


def revelar(codigo: str) -> str:
    if not disponivel():
        raise ErroSeguranca("A proteção da senha está disponível apenas no Windows.")
    try:
        dados = base64.b64decode(codigo, validate=True)
    except ValueError as erro:
        raise ErroSeguranca("Senha protegida inválida no config.json.") from erro
    entrada, _buffer = _blob_entrada(dados)
    saida = _Blob()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(entrada),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(saida),
    ):
        raise _erro("CryptUnprotectData")
    try:
        return _ler_blob(saida).decode("utf-8")
    except UnicodeDecodeError as erro:
        raise ErroSeguranca("Senha protegida ilegível.") from erro
