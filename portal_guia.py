"""Guias estaduais emitidas no portal do estado, fora da GNRE.

Goiás não recolhe ICMS-ST por GNRE: a guia é emitida no ARR
(Sistema de Arrecadação), uma para o ICMS-ST (receita 116) e outra para o
FCP (Adicional ICMS 2% / PROTEGE, receita 4146).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from gnre_api import ResultadoEmissao

UFS_PORTAL = ("GO",)


def usa_portal(uf: str | None) -> bool:
    return (uf or "").strip().upper() in UFS_PORTAL


def emitir(
    caminho: str | Path,
    dados=None,
    log=None,
    visivel: bool | None = None,
    saida_dir: Path | None = None,
) -> ResultadoEmissao:
    """Emite os DAREs de GO e devolve o resultado no formato do app."""
    if dados is None:
        from leitor_nfe import ler_nfe_xml

        dados = ler_nfe_xml(caminho)

    def _log(mensagem: str) -> None:
        if log is not None:
            log(mensagem)

    vencimento = dados.data_vencimento or date.today()
    pagamento = dados.data_pagamento or date.today()
    destino = Path(saida_dir) if saida_dir is not None else Path(caminho).parent
    if not destino.is_absolute():
        destino = Path.cwd() / destino

    from portal_go import ErroPortalGo, emitir_go

    try:
        guias = emitir_go(
            dados,
            vencimento=vencimento,
            pagamento=pagamento,
            visivel=bool(visivel),
            saida_dir=destino,
            log=_log,
        )
    except ErroPortalGo as erro:
        return ResultadoEmissao(
            sucesso=False,
            ambiente="portal",
            dados=dados,
            mensagens=[str(erro)],
        )
    except Exception as erro:  # portal fora do ar, navegador ausente, etc.
        return ResultadoEmissao(
            sucesso=False,
            ambiente="portal",
            dados=dados,
            mensagens=[f"Falha no portal de Goiás: {erro}"],
        )

    principal = next((g for g in guias if g["rotulo"] == "ICMS-ST"), guias[0])
    resultado = ResultadoEmissao(
        sucesso=True,
        ambiente="portal",
        dados=dados,
        pdf_path=principal["pdf"],
    )
    resultado.avisos.append(
        "Goiás não emite esta guia pela GNRE; o recolhimento foi gerado no "
        "portal ARR (DARE)."
    )
    if len(guias) > 1:
        fcp = next((g for g in guias if g["rotulo"].startswith("PROTEGE")), None)
        if fcp is not None:
            resultado.avisos.append(
                f"O FCP (PROTEGE) saiu em DARE separado: {fcp['pdf'].name}."
            )
            resultado.mensagens.append(
                f"Segunda guia (PROTEGE/FCP): {fcp['pdf']}"
            )
    resultado.mensagens.append(
        "Confira no próprio DARE o valor, a data e a inscrição estadual antes de pagar."
    )
    return resultado
