"""Chromium usado pelo robô: descoberta e instalação na máquina do usuário."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") in (None, "", "0"):
    # Congelado, o Playwright passaria a procurar o navegador dentro do
    # executável; aqui usamos sempre a pasta padrão do Windows.
    _base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(_base) / "ms-playwright")


class NavegadorAusente(RuntimeError):
    pass


def pasta() -> Path:
    return Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])


def _driver() -> tuple[str, str]:
    from playwright._impl._driver import compute_driver_executable

    node, cli = compute_driver_executable()
    return str(node), str(cli)


def revisao() -> str | None:
    try:
        caminho = Path(_driver()[1]).parent / "browsers.json"
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        for navegador in dados.get("browsers", []):
            if navegador.get("name") == "chromium":
                return str(navegador.get("revision") or "")
    except Exception:
        return None
    return None


def instalado() -> bool:
    raiz = pasta()
    esperada = revisao()
    if esperada:
        return (raiz / f"chromium-{esperada}").is_dir()
    return raiz.is_dir() and any(raiz.glob("chromium-*"))


def instalar(log=None) -> None:
    log = log or (lambda mensagem: None)
    node, cli = _driver()
    log("Baixando o navegador do portal (uma única vez, cerca de 170 MB)...")
    processo = subprocess.run(
        [node, cli, "install", "chromium", "--no-shell"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if processo.returncode != 0:
        # versões antigas do driver não conhecem --no-shell
        processo = subprocess.run(
            [node, cli, "install", "chromium"],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
    if processo.returncode != 0:
        detalhe = (processo.stderr or processo.stdout or "").strip().splitlines()
        raise NavegadorAusente(
            "Não consegui baixar o navegador do portal"
            + (f": {detalhe[-1]}" if detalhe else ".")
        )
    if not instalado():
        raise NavegadorAusente("O navegador foi baixado, mas não foi localizado.")
    log("Navegador do portal instalado.")
