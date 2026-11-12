"""Test GUI of Stufe 2 (Analyzer System) - same shell, design and rules as the Combo Generator's GUI.

Started with `deepyugioh gui --stage 2`, only in test mode. Pages:

    Analyse       run the analysis and the handtrap search, numbers of Database 2, log
    Bewertungen   the evaluation profile of every combo: axes, handtraps, partners and conflicts
    Gewichte      weights of the overall values and of the handtraps; recomputed without a new analysis
    Filter        filter profiles: create, apply, inspect and export ranked combo sets
    Deckprofil    how well the combos fit a deck (archetypes, types, attributes)

The pages only use the public interfaces of the analysis module. Engine work
and everything that takes longer runs in a background thread with its own
database connections (App.task).
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from deepyugioh import settings
from deepyugioh.gui import format as fmt
from deepyugioh.gui.app import App, _optional, _page, card, make_text, make_tree, restripe, set_text, tile_row

PAGES = (
    ("overview", "Analyse", "Stufe 2: die Combos aus Database 1 bewerten, Ergebnisse in Database 2"),
    ("profiles", "Bewertungen", "Bewertungsprofil jeder Combo: Achsen, Handtraps, Partner und Konflikte"),
    ("weights", "Gewichte", "Gewichte der Gesamtwerte und der Handtraps, Neuberechnung ohne neue Analyse"),
    ("filters", "Filter", "Filterprofile: gespeicherte, geordnete Combo-Mengen für den Deckbauer"),
    ("fit", "Deckprofil", "Passung der Combos zu einem Deck aus Archetypen, Typen und Attributen"),
)
SCORE_AXES = ("strength", "opening", "breaking", "late", "outcome", "cost", "consistency", "robustness", "genericness",
              "compatibility")
LOCKS = {"beliebig": "any", "keine sichere Sperre": "none", "nur zum Deck passende": "fitting"}
SITUATIONS = {"alle": None, "nur aus der Starthand": False, "nur späteres Spiel": True}


class AnalysisHeader:
    """Page title and the state of the analysis work, visible on every page."""

    def __init__(self, app: "AnalysisApp", parent):
        self.app = app
        frame = ttk.Frame(parent, style="Header.TFrame", padding=(24, 14))
        frame.pack(fill="x")
        ttk.Separator(parent).pack(fill="x")
        left = ttk.Frame(frame, style="Header.TFrame")
        left.pack(side="left", fill="y")
        self.title = ttk.Label(left, style="Title.TLabel")
        self.title.pack(anchor="w")
        self.subtitle = ttk.Label(left, style="Muted.TLabel")
        self.subtitle.pack(anchor="w", pady=(2, 0))
        self.stop_btn = ttk.Button(frame, text="Stoppen", style="Danger.TButton", command=app.stop_work)
        self.stop_btn.pack(side="right", padx=(8, 0))
        self.start_btn = ttk.Button(frame, text="Analyse starten", style="Accent.TButton", command=app.start_analysis)
        self.start_btn.pack(side="right", padx=(16, 0))
        run = ttk.Frame(frame, style="Header.TFrame")
        run.pack(side="right")
        self.dot = tk.Canvas(run, width=16, height=16)
        app.theme.register(self.dot, "canvas")
        self.dot.create_oval(2, 2, 14, 14, outline="", tags="dot")
        self.dot.pack(side="left", padx=(0, 6))
        self.state = ttk.Label(run, text="Bereit", style="CardTitle.TLabel")
        self.state.pack(side="left")
        self.detail = ttk.Label(run, text="", style="Muted.TLabel")
        self.detail.pack(side="left", padx=(12, 0))
        app.theme.on_change(lambda _p: self.refresh())

    def set_page(self, title: str, subtitle: str):
        self.title.configure(text=title)
        self.subtitle.configure(text=subtitle)

    def refresh(self):
        app, p = self.app, self.app.theme.pal
        stopping = app.stop_event.is_set()
        if app.busy:
            state, color = (f"{app.busy} stoppt, laufende Suchen werden fertig", p["warning"]) if stopping else (f"{app.busy} läuft", p["accent"])
        else:
            state, color = "Bereit", p["muted"]
        self.dot.itemconfigure("dot", fill=color)
        self.state.configure(text=state)
        self.detail.configure(text=app.progress_text if app.busy else "")
        self.start_btn.configure(state="disabled" if app.busy else "normal")
        self.stop_btn.configure(state="normal" if app.busy == "Robustheit" and not stopping else "disabled")


class AnalysisApp(App):
    PAGES = PAGES
    SUBTITLE = "Analyzer System"
    TITLE = "DeepYUGIOH - Testmodus - Stufe 2"

    def __init__(self, root: tk.Tk):
        from deepyugioh.analysis import AnalysisRepository

        self.analysis = AnalysisRepository()   # main thread only
        self.stop_event = threading.Event()
        self.progress_text = ""
        super().__init__(root)

    # ---- shell hooks ---------------------------------------------------------------------------
    def _make_header(self, parent):
        return AnalysisHeader(self, parent)

    def _build_pages(self) -> dict:
        self.overview = OverviewPage(self)
        self.profiles = ProfilesPage(self)
        self.weights = WeightsPage(self)
        self.filters = FiltersPage(self)
        self.fit = FitPage(self)
        return {"overview": self.overview, "profiles": self.profiles, "weights": self.weights, "filters": self.filters,
                "fit": self.fit}

    def _unsaved_changes(self) -> bool:
        return False

    def _stop_work(self):
        self.stop_work()

    # ---- work ------------------------------------------------------------------------------------
    def log(self, line: str):      # any thread
        self.post(self.overview.append, line)

    def set_progress(self, text: str):   # any thread
        def apply():
            self.progress_text = text
            self.header.refresh()

        self.post(apply)

    def stop_work(self):
        self.stop_event.set()
        self.header.refresh()

    def start_analysis(self, full: bool = False):
        from deepyugioh.analysis.pipeline import run_analysis

        self.stop_event.clear()
        self.show("overview")
        self.task("Analyse", lambda: run_analysis(
            full=full, log=self.log, progress=lambda what, i, n: self.set_progress(f"{what} {i}/{n}")), self._work_done)

    def start_robustness(self, max_combos: int | None, time_budget: float | None, retry: bool):
        from deepyugioh.analysis.pipeline import run_robustness_search

        self.stop_event.clear()

        def progress(s):
            self.set_progress(f"{s['combos']}/{s['pending']} Combos, {s['searches']} Suchen, {s['elapsed']:.0f}s")

        self.task("Robustheit", lambda: run_robustness_search(
            max_combos=max_combos, time_budget=time_budget, retry_incomplete=retry, should_stop=self.stop_event.is_set,
            log=self.log, progress=progress), self._work_done)

    def recompute(self, label: str = "Neuberechnung"):
        from deepyugioh.analysis.pipeline import rescore

        self.task(label, lambda: rescore(log=self.log), self._work_done)

    def _work_done(self, _result):
        self.progress_text = ""
        self.stop_event.clear()
        for page in self.pages.values():   # every page shows Database 2: reload on the next visit
            if hasattr(page, "loaded"):
                page.loaded = False
        page = self.pages[self.current]
        if hasattr(page, "loaded"):
            page.loaded = True
            page.refresh()

    # ---- shared -------------------------------------------------------------------------------------
    def short(self, signature: str) -> str:
        combo = self.repo.by_signature(signature)
        return f"#{combo.id} {fmt.names(combo.cards, self.store.name)}" if combo else signature[:12]

    def detail(self, signature: str, fit: float | None = None, deck: str = "") -> str:
        rs = settings.robustness_settings()
        rows = [("ungestört" if h == "-" else rs.catalog.get(h, {}).get("card", h), status, value, data)
                for _, h, status, value, data in self.analysis.robustness_rows(signature, rs.search_version())]
        return fmt.profile_text(self.repo.by_signature(signature), self.analysis.profile(signature), self.store.name,
                                edges=self.analysis.edges(signature, limit=25), robustness=rows, short=self.short,
                                fit=fit, deck=deck)


class ComboTable:
    """Table of evaluated combos + the evaluation profile of the selected one."""

    def __init__(self, app: AnalysisApp, parent, columns: list[tuple[str, str, int]], height: int = 12):
        self.app = app
        self.rows: list[tuple[str, float | None, str]] = []   # (signature, fit, deck name)
        self.frame = ttk.Panedwindow(parent, orient="horizontal", style="Card.TPanedwindow")
        left = ttk.Frame(self.frame)
        tree_frame, self.tree = make_tree(left, app.theme, columns, height=height)
        tree_frame.pack(fill="both", expand=True, padx=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.frame.add(left, weight=3)
        right = ttk.Frame(self.frame)
        text_frame, self.text = make_text(right, app.theme, height=height + 6, width=70)
        text_frame.pack(fill="both", expand=True, padx=(8, 0))
        self.frame.add(right, weight=3)

    def set(self, rows: list[tuple[str, tuple, float | None, str]]):
        """rows: (signature, table values, fit, deck name)."""
        self.rows = [(sig, fit, deck) for sig, _, fit, deck in rows]
        self.tree.delete(*self.tree.get_children())
        for i, (_, values, _, _) in enumerate(rows):
            self.tree.insert("", "end", iid=str(i), values=values)
        restripe(self.tree)
        set_text(self.text, "" if rows else "Keine Combos.")
        if rows:
            self.tree.selection_set("0")

    def _select(self, *_):
        sel = self.tree.selection()
        if sel:
            sig, fit, deck = self.rows[int(sel[0])]
            set_text(self.text, self.app.detail(sig, fit, deck), highlight=True)


# ---- page: analysis -----------------------------------------------------------------------------
class OverviewPage:
    MAX_LOG_LINES = 2000

    def __init__(self, app: AnalysisApp):
        self.app = app
        self.loaded = False
        self.frame = _page(app)
        top = card(self.frame, padding=12)
        top.pack(fill="x")
        row, self.tiles = tile_row(top.body, [
            ("combos", "Combos in Database 1"), ("analysed", "bewertet"), ("families", "Combo-Familien"),
            ("edges", "Kanten"), ("robust", "Robustheit vollständig"), ("filters", "Filterprofile"),
        ])
        row.pack(fill="x")
        self.note = ttk.Label(top.body, text="", style="Muted.TLabel", justify="left", wraplength=1150)
        self.note.pack(fill="x", pady=(10, 0))

        mid = ttk.Frame(self.frame, style="Page.TFrame")
        mid.pack(fill="x", pady=(16, 0))
        mid.columnconfigure(0, weight=1, uniform="mid")
        mid.columnconfigure(1, weight=1, uniform="mid")
        run = card(mid, "Analyse", "inkrementell: nur neue Combos und geänderte Achsen")
        run.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(run.body, style="Muted.TLabel", justify="left", wraplength=520, text=(
            "Spielt die Linien per Engine nach (Linienfakten), berechnet die Achsen, die Kompatibilität, "
            "die Gesamtwerte und wendet die Filterprofile an.")).pack(anchor="w")
        opts = ttk.Frame(run.body)
        opts.pack(fill="x", pady=(10, 0))
        self.full = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Alles neu berechnen", variable=self.full).pack(side="left")
        self.run_btn = ttk.Button(opts, text="Analyse starten", style="Accent.TButton",
                                  command=lambda: app.start_analysis(self.full.get()))
        self.run_btn.pack(side="right")

        rob = card(mid, "Robustheit per Engine", "Handtrap-Suche, fortsetzbar, die stärksten Combos zuerst")
        rob.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(rob.body, style="Muted.TLabel", justify="left", wraplength=520, text=(
            "Pro Combo eine Suche ohne Handtrap und eine je Handtrap des Katalogs. Rechenintensiv: "
            "mit einer Anzahl oder einem Zeitbudget begrenzen und später fortsetzen.")).pack(anchor="w")
        opts = ttk.Frame(rob.body)
        opts.pack(fill="x", pady=(10, 0))
        ttk.Label(opts, text="Max. Combos").pack(side="left")
        self.max_combos = tk.StringVar(value="50")
        ttk.Spinbox(opts, textvariable=self.max_combos, from_=1, to=1_000_000, width=8).pack(side="left", padx=(6, 14))
        ttk.Label(opts, text="Zeitbudget (s)").pack(side="left")
        self.budget = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.budget, width=8).pack(side="left", padx=(6, 14))
        self.retry = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Unvollständige erneut", variable=self.retry).pack(side="left")
        self.rob_btn = ttk.Button(opts, text="Robustheit suchen", style="Accent.TButton", command=self._robustness)
        self.rob_btn.pack(side="right")

        low = ttk.Frame(self.frame, style="Page.TFrame")
        low.pack(fill="both", expand=True, pady=(16, 0))
        low.columnconfigure(0, weight=3, uniform="low")
        low.columnconfigure(1, weight=2, uniform="low")
        low.rowconfigure(0, weight=1)
        axes = card(low, "Achsen in Database 2")
        axes.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tree_frame, self.axes = make_tree(axes.body, app.theme, [
            ("axis", "Achse", 150), ("combos", "Combos", 80), ("min", "Min.", 80), ("mean", "Mittel", 80),
            ("max", "Max.", 80), ("version", "Version", 220),
        ], height=9)
        tree_frame.pack(fill="both", expand=True)
        log = card(low, "Protokoll")
        log.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        log_frame, self.log = make_text(log.body, app.theme, height=9, width=50, wrap="word")
        log_frame.pack(fill="both", expand=True)
        app.busy_listeners.append(self._busy)

    def _busy(self):
        state = "disabled" if self.app.busy else "normal"
        self.run_btn.configure(state=state)
        self.rob_btn.configure(state=state)

    def append(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > self.MAX_LOG_LINES:
            self.log.delete("1.0", "200.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _robustness(self):
        try:
            n = _optional(self.max_combos.get(), int, "Max. Combos")
            budget = _optional(self.budget.get(), float, "Zeitbudget")
        except ValueError as e:
            messagebox.showerror("Robustheit", str(e))
            return
        self.app.start_robustness(n, budget, self.retry.get())

    def refresh(self):
        a = self.app.analysis
        stats = a.stats()
        axes = stats["axes"]
        t = self.tiles
        combos = self.app.repo.count()
        analysed = axes.get("outcome", {}).get("combos", 0)
        t["combos"].set(combos)
        t["analysed"].set(analysed, None if not combos else "success" if analysed >= combos else "warning")
        t["families"].set(stats["families"])
        edges = a.edge_stats()["edges"]
        t["edges"].set(sum(edges.values()))
        robust = axes.get("robustness", {}).get("combos", 0)
        t["robust"].set(robust, "accent" if robust else None)
        filters = a.filters()
        stale = sum(1 for f in filters.values() if f["stale"])
        t["filters"].set(len(filters), "warning" if stale else None)
        rob = a.robustness_stats()
        self.note.configure(text=(
            "Kanten: " + (", ".join(f"{fmt.EDGE_LABELS.get(k, k)} {v}" for k, v in edges.items()) or "-")
            + "      Handtrap-Suchen: " + (", ".join(f"{k} {v['jobs']} ({v['combos']} Combos)" for k, v in rob.items()) or "noch keine")
            + (f"      {stale} Filterprofil(e) veraltet: Analyse starten oder im Reiter Filter anwenden" if stale else "")
            + (f"      {combos - analysed} Combos noch nicht bewertet: Analyse starten" if analysed < combos else "")))
        self.axes.delete(*self.axes.get_children())
        for axis in (*fmt.AXIS_ORDER, *sorted(set(axes) - set(fmt.AXIS_ORDER))):
            if axis in axes:
                x = axes[axis]
                self.axes.insert("", "end", values=(fmt.axis_label(axis), x["combos"], f"{x['min']:.2f}", f"{x['mean']:.2f}",
                                                    f"{x['max']:.2f}", x["version"]))
        restripe(self.axes)


# ---- page: evaluation profiles ---------------------------------------------------------------------
class ProfilesPage:
    def __init__(self, app: AnalysisApp):
        self.app = app
        self.loaded = False
        self.frame = _page(app)
        filt = card(self.frame, padding=12)
        filt.pack(fill="x")
        row = filt.body
        ttk.Label(row, text="Ordnen nach").pack(side="left")
        self.axis = tk.StringVar(value=fmt.axis_label("strength"))
        self.axis_box = ttk.Combobox(row, textvariable=self.axis, state="readonly", width=18)
        self.axis_box.pack(side="left", padx=8)
        ttk.Label(row, text="Karte").pack(side="left", padx=(12, 0))
        self.card = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.card, width=28)
        e.pack(side="left", padx=8)
        e.bind("<Return>", lambda _e: self.refresh())
        ttk.Label(row, text="Mindestwert").pack(side="left", padx=(12, 6))
        self.minimum = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.minimum, width=6).pack(side="left")
        ttk.Label(row, text="Anzahl").pack(side="left", padx=(16, 6))
        self.limit = tk.StringVar(value="200")
        ttk.Spinbox(row, textvariable=self.limit, from_=1, to=100_000, increment=100, width=8).pack(side="left")
        ttk.Button(row, text="Anzeigen", style="Accent.TButton", command=self.refresh).pack(side="left", padx=16)
        lst = card(self.frame, padding=12)
        lst.pack(fill="both", expand=True, pady=(16, 0))
        self.table = ComboTable(app, lst.body, [
            ("id", "#", 60), ("names", "Benötigte Karten", 300),
            *[(a, fmt.axis_label(a)[:11], 78) for a in SCORE_AXES],
        ], height=18)
        self.table.frame.pack(fill="both", expand=True)

    def refresh(self):
        a = self.app.analysis
        present = [x for x in (*fmt.AXIS_ORDER, *sorted(set(a.axes()) - set(fmt.AXIS_ORDER))) if x in set(a.axes())]
        labels = {fmt.axis_label(x): x for x in present}
        self.axis_box.configure(values=list(labels))
        axis = labels.get(self.axis.get())
        if axis is None:
            if not labels:
                self.table.set([])
                return
            axis = present[0]
            self.axis.set(fmt.axis_label(axis))
        try:
            limit = _optional(self.limit.get(), int, "Anzahl") or 200
            least = _optional(self.minimum.get(), float, "Mindestwert")
            code = self.app.store.resolve(self.card.get().strip()).code if self.card.get().strip() else None
        except (KeyError, ValueError) as e:
            messagebox.showerror("Bewertungen", str(e).strip("'\""))
            return
        if code is None:
            sigs = [s for s, _ in a.top(axis, limit=limit, min_score=least)]
        else:
            scores = a.scores(axis)
            sigs = [c.signature for c in self.app.repo.iter_combos(card=code)]
            sigs = sorted((s for s in sigs if s in scores and (least is None or scores[s] >= least)),
                          key=lambda s: -scores[s])[:limit]
        rows = []
        for sig in sigs:
            combo, prof = self.app.repo.by_signature(sig), a.profile(sig)
            rows.append((sig, (combo.id if combo else "?", fmt.names(combo.cards, self.app.store.name) if combo else sig[:12],
                               *[fmt.score(prof[x].score) if x in prof else "-" for x in SCORE_AXES]), None, ""))
        self.table.set(rows)


# ---- page: weights -------------------------------------------------------------------------------------
class WeightsPage:
    def __init__(self, app: AnalysisApp):
        self.app = app
        self.loaded = False
        self.frame = _page(app)
        self.frame.columnconfigure(0, weight=3, uniform="w")
        self.frame.columnconfigure(1, weight=2, uniform="w")
        self.frame.rowconfigure(0, weight=1)
        sc = card(self.frame, "Gesamtwerte", "gewichtete Kombination gespeicherter Achsen und Rohwerte")
        sc.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        head = ttk.Frame(sc.body)
        head.pack(fill="x")
        ttk.Label(head, text="Bewertung").pack(side="left")
        self.scoring = tk.StringVar()
        self.scoring_box = ttk.Combobox(head, textvariable=self.scoring, state="readonly", width=40)
        self.scoring_box.pack(side="left", padx=8)
        self.scoring_box.bind("<<ComboboxSelected>>", lambda _e: self._show_scoring())
        self.rows = ttk.Frame(sc.body)
        self.rows.pack(fill="x", pady=(14, 0))
        self.vars: dict[str, tk.StringVar] = {}
        ttk.Label(sc.body, style="Muted.TLabel", justify="left", wraplength=640, text=(
            "Gewichte müssen sich nicht zu 1 summieren, sie werden normiert. 0 schaltet einen Bestandteil ab. "
            "Fehlt einer Combo ein Bestandteil (z. B. Robustheit noch nicht gesucht), werden die übrigen "
            "hochgerechnet und der Wert ist als vorläufig markiert.")).pack(anchor="w", pady=(14, 0))
        btns = ttk.Frame(sc.body)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="Speichern und neu berechnen", style="Accent.TButton", command=self._save_scoring).pack(side="left")
        ttk.Button(btns, text="Standardgewichte", command=self._reset_scoring).pack(side="left", padx=8)

        ht = card(self.frame, "Handtraps", "Verbreitung 0 bis 1: Gewicht in der Achse Robustheit")
        ht.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.trap_rows = ttk.Frame(ht.body)
        self.trap_rows.pack(fill="x")
        self.trap_vars: dict[str, tk.StringVar] = {}
        ttk.Label(ht.body, style="Muted.TLabel", justify="left", wraplength=420, text=(
            "Andere Gewichte ändern nur die Mittelung, die gespeicherten Suchergebnisse bleiben gültig. "
            "Welche Handtraps im Katalog stehen, legt [analysis.robustness.handtraps] in deepyugioh.toml fest.")).pack(
            anchor="w", pady=(14, 0))
        ttk.Button(ht.body, text="Speichern und neu berechnen", style="Accent.TButton", command=self._save_traps).pack(
            anchor="w", pady=(14, 0))

    def refresh(self):
        self.scorings = {f"{s.label} ({s.name})": s for s in settings.analysis_scorings()}
        self.scoring_box.configure(values=list(self.scorings))
        if self.scoring.get() not in self.scorings:
            self.scoring.set(next(iter(self.scorings)))
        self._show_scoring()
        for w in self.trap_rows.winfo_children():
            w.destroy()
        self.trap_vars = {}
        for n, (key, entry) in enumerate(settings.robustness_settings().catalog.items()):
            ttk.Label(self.trap_rows, text=entry["card"]).grid(row=n, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=f"{entry.get('weight', 1.0):g}")
            ttk.Entry(self.trap_rows, textvariable=var, width=8).grid(row=n, column=1, sticky="w", padx=12)
            self.trap_vars[key] = var

    def _show_scoring(self):
        from deepyugioh.analysis.strength import DEFAULT_SCORINGS, component_label

        sc = self.scorings[self.scoring.get()]
        comps = list(sc.weights)
        for extra in DEFAULT_SCORINGS.get(sc.name, {}).get("weights", {}):
            if extra not in comps:
                comps.append(extra)
        for w in self.rows.winfo_children():
            w.destroy()
        self.vars = {}
        total = sum(sc.weights.values()) or 1.0
        for n, comp in enumerate(comps):
            ttk.Label(self.rows, text=component_label(comp)).grid(row=n, column=0, sticky="w", pady=3)
            ttk.Label(self.rows, text=comp, style="Muted.TLabel").grid(row=n, column=1, sticky="w", padx=14)
            var = tk.StringVar(value=f"{sc.weights.get(comp, 0.0):g}")
            ttk.Entry(self.rows, textvariable=var, width=8).grid(row=n, column=2, sticky="w")
            ttk.Label(self.rows, text=f"{100 * sc.weights.get(comp, 0.0) / total:.0f} %", style="Muted.TLabel").grid(
                row=n, column=3, sticky="w", padx=14)
            self.vars[comp] = var

    def _numbers(self, variables: dict[str, tk.StringVar], what: str) -> dict[str, float] | None:
        try:
            return {k: float(v.get().strip().replace(",", ".")) for k, v in variables.items()}
        except ValueError:
            messagebox.showerror(what, "Bitte nur Zahlen eintragen.")
            return None

    def _save_scoring(self):
        weights = self._numbers(self.vars, "Gewichte")
        if weights is None:
            return
        sc = self.scorings[self.scoring.get()]
        try:
            settings.set_analysis_weights(sc.name, {k: v for k, v in weights.items() if v > 0 or k in sc.weights})
        except ValueError as e:
            messagebox.showerror("Gewichte", str(e))
            return
        self.app.recompute()

    def _reset_scoring(self):
        settings.reset_analysis_weights(self.scorings[self.scoring.get()].name)
        self.app.recompute()

    def _save_traps(self):
        weights = self._numbers(self.trap_vars, "Handtraps")
        if weights is None:
            return
        try:
            settings.set_handtrap_weights(weights)
        except ValueError as e:
            messagebox.showerror("Handtraps", str(e))
            return
        self.app.recompute()


# ---- page: filter profiles ---------------------------------------------------------------------------
class FiltersPage:
    FIELDS = (("name", "Name", 16), ("label", "Bezeichnung", 34), ("minimum", "Mindestwerte", 34),
              ("weights", "Eigene Gewichte", 34), ("archetypes", "Archetypen", 24), ("races", "Typen", 24),
              ("attributes", "Attribute", 24), ("min_fit", "Min. Passung", 8), ("min_cards", "Min. Karten", 6),
              ("max_cards", "Max. Karten", 6), ("limit", "Nur die besten N", 8))

    def __init__(self, app: AnalysisApp):
        self.app = app
        self.loaded = False
        self.frame = _page(app)
        top = ttk.Frame(self.frame, style="Page.TFrame")
        top.pack(fill="x")
        top.columnconfigure(0, weight=2, uniform="f")
        top.columnconfigure(1, weight=3, uniform="f")
        lst = card(top, "Filterprofile", "in Database 2 gespeichert")
        lst.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tree_frame, self.profiles = make_tree(lst.body, app.theme, [
            ("name", "Name", 120), ("members", "Combos", 70), ("of", "von", 70), ("state", "Stand", 150),
        ], height=8)
        tree_frame.pack(fill="both", expand=True)
        self.profiles.bind("<<TreeviewSelect>>", self._select)

        form = card(top, "Profil", "Mindestwerte als Name=Zahl, z. B. strength=70, outcome.disruptions=1")
        form.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        grid = ttk.Frame(form.body)
        grid.pack(fill="x")
        self.vars: dict[str, tk.StringVar] = {}
        for n, (key, label, width) in enumerate(self.FIELDS):
            r, c = divmod(n, 2)
            ttk.Label(grid, text=label).grid(row=r, column=2 * c, sticky="w", pady=3, padx=(0 if c == 0 else 18, 8))
            self.vars[key] = tk.StringVar()
            ttk.Entry(grid, textvariable=self.vars[key], width=width).grid(row=r, column=2 * c + 1, sticky="w")
        r = (len(self.FIELDS) + 1) // 2
        ttk.Label(grid, text="Ordnen nach").grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        self.rank_by = tk.StringVar(value="strength")
        self.rank_box = ttk.Combobox(grid, textvariable=self.rank_by, width=20)
        self.rank_box.grid(row=r, column=1, sticky="w")
        ttk.Label(grid, text="Sperren").grid(row=r, column=2, sticky="w", padx=(18, 8))
        self.locks = tk.StringVar(value="beliebig")
        ttk.Combobox(grid, textvariable=self.locks, values=list(LOCKS), state="readonly", width=22).grid(row=r, column=3, sticky="w")
        ttk.Label(grid, text="Situation").grid(row=r + 1, column=0, sticky="w", pady=3, padx=(0, 8))
        self.situation = tk.StringVar(value="alle")
        ttk.Combobox(grid, textvariable=self.situation, values=list(SITUATIONS), state="readonly", width=20).grid(
            row=r + 1, column=1, sticky="w")
        self.dominated = tk.BooleanVar(value=True)
        ttk.Checkbutton(grid, text="Unterlegene Combos ausblenden", variable=self.dominated).grid(
            row=r + 1, column=2, columnspan=2, sticky="w", padx=(18, 0))
        btns = ttk.Frame(form.body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Speichern und anwenden", style="Accent.TButton", command=self._save).pack(side="left")
        ttk.Button(btns, text="Als Datei exportieren", command=self._export).pack(side="left", padx=8)
        ttk.Button(btns, text="Löschen", style="Danger.TButton", command=self._delete).pack(side="right")

        res = card(self.frame, padding=12)
        res.pack(fill="both", expand=True, pady=(16, 0))
        self.summary = ttk.Label(res.body, text="", style="Muted.TLabel", justify="left", wraplength=1150)
        self.summary.pack(fill="x", pady=(0, 8))
        self.table = ComboTable(app, res.body, [
            ("rank", "Rang", 55), ("value", "Wert", 65), ("fit", "Passung", 70), ("id", "#", 60),
            ("names", "Benötigte Karten", 420),
        ], height=10)
        self.table.frame.pack(fill="both", expand=True)

    def refresh(self, select: str | None = None):
        from deepyugioh.analysis import QualityFilter

        a = self.app.analysis
        QualityFilter(a).ensure_defaults()
        self.rank_box.configure(values=[x for x in fmt.AXIS_ORDER if x in set(a.axes()) and x != "line"])
        self.info = a.filters()
        self.profiles.delete(*self.profiles.get_children())
        for name, info in self.info.items():
            state = "noch nicht angewendet" if info["applied_at"] is None else "veraltet" if info["stale"] else "aktuell"
            self.profiles.insert("", "end", iid=name, values=(name, info["members"], info["considered"], state),
                                 tags=("warn",) if state != "aktuell" else ())
        restripe(self.profiles)
        pick = select if select in self.info else next(iter(self.info), None)
        if pick:
            self.profiles.selection_set(pick)

    def _select(self, *_):
        sel = self.profiles.selection()
        if not sel:
            return
        name = sel[0]
        info = self.info[name]
        spec = info["spec"]
        v = self.vars
        v["name"].set(name)
        v["label"].set(spec.get("label", ""))
        v["minimum"].set(fmt.pairs_text(spec.get("minimum", {})))
        v["weights"].set(fmt.pairs_text(spec.get("weights", {})))
        for key in ("archetypes", "races", "attributes"):
            v[key].set(", ".join(spec.get(key, ())))
        for key in ("min_fit", "min_cards", "max_cards", "limit"):
            v[key].set("" if spec.get(key) is None else f"{spec[key]:g}")
        self.rank_by.set(spec.get("rank_by", "strength"))
        self.locks.set(next(k for k, x in LOCKS.items() if x == spec.get("locks", "any")))
        self.situation.set(next(k for k, x in SITUATIONS.items() if x == spec.get("late")))
        self.dominated.set(spec.get("drop_dominated", True))
        dropped = ", ".join(f"{k} {n}" for k, n in info["dropped"].items()) or "nichts"
        self.summary.configure(text=(
            f"{name}: {info['members']} von {info['considered']} Combos, entfernt: {dropped}"
            + ("      Stand veraltet: Database 2 hat sich seit dem Anwenden geändert" if info["stale"] and info["applied_at"] else "")))
        rows = []
        for sig, rank, value, fit in self.app.analysis.filter_results(name, 500):
            combo = self.app.repo.by_signature(sig)
            rows.append((sig, (rank, f"{value:.1f}", "-" if fit is None else f"{fit:.0f}", combo.id if combo else "?",
                               fmt.names(combo.cards, self.app.store.name) if combo else sig[:12]),
                         fit, ", ".join([*spec.get("archetypes", ()), *spec.get("races", ()), *spec.get("attributes", ())])))
        self.table.set(rows)

    def _profile(self):
        from deepyugioh.analysis import FilterProfile

        v = self.vars
        number = lambda key, kind: _optional(v[key].get(), kind, dict((k, lab) for k, lab, _ in self.FIELDS)[key])
        return FilterProfile(
            v["name"].get().strip(), v["label"].get().strip(), fmt.parse_pairs(v["minimum"].get(), "Mindestwerte"),
            self.rank_by.get().strip() or "strength", fmt.parse_pairs(v["weights"].get(), "Eigene Gewichte"),
            fmt.words(v["archetypes"].get()), fmt.words(v["races"].get()), fmt.words(v["attributes"].get()),
            number("min_fit", float), LOCKS[self.locks.get()], number("min_cards", int), number("max_cards", int),
            SITUATIONS[self.situation.get()], self.dominated.get(), number("limit", int))

    def _save(self):
        from deepyugioh.analysis import QualityFilter, deck_profile_from_names

        try:
            profile = self._profile()
            if profile.has_deck:
                deck_profile_from_names(profile.archetypes, profile.races, profile.attributes)
            qf = QualityFilter(self.app.analysis)
            qf.save(profile)
            qf.run(profile.name)
        except (ValueError, KeyError) as e:
            messagebox.showerror("Filterprofil", str(e).strip("'\""))
            return
        self.app.status.set(f"Filterprofil {profile.name} gespeichert und angewendet")
        self.refresh(select=profile.name)

    def _delete(self):
        name = self.vars["name"].get().strip()
        if name in self.info and messagebox.askyesno("Filterprofil", f"Profil {name} und seine gespeicherten Ergebnisse löschen?"):
            self.app.analysis.delete_filter(name)
            self.refresh()

    def _export(self):
        from deepyugioh.analysis import QualityFilter

        name = self.vars["name"].get().strip()
        if name not in self.info:
            messagebox.showinfo("Export", "Zuerst ein gespeichertes Profil auswählen.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile=f"{name}.json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        n = QualityFilter(self.app.analysis).export(name, path, self.app.repo.by_signature, self.app.store.name)
        self.app.status.set(f"{n} Combos des Profils {name} exportiert nach {path}")


# ---- page: deck profile fit ---------------------------------------------------------------------------
class FitPage:
    def __init__(self, app: AnalysisApp):
        self.app = app
        self.frame = _page(app)
        form = card(self.frame, "Deckprofil", "Namen wie in den Kartenskripten, mit Komma getrennt: Blue-Eyes; Dragon; LIGHT")
        form.pack(fill="x")
        row = form.body
        self.vars = {}
        for key, label, width in (("archetypes", "Archetypen", 26), ("races", "Typen", 20), ("attributes", "Attribute", 16)):
            ttk.Label(row, text=label).pack(side="left", padx=(0 if key == "archetypes" else 14, 6))
            self.vars[key] = tk.StringVar()
            e = ttk.Entry(row, textvariable=self.vars[key], width=width)
            e.pack(side="left")
            e.bind("<Return>", lambda _e: self.start())
        ttk.Label(row, text="Min. Passung").pack(side="left", padx=(14, 6))
        self.min_fit = tk.StringVar(value="80")
        ttk.Entry(row, textvariable=self.min_fit, width=6).pack(side="left")
        ttk.Label(row, text="Anzahl").pack(side="left", padx=(14, 6))
        self.limit = tk.StringVar(value="200")
        ttk.Spinbox(row, textvariable=self.limit, from_=1, to=100_000, increment=100, width=8).pack(side="left")
        ttk.Button(row, text="Passung berechnen", style="Accent.TButton", command=self.start).pack(side="left", padx=16)
        res = card(self.frame, padding=12)
        res.pack(fill="both", expand=True, pady=(16, 0))
        self.summary = ttk.Label(res.body, text="Passende Combos werden nach Spielstärke geordnet.", style="Muted.TLabel")
        self.summary.pack(fill="x", pady=(0, 8))
        self.table = ComboTable(app, res.body, [
            ("fit", "Passung", 70), ("strength", "Stärke", 65), ("generic", "Generizität", 85), ("id", "#", 60),
            ("names", "Benötigte Karten", 420),
        ], height=16)
        self.table.frame.pack(fill="both", expand=True)

    def start(self):
        from deepyugioh.analysis import deck_profile_from_names

        try:
            deck = deck_profile_from_names(*(fmt.words(self.vars[k].get()) for k in ("archetypes", "races", "attributes")))
            if not (deck.archetypes or deck.races or deck.attributes):
                raise ValueError("Ein Deckprofil braucht mindestens einen Archetyp, Typ oder ein Attribut.")
            least = _optional(self.min_fit.get(), float, "Min. Passung") or 0.0
            limit = _optional(self.limit.get(), int, "Anzahl") or 200
        except (KeyError, ValueError) as e:
            messagebox.showerror("Deckprofil", str(e).strip("'\""))
            return

        def work():
            from deepyugioh.analysis import AnalysisRepository, deck_fit

            repo = AnalysisRepository()
            strength = repo.scores("strength")
            hits = []
            total = 0
            for sig, raw, score in repo.axis_rows("genericness"):
                total += 1
                fit = deck_fit(raw, deck)
                if fit >= least:
                    hits.append((strength.get(sig, 0.0), fit, sig, score))
            hits.sort(reverse=True)
            return deck.name, total, len(hits), hits[:limit]

        self.app.task("Passung", work, self._done, exclusive=False)

    def _done(self, result):
        deck, total, n, hits = result
        self.summary.configure(text=f"Deckprofil {deck}: {n} von {total} Combos passen, die {len(hits)} stärksten sind gelistet.")
        rows = []
        for strength, fit, sig, generic in hits:
            combo = self.app.repo.by_signature(sig)
            rows.append((sig, (f"{fit:.0f}", f"{strength:.0f}", fmt.score(generic), combo.id if combo else "?",
                               fmt.names(combo.cards, self.app.store.name) if combo else sig[:12]), fit, deck))
        self.table.set(rows)


def run_gui() -> int:
    from deepyugioh.gui.app import TestModeRequired

    if not settings.test_mode():
        raise TestModeRequired(
            "Die Test-GUI ist nur im Testmodus verfügbar. Einschalten mit `deepyugioh testmode on` "
            f"(oder {settings.TEST_MODE_ENV}=1)."
        )
    root = tk.Tk()
    try:
        AnalysisApp(root)
    except Exception:
        root.destroy()
        raise
    root.mainloop()
    return 0
