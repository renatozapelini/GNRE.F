"""Reproduz o PDF da guia GNRE no layout oficial do portal.

O portal monta a guia em JasperReports (A4 595x842 pt, duas vias por página).
As coordenadas abaixo foram medidas no PDF oficial e são usadas para que a
guia gerada localmente seja idêntica à entregue pelo portal: mesma grade,
mesmas fontes, código de barras ITF e QR Code PIX.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

LARGURA_PAGINA = 595.0
ALTURA_PAGINA = 842.0
DESLOCAMENTO_VIA = 262.0
ASSETS = Path(__file__).resolve().parent / "assets"
LOGO_PIX = ASSETS / "pix_logo.jpg"

NORMAL = "Helvetica"
NEGRITO = "Helvetica-Bold"

# Padrão ITF (Interleaved 2 of 5): 0 = estreito, 1 = largo (3x).
_ITF = {
    "0": "00110",
    "1": "10001",
    "2": "01001",
    "3": "11000",
    "4": "00101",
    "5": "10100",
    "6": "01100",
    "7": "00011",
    "8": "10010",
    "9": "01010",
}
_LARG_ESTREITA = 312.0 / 405.0
_LARG_LARGA = 3.0 * _LARG_ESTREITA
_BARRAS_X = 33.0
_BARRAS_TOPO = 215.48
_BARRAS_ALTURA = 38.52


def _y(topo: float) -> float:
    """Converte y com origem no topo (como no PDF oficial) para o canvas."""
    return ALTURA_PAGINA - topo


def _texto(
    c: canvas.Canvas,
    x: float,
    topo: float,
    texto: str,
    fonte: str = NORMAL,
    tamanho: float = 7.0,
    alinhamento: str = "left",
) -> None:
    if not texto:
        return
    c.setFont(fonte, tamanho)
    c.setFillColorRGB(0, 0, 0)
    if alinhamento == "right":
        c.drawRightString(x, _y(topo), texto)
    elif alinhamento == "center":
        c.drawCentredString(x, _y(topo), texto)
    else:
        c.drawString(x, _y(topo), texto)


def _ajustar(texto: str, fonte: str, tamanho: float, largura: float) -> str:
    if pdfmetrics.stringWidth(texto, fonte, tamanho) <= largura:
        return texto
    while texto and pdfmetrics.stringWidth(texto + "...", fonte, tamanho) > largura:
        texto = texto[:-1]
    return texto + "..."


def _moeda(valor: object) -> str:
    texto = str(valor or "").strip().replace("R$", "").strip()
    if not texto:
        return "R$ 0,00"
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        numero = Decimal(texto)
    except (InvalidOperation, ValueError):
        return str(valor)
    return f"R$ {numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _data(valor: object) -> str:
    texto = str(valor or "").strip()
    partes = texto.split("-")
    if len(partes) == 3 and len(partes[0]) == 4:
        return f"{partes[2]}/{partes[1]}/{partes[0]}"
    return texto


def _documento(valor: object) -> str:
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) == 14:
        return f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:]}"
    if len(digitos) == 11:
        return f"{digitos[:3]}.{digitos[3:6]}.{digitos[6:9]}-{digitos[9:]}"
    return str(valor or "")


def _cep(valor: object) -> str:
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) == 8:
        return f"{digitos[:5]}-{digitos[5:]}"
    return str(valor or "")


def _linha_digitavel(valor: object) -> str:
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) == 48:
        return (
            f"{digitos[0:11]} {digitos[11]}  "
            f"{digitos[12:23]} {digitos[23]}  "
            f"{digitos[24:35]} {digitos[35]}  "
            f"{digitos[36:47]} {digitos[47]}"
        )
    return str(valor or "").strip()


def _codigo_barras(guia: dict) -> str:
    codigo = re.sub(r"\D", "", str(guia.get("codigo_barras") or ""))
    if len(codigo) == 44:
        return codigo
    linha = re.sub(r"\D", "", str(guia.get("linha_digitavel") or ""))
    if len(linha) == 48:
        return linha[0:11] + linha[12:23] + linha[24:35] + linha[36:47]
    return ""


def _itf(codigo: str) -> str:
    """Sequência de elementos (barra/espaço alternados): 1 estreito, 2 largo."""
    if len(codigo) % 2 or not codigo.isdigit():
        return ""
    partes = ["1111"]
    for i in range(0, len(codigo), 2):
        barras = _ITF[codigo[i]]
        espacos = _ITF[codigo[i + 1]]
        for posicao in range(5):
            partes.append("2" if barras[posicao] == "1" else "1")
            partes.append("2" if espacos[posicao] == "1" else "1")
    partes.append("211")
    return "".join(partes)


def _barras(c: canvas.Canvas, codigo: str, topo: float) -> None:
    elementos = _itf(codigo)
    if not elementos:
        return
    x = _BARRAS_X
    c.setFillColorRGB(0, 0, 0)
    for indice, elemento in enumerate(elementos):
        largura = _LARG_LARGA if elemento == "2" else _LARG_ESTREITA
        if indice % 2 == 0:
            c.rect(x, _y(topo + _BARRAS_ALTURA), largura, _BARRAS_ALTURA,
                   stroke=0, fill=1)
        x += largura


def _qr(c: canvas.Canvas, payload: str, x: float, topo: float,
        lado: float = 70.0) -> None:
    if not payload:
        return
    try:
        import segno
    except ImportError:
        return
    qr = segno.make_qr(payload, error="l", boost_error=False)
    matriz = qr.matrix
    modulos = len(matriz)
    margem = lado * 54.0 / 500.0
    area = lado - 2 * margem
    passo = area / modulos
    c.setFillColorRGB(0, 0, 0)
    for linha, valores in enumerate(matriz):
        for coluna, valor in enumerate(valores):
            if not valor:
                continue
            c.rect(
                x + margem + coluna * passo,
                _y(topo + margem + (linha + 1) * passo),
                passo,
                passo,
                stroke=0,
                fill=1,
            )


def _logo_pix(c: canvas.Canvas, x: float, topo: float) -> None:
    if not LOGO_PIX.exists():
        return
    c.drawImage(
        ImageReader(str(LOGO_PIX)),
        x,
        _y(topo + 42.0),
        width=15.0,
        height=42.0,
        mask=None,
    )


def _grade(c: canvas.Canvas, dy: float, segunda: bool = False) -> None:
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(1)
    linhas = [
        (31.5, 22.0, 31.5, 205.0),
        (557.5, 22.0, 557.5, 205.0),
        (32.0, 22.5, 557.0, 22.5),
        (32.0, 204.5, 557.0, 204.5),
        (408.5, 23.0, 408.5, 204.0),
        (32.0, 41.5, 408.0, 41.5),
        (32.0, 95.5, 408.0, 95.5),
        (32.0, 122.5, 408.0, 122.5),
        (32.0, 158.5, 408.0, 158.5),
        (472.5, 23.0, 472.5, 41.0),
        (409.0, 41.5, 557.0, 41.5),
        (409.0, 59.5, 557.0, 59.5),
        (409.0, 77.5, 557.0, 77.5),
        (409.0, 95.5, 557.0, 95.5),
        (409.0, 113.5, 557.0, 113.5),
        (500.5, 96.0, 500.5, 113.0),
        (409.0, 131.5, 557.0, 131.5),
        (409.0, 149.5, 557.0, 149.5),
        (409.0, 167.5, 557.0, 167.5),
        (409.0, 185.5, 557.0, 185.5),
    ]
    for x1, y1, x2, y2 in linhas:
        if segunda and y1 == 158.5:
            y1, y2 = 159.5, 159.5
        c.line(x1, _y(y1 + dy), x2, _y(y2 + dy))


def _rotulo(c: canvas.Canvas, x: float, topo: float, texto: str,
            tamanho: float = 6.0) -> None:
    _texto(c, x, topo, texto, NEGRITO, tamanho)


def _autenticacao(c: canvas.Canvas, dy: float) -> None:
    c.setFont(NORMAL, 5)
    c.setFillColorRGB(0, 0, 0)
    for indice, letra in enumerate("Autenticação"):
        c.drawCentredString(561.0, _y(132.72 + indice * 5.82 + dy), letra)


def via_label(via: str) -> str:
    return "1ª via - Banco" if via == "1" else "2ª via - Contribuinte"


def _desenhar_via(c: canvas.Canvas, guia: dict, dy: float, via: str,
                  municipio_nome: str, segunda: bool = False) -> None:
    emitente = guia.get("emitente") or {}
    valor = _moeda(guia.get("valor"))
    # A segunda via do portal usa pequenos deslocamentos próprios no template.
    x_uf = 412.0 if segunda else 413.0
    x_uf_rotulo = 409.0 if segunda else 410.0
    x_uf_emitente = 337.0 if segunda else 335.0
    x_moeda = 555.0 if segunda else 554.0
    y_cep = 92.58 if segunda else 91.58
    y_via = 211.58 if segunda else 213.58
    x_via = 556.0 if segunda else 555.0

    _grade(c, dy, segunda)
    _autenticacao(c, dy)

    # Cabeçalho
    _texto(c, 220, 35.12 + dy,
           "Guia Nacional de Recolhimento de Tributos Estaduais - GNRE",
           NEGRITO, 9, "center")
    _rotulo(c, x_uf_rotulo, 29.08 + dy, "UF Favorecida")
    _rotulo(c, 474, 29.08 + dy, "Código da Receita")
    _texto(c, x_uf, 37.92 + dy, str(guia.get("uf_favorecida") or ""))
    _texto(c, 477, 37.92 + dy, str(guia.get("receita") or ""))

    # Coluna direita
    linhas_direita = [
        ("Nº de Controle", 48.08, 56.92, str(guia.get("numero") or ""), 555.0),
        ("Data de Vencimento", 66.08, 74.92, _data(guia.get("data_vencimento")), 555.0),
        ("Nº Documento de Origem", 84.08, 92.92,
         str(guia.get("documento_origem") or ""), 555.0),
    ]
    for rotulo, y_rotulo, y_valor, valor_campo, x in linhas_direita:
        _rotulo(c, 410, y_rotulo + dy, rotulo)
        _texto(c, x, y_valor + dy, valor_campo, NORMAL, 7, "right")

    _rotulo(c, 410, 102.08 + dy, "Período de Referência")
    _rotulo(c, 505, 102.08 + dy, "Parcela")

    valores_direita = [
        ("Valor Principal", 120.08, 128.92, valor),
        ("Atualização Monetária", 138.08, 146.92, "R$ 0,00"),
        ("Juros", 156.08, 164.92, "R$ 0,00"),
        ("Multa", 174.08, 182.92, "R$ 0,00"),
        ("Total a Recolher", 192.08, 200.92, valor),
    ]
    for rotulo, y_rotulo, y_valor, valor_campo in valores_direita:
        _rotulo(c, 410, y_rotulo + dy, rotulo)
        _texto(c, x_moeda, y_valor + dy, valor_campo, NORMAL, 7, "right")

    # Contribuinte emitente
    _texto(c, 220, 48.08 + dy, "Dados do Contribuinte Emitente",
           NEGRITO, 6, "center")
    _rotulo(c, 35, 56.08 + dy, "Razão Social:")
    _texto(c, 35, 64.92 + dy,
           _ajustar(str(emitente.get("razao_social") or ""), NORMAL, 7, 280))
    _rotulo(c, 319, 56.08 + dy, "CNPJ/CPF/Insc. Est.:")
    _texto(c, 319, 64.92 + dy,
           _documento(emitente.get("cnpj") or emitente.get("cpf")))

    _rotulo(c, 35, 73.58 + dy, "Endereço:")
    _texto(c, 74, 73.92 + dy,
           _ajustar(str(emitente.get("endereco") or ""), NORMAL, 7, 330))
    _rotulo(c, 35, 82.58 + dy, "Município:")
    _texto(c, 74, 82.92 + dy,
           _ajustar(
               municipio_nome or str(emitente.get("municipio") or ""),
               NORMAL, 7, 240,
           ))
    _rotulo(c, 322, 82.58 + dy, "UF:")
    _texto(c, x_uf_emitente, 82.92 + dy, str(emitente.get("uf") or ""))
    _rotulo(c, 35, y_cep + dy, "CEP:")
    _texto(c, 74, 91.92 + dy, _cep(emitente.get("cep")))
    _rotulo(c, 305.33, 91.58 + dy, "Telefone:")

    # Destinatário / fiscalização (sem dados na GNRE de ICMS Outra UF)
    _texto(c, 220, 102.08 + dy, "Dados do Destinatário", NEGRITO, 6, "center")
    _rotulo(c, 35, 110.58 + dy, "CPF/CNPJ/Insc. Est.:")
    _rotulo(c, 35, 119.58 + dy, "Município:")
    _texto(c, 220, 129.08 + dy, "Reservado à Fiscalização", NEGRITO, 6, "center")
    _rotulo(c, 35, 137.58 + dy, "Convênio/Protocolo:")
    _rotulo(c, 35, 146.58 + dy, "Produto:")

    _rotulo(c, 35, 166.08 + dy, "Informações Complementares:")
    _texto(c, 36, 174.50 + dy,
           _ajustar(str(guia.get("informacoes") or ""), NORMAL, 7, 370))

    _texto(c, 35, 201.92 + dy, "Documento Válido para pagamento", NEGRITO, 7)
    _texto(c, 185, 201.92 + dy,
           _data(guia.get("data_pagamento") or guia.get("data_limite_pagamento")))

    # Base da via: linha digitável, código de barras, QR e logo PIX
    _texto(c, 71.13, 213.27 + dy, _linha_digitavel(guia.get("linha_digitavel")),
           NEGRITO, 8)
    _texto(c, x_via, y_via + dy, via_label(via), NORMAL, 6, "right")

    codigo = _codigo_barras(guia)
    if codigo:
        _barras(c, codigo, _BARRAS_TOPO + dy)
    x_logo = 395.0 if segunda else 396.0
    _qr(c, str(guia.get("qrcode") or ""), 413.0, 206.0 + dy)
    _logo_pix(c, x_logo, 222.0 + dy)


def _separador(c: canvas.Canvas) -> None:
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    c.setDash([2.5, 1.5], 0)
    c.line(33.0, _y(277.0), 561.0, _y(277.0))
    c.setDash()


def gerar_pdf_guia(
    destino: Path | str,
    guias: list[dict],
    recibo: str = "",
    municipio_nome: str = "",
) -> Path | None:
    """Grava o PDF com o layout oficial; devolve o caminho ou None."""
    validas = [
        guia for guia in guias
        if guia.get("codigo_barras") or guia.get("linha_digitavel")
    ]
    if not validas:
        return None

    caminho = Path(destino)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(
        str(caminho), pagesize=(LARGURA_PAGINA, ALTURA_PAGINA), pageCompression=1
    )
    c.setTitle("GNRE")

    for indice, guia in enumerate(validas):
        if indice:
            c.showPage()
        nome = (guia.get("emitente") or {}).get("municipio_nome") or municipio_nome
        _desenhar_via(c, guia, 0.0, "1", nome)
        _separador(c)
        _desenhar_via(c, guia, DESLOCAMENTO_VIA, "2", nome, segunda=True)

    c.save()
    return caminho
