"""Conferência do PDF emitido contra o XML (bloqueio de guia divergente)."""

from __future__ import annotations

import io
import re
from datetime import date

from .models import Guia, somente_digitos


def _extrair_texto(pdf: bytes) -> str:
    try:
        import pymupdf

        with pymupdf.open(stream=pdf, filetype="pdf") as documento:
            return "\n".join(pagina.get_text() for pagina in documento)
    except Exception:
        pass
    try:
        from pypdf import PdfReader

        leitor = PdfReader(io.BytesIO(pdf))
        return "\n".join(pagina.extract_text() or "" for pagina in leitor.pages)
    except Exception:
        return ""


def _sem_acento(texto: str) -> str:
    import unicodedata

    limpo = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in limpo if not unicodedata.combining(c))


def conferir_pdf(pdf: bytes, guia: Guia) -> tuple[list[str], list[str]]:
    """Retorna (falhas, avisos). Falha = guia não deve ser usada para pagamento."""
    if not pdf.startswith(b"%PDF"):
        return ["o portal não devolveu um PDF válido"], []

    texto = _extrair_texto(pdf)
    if not texto.strip():
        return ["não foi possível ler o texto do PDF para conferência"], []

    plano = _sem_acento(texto).upper()
    digitos = somente_digitos(plano)
    falhas: list[str] = []
    avisos: list[str] = []

    if guia.valor_br not in plano and guia.valor_ponto not in plano:
        falhas.append(f"valor R$ {guia.valor_br}")

    doc = somente_digitos(guia.emitente.documento)
    if doc:
        achado = doc in digitos
        if not achado and len(doc) == 14:
            achado = doc[:8] in digitos
        if not achado:
            falhas.append(f"CNPJ/CPF {guia.emitente.documento_fmt}")

    nota = somente_digitos(guia.numero_cte).lstrip("0")
    chave = somente_digitos(guia.chave_cte)
    if nota and nota not in digitos.lstrip("0") and chave not in digitos:
        falhas.append(f"CT-e/nº da nota {guia.numero_cte or guia.chave_cte}")

    if guia.vencimento:
        data_br = guia.vencimento.strftime("%d/%m/%Y")
        if data_br not in plano:
            avisos.append(f"vencimento {data_br} não aparece no PDF")

    nome = _sem_acento(guia.emitente.nome).upper()
    if nome:
        trecho = re.sub(r"\s+", " ", nome).strip()[:18]
        if trecho and trecho not in plano:
            avisos.append("razão social do contribuinte não localizada no PDF")

    if guia.destinatario and guia.destinatario.nome:
        trecho = re.sub(r"\s+", " ", _sem_acento(guia.destinatario.nome).upper())[:18]
        if trecho and trecho not in plano:
            avisos.append("nome do destinatário não aparece no PDF")

    if falta_pagamento(pdf):
        avisos.append("o PDF já traz marca de guia vencida/baixada")

    return falhas, avisos


def falta_pagamento(pdf: bytes) -> bool:
    return b"/NAO_PAGUE" in pdf.upper()


def carimbar_nao_pague(pdf: bytes) -> bytes:
    """Marca a guia divergente para que ela nunca seja paga por engano."""
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject, TextStringObject
    except ImportError:  # pragma: no cover
        return pdf
    try:
        leitor = PdfReader(io.BytesIO(pdf))
        escritor = PdfWriter()
        for pagina in leitor.pages:
            escritor.add_page(pagina)
        metadados = dict(leitor.metadata or {})
        metadados["/Title"] = "NAO PAGUE - GUIA DIVERGENTE"
        metadados["/Subject"] = "Guia bloqueada pelo BypassGuia: campos divergem do XML"
        escritor.add_metadata(metadados)
        saida = io.BytesIO()
        escritor.write(saida)
        return saida.getvalue()
    except Exception:
        return pdf


def data_recente(guia: Guia) -> bool:
    return bool(guia.emissao) and (date.today() - guia.emissao).days <= 90
