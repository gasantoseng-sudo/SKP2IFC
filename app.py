"""SKP2IFC — janela principal (tkinter).

Aba 1: converte .skp em .ifc (referência para o AltoQi Eberick).
Aba 2: otimiza .ifc existentes (IFC2X3 e IFC4), mantendo classes e propriedades.

Execute:  python app.py
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import ifc_optimize as opt
import skp2ifc_core as core
import updater
import version as appver
import viewer_core as vc

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".skp2ifc.json")

UNIT_LABELS = {
    "Metros (m)": "m",
    "Centímetros (cm)": "cm",
    "Milímetros (mm)": "mm",
}
SCHEMA_LABELS = {
    "IFC2X3 (mais compatível)": "IFC2X3",
    "IFC4": "IFC4",
}


def parse_float(text: str, default: float = 0.0) -> float:
    text = (text or "").strip().replace(",", ".")
    if not text:
        return default
    return float(text)


class FileListFrame(ttk.LabelFrame):
    """Lista de arquivos com botões de adicionar/remover (reaproveitada nas duas abas)."""

    def __init__(self, parent, title: str, ext: str, type_label: str, finder) -> None:
        super().__init__(parent, text=title)
        self.ext = ext
        self.type_label = type_label
        self.finder = finder
        self.files: list[str] = []

        inner = ttk.Frame(self)
        inner.pack(fill="both", expand=True, padx=6, pady=6)
        self.lst = tk.Listbox(inner, height=5, selectmode="extended", activestyle="none")
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.lst.yview)
        self.lst.configure(yscrollcommand=sb.set)
        self.lst.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        btns = ttk.Frame(inner)
        btns.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(btns, text="Adicionar arquivos…", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="Adicionar pasta…", command=self.add_folder).pack(fill="x", pady=2)
        self.var_recursive = tk.BooleanVar(value=True)
        ttk.Checkbutton(btns, text="Incluir subpastas", variable=self.var_recursive).pack(anchor="w")
        ttk.Button(btns, text="Remover selecionados", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="Limpar lista", command=self.clear).pack(fill="x", pady=2)

    def _refresh(self) -> None:
        self.lst.delete(0, "end")
        for p in self.files:
            self.lst.insert("end", p)

    def _add(self, paths) -> None:
        for p in paths:
            p = os.path.normpath(p)
            if p.lower().endswith(self.ext) and p not in self.files:
                self.files.append(p)
        self._refresh()

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title=f"Escolha arquivos {self.type_label}",
            filetypes=[(self.type_label, f"*{self.ext}"), ("Todos", "*.*")])
        self._add(paths)

    def add_folder(self) -> None:
        d = filedialog.askdirectory(title=f"Escolha uma pasta com arquivos {self.ext}")
        if d:
            found = self.finder(d, self.var_recursive.get())
            if not found:
                messagebox.showinfo("Nada encontrado", f"Não há arquivos {self.ext} nessa pasta.")
            self._add(found)

    def remove_selected(self) -> None:
        sel = set(self.lst.curselection())
        self.files = [p for i, p in enumerate(self.files) if i not in sel]
        self._refresh()

    def clear(self) -> None:
        self.files = []
        self._refresh()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"SKP2IFC {appver.__version__} — conversor SKP → IFC e otimizador de IFC")
        self.geometry("960x900")
        self.minsize(860, 700)

        self.msgs: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_evt = threading.Event()
        self.tag_state: dict[str, bool] = {}  # tag -> incluir?
        self.status_text = ""
        self.t_start = 0.0
        self.running = False
        self.pb_indeterminate = False

        self._build_ui()
        self._load_settings()
        updater.cleanup_old()
        self.pending_release = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.var_autoupd.get() and updater.repo_configured():
            self.after(2000, lambda: self.check_updates(False))
        self.after(100, self._poll)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=8, pady=8)

        # parte de baixo (compartilhada): cancelar, barra, status, log
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="both", **pad)
        act = ttk.Frame(bottom)
        act.pack(fill="x")
        self.btn_cancel = ttk.Button(act, text="Cancelar", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left")
        self.pb = ttk.Progressbar(act, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True, padx=10)
        self.var_status = tk.StringVar(value="Pronto.")
        ttk.Label(bottom, textvariable=self.var_status, anchor="w").pack(fill="x", pady=(4, 0))
        lg = ttk.Frame(bottom)
        lg.pack(fill="both", expand=True, pady=(4, 0))
        self.log = tk.Text(lg, height=9, wrap="word", state="disabled")
        lsb = ttk.Scrollbar(lg, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")

        bar = ttk.Frame(outer)
        bar.pack(side="top", fill="x", pady=(0, 4))
        ttk.Label(bar, text=f"SKP2IFC  v{appver.__version__}", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.var_upd_banner = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_upd_banner, foreground="#0a7a2f").pack(side="left", padx=12)
        self.btn_update = ttk.Button(bar, text="Verificar atualizações", command=lambda: self.check_updates(True))
        self.btn_update.pack(side="right")
        self.var_autoupd = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Verificar ao abrir", variable=self.var_autoupd).pack(side="right", padx=10)

        nb = ttk.Notebook(outer)
        nb.pack(side="top", fill="both", expand=True)
        tab1 = ttk.Frame(nb)
        tab2 = ttk.Frame(nb)
        nb.add(tab1, text="  SKP → IFC  ")
        tab3 = ttk.Frame(nb)
        nb.add(tab2, text="  Otimizar IFC  ")
        nb.add(tab3, text="  Visualizar origem  ")
        self._build_tab_skp(tab1, pad)
        self._build_tab_ifc(tab2, pad)
        self._build_tab_view(tab3, pad)

    # --------------------------------------------------------------- aba 1
    def _build_tab_skp(self, root, pad) -> None:
        self.skp_list = FileListFrame(root, "Arquivos .skp", ".skp", "SketchUp", core.find_skp_files)
        self.skp_list.pack(fill="x", **pad)

        out = ttk.LabelFrame(root, text="Saída")
        out.pack(fill="x", **pad)
        self.var_outmode = tk.StringVar(value="side")
        ttk.Radiobutton(out, text="Ao lado de cada arquivo original", value="side",
                        variable=self.var_outmode).grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        ttk.Radiobutton(out, text="Na pasta:", value="folder",
                        variable=self.var_outmode).grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.var_outdir = tk.StringVar()
        ttk.Entry(out, textvariable=self.var_outdir).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(out, text="Procurar…", command=lambda: self.pick_outdir(self.var_outdir, self.var_outmode)
                   ).grid(row=1, column=2, padx=6)
        self.var_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(out, text="Sobrescrever IFC existente (desmarcado = cria _2, _3…)",
                        variable=self.var_overwrite).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        out.columnconfigure(1, weight=1)

        o = ttk.LabelFrame(root, text="Opções do IFC")
        o.pack(fill="x", **pad)
        ttk.Label(o, text="Unidade:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_unit = tk.StringVar(value="Metros (m)")
        ttk.Combobox(o, textvariable=self.var_unit, values=list(UNIT_LABELS), state="readonly",
                     width=18).grid(row=0, column=1, sticky="w")
        ttk.Label(o, text="Esquema:").grid(row=0, column=2, sticky="w", padx=(20, 6))
        self.var_schema = tk.StringVar(value="IFC2X3 (mais compatível)")
        ttk.Combobox(o, textvariable=self.var_schema, values=list(SCHEMA_LABELS), state="readonly",
                     width=24).grid(row=0, column=3, sticky="w")

        ttk.Label(o, text="Deslocar origem (na unidade escolhida):").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        self.var_ox, self.var_oy, self.var_oz = tk.StringVar(value="0"), tk.StringVar(value="0"), tk.StringVar(value="0")
        frm = ttk.Frame(o)
        frm.grid(row=1, column=2, columnspan=2, sticky="w", padx=(20, 0))
        for lbl, var in (("X", self.var_ox), ("Y", self.var_oy), ("Z", self.var_oz)):
            ttk.Label(frm, text=lbl).pack(side="left", padx=(0, 2))
            ttk.Entry(frm, textvariable=var, width=9).pack(side="left", padx=(0, 10))
        self.var_tomin = tk.BooleanVar(value=False)
        ttk.Checkbutton(o, text="Levar o canto mínimo do modelo para a origem (0,0,0) antes de aplicar o deslocamento",
                        variable=self.var_tomin).grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=2)
        self.var_hidden = tk.BooleanVar(value=True)
        ttk.Checkbutton(o, text="Ignorar tags desligadas (ocultas) no SketchUp",
                        variable=self.var_hidden).grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=2)
        ttk.Label(o, text="Máx. de triângulos por elemento (0 = sem limite):").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        self.var_maxtri = tk.StringVar(value="30000")
        ttk.Entry(o, textvariable=self.var_maxtri, width=10).grid(row=4, column=2, sticky="w", padx=(20, 0))
        ttk.Label(o, text="Acima disso a malha é simplificada (arquivo bem mais leve).").grid(
            row=4, column=3, sticky="w")

        tg = ttk.LabelFrame(root, text="Filtro por tags (o que NÃO precisa ir para a referência)")
        tg.pack(fill="both", expand=True, **pad)
        top = ttk.Frame(tg)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="Excluir tags que contenham (separe por vírgula):").pack(anchor="w")
        self.var_kw = tk.StringVar(value=", ".join(core.DEFAULT_EXCLUDE_KEYWORDS))
        ttk.Entry(top, textvariable=self.var_kw).pack(fill="x", pady=2)
        row = ttk.Frame(top)
        row.pack(fill="x")
        self.btn_scan = ttk.Button(row, text="Analisar tags dos arquivos", command=self.scan_tags)
        self.btn_scan.pack(side="left")
        ttk.Button(row, text="Marcar todas", command=lambda: self._set_all(True)).pack(side="left", padx=4)
        ttk.Button(row, text="Desmarcar todas", command=lambda: self._set_all(False)).pack(side="left")
        ttk.Label(row, text="  (clique na linha para incluir/excluir)").pack(side="left")

        cols = ("inc", "tag", "tri", "hid")
        tv = ttk.Frame(tg)
        tv.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree = ttk.Treeview(tv, columns=cols, show="headings", height=5, selectmode="browse")
        for c, t, w, a in (("inc", "Incluir", 70, "center"), ("tag", "Tag", 360, "w"),
                           ("tri", "Triângulos", 100, "e"), ("hid", "Oculta no SKP", 110, "center")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        tsb = ttk.Scrollbar(tv, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)

        self.btn_go = ttk.Button(root, text="Converter para IFC", command=self.start)
        self.btn_go.pack(anchor="w", **pad)

    # --------------------------------------------------------------- aba 2
    def _build_tab_ifc(self, root, pad) -> None:
        self.ifc_list = FileListFrame(root, "Arquivos .ifc (IFC2X3 ou IFC4)", ".ifc", "IFC", opt.find_ifc_files)
        self.ifc_list.pack(fill="x", **pad)

        out = ttk.LabelFrame(root, text="Saída (o arquivo original nunca é alterado)")
        out.pack(fill="x", **pad)
        self.var_o_outmode = tk.StringVar(value="side")
        ttk.Radiobutton(out, text="Ao lado de cada original, com o final “_otimizado”", value="side",
                        variable=self.var_o_outmode).grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        ttk.Radiobutton(out, text="Na pasta:", value="folder",
                        variable=self.var_o_outmode).grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.var_o_outdir = tk.StringVar()
        ttk.Entry(out, textvariable=self.var_o_outdir).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(out, text="Procurar…",
                   command=lambda: self.pick_outdir(self.var_o_outdir, self.var_o_outmode)
                   ).grid(row=1, column=2, padx=6)
        self.var_o_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(out, text="Sobrescrever arquivo “_otimizado” existente (desmarcado = cria _2, _3…)",
                        variable=self.var_o_overwrite).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        out.columnconfigure(1, weight=1)

        o = ttk.LabelFrame(root, text="Otimização")
        o.pack(fill="x", **pad)
        ttk.Label(o, text="Máx. de triângulos por representação (0 = só limpeza, sem perda):").grid(
            row=0, column=0, sticky="w", padx=6, pady=6)
        self.var_o_maxtri = tk.StringVar(value="30000")
        ttk.Entry(o, textvariable=self.var_o_maxtri, width=10).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(o, text="Manter só este % dos triângulos de cada malha (100 = desligado):").grid(
            row=1, column=0, sticky="w", padx=6, pady=6)
        self.var_o_keep = tk.StringVar(value="100")
        ttk.Entry(o, textvariable=self.var_o_keep, width=10).grid(row=1, column=1, sticky="w", padx=8)
        info = ("O que faz: une vértices e pontos duplicados (sem perda) e, onde uma malha passar do limite,\n"
                "simplifica a malha. Classes, nomes, propriedades, tipos, materiais, estilos e hierarquia\n"
                "são mantidos. Extrusões e outras geometrias paramétricas não são tocadas, e sólidos\n"
                "facetados só têm os pontos unidos.")
        ttk.Label(o, text=info, justify="left").grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 6))

        self.btn_opt = ttk.Button(root, text="Otimizar IFC", command=self.start_optimize)
        self.btn_opt.pack(anchor="w", **pad)

    # --------------------------------------------------------------- aba 3
    def _build_tab_view(self, root, pad) -> None:
        self.model = None
        self.view_names: list[str] = []
        self.view_vis: dict[str, bool] = {}
        self.view_colors: dict = {}
        self.view3d = vc.View3D()
        self.plan_view = vc.View3D(zoom=1.0)
        self._imgs: dict = {}
        self._drag: dict = {}
        self._redraw_pending = False

        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        self.btn_view_open = ttk.Button(top, text="Abrir .skp ou .ifc…", command=self.view_open)
        self.btn_view_open.pack(side="left")
        self.btn_view_list = ttk.Button(top, text="Usar 1º arquivo da aba SKP → IFC", command=self.view_from_list)
        self.btn_view_list.pack(side="left", padx=6)
        self.var_view_file = tk.StringVar(value="Nenhum arquivo carregado.")
        ttk.Label(top, textvariable=self.var_view_file).pack(side="left", padx=8)

        ctl = ttk.LabelFrame(root, text="Deslocamento de origem (prévia; mesma regra da aba SKP → IFC)")
        ctl.pack(fill="x", **pad)
        ttk.Label(ctl, text="Unidade:").pack(side="left", padx=(6, 2))
        self.var_v_unit = tk.StringVar(value="Metros (m)")
        cb = ttk.Combobox(ctl, textvariable=self.var_v_unit, values=list(UNIT_LABELS), state="readonly", width=16)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.view_redraw())
        self.var_vx, self.var_vy, self.var_vz = tk.StringVar(value="0"), tk.StringVar(value="0"), tk.StringVar(value="0")
        for lbl, var in (("X", self.var_vx), ("Y", self.var_vy), ("Z", self.var_vz)):
            ttk.Label(ctl, text=lbl).pack(side="left", padx=(12, 2))
            e = ttk.Entry(ctl, textvariable=var, width=10)
            e.pack(side="left")
            e.bind("<Return>", lambda ev: self.view_redraw())
            e.bind("<FocusOut>", lambda ev: self.view_redraw())
        self.var_vtomin = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Canto mínimo na origem", variable=self.var_vtomin,
                        command=self.view_redraw).pack(side="left", padx=12)
        self.var_vorigin = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Enquadrar a origem", variable=self.var_vorigin,
                        command=self.view_redraw).pack(side="left")
        ttk.Button(ctl, text="Aplicar", command=self.view_redraw).pack(side="left", padx=6)
        ttk.Button(ctl, text="Enviar para aba SKP → IFC", command=self.view_send_to_skp).pack(side="left", padx=6)

        mid = ttk.Frame(root)
        mid.pack(fill="both", expand=True, **pad)
        right = ttk.LabelFrame(mid, text="Tags / classes (clique para ocultar)")
        right.pack(side="right", fill="y")
        self.vtree = ttk.Treeview(right, columns=("v", "n", "c"), show="headings", height=12, selectmode="browse")
        for c, t, w in (("v", "Ver", 40), ("n", "Nome", 170), ("c", "Qtd", 50)):
            self.vtree.heading(c, text=t)
            self.vtree.column(c, width=w, anchor="w" if c == "n" else "center")
        self.vtree.pack(fill="both", expand=True, padx=4, pady=4)
        self.vtree.bind("<Button-1>", self._on_vtree_click)
        self.vtree.tag_configure("x", foreground="#999999")

        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)
        # Canvas (e não Label): o tamanho do widget não depende da imagem desenhada nele,
        # senão cada redesenho muda o layout e a vista "escorrega".
        self.lbl_plan = tk.Canvas(left, bg="#fafafa", width=300, height=300, highlightthickness=1,
                                  highlightbackground="#888888")
        self.lbl_3d = tk.Canvas(left, bg="#fafafa", width=300, height=300, highlightthickness=1,
                                highlightbackground="#888888")
        self.lbl_plan.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.lbl_3d.pack(side="left", fill="both", expand=True)
        for lbl in (self.lbl_plan, self.lbl_3d):
            lbl.bind("<Configure>", lambda e: self.view_redraw())
        self.lbl_3d.bind("<ButtonPress-1>", lambda e: self._drag_start(e, "rot"))
        self.lbl_3d.bind("<B1-Motion>", lambda e: self._drag_move(e, "rot"))
        self.lbl_3d.bind("<ButtonPress-3>", lambda e: self._drag_start(e, "pan3"))
        self.lbl_3d.bind("<B3-Motion>", lambda e: self._drag_move(e, "pan3"))
        self.lbl_plan.bind("<ButtonPress-1>", lambda e: self._drag_start(e, "pan2"))
        self.lbl_plan.bind("<B1-Motion>", lambda e: self._drag_move(e, "pan2"))
        for lbl, which in ((self.lbl_3d, "3"), (self.lbl_plan, "2")):
            lbl.bind("<MouseWheel>", lambda e, w=which: self._wheel(e, w))
            lbl.bind("<Button-4>", lambda e, w=which: self._wheel(e, w, +1))
            lbl.bind("<Button-5>", lambda e, w=which: self._wheel(e, w, -1))

        self.var_view_info = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.var_view_info, justify="left", font=("Consolas", 9)).pack(
            anchor="w", padx=10, pady=(0, 4))

    def _view_unit(self) -> str:
        return UNIT_LABELS[self.var_v_unit.get()]

    def _view_shift(self):
        k = {"m": 1.0, "cm": 100.0, "mm": 1000.0}[self._view_unit()]
        try:
            off = (parse_float(self.var_vx.get()) / k, parse_float(self.var_vy.get()) / k,
                   parse_float(self.var_vz.get()) / k)
        except ValueError:
            off = (0.0, 0.0, 0.0)
        return vc.compute_shift(self.model, off, self.var_vtomin.get())

    def view_open(self) -> None:
        p = filedialog.askopenfilename(title="Escolha um SKP ou IFC",
                                       filetypes=[("SKP / IFC", "*.skp *.ifc"), ("Todos", "*.*")])
        if p:
            self._view_load(p)

    def view_from_list(self) -> None:
        if not self.skp_list.files:
            messagebox.showinfo("Sem arquivos", "A lista da aba SKP → IFC está vazia.")
            return
        self._view_load(self.skp_list.files[0])

    def _view_load(self, path: str) -> None:
        if self._busy():
            return
        self._set_running(True)
        self.msgs.put(("log", f"Carregando visualização de {os.path.basename(path)} ..."))

        def work():
            try:
                m = vc.load_model(path, status=self._status_cb)
                self.msgs.put(("model", m))
            except Exception as e:
                self.msgs.put(("log", f"  ERRO ao visualizar: {type(e).__name__}: {e}"))
            self.msgs.put(("done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _view_set_model(self, m) -> None:
        self.model = m
        self.var_view_file.set(f"{os.path.basename(m.path)}  ({m.kind.upper()})")
        self.view_colors = vc.group_colors(m.groups)
        self.view_vis = {n: True for n in m.groups}
        self.vtree.delete(*self.vtree.get_children())
        for n in sorted(m.groups, key=str.lower):
            r, g, b = self.view_colors[n]
            self.vtree.insert("", "end", iid=n, values=("☑", n, m.counts.get(n, 0)))
        self.view3d = vc.View3D()
        self.plan_view = vc.View3D()
        self.view_redraw()
        self._log("  Visualização pronta.")

    def _on_vtree_click(self, ev) -> None:
        iid = self.vtree.identify_row(ev.y)
        if not iid:
            return
        self.view_vis[iid] = not self.view_vis.get(iid, True)
        vals = list(self.vtree.item(iid, "values"))
        vals[0] = "☑" if self.view_vis[iid] else "☐"
        self.vtree.item(iid, values=vals, tags=() if self.view_vis[iid] else ("x",))
        self.view_redraw()

    def view_redraw(self) -> None:
        if self.model is None or self._redraw_pending:
            return
        self._redraw_pending = True
        self.after(30, self._view_draw)

    def _view_draw(self) -> None:
        self._redraw_pending = False
        if self.model is None:
            return
        try:
            from PIL import ImageTk
        except ImportError:
            self.var_view_info.set("Instale o Pillow:  pip install pillow")
            return
        sh = self._view_shift()
        unit = self._view_unit()
        inc = self.var_vorigin.get()
        for lbl, mode, v in ((self.lbl_plan, "plan", self.plan_view), (self.lbl_3d, "3d", self.view3d)):
            w, h = max(200, lbl.winfo_width() - 2), max(200, lbl.winfo_height() - 2)
            img = vc.render(self.model, sh, self.view_vis, self.view_colors, mode, (w, h), v, unit, inc)
            ph = ImageTk.PhotoImage(img)
            self._imgs[mode] = ph
            lbl.delete("all")
            lbl.create_image(0, 0, anchor="nw", image=ph)
        self.var_view_info.set(vc.describe(self.model, sh, unit))

    def _drag_start(self, e, kind) -> None:
        self._drag = {"x": e.x, "y": e.y}

    def _drag_move(self, e, kind) -> None:
        if not self._drag:
            return
        dx, dy = e.x - self._drag["x"], e.y - self._drag["y"]
        self._drag = {"x": e.x, "y": e.y}
        if kind == "rot":
            self.view3d.yaw += dx * 0.5
            self.view3d.pitch = max(0.0, min(90.0, self.view3d.pitch + dy * 0.5))
        elif kind == "pan3":
            px, py = self.view3d.pan
            self.view3d.pan = (px + dx, py + dy)
        else:
            px, py = self.plan_view.pan
            self.plan_view.pan = (px + dx, py + dy)
        self.view_redraw()

    def _wheel(self, e, which, direction=None) -> None:
        d = direction if direction is not None else (1 if e.delta > 0 else -1)
        v = self.view3d if which == "3" else self.plan_view
        v.zoom = max(0.2, min(50.0, v.zoom * (1.15 if d > 0 else 1 / 1.15)))
        self.view_redraw()

    def view_send_to_skp(self) -> None:
        self.var_unit.set(self.var_v_unit.get())
        self.var_ox.set(self.var_vx.get())
        self.var_oy.set(self.var_vy.get())
        self.var_oz.set(self.var_vz.get())
        self.var_tomin.set(self.var_vtomin.get())
        messagebox.showinfo("Enviado", "Unidade e deslocamento copiados para a aba SKP → IFC.")

    # ------------------------------------------------------------- saída
    def pick_outdir(self, var_dir: tk.StringVar, var_mode: tk.StringVar) -> None:
        d = filedialog.askdirectory(title="Pasta de saída")
        if d:
            var_dir.set(d)
            var_mode.set("folder")

    # ----------------------------------------------------------------- tags
    def _fill_tree(self, infos: dict) -> None:
        self.tree.delete(*self.tree.get_children())
        kws = [k.strip() for k in self.var_kw.get().split(",")]
        auto_ex = core.default_excluded(infos.keys(), kws)
        self.tag_state = {}
        for name in sorted(infos, key=str.lower):
            inc = name not in auto_ex
            self.tag_state[name] = inc
            i = infos[name]
            self.tree.insert("", "end", iid=name,
                             values=("☑" if inc else "☐", name, i.triangles, "sim" if i.hidden else ""))

    def _on_tree_click(self, ev) -> None:
        iid = self.tree.identify_row(ev.y)
        if not iid:
            return
        self.tag_state[iid] = not self.tag_state.get(iid, True)
        vals = list(self.tree.item(iid, "values"))
        vals[0] = "☑" if self.tag_state[iid] else "☐"
        self.tree.item(iid, values=vals)

    def _set_all(self, value: bool) -> None:
        for iid in self.tree.get_children():
            self.tag_state[iid] = value
            vals = list(self.tree.item(iid, "values"))
            vals[0] = "☑" if value else "☐"
            self.tree.item(iid, values=vals)

    def scan_tags(self) -> None:
        files = list(self.skp_list.files)
        if not files:
            messagebox.showinfo("Sem arquivos", "Adicione ao menos um arquivo .skp.")
            return
        if self._busy():
            return
        self._set_running(True)

        def work():
            merged: dict[str, core.TagInfo] = {}
            for p in files:
                self.msgs.put(("log", f"Analisando tags de {os.path.basename(p)} ..."))
                try:
                    for name, info in core.scan_tags(p, status=self._status_cb).items():
                        cur = merged.setdefault(name, core.TagInfo(name, info.hidden))
                        cur.triangles += info.triangles
                        cur.hidden = cur.hidden and info.hidden
                except Exception as e:
                    self.msgs.put(("log", f"  ERRO: {type(e).__name__}: {e}"))
            self.msgs.put(("tags", merged))
            self.msgs.put(("done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    # ------------------------------------------------- estado / progresso
    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _status_cb(self, text: str, frac) -> None:
        """Chamado pela thread de trabalho; só enfileira."""
        self.msgs.put(("status", (text, frac)))

    def _apply_status(self, text: str, frac) -> None:
        self.status_text = text
        if frac is None:
            if not self.pb_indeterminate:
                self.pb.configure(mode="indeterminate")
                self.pb.start(15)
                self.pb_indeterminate = True
        else:
            if self.pb_indeterminate:
                self.pb.stop()
                self.pb.configure(mode="determinate")
                self.pb_indeterminate = False
            self.pb["value"] = max(0.0, min(1.0, frac)) * 100
        self._refresh_status_label()

    def _refresh_status_label(self) -> None:
        if not self.running:
            return
        el = int(time.monotonic() - self.t_start)
        clock = f"{el // 60:d}:{el % 60:02d}"
        self.var_status.set(f"{self.status_text}   ·   decorrido {clock}")

    def _tick(self) -> None:
        if self.running:
            self._refresh_status_label()
            self.after(500, self._tick)

    def _set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        for b in (self.btn_go, self.btn_scan, self.btn_opt, self.btn_view_open, self.btn_view_list, self.btn_update):
            b.configure(state=state)
        self.btn_cancel.configure(state="normal" if running else "disabled")
        if running:
            self.t_start = time.monotonic()
            self.status_text = "Iniciando"
            self._apply_status("Iniciando", None)
            self.after(500, self._tick)
        else:
            if self.pb_indeterminate:
                self.pb.stop()
                self.pb.configure(mode="determinate")
                self.pb_indeterminate = False
            self.pb["value"] = 0
            self.var_status.set("Pronto.")

    # ---------------------------------------------------- conversão SKP→IFC
    def _collect_options(self) -> core.Options:
        try:
            off = (parse_float(self.var_ox.get()), parse_float(self.var_oy.get()),
                   parse_float(self.var_oz.get()))
        except ValueError:
            raise ValueError("Deslocamento de origem inválido: use números (ex.: 1500 ou 1,5).")
        kws = tuple(k.strip() for k in self.var_kw.get().split(",") if k.strip())
        try:
            max_tri = int(parse_float(self.var_maxtri.get(), 0))
        except ValueError:
            raise ValueError("Limite de triângulos inválido: use um número inteiro (0 = sem limite).")
        if max_tri < 0:
            raise ValueError("Limite de triângulos não pode ser negativo.")
        return core.Options(
            unit=UNIT_LABELS[self.var_unit.get()],
            schema=SCHEMA_LABELS[self.var_schema.get()],
            offset=off,
            origin_to_min=self.var_tomin.get(),
            exclude_tags={t for t, inc in self.tag_state.items() if not inc},
            force_include_tags={t for t, inc in self.tag_state.items() if inc},
            exclude_keywords=kws,
            skip_hidden_tags=self.var_hidden.get(),
            overwrite=self.var_overwrite.get(),
            max_tri_per_element=max_tri,
        )

    def start(self) -> None:
        if self._busy():
            return
        files = list(self.skp_list.files)
        if not files:
            messagebox.showinfo("Sem arquivos", "Adicione ao menos um arquivo .skp.")
            return
        try:
            opts = self._collect_options()
        except ValueError as e:
            messagebox.showerror("Opções", str(e))
            return
        out_dir = None
        if self.var_outmode.get() == "folder":
            out_dir = self.var_outdir.get().strip()
            if not out_dir:
                messagebox.showinfo("Saída", "Escolha a pasta de saída.")
                return
        self._save_settings()
        self.cancel_evt.clear()
        self._set_running(True)

        def work():
            results = core.convert_batch(
                files, out_dir, opts,
                log=lambda m: self.msgs.put(("log", m)),
                progress=lambda x: None,
                cancel=self.cancel_evt,
                status=self._status_cb)
            ok = sum(1 for r in results if not r.error)
            self.msgs.put(("log", f"\nConcluído: {ok} de {len(files)} arquivo(s) convertido(s)."))
            self.msgs.put(("done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    # ------------------------------------------------------ atualização
    def check_updates(self, manual: bool) -> None:
        if self._busy():
            return
        if manual:
            self._log("Procurando atualizações ...")
        self.btn_update.configure(state="disabled")

        def work():
            try:
                info = updater.check_latest()
                self.msgs.put(("upd_found" if updater.is_newer(info.version) else "upd_none", (info, manual)))
            except updater.UpdateError as e:
                self.msgs.put(("upd_error", (str(e), manual)))
            except Exception as e:  # nunca derruba o app por causa de uma checagem
                self.msgs.put(("upd_error", (f"{type(e).__name__}: {e}", manual)))

        threading.Thread(target=work, daemon=True).start()

    def _on_upd_found(self, info, manual: bool) -> None:
        self.btn_update.configure(state="normal")
        self.pending_release = info
        self.var_upd_banner.set(f"Nova versão {info.version} disponível")
        notes = info.notes[:600] + ("…" if len(info.notes) > 600 else "")
        if updater.can_self_update():
            if messagebox.askyesno("Atualização disponível",
                                   f"Versão {info.version} disponível (você está na {appver.__version__}).\n\n"
                                   f"{notes}\n\nBaixar e instalar agora? O programa será reaberto.",
                                   parent=self):
                self.start_update(info)
        else:
            if messagebox.askyesno("Atualização disponível",
                                   f"Versão {info.version} disponível (você está na {appver.__version__}).\n\n"
                                   f"{notes}\n\nA troca automática só funciona no programa .exe. "
                                   "Abrir a página de download?", parent=self):
                import webbrowser
                webbrowser.open(info.html_url)

    def start_update(self, info) -> None:
        if self._busy():
            return
        self.cancel_evt.clear()
        self._set_running(True)
        self._log(f"Baixando a versão {info.version} ...")

        def work():
            try:
                def prog(done, total):
                    frac = (done / total) if total else None
                    self._status_cb(f"Baixando atualização: {done / 1e6:.0f} de {total / 1e6:.0f} MB"
                                    if total else f"Baixando atualização: {done / 1e6:.0f} MB", frac)
                path = updater.download(info, prog, self.cancel_evt)
                self.msgs.put(("upd_ready", path))
            except updater.UpdateError as e:
                self.msgs.put(("log", f"  Atualização não concluída: {e}"))
            except Exception as e:
                self.msgs.put(("log", f"  ERRO na atualização: {type(e).__name__}: {e}"))
            self.msgs.put(("done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_upd_ready(self, path: str) -> None:
        self._log("Download concluído e verificado (SHA-256). Reiniciando para instalar ...")
        try:
            self._save_settings()
            updater.apply_update(path)
        except Exception as e:
            self._log(f"  Não consegui aplicar a atualização: {e}")
            return
        self.destroy()

    # ------------------------------------------------------ otimizar IFC
    def start_optimize(self) -> None:
        if self._busy():
            return
        files = list(self.ifc_list.files)
        if not files:
            messagebox.showinfo("Sem arquivos", "Adicione ao menos um arquivo .ifc.")
            return
        try:
            max_tri = int(parse_float(self.var_o_maxtri.get(), 0))
        except ValueError:
            messagebox.showerror("Opções", "Limite de triângulos inválido: use um número inteiro (0 = só limpeza).")
            return
        if max_tri < 0:
            messagebox.showerror("Opções", "Limite de triângulos não pode ser negativo.")
            return
        out_dir = None
        if self.var_o_outmode.get() == "folder":
            out_dir = self.var_o_outdir.get().strip()
            if not out_dir:
                messagebox.showinfo("Saída", "Escolha a pasta de saída.")
                return
        try:
            keep = parse_float(self.var_o_keep.get(), 100.0)
        except ValueError:
            messagebox.showerror("Opções", "Percentual inválido: use um número entre 1 e 100.")
            return
        if not 1 <= keep <= 100:
            messagebox.showerror("Opções", "O percentual deve ficar entre 1 e 100.")
            return
        o = opt.OptOptions(max_tri_per_rep=max_tri, keep_ratio=keep / 100.0,
                           overwrite=self.var_o_overwrite.get())
        self._save_settings()
        self.cancel_evt.clear()
        self._set_running(True)

        def work():
            results = opt.optimize_batch(
                files, out_dir, o,
                log=lambda m: self.msgs.put(("log", m)),
                status=self._status_cb,
                cancel=self.cancel_evt)
            ok = sum(1 for r in results if not r.error)
            self.msgs.put(("log", f"\nConcluído: {ok} de {len(files)} arquivo(s) otimizado(s)."))
            self.msgs.put(("done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def cancel(self) -> None:
        self.cancel_evt.set()
        self._log("Cancelando… (termina a etapa em andamento)")

    # ------------------------------------------------------------ mensagens
    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.msgs.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self._apply_status(*payload)
                elif kind == "tags":
                    self._fill_tree(payload)
                elif kind == "model":
                    self._view_set_model(payload)
                elif kind == "upd_found":
                    self._on_upd_found(*payload)
                elif kind == "upd_none":
                    self.btn_update.configure(state="normal")
                    self.var_upd_banner.set("")
                    if payload[1]:
                        self._log(f"Você já está na versão mais recente ({appver.__version__}).")
                        messagebox.showinfo("Atualizações", "Você já está na versão mais recente.", parent=self)
                elif kind == "upd_error":
                    self.btn_update.configure(state="normal")
                    if payload[1]:
                        self._log(f"  Não foi possível verificar: {payload[0]}")
                        messagebox.showwarning("Atualizações", payload[0], parent=self)
                elif kind == "upd_ready":
                    self._on_upd_ready(payload)
                elif kind == "done":
                    self._set_running(False)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # --------------------------------------------------------- preferências
    def _save_settings(self) -> None:
        data = {
            "unit": self.var_unit.get(), "schema": self.var_schema.get(),
            "ox": self.var_ox.get(), "oy": self.var_oy.get(), "oz": self.var_oz.get(),
            "tomin": self.var_tomin.get(), "hidden": self.var_hidden.get(),
            "overwrite": self.var_overwrite.get(), "keywords": self.var_kw.get(),
            "outmode": self.var_outmode.get(), "outdir": self.var_outdir.get(),
            "recursive": self.skp_list.var_recursive.get(), "maxtri": self.var_maxtri.get(),
            "o_outmode": self.var_o_outmode.get(), "o_outdir": self.var_o_outdir.get(),
            "o_overwrite": self.var_o_overwrite.get(), "o_maxtri": self.var_o_maxtri.get(), "o_keep": self.var_o_keep.get(),
            "o_recursive": self.ifc_list.var_recursive.get(), "autoupd": self.var_autoupd.get(),
        }
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load_settings(self) -> None:
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return

        def put(var, key, valid=None):
            if key in d and (valid is None or d[key] in valid):
                var.set(d[key])

        put(self.var_unit, "unit", UNIT_LABELS)
        put(self.var_schema, "schema", SCHEMA_LABELS)
        for var, key in ((self.var_ox, "ox"), (self.var_oy, "oy"), (self.var_oz, "oz"),
                         (self.var_kw, "keywords"), (self.var_outdir, "outdir"),
                         (self.var_maxtri, "maxtri"), (self.var_o_outdir, "o_outdir"),
                         (self.var_o_maxtri, "o_maxtri"), (self.var_o_keep, "o_keep")):
            put(var, key)
        for var, key in ((self.var_tomin, "tomin"), (self.var_hidden, "hidden"),
                         (self.var_overwrite, "overwrite"), (self.skp_list.var_recursive, "recursive"),
                         (self.var_o_overwrite, "o_overwrite"), (self.ifc_list.var_recursive, "o_recursive"),
                         (self.var_autoupd, "autoupd")):
            put(var, key)
        put(self.var_outmode, "outmode", ("side", "folder"))
        put(self.var_o_outmode, "o_outmode", ("side", "folder"))

    def _on_close(self) -> None:
        self._save_settings()
        self.cancel_evt.set()
        self.destroy()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        import cli
        sys.exit(cli.main(sys.argv[2:]))
    App().mainloop()


if __name__ == "__main__":
    main()
