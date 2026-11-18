"""Orquestração: escolhe o adapter, controla o navegador e entrega o PDF."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.sync_api import sync_playwright

from .adapters import CaptchaRequerido, DivergenciaGuia, FalhaPortal, adapter_para
from .config import carregar
from .models import Guia
from .navegador import NavegadorAusente, instalado
from .paths import DADOS, LOG_DIR, PERFIL_DIR, SAIDA_DIR
from .pdfcheck import carimbar_nao_pague, conferir_pdf
from .xml_reader import ErroXML, ler_xml

INDICE_PATH = DADOS / "emitidas.json"

Log = Callable[[str, str], None]


@dataclass
class Entrega:
    estado: str = "falha"
    guia: Guia | None = None
    pdf: Path | None = None
    identificador: str = ""
    mensagem: str = ""
    avisos: list[str] = field(default_factory=list)
    detalhes: dict = field(default_factory=dict)


def _sem_log(mensagem: str, nivel: str = "info") -> None:
    pass


class Runner:
    def __init__(
        self,
        config: dict | None = None,
        log: Log | None = None,
        saida_dir: Path | None = None,
    ):
        self.config = config or carregar()
        self.log: Log = log or _sem_log
        self.saida_dir = Path(saida_dir) if saida_dir else SAIDA_DIR

    def processar(self, caminho: str | Path, modo_visivel: bool | None = None) -> Entrega:
        caminho = Path(caminho)
        try:
            guia = ler_xml(caminho)
        except (ErroXML, FileNotFoundError, ValueError) as erro:
            return Entrega(estado="falha", mensagem=f"XML recusado: {erro}")

        entrega = Entrega(guia=guia)
        try:
            adapter = adapter_para(guia.uf, self.config, log=self.log)
        except FalhaPortal as erro:
            entrega.mensagem = str(erro)
            return entrega

        visivel = (
            self.config.get("modo_visivel", False)
            if modo_visivel is None
            else modo_visivel
        )
        resultado = None
        try:
            resultado = self._executar(adapter, guia, visivel, entrega)
        except CaptchaRequerido as erro:
            if visivel or not self.config.get("tentar_visivel_em_captcha", True):
                entrega.estado = "captcha"
                entrega.mensagem = str(erro)
                return entrega
            self.log("Portal pediu captcha; abrindo o navegador para confirmação.", "aviso")
            try:
                resultado = self._executar(adapter, guia, True, entrega)
            except CaptchaRequerido as novo:
                entrega.estado = "captcha"
                entrega.mensagem = str(novo)
                return entrega
            except (FalhaPortal, DivergenciaGuia) as erro:
                entrega.mensagem = str(erro)
                return entrega
        except DivergenciaGuia as erro:
            entrega.estado = "divergente"
            entrega.mensagem = str(erro)
            return entrega
        except (FalhaPortal, Exception) as erro:
            entrega.mensagem = str(erro)
            self.log(f"Falha: {erro}", "erro")
            return entrega

        return self._entregar(entrega, resultado)

    # ------------------------------------------------------------------ internos
    def _executar(self, adapter, guia: Guia, visivel: bool, entrega: Entrega):
        if not instalado():
            raise NavegadorAusente(
                "O navegador do portal não está instalado. Use Opções → "
                "Instalar navegador do portal."
            )
        with sync_playwright() as playwright:
            try:
                contexto = playwright.chromium.launch_persistent_context(
                    str(PERFIL_DIR),
                    channel="chromium",
                    headless=not visivel,
                    viewport={"width": 1366, "height": 900},
                    locale="pt-BR",
                    accept_downloads=True,
                )
            except Exception as erro:
                if "Executable doesn't exist" in str(erro):
                    raise NavegadorAusente(
                        "O navegador do portal precisa ser instalado. Use Opções → "
                        "Instalar navegador do portal."
                    ) from erro
                raise
            try:
                paginas = contexto.pages
                page = paginas[0] if paginas else contexto.new_page()
                return adapter.emitir(page, guia)
            finally:
                try:
                    contexto.close()
                except Exception:
                    pass

    def _entregar(self, entrega: Entrega, resultado) -> Entrega:
        guia = entrega.guia
        falhas, avisos = conferir_pdf(resultado.pdf, guia, resultado.identificador)
        entrega.avisos = list(dict.fromkeys([*resultado.avisos, *avisos]))
        entrega.identificador = resultado.identificador
        entrega.detalhes = resultado.detalhes

        if falhas:
            pasta = LOG_DIR / "diagnosticos"
            pasta.mkdir(parents=True, exist_ok=True)
            bloqueado = pasta / f"{time.strftime('%Y%m%d_%H%M%S')}_{guia.uf}_NAO_PAGUE.pdf"
            bloqueado.write_bytes(carimbar_nao_pague(resultado.pdf))
            entrega.estado = "divergente"
            entrega.mensagem = (
                "NÃO PAGUE: a guia gerada diverge do XML ("
                + ", ".join(falhas)
                + f"). O arquivo ficou retido em {bloqueado}."
            )
            self.log(entrega.mensagem, "erro")
            return entrega

        SAIDA_DIR.mkdir(parents=True, exist_ok=True)
        self.saida_dir.mkdir(parents=True, exist_ok=True)
        destino = self._nome_livre(self.saida_dir / guia.nome_pdf())
        destino.write_bytes(resultado.pdf)
        entrega.pdf = destino
        entrega.estado = "ok"
        entrega.mensagem = "Guia gerada e conferida."
        self._registrar(entrega)
        self.log(f"PDF salvo em {destino}", "ok")
        return entrega

    def _nome_livre(self, destino: Path) -> Path:
        if not destino.exists():
            return destino
        contador = 2
        while True:
            alternativo = destino.with_name(f"{destino.stem} ({contador}){destino.suffix}")
            if not alternativo.exists():
                return alternativo
            contador += 1

    def _registrar(self, entrega: Entrega) -> None:
        guia = entrega.guia
        registro = {
            "data_hora": datetime.now().isoformat(timespec="seconds"),
            "uf": guia.uf,
            "xml": str(guia.arquivo),
            "pdf": str(entrega.pdf),
            "identificador": entrega.identificador,
            "cte": guia.numero_cte or guia.chave_cte,
            "valor": guia.valor_br,
            "detalhes": entrega.detalhes,
            "avisos": entrega.avisos,
        }
        dados = []
        if INDICE_PATH.exists():
            try:
                dados = json.loads(INDICE_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                dados = []
        dados.append(registro)
        try:
            INDICE_PATH.write_text(
                json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as erro:
            self.log(f"Não registrei no índice: {erro}", "aviso")
