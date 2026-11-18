"""Leitura e validação do CT-e (modelo 57) para emissão de GNRE."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from calendario import vencimento_padrao


class ErroCTe(ValueError):
    pass


@dataclass
class Participante:
    papel: str
    cnpj: str = ""
    cpf: str = ""
    ie: str = ""
    nome: str = ""
    endereco: str = ""
    municipio: str = ""
    municipio_nome: str = ""
    uf: str = ""
    cep: str = ""
    telefone: str = ""

    @property
    def documento(self) -> str:
        return self.cnpj or self.cpf


@dataclass
class DadosCTe:
    caminho: str
    chave: str
    numero: str
    serie: str
    cfop: str
    natureza: str
    dh_emi: datetime
    tp_amb: int
    uf_ini: str
    uf_fim: str
    municipio_ini: str
    municipio_fim: str
    grupo_icms: str
    valor_icms: Decimal
    base_icms: Decimal | None
    uf_favorecida: str | None
    motivo_favorecida: str
    participantes: list[Participante]
    data_vencimento: date
    data_pagamento: date
    status_cte: str | None
    motivo_status: str | None
    protocolo: str | None
    avisos: list[str] = field(default_factory=list)

    @property
    def emitente(self) -> Participante | None:
        return next((p for p in self.participantes if p.papel == "emit"), None)

    def participante_por_cnpj(self, cnpj: str) -> Participante | None:
        cnpj = somente_digitos(cnpj)
        achados = [p for p in self.participantes if p.cnpj == cnpj]
        if not achados:
            return None
        ordem = {"dest": 0, "rem": 1, "emit": 2, "exped": 3, "receb": 4, "toma": 5}
        return sorted(achados, key=lambda p: ordem.get(p.papel, 9))[0]

    def resumo(self) -> str:
        valor = f"{self.valor_icms:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        venc = self.data_vencimento.strftime("%d/%m/%Y")
        favorecida = self.uf_favorecida or "NÃO DEFINIDA"
        return (
            f"Chave: {self.chave}\n"
            f"CT-e: {self.numero} (série {self.serie})  CFOP: {self.cfop}\n"
            f"Trajeto: {self.uf_ini} -> {self.uf_fim}\n"
            f"UF favorecida: {favorecida}\n"
            f"Grupo ICMS: {self.grupo_icms}  Valor: R$ {valor}\n"
            f"Vencimento sugerido: {venc}  Pagamento: {self.data_pagamento:%d/%m/%Y}\n"
            f"Regra da UF: {self.motivo_favorecida}"
        )


def somente_digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


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


def _decimal(texto: str) -> Decimal | None:
    if not texto:
        return None
    try:
        return Decimal(texto.replace(",", "."))
    except InvalidOperation:
        return None


def _data(texto: str) -> date | None:
    if not texto:
        return None
    try:
        return date.fromisoformat(texto[:10])
    except ValueError:
        return None


def validar_chave(chave: str) -> None:
    if not re.fullmatch(r"\d{44}", chave or ""):
        raise ErroCTe(f"Chave de acesso inválida (esperados 44 dígitos): {chave!r}")
    soma = 0
    peso = 2
    for digito in reversed(chave[:43]):
        soma += int(digito) * peso
        peso = 2 if peso == 9 else peso + 1
    resto = soma % 11
    dv = 0 if resto < 2 else 11 - resto
    if dv != int(chave[-1]):
        raise ErroCTe(f"Dígito verificador da chave não confere (calculado {dv}, informado {chave[-1]}).")


def _endereco(participante: ET.Element) -> tuple[str, str, str, str, str, str]:
    ender = None
    for filho in participante:
        if _local(filho.tag).startswith("ender"):
            ender = filho
            break
    if ender is None:
        return "", "", "", "", "", ""
    partes = [
        _texto(ender, "xLgr"),
        _texto(ender, "nro"),
        _texto(ender, "xCpl"),
        _texto(ender, "xBairro"),
    ]
    endereco = ", ".join(p for p in partes if p)
    municipio = somente_digitos(_texto(ender, "cMun"))
    municipio_nome = _texto(ender, "xMun")
    uf = _texto(ender, "UF")
    cep = somente_digitos(_texto(ender, "CEP"))
    return endereco, municipio, municipio_nome, uf, cep, ""


def _participante(node: ET.Element | None, papel: str) -> Participante | None:
    if node is None:
        return None
    cnpj = somente_digitos(_texto(node, "CNPJ"))
    cpf = somente_digitos(_texto(node, "CPF"))
    if not cnpj and not cpf:
        return None
    endereco, municipio, municipio_nome, uf, cep, _ = _endereco(node)
    telefone = somente_digitos(_texto(node, "fone")).lstrip("0")
    return Participante(
        papel=papel,
        cnpj=cnpj,
        cpf=cpf,
        ie=_texto(node, "IE"),
        nome=_texto(node, "xNome"),
        endereco=endereco,
        municipio=municipio,
        municipio_nome=municipio_nome,
        uf=uf,
        cep=cep,
        telefone=telefone,
    )


def ler_cte_xml(caminho_arquivo: str | Path) -> DadosCTe:
    """Lê o CT-e autorizado e devolve os dados necessários para a GNRE.

    A UF favorecida não é fixa: para ICMSOutraUF ela é a UF de início da
    prestação (ide/UFIni), que muda de CT-e para CT-e.
    """
    caminho = Path(caminho_arquivo)
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")

    try:
        root = ET.parse(caminho).getroot()
    except ET.ParseError as erro:
        raise ErroCTe(f"XML inválido: {erro}") from erro

    inf = _find(root, "infCte")
    if inf is None:
        raise ErroCTe("XML não contém infCte. Envie o CT-e (modelo 57) autorizado.")

    avisos: list[str] = []
    chave = (inf.get("Id") or "").removeprefix("CTe")
    if not chave:
        chave = _texto(root, "chCTe")
    validar_chave(chave)

    prot = _find(root, "infProt")
    status_cte = _texto(prot, "cStat") or None
    motivo_status = _texto(prot, "xMotivo") or None
    protocolo = _texto(prot, "nProt") or None
    if status_cte and status_cte != "100":
        raise ErroCTe(
            f"CT-e não está autorizado (cStat={status_cte}: {motivo_status}). "
            "Não é possível emitir GNRE."
        )
    if status_cte is None:
        avisos.append("XML sem protocolo de autorização (protCTe/infProt); confirme se o CT-e está autorizado.")

    eventos = {_texto(ev, "tpEvento") for ev in _findall(root, "tpEvento")}
    if "110111" in eventos:
        raise ErroCTe("CT-e cancelado (evento 110111). Não é possível emitir GNRE.")

    ide = _find(inf, "ide")
    if ide is None:
        raise ErroCTe("CT-e sem grupo ide.")

    tp_amb = int(_texto(ide, "tpAmb") or "1")
    uf_ini = _texto(ide, "UFIni").upper()
    uf_fim = _texto(ide, "UFFim").upper()
    if not uf_ini or not uf_fim:
        raise ErroCTe("CT-e sem UFIni/UFFim.")

    dh_emi_txt = _texto(ide, "dhEmi")
    try:
        dh_emi = datetime.fromisoformat(dh_emi_txt)
    except ValueError as erro:
        raise ErroCTe(f"Data de emissão inválida: {dh_emi_txt!r}") from erro

    emitente = _participante(_find(inf, "emit"), "emit")
    remetente = _participante(_find(inf, "rem"), "rem")
    destinatario = _participante(_find(inf, "dest"), "dest")
    expedidor = _participante(_find(inf, "exped"), "exped")
    recebedor = _participante(_find(inf, "receb"), "receb")
    tomador = _participante(_find(inf, "toma4"), "toma")

    participantes = [p for p in (emitente, remetente, destinatario, expedidor, recebedor, tomador) if p]
    if emitente is None:
        raise ErroCTe("CT-e sem emitente (emit).")

    imp = _find(inf, "imp")
    icms = _find(imp, "ICMS")
    if icms is None:
        raise ErroCTe("CT-e sem grupo imp/ICMS.")

    grupos = {"ICMS00", "ICMS20", "ICMS45", "ICMS60", "ICMS90", "ICMSOutraUF"}
    grupo_icms = ""
    no_icms = None
    for filho in icms:
        if _local(filho.tag) in grupos:
            grupo_icms = _local(filho.tag)
            no_icms = filho
            break
    if no_icms is None:
        raise ErroCTe("Não foi identificado o grupo de ICMS do CT-e.")

    base_icms = _decimal(_texto(no_icms, "vBC")) or _decimal(_texto(no_icms, "vBCOutraUF"))
    if grupo_icms == "ICMSOutraUF":
        valor_icms = _decimal(_texto(no_icms, "vICMSOutraUF"))
        uf_favorecida = uf_ini
        motivo = (
            "ICMSOutraUF: ICMS devido à UF de início da prestação "
            f"(ide/UFIni={uf_ini})."
        )
        if emitente.uf and uf_ini == emitente.uf:
            avisos.append(
                "ICMSOutraUF com UFIni igual à UF do emitente; confira a regra fiscal."
            )
    elif grupo_icms == "ICMS60":
        valor_icms = _decimal(_texto(no_icms, "vICMSSTRet")) or _decimal(
            _texto(no_icms, "vICMS"))
        uf_favorecida = None
        motivo = "ICMS60 (substituição tributária): informe a UF favorecida manualmente."
    else:
        valor_icms = _decimal(_texto(no_icms, "vICMS"))
        uf_favorecida = None
        motivo = (
            f"{grupo_icms}: ICMS da própria UF do emitente; normalmente não gera GNRE. "
            "Informe a UF favorecida manualmente se necessário."
        )

    if valor_icms is None or valor_icms <= 0:
        raise ErroCTe(
            f"Não foi encontrado valor de ICMS válido para {grupo_icms}. "
            "Confira o grupo imp/ICMS do CT-e."
        )

    vencimento = vencimento_padrao(uf_favorecida)
    if vencimento != date.today():
        avisos.append(
            "Vencimento ajustado para o próximo dia útil da UF favorecida "
            f"({vencimento:%d/%m/%Y})."
        )

    return DadosCTe(
        caminho=str(caminho),
        chave=chave,
        numero=_texto(ide, "nCT"),
        serie=_texto(ide, "serie"),
        cfop=_texto(ide, "CFOP"),
        natureza=_texto(ide, "natOp"),
        dh_emi=dh_emi,
        tp_amb=tp_amb,
        uf_ini=uf_ini,
        uf_fim=uf_fim,
        municipio_ini=somente_digitos(_texto(ide, "cMunIni")),
        municipio_fim=somente_digitos(_texto(ide, "cMunFim")),
        grupo_icms=grupo_icms,
        valor_icms=valor_icms,
        base_icms=base_icms,
        uf_favorecida=uf_favorecida,
        motivo_favorecida=motivo,
        participantes=participantes,
        data_vencimento=vencimento,
        data_pagamento=date.today(),
        status_cte=status_cte,
        motivo_status=motivo_status,
        protocolo=protocolo,
        avisos=avisos,
    )
