"""Registro dos adapters por UF."""

from __future__ import annotations

from .base import BaseAdapter, CaptchaRequerido, DivergenciaGuia, FalhaPortal, Resultado
from .es import AdapterES
from .pr import AdapterPR
from .sp import AdapterSP

ADAPTERS: dict[str, type[BaseAdapter]] = {
    "ES": AdapterES,
    "PR": AdapterPR,
    "SP": AdapterSP,
}

UFS_ATENDIDAS = tuple(sorted(ADAPTERS))


def adapter_para(uf: str, config: dict, log=None) -> BaseAdapter:
    uf = (uf or "").strip().upper()
    classe = ADAPTERS.get(uf)
    if classe is None:
        raise FalhaPortal(f"Nenhum robô implementado para a UF {uf or '?'}.")
    return classe(config, log=log)


__all__ = [
    "ADAPTERS",
    "UFS_ATENDIDAS",
    "BaseAdapter",
    "CaptchaRequerido",
    "DivergenciaGuia",
    "FalhaPortal",
    "Resultado",
    "adapter_para",
]
