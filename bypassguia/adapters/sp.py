"""Adapter do DARE ICMS avulso de São Paulo (ICMS Transporte - outra UF)."""

from __future__ import annotations

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from ..models import Guia
from .base import BaseAdapter, CaptchaRequerido, FalhaPortal, Resultado


class AdapterSP(BaseAdapter):
    uf = "SP"
    nome = "DARE ICMS Avulso"

    _AGUARDAR_CONSULTA = """() => {
        const dare = document.querySelector('#fsInformacaoDare');
        const captcha = document.querySelector('#opcaoCaptcha2');
        const corpo = (document.body && document.body.innerText) || '';
        const erro = corpo.includes('FaultException') ||
                     corpo.includes('Index was outside the bounds');
        return (dare && dare.offsetParent !== null) ||
               (captcha && !captcha.classList.contains('d-none')) || erro;
    }"""

    def emitir(self, page: Page, guia: Guia) -> Resultado:
        cfg = self.config["ufs"]["SP"]
        self._consultar_contribuinte(page, cfg, guia)

        if self.visivel(page, "#opcaoCaptcha2"):
            self.diagnostico(page, "captcha")
            raise CaptchaRequerido("SP: a SEFAZ pediu o captcha de letras.")

        erro = self.erro_na_pagina(page)
        if erro and not self.visivel(page, "#fsInformacaoDare"):
            raise FalhaPortal(f"SP: {erro}")

        self.log("SP: preenchendo identificação e dados do DARE", "passo")
        self._preencher_identificacao(page, guia)
        page.select_option("#ddlTipoDebito", str(cfg["receita_transporte"]))
        page.wait_for_timeout(500)

        self._preencher(page, "#txtReferencia", guia.referencia_mmaaaa, forcar=True)
        page.locator("#txtReferencia").press("Tab")
        page.wait_for_timeout(1200)
        self._preencher(page, "#txtVencimento", guia.vencimento.isoformat(), forcar=True)

        observacao = (cfg.get("observacao") or "CT-e {chave}").format(
            chave=guia.chave_cte or guia.numero_cte,
            numero=guia.numero_cte,
        )[:100]
        self._preencher(page, "#txtObservacao", observacao, forcar=True)
        self._preencher(page, "#txtValorCalculo", guia.valor_br, forcar=True)
        page.locator("#txtValorCalculo").press("Tab")
        page.wait_for_timeout(400)

        self.log("SP: calculando juros/multa e total", "passo")
        self.clicar(page, "#btnCalcular")
        try:
            page.wait_for_function(
                """() => {
                    const total = document.querySelector('#txtValorTotal');
                    return total && total.value && total.value !== '0,00';
                }""",
                timeout=30000,
            )
        except PlaywrightTimeout as erro:
            self.diagnostico(page, "calculo")
            raise FalhaPortal(
                "SP: o cálculo não retornou. " + self.erro_na_pagina(page)
            ) from erro

        total = (page.locator("#txtValorTotal").input_value() or "").strip()
        juros = (page.locator("#txtValorJuros").input_value() or "").strip()
        multa = (page.locator("#txtValorMulta").input_value() or "").strip()
        self.log(f"SP: total calculado R$ {total} (juros {juros} / multa {multa})", "info")

        self.log("SP: gerando o PDF do DARE", "passo")
        try:
            with page.expect_download(timeout=60000) as evento:
                self.clicar(page, "#btnGerar")
            download = evento.value
            caminho = download.path()
            pdf = caminho.read_bytes() if caminho else b""
            nome = download.suggested_filename or "Dare.pdf"
        except PlaywrightTimeout:
            pdf = b""
            nome = ""

        if not pdf:
            corpo = (page.content() or "").lower()
            if "requer validação adicional" in corpo or self.visivel(page, "#opcaoCaptcha2"):
                raise CaptchaRequerido("SP: captcha exigido na geração do DARE.")
            self.diagnostico(page, "geracao")
            raise FalhaPortal("SP: o download do DARE não foi iniciado. " + self.erro_na_pagina(page))

        pix = ""
        if self.visivel(page, "#pixCopiaColaCodigo"):
            pix = (page.locator("#pixCopiaColaCodigo").input_value() or "").strip()

        return Resultado(
            pdf=pdf,
            identificador=pix,
            detalhes={
                "arquivo": nome,
                "total": total,
                "juros": juros,
                "multa": multa,
                "receita": str(cfg["receita_transporte"]),
                "referencia": guia.referencia_mmaaaa,
                "observacao": observacao,
            },
            avisos=list(guia.avisos),
        )

    # ------------------------------------------------------------------ internos
    def _consultar_contribuinte(self, page: Page, cfg: dict, guia: Guia) -> None:
        """Consulta o CNPJ do emitente; o portal de SP às vezes responde HTTP 500."""
        tentativas = 3
        for tentativa in range(1, tentativas + 1):
            page.goto(cfg["url"], wait_until="domcontentloaded")
            self.esperar(page, "#txtCriterioConsulta")
            page.select_option("#ddlContribuinte", "CNPJ")
            page.wait_for_timeout(800)
            for _ in range(3):
                self._preencher(
                    page, "#txtCriterioConsulta", guia.emitente.documento_fmt, forcar=True
                )
                page.wait_for_timeout(700)
                if page.locator("#txtCriterioConsulta").input_value().strip("_./- "):
                    break

            self.log(
                "SP: consultando o CNPJ do emitente "
                f"{guia.emitente.documento_fmt} na SEFAZ-SP "
                f"(tentativa {tentativa}/{tentativas})",
                "passo",
            )
            codigo: dict = {}

            def anotar(resposta, alvo=codigo):
                if "btnConsultar_Click" in resposta.url:
                    alvo["status"] = resposta.status

            page.on("response", anotar)
            try:
                self.clicar(page, "#btnConsultar")
                try:
                    page.wait_for_function(self._AGUARDAR_CONSULTA, timeout=30000)
                except PlaywrightTimeout as erro:
                    self.diagnostico(page, "consulta")
                    if tentativa < tentativas:
                        self.log("SP: portal sem resposta; nova tentativa em 15s.", "aviso")
                        page.wait_for_timeout(15000)
                        continue
                    extra = f" (HTTP {codigo['status']})" if codigo.get("status") else ""
                    raise FalhaPortal(
                        "SP: a consulta do CNPJ não retornou" + extra + ". "
                        + self.erro_na_pagina(page)
                    ) from erro
            finally:
                page.remove_listener("response", anotar)

            if self._erro_servidor(page):
                self.diagnostico(page, "consulta_servidor")
                if tentativa < tentativas:
                    self.log(
                        "SP: a SEFAZ-SP respondeu com erro interno (HTTP 500); "
                        "nova tentativa em 15s.",
                        "aviso",
                    )
                    page.wait_for_timeout(15000)
                    continue
                raise FalhaPortal(
                    "SP: o portal da SEFAZ-SP está com erro interno na consulta do CNPJ "
                    "(HTTP 500) e não gerou a guia. Nada foi emitido nem pago; "
                    "tente novamente em alguns minutos."
                )
            return

    def _erro_servidor(self, page: Page) -> bool:
        try:
            corpo = page.inner_text("body")
        except Exception:
            return False
        return (
            "FaultException" in corpo
            or "Index was outside the bounds" in corpo
        )

    def _preencher_identificacao(self, page: Page, guia: Guia) -> None:
        valores = {
            "#txtCNPJ": guia.emitente.documento_fmt,
            "#txtRazaoSocial": guia.emitente.nome,
            "#txtTelefone": guia.emitente.telefone or str(self._cfg("telefone_fallback", "")),
            "#txtEndereco": guia.emitente.endereco_completo,
            "#txtCidade": guia.emitente.municipio_nome or guia.municipio_ini_nome,
        }
        for seletor, valor in valores.items():
            if not valor:
                continue
            try:
                if page.locator(seletor).first.is_disabled():
                    continue
                self._preencher(page, seletor, valor, forcar=True)
            except Exception:
                continue
        try:
            if not page.locator("#ddlUF").first.is_disabled():
                page.select_option("#ddlUF", guia.emitente.uf)
        except Exception:
            pass

    def _preencher(self, page: Page, seletor: str, valor: str, forcar: bool = False) -> None:
        loc = page.locator(seletor).first
        if forcar and loc.is_disabled():
            return
        tipo = (loc.get_attribute("type") or "").lower()
        if tipo == "date":
            loc.fill(str(valor), timeout=10000)
        else:
            page.evaluate(
                """([sel, val]) => {
                    const el = document.querySelector(sel);
                    if (!el) throw new Error('campo ausente: ' + sel);
                    el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                }""",
                [seletor, str(valor)],
            )
            page.wait_for_timeout(150)
        atual = loc.input_value()
        if not _confere(atual, str(valor)):
            raise FalhaPortal(f"SP: o campo {seletor} não aceitou '{valor}' (ficou '{atual}').")


def _confere(atual: str, esperado: str) -> bool:
    import re

    def digitos(texto: str) -> str:
        return re.sub(r"\D", "", texto or "")

    if not esperado:
        return True
    if digitos(esperado):
        return digitos(atual).lstrip("0") == digitos(esperado).lstrip("0")
    return (atual or "").strip().upper() == esperado.strip().upper()
