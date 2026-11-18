"""Leitura do XML do GNRE: CT-e autorizado (modelo 57) ou lote TLote_GNRE."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from .models import Guia, Participante, somente_digitos
from .paths import MUNICIPIOS_PATH

UF_IBGE = {
    "AC": "12", "AL": "27", "AP": "16", "AM": "13", "BA": "29", "CE": "23",
    "DF": "53", "ES": "32", "GO": "52", "MA": "21", "MT": "51", "MS": "50",
    "MG": "31", "PA": "15", "PB": "25", "PR": "41", "PE": "26", "PI": "22",
    "RJ": "33", "RN": "24", "RS": "43", "RO": "11", "RR": "14", "SC": "42",
    "SP": "35", "SE": "28", "TO": "17",
}


class ErroXML(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node: ET.Element | None, nome: str) -> ET.Element | None:
    if node is None:
        return None
    for filho in node.iter():
        if _local(filho.tag) == nome:
            return filho
    return None


def _findall(node: ET.Element | None, nome: str) -> list[ET.Element]:
    if node is None:
        return []
    return [filho for filho in node.iter() if _local(filho.tag) == nome]


def _texto(node: ET.Element | None, nome: str) -> str:
    achado = _find(node, nome)
    return (achado.text or "").strip() if achado is not None else ""


def _decimal(texto: str | None) -> Decimal | None:
    if not texto:
        return None
    try:
        return Decimal(str(texto).replace(".", "").replace(",", ".")) if "," in str(texto) else Decimal(str(texto))
    except InvalidOperation:
        return None


def _data(texto: str | None) -> date | None:
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError:
        return None


def _telefone(*valores: str) -> str:
    for valor in valores:
        d = somente_digitos(valor)
        if d:
            return d.lstrip("0") or d
    return ""


@lru_cache(maxsize=1)
def _municipios() -> dict[str, str]:
    try:
        dados = json.loads(MUNICIPIOS_PATH.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in dados.items()}
    except (OSError, ValueError):
        return {}


def nome_municipio(codigo: str, uf: str) -> str:
    d = somente_digitos(codigo)
    if not d:
        return ""
    tabela = _municipios()
    if d in tabela:
        return tabela[d]
    if len(d) < 7 and uf in UF_IBGE:
        completo = UF_IBGE[uf] + d.zfill(5)
        if completo in tabela:
            return tabela[completo]
    for chave, nome in tabela.items():
        if chave.endswith(d):
            return nome
    return ""


def _normalizar_endereco(no: ET.Element | None) -> dict[str, str]:
    if no is None:
        return {}
    return {
        "endereco": _texto(no, "xLgr"),
        "numero": _texto(no, "nro"),
        "complemento": _texto(no, "xCpl"),
        "bairro": _texto(no, "xBairro"),
        "municipio_codigo": somente_digitos(_texto(no, "cMun")),
        "municipio_nome": _texto(no, "xMun"),
        "uf": _texto(no, "UF").upper(),
        "cep": somente_digitos(_texto(no, "CEP")),
    }


def _participante_cte(inf: ET.Element, tag: str, papel: str) -> Participante | None:
    no = None
    for filho in inf:
        if _local(filho.tag) == tag:
            no = filho
            break
    if no is None:
        return None
    cnpj = somente_digitos(_texto(no, "CNPJ"))
    cpf = somente_digitos(_texto(no, "CPF"))
    if not cnpj and not cpf:
        return None
    end_no = None
    for filho in no:
        if _local(filho.tag).startswith("ender"):
            end_no = filho
            break
    end = _normalizar_endereco(end_no)
    return Participante(
        papel=papel,
        cnpj=cnpj,
        cpf=cpf,
        ie=_texto(no, "IE"),
        nome=_texto(no, "xNome"),
        telefone=_telefone(_texto(no, "fone")),
        **end,
    )


def _icms_cte(inf: ET.Element) -> tuple[str, Decimal | None]:
    icms = _find(_find(inf, "imp"), "ICMS")
    if icms is None:
        return "", None
    grupos = {"ICMS00", "ICMS20", "ICMS45", "ICMS60", "ICMS90", "ICMSOutraUF"}
    for filho in icms:
        if _local(filho.tag) not in grupos:
            continue
        grupo = _local(filho.tag)
        valor = _decimal(_texto(filho, "vICMSOutraUF"))
        if valor is None:
            valor = _decimal(_texto(filho, "vICMSSTRet"))
        if valor is None:
            valor = _decimal(_texto(filho, "vICMS"))
        return grupo, valor
    return "", None


def _vencimento_padrao(uf: str) -> date:
    try:
        import holidays

        feriados = holidays.Brazil(years=date.today().year, subdiv=uf, language="pt_BR")
    except Exception:
        feriados = {}
    dia = date.today()
    while dia.weekday() >= 5 or dia in feriados:
        dia += timedelta(days=1)
    return dia


def _ler_cte(root: ET.Element, caminho: Path) -> Guia:
    inf = _find(root, "infCte")
    if inf is None:
        raise ErroXML("XML não contém infCte; não parece ser um CT-e (modelo 57).")

    ide = _find(inf, "ide")
    if ide is None:
        raise ErroXML("CT-e sem o grupo ide.")
    chave = somente_digitos((inf.get("Id") or "").replace("CTe", "")) or somente_digitos(_texto(root, "chCTe"))
    if len(chave) != 44:
        raise ErroXML("Chave do CT-e inválida (esperados 44 dígitos).")

    emitente = _participante_cte(inf, "emit", "emit")
    if emitente is None:
        raise ErroXML("CT-e sem emitente (emit).")
    destinatario = _participante_cte(inf, "dest", "dest")
    remetente = _participante_cte(inf, "rem", "rem")

    grupo, valor = _icms_cte(inf)
    if valor is None or valor <= 0:
        raise ErroXML(f"CT-e sem valor de ICMS válido (grupo {grupo or 'não identificado'}).")

    uf_ini = _texto(ide, "UFIni").upper()
    uf_fim = _texto(ide, "UFFim").upper()
    cMun_ini = somente_digitos(_texto(ide, "cMunIni"))
    xMun_ini = _texto(ide, "xMunIni")
    cMun_fim = somente_digitos(_texto(ide, "cMunFim"))
    xMun_fim = _texto(ide, "xMunFim")

    dh_emi = _data(_texto(ide, "dhEmi"))
    hoje = date.today()
    vencimento = _vencimento_padrao(uf_ini)

    if grupo != "ICMSOutraUF":
        raise ErroXML(
            f"CT-e com {grupo or 'ICMS não identificado'}. O BypassGuia trata ICMS Transporte "
            "devido a outra UF (ICMSOutraUF); use a GNRE para os demais casos."
        )

    avisos: list[str] = []
    if not emitente.telefone:
        avisos.append("CT-e sem telefone do emitente; o portal pode exigir o campo.")
    if emitente.uf != uf_ini:
        avisos.append(
            f"Emitente em {emitente.uf} e início da prestação em {uf_ini}; "
            "os portais usarão o município de início do CT-e."
        )

    return Guia(
        arquivo=caminho,
        uf=uf_ini,
        emitente=emitente,
        valor=valor,
        destinatario=destinatario,
        remetente=remetente,
        receita="100030",
        chave_cte=chave,
        numero_cte=_texto(ide, "nCT"),
        serie_cte=_texto(ide, "serie"),
        uf_ini=uf_ini,
        municipio_ini_codigo=cMun_ini,
        municipio_ini_nome=xMun_ini or nome_municipio(cMun_ini, uf_ini),
        uf_fim=uf_fim,
        municipio_fim_codigo=cMun_fim,
        municipio_fim_nome=xMun_fim or nome_municipio(cMun_fim, uf_fim),
        referencia_mes=(dh_emi or hoje).month,
        referencia_ano=(dh_emi or hoje).year,
        vencimento=vencimento,
        emissao=dh_emi,
        observacao=f"CT-e {chave}",
        origem="cte",
        avisos=avisos,
    )


def _ler_lote(root: ET.Element, caminho: Path) -> Guia:
    dados = _find(root, "TDadosGNRE")
    if dados is None:
        raise ErroXML("XML de lote sem TDadosGNRE.")

    uf = _texto(dados, "ufFavorecida").upper()
    if not uf:
        raise ErroXML("Lote sem ufFavorecida.")

    emit_no = _find(dados, "contribuinteEmitente")
    if emit_no is None:
        raise ErroXML("Lote sem contribuinteEmitente.")
    cnpj = somente_digitos(_texto(emit_no, "CNPJ"))
    cpf = somente_digitos(_texto(emit_no, "CPF"))
    if not cnpj and not cpf:
        raise ErroXML("Lote sem CNPJ/CPF do contribuinte emitente.")
    municipio_codigo = somente_digitos(_texto(emit_no, "municipio"))
    emitente = Participante(
        papel="emit",
        cnpj=cnpj,
        cpf=cpf,
        nome=_texto(emit_no, "razaoSocial"),
        endereco=_texto(emit_no, "endereco"),
        municipio_codigo=municipio_codigo,
        municipio_nome=nome_municipio(municipio_codigo, _texto(emit_no, "uf").upper()),
        uf=_texto(emit_no, "uf").upper(),
        cep=somente_digitos(_texto(emit_no, "cep")),
    )

    item = _find(dados, "item")
    if item is None:
        raise ErroXML("Lote sem item de guia.")

    valor = _decimal(_texto(item, "valor"))
    if valor is None or valor <= 0:
        raise ErroXML("Lote sem valor de guia válido.")

    chave = ""
    for extra in _findall(item, "campoExtra"):
        codigo = _texto(extra, "codigo")
        conteudo = somente_digitos(_texto(extra, "valor"))
        if codigo in {"89", "91", "95", "99", "100", "120", "137"} and len(conteudo) == 44:
            chave = conteudo
            break
        if len(conteudo) == 44 and not chave:
            chave = conteudo

    ref = _find(item, "referencia")
    mes = int(_texto(ref, "mes") or 0) if ref is not None else 0
    ano = int(_texto(ref, "ano") or 0) if ref is not None else 0
    emissao = None
    if not mes or not ano:
        emissao = date.today()
        mes, ano = emissao.month, emissao.year

    vencimento = _data(_texto(item, "dataVencimento")) or _vencimento_padrao(uf)
    numero_cte = _texto(item, "documentoOrigem")
    inicio_nome = nome_municipio(municipio_codigo, emitente.uf)
    avisos = [
        "XML de lote: o município de início não vem explicitado; foi usado o município do emitente. "
        "Para ES, prefira o XML do CT-e."
    ]

    return Guia(
        arquivo=caminho,
        uf=uf,
        emitente=emitente,
        valor=valor,
        receita=_texto(item, "receita") or "100030",
        chave_cte=chave,
        numero_cte=numero_cte,
        uf_ini=uf,
        municipio_ini_codigo=municipio_codigo,
        municipio_ini_nome=inicio_nome,
        referencia_mes=mes,
        referencia_ano=ano,
        vencimento=vencimento,
        emissao=emissao,
        observacao=f"CT-e {chave}" if chave else f"CT-e {numero_cte}",
        origem="lote",
        avisos=avisos,
    )


def _ler_generico(root: ET.Element, caminho: Path) -> Guia:
    """Aceita um XML achatado com os mesmos nomes, sem namespace."""
    folhas: dict[str, str] = {}
    for no in root.iter():
        nome = _local(no.tag)
        if len(no) == 0 and (no.text or "").strip():
            folhas.setdefault(nome, (no.text or "").strip())

    def pegar(*nomes: str) -> str:
        for nome in nomes:
            if folhas.get(nome):
                return folhas[nome]
        return ""

    cnpj = somente_digitos(pegar("CNPJ", "cnpj", "Cnpj", "documento"))
    if not cnpj:
        raise ErroXML("XML não reconhecido: não foi encontrado CNPJ do emitente.")
    uf = pegar("ufFavorecida", "UF", "uf", "estado").upper()
    if not uf:
        raise ErroXML("XML não reconhecido: não foi encontrada a UF favorecida.")
    valor = _decimal(pegar("valor", "valorGNRE", "vICMSOutraUF"))
    if valor is None:
        raise ErroXML("XML não reconhecido: não foi encontrado o valor da guia.")
    venc = _data(pegar("dataVencimento", "vencimento")) or _vencimento_padrao(uf)
    chave = somente_digitos(pegar("chave", "chaveCte", "chCTe"))
    numero = pegar("documentoOrigem", "numeroCte", "nCT")
    mes = int(pegar("mes") or venc.month)
    ano = int(pegar("ano") or venc.year)
    municipio = somente_digitos(pegar("municipio", "cMunIni"))
    emitente = Participante(
        papel="emit",
        cnpj=cnpj,
        nome=pegar("razaoSocial", "nome", "xNome"),
        endereco=pegar("endereco", "xLgr"),
        municipio_codigo=municipio,
        municipio_nome=nome_municipio(municipio, uf),
        uf=pegar("ufEmitente", "UFIni") or uf,
        cep=somente_digitos(pegar("cep", "CEP")),
        telefone=somente_digitos(pegar("telefone", "fone")),
    )
    return Guia(
        arquivo=caminho,
        uf=uf,
        emitente=emitente,
        valor=valor,
        receita=pegar("receita", "codigoReceita") or "100030",
        chave_cte=chave,
        numero_cte=numero,
        uf_ini=uf,
        municipio_ini_codigo=municipio,
        municipio_ini_nome=emitente.municipio_nome,
        referencia_mes=mes,
        referencia_ano=ano,
        vencimento=venc,
        observacao=f"CT-e {chave}" if chave else f"CT-e {numero}",
        origem="generico",
        avisos=["XML genérico: confira os campos na prévia antes de gerar."],
    )


def ler_xml(caminho: str | Path) -> Guia:
    caminho = Path(caminho)
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")
    try:
        root = ET.parse(caminho).getroot()
    except ET.ParseError as erro:
        raise ErroXML(f"XML inválido: {erro}") from erro

    nomes = {_local(no.tag) for no in root.iter()}
    if "infCte" in nomes:
        return _ler_cte(root, caminho)
    if "TDadosGNRE" in nomes:
        return _ler_lote(root, caminho)
    return _ler_generico(root, caminho)
