"""Caminhos do aplicativo, funcionando também no executável."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _documentos() -> Path:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders",
            ) as chave:
                valor, _ = winreg.QueryValueEx(chave, "Personal")
            caminho = Path(os.path.expandvars(str(valor)))
            if caminho.is_dir():
                return caminho
        except OSError:
            pass
    return Path.home() / "Documents"


if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
    RECURSOS = Path(getattr(sys, "_MEIPASS", BASE))
else:
    BASE = Path(__file__).resolve().parent.parent
    RECURSOS = BASE

DADOS = Path(os.environ.get("BYPASSGUIA_DIR") or (_documentos() / "BypassGuia")).resolve()
CONFIG_PATH = DADOS / "config.json"
LOG_DIR = DADOS / "logs"
SAIDA_DIR = DADOS / "saida"
PERFIL_DIR = DADOS / "perfil"
MUNICIPIOS_PATH = RECURSOS / "config" / "municipios.json"

for _pasta in (DADOS, LOG_DIR, SAIDA_DIR, PERFIL_DIR):
    _pasta.mkdir(parents=True, exist_ok=True)
