# -*- mode: python ; coding: utf-8 -*-
"""Empacota o instalador (arquivo único) com o programa embutido em app/."""

a = Analysis(
    ["instalar.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("../dist/Emissor GNRE Faturamento.exe", "app"),
        ("../assets/tottal.ico", "."),
        ("../assets/tottal_icone.png", "."),
        ("../assets/tottal_logo_instalador.png", "."),
    ],
    hiddenimports=["win32com.client", "pythoncom", "win32com.shell"],
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
    name="Instalar GNRE Faturamento",
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
    icon="../assets/tottal.ico",
    version="versao_instalador.txt",
)
