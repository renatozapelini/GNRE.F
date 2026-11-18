"""Configuração do BypassGuia."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .paths import CONFIG_PATH

PADRAO: dict[str, Any] = {
    "modo_visivel": False,
    "abrir_pdf": True,
    "tentar_visivel_em_captcha": True,
    "timeout_ms": 45000,
    "espera_portal_ms": 20000,
    "ufs": {
        "SP": {
            "ativo": True,
            "url": "https://www4.fazenda.sp.gov.br/DareICMS/DareAvulso",
            "receita_transporte": "11101",
            "observacao": "CT-e {chave}",
            "telefone_fallback": "4835121000",
        },
        "ES": {
            "ativo": True,
            "url": "https://s1-internet.sefaz.es.gov.br/site-internet-dua/emissao/dua/icms-transporte",
            "servico_outros_estados": "1458",
            "servico_autonomo": "1459",
            "informacao": "CT-e {chave}",
            "exige_cte": True,
        },
        "PR": {
            "ativo": True,
            "url": "https://emitirgrpr.sefa.pr.gov.br/arrecadacao/emitir/guiatela",
            "categoria": 1,
            "categoria_texto": "ICMS",
            "arrecadacao": 1317,
            "arrecadacao_texto": "Transporte",
            "uga": 990000,
            "telefone_fallback": "4835121000",
            "email": "",
            "exige_cte": True,
        },
        "GO": {
            "ativo": True,
            "url": "https://arr.economia.go.gov.br/arr-www/view/entradaContribuinte.jsf",
            "tipo_contribuinte": "nao_inscrito",
            "menu_texto": "OUTRAS RECEITAS DO TESOURO ESTADUAL",
            "receita_codigo": "",
            "receita_texto": "TRANSPORTE",
        },
    },
}


def _merge(base: dict[str, Any], dados: dict[str, Any]) -> dict[str, Any]:
    resultado = deepcopy(base)
    for chave, valor in dados.items():
        if isinstance(valor, dict) and isinstance(resultado.get(chave), dict):
            resultado[chave] = _merge(resultado[chave], valor)
        else:
            resultado[chave] = valor
    return resultado


def carregar() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        salvar(PADRAO)
        return deepcopy(PADRAO)
    try:
        dados = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return deepcopy(PADRAO)
    return _merge(PADRAO, dados)


def salvar(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
