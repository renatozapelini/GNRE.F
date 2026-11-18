"""XML do CT-e pelo portal Edocs (Senior), usado quando a SEFAZ não entrega.

A Distribuição DF-e nem sempre libera o XML do CT-e para o CNPJ do
certificado, mesmo com o documento autorizado e a Eskimo como destinatária.
O Edocs guarda o XML autorizado e permite baixá-lo pelo número; a conta do
portal fica em ConfGNRE/edocs.json com a senha protegida pelo DPAPI.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

import requests

from seguranca import ErroSeguranca, proteger, revelar

URL_PADRAO = "http://10.15.1.140/EdocsProducao"
EMPRESA_PADRAO = "TRANSREIS"
ARQUIVO_CONTA = "edocs.json"
TEMPO = 25


class ErroEdocs(RuntimeError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def numero_e_serie_da_chave(chave: str) -> tuple[str, str]:
    chave = re.sub(r"\D", "", chave or "")
    if len(chave) != 44:
        return "", ""
    return chave[25:34].lstrip("0"), chave[22:25].lstrip("0")


def _campos_ocultos(html: str) -> dict[str, str]:
    campos: dict[str, str] = {}
    for achado in re.finditer(r"<input[^>]*type=\"hidden\"[^>]*>", html, re.I):
        tag = achado.group(0)
        nome = re.search(r"name=\"([^\"]+)\"", tag)
        valor = re.search(r"value=\"([^\"]*)\"", tag)
        if nome:
            campos[nome.group(1)] = valor.group(1) if valor else ""
    return campos


def _chave_do_xml(conteudo: bytes) -> str:
    try:
        raiz = ET.fromstring(conteudo)
    except ET.ParseError:
        return ""
    for elemento in raiz.iter():
        if _local(elemento.tag) == "infCte":
            chave = re.sub(r"\D", "", elemento.get("Id") or "")
            if len(chave) == 44:
                return chave
    for elemento in raiz.iter():
        if _local(elemento.tag) == "chCTe":
            chave = re.sub(r"\D", "", elemento.text or "")
            if len(chave) == 44:
                return chave
    return ""


# ----------------------------------------------------------------------
# conta do portal
# ----------------------------------------------------------------------


def carregar_conta(pasta: str | Path) -> dict | None:
    caminho = Path(pasta) / ARQUIVO_CONTA
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    usuario = str(dados.get("usuario") or "").strip()
    protegida = str(dados.get("senha") or "").strip()
    if not usuario or not protegida:
        return None
    try:
        senha = revelar(protegida)
    except ErroSeguranca:
        return None
    return {
        "url": str(dados.get("url") or URL_PADRAO).strip().rstrip("/"),
        "usuario": usuario,
        "senha": senha,
        "arquivo": caminho,
    }


def salvar_conta(
    pasta: str | Path, usuario: str, senha: str, url: str = URL_PADRAO
) -> Path:
    usuario = (usuario or "").strip()
    if not usuario:
        raise ErroEdocs("Informe o usuário do portal Edocs.")
    if not senha:
        raise ErroEdocs("Informe a senha do portal Edocs.")
    caminho = Path(pasta) / ARQUIVO_CONTA
    conteudo = {
        "url": (url or URL_PADRAO).strip().rstrip("/"),
        "usuario": usuario,
        "senha": proteger(senha),
    }
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(
        json.dumps(conteudo, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporario.replace(caminho)
    return caminho


def remover_conta(pasta: str | Path) -> None:
    try:
        (Path(pasta) / ARQUIVO_CONTA).unlink()
    except FileNotFoundError:
        pass


# ----------------------------------------------------------------------
# portal
# ----------------------------------------------------------------------


class PortalEdocs:
    def __init__(
        self,
        conta: dict,
        log: Callable[[str], None] | None = None,
    ):
        self.url = str(conta.get("url") or URL_PADRAO).rstrip("/")
        self.usuario = conta.get("usuario") or ""
        self.senha = conta.get("senha") or ""
        self.log = log
        self.sessao: requests.Session | None = None
        self.empresa = ""

    def __enter__(self) -> "PortalEdocs":
        self.sessao = requests.Session()
        self._entrar()
        self._escolher_empresa()
        return self

    def __exit__(self, *_exc) -> None:
        if self.sessao is not None:
            self.sessao.close()
            self.sessao = None

    def _registrar(self, mensagem: str) -> None:
        if self.log:
            self.log(mensagem)

    def _pagina(self, caminho: str) -> str:
        if self.sessao is None:
            raise ErroEdocs("Portal usado fora do contexto 'with'.")
        return f"{self.url}/{caminho.lstrip('/')}"

    def _entrar(self) -> None:
        if self.sessao is None:
            raise ErroEdocs("Portal usado fora do contexto 'with'.")
        try:
            resposta = self.sessao.get(self._pagina("Login.aspx"), timeout=TEMPO)
        except requests.RequestException as erro:
            raise ErroEdocs(
                f"Portal Edocs inacessível em {self.url}: {erro}"
            ) from erro
        resposta.encoding = "utf-8"
        campos = _campos_ocultos(resposta.text)
        if "txbLogin" not in resposta.text or not campos:
            raise ErroEdocs("Página de login do Edocs não reconhecida.")
        campos.update(
            {
                "txbLogin": self.usuario,
                "txbSenha": self.senha,
                "btnLogin": "Entrar",
            }
        )
        try:
            resposta = self.sessao.post(
                self._pagina("Login.aspx"),
                data=campos,
                timeout=TEMPO,
                allow_redirects=True,
            )
        except requests.RequestException as erro:
            raise ErroEdocs(f"Falha ao entrar no Edocs: {erro}") from erro
        if "Login.aspx" in resposta.url:
            raise ErroEdocs(
                "O Edocs recusou o usuário ou a senha. Confira a conta em "
                "menu → Conta do portal Edocs."
            )

    def _chamar(self, metodo: str, corpo: dict) -> object:
        if self.sessao is None:
            raise ErroEdocs("Portal usado fora do contexto 'with'.")
        try:
            resposta = self.sessao.post(
                self._pagina(f"Cte/Cte.aspx/{metodo}"),
                json=corpo,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self._pagina("Cte/Cte.aspx"),
                    "Origin": self.url,
                },
                timeout=TEMPO,
            )
        except requests.RequestException as erro:
            raise ErroEdocs(f"Falha de conexão com o Edocs: {erro}") from erro
        if resposta.status_code != 200:
            raise ErroEdocs(
                f"O Edocs recusou a consulta ({resposta.status_code})."
            )
        try:
            return resposta.json().get("d")
        except ValueError as erro:
            raise ErroEdocs("Resposta inesperada do Edocs.") from erro

    def _escolher_empresa(self) -> None:
        menu = self._chamar("ObterMenuUsuario", {})
        if not isinstance(menu, list) or not menu:
            raise ErroEdocs("Não foi possível listar as empresas do Edocs.")
        alvo = None
        for item in menu:
            nome = str(item.get("Nome") or "")
            if EMPRESA_PADRAO in nome.upper():
                alvo = item
                break
        if alvo is None:
            raise ErroEdocs(
                f"A conta do Edocs não tem acesso à empresa {EMPRESA_PADRAO}."
            )
        self.empresa = str(alvo.get("Nome") or "")
        if self.sessao is not None:
            try:
                self.sessao.post(
                    self._pagina("Cte/Cte.aspx/TrocarEmpresa"),
                    json={"sequencialEmpresa": int(alvo.get("SequenciaEmpresa") or 0)},
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": self._pagina("Cte/Cte.aspx"),
                        "Origin": self.url,
                    },
                    timeout=TEMPO,
                )
                self.sessao.get(self._pagina("Cte/Cte.aspx"), timeout=TEMPO)
            except (requests.RequestException, ValueError) as erro:
                raise ErroEdocs(f"Falha ao trocar para a empresa {self.empresa}: {erro}") from erro
        self._registrar(f"Edocs: empresa {self.empresa} selecionada.")

    def _consultar(self, numero: str, serie: str = "") -> list[dict]:
        filtros = [
            {
                "campo": "Numero",
                "operacao": "=",
                "primeiroValor": str(numero),
            }
        ]
        if serie:
            filtros.append(
                {
                    "campo": "Serie",
                    "operacao": "=",
                    "primeiroValor": str(serie),
                }
            )
        controle = {
            "IdentificadorGrid": "gridCte",
            "IdentificadorPager": "MainContent_TabContainer1_TabPanel1_Pager1",
            "IdentificadorPainelFiltros": None,
            "IdentificadorPainelMensagens": "message",
            "NomeWebMethodCarregamentoGrid": "PageMethods.ObterConhecimentosTransporte",
            "NomeCompletoClasseMetodoCarregamento": None,
            "Paginacao": {"linhaInicial": 0, "numeroRegistrosPorPagina": 10},
            "Ordenacao": {
                "colunaOrdenacao": "Numero, SequenciaConhecimentoFrete",
                "tipoOrdenacao": "DESC",
            },
            "Filtros": filtros,
            "IdentificadorUnicoGrid": "grid_cte_emissao",
            "IdentificadorUnicoPagina": "cte_emissao",
            "Carregando": False,
        }
        dados = self._chamar("ObterConhecimentosTransporte", {"controle": controle})
        if not isinstance(dados, dict):
            return []
        linhas = dados.get("Rows") or []
        return [linha for linha in linhas if isinstance(linha, dict)]

    @staticmethod
    def _escolher(linhas: list[dict]) -> dict | None:
        validos = [linha for linha in linhas if linha.get("DocumentoValido")]
        autorizados = [
            linha
            for linha in validos
            if str(linha.get("SituacaoDescricao") or "").lower() == "autorizado"
        ]
        candidatos = autorizados or validos or linhas
        if not candidatos:
            return None
        return max(
            candidatos,
            key=lambda linha: (
                int(linha.get("SequenciaCte") or 0),
                str(linha.get("Emissao") or ""),
            ),
        )

    def _baixar(self, sequencia: int) -> bytes:
        if self.sessao is None:
            raise ErroEdocs("Portal usado fora do contexto 'with'.")
        endereco = self._pagina(
            "DocumentoAuxiliar/XmlDocumentoAuxiliar.ashx"
            f"?TipoDocumento=3&Chave={int(sequencia)}"
            "&TipoOperacao=1&FuncaoAcesso=cte_emissao_xml"
        )
        try:
            resposta = self.sessao.get(
                endereco,
                timeout=TEMPO * 2,
                headers={"Referer": self._pagina("Cte/Cte.aspx")},
            )
        except requests.RequestException as erro:
            raise ErroEdocs(f"Falha ao baixar o XML do Edocs: {erro}") from erro
        if resposta.status_code != 200 or not resposta.content.startswith(b"<"):
            raise ErroEdocs(
                "O Edocs não liberou o XML deste CT-e. "
                f"(HTTP {resposta.status_code})"
            )
        return resposta.content

    def obter(
        self,
        numero: str,
        pasta_xmls: str | Path,
        serie: str = "",
        chave_esperada: str = "",
    ) -> tuple[str, Path]:
        """Localiza o CT-e pelo número e grava o XML em pasta_xmls."""
        numero = re.sub(r"\D", "", numero or "").lstrip("0") or "0"
        serie = re.sub(r"\D", "", serie or "").lstrip("0")
        linhas = self._consultar(numero, serie)
        if not linhas:
            sufixo = f" série {serie}" if serie else ""
            raise ErroEdocs(
                f"CT-e nº {numero}{sufixo} não encontrado no Edocs "
                f"({self.empresa or 'empresa não selecionada'})."
            )
        linha = self._escolher(linhas)
        if linha is None:
            raise ErroEdocs(f"CT-e nº {numero} sem documento válido no Edocs.")
        situacao = str(linha.get("SituacaoDescricao") or "")
        if not linha.get("DocumentoValido"):
            raise ErroEdocs(
                f"CT-e nº {numero} está como {situacao or 'não autorizado'} no "
                "Edocs; não é possível emitir a GNRE."
            )
        chave = re.sub(r"\D", "", str(linha.get("Chave") or ""))
        if chave_esperada and chave and chave != chave_esperada:
            raise ErroEdocs(
                f"O Edocs devolveu o CT-e nº {numero} com outra chave "
                f"({chave}); confira o número digitado."
            )
        if not chave:
            codigo = str(linha.get("Chave") or "")
            raise ErroEdocs(
                f"O Edocs não informou a chave do CT-e nº {numero} "
                f"(documento {codigo})."
            )
        sequencia = int(linha.get("SequenciaCte") or 0)
        if sequencia <= 0:
            raise ErroEdocs(f"O Edocs não informou o identificador do CT-e nº {numero}.")
        conteudo = self._baixar(sequencia)
        chave_xml = _chave_do_xml(conteudo)
        if not chave_xml:
            raise ErroEdocs(
                f"O arquivo devolvido pelo Edocs para o CT-e nº {numero} não é um XML de CT-e."
            )
        if chave_xml != chave:
            raise ErroEdocs(
                f"O XML devolvido pelo Edocs não corresponde ao CT-e nº {numero} "
                f"(chave {chave_xml})."
            )
        destino = Path(pasta_xmls) / f"{chave}.xml"
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(conteudo)
        self._registrar(
            f"Edocs: CT-e nº {numero} série {linha.get('Serie')} baixado "
            f"({situacao or 'situação não informada'})."
        )
        return chave, destino

    def obter_por_chave(self, chave: str, pasta_xmls: str | Path) -> tuple[str, Path]:
        chave = re.sub(r"\D", "", chave or "")
        numero, serie = numero_e_serie_da_chave(chave)
        if not numero:
            raise ErroEdocs("Chave do CT-e inválida.")
        return self.obter(numero, pasta_xmls, serie=serie, chave_esperada=chave)
