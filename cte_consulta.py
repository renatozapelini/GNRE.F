"""Busca o CT-e autorizado pela chave na Distribuição DF-e do CT-e.

O serviço oficial (CTeDistribuicaoDFe) não consulta por chave: ele entrega
os documentos em que o certificado participa por NSU. A busca então sincroniza
o fluxo de NSUs com ponto de parada persistido, guarda os CT-es em disco e
consulta a janela recente (a SEFAZ limita a quantidade de requisições).
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

from certificado import Certificado, preparar_pem_temporario
from cte_leitor import validar_chave

URLS = {
    "1": "https://www1.cte.fazenda.gov.br/CTeDistribuicaoDFe/CTeDistribuicaoDFe.asmx",
    "2": "https://hom1.cte.fazenda.gov.br/CTeDistribuicaoDFe/CTeDistribuicaoDFe.asmx",
}
NS_SERVICO = "http://www.portalfiscal.inf.br/cte/wsdl/CTeDistribuicaoDFe"
NS_DADOS = "http://www.portalfiscal.inf.br/cte"
SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"

JANELA_INICIAL = 600
PAGINAS_MAX = 12
PAUSA_PAGINA = 1.5


class ErroDistribuicaoCTe(RuntimeError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _filhos(elemento: ET.Element, nome: str) -> list[ET.Element]:
    return [filho for filho in elemento.iter() if _local(filho.tag) == nome]


def _achar(elemento: ET.Element | None, nome: str) -> ET.Element | None:
    if elemento is None:
        return None
    for filho in elemento.iter():
        if _local(filho.tag) == nome:
            return filho
    return None


def _texto(elemento: ET.Element | None, nome: str) -> str:
    achado = _achar(elemento, nome)
    return (achado.text or "").strip() if achado is not None else ""


def _chave_do_infcte(raiz: ET.Element) -> str:
    inf = _achar(raiz, "infCte")
    if inf is not None:
        chave = re.sub(r"\D", "", inf.get("Id") or "")
        if len(chave) == 44:
            return chave
    chave = re.sub(r"\D", "", _texto(raiz, "chCTe"))
    return chave if len(chave) == 44 else ""


def _cancelamento_autorizado(raiz: ET.Element) -> bool:
    if _texto(raiz, "tpEvento") != "110111":
        return False
    status = _texto(raiz, "cStat")
    return status in ("", "135", "155")


class DistribuicaoCTe:
    def __init__(
        self,
        cert: Certificado,
        pasta: str | Path,
        ambiente: str = "2",
        uf_autor: str = "42",
        timeout: int = 60,
        verificar_ssl: bool = True,
        log: Callable[[str], None] | None = None,
    ):
        if ambiente not in URLS:
            raise ErroDistribuicaoCTe(f"Ambiente inválido: {ambiente!r}.")
        self.cert = cert
        self.pasta = Path(pasta)
        self.ambiente = ambiente
        self.uf_autor = str(uf_autor)
        self.timeout = timeout
        self.verificar_ssl = verificar_ssl
        self.log = log
        self._pem = None
        self._sessao: requests.Session | None = None
        self.estado_path = self.pasta / f"distribuicao_{ambiente}.json"

    def __enter__(self) -> "DistribuicaoCTe":
        self.pasta.mkdir(parents=True, exist_ok=True)
        self._pem = preparar_pem_temporario(self.cert)
        self._sessao = requests.Session()
        self._sessao.cert = (str(self._pem.cert_path), str(self._pem.key_path))
        self._sessao.verify = self.verificar_ssl
        adaptador = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2)
        self._sessao.mount("https://", adaptador)
        return self

    def __exit__(self, *_exc) -> None:
        if self._sessao is not None:
            self._sessao.close()
            self._sessao = None
        if self._pem is not None:
            self._pem.limpar()
            self._pem = None

    def _registrar(self, mensagem: str) -> None:
        if self.log:
            self.log(mensagem)

    # ------------------------------------------------------------------
    # estado da varredura
    # ------------------------------------------------------------------

    def _carregar_estado(self) -> dict:
        try:
            dados = json.loads(self.estado_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            dados = {}
        return {
            "ult_nsu": int(dados.get("ult_nsu") or 0),
            "iniciado": bool(dados.get("iniciado")),
            "cancelados": set(str(item) for item in dados.get("cancelados") or []),
            "eventos": [str(item) for item in dados.get("eventos") or []],
            "atualizado": dados.get("atualizado"),
        }

    def _salvar_estado(self, estado: dict) -> None:
        conteudo = {
            "ult_nsu": int(estado["ult_nsu"]),
            "iniciado": bool(estado["iniciado"]),
            "cancelados": sorted(estado["cancelados"]),
            "eventos": estado["eventos"][-500:],
            "atualizado": datetime.now().isoformat(timespec="seconds"),
        }
        temporario = self.estado_path.with_suffix(".tmp")
        temporario.write_text(
            json.dumps(conteudo, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporario.replace(self.estado_path)

    # ------------------------------------------------------------------
    # transporte
    # ------------------------------------------------------------------

    def _payload(self, ult_nsu: int) -> str:
        return (
            f'<distDFeInt xmlns="{NS_DADOS}" versao="1.00">'
            f"<tpAmb>{self.ambiente}</tpAmb>"
            f"<cUFAutor>{self.uf_autor}</cUFAutor>"
            f"<CNPJ>{self.cert.cnpj}</CNPJ>"
            f"<distNSU><ultNSU>{ult_nsu:015d}</ultNSU></distNSU>"
            "</distDFeInt>"
        )

    def _post(self, ult_nsu: int) -> ET.Element:
        if self._sessao is None:
            raise ErroDistribuicaoCTe("Cliente usado fora do contexto 'with'.")
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<soap12:Envelope xmlns:soap12="{SOAP_ENV}">'
            f'<soap12:Header><cteCabecMsg xmlns="{NS_SERVICO}">'
            f"<cUF>{self.uf_autor}</cUF><versaoDados>1.00</versaoDados>"
            "</cteCabecMsg></soap12:Header>"
            f'<soap12:Body><cteDistDFeInteresse xmlns="{NS_SERVICO}">'
            f"<cteDadosMsg>{self._payload(ult_nsu)}</cteDadosMsg>"
            "</cteDistDFeInteresse></soap12:Body></soap12:Envelope>"
        )
        acao = f"{NS_SERVICO}/cteDistDFeInteresse"
        try:
            resposta = self._sessao.post(
                URLS[self.ambiente],
                data=envelope.encode("utf-8"),
                headers={
                    "Content-Type": f'application/soap+xml; charset=utf-8; action="{acao}"',
                    "SOAPAction": f'"{acao}"',
                    "User-Agent": "EmissorGNRE/3.2",
                },
                timeout=self.timeout,
            )
        except requests.Timeout as erro:
            raise ErroDistribuicaoCTe(
                "A distribuição do CT-e não respondeu no tempo esperado."
            ) from erro
        except requests.RequestException as erro:
            raise ErroDistribuicaoCTe(
                f"Falha de conexão com a distribuição do CT-e: {erro}"
            ) from erro

        try:
            raiz = ET.fromstring(resposta.content)
        except ET.ParseError as erro:
            dica = ""
            if resposta.status_code in (401, 403):
                dica = " Verifique se o certificado A1 está válido e autorizado."
            raise ErroDistribuicaoCTe(
                f"Resposta inválida da distribuição do CT-e "
                f"(HTTP {resposta.status_code}).{dica}"
            ) from erro

        falha = _achar(raiz, "Fault")
        if falha is not None:
            motivo = _texto(falha, "Text") or _texto(falha, "faultstring")
            raise ErroDistribuicaoCTe(f"Distribuição do CT-e recusou a consulta: {motivo}")
        ret = _achar(raiz, "retDistDFeInt")
        if ret is None:
            raise ErroDistribuicaoCTe(
                f"Resposta sem retDistDFeInt (HTTP {resposta.status_code})."
            )
        return ret

    def _consultar(self, ult_nsu: int) -> dict:
        for tentativa in range(1, 4):
            ret = self._post(ult_nsu)
            cstat = _texto(ret, "cStat")
            motivo = _texto(ret, "xMotivo")
            if cstat == "656":
                raise ErroDistribuicaoCTe(
                    "A SEFAZ limitou as consultas da distribuição do CT-e "
                    "(consumo indevido). Aguarde cerca de 1 hora e tente novamente. "
                    "Os XMLs já baixados continuam disponíveis."
                )
            if cstat in ("108", "109") and tentativa < 3:
                time.sleep(5)
                continue
            lote = []
            for doc in _filhos(ret, "docZip"):
                lote.append(
                    {
                        "nsu": int(doc.get("NSU") or 0),
                        "schema": doc.get("schema") or "",
                        "conteudo": (doc.text or "").strip(),
                    }
                )
            return {
                "cstat": cstat,
                "motivo": motivo,
                "ult_nsu": int(_texto(ret, "ultNSU") or ult_nsu),
                "max_nsu": int(_texto(ret, "maxNSU") or ult_nsu),
                "doczip": lote,
            }
        raise ErroDistribuicaoCTe(
            "A distribuição do CT-e está indisponível ou limitou as consultas "
            "(consumo indevido). Tente novamente mais tarde."
        )

    # ------------------------------------------------------------------
    # processamento dos lotes
    # ------------------------------------------------------------------

    def _processar_docs(self, docs: list[dict], estado: dict) -> tuple[int, int]:
        """Grava os CT-es novos e conta os cancelamentos vistos na varredura."""
        salvos = 0
        cancelados = 0
        for doc in docs:
            if not doc["conteudo"]:
                continue
            try:
                bruto = gzip.decompress(base64.b64decode(doc["conteudo"]))
                raiz = ET.fromstring(bruto)
            except Exception:
                continue
            chave = _chave_do_infcte(raiz)
            if not chave:
                continue
            if _cancelamento_autorizado(raiz):
                if chave not in estado["cancelados"]:
                    cancelados += 1
                estado["cancelados"].add(chave)
                continue
            if _achar(raiz, "infCte") is None:
                # A SEFAZ manda eventos (MDF-e autorizado etc.) mesmo quando o
                # XML do CT-e não é distribuído para o CNPJ; guardar a chave
                # permite explicar o motivo na hora de buscar o documento.
                if chave not in estado["eventos"]:
                    estado["eventos"].append(chave)
                continue
            destino = self.pasta / f"{chave}.xml"
            if not destino.exists():
                destino.write_bytes(bruto)
                salvos += 1
        return salvos, cancelados

    def sincronizar(self, limite_paginas: int = PAGINAS_MAX) -> dict:
        estado = self._carregar_estado()
        salvos = 0
        cancelados = 0
        max_nsu = 0
        parcial = False

        if not estado["iniciado"]:
            ret = self._consultar(0)
            novos, vistos = self._processar_docs(ret["doczip"], estado)
            salvos += novos
            cancelados += vistos
            max_nsu = ret["max_nsu"]
            if ret["cstat"] not in ("137", "138"):
                raise ErroDistribuicaoCTe(
                    f"Distribuição do CT-e: {ret['cstat']} — {ret['motivo']}"
                )
            if ret["cstat"] == "138":
                # Parte do NSU já recebido para não repetir a mesma consulta.
                estado["ult_nsu"] = max(
                    ret["ult_nsu"], max_nsu - JANELA_INICIAL
                )
                self._registrar(
                    f"Distribuição do CT-e: {max_nsu} NSUs disponíveis; "
                    f"varrendo os {min(JANELA_INICIAL, max_nsu)} mais recentes."
                )
            else:
                estado["ult_nsu"] = max_nsu
            estado["iniciado"] = True
            self._salvar_estado(estado)
            if estado["ult_nsu"] >= max_nsu:
                return {
                    "salvos": salvos,
                    "ult_nsu": estado["ult_nsu"],
                    "max_nsu": max_nsu,
                    "parcial": False,
                }

        paginas = 0
        while True:
            ret = self._consultar(estado["ult_nsu"])
            novos, vistos = self._processar_docs(ret["doczip"], estado)
            salvos += novos
            cancelados += vistos
            max_nsu = ret["max_nsu"]
            if ret["cstat"] not in ("137", "138"):
                raise ErroDistribuicaoCTe(
                    f"Distribuição do CT-e: {ret['cstat']} — {ret['motivo']}"
                )
            anterior = estado["ult_nsu"]
            estado["ult_nsu"] = max(ret["ult_nsu"], anterior)
            estado["iniciado"] = True
            self._salvar_estado(estado)
            paginas += 1
            if ret["cstat"] == "137" or estado["ult_nsu"] >= max_nsu:
                break
            if paginas >= limite_paginas:
                parcial = True
                break
            time.sleep(PAUSA_PAGINA)

        extra = []
        if salvos:
            extra.append(f"{salvos} XML(s) novo(s)")
        if cancelados:
            extra.append(f"{cancelados} cancelamento(s) registrado(s)")
        self._registrar(
            f"Distribuição do CT-e: NSU {estado['ult_nsu']} de {max_nsu} "
            f"({', '.join(extra) or 'nada novo'})"
            f"{' — consulta parcial' if parcial else ''}."
        )
        return {
            "salvos": salvos,
            "ult_nsu": estado["ult_nsu"],
            "max_nsu": max_nsu,
            "parcial": parcial,
        }

    def _ultimo_do_emitente(self, chave: str) -> tuple[str, str] | None:
        """Maior CT-e do mesmo emitente/modelo/série já entregue pela SEFAZ."""
        if len(chave) != 44:
            return None
        emitente = chave[6:20]
        modelo_serie = chave[20:25]
        ultimo = ""
        for arquivo in self.pasta.glob("*.xml"):
            outra = arquivo.stem
            if len(outra) != 44 or not outra.isdigit():
                continue
            if outra[6:20] != emitente or outra[20:25] != modelo_serie:
                continue
            numero = outra[25:34]
            if numero.isdigit() and (not ultimo or int(numero) > int(ultimo[25:34])):
                ultimo = outra
        if not ultimo:
            return None
        data = ""
        try:
            raiz = ET.fromstring((self.pasta / f"{ultimo}.xml").read_bytes())
            data = _texto(raiz, "dhRecbto") or _texto(raiz, "dhEmi")
        except (OSError, ET.ParseError):
            pass
        return ultimo[25:34].lstrip("0") or "0", data

    @staticmethod
    def _formatar_data(texto: str) -> str:
        try:
            momento = datetime.fromisoformat(texto)
        except (TypeError, ValueError):
            return ""
        return momento.strftime("%d/%m/%Y %H:%M")

    def _ultimo_xml_recebido(self) -> str:
        """Data do CT-e mais recente que a distribuição já entregou."""
        mais_novo = None
        try:
            for arquivo in self.pasta.glob("*.xml"):
                if len(arquivo.stem) != 44 or not arquivo.stem.isdigit():
                    continue
                modificado = arquivo.stat().st_mtime
                if mais_novo is None or modificado > mais_novo:
                    mais_novo = modificado
        except OSError:
            return ""
        if mais_novo is None:
            return ""
        return datetime.fromtimestamp(mais_novo).strftime("%d/%m/%Y %H:%M")

    def obter(self, chave: str) -> Path:
        validar_chave(chave)
        destino = self.pasta / f"{chave}.xml"
        estado = self._carregar_estado()
        if chave in estado["cancelados"]:
            raise ErroDistribuicaoCTe(
                "Este CT-e está cancelado na SEFAZ. Não é possível emitir a GNRE."
            )

        if destino.exists():
            # XML já salvo: uma consulta incremental basta para ver cancelamentos.
            try:
                self.sincronizar(limite_paginas=1)
            except ErroDistribuicaoCTe as erro:
                self._registrar(f"AVISO: {erro} Usando o XML já salvo.")
                return destino
            estado = self._carregar_estado()
            if chave in estado["cancelados"]:
                raise ErroDistribuicaoCTe(
                    "Este CT-e está cancelado na SEFAZ. Não é possível emitir a GNRE."
                )
            return destino

        self.sincronizar()
        estado = self._carregar_estado()
        if chave in estado["cancelados"]:
            raise ErroDistribuicaoCTe(
                "Este CT-e está cancelado na SEFAZ. Não é possível emitir a GNRE."
            )
        if destino.exists():
            return destino

        ambiente = "produção" if self.ambiente == "1" else "homologação"
        if chave in estado["eventos"]:
            desde = self._ultimo_xml_recebido()
            raise ErroDistribuicaoCTe(
                f"CT-e {chave}: a SEFAZ em {ambiente} entregou apenas os eventos "
                "deste documento (como o MDF-e autorizado), mas não liberou o XML "
                "pela distribuição. Como os eventos chegaram, a SEFAZ reconhece o "
                "CNPJ do certificado neste documento; a falta do XML é atraso ou "
                "falha da própria distribuição"
                f"{f' — nenhum XML de CT-e novo é entregue desde {desde}' if desde else ''}. "
                "Tente novamente mais tarde. Se precisar emitir agora, peça o XML "
                "à transportadora ou baixe no portal do CT-e com o certificado e "
                "use a aba XML."
            )
        mensagem = (
            f"CT-e {chave} não apareceu na distribuição em {ambiente}. "
            "A SEFAZ não entregou esse CT-e para o CNPJ do certificado: "
            "verifique se ele está autorizado (protocolo de autorização) e se "
            "o CNPJ do certificado consta como remetente ou destinatário. "
            "CT-es recém-emitidos podem levar cerca de 1 hora para aparecer."
        )
        ultimo = self._ultimo_do_emitente(chave)
        if ultimo is not None:
            numero, data = ultimo
            quando = self._formatar_data(data)
            mensagem += (
                f" Último CT-e deste emitente que a distribuição entregou: "
                f"nº {numero}{f', autorizado em {quando}' if quando else ''}."
            )
        mensagem += " Se o XML já estiver em mãos, use a aba XML."
        raise ErroDistribuicaoCTe(mensagem)
