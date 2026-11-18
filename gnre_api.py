"""Emissão de GNRE 2.00 para ICMS-ST e FCP a partir de uma NF-e autorizada.

Fluxo: lê a NF-e -> consulta as regras da UF favorecida (UF do destinatário)
-> monta o lote (ST no valor 11 e FCP no valor 12, ou FCP em receita própria
quando a UF exige) -> assina a conexão com o e-CNPJ (mTLS) -> envia ->
consulta o resultado com `incluirPDFGuias=S` -> grava o PDF da guia.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Callable

import requests

from calendario import vencimento_padrao
from certificado import Certificado, carregar_certificado, preparar_pem_temporario
from guia_pdf import gerar_pdf_guia
from leitor_nfe import (
    DadosNFe,
    ErroNFe,
    Participante,
    ler_nfe_xml,
    somente_digitos,
)
from seguranca import ErroSeguranca, proteger, revelar

NS = "http://www.gnre.pe.gov.br"
SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
URLS = {
    "1": "https://www.gnre.pe.gov.br/gnreWS/services",
    "2": "https://www.testegnre.pe.gov.br/gnreWS/services",
}
NOME_AMBIENTE = {"1": "produção", "2": "homologação"}
URL_DISPONIBILIDADE = {
    "1": "https://www.gnre.pe.gov.br:444/gnre/v/guia/getDisponibilidadeUFsv1",
}

SITUACOES_GUIA = {
    "0": "Processada com sucesso",
    "1": "Invalidada pelo Portal",
    "2": "Invalidada pela UF",
    "3": "Erro de comunicação",
    "4": "Pendência de processamento",
}

CODIGOS_PENDENTES = {"400", "401"}
CODIGOS_CONCLUIDOS = {"402", "403"}
CODIGOS_GUIA_OK = {"0"}

POLL_TIMEOUT = {"1": 15, "2": 20}
POLL_PRAZO = {"1": 120, "2": 180}
POLL_ESPERAS = (0.4, 0.7, 1.0, 1.4, 1.8)

RECEITA_ST = "100099"
RECEITA_FCP = "100129"


class ErroGNRE(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# utilidades XML
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node: ET.Element | None, nome: str) -> ET.Element | None:
    if node is None:
        return None
    for filho in node.iter():
        if _local(filho.tag) == nome:
            return filho
    return None


def _findall(node: ET.Element | None, nome: str) -> list[ET.Element]:
    if node is None:
        return []
    return [filho for filho in node.iter() if _local(filho.tag) == nome]


def _texto(node: ET.Element | None, nome: str) -> str:
    achado = _find(node, nome)
    return (achado.text or "").strip() if achado is not None else ""


def _booleano(texto: str) -> bool:
    return (texto or "").strip().upper() in {"S", "1", "TRUE", "SIM"}


def _cortar(texto: str, limite: int) -> str:
    return re.sub(r"\s+", " ", (texto or "").strip())[:limite]


def _valor(valor: Decimal) -> str:
    return f"{valor:.2f}"


# ---------------------------------------------------------------------------
# configuração
# ---------------------------------------------------------------------------


@dataclass
class ConfigGNRE:
    caminho_pfx: Path
    senha: str
    ambiente: str = "2"
    pasta_saida: Path = Path("guias")
    arquivo_regras: Path = Path("regras_uf.json")
    timeout: int = 180
    tentativas: int = 24
    intervalo: int = 5
    verificar_ssl: bool = True
    contribuinte: dict | None = None
    convenios: dict[str, str] = field(default_factory=dict)
    cache_config: Path | None = None
    cache_horas: int = 24
    schemas_dir: Path | None = None
    indice_guias: Path | None = None


def _senha_certificado(bruto: dict, tolerar_ausencia: bool = False) -> str:
    senha = os.environ.get("GNRE_PFX_SENHA")
    if senha:
        return str(senha)
    protegida = bruto.get("senha_protegida")
    if protegida:
        try:
            return revelar(str(protegida))
        except ErroSeguranca as erro:
            raise ErroGNRE(
                f"Não foi possível ler a senha protegida do certificado ({erro}). "
                "Abra o aplicativo e use 'Atualizar certificado'."
            ) from erro
    if bruto.get("senha"):
        return str(bruto["senha"])
    if tolerar_ausencia:
        return ""
    raise ErroGNRE(
        "Senha do certificado ausente. Use 'Atualizar certificado' no aplicativo "
        "ou defina GNRE_PFX_SENHA."
    )


def salvar_certificado_config(
    caminho_config: str | Path,
    caminho_pfx: str | Path,
    senha: str,
) -> Path:
    """Grava o novo PFX e protege a senha com o DPAPI do Windows."""
    caminho = Path(caminho_config).resolve()
    bruto = json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else {}
    bruto["pfx_path"] = str(Path(caminho_pfx).resolve())
    bruto.pop("senha", None)
    bruto["senha_protegida"] = proteger(senha)
    temporario = caminho.with_name(caminho.name + ".tmp")
    try:
        temporario.write_text(
            json.dumps(bruto, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
        )
        os.replace(temporario, caminho)
    except OSError as erro:
        raise ErroGNRE(
            f"A pasta de configuração não aceita gravação: {caminho.parent}\n\n"
            f"{erro}\n\n"
            "Feche o programa, execute o instalador novamente e tente de novo."
        ) from erro
    finally:
        try:
            temporario.unlink()
        except OSError:
            pass
    return caminho


def carregar_config(
    caminho: str | Path = "config.json", tolerar_sem_senha: bool = False
) -> ConfigGNRE:
    caminho = Path(caminho).resolve()
    if not caminho.exists():
        raise ErroGNRE(f"Arquivo de configuração não encontrado: {caminho}")

    bruto = json.loads(caminho.read_text(encoding="utf-8"))
    base = caminho.parent

    def resolver(valor: str | None, padrao: str) -> Path:
        destino = Path(valor or padrao)
        return destino if destino.is_absolute() else (base / destino)

    senha = _senha_certificado(bruto, tolerar_sem_senha)
    if not bruto.get("pfx_path"):
        raise ErroGNRE("Informe 'pfx_path' no config.json.")

    return ConfigGNRE(
        caminho_pfx=resolver(bruto.get("pfx_path"), ""),
        senha=str(senha),
        ambiente=str(bruto.get("ambiente", "2")),
        pasta_saida=resolver(bruto.get("pasta_saida"), "guias"),
        arquivo_regras=resolver(bruto.get("arquivo_regras"), "regras_uf.json"),
        timeout=int(bruto.get("timeout", 60)),
        tentativas=max(1, int(bruto.get("tentativas", 24))),
        intervalo=max(1, int(bruto.get("intervalo", 5))),
        verificar_ssl=bool(bruto.get("verificar_ssl", True)),
        contribuinte=bruto.get("contribuinte"),
        convenios={
            str(uf).upper(): str(convenio)
            for uf, convenio in (bruto.get("convenios") or {}).items()
        },
        cache_config=resolver(bruto.get("cache_config"), "cache")
        if bruto.get("cache_config", "cache")
        else None,
        cache_horas=int(bruto.get("cache_horas", 24)),
        schemas_dir=resolver(bruto.get("schemas_dir"), "schemas_gnre")
        if bruto.get("schemas_dir", "schemas_gnre")
        else None,
        indice_guias=(
            resolver(bruto.get("indice_guias"), "guias_emitidas.json")
            if bruto.get("indice_guias", "guias_emitidas.json")
            else None
        ),
    )


# ---------------------------------------------------------------------------
# regras da UF
# ---------------------------------------------------------------------------


@dataclass
class RegrasUF:
    uf: str
    receita: str
    detalhamento: str | None
    documento_tipo: str
    documento_tipos_aceitos: list[str]
    exige_detalhamento: bool
    exige_contribuinte_destinatario: bool
    exige_data_vencimento: bool
    exige_data_pagamento: bool
    exige_convenio: bool
    exige_periodo_referencia: bool
    exige_parcela: bool
    exige_produto: bool
    exige_campos_adicionais: bool
    exige_valor_fecp: bool
    valor_exigido: str
    campos_adicionais: list[dict]
    campos_extras: dict[str, str]
    convenio: str | None
    produto: str | None
    fcp_receita: str | None
    fcp_no_item: bool
    fcp_guia_separada: bool
    fcp_rotulo: str | None
    fcp_detalhamento: str | None
    fcp_convenio: str | None
    fcp_campos_extras: dict[str, str]
    tipo_gnre: str
    fonte: str
    avisos: list[str] = field(default_factory=list)


def carregar_regras_locais(caminho: Path) -> dict:
    if not caminho.exists():
        return {}
    return json.loads(caminho.read_text(encoding="utf-8"))


def _config_cache_path(cache_dir: Path, ambiente: str, uf: str, receita: str) -> Path:
    return cache_dir / f"configuf_{ambiente}_{uf}_{receita}.json"


def _ler_cache(caminho: Path, horas: int) -> dict | None:
    if horas <= 0 or not caminho.exists():
        return None
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        obtido = datetime.fromisoformat(dados["obtido_em"])
        if datetime.now() - obtido <= timedelta(hours=horas):
            return dados["config"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    return None


def _gravar_cache(caminho: Path, config: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps(
            {"obtido_em": datetime.now().isoformat(timespec="seconds"), "config": config},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def normalizar_config_uf(xml_bytes: bytes, uf: str, receita: str) -> dict:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as erro:
        raise ErroGNRE(
            f"Resposta inválida do GnreConfigUF: {erro}. "
            f"Início: {xml_bytes[:300].decode('utf-8', 'replace')!r}"
        ) from erro

    situacao = _find(root, "situacaoConsulta")
    codigo = _texto(situacao, "codigo")
    descricao = _texto(situacao, "descricao")
    if codigo != "450":
        raise ErroGNRE(f"GnreConfigUF {uf}: {codigo or 'sem código'} - {descricao}")

    alvo = None
    for no_receita in _findall(_find(root, "receitas"), "receita"):
        if no_receita.get("codigo") == receita:
            alvo = no_receita
            break
    if alvo is None:
        raise ErroGNRE(f"A UF {uf} não retornou configuração para a receita {receita}.")

    campos = []
    for campo in _findall(_find(alvo, "camposAdicionais"), "campoAdicional"):
        campos.append(
            {
                "codigo": _texto(campo, "codigo"),
                "obrigatorio": _booleano(_texto(campo, "obrigatorio")),
                "titulo": _texto(campo, "titulo"),
                "tamanho": int(_texto(campo, "tamanho") or "0"),
                "tipo": _texto(campo, "tipo"),
            }
        )

    detalhamentos = [
        {"codigo": _texto(no, "codigo"), "descricao": _texto(no, "descricao")}
        for no in _findall(_find(alvo, "detalhamentosReceita"), "detalhamentoReceita")
    ]
    produtos = [
        {"codigo": _texto(no, "codigo"), "descricao": _texto(no, "descricao")}
        for no in _findall(_find(alvo, "produtos"), "produto")
    ]
    tipos = [
        _texto(no_tipo, "codigo")
        for no_tipo in _findall(_find(alvo, "tiposDocumentosOrigem"), "tipoDocumentoOrigem")
    ]

    exigido_fecp = _texto(alvo, "exigeValorFecp").upper()

    return {
        "uf": _texto(root, "uf") or uf,
        "ambiente": _texto(root, "ambiente"),
        "receita": {
            "codigo": receita,
            "descricao": alvo.get("descricao", ""),
            "exige_detalhamento": _booleano(_texto(alvo, "exigeDetalhamentoReceita")),
            "exige_contribuinte_emitente": _booleano(_texto(alvo, "exigeContribuinteEmitente")),
            "exige_contribuinte_destinatario": _booleano(_texto(alvo, "exigeContribuinteDestinatario")),
            "exige_data_vencimento": _booleano(_texto(alvo, "exigeDataVencimento")),
            "exige_data_pagamento": _booleano(_texto(alvo, "exigeDataPagamento")),
            "exige_convenio": _booleano(_texto(alvo, "exigeConvenio")),
            "exige_periodo_referencia": _booleano(_texto(alvo, "exigePeriodoReferencia")),
            "exige_parcela": _booleano(_texto(alvo, "exigeParcela")),
            "exige_produto": _booleano(_texto(alvo, "exigeProduto")),
            "exige_campos_adicionais": _booleano(_texto(alvo, "exigeCamposAdicionais")),
            "exige_valor_fecp": exigido_fecp,
            "valor_exigido": _texto(alvo, "valorExigido"),
            "tipos_documento": tipos,
            "campos_adicionais": campos,
            "detalhamentos": detalhamentos,
            "produtos": produtos,
            "tipos_gnre": [_texto(no, "tipoGnre") for no in _findall(_find(alvo, "tiposGnre"), "tipoGnre")],
            "aceita_pix": _booleano(_texto(alvo, "aceitaPgPix")),
        },
        "guias_por_lote": int(_texto(_find(root, "qtdMaximas"), "guiasPorLote") or "50"),
        "itens_por_guia": int(_texto(_find(root, "qtdMaximas"), "itensPorGuia") or "100"),
    }


def montar_regras(
    uf: str,
    live: dict | None,
    locais: dict,
    convenios: dict[str, str] | None = None,
    ambiente: str | None = None,
    local_completo: bool = False,
) -> RegrasUF:
    uf = uf.upper()
    local = locais.get(uf) or {}
    fallback = local.get("fallback") or {}
    live_receita = (live or {}).get("receita") or {}

    receita = str(local.get("receita") or RECEITA_ST)
    detalhamento = local.get("detalhamento")
    tipo_preferido = str(local.get("documento_tipo_preferido") or "10")
    produto = local.get("produto")

    if live and live_receita.get("codigo") != receita:
        raise ErroGNRE(f"Configuração online da UF {uf} não contém a receita {receita}.")

    aceitos = list(live_receita.get("tipos_documento") or fallback.get("tipos_documento") or [])
    padrao_documento = False
    if not aceitos:
        aceitos = ["10", "22", "24"]
        padrao_documento = True

    if tipo_preferido in aceitos:
        documento_tipo = tipo_preferido
    else:
        # 10 = número da NF-e, 22 = chave da NF-e, 24 = chave do DFe.
        documento_tipo = next(
            (tipo for tipo in ("10", "22", "24") if tipo in aceitos),
            "",
        )
    if not documento_tipo:
        raise ErroGNRE(
            f"A UF {uf} não aceita NF-e (tipos 10/22/24) na receita {receita}; "
            f"tipos aceitos: {', '.join(aceitos)}. Emita esta guia manualmente "
            "no portal GNRE."
        )

    exige_detalhamento = bool(
        live_receita.get("exige_detalhamento", fallback.get("exige_detalhamento", False))
    )
    if exige_detalhamento and not detalhamento:
        raise ErroGNRE(
            f"A UF {uf} exige detalhamento da receita {receita}, mas não há código "
            "configurado em regras_uf.json. Consulte o portal GNRE antes de emitir."
        )

    exige_produto = bool(
        live_receita.get("exige_produto", fallback.get("exige_produto", False))
    )
    if exige_produto and not produto:
        raise ErroGNRE(
            f"A UF {uf} exige o código de produto para a receita {receita}, que não "
            "está configurado em regras_uf.json. Consulte o portal GNRE antes de emitir."
        )

    if bool(live_receita.get("exige_parcela")):
        raise ErroGNRE(
            f"A UF {uf} exige parcela de referência para a receita {receita}, que não é "
            "preenchida automaticamente. Consulte o portal GNRE antes de emitir."
        )

    campos_live = live_receita.get("campos_adicionais") or []
    codigos_aceitos = {c["codigo"] for c in campos_live if c.get("codigo")}
    extras_locais = local.get("campos_extras") or {}
    if ambiente == "1" and local.get("campos_extras_producao"):
        extras_locais = local["campos_extras_producao"]
    if live is None:
        # Sem a configuração online, envia só os campos que o fallback da UF lista;
        # um código que a UF não conhece derrubaria o lote.
        codigos_fallback = {
            str(campo.get("codigo"))
            for campo in fallback.get("campos_adicionais") or []
            if campo.get("codigo")
        }
        extras = {
            str(codigo): str(template)
            for codigo, template in extras_locais.items()
            if not codigos_fallback or str(codigo) in codigos_fallback
        }
    else:
        extras = {
            str(codigo): str(template)
            for codigo, template in extras_locais.items()
            if str(codigo) in codigos_aceitos
        }

    # A chave da NF-e é o mesmo dado em toda UF; só o código do campo muda
    # (RO pede 83, MT 38, outras 52). O título do campo oficial identifica.
    for campo in campos_live:
        codigo = str(campo.get("codigo") or "")
        titulo = str(campo.get("titulo") or "").lower()
        if codigo and codigo not in extras and "chave de acesso" in titulo:
            extras[codigo] = "{chave}"

    for campo in campos_live:
        if campo.get("obrigatorio") and campo["codigo"] not in extras:
            raise ErroGNRE(
                f"A UF {uf} exige o campo extra {campo['codigo']} "
                f"({campo.get('titulo')}), que não possui preenchimento automático "
                "em regras_uf.json."
            )

    convenio = local.get("convenio") or (convenios or {}).get(uf)
    if convenio:
        convenio = str(convenio)
    avisos = []
    if padrao_documento:
        avisos.append(
            f"A UF {uf} não informou os tipos de documento aceitos; usando o tipo "
            f"{documento_tipo} (NF-e) por padrão."
        )
    if bool(live_receita.get("exige_periodo_referencia")):
        avisos.append(
            "A UF exige período de referência; será usado o mês/ano de emissão da NF-e."
        )
    if exige_produto:
        avisos.append(
            f"A UF {uf} exige o código de produto {produto}; a lista oficial não tem "
            "o produto da empresa e foi usado o código residual configurado."
        )
    if convenio:
        avisos.append(
            f"Convênio {convenio} informado automaticamente; confira no portal antes "
            "de pagar."
        )
    if not local:
        avisos.append(
            f"UF {uf} sem regra local detalhada; usando apenas a configuração online. "
            "Confira os campos extras no portal antes de pagar."
        )
    if live is None and not local_completo:
        avisos.append(
            "Configuração online indisponível; usando os valores de contingência "
            "do regras_uf.json."
        )
    if live_receita.get("exige_convenio") and not convenio:
        avisos.append(
            "A configuração da UF indica convênio. Se o lote for rejeitado, informe "
            "o convênio em 'convenios' no config.json."
        )

    fcp_no_item = bool(local.get("fcp_no_item", False))
    fcp_receita = local.get("fcp_receita")
    if fcp_receita:
        fcp_receita = str(fcp_receita)
    fcp_detalhamento = local.get("fcp_detalhamento")
    fcp_convenio = local.get("fcp_convenio") or convenio
    fcp_extras = {
        str(codigo): str(template)
        for codigo, template in (local.get("fcp_campos_extras") or {}).items()
    }

    def exigido(chave: str, padrao: bool) -> bool:
        return bool(live_receita.get(chave, fallback.get(chave, padrao)))

    fecp = str(
        live_receita.get("exige_valor_fecp")
        or fallback.get("exige_valor_fecp")
        or ""
    ).upper()

    return RegrasUF(
        uf=uf,
        receita=receita,
        detalhamento=detalhamento,
        documento_tipo=documento_tipo,
        documento_tipos_aceitos=aceitos,
        exige_detalhamento=exige_detalhamento,
        exige_contribuinte_destinatario=exigido("exige_contribuinte_destinatario", False),
        exige_data_vencimento=exigido("exige_data_vencimento", True),
        exige_data_pagamento=exigido("exige_data_pagamento", True),
        exige_convenio=exigido("exige_convenio", False),
        exige_periodo_referencia=exigido("exige_periodo_referencia", False),
        exige_parcela=exigido("exige_parcela", False),
        exige_produto=exige_produto,
        exige_campos_adicionais=exigido("exige_campos_adicionais", True),
        exige_valor_fecp=fecp in {"S", "O"},
        valor_exigido=str(
            live_receita.get("valor_exigido") or fallback.get("valor_exigido") or "P"
        ),
        campos_adicionais=campos_live or fallback.get("campos_adicionais") or [],
        campos_extras=extras,
        convenio=convenio,
        produto=produto,
        fcp_receita=fcp_receita,
        fcp_no_item=fcp_no_item,
        fcp_guia_separada=bool(local.get("fcp_guia_separada", False)),
        fcp_rotulo=str(local.get("fcp_rotulo") or "") or None,
        fcp_detalhamento=fcp_detalhamento,
        fcp_convenio=str(fcp_convenio) if fcp_convenio else None,
        fcp_campos_extras=fcp_extras,
        tipo_gnre=str((live_receita.get("tipos_gnre") or fallback.get("tipos_gnre") or ["0"])[0]),
        fonte="online" if live else ("local" if local_completo else "contingência"),
        avisos=avisos,
    )


# ---------------------------------------------------------------------------
# contribuinte
# ---------------------------------------------------------------------------


def resolver_contribuinte(dados: DadosNFe, cert: Certificado, config: ConfigGNRE) -> Participante:
    """O contribuinte da GNRE é o emitente da NF-e (remetente da transferência).

    O certificado apenas autentica o acesso ao portal: o CNPJ/endereço do lote
    vêm do emitente da NF-e, ou do bloco 'contribuinte' do config.json.
    """
    base = config.contribuinte or {}
    cnpj_config = somente_digitos(base.get("cnpj"))
    papel = str(base.get("papel") or "emit").lower()

    if cnpj_config:
        candidatos = [p for p in dados.participantes if p.cnpj == cnpj_config]
    else:
        candidatos = [p for p in dados.participantes if p.papel == papel]
        if not candidatos:
            candidatos = [
                p for p in dados.participantes if p.cnpj == somente_digitos(cert.cnpj)
            ]

    if candidatos:
        escolhido = sorted(
            candidatos, key=lambda p: {"emit": 0, "dest": 1}.get(p.papel, 9)
        )[0]
    elif base.get("cnpj"):
        escolhido = Participante(
            papel="config",
            cnpj=somente_digitos(base.get("cnpj")),
            ie=str(base.get("ie") or ""),
            nome=str(base.get("razao_social") or ""),
            endereco=str(base.get("endereco") or ""),
            municipio=str(base.get("municipio") or ""),
            uf=str(base.get("uf") or "").upper(),
            cep=somente_digitos(base.get("cep")),
            telefone=somente_digitos(base.get("telefone")),
        )
    else:
        raise ErroGNRE(
            "A NF-e não contém o participante esperado e não há bloco "
            "'contribuinte' no config.json. Ajuste 'contribuinte.papel' ou "
            "'contribuinte.cnpj'."
        )

    def juntar(campo: str) -> str:
        return str(getattr(escolhido, campo) or base.get(campo) or "")

    contribuinte = Participante(
        papel=escolhido.papel,
        cnpj=somente_digitos(juntar("cnpj")),
        cpf=somente_digitos(juntar("cpf")),
        ie=juntar("ie"),
        nome=juntar("nome") or juntar("razao_social"),
        endereco=juntar("endereco"),
        municipio=juntar("municipio"),
        uf=juntar("uf").upper(),
        cep=somente_digitos(juntar("cep")),
        telefone=somente_digitos(juntar("telefone")),
    )

    faltando = [
        rotulo
        for rotulo, valor in (
            ("CNPJ/CPF", contribuinte.cnpj or contribuinte.cpf),
            ("razão social", contribuinte.nome),
            ("endereço", contribuinte.endereco),
            ("município", contribuinte.municipio),
            ("UF", contribuinte.uf),
            ("CEP", contribuinte.cep),
        )
        if not valor
    ]
    if faltando:
        raise ErroGNRE(
            "Dados do contribuinte incompletos na NF-e: "
            + ", ".join(faltando)
            + ". Preencha o bloco 'contribuinte' do config.json."
        )
    return contribuinte


# ---------------------------------------------------------------------------
# montagem do lote
# ---------------------------------------------------------------------------


def _data_iso(valor: date | str | None, campo: str, padrao: date | None = None) -> date:
    if valor is None or valor == "":
        if padrao is not None:
            return padrao
        raise ErroGNRE(f"Informe {campo}.")
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError as erro:
        raise ErroGNRE(f"{campo} inválido ({valor!r}); use AAAA-MM-DD.") from erro


def _data_br(valor: str | None) -> str:
    try:
        return date.fromisoformat(str(valor)[:10]).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return str(valor or "")


def _documento_origem(
    dados: DadosNFe, regras: RegrasUF, numero_documento: str | None
) -> str:
    tipo = regras.documento_tipo
    if tipo == "10":
        return numero_documento or dados.numero
    if tipo in {"22", "24"}:
        return dados.chave
    if numero_documento:
        return somente_digitos(numero_documento)
    raise ErroGNRE(
        f"A UF {regras.uf} usa o tipo de documento {tipo}, que não é a NF-e. "
        "Informe 'numero_documento' manualmente."
    )


def _municipio_gnre(participante: Participante, papel: str) -> str:
    codigo = somente_digitos(participante.municipio)[-5:]
    if len(codigo) != 5:
        raise ErroGNRE(
            f"Código IBGE do município do {papel} inválido "
            f"({participante.municipio!r}); a GNRE usa 5 dígitos (IBGE sem a UF)."
        )
    return codigo


_IE_GNRE = re.compile(r"^(?:\d{2,14}|\d{8,12}P\d{0,2})$")


def _ie_gnre(valor: object) -> str:
    """IE sem máscara; vazio quando o XML traz 'ISENTO' ou texto inválido."""
    limpo = re.sub(r"[^0-9A-Za-z]", "", str(valor or "")).upper()
    return limpo if _IE_GNRE.fullmatch(limpo) else ""


def _dados_destinatario(
    dados: DadosNFe,
    favorecida: str,
) -> dict | None:
    destinatario = dados.destinatario
    if destinatario is None:
        return None
    ie = _ie_gnre(destinatario.ie)
    contribuinte_icms = str(destinatario.ind_ie or "1") in {"", "1"}
    if ie and contribuinte_icms and destinatario.uf == favorecida:
        identificacao = "IE"
        documento = ie
    elif destinatario.cnpj:
        identificacao = "CNPJ"
        documento = destinatario.cnpj
    elif destinatario.cpf:
        identificacao = "CPF"
        documento = destinatario.cpf
    else:
        return None
    return {
        "participante": destinatario,
        "identificacao": identificacao,
        "documento": documento,
    }


def _campos_extras(
    dados: DadosNFe,
    regras: RegrasUF,
    templates: dict[str, str],
    campos_adicionais: list[dict],
) -> tuple[list[tuple[str, str]], list[str]]:
    tamanhos = {
        str(campo.get("codigo")): int(campo.get("tamanho") or 0)
        for campo in campos_adicionais
    }
    obrigatorios = {
        str(campo.get("codigo"))
        for campo in campos_adicionais
        if campo.get("obrigatorio")
    }
    avisos: list[str] = []
    campos: list[tuple[str, str]] = []
    valores = {
        "chave": dados.chave,
        "numero": dados.numero,
        "serie": dados.serie,
        "data_emissao": dados.dh_emi.strftime("%Y-%m-%d"),
        "data_emissao_br": dados.dh_emi.strftime("%d/%m/%Y"),
        "data_vencimento": dados.data_vencimento.strftime("%d/%m/%Y"),
        "valor_st": f"{dados.valor_st:.2f}",
        "valor_fcp": f"{dados.valor_fcp:.2f}",
        "valor_total": f"{dados.valor_st_fcp:.2f}",
        "uf": dados.uf_favorecida,
        "emitente": dados.emitente.nome if dados.emitente else "",
        "destinatario": dados.destinatario.nome if dados.destinatario else "",
        "cfop": dados.cfop,
    }
    for codigo, template in templates.items():
        try:
            valor_campo = template.format(**valores)
        except (KeyError, IndexError):
            valor_campo = template
        limite = min(tamanhos.get(str(codigo)) or 100, 100)
        original = _cortar(valor_campo, len(valor_campo) or 1)
        valor_campo = _cortar(valor_campo, limite)
        if valor_campo != original:
            avisos.append(
                f"Campo extra {codigo} truncado para {limite} caracteres (limite da UF)."
            )
        if not valor_campo:
            if str(codigo) in obrigatorios:
                raise ErroGNRE(
                    f"A UF {regras.uf} exige o campo extra {codigo} preenchido."
                )
            continue
        campos.append((str(codigo), valor_campo))
    return campos, avisos


def montar_lote(
    dados: DadosNFe,
    contribuinte: Participante,
    regras: RegrasUF,
    uf_favorecida: str | None = None,
    data_vencimento: date | str | None = None,
    data_pagamento: date | str | None = None,
    convenio: str | None = None,
    numero_documento: str | None = None,
) -> tuple[str, dict]:
    favorecida = (dados.uf_favorecida or uf_favorecida or "").upper()
    if len(favorecida) != 2:
        raise ErroGNRE("UF favorecida não definida na NF-e.")
    if favorecida != regras.uf:
        raise ErroGNRE(f"As regras carregadas são de {regras.uf}, mas a UF favorecida é {favorecida}.")

    vencimento = _data_iso(
        data_vencimento, "data de vencimento", vencimento_padrao(favorecida)
    )
    pagamento = _data_iso(data_pagamento, "data de pagamento", date.today())

    dados.valor_st = dados.valor_st or Decimal("0")
    dados.valor_fcp = dados.valor_fcp or Decimal("0")
    if dados.valor_st <= 0 and dados.valor_fcp <= 0:
        raise ErroGNRE("A NF-e não tem ICMS-ST nem FCP-ST para recolher.")
    valor_total = dados.valor_st + dados.valor_fcp

    documento = _documento_origem(dados, regras, numero_documento)
    if not documento:
        raise ErroGNRE("Número do documento de origem vazio.")

    municipio = _municipio_gnre(contribuinte, "contribuinte")

    destinatario = _dados_destinatario(dados, favorecida)
    if regras.exige_contribuinte_destinatario and destinatario is None:
        raise ErroGNRE(
            f"A UF {favorecida} exige o contribuinte destinatário, mas a NF-e não "
            "traz a identificação do destinatário."
        )

    ET.register_namespace("", NS)

    def no(pai: ET.Element, nome: str, texto: str | None = None, **attrs: str) -> ET.Element:
        filho = ET.SubElement(pai, f"{{{NS}}}{nome}", attrs)
        if texto is not None:
            filho.text = str(texto)
        return filho

    def bloco_destinatario(item: ET.Element) -> None:
        if destinatario is None:
            return
        bloco = no(item, "contribuinteDestinatario")
        identificacao = no(bloco, "identificacao")
        no(identificacao, destinatario["identificacao"], _cortar(destinatario["documento"], 20))
        if destinatario["participante"].nome:
            no(bloco, "razaoSocial", _cortar(destinatario["participante"].nome, 60))
        no(bloco, "municipio", _municipio_gnre(destinatario["participante"], "destinatário"))

    def referencia(item: ET.Element) -> None:
        ref = no(item, "referencia")
        no(ref, "periodo", "0")
        no(ref, "mes", dados.dh_emi.strftime("%m"))
        no(ref, "ano", dados.dh_emi.strftime("%Y"))

    lote = ET.Element(f"{{{NS}}}TLote_GNRE", {"versao": "2.00"})
    guias = no(lote, "guias")
    avisos_campos: list[str] = []
    itens_resumo: list[dict] = []
    convenio_final = convenio or regras.convenio

    def abrir_guia() -> tuple[ET.Element, ET.Element]:
        dados_gnre = no(guias, "TDadosGNRE", versao="2.00")
        no(dados_gnre, "ufFavorecida", favorecida)
        no(dados_gnre, "tipoGnre", regras.tipo_gnre)
        emitente = no(dados_gnre, "contribuinteEmitente")
        identificacao = no(emitente, "identificacao")
        if contribuinte.cnpj:
            no(identificacao, "CNPJ", contribuinte.cnpj)
        elif contribuinte.cpf:
            no(identificacao, "CPF", contribuinte.cpf)
        else:
            raise ErroGNRE("Contribuinte sem CNPJ/CPF.")
        no(emitente, "razaoSocial", _cortar(contribuinte.nome, 60))
        no(emitente, "endereco", _cortar(contribuinte.endereco, 60))
        no(emitente, "municipio", municipio)
        no(emitente, "uf", contribuinte.uf)
        no(emitente, "cep", contribuinte.cep[:8])
        if contribuinte.telefone:
            no(emitente, "telefone", contribuinte.telefone[:11])
        return dados_gnre, no(dados_gnre, "itensGNRE")

    def fechar_guia(dados_gnre: ET.Element, total: Decimal) -> None:
        no(dados_gnre, "valorGNRE", _valor(total))
        no(dados_gnre, "dataPagamento", pagamento.isoformat())

    # Guia do ICMS-ST (receita 100099). Quando o FCP vai em guia separada
    # (ex.: GO), a guia do ST fica só com o valor principal de ICMS.
    guia_fcp_separada = bool(
        regras.fcp_guia_separada and dados.valor_fcp > 0 and regras.fcp_receita
    )
    if dados.valor_st > 0 or not guia_fcp_separada:
        dados_gnre, itens = abrir_guia()
        item = no(itens, "item")
        no(item, "receita", regras.receita)
        if regras.detalhamento:
            no(item, "detalhamentoReceita", regras.detalhamento)
        no(item, "documentoOrigem", documento, tipo=regras.documento_tipo)
        if regras.produto:
            no(item, "produto", regras.produto)
        if regras.exige_periodo_referencia:
            referencia(item)
        no(item, "dataVencimento", vencimento.isoformat())
        if dados.valor_st > 0:
            no(item, "valor", _valor(dados.valor_st), tipo="11")
        if regras.fcp_no_item and dados.valor_fcp > 0:
            no(item, "valor", _valor(dados.valor_fcp), tipo="12")

        if convenio_final:
            no(item, "convenio", convenio_final)
        if regras.exige_contribuinte_destinatario:
            bloco_destinatario(item)

        campos, avisos = _campos_extras(
            dados, regras, regras.campos_extras, regras.campos_adicionais
        )
        avisos_campos.extend(avisos)
        if campos:
            extras = no(item, "camposExtras")
            for codigo, valor_campo in campos:
                campo = no(extras, "campoExtra")
                no(campo, "codigo", codigo)
                no(campo, "valor", valor_campo)

        itens_resumo.append(
            {
                "guia": 1,
                "receita": regras.receita,
                "detalhamento": regras.detalhamento,
                "valor_st": _valor(dados.valor_st),
                "valor_fcp": _valor(dados.valor_fcp) if regras.fcp_no_item else "0.00",
                "documento": documento,
                "documento_tipo": regras.documento_tipo,
                "convenio": convenio_final,
                "produto": regras.produto,
                "campos_extras": dict(campos),
            }
        )
        fechar_guia(
            dados_gnre,
            dados.valor_st + (dados.valor_fcp if regras.fcp_no_item else Decimal("0")),
        )

    # FCP em receita própria (ex.: GO 100129).
    if guia_fcp_separada:
        dados_fcp, itens_fcp = abrir_guia()
        item_fcp = no(itens_fcp, "item")
        no(item_fcp, "receita", regras.fcp_receita)
        if regras.fcp_detalhamento:
            no(item_fcp, "detalhamentoReceita", regras.fcp_detalhamento)
        if regras.exige_periodo_referencia:
            referencia(item_fcp)
        no(item_fcp, "dataVencimento", vencimento.isoformat())
        no(item_fcp, "valor", _valor(dados.valor_fcp), tipo="11")
        if regras.fcp_convenio or convenio_final:
            no(item_fcp, "convenio", regras.fcp_convenio or convenio_final)
        if regras.exige_contribuinte_destinatario:
            bloco_destinatario(item_fcp)
        campos_fcp, avisos_fcp = _campos_extras(
            dados, regras, regras.fcp_campos_extras, []
        )
        avisos_campos.extend(avisos_fcp)
        if campos_fcp:
            extras = no(item_fcp, "camposExtras")
            for codigo, valor_campo in campos_fcp:
                campo = no(extras, "campoExtra")
                no(campo, "codigo", codigo)
                no(campo, "valor", valor_campo)
        itens_resumo.append(
            {
                "guia": 2,
                "receita": regras.fcp_receita,
                "detalhamento": regras.fcp_detalhamento,
                "valor_st": "0.00",
                "valor_fcp": _valor(dados.valor_fcp),
                "documento": "",
                "documento_tipo": "",
                "convenio": regras.fcp_convenio or convenio_final,
                "produto": None,
                "campos_extras": dict(campos_fcp),
            }
        )
        fechar_guia(dados_fcp, dados.valor_fcp)

    xml = ET.tostring(lote, encoding="utf-8", xml_declaration=True).decode("utf-8")

    resumo = {
        "uf_favorecida": favorecida,
        "receita": regras.receita,
        "documento_tipo": regras.documento_tipo,
        "documento_origem": documento,
        "valor_st": _valor(dados.valor_st),
        "valor_fcp": _valor(dados.valor_fcp),
        "valor": _valor(valor_total),
        "fcp_no_item": regras.fcp_no_item,
        "fcp_guia_separada": guia_fcp_separada,
        "fcp_rotulo": regras.fcp_rotulo,
        "fcp_receita": regras.fcp_receita if not regras.fcp_no_item else None,
        "data_vencimento": vencimento.isoformat(),
        "data_pagamento": pagamento.isoformat(),
        "contribuinte": contribuinte.nome,
        "cnpj": contribuinte.cnpj,
        "identificacao_por": "CNPJ + endereço (sem IE na UF favorecida)",
        "periodo_referencia": (
            dados.dh_emi.strftime("%m/%Y") if regras.exige_periodo_referencia else None
        ),
        "destinatario": (
            {
                "nome": destinatario["participante"].nome,
                "cnpj": destinatario["participante"].cnpj,
                "identificacao": destinatario["identificacao"],
                "documento": destinatario["documento"],
            }
            if destinatario
            else None
        ),
        "itens": itens_resumo,
        "convenio": convenio_final,
        "produto": regras.produto,
        "regras": regras.fonte,
        "avisos": avisos_campos,
    }
    return xml, resumo


def validar_lote_xsd(xml: str, schemas_dir: Path | None) -> list[str]:
    """Valida o lote contra o XSD oficial 2.00; devolve avisos ou levanta ErroGNRE."""
    if not schemas_dir:
        return []
    xsd = schemas_dir / "lote_gnre_v2.00.xsd"
    if not xsd.exists():
        return [f"XSD não encontrado em {xsd}; validação local ignorada."]
    try:
        from lxml import etree as lxml_etree
    except ImportError:
        return ["lxml não instalado; validação local do XSD ignorada."]
    try:
        schema = _schema_lote(str(xsd), xsd.stat().st_mtime)
        documento = lxml_etree.fromstring(xml.encode("utf-8"))
    except (lxml_etree.XMLSchemaParseError, lxml_etree.XMLSyntaxError) as erro:
        return [f"Não foi possível validar o lote no XSD: {erro}"]
    if schema.validate(documento):
        return []
    erros = "; ".join(
        f"linha {erro.line}: {erro.message}" for erro in schema.error_log
    )
    raise ErroGNRE(f"Lote reprovado na validação do XSD oficial: {erros}")


@lru_cache(maxsize=4)
def _schema_lote(caminho: str, _mtime: float):
    from lxml import etree as lxml_etree

    return lxml_etree.XMLSchema(lxml_etree.parse(caminho))


# ---------------------------------------------------------------------------
# cliente SOAP
# ---------------------------------------------------------------------------


def _verificar_fault(xml_bytes: bytes) -> None:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return
    fault = _find(root, "Fault")
    if fault is None:
        return
    motivo = _texto(_find(fault, "Reason"), "Text") or _texto(fault, "faultstring")
    raise ErroGNRE(f"Falha SOAP do serviço GNRE: {motivo or 'sem detalhe'}")


def _sem_declaracao(xml: str) -> str:
    return re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", xml or "", count=1)


class ClienteGNRE:
    def __init__(
        self,
        cert: Certificado,
        ambiente: str = "2",
        timeout: int = 180,
        verificar_ssl: bool = True,
        log: Callable[[str], None] | None = None,
    ):
        if ambiente not in URLS:
            raise ErroGNRE(
                f"Ambiente inválido: {ambiente!r}. Use '1' (produção) ou "
                "'2' (homologação)."
            )
        self.cert = cert
        self.ambiente = ambiente
        self.timeout = timeout
        self.verificar_ssl = verificar_ssl
        self.log = log
        self.ultima_resposta: bytes | None = None
        self._pem = None
        self._sessao: requests.Session | None = None
        self._disponibilidade: dict[str, tuple[float, tuple[str, str] | None]] = {}

    def __enter__(self) -> "ClienteGNRE":
        self._pem = preparar_pem_temporario(self.cert)
        self._sessao = requests.Session()
        self._sessao.cert = (str(self._pem.cert_path), str(self._pem.key_path))
        self._sessao.verify = self.verificar_ssl
        adaptador = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
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

    def _post(
        self,
        servico: str,
        acao: str,
        payload: str,
        tentativas: int = 1,
        repetir_timeout: bool = True,
        timeout: int | None = None,
    ) -> bytes:
        if self._pem is None or self._sessao is None:
            raise ErroGNRE("Cliente GNRE usado fora do contexto 'with'.")

        self.ultima_resposta = None
        namespace = f"http://www.gnre.pe.gov.br/webservice/{servico}"
        corpo = _sem_declaracao(payload)
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<soapenv:Envelope xmlns:soapenv="{SOAP_ENV}" xmlns:gnr="{namespace}">'
            "<soapenv:Header><gnr:gnreCabecMsg>"
            "<gnr:versaoDados>2.00</gnr:versaoDados>"
            "</gnr:gnreCabecMsg></soapenv:Header>"
            f"<soapenv:Body><gnr:gnreDadosMsg>{corpo}</gnr:gnreDadosMsg></soapenv:Body>"
            "</soapenv:Envelope>"
        )
        url = f"{URLS[self.ambiente]}/{servico}"
        action = f"http://www.gnre.pe.gov.br/webservice/{servico}/{acao}"

        for tentativa in range(1, tentativas + 1):
            inicio = time.monotonic()
            try:
                resposta = self._sessao.post(
                    url,
                    data=envelope.encode("utf-8"),
                    headers={
                        "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
                        "SOAPAction": f'"{action}"',
                        "User-Agent": "EmissorGNRE-Faturamento/1.0",
                    },
                    timeout=timeout or self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as erro:
                self._registrar(
                    f"{servico}: sem resposta em {time.monotonic() - inicio:.1f}s "
                    f"(tentativa {tentativa}/{tentativas})."
                )
                if repetir_timeout and tentativa < tentativas:
                    time.sleep(3)
                    continue
                raise ErroGNRE(
                    f"Falha de comunicação com {servico} "
                    f"({NOME_AMBIENTE[self.ambiente]}): {erro}"
                ) from erro
            except requests.RequestException as erro:
                raise ErroGNRE(
                    f"Falha de comunicação com {servico} "
                    f"({NOME_AMBIENTE[self.ambiente]}): {erro}"
                ) from erro

            self.ultima_resposta = resposta.content
            self._registrar(
                f"{servico}: HTTP {resposta.status_code} em "
                f"{time.monotonic() - inicio:.1f}s ({len(resposta.content)} bytes)."
            )
            indisponivel = (
                {502, 503} if not repetir_timeout else {502, 503, 504, 429}
            )
            if resposta.status_code in indisponivel and tentativa < tentativas:
                espera = 3
                if resposta.status_code == 429:
                    try:
                        espera = min(
                            30, max(3, int(resposta.headers.get("Retry-After", "5")))
                        )
                    except (TypeError, ValueError):
                        espera = 5
                    self._registrar(
                        f"{servico}: portal limitou as requisições (429); "
                        f"repetindo em {espera}s."
                    )
                else:
                    self._registrar(f"{servico}: portal indisponível; repetindo em 3s.")
                time.sleep(espera)
                continue
            break

        if resposta.status_code != 200:
            raise ErroGNRE(
                f"{servico} retornou HTTP {resposta.status_code}: "
                f"{resposta.text[:500] or 'sem corpo'}"
            )

        _verificar_fault(resposta.content)
        return resposta.content

    def consultar_config_uf(
        self,
        uf: str,
        receita: str = RECEITA_ST,
        timeout: int | None = None,
        tentativas: int = 2,
    ) -> dict:
        payload = (
            f'<TConsultaConfigUf xmlns="{NS}">'
            f"<ambiente>{self.ambiente}</ambiente>"
            f"<uf>{uf.upper()}</uf>"
            f"<receita>{receita}</receita>"
            "</TConsultaConfigUf>"
        )
        xml = self._post(
            "GnreConfigUF", "consultar", payload, tentativas=tentativas, timeout=timeout
        )
        return normalizar_config_uf(xml, uf.upper(), receita)

    def disponibilidade_uf(
        self, uf: str, validade: float = 60.0, validade_falha: float = 20.0
    ) -> tuple[str, str] | None:
        uf = uf.upper()
        agora = time.monotonic()
        anterior = self._disponibilidade.get(uf)
        if anterior is not None and anterior[0] > agora:
            return anterior[1]
        valor = self._consultar_disponibilidade(uf)
        vencimento = agora + (validade if valor is not None else validade_falha)
        self._disponibilidade[uf] = (vencimento, valor)
        return valor

    def _consultar_disponibilidade(self, uf: str) -> tuple[str, str] | None:
        url = URL_DISPONIBILIDADE.get(self.ambiente)
        if not url or self._sessao is None:
            return None
        try:
            resposta = self._sessao.get(url, timeout=5)
            dados = resposta.json()
        except (requests.RequestException, ValueError):
            return None
        for item in dados.get("ufs") or []:
            if str(item.get("sigla", "")).upper() == uf.upper():
                return (
                    str(item.get("statusComunicacao") or ""),
                    str(item.get("statusRequisicao") or ""),
                )
        return None

    def recepcionar_lote(self, lote_xml: str) -> dict:
        xml = self._post(
            "GnreLoteRecepcao", "processar", lote_xml, tentativas=3, repetir_timeout=False
        )
        try:
            return analisar_recepcao(xml)
        except ET.ParseError as erro:
            raise ErroGNRE(
                f"Resposta inválida do GnreLoteRecepcao: {erro}. "
                f"Início: {xml[:300].decode('utf-8', 'replace')!r}"
            ) from erro

    def consultar_lote(
        self,
        numero_recibo: str,
        incluir_pdf: bool = True,
        timeout: int | None = None,
    ) -> dict:
        payload = (
            f'<TConsLote_GNRE xmlns="{NS}">'
            f"<ambiente>{self.ambiente}</ambiente>"
            f"<numeroRecibo>{numero_recibo}</numeroRecibo>"
            f'<incluirPDFGuias>{"S" if incluir_pdf else "N"}</incluirPDFGuias>'
            "</TConsLote_GNRE>"
        )
        xml = self._post(
            "GnreResultadoLote",
            "consultar",
            payload,
            timeout=timeout if timeout is not None else min(self.timeout, 60),
        )
        try:
            return analisar_resultado(xml)
        except ET.ParseError as erro:
            raise ErroGNRE(
                f"Resposta inválida do GnreResultadoLote: {erro}. "
                f"Início: {xml[:300].decode('utf-8', 'replace')!r}"
            ) from erro


def _situacao(root: ET.Element) -> dict:
    no = _find(root, "situacaoProcess")
    return {
        "codigo": _texto(no, "codigo"),
        "descricao": _texto(no, "descricao"),
    }


def analisar_recepcao(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    situacao_no = _find(root, "situacaoRecepcao")
    situacao = {
        "codigo": _texto(situacao_no, "codigo"),
        "descricao": _texto(situacao_no, "descricao"),
    }
    recibo = _texto(_find(root, "recibo"), "numero")
    if not recibo:
        dica = {
            "101": " Certificado inválido; confira o certificado e a cadeia enviada.",
            "102": (
                " O CNPJ do emitente não está habilitado para o WebService neste "
                "ambiente; solicite 'Uso do WebService' no portal GNRE (Automação) "
                "para esse CNPJ."
            ),
        }.get(situacao["codigo"], "")
        raise ErroGNRE(
            f"Lote não recebido ({situacao['codigo']}: {situacao['descricao']}).{dica}"
        )
    return {"numero_recibo": recibo, "situacao": situacao, "xml": xml_bytes}


def _municipio_emitente(dados: DadosNFe | None) -> str:
    emitente = dados.emitente if dados else None
    return emitente.municipio_nome if emitente else ""


def _cnpj_destinatario(dados: DadosNFe | None) -> str:
    destinatario = dados.destinatario if dados else None
    return destinatario.cnpj if destinatario else ""


def _conferir_pdf_local(
    resultado: "ResultadoEmissao",
    base: str,
    config: ConfigGNRE,
    municipio_nome: str,
    registrar: Callable[[str], None],
    cnpj_destino: str = "",
) -> None:
    """Gera a réplica local e confere contra o PDF oficial (modo validação)."""
    try:
        import validacao_pdf
    except ImportError:
        return
    if resultado.pdf_path is None or resultado.pdf_local:
        return
    if not validacao_pdf.ativo(resultado.ambiente):
        return
    local = gerar_pdf_guia(
        config.pasta_saida / f"{base}_guia_local.pdf",
        resultado.guias,
        recibo=resultado.numero_recibo or "",
        municipio_nome=municipio_nome,
        ambiente=resultado.ambiente or "1",
        cnpj_destino=cnpj_destino,
    )
    if not local:
        return
    registrar("Modo validação: conferindo a guia local contra o PDF oficial...")
    info = validacao_pdf.validar(
        ambiente=resultado.ambiente,
        recibo=resultado.numero_recibo or "",
        local=local,
        oficial=resultado.pdf_path,
        relatorio=config.pasta_saida / f"{base}_validacao.txt",
        log=registrar,
    )
    if info is not None:
        resultado.validacao = info


def analisar_resultado(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    situacao = _situacao(root)

    guias = []
    for no_guia in _findall(_find(root, "resultado"), "guia"):
        codigo_guia = _texto(no_guia, "situacaoGuia")
        motivos = [
            f"{_texto(motivo, 'codigo')}: {_texto(motivo, 'descricao')}".strip(": ")
            for motivo in _findall(_find(no_guia, "motivosRejeicao"), "motivo")
        ]
        rotulo = SITUACOES_GUIA.get(codigo_guia, "")
        detalhe = "; ".join(motivos)
        emitente = _find(no_guia, "contribuinteEmitente")
        item = _find(_find(no_guia, "itensGNRE"), "item")
        documento = _find(item, "documentoOrigem")
        referencia_no = _find(item, "referencia")
        mes_ref = _texto(referencia_no, "mes")
        ano_ref = _texto(referencia_no, "ano")
        destinatario_no = _find(item, "contribuinteDestinatario")
        identificacao_dest = _find(destinatario_no, "identificacao")
        informacoes_lista = [
            texto
            for texto in (
                _texto(no_info, "informacao")
                for no_info in _findall(_find(no_guia, "informacoesComplementares"), "informacao")
            )
            if texto
        ]
        informacoes = " | ".join(informacoes_lista)
        guias.append(
            {
                "codigo": codigo_guia,
                "descricao": f"{rotulo}: {detalhe}" if detalhe else rotulo,
                "numero": _texto(no_guia, "nossoNumero") or _texto(no_guia, "numeroControle"),
                "linha_digitavel": _texto(no_guia, "linhaDigitavel"),
                "codigo_barras": _texto(no_guia, "codigoBarras"),
                "valor": _texto(no_guia, "valorGNRE"),
                "uf_favorecida": _texto(no_guia, "ufFavorecida"),
                "tipo_gnre": _texto(no_guia, "tipoGnre"),
                "emitente": {
                    "cnpj": _texto(_find(emitente, "identificacao"), "CNPJ"),
                    "cpf": _texto(_find(emitente, "identificacao"), "CPF"),
                    "razao_social": _texto(emitente, "razaoSocial"),
                    "endereco": _texto(emitente, "endereco"),
                    "municipio": _texto(emitente, "municipio"),
                    "uf": _texto(emitente, "uf"),
                    "cep": _texto(emitente, "cep"),
                    "telefone": _texto(emitente, "telefone"),
                },
                "receita": _texto(item, "receita"),
                "documento_origem": _texto(item, "documentoOrigem"),
                "documento_tipo": documento.get("tipo", "") if documento is not None else "",
                "referencia": (
                    f"{mes_ref}/{ano_ref}" if mes_ref and ano_ref else ""
                ),
                "parcela": _texto(referencia_no, "parcela"),
                "convenio": _texto(item, "convenio"),
                "produto": _texto(item, "produto"),
                "destinatario": {
                    "cnpj": _texto(identificacao_dest, "CNPJ"),
                    "cpf": _texto(identificacao_dest, "CPF"),
                    "ie": _texto(identificacao_dest, "IE"),
                    "razao_social": _texto(destinatario_no, "razaoSocial"),
                    "municipio": _texto(destinatario_no, "municipio"),
                },
                "data_vencimento": _texto(item, "dataVencimento"),
                "data_pagamento": _texto(no_guia, "dataPagamento"),
                "data_limite_pagamento": _texto(no_guia, "dataLimitePagamento"),
                "informacoes": informacoes,
                "informacoes_lista": informacoes_lista,
                "qrcode": _texto(no_guia, "qrcodePayload"),
            }
        )

    pdf = None
    no_pdf = _find(root, "pdfGuias")
    if no_pdf is not None and (no_pdf.text or "").strip():
        bruto = re.sub(r"\s+", "", no_pdf.text)
        bruto += "=" * (-len(bruto) % 4)
        try:
            pdf = base64.b64decode(bruto, validate=False)
        except ValueError as erro:
            raise ErroGNRE(f"PDF retornado pela GNRE inválido: {erro}") from erro

    return {
        "situacao": situacao,
        "guias": guias,
        "pdf": pdf,
        "resultado": _texto(root, "resultado"),
        "xml": xml_bytes,
    }


# ---------------------------------------------------------------------------
# conferência da guia
# ---------------------------------------------------------------------------

_PESOS_DV = (4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def _dv_mod11(bloco: str) -> int:
    soma = sum(int(digito) * peso for digito, peso in zip(bloco, _PESOS_DV))
    dv = 11 - (soma % 11)
    return 0 if dv >= 10 else dv


def validar_linha_digitavel(linha: str, codigo_barras: str) -> list[str]:
    """Confere os 4 DVs e a correspondência entre linha digitável (48) e barras (44)."""
    linha = somente_digitos(linha)
    barras = somente_digitos(codigo_barras)
    erros: list[str] = []
    if len(linha) != 48:
        erros.append(f"Linha digitável com {len(linha)} dígitos (esperado 48).")
    if len(barras) != 44:
        erros.append(f"Código de barras com {len(barras)} dígitos (esperado 44).")
    if erros:
        return erros
    blocos: list[str] = []
    for indice in range(4):
        inicio = indice * 12
        bloco = linha[inicio : inicio + 11]
        dv = int(linha[inicio + 11])
        blocos.append(bloco)
        calculado = _dv_mod11(bloco)
        if calculado != dv:
            erros.append(
                f"Dígito verificador do bloco {indice + 1} da linha digitável "
                f"inválido (calculado {calculado}, veio {dv})."
            )
    if "".join(blocos) != barras:
        erros.append("O código de barras não corresponde à linha digitável.")
    return erros


def _decimal_br(texto: object) -> Decimal | None:
    if texto is None:
        return None
    limpo = str(texto).strip().replace("R$", "").replace(" ", "")
    limpo = limpo.replace(".", "").replace(",", ".") if "," in limpo else limpo
    try:
        return Decimal(limpo)
    except InvalidOperation:
        return None


def _data_aaaammdd(texto: object) -> str:
    digitos = somente_digitos(str(texto or ""))
    if len(digitos) == 8 and "/" in str(texto):
        return digitos[4:] + digitos[2:4] + digitos[:2]
    return digitos


def conferir_guia(
    guia: dict,
    *,
    valor_esperado: object = None,
    uf_esperada: str | None = None,
    receita_esperada: object = None,
    cnpj_esperado: object = None,
    vencimento_esperado: object = None,
    chave_esperada: str | None = None,
) -> list[str]:
    """Divergências entre a guia devolvida pelo portal e a NF-e/lote montado.

    Lista vazia significa que a guia confere e pode ser paga.
    """
    erros = validar_linha_digitavel(
        guia.get("linha_digitavel") or "", guia.get("codigo_barras") or ""
    )
    if not str(guia.get("numero") or "").strip():
        erros.append("A guia não traz o número de controle.")
    if valor_esperado is not None:
        valor = _decimal_br(guia.get("valor"))
        esperado = _decimal_br(valor_esperado)
        if valor is None or esperado is None:
            erros.append(
                f"Valor da guia ({guia.get('valor') or '?'}) ou da NF-e "
                f"({valor_esperado}) ilegível."
            )
        elif valor != esperado:
            erros.append(
                f"Valor da guia R$ {valor} difere do ST+FCP da NF-e R$ {esperado}."
            )
    if uf_esperada:
        uf_guia = str(guia.get("uf_favorecida") or "").upper()
        if uf_guia != uf_esperada.upper():
            erros.append(
                f"UF favorecida da guia ({uf_guia or '?'}) difere da UF do "
                f"destinatário ({uf_esperada.upper()})."
            )
    if receita_esperada:
        if str(guia.get("receita") or "") != str(receita_esperada):
            erros.append(
                f"Receita da guia ({guia.get('receita') or '?'}) difere da receita "
                f"usada no lote ({receita_esperada})."
            )
    if cnpj_esperado:
        cnpj_guia = somente_digitos(str(guia.get("emitente", {}).get("cnpj") or ""))
        if cnpj_guia != somente_digitos(str(cnpj_esperado)):
            erros.append(
                f"CNPJ do contribuinte na guia ({cnpj_guia or '?'}) difere do "
                f"emitente da NF-e ({somente_digitos(str(cnpj_esperado))})."
            )
    if vencimento_esperado:
        if _data_aaaammdd(guia.get("data_vencimento")) != _data_aaaammdd(
            vencimento_esperado
        ):
            erros.append(
                f"Vencimento da guia ({guia.get('data_vencimento') or '?'}) difere do "
                f"vencimento do lote ({vencimento_esperado})."
            )
    if chave_esperada:
        chave = somente_digitos(str(chave_esperada))
        citadas = re.findall(r"\d{44}", str(guia.get("informacoes") or ""))
        if citadas and chave not in citadas:
            erros.append(
                "A guia referencia outra chave de acesso nas informações "
                f"complementares ({citadas[0]}), e não a desta NF-e ({chave})."
            )
    return erros


def conferir_guias(
    guias: list[dict],
    *,
    receitas_permitidas: object = None,
    **esperado: object,
) -> list[str]:
    """Confere o conjunto de guias; com mais de uma, compara a soma dos valores."""
    erros: list[str] = []
    esperado_individual = dict(esperado)
    if len(guias) > 1:
        valor_esperado = esperado_individual.pop("valor_esperado", None)
        if valor_esperado is not None:
            total = Decimal("0")
            for indice, guia in enumerate(guias, start=1):
                valor = _decimal_br(guia.get("valor"))
                if valor is None:
                    erros.append(
                        f"Guia {indice}: valor ilegível ({guia.get('valor') or '?'})."
                    )
                else:
                    total += valor
            esperado_valor = _decimal_br(valor_esperado)
            if esperado_valor is None or total != esperado_valor:
                erros.append(
                    f"A soma das guias R$ {total} difere do ICMS-ST + FCP da NF-e "
                    f"R$ {valor_esperado}."
                )
        permitidas = (
            {str(receita) for receita in receitas_permitidas}  # type: ignore[union-attr]
            if receitas_permitidas
            else None
        )
        for indice, guia in enumerate(guias, start=1):
            if permitidas is not None:
                receita = str(guia.get("receita") or "")
                if receita not in permitidas:
                    erros.append(
                        f"Guia {indice}: receita {receita or '?'} não faz parte do "
                        f"lote ({', '.join(sorted(permitidas))})."
                    )
        esperado_receita = esperado_individual.pop("receita_esperada", None)
        if esperado_receita and permitidas is None:
            esperado_individual["receita_esperada"] = esperado_receita
    for indice, guia in enumerate(guias, start=1):
        for erro in conferir_guia(guia, **esperado_individual):
            prefixo = f"Guia {indice}: " if len(guias) > 1 else ""
            erros.append(prefixo + erro)
    return erros


def guias_pagaveis(guias: list[dict]) -> list[dict]:
    """Guias com situação de pagamento válida (dispensa as rejeitadas)."""
    return [
        guia
        for guia in guias
        if not guia.get("codigo") or str(guia["codigo"]) in CODIGOS_GUIA_OK
    ]


def registrar_divergencias(
    resultado: "ResultadoEmissao",
    divergencias: list[str],
    registrar: Callable[[str], None],
) -> None:
    resultado.guia_divergente = True
    resultado.divergencias = list(divergencias)
    resultado.sucesso = False
    for linha in divergencias:
        registrar(f"CONFERÊNCIA REPROVADA: {linha}")
    resultado.avisos.append(
        "CONFERÊNCIA REPROVADA — NÃO PAGUE esta guia até conferir no portal GNRE."
    )
    resultado.mensagens.append(
        "A guia devolvida pelo portal diverge da NF-e/lote e NÃO deve ser paga: "
        + " ".join(divergencias)
    )


def _resultado_salvo(
    pasta_saida: Path, recibo: str, ambiente: str
) -> tuple[dict, Path] | None:
    """Retorno oficial já gravado em disco para um recibo (consultas anteriores)."""
    recibo = somente_digitos(recibo)
    if not recibo:
        return None
    encontrados: list[tuple[float, dict, Path]] = []
    for arquivo in pasta_saida.glob("*.xml"):
        try:
            bruto = arquivo.read_bytes()
            root = ET.fromstring(bruto)
        except (OSError, ET.ParseError):
            continue
        if _texto(root, "numeroRecibo") != recibo:
            continue
        if _texto(root, "ambiente") != ambiente:
            continue
        try:
            dados = analisar_resultado(bruto)
        except ET.ParseError:
            continue
        if not dados["guias"]:
            continue
        encontrados.append((arquivo.stat().st_mtime, dados, arquivo))
    if not encontrados:
        return None
    _, dados, arquivo = max(encontrados, key=lambda item: item[0])
    return dados, arquivo


# ---------------------------------------------------------------------------
# orquestração
# ---------------------------------------------------------------------------


@dataclass
class ResultadoEmissao:
    sucesso: bool
    ambiente: str
    mensagens: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    dados: DadosNFe | None = None
    contribuinte: Participante | None = None
    regras: RegrasUF | None = None
    resumo_lote: dict | None = None
    lote_xml: str = ""
    lote_path: Path | None = None
    numero_recibo: str | None = None
    situacao: dict | None = None
    guias: list[dict] = field(default_factory=list)
    pdf_path: Path | None = None
    pdf_local: bool = False
    retorno_path: Path | None = None
    uf_indisponivel: bool = False
    guia_divergente: bool = False
    divergencias: list[str] = field(default_factory=list)
    validacao: dict | None = None

    def resumo(self) -> str:
        ambiente = (
            "portal do estado (fora da GNRE)"
            if self.ambiente == "portal"
            else NOME_AMBIENTE.get(self.ambiente, self.ambiente)
        )
        linhas = [f"Ambiente: {ambiente}"]
        if self.dados:
            linhas.append(
                f"NF-e {self.dados.numero} (série {self.dados.serie}) - "
                f"chave {self.dados.chave}"
            )
        if self.resumo_lote:
            r = self.resumo_lote
            identificacao = r.get("identificacao_por") or "CNPJ + endereço"
            detalhe_contribuinte = f"{r['contribuinte']} - CNPJ {r['cnpj']}"
            linhas.extend(
                [
                    f"UF favorecida: {r['uf_favorecida']}",
                    f"Contribuinte: {detalhe_contribuinte} ({identificacao})",
                    f"Receita: {r['receita']} - documento tipo {r['documento_tipo']}: "
                    f"{r['documento_origem']}",
                    f"ICMS-ST: R$ {r['valor_st'].replace('.', ',')} | "
                    f"FCP-ST: R$ {r['valor_fcp'].replace('.', ',')} | "
                    f"Total: R$ {r['valor'].replace('.', ',')}",
                    f"Vencimento: {_data_br(r['data_vencimento'])}"
                    f" - pagamento: {_data_br(r['data_pagamento'])}",
                ]
            )
            if r.get("fcp_guia_separada"):
                rotulo = r.get("fcp_rotulo") or "FCP-ST"
                linhas.append(
                    f"O {rotulo} foi enviado na receita própria "
                    f"{r['fcp_receita']} em guia separada (o portal da UF não aceita "
                    "duas receitas na mesma guia)."
                )
            elif r.get("fcp_receita"):
                linhas.append(
                    "O FCP-ST foi enviado na receita própria "
                    f"{r['fcp_receita']} (mesma guia)."
                )
        if self.numero_recibo:
            linhas.append(f"Recibo do lote: {self.numero_recibo}")
        if self.situacao:
            linhas.append(f"Situação: {self.situacao['codigo']} - {self.situacao['descricao']}")
        for guia in self.guias:
            detalhe = f"Guia {guia.get('numero') or ''}: {guia['codigo']} - {guia['descricao']}"
            if guia.get("linha_digitavel"):
                detalhe += f" | linha digitável {guia['linha_digitavel']}"
            linhas.append(detalhe)
        if self.guia_divergente:
            linhas.append("CONFERÊNCIA REPROVADA — NÃO PAGUE esta guia.")
        if self.pdf_path:
            linhas.append(f"PDF: {self.pdf_path}")
        if self.validacao:
            linhas.append(self.validacao.get("resumo") or "")
        return "\n".join(linhas)


def _salvar(caminho: Path, conteudo: bytes | str) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(caminho.name + ".tmp")
    if isinstance(conteudo, bytes):
        temporario.write_bytes(conteudo)
    else:
        temporario.write_text(conteudo, encoding="utf-8")
    os.replace(temporario, caminho)
    return caminho


def numero_do_documento(valor: str) -> str:
    """Número da NF-e a partir do documento de origem, mesmo quando é a chave."""
    digitos = somente_digitos(valor)
    if len(digitos) == 44:
        numero = digitos[25:34].lstrip("0")
        return numero or digitos
    return valor.strip()


def detalhes_do_recibo(pasta_saida: Path, recibo: str) -> tuple[str, str]:
    """(UF favorecida, número da NF-e) do lote local que gerou o recibo."""
    if not recibo:
        return "", ""
    for recepcao in pasta_saida.glob("*_recepcao.xml"):
        try:
            texto = recepcao.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        achado = re.search(r"<ns1:numero>(\d+)</ns1:numero>", texto)
        if not achado or achado.group(1) != recibo:
            continue
        base = recepcao.name[: -len("_recepcao.xml")]
        lote = pasta_saida / f"{base}_lote.xml"
        if not lote.exists():
            continue
        try:
            raiz = ET.fromstring(lote.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ET.ParseError):
            continue
        uf = ""
        numero = ""
        for no in raiz.iter():
            tag = _local(no.tag)
            if tag == "ufFavorecida" and not uf:
                uf = (no.text or "").strip()
            elif tag == "documentoOrigem" and not numero:
                numero = numero_do_documento(no.text or "")
        return uf, numero
    return "", ""


def localizar_recibo_do_lote(
    pasta_saida: Path, chave: str, ambiente: str, lote_xml: str
) -> str | None:
    """Recibo de um lote já enviado para esta NF-e no mesmo ambiente."""
    alvo = _sem_declaracao(lote_xml).strip()
    bases: list[str] = []
    for arquivo in pasta_saida.glob(f"GNRE_{chave}_*_lote.xml"):
        try:
            texto = arquivo.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _sem_declaracao(texto).strip() == alvo:
            bases.append(arquivo.name[: -len("_lote.xml")])

    if not bases:
        bases = [
            arquivo.name[: -len("_lote.xml")]
            for arquivo in pasta_saida.glob(f"GNRE_{chave}_*_lote.xml")
        ]

    for base in sorted(set(bases), reverse=True):
        recepcao = pasta_saida / f"{base}_recepcao.xml"
        if not recepcao.exists():
            continue
        try:
            texto = recepcao.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        amb = re.search(r"<ns1:ambiente>(\d)</ns1:ambiente>", texto)
        if amb and amb.group(1) != ambiente:
            continue
        numero = re.search(r"<ns1:numero>(\d+)</ns1:numero>", texto)
        if numero:
            return numero.group(1)
    return None


def _regras_locais_completas(locais: dict, uf: str, ambiente: str) -> bool:
    """Diz se o regras_uf.json já cobre a UF sem depender do portal."""
    local = locais.get(uf) or {}
    fallback = local.get("fallback") or {}
    if not fallback.get("tipos_documento"):
        return False
    if ambiente == "1":
        return bool(local.get("campos_extras_producao"))
    return bool(local.get("campos_extras") is not None)


# ---------------------------------------------------------------------------
# índice de guias já emitidas por NF-e (proteção contra duplicidade)
# ---------------------------------------------------------------------------


def _ler_indice(config: ConfigGNRE) -> dict:
    caminho = config.indice_guias
    if caminho is None or not caminho.exists():
        return {}
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return bruto if isinstance(bruto, dict) else {}


def _recibo_do_indice(config: ConfigGNRE, chave: str, ambiente: str) -> str | None:
    item = _ler_indice(config).get(f"{ambiente}:{chave}") or {}
    recibo = somente_digitos(str(item.get("recibo") or ""))
    return recibo or None


def _registrar_no_indice(
    config: ConfigGNRE,
    ambiente: str,
    chave: str,
    uf: str,
    recibo: str,
    guia: dict,
) -> None:
    caminho = config.indice_guias
    if caminho is None or not chave or not recibo:
        return
    dados = _ler_indice(config)
    registro = {
        "chave": chave,
        "ambiente": ambiente,
        "uf": uf,
        "recibo": recibo,
        "numero_controle": str(guia.get("numero") or ""),
        "linha_digitavel": str(guia.get("linha_digitavel") or ""),
        "valor": str(guia.get("valor") or ""),
        "data_vencimento": str(guia.get("data_vencimento") or ""),
        "emitida_em": datetime.now().isoformat(timespec="seconds"),
    }
    chave_indice = f"{ambiente}:{chave}"
    if dados.get(chave_indice) == registro:
        return
    dados[chave_indice] = registro
    try:
        _salvar(caminho, json.dumps(dados, ensure_ascii=False, indent=2) + "\n")
    except OSError as erro:
        print(f"Aviso: não foi possível gravar {caminho.name}: {erro}", file=sys.stderr)


def emitir_guia(
    caminho_nfe: str | Path,
    config: ConfigGNRE | None = None,
    ambiente: str | None = None,
    emitir: bool = False,
    gerar_pdf: bool = True,
    consultar_regras: bool = True,
    uf_favorecida: str | None = None,
    data_vencimento: date | str | None = None,
    data_pagamento: date | str | None = None,
    convenio: str | None = None,
    numero_documento: str | None = None,
    log: Callable[[str], None] | None = None,
    ao_receber: Callable[[str], None] | None = None,
    cert: Certificado | None = None,
    cliente: ClienteGNRE | None = None,
) -> ResultadoEmissao:
    if config is None:
        config = carregar_config()
    ambiente = str(ambiente or config.ambiente)
    if ambiente not in URLS:
        raise ErroGNRE("Ambiente deve ser '1' (produção) ou '2' (homologação).")

    def registrar(mensagem: str) -> None:
        if log:
            log(mensagem)

    resultado = ResultadoEmissao(sucesso=False, ambiente=ambiente)
    tempos: dict[str, float] = {}
    inicio_geral = time.monotonic()

    def etapa(nome: str, inicio: float) -> float:
        agora = time.monotonic()
        tempos[nome] = round(agora - inicio, 1)
        return agora

    try:
        if cert is None:
            registrar("Carregando certificado digital...")
            t0 = time.monotonic()
            cert = carregar_certificado(config.caminho_pfx, config.senha)
            etapa("certificado", t0)
            resultado.avisos.extend(cert.avisos)
        registrar(
            f"Certificado: {cert.razao_social} (CNPJ {cert.cnpj}), "
            f"válido até {cert.validade:%d/%m/%Y}."
        )

        registrar("Lendo e validando a NF-e...")
        t0 = time.monotonic()
        dados = ler_nfe_xml(caminho_nfe)
        etapa("nfe", t0)
        resultado.dados = dados
        resultado.avisos.extend(dados.avisos)
        registrar(
            f"NF-e {dados.numero}: {dados.uf_emitente} -> {dados.uf_destino}, "
            f"ST R$ {dados.valor_st:.2f} + FCP R$ {dados.valor_fcp:.2f}."
        )

        favorecida = (dados.uf_favorecida or uf_favorecida or "").upper()
        if len(favorecida) != 2:
            raise ErroGNRE("UF favorecida não definida na NF-e.")
        if favorecida == "GO":
            raise ErroGNRE(
                "Goiás não recolhe ICMS-ST pela GNRE: use o portal ARR "
                "(DARE de Substituição Tributária e de PROTEGE/FCP)."
            )

        locais = carregar_regras_locais(config.arquivo_regras)
        live = None

        with ExitStack() as stack:
            if cliente is None and (consultar_regras or emitir):
                cliente = stack.enter_context(
                    ClienteGNRE(
                        cert,
                        ambiente=ambiente,
                        timeout=config.timeout,
                        verificar_ssl=config.verificar_ssl,
                        log=registrar,
                    )
                )

            t0 = time.monotonic()
            local_completo = False
            if consultar_regras:
                cache_path = None
                if config.cache_config is not None:
                    cache_path = _config_cache_path(
                        config.cache_config, ambiente, favorecida, RECEITA_ST
                    )
                    live = _ler_cache(cache_path, config.cache_horas)
                    if live:
                        registrar(f"Regras da UF {favorecida} obtidas do cache local.")
                if live is None:
                    local_completo = _regras_locais_completas(locais, favorecida, ambiente)
                if live is None and local_completo:
                    registrar(
                        f"Regras da UF {favorecida} completas no regras_uf.json; "
                        "consulta ao portal dispensada."
                    )
                elif live is None:
                    registrar(f"Consultando as regras da UF {favorecida} no portal GNRE...")
                    try:
                        # Uma tentativa só: em caso de lentidão vale cair para o
                        # cache local em vez de segurar a emissão por dois timeouts.
                        live = cliente.consultar_config_uf(
                            favorecida,
                            RECEITA_ST,
                            timeout=min(config.timeout, 25),
                            tentativas=1,
                        )
                    except ErroGNRE as erro:
                        texto = str(erro).lower()
                        if "habilitada" in texto or "não relacionada" in texto or "nao relacionada" in texto:
                            raise ErroGNRE(
                                f"A UF {favorecida} não usa a GNRE para ICMS-ST "
                                f"(receita {RECEITA_ST}). Emita esta guia pelo portal "
                                "estadual."
                            ) from erro
                        live = (
                            _ler_cache(cache_path, 24 * 3650)
                            if cache_path is not None
                            else None
                        )
                        if live is None:
                            registrar(
                                f"Portal não respondeu ({erro}); seguindo com o "
                                "regras_uf.json."
                            )
                            resultado.avisos.append(
                                f"Regras da UF {favorecida} não puderam ser confirmadas "
                                "no portal agora; foi usado o regras_uf.json. Confira os "
                                "campos no portal antes de pagar."
                            )
                        else:
                            registrar(
                                f"Portal não respondeu ({erro}); usando a última "
                                f"configuração da UF {favorecida} salva em cache."
                            )
                            resultado.avisos.append(
                                f"Regras da UF {favorecida} vieram do cache porque o "
                                "portal não respondeu agora; confira os campos no portal "
                                "antes de pagar."
                            )
                    else:
                        if cache_path is not None:
                            _gravar_cache(cache_path, live)

            regras = montar_regras(
                favorecida,
                live,
                locais,
                config.convenios,
                ambiente=ambiente,
                local_completo=local_completo,
            )
            etapa("regras", t0)
            resultado.regras = regras
            resultado.avisos.extend(regras.avisos)
            receitas_lote = [regras.receita]
            if regras.fcp_receita and not regras.fcp_no_item and dados.valor_fcp > 0:
                receitas_lote.append(regras.fcp_receita)
            if regras.fcp_guia_separada and regras.fcp_receita:
                descricao_fcp = (
                    f"guia separada na receita {regras.fcp_receita}"
                    + (f" ({regras.fcp_rotulo})" if regras.fcp_rotulo else "")
                )
            elif regras.fcp_no_item:
                descricao_fcp = "no item"
            elif regras.fcp_receita:
                descricao_fcp = f"receita {regras.fcp_receita}"
            else:
                descricao_fcp = "não exigido"
            registrar(
                f"Regras: receita {regras.receita}, documento tipo "
                f"{regras.documento_tipo}, FCP {descricao_fcp}, "
                f"fonte {regras.fonte}."
            )

            contribuinte = resolver_contribuinte(dados, cert, config)
            resultado.contribuinte = contribuinte
            registrar(
                f"Contribuinte da GNRE: {contribuinte.nome} (CNPJ "
                f"{contribuinte.cnpj}, UF {contribuinte.uf or 'não informada'})."
            )

            lote_xml, resumo_lote = montar_lote(
                dados,
                contribuinte,
                regras,
                uf_favorecida=favorecida,
                data_vencimento=data_vencimento,
                data_pagamento=data_pagamento,
                convenio=convenio,
                numero_documento=numero_documento,
            )
            resultado.lote_xml = lote_xml
            resultado.resumo_lote = resumo_lote
            resultado.avisos.extend(resumo_lote.get("avisos") or [])
            resultado.avisos.extend(validar_lote_xsd(lote_xml, config.schemas_dir))
            etapa("lote", t0)
            registrar(
                "Identificação do contribuinte no lote: "
                f"{resumo_lote['identificacao_por']}."
            )
            registrar(
                f"Valores: ICMS-ST R$ {resumo_lote['valor_st']} + FCP-ST "
                f"R$ {resumo_lote['valor_fcp']} = R$ {resumo_lote['valor']}."
            )
            if resumo_lote.get("periodo_referencia"):
                registrar(
                    f"Período de referência: {resumo_lote['periodo_referencia']} "
                    "(mês/ano de emissão da NF-e)."
                )
            if resumo_lote.get("destinatario"):
                dest = resumo_lote["destinatario"]
                registrar(
                    f"Contribuinte destinatário no lote: {dest['nome']} "
                    f"({dest['identificacao']} {dest['documento']})."
                )

            if pagamento_apos_vencimento(resumo_lote):
                resultado.avisos.append(
                    "A data de pagamento é posterior ao vencimento; confira "
                    "juros/multa no portal."
                )

            base = f"GNRE_{dados.chave}_{datetime.now():%Y%m%d_%H%M%S}"
            resultado.lote_path = _salvar(config.pasta_saida / f"{base}_lote.xml", lote_xml)
            registrar(f"Lote XML gravado em {resultado.lote_path}.")

            def salvar_bruto(sufixo: str) -> None:
                if not cliente.ultima_resposta:
                    return
                caminho = _salvar(
                    config.pasta_saida / f"{base}_{sufixo}.xml", cliente.ultima_resposta
                )
                registrar(f"Resposta bruta do portal gravada em {caminho}.")

            if not emitir:
                resultado.sucesso = True
                resultado.mensagens.append(
                    "Geração local concluída; nenhum lote foi enviado."
                )
                return resultado

            limite_consulta = 45 if ambiente == "1" else 35
            recibo_anterior = localizar_recibo_do_lote(
                config.pasta_saida, dados.chave, ambiente, lote_xml
            ) or _recibo_do_indice(config, dados.chave, ambiente)
            if recibo_anterior:
                registrar(
                    f"Esta NF-e já tem lote enviado (recibo {recibo_anterior}); "
                    "consultando a guia antes de reenviar..."
                )
                try:
                    anterior = None
                    ultima_excecao: ErroGNRE | None = None
                    espera_consulta = limite_consulta
                    prazo_consulta = time.monotonic() + (150 if ambiente == "1" else 120)
                    for tentativa in range(1, 4):
                        if tentativa > 1:
                            registrar(
                                "Consultando o recibo anterior de novo "
                                f"(tentativa {tentativa}/3, até {espera_consulta}s)..."
                            )
                            time.sleep(3)
                        try:
                            anterior = cliente.consultar_lote(
                                recibo_anterior,
                                incluir_pdf=False,
                                timeout=espera_consulta,
                            )
                            break
                        except ErroGNRE as erro:
                            ultima_excecao = erro
                            registrar(
                                f"Recibo anterior não respondeu em {espera_consulta}s "
                                f"(tentativa {tentativa}/3)."
                            )
                            espera_consulta = min(espera_consulta + 30, 120)
                        if time.monotonic() > prazo_consulta:
                            break
                    if anterior is None and ultima_excecao is not None:
                        raise ultima_excecao
                    if anterior is None:
                        raise ErroGNRE("Consulta do recibo anterior sem resposta.")
                except ErroGNRE as erro:
                    registrar(f"Recibo anterior não respondeu ({erro}).")
                    resultado.numero_recibo = recibo_anterior
                    salvo = _resultado_salvo(config.pasta_saida, recibo_anterior, ambiente)
                    if salvo:
                        dados_salvos, arquivo_salvo = salvo
                        resultado.situacao = dados_salvos["situacao"]
                        resultado.guias = dados_salvos["guias"]
                        divergencias = conferir_guias(
                            guias_pagaveis(resultado.guias),
                            valor_esperado=resumo_lote.get("valor"),
                            uf_esperada=favorecida,
                            receitas_permitidas=receitas_lote,
                            cnpj_esperado=resumo_lote.get("cnpj"),
                            chave_esperada=dados.chave,
                        )
                        if divergencias:
                            registrar_divergencias(resultado, divergencias, registrar)
                            return resultado
                        local = gerar_pdf_guia(
                            config.pasta_saida / f"{base}_guia.pdf",
                            guias_pagaveis(resultado.guias),
                            recibo=recibo_anterior,
                            municipio_nome=_municipio_emitente(dados),
                            ambiente=resultado.ambiente or "1",
                            cnpj_destino=_cnpj_destinatario(dados),
                        )
                        if local:
                            resultado.pdf_path = local
                            resultado.pdf_local = True
                            resultado.sucesso = True
                            resultado.avisos.append(
                                "O portal não respondeu à consulta do recibo agora; a "
                                "guia foi impressa a partir do retorno oficial salvo em "
                                f"{arquivo_salvo.name}."
                            )
                            registrar(f"Guia gerada localmente em {resultado.pdf_path}.")
                            resultado.mensagens.append(
                                f"A guia da NF-e {dados.numero} já estava emitida "
                                f"(recibo {recibo_anterior}); nenhum lote novo foi enviado."
                            )
                            return resultado
                    resultado.situacao = {
                        "codigo": "",
                        "descricao": "aguardando consulta do recibo",
                    }
                    resultado.avisos.append(
                        "Já existe um lote idêntico enviado para esta NF-e, mas o "
                        f"portal não respondeu à consulta do recibo {recibo_anterior} agora."
                    )
                    resultado.mensagens.append(
                        "Nenhum lote novo foi enviado. Clique de novo em "
                        "'Emitir guia' com a mesma NF-e para consultar o "
                        f"recibo {recibo_anterior} e obter a guia."
                    )
                    return resultado
                else:
                    situacao_anterior = anterior["situacao"]["codigo"]
                    if situacao_anterior in CODIGOS_PENDENTES:
                        prazo = time.monotonic() + (120 if ambiente == "1" else 90)
                        while (
                            situacao_anterior in CODIGOS_PENDENTES
                            and time.monotonic() < prazo
                        ):
                            registrar(
                                "O lote anterior ainda está em processamento "
                                f"({situacao_anterior}); aguardando o portal..."
                            )
                            time.sleep(max(config.intervalo, 3))
                            try:
                                anterior = cliente.consultar_lote(
                                    recibo_anterior,
                                    incluir_pdf=False,
                                    timeout=espera_consulta,
                                )
                            except ErroGNRE as erro:
                                registrar(
                                    f"Portal não respondeu ({erro}); tentando de novo."
                                )
                                continue
                            situacao_anterior = anterior["situacao"]["codigo"]
                            resultado.situacao = anterior["situacao"]
                            resultado.guias = anterior["guias"]
                        resultado.numero_recibo = recibo_anterior
                        resultado.situacao = anterior["situacao"]
                        resultado.guias = anterior["guias"]
                    guias_ok = [
                        guia
                        for guia in anterior["guias"]
                        if str(guia.get("codigo") or "") in CODIGOS_GUIA_OK
                    ]
                    if situacao_anterior in CODIGOS_CONCLUIDOS and guias_ok:
                        resultado.numero_recibo = recibo_anterior
                        resultado.situacao = anterior["situacao"]
                        resultado.guias = anterior["guias"]
                        _registrar_no_indice(
                            config,
                            ambiente,
                            dados.chave,
                            favorecida,
                            recibo_anterior,
                            guias_ok[0],
                        )
                        resultado.retorno_path = _salvar(
                            config.pasta_saida / f"{base}_resultado.xml", anterior["xml"]
                        )
                        registrar(
                            f"Guia já emitida (recibo {recibo_anterior}); imprimindo "
                            "sem enviar um novo lote."
                        )
                        divergencias = conferir_guias(
                            guias_pagaveis(resultado.guias),
                            valor_esperado=resumo_lote.get("valor"),
                            uf_esperada=favorecida,
                            receitas_permitidas=receitas_lote,
                            cnpj_esperado=resumo_lote.get("cnpj"),
                            chave_esperada=dados.chave,
                        )
                        if divergencias:
                            registrar_divergencias(resultado, divergencias, registrar)
                            return resultado
                        local = gerar_pdf_guia(
                            config.pasta_saida / f"{base}_guia.pdf",
                            guias_pagaveis(resultado.guias),
                            recibo=recibo_anterior,
                            municipio_nome=_municipio_emitente(dados),
                            ambiente=resultado.ambiente or "1",
                            cnpj_destino=_cnpj_destinatario(dados),
                        )
                        if local:
                            resultado.pdf_path = local
                            resultado.pdf_local = True
                            resultado.avisos.append(
                                "Guia já emitida; impressa localmente com o código de "
                                "barras e a linha digitável oficiais. O PDF oficial é "
                                "buscado em segundo plano."
                            )
                            registrar(f"Guia gerada localmente em {resultado.pdf_path}.")
                        resultado.mensagens.append(
                            f"A guia da NF-e {dados.numero} já estava emitida "
                            f"(recibo {recibo_anterior}); nenhum lote novo foi enviado."
                        )
                        resultado.sucesso = bool(resultado.pdf_path)
                        if not resultado.pdf_path:
                            resultado.avisos.append(
                                "A guia existe, mas o portal não entregou o PDF agora; "
                                "clique de novo em 'Emitir guia' com a mesma "
                                f"NF-e para tentar baixar a guia do recibo {recibo_anterior}."
                            )
                        return resultado
                    if situacao_anterior in CODIGOS_PENDENTES:
                        resultado.numero_recibo = recibo_anterior
                        resultado.situacao = anterior["situacao"]
                        resultado.guias = anterior["guias"]
                        resultado.mensagens.append(
                            f"A NF-e {dados.numero} já tem lote em processamento "
                            f"(recibo {recibo_anterior}); nenhum lote novo foi enviado. "
                            "Clique de novo em 'Emitir guia' em alguns minutos."
                        )
                        return resultado
                    registrar(
                        f"Lote anterior ({recibo_anterior}) não gerou guia válida "
                        f"(situação {situacao_anterior}); seguindo com o envio."
                    )

            if ambiente == "1":
                disponibilidade = cliente.disponibilidade_uf(favorecida)
                if disponibilidade is None:
                    registrar(
                        f"Disponibilidade da UF {favorecida} não informada pelo "
                        "portal; seguindo com o envio."
                    )
                else:
                    comunicacao, requisicao = disponibilidade
                    registrar(
                        f"Disponibilidade da UF {favorecida}: "
                        f"comunicação {comunicacao or '?'}, requisição {requisicao or '?'}."
                    )
                    if comunicacao in {"I", "M"} or requisicao == "I":
                        raise ErroGNRE(
                            f"A UF {favorecida} está indisponível no portal GNRE "
                            f"(comunicação {comunicacao or '?'}, requisição "
                            f"{requisicao or '?'}). O lote não foi enviado; tente "
                            "novamente mais tarde."
                        )
                    if comunicacao and comunicacao != "A":
                        resultado.avisos.append(
                            f"A UF {favorecida} está com comunicação {comunicacao} no "
                            "portal; o lote pode demorar a processar."
                        )
                    if requisicao and requisicao != "A":
                        resultado.avisos.append(
                            f"A UF {favorecida} está com a requisição em {requisicao} no "
                            "portal; o lote pode demorar a processar."
                        )

            registrar(
                f"Enviando lote para a {NOME_AMBIENTE[ambiente]} "
                f"(receita {regras.receita}, UF {favorecida})..."
            )
            t0 = time.monotonic()
            try:
                recepcao = cliente.recepcionar_lote(lote_xml)
            except ErroGNRE:
                salvar_bruto("recepcao_erro")
                if cliente.ultima_resposta is None:
                    resultado.avisos.append(
                        "Sem resposta do portal durante a recepção: o lote pode ter "
                        "sido aceito. Confira no portal GNRE antes de reenviar."
                    )
                raise
            etapa("recepcao", t0)
            resultado.numero_recibo = recepcao["numero_recibo"]
            resultado.situacao = recepcao["situacao"]
            if ao_receber is not None:
                ao_receber(resultado.numero_recibo)
            resultado.retorno_path = _salvar(
                config.pasta_saida / f"{base}_recepcao.xml", recepcao["xml"]
            )
            registrar(
                f"Lote recebido. Recibo {resultado.numero_recibo} "
                f"({recepcao['situacao']['codigo']}: {recepcao['situacao']['descricao']})."
            )
            if recepcao["situacao"]["codigo"] != "100":
                resultado.avisos.append(
                    f"Lote recebido com código {recepcao['situacao']['codigo']} "
                    f"({recepcao['situacao']['descricao']}); o processamento segue "
                    "normalmente."
                )

            consulta = {
                "situacao": {"codigo": "", "descricao": "aguardando processamento"},
                "guias": [],
                "pdf": None,
            }
            ultima_falha: str | None = None
            falhas_seguidas = 0
            limite_poll = POLL_TIMEOUT[ambiente]
            esperas = POLL_ESPERAS
            t0 = time.monotonic()
            prazo_final = t0 + POLL_PRAZO[ambiente]
            for tentativa in range(1, config.tentativas + 1):
                codigo = consulta["situacao"]["codigo"]
                if codigo in CODIGOS_CONCLUIDOS:
                    break
                if codigo and codigo not in CODIGOS_PENDENTES:
                    raise ErroGNRE(
                        f"Processamento interrompido ({codigo}: "
                        f"{consulta['situacao']['descricao']})."
                    )
                if time.monotonic() > prazo_final:
                    registrar(
                        "Tempo máximo de espera do processamento atingido; "
                        "a guia pode ser buscada clicando de novo com a mesma NF-e."
                    )
                    break
                if tentativa > 1 or codigo:
                    espera = min(
                        config.intervalo, esperas[min(tentativa - 2, len(esperas) - 1)]
                    )
                    registrar(
                        f"Lote ainda em processamento ({codigo or 'sem código'}); "
                        f"aguardando {espera:.1f}s..."
                    )
                    time.sleep(espera)
                try:
                    consulta = cliente.consultar_lote(
                        resultado.numero_recibo,
                        incluir_pdf=False,
                        timeout=limite_poll,
                    )
                except ErroGNRE as erro:
                    salvar_bruto("resultado_erro")
                    ultima_falha = str(erro)
                    falhas_seguidas += 1
                    registrar(
                        f"Portal não respondeu em {limite_poll}s; consultando de novo "
                        f"(falha {falhas_seguidas}/{config.tentativas})."
                    )
                    if limite_poll < 60:
                        limite_poll = min(limite_poll * 2, 60)
                        registrar(
                            f"O portal está respondendo devagar; próxima consulta "
                            f"espera até {limite_poll}s."
                        )
                    if tentativa >= config.tentativas or falhas_seguidas >= 6:
                        break
                    continue
                ultima_falha = None
                falhas_seguidas = 0
                resultado.situacao = consulta["situacao"]
                resultado.guias = consulta["guias"]

            if (
                resultado.situacao is None
                or resultado.situacao["codigo"] not in CODIGOS_CONCLUIDOS
            ):
                resultado.sucesso = False
                if resultado.situacao and resultado.situacao["codigo"] in CODIGOS_PENDENTES:
                    texto = (
                        "O lote continua em processamento no portal. "
                        f"Recibo {resultado.numero_recibo}: clique de novo em "
                        "'Emitir guia' em alguns minutos para buscar a guia."
                    )
                else:
                    texto = (
                        "O lote foi recebido pelo portal, mas a consulta não respondeu "
                        f"a tempo. Recibo {resultado.numero_recibo}: clique de novo em "
                        "'Emitir guia' para buscar a guia antes de reenviar."
                    )
                if ultima_falha:
                    texto += f" Última falha: {ultima_falha}"
                resultado.mensagens.append(texto)
                return resultado

            guias_validas = guias_pagaveis(resultado.guias)
            divergencias = conferir_guias(
                guias_validas,
                valor_esperado=resumo_lote.get("valor"),
                uf_esperada=favorecida,
                receitas_permitidas=receitas_lote,
                cnpj_esperado=resumo_lote.get("cnpj"),
                vencimento_esperado=resumo_lote.get("data_vencimento"),
                chave_esperada=dados.chave,
            )
            if divergencias:
                etapa("consulta", t0)
                resultado.retorno_path = _salvar(
                    config.pasta_saida / f"{base}_resultado.xml", consulta["xml"]
                )
                registrar_divergencias(resultado, divergencias, registrar)
                return resultado

            if gerar_pdf:
                local = gerar_pdf_guia(
                    config.pasta_saida / f"{base}_guia.pdf",
                    guias_validas,
                    recibo=resultado.numero_recibo or "",
                    municipio_nome=_municipio_emitente(dados),
                    ambiente=resultado.ambiente or "1",
                    cnpj_destino=_cnpj_destinatario(dados),
                )
                if local:
                    resultado.pdf_path = local
                    resultado.pdf_local = True
                    resultado.avisos.append(
                        "Guia impressa localmente assim que o portal devolveu o Nº de "
                        "Controle; o PDF oficial é buscado em segundo plano."
                    )
                    registrar(f"Guia gerada localmente em {resultado.pdf_path}.")
            etapa("consulta", t0)

            resultado.retorno_path = _salvar(
                config.pasta_saida / f"{base}_resultado.xml", consulta["xml"]
            )
            registrar(
                f"Processamento concluído ({resultado.situacao['codigo']}: "
                f"{resultado.situacao['descricao']})."
            )

            erros = [
                guia
                for guia in resultado.guias
                if guia["codigo"] and guia["codigo"] not in CODIGOS_GUIA_OK
            ]
            if erros:
                for guia in erros:
                    codigo_guia = guia["codigo"]
                    descricao_guia = guia.get("descricao") or ""
                    if codigo_guia == "703" or "703" in descricao_guia:
                        resultado.uf_indisponivel = True
                        texto = (
                            f"Guia não emitida (703): a UF {favorecida} não respondeu "
                            "ao portal GNRE; o lote foi recebido"
                        )
                        if resultado.numero_recibo:
                            texto += f" (recibo {resultado.numero_recibo})"
                        texto += ". Reenvie quando a UF voltar a responder."
                        if ambiente == "2":
                            texto += (
                                " Em homologação essa indisponibilidade é comum e não "
                                "indica erro no lote nem afeta a produção."
                            )
                        resultado.mensagens.append(texto)
                    else:
                        resultado.mensagens.append(
                            f"Guia rejeitada ({codigo_guia}: {guia['descricao']})."
                        )
                resultado.sucesso = False
            else:
                resultado.sucesso = True
                _registrar_no_indice(
                    config,
                    ambiente,
                    dados.chave,
                    favorecida,
                    resultado.numero_recibo or "",
                    guias_validas[0] if guias_validas else {},
                )

            if resultado.situacao["codigo"] == "403":
                resultado.avisos.append(
                    "Lote processado com advertência; leia a descrição antes de pagar."
                )

            if resultado.sucesso:
                resultado.mensagens.append("Emissão concluída.")
            return resultado

    except (ErroGNRE, ErroNFe, FileNotFoundError, ValueError) as erro:
        resultado.sucesso = False
        resultado.mensagens.append(str(erro))
        return resultado
    finally:
        resumo = " | ".join(f"{nome} {segundos:.1f}s" for nome, segundos in tempos.items())
        registrar(
            f"Tempos: {resumo + ' | ' if resumo else ''}"
            f"total {time.monotonic() - inicio_geral:.1f}s."
        )


def baixar_pdf_recibo(
    numero_recibo: str,
    config: ConfigGNRE | None = None,
    ambiente: str | None = None,
    log: Callable[[str], None] | None = None,
    municipio_nome: str = "",
    gerar_local: bool = True,
    cnpj_destino: str = "",
) -> ResultadoEmissao:
    """Consulta um recibo já recebido e grava o PDF, sem reenviar o lote."""
    if config is None:
        config = carregar_config()
    ambiente = str(ambiente or config.ambiente)
    if ambiente not in URLS:
        raise ErroGNRE("Ambiente deve ser '1' (produção) ou '2' (homologação).")
    recibo = somente_digitos(numero_recibo)
    if not recibo:
        raise ErroGNRE("Informe o número do recibo do lote.")

    def registrar(mensagem: str) -> None:
        if log:
            log(mensagem)

    resultado = ResultadoEmissao(sucesso=False, ambiente=ambiente)
    resultado.numero_recibo = recibo
    base = f"GNRE_recibo_{recibo}_{datetime.now():%Y%m%d_%H%M%S}"
    try:
        registrar("Carregando certificado digital...")
        cert = carregar_certificado(config.caminho_pfx, config.senha)
        with ClienteGNRE(
            cert,
            ambiente=ambiente,
            timeout=config.timeout,
            verificar_ssl=config.verificar_ssl,
            log=registrar,
        ) as cliente:
            registrar(f"Consultando o recibo {recibo} na {NOME_AMBIENTE[ambiente]}...")
            consulta = cliente.consultar_lote(
                recibo, incluir_pdf=False, timeout=min(config.timeout, 60)
            )
            resultado.situacao = consulta["situacao"]
            resultado.guias = consulta["guias"]
            resultado.retorno_path = _salvar(
                config.pasta_saida / f"{base}_resultado.xml", consulta["xml"]
            )
            if resultado.situacao["codigo"] in CODIGOS_PENDENTES:
                resultado.avisos.append(
                    "O lote ainda está em processamento; consulte de novo em alguns "
                    "minutos."
                )
                resultado.mensagens.append(
                    "Lote em processamento; PDF ainda indisponível."
                )
                return resultado

            divergencias = conferir_guias(guias_pagaveis(resultado.guias))
            if divergencias:
                registrar_divergencias(resultado, divergencias, registrar)
                return resultado

            registrar("Baixando o PDF da guia (o portal pode levar até 45s)...")
            pdf: bytes | None = None
            ultima_falha: str | None = None
            limite_pdf = min(config.timeout, 45)
            for tentativa in range(1, 4):
                try:
                    consulta_pdf = cliente.consultar_lote(
                        recibo, incluir_pdf=True, timeout=limite_pdf
                    )
                except ErroGNRE as erro:
                    ultima_falha = str(erro)
                    registrar(
                        f"PDF não retornou em {limite_pdf}s (tentativa {tentativa}/3)."
                    )
                    if tentativa < 3:
                        time.sleep(3)
                    continue
                resultado.situacao = consulta_pdf["situacao"]
                resultado.guias = consulta_pdf["guias"]
                resultado.retorno_path = _salvar(
                    config.pasta_saida / f"{base}_resultado.xml", consulta_pdf["xml"]
                )
                pdf = consulta_pdf["pdf"]
                break

            if pdf:
                resultado.pdf_path = _salvar(
                    config.pasta_saida / f"{base}_guia.pdf", pdf
                )
                registrar(f"PDF da guia gravado em {resultado.pdf_path}.")
                _conferir_pdf_local(
                    resultado,
                    base,
                    config,
                    municipio_nome,
                    registrar,
                    cnpj_destino=cnpj_destino,
                )
            else:
                resultado.avisos.append(
                    "O portal não retornou pdfGuias nesta consulta; tente novamente "
                    "em instantes ou baixe pelo portal GNRE."
                )
                if ultima_falha:
                    resultado.avisos.append(f"Última falha: {ultima_falha}")
                local = (
                    gerar_pdf_guia(
                        config.pasta_saida / f"{base}_guia.pdf",
                        guias_pagaveis(resultado.guias),
                        recibo=recibo,
                        municipio_nome=municipio_nome,
                        ambiente=ambiente or "1",
                        cnpj_destino=cnpj_destino,
                    )
                    if gerar_local
                    else None
                )
                if local:
                    resultado.pdf_path = local
                    resultado.pdf_local = True
                    resultado.avisos.append(
                        "Guia gerada localmente com o código de barras e a linha "
                        "digitável oficiais."
                    )
                    registrar(f"Guia gerada localmente em {resultado.pdf_path}.")
            resultado.sucesso = True
            resultado.mensagens.append("Consulta do recibo concluída.")
            return resultado
    except (ErroGNRE, FileNotFoundError, ValueError) as erro:
        salvo = _resultado_salvo(config.pasta_saida, recibo, ambiente)
        if salvo:
            dados_salvos, arquivo_salvo = salvo
            resultado.situacao = dados_salvos["situacao"]
            resultado.guias = dados_salvos["guias"]
            divergencias = conferir_guias(guias_pagaveis(resultado.guias))
            if divergencias:
                registrar_divergencias(resultado, divergencias, registrar)
                return resultado
            local = (
                gerar_pdf_guia(
                    config.pasta_saida / f"{base}_guia.pdf",
                    guias_pagaveis(resultado.guias),
                    recibo=recibo,
                    municipio_nome=municipio_nome,
                    ambiente=ambiente or "1",
                    cnpj_destino=cnpj_destino,
                )
                if gerar_local
                else None
            )
            if local:
                resultado.pdf_path = local
                resultado.pdf_local = True
                resultado.sucesso = True
                resultado.avisos.append(
                    f"O portal não respondeu ({erro}); a guia foi impressa a partir do "
                    f"retorno oficial salvo em {arquivo_salvo.name}."
                )
                registrar(f"Guia gerada localmente em {resultado.pdf_path}.")
                resultado.mensagens.append(
                    "Consulta do recibo concluída com os dados oficiais salvos "
                    "localmente."
                )
                return resultado
        resultado.mensagens.append(str(erro))
        return resultado


def pagamento_apos_vencimento(resumo_lote: dict) -> bool:
    try:
        return date.fromisoformat(resumo_lote["data_pagamento"]) > date.fromisoformat(
            resumo_lote["data_vencimento"]
        )
    except (KeyError, ValueError):
        return False


# ---------------------------------------------------------------------------
# linha de comando
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emite GNRE de ICMS-ST/FCP a partir de uma NF-e autorizada."
    )
    parser.add_argument("xml", nargs="?", help="Caminho do XML da NF-e autorizada")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="Arquivo de configuração",
    )
    parser.add_argument("--ambiente", choices=["1", "2"], help="1=produção, 2=homologação")
    parser.add_argument("--producao", action="store_true", help="Atalho para ambiente 1")
    parser.add_argument(
        "--confirmar-producao",
        action="store_true",
        help="Confirma a emissão real em produção",
    )
    parser.add_argument("--somente-xml", action="store_true", help="Gera o lote sem enviar")
    parser.add_argument("--offline", action="store_true", help="Não consulta as regras online")
    parser.add_argument("--sem-pdf", action="store_true", help="Não solicita o PDF")
    parser.add_argument("--uf", help="UF favorecida (padrão: UF do destinatário)")
    parser.add_argument("--vencimento", help="Data de vencimento AAAA-MM-DD")
    parser.add_argument("--pagamento", help="Data de pagamento AAAA-MM-DD")
    parser.add_argument("--convenio", help="Número do convênio, se a UF exigir")
    parser.add_argument("--documento", help="Número do documento de origem")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    config = carregar_config(args.config)
    if not args.xml:
        parser.error("informe o XML da NF-e")

    ambiente = "1" if args.producao else (args.ambiente or config.ambiente)
    if ambiente == "1" and not args.confirmar_producao:
        print(
            "Emissão em PRODUÇÃO bloqueada. Use --confirmar-producao para assumir "
            "o risco.",
            file=sys.stderr,
        )
        return 2

    resultado = emitir_guia(
        args.xml,
        config=config,
        ambiente=ambiente,
        emitir=not args.somente_xml,
        gerar_pdf=not args.sem_pdf,
        consultar_regras=not args.offline,
        uf_favorecida=args.uf,
        data_vencimento=args.vencimento,
        data_pagamento=args.pagamento,
        convenio=args.convenio,
        numero_documento=args.documento,
        log=print,
    )

    print("\n" + resultado.resumo())
    for aviso in resultado.avisos:
        print(f"AVISO: {aviso}")
    if not resultado.sucesso:
        print("\nFALHA:")
        for mensagem in resultado.mensagens:
            print(f"- {mensagem}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
