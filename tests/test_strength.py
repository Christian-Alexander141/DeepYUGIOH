"""Stufe 2, step 2.4: overall strength and situations from stored axes, configurable weights."""

import pytest

from deepyugioh.analysis import AnalysisRepository, Rescorer, scorings_from_dict
from deepyugioh.analysis.strength import percentiles


def test_scorings_merge_defaults_and_validate():
    names = {s.name for s in scorings_from_dict({})}
    assert {"strength", "opening", "breaking"} <= names
    own = {s.name: s for s in scorings_from_dict({"strength": {"weights": {"outcome": 1.0}},
                                                   "grind": {"label": "Grind", "weights": {"outcome.card_advantage": 1}}})}
    assert own["strength"].weights == {"outcome": 1.0} and own["grind"].label == "Grind"
    assert own["strength"].version != scorings_from_dict({})[0].version   # other weights, other version
    for bad in ({"strength": {"weights": {"nope": 1}}}, {"strength": {"weights": {"outcome": -1}}},
                {"strength": {"weights": {"outcome": 0}}}, {"outcome": {"weights": {"cost": 1}}}):
        with pytest.raises(ValueError):
            scorings_from_dict(bad)


def test_percentiles_floor_the_minimum():
    vals = {"a": 0, "b": 0, "c": 0, "d": 2, "e": 5}
    assert percentiles(vals)["a"] == 30.0                       # mean rank of the ties
    floored = percentiles(vals, floor_minimum=True)
    assert floored["a"] == 0.0 and floored["e"] == 90.0 and floored["d"] == 70.0


def _fill(repo):
    aid = repo.start({}, {}, "test", 3)
    rows = []
    for sig, out, cost, cons, dis in (("x", 10.0, -2.0, 0.6, 2), ("y", 5.0, -8.0, 0.3, 0), ("z", 1.0, -5.0, 0.1, 0)):
        rows += [(sig, "outcome", "1", {"disruptions": dis, "opp_losses": 0}, out, ""),
                 (sig, "cost", "1", {}, cost, ""),
                 (sig, "consistency", "1", {"family_first": cons, "family_second": cons + 0.1}, cons, "")]
    repo.write(rows, aid)
    for axis in ("outcome", "cost", "consistency"):
        repo.normalise(axis)


def test_rescore_combines_stored_axes_and_reports_missing(tmp_path):
    repo = AnalysisRepository(tmp_path / "a.sqlite")
    _fill(repo)
    summary = Rescorer(repo, scorings_from_dict({})).run()
    assert summary["strength"]["missing"] == ["robustness"] and summary["breaking"]["missing"] == []
    best = repo.top("strength", limit=3)
    assert [s for s, _ in best] == ["x", "y", "z"]
    x = repo.profile("x")["strength"]
    assert "ohne Robustheit" in x.explanation and abs(sum(x.raw["weights"].values()) - 1) < 1e-6
    assert repo.profile("y")["opening"].raw["components"]["outcome.disruptions"] == 0.0   # no disruption -> 0
    # other weights: only the derived axis changes, nothing is analysed again
    Rescorer(repo, scorings_from_dict({"strength": {"weights": {"cost": 1.0}}})).run()
    assert [s for s, _ in repo.top("strength", limit=3)] == ["x", "z", "y"]
    assert repo.profile("x")["outcome"].version == "1"


def test_weights_are_stored_in_the_settings(tmp_path, monkeypatch):
    from deepyugioh import settings

    monkeypatch.setenv(settings.SETTINGS_ENV, str(tmp_path / "s.toml"))
    settings.set_analysis_weights("strength", {"outcome": 2.0, "cost": 1.0})
    assert {s.name: s for s in settings.analysis_scorings()}["strength"].weights == {"outcome": 2.0, "cost": 1.0}
    with pytest.raises(ValueError):
        settings.set_analysis_weights("strength", {"unknown": 1.0})
    settings.reset_analysis_weights("strength")
    assert {s.name: s for s in settings.analysis_scorings()}["strength"].weights["outcome"] == 0.45
