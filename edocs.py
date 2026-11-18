"""XML da NF-e pelo portal Edocs (Senior), empresa 01 - ESKIMO.

A Distribuição DF-e da SEFAZ não entrega ao emitente o XML da NF-e da própria
empresa (cStat 641) e às vezes nem tem o XML de notas de transferência. O
Edocs guarda o XML autorizado de todas as notas da empresa e permite baixá-lo
pelo número; a conta do portal fica em edocs.json com a senha protegida pelo
DPAPI.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable

import requests

from seguranca import ErroSeguranca, proteger, revelar

URL_PADRAO = "http://10.15.1.140/EdocsProducao"
EMPRESA_PADRAO = "ESKIMO"
ARQUIVO_CONTA = "edocs.json"
TEMPO = 25
PAGINA = "Nfe/NFe.aspx"


class ErroEdocs(RuntimeError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


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
        if _local(elemento.tag) == "infNFe":
            chave = re.sub(r"\D", "", elemento.get("Id") or "")
            if len(chave) == 44:
                return chave
    for elemento in raiz.iter():
        if _local(elemento.tag) == "chNFe":
            chave = re.sub(r"\D", "", elemento.text or "")
            if len(chave) == 44:
                return chave
    return ""


# ----------------------------------------------------------------------
# conta do portal
# ----------------------------------------------------------------------


def _ler_conta(caminho: Path) -> dict | None:
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


def carregar_conta(
    pasta: str | Path, alternativas: Iterable[str | Path] = ()
) -> dict | None:
    """Conta do Edocs da pasta do app; se não houver, tenta as alternativas."""
    caminhos = [Path(pasta) / ARQUIVO_CONTA]
    caminhos += [Path(alt) / ARQUIVO_CONTA for alt in alternativas]
    for caminho in caminhos:
        conta = _ler_conta(caminho)
        if conta is not None:
            return conta
    return None


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
    caminho.parent.mkdir(parents=True, exist_ok=True)
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


def _filtro(campo: str, valor: str) -> dict:
    return {
        "FilterType": None,
        "FiltroIntervalo": False,
        "Name": None,
        "Value": None,
        "ValueType": None,
        "campo": campo,
        "operacao": "=",
        "primeiroValor": valor,
        "segundoValor": None,
    }


class PortalEdocs:
    def __init__(self, conta: dict, log: Callable[[str], None] | None = None):
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

    def _pagina(self, caminho: str = PAGINA) -> str:
        return f"{self.url}/{caminho.lstrip('/')}"

    def _cabecalhos(self) -> dict[str, str]:
        return {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self._pagina(),
            "Origin": self.url,
        }

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
            {"txbLogin": self.usuario, "txbSenha": self.senha, "btnLogin": "Entrar"}
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
                "O Edocs recusou o usuário ou a senha da conta salva."
            )

    def _chamar(self, metodo: str, corpo: dict) -> object:
        if self.sessao is None:
            raise ErroEdocs("Portal usado fora do contexto 'with'.")
        try:
            resposta = self.sessao.post(
                f"{self._pagina()}/{metodo}",
                json=corpo,
                headers=self._cabecalhos(),
                timeout=TEMPO * 2,
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
                    f"{self._pagina()}/TrocarEmpresa",
                    json={"sequencialEmpresa": int(alvo.get("SequenciaEmpresa") or 0)},
                    headers=self._cabecalhos(),
                    timeout=TEMPO,
                )
                self.sessao.get(self._pagina(), timeout=TEMPO)
            except (requests.RequestException, ValueError) as erro:
                raise ErroEdocs(
                    f"Falha ao trocar para a empresa {self.empresa}: {erro}"
                ) from erro
        self._registrar(f"Edocs: empresa {self.empresa} selecionada.")

    def _consultar(self, numero: str) -> list[dict]:
        controle = {
            "IdentificadorGrid": "nfeGridInfo",
            "IdentificadorPager": "MainContent_tctAbasEmissaoNfe_tabNfe_pgrNfe",
            "IdentificadorPainelFiltros": None,
            "IdentificadorPainelMensagens": "messagePanelNfe",
            "NomeWebMethodCarregamentoGrid": "PageMethods.ObterNotas",
            "NomeCompletoClasseMetodoCarregamento": None,
            "Paginacao": {"linhaInicial": 0, "numeroRegistrosPorPagina": 20},
            "Ordenacao": {"colunaOrdenacao": "DataEmissao", "tipoOrdenacao": "DESC"},
            "Filtros": [
                _filtro("NumeroNfe", numero),
                _filtro("Filial.Sequencia", ""),
                _filtro("TipoProcessamento", "E"),
            ],
            "IdentificadorUnicoGrid": "grid_nfe_emissao",
            "IdentificadorUnicoPagina": "nfe_emissao",
            "Carregando": False,
        }
        dados = self._chamar("ObterNotas", {"controleGrid": controle})
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
            if str(linha.get("IND_STATUS_NFE_DESC") or "").lower() == "autorizada"
        ]
        candidatos = autorizados or validos or linhas
        if not candidatos:
            return None
        return max(candidatos, key=lambda linha: int(linha.get("SequenciaNota") or 0))

    def _baixar(self, sequencia: str) -> bytes:
        if self.sessao is None:
            raise ErroEdocs("Portal usado fora do contexto 'with'.")
        endereco = self._pagina(
            "DocumentoAuxiliar/XmlDocumentoAuxiliar.ashx"
            f"?TipoDocumento=1&Chave={sequencia}&TipoOperacao=1"
            "&FuncaoAcesso=nfe_emissao_xml"
        )
        try:
            resposta = self.sessao.get(
                endereco,
                timeout=TEMPO * 3,
                headers={"Referer": self._pagina()},
            )
        except requests.RequestException as erro:
            raise ErroEdocs(f"Falha ao baixar o XML do Edocs: {erro}") from erro
        if resposta.status_code != 200 or not resposta.content.startswith(b"<"):
            raise ErroEdocs(
                "O Edocs não liberou o XML desta NF-e. "
                f"(HTTP {resposta.status_code})"
            )
        return resposta.content

    def obter(self, numero: str, pasta_xmls: str | Path) -> tuple[str, Path]:
        """Localiza a NF-e pelo número e grava o XML em pasta_xmls."""
        numero = re.sub(r"\D", "", numero or "").lstrip("0") or "0"
        linhas = self._consultar(numero)
        if not linhas:
            raise ErroEdocs(
                f"NF-e nº {numero} não encontrada no Edocs "
                f"({self.empresa or 'empresa não selecionada'})."
            )
        linha = self._escolher(linhas)
        if linha is None:
            raise ErroEdocs(f"NF-e nº {numero} sem documento válido no Edocs.")
        situacao = str(linha.get("IND_STATUS_NFE_DESC") or "")
        if not linha.get("DocumentoValido"):
            raise ErroEdocs(
                f"NF-e nº {numero} está como {situacao or 'não autorizada'} no "
                "Edocs; não é possível emitir a GNRE."
            )
        chave = re.sub(r"\D", "", str(linha.get("Chave") or ""))
        if len(chave) != 44:
            raise ErroEdocs(
                f"O Edocs não informou a chave da NF-e nº {numero}."
            )
        sequencia = str(linha.get("SEQ_NFE") or "").strip()
        if not sequencia:
            raise ErroEdocs(
                f"O Edocs não informou o identificador da NF-e nº {numero}."
            )
        conteudo = self._baixar(sequencia)
        chave_xml = _chave_do_xml(conteudo)
        if not chave_xml:
            raise ErroEdocs(
                f"O arquivo devolvido pelo Edocs para a NF-e nº {numero} "
                "não é um XML de NF-e."
            )
        if chave_xml != chave:
            raise ErroEdocs(
                f"O XML devolvido pelo Edocs não corresponde à NF-e nº {numero} "
                f"(chave {chave_xml})."
            )
        destino = Path(pasta_xmls) / f"{chave}.xml"
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(conteudo)
        self._registrar(
            f"Edocs: NF-e nº {numero} série {linha.get('B_SERIE')} baixada "
            f"({situacao or 'situação não informada'})."
        )
        return chave, destino
