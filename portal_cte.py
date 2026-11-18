"""Guias de SP, ES e PR emitidas no portal do estado (robô BypassGuia).

Essas UFs não emitem o ICMS do transporte pela GNRE: a guia é gerada no
portal estadual. O robô reaproveita o mesmo XML do CT-e e confere o PDF
contra o XML antes de entregar (guia divergente nunca vira PDF pagável).
"""

from __future__ import annotations

import os
from pathlib import Path

from gnre_api import ResultadoEmissao

UFS_ROBO = ("SP", "ES", "PR")
_PREFIXO = {"aviso": "AVISO: ", "erro": "ERRO: "}


def usa_robo(uf: str | None) -> bool:
    return (uf or "").strip().upper() in UFS_ROBO


def configurar(dados_dir: str | Path) -> None:
    """Aponta o robô para a pasta do app (config, logs, perfil do navegador)."""
    os.environ["BYPASSGUIA_DIR"] = str(Path(dados_dir) / "portal")


def emitir(
    caminho: str | Path,
    dados=None,
    log=None,
    visivel: bool | None = None,
    saida_dir: Path | None = None,
) -> ResultadoEmissao:
    """Emite a guia no portal da UF e devolve o resultado no formato do app."""
    if dados is None:
        try:
            from cte_leitor import ler_cte_xml

            dados = ler_cte_xml(caminho)
        except Exception:
            dados = None
    uf = (getattr(dados, "uf_favorecida", "") or "").strip().upper()

    def _log(mensagem: str, nivel: str = "info") -> None:
        if log is not None:
            log(_PREFIXO.get(nivel, "") + mensagem)

    try:
        from bypassguia.config import carregar
        from bypassguia.runner import Runner

        config = carregar()
        if visivel is not None:
            config["modo_visivel"] = bool(visivel)
        runner = Runner(
            config=config,
            log=_log,
            saida_dir=Path(saida_dir) if saida_dir else None,
        )
        entrega = runner.processar(caminho)
    except Exception as erro:  # navegador ausente, portal fora do ar, etc.
        rotulo = f" {uf}" if uf else ""
        return ResultadoEmissao(
            sucesso=False,
            ambiente="portal",
            dados=dados,
            mensagens=[f"Falha no robô do portal{rotulo}: {erro}"],
        )

    resultado = ResultadoEmissao(
        sucesso=entrega.estado == "ok",
        ambiente="portal",
        dados=dados,
        avisos=list(entrega.avisos),
        pdf_path=entrega.pdf,
    )
    if entrega.estado == "ok":
        resultado.pdf_local = True
        resultado.mensagens.append(
            f"Guia emitida no portal do estado ({uf}) e conferida contra o CT-e."
        )
        return resultado
    if entrega.estado == "divergente":
        resultado.guia_divergente = True
        resultado.divergencias = [entrega.mensagem]
        resultado.pdf_path = None
        return resultado
    if entrega.estado == "captcha":
        resultado.pdf_path = None
        resultado.mensagens = [
            f"O portal {uf} pediu captcha. Use 'Portal: mostrar navegador' "
            "e emita novamente.",
            entrega.mensagem,
        ]
        return resultado
    resultado.pdf_path = None
    resultado.mensagens = [entrega.mensagem or "Falha ao emitir pelo portal."]
    return resultado
