"""Modelos de dados do BypassGuia."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path


def somente_digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


def fmt_cnpj(cnpj: str) -> str:
    d = somente_digitos(cnpj)
    if len(d) != 14:
        return cnpj
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def fmt_valor(valor: Decimal) -> str:
    texto = f"{valor:,.2f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


@dataclass
class Participante:
    papel: str = ""
    cnpj: str = ""
    cpf: str = ""
    ie: str = ""
    nome: str = ""
    endereco: str = ""
    numero: str = ""
    complemento: str = ""
    bairro: str = ""
    municipio_codigo: str = ""
    municipio_nome: str = ""
    uf: str = ""
    cep: str = ""
    telefone: str = ""

    @property
    def documento(self) -> str:
        return self.cnpj or self.cpf

    @property
    def documento_fmt(self) -> str:
        return fmt_cnpj(self.cnpj) if self.cnpj else self.cpf

    @property
    def endereco_completo(self) -> str:
        partes = [self.endereco, self.numero, self.complemento, self.bairro]
        return ", ".join(p for p in partes if p)


@dataclass
class Guia:
    arquivo: Path
    uf: str
    emitente: Participante
    valor: Decimal
    destinatario: Participante | None = None
    remetente: Participante | None = None
    receita: str = "100030"
    chave_cte: str = ""
    numero_cte: str = ""
    serie_cte: str = ""
    uf_ini: str = ""
    municipio_ini_codigo: str = ""
    municipio_ini_nome: str = ""
    uf_fim: str = ""
    municipio_fim_codigo: str = ""
    municipio_fim_nome: str = ""
    referencia_mes: int = 0
    referencia_ano: int = 0
    vencimento: date | None = None
    emissao: date | None = None
    observacao: str = ""
    origem: str = "cte"
    avisos: list[str] = field(default_factory=list)

    @property
    def referencia_mmaaaa(self) -> str:
        return f"{self.referencia_mes:02d}/{self.referencia_ano:04d}"

    @property
    def referencia_aaaa_mm(self) -> str:
        return f"{self.referencia_ano:04d}-{self.referencia_mes:02d}"

    @property
    def referencia_mes_ano(self) -> str:
        return f"{self.referencia_mes:02d}/{self.referencia_ano:04d}"

    @property
    def valor_br(self) -> str:
        return fmt_valor(self.valor)

    @property
    def valor_ponto(self) -> str:
        return f"{self.valor:.2f}"

    @property
    def chave_ou_numero(self) -> str:
        return self.chave_cte or self.numero_cte

    def nome_pdf(self) -> str:
        num = self.numero_cte or ""
        if not num or len(somente_digitos(num)) > 12:
            digitos = somente_digitos(self.chave_cte)
            num = digitos[25:34].lstrip("0") if len(digitos) == 44 else digitos[-6:]
        num = num or "GUIA"
        return f"Guia - {self.uf} - CTE {num}.pdf"

    def resumo(self) -> str:
        return (
            f"UF {self.uf} | CT-e {self.numero_cte} | "
            f"{self.emitente.nome[:34]} | R$ {self.valor_br}"
        )
