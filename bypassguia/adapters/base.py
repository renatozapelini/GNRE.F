"""Base dos adapters de portal."""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from ..models import Guia
from ..paths import LOG_DIR


class FalhaPortal(RuntimeError):
    """Falha controlada ao preencher/gerar a guia."""


class CaptchaRequerido(FalhaPortal):
    """O portal pediu captcha e o modo invisível não conseguiu passar."""


class DivergenciaGuia(FalhaPortal):
    """A guia gerada não confere com o XML."""


@dataclass
class Resultado:
    pdf: bytes
    identificador: str = ""
    detalhes: dict = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)


def normalizar(texto: str | None) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().upper()


class BaseAdapter:
    uf = ""
    nome = ""

    def __init__(self, config: dict, log: Callable[[str, str], None] | None = None):
        self.config = config
        self.log = log or (lambda mensagem, nivel="info": None)

    # ------------------------------------------------------------------ helpers
    def _cfg(self, chave: str, padrao=None):
        return self.config.get("ufs", {}).get(self.uf, {}).get(chave, padrao)

    def esperar(self, page: Page, seletor: str, estado: str = "visible", timeout: int | None = None):
        timeout = timeout or self._cfg("timeout_ms", self.config.get("timeout_ms", 45000))
        try:
            page.locator(seletor).first.wait_for(state=estado, timeout=timeout)
        except PlaywrightTimeout as erro:
            raise FalhaPortal(f"Elemento não apareceu: {seletor}") from erro

    def preencher(self, page: Page, seletor: str, valor: str, blur: bool = False):
        loc = page.locator(seletor).first
        try:
            loc.fill(str(valor), timeout=15000)
        except Exception:
            page.evaluate(
                """([sel, val]) => {
                    const el = document.querySelector(sel);
                    if (!el) throw new Error('campo ausente: ' + sel);
                    el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                [seletor, str(valor)],
            )
        if blur:
            loc.press("Tab")
            page.wait_for_timeout(250)

    def clicar(self, page: Page, seletor: str):
        loc = page.locator(seletor).first
        loc.scroll_into_view_if_needed()
        loc.click(timeout=20000)

    def clicar_texto(self, page: Page, texto: str, tag: str = "button"):
        alvo = normalizar(texto)
        candidatos = page.locator(tag)
        for i in range(candidatos.count()):
            item = candidatos.nth(i)
            try:
                if alvo in normalizar(item.inner_text()):
                    item.scroll_into_view_if_needed()
                    item.click(timeout=15000)
                    return
            except Exception:
                continue
        raise FalhaPortal(f"Botão não encontrado: {texto}")

    def selecionar_texto(self, page: Page, seletor: str, texto: str, uf: str = "") -> str:
        alvo = normalizar(texto)
        opcoes = page.eval_on_selector_all(
            f"{seletor} option",
            "els => els.map(e => ({value: e.value, text: e.textContent}))",
        )
        melhor = None
        for opcao in opcoes:
            rotulo = normalizar(opcao["text"])
            if rotulo == alvo:
                melhor = opcao
                break
            if alvo and alvo in rotulo and melhor is None:
                melhor = opcao
        if melhor is None:
            amostra = ", ".join(o["text"].strip() for o in opcoes[:12])
            raise FalhaPortal(
                f"Opção '{texto}' não encontrada em {seletor}. Opções: {amostra}"
            )
        page.select_option(seletor, value=melhor["value"])
        return melhor["value"]

    def texto(self, page: Page, seletor: str) -> str:
        try:
            return (page.locator(seletor).first.inner_text(timeout=5000) or "").strip()
        except Exception:
            return ""

    def visivel(self, page: Page, seletor: str) -> bool:
        try:
            return page.locator(seletor).first.is_visible(timeout=1000)
        except Exception:
            return False

    def esperar_texto(self, page: Page, texto: str, timeout: int | None = None) -> None:
        timeout = timeout or self._cfg("espera_portal_ms", 25000)
        try:
            page.get_by_text(texto, exact=False).first.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeout as erro:
            raise FalhaPortal(f"Texto não apareceu na página: {texto}") from erro

    def diagnostico(self, page: Page | None, nome: str) -> Path:
        pasta = LOG_DIR / "diagnosticos"
        pasta.mkdir(parents=True, exist_ok=True)
        base = pasta / f"{time.strftime('%Y%m%d_%H%M%S')}_{self.uf}_{nome}"
        if page is not None:
            try:
                base.with_suffix(".png").write_bytes(page.screenshot(full_page=True))
            except Exception:
                pass
            try:
                base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
        return base

    def erro_na_pagina(self, page: Page) -> str:
        textos = []
        for seletor in (
            ".alert-danger", ".alert-warning", ".invalid-feedback",
            "[role=alert]", ".mensagem-erro", ".erro",
        ):
            try:
                for item in page.locator(seletor).all():
                    if item.is_visible():
                        conteudo = (item.inner_text() or "").strip()
                        if conteudo and conteudo not in textos:
                            textos.append(conteudo)
            except Exception:
                continue
        return " | ".join(textos)[:800]

    def aguardar_estabilidade(self, page: Page, ms: int = 800) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(ms)

    # ------------------------------------------------------------------- contrato
    def emitir(self, page: Page, guia: Guia) -> Resultado:
        raise NotImplementedError
