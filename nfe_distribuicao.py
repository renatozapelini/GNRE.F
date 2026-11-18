"""Baixa o XML da NF-e autorizada a partir da chave (NFeDistribuicaoDFe/AN).

O certificado A1 da empresa precisa ser emitente ou destinatário da NF-e;
a consulta é por chave e devolve o procNFe completo (com protocolo).
"""

from __future__ import annotations

import base64
import gzip
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from certificado import Certificado, preparar_pem_temporario
from leitor_nfe import ErroNFe, validar_chave

URL_AN = (
    "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx"
)
NS_NFE = "http://www.portalfiscal.inf.br/nfe"
NS_WSDL = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe"

CODIGO_UF = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17,
    "MA": 21, "PI": 22, "CE": 23, "RN": 24, "PB": 25, "PE": 26, "AL": 27,
    "SE": 28, "BA": 29, "MG": 31, "ES": 32, "RJ": 33, "SP": 35, "PR": 41,
    "RS": 42, "SC": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53,
}


class ErroDistribuicao(RuntimeError):
    pass


def somente_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto or "")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _filho(node: ET.Element | None, nome: str) -> ET.Element | None:
    if node is None:
        return None
    for item in node.iter():
        if _local(item.tag) == nome:
            return item
    return None


def _texto(node: ET.Element | None, nome: str) -> str:
    achado = _filho(node, nome)
    return (achado.text or "").strip() if achado is not None else ""


def codigo_uf_da_chave(chave: str) -> int:
    return int(chave[:2])


def _envelope(chave: str, cnpj: str, uf_codigo: int) -> bytes:
    documento = (
        f"<CNPJ>{cnpj}</CNPJ>" if len(cnpj) == 14 else f"<CPF>{cnpj}</CPF>"
    )
    corpo = (
        f'<nfeDadosMsg xmlns="{NS_WSDL}">'
        f'<distDFeInt xmlns="{NS_NFE}" versao="1.01">'
        "<tpAmb>1</tpAmb>"
        f"<cUFAutor>{uf_codigo}</cUFAutor>"
        f"{documento}"
        f"<consChNFe><chNFe>{chave}</chNFe></consChNFe>"
        "</distDFeInt>"
        "</nfeDadosMsg>"
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">'
        f"<soap12:Body><nfeDistDFeInteresse xmlns=\"{NS_WSDL}\">"
        f"{corpo}"
        "</nfeDistDFeInteresse></soap12:Body></soap12:Envelope>"
    ).encode("utf-8")


def _descompactar(doc_zip: ET.Element) -> str:
    bruto = base64.b64decode((doc_zip.text or "").strip())
    try:
        return gzip.decompress(bruto).decode("utf-8")
    except OSError:
        return bruto.decode("utf-8")


_PASTAS_IGNORADAS = {
    "build",
    "dist",
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "ms-playwright",
    "site-packages",
    "_mei",
}
_LIMITE_ARQUIVOS = 6000
_LIMITE_BYTES = 5_000_000
_PROFUNDIDADE = 4


def _raizes_busca(extras: tuple[str | Path, ...] = ()) -> list[Path]:
    raizes: list[Path] = []

    def adicionar(caminho: str | Path) -> None:
        try:
            resolvido = Path(caminho).resolve()
        except OSError:
            return
        if not resolvido.is_dir() or len(resolvido.parts) < 2:
            return
        texto = str(resolvido).lower()
        if texto.startswith("c:\\windows") or "\\program files" in texto:
            return
        for existente in raizes:
            if resolvido == existente or existente in resolvido.parents:
                return
        raizes[:] = [r for r in raizes if resolvido not in r.parents]
        raizes.append(resolvido)

    adicionar(Path.cwd())
    if getattr(sys, "frozen", False):
        adicionar(Path(sys.executable).parent)
    adicionar(Path(__file__).resolve().parent)
    perfil = Path.home()
    for nome in ("Desktop", "Documents", "Downloads"):
        adicionar(perfil / nome)
    for extra in extras:
        adicionar(extra)
    return raizes


def _xml_da_chave(caminho: Path, chave: bytes) -> bool:
    try:
        if caminho.stat().st_size > _LIMITE_BYTES:
            return False
        conteudo = caminho.read_bytes()
    except OSError:
        return False
    return chave in conteudo and b"<infNFe" in conteudo


def procurar_xml_local(
    chave: str, extras: tuple[str | Path, ...] = ()
) -> Path | None:
    """Acha o XML da NF-e já salvo no computador (NF emitida pela empresa)."""
    chave = somente_digitos(chave)
    numero = chave[25:34].lstrip("0") or chave[25:34]
    alvo = chave.encode()
    candidatos: list[Path] = []
    for raiz in _raizes_busca(extras):
        for pasta, subpastas, arquivos in os.walk(raiz):
            profundidade = len(Path(pasta).relative_to(raiz).parts)
            subpastas[:] = [
                nome
                for nome in subpastas
                if nome.lower() not in _PASTAS_IGNORADAS
                and profundidade < _PROFUNDIDADE
            ]
            for nome in arquivos:
                if not nome.lower().endswith(".xml"):
                    continue
                candidatos.append(Path(pasta) / nome)
                if len(candidatos) >= _LIMITE_ARQUIVOS:
                    break
            if len(candidatos) >= _LIMITE_ARQUIVOS:
                break
        if len(candidatos) >= _LIMITE_ARQUIVOS:
            break

    por_nome = [
        caminho
        for caminho in candidatos
        if chave in caminho.name or numero in caminho.name
    ]
    for caminho in por_nome + candidatos:
        if _xml_da_chave(caminho, alvo):
            return caminho
    return None



def baixar_xml_por_chave(
    chave: str,
    cert: Certificado,
    destino_dir: str | Path,
    *,
    timeout: int = 60,
    usar_cache: bool = True,
    log=None,
) -> Path:
    """Devolve o caminho do XML da NF-e, baixando da SEFAZ quando necessário."""
    chave = somente_digitos(chave)
    try:
        validar_chave(chave)
    except ErroNFe as erro:
        raise ErroDistribuicao(str(erro)) from erro

    destino_dir = Path(destino_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / f"{chave}.xml"
    if usar_cache and destino.exists() and destino.stat().st_size > 0:
        if log:
            log(f"NF-e {chave}: XML já está na pasta local.")
        return destino

    if chave[6:20] == somente_digitos(cert.cnpj):
        # NF emitida pela própria empresa: a SEFAZ não entrega (cStat 641),
        # mas o XML normalmente está no computador (sistema de faturamento).
        local = procurar_xml_local(chave)
        if local is not None:
            try:
                destino.write_bytes(local.read_bytes())
            except OSError as erro:
                raise ErroDistribuicao(
                    f"Não foi possível copiar o XML de {local}: {erro}."
                ) from erro
            if log:
                log(f"NF-e {chave}: emitida pela empresa; XML encontrado em {local}.")
            return destino

    if log:
        log(f"NF-e {chave}: consultando a SEFAZ (Distribuição DF-e)...")
    pem = preparar_pem_temporario(cert)
    try:
        resposta = requests.post(
            URL_AN,
            data=_envelope(chave, cert.cnpj, codigo_uf_da_chave(chave)),
            headers={
                "Content-Type": (
                    "application/soap+xml; charset=utf-8; "
                    f'action="{NS_WSDL}/nfeDistDFeInteresse"'
                ),
                "SOAPAction": f'"{NS_WSDL}/nfeDistDFeInteresse"',
                "User-Agent": "EmissorGNRE-Faturamento/1.0",
            },
            cert=(str(pem.cert_path), str(pem.key_path)),
            timeout=timeout,
        )
    except requests.Timeout as erro:
        raise ErroDistribuicao(
            "A SEFAZ não respondeu à consulta da chave em "
            f"{timeout}s. Tente novamente em instantes."
        ) from erro
    except requests.RequestException as erro:
        raise ErroDistribuicao(
            f"Falha de comunicação com a SEFAZ: {erro}"
        ) from erro
    finally:
        pem.limpar()

    if resposta.status_code != 200:
        raise ErroDistribuicao(
            f"A SEFAZ retornou HTTP {resposta.status_code}: "
            f"{resposta.text[:300] or 'sem corpo'}"
        )

    try:
        raiz = ET.fromstring(resposta.content)
    except ET.ParseError as erro:
        raise ErroDistribuicao(
            f"Resposta inválida da SEFAZ: {erro}"
        ) from erro

    retorno = _filho(raiz, "retDistDFeInt")
    if retorno is None:
        detalhe = _texto(raiz, "faultstring") or _texto(raiz, "mensagem")
        raise ErroDistribuicao(
            "A SEFAZ não devolveu o resultado da consulta"
            + (f": {detalhe}" if detalhe else ".")
        )

    cstat = _texto(retorno, "cStat")
    motivo = _texto(retorno, "xMotivo")
    if cstat == "138":
        for doc in retorno.iter():
            if _local(doc.tag) != "docZip":
                continue
            schema = (doc.get("schema") or "").lower()
            if schema.startswith("resnfe"):
                raise ErroDistribuicao(
                    "A SEFAZ devolveu apenas o resumo da NF-e (resNFe), sem os "
                    "valores do ICMS-ST. Baixe o XML completo no portal da "
                    "SEFAZ e use a aba 'XML'."
                )
            conteudo = _descompactar(doc)
            if "<infNFe" not in conteudo:
                continue
            destino.write_text(conteudo, encoding="utf-8")
            if log:
                log(f"NF-e {chave}: XML baixado da SEFAZ ({destino.name}).")
            return destino
        raise ErroDistribuicao(
            "A SEFAZ localizou a NF-e, mas não devolveu o XML completo "
            f"(cStat {cstat}: {motivo})."
        )
    if cstat == "137":
        raise ErroDistribuicao(
            "A SEFAZ não localizou esta chave para o CNPJ do certificado. "
            "Ela precisa ser emitida pela empresa ou destinada a ela."
        )
    if cstat == "656":
        raise ErroDistribuicao(
            "A SEFAZ bloqueou temporariamente novas consultas por consumo "
            "indevido. Aguarde cerca de 1 hora ou use a aba 'XML'."
        )
    if cstat == "641":
        raise ErroDistribuicao(
            "Esta NF-e foi emitida pela própria empresa. A SEFAZ não entrega o "
            "XML ao emitente pelo Distribuição DF-e e o XML não foi encontrado "
            "no computador. Use o XML do sistema de faturamento na aba 'XML'."
        )
    raise ErroDistribuicao(
        f"A SEFAZ recusou a consulta (cStat {cstat}: {motivo or 'sem motivo'})."
    )
