"""Frente e fluxo do CT-e (modelo 57), iguais aos do app de transporte.

O modo CT-e emite a GNRE do transportador (TRANSREIS): a frente troca para
os campos do CT-e (número, XML ou DACTE) e o restante do app continua igual.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import threading
import time
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import tkinter as tk
import ttkbootstrap as ttkb

from calendario import vencimento_padrao
from certificado import carregar_certificado

import cte_consulta
import cte_edocs
import cte_gnre
from cte_gnre import (
    ClienteGNRE,
    ResultadoEmissao,
    baixar_pdf_recibo,
    detalhes_do_recibo,
    emitir_guia,
    numero_do_documento,
    resolver_contribuinte,
)
from cte_leitor import DadosCTe, ler_cte_xml, validar_chave

from app import ALERTA, CARTAO, FUNDO, PERIGO, Quadro, Rotulo, SUCESSO, TEXTO, TEXTO_SUAVE


class PainelCTe:
    def __init__(self, master: tk.Widget, app, dados_dir: Path):
        self.app = app
        self.root = app.root
        self.dados_dir = Path(dados_dir)
        self.config: cte_gnre.ConfigGNRE | None = None

        self.dados: DadosCTe | None = None
        self.arquivo_lido: str | None = None
        self.arquivos: list[str] = []
        self.arquivos_lidos: dict[str, DadosCTe] = {}
        self.resultado: ResultadoEmissao | None = None
        self.pdf_path: Path | None = None
        self.em_execucao = False
        self._pdf_retry_id = 0
        self._lote_info: tuple[int, int] | None = None

        self.modo_entrada = "numero"
        self.chaves: list[dict] = []
        self.xml_dir = self.dados_dir / "xmls"
        self._ultima_distribuicao = 0.0
        self.fila: queue.Queue = queue.Queue()
        self._fila_chaves: queue.Queue = queue.Queue()
        threading.Thread(target=self._baixar_chaves, daemon=True).start()

        self.var_chave = tk.StringVar()
        self.var_arquivo = tk.StringVar()
        self.var_pdf = tk.StringVar()
        self.var_modo = tk.StringVar(value="unica")
        self.var_uf = tk.StringVar()
        self.var_valor = tk.StringVar()
        self.var_vencimento = tk.StringVar(
            value=vencimento_padrao().strftime("%d/%m/%Y")
        )
        self.var_pagamento = tk.StringVar(value=date.today().strftime("%d/%m/%Y"))
        self.var_ambiente = tk.StringVar(value="2")

        self._montar(master)
        self._carregar_config()
        self.root.after(150, self._processar_fila)

    # ------------------------------------------------------------------
    # tela
    # ------------------------------------------------------------------

    def _montar(self, master: tk.Widget) -> None:
        titulo_area = Quadro(master, bg=FUNDO)
        titulo_area.pack(fill="x", pady=(0, 5))
        Rotulo(
            titulo_area,
            text="Nova guia",
            bg=FUNDO,
            fg=TEXTO,
            font=(self.app.fonte_titulo, 15, "bold"),
        ).pack(anchor="w")
        Rotulo(
            titulo_area,
            text="Emita a GNRE a partir do CT-e autorizado.",
            bg=FUNDO,
            fg=TEXTO_SUAVE,
            font=(self.app.fonte, 8),
        ).pack(anchor="w", pady=(1, 0))

        cartao_cte = self.app._cartao(
            master, "CT-e autorizado", "informe o número, o XML ou o PDF"
        )
        linha_abas = Quadro(cartao_cte, bg=CARTAO)
        linha_abas.pack(fill="x", pady=(0, 5))
        self.btn_aba_numero = ttk.Button(
            linha_abas,
            text="Número do CT-e",
            style="AbaAtiva.TButton",
            command=lambda: self._trocar_aba("numero"),
        )
        self.btn_aba_numero.pack(side="left")
        self.btn_aba_xml = ttk.Button(
            linha_abas,
            text="XML",
            style="Aba.TButton",
            command=lambda: self._trocar_aba("xml"),
        )
        self.btn_aba_xml.pack(side="left", padx=(4, 0))
        self.btn_aba_pdf = ttk.Button(
            linha_abas,
            text="PDF",
            style="Aba.TButton",
            command=lambda: self._trocar_aba("pdf"),
        )
        self.btn_aba_pdf.pack(side="left", padx=(4, 0))
        self.legenda_aba = Rotulo(
            linha_abas,
            text="Informe o número do CT-e autorizado.",
            bg=CARTAO,
            fg=TEXTO_SUAVE,
            font=(self.app.fonte, 8),
        )
        self.legenda_aba.pack(side="right")

        linha_modo = Quadro(cartao_cte, bg=CARTAO)
        linha_modo.pack(fill="x", pady=(0, 5))
        ttk.Label(linha_modo, text="Modo:", style="Legenda.TLabel").pack(
            side="left", padx=(0, 8)
        )
        ttkb.Radiobutton(
            linha_modo,
            text="Gerar GNRE única",
            value="unica",
            variable=self.var_modo,
            bootstyle="primary",
        ).pack(side="left")
        ttkb.Radiobutton(
            linha_modo,
            text="Gerar várias GNRE",
            value="varias",
            variable=self.var_modo,
            bootstyle="primary",
        ).pack(side="left", padx=(14, 0))

        self.painel_numero = Quadro(cartao_cte, bg=CARTAO)
        self.painel_numero.pack(fill="x")
        linha_numero = Quadro(self.painel_numero, bg=CARTAO)
        linha_numero.pack(fill="x")
        self.entrada_numero = ttkb.Entry(
            linha_numero, textvariable=self.var_chave, style="Campo.TEntry"
        )
        self.entrada_numero.pack(side="left", fill="x", expand=True)
        self.entrada_numero.bind(
            "<Return>", lambda _evento: self._adicionar_numero()
        )
        ttkb.Button(
            linha_numero,
            text="Adicionar",
            bootstyle="secondary",
            command=self._adicionar_numero,
        ).pack(side="left", padx=(8, 0))
        ttkb.Button(
            linha_numero,
            text="Remover",
            bootstyle="secondary",
            command=self._remover_chave,
        ).pack(side="left", padx=(4, 0))
        ttkb.Button(
            linha_numero,
            text="Limpar",
            bootstyle="secondary",
            command=self._limpar_chaves,
        ).pack(side="left", padx=(4, 0))

        caixa_lista = Quadro(self.painel_numero, bg=CARTAO)
        self.caixa_lista = caixa_lista
        self.lista_chaves = ttk.Treeview(
            caixa_lista,
            columns=("chave", "situacao"),
            show="",
            height=1,
            selectmode="browse",
            style="Chaves.Treeview",
        )
        self.lista_chaves.column("chave", width=350, anchor="w")
        self.lista_chaves.column("situacao", width=190, anchor="w", stretch=True)
        barra_lista = ttk.Scrollbar(
            caixa_lista, orient="vertical", command=self.lista_chaves.yview
        )
        self.lista_chaves.configure(yscrollcommand=barra_lista.set)
        self.lista_chaves.pack(side="left", fill="x", expand=True)
        barra_lista.pack(side="left", fill="y")
        self.lista_chaves.tag_configure("pendente", foreground=ALERTA)
        self.lista_chaves.tag_configure("pronto", foreground=SUCESSO)
        self.lista_chaves.tag_configure("erro", foreground=PERIGO)

        self.painel_xml = Quadro(cartao_cte, bg=CARTAO)
        linha_cte = Quadro(self.painel_xml, bg=CARTAO)
        linha_cte.pack(fill="x")
        self.entrada_arquivo = ttkb.Entry(
            linha_cte, textvariable=self.var_arquivo, style="Campo.TEntry"
        )
        self.entrada_arquivo.pack(side="left", fill="x", expand=True)
        self.entrada_arquivo.bind("<Return>", lambda _evento: self._ler_cte())
        ttkb.Button(
            linha_cte,
            text="Procurar",
            bootstyle="secondary",
            command=self._procurar,
        ).pack(side="left", padx=(8, 0))
        ttkb.Button(
            linha_cte,
            text="Ler e conferir",
            bootstyle="secondary",
            command=self._ler_cte,
        ).pack(side="left", padx=(8, 0))

        self.painel_pdf = Quadro(cartao_cte, bg=CARTAO)
        linha_pdf = Quadro(self.painel_pdf, bg=CARTAO)
        linha_pdf.pack(fill="x")
        self.entrada_pdf = ttkb.Entry(
            linha_pdf, textvariable=self.var_pdf, style="Campo.TEntry"
        )
        self.entrada_pdf.pack(side="left", fill="x", expand=True)
        self.entrada_pdf.bind("<Return>", lambda _evento: self._ler_pdf())
        ttkb.Button(
            linha_pdf,
            text="Procurar",
            bootstyle="secondary",
            command=self._procurar_pdf,
        ).pack(side="left", padx=(8, 0))
        ttkb.Button(
            linha_pdf,
            text="Ler e conferir",
            bootstyle="secondary",
            command=self._ler_pdf,
        ).pack(side="left", padx=(8, 0))

        cartao_dados = self.app._cartao(
            master, "Dados da guia", "confira antes de emitir"
        )
        grade = Quadro(cartao_dados, bg=CARTAO)
        grade.pack(fill="x")
        self.entrada_uf = self.app._campo(grade, 0, "UF favorecida", self.var_uf, 8)
        self.app._campo(grade, 1, "Valor da guia", self.var_valor, 16)
        self.app._campo(grade, 2, "Vencimento", self.var_vencimento, 13)
        self.app._campo(grade, 3, "Pagamento", self.var_pagamento, 13)
        self.entrada_uf.configure(state="readonly")

        linha_acao = Quadro(cartao_dados, bg=CARTAO)
        linha_acao.pack(fill="x", pady=(9, 0))
        self.btn_emitir = ttk.Button(
            linha_acao,
            text="Emitir guia",
            style="Acao.TButton",
            command=self._emitir,
        )
        self.btn_emitir.pack(side="right")
        ttk.Label(linha_acao, text="Ambiente:", style="Valor.TLabel").pack(
            side="left", padx=(0, 10)
        )
        ttkb.Radiobutton(
            linha_acao,
            text="Homologação (teste)",
            value="2",
            variable=self.var_ambiente,
            bootstyle="primary",
        ).pack(side="left")
        ttkb.Radiobutton(
            linha_acao,
            text="Produção (guia real)",
            value="1",
            variable=self.var_ambiente,
            bootstyle="primary",
        ).pack(side="left", padx=(14, 0))

        self.var_modo.trace_add("write", lambda *_args: self._mudar_modo())
        self._ajustar_lista_chaves()

    def _carregar_config(self) -> None:
        try:
            config = cte_gnre.carregar_config(self.dados_dir / "config.json")
        except Exception as erro:
            self.app._log(f"AVISO: configuração do CT-e não carregada ({erro}).")
            return
        config.arquivo_regras = self.dados_dir / "regras_cte.json"
        config.cache_config = self.dados_dir / "cache_cte"
        self.config = config

    def focar(self) -> None:
        if self.modo_entrada == "xml":
            self.entrada_arquivo.focus_set()
        elif self.modo_entrada == "pdf":
            self.entrada_pdf.focus_set()
        else:
            self.entrada_numero.focus_set()

    def _status(self, texto: str) -> None:
        self.app.var_status.set(texto)

    def _log(self, mensagem: str) -> None:
        self.app._log(mensagem)

    # ------------------------------------------------------------------
    # abas e modo
    # ------------------------------------------------------------------

    def _limpar_leitura(self) -> None:
        self.arquivos_lidos = {}
        self.dados = None
        self.arquivo_lido = None
        self.resultado = None
        self.pdf_path = None
        self.var_uf.set("")
        self.var_valor.set("")
        self.var_vencimento.set(vencimento_padrao().strftime("%d/%m/%Y"))
        self.var_pagamento.set(date.today().strftime("%d/%m/%Y"))
        self.entrada_uf.configure(state="readonly")

    def _trocar_aba(self, modo: str) -> None:
        if modo == self.modo_entrada:
            self.focar()
            return
        self.modo_entrada = modo
        for painel in (self.painel_numero, self.painel_xml, self.painel_pdf):
            painel.pack_forget()
        for botao in (self.btn_aba_numero, self.btn_aba_xml, self.btn_aba_pdf):
            botao.configure(style="Aba.TButton")
        if modo == "numero":
            self.painel_numero.pack(fill="x")
            self.btn_aba_numero.configure(style="AbaAtiva.TButton")
            self.legenda_aba.configure(text="Informe o número do CT-e autorizado.")
            self._status(
                "Informe o número do CT-e. O XML é baixado no portal Edocs."
            )
        elif modo == "pdf":
            self.painel_pdf.pack(fill="x")
            self.btn_aba_pdf.configure(style="AbaAtiva.TButton")
            self.legenda_aba.configure(text="Selecione o DACTE em PDF.")
            self._status(
                "Selecione o PDF do DACTE: a chave do CT-e é lida do documento."
            )
        else:
            self.painel_xml.pack(fill="x")
            self.btn_aba_xml.configure(style="AbaAtiva.TButton")
            self.legenda_aba.configure(text="Selecione o XML autorizado.")
            self._status("Selecione o XML do CT-e.")
        self._limpar_leitura()
        self.focar()

    @staticmethod
    def _formatar_chave(chave: str) -> str:
        return " ".join(chave[i : i + 4] for i in range(0, len(chave), 4))

    @staticmethod
    def _moeda(valor: float) -> str:
        texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {texto}"

    def _rotulo_valor(self, lidos: dict[str, DadosCTe]) -> str:
        valores = [dados.valor_icms for dados in lidos.values() if dados.valor_icms > 0]
        if not valores:
            return ""
        distintos = {round(valor, 2) for valor in valores}
        if len(distintos) == 1:
            texto = self._moeda(valores[0])
            return f"{texto} cada" if len(valores) > 1 else texto
        return f"{self._moeda(sum(valores))} total"

    def _resetar_selecao(self) -> None:
        self.entrada_arquivo.configure(state="normal")
        self.arquivos = []
        self.arquivos_lidos = {}
        self.dados = None
        self.arquivo_lido = None
        self.resultado = None
        self.pdf_path = None
        self.var_arquivo.set("")
        self.var_uf.set("")
        self.var_valor.set("")
        self.var_vencimento.set(vencimento_padrao().strftime("%d/%m/%Y"))
        self.var_pagamento.set(date.today().strftime("%d/%m/%Y"))
        self.entrada_uf.configure(state="readonly")
        self._atualizar_selecao()

    def _modo_varias(self) -> bool:
        return self.var_modo.get() == "varias"

    def _mudar_modo(self) -> None:
        self._limpar_leitura()
        self._ajustar_lista_chaves()
        if self.modo_entrada == "numero":
            if self._modo_varias():
                self._status(
                    "Modo várias GNRE: adicione os números dos CT-es do mesmo estado."
                )
            else:
                self._status("Modo GNRE única: informe o número do CT-e.")
        elif self.modo_entrada == "pdf":
            self._status("Selecione o PDF do DACTE do CT-e.")
        elif self._modo_varias():
            self._status("Modo várias GNRE: selecione XMLs do mesmo estado.")
        else:
            self._status("Modo GNRE única: selecione um XML do CT-e.")

    def _ajustar_lista_chaves(self) -> None:
        """A lista só aparece no modo várias; na GNRE única fica oculta."""
        if self._modo_varias():
            self.lista_chaves.configure(height=2)
            self.caixa_lista.pack(fill="x", pady=(5, 0))
            return
        self.caixa_lista.pack_forget()
        self.lista_chaves.configure(height=1)
        if len(self.chaves) <= 1:
            return
        ultima = self.chaves[-1]
        for chave in list(self.lista_chaves.get_children()):
            if chave != ultima["chave"]:
                self.lista_chaves.delete(chave)
        self.chaves = [ultima]
        self._limpar_leitura()
        self._log("Modo GNRE única: mantida apenas a última chave da lista.")

    def _atualizar_selecao(self) -> None:
        self.btn_emitir.configure(
            text="Emitir guias" if self._modo_varias() else "Emitir guia"
        )

    # ------------------------------------------------------------------
    # entrada por número (ou chave) do CT-e
    # ------------------------------------------------------------------

    def _item_chave(self, chave: str) -> dict | None:
        return next((item for item in self.chaves if item["chave"] == chave), None)

    @staticmethod
    def _rotulo_chave(chave: str) -> str:
        try:
            numero, serie = cte_edocs.numero_e_serie_da_chave(chave)
        except Exception:
            return chave
        if not numero:
            return chave
        return f"CT-e nº {numero} · série {serie}"

    def _linha_chave(self, item: dict) -> None:
        if not self.lista_chaves.exists(item["chave"]):
            return
        self.lista_chaves.item(
            item["chave"],
            values=(self._rotulo_chave(item["chave"]), item["situacao"]),
            tags=(item["tag"],),
        )

    def _definir_situacao(self, chave: str, situacao: str, tag: str) -> None:
        item = self._item_chave(chave)
        if item is None:
            return
        item["situacao"] = situacao
        item["tag"] = tag
        self._linha_chave(item)

    def _adicionar_numero(self) -> None:
        """Aceita só o número do CT-e (até 9 dígitos) ou a chave completa."""
        texto = re.sub(r"\D", "", self.var_chave.get())
        if not texto:
            return
        self.var_chave.set("")
        if len(texto) == 44:
            self._entrada_por_chave(texto)
            return
        if len(texto) > 9:
            messagebox.showwarning(
                "Número inválido",
                "Informe apenas o número do CT-e (até 9 dígitos). Se preferir, "
                "cole a chave completa com 44 dígitos.",
            )
            self._status(
                "Número inválido: use só o número do CT-e ou a chave de 44 dígitos."
            )
            return
        self._buscar_numero(texto)

    def _entrada_por_chave(self, chave: str) -> None:
        try:
            validar_chave(chave)
        except Exception as erro:
            messagebox.showwarning("Chave inválida", str(erro))
            self._status("Chave inválida. Confira os 44 dígitos.")
            return
        if self._item_chave(chave) is not None:
            messagebox.showinfo("CT-e repetido", "Este CT-e já está na lista.")
            return
        if self.modo_entrada != "numero":
            self._trocar_aba("numero")
        if not self._modo_varias() and self.chaves:
            for antiga in list(self.lista_chaves.get_children()):
                self.lista_chaves.delete(antiga)
            self.chaves = []
            self._limpar_leitura()
            self._log("Modo GNRE única: o CT-e anterior foi substituído.")
        item = {
            "chave": chave,
            "situacao": "Aguardando o XML...",
            "tag": "pendente",
            "caminho": None,
            "erro": None,
            "ambiente": self.var_ambiente.get(),
        }
        self.chaves.append(item)
        self.lista_chaves.insert(
            "",
            "end",
            iid=chave,
            values=(self._rotulo_chave(chave), item["situacao"]),
            tags=("pendente",),
        )
        self.lista_chaves.see(chave)
        self._ajustar_lista_chaves()
        self._log(f"CT-e adicionado: {self._rotulo_chave(chave)}")
        self._status("Baixando o XML do CT-e...")
        self._fila_chaves.put(chave)

    def _buscar_numero(self, numero: str) -> None:
        """Entrada só com o número do CT-e: resolve no Edocs e baixa o XML."""
        numero = numero.lstrip("0") or "0"
        if self.app._conta_edocs() is None:
            messagebox.showinfo(
                "Conta do portal Edocs",
                "Para buscar pelo número do CT-e, cadastre a conta do portal "
                "Edocs no menu → Conta do Edocs.",
            )
            self._status("Cadastre a conta do Edocs para buscar pelo número.")
            return
        if self.modo_entrada != "numero":
            self._trocar_aba("numero")
        self._status(f"Localizando o CT-e nº {numero} no Edocs...")
        self._log(f"Buscando o CT-e nº {numero} no portal Edocs...")
        self._fila_chaves.put(("numero", numero))

    def _resolver_numero(self, numero: str) -> None:
        conta = self.app._conta_edocs()
        if conta is None:
            self.fila.put(
                (
                    "numero_erro",
                    (numero, "A conta do portal Edocs não está cadastrada."),
                )
            )
            return
        try:
            with cte_edocs.PortalEdocs(
                conta, log=lambda mensagem: self.fila.put(("log", mensagem))
            ) as portal:
                chave, caminho = portal.obter(numero, self.xml_dir)
        except Exception as erro:
            self.fila.put(("numero_erro", (numero, f"{erro}")))
            return
        self.fila.put(("numero_ok", (numero, chave, str(caminho))))

    def _chave_do_numero(self, numero: str, chave: str, caminho: str) -> None:
        if self._item_chave(chave) is None:
            if not self._modo_varias() and self.chaves:
                for antiga in list(self.lista_chaves.get_children()):
                    self.lista_chaves.delete(antiga)
                self.chaves = []
                self._limpar_leitura()
            item = {
                "chave": chave,
                "situacao": "XML pronto",
                "tag": "pronto",
                "caminho": caminho,
                "erro": None,
                "ambiente": self.var_ambiente.get(),
            }
            self.chaves.append(item)
            self.lista_chaves.insert(
                "",
                "end",
                iid=chave,
                values=(self._rotulo_chave(chave), item["situacao"]),
                tags=("pronto",),
            )
            self.lista_chaves.see(chave)
            self._ajustar_lista_chaves()
            self._log(
                f"CT-e nº {numero} localizado no Edocs: "
                f"{self._formatar_chave(chave)}"
            )
        item = self._item_chave(chave)
        if item is not None:
            item["caminho"] = caminho
            item["erro"] = None
        self._definir_situacao(chave, "XML pronto", "pronto")
        self._status("CT-e localizado no Edocs. Lendo os dados da guia...")
        self._ler_cte(avisar=False)

    def _baixar_do_edocs(self, chave: str) -> str | None:
        conta = self.app._conta_edocs()
        if conta is None:
            return None
        self.fila.put(
            (
                "log",
                "XML não está na distribuição da SEFAZ; procurando no portal Edocs...",
            )
        )
        try:
            with cte_edocs.PortalEdocs(
                conta, log=lambda mensagem: self.fila.put(("log", mensagem))
            ) as portal:
                _, caminho = portal.obter_por_chave(chave, self.xml_dir)
        except Exception as erro:
            self.fila.put(("log", f"Edocs: {erro}"))
            return None
        return str(caminho)

    def _baixar_chaves(self) -> None:
        while True:
            pedido = self._fila_chaves.get()
            if pedido is None:
                return
            if isinstance(pedido, tuple):
                self._resolver_numero(pedido[1])
                continue
            chave = pedido
            item = self._item_chave(chave)
            if item is None:
                continue
            cert = self.app.certificado
            if cert is None and self.config is not None:
                try:
                    cert = carregar_certificado(
                        self.config.caminho_pfx, self.config.senha
                    )
                except Exception as erro:
                    self.fila.put(
                        (
                            "chave_erro",
                            (chave, f"Certificado digital indisponível: {erro}"),
                        )
                    )
                    continue
            if cert is None:
                self.fila.put(
                    ("chave_erro", (chave, "Certificado digital não carregado."))
                )
                continue
            cache = self.xml_dir / f"{chave}.xml"
            if cache.exists() and time.monotonic() - self._ultima_distribuicao < 300:
                self.fila.put(("chave_pronta", (chave, str(cache))))
                continue
            self.fila.put(("chave_situacao", (chave, "Consultando a SEFAZ...")))
            config = self.config
            # O CT-e é documento de produção mesmo quando a GNRE é emitida em
            # homologação; a homologação só entra como segunda tentativa.
            ambientes = ["1"] + (["2"] if item["ambiente"] != "1" else [])
            caminho = None
            falhas: dict[str, str] = {}
            for ambiente in ambientes:
                if ambiente == "2":
                    self.fila.put(
                        ("log", "CT-e não está em produção; consultando a homologação.")
                    )
                try:
                    with cte_consulta.DistribuicaoCTe(
                        cert,
                        self.xml_dir,
                        ambiente=ambiente,
                        uf_autor=config.uf_autor if config is not None else "42",
                        timeout=config.timeout if config is not None else 60,
                        verificar_ssl=config.verificar_ssl if config is not None else True,
                        log=lambda mensagem: self.fila.put(("log", mensagem)),
                    ) as cliente:
                        caminho = cliente.obter(chave)
                    break
                except Exception as erro:
                    falhas[ambiente] = f"{erro}"
            self._ultima_distribuicao = time.monotonic()
            if caminho is None:
                caminho = self._baixar_do_edocs(chave)
            if caminho is not None:
                self.fila.put(("chave_pronta", (chave, str(caminho))))
            else:
                mensagem = falhas.get("1") or next(iter(falhas.values()))
                self.fila.put(("chave_erro", (chave, mensagem)))

    def _remover_chave(self) -> None:
        selecao = self.lista_chaves.selection()
        if not selecao:
            messagebox.showwarning("Aviso", "Selecione o CT-e que deseja remover.")
            return
        chave = selecao[0]
        self.lista_chaves.delete(chave)
        self.chaves = [item for item in self.chaves if item["chave"] != chave]
        if self._caminhos_selecionados():
            self._ler_cte(avisar=False)
        else:
            self._limpar_leitura()
            self._status("Nenhum CT-e selecionado.")

    def _limpar_chaves(self) -> None:
        for chave in list(self.lista_chaves.get_children()):
            self.lista_chaves.delete(chave)
        self.chaves = []
        self._limpar_leitura()
        self._status("Lista de CT-es limpa.")

    # ------------------------------------------------------------------
    # DACTE e XML
    # ------------------------------------------------------------------

    @staticmethod
    def _chave_do_dacte(caminho: Path) -> str:
        """Lê a chave do CT-e impressa no DACTE (modelo 57)."""
        try:
            from pypdf import PdfReader
        except ImportError as erro:
            raise RuntimeError(
                "Leitura de PDF indisponível neste computador. "
                "Use a aba Número do CT-e."
            ) from erro
        try:
            leitor = PdfReader(str(caminho))
            texto = "\n".join(
                pagina.extract_text() or "" for pagina in leitor.pages[:3]
            )
        except Exception as erro:
            raise RuntimeError(f"PDF ilegível: {erro}") from erro

        candidatas: list[str] = []
        for bruto in re.findall(r"\d(?:[ -]?\d){43,79}", texto):
            digitos = re.sub(r"\D", "", bruto)
            for inicio in range(len(digitos) - 43):
                chave = digitos[inicio : inicio + 44]
                if chave[20:22] != "57" or chave in candidatas:
                    continue
                try:
                    validar_chave(chave)
                except Exception:
                    continue
                candidatas.append(chave)
        if candidatas:
            return candidatas[0]
        raise RuntimeError(
            "Não encontrei a chave do CT-e neste PDF. Confira se é o DACTE e, "
            "se o arquivo for digitalizado sem texto, use a aba Número do CT-e."
        )

    def _procurar_pdf(self) -> None:
        escolhido = filedialog.askopenfilename(
            title="Selecionar o DACTE em PDF",
            filetypes=[("Arquivos PDF", "*.pdf"), ("Todos os arquivos", "*.*")],
        )
        if not escolhido:
            return
        self.var_pdf.set(escolhido)
        self._ler_pdf()

    def _ler_pdf(self) -> None:
        caminho = Path(self.var_pdf.get().strip().strip('"'))
        if not caminho.is_file():
            messagebox.showwarning("PDF", "Selecione o PDF do DACTE do CT-e.")
            return
        self._status("Lendo a chave do CT-e no PDF...")
        try:
            chave = self._chave_do_dacte(caminho)
        except Exception as erro:
            self._status("Não foi possível ler a chave no PDF.")
            self._log(f"ERRO no PDF {caminho.name}: {erro}")
            messagebox.showerror("PDF", f"{erro}")
            return
        self._log(f"PDF lido: {caminho.name} · {self._rotulo_chave(chave)}")
        if self.modo_entrada != "numero":
            self._trocar_aba("numero")
        self._entrada_por_chave(chave)

    def _procurar(self) -> None:
        if self._modo_varias():
            arquivos = filedialog.askopenfilenames(
                title="Selecionar XMLs do CT-e (mesmo estado)",
                filetypes=[("Arquivos XML", "*.xml"), ("Todos os arquivos", "*.*")],
            )
        else:
            escolhido = filedialog.askopenfilename(
                title="Selecionar XML do CT-e",
                filetypes=[("Arquivos XML", "*.xml"), ("Todos os arquivos", "*.*")],
            )
            arquivos = (escolhido,) if escolhido else ()
        if not arquivos:
            return
        self._resetar_selecao()
        caminhos = [str(Path(arquivo).resolve()) for arquivo in arquivos]
        if len(caminhos) == 1:
            self.var_arquivo.set(caminhos[0])
            self._log(f"Arquivo selecionado: {caminhos[0]}")
            if self._modo_varias():
                self._status("Lendo o CT-e...")
                self._ler_cte()
            else:
                self._status("Clique em 'Ler e conferir'.")
            return

        self.arquivos = caminhos
        self.var_arquivo.set(f"{len(caminhos)} XMLs selecionados")
        self.entrada_arquivo.configure(state="readonly")
        self._log(f"{len(caminhos)} arquivos selecionados:")
        for caminho in caminhos:
            self._log(f"- {caminho}")
        self._status("Lendo os CT-es...")
        self._ler_cte()

    def _caminhos_selecionados(self) -> list[str]:
        if self.modo_entrada == "numero":
            return [
                item["caminho"]
                for item in self.chaves
                if item.get("caminho") and not item.get("erro")
            ]
        if len(self.arquivos) > 1:
            return list(self.arquivos)
        caminho = self.var_arquivo.get().strip().strip('"')
        return [caminho] if caminho else []

    def _ler_cte(self, avisar: bool = True) -> None:
        caminhos = self._caminhos_selecionados()
        if not caminhos:
            if avisar:
                messagebox.showwarning(
                    "Aviso", "Selecione um arquivo XML de CT-e válido."
                )
            else:
                self._status("Aguardando o XML do CT-e.")
            return
        if self.config is None:
            if avisar:
                messagebox.showerror(
                    "Erro", "Configuração inválida. Corrija o config.json."
                )
            else:
                self._status("Configuração inválida. Corrija o config.json.")
            return

        for caminho in caminhos:
            if not Path(caminho).exists():
                self._status("Arquivo não encontrado.")
                if avisar:
                    messagebox.showwarning(
                        "Aviso", f"Arquivo não encontrado:\n{caminho}"
                    )
                else:
                    self._log(f"ERRO: arquivo não encontrado: {caminho}")
                return

        self.arquivos_lidos = {}
        self.dados = None
        self.arquivo_lido = None
        self.var_uf.set("")
        self.var_valor.set("")
        self._atualizar_selecao()

        lidos: dict[str, DadosCTe] = {}
        for caminho in caminhos:
            try:
                lidos[str(Path(caminho).resolve())] = ler_cte_xml(caminho)
            except Exception as erro:
                self._status("Erro ao ler o CT-e.")
                self._log(f"ERRO ao ler o CT-e {caminho}: {erro}")
                if avisar:
                    messagebox.showerror(
                        "Erro ao ler o CT-e",
                        f"{Path(caminho).name}\n\n{erro}",
                    )
                return

        lote = self._modo_varias() or len(lidos) > 1
        ufs = {dados.uf_favorecida for dados in lidos.values() if dados.uf_favorecida}
        sem_uf = [caminho for caminho, dados in lidos.items() if not dados.uf_favorecida]

        if len(ufs) > 1:
            lista = ", ".join(sorted(ufs))
            self._status(f"XMLs de UFs diferentes ({lista}) — lote não carregado.")
            self._log(f"ERRO: os XMLs do lote têm UFs diferentes: {lista}.")
            if avisar:
                messagebox.showerror(
                    "UFs diferentes",
                    f"Os XMLs selecionados são de UFs diferentes ({lista}).\n"
                    "Selecione apenas arquivos do mesmo estado para gerar o lote.",
                )
            return
        if lote and sem_uf:
            nomes = "\n".join(f"- {Path(caminho).name}" for caminho in sem_uf)
            self._status("XML sem UF favorecida — lote não carregado.")
            self._log("ERRO: o lote contém XML sem UF favorecida:")
            for caminho in sem_uf:
                self._log(f"- {Path(caminho).name}")
            if avisar:
                messagebox.showerror(
                    "UF não informada",
                    "Estes XMLs não trazem a UF favorecida (ICMSOutraUF):\n"
                    f"{nomes}\n\nO lote exige a UF definida em todos os arquivos. "
                    "Emita esses arquivos individualmente e informe a UF.",
                )
            return

        self.arquivos_lidos = lidos
        self.arquivo_lido, self.dados = next(iter(lidos.items()))
        uf_comum = next(iter(ufs), "")
        self.var_uf.set(uf_comum)
        self.var_valor.set(self._rotulo_valor(lidos))
        venc = vencimento_padrao(uf_comum or None)
        self.var_vencimento.set(venc.strftime("%d/%m/%Y"))
        self.var_pagamento.set(date.today().strftime("%d/%m/%Y"))
        if uf_comum:
            self.entrada_uf.configure(state="readonly")
        else:
            self.entrada_uf.configure(state="normal")
            self.entrada_uf.focus_set()

        self._log("")
        if lote:
            if len(lidos) > 1:
                self._log(f"=== Lote de {len(lidos)} CT-es lidos (UF {uf_comum}) ===")
            else:
                self._log(f"=== CT-e lido (UF {uf_comum}) — modo lote ===")
            for caminho, dados in lidos.items():
                valor = f"{dados.valor_icms:,.2f}".replace(",", "X")
                valor = valor.replace(".", ",").replace("X", ".")
                self._log(
                    f"- CT-e {dados.numero} (série {dados.serie}) | "
                    f"{Path(caminho).name} | R$ {valor} | "
                    f"{dados.uf_ini} -> {dados.uf_fim}"
                )
                for aviso in dados.avisos:
                    self._log(f"  AVISO: {aviso}")
            self._log("Cada XML gera uma guia separada; a conferência é individual.")
        else:
            self._log("=== CT-e lido ===")
            for linha in self.dados.resumo().splitlines():
                self._log(linha)
            for aviso in self.dados.avisos:
                self._log(f"AVISO: {aviso}")

        if not lote and self.app.certificado is not None:
            try:
                contribuinte = resolver_contribuinte(
                    self.dados, self.app.certificado, self.config
                )
                self._log(
                    f"Contribuinte: {contribuinte.nome} | CNPJ {contribuinte.cnpj} | "
                    "CNPJ + endereço (sem IE na UF favorecida)"
                )
            except Exception as erro:
                self._log(f"AVISO: {erro}")

        if not lote and not self.dados.uf_favorecida:
            self._log(
                "AVISO: este CT-e não trouxe ICMSOutraUF. "
                "O campo UF favorecida foi liberado para preenchimento manual."
            )

        self._atualizar_selecao()
        if lote:
            if len(lidos) > 1:
                self._status(
                    f"{len(lidos)} CT-es lidos. Uma guia será gerada para cada XML."
                )
            else:
                self._status("CT-e lido. O modo lote gera uma guia para o XML.")
        else:
            self._status("CT-e lido. Confira os dados e escolha a ação.")

    # ------------------------------------------------------------------
    # emissão
    # ------------------------------------------------------------------

    def _emitir(self) -> None:
        if self.em_execucao:
            return
        if self.modo_entrada == "numero":
            pendentes = [item for item in self.chaves if not item.get("caminho")]
            if pendentes:
                messagebox.showwarning(
                    "Aviso",
                    f"{len(pendentes)} CT-e(s) ainda sem XML. Aguarde o download "
                    "ou remova os CT-es com erro antes de emitir.",
                )
                return
        caminhos = self._caminhos_selecionados()
        if not caminhos:
            if self.modo_entrada == "numero":
                messagebox.showwarning(
                    "Aviso", "Informe ao menos um número de CT-e."
                )
            else:
                messagebox.showwarning("Aviso", "Selecione o XML do CT-e.")
            return
        if self.config is None:
            messagebox.showerror("Erro", "Configuração inválida. Corrija o config.json.")
            return
        if self._modo_varias() or len(caminhos) > 1:
            self._emitir_lote(caminhos)
            return

        caminho = caminhos[0]
        if not Path(caminho).exists():
            messagebox.showwarning("Aviso", "Selecione o XML do CT-e.")
            return
        lido = (
            self.dados is not None
            and self.arquivo_lido == str(Path(caminho).resolve())
        )
        if not lido:
            # A rota (portal x GNRE) sai da UF do XML, nunca do que está na tela.
            try:
                self.dados = ler_cte_xml(caminho)
                self.arquivo_lido = str(Path(caminho).resolve())
                lido = True
            except Exception as erro:
                self._log(
                    f"AVISO: o CT-e não pôde ser lido antes da emissão ({erro})."
                )
        uf: str | None = None
        if lido and not self.dados.uf_favorecida:
            uf = self.var_uf.get().strip().upper() or None
            if not uf:
                messagebox.showwarning(
                    "Aviso",
                    "Este CT-e não informa a UF favorecida (ICMSOutraUF).\n"
                    "Informe a UF no campo 'UF favorecida'.",
                )
                return
        elif lido:
            self.var_uf.set(self.dados.uf_favorecida)

        uf_efetiva = (self.dados.uf_favorecida if lido else None) or uf
        if (uf_efetiva or "").strip().upper() in ("SP", "ES", "PR"):
            messagebox.showerror(
                "Portal estadual",
                f"A UF {uf_efetiva} é emitida pelo portal do estado, e o robô "
                "desse portal não está disponível neste app.\n\n"
                "Use o app de transporte para emitir nesta UF.",
            )
            return
        if self.app.certificado is None:
            messagebox.showerror(
                "Erro",
                "Certificado digital inválido. Carregue o A1 para emitir pela GNRE.",
            )
            return

        vencimento: str | None = None
        if lido or uf:
            uf_calendario = (self.dados.uf_favorecida if lido else None) or uf
            venc = vencimento_padrao(uf_calendario)
            vencimento = venc.isoformat()
            self.var_vencimento.set(venc.strftime("%d/%m/%Y"))
        hoje = date.today()
        pagamento = hoje.isoformat()
        self.var_pagamento.set(hoje.strftime("%d/%m/%Y"))
        ambiente = self.var_ambiente.get()

        if ambiente == "1":
            if not messagebox.askyesno(
                "Emitir em produção",
                "A guia será emitida de verdade em PRODUÇÃO e gerará um débito fiscal.\n\n"
                "Confirma a emissão?",
            ):
                return

        self.em_execucao = True
        self.resultado = None
        self.pdf_path = None
        self._pdf_retry_id += 1
        self.btn_emitir.configure(state="disabled")
        self.app._limpar_log()
        self._status("Emitindo guia...")
        self._log(
            f"CT-e: {caminho}\n"
            f"UF favorecida: {uf_efetiva or 'automática'} | "
            f"Via: GNRE | Ambiente: "
            f"{'produção' if ambiente == '1' else 'homologação'}"
        )
        threading.Thread(
            target=self._executar,
            args=(
                caminho,
                True,
                uf_efetiva or None,
                vencimento or None,
                pagamento or None,
                ambiente,
            ),
            daemon=True,
        ).start()

    def _executar(
        self,
        caminho: str,
        emitir: bool,
        uf: str | None,
        vencimento: str | None,
        pagamento: str | None,
        ambiente: str,
    ) -> None:
        try:
            dados = self.dados
            if dados is None or self.arquivo_lido != str(Path(caminho).resolve()):
                try:
                    dados = ler_cte_xml(caminho)
                except Exception:
                    dados = self.dados
            uf_efetiva = (dados.uf_favorecida if dados is not None else None) or uf
            if (uf_efetiva or "").strip().upper() in ("SP", "ES", "PR"):
                raise RuntimeError(
                    f"A UF {uf_efetiva} só pode ser emitida pelo portal do estado "
                    "e o robô do portal não está disponível neste app. Nada foi "
                    "enviado à GNRE."
                )
            resultado = emitir_guia(
                caminho,
                config=self.config,
                ambiente=ambiente,
                emitir=emitir,
                gerar_pdf=True,
                consultar_regras=True,
                uf_favorecida=uf,
                data_vencimento=vencimento,
                data_pagamento=pagamento,
                log=lambda mensagem: self.fila.put(("log", mensagem)),
                ao_receber=lambda recibo: self.fila.put(("recibo", recibo)),
            )
        except Exception as erro:
            self.fila.put(("erro", f"{type(erro).__name__}: {erro}"))
        else:
            self.fila.put(("fim", resultado))

    def _emitir_lote(self, caminhos: list[str]) -> None:
        faltando = [caminho for caminho in caminhos if caminho not in self.arquivos_lidos]
        if faltando:
            dica = (
                "Aguarde o download dos XMLs dos CT-es antes de emitir."
                if self.modo_entrada == "numero"
                else "Clique em 'Ler e conferir' para validar o lote antes de emitir."
            )
            messagebox.showinfo("Leia os CT-es", dica)
            return
        dados = [self.arquivos_lidos[caminho] for caminho in caminhos]
        ufs = {item.uf_favorecida for item in dados}
        if len(ufs) != 1 or not next(iter(ufs)):
            messagebox.showerror(
                "UFs diferentes",
                "O lote precisa de XMLs do mesmo estado, todos com UF definida.",
            )
            return
        uf = next(iter(ufs))
        if uf in ("SP", "ES", "PR"):
            messagebox.showerror(
                "Portal estadual",
                f"A UF {uf} é emitida pelo portal do estado, e o robô desse "
                "portal não está disponível neste app.\n\n"
                "Use o app de transporte para emitir nesta UF.",
            )
            return
        venc = vencimento_padrao(uf)
        hoje = date.today()
        self.var_uf.set(uf)
        self.var_vencimento.set(venc.strftime("%d/%m/%Y"))
        self.var_pagamento.set(hoje.strftime("%d/%m/%Y"))
        ambiente = self.var_ambiente.get()
        total_guias = len(caminhos)
        plural = "s" if total_guias > 1 else ""

        if ambiente == "1":
            if not messagebox.askyesno(
                "Emitir lote em produção",
                f"{total_guias} guia{plural} {'serão emitidas' if plural else 'será emitida'} "
                "de verdade em PRODUÇÃO e gerará débitos fiscais.\n\nConfirma a emissão?",
            ):
                return

        self.em_execucao = True
        self.resultado = None
        self.pdf_path = None
        self._pdf_retry_id += 1
        self.btn_emitir.configure(state="disabled")
        self.app._limpar_log()
        self._status(f"Emitindo {total_guias} guia{plural}...")
        self._log(
            f"Lote: {total_guias} CT-e{plural} | UF {uf} | Ambiente "
            f"{'produção' if ambiente == '1' else 'homologação'}\n"
            "Ao final da emissão você escolhe onde salvar os PDFs."
        )
        threading.Thread(
            target=self._executar_lote,
            args=(
                caminhos,
                dict(self.arquivos_lidos),
                uf,
                venc.isoformat(),
                hoje.isoformat(),
                ambiente,
            ),
            daemon=True,
        ).start()

    def _executar_lote(
        self,
        caminhos: list[str],
        lidos: dict[str, DadosCTe],
        uf: str,
        vencimento: str,
        pagamento: str,
        ambiente: str,
    ) -> None:
        total = len(caminhos)
        resumo: list[dict] = []
        try:
            try:
                certificado = carregar_certificado(
                    self.config.caminho_pfx, self.config.senha
                )
            except Exception as erro:
                mensagem = f"{type(erro).__name__}: {erro}"
                self.fila.put(("log", f"ERRO: {mensagem}"))
                for indice, caminho in enumerate(caminhos, 1):
                    item = self._item_do_lote(indice, caminho, lidos)
                    item["mensagem"] = mensagem
                    resumo.append(item)
                return
            for aviso in certificado.avisos:
                self.fila.put(("log", f"AVISO: {aviso}"))
            # Um único cliente para o lote inteiro: o certificado é carregado
            # uma vez e o TLS fica aquecido entre um CT-e e outro.
            with ClienteGNRE(
                certificado,
                ambiente=ambiente,
                timeout=self.config.timeout,
                verificar_ssl=self.config.verificar_ssl,
                log=lambda mensagem: self.fila.put(("log", mensagem)),
            ) as cliente:
                for indice, caminho in enumerate(caminhos, 1):
                    item = self._item_do_lote(indice, caminho, lidos)
                    self.fila.put(("log", ""))
                    self.fila.put(
                        ("log", f"=== CT-e {indice}/{total}: {Path(caminho).name} ===")
                    )
                    self.fila.put(("lote_etapa", (indice, total)))
                    try:
                        resultado = emitir_guia(
                            caminho,
                            config=self.config,
                            ambiente=ambiente,
                            emitir=True,
                            gerar_pdf=True,
                            consultar_regras=True,
                            uf_favorecida=uf,
                            data_vencimento=vencimento,
                            data_pagamento=pagamento,
                            log=lambda mensagem: self.fila.put(("log", mensagem)),
                            ao_receber=lambda recibo, i=indice: self.fila.put(
                                ("recibo_lote", (i, total, recibo))
                            ),
                            cert=certificado,
                            cliente=cliente,
                        )
                        item["resultado"] = resultado
                        self._classificar_item(item, resultado)
                    except Exception as erro:
                        item["tipo"] = "falha"
                        item["mensagem"] = f"{type(erro).__name__}: {erro}"
                        self.fila.put(("log", f"ERRO: {item['mensagem']}"))
                    resumo.append(item)
        finally:
            self.fila.put(("lote_fim", resumo))

    def _item_do_lote(
        self, indice: int, caminho: str, lidos: dict[str, DadosCTe]
    ) -> dict:
        dados = lidos.get(caminho)
        return {
            "indice": indice,
            "caminho": caminho,
            "numero": (dados.numero if dados else "") or Path(caminho).stem,
            "tipo": "falha",
            "resultado": None,
            "destino": None,
            "mensagem": "",
        }

    def _classificar_item(self, item: dict, resultado: ResultadoEmissao) -> None:
        if resultado.guia_divergente:
            item["tipo"] = "divergente"
            item["mensagem"] = "; ".join(resultado.divergencias)
            self.fila.put(
                ("log", "ERRO: guia divergente — NÃO PAGUE. Nenhum PDF foi gerado.")
            )
            return
        if resultado.pdf_path:
            item["tipo"] = "ok"
            item["pdf_origem"] = str(resultado.pdf_path)
            self.fila.put(
                ("log", f"Guia {item['numero']} emitida; PDF pronto para salvar.")
            )
            return
        guias = self._guias_processadas(resultado)
        if guias:
            item["tipo"] = "sem_pdf"
            item["mensagem"] = self._detalhe_guias(guias).replace("\n", " | ")
            self.fila.put(
                (
                    "log",
                    f"AVISO: guia {item['numero']} emitida sem PDF do portal; "
                    "pague pela linha digitável.",
                )
            )
            return
        if resultado.uf_indisponivel:
            item["tipo"] = "uf"
            item["mensagem"] = " ".join(resultado.mensagens) or "UF indisponível."
            self.fila.put(("log", f"ERRO: guia {item['numero']} — UF indisponível."))
            return
        if resultado.numero_recibo:
            item["tipo"] = "pendente"
            item["mensagem"] = f"recibo {resultado.numero_recibo}"
            self.fila.put(
                (
                    "log",
                    f"AVISO: guia {item['numero']} ainda em processamento "
                    f"(recibo {resultado.numero_recibo}).",
                )
            )
            return
        item["tipo"] = "falha"
        item["mensagem"] = " ".join(resultado.mensagens) or "Falha não detalhada."
        self.fila.put(("log", f"ERRO: {item['mensagem']}"))

    # ------------------------------------------------------------------
    # PDFs
    # ------------------------------------------------------------------

    def _guias_processadas(self, resultado: ResultadoEmissao) -> list[dict]:
        return [
            guia
            for guia in resultado.guias
            if str(guia.get("codigo") or "") == "0"
        ]

    def _detalhe_guias(self, guias: list[dict]) -> str:
        linhas = []
        for guia in guias:
            numero = guia.get("numero") or ""
            linha = guia.get("linha_digitavel") or ""
            if numero:
                linhas.append(f"Guia: {numero}")
            if linha:
                linhas.append(f"Linha digitável: {linha}")
        return "\n".join(linhas)

    def _pasta_guias(self) -> Path:
        pasta = Path(self.config.pasta_saida) if self.config else self.dados_dir / "guias"
        if not pasta.is_absolute():
            pasta = self.dados_dir / pasta
        pasta.mkdir(parents=True, exist_ok=True)
        return pasta

    def _destino_livre(self, pasta: Path, nome: str) -> Path:
        destino = pasta / nome
        contador = 2
        while destino.exists():
            destino = pasta / f"{Path(nome).stem} ({contador}){Path(nome).suffix}"
            contador += 1
        return destino

    def _nome_pdf(self, resultado: ResultadoEmissao) -> str:
        uf = ""
        numero = ""
        if resultado.resumo_lote:
            uf = str(resultado.resumo_lote.get("uf_favorecida") or "")
        if resultado.dados:
            numero = resultado.dados.numero or ""
            uf = uf or (resultado.dados.uf_favorecida or "")
        if (not uf or not numero) and resultado.numero_recibo and self.config:
            uf_lote, numero_lote = detalhes_do_recibo(
                self.config.pasta_saida, resultado.numero_recibo
            )
            uf = uf or uf_lote
            numero = numero or numero_lote
        numero = numero_do_documento(numero)
        partes = ["Guia"]
        if uf:
            partes.append(uf)
        if numero:
            partes.append(f"CTE {numero}")
        if len(partes) == 1:
            partes.append(resultado.numero_recibo or "GNRE")
        nome = re.sub(r'[\\/:*?"<>|]', "", " - ".join(partes))
        return nome + ".pdf"

    def _entregar_pdf(self, resultado: ResultadoEmissao) -> Path | None:
        origem = Path(resultado.pdf_path) if resultado.pdf_path else None
        if origem is None or not origem.exists():
            self._log("AVISO: o PDF da guia não foi encontrado para entrega.")
            return None
        pasta = self._pasta_guias()
        nome = self._nome_pdf(resultado)
        candidato = pasta / nome
        if origem.resolve().parent == pasta.resolve() and origem.name == nome:
            destino = origem
        elif candidato.exists():
            destino = candidato
            try:
                shutil.copy2(origem, candidato)
            except OSError as erro:
                self._log(
                    f"AVISO: não foi possível atualizar {candidato} ({erro}); "
                    f"entregando {origem}."
                )
                destino = origem
        else:
            destino = self._destino_livre(pasta, nome)
            try:
                shutil.copy2(origem, destino)
            except OSError as erro:
                self._log(
                    f"AVISO: não foi possível copiar o PDF ({erro}); entregando {origem}."
                )
                destino = origem
        resultado.pdf_path = destino
        self.pdf_path = destino
        self._log(f"Guia pronta para pagamento: {destino}")
        return destino

    def _abrir_pdf(self, caminho: Path) -> None:
        try:
            os.startfile(str(caminho))
        except OSError as erro:
            self._log(f"AVISO: não foi possível abrir o PDF automaticamente ({erro}).")

    def _quarentenar_pdf(self, resultado: ResultadoEmissao, motivo: str) -> None:
        """Tira de circulação um PDF que não confere com o CT-e."""
        origem = Path(resultado.pdf_path) if resultado.pdf_path else None
        if origem is None or not origem.exists():
            return
        pasta = origem.parent / "diagnosticos"
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            destino = (
                pasta
                / f"{time.strftime('%Y%m%d_%H%M%S')}_{origem.stem}_NAO_PAGUE.pdf"
            )
            shutil.move(str(origem), destino)
        except OSError as erro:
            self._log(f"AVISO: não consegui reter o PDF divergente ({erro}).")
            return
        resultado.pdf_path = destino
        self.pdf_path = destino
        self._log(f"ERRO: {motivo} PDF retido como NÃO PAGUE em {destino}.")

    def _salvar_pdf_no_lote(
        self, resultado: ResultadoEmissao, pasta: Path
    ) -> tuple[Path | None, str]:
        origem = Path(resultado.pdf_path) if resultado.pdf_path else None
        if origem is None or not origem.exists():
            return None, "PDF não encontrado"
        destino = self._destino_livre(pasta, self._nome_pdf(resultado))
        try:
            shutil.copy2(origem, destino)
        except OSError as erro:
            return None, str(erro)
        return destino, ""

    def _salvar_pdfs_do_lote(self, itens: list[dict]) -> None:
        total = len(itens)
        plural = "s" if total > 1 else ""
        inicial = Path(self.config.pasta_saida) if self.config else self.dados_dir / "guias"
        if not inicial.is_absolute():
            inicial = self.dados_dir / inicial
        inicial.mkdir(parents=True, exist_ok=True)
        pasta = filedialog.askdirectory(
            parent=self.root,
            title=f"Salvar {'as' if plural else 'a'} guia{plural} em PDF",
            initialdir=str(inicial),
            mustexist=True,
        )
        if not pasta:
            for item in itens:
                item["destino"] = item.get("pdf_origem") or ""
                self._log(
                    f"Salvamento cancelado; o PDF de CT-e {item['numero']} continua em "
                    f"{item['destino']}."
                )
            return
        for item in itens:
            resultado = item.get("resultado")
            if resultado is None:
                item["destino"] = item.get("pdf_origem") or ""
                continue
            destino, erro = self._salvar_pdf_no_lote(resultado, Path(pasta))
            item["destino"] = str(destino) if destino else (item.get("pdf_origem") or "")
            if destino:
                self._log(f"Guia {item['numero']}: PDF salvo em {destino}.")
            else:
                item["mensagem"] = erro
                self._log(
                    f"AVISO: guia {item['numero']} emitida, mas o PDF não foi copiado "
                    f"({erro}). Original em {item['destino']}."
                )

    def _oficializar_lote(self, itens: list[dict]) -> None:
        """Busca o PDF oficial de cada guia e substitui o PDF entregue."""
        alvos = []
        for item in itens:
            resultado = item.get("resultado")
            if resultado is None or not item.get("destino"):
                continue
            if not resultado.numero_recibo or not resultado.pdf_local:
                continue
            if resultado.ambiente not in ("1", "2"):
                continue
            alvos.append(item)
        if alvos:
            threading.Thread(
                target=self._baixar_oficiais, args=(alvos,), daemon=True
            ).start()

    def _baixar_oficiais(self, itens: list[dict]) -> None:
        # Uma guia por vez: o portal limita consultas e cada download já
        # faz tentativas próprias.
        for item in itens:
            self._baixar_oficial_item(item)

    def _baixar_oficial_item(self, item: dict) -> None:
        resultado: ResultadoEmissao = item["resultado"]
        destino = Path(item["destino"])
        municipio = (
            resultado.dados.emitente.municipio_nome
            if resultado.dados and resultado.dados.emitente
            else ""
        )
        for tentativa in range(1, 4):
            if tentativa > 1:
                time.sleep(30)
            self.fila.put(
                (
                    "log",
                    f"Buscando o PDF oficial do CT-e {item['numero']} "
                    f"(tentativa {tentativa}/3)...",
                )
            )
            try:
                recuperacao = baixar_pdf_recibo(
                    resultado.numero_recibo,
                    config=self.config,
                    ambiente=resultado.ambiente,
                    log=lambda mensagem: self.fila.put(("log", mensagem)),
                    municipio_nome=municipio,
                    gerar_local=False,
                    conferir=False,
                )
            except Exception as erro:  # noqa: BLE001 - a guia local já está entregue
                self.fila.put(
                    (
                        "log",
                        f"AVISO: PDF oficial do CT-e {item['numero']} falhou "
                        f"({type(erro).__name__}: {erro}).",
                    )
                )
                continue
            oficial = recuperacao.pdf_path
            if oficial is None:
                continue
            aplicado, mensagem = self._aplicar_pdf_oficial(
                Path(oficial), destino
            )
            if aplicado:
                item["destino"] = str(aplicado)
                self.fila.put(
                    (
                        "log",
                        f"PDF oficial aplicado na guia do CT-e "
                        f"{item['numero']}: {aplicado}.",
                    )
                )
            self.fila.put(("log", mensagem))
            return
        self.fila.put(
            (
                "log",
                f"AVISO: o portal não entregou o PDF oficial do CT-e "
                f"{item['numero']}; a guia local (mesmo layout) foi mantida.",
            )
        )

    def _aplicar_pdf_oficial(
        self, oficial: Path, destino: Path
    ) -> tuple[Path | None, str]:
        """Copia o PDF oficial sobre o entregue, tolerando arquivo aberto."""
        if oficial.resolve() == destino.resolve():
            return destino, f"PDF oficial confirmado em {destino}."
        for tentativa in range(1, 6):
            try:
                shutil.copy2(oficial, destino)
                return destino, f"PDF oficial aplicado: {destino}."
            except PermissionError:
                if tentativa < 5:
                    time.sleep(1.2)
            except OSError as erro:
                return None, (
                    f"AVISO: PDF oficial disponível em {oficial}; não copiei "
                    f"para {destino} ({erro})."
                )
        alternativo = destino.with_name(
            f"{destino.stem} (oficial){destino.suffix}"
        )
        try:
            shutil.copy2(oficial, alternativo)
            return alternativo, (
                f"A guia estava aberta; o PDF oficial foi salvo em "
                f"{alternativo}."
            )
        except OSError as erro:
            return None, (
                f"AVISO: PDF oficial disponível em {oficial}; não copiei "
                f"({erro})."
            )

    def _retomar_pdf(
        self, resultado: ResultadoEmissao, silencioso: bool = False
    ) -> None:
        """Tenta o PDF oficial em segundo plano quando o lote já está processado."""
        if not resultado.numero_recibo or self.config is None:
            return
        self._pdf_retry_id += 1
        meu_id = self._pdf_retry_id
        recibo = resultado.numero_recibo
        ambiente = resultado.ambiente
        ja_impressa = resultado.pdf_path is not None

        def trabalhar() -> None:
            for tentativa in range(1, 4):
                espera = 0 if (tentativa == 1 and not ja_impressa) else (
                    20 if silencioso else 45
                )
                if espera:
                    time.sleep(espera)
                if self._pdf_retry_id != meu_id:
                    return
                self.fila.put(
                    (
                        "log",
                        f"Buscando o PDF do recibo {recibo} em segundo plano "
                        f"(tentativa {tentativa}/3)...",
                    )
                )
                recuperacao = baixar_pdf_recibo(
                    recibo,
                    config=self.config,
                    ambiente=ambiente,
                    log=lambda mensagem: self.fila.put(("log", mensagem)),
                    municipio_nome=(
                        resultado.dados.emitente.municipio_nome
                        if resultado.dados and resultado.dados.emitente
                        else ""
                    ),
                    gerar_local=not silencioso,
                )
                if recuperacao.pdf_path and (
                    not recuperacao.pdf_local or not ja_impressa
                ):
                    self.fila.put(
                        (
                            "pdf_preparado",
                            (
                                meu_id,
                                resultado,
                                recuperacao.pdf_path,
                                recuperacao.validacao,
                                silencioso,
                            ),
                        )
                    )
                    return
            self.fila.put(
                (
                    "log",
                    "O portal continua sem entregar o PDF; a guia pode ser paga "
                    "pela linha digitável e o PDF pode ser buscado depois.",
                )
            )

        self._log("Tentando baixar o PDF em segundo plano (3 tentativas).")
        threading.Thread(target=trabalhar, daemon=True).start()

    def _pdf_preparado(
        self,
        meu_id: int,
        resultado: ResultadoEmissao,
        caminho: Path,
        validacao: dict | None = None,
        silencioso: bool = False,
    ) -> None:
        if meu_id != self._pdf_retry_id:
            return
        self._log(f"PDF obtido: {caminho}.")
        if validacao:
            resultado.validacao = validacao
            for linha in validacao.get("linhas", []):
                self._log(linha)
            if not validacao.get("ok", True):
                self._quarentenar_pdf(resultado, "a guia divergiu do CT-e;")
                self._status("CONFERÊNCIA REPROVADA — não pague esta guia.")
                messagebox.showerror(
                    "Guia divergente — NÃO PAGUE",
                    "\n".join(validacao.get("linhas", []))
                    + "\n\nRelatório: "
                    + str(validacao.get("relatorio") or "")
                    + (
                        f"\n\nO PDF foi retido como: {resultado.pdf_path}"
                        if resultado.pdf_path
                        else ""
                    ),
                )
                return
        if silencioso:
            alvo = Path(resultado.pdf_path) if resultado.pdf_path else None
            if alvo and alvo.exists():
                aplicado, mensagem = self._aplicar_pdf_oficial(
                    Path(caminho), alvo
                )
                if aplicado:
                    resultado.pdf_path = aplicado
                self._log(mensagem)
            else:
                self._log(f"PDF oficial confirmado em {caminho}.")
            return
        resultado.pdf_path = caminho
        destino = self._entregar_pdf(resultado)
        if destino is None:
            return
        self._status(f"Guia pronta para pagamento: {destino.name}")
        self._abrir_pdf(destino)

    # ------------------------------------------------------------------
    # fila e finalização
    # ------------------------------------------------------------------

    def _processar_fila(self) -> None:
        try:
            while True:
                tipo, dado = self.fila.get_nowait()
                if tipo == "log":
                    self._log(dado)
                elif tipo == "recibo":
                    self._status(
                        f"Lote recebido — recibo {dado}. Consultando o processamento..."
                    )
                    self._log(
                        f">>> Recibo {dado} — guarde este número. "
                        "Se precisar retomar depois, clique de novo em 'Emitir guia' "
                        "com o mesmo CT-e: o app consulta esse recibo no portal e não reenvia o lote."
                    )
                elif tipo == "lote_etapa":
                    indice, total = dado
                    self._lote_info = (indice, total)
                    self._status(f"Emitindo guia {indice} de {total}...")
                elif tipo == "recibo_lote":
                    indice, total, recibo = dado
                    self._status(
                        f"Guia {indice}/{total}: lote recebido (recibo {recibo}). "
                        "Consultando..."
                    )
                    self._log(f">>> Recibo {recibo} — guia {indice} de {total}.")
                elif tipo == "lote_fim":
                    self._finalizar_lote(dado)
                elif tipo == "erro":
                    self._finalizar_erro(dado)
                elif tipo == "fim":
                    self._finalizar(dado)
                elif tipo == "pdf_preparado":
                    self._pdf_preparado(*dado)
                elif tipo == "chave_situacao":
                    chave, situacao = dado
                    self._definir_situacao(chave, situacao, "pendente")
                elif tipo == "chave_pronta":
                    chave, caminho = dado
                    item = self._item_chave(chave)
                    if item is not None:
                        item["caminho"] = caminho
                        item["erro"] = None
                    self._definir_situacao(chave, "XML pronto", "pronto")
                    self._log(
                        f"{self._rotulo_chave(chave)} baixado: {caminho}"
                    )
                    pendentes = [
                        outro
                        for outro in self.chaves
                        if not outro.get("caminho") and not outro.get("erro")
                    ]
                    if pendentes:
                        self._status(
                            f"Baixando os CT-es... {len(pendentes)} restante(s)."
                        )
                    else:
                        self._status("CT-es baixados. Lendo os dados da guia...")
                        self._ler_cte(avisar=False)
                elif tipo == "chave_erro":
                    chave, mensagem = dado
                    item = self._item_chave(chave)
                    if item is not None:
                        item["erro"] = mensagem
                    self._definir_situacao(chave, "Erro — veja o Registro", "erro")
                    self._log(
                        f"ERRO no {self._rotulo_chave(chave)}: {mensagem}"
                    )
                    self._status(
                        "Não foi possível baixar um dos CT-es. Veja o Registro."
                    )
                elif tipo == "numero_ok":
                    numero, chave, caminho = dado
                    self._chave_do_numero(numero, chave, caminho)
                elif tipo == "numero_erro":
                    numero, mensagem = dado
                    self._status(f"CT-e nº {numero}: não localizado.")
                    self._log(
                        f"ERRO ao buscar o CT-e nº {numero} no Edocs: {mensagem}"
                    )
                    messagebox.showerror("CT-e não localizado", mensagem)
        except queue.Empty:
            pass
        self.root.after(120, self._processar_fila)

    def _finalizar(self, resultado: ResultadoEmissao) -> None:
        self.resultado = resultado
        self._log("")
        self._log("=== Resultado ===")
        for linha in resultado.resumo().splitlines():
            self._log(linha)
        for aviso in resultado.avisos:
            self._log(f"AVISO: {aviso}")
        for mensagem in resultado.mensagens:
            self._log(f"- {mensagem}")
        if resultado.validacao:
            self._log("")
            for linha in resultado.validacao.get("linhas", [])[1:]:
                self._log(linha)

        guias = self._guias_processadas(resultado)
        if resultado.guia_divergente:
            self._quarentenar_pdf(resultado, "a guia divergiu do CT-e;")
            self._status("CONFERÊNCIA REPROVADA — não pague esta guia.")
            detalhe = [
                "A guia devolvida pelo portal NÃO confere com o CT-e e não deve ser paga.",
                "",
                *[f"- {linha}" for linha in resultado.divergencias],
            ]
            if resultado.numero_recibo:
                detalhe.append(f"\nRecibo do lote: {resultado.numero_recibo}")
            if resultado.retorno_path:
                detalhe.append(f"Retorno oficial: {resultado.retorno_path}")
            detalhe.append(
                "\nConfira o lote no portal GNRE antes de qualquer pagamento. "
                "Nenhum PDF foi gerado."
            )
            messagebox.showerror("Guia divergente — NÃO PAGUE", "\n".join(detalhe))
        elif resultado.sucesso and resultado.pdf_path:
            destino = self._entregar_pdf(resultado)
            if destino is None:
                self._status("Guia emitida, mas o PDF não foi encontrado.")
                self._log("AVISO: o PDF da guia não foi encontrado para entrega.")
            else:
                self._status(f"Guia pronta para pagamento: {destino.name}")
                self._abrir_pdf(destino)
                if resultado.pdf_local and resultado.numero_recibo:
                    self._retomar_pdf(resultado, silencioso=True)
        elif guias:
            self._status(
                "Guia emitida; o portal não entregou o PDF. Use a linha digitável."
            )
            detalhe = [
                "A guia foi processada pelo portal, mas o PDF não foi entregue agora.",
                "",
                self._detalhe_guias(guias),
            ]
            if resultado.numero_recibo:
                detalhe.append(f"\nRecibo do lote: {resultado.numero_recibo}")
            detalhe.append(
                "\nVocê já pode pagar com a linha digitável. O app gera o PDF com esses "
                "dados na hora e continua tentando o PDF oficial em segundo plano; a "
                "guia é entregue e aberta automaticamente."
            )
            faltantes = [m for m in resultado.mensagens if m]
            if faltantes:
                detalhe.append("\n" + "\n".join(faltantes))
            self._retomar_pdf(resultado)
            messagebox.showwarning(
                "Guia emitida — PDF indisponível no portal", "\n".join(detalhe)
            )
        elif resultado.uf_indisponivel:
            self._status("UF indisponível no portal; o lote pode ser reenviado.")
            detalhe = list(resultado.mensagens) or ["UF indisponível no portal GNRE."]
            if resultado.numero_recibo:
                detalhe.append(f"Recibo do lote: {resultado.numero_recibo}.")
            messagebox.showwarning(
                "GNRE não emitida — UF indisponível", "\n".join(detalhe)
            )
        elif resultado.numero_recibo and resultado.situacao:
            self._status(
                f"Lote recebido — recibo {resultado.numero_recibo}. Ainda em processamento."
            )
            detalhe = list(resultado.mensagens) or [
                "O portal não confirmou o processamento dentro do tempo."
            ]
            detalhe.append(f"Recibo do lote: {resultado.numero_recibo}.")
            detalhe.append(
                "Clique de novo em 'Emitir guia' com o mesmo CT-e para "
                "consultar o recibo e baixar a guia — o lote não é reenviado."
            )
            messagebox.showwarning("Lote recebido — aguardando o portal", "\n".join(detalhe))
        else:
            self._status("Falha na emissão. Veja o andamento.")
            detalhe = list(resultado.mensagens) or ["Falha não detalhada."]
            if resultado.numero_recibo:
                detalhe.append(
                    f"O lote já foi recebido pelo portal (recibo {resultado.numero_recibo}). "
                    "Clique de novo em 'Emitir guia' com o mesmo CT-e para "
                    "consultar esse recibo antes de reenviar."
                )
            messagebox.showerror("Falha na GNRE", "\n".join(detalhe))

        self.em_execucao = False
        self.btn_emitir.configure(state="normal")

    def _finalizar_lote(self, resumo: list[dict]) -> None:
        self.em_execucao = False
        self._lote_info = None
        self.btn_emitir.configure(state="normal")

        total = len(resumo)
        emitidas = [item for item in resumo if item["tipo"] == "ok"]
        if emitidas:
            self._salvar_pdfs_do_lote(emitidas)
            self._oficializar_lote(emitidas)
        com_pdf = [item for item in emitidas if item["destino"]]
        sem_pdf = [
            item
            for item in resumo
            if item["tipo"] == "sem_pdf"
            or (item["tipo"] == "ok" and not item["destino"])
        ]
        divergentes = [item for item in resumo if item["tipo"] == "divergente"]
        pendentes = [item for item in resumo if item["tipo"] == "pendente"]
        indisponiveis = [item for item in resumo if item["tipo"] == "uf"]
        falhas = [item for item in resumo if item["tipo"] == "falha"]

        self._log("")
        self._log("=== Resumo do lote ===")
        rotulos = {
            "ok": "guia pronta",
            "sem_pdf": "emitida sem PDF",
            "divergente": "DIVERGENTE — NÃO PAGUE",
            "pendente": "em processamento",
            "uf": "UF indisponível",
            "falha": "falha",
        }
        for item in resumo:
            linha = f"CT-e {item['numero']}: {rotulos.get(item['tipo'], item['tipo'])}"
            if item["destino"]:
                linha += f" — {item['destino']}"
            elif item["mensagem"]:
                linha += f" — {item['mensagem']}"
            self._log(linha)

        plural_lote = "s" if total > 1 else ""
        plural_pdf = "s" if len(com_pdf) != 1 else ""
        linhas = [
            f"Lote de {total} CT-e{plural_lote}: "
            f"{len(com_pdf)} guia{plural_pdf} com PDF salvo."
        ]
        if com_pdf:
            linhas.append("")
            linhas.append("Guias geradas:")
            for item in com_pdf:
                linhas.append(f"- CT-e {item['numero']}: {item['destino']}")
        if sem_pdf:
            linhas.append("")
            linhas.append("Emitidas sem PDF salvo (pague pela linha digitável):")
            for item in sem_pdf:
                detalhe = item["mensagem"] or "PDF não copiado"
                linhas.append(f"- CT-e {item['numero']}: {detalhe}")
        if pendentes:
            linhas.append("")
            linhas.append("Ainda em processamento no portal:")
            for item in pendentes:
                linhas.append(f"- CT-e {item['numero']}: {item['mensagem']}")
            linhas.append(
                "Reenvie o mesmo lote depois: os CT-es já emitidos são apenas "
                "consultados, sem novo envio."
            )
        if indisponiveis:
            linhas.append("")
            linhas.append("UF indisponível no portal:")
            for item in indisponiveis:
                linhas.append(f"- CT-e {item['numero']}: {item['mensagem']}")
        if falhas:
            linhas.append("")
            linhas.append("Falhas:")
            for item in falhas:
                linhas.append(f"- CT-e {item['numero']}: {item['mensagem']}")
        if divergentes:
            linhas.append("")
            linhas.append("DIVERGENTES — NÃO PAGUE:")
            for item in divergentes:
                linhas.append(f"- CT-e {item['numero']}: {item['mensagem']}")
            linhas.append(
                "Confira esses lotes no portal GNRE antes de qualquer pagamento. "
                "Nenhum PDF foi gerado para eles."
            )
        detalhe = "\n".join(linhas)

        if divergentes:
            self._status(
                f"Lote concluído: {len(divergentes)} guia(s) divergente(s) — NÃO PAGUE."
            )
            messagebox.showerror("Lote — guias divergentes: NÃO PAGUE", detalhe)
        elif falhas or indisponiveis:
            self._status(
                f"Lote com pendências: {len(com_pdf)} de {total} guia{plural_lote} com PDF."
            )
            messagebox.showerror("Lote concluído com pendências", detalhe)
        elif pendentes or sem_pdf:
            self._status(
                f"Lote com pendências: {len(com_pdf)} de {total} guia{plural_lote} com PDF."
            )
            messagebox.showwarning("Lote concluído com pendências", detalhe)
        else:
            self._status(
                f"Lote concluído: {len(com_pdf)} guia{plural_pdf} com PDF."
            )
            messagebox.showinfo("Lote concluído", detalhe)

    def _finalizar_erro(self, mensagem: str) -> None:
        self._log(f"ERRO: {mensagem}")
        self._status("Erro inesperado. Veja o andamento.")
        self.em_execucao = False
        self.btn_emitir.configure(state="normal")
        messagebox.showerror("Erro", mensagem)
