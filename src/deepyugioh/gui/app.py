"""Test GUI: fine-tune the Combo Generator and read its results.

Only available in test mode (`deepyugioh testmode on`), started with
`deepyugioh gui`. Layout: sidebar navigation, a header that always shows the
global run (start/stop, progress, live numbers) and these pages:

    Einstellungen   all generator settings, saved permanently in deepyugioh.toml
    Einzelsuche     search one start setup with the current settings
    Globaler Lauf   options, live numbers, coverage and log of the global search
    Combos          Database 1: filter, inspect, export
    Läufe & Tuning  every stored search, why it stopped early, which setting to change

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
from deepyugioh.gui.theme import Theme


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

AIDE_NUMBERS = [
    ("aide_share", "Anteil AIDE in Stufe 7", "0,05 bis 0,95", 0.05, 0.95),
    ("aide_branching", "Verzweigung pro Effekt", "Karten je Bewegung", 1, 50),
    ("aide_plans_per_card", "Pläne pro Karte", "je Start- bzw. Zielkarte", 1, 100),
    ("aide_neighbors", "Zusätzliche Deck-Nachbarn", "im Startaufbau eines Plans", 0, 200),
]

PAGES = (
    ("settings", "Einstellungen", "Alle Parameter des Combo Generators, dauerhaft in deepyugioh.toml"),
    ("search", "Einzelsuche", "Einen Startaufbau mit den aktuellen Einstellungen durchsuchen"),
    ("global", "Globaler Lauf", "Fortsetzbare Suche über alle generierten Startaufbauten"),
    ("combos", "Combos", "Database 1: gefundene und per Replay validierte Combos"),
    ("runs", "Läufe & Tuning", "Warum Suchen unvollständig waren und welche Einstellung hilft"),
)


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


def _display_path(path) -> str:
    """Relative to the project root if inside it (shorter, no home directory in screenshots)."""
    from deepyugioh import paths

    try:
        return str(path.resolve().relative_to(paths.ROOT.resolve()))
    except ValueError:
        return str(path)


def _duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ---- widget helpers ----------------------------------------------------------------
def card(parent, title: str | None = None, subtitle: str | None = None, padding=16) -> ttk.Frame:
    """Bordered surface with optional title. Put content into `.body`, extra header widgets into `.head`."""
    outer = ttk.Frame(parent, style="Card.TFrame", padding=padding)
    outer.head = ttk.Frame(outer)
    if title:
        outer.head.pack(fill="x", pady=(0, 12))
        ttk.Label(outer.head, text=title, style="CardTitle.TLabel").pack(side="left")
        if subtitle:
            ttk.Label(outer.head, text=subtitle, style="Muted.TLabel").pack(side="left", padx=10)
    outer.body = ttk.Frame(outer)
    outer.body.pack(fill="both", expand=True)
    return outer


class Tile:
    """Key figure: large value, small label."""

    def __init__(self, parent, label: str, value: str = "-"):
        self.frame = ttk.Frame(parent, style="Tile.TFrame", padding=(14, 10))
        self.value = ttk.Label(self.frame, text=value, style="TileValue.TLabel")
        self.value.pack(anchor="w")
        ttk.Label(self.frame, text=label, style="TileLabel.TLabel").pack(anchor="w")

    def set(self, value, tone: str | None = None):
        style = f"{tone.title()}.TileValue.TLabel" if tone else "TileValue.TLabel"
        self.value.configure(text=str(value), style=style)


def tile_row(parent, specs: list[tuple[str, str]]) -> tuple[ttk.Frame, dict[str, Tile]]:
    frame = ttk.Frame(parent)
    tiles = {}
    for col, (key, label) in enumerate(specs):
        tiles[key] = Tile(frame, label)
        tiles[key].frame.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
        frame.columnconfigure(col, weight=1, uniform="tiles")
    return frame, tiles


def make_tree(parent, theme: Theme, columns: list[tuple[str, str, int]], height: int = 12) -> tuple[ttk.Frame, ttk.Treeview]:
    """Treeview with scrollbar and row stripes; click on a heading sorts by that column."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height, selectmode="browse")
    for key, heading, width in columns:
        tree.heading(key, text=heading, anchor="w" if width >= 120 else "center", command=lambda k=key: _sort(tree, k))
        tree.column(key, width=width, stretch=width >= 200, anchor="w" if width >= 120 else "center")
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    theme.register(tree, "tree")
    return frame, tree


def restripe(tree: ttk.Treeview):
    for n, item in enumerate(tree.get_children("")):
        tags = [t for t in tree.item(item, "tags") if t != "odd"]
        tree.item(item, tags=tags + (["odd"] if n % 2 else []))


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
    restripe(tree)


def make_text(parent, theme: Theme, height: int = 10, width: int = 80, wrap: str = "none") -> tuple[ttk.Frame, tk.Text]:
    frame = ttk.Frame(parent)
    text = tk.Text(frame, height=height, width=width, wrap=wrap, font=theme.fonts["mono"], undo=False)
    theme.register(text, "text")
    sy = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=sy.set)
    text.grid(row=0, column=0, sticky="nsew")
    sy.grid(row=0, column=1, sticky="ns")
    if wrap == "none":
        sx = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(xscrollcommand=sx.set)
        sx.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, text


def set_text(widget: tk.Text, content: str, readonly: bool = True, highlight: bool = False):
    """Replace the content. highlight: headings (first line, lines ending in ':') and warnings get colours."""
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", content)
    if highlight:
        for n, line in enumerate(content.splitlines(), start=1):
            if line and not line[0].isspace() and (n == 1 or line.rstrip().endswith(":")):
                widget.tag_add("h", f"{n}.0", f"{n}.end")
            elif "UNVOLLSTÄNDIG" in line or line.lstrip().startswith("->"):
                widget.tag_add("warn", f"{n}.0", f"{n}.end")
    if readonly:
        widget.configure(state="disabled")


class NavItem:
    """Sidebar entry: accent bar + label, hover and active state."""

    def __init__(self, parent, theme: Theme, text: str, command: Callable[[], None]):
        self.theme, self.active, self.hover = theme, False, False
        self.row = tk.Frame(parent, cursor="hand2")
        self.bar = tk.Frame(self.row, width=4)
        self.bar.pack(side="left", fill="y")
        self.label = tk.Label(self.row, text=text, anchor="w", padx=18, pady=10, font=theme.fonts["nav"], cursor="hand2")
        self.label.pack(side="left", fill="x", expand=True)
        for w in (self.row, self.label):
            w.bind("<Button-1>", lambda _e: command())
            w.bind("<Enter>", lambda _e: self._hover(True))
            w.bind("<Leave>", lambda _e: self._hover(False))

    def _hover(self, on: bool):
        self.hover = on
        self.paint()

    def paint(self, _pal=None):
        p = self.theme.pal
        bg = p["sidebar_active"] if self.active else p["sidebar_hover"] if self.hover else p["sidebar"]
        self.row.configure(bg=bg)
        self.bar.configure(bg=p["accent"] if self.active else bg)
        self.label.configure(bg=bg, fg="#ffffff" if self.active else p["sidebar_fg"])


class Sidebar:
    def __init__(self, app: "App"):
        t = app.theme
        self.app = app
        self.frame = tk.Frame(app.root, width=230)
        self.frame.pack(side="left", fill="y")
        self.frame.pack_propagate(False)
        self.brand = tk.Label(self.frame, text="DeepYUGIOH", font=t.fonts["brand"], anchor="w", padx=22)
        self.brand.pack(fill="x", pady=(24, 0))
        self.sub = tk.Label(self.frame, text=app.SUBTITLE, font=t.fonts["base"], anchor="w", padx=22)
        self.sub.pack(fill="x")
        self.badge = tk.Label(self.frame, text="TESTMODUS", font=t.fonts["bold"], padx=8, pady=2)
        self.badge.pack(anchor="w", padx=22, pady=(12, 22))
        self.items: dict[str, NavItem] = {}
        for key, title, _ in app.PAGES:
            item = NavItem(self.frame, t, title, lambda k=key: app.show(k))
            item.row.pack(fill="x")
            self.items[key] = item

        self.path = tk.Label(self.frame, text=f"Einstellungen:\n{_display_path(settings.settings_path())}", font=t.fonts["small"],
                             justify="left", anchor="w", wraplength=190, padx=22)
        self.path.pack(side="bottom", fill="x", pady=(6, 18))
        self.theme_item = NavItem(self.frame, t, "", self.app.toggle_theme)
        self.theme_item.row.pack(side="bottom", fill="x")
        t.on_change(self.paint)
        self.paint()

    def select(self, key: str):
        for k, item in self.items.items():
            item.active = k == key
            item.paint()

    def paint(self, _pal=None):
        p = self.app.theme.pal
        for w in (self.frame, self.brand, self.sub, self.path):
            w.configure(bg=p["sidebar"])
        self.brand.configure(fg="#ffffff")
        self.sub.configure(fg=p["sidebar_fg"])
        self.path.configure(fg="#6b7385")
        self.badge.configure(bg=p["warning"], fg="#111111")
        self.theme_item.label.configure(text="Helles Design" if self.app.theme.name == "dark" else "Dunkles Design")
        for item in (*self.items.values(), self.theme_item):
            item.paint()


class Header:
    """Page title + global run controls, visible on every page."""

    def __init__(self, app: "App", parent):
        self.app = app
        t = app.theme
        frame = ttk.Frame(parent, style="Header.TFrame", padding=(24, 14))
        frame.pack(fill="x")
        ttk.Separator(parent).pack(fill="x")

        left = ttk.Frame(frame, style="Header.TFrame")
        left.pack(side="left", fill="y")
        self.title = ttk.Label(left, style="Title.TLabel")
        self.title.pack(anchor="w")
        self.subtitle = ttk.Label(left, style="Muted.TLabel")
        self.subtitle.pack(anchor="w", pady=(2, 0))

        self.stop_btn = ttk.Button(frame, text="Stoppen", style="Danger.TButton", command=app.stop_run)
        self.stop_btn.pack(side="right", padx=(8, 0))
        self.start_btn = ttk.Button(frame, text="Lauf starten", style="Accent.TButton", command=app.start_run)
        self.start_btn.pack(side="right", padx=(16, 0))

        run = ttk.Frame(frame, style="Header.TFrame")
        run.pack(side="right")
        top = ttk.Frame(run, style="Header.TFrame")
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=16, height=16)
        t.register(self.dot, "canvas")
        self.dot.create_oval(2, 2, 14, 14, outline="", tags="dot")
        self.dot.pack(side="left", padx=(0, 6))
        self.state = ttk.Label(top, text="Bereit", style="CardTitle.TLabel")
        self.state.pack(side="left")
        self.metrics = ttk.Label(top, text="", style="Muted.TLabel")
        self.metrics.pack(side="right", padx=(12, 0))
        self.bar = ttk.Progressbar(run, length=360, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(6, 4))
        self.summary = ttk.Label(run, text="", style="Muted.TLabel", cursor="hand2")
        self.summary.pack(anchor="w")
        self.summary.bind("<Button-1>", lambda _e: app.show("global"))
        t.on_change(lambda _p: self.refresh())

    def set_page(self, title: str, subtitle: str):
        self.title.configure(text=title)
        self.subtitle.configure(text=subtitle)

    def refresh(self):
        app, p = self.app, self.app.theme.pal
        g = getattr(app, "global_tab", None)
        if g is None:  # still building
            return
        if g.running:
            stopping = g.stop_event is not None and g.stop_event.is_set()
            state, color = ("Stoppt, laufende Suchen werden fertig", p["warning"]) if stopping else ("Globaler Lauf läuft", p["accent"])
        elif app.busy:
            state, color = f"{app.busy} läuft", p["accent"]
        elif g.last_report is not None:
            state, color = "Letzter Lauf beendet", p["success"]
        else:
            state, color = "Bereit", p["muted"]
        self.dot.itemconfigure("dot", fill=color)
        self.state.configure(text=state)
        self.metrics.configure(text=g.metrics_text())
        self.summary.configure(text=g.options_text() + "   (Optionen: Globaler Lauf)")
        self.start_btn.configure(state="disabled" if app.busy else "normal")
        stoppable = g.running and not (g.stop_event and g.stop_event.is_set())
        self.stop_btn.configure(state="normal" if stoppable else "disabled")


# ---- application ---------------------------------------------------------------
class App:
    """Shell of a test GUI: sidebar, header, pages, background tasks. The Combo Generator's pages are built
    here; the GUI of another stage subclasses it and overrides PAGES, SUBTITLE, _make_header, _build_pages,
    _unsaved_changes and _stop_work (see gui/analysis.py)."""

    PAGES = PAGES
    SUBTITLE = "Combo Generator"
    TITLE = "DeepYUGIOH - Testmodus"

    def __init__(self, root: tk.Tk):
        from deepyugioh.cards.store import CardStore
        from deepyugioh.repository.combo_db import ComboRepository

        self.root = root
        self.theme = Theme(root, settings.gui_theme())
        self.store = CardStore()          # main thread only
        self.repo = ComboRepository()     # main thread only (reading)
        self.queue: queue.Queue = queue.Queue()
        self.busy: str | None = None
        self.closing = False
        self.busy_listeners: list[Callable[[], None]] = []
        self.current = self.PAGES[0][0]

        try:
            self.saved = settings.generator_config()
        except ValueError as e:
            messagebox.showerror("Einstellungen", f"{settings.settings_path()} ist ungültig:\n{e}\n\nEs gelten die Standardwerte.")
            self.saved = GeneratorConfig()

        root.title(self.TITLE)
        root.geometry("1480x940")
        root.minsize(1150, 700)

        self.sidebar = Sidebar(self)
        main = ttk.Frame(root, style="Page.TFrame")
        main.pack(side="left", fill="both", expand=True)
        self.header = self._make_header(main)
        self.content = ttk.Frame(main, style="Page.TFrame")
        self.content.pack(fill="both", expand=True)
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        bar = ttk.Frame(main, style="Page.TFrame", padding=(24, 0, 24, 8))
        bar.pack(fill="x")
        self.status = tk.StringVar(value="bereit")
        ttk.Label(bar, textvariable=self.status, style="PageMuted.TLabel").pack(side="left")

        self.pages = self._build_pages()
        for page in self.pages.values():
            page.frame.grid(row=0, column=0, sticky="nsew")
        self.busy_listeners.append(self.header.refresh)
        self.show(self.PAGES[0][0])
        self.header.refresh()

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._poll)

    # ---- hooks for the GUIs of other stages ----------------------------------------------
    def _make_header(self, parent):
        return Header(self, parent)

    def _build_pages(self) -> dict:
        self.settings_tab = SettingsTab(self)
        self.search_tab = SearchTab(self)
        self.global_tab = GlobalTab(self)
        self.combos_tab = CombosTab(self)
        self.runs_tab = RunsTab(self)
        return {"settings": self.settings_tab, "search": self.search_tab, "global": self.global_tab,
                "combos": self.combos_tab, "runs": self.runs_tab}

    def _unsaved_changes(self) -> bool:
        return self.settings_tab.dirty

    def _stop_work(self):
        self.global_tab.stop()

    # ---- navigation ------------------------------------------------------------------
    def show(self, key: str):
        self.current = key
        page = self.pages[key]
        page.frame.tkraise()
        self.sidebar.select(key)
        _, title, subtitle = next(p for p in self.PAGES if p[0] == key)
        self.header.set_page(title, subtitle)
        if getattr(page, "loaded", True) is False:  # first visit; afterwards via the refresh buttons
            page.loaded = True
            page.refresh()

    def toggle_theme(self):
        name = self.theme.toggle()
        try:
            settings.set_gui_theme(name)
        except OSError as e:
            self.status.set(f"Design nicht gespeichert: {e}")

    # ---- global run (header buttons) -----------------------------------------------------
    def start_run(self):
        self.global_tab.start()

    def stop_run(self):
        self.global_tab.stop()

    # ---- configuration -------------------------------------------------------
    def config(self) -> GeneratorConfig:
        """The configuration in the settings form (also unsaved changes). Raises ValueError."""
        return self.settings_tab.read()

    def config_or_warn(self) -> GeneratorConfig | None:
        try:
            return self.config()
        except ValueError as e:
            messagebox.showerror("Ungültige Einstellungen", str(e))
            self.show("settings")
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
        self.status.set(f"{label} läuft ...")
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

    def close(self):
        if self._unsaved_changes() and not messagebox.askyesno(
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
            self._stop_work()
            self.status.set("wird beendet, sobald die laufenden Suchen fertig sind ...")
            return
        self.root.destroy()

    # ---- cross-page actions -----------------------------------------------------
    def open_in_search(self, setup: StartSetup):
        self.search_tab.load_setup(setup)
        self.show("search")


def _page(app: App) -> ttk.Frame:
    return ttk.Frame(app.content, style="Page.TFrame", padding=(24, 18, 24, 12))


# ---- page: settings -----------------------------------------------------------------
class SettingsTab:
    def __init__(self, app: App):
        self.app = app
        self.frame = _page(app)
        self.vars: dict[str, tk.Variable] = {}
        self.tier_vars: dict[int, tk.BooleanVar] = {}
        self.board_vars: dict[str, tk.BooleanVar] = {}
        self.boards: dict[str, list[dict]] = dict(app.saved.opponent_boards)
        self._kinds: dict[str, tuple] = {}   # number field -> (type, label, min, max)
        self.dirty = False
        self._loading = False

        bar = ttk.Frame(self.frame, style="Page.TFrame")
        bar.pack(fill="x", pady=(0, 14))
        ttk.Label(bar, style="PageMuted.TLabel", text=(
            "Einzelsuche und globaler Lauf verwenden immer die Werte in diesem Formular. "
            "Speichern macht sie dauerhaft, auch für deepyugioh generate, plan und search."
        )).pack(side="left")
        ttk.Button(bar, text="Standardwerte", command=self.defaults).pack(side="right")
        ttk.Button(bar, text="Verwerfen", command=self.reload).pack(side="right", padx=8)
        ttk.Button(bar, text="Speichern", style="Accent.TButton", command=self.save).pack(side="right")
        self.dirty_label = ttk.Label(bar, text="", style="Dirty.PageMuted.TLabel")
        self.dirty_label.pack(side="right", padx=14)

        cols = ttk.Frame(self.frame, style="Page.TFrame")
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=1, uniform="cols")
        cols.columnconfigure(1, weight=1, uniform="cols")
        left_col = ttk.Frame(cols, style="Page.TFrame")
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right_col = ttk.Frame(cols, style="Page.TFrame")
        right_col.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        limits = card(left_col, "Suchgrenzen", "pro Startaufbau")
        limits.pack(fill="x")
        body = limits.body
        row = 0
        for key, label, hint, lo, hi in LIMIT_NUMBERS:
            row = self._number(body, row, key, label, hint, lo, hi, float if key == "time_limit" else int)
        ttk.Separator(body).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        for key, label, hint in LIMIT_FLAGS:
            row = self._flag(body, row, key, label, hint)
        body.columnconfigure(1, weight=1)

        run = card(left_col, "Ausführung")
        run.pack(fill="x", pady=(16, 0))
        row = self._number(run.body, 0, "workers", "Parallele Prozesse",
                           f"Nur globaler Lauf; dieser Rechner hat {os.cpu_count()} Kerne", 1, max(64, os.cpu_count() or 1), int)
        self._flag(run.body, row, "validate", "Combos per Replay validieren", "Nur validierte Combos kommen in Database 1")
        run.body.columnconfigure(1, weight=1)

        cand = card(right_col, "Startaufbauten", "globale Suche")
        cand.pack(fill="x")
        body = cand.body
        ttk.Label(body, text="Stufen", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        tiers = ttk.Frame(body)
        tiers.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 10))
        for tier, desc in TIERS.items():
            v = self.tier_vars[tier] = tk.BooleanVar()
            v.trace_add("write", self._changed)
            ttk.Checkbutton(tiers, text=f"Stufe {tier}: {desc}", variable=v).pack(anchor="w")
        ttk.Label(body, text="Gegnerfelder", style="CardTitle.TLabel").grid(row=2, column=0, columnspan=2, sticky="w")
        self.boards_frame = ttk.Frame(body)
        self.boards_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 2))
        ttk.Label(body, style="Muted.TLabel", wraplength=560, justify="left", text=(
            "Gegnerfelder selbst werden in der Einstellungsdatei unter [opponent_boards.NAME] definiert "
            "(Format siehe deepyugioh.example.toml); hier wird nur ausgewählt, welche verwendet werden."
        )).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 10))
        row = 5
        for key, label, hint, lo, hi in CANDIDATE_NUMBERS:
            row = self._number(body, row, key, label, hint, lo, hi, int)
        self._flag(body, row, "enablers", "Hilfskarten ergänzen", "Z. B. Foolish Burial, wenn eine Karte im Friedhof wirkt")
        body.columnconfigure(1, weight=1)

        aide = card(right_col, "AIDE", "algorithmische Suche, Stufen 5 bis 7")
        aide.pack(fill="x", pady=(16, 0))
        halves = [ttk.Frame(aide.body), ttk.Frame(aide.body)]
        for i, half in enumerate(halves):
            half.grid(row=0, column=i, sticky="nsew", padx=(0, 12) if i == 0 else 0)
            aide.body.columnconfigure(i, weight=1, uniform="aide")
        rows = [0, 0]
        for i, (key, label, hint, lo, hi) in enumerate(AIDE_NUMBERS):
            rows[i % 2] = self._number(halves[i % 2], rows[i % 2], key, label, hint, lo, hi,
                                       float if key == "aide_share" else int)
        flag = ttk.Frame(aide.body)
        flag.grid(row=1, column=0, columnspan=2, sticky="ew")
        self._flag(flag, 0, "aide_helpers", "Hilfskarten ergänzen", "wie in den Stufen 1 bis 4 (laut Benchmark langsamer)")

        self.fill(app.saved)

    # ---- widgets -------------------------------------------------------------
    def _number(self, parent, row, key, label, hint, lo, hi, kind):
        v = self.vars[key] = tk.StringVar()
        v.trace_add("write", self._changed)
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        step = 1 if kind is int else (0.05 if hi <= 1 else 5.0)
        ttk.Spinbox(cell, textvariable=v, from_=lo, to=hi, increment=step, width=11).pack(side="left")
        text = ttk.Frame(cell)
        text.pack(side="left", padx=12, fill="x")
        ttk.Label(text, text=label).pack(anchor="w")
        ttk.Label(text, text=hint, style="Muted.TLabel").pack(anchor="w")
        self._kinds[key] = (kind, label, lo, hi)
        return row + 1

    def _flag(self, parent, row, key, label, hint):
        v = self.vars[key] = tk.BooleanVar()
        v.trace_add("write", self._changed)
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Checkbutton(cell, text=label, variable=v).pack(side="left")
        ttk.Label(cell, text=hint, style="Muted.TLabel").pack(side="left", padx=10)
        return row + 1

    def _changed(self, *_):
        if not self._loading:
            self.dirty = True
            self.dirty_label.configure(text="Ungespeicherte Änderungen")

    def _set_clean(self):
        self.dirty = False
        self.dirty_label.configure(text="")

    # ---- form <-> config -------------------------------------------------------
    def fill(self, cfg: GeneratorConfig):
        self._loading = True
        try:
            for f in fields(SearchLimits):
                self.vars[f.name].set(getattr(cfg.limits, f.name))
            for key in ("max_deck_neighbors", "max_extra", "enablers", "aide_plans_per_card", "aide_branching",
                        "aide_neighbors", "aide_helpers"):
                self.vars[key].set(getattr(cfg.candidates, key))
            self.vars["aide_share"].set(f"{cfg.candidates.aide_share:g}")
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
            aide_share=num["aide_share"],
            aide_plans_per_card=num["aide_plans_per_card"],
            aide_branching=num["aide_branching"],
            aide_neighbors=num["aide_neighbors"],
            aide_helpers=bool(self.vars["aide_helpers"].get()),
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
        self.app.status.set("Standardwerte eingetragen, noch nicht gespeichert")


# ---- shared: combo list with detail view ---------------------------------------------
class ComboView:
    """Combo table + detail text. `runs_for` adds the runs a stored combo was found in."""

    def __init__(self, app: App, parent, runs_for: Callable[[int], list[dict]] | None = None):
        self.app = app
        self.runs_for = runs_for
        self.combos: list[ComboRecord] = []
        self.frame = ttk.Panedwindow(parent, orient="horizontal", style="Card.TPanedwindow")
        left = ttk.Frame(self.frame)
        tree_frame, self.tree = make_tree(left, app.theme, [
            ("n", "#", 55), ("cards", "Karten", 60), ("actions", "Aktionen", 70), ("opp", "Gegner", 85),
            ("names", "Benötigte Karten", 330), ("benefit", "Nutzen", 210),
        ])
        tree_frame.pack(fill="both", expand=True, padx=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.frame.add(left, weight=3)

        right = ttk.Frame(self.frame)
        opts = ttk.Frame(right)
        opts.pack(fill="x", padx=(8, 0), pady=(0, 6))
        self.all_steps = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Alle Engine-Schritte", variable=self.all_steps, command=self._select).pack(side="left")
        self.open_btn = ttk.Button(opts, text="In Einzelsuche öffnen", command=self._open, state="disabled")
        self.open_btn.pack(side="right")
        text_frame, self.text = make_text(right, app.theme, height=20, width=64)
        text_frame.pack(fill="both", expand=True, padx=(8, 0))
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
        restripe(self.tree)
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
        set_text(self.text, fmt.combo_text(c, self.app.store.name, all_steps=self.all_steps.get(), runs=runs), highlight=True)
        self.open_btn.configure(state="normal")

    def _open(self):
        c = self.selected()
        if c is not None:
            self.app.open_in_search(c.setup)


# ---- page: single search ------------------------------------------------------------------
class SearchTab:
    def __init__(self, app: App):
        self.app = app
        self.frame = _page(app)
        top = ttk.Frame(self.frame, style="Page.TFrame")
        top.pack(fill="x")

        # card finder
        finder = card(top, "Karte suchen")
        finder.pack(side="left", fill="y")
        self.query = tk.StringVar()
        ttk.Entry(finder.body, textvariable=self.query, width=36).pack(fill="x")
        self.query.trace_add("write", lambda *_: self._find())
        self.results = tk.Listbox(finder.body, height=7, width=40, exportselection=False, font=app.theme.fonts["base"])
        app.theme.register(self.results, "listbox")
        self.results.pack(fill="both", expand=True, pady=8)
        self.results.bind("<Double-Button-1>", lambda _e: self._add("hand"))
        btns = ttk.Frame(finder.body)
        btns.pack(fill="x")
        for target, label in (("hand", "In Hand"), ("deck", "In Deck"), ("extra", "Ins Extra")):
            ttk.Button(btns, text=label, command=lambda t=target: self._add(t)).pack(side="left", padx=(0, 6))
        self.found: list = []

        # setup
        setup = card(top, "Startaufbau", "eine Karte pro Zeile, Name oder Passcode, 3x Name für Kopien")
        setup.pack(side="left", fill="both", expand=True, padx=(16, 0))
        body = setup.body
        self.lists: dict[str, tk.Text] = {}
        for col, (key, label) in enumerate((("hand", "Hand"), ("deck", "Deck"), ("extra", "Extra Deck"))):
            ttk.Label(body, text=label, style="Muted.TLabel").grid(row=0, column=col, sticky="w", padx=(0 if col == 0 else 10, 0))
            box = tk.Text(body, height=8, width=30, undo=True, font=app.theme.fonts["base"])
            app.theme.register(box, "text")
            box.grid(row=1, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0), pady=(4, 0))
            body.columnconfigure(col, weight=1, uniform="lists")
            self.lists[key] = box
        # later game situations: what player 0 already has when the turn starts
        for col, (key, label) in enumerate((("grave", "Schon im Friedhof"), ("banished", "Schon verbannt"),
                                            ("field", "Schon auf dem Feld"))):
            ttk.Label(body, text=label, style="Muted.TLabel").grid(row=2, column=col, sticky="w", pady=(8, 0),
                                                                   padx=(0 if col == 0 else 10, 0))
            box = tk.Text(body, height=2, width=30, undo=True, font=app.theme.fonts["base"])
            app.theme.register(box, "text")
            box.grid(row=3, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0), pady=(4, 0))
            self.lists[key] = box
        opts = ttk.Frame(body)
        opts.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Label(opts, text="Gegnerfeld").pack(side="left")
        self.opponent = tk.StringVar(value="empty")
        self.opponent_box = ttk.Combobox(opts, textvariable=self.opponent, state="readonly", width=14)
        self.opponent_box.pack(side="left", padx=8)
        self.branch_zones = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Zonen verzweigen (Link)", variable=self.branch_zones).pack(side="left", padx=10)
        self.store_results = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="In Database 1 speichern", variable=self.store_results).pack(side="left", padx=10)
        self.start_btn = ttk.Button(opts, text="Suche starten", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="right")
        ttk.Button(opts, text="Leeren", command=self.clear).pack(side="right", padx=8)

        result = card(self.frame, "Ergebnis")
        result.pack(fill="both", expand=True, pady=(16, 0))
        row, self.tiles = tile_row(result.body, [
            ("nodes", "Knoten"), ("replays", "Replays"), ("time", "Dauer"), ("status", "Status"),
            ("found", "Combos gefunden"), ("valid", "validiert"), ("new", "neu in Database 1"),
        ])
        row.pack(fill="x")
        self.summary = ttk.Label(result.body, text="Noch keine Suche.", style="Muted.TLabel", justify="left", wraplength=1150)
        self.summary.pack(fill="x", pady=10)
        self.view = ComboView(app, result.body)
        self.view.frame.pack(fill="both", expand=True)
        app.busy_listeners.append(self._busy)
        self.config_changed()

    def config_changed(self):
        boards = list(self.app.settings_tab.boards)
        self.opponent_box.configure(values=boards)
        if self.opponent.get() not in boards and boards:
            self.opponent.set(boards[0])

    def _busy(self):
        self.start_btn.configure(state="disabled" if self.app.busy else "normal")

    def _find(self):
        self.found = self.app.store.search(self.query.get(), limit=60) if len(self.query.get().strip()) >= 2 else []
        self.results.delete(0, "end")
        for card_ in self.found:
            self.results.insert("end", f"{card_.name}  [{card_.code}]")

    def _add(self, target: str):
        sel = self.results.curselection()
        if not sel:
            return
        card_ = self.found[sel[0]]
        box = self.lists[target]
        content = box.get("1.0", "end").rstrip("\n")
        box.delete("1.0", "end")
        box.insert("1.0", (content + "\n" if content else "") + card_.name)

    def clear(self):
        for box in self.lists.values():
            box.delete("1.0", "end")

    def load_setup(self, setup: StartSetup):
        name = self.app.store.name
        for key in ("hand", "deck", "extra", "grave", "banished"):
            set_text(self.lists[key], fmt.card_list_text(getattr(setup, key), name), readonly=False)
        set_text(self.lists["field"], fmt.card_list_text([c.code for c in setup.field], name), readonly=False)
        self._field = {c.code: c for c in setup.field}   # keeps zone and position of loaded field cards
        boards = self.opponent_box.cget("values")
        if setup.opponent.name in boards:
            self.opponent.set(setup.opponent.name)
        else:
            messagebox.showwarning("Gegnerfeld", f"Gegnerfeld '{setup.opponent.name}' ist in den Einstellungen nicht definiert.")
        self.branch_zones.set(setup.branch_zones)

    def _field_card(self, code: int):
        """A card typed into "Schon auf dem Feld": as loaded, else monsters face-up in a monster zone,
        spells and traps face-up in a spell/trap zone."""
        from deepyugioh.contracts.setups import FieldCard

        known = getattr(self, "_field", {}).get(code)
        if known is not None:
            return known
        card_ = self.app.store.get(code)
        return FieldCard(code, "mzone" if card_ is not None and card_.type & 0x1 else "szone")

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
            field = tuple(self._field_card(c) for c in self._codes("field"))
            grave, banished = self._codes("grave"), self._codes("banished")
            if not hand and not (field or grave or banished):
                raise ValueError("Die Hand ist leer.")
            boards = cfg.boards(self.app.store)
            setup = StartSetup(hand=hand, deck=self._codes("deck"), extra=self._codes("extra"),
                               opponent=boards[self.opponent.get()], branch_zones=self.branch_zones.get(),
                               grave=grave, banished=banished, field=field)
        except (ValueError, KeyError) as e:
            messagebox.showerror("Startaufbau", str(e).strip("'\""))
            return
        store = self.store_results.get()

        def work():
            from deepyugioh.cards.store import CardStore
            from deepyugioh.generator import ComboGenerator
            from deepyugioh.repository.combo_db import ComboRepository

            return ComboGenerator(CardStore(), ComboRepository() if store else None, cfg).search(setup)

        if not self.app.task("Einzelsuche", work, self._done):
            return
        for tile in self.tiles.values():
            tile.set("-")
        self.tiles["status"].set("läuft", "accent")
        self.summary.configure(text=f"Suche läuft: Zeitlimit {cfg.limits.time_limit:g}s, max. {cfg.limits.max_nodes} Knoten, "
                                    f"{cfg.limits.min_cards} bis {cfg.limits.max_cards} Karten")

    def _done(self, rep):
        run = rep.run
        t = self.tiles
        t["nodes"].set(run.nodes)
        t["replays"].set(run.replays)
        t["time"].set(f"{run.elapsed_s:.2f}s")
        if run.complete:
            t["status"].set("vollständig", "success")
        else:
            t["status"].set("unvollständig", "warning")
        t["found"].set(run.combos_found)
        t["valid"].set(len(rep.combos), "success" if rep.combos else None)
        t["new"].set(rep.new_combos if rep.run_id is not None else "-", "accent" if rep.new_combos else None)
        hints = [fmt.INCOMPLETE_HINTS[k] for k in run.incomplete if k in fmt.INCOMPLETE_HINTS]
        text = f"Abbruchgründe: {fmt.reasons(run.incomplete)}"
        if hints:
            text += f"  ({'; '.join(hints)})"
        text += f"     Beschnitten: {fmt.reasons(run.pruned)}"
        if rep.run_id is not None:
            text += f"     Gespeichert als Lauf #{rep.run_id}"
        self.summary.configure(text=text)
        self.view.set(rep.combos)


# ---- page: global run -----------------------------------------------------------------
class _Listener:
    def __init__(self, app: App, on_job):
        self.app, self.on_job_main = app, on_job

    def on_job(self, job):  # worker thread
        self.app.post(self.on_job_main, job)


class GlobalTab:
    MAX_LOG_LINES = 3000

    def __init__(self, app: App):
        self.app = app
        self.frame = _page(app)
        self.stop_event: threading.Event | None = None
        self.running = False
        self.last_report = None
        self.stats = self._empty_stats()
        self.t0 = 0.0
        self.run_max_jobs: int | None = None
        self.run_budget: float | None = None
        self.run_target: int | None = None
        self.run_benefit: str | None = None
        self._indeterminate = False

        opts = card(self.frame, "Laufoptionen", "ein Lauf setzt immer am gespeicherten Stand fort; "
                                                "die Optionen werden beim Start gespeichert")
        opts.pack(fill="x")
        row = ttk.Frame(opts.body)
        row.pack(fill="x")
        ttk.Label(row, text="Stufen").pack(side="left")
        self.tier_vars = {t: tk.BooleanVar() for t in TIERS}
        for t, v in self.tier_vars.items():
            ttk.Checkbutton(row, text=str(t), variable=v).pack(side="left", padx=(8, 0))

        saved = settings.run_options()
        ttk.Label(row, text="Lauf endet bei").pack(side="left", padx=(24, 6))
        self.mode = tk.StringVar(value=fmt.RUN_MODE_LABELS[saved["mode"]])
        ttk.Combobox(row, textvariable=self.mode, values=list(fmt.RUN_MODE_LABELS.values()), state="readonly",
                     width=24).pack(side="left")
        self.target = tk.StringVar(value=str(saved["target"]))
        self.benefit = tk.StringVar(value=fmt.TARGET_BENEFIT_LABELS.get(saved["benefit"], fmt.TARGET_BENEFIT_LABELS[""]))
        self.max_jobs = tk.StringVar(value=str(saved["max_jobs"]))
        self.time_budget = tk.StringVar(value=f"{saved['time_budget']:g}")
        end = ttk.Frame(row)
        end.pack(side="left", padx=(10, 0))
        self._end_widgets = {
            "target": [ttk.Entry(end, textvariable=self.target, width=7),
                       ttk.Label(end, text="neue Combos, zählen:"),
                       ttk.Combobox(end, textvariable=self.benefit, values=list(fmt.TARGET_BENEFIT_LABELS.values()),
                                    state="readonly", width=26)],
            "max_jobs": [ttk.Entry(end, textvariable=self.max_jobs, width=9), ttk.Label(end, text="Suchen")],
            "time_budget": [ttk.Entry(end, textvariable=self.time_budget, width=9), ttk.Label(end, text="Sekunden")],
            "unlimited": [],
        }
        self.end_hint = ttk.Label(opts.body, style="Muted.TLabel", justify="left")
        self.end_hint.pack(fill="x", pady=(10, 0))
        self.end_hint.bind("<Configure>", lambda e: self.end_hint.configure(wraplength=max(200, e.width)))
        for v in (*self.tier_vars.values(), self.target, self.benefit, self.max_jobs, self.time_budget):
            v.trace_add("write", lambda *_: self.app.header.refresh())
        self.mode.trace_add("write", lambda *_: self._show_end())
        self.stop_btn = ttk.Button(row, text="Stoppen", style="Danger.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.start_btn = ttk.Button(row, text="Lauf starten", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="right", padx=8)

        live = card(self.frame, "Aktueller Lauf")
        live.pack(fill="x", pady=(16, 0))
        tiles, self.tiles = tile_row(live.body, [
            ("jobs", "Suchen"), ("complete", "vollständig"), ("incomplete", "unvollständig"), ("error", "Fehler"),
            ("found", "Combos"), ("new", "neu in DB 1"), ("target", "Ziel"), ("rate", "Suchen/Min."),
            ("time", "Laufzeit"),
        ])
        tiles.pack(fill="x")
        self.bar = ttk.Progressbar(live.body, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(12, 0))
        self._show_stats()

        lower = ttk.Frame(self.frame, style="Page.TFrame")
        lower.pack(fill="both", expand=True, pady=(16, 0))
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(1, weight=1)
        cov = card(lower, "Abdeckung", "mit den Kandidaten-Einstellungen im Formular")
        cov.grid(row=0, column=0, sticky="ew")
        ttk.Button(cov.head, text="Aktualisieren", command=self.refresh_coverage).pack(side="right")
        tree_frame, self.coverage = make_tree(cov.body, app.theme, [
            ("tier", "Stufe", 55), ("total", "Kandidaten", 100), ("done", "erledigt", 85), ("pct", "%", 70),
            ("complete", "vollständig", 90), ("incomplete", "unvollst.", 85), ("error", "Fehler", 65),
            ("found", "Combos", 75), ("new", "neu", 65), ("desc", "Beschreibung", 400),
        ], height=4)
        tree_frame.pack(fill="both", expand=True)

        logc = card(lower, "Protokoll", "Suchen mit neuen Combos, Fehler und jede 25. Suche")
        logc.grid(row=1, column=0, sticky="nsew", pady=(16, 0))
        ttk.Button(logc.head, text="Leeren", command=lambda: set_text(self.log, "")).pack(side="right")
        text_frame, self.log = make_text(logc.body, app.theme, height=8)
        text_frame.pack(fill="both", expand=True)
        self.log.configure(state="disabled")
        app.busy_listeners.append(self._busy)
        self.config_changed()
        self._show_end()

    # ---- end condition -------------------------------------------------------------------
    END_HINTS = {
        "target": "Kein Zeitlimit für den Lauf: er sucht, bis so viele neue Combos in Database 1 stehen. Er endet "
                  "vorher nur, wenn alle Kandidaten der gewählten Stufen bearbeitet sind oder du stoppst. "
                  "Die Grenzen pro Suche (Zeitlimit, Knoten) gelten weiter.",
        "max_jobs": "Endet nach dieser Anzahl Suchen (Startaufbauten).",
        "time_budget": "Nach Ablauf starten keine neuen Suchen mehr, laufende werden fertig.",
        "unlimited": "Läuft, bis alle Kandidaten der gewählten Stufen bearbeitet sind oder du stoppst.",
    }

    def mode_key(self) -> str:
        return next(k for k, label in fmt.RUN_MODE_LABELS.items() if label == self.mode.get())

    def benefit_key(self) -> str:
        return next((k for k, label in fmt.TARGET_BENEFIT_LABELS.items() if label == self.benefit.get()), "")

    def _show_end(self):
        mode = self.mode_key()
        for widgets in self._end_widgets.values():
            for w in widgets:
                w.pack_forget()
        for i, w in enumerate(self._end_widgets[mode]):
            w.pack(side="left", padx=(0 if i == 0 else 6, 0))
        self.end_hint.configure(text=self.END_HINTS[mode])
        self.app.header.refresh()

    def end_condition(self) -> dict:
        """The end condition in the form as run() arguments. Raises ValueError."""
        mode = self.mode_key()
        out = {"max_jobs": None, "time_budget": None, "target_combos": None, "target_benefit": None}
        if mode == "target":
            out["target_combos"] = _parse_number(self.target.get(), int, "Ziel-Combos", 1, 10**9)
            out["target_benefit"] = self.benefit_key() or None
        elif mode == "max_jobs":
            out["max_jobs"] = _parse_number(self.max_jobs.get(), int, "Anzahl Suchen", 1, 10**12)
        elif mode == "time_budget":
            out["time_budget"] = _parse_number(self.time_budget.get(), float, "Zeitbudget", 1, 10**9)
        return out

    def _save_end(self, end: dict):
        mode = self.mode_key()
        values = {"mode": mode}
        if mode == "target":
            values.update(target=end["target_combos"], benefit=end["target_benefit"] or "")
        elif mode == "max_jobs":
            values["max_jobs"] = end["max_jobs"]
        elif mode == "time_budget":
            values["time_budget"] = end["time_budget"]
        try:
            settings.set_run_options(values)
        except (OSError, ValueError) as e:  # never block a run because the file is not writable
            self._append(f"Laufoptionen nicht gespeichert: {e}", "warn")

    # ---- state shown in the header -------------------------------------------------------
    @staticmethod
    def _empty_stats() -> dict:
        return {"jobs": 0, "complete": 0, "incomplete": 0, "error": 0, "found": 0, "new": 0, "hits": 0}

    def options_text(self) -> str:
        tiers = [str(t) for t, v in self.tier_vars.items() if v.get()]
        mode = self.mode_key()
        if mode == "target":
            benefit = self.benefit_key()
            end = f"Ziel {self.target.get().strip() or '?'} neue Combos" + (f" ({fmt.TARGET_BENEFIT_LABELS[benefit]})" if benefit else "")
        elif mode == "max_jobs":
            end = f"max. {self.max_jobs.get().strip() or '?'} Suchen"
        elif mode == "time_budget":
            end = f"Zeitbudget {self.time_budget.get().strip() or '?'}s"
        else:
            end = "bis alle Kandidaten bearbeitet sind"
        return f"Stufen {', '.join(tiers) or '-'}  |  {end}"

    def metrics_text(self) -> str:
        if not self.running and self.last_report is None:
            return ""
        s = self.stats
        elapsed = (time.perf_counter() - self.t0) if self.running else self.last_report.elapsed_s
        goal = f"Ziel {s['hits']}/{self.run_target}  |  " if self.run_target else ""
        return f"{goal}{s['jobs']} Suchen  |  {s['new']} neue Combos  |  {_duration(elapsed)}"

    def config_changed(self):
        for t, v in self.tier_vars.items():
            v.set(self.app.settings_tab.tier_vars[t].get())

    def _busy(self):
        if not self.app.busy:
            self.running = False
            self.stop_event = None  # also after a failed run
            self._update_progress()
        stoppable = self.running and not (self.stop_event and self.stop_event.is_set())
        self.start_btn.configure(state="disabled" if self.app.busy else "normal")
        self.stop_btn.configure(state="normal" if stoppable else "disabled")
        self.app.header.refresh()

    def _append(self, line: str, tag: str | None = None):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", (tag,) if tag else ())
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > self.MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - self.MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_stats(self):
        s, t = self.stats, self.tiles
        elapsed = (time.perf_counter() - self.t0) if self.running else (self.last_report.elapsed_s if self.last_report else 0)
        t["jobs"].set(s["jobs"])
        t["complete"].set(s["complete"], "success" if s["complete"] else None)
        t["incomplete"].set(s["incomplete"], "warning" if s["incomplete"] else None)
        t["error"].set(s["error"], "danger" if s["error"] else None)
        t["found"].set(s["found"])
        t["new"].set(s["new"], "accent" if s["new"] else None)
        if self.run_target:
            t["target"].set(f"{s['hits']}/{self.run_target}",
                            "success" if s["hits"] >= self.run_target else "accent" if s["hits"] else None)
        else:
            t["target"].set("-")
        t["rate"].set(f"{s['jobs'] / elapsed * 60:.1f}" if elapsed > 1 else "-")
        t["time"].set(_duration(elapsed))

    def _update_progress(self):
        bars = (self.bar, self.app.header.bar)
        if self.running:
            frac = None
            if self.run_target:
                frac = self.stats["hits"] / self.run_target
            elif self.run_max_jobs:
                frac = self.stats["jobs"] / self.run_max_jobs
            elif self.run_budget:
                frac = (time.perf_counter() - self.t0) / self.run_budget
            if frac is None:
                if not self._indeterminate:
                    for b in bars:
                        b.configure(mode="indeterminate")
                        b.start(15)
                    self._indeterminate = True
                return
            for b in bars:
                b.configure(mode="determinate", value=min(100.0, 100 * frac))
        else:
            for b in bars:
                b.stop()
                b.configure(mode="determinate", value=100 if self.last_report is not None else 0)
            self._indeterminate = False

    def _tick(self):
        if not self.running:
            return
        self._show_stats()
        self._update_progress()
        self.app.header.refresh()
        self.app.root.after(1000, self._tick)

    # ---- generator access ----------------------------------------------------------------
    @staticmethod
    def _generator(cfg: GeneratorConfig):
        """Build in the calling (background) thread - SQLite connections are thread bound."""
        from deepyugioh.cards.graph import CardGraph
        from deepyugioh.cards.store import CardStore
        from deepyugioh.generator import ComboGenerator
        from deepyugioh.repository.combo_db import ComboRepository

        graph = CardGraph()
        if graph.version() == "unbuilt":
            raise FileNotFoundError("Interaktionsgraph fehlt, zuerst `deepyugioh import-cards` ausführen")
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
        restripe(self.coverage)

    # ---- run control ---------------------------------------------------------------------
    def start(self):
        cfg = self.app.config_or_warn()
        if cfg is None:
            return
        tiers = [t for t, v in self.tier_vars.items() if v.get()]
        try:
            if not tiers:
                raise ValueError("Mindestens eine Stufe auswählen (Seite Globaler Lauf).")
            end = self.end_condition()
        except ValueError as e:
            messagebox.showerror("Globaler Lauf", str(e))
            self.app.show("global")
            return
        stop = threading.Event()
        lim = cfg.limits

        def work():
            gen = self._generator(cfg)
            return gen.run(tiers=tiers, progress=_Listener(self.app, self._job), should_stop=stop.is_set, **end)

        # state first: task() notifies the busy listeners, which read it
        prev = (self.stop_event, self.running, self.stats, self.last_report, self.run_target)
        self.stop_event, self.running = stop, True
        self.stats, self.last_report = self._empty_stats(), None
        self.t0, self.run_max_jobs, self.run_budget = time.perf_counter(), end["max_jobs"], end["time_budget"]
        self.run_target, self.run_benefit = end["target_combos"], end["target_benefit"]
        if not self.app.task("Globaler Lauf", work, self._done):
            self.stop_event, self.running, self.stats, self.last_report, self.run_target = prev
            return
        self._save_end(end)
        self._append(f"Start {time.strftime('%H:%M:%S')}: Stufen {tiers}, Ende bei {self._end_text(end)}, "
                     f"{cfg.workers} Prozess(e); "
                     f"Karten {lim.min_cards}-{lim.max_cards}, Knoten {lim.max_nodes}, Zeitlimit {lim.time_limit:g}s, "
                     f"Gegner {', '.join(cfg.candidates.opponent_boards)}", "h")
        self._busy()
        self._tick()

    @staticmethod
    def _end_text(end: dict) -> str:
        if end["target_combos"]:
            return fmt.target_text(end["target_combos"], end["target_benefit"] or "")
        if end["max_jobs"]:
            return f"{end['max_jobs']} Suchen"
        if end["time_budget"]:
            return f"Zeitbudget {end['time_budget']:g}s"
        return "allen Kandidaten"

    def stop(self):
        if self.stop_event is not None and not self.stop_event.is_set():
            self.stop_event.set()
            self._append("Stopp angefordert: keine neuen Suchen, laufende werden abgeschlossen und gespeichert", "warn")
            self._busy()

    def _job(self, job):
        s = self.stats
        s["jobs"] += 1
        s[job.status] += 1
        s["found"] += job.combos_found
        s["new"] += job.combos_new
        s["hits"] += job.target_hits
        self._show_stats()
        self._update_progress()
        self.app.header.refresh()
        if job.status == "error" or job.combos_new or s["jobs"] % 25 == 0:
            extra = f"  FEHLER {job.error}" if job.error else ""
            tag = "err" if job.status == "error" else "ok" if job.combos_new else None
            goal = f"  Ziel {s['hits']}/{self.run_target}" if self.run_target else ""
            self._append(f"[Stufe {job.tier} #{job.position}] {job.status:<10} {job.elapsed_s:6.1f}s  "
                         f"Combos {job.combos_found} (neu {job.combos_new}){goal}  Lauf #{job.run_id}{extra}", tag)

    def _done(self, report):
        self.last_report = report
        self.running = False
        reason = fmt.STOP_REASONS.get(report.stop_reason, report.stop_reason)
        self._append(f"Ende ({reason}): {report.jobs} Suchen in {_duration(report.elapsed_s)} - "
                     f"{report.complete} vollständig, {report.incomplete} unvollständig, {report.errors} Fehler, "
                     f"{report.skipped} übersprungen; {report.combos_found} Combos, {report.combos_new} neu in Database 1",
                     "h")
        if self.run_target:
            goal = fmt.target_text(self.run_target, self.run_benefit or "")
            if report.target_reached:
                self._append(f"Ziel erreicht: {report.target_hits} von {goal}", "ok")
            else:
                hint = (" Weitere Stufen auswählen oder die Grenzen pro Suche erhöhen (wirkt nur auf noch nicht "
                        "bearbeitete Kandidaten)." if report.stop_reason == "exhausted" else "")
                self._append(f"Ziel nicht erreicht: {report.target_hits} von {goal}.{hint}", "warn")
        self._show_stats()
        self._update_progress()
        self.refresh_coverage()
        self.app.combos_tab.loaded = self.app.runs_tab.loaded = False  # reload on next visit


# ---- page: combos (Database 1) ---------------------------------------------------------
class CombosTab:
    def __init__(self, app: App):
        self.app = app
        self.loaded = False
        self.frame = _page(app)
        overview = card(self.frame, padding=12)
        overview.pack(fill="x")
        tiles, self.tiles = tile_row(overview.body, [
            ("combos", "Combos in Database 1"), ("runs", "Läufe"), ("complete", "Läufe vollständig"),
            ("elapsed", "Dauer pro Lauf"), ("nodes", "Knoten pro Lauf"),
        ])
        tiles.pack(fill="x")
        self.breakdown = ttk.Label(overview.body, text="", style="Muted.TLabel", justify="left")
        self.breakdown.pack(fill="x", pady=(10, 0))

        filt = card(self.frame, padding=12)
        filt.pack(fill="x", pady=(16, 0))
        row = filt.body
        ttk.Label(row, text="Karte").pack(side="left")
        self.card = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.card, width=30)
        e.pack(side="left", padx=8)
        e.bind("<Return>", lambda _e: self.refresh())
        ttk.Label(row, text="Nutzen").pack(side="left", padx=(12, 6))
        self.benefit = tk.StringVar(value="")
        ttk.Combobox(row, textvariable=self.benefit, values=["", *BENEFIT_CRITERIA, "wins"], state="readonly",
                     width=16).pack(side="left")
        ttk.Label(row, text="Max. Karten").pack(side="left", padx=(16, 6))
        self.max_cards = tk.StringVar(value="")
        ttk.Spinbox(row, textvariable=self.max_cards, from_=1, to=60, width=5).pack(side="left")
        ttk.Label(row, text="Anzahl").pack(side="left", padx=(16, 6))
        self.limit = tk.StringVar(value="500")
        ttk.Spinbox(row, textvariable=self.limit, from_=1, to=1_000_000, increment=100, width=8).pack(side="left")
        ttk.Button(row, text="Anzeigen", style="Accent.TButton", command=self.refresh).pack(side="left", padx=16)
        ttk.Button(row, text="Als JSON exportieren", command=self.export).pack(side="right")

        lst = card(self.frame, padding=12)
        lst.pack(fill="both", expand=True, pady=(16, 0))
        self.view = ComboView(app, lst.body, runs_for=app.repo.runs_for)
        self.view.frame.pack(fill="both", expand=True)

    def refresh(self):
        try:
            card_ = None
            if self.card.get().strip():
                card_ = self.app.store.resolve(self.card.get().strip()).code
            max_cards = _optional(self.max_cards.get(), int, "Max. Karten")
            limit = _optional(self.limit.get(), int, "Anzahl")
        except (KeyError, ValueError) as e:
            messagebox.showerror("Filter", str(e).strip("'\""))
            return
        repo = self.app.repo
        stats = repo.stats()
        runs = stats["runs"]
        t = self.tiles
        t["combos"].set(stats["combos"], "accent" if stats["combos"] else None)
        t["runs"].set(runs)
        share = stats["runs_complete"] / runs if runs else None
        t["complete"].set(f"{100 * share:.0f} %" if share is not None else "-",
                          None if share is None else "success" if share >= 0.9 else "warning")
        t["elapsed"].set(f"{stats['avg_elapsed_s']:.1f}s" if runs else "-")
        t["nodes"].set(f"{stats['avg_nodes']:.0f}" if runs else "-")
        self.breakdown.configure(text=(
            "Nach Kartenanzahl: " + (", ".join(f"{k} Karten: {v}" for k, v in stats["by_card_count"].items()) or "-")
            + "      Nach Nutzen: " + (", ".join(f"{k}: {v}" for k, v in stats["by_benefit"].items()) or "-")
        ))
        combos = list(repo.iter_combos(card=card_, max_cards=max_cards, positive=self.benefit.get() or None, limit=limit))
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


# ---- page: runs & tuning -------------------------------------------------------------
class RunsTab:
    def __init__(self, app: App):
        self.app = app
        self.loaded = False
        self.frame = _page(app)
        self.runs: list[dict] = []

        top = ttk.Frame(self.frame, style="Page.TFrame")
        top.pack(fill="x")
        top.columnconfigure(0, weight=2, uniform="top")
        top.columnconfigure(1, weight=3, uniform="top")
        summary = card(top, "Überblick")
        summary.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        grid = ttk.Frame(summary.body)
        grid.pack(fill="both", expand=True)
        self.tiles = {}
        for n, (key, label) in enumerate([("runs", "Läufe"), ("incomplete", "unvollständig"),
                                          ("complete", "vollständig"), ("elapsed", "Dauer pro Lauf")]):
            self.tiles[key] = Tile(grid, label)
            self.tiles[key].frame.grid(row=n // 2, column=n % 2, sticky="nsew", padx=(0 if n % 2 == 0 else 8, 0),
                                       pady=(0 if n < 2 else 8, 0))
            grid.columnconfigure(n % 2, weight=1, uniform="ov")

        tune = card(top, "Tuning", "warum Suchen vorzeitig endeten, über alle gespeicherten Läufe")
        tune.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tree_frame, self.reasons = make_tree(tune.body, app.theme, [
            ("kind", "Art", 95), ("reason", "Grund", 230), ("runs", "Läufe", 70), ("total", "Summe", 80),
            ("hint", "Einstellung", 260),
        ], height=5)
        tree_frame.pack(fill="both", expand=True)

        lst = card(self.frame, padding=12)
        lst.pack(fill="both", expand=True, pady=(16, 0))
        filt = ttk.Frame(lst.body)
        filt.pack(fill="x", pady=(0, 10))
        self.incomplete_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="Nur unvollständige", variable=self.incomplete_only, command=self.refresh).pack(side="left")
        ttk.Label(filt, text="Stufe").pack(side="left", padx=(16, 6))
        self.tier = tk.StringVar(value="alle")
        box = ttk.Combobox(filt, textvariable=self.tier, values=["alle", *TIERS], state="readonly", width=6)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Label(filt, text="Anzahl").pack(side="left", padx=(16, 6))
        self.limit = tk.StringVar(value="500")
        ttk.Spinbox(filt, textvariable=self.limit, from_=1, to=1_000_000, increment=100, width=8).pack(side="left")
        ttk.Button(filt, text="Aktualisieren", style="Accent.TButton", command=self.refresh).pack(side="left", padx=16)
        self.open_btn = ttk.Button(filt, text="In Einzelsuche öffnen", command=self._open, state="disabled")
        self.open_btn.pack(side="right")

        panes = ttk.Panedwindow(lst.body, orient="horizontal", style="Card.TPanedwindow")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        tree_frame, self.tree = make_tree(left, app.theme, [
            ("id", "Lauf", 60), ("time", "Zeit", 140), ("tier", "Stufe", 55), ("opp", "Gegner", 85),
            ("nodes", "Knoten", 80), ("elapsed", "Dauer s", 75), ("status", "Status", 100),
            ("found", "Combos", 70), ("new", "neu", 55), ("reasons", "Abbruchgründe", 230),
        ])
        tree_frame.pack(fill="both", expand=True, padx=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", self._select)
        panes.add(left, weight=3)
        right = ttk.Frame(panes)
        detail_frame, self.detail = make_text(right, app.theme, height=16, width=58)
        detail_frame.pack(fill="both", expand=True, padx=(8, 0))
        panes.add(right, weight=2)

    def refresh(self):
        try:
            limit = _optional(self.limit.get(), int, "Anzahl") or 500
        except ValueError as e:
            messagebox.showerror("Läufe", str(e))
            return
        tier = None if self.tier.get() in ("", "alle") else int(self.tier.get())
        repo = self.app.repo
        stats = repo.stats()
        runs = stats["runs"]
        incomplete = runs - stats["runs_complete"]
        self.tiles["runs"].set(runs)
        self.tiles["incomplete"].set(incomplete, "warning" if incomplete else None)
        self.tiles["complete"].set(stats["runs_complete"], "success" if stats["runs_complete"] else None)
        self.tiles["elapsed"].set(f"{stats['avg_elapsed_s']:.1f}s" if runs else "-")

        self.reasons.delete(*self.reasons.get_children())
        for reason, (n, total) in stats["incomplete"].items():
            self.reasons.insert("", "end", values=("Abbruch", reason, n, total, fmt.INCOMPLETE_HINTS.get(reason, "")),
                                tags=("warn",))
        for rule, (n, total) in stats["pruned"].items():
            self.reasons.insert("", "end", values=("Beschnitten", rule, n, total, ""))
        if not self.reasons.get_children():
            self.reasons.insert("", "end", values=("", "keine Abbrüche" if runs else "noch keine Läufe", "", "", ""))
        restripe(self.reasons)

        self.runs = repo.recent_runs(limit, incomplete_only=self.incomplete_only.get(), tier=tier)
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.runs):
            self.tree.insert("", "end", iid=str(i), tags=() if r["complete"] else ("warn",), values=(
                r["id"], r["finished_at"].replace("T", " ").split("+")[0], r["tier"] if r["tier"] is not None else "-",
                r["opponent_board"], r["nodes"], f"{r['elapsed_s']:.1f}",
                "vollständig" if r["complete"] else "unvollständig", r["combos_found"], r["combos_new"],
                fmt.reasons(r["incomplete"]),
            ))
        restripe(self.tree)
        set_text(self.detail, "")
        self.open_btn.configure(state="disabled")

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self.runs[int(sel[0])] if sel else None

    def _select(self, _event=None):
        r = self._selected()
        if r is not None:
            set_text(self.detail, fmt.run_text(r, self.app.store.name), highlight=True)
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
