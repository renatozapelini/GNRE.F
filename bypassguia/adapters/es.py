"""Adapter do DUA ICMS Transporte do Espírito Santo."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from ..models import Guia, somente_digitos
from .base import BaseAdapter, DivergenciaGuia, FalhaPortal, Resultado, normalizar


class AdapterES(BaseAdapter):
    uf = "ES"
    nome = "DUA ICMS Transporte"

    def emitir(self, page: Page, guia: Guia) -> Resultado:
        cfg = self.config["ufs"]["ES"]
        if cfg.get("exige_cte", True) and guia.origem != "cte":
            raise FalhaPortal(
                "ES: envie o XML do CT-e (o lote do GNRE não traz o município de início "
                "da prestação, exigido pelo DUA)."
            )

        page.goto(cfg["url"], wait_until="domcontentloaded")
        self.esperar(page, "#codCpfCnpjPessoa")

        municipio = guia.municipio_ini_nome or guia.emitente.municipio_nome
        if not municipio:
            raise FalhaPortal("ES: município de início não identificado no XML.")
        try:
            self.selecionar_texto(page, "#codMunicipioTom", municipio, uf="ES")
        except FalhaPortal as erro:
            raise FalhaPortal(
                f"ES: município de início '{municipio}' não está na lista do DUA. {erro}"
            ) from erro

        self.preencher(page, "#codCpfCnpjPessoa", guia.emitente.documento)
        codigo_servico = (
            cfg["servico_autonomo"] if guia.emitente.cpf else cfg["servico_outros_estados"]
        )
        page.select_option("#idServico", str(codigo_servico))
        page.wait_for_timeout(300)

        self.preencher(page, "#datReferencia", guia.referencia_aaaa_mm)
        self.preencher(page, "#datVencimento", guia.vencimento.isoformat())
        self.preencher(page, "#vlrReceita", guia.valor_br)
        page.locator("#vlrReceita").press("Tab")
        if not page.locator("#vlrCredito").first.is_disabled():
            self.preencher(page, "#vlrCredito", "0,00")

        informacao = (cfg.get("informacao") or "CT-e {chave}").format(
            chave=guia.chave_cte or guia.numero_cte,
            numero=guia.numero_cte,
        )[:500]
        self.preencher(page, "#dscInformacao", informacao)

        self.log("ES: avançando para a conferência do DUA", "passo")
        self.clicar_texto(page, "Próximo")

        try:
            page.get_by_text("Gerar DUA", exact=False).first.wait_for(
                state="visible", timeout=30000
            )
        except PlaywrightTimeout as erro:
            self.diagnostico(page, "conferencia")
            raise FalhaPortal("ES: a conferência não abriu. " + self.erro_na_pagina(page)) from erro

        self._conferir_resumo(page, guia, codigo_servico)
        self.log("ES: conferência OK, gerando o DUA", "passo")

        pdf = self._gerar_e_capturar(page, guia)
        if not pdf:
            self.diagnostico(page, "geracao")
            raise FalhaPortal("ES: o DUA não foi gerado/baixado. " + self.erro_na_pagina(page))

        return Resultado(
            pdf=pdf,
            identificador=self._identificador(page),
            detalhes={
                "arquivo": "dua.pdf",
                "municipio": municipio,
                "servico": codigo_servico,
                "referencia": guia.referencia_mmaaaa,
                "informacao": informacao,
            },
            avisos=list(guia.avisos),
        )

    # ------------------------------------------------------------------ internos
    def _conferir_resumo(self, page: Page, guia: Guia, codigo_servico: str) -> None:
        corpo = normalizar(page.inner_text("body"))
        faltas = []
        if somente_digitos(guia.emitente.documento) not in somente_digitos(corpo):
            faltas.append("CNPJ do contribuinte")
        if normalizar(guia.valor_br) not in corpo:
            faltas.append(f"valor {guia.valor_br}")
        if normalizar(guia.referencia_mmaaaa) not in corpo:
            faltas.append(f"referência {guia.referencia_mmaaaa}")
        if guia.municipio_ini_nome and normalizar(guia.municipio_ini_nome) not in corpo:
            faltas.append(f"município {guia.municipio_ini_nome}")
        if faltas:
            self.diagnostico(page, "divergencia")
            raise DivergenciaGuia(
                "ES: a conferência não confere com o XML: " + ", ".join(faltas)
            )

    def _gerar_e_capturar(self, page: Page, guia: Guia) -> bytes:
        try:
            with page.expect_download(timeout=15000) as evento:
                self.clicar_texto(page, "Gerar DUA")
            download = evento.value
            caminho: Path | None = download.path()
            if caminho:
                return caminho.read_bytes()
        except PlaywrightTimeout:
            pass

        try:
            page.wait_for_selector("a[href*='html-dua']", timeout=45000)
        except PlaywrightTimeout as erro:
            raise FalhaPortal(
                "ES: a tela de sucesso do DUA não abriu. " + self.erro_na_pagina(page)
            ) from erro

        link = page.locator("a[href*='html-dua']").first.get_attribute("href") or ""
        if not link:
            return b""
        if link.startswith("/"):
            link = "https://s1-internet.sefaz.es.gov.br" + link

        pagina = page.context.new_page()
        try:
            pagina.goto(link, wait_until="networkidle", timeout=60000)
            pagina.emulate_media(media="print")
            return pagina.pdf(
                format="A4",
                print_background=True,
                margin={"top": "6mm", "bottom": "6mm", "left": "8mm", "right": "8mm"},
            )
        except Exception as erro:
            self.log(f"ES: falha ao renderizar o DUA: {erro}", "aviso")
            return b""
        finally:
            pagina.close()

    def _identificador(self, page: Page) -> str:
        texto = page.inner_text("body")
        achado = re.search(r"DUA\s*N[ºo°]?\s*(\d{6,})", texto, re.IGNORECASE)
        if achado:
            return achado.group(1)
        digitos = somente_digitos(texto)
        return digitos[-10:] if len(digitos) > 44 else ""
