"""Overall strength and situations (Stufe 2, step 2.4) - weighted combinations of stored axes.

A *scoring* is a named weighting of components:

    component "outcome"              the 0-100 score of an axis
    component "outcome.disruptions"  a raw value of an axis, ranked as a 0-100 percentile over all combos

The result is stored as a derived axis in Database 2 (same table, same 0-100
scores), with the weights it was computed with in its raw values and version.
Because only stored axis results are combined, other weights never require a
new analysis - `rescore` takes seconds.

Components that do not exist yet (robustness until step 2.8) are left out and
the remaining weights are scaled up; the result says so. The default weights
are reasoned starting values, to be calibrated against the simulator later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

DEFAULT_SCORINGS: dict[str, dict] = {
    "strength": {
        "label": "Allgemeine Spielstärke",
        "weights": {"outcome": 0.45, "consistency": 0.25, "cost": 0.15, "robustness": 0.15},
    },
    "opening": {
        "label": "Spielbeginn (als Erster)",
        "weights": {"outcome.disruptions": 0.35, "outcome": 0.20, "consistency.family_first": 0.25, "cost": 0.10,
                    "robustness": 0.10},
    },
    "breaking": {
        "label": "Durchbrechen (als Zweiter)",
        "weights": {"outcome.opp_losses": 0.45, "consistency.family_second": 0.25, "cost": 0.15, "outcome": 0.15},
    },
}
BASE_AXES = ("outcome", "cost", "consistency", "genericness", "robustness")


@dataclass(frozen=True)
class Scoring:
    name: str
    label: str
    weights: dict[str, float]

    @property
    def version(self) -> str:
        payload = json.dumps([2, sorted(self.weights.items())])   # 2: raw components start at 0
        return "w-" + hashlib.sha256(payload.encode()).hexdigest()[:10]


def scorings_from_dict(d: dict | None) -> list[Scoring]:
    """Defaults, overridden per scoring by `d` ({name: {label?, weights?}}); new names add scorings."""
    merged: dict[str, dict] = {k: {"label": v["label"], "weights": dict(v["weights"])} for k, v in DEFAULT_SCORINGS.items()}
    for name, spec in (d or {}).items():
        if name in BASE_AXES:
            raise ValueError(f"[analysis.scoring.{name}]: name of a base axis")
        entry = merged.setdefault(name, {"label": name, "weights": {}})
        if "label" in spec:
            entry["label"] = str(spec["label"])
        if "weights" in spec:
            entry["weights"] = {str(k): float(v) for k, v in spec["weights"].items()}
    out = []
    for name, spec in merged.items():
        w = spec["weights"]
        for comp, val in w.items():
            axis = comp.split(".", 1)[0]
            if axis not in BASE_AXES:
                raise ValueError(f"[analysis.scoring.{name}]: unknown axis in component {comp!r}")
            if val < 0:
                raise ValueError(f"[analysis.scoring.{name}]: weight of {comp!r} is negative")
        if not any(v > 0 for v in w.values()):
            raise ValueError(f"[analysis.scoring.{name}]: needs at least one positive weight")
        out.append(Scoring(name, spec["label"], w))
    return out


def percentiles(values: dict[str, float], floor_minimum: bool = False) -> dict[str, float]:
    """0-100 percentile per key; ties share the mean rank (same rule as the axis scores).

    floor_minimum: the smallest value gets 0 - for raw counts where most combos have nothing
    (no disruption is worth nothing, not the median)."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out, i = {}, 0
    lowest = items[0][1] if items else None
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        score = 0.0 if floor_minimum and items[i][1] == lowest else 100.0 * (i + (j - i + 1) / 2) / n
        for k in range(i, j + 1):
            out[items[k][0]] = round(score, 3)
        i = j + 1
    return out


_COMPONENT_LABELS = {
    "outcome": "Ergebnis", "cost": "Einsatz", "consistency": "Konsistenz", "genericness": "Generizität",
    "robustness": "Robustheit", "outcome.disruptions": "Störungen", "outcome.opp_losses": "Verluste des Gegners",
    "consistency.family_first": "Konsistenz als Erster", "consistency.family_second": "Konsistenz als Zweiter",
    "outcome.negates": "Negates", "outcome.card_advantage": "Kartenvorteil",
}


def component_label(comp: str) -> str:
    return _COMPONENT_LABELS.get(comp, comp)


class Rescorer:
    """Computes every scoring for every analysed combo and stores it as a derived axis."""

    def __init__(self, repo, scorings: list[Scoring]):
        self.repo = repo
        self.scorings = scorings

    def run(self) -> dict:
        needed = {c for s in self.scorings for c in s.weights}
        axes = {c.split(".", 1)[0] for c in needed}
        rows = {a: {sig: (raw, score) for sig, raw, score in self.repo.axis_rows(a)} for a in axes}
        component: dict[str, dict[str, float]] = {}
        for comp in needed:
            axis, _, key = comp.partition(".")
            data = rows.get(axis, {})
            if not data:
                continue
            if not key:
                component[comp] = {sig: score for sig, (_, score) in data.items() if score is not None}
            else:
                vals = {sig: float(raw[key]) for sig, (raw, _) in data.items() if key in raw}
                if vals:
                    component[comp] = percentiles(vals, floor_minimum=True)
        signatures = set().union(*(set(v) for v in rows.values())) if rows else set()
        summary = {}
        analysis_id = self.repo.start({s.name: s.version for s in self.scorings}, {"scorings": {
            s.name: {"label": s.label, "weights": s.weights} for s in self.scorings}}, "rescore", len(signatures))
        written = 0
        for sc in self.scorings:
            present = {c: w for c, w in sc.weights.items() if c in component and w > 0}
            missing = sorted(c for c, w in sc.weights.items() if c not in component and w > 0)
            total = sum(present.values())
            out = []
            for sig in signatures:
                parts = {c: component[c].get(sig) for c in present}
                used = {c: v for c, v in parts.items() if v is not None}
                wsum = sum(present[c] for c in used)
                if not wsum:
                    continue
                value = sum(present[c] * v for c, v in used.items()) / wsum
                raw = {"components": {c: round(v, 2) for c, v in used.items()},
                       "weights": {c: round(present[c] / total, 4) for c in used}, "missing": missing,
                       "label": sc.label}
                out.append((sig, sc.name, sc.version, raw, round(value, 4), self._explain(sc, value, used, missing)))
            self.repo.replace_axis(sc.name, out, analysis_id)
            self.repo.normalise(sc.name)
            written += len(out)
            summary[sc.name] = {"combos": len(out), "missing": missing, "version": sc.version}
        self.repo.finish(analysis_id, written)
        return summary

    @staticmethod
    def _explain(sc: Scoring, value: float, used: dict, missing: list[str]) -> str:
        best = sorted(used.items(), key=lambda kv: -sc.weights[kv[0]])
        parts = ", ".join(f"{component_label(c)} {v:.0f}" for c, v in best)
        text = f"{sc.label} {value:.0f}: {parts}"
        if missing:
            text += f" (vorläufig, ohne {', '.join(component_label(m) for m in missing)})"
        return text
