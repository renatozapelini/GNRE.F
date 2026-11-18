"""Conferência automática da guia local contra o PDF oficial do portal.

A comparação é feita pelos DADOS FISCAIS impressos na guia (valores, datas,
documentos, receita, linha digitável, UF e códigos de barras/QR), não pela
fonte ou posição de cada texto — formatos diferentes do portal não reprovam
uma guia que está correta.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable

LIMITE_PRODUCAO = 5
_ESTADO = Path(__file__).resolve().parent / "cache" / "validacao_pdf.json"

_VALOR = re.compile(r"R\$\s*([0-9][0-9.]*,[0-9]{2})")
_DATA = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
_PERIODO = re.compile(r"\b\d{2}/\d{4}\b")
_NUMERO = re.compile(r"\d[\d./-]*\d")
_RECEITA = re.compile(r"\b\d{6}\b")
_LINHA = re.compile(
    r"\b\d{11}\s+\d\s+\d{11}\s+\d\s+\d{11}\s+\d\s+\d{11}\s+\d\b"
)
_UF = re.compile(r"\b[A-Z]{2}\b")
_LINHA_CNPJ_DESTINO = re.compile(r"CNPJ DESTINO:\s*[0-9./-]+")
_UFS = {
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
}


def _ler_estado() -> dict:
    try:
        dados = json.loads(_ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {}
    if not isinstance(dados, dict):
        dados = {}
    dados.setdefault("usadas", 0)
    dados.setdefault("historico", [])
    return dados


def _gravar_estado(dados: dict) -> None:
    _ESTADO.parent.mkdir(parents=True, exist_ok=True)
    historico = dados.get("historico") or []
    dados["historico"] = historico[-30:]
    _ESTADO.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def usadas() -> int:
    try:
        return int(_ler_estado().get("usadas") or 0)
    except (TypeError, ValueError):
        return 0


def ativo(_ambiente: str = "") -> bool:
    """True enquanto as 5 emissões de produção não foram conferidas."""
    return usadas() < LIMITE_PRODUCAO


def _sem_acento(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def _texto(paginas) -> str:
    return "\n".join(_sem_acento(pagina.get_text("text")).upper() for pagina in paginas)


def _decimal(valor: str) -> str:
    return valor.replace(".", "").replace(",", ".")


def _digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto)


def _campos_fiscais(paginas) -> dict[str, list[str]]:
    """Dados fiscais impressos na guia, independentes de fonte e posição."""
    texto = _texto(paginas)
    # A linha "CNPJ Destino" é informativa e só existe na réplica local: não
    # deve contar como documento fiscal novo na comparação.
    texto = _LINHA_CNPJ_DESTINO.sub(" ", texto)
    sem_datas = _PERIODO.sub(" ", _DATA.sub(" ", texto))
    documentos = {
        digitos
        for digitos in (_digitos(bruto) for bruto in _NUMERO.findall(sem_datas))
        if len(digitos) >= 5
    }
    linhas = {
        re.sub(r"\s+", "", achado.group(0)) for achado in _LINHA.finditer(texto)
    }
    return {
        # Campos zerados ("R$ 0,00") variam de layout entre estados e não
        # representam valor a pagar: comparar só os valores efetivos.
        "valores": sorted(
            {
                _decimal(valor)
                for valor in _VALOR.findall(texto)
                if _decimal(valor) != "0.00"
            }
        ),
        "datas": sorted(set(_DATA.findall(texto))),
        "documentos": sorted(documentos),
        "receitas": sorted(set(_RECEITA.findall(texto))),
        "linhas": sorted(linhas),
        "ufs": sorted({uf for uf in _UF.findall(texto) if uf in _UFS}),
    }


_ROTULOS = {
    "valores": "Valores",
    "datas": "Datas",
    "documentos": "Números de controle/documento",
    "receitas": "Receita",
    "linhas": "Linha digitável",
    "ufs": "UF",
}


def _amostra(valores: list[str]) -> str:
    if not valores:
        return "nenhum"
    texto = ", ".join(valores[:6])
    if len(valores) > 6:
        texto += f" (+{len(valores) - 6})"
    return texto


def _comparar_fiscal(local, oficial) -> tuple[int, int, list[str]]:
    """(grupos conferidos, total de grupos, divergências) entre os PDFs."""
    dados_local = _campos_fiscais(local)
    dados_oficial = _campos_fiscais(oficial)
    conferidos = 0
    total = 0
    divergencias = []
    for campo, rotulo in _ROTULOS.items():
        esperado = dados_oficial.get(campo, [])
        obtido = dados_local.get(campo, [])
        if not esperado:
            continue  # grupo ausente no oficial: não há o que comparar
        total += 1
        if obtido == esperado:
            conferidos += 1
        else:
            divergencias.append(
                f"{rotulo} difere: local [{_amostra(obtido)}] x "
                f"oficial [{_amostra(esperado)}]"
            )
    return conferidos, total, divergencias


def _decodificar(pagina) -> dict[str, set[str]]:
    import numpy
    import zxingcpp

    imagem = pagina.get_pixmap(dpi=200)
    matriz = numpy.frombuffer(
        imagem.samples, dtype=numpy.uint8
    ).reshape(imagem.height, imagem.width, imagem.n)
    achados: dict[str, set[str]] = {"ITF": set(), "QR": set()}
    for codigo in zxingcpp.read_barcodes(matriz):
        formato = str(codigo.format).upper()
        if "ITF" in formato or "INTERLEAVED" in formato:
            achados["ITF"].add(codigo.text)
        elif "QR" in formato:
            achados["QR"].add(codigo.text)
    return achados


def _comparar_codigos(paginas_local, paginas_oficial) -> tuple[dict, list[str]]:
    local: dict[str, set[str]] = {"ITF": set(), "QR": set()}
    oficial: dict[str, set[str]] = {"ITF": set(), "QR": set()}
    for pagina in paginas_local:
        for tipo, valores in _decodificar(pagina).items():
            local[tipo].update(valores)
    for pagina in paginas_oficial:
        for tipo, valores in _decodificar(pagina).items():
            oficial[tipo].update(valores)

    divergencias = []
    # Só acusa diferença quando o oficial decodificou o código: se a leitura
    # falhou no PDF oficial, comparar com um conjunto vazio geraria "NÃO PAGUE"
    # falso. Ausência nos dois lados também não é divergência.
    if oficial["ITF"] and local["ITF"] != oficial["ITF"]:
        divergencias.append(
            f"código de barras difere: local {sorted(local['ITF'])} x "
            f"oficial {sorted(oficial['ITF'])}"
        )
    if oficial["QR"] and local["QR"] != oficial["QR"]:
        divergencias.append("payload do QR PIX difere do oficial")
    return oficial, divergencias


def validar(
    ambiente: str,
    recibo: str,
    local: Path,
    oficial: Path,
    relatorio: Path,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Compara a guia local com o PDF oficial e grava o relatório."""
    local = Path(local)
    oficial = Path(oficial)
    if not local.is_file() or not oficial.is_file():
        return None

    def registrar(mensagem: str) -> None:
        if log:
            log(mensagem)

    try:
        import numpy  # noqa: F401
        import pymupdf
        import zxingcpp  # noqa: F401
    except ImportError:
        registrar(
            "Modo validação indisponível: instale as dependências "
            "'pymupdf' e 'zxing-cpp' para a conferência automática."
        )
        return None

    numero = usadas() + 1
    rotulo = f"{numero}/{LIMITE_PRODUCAO}" if ambiente == "1" else "teste"
    linhas: list[str] = []
    detalhes: list[str] = []
    try:
        doc_local = pymupdf.open(local)
        doc_oficial = pymupdf.open(oficial)
        try:
            paginas_local = [doc_local[i] for i in range(doc_local.page_count)]
            paginas_oficial = [doc_oficial[i] for i in range(doc_oficial.page_count)]

            if doc_local.page_count != doc_oficial.page_count:
                linhas.append(
                    f"Páginas: local {doc_local.page_count} x oficial "
                    f"{doc_oficial.page_count} (informativo)."
                )
            else:
                linhas.append(
                    f"Páginas: {doc_local.page_count} — mesma quantidade."
                )

            conferidos, total, divs = _comparar_fiscal(
                paginas_local, paginas_oficial
            )
            detalhes.extend(divs)
            if total:
                linhas.append(
                    f"Dados fiscais: {conferidos}/{total} grupos idênticos "
                    "(valores, datas, documentos, receita, linha digitável e UF)."
                )
            else:
                detalhes.append(
                    "não foi possível extrair os dados fiscais dos PDFs "
                    "para comparação"
                )

            codigos, divs = _comparar_codigos(paginas_local, paginas_oficial)
            detalhes.extend(divs)
            if not divs:
                if codigos["ITF"]:
                    barras = ", ".join(sorted(codigos["ITF"]))
                    linhas.append(f"Código de barras ITF: idêntico ({barras}).")
                else:
                    linhas.append(
                        "Código de barras ITF: não decodificado no PDF oficial "
                        "(informativo)."
                    )
                if codigos["QR"]:
                    linhas.append("QR PIX: mesmo payload do oficial.")
                else:
                    linhas.append(
                        "QR PIX: não decodificado no PDF oficial (informativo)."
                    )
        finally:
            doc_local.close()
            doc_oficial.close()
    except Exception as erro:  # noqa: BLE001 - a conferência não pode derrubar a emissão
        registrar(f"Modo validação falhou ao ler os PDFs: {erro}")
        return None

    ok = not detalhes
    linhas.insert(0, f"Conferência do PDF — {'OK' if ok else 'DIVERGÊNCIA'} ({rotulo}).")
    for item in detalhes[:40]:
        linhas.append(f"- {item}")

    relatorio = Path(relatorio)
    relatorio.parent.mkdir(parents=True, exist_ok=True)
    cabecalho = [
        f"Data: {datetime.now():%d/%m/%Y %H:%M:%S}",
        f"Ambiente: {'produção' if ambiente == '1' else 'homologação'}",
        f"Recibo: {recibo or '-'}",
        f"Guia local: {local.name}",
        f"Guia oficial: {oficial.name}",
        "",
    ]
    relatorio.write_text(
        "\n".join(cabecalho + linhas) + "\n", encoding="utf-8"
    )

    estado = _ler_estado()
    if ambiente == "1":
        estado["usadas"] = numero
    estado["historico"].append(
        {
            "data": datetime.now().isoformat(timespec="seconds"),
            "ambiente": ambiente,
            "recibo": recibo,
            "ok": ok,
            "local": local.name,
            "oficial": oficial.name,
            "relatorio": relatorio.name,
        }
    )
    _gravar_estado(estado)

    resumo = linhas[0]
    if detalhes:
        resumo += f" {len(detalhes)} divergência(s)."
    resumo += f" Relatório: {relatorio}"
    registrar(linhas[0])
    for linha in linhas[1:]:
        registrar(linha)
    return {
        "ok": ok,
        "linhas": linhas,
        "detalhes": detalhes,
        "resumo": resumo,
        "relatorio": str(relatorio),
        "rotulo": rotulo,
    }
