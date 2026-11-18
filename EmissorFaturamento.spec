# -*- mode: python ; coding: utf-8 -*-
"""Empacota o emissor de GNRE do faturamento como arquivo único, sem console."""

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [
    ("assets", "assets"),
    ("schemas_gnre", "schemas_gnre"),
    ("config", "config"),
    ("regras_uf.json", "."),
    ("regras_cte.json", "."),
    ("versao.py", "."),
]
datas += collect_data_files("ttkbootstrap")

# O portal de Goiás roda com Playwright; o driver precisa ir junto.
# O navegador Chromium fica no perfil do usuário (ms-playwright).
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
datas += pw_datas

hiddenimports = [
    "pymupdf",
    "zxingcpp",
    "lxml",
    "lxml.etree",
    "reportlab",
    "requests",
    "PIL",
    "greenlet",
    "portal_guia",
    "portal_go",
    "portal_cte",
    "bypassguia",
    "bypassguia.config",
    "bypassguia.models",
    "bypassguia.navegador",
    "bypassguia.paths",
    "bypassguia.pdfcheck",
    "bypassguia.runner",
    "bypassguia.xml_reader",
    "bypassguia.adapters",
    "bypassguia.adapters.base",
    "bypassguia.adapters.es",
    "bypassguia.adapters.pr",
    "bypassguia.adapters.sp",
    "cte_painel",
    "cte_gnre",
    "cte_leitor",
    "cte_pdf",
    "cte_consulta",
    "cte_edocs",
    "pypdf",
    "atualizador",
]
hiddenimports += pw_hidden

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=pw_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test", "pydoc_data", "matplotlib", "pandas", "IPython"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Emissor GNRE Faturamento",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/tottal.ico",
)
