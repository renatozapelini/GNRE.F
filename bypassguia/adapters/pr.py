"""Adapter da GR-PR (ICMS Transporte - contribuinte não inscrito)."""

from __future__ import annotations

import re
from datetime import date

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from ..models import Guia
from .base import (
    BaseAdapter,
    CaptchaRequerido,
    DivergenciaGuia,
    FalhaPortal,
    Resultado,
    normalizar,
)

NOMES_UF = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia",
    "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás",
    "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais",
    "PA": "Pará", "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí",
    "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul",
    "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo",
    "SE": "Sergipe", "TO": "Tocantins",
}


class AdapterPR(BaseAdapter):
    uf = "PR"
    nome = "GR-PR"

    def emitir(self, page: Page, guia: Guia) -> Resultado:
        cfg = self.config["ufs"]["PR"]
        if guia.origem != "cte":
            raise FalhaPortal(
                "PR: envie o XML do CT-e. A GR-PR exige o destinatário e o município "
                "onde a prestação começou, dados que o lote do GNRE não traz."
            )
        if not guia.vencimento:
            raise FalhaPortal("PR: o CT-e não permitiu calcular o vencimento.")

        page.goto(cfg["url"], wait_until="domcontentloaded")
        self._fechar_cookies(page)
        try:
            page.wait_for_function(
                "() => !!document.querySelector('#codCategoria') && "
                "!!document.querySelector('.multiselect__tags')",
                timeout=45000,
            )
        except PlaywrightTimeout as erro:
            raise FalhaPortal("PR: a tela de emissão não carregou.") from erro
        self.aguardar_estabilidade(page, 500)

        self.log("PR: selecionando ICMS / Transporte (1317)", "passo")
        self._multiselect(page, "Categoria", cfg.get("categoria_texto", "ICMS"))
        page.wait_for_timeout(900)
        self._multiselect(page, "Código de Arrecadação", str(cfg["arrecadacao"]))
        page.wait_for_timeout(900)
        self._clicar_avancar(page)

        self.log("PR: identificando o contribuinte", "passo")
        self._esperar_etapa_2(page)
        tipo = "CPF" if guia.emitente.cpf else "CNPJ"
        self._multiselect(page, "Tipo de Identificação do Contribuinte", tipo)
        page.wait_for_timeout(1200)
        documento = self._campo_ativo(page, tipo).locator("input").first
        documento.fill(guia.emitente.documento)
        documento.press("Tab")
        page.wait_for_timeout(600)

        data_escolhida, aviso_data = self._escolher_data(
            page, "Data para Pagamento", guia.vencimento
        )
        avisos = list(guia.avisos)
        if aviso_data:
            avisos.append(aviso_data)
            self.log(aviso_data, "aviso")

        self._clicar_avancar(page)
        self._esperar_etapa_2_final(page)

        self.log("PR: preenchendo os dados da guia", "passo")
        self._preencher_contribuinte(page, guia)
        self._preencher_operacao(page, guia, data_escolhida)
        self._preencher_destinatario(page, guia)

        self._conferir_preenchimento(page, guia)

        self.log("PR: emitindo a GR-PR", "passo")
        dados = self._emitir(page)
        link = self._link_da_guia(page, dados)
        if not link:
            self.diagnostico(page, "sem_link")
            raise FalhaPortal(
                "PR: a guia foi emitida, mas o link do documento não foi localizado. "
                + self.erro_na_pagina(page)
            )

        pdf = self._capturar_pdf(page, link)
        if not pdf:
            self.diagnostico(page, "pdf_vazio")
            raise FalhaPortal("PR: não foi possível renderizar o PDF da GR-PR.")

        identificador = self._codigo_barras(link, dados)
        return Resultado(
            pdf=pdf,
            identificador=identificador,
            detalhes={
                "link": link,
                "codigo_barras": identificador,
                "arrecadacao": str(cfg["arrecadacao"]),
                "data_pagamento": data_escolhida.strftime("%d/%m/%Y"),
                "municipio": guia.municipio_ini_nome,
                "valor": guia.valor_br,
            },
            avisos=avisos,
        )

    # ------------------------------------------------------------------ passos
    def _fechar_cookies(self, page: Page) -> None:
        for texto in ("Aceitar tudo", "Entendi. Fechar este aviso."):
            try:
                botao = page.get_by_role("button", name=texto).first
                if botao.count() and botao.is_visible():
                    botao.click(timeout=3000)
                    page.wait_for_timeout(300)
                    return
            except Exception:
                continue
        try:
            page.evaluate(
                "() => { const p=document.querySelector('#sliding-popup'); if(p) p.remove(); }"
            )
        except Exception:
            pass

    def _campo(self, page: Page, rotulo: str):
        return page.locator(
            f"xpath=//label[normalize-space(text())='{rotulo}']/following-sibling::fieldset[1]"
        ).first

    def _campo_ativo(self, page: Page, rotulo: str):
        campos = page.locator(
            f"xpath=//label[normalize-space(text())='{rotulo}']/following-sibling::fieldset[1]"
        )
        for i in range(campos.count() - 1, -1, -1):
            campo = campos.nth(i)
            try:
                entrada = campo.locator("input, textarea").first
                if campo.is_visible() and entrada.is_visible() and entrada.is_enabled():
                    return campo
            except Exception:
                continue
        return campos.first

    def _multiselect(self, page: Page, rotulo: str, texto: str) -> None:
        campo = self._campo(page, rotulo)
        try:
            campo.locator(".multiselect__tags").click(timeout=15000)
        except PlaywrightTimeout as erro:
            self.diagnostico(page, f"campo_{rotulo[:12]}")
            raise FalhaPortal(f"PR: campo '{rotulo}' não encontrado.") from erro
        page.wait_for_timeout(600)
        try:
            campo.locator(".multiselect__input").first.focus()
            page.keyboard.type(texto, delay=40)
            page.wait_for_timeout(900)
        except Exception:
            pass
        opcoes = campo.locator(".multiselect__option")
        alvo = normalizar(texto)
        escolhida = None
        for i in range(opcoes.count()):
            item = opcoes.nth(i)
            try:
                rotulo_opcao = normalizar(item.inner_text())
            except Exception:
                continue
            if alvo and (alvo in rotulo_opcao or rotulo_opcao in alvo):
                escolhida = item
                break
        if escolhida is None:
            raise FalhaPortal(f"PR: opção '{texto}' não encontrada em '{rotulo}'.")
        escolhida.scroll_into_view_if_needed()
        escolhida.click(timeout=10000)
        page.wait_for_timeout(700)
        marcado = self._selecionado(campo)
        if marcado and alvo not in marcado and marcado not in alvo:
            raise FalhaPortal(
                f"PR: a seleção de '{rotulo}' não fixou '{texto}' (ficou '{marcado}')."
            )

    def _selecionado(self, campo) -> str:
        """Texto da opção fixada; no modo single o tags-wrap fica oculto e vazio."""
        for seletor in (".multiselect__single", ".multiselect__tags-wrap"):
            try:
                elemento = campo.locator(seletor).first
                if elemento.count() and elemento.is_visible():
                    texto = normalizar(elemento.inner_text())
                    if texto:
                        return texto
            except Exception:
                continue
        return ""

    def _clicar_avancar(self, page: Page) -> None:
        botoes = page.get_by_role("button", name="Avançar")
        for i in range(botoes.count()):
            botao = botoes.nth(i)
            try:
                if botao.is_visible() and botao.is_enabled():
                    botao.click(timeout=10000)
                    page.wait_for_timeout(2500)
                    return
            except Exception:
                continue
        raise FalhaPortal("PR: botão Avançar indisponível " + self.erro_na_pagina(page))

    def _esperar_etapa_2(self, page: Page) -> None:
        try:
            self._campo(page, "Data para Pagamento").locator(".multiselect__tags").wait_for(
                state="visible", timeout=30000
            )
        except PlaywrightTimeout as erro:
            self._captcha_ou_falha(page, "etapa 1")
            raise FalhaPortal("PR: a etapa 1 não abriu. " + self.erro_na_pagina(page)) from erro

    def _esperar_etapa_2_final(self, page: Page) -> None:
        try:
            self._campo(page, "Valor Principal (R$)").locator("input").wait_for(
                state="visible", timeout=45000
            )
        except PlaywrightTimeout as erro:
            self._captcha_ou_falha(page, "etapa 2")
            raise FalhaPortal(
                "PR: a etapa 2 não abriu. " + self.erro_na_pagina(page)
            ) from erro

    def _captcha_ou_falha(self, page: Page, etapa: str) -> None:
        texto = normalizar(page.inner_text("body"))[:4000]
        if "NAO FOI POSSIVEL CONTINUAR" in texto or "SCORE (0" in texto:
            self.diagnostico(page, f"captcha_{etapa}")
            raise CaptchaRequerido(
                "PR: o portal recusou a emissão automática (reCAPTCHA). "
                "Abrindo o navegador para concluir com o usuário."
            )

    def _escolher_data(
        self, page: Page, rotulo: str, alvo: date
    ) -> tuple[date, str]:
        campo = self._campo(page, rotulo)
        campo.locator(".multiselect__tags").click()
        page.wait_for_timeout(600)
        datas: list[tuple[date, object]] = []
        for item in campo.locator(".multiselect__option").all():
            texto = (item.inner_text() or "").strip()
            achado = re.match(r"(\d{2})/(\d{2})/(\d{4})", texto)
            if achado:
                d, m, a = map(int, achado.groups())
                datas.append((date(a, m, d), item))
        if not datas:
            raise FalhaPortal("PR: não há datas de pagamento disponíveis.")
        exata = next((item for item in datas if item[0] == alvo), None)
        aviso = ""
        if exata:
            escolhida = exata
        else:
            futuras = [item for item in datas if item[0] >= alvo]
            escolhida = futuras[0] if futuras else max(datas, key=lambda item: item[0])
            aviso = (
                f"PR: vencimento {alvo.strftime('%d/%m/%Y')} indisponível no portal; "
                f"usei {escolhida[0].strftime('%d/%m/%Y')}."
            )
        escolhida[1].scroll_into_view_if_needed()
        escolhida[1].click(timeout=10000)
        page.wait_for_timeout(500)
        return escolhida[0], aviso

    # ------------------------------------------------------------- preenchimento
    def _preencher_contribuinte(self, page: Page, guia: Guia) -> None:
        emitente = guia.emitente
        municipio = emitente.municipio_nome or guia.municipio_ini_nome
        self._texto(page, "Nome ou Nome Empresarial do Contribuinte", emitente.nome)
        self._texto(page, "Endereço do Contribuinte", emitente.endereco_completo)
        self._texto(page, "Município do Contribuinte", municipio)
        nome_uf = NOMES_UF.get(emitente.uf, emitente.uf)
        self._multiselect(page, "UF do Contribuinte", nome_uf)
        self._texto(page, "Telefone do Contribuinte", self._telefone(emitente))
        if self._cfg("email"):
            self._texto(page, "E-mail", self._cfg("email"))

        vencimento = self._campo(page, "Vencimento Original").locator("input").first
        vencimento.fill(guia.vencimento.strftime("%d/%m/%Y"))
        vencimento.press("Tab")
        page.wait_for_timeout(700)

    def _preencher_operacao(self, page: Page, guia: Guia, data_pagamento: date) -> None:
        self._texto(page, "Número da Nota Fiscal", guia.numero_cte or guia.chave_cte[-9:])
        municipio = self._municipio_operacao(guia)
        if not municipio:
            raise FalhaPortal("PR: não foi possível identificar o município do remetente.")
        self._multiselect(page, "Município", municipio)

        campo_valor = self._campo(page, "Valor Principal (R$)").locator("input").first
        campo_valor.click()
        campo_valor.press("Control+A")
        page.keyboard.type(f"{guia.valor:.2f}".replace(".", ""))
        campo_valor.press("Tab")
        page.wait_for_timeout(900)

        total = self._campo(page, "Total a Recolher (R$)").locator("input").first.input_value()
        self.log(f"PR: total a recolher {total}", "info")

    def _preencher_destinatario(self, page: Page, guia: Guia) -> None:
        destinatario = guia.destinatario
        if not destinatario or not destinatario.documento:
            raise FalhaPortal("PR: o CT-e não traz o destinatário (CNPJ/CPF).")
        tipo = "CPF" if destinatario.cpf else "CNPJ"
        self._multiselect(page, "Tipo de Identificação do Destinatário", tipo)
        page.wait_for_timeout(900)
        identificacao = self._campo_ativo(page, tipo).locator("input").first
        identificacao.fill(destinatario.documento)
        identificacao.press("Tab")
        page.wait_for_timeout(700)
        self._texto(page, "Nome ou Nome Empresarial do Destinatário", destinatario.nome)
        self._texto(
            page,
            "Município do Destinatário",
            destinatario.municipio_nome or guia.municipio_fim_nome,
        )
        nome_uf = NOMES_UF.get(destinatario.uf, destinatario.uf)
        self._multiselect(page, "UF do Destinatário", nome_uf)

    def _municipio_operacao(self, guia: Guia) -> str:
        remetente = guia.remetente
        if remetente and remetente.uf == "PR" and remetente.municipio_nome:
            return remetente.municipio_nome
        return guia.municipio_ini_nome

    def _texto(self, page: Page, rotulo: str, valor: str) -> None:
        if not valor:
            return
        campo = self._campo(page, rotulo).locator("input, textarea").first
        campo.fill(str(valor))
        campo.press("Tab")
        page.wait_for_timeout(200)

    def _telefone(self, participante) -> str:
        bruto = participante.telefone or ""
        numero = re.sub(r"\D", "", bruto)
        if numero.startswith("0000") and len(numero) > 11:
            numero = numero[4:]
        if not numero:
            numero = re.sub(r"\D", "", str(self._cfg("telefone_fallback", "")))
        if len(numero) in (10, 11):
            return f"({numero[:2]}) {numero[2:-4]}-{numero[-4:]}"
        return numero

    # ---------------------------------------------------------------- conferência
    def _conferir_preenchimento(self, page: Page, guia: Guia) -> None:
        faltas = []
        for rotulo, esperado in (
            ("Nome ou Nome Empresarial do Contribuinte", guia.emitente.nome),
            ("Município do Destinatário", guia.destinatario.municipio_nome),
            ("Nome ou Nome Empresarial do Destinatário", guia.destinatario.nome),
        ):
            if not esperado:
                continue
            atual = self._campo(page, rotulo).locator("input, textarea").first.input_value()
            if normalizar(atual) != normalizar(esperado):
                faltas.append(f"{rotulo}: '{atual}' != '{esperado}'")

        nota = self._campo(page, "Número da Nota Fiscal").locator("input").first.input_value()
        esperado_nota = re.sub(r"\D", "", guia.numero_cte or "")
        if esperado_nota and re.sub(r"\D", "", nota).lstrip("0") != esperado_nota.lstrip("0"):
            faltas.append(f"Número da Nota Fiscal: '{nota}' != '{guia.numero_cte}'")

        escolhido = self._selecionado(self._campo(page, "Município"))
        esperado_municipio = self._municipio_operacao(guia)
        if normalizar(escolhido) != normalizar(esperado_municipio):
            faltas.append(f"Município: '{escolhido}' != '{esperado_municipio}'")

        valor = self._campo(page, "Valor Principal (R$)").locator("input").first.input_value()
        if guia.valor_br not in valor:
            faltas.append(f"valor: '{valor}' != '{guia.valor_br}'")
        if faltas:
            self.diagnostico(page, "divergencia")
            raise DivergenciaGuia("PR: campos divergem do XML: " + "; ".join(faltas))

    # ------------------------------------------------------------------- emissão
    def _emitir(self, page: Page) -> dict:
        try:
            with page.expect_response(
                lambda resposta: "/v1/emissao-grpr/emitir" in resposta.url,
                timeout=120000,
            ) as evento:
                self.clicar_texto(page, "Emitir Guia")
            resposta = evento.value
            dados = resposta.json() if resposta.ok else {}
        except PlaywrightTimeout as erro:
            self._captcha_ou_falha(page, "emissao")
            self.diagnostico(page, "emissao")
            raise FalhaPortal(
                "PR: o portal não respondeu à emissão. " + self.erro_na_pagina(page)
            ) from erro

        if dados.get("temErro") or dados.get("erro"):
            self.diagnostico(page, "emissao_erro")
            raise FalhaPortal("PR: " + self._mensagem_erro(dados, page))
        return dados

    def _mensagem_erro(self, dados: dict, page: Page) -> str:
        partes = []
        for chave in ("mensagem", "descricao", "msg"):
            if isinstance(dados.get(chave), str):
                partes.append(dados[chave])
        for erro in dados.get("lstErros") or dados.get("erros") or []:
            if isinstance(erro, dict):
                partes.append(str(erro.get("descricao") or erro.get("mensagem") or erro))
            else:
                partes.append(str(erro))
        if not partes:
            partes.append(self.erro_na_pagina(page) or "erro não detalhado pelo portal")
        return " | ".join(partes)[:600]

    def _link_da_guia(self, page: Page, dados: dict) -> str:
        candidatos = []
        for chave in ("linkGuia", "urlGuia", "link", "url"):
            valor = dados.get(chave)
            if isinstance(valor, str) and "emissao-grpr/html" in valor:
                candidatos.append(valor)
        if not candidatos:
            try:
                for ancora in page.locator("a[href*='emissao-grpr/html']").all():
                    href = ancora.get_attribute("href") or ""
                    if href:
                        candidatos.append(href)
            except Exception:
                pass
        if not candidatos:
            corpo = page.content()
            achado = re.search(r"/arrecadacao/api/v1/emissao-grpr/html/[A-Za-z0-9.\-]+", corpo)
            if achado:
                candidatos.append(achado.group(0))
        if not candidatos:
            return ""
        link = candidatos[0]
        if link.startswith("/"):
            link = "https://emitirgrpr.sefa.pr.gov.br" + link
        return link

    def _codigo_barras(self, link: str, dados: dict) -> str:
        for chave in ("codigoBarra", "codigoBarras", "codBarra", "numeroDocumento"):
            valor = dados.get(chave)
            if valor:
                return str(valor)
        return link.rstrip("/").rsplit("/", 1)[-1]

    def _capturar_pdf(self, page: Page, link: str) -> bytes:
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
            self.log(f"PR: falha ao renderizar a guia: {erro}", "aviso")
            return b""
        finally:
            pagina.close()
