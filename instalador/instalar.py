"""Instalador do Emissor de GNRE do faturamento: programa, pasta e atalhos."""

from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

VERSAO = "3.3.0"
ATALHO = "Emissor GNRE Faturamento"
PASTA_PROGRAMA = "GNRE Faturamento"
PASTA_CONFIG = "ConfGNRE-Faturamento"
EXE_NOME = "Emissor GNRE Faturamento.exe"

FUNDO = "#EBECED"
CARTAO = "#F5F6F6"
BORDA = "#DEDFE0"
TEXTO = "#1C1C1C"
SUAVE = "#6E6E6E"
ACENTO = "#1A73D1"
ACENTO_ESCURO = "#0F5AA8"
SUCESSO = "#107C41"
PERIGO = "#B3261E"

CONFIG_MODELO = {
    "pfx_path": "ESKIMO.pfx",
    "ambiente": "2",
    "pasta_saida": "guias",
    "arquivo_regras": "regras_uf.json",
    "cache_config": "cache",
    "cache_horas": 168,
    "schemas_dir": "schemas_gnre",
    "indice_guias": "guias_emitidas.json",
    "timeout": 180,
    "tentativas": 24,
    "intervalo": 2,
    "verificar_ssl": True,
    "convenios": {},
    "contribuinte": {"papel": "emit"},
}

LEIA_ME = """EMISSOR DE GNRE - FATURAMENTO (TOTTAL)
========================================

Esta pasta guarda toda a configuração e os arquivos gerados pelo programa.
Nada é gravado na Área de Trabalho.

config.json          configuração (caminhos relativos a esta pasta)
ESKIMO.pfx           certificado digital A1 (quando copiado pelo instalador)
regras_uf.json       regras das UFs usadas quando o portal não responde
regras_cte.json      regras do CT-e (TRANSREIS)
schemas_gnre\\        esquemas XSD do lote GNRE 2.00
cache\\               últimas configurações de UF obtidas do portal
guias\\               lotes, recibos, resultados e PDFs emitidos
guias_emitidas.json  índice de guias já emitidas (proteção contra duplicidade)
xmls\\                XMLs baixados pelo portal Edocs
portal\\              robô dos portais estaduais (perfil, logs e cópias do robô)
edocs.json           conta do portal Edocs (senha protegida pelo Windows)
emissor.log          registro de execução
ui_state.json        tamanho e posição da janela

GUIA A PARTIR DA NF-E (ICMS-ST E FCP)
-------------------------------------
Informe o Nº da NF-e (busca no portal Edocs) ou selecione o XML autorizado.
O programa calcula ICMS-ST e FCP pela UF de destino e emite a GNRE 2.00.
Para Goiás, o DARE é gerado no portal estadual.

GUIA A PARTIR DO CT-E (TRANSREIS)
---------------------------------
No menu lateral, "Guia de CT-e" abre a frente de transporte: informe o número,
o XML ou o PDF do CT-e autorizado. SP, ES e PR são emitidas no portal do
estado pelo aplicativo de transporte (TRANSREIS).

ATENÇÃO FISCAL
--------------
Guia divergente nunca vira PDF pagável: o arquivo fica marcado como
NÃO PAGUE na pasta diagnosticos\\ e o programa bloqueia a entrega.

PRIMEIRO USO EM UMA MÁQUINA NOVA
--------------------------------
A senha do certificado fica protegida pelo Windows e não acompanha a cópia
para outra máquina. Abra o programa, clique em "Certificado digital",
selecione ESKIMO.pfx nesta pasta e informe a senha uma única vez.

O padrão é Homologação (teste). A emissão em Produção exige escolha manual
na tela e autorização da empresa.
"""


# ---------------------------------------------------------------------------
# caminhos do sistema
# ---------------------------------------------------------------------------


def _pasta_registro(nome: str, padrao: str) -> Path:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer"
            r"\User Shell Folders",
        ) as chave:
            valor, _ = winreg.QueryValueEx(chave, nome)
        caminho = Path(os.path.expandvars(str(valor)))
        if caminho.is_dir():
            return caminho
    except OSError:
        pass
    return Path.home() / padrao


def pasta_documentos() -> Path:
    return _pasta_registro("Personal", "Documents")


def pasta_area_trabalho() -> Path:
    return _pasta_registro("Desktop", "Desktop")


def pasta_programa() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Programs" / PASTA_PROGRAMA


_config_escolhida: Path | None = None


def _pasta_utilizavel(pasta: Path) -> bool:
    """Confirma que a pasta aceita arquivos.

    Um junction/reparse point quebrado (OneDrive) passa em is_dir() e no
    mkdir(exist_ok=True), mas toda gravação falha com Errno 2.
    """
    sonda = pasta / ".escrita.tmp"
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        sonda.write_text("ok", encoding="utf-8")
        return True
    except OSError:
        return False
    finally:
        try:
            sonda.unlink()
        except OSError:
            pass


def pasta_config() -> Path:
    """ConfGNRE-Faturamento utilizável: Documentos ou LOCALAPPDATA.

    Em algumas máquinas a pasta Documentos é um atalho do OneDrive quebrado e
    criar pastas dentro dela falha com WinError 2. Nesse caso usamos a pasta
    local do usuário, que existe em qualquer Windows.
    """
    global _config_escolhida
    if _config_escolhida is not None:
        return _config_escolhida

    candidatas: list[Path] = []
    preferida = os.environ.get("GNRE_CONF_DIR")
    if preferida:
        candidatas.append(Path(preferida))
    candidatas.append(pasta_documentos() / PASTA_CONFIG)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidatas.append(Path(local) / PASTA_CONFIG)
    candidatas.append(Path.home() / PASTA_CONFIG)

    for pasta in candidatas:
        if _pasta_utilizavel(pasta):
            _config_escolhida = pasta
            return pasta
    _config_escolhida = candidatas[-1]
    return _config_escolhida


def raiz_recursos() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _origem(nome: str) -> Path:
    embutido = raiz_recursos() / nome
    if embutido.exists():
        return embutido
    if not getattr(sys, "frozen", False):
        candidatos = (
            Path(__file__).resolve().parent.parent / "dist" / nome,
            Path(__file__).resolve().parent.parent / "assets" / nome,
        )
        for candidato in candidatos:
            if candidato.exists():
                return candidato
    raise FileNotFoundError(f"Componente não encontrado no instalador: {nome}")


def pasta_app_embutida() -> Path:
    """Pasta do programa embutida em app/ no instalador.

    Aceita o EXE único (build onefile) e também uma pasta com o EXE e as DLLs.
    """
    if getattr(sys, "frozen", False):
        pasta = raiz_recursos() / "app"
    else:
        pasta = Path(__file__).resolve().parent.parent / "dist"
    if (pasta / EXE_NOME).is_file():
        return pasta
    pasta_onedir = pasta / Path(EXE_NOME).stem
    if (pasta_onedir / EXE_NOME).is_file():
        return pasta_onedir
    raise FileNotFoundError(f"{EXE_NOME} não encontrado no instalador: {pasta}")


def certificado_disponivel() -> Path | None:
    pastas = (
        [Path(sys.executable).parent, raiz_recursos()]
        if getattr(sys, "frozen", False)
        else [Path(__file__).resolve().parent]
    )
    vistos: set[Path] = set()
    for pasta in pastas:
        if not pasta.is_dir() or pasta in vistos:
            continue
        vistos.add(pasta)
        for arquivo in sorted(pasta.glob("*.pfx")) + sorted(pasta.glob("*.p12")):
            return arquivo
    return None


# ---------------------------------------------------------------------------
# instalação
# ---------------------------------------------------------------------------


class InstalacaoBloqueada(RuntimeError):
    pass


def _garantir_certificado(config: Path, pfx: Path) -> bool:
    bruto = json.loads(config.read_text(encoding="utf-8"))
    atual = str(bruto.get("pfx_path") or "").strip()
    if atual:
        caminho = Path(atual)
        if not caminho.is_absolute():
            caminho = config.parent / caminho
        if caminho.exists():
            return False
    bruto["pfx_path"] = pfx.name
    config.write_text(
        json.dumps(bruto, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
    )
    return True


def _criar_atalho(destino: Path, alvo: Path, icone: Path, descricao: str) -> None:
    from win32com.client import Dispatch

    shell = Dispatch("WScript.Shell")
    atalho = shell.CreateShortCut(str(destino))
    atalho.Targetpath = str(alvo)
    atalho.WorkingDirectory = str(alvo.parent)
    atalho.IconLocation = f"{icone},0"
    atalho.Description = descricao
    atalho.save()


def _arquivo_em_uso(erro: Exception) -> bool:
    texto = str(erro)
    return (
        isinstance(erro, PermissionError)
        or "[WinError 32]" in texto
        or "usado por outro processo" in texto
    )


def _copiar_arquivo(origem: Path, destino: Path, limite: float = 90.0) -> None:
    inicio = time.monotonic()
    while True:
        try:
            shutil.copy2(origem, destino)
            return
        except (PermissionError, OSError) as erro:
            if not _arquivo_em_uso(erro) or time.monotonic() - inicio >= limite:
                raise InstalacaoBloqueada(
                    "O Emissor GNRE Faturamento está aberto. Feche o programa e "
                    "clique em Instalar novamente."
                ) from erro
            time.sleep(1.5)


def _copiar_programa(origem: Path, destino: Path, limite: float = 90.0) -> None:
    """Copia o programa; se algum arquivo estiver em uso, tenta de novo.

    O copytree não levanta PermissionError: ele junta as falhas por arquivo em
    shutil.Error. Na atualização automática o app pode reabrir antes da cópia
    terminar (ou haver outra instância aberta) e os arquivos ficam com WinError 32.
    """
    if origem.is_file():
        _copiar_arquivo(origem, destino / origem.name, limite)
        return
    inicio = time.monotonic()
    while True:
        try:
            shutil.copytree(origem, destino, dirs_exist_ok=True)
            return
        except (PermissionError, shutil.Error) as erro:
            if not _arquivo_em_uso(erro) or time.monotonic() - inicio >= limite:
                raise InstalacaoBloqueada(
                    "O Emissor GNRE Faturamento está aberto. Feche o programa e "
                    "clique em Instalar novamente."
                ) from erro
            time.sleep(1.5)


def instalar(com_certificado: bool, registrar=print) -> dict:
    programa = pasta_programa()
    programa.mkdir(parents=True, exist_ok=True)
    origem_app = pasta_app_embutida()
    destino_exe = programa / EXE_NOME
    _copiar_programa(origem_app, programa)
    registrar(f"Programa: {destino_exe}")

    icone = programa / "tottal.ico"
    try:
        shutil.copy2(_origem("tottal.ico"), icone)
    except FileNotFoundError:
        icone = destino_exe

    dados = pasta_config()
    try:
        dados.mkdir(parents=True, exist_ok=True)
    except OSError as erro:
        raise RuntimeError(
            "Não foi possível criar a pasta de configuração do programa.\n\n"
            f"Pasta: {dados}\n\n{erro}"
        ) from erro
    config = dados / "config.json"
    if not config.exists():
        config.write_text(
            json.dumps(CONFIG_MODELO, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
        registrar(f"Configuração criada: {config}")
    else:
        registrar(f"Configuração preservada: {config}")
    (dados / "LEIA-ME.txt").write_text(LEIA_ME, encoding="utf-8")

    pfx = certificado_disponivel()
    copiou_cert = False
    if com_certificado and pfx is not None:
        destino_pfx = dados / pfx.name
        if pfx.resolve() != destino_pfx.resolve():
            shutil.copy2(pfx, destino_pfx)
        copiou_cert = _garantir_certificado(config, destino_pfx)
        registrar(f"Certificado copiado: {destino_pfx}")

    descricao = "Emissor de GNRE do faturamento (NF-e com ST/FCP e CT-e)"
    atalhos = []
    area = pasta_area_trabalho()
    if area.is_dir():
        atalho = area / f"{ATALHO}.lnk"
        _criar_atalho(atalho, destino_exe, icone, descricao)
        atalhos.append(atalho)
        registrar(f"Atalho: {atalho}")

    menu = Path(os.environ.get("APPDATA") or "") / "Microsoft/Windows/Start Menu/Programs"
    if menu.is_dir():
        atalho = menu / f"{ATALHO}.lnk"
        _criar_atalho(atalho, destino_exe, icone, descricao)
        atalhos.append(atalho)
        registrar(f"Menu Iniciar: {atalho}")

    if not atalhos:
        copia = pasta_area_trabalho() / EXE_NOME
        shutil.copy2(destino_exe, copia)
        registrar(f"Atalho indisponível; cópia do programa em {copia}")

    registrar("Instalação concluída.")
    return {
        "programa": destino_exe,
        "config": config,
        "certificado": copiou_cert,
        "atalhos": atalhos,
        "pfx": pfx,
    }


# ---------------------------------------------------------------------------
# interface
# ---------------------------------------------------------------------------


class Instalador:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.fila: queue.Queue = queue.Queue()
        self.resultado: dict | None = None
        self.instalando = False
        self.registro_iniciado = False
        self.pfx = certificado_disponivel()

        root.title(f"Instalar {ATALHO}")
        root.configure(bg=FUNDO)
        root.resizable(False, False)
        try:
            icone = tk.PhotoImage(file=str(_origem("tottal_icone.png")))
            root.iconphoto(True, icone)
            self.icone = icone
        except Exception:
            pass

        self._cabecalho()
        # A barra de ações é ancorada embaixo e empacotada antes do corpo:
        # com escala do Windows (125%/150%) o log encolhe e os botões continuam à vista.
        self._botoes()
        self._corpo()

        root.update_idletasks()
        largura = max(680, root.winfo_reqwidth())
        altura = max(560, root.winfo_reqheight())
        altura = min(altura, max(420, root.winfo_screenheight() - 140))
        largura = min(largura, max(480, root.winfo_screenwidth() - 80))
        tela_x = (root.winfo_screenwidth() - largura) // 2
        tela_y = (root.winfo_screenheight() - altura) // 3
        root.geometry(f"{largura}x{altura}+{tela_x}+{tela_y}")
        root.after(80, self._processar_fila)

    # -- construção ---------------------------------------------------------

    def _cabecalho(self) -> None:
        topo = tk.Frame(self.root, bg=CARTAO)
        topo.pack(fill="x")
        tk.Frame(topo, bg=ACENTO, height=3).pack(fill="x", side="top")
        miolo = tk.Frame(topo, bg=CARTAO)
        miolo.pack(fill="x", padx=18, pady=14)
        try:
            self.logo = tk.PhotoImage(file=str(_origem("tottal_logo_instalador.png")))
            tk.Label(miolo, image=self.logo, bg=CARTAO).pack(side="left", padx=(0, 14))
        except Exception:
            pass
        texto = tk.Frame(miolo, bg=CARTAO)
        texto.pack(side="left", fill="x", expand=True)
        tk.Label(
            texto,
            text="Instalador do Emissor de GNRE",
            bg=CARTAO,
            fg=TEXTO,
            font=("Segoe UI", 15, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            texto,
            text=f"Emissor GNRE Faturamento {VERSAO} · Instalador 1.0 · TOTTAL",
            bg=CARTAO,
            fg=SUAVE,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        tk.Frame(self.root, bg=BORDA, height=1).pack(fill="x")

    def _linha(self, pai: tk.Widget, titulo: str, valor: str) -> None:
        linha = tk.Frame(pai, bg=CARTAO)
        linha.pack(fill="x", pady=4)
        tk.Label(
            linha, text=titulo, bg=CARTAO, fg=SUAVE, font=("Segoe UI", 8, "bold"), width=13,
            anchor="w",
        ).pack(side="left")
        tk.Label(
            linha, text=valor, bg=CARTAO, fg=TEXTO, font=("Segoe UI", 9), anchor="w",
            justify="left", wraplength=500,
        ).pack(side="left", fill="x", expand=True)

    def _corpo(self) -> None:
        corpo = tk.Frame(self.root, bg=FUNDO)
        corpo.pack(fill="both", expand=True, padx=16, pady=(14, 0))

        cartao = tk.Frame(corpo, bg=CARTAO, highlightthickness=1, highlightbackground=BORDA)
        cartao.pack(fill="x")
        dentro = tk.Frame(cartao, bg=CARTAO)
        dentro.pack(fill="x", padx=14, pady=12)
        tk.Label(
            dentro, text="O QUE SERÁ INSTALADO", bg=CARTAO, fg=SUAVE,
            font=("Segoe UI", 8, "bold"), anchor="w",
        ).pack(anchor="w", pady=(0, 6))
        self._linha(dentro, "Programa", str(pasta_programa() / EXE_NOME))
        self._linha(dentro, "Configuração", str(pasta_config()))
        self._linha(dentro, "Atalho", f"Área de Trabalho · {ATALHO}")

        self.var_cert = tk.BooleanVar(value=self.pfx is not None)
        if self.pfx is not None:
            certificado = tk.Checkbutton(
                dentro,
                text=f"Copiar o certificado {self.pfx.name} para a pasta de configuração",
                variable=self.var_cert,
                bg=CARTAO,
                fg=TEXTO,
                activebackground=CARTAO,
                selectcolor=CARTAO,
                font=("Segoe UI", 9),
                anchor="w",
            )
            certificado.pack(anchor="w", pady=(8, 0))
            tk.Label(
                dentro,
                text="A senha do certificado é digitada uma única vez em cada máquina.",
                bg=CARTAO,
                fg=SUAVE,
                font=("Segoe UI", 8),
                anchor="w",
            ).pack(anchor="w", padx=24)

        registro = tk.Frame(corpo, bg=CARTAO, highlightthickness=1, highlightbackground=BORDA)
        registro.pack(fill="both", expand=True, pady=(12, 0))
        self.txt = tk.Text(
            registro,
            height=8,
            bg="#FFFFFF",
            fg=TEXTO,
            relief="flat",
            font=("Consolas", 9),
            padx=10,
            pady=8,
            wrap="word",
        )
        self.txt.pack(fill="both", expand=True, padx=1, pady=1)
        self.txt.configure(state="normal")
        self.txt.insert("1.0", "Os passos da instalação aparecerão aqui.")
        self.txt.configure(state="disabled")

    def _botoes(self) -> None:
        barra = tk.Frame(self.root, bg=FUNDO)
        barra.pack(side="bottom", fill="x", padx=16, pady=14)
        self.lbl_status = tk.Label(
            barra, text="Pronto para instalar.", bg=FUNDO, fg=SUAVE, font=("Segoe UI", 9),
            anchor="w",
        )
        self.lbl_status.pack(side="left")

        self.btn_fechar = self._botao(barra, "Fechar", self.root.destroy, ACENTO_ESCURO, "white")
        self.btn_abrir = self._botao(barra, "Abrir configuração", self._abrir_config, "#FFFFFF", TEXTO, BORDA)
        self.btn_executar = self._botao(barra, "Executar programa", self._executar, "#FFFFFF", TEXTO, BORDA)
        self.btn_instalar = self._botao(barra, "Instalar", self._iniciar, ACENTO, "white")
        self.btn_instalar.pack(side="right", padx=(6, 0))
        self.btn_fechar.pack(side="right")
        self.btn_abrir.pack_forget()
        self.btn_executar.pack_forget()

    def _botao(self, pai, texto, comando, fundo, frente, borda=None):
        return tk.Button(
            pai,
            text=texto,
            command=comando,
            bg=fundo,
            fg=frente,
            activebackground=ACENTO_ESCURO if fundo == ACENTO else fundo,
            activeforeground=frente,
            relief="flat",
            bd=0,
            highlightthickness=1 if borda else 0,
            highlightbackground=borda or fundo,
            padx=16,
            pady=6,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )

    # -- ações --------------------------------------------------------------

    def _registrar(self, mensagem: str) -> None:
        self.txt.configure(state="normal")
        if not self.registro_iniciado:
            self.txt.delete("1.0", "end")
            self.registro_iniciado = True
        self.txt.insert("end", mensagem + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _iniciar(self) -> None:
        if self.instalando:
            return
        self.instalando = True
        for botao in (self.btn_instalar, self.btn_fechar):
            botao.configure(state="disabled")
        self.lbl_status.configure(text="Instalando...", fg=SUAVE)
        threading.Thread(
            target=self._instalar_em_segundo_plano,
            args=(bool(self.var_cert.get()),),
            daemon=True,
        ).start()

    def _instalar_em_segundo_plano(self, com_certificado: bool) -> None:
        try:
            resultado = instalar(com_certificado, lambda m: self.fila.put(("log", m)))
            self.fila.put(("ok", resultado))
        except Exception as erro:
            self.fila.put(("erro", erro))

    def _processar_fila(self) -> None:
        try:
            while True:
                tipo, dado = self.fila.get_nowait()
                if tipo == "log":
                    self._registrar(dado)
                elif tipo == "ok":
                    self._concluir(dado)
                else:
                    self._falhar(dado)
        except queue.Empty:
            pass
        self.root.after(80, self._processar_fila)

    def _concluir(self, resultado: dict) -> None:
        self.resultado = resultado
        self.instalando = False
        self.lbl_status.configure(text="Instalação concluída.", fg=SUCESSO)
        self.btn_instalar.configure(text="Reinstalar", state="normal")
        self.btn_fechar.configure(state="normal")
        self.btn_executar.pack(side="right", padx=(6, 0))
        self.btn_abrir.pack(side="right", padx=(6, 0))
        extra = []
        if resultado["certificado"]:
            extra.append("Certificado copiado para a pasta de configuração.")
        if resultado["pfx"]:
            extra.append(
                "A senha do certificado é digitada uma única vez em cada máquina: "
                "abra o programa e clique em 'Certificado digital'."
            )
        else:
            extra.append(
                "O certificado não acompanhou o instalador: copie o arquivo .pfx para "
                f"{resultado['config'].parent} e clique em 'Certificado digital'."
            )
        messagebox.showinfo(
            "Instalação concluída",
            "O Emissor GNRE Faturamento está instalado.\n\n"
            f"Abra o atalho '{ATALHO}' na Área de Trabalho.\n"
            f"Configuração: {resultado['config'].parent}\n\n" + "\n".join(extra),
            parent=self.root,
        )

    def _falhar(self, erro: Exception) -> None:
        self.instalando = False
        self.lbl_status.configure(text="Falha na instalação.", fg=PERIGO)
        self.btn_instalar.configure(state="normal")
        self.btn_fechar.configure(state="normal")
        self._registrar(f"ERRO: {erro}")
        messagebox.showerror("Falha na instalação", str(erro), parent=self.root)

    def _abrir_config(self) -> None:
        try:
            os.startfile(str(pasta_config()))
        except OSError as erro:
            messagebox.showerror("Erro", str(erro), parent=self.root)

    def _executar(self) -> None:
        try:
            os.startfile(str(pasta_programa() / EXE_NOME))
        except OSError as erro:
            messagebox.showerror("Erro", str(erro), parent=self.root)


def _argumento(nome: str, padrao: str = "") -> str:
    try:
        indice = sys.argv.index(nome)
    except ValueError:
        return padrao
    if indice + 1 < len(sys.argv):
        return sys.argv[indice + 1]
    return padrao


def _aguardar_processo(pid: int, limite: float = 180.0) -> None:
    """Espera o programa em uso terminar; o EXE fica travado até sair."""
    if pid <= 0:
        return
    try:
        import ctypes

        SYNCHRONIZE = 0x00100000
        alca = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not alca:
            return
        try:
            ctypes.windll.kernel32.WaitForSingleObject(alca, int(limite * 1000))
        finally:
            ctypes.windll.kernel32.CloseHandle(alca)
    except OSError:
        pass


def _atualizacao_silenciosa() -> int:
    """Atualização disparada pelo próprio programa.

    Espera o programa antigo fechar, aplica a versão e reabre o programa.
    A janela de progresso e a caixa de erro existem para que uma falha nunca
    termine sem aviso.
    """
    log = Path(os.environ.get("TEMP", ".")) / "atualizar_gnre_faturamento.log"
    pid = int(_argumento("--aguardar-pid", "0") or 0)
    alvo = Path(_argumento("--reabrir") or (pasta_programa() / EXE_NOME))

    root = tk.Tk()
    root.title("Atualização do Emissor GNRE Faturamento")
    root.configure(bg=FUNDO)
    root.resizable(False, False)
    try:
        root.iconphoto(True, tk.PhotoImage(file=str(_origem("tottal_icone.png"))))
    except Exception:
        pass
    tk.Label(
        root,
        text="Aplicando a atualização...",
        bg=FUNDO,
        fg=TEXTO,
        font=("Segoe UI", 11, "bold"),
    ).pack(padx=26, pady=(18, 6))
    barra = ttk.Progressbar(root, mode="indeterminate", length=300)
    barra.pack(padx=26, pady=(0, 10))
    barra.start(12)
    detalhe = tk.Label(
        root,
        text="O programa reabre sozinho no fim.",
        bg=FUNDO,
        fg=SUAVE,
        font=("Segoe UI", 8),
    )
    detalhe.pack(padx=26, pady=(0, 16))
    root.update_idletasks()
    largura, altura = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(
        f"{largura}x{altura}"
        f"+{(root.winfo_screenwidth() - largura) // 2}"
        f"+{(root.winfo_screenheight() - altura) // 2}"
    )

    fila: queue.Queue = queue.Queue()
    estado = {"codigo": 0}

    def tarefa() -> None:
        if pid:
            fila.put(("aviso", "Aguardando o programa fechar..."))
            _aguardar_processo(pid)
        fila.put(("aviso", "Copiando os arquivos da nova versão..."))
        linhas: list[str] = []
        try:
            resultado = instalar(True, linhas.append)
        except Exception as erro:
            fila.put(("erro", (erro, linhas)))
        else:
            fila.put(("ok", (resultado, linhas)))

    threading.Thread(target=tarefa, daemon=True).start()

    def processar() -> None:
        try:
            while True:
                tipo, dado = fila.get_nowait()
                if tipo == "aviso":
                    detalhe.configure(text=dado)
                elif tipo == "ok":
                    resultado, linhas = dado
                    log.write_text(
                        "\n".join(linhas) + f"\nprograma={resultado['programa']}\n",
                        encoding="utf-8",
                    )
                    barra.stop()
                    root.destroy()
                    return
                else:
                    erro, linhas = dado
                    estado["codigo"] = 1
                    barra.stop()
                    log.write_text(
                        "\n".join(linhas) + f"\nERRO: {erro}\n", encoding="utf-8"
                    )
                    root.withdraw()
                    messagebox.showerror(
                        "Atualização não concluída",
                        "A atualização não foi aplicada.\n\n"
                        f"{erro}\n\nO programa será reaberto na versão atual.\n"
                        f"Detalhes: {log}",
                        parent=root,
                    )
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(80, processar)

    root.after(80, processar)
    root.mainloop()

    programa = alvo if alvo.exists() else pasta_programa() / EXE_NOME
    if programa.exists():
        try:
            os.startfile(str(programa))
        except OSError:
            pass
    return estado["codigo"]


def principal() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    if "--silencioso" in sys.argv:
        sys.exit(_atualizacao_silenciosa())

    root = tk.Tk()
    Instalador(root)
    root.mainloop()


if __name__ == "__main__":
    principal()
