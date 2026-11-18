"""Leitura e validação da NF-e (modelo 55) para emissão de GNRE de ICMS-ST."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from calendario import vencimento_padrao


class ErroNFe(ValueError):
    pass


@dataclass
class Participante:
    papel: str
    cnpj: str = ""
    cpf: str = ""
    ie: str = ""
    nome: str = ""
    endereco: str = ""
    logradouro: str = ""
    numero: str = ""
    complemento: str = ""
    bairro: str = ""
    municipio: str = ""
    municipio_nome: str = ""
    uf: str = ""
    cep: str = ""
    telefone: str = ""
    ind_ie: str = ""

    @property
    def documento(self) -> str:
        return self.cnpj or self.cpf

    @property
    def documento_fmt(self) -> str:
        digitos = somente_digitos(self.documento)
        if len(digitos) == 14:
            return (
                f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/"
                f"{digitos[8:12]}-{digitos[12:]}"
            )
        if len(digitos) == 11:
            return f"{digitos[:3]}.{digitos[3:6]}.{digitos[6:9]}-{digitos[9:]}"
        return self.documento


@dataclass
class DadosNFe:
    caminho: str
    chave: str
    numero: str
    serie: str
    modelo: str
    cfop: str
    natureza: str
    dh_emi: datetime
    tp_amb: int
    uf_emitente: str
    uf_destino: str
    municipio_emitente: str
    municipio_destino: str
    valor_produto: Decimal
    valor_icms: Decimal
    base_icms: Decimal | None
    base_st: Decimal | None
    valor_st: Decimal
    valor_fcp: Decimal
    valor_total_nf: Decimal
    participantes: list[Participante]
    data_vencimento: date
    data_pagamento: date
    status_nfe: str | None
    motivo_status: str | None
    protocolo: str | None
    avisos: list[str] = field(default_factory=list)

    @property
    def emitente(self) -> Participante | None:
        return next((p for p in self.participantes if p.papel == "emit"), None)

    @property
    def destinatario(self) -> Participante | None:
        return next((p for p in self.participantes if p.papel == "dest"), None)

    @property
    def uf_favorecida(self) -> str:
        """O ICMS-ST da transferência é devido à UF do destinatário."""
        return self.uf_destino

    @property
    def valor_st_fcp(self) -> Decimal:
        return self.valor_st + self.valor_fcp

    def participante_por_cnpj(self, cnpj: str) -> Participante | None:
        cnpj = somente_digitos(cnpj)
        achados = [p for p in self.participantes if p.cnpj == cnpj]
        if not achados:
            return None
        ordem = {"dest": 0, "emit": 1}
        return sorted(achados, key=lambda p: ordem.get(p.papel, 9))[0]

    def resumo(self) -> str:
        def brl(valor: Decimal) -> str:
            return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

        venc = self.data_vencimento.strftime("%d/%m/%Y")
        return (
            f"Chave: {self.chave}\n"
            f"NF-e: {self.numero} (série {self.serie})  CFOP: {self.cfop}\n"
            f"Operação: {self.uf_emitente} -> {self.uf_destino} ({self.natureza})\n"
            f"UF favorecida: {self.uf_favorecida}\n"
            f"ICMS-ST: R$ {brl(self.valor_st)}  FCP-ST: R$ {brl(self.valor_fcp)}\n"
            f"Total a recolher: R$ {brl(self.valor_st_fcp)}\n"
            f"Vencimento sugerido: {venc}  Pagamento: {self.data_pagamento:%d/%m/%Y}"
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


def validar_chave(chave: str) -> None:
    if not re.fullmatch(r"\d{44}", chave or ""):
        raise ErroNFe(f"Chave de acesso inválida (esperados 44 dígitos): {chave!r}")
    soma = 0
    peso = 2
    for digito in reversed(chave[:43]):
        soma += int(digito) * peso
        peso = 2 if peso == 9 else peso + 1
    resto = soma % 11
    dv = 0 if resto < 2 else 11 - resto
    if dv != int(chave[-1]):
        raise ErroNFe(
            f"Dígito verificador da chave não confere "
            f"(calculado {dv}, informado {chave[-1]})."
        )


def _endereco(participante: ET.Element) -> dict[str, str]:
    ender = None
    for filho in participante:
        if _local(filho.tag).startswith("ender"):
            ender = filho
            break
    if ender is None:
        return {}
    logradouro = _texto(ender, "xLgr")
    numero = _texto(ender, "nro")
    complemento = _texto(ender, "xCpl")
    bairro = _texto(ender, "xBairro")
    endereco = ", ".join(
        parte for parte in (logradouro, numero, complemento, bairro) if parte
    )
    return {
        "endereco": endereco,
        "logradouro": logradouro,
        "numero": numero,
        "complemento": complemento,
        "bairro": bairro,
        "municipio": somente_digitos(_texto(ender, "cMun")),
        "municipio_nome": _texto(ender, "xMun"),
        "uf": _texto(ender, "UF"),
        "cep": somente_digitos(_texto(ender, "CEP")),
    }


def _participante(node: ET.Element | None, papel: str) -> Participante | None:
    if node is None:
        return None
    cnpj = somente_digitos(_texto(node, "CNPJ"))
    cpf = somente_digitos(_texto(node, "CPF"))
    if not cnpj and not cpf:
        return None
    endereco = _endereco(node)
    telefone = somente_digitos(_texto(node, "fone")).lstrip("0")
    return Participante(
        papel=papel,
        cnpj=cnpj,
        cpf=cpf,
        ie=_texto(node, "IE"),
        nome=_texto(node, "xNome"),
        endereco=endereco.get("endereco", ""),
        logradouro=endereco.get("logradouro", ""),
        numero=endereco.get("numero", ""),
        complemento=endereco.get("complemento", ""),
        bairro=endereco.get("bairro", ""),
        municipio=endereco.get("municipio", ""),
        municipio_nome=endereco.get("municipio_nome", ""),
        uf=endereco.get("uf", ""),
        cep=endereco.get("cep", ""),
        telefone=telefone,
        ind_ie=_texto(node, "indIEDest") if papel == "dest" else "",
    )


def _grupo_icms(det: ET.Element) -> tuple[str, ET.Element | None]:
    imposto = None
    for filho in det:
        if _local(filho.tag) == "imposto":
            imposto = filho
            break
    if imposto is None:
        return "", None
    icms = None
    for filho in imposto:
        if _local(filho.tag) == "ICMS":
            icms = filho
            break
    if icms is None:
        return "", None
    for filho in icms:
        return _local(filho.tag), filho
    return "", None


def ler_nfe_xml(caminho_arquivo: str | Path) -> DadosNFe:
    """Lê a NF-e autorizada e devolve os valores de ICMS-ST e FCP-ST.

    A UF favorecida da GNRE é sempre a UF do destinatário: na transferência
    interestadual com CST 10 o remetente recolhe o ST para o estado de destino.
    """
    caminho = Path(caminho_arquivo)
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")

    try:
        root = ET.parse(caminho).getroot()
    except ET.ParseError as erro:
        raise ErroNFe(f"XML inválido: {erro}") from erro

    inf = _find(root, "infNFe")
    if inf is None:
        raise ErroNFe("XML não contém infNFe. Envie a NF-e (modelo 55) autorizada.")

    avisos: list[str] = []
    chave = (inf.get("Id") or "").removeprefix("NFe")
    if not chave:
        chave = _texto(root, "chNFe")
    validar_chave(chave)

    prot = _find(root, "infProt")
    status_nfe = _texto(prot, "cStat") or None
    motivo_status = _texto(prot, "xMotivo") or None
    protocolo = _texto(prot, "nProt") or None
    if status_nfe and status_nfe != "100":
        raise ErroNFe(
            f"NF-e não está autorizada (cStat={status_nfe}: {motivo_status}). "
            "Não é possível emitir GNRE."
        )
    if status_nfe is None:
        avisos.append(
            "XML sem protocolo de autorização (infProt); confirme se a NF-e "
            "está autorizada."
        )

    eventos = {_texto(ev, "tpEvento") for ev in _findall(root, "tpEvento")}
    if "110111" in eventos:
        raise ErroNFe("NF-e cancelada (evento 110111). Não é possível emitir GNRE.")
    for ocorrencia in _findall(root, "cStat"):
        if (ocorrencia.text or "").strip() in {"101", "135", "151", "155"}:
            if _texto(_find(root, "tpEvento"), "tpEvento") in {"", "110111"}:
                raise ErroNFe("NF-e com evento de cancelamento. Não é possível emitir GNRE.")

    ide = _find(inf, "ide")
    if ide is None:
        raise ErroNFe("NF-e sem grupo ide.")

    modelo = _texto(ide, "mod")
    if modelo and modelo != "55":
        raise ErroNFe(
            f"Este XML é do modelo {modelo}; a GNRE de faturamento usa a NF-e (55)."
        )

    tp_amb = int(_texto(ide, "tpAmb") or "1")
    if tp_amb == 2:
        avisos.append(
            "A NF-e foi emitida em ambiente de homologação; ela não representa "
            "uma operação real para pagamento."
        )

    dh_emi_txt = _texto(ide, "dhEmi") or _texto(ide, "dEmi")
    try:
        dh_emi = datetime.fromisoformat(dh_emi_txt)
    except ValueError as erro:
        raise ErroNFe(f"Data de emissão inválida: {dh_emi_txt!r}") from erro

    emitente = _participante(_find(inf, "emit"), "emit")
    destinatario = _participante(_find(inf, "dest"), "dest")
    if emitente is None:
        raise ErroNFe("NF-e sem emitente (emit).")
    if destinatario is None:
        raise ErroNFe("NF-e sem destinatário (dest).")
    if not emitente.uf or not destinatario.uf:
        raise ErroNFe("NF-e sem UF do emitente ou do destinatário.")

    if emitente.uf == destinatario.uf:
        avisos.append(
            f"Emitente e destinatário são da mesma UF ({emitente.uf}); "
            "a GNRE de ICMS-ST interestadual normalmente não se aplica."
        )

    grupos = {"ICMS00", "ICMS10", "ICMS20", "ICMS30", "ICMS40", "ICMS51",
              "ICMS60", "ICMS70", "ICMS90", "ICMSSN101", "ICMSSN102",
              "ICMSSN201", "ICMSSN202", "ICMSSN500", "ICMSSN900"}
    cfops: list[str] = []
    grupo_icms = ""
    for det in _findall(inf, "det"):
        prod = _find(det, "prod")
        cfop = _texto(prod, "CFOP")
        if cfop and cfop not in cfops:
            cfops.append(cfop)
        nome_grupo, no_grupo = _grupo_icms(det)
        if nome_grupo in grupos and not grupo_icms:
            grupo_icms = nome_grupo
    if not cfops:
        raise ErroNFe("NF-e sem CFOP nos itens.")
    cfop = cfops[0]
    if len(cfops) > 1:
        avisos.append(
            "A NF-e tem mais de um CFOP nos itens ("
            + ", ".join(cfops)
            + "); foi usado o primeiro."
        )
    if not cfop.startswith("6") and emitente.uf != destinatario.uf:
        avisos.append(
            f"CFOP {cfop} não é de operação interestadual; confira a operação "
            "antes de pagar."
        )

    total = _find(inf, "ICMSTot")
    if total is None:
        raise ErroNFe("NF-e sem grupo total/ICMSTot.")

    valor_produto = _decimal(_texto(total, "vProd")) or Decimal("0")
    valor_icms = _decimal(_texto(total, "vICMS")) or Decimal("0")
    base_icms = _decimal(_texto(total, "vBC"))
    base_st = _decimal(_texto(total, "vBCST"))
    valor_st = _decimal(_texto(total, "vST")) or Decimal("0")
    valor_fcp = _decimal(_texto(total, "vFCPST")) or Decimal("0")
    valor_total_nf = _decimal(_texto(total, "vNF")) or Decimal("0")

    if valor_st <= 0 and valor_fcp <= 0:
        raise ErroNFe(
            f"A NF-e não tem ICMS-ST nem FCP-ST a recolher "
            f"(vST={valor_st}, vFCPST={valor_fcp}). "
            "Confira se os itens usam CST 10/70 ou se a ST já foi retida."
        )

    if valor_st <= 0:
        avisos.append(
            f"Somente FCP-ST foi encontrado (R$ {valor_fcp:.2f}); "
            "confira a regra da UF antes de pagar."
        )

    vencimento = vencimento_padrao(destinatario.uf)
    if vencimento != date.today():
        avisos.append(
            "Vencimento ajustado para o próximo dia útil da UF favorecida "
            f"({vencimento:%d/%m/%Y})."
        )

    return DadosNFe(
        caminho=str(caminho),
        chave=chave,
        numero=_texto(ide, "nNF"),
        serie=_texto(ide, "serie"),
        modelo=modelo or "55",
        cfop=cfop,
        natureza=_texto(ide, "natOp"),
        dh_emi=dh_emi,
        tp_amb=tp_amb,
        uf_emitente=emitente.uf,
        uf_destino=destinatario.uf,
        municipio_emitente=emitente.municipio,
        municipio_destino=destinatario.municipio,
        valor_produto=valor_produto,
        valor_icms=valor_icms,
        base_icms=base_icms,
        base_st=base_st,
        valor_st=valor_st,
        valor_fcp=valor_fcp,
        valor_total_nf=valor_total_nf,
        participantes=[emitente, destinatario],
        data_vencimento=vencimento,
        data_pagamento=date.today(),
        status_nfe=status_nfe,
        motivo_status=motivo_status,
        protocolo=protocolo,
        avisos=avisos,
    )
