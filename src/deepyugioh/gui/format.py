"""Text formatting for the test GUI - kept free of tkinter so it can be tested."""

from __future__ import annotations

import re
from collections import Counter
from typing import Callable, Iterable

from deepyugioh.contracts.combos import ComboRecord
from deepyugioh.contracts.setups import StartSetup

Namer = Callable[[int], str]

# Engine steps without strategic meaning (same list as the CLI)
HIDDEN_STEPS = {"no chain", "place", "position", "keep order"}

# Why a search was incomplete -> the setting that controls it
INCOMPLETE_HINTS = {
    "max_nodes": "Max. Knoten pro Suche erhöhen",
    "time_limit": "Zeitlimit pro Suche erhöhen",
    "max_depth": "Max. Entscheidungen pro Linie erhöhen",
    "choices_truncated": "Max. Optionen pro Kartenauswahl erhöhen",
    "neighbourhood_truncated:deck": "Max. Deck-Nachbarn erhöhen",
    "neighbourhood_truncated:extra": "Max. Extra Deck erhöhen",
    "validation_failed": "Engine nicht deterministisch für diese Linie - Skript prüfen",
    "engine_rejected_response": "Antwortkodierung prüfen (Bug im Generator)",
}

# Benefit criteria a run target can count ("" = every new combo)
TARGET_BENEFIT_LABELS = {
    "": "jede neue Combo",
    "wins": "Sieg (Duell gewonnen)",
    "opp_lp_loss": "Schaden am Gegner",
    "opp_field_loss": "Gegner verliert Feldkarten",
    "opp_hand_loss": "Gegner verliert Handkarten",
    "field_delta": "mehr eigene Feldkarten",
    "hand_delta": "mehr eigene Handkarten",
    "lp_gain": "eigene LP gewonnen",
}

# How a global run can end (settings.RUN_MODES) -> label in the GUI
RUN_MODE_LABELS = {
    "target": "Ziel-Combos erreicht",
    "max_jobs": "Anzahl Suchen",
    "time_budget": "Zeitbudget",
    "unlimited": "alle Kandidaten bearbeitet",
}

# GenerationReport.stop_reason -> sentence for the log
STOP_REASONS = {
    "target": "Ziel erreicht",
    "max_jobs": "Max. Suchen erreicht",
    "time_budget": "Zeitbudget abgelaufen",
    "stopped": "gestoppt",
    "exhausted": "alle Kandidaten der gewählten Stufen bearbeitet",
}


def target_text(target: int, benefit: str) -> str:
    """'100 neue Combos' or '100 neue Combos mit Sieg (Duell gewonnen)'."""
    what = f"{target} neue Combo{'' if target == 1 else 's'}"
    return f"{what} mit {TARGET_BENEFIT_LABELS[benefit]}" if benefit else what


_COUNT_PREFIX = re.compile(r"^\s*(\d+)\s*[x×]\s+(.+?)\s*$", re.IGNORECASE)


def parse_card_list(text: str) -> list[str]:
    """One card per line (name or passcode); `3x Name` adds three copies."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _COUNT_PREFIX.match(line)
        if m:
            out += [m.group(2)] * int(m.group(1))
        else:
            out.append(line)
    return out


def card_list_text(codes: Iterable[int], name: Namer) -> str:
    """Inverse of parse_card_list: grouped `2x Name` lines, order of first appearance."""
    counts = Counter(codes)
    return "\n".join(f"{n}x {name(c)}" if n > 1 else name(c) for c, n in counts.items())


def names(codes: Iterable[int], name: Namer) -> str:
    counts = Counter(codes)
    return ", ".join(f"{n}x {name(c)}" if n > 1 else name(c) for c, n in counts.items()) or "-"


def reasons(d: dict) -> str:
    return ", ".join(f"{k} ({v})" for k, v in d.items()) or "-"


def setup_text(setup: StartSetup, name: Namer) -> str:
    opp = ", ".join(f"{name(c.code)} [{c.zone}]" for c in setup.opponent.cards) or "leer"
    return (
        f"  Hand:   {names(setup.hand, name)}\n"
        f"  Deck:   {names(setup.deck, name)}\n"
        f"  Extra:  {names(setup.extra, name)}\n"
        + (f"  Friedhof: {names(setup.grave, name)}\n" if setup.grave else "")
        + (f"  Verbannt: {names(setup.banished, name)}\n" if setup.banished else "")
        + (f"  Feld:   {', '.join(f'{name(c.code)} [{c.zone}]' for c in setup.field)}\n" if setup.field else "")
        + f"  Gegner: {setup.opponent.name} - {opp}\n"
        f"  Zonen verzweigen: {'ja' if setup.branch_zones else 'nein'}"
    )


def _player(p: dict, name: Namer) -> str:
    field = lambda cards: names([c["code"] for c in cards], name)
    return (
        f"    Monster:        {field(p['mzone'])}\n"
        f"    Zauber/Fallen:  {field(p['szone'])}\n"
        f"    Hand:           {names(p['hand'], name)}\n"
        f"    Friedhof:       {names(p['grave'], name)}\n"
        f"    Verbannt:       {names(p['banished'], name)}\n"
        f"    Deck/Extra:     {p['deck_count']} / {p['extra_count']}"
    )


def line_text(combo: ComboRecord, all_steps: bool = False) -> str:
    rows, hidden = [], 0
    for step in combo.line:
        if not all_steps and step.label in HIDDEN_STEPS:
            hidden += 1
            continue
        rows.append(f"  {len(rows) + 1:>3}. {step.label}")
    if hidden:
        rows.append(f"  ({hidden} Engine-Schritte ohne Bedeutung ausgeblendet)")
    return "\n".join(rows)


def combo_text(combo: ComboRecord, name: Namer, *, all_steps: bool = False, runs: list[dict] | None = None) -> str:
    title = f"Combo #{combo.id}" if combo.id is not None else "Combo aus dieser Suche"
    me, opp = combo.end_board["players"]
    raw = {k: v for k, v in combo.benefit.items() if v}
    parts = [
        f"{title}   Signatur {combo.signature[:16]}",
        "",
        f"Benötigte Karten ({combo.card_count}):",
        f"  Hand:   {names(combo.hand, name)}",
        f"  Deck:   {names(combo.deck, name)}",
        f"  Extra:  {names(combo.extra, name)}",
        *([f"  Schon im Friedhof: {names(combo.grave, name)}"] if combo.grave else []),
        *([f"  Schon verbannt:    {names(combo.banished, name)}"] if combo.banished else []),
        *([f"  Schon auf dem Feld: {names(combo.field, name)}"] if combo.field else []),
        "",
        f"Nutzen:  {', '.join(combo.positive) or '-'}",
        f"  Werte: {raw or '-'}",
        f"Validiert: {'ja' if combo.validated else 'nein'}   Zug beendet: {'ja' if combo.turn_ended else 'nein'}",
        "",
        "Endfeld",
        f"  Eigenes Feld (LP {me['lp']}):",
        _player(me, name),
        f"  Gegner (LP {opp['lp']}):",
        _player(opp, name),
        "",
        f"Linie ({len(combo.line)} Engine-Schritte):",
        line_text(combo, all_steps),
        "",
        "Startaufbau, in dem die Linie abläuft:",
        setup_text(combo.setup, name),
    ]
    if runs:
        parts += ["", f"Gefunden in {len(runs)} Lauf/Läufen:"]
        parts += [
            f"  Lauf #{r['run_id']}  Stufe {r['tier'] if r['tier'] is not None else '-'}  "
            f"vs {r['opponent_board']}  {'vollständig' if r['complete'] else 'unvollständig'}"
            for r in runs
        ]
    return "\n".join(parts)


def run_text(run: dict, name: Namer) -> str:
    setup = StartSetup.from_dict(run["setup"])
    lim = run["limits"]
    return "\n".join([
        f"Lauf #{run['id']}   {run['finished_at']}",
        f"Kandidat: {run['candidate'] or 'manuelle Suche'}   Stufe: {run['tier'] if run['tier'] is not None else '-'}",
        f"Status: {'vollständig' if run['complete'] else 'UNVOLLSTÄNDIG'}",
        f"Knoten {run['nodes']}, Replays {run['replays']}, {run['elapsed_s']:.2f}s",
        f"Combos gefunden {run['combos_found']}, neu in Database 1: {run['combos_new']}",
        "",
        f"Abbruchgründe: {reasons(run['incomplete'])}",
        *[f"  -> {INCOMPLETE_HINTS[k]}" for k in run["incomplete"] if k in INCOMPLETE_HINTS],
        f"Beschnitten:   {reasons(run['pruned'])}",
        "",
        "Startaufbau:",
        setup_text(setup, name),
        "",
        "Suchgrenzen dieses Laufs:",
        "  " + ", ".join(f"{k}={v}" for k, v in lim.items()),
    ])


def stats_text(stats: dict) -> str:
    runs = stats["runs"]
    share = f"{100 * stats['runs_complete'] / runs:.0f} %" if runs else "-"
    lines = [
        f"{stats['combos']} Combos in Database 1 aus {runs} Läufen "
        f"({stats['runs_complete']} vollständig = {share}); "
        f"Ø {stats['avg_elapsed_s']:.1f}s und {stats['avg_nodes']:.0f} Knoten pro Lauf",
        "Nach Kartenanzahl: " + (", ".join(f"{k} Karten: {v}" for k, v in stats["by_card_count"].items()) or "-"),
        "Nach Nutzen: " + (", ".join(f"{k}: {v}" for k, v in stats["by_benefit"].items()) or "-"),
    ]
    return "\n".join(lines)


def tuning_text(stats: dict) -> str:
    """Why searches stopped early, with the setting to change - the core of fine-tuning."""
    runs = stats["runs"]
    if not runs:
        return "Noch keine Läufe gespeichert."
    incomplete = runs - stats["runs_complete"]
    lines = [f"{incomplete} von {runs} Läufen unvollständig.", "", "Abbruchgründe (betroffene Läufe / Summe):"]
    for reason, (n, total) in stats["incomplete"].items():
        hint = INCOMPLETE_HINTS.get(reason, "")
        lines.append(f"  {reason:<34} {n:>7} / {total:<9} {('-> ' + hint) if hint else ''}")
    if not stats["incomplete"]:
        lines.append("  keine")
    lines += ["", "Beschneidung (betroffene Läufe / Summe):"]
    lines += [f"  {rule:<34} {n:>7} / {total}" for rule, (n, total) in stats["pruned"].items()] or ["  keine"]
    return "\n".join(lines)


def combo_export(combo: ComboRecord, name: Namer) -> dict:
    """JSON-friendly view of a combo, with card names next to the codes."""
    named = lambda codes: [{"code": c, "name": name(c)} for c in codes]
    return {
        "id": combo.id,
        "signature": combo.signature,
        "card_count": combo.card_count,
        "hand": named(combo.hand),
        "deck": named(combo.deck),
        "extra": named(combo.extra),
        "positive": list(combo.positive),
        "benefit": combo.benefit,
        "turn_ended": combo.turn_ended,
        "validated": combo.validated,
        "line": [s.label for s in combo.line if s.label not in HIDDEN_STEPS],
        "end_board": combo.end_board,
        "setup": combo.setup.to_dict(),
    }


# ---- Stufe 2: evaluation profiles -------------------------------------------------------------
AXIS_ORDER = ("strength", "opening", "breaking", "late", "outcome", "cost", "consistency", "robustness", "genericness",
              "compatibility", "line")
AXIS_LABELS = {
    "strength": "Spielstärke", "opening": "Spielbeginn", "breaking": "Durchbrechen", "late": "Späteres Spiel",
    "outcome": "Ergebnis", "cost": "Einsatz", "consistency": "Konsistenz", "robustness": "Robustheit",
    "genericness": "Generizität", "compatibility": "Kompatibilität", "line": "Linienfakten",
}
EDGE_LABELS = {"shared_cards": "gemeinsame Karten", "archetype": "gemeinsamer Archetyp", "lock_conflict": "Sperrkonflikt"}


def axis_label(axis: str) -> str:
    return AXIS_LABELS.get(axis, axis)


def score(value) -> str:
    return "-" if value is None else f"{value:.0f}"


def profile_text(combo: ComboRecord | None, profile: dict, name: Namer, *, edges: list[dict] | None = None,
                 robustness: list[tuple] | None = None, short: Callable[[str], str] | None = None,
                 fit: float | None = None, deck: str = "") -> str:
    """Evaluation profile of one combo: every axis with score and reason, handtraps, partners, the line."""
    title = "Combo" if combo is None else (f"Combo #{combo.id}" if combo.id is not None else "Combo")
    parts = [f"{title}: {names(combo.cards, name) if combo else '?'}", ""]
    if fit is not None:
        parts += [f"Passung zum Deckprofil {deck}: {fit:.0f}", ""]
    parts.append("Bewertungsprofil (Wert 0-100 = besser als so viele Prozent der Combos):")
    for axis in (*AXIS_ORDER, *sorted(set(profile) - set(AXIS_ORDER))):
        if axis in profile:
            a = profile[axis]
            parts.append(f"  {axis_label(axis):<16}{score(a.score):>4}   {a.explanation}")
    if robustness:
        parts += ["", "Handtraps (garantiertes Ergebnis nach der besten Antwort):"]
        for handtrap, status, value, data in robustness:
            crit = data.get("critical")
            where = f"   kritisch nach: {crit['after']}" if crit else ""
            state = "" if status == "complete" else f"   UNVOLLSTÄNDIG {reasons(data.get('incomplete', {}))}"
            val = "-" if value is None else f"{value:.2f}"
            parts.append(f"  {handtrap:<32}{val:>8}{where}{state}")
    if edges:
        parts += ["", "Partner und Konflikte (gespeicherte Kanten, stärkste zuerst):"]
        for e in edges:
            d = e["data"]
            if e["kind"] == "shared_cards":
                detail = names(d["cards"], name)
            elif e["kind"] == "archetype":
                detail = d["archetype"]
            else:
                who = "sperrt" if e["direction"] == "out" else "wird gesperrt von"
                detail = f"{who}: " + "; ".join(names(f["cards"], name) for f in d["conflicts"])
            other = short(e["other"]) if short else e["other"][:12]
            parts.append(f"  {EDGE_LABELS.get(e['kind'], e['kind']):<22}{e['strength']:.2f}  {other}")
            parts.append(f"      {detail}")
    if combo is not None:
        me = combo.end_board["players"][0]
        parts += ["", "Benötigte Karten:", f"  Hand:   {names(combo.hand, name)}", f"  Deck:   {names(combo.deck, name)}",
                  f"  Extra:  {names(combo.extra, name)}",
                  *([f"  Schon im Friedhof / verbannt / auf dem Feld: {names(combo.resources, name)}"] if combo.resources else []),
                  "", "Endfeld:", _player(me, name), "", "Linie:", line_text(combo)]
    return "\n".join(parts)


def parse_pairs(text: str, what: str) -> dict[str, float]:
    """'strength=70, outcome.disruptions=1' -> {...}; raises ValueError with a readable message."""
    out = {}
    for item in text.replace(";", ",").replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        try:
            if not sep:
                raise ValueError
            out[key.strip()] = float(value.strip().replace(",", "."))
        except ValueError:
            raise ValueError(f"{what}: '{item}' ist nicht in der Form Name=Zahl") from None
    return out


def pairs_text(d: dict[str, float]) -> str:
    return ", ".join(f"{k}={v:g}" for k, v in d.items())


def words(text: str) -> tuple[str, ...]:
    """Comma separated names -> tuple (archetypes, types, attributes of a deck profile)."""
    return tuple(w.strip() for w in text.split(",") if w.strip())
