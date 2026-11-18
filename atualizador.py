"""Atualização interna do aplicativo pelo GitHub.

Confere a última release do repositório público, compara a versão com a do
programa e baixa o instalador completo. O instalador recebe o PID do programa
em uso, espera ele fechar e aplica a versão. A configuração em
Documentos\\ConfGNRE-Faturamento fica intacta.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPOSITORIO = "renatozapelini/GNRE.F"
API_ULTIMA = f"https://api.github.com/repos/{REPOSITORIO}/releases/latest"
NOME_ASSET = "Emissor GNRE Faturamento.exe"
NOME_INSTALADOR = "Instalar GNRE Faturamento.exe"
CABECALHOS = {
    "User-Agent": "GNRE-Faturamento-Updater",
    "Accept": "application/vnd.github+json",
}
TEMPO_LIMITE = 30.0
BLOCO = 256 * 1024


class ErroAtualizacao(RuntimeError):
    """Falha prevista: rede, release ausente ou arquivo inválido."""


@dataclass
class Atualizacao:
    versao: str
    tag: str
    notas: str
    url: str
    tamanho: int
    nome: str
    tipo: str = "exe"  # "instalador" (pacote completo) ou "exe" (só o programa)


def _numeros(versao: str) -> tuple[int, ...]:
    partes = re.findall(r"\d+", versao or "")
    return tuple(int(parte) for parte in partes[:4]) or (0,)


def comparar_versoes(esquerda: str, direita: str) -> int:
    """1 se esquerda é mais nova, -1 se mais antiga, 0 se iguais."""
    a, b = _numeros(esquerda), _numeros(direita)
    tamanho = max(len(a), len(b))
    a += (0,) * (tamanho - len(a))
    b += (0,) * (tamanho - len(b))
    if a > b:
        return 1
    if a < b:
        return -1
    return 0


def _requisitar(url: str):
    pedido = urllib.request.Request(url, headers=CABECALHOS)
    return urllib.request.urlopen(pedido, timeout=TEMPO_LIMITE)


def _chave_asset(nome: str) -> str:
    """Normaliza o nome do ativo: "Instalar GNRE Faturamento.exe" -> chave."""
    chave = re.sub(r"[^a-z0-9]", "", nome.lower())
    if chave.endswith("exe"):
        chave = chave[:-3]
    return chave


def consultar_ultima() -> Atualizacao:
    """Lê a última release publicada. Levanta ErroAtualizacao se não houver."""
    try:
        with _requisitar(API_ULTIMA) as resposta:
            dados = json.loads(resposta.read().decode("utf-8"))
    except urllib.error.HTTPError as erro:
        if erro.code == 404:
            raise ErroAtualizacao(
                f"O repositório {REPOSITORIO} ainda não tem nenhuma release publicada."
            ) from erro
        raise ErroAtualizacao(f"GitHub respondeu {erro.code} ao consultar a versão.") from erro
    except (urllib.error.URLError, TimeoutError, OSError) as erro:
        raise ErroAtualizacao(f"Sem conexão com o GitHub: {erro}") from erro
    except json.JSONDecodeError as erro:
        raise ErroAtualizacao("Resposta do GitHub em formato inesperado.") from erro

    if dados.get("draft") or dados.get("prerelease"):
        raise ErroAtualizacao("A última publicação do GitHub não é uma versão estável.")

    tag = str(dados.get("tag_name") or "").strip()
    versao = tag[1:] if tag.lower().startswith("v") else tag
    if not versao:
        raise ErroAtualizacao("A release do GitHub está sem número de versão.")

    assets = dados.get("assets") or []
    instalador = None
    programa = None
    for asset in assets:
        # O GitHub troca espaços por pontos no nome do ativo; comparamos a forma
        # normalizada para reconhecer "Instalar GNRE Faturamento.exe".
        chave = _chave_asset(str(asset.get("name") or ""))
        if chave == "instalargnrefaturamento":
            instalador = asset
        elif chave == "emissorgnrefaturamento":
            programa = asset
    # O instalador traz o programa inteiro (exe + bibliotecas + dados), por isso
    # é preferido: a troca só do .exe funciona, mas não leva arquivos novos.
    escolhido = instalador or programa
    if escolhido is None:
        for asset in assets:
            nome = str(asset.get("name") or "")
            if nome.lower().endswith(".exe") and "faturamento" in nome.lower():
                escolhido = asset
                break
    if escolhido is None:
        raise ErroAtualizacao(
            f"A release {tag} não traz {NOME_INSTALADOR} nem {NOME_ASSET} para instalação."
        )
    tipo = "instalador" if escolhido is instalador else "exe"

    tamanho = int(escolhido.get("size") or 0)
    minimo = 40 * 1024 * 1024 if tipo == "instalador" else 8 * 1024 * 1024
    if tamanho and tamanho < minimo:
        raise ErroAtualizacao(
            f"O arquivo {escolhido.get('name')} da release {tag} parece incompleto."
        )

    return Atualizacao(
        versao=versao,
        tag=tag,
        notas=str(dados.get("body") or "").strip(),
        url=str(escolhido.get("browser_download_url") or ""),
        tamanho=tamanho,
        nome=str(escolhido.get("name") or NOME_ASSET),
        tipo=tipo,
    )


def baixar(
    atualizacao: Atualizacao,
    destino: Path | None = None,
    progresso=None,
) -> Path:
    """Baixa o executável da release para uma pasta temporária do usuário."""
    if not atualizacao.url:
        raise ErroAtualizacao("A release não informou o endereço do executável.")
    pasta = Path(tempfile.gettempdir()) / "GNRE-Faturamento-atualizacao"
    pasta.mkdir(parents=True, exist_ok=True)
    prefixo = "Instalar-GNRE-Faturamento" if atualizacao.tipo == "instalador" else "GNRE-Faturamento"
    destino = destino or pasta / f"{prefixo}-{atualizacao.versao}.exe"
    parcial = destino.with_name(destino.name + ".parcial")

    recebido = 0
    try:
        with _requisitar(atualizacao.url) as resposta, parcial.open("wb") as arquivo:
            total = int(resposta.headers.get("Content-Length") or atualizacao.tamanho or 0)
            while True:
                bloco = resposta.read(BLOCO)
                if not bloco:
                    break
                arquivo.write(bloco)
                recebido += len(bloco)
                if progresso is not None and total:
                    progresso(recebido, total)
        minimo = 40 * 1024 * 1024 if atualizacao.tipo == "instalador" else 8 * 1024 * 1024
        if recebido < minimo:
            raise ErroAtualizacao(
                f"O download terminou com {recebido} bytes, menor que um executável válido."
            )
        if parcial.read_bytes()[:2] != b"MZ":
            raise ErroAtualizacao("O arquivo baixado não é um executável do Windows.")
        os.replace(parcial, destino)
    except (urllib.error.URLError, TimeoutError, OSError) as erro:
        parcial.unlink(missing_ok=True)
        raise ErroAtualizacao(f"Falha ao baixar a atualização: {erro}") from erro
    except ErroAtualizacao:
        parcial.unlink(missing_ok=True)
        raise
    return destino


def _pasta_programa() -> Path:
    """Mesmo destino usado pelo instalador (instalação por usuário)."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Programs" / "GNRE Faturamento"


def instalar(
    novo_instalador: Path,
    reiniciar: bool = True,
    tipo: str = "instalador",
) -> Path:
    """Inicia o instalador baixado; ele espera este programa fechar e aplica a versão."""
    if not getattr(sys, "frozen", False):
        raise ErroAtualizacao(
            "A troca automática só funciona no executável; no código-fonte baixe a release manualmente."
        )
    if tipo != "instalador":
        raise ErroAtualizacao(
            f"Esta release não traz o {NOME_INSTALADOR}. Baixe o instalador da "
            "release e rode manualmente; a troca só do executável não atualiza "
            "as bibliotecas do programa."
        )
    novo_instalador = Path(novo_instalador).resolve()
    if not novo_instalador.is_file():
        raise ErroAtualizacao("O instalador baixado não foi encontrado.")

    alvo = _pasta_programa() / NOME_ASSET
    comando = [
        str(novo_instalador),
        "--silencioso",
        "--aguardar-pid",
        str(os.getpid()),
    ]
    if reiniciar:
        comando += ["--reabrir", str(alvo)]
    ambiente = {
        chave: valor
        for chave, valor in os.environ.items()
        if not chave.upper().startswith("_PYI_")
    }
    try:
        subprocess.Popen(
            comando,
            env=ambiente,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as erro:
        raise ErroAtualizacao(f"Não foi possível iniciar o instalador: {erro}") from erro
    return novo_instalador
