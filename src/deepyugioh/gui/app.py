"""Test GUI: fine-tune the Combo Generator and read its results.

Only available in test mode (`deepyugioh testmode on`), started with
`deepyugioh gui`. Tabs:

    Einstellungen   all generator settings, saved permanently in deepyugioh.toml
    Einzelsuche     search one start setup with the current settings
    Globaler Lauf   resumable global search with stop button and coverage
    Combos          Database 1: filter, inspect, export
    Läufe           every stored search, why it stopped early, which setting to change

Engine work runs in a background thread; every thread opens its own SQLite
connections. Results come back through a queue polled by the Tk main loop.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import tkinter as tk
from dataclasses import fields
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from deepyugioh import settings
from deepyugioh.contracts.combos import BENEFIT_CRITERIA, ComboRecord
from deepyugioh.contracts.setups import StartSetup
from deepyugioh.generator import TIERS, CandidateConfig, GeneratorConfig, SearchLimits
from deepyugioh.gui import format as fmt


class TestModeRequired(RuntimeError):
    pass


# ---- setting definitions -----------------------------------------------------
# (key, label, help, min, max)
LIMIT_NUMBERS = [
    ("min_cards", "Min. Karten pro Combo", "Darunter ist es der Effekt einer einzelnen Karte", 1, 60),
    ("max_cards", "Max. Karten pro Combo", "Wie viele bestimmte Karten eine Combo höchstens braucht (z. B. 5 oder mehr)", 1, 60),
    ("max_depth", "Max. Entscheidungen pro Linie", "Tiefe einer einzelnen Linie", 1, 100_000),
    ("max_nodes", "Max. Knoten pro Suche", "Entscheidungspunkte, die eine Suche höchstens expandiert", 1, 1_000_000_000),
    ("time_limit", "Zeitlimit pro Suche (s)", "Pro Startaufbau", 0.1, 7 * 86_400),
    ("max_choices", "Max. Optionen pro Kartenauswahl", "Mehr Optionen werden abgeschnitten (Lauf unvollständig)", 1, 1_000_000),
]
LIMIT_FLAGS = [
    ("transpositions", "Transpositionen erkennen", "Gleiche Spielstände nur einmal durchsuchen"),
    ("drop_redundant", "Redundante Combos verwerfen", "Wenn eine echte Teilmenge der Karten denselben Nutzen erreicht"),
    ("allow_mset", "Monster setzen durchsuchen", "Verzweigt zusätzlich über verdecktes Setzen"),
    ("allow_sset", "Zauber/Fallen setzen durchsuchen", "Verzweigt zusätzlich über Setzen in die Zone"),
]
CANDIDATE_NUMBERS = [
    ("max_deck_neighbors", "Max. Deck-Nachbarn", "Karten aus der Interaktions-Nachbarschaft im Deck", 0, 200),
    ("max_extra", "Max. Extra Deck", "Größe des Extra Decks im Startaufbau", 0, 200),
]
_DEFAULT_LIMITS = SearchLimits()
_DEFAULT_CANDIDATES = CandidateConfig()


def _parse_number(raw: str, kind: type, label: str, lo, hi):
    raw = raw.strip().replace(",", ".") if kind is float else raw.strip()
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f"{label}: '{raw}' ist keine {'ganze ' if kind is int else ''}Zahl") from None
    if not lo <= value <= hi:
        raise ValueError(f"{label}: {value} liegt nicht zwischen {lo} und {hi}")
    return value


def _optional(raw: str, kind: type, label: str):
    raw = raw.strip()
    return None if not raw else _parse_number(raw, kind, label, 0 if kind is float else 1, 10**12)


# ---- small widget helpers ----------------------------------------------------
def make_tree(parent, columns: list[tuple[str, str, int]], height: int = 12) -> tuple[ttk.Frame, ttk.Treeview]:
    """Treeview with scrollbar; click on a heading sorts by that column."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height, selectmode="browse")
    for key, heading, width in columns:
        tree.heading(key, text=heading, command=lambda k=key: _sort(tree, k))
        tree.column(key, width=width, stretch=width >= 200, anchor="w" if width >= 120 else "center")
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return frame, tree


def _sort(tree: ttk.Treeview, col: str):
    rows = [(tree.set(i, col), i) for i in tree.get_children("")]

    def key(v):
        try:
            return (0, float(v[0].rstrip("s %")), "")
        except ValueError:
            return (1, 0.0, v[0].lower())

    reverse = getattr(tree, "_sort_state", None) == col
    rows.sort(key=key, reverse=reverse)
    for n, (_, item) in enumerate(rows):
        tree.move(item, "", n)
    tree._sort_state = None if reverse else col


def make_text(parent, height: int = 10, width: int = 80) -> tuple[ttk.Frame, tk.Text]:
    frame = ttk.Frame(parent)
    text = tk.Text(frame, height=height, width=width, wrap="none", font="TkFixedFont", undo=False)
    sy = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    sx = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    text.grid(row=0, column=0, sticky="nsew")
    sy.grid(row=0, column=1, sticky="ns")
    sx.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, text


def set_text(widget: tk.Text, content: str, readonly: bool = True):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", content)
    if readonly:
        widget.configure(state="disabled")


# ---- application ---------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk):
        from deepyugioh.cards.store import CardStore
        from deepyugioh.repository.combo_db import ComboRepository

        self.root = root
        self.store = CardStore()          # main thread only
        self.repo = ComboRepository()     # main thread only (reading)
        self.queue: queue.Queue = queue.Queue()
        self.busy: str | None = None
        self.closing = False
        self.busy_listeners: list[Callable[[], None]] = []

        try:
            self.saved = settings.generator_config()
        except ValueError as e:
            messagebox.showerror("Einstellungen", f"{settings.settings_path()} ist ungültig:\n{e}\n\nEs gelten die Standardwerte.")
            self.saved = GeneratorConfig()

        root.title("DeepYUGIOH - Testmodus: Combo Generator")
        root.geometry("1400x900")
        root.minsize(1000, 650)
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Dirty.TLabel", foreground="#b35c00")
        style.configure("Head.TLabel", font=("TkDefaultFont", 11, "bold"))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.settings_tab = SettingsTab(self)
        self.search_tab = SearchTab(self)
        self.global_tab = GlobalTab(self)
        self.combos_tab = CombosTab(self)
        self.runs_tab = RunsTab(self)
        for tab, title in ((self.settings_tab, "Einstellungen"), (self.search_tab, "Einzelsuche"),
                           (self.global_tab, "Globaler Lauf"), (self.combos_tab, "Combos (Database 1)"),
                           (self.runs_tab, "Läufe & Tuning")):
            self.notebook.add(tab.frame, text=title)
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=6, pady=4)
        self.status = tk.StringVar(value="bereit")
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        ttk.Label(bar, text=f"Testmodus aktiv · Einstellungen: {settings.settings_path()}",
                  style="Hint.TLabel").pack(side="right")

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._poll)

    # ---- configuration -------------------------------------------------------
    def config(self) -> GeneratorConfig:
        """The configuration in the settings form (also unsaved changes). Raises ValueError."""
        return self.settings_tab.read()

    def config_or_warn(self) -> GeneratorConfig | None:
        try:
            return self.config()
        except ValueError as e:
            messagebox.showerror("Ungültige Einstellungen", str(e))
            self.notebook.select(self.settings_tab.frame)
            return None

    # ---- background work -----------------------------------------------------
    def post(self, fn: Callable, *args):
        """Run fn(*args) in the Tk main thread (callable from any thread)."""
        self.queue.put((fn, args))

    def task(self, label: str, work: Callable[[], object], done: Callable[[object], None], *, exclusive: bool = True) -> bool:
        if exclusive and self.busy:
            messagebox.showinfo("Beschäftigt", f"Es läuft bereits: {self.busy}")
            return False
        if exclusive:
            self.busy = label
            self._busy_changed()
        self.status.set(f"{label} läuft …")
        t0 = time.perf_counter()

        def target():
            try:
                result = work()
                self.post(done, result)
                self.post(self.status.set, f"{label} fertig ({time.perf_counter() - t0:.1f}s)")
            except Exception as e:  # shown to the user, never lost in the thread
                self.post(self._failed, label, e, traceback.format_exc())
            finally:
                if exclusive:
                    self.post(self._finished)

        threading.Thread(target=target, name=label, daemon=True).start()
        return True

    def _failed(self, label: str, error: Exception, trace: str):
        self.status.set(f"{label} fehlgeschlagen: {error}")
        print(trace, flush=True)
        messagebox.showerror(label, f"{type(error).__name__}: {error}")

    def _finished(self):
        self.busy = None
        self._busy_changed()
        if self.closing:
            self.root.destroy()

    def _busy_changed(self):
        for fn in self.busy_listeners:
            fn()

    def _poll(self):
        try:
            while True:
                fn, args = self.queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        try:
            self.root.after(100, self._poll)
        except tk.TclError:  # window already destroyed
            pass

    def _tab_changed(self, _event):
        tab = self.notebook.nametowidget(self.notebook.select())
        for t in (self.combos_tab, self.runs_tab):
            if t.frame is tab and not t.loaded:  # first visit; afterwards via the refresh buttons
                t.loaded = True
                t.refresh()

    def close(self):
        if self.settings_tab.dirty and not messagebox.askyesno(
            "Ungespeicherte Einstellungen", "Die Einstellungen wurden geändert, aber nicht gespeichert. Trotzdem beenden?"
        ):
            return
        if self.busy:
            if not messagebox.askyesno(
                "Läuft noch", f"{self.busy} läuft noch.\nAbbrechen und beenden? Laufende Suchen werden noch "
                "abgeschlossen und gespeichert, danach schließt das Fenster."
            ):
                return
            self.closing = True
            self.global_tab.stop()
            self.status.set("wird beendet, sobald die laufenden Suchen fertig sind …")
            return
        self.root.destroy()

    # ---- cross-tab actions -----------------------------------------------------
    def open_in_search(self, setup: StartSetup):
        self.search_tab.load_setup(setup)
        self.notebook.select(self.search_tab.frame)


# ---- tab: settings -----------------------------------------------------------------
class SettingsTab:
    def __init__(self, app: App):
        self.app = app
        self.frame = ttk.Frame(app.notebook, padding=10)
        self.vars: dict[str, tk.Variable] = {}
        self.tier_vars: dict[int, tk.BooleanVar] = {}
        self.board_vars: dict[str, tk.BooleanVar] = {}
        self.boards: dict[str, list[dict]] = dict(app.saved.opponent_boards)
        self._kinds: dict[str, tuple] = {}   # number field -> (type, label, min, max)
        self.dirty = False
        self._loading = False

        head = ttk.Frame(self.frame)
        head.pack(fill="x")
        ttk.Label(head, text="Generator-Einstellungen", style="Head.TLabel").pack(side="left")
        self.dirty_label = ttk.Label(head, text="", style="Dirty.TLabel")
        self.dirty_label.pack(side="left", padx=12)
        ttk.Button(head, text="Standardwerte", command=self.defaults).pack(side="right")
        ttk.Button(head, text="Verwerfen", command=self.reload).pack(side="right", padx=4)
        ttk.Button(head, text="Speichern", command=self.save).pack(side="right")
        ttk.Label(self.frame, style="Hint.TLabel", text=(
            "Einzelsuche und globaler Lauf verwenden immer die Werte in diesem Formular. "
            "„Speichern“ macht sie dauerhaft – auch für `deepyugioh generate`, `plan` und `search`."
        )).pack(fill="x", pady=(2, 10))

        cols = ttk.Frame(self.frame)
        cols.pack(fill="both", expand=True)
        left = ttk.LabelFrame(cols, text="Suchgrenzen (pro Startaufbau)", padding=8)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right = ttk.Frame(cols)
        right.pack(side="left", fill="both", expand=True)

        row = 0
        for key, label, hint, lo, hi in LIMIT_NUMBERS:
            row = self._number(left, row, key, label, hint, lo, hi, float if key == "time_limit" else int)
        ttk.Separator(left).grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
        row += 1
        for key, label, hint in LIMIT_FLAGS:
            row = self._flag(left, row, key, label, hint)
        left.columnconfigure(2, weight=1)

        cand = ttk.LabelFrame(right, text="Startaufbauten (globale Suche)", padding=8)
        cand.pack(fill="x")
        ttk.Label(cand, text="Stufen").grid(row=0, column=0, sticky="nw", pady=2)
        tiers = ttk.Frame(cand)
        tiers.grid(row=0, column=1, columnspan=2, sticky="w")
        for tier, desc in TIERS.items():
            v = self.tier_vars[tier] = tk.BooleanVar()
            v.trace_add("write", self._changed)
            ttk.Checkbutton(tiers, text=f"{tier}: {desc}", variable=v).pack(anchor="w")
        ttk.Label(cand, text="Gegnerfelder").grid(row=1, column=0, sticky="nw", pady=(8, 2))
        self.boards_frame = ttk.Frame(cand)
        self.boards_frame.grid(row=1, column=1, columnspan=2, sticky="w", pady=(8, 2))
        row = 2
        for key, label, hint, lo, hi in CANDIDATE_NUMBERS:
            row = self._number(cand, row, key, label, hint, lo, hi, int)
        row = self._flag(cand, row, "enablers", "Hilfskarten ergänzen", "Z. B. Foolish Burial, wenn eine Karte im Friedhof wirkt")
        cand.columnconfigure(2, weight=1)

        run = ttk.LabelFrame(right, text="Ausführung", padding=8)
        run.pack(fill="x", pady=(8, 0))
        row = self._number(run, 0, "workers", "Parallele Prozesse", f"Nur globaler Lauf; dieser Rechner hat {os.cpu_count()} Kerne",
                           1, max(64, os.cpu_count() or 1), int)
        self._flag(run, row, "validate", "Combos per Replay validieren", "Nur validierte Combos kommen in Database 1")
        run.columnconfigure(2, weight=1)

        ttk.Label(right, style="Hint.TLabel", wraplength=520, justify="left", text=(
            "Gegnerfelder selbst werden in der Einstellungsdatei unter [opponent_boards.NAME] definiert "
            "(Format siehe deepyugioh.example.toml); hier wird nur ausgewählt, welche verwendet werden."
        )).pack(fill="x", pady=(10, 0))

        self.fill(app.saved)

    # ---- widgets -------------------------------------------------------------
    def _number(self, parent, row, key, label, hint, lo, hi, kind):
        v = self.vars[key] = tk.StringVar()
        v.trace_add("write", self._changed)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        inc = 1 if kind is int else 5.0
        ttk.Spinbox(parent, textvariable=v, from_=lo, to=hi, increment=inc, width=12).grid(row=row, column=1, sticky="w", padx=6)
        ttk.Label(parent, text=hint, style="Hint.TLabel").grid(row=row, column=2, sticky="w")
        self._kinds[key] = (kind, label, lo, hi)
        return row + 1

    def _flag(self, parent, row, key, label, hint):
        v = self.vars[key] = tk.BooleanVar()
        v.trace_add("write", self._changed)
        ttk.Checkbutton(parent, text=label, variable=v).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(parent, text=hint, style="Hint.TLabel").grid(row=row, column=2, sticky="w")
        return row + 1

    def _changed(self, *_):
        if not self._loading:
            self.dirty = True
            self.dirty_label.configure(text="● ungespeicherte Änderungen")

    def _set_clean(self):
        self.dirty = False
        self.dirty_label.configure(text="")

    # ---- form <-> config -------------------------------------------------------
    def fill(self, cfg: GeneratorConfig):
        self._loading = True
        try:
            for f in fields(SearchLimits):
                self.vars[f.name].set(getattr(cfg.limits, f.name))
            for key in ("max_deck_neighbors", "max_extra", "enablers"):
                self.vars[key].set(getattr(cfg.candidates, key))
            self.vars["workers"].set(cfg.workers)
            self.vars["validate"].set(cfg.validate)
            for tier, v in self.tier_vars.items():
                v.set(tier in cfg.candidates.tiers)
            self.boards = dict(cfg.opponent_boards)
            for child in self.boards_frame.winfo_children():
                child.destroy()
            self.board_vars = {}
            for name, cards in self.boards.items():
                v = self.board_vars[name] = tk.BooleanVar(value=name in cfg.candidates.opponent_boards)
                v.trace_add("write", self._changed)
                desc = ", ".join(c["card"] if isinstance(c["card"], str) else str(c["card"]) for c in cards) or "leer"
                ttk.Checkbutton(self.boards_frame, text=f"{name}  ({desc})", variable=v).pack(anchor="w")
        finally:
            self._loading = False
        self.app.post(self._after_fill)

    def _after_fill(self):
        self._set_clean()
        for tab in ("search_tab", "global_tab"):
            if hasattr(self.app, tab):
                getattr(self.app, tab).config_changed()

    def read(self) -> GeneratorConfig:
        num = {}
        for key, (kind, label, lo, hi) in self._kinds.items():
            num[key] = _parse_number(str(self.vars[key].get()), kind, label, lo, hi)
        flags = {k: bool(self.vars[k].get()) for k, *_ in LIMIT_FLAGS}
        limits = SearchLimits(**{k: num[k] for k, *_ in LIMIT_NUMBERS}, **flags)
        candidates = CandidateConfig(
            tiers=tuple(t for t, v in self.tier_vars.items() if v.get()),
            opponent_boards=tuple(n for n, v in self.board_vars.items() if v.get()),
            max_deck_neighbors=num["max_deck_neighbors"],
            max_extra=num["max_extra"],
            enablers=bool(self.vars["enablers"].get()),
        )
        cfg = GeneratorConfig(limits, candidates, dict(self.boards), num["workers"], bool(self.vars["validate"].get()))
        try:
            return cfg.check()
        except ValueError as e:
            raise ValueError(f"Ungültige Einstellungen: {e}") from None

    # ---- buttons -----------------------------------------------------------------
    def save(self):
        cfg = self.app.config_or_warn()
        if cfg is None:
            return
        path = settings.save_generator_config(cfg)
        self.app.saved = cfg
        self._set_clean()
        self.app.status.set(f"Einstellungen gespeichert: {path}")

    def reload(self):
        try:
            cfg = settings.generator_config()
        except ValueError as e:
            messagebox.showerror("Einstellungen", f"{settings.settings_path()} ist ungültig:\n{e}")
            return
        self.app.saved = cfg
        self.fill(cfg)
        self.app.status.set("Gespeicherte Einstellungen geladen")

    def defaults(self):
        self.fill(GeneratorConfig().with_(opponent_boards={**GeneratorConfig().opponent_boards, **self.boards}))
        self.app.post(self._changed)
        self.app.status.set("Standardwerte eingetragen – noch nicht gespeichert")


# ---- shared: combo list with detail view ---------------------------------------------
class ComboView:
    """Combo table + detail text. `runs_for` adds the runs a stored combo was found in."""

    def __init__(self, app: App, parent, runs_for: Callable[[int], list[dict]] | None = None):
        self.app = app
        self.runs_for = runs_for
        self.combos: list[ComboRecord] = []
        self.frame = ttk.Panedwindow(parent, orient="horizontal")
        left = ttk.Frame(self.frame)
        tree_frame, self.tree = make_tree(left, [
            ("n", "#", 50), ("cards", "Karten", 55), ("actions", "Aktionen", 65), ("opp", "Gegner", 80),
            ("names", "Benötigte Karten", 330), ("benefit", "Nutzen", 220),
        ])
        tree_frame.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.frame.add(left, weight=3)

        right = ttk.Frame(self.frame)
        opts = ttk.Frame(right)
        opts.pack(fill="x")
        self.all_steps = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Alle Engine-Schritte zeigen", variable=self.all_steps, command=self._select).pack(side="left")
        self.open_btn = ttk.Button(opts, text="Startaufbau in Einzelsuche öffnen", command=self._open, state="disabled")
        self.open_btn.pack(side="right")
        text_frame, self.text = make_text(right, height=20, width=70)
        text_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.frame.add(right, weight=2)

    def set(self, combos: list[ComboRecord]):
        self.combos = combos
        self.tree.delete(*self.tree.get_children())
        name = self.app.store.name
        for i, c in enumerate(combos):
            visible = sum(s.label not in fmt.HIDDEN_STEPS for s in c.line)
            self.tree.insert("", "end", iid=str(i), values=(
                c.id if c.id is not None else i + 1, c.card_count, visible, c.setup.opponent.name,
                fmt.names(c.cards, name), ", ".join(c.positive),
            ))
        set_text(self.text, "" if combos else "Keine Combos.")
        self.open_btn.configure(state="disabled")
        if combos:
            self.tree.selection_set("0")

    def selected(self) -> ComboRecord | None:
        sel = self.tree.selection()
        return self.combos[int(sel[0])] if sel else None

    def _select(self, *_):
        c = self.selected()
        if c is None:
            return
        runs = self.runs_for(c.id) if self.runs_for and c.id is not None else None
        set_text(self.text, fmt.combo_text(c, self.app.store.name, all_steps=self.all_steps.get(), runs=runs))
        self.open_btn.configure(state="normal")

    def _open(self):
        c = self.selected()
        if c is not None:
            self.app.open_in_search(c.setup)


# ---- tab: single search ------------------------------------------------------------------
class SearchTab:
    def __init__(self, app: App):
        self.app = app
        self.frame = ttk.Frame(app.notebook, padding=8)
        top = ttk.Frame(self.frame)
        top.pack(fill="x")

        # card finder
        finder = ttk.LabelFrame(top, text="Karte suchen", padding=6)
        finder.pack(side="left", fill="y")
        self.query = tk.StringVar()
        entry = ttk.Entry(finder, textvariable=self.query, width=34)
        entry.pack(fill="x")
        self.query.trace_add("write", lambda *_: self._find())
        self.results = tk.Listbox(finder, height=8, width=40, exportselection=False)
        self.results.pack(fill="both", expand=True, pady=4)
        self.results.bind("<Double-Button-1>", lambda _e: self._add("hand"))
        btns = ttk.Frame(finder)
        btns.pack(fill="x")
        for target, label in (("hand", "→ Hand"), ("deck", "→ Deck"), ("extra", "→ Extra")):
            ttk.Button(btns, text=label, width=8, command=lambda t=target: self._add(t)).pack(side="left", padx=1)
        self.found: list = []

        # setup
        setup = ttk.LabelFrame(top, text="Startaufbau (eine Karte pro Zeile, Name oder Passcode, „3x Name“ für Kopien)", padding=6)
        setup.pack(side="left", fill="both", expand=True, padx=6)
        self.lists: dict[str, tk.Text] = {}
        for col, (key, label) in enumerate((("hand", "Hand"), ("deck", "Deck"), ("extra", "Extra Deck"))):
            ttk.Label(setup, text=label).grid(row=0, column=col, sticky="w")
            box = tk.Text(setup, height=9, width=30, undo=True)
            box.grid(row=1, column=col, sticky="nsew", padx=2)
            setup.columnconfigure(col, weight=1)
            self.lists[key] = box
        opts = ttk.Frame(setup)
        opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(opts, text="Gegnerfeld").pack(side="left")
        self.opponent = tk.StringVar(value="empty")
        self.opponent_box = ttk.Combobox(opts, textvariable=self.opponent, state="readonly", width=14)
        self.opponent_box.pack(side="left", padx=4)
        self.branch_zones = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Zonen verzweigen (Link)", variable=self.branch_zones).pack(side="left", padx=8)
        self.store_results = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Ergebnis in Database 1 speichern", variable=self.store_results).pack(side="left", padx=8)
        ttk.Button(opts, text="Leeren", command=self.clear).pack(side="right")
        self.start_btn = ttk.Button(opts, text="Suche starten", command=self.start)
        self.start_btn.pack(side="right", padx=4)

        self.summary = tk.StringVar(value="Noch keine Suche.")
        ttk.Label(self.frame, textvariable=self.summary, justify="left", wraplength=1300).pack(fill="x", pady=6)
        self.view = ComboView(app, self.frame)
        self.view.frame.pack(fill="both", expand=True)
        app.busy_listeners.append(self._busy)
        self.config_changed()

    def config_changed(self):
        boards = list(self.app.settings_tab.boards) if hasattr(self.app, "settings_tab") else list(self.app.saved.opponent_boards)
        self.opponent_box.configure(values=boards)
        if self.opponent.get() not in boards and boards:
            self.opponent.set(boards[0])

    def _busy(self):
        self.start_btn.configure(state="disabled" if self.app.busy else "normal")

    def _find(self):
        self.found = self.app.store.search(self.query.get(), limit=60) if len(self.query.get().strip()) >= 2 else []
        self.results.delete(0, "end")
        for card in self.found:
            self.results.insert("end", f"{card.name}  [{card.code}]")

    def _add(self, target: str):
        sel = self.results.curselection()
        if not sel:
            return
        card = self.found[sel[0]]
        box = self.lists[target]
        content = box.get("1.0", "end").rstrip("\n")
        box.delete("1.0", "end")
        box.insert("1.0", (content + "\n" if content else "") + card.name)

    def clear(self):
        for box in self.lists.values():
            box.delete("1.0", "end")

    def load_setup(self, setup: StartSetup):
        name = self.app.store.name
        for key in ("hand", "deck", "extra"):
            set_text(self.lists[key], fmt.card_list_text(getattr(setup, key), name), readonly=False)
        boards = self.opponent_box.cget("values")
        if setup.opponent.name in boards:
            self.opponent.set(setup.opponent.name)
        else:
            messagebox.showwarning("Gegnerfeld", f"Gegnerfeld '{setup.opponent.name}' ist in den Einstellungen nicht definiert.")
        self.branch_zones.set(setup.branch_zones)

    def _codes(self, key: str) -> tuple[int, ...]:
        refs = fmt.parse_card_list(self.lists[key].get("1.0", "end"))
        out = []
        for ref in refs:
            try:
                out.append(self.app.store.resolve(ref).code)
            except KeyError:
                hits = self.app.store.search(ref, limit=3)
                guess = f"\nMeintest du: {', '.join(c.name for c in hits)}?" if hits else ""
                raise ValueError(f"{key}: unbekannte Karte '{ref}'{guess}") from None
        return tuple(out)

    def start(self):
        cfg = self.app.config_or_warn()
        if cfg is None:
            return
        try:
            hand = self._codes("hand")
            if not hand:
                raise ValueError("Die Hand ist leer.")
            boards = cfg.boards(self.app.store)
            setup = StartSetup(hand=hand, deck=self._codes("deck"), extra=self._codes("extra"),
                               opponent=boards[self.opponent.get()], branch_zones=self.branch_zones.get())
        except (ValueError, KeyError) as e:
            messagebox.showerror("Startaufbau", str(e).strip("'\""))
            return
        store = self.store_results.get()
        self.summary.set(f"Suche läuft … (Zeitlimit {cfg.limits.time_limit:g}s, max. {cfg.limits.max_nodes} Knoten, "
                         f"{cfg.limits.min_cards}–{cfg.limits.max_cards} Karten)")

        def work():
            from deepyugioh.cards.store import CardStore
            from deepyugioh.generator import ComboGenerator
            from deepyugioh.repository.combo_db import ComboRepository

            return ComboGenerator(CardStore(), ComboRepository() if store else None, cfg).search(setup)

        self.app.task("Einzelsuche", work, self._done)

    def _done(self, rep):
        run = rep.run
        status = "vollständig" if run.complete else f"UNVOLLSTÄNDIG: {fmt.reasons(run.incomplete)}"
        hints = [fmt.INCOMPLETE_HINTS[k] for k in run.incomplete if k in fmt.INCOMPLETE_HINTS]
        text = (f"{run.nodes} Knoten, {run.replays} Replays, {run.elapsed_s:.2f}s – {status}"
                f"{'  →  ' + '; '.join(hints) if hints else ''}\n"
                f"Beschnitten: {fmt.reasons(run.pruned)}\n"
                f"{run.combos_found} Combos gefunden, {len(rep.combos)} per Replay validiert")
        if rep.run_id is not None:
            text += f" · gespeichert als Lauf #{rep.run_id}, {rep.new_combos} neu in Database 1"
        self.summary.set(text)
        self.view.set(rep.combos)


# ---- tab: global run -----------------------------------------------------------------
class _Listener:
    def __init__(self, app: App, on_job):
        self.app, self.on_job_main = app, on_job

    def on_job(self, job):  # worker thread
        self.app.post(self.on_job_main, job)


class GlobalTab:
    MAX_LOG_LINES = 3000

    def __init__(self, app: App):
        self.app = app
        self.frame = ttk.Frame(app.notebook, padding=8)
        self.stop_event: threading.Event | None = None

        opts = ttk.LabelFrame(self.frame, text="Lauf", padding=6)
        opts.pack(fill="x")
        ttk.Label(opts, text="Stufen").pack(side="left")
        self.tier_vars = {t: tk.BooleanVar() for t in TIERS}
        for t, v in self.tier_vars.items():
            ttk.Checkbutton(opts, text=str(t), variable=v).pack(side="left")
        ttk.Label(opts, text="Max. Suchen").pack(side="left", padx=(16, 2))
        self.max_jobs = tk.StringVar(value="100")
        ttk.Entry(opts, textvariable=self.max_jobs, width=9).pack(side="left")
        ttk.Label(opts, text="Zeitbudget (s)").pack(side="left", padx=(12, 2))
        self.time_budget = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.time_budget, width=9).pack(side="left")
        ttk.Label(opts, text="leer = unbegrenzt", style="Hint.TLabel").pack(side="left", padx=6)
        self.stop_btn = ttk.Button(opts, text="Stoppen", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.start_btn = ttk.Button(opts, text="Lauf starten / fortsetzen", command=self.start)
        self.start_btn.pack(side="right", padx=4)

        self.counters = tk.StringVar(value="Kein Lauf aktiv. Ein Lauf setzt immer am gespeicherten Stand fort.")
        ttk.Label(self.frame, textvariable=self.counters).pack(fill="x", pady=6)

        panes = ttk.Panedwindow(self.frame, orient="vertical")
        panes.pack(fill="both", expand=True)
        cov = ttk.LabelFrame(panes, text="Abdeckung der globalen Suche", padding=4)
        bar = ttk.Frame(cov)
        bar.pack(fill="x")
        ttk.Button(bar, text="Aktualisieren", command=self.refresh_coverage).pack(side="left")
        ttk.Label(bar, text="mit den Kandidaten-Einstellungen im Formular (Stufen, Gegnerfelder, Nachbarn)",
                  style="Hint.TLabel").pack(side="left", padx=8)
        tree_frame, self.coverage = make_tree(cov, [
            ("tier", "Stufe", 50), ("total", "Kandidaten", 95), ("done", "erledigt", 80), ("pct", "%", 60),
            ("complete", "vollständig", 85), ("incomplete", "unvollst.", 80), ("error", "Fehler", 60),
            ("found", "Combos", 70), ("new", "neu", 60), ("desc", "Beschreibung", 400),
        ], height=5)
        tree_frame.pack(fill="both", expand=True, pady=(4, 0))
        panes.add(cov, weight=1)

        logf = ttk.LabelFrame(panes, text="Protokoll (Suchen mit neuen Combos, Fehler und jede 25. Suche)", padding=4)
        text_frame, self.log = make_text(logf, height=14)
        text_frame.pack(fill="both", expand=True)
        self.log.configure(state="disabled")
        panes.add(logf, weight=3)
        app.busy_listeners.append(self._busy)
        self.config_changed()

    def config_changed(self):
        for t, v in self.tier_vars.items():
            v.set(self.app.settings_tab.tier_vars[t].get())

    def _busy(self):
        if not self.app.busy:
            self.stop_event = None  # also after a failed run
        running = self.stop_event is not None
        self.start_btn.configure(state="disabled" if self.app.busy else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _append(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > self.MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - self.MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    @staticmethod
    def _generator(cfg: GeneratorConfig):
        """Build in the calling (background) thread - SQLite connections are thread bound."""
        from deepyugioh.cards.graph import CardGraph
        from deepyugioh.cards.store import CardStore
        from deepyugioh.generator import ComboGenerator
        from deepyugioh.repository.combo_db import ComboRepository

        graph = CardGraph()
        if graph.version() == "unbuilt":
            raise FileNotFoundError("Interaktionsgraph fehlt – zuerst `deepyugioh import-cards` ausführen")
        return ComboGenerator(CardStore(), ComboRepository(), cfg, graph=graph, pool_factory=CardStore)

    def refresh_coverage(self):
        cfg = self.app.config_or_warn()
        if cfg is not None:
            self.app.task("Abdeckung", lambda: self._generator(cfg).coverage(), self._show_coverage, exclusive=False)

    def _show_coverage(self, cov: dict):
        self.coverage.delete(*self.coverage.get_children())
        for tier, row in cov.items():
            done = row["complete"] + row["incomplete"] + row["error"]
            total = row["total"]
            pct = f"{100 * done / total:.2f}" if total else "-"
            self.coverage.insert("", "end", values=(
                tier, total if total is not None else "?", done, pct, row["complete"], row["incomplete"],
                row["error"], row["combos_found"], row["combos_new"], TIERS.get(tier, ""),
            ))

    def start(self):
        cfg = self.app.config_or_warn()
        if cfg is None:
            return
        tiers = [t for t, v in self.tier_vars.items() if v.get()]
        try:
            if not tiers:
                raise ValueError("Mindestens eine Stufe auswählen.")
            max_jobs = _optional(self.max_jobs.get(), int, "Max. Suchen")
            budget = _optional(self.time_budget.get(), float, "Zeitbudget")
        except ValueError as e:
            messagebox.showerror("Globaler Lauf", str(e))
            return
        stop = threading.Event()
        self.stats = {"jobs": 0, "complete": 0, "incomplete": 0, "error": 0, "found": 0, "new": 0}
        self.t0 = time.perf_counter()
        lim = cfg.limits
        self._append(f"=== Start {time.strftime('%H:%M:%S')}: Stufen {tiers}, max. {max_jobs or 'unbegrenzt'} Suchen, "
                     f"Budget {f'{budget:g}s' if budget else 'unbegrenzt'}, {cfg.workers} Prozess(e); "
                     f"Karten {lim.min_cards}-{lim.max_cards}, "
                     f"Knoten {lim.max_nodes}, Zeitlimit {lim.time_limit:g}s, Gegner {', '.join(cfg.candidates.opponent_boards)}")

        def work():
            gen = self._generator(cfg)
            return gen.run(tiers=tiers, max_jobs=max_jobs, time_budget=budget,
                           progress=_Listener(self.app, self._job), should_stop=stop.is_set)

        self.stop_event = stop
        if not self.app.task("Globaler Lauf", work, self._done):
            self.stop_event = None
            return
        self._busy()

    def stop(self):
        if self.stop_event is not None and not self.stop_event.is_set():
            self.stop_event.set()
            self._append("… Stopp angefordert: keine neuen Suchen, laufende werden abgeschlossen und gespeichert")

    def _job(self, job):
        s = self.stats
        s["jobs"] += 1
        s[job.status] += 1
        s["found"] += job.combos_found
        s["new"] += job.combos_new
        elapsed = time.perf_counter() - self.t0
        self.counters.set(
            f"{s['jobs']} Suchen in {elapsed:.0f}s ({s['jobs'] / max(elapsed, 1e-9) * 60:.1f}/min): "
            f"{s['complete']} vollständig, {s['incomplete']} unvollständig, {s['error']} Fehler · "
            f"{s['found']} Combos gefunden, {s['new']} neu"
        )
        if job.status == "error" or job.combos_new or s["jobs"] % 25 == 0:
            extra = f"  FEHLER {job.error}" if job.error else ""
            self._append(f"[Stufe {job.tier} #{job.position}] {job.status:<10} {job.elapsed_s:6.1f}s  "
                         f"Combos {job.combos_found} (neu {job.combos_new})  Lauf #{job.run_id}{extra}")

    def _done(self, report):
        self._append(f"=== Ende: {report.jobs} Suchen in {report.elapsed_s:.0f}s - {report.complete} vollständig, "
                     f"{report.incomplete} unvollständig, {report.errors} Fehler, {report.skipped} übersprungen; "
                     f"{report.combos_found} Combos, {report.combos_new} neu in Database 1")
        self.stop_event = None
        self._busy()
        self.refresh_coverage()


# ---- tab: combos (Database 1) ---------------------------------------------------------
class CombosTab:
    def __init__(self, app: App):
        self.app = app
        self.loaded = False
        self.frame = ttk.Frame(app.notebook, padding=8)
        self.stats = tk.StringVar()
        ttk.Label(self.frame, textvariable=self.stats, justify="left").pack(fill="x")

        filt = ttk.LabelFrame(self.frame, text="Filter", padding=6)
        filt.pack(fill="x", pady=6)
        ttk.Label(filt, text="Karte").pack(side="left")
        self.card = tk.StringVar()
        e = ttk.Entry(filt, textvariable=self.card, width=30)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self.refresh())
        ttk.Label(filt, text="Nutzen").pack(side="left", padx=(10, 2))
        self.benefit = tk.StringVar(value="")
        ttk.Combobox(filt, textvariable=self.benefit, values=["", *BENEFIT_CRITERIA, "wins"], state="readonly",
                     width=16).pack(side="left")
        ttk.Label(filt, text="Max. Karten").pack(side="left", padx=(10, 2))
        self.max_cards = tk.StringVar(value="")
        ttk.Spinbox(filt, textvariable=self.max_cards, from_=1, to=60, width=5).pack(side="left")
        ttk.Label(filt, text="Anzahl").pack(side="left", padx=(10, 2))
        self.limit = tk.StringVar(value="500")
        ttk.Spinbox(filt, textvariable=self.limit, from_=1, to=1_000_000, increment=100, width=8).pack(side="left")
        ttk.Button(filt, text="Anzeigen", command=self.refresh).pack(side="left", padx=10)
        ttk.Button(filt, text="Exportieren (JSON) …", command=self.export).pack(side="right")

        self.view = ComboView(app, self.frame, runs_for=app.repo.runs_for)
        self.view.frame.pack(fill="both", expand=True)

    def refresh(self):
        try:
            card = None
            if self.card.get().strip():
                card = self.app.store.resolve(self.card.get().strip()).code
            max_cards = _optional(self.max_cards.get(), int, "Max. Karten")
            limit = _optional(self.limit.get(), int, "Anzahl")
        except (KeyError, ValueError) as e:
            messagebox.showerror("Filter", str(e).strip("'\""))
            return
        repo = self.app.repo
        self.stats.set(fmt.stats_text(repo.stats()))
        combos = list(repo.iter_combos(card=card, max_cards=max_cards, positive=self.benefit.get() or None, limit=limit))
        self.view.set(combos)

    def export(self):
        if not self.view.combos:
            messagebox.showinfo("Export", "Keine Combos in der Liste.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="combos.json",
                                            filetypes=[("JSON", "*.json")])
        if not path:
            return
        data = [fmt.combo_export(c, self.app.store.name) for c in self.view.combos]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.app.status.set(f"{len(data)} Combos exportiert nach {path}")


# ---- tab: runs & tuning -------------------------------------------------------------
class RunsTab:
    def __init__(self, app: App):
        self.app = app
        self.loaded = False
        self.frame = ttk.Frame(app.notebook, padding=8)
        self.runs: list[dict] = []

        top = ttk.Panedwindow(self.frame, orient="vertical")
        top.pack(fill="both", expand=True)
        tune = ttk.LabelFrame(top, text="Tuning: warum Suchen unvollständig waren (alle gespeicherten Läufe)", padding=4)
        tune_frame, self.tuning = make_text(tune, height=10)
        tune_frame.pack(fill="both", expand=True)
        top.add(tune, weight=1)

        lower = ttk.Frame(top)
        filt = ttk.Frame(lower)
        filt.pack(fill="x", pady=4)
        self.incomplete_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="Nur unvollständige", variable=self.incomplete_only, command=self.refresh).pack(side="left")
        ttk.Label(filt, text="Stufe").pack(side="left", padx=(12, 2))
        self.tier = tk.StringVar(value="alle")
        box = ttk.Combobox(filt, textvariable=self.tier, values=["alle", *TIERS], state="readonly", width=6)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Label(filt, text="Anzahl").pack(side="left", padx=(12, 2))
        self.limit = tk.StringVar(value="500")
        ttk.Spinbox(filt, textvariable=self.limit, from_=1, to=1_000_000, increment=100, width=8).pack(side="left")
        ttk.Button(filt, text="Aktualisieren", command=self.refresh).pack(side="left", padx=10)
        self.open_btn = ttk.Button(filt, text="Startaufbau in Einzelsuche öffnen", command=self._open, state="disabled")
        self.open_btn.pack(side="right")

        panes = ttk.Panedwindow(lower, orient="horizontal")
        panes.pack(fill="both", expand=True)
        tree_frame, self.tree = make_tree(panes, [
            ("id", "Lauf", 60), ("time", "Zeit", 150), ("tier", "Stufe", 50), ("opp", "Gegner", 80),
            ("nodes", "Knoten", 75), ("elapsed", "Dauer s", 70), ("status", "Status", 90),
            ("reasons", "Abbruchgründe", 240), ("found", "Combos", 65), ("new", "neu", 50),
        ])
        self.tree.bind("<<TreeviewSelect>>", self._select)
        panes.add(tree_frame, weight=3)
        detail_frame, self.detail = make_text(panes, height=16, width=60)
        panes.add(detail_frame, weight=2)
        top.add(lower, weight=3)

    def refresh(self):
        try:
            limit = _optional(self.limit.get(), int, "Anzahl") or 500
        except ValueError as e:
            messagebox.showerror("Läufe", str(e))
            return
        tier = None if self.tier.get() in ("", "alle") else int(self.tier.get())
        repo = self.app.repo
        stats = repo.stats()
        set_text(self.tuning, fmt.stats_text(stats) + "\n\n" + fmt.tuning_text(stats))
        self.runs = repo.recent_runs(limit, incomplete_only=self.incomplete_only.get(), tier=tier)
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.runs):
            self.tree.insert("", "end", iid=str(i), values=(
                r["id"], r["finished_at"].replace("T", " ").split("+")[0], r["tier"] if r["tier"] is not None else "-",
                r["opponent_board"], r["nodes"], f"{r['elapsed_s']:.1f}",
                "vollständig" if r["complete"] else "unvollständig", fmt.reasons(r["incomplete"]),
                r["combos_found"], r["combos_new"],
            ))
        set_text(self.detail, "")
        self.open_btn.configure(state="disabled")

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self.runs[int(sel[0])] if sel else None

    def _select(self, _event=None):
        r = self._selected()
        if r is not None:
            set_text(self.detail, fmt.run_text(r, self.app.store.name))
            self.open_btn.configure(state="normal")

    def _open(self):
        r = self._selected()
        if r is not None:
            self.app.open_in_search(StartSetup.from_dict(r["setup"]))


def run_gui() -> int:
    if not settings.test_mode():
        raise TestModeRequired(
            "Die Test-GUI ist nur im Testmodus verfügbar. Einschalten mit `deepyugioh testmode on` "
            f"(oder {settings.TEST_MODE_ENV}=1)."
        )
    root = tk.Tk()
    try:
        App(root)
    except Exception:
        root.destroy()
        raise
    root.mainloop()
    return 0
