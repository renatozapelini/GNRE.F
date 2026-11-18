"""Dias úteis por UF para o vencimento da GNRE."""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

try:
    import holidays as _holidays
except ImportError:  # sem a biblioteca, ainda pula fins de semana
    _holidays = None


@lru_cache(maxsize=None)
def _feriados(uf: str | None, ano: int) -> frozenset[date]:
    if _holidays is None:
        return frozenset()
    try:
        calendario = _holidays.Brazil(years=ano, subdiv=uf, language="pt_BR")
    except Exception:
        return frozenset()
    return frozenset(dia for dia in calendario if dia.year == ano)


def eh_dia_util(dia: date, uf: str | None = None) -> bool:
    if dia.weekday() >= 5:
        return False
    return dia not in _feriados((uf or "").upper() or None, dia.year)


def proximo_dia_util(dia: date | None = None, uf: str | None = None) -> date:
    atual = dia or date.today()
    while not eh_dia_util(atual, uf):
        atual += timedelta(days=1)
    return atual


def vencimento_padrao(uf: str | None = None, hoje: date | None = None) -> date:
    """Vencimento = hoje; em fim de semana/feriado, o próximo dia útil da UF."""
    return proximo_dia_util(hoje or date.today(), uf)
