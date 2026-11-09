"""Flat dark/light theme for the test GUI (ttk 'clam' + recoloured classic Tk widgets).

The Tk build of the uv Python has no Xft, so only X11 core fonts exist; the
look therefore comes from colour, spacing and layout, not from typefaces.
Classic Tk widgets (Text, Listbox, Canvas, the sidebar) do not follow ttk
styles; they are registered here and recoloured when the theme changes.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable

PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#15181e", "surface": "#1e222a", "tile": "#262b35", "field": "#262b35", "border": "#323946",
        "fg": "#e4e8ef", "muted": "#8b94a5", "accent": "#4c8dff", "accent_hover": "#6ea3ff", "accent_fg": "#ffffff",
        "success": "#3fb950", "warning": "#d29922", "danger": "#f0524a", "select": "#2c4573", "stripe": "#222731",
        "sidebar": "#0f1115", "sidebar_hover": "#1a1d24", "sidebar_active": "#1e222a", "sidebar_fg": "#a3abba",
        "button": "#2b313c", "button_hover": "#353c49",
    },
    "light": {
        "bg": "#eef1f6", "surface": "#ffffff", "tile": "#f4f6fa", "field": "#ffffff", "border": "#d6dbe4",
        "fg": "#1c2230", "muted": "#677084", "accent": "#2f6bed", "accent_hover": "#4a80f0", "accent_fg": "#ffffff",
        "success": "#1a7f37", "warning": "#9a6700", "danger": "#cf222e", "select": "#d6e4ff", "stripe": "#f7f9fc",
        "sidebar": "#1c2230", "sidebar_hover": "#262e3e", "sidebar_active": "#2f3850", "sidebar_fg": "#b7bfce",
        "button": "#eef1f6", "button_hover": "#e2e7ef",
    },
}

FAMILY = "helvetica"   # X11 core font alias (Nimbus Sans)
MONO = "courier"


class Theme:
    def __init__(self, root: tk.Tk, name: str = "dark"):
        self.root = root
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.name = name if name in PALETTES else "dark"
        self._widgets: list[tuple[tk.Widget, str]] = []
        self._listeners: list[Callable[[dict], None]] = []
        self.fonts = {
            "base": tkfont.Font(root, family=FAMILY, size=10),
            "small": tkfont.Font(root, family=FAMILY, size=9),
            "bold": tkfont.Font(root, family=FAMILY, size=10, weight="bold"),
            "title": tkfont.Font(root, family=FAMILY, size=15, weight="bold"),
            "brand": tkfont.Font(root, family=FAMILY, size=14, weight="bold"),
            "nav": tkfont.Font(root, family=FAMILY, size=11),
            "kpi": tkfont.Font(root, family=FAMILY, size=17, weight="bold"),
            "mono": tkfont.Font(root, family=MONO, size=10),
        }
        for named in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(named).configure(family=FAMILY, size=10)
        self.apply(self.name)

    @property
    def pal(self) -> dict[str, str]:
        return PALETTES[self.name]

    def toggle(self) -> str:
        self.apply("light" if self.name == "dark" else "dark")
        return self.name

    def on_change(self, fn: Callable[[dict], None]):
        self._listeners.append(fn)

    # ---- classic Tk widgets ------------------------------------------------------
    def register(self, widget: tk.Widget, kind: str) -> tk.Widget:
        """kind: text | listbox | tree (row tags) | canvas (surface background) | sidebar."""
        self._widgets.append((widget, kind))
        self._paint(widget, kind)
        return widget

    def _paint(self, w: tk.Widget, kind: str):
        p = self.pal
        try:
            if kind in ("text", "listbox"):
                w.configure(bg=p["field"], fg=p["fg"], selectbackground=p["select"], selectforeground=p["fg"],
                            relief="flat", borderwidth=0, highlightthickness=1, highlightbackground=p["border"],
                            highlightcolor=p["accent"])
                if kind == "text":
                    w.configure(insertbackground=p["fg"], padx=10, pady=8, inactiveselectbackground=p["select"])
                    w.tag_configure("h", font=self.fonts["bold"], foreground=p["accent"])
                    w.tag_configure("ok", foreground=p["success"])
                    w.tag_configure("warn", foreground=p["warning"])
                    w.tag_configure("err", foreground=p["danger"])
                    w.tag_configure("muted", foreground=p["muted"])
                else:
                    w.configure(activestyle="none")
            elif kind == "tree":  # ttk.Treeview row tags
                w.tag_configure("odd", background=p["stripe"])
                w.tag_configure("warn", foreground=p["warning"])
                w.tag_configure("err", foreground=p["danger"])
                w.tag_configure("ok", foreground=p["success"])
            elif kind == "canvas":
                w.configure(bg=p["surface"], highlightthickness=0)
            elif kind == "sidebar":
                w.configure(bg=p["sidebar"])
        except tk.TclError:  # widget destroyed
            pass

    # ---- ttk styles -------------------------------------------------------------------
    def apply(self, name: str):
        self.name = name
        p, s, f = self.pal, self.style, self.fonts
        r = self.root
        r.configure(bg=p["bg"])
        for opt, val in (("background", p["field"]), ("foreground", p["fg"]), ("selectBackground", p["select"]),
                         ("selectForeground", p["fg"])):
            r.option_add(f"*TCombobox*Listbox.{opt}", val)
        r.option_add("*TCombobox*Listbox.font", f["base"])

        s.configure(".", background=p["surface"], foreground=p["fg"], bordercolor=p["border"], lightcolor=p["surface"],
                    darkcolor=p["surface"], troughcolor=p["field"], focuscolor=p["accent"], selectbackground=p["select"],
                    selectforeground=p["fg"], insertcolor=p["fg"], fieldbackground=p["field"], font=f["base"],
                    arrowcolor=p["muted"])
        s.map(".", foreground=[("disabled", p["muted"])])

        # frames and labels
        s.configure("TFrame", background=p["surface"])
        s.configure("Page.TFrame", background=p["bg"])
        s.configure("Card.TFrame", background=p["surface"], bordercolor=p["border"], relief="solid", borderwidth=1)
        s.configure("Tile.TFrame", background=p["tile"])
        s.configure("Header.TFrame", background=p["surface"])
        s.configure("TLabel", background=p["surface"], foreground=p["fg"])
        s.configure("Muted.TLabel", foreground=p["muted"], font=f["small"])
        s.configure("CardTitle.TLabel", font=f["bold"])
        s.configure("Title.TLabel", font=f["title"])
        s.configure("Page.TLabel", background=p["bg"])
        s.configure("PageMuted.TLabel", background=p["bg"], foreground=p["muted"], font=f["small"])
        s.configure("Dirty.PageMuted.TLabel", foreground=p["warning"])
        s.configure("TileValue.TLabel", background=p["tile"], font=f["kpi"])
        s.configure("TileLabel.TLabel", background=p["tile"], foreground=p["muted"], font=f["small"])
        for tone in ("success", "warning", "danger", "accent"):
            s.configure(f"{tone.title()}.TileValue.TLabel", foreground=p[tone])
            s.configure(f"{tone.title()}.TLabel", foreground=p[tone], font=f["bold"])

        # buttons
        s.configure("TButton", background=p["button"], foreground=p["fg"], bordercolor=p["border"],
                    lightcolor=p["button"], darkcolor=p["button"], padding=(12, 6), relief="flat", focusthickness=0)
        s.map("TButton", background=[("disabled", p["surface"]), ("pressed", p["button_hover"]), ("active", p["button_hover"])],
              lightcolor=[("active", p["button_hover"])], darkcolor=[("active", p["button_hover"])],
              bordercolor=[("focus", p["border"])])
        s.configure("Accent.TButton", background=p["accent"], foreground=p["accent_fg"], bordercolor=p["accent"],
                    lightcolor=p["accent"], darkcolor=p["accent"], font=f["bold"])
        s.map("Accent.TButton",
              background=[("disabled", p["button"]), ("pressed", p["accent_hover"]), ("active", p["accent_hover"])],
              foreground=[("disabled", p["muted"])],
              bordercolor=[("disabled", p["border"]), ("active", p["accent_hover"])],
              lightcolor=[("disabled", p["button"]), ("active", p["accent_hover"])],
              darkcolor=[("disabled", p["button"]), ("active", p["accent_hover"])])
        s.configure("Danger.TButton", background=p["danger"], foreground="#ffffff", bordercolor=p["danger"],
                    lightcolor=p["danger"], darkcolor=p["danger"], font=f["bold"])
        s.map("Danger.TButton", background=[("disabled", p["button"])], foreground=[("disabled", p["muted"])],
              bordercolor=[("disabled", p["border"])], lightcolor=[("disabled", p["button"])],
              darkcolor=[("disabled", p["button"])])

        # inputs
        for w in ("TEntry", "TSpinbox", "TCombobox"):
            s.configure(w, fieldbackground=p["field"], foreground=p["fg"], bordercolor=p["border"],
                        lightcolor=p["field"], darkcolor=p["field"], background=p["button"], padding=(6, 4),
                        arrowcolor=p["muted"], insertcolor=p["fg"])
            s.map(w, bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["field"])],
                  fieldbackground=[("readonly", p["field"]), ("disabled", p["surface"])],
                  foreground=[("disabled", p["muted"])], background=[("active", p["button_hover"])])
        s.map("TCombobox", selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["fg"])])
        s.configure("TCheckbutton", background=p["surface"], foreground=p["fg"], indicatorbackground=p["field"],
                    indicatorforeground=p["accent_fg"], upperbordercolor=p["border"], lowerbordercolor=p["border"],
                    padding=(0, 3))
        s.map("TCheckbutton", background=[("active", p["surface"])],
              indicatorbackground=[("selected", p["accent"]), ("active", p["field"])],
              upperbordercolor=[("selected", p["accent"])], lowerbordercolor=[("selected", p["accent"])])

        # tables, scrollbars, progress, panes
        s.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["fg"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], rowheight=26)
        s.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", p["fg"])])
        s.configure("Treeview.Heading", background=p["tile"], foreground=p["muted"], bordercolor=p["border"],
                    lightcolor=p["tile"], darkcolor=p["tile"], relief="flat", font=f["bold"], padding=(6, 6))
        s.map("Treeview.Heading", background=[("active", p["button_hover"])])
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"{orient}.TScrollbar", background=p["button"], troughcolor=p["surface"],
                        bordercolor=p["surface"], lightcolor=p["button"], darkcolor=p["button"],
                        arrowcolor=p["muted"], arrowsize=13, gripcount=0)
            s.map(f"{orient}.TScrollbar", background=[("active", p["button_hover"])])
        s.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["field"],
                    bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"], thickness=8)
        s.configure("TPanedwindow", background=p["bg"])
        s.configure("Card.TPanedwindow", background=p["surface"])
        s.configure("Sash", sashthickness=8, gripcount=0, background=p["bg"])
        s.configure("TSeparator", background=p["border"])

        for w, kind in list(self._widgets):
            self._paint(w, kind)
        for fn in self._listeners:
            fn(p)
