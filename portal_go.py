"""Guias de ICMS-ST e FCP/PROTEGE de Goiás pelo portal ARR (DARE).

Goiás não usa a GNRE para estas operações: o recolhimento é feito no
Sistema de Arrecadação da Secretaria da Economia (arr.economia.go.gov.br),
gerando um DARE por receita:

- ICMS-ST ......... SUBSTITUIÇÃO TRIBUTÁRIA > 116 (Subst. Operação Anterior) > 40
- FCP/PROTEGE ..... PROTEGE > 4146 (Adicional ICMS 2% - Lei 15.505/2005) > 46 > 40

Cada DARE é um documento próprio; o FCP nunca vai junto com o ST.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

# Empacotado (EXE), o Playwright procuraria o navegador dentro da pasta
# temporária do próprio pacote. Fixamos a pasta padrão do usuário.
if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and os.environ.get("LOCALAPPDATA"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
        Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    )

URL_ARR = "https://arr.economia.go.gov.br/arr-www/view/entradaContribuinte.jsf"

RECEITA_ST = (
    "SUBSTITUIÇÃO TRIBUTÁRIA",
    "SUBSTITUIÇÃO TRIBUTÁRIA",
    "1 - ICMS - IMPOSTO CIRCUL",
    "116 - Subst. Operação Anterior",
    "40 - Instantâneo",
)
RECEITA_FCP = (
    "PROTEGE",
    "PROTEGE",
    "4146 - ADICIONAL ICMS",
    "46 - ADICIONAL ICMS 2% SUBSTITUI",
    "40 - Instantâneo",
)

_JS_EXPANDIR = """(texto) => {
  const lis=[...document.querySelectorAll('.ui-tree li')];
  for (const li of lis) {
    const no=li.querySelector(':scope > .ui-tree-node'); if(!no) continue;
    const lab=no.querySelector('.ui-tree-node-label'); if(!lab || !lab.textContent.trim().startsWith(texto)) continue;
    const r=lab.getBoundingClientRect(); if(!r.width || !r.height) continue;
    if(no.getAttribute('aria-expanded')!=='true'){ const ic=no.querySelector('.ui-tree-icon'); if(ic){ ic.click(); return 'expandido'; } }
    return 'ja-expandido';
  }
  return 'nao-encontrado';
}"""

_JS_SELECIONAR = """(texto) => {
  const lis=[...document.querySelectorAll('.ui-tree li')];
  for (const li of lis) {
    const no=li.querySelector(':scope > .ui-tree-node'); if(!no) continue;
    const lab=no.querySelector('.ui-tree-node-label'); if(!lab || !lab.textContent.trim().startsWith(texto)) continue;
    const r=lab.getBoundingClientRect(); if(!r.width || !r.height) continue;
    lab.click(); return 'selecionado';
  }
  return 'nao-encontrado';
}"""


class ErroPortalGo(RuntimeError):
    pass


def navegador_instalado() -> bool:
    """Informa se o Chromium do Playwright já está baixado nesta máquina."""
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


def instalar_navegador(log=print) -> None:
    """Baixa o Chromium do Playwright (uma vez por máquina)."""
    import os
    import subprocess

    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli = compute_driver_executable()
    log("Baixando o navegador do portal de Goiás (cerca de 170 MB)...")
    processo = subprocess.run(
        [node, cli, "install", "chromium"],
        env={**os.environ, **get_driver_env()},
        capture_output=True,
        text=True,
    )
    if processo.returncode != 0:
        raise ErroPortalGo(
            "Não foi possível baixar o navegador do portal: "
            + (processo.stderr or processo.stdout).strip()[-300:]
        )
    log("Navegador do portal pronto para uso.")


def _brl(valor: Decimal | float) -> str:
    texto = f"{Decimal(valor):,.2f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _digitos(texto: str) -> str:
    return "".join(c for c in str(texto or "") if c.isdigit())


def _abrir_menu(pagina: Page, nome: str) -> None:
    href = pagina.get_by_role("link", name=nome).first.get_attribute("href")
    if not href:
        raise ErroPortalGo(f"Não encontrei o serviço '{nome}' no portal de Goiás.")
    destino = href if href.startswith("http") else pagina.url.rsplit("/", 1)[0] + "/" + href
    pagina.goto(destino, wait_until="domcontentloaded", timeout=60000)
    pagina.wait_for_timeout(800)


def _js_preencher(pagina: Page, elemento: str, valor: str) -> None:
    pagina.evaluate(
        """([id, valor]) => {
            const el = document.getElementById(id);
            if (!el) return false;
            el.value = valor;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""",
        [elemento, valor],
    )


_IE_GO = re.compile(r"^\d{9}$")


def _contribuinte_goiano(destinatario) -> str:
    """IE goiana válida do destinatário; vazio quando não é contribuinte em GO."""
    ie = re.sub(r"\D", "", str(getattr(destinatario, "ie", "") or ""))
    uf = str(getattr(destinatario, "uf", "") or "").upper()
    indicador = str(getattr(destinatario, "ind_ie", "") or "1")
    if uf == "GO" and indicador in {"", "1"} and _IE_GO.fullmatch(ie):
        return ie
    return ""


_UF_GO_NOMES = {
    "AC": "ACRE",
    "AL": "ALAGOAS",
    "AP": "AMAPA",
    "AM": "AMAZONAS",
    "BA": "BAHIA",
    "CE": "CEARA",
    "DF": "DISTRITO FEDERAL",
    "ES": "ESPIRITO SANTO",
    "GO": "GOIAS",
    "MA": "MARANHAO",
    "MT": "MATO GROSSO",
    "MS": "MATO GROSSO DO SUL",
    "MG": "MINAS GERAIS",
    "PA": "PARA",
    "PB": "PARAIBA",
    "PR": "PARANA",
    "PE": "PERNAMBUCO",
    "PI": "PIAUI",
    "RJ": "RIO DE JANEIRO",
    "RN": "RIO GRANDE DO NORTE",
    "RS": "RIO GRANDE DO SUL",
    "RO": "RONDONIA",
    "RR": "RORAIMA",
    "SC": "SANTA CATARINA",
    "SP": "SAO PAULO",
    "SE": "SERGIPE",
    "TO": "TOCANTINS",
}


def _selecionar_uf(pagina: Page, uf: str) -> None:
    """Troca a UF pelo menu do portal; o select nativo fica oculto."""
    select = pagina.locator("#frmEmissao\\:apContribuinte\\:txtEstado_input")
    if select.input_value() == uf:
        return
    nome = _UF_GO_NOMES.get(uf)
    if not nome:
        raise ErroPortalGo(f"UF desconhecida para o portal de Goiás: {uf}.")
    pagina.locator(
        'input[name="frmEmissao:apContribuinte:txtEstado_editableInput"]'
    ).click()
    painel = pagina.locator("#frmEmissao\\:apContribuinte\\:txtEstado_panel")
    painel.wait_for(state="visible", timeout=15000)
    painel.get_by_text(nome, exact=True).click()
    try:
        pagina.wait_for_function(
            "(uf) => document.getElementById("
            "'frmEmissao:apContribuinte:txtEstado_input').value === uf",
            arg=uf,
            timeout=15000,
        )
    except PlaywrightTimeout as erro:
        raise ErroPortalGo(
            f"O portal de Goiás não aceitou a UF {uf}."
        ) from erro


def _clicar_nao_inscrito(pagina: Page) -> None:
    painel = pagina.locator("#frmEmissao\\:apContribuinte\\:tabNaoInscrito")
    if painel.is_visible():
        return
    pagina.get_by_role(
        "link", name="Não Inscrito no Cadastro de Contribuintes do Estado de Goiás"
    ).first.click()
    painel.wait_for(state="visible", timeout=15000)


def _preencher_nao_inscrito(pagina: Page, destinatario) -> str:
    _clicar_nao_inscrito(pagina)
    cnpj = re.sub(r"\D", "", str(getattr(destinatario, "cnpj", "") or ""))
    cpf = re.sub(r"\D", "", str(getattr(destinatario, "cpf", "") or ""))
    documento = cnpj or cpf
    if not documento:
        raise ErroPortalGo(
            "A NF-e não traz o CNPJ/CPF do destinatário; sem ele o portal de "
            "Goiás não emite o DARE como não inscrito."
        )
    pagina.locator(
        f'[id="frmEmissao:apContribuinte:radioCPFCNPJ:{0 if cnpj else 1}"]'
    ).check()
    pagina.wait_for_timeout(800)

    _js_preencher(pagina, "frmEmissao:apContribuinte:txtNumeroCPFCNPJ", documento)
    _js_preencher(
        pagina,
        "frmEmissao:apContribuinte:txtNomeRazaoSocial",
        str(getattr(destinatario, "nome", "") or "")[:80],
    )
    _js_preencher(
        pagina,
        "frmEmissao:apContribuinte:txtCEP",
        re.sub(r"\D", "", str(getattr(destinatario, "cep", "") or "")),
    )
    _js_preencher(
        pagina,
        "frmEmissao:apContribuinte:txtLogradouro",
        str(getattr(destinatario, "logradouro", "") or "")[:80],
    )
    _js_preencher(
        pagina,
        "frmEmissao:apContribuinte:txtNumero",
        str(getattr(destinatario, "numero", "") or "")[:8],
    )
    _js_preencher(
        pagina,
        "frmEmissao:apContribuinte:txtComplemento",
        str(getattr(destinatario, "complemento", "") or "")[:80],
    )
    _js_preencher(
        pagina,
        "frmEmissao:apContribuinte:txtBairro",
        str(getattr(destinatario, "bairro", "") or "")[:50],
    )

    uf = str(getattr(destinatario, "uf", "") or "GO").upper()
    _selecionar_uf(pagina, uf)

    municipio = str(getattr(destinatario, "municipio_nome", "") or "").strip()
    if municipio:
        campo = pagina.locator(
            "#frmEmissao\\:apContribuinte\\:acpMunicipio_input"
        )
        campo.click()
        campo.fill("")
        campo.press_sequentially(municipio, delay=80)
        itens = pagina.locator(
            "#frmEmissao\\:apContribuinte\\:acpMunicipio_panel li"
        )
        try:
            itens.first.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeout as erro:
            raise ErroPortalGo(
                f"O portal de Goiás não encontrou o município {municipio}."
            ) from erro
        alvo = itens.filter(has_text=municipio.upper()).first
        escolhido = alvo if alvo.count() else itens.first
        escolhido.click()
        pagina.wait_for_timeout(600)

    telefone = re.sub(r"\D", "", str(getattr(destinatario, "telefone", "") or ""))
    if len(telefone) >= 10:
        _js_preencher(pagina, "frmEmissao:apContribuinte:txtDddTelefone", telefone[:2])
        _js_preencher(
            pagina, "frmEmissao:apContribuinte:txtNumrTelefone", telefone[2:]
        )

    pagina.locator('[id="frmEmissao:btnContinueBaixo"]').click()
    return documento


def _informar_contribuinte(pagina: Page, destinatario) -> str:
    """Informa o contribuinte goiano ou declara o destinatário não inscrito."""
    inscricao = _contribuinte_goiano(destinatario)
    if inscricao:
        pagina.get_by_role(
            "link", name="Inscrito no Cadastro de Contribuintes do Estado de Goiás"
        ).first.click()
        pagina.wait_for_timeout(1000)
        _js_preencher(
            pagina, "frmEmissao:apContribuinte:txtInscricao", inscricao
        )
        pagina.get_by_role("button", name="Continue").click()
        rotulo = f"IE {inscricao}"
    else:
        documento = _preencher_nao_inscrito(pagina, destinatario)
        rotulo = f"não inscrito (CNPJ/CPF {documento})"
    try:
        pagina.wait_for_selector(".ui-tree .ui-tree-node-label", timeout=30000)
    except PlaywrightTimeout as erro:
        texto = pagina.locator("body").inner_text()
        raise ErroPortalGo(
            f"O portal de Goiás não aceitou o contribuinte ({rotulo}). "
            f"Retorno: {texto[-200:].strip()}"
        ) from erro
    return rotulo


def _escolher_receita(pagina: Page, caminho: tuple[str, ...]) -> None:
    for indice, texto in enumerate(caminho):
        ultimo = indice == len(caminho) - 1
        if ultimo:
            resultado = pagina.evaluate(_JS_SELECIONAR, texto)
            esperado = "selecionado"
        else:
            resultado = pagina.evaluate(_JS_EXPANDIR, texto)
            esperado = "expandido"
            if resultado == "ja-expandido":
                resultado = esperado
        if resultado != esperado:
            raise ErroPortalGo(f"Não encontrei a opção '{texto}' no portal de Goiás.")
        pagina.wait_for_timeout(450)
    pagina.get_by_role("button", name="Continue").click()
    pagina.wait_for_selector(
        '[id="frmEmissao:acInfoDocumento:txtValorOriginal"]', timeout=30000
    )


MESES = (
    "",
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
)


def _combo(pagina: Page, base_id: str, rotulo: str) -> None:
    """Escolhe uma opção no combo PrimeFaces pelo gatilho (o select é oculto)."""
    widget = pagina.locator(f'[id="{base_id}"]')
    painel = pagina.locator(f'[id="{base_id}_panel"]')
    widget.locator(".ui-selectonemenu-trigger").click()
    try:
        painel.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeout as erro:
        raise ErroPortalGo(
            f"O portal de Goiás não abriu a lista do campo {base_id}."
        ) from erro
    opcao = painel.locator("li.ui-selectonemenu-item", has_text=rotulo)
    if not opcao.count():
        raise ErroPortalGo(f"A opção '{rotulo}' não existe no campo {base_id}.")
    opcao.first.click()
    pagina.wait_for_timeout(300)
    valor = widget.locator(".ui-selectonemenu-label").input_value()
    if valor.strip() != rotulo:
        raise ErroPortalGo(
            f"O portal de Goiás não confirmou '{rotulo}' em {base_id} (ficou '{valor}')."
        )


def _data(pagina: Page, campo: str, valor: date) -> None:
    pagina.evaluate(
        """([campo, valor]) => {
            const el = document.getElementById(campo);
            if (!el) return;
            el.value = valor;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        [campo, valor.strftime("%d/%m/%Y")],
    )


def _preencher_documento(
    pagina: Page,
    *,
    numero: str,
    serie: str,
    chave: str,
    mes: int,
    ano: int,
    vencimento: date,
    pagamento: date,
    valor: Decimal,
    descricao: str,
) -> None:
    _combo(pagina, "frmEmissao:acInfoDocumento:cbMes", MESES[mes])
    _combo(pagina, "frmEmissao:acInfoDocumento:txtAno", str(ano))
    _combo(
        pagina,
        "frmEmissao:acInfoDocumento:txtNumeroDocumentoOrigemSelect",
        "NOTA FISCAL",
    )
    pagina.locator('[id="frmEmissao:acInfoDocumento:txtNumeroDocumentoOrigem"]').fill(
        numero
    )
    parcela = pagina.locator('[id="frmEmissao:acInfoDocumento:txtParcela"]')
    if parcela.count():
        parcela.fill("1")
    _data(pagina, "frmEmissao:acInfoDocumento:cdDataVencimento_input", vencimento)
    _data(pagina, "frmEmissao:acInfoDocumento:cdDataPagamento_input", pagamento)
    pagina.locator('[id="frmEmissao:acInfoDocumento:txtValorOriginal"]').fill(
        _brl(valor)
    )
    pagina.locator(
        '[id="frmEmissao:acInfoDocumento:txtInformacoesComplementares"]'
    ).fill(f"NF-e {numero}/{serie} - {descricao} chave {chave}")


def _codigo_dare(url: str) -> str | None:
    if "exibeDARE" not in url:
        return None
    consulta = urlsplit(url).query
    codigo = parse_qs(consulta).get("codigo", [""])[0]
    codigo = "".join(caractere for caractere in codigo if caractere.isdigit())
    if len(codigo) < 5 or set(codigo) == {"0"}:
        return None
    return codigo


def _validar_pdf_dare(caminho: Path) -> None:
    try:
        tamanho = caminho.stat().st_size
        with caminho.open("rb") as arquivo:
            inicio = arquivo.read(5)
    except OSError as erro:
        raise ErroPortalGo(f"Não foi possível ler o DARE baixado: {erro}.") from erro
    if inicio != b"%PDF-" or tamanho < 5000:
        raise ErroPortalGo(
            "O portal de Goiás gerou o DARE, mas o arquivo recebido não é o PDF "
            "da guia. Emita novamente; se repetir, use 'Portal: mostrar navegador'."
        )


def _url_dare(codigo: str) -> str:
    return f"https://arr.economia.go.gov.br/arr-www/view/exibeDARE.jsf?codigo={codigo}"


def _salvar_download(download, destino: Path, log, codigo: str | None) -> Path:
    try:
        download.save_as(str(destino))
    except Exception as erro:
        raise ErroPortalGo(
            f"O portal de Goiás iniciou o download do DARE, mas ele falhou: {erro}."
        ) from erro
    _validar_pdf_dare(destino)
    if codigo:
        log(f"DARE {codigo} recebido do portal de Goiás.")
    else:
        log("PDF do DARE recebido do portal de Goiás.")
    return destino


def _gerar_dare(pagina: Page, destino: Path, log) -> Path:
    """Clica em 'Gerar DARE' e guarda o PDF que o ARR entrega.

    O ARR abre um popup ``exibeDARE.jsf?codigo=<DARE>`` e dispara o download
    ``dare.pdf``. A tela que fica na aba principal é só a confirmação do
    pedido — jamais deve ser salva como guia.
    """
    contexto = pagina.context
    downloads: list = []
    novas: list[Page] = []
    # O Playwright não aceita builtins (list.append) como listener.
    contexto.on("download", lambda download: downloads.append(download))
    contexto.on("page", lambda nova: novas.append(nova))
    try:
        pagina.get_by_role("button", name="Gerar DARE").click(timeout=60000)
    except PlaywrightTimeout as erro:
        raise ErroPortalGo(
            "O portal de Goiás não respondeu ao 'Gerar DARE'. Emita novamente; "
            "se repetir, use 'Portal: mostrar navegador'."
        ) from erro
    except Exception as erro:
        if "download" not in str(erro).lower():
            raise

    codigo: str | None = None
    codigo_em = 0.0
    prazo = time.monotonic() + 90
    while time.monotonic() < prazo and not downloads:
        for nova in novas:
            if codigo is None:
                codigo = _codigo_dare(nova.url)
                if codigo:
                    codigo_em = time.monotonic()
        if codigo and time.monotonic() - codigo_em > 5:
            # O popup pode não refazer o download: pedimos o mesmo DARE por ele.
            break
        pagina.wait_for_timeout(300)

    if downloads:
        for nova in novas:
            codigo = codigo or _codigo_dare(nova.url)
        return _salvar_download(downloads[0], destino, log, codigo)

    if codigo:
        alvo = contexto.new_page()
        try:
            with alvo.expect_download(timeout=60000) as espera:
                try:
                    alvo.goto(_url_dare(codigo), wait_until="domcontentloaded", timeout=60000)
                except Exception as erro:
                    if "download" not in str(erro).lower():
                        raise
            return _salvar_download(espera.value, destino, log, codigo)
        except PlaywrightTimeout as erro:
            raise ErroPortalGo(
                f"O portal de Goiás abriu o DARE {codigo}, mas não entregou o PDF. "
                "Emita novamente ou baixe pelo navegador visível."
            ) from erro
        finally:
            alvo.close()

    texto = pagina.locator("body").inner_text().lower()
    if "captcha" in texto:
        raise ErroPortalGo(
            "O portal de Goiás pediu captcha. Use 'Portal: mostrar navegador' "
            "e emita novamente para concluir a etapa manualmente."
        )
    raise ErroPortalGo(
        "O portal de Goiás gerou o pedido, mas não entregou o PDF do DARE. "
        "A tela de confirmação não serve como guia; emita novamente pelo portal."
    )


def _emitir_uma(
    dados,
    *,
    caminho_receita: tuple[str, ...],
    valor: Decimal,
    rotulo: str,
    vencimento: date,
    pagamento: date,
    visivel: bool,
    destino: Path,
    log,
) -> Path:
    destinatario = dados.destinatario
    if destinatario is None:
        raise ErroPortalGo(
            "A NF-e não traz os dados do destinatário; sem eles o portal de "
            "Goiás não emite o DARE."
        )
    if not (destinatario.cnpj or destinatario.cpf or _contribuinte_goiano(destinatario)):
        raise ErroPortalGo(
            "A NF-e não traz CNPJ/CPF nem inscrição estadual do destinatário; "
            "sem isso o portal de Goiás não emite o DARE."
        )
    if valor <= 0:
        raise ErroPortalGo(f"O valor de {rotulo} está zerado.")

    with sync_playwright() as p:
        try:
            navegador = p.chromium.launch(
                headless=not visivel, args=["--start-maximized"]
            )
        except Exception as erro:
            if "Executable doesn't exist" in str(erro):
                raise ErroPortalGo(
                    "O navegador do portal de Goiás não está instalado neste "
                    "computador. Rode 'playwright install chromium' uma vez."
                ) from erro
            raise
        contexto = navegador.new_context(
            viewport=None if visivel else {"width": 1280, "height": 900},
            accept_downloads=True,
        )
        pagina = contexto.new_page()
        try:
            log(f"Portal de Goiás: abrindo o DARE de {rotulo}...")
            pagina.goto(URL_ARR, wait_until="domcontentloaded", timeout=60000)
            _abrir_menu(pagina, caminho_receita[0])
            contribuinte = _informar_contribuinte(pagina, destinatario)
            log(f"Contribuinte informado no portal de Goiás: {contribuinte}.")
            _escolher_receita(pagina, caminho_receita[1:])
            emissao = dados.dh_emi.date()
            _preencher_documento(
                pagina,
                numero=dados.numero,
                serie=dados.serie,
                chave=dados.chave,
                mes=emissao.month,
                ano=date.today().year,
                vencimento=vencimento,
                pagamento=pagamento,
                valor=valor,
                descricao=rotulo,
            )
            log(
                f"DARE de {rotulo} preenchido: R$ {_brl(valor)}, "
                f"referência {emissao.month:02d}/{date.today().year}."
            )
            destino.parent.mkdir(parents=True, exist_ok=True)
            return _gerar_dare(pagina, destino, log)
        finally:
            if visivel:
                pagina.wait_for_timeout(1500)
            contexto.close()
            navegador.close()


def emitir_go(
    dados,
    *,
    vencimento: date,
    pagamento: date,
    visivel: bool,
    saida_dir: Path,
    log,
) -> list[dict]:
    """Emite os DAREs de ST e FCP/PROTEGE e devolve os arquivos gerados."""
    base = f"Guia - GO - NFE {dados.numero}"
    guias: list[dict] = []
    etapas = [
        ("ICMS-ST", RECEITA_ST, dados.valor_st, f"{base}.pdf"),
        (
            "PROTEGE (FCP)",
            RECEITA_FCP,
            dados.valor_fcp,
            f"{base} - PROTEGE.pdf",
        ),
    ]
    for rotulo, receita, valor, nome in etapas:
        if valor <= 0:
            log(f"{rotulo}: sem valor nesta NF-e; guia não emitida.")
            continue
        destino = _emitir_uma(
            dados,
            caminho_receita=receita,
            valor=valor,
            rotulo=rotulo,
            vencimento=vencimento,
            pagamento=pagamento,
            visivel=visivel,
            destino=saida_dir / nome,
            log=log,
        )
        guias.append({"rotulo": rotulo, "valor": valor, "pdf": destino})
    if not guias:
        raise ErroPortalGo("A NF-e não tem ICMS-ST nem FCP para recolher em Goiás.")
    return guias
