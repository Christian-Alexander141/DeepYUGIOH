"""Persistent settings, test mode switch and the test GUI helpers (no display needed)."""

import pytest

from deepyugioh import cli, settings
from deepyugioh.contracts import StartSetup
from deepyugioh.generator import CandidateConfig, ComboGenerator, GeneratorConfig, SearchLimits
from deepyugioh.gui import format as fmt
from deepyugioh.repository.combo_db import ComboRepository


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "deepyugioh.toml"
    monkeypatch.setenv(settings.SETTINGS_ENV, str(path))
    monkeypatch.delenv(settings.TEST_MODE_ENV, raising=False)
    return path


def test_generator_config_roundtrip_keeps_test_mode(settings_file):
    settings.set_test_mode(True)
    cfg = GeneratorConfig().with_(
        limits=SearchLimits(max_cards=7, min_cards=3, time_limit=12.5, allow_mset=True),
        candidates=CandidateConfig(tiers=(1, 2), opponent_boards=("standard",), max_extra=10),
        workers=3,
    )
    settings.save_generator_config(cfg)
    assert settings.generator_config() == cfg
    assert settings.test_mode()  # saving the generator part keeps the switch


def test_missing_settings_file_gives_defaults(settings_file):
    assert not settings_file.exists()
    assert settings.generator_config() == GeneratorConfig()
    assert not settings.test_mode()


def test_env_overrides_test_mode(settings_file, monkeypatch):
    settings.set_test_mode(False)
    monkeypatch.setenv(settings.TEST_MODE_ENV, "1")
    assert settings.test_mode()
    monkeypatch.setenv(settings.TEST_MODE_ENV, "off")
    settings.set_test_mode(True)
    assert not settings.test_mode()


def test_check_rejects_inconsistent_config():
    with pytest.raises(ValueError, match="max_cards"):
        GeneratorConfig().with_(limits=SearchLimits(min_cards=6, max_cards=5)).check()
    with pytest.raises(ValueError, match="unknown opponent boards"):
        GeneratorConfig().with_(candidates=CandidateConfig(opponent_boards=("nope",))).check()
    assert GeneratorConfig().check() == GeneratorConfig()


def test_gui_only_in_test_mode(settings_file, capsys):
    assert cli.main(["gui"]) == 2
    assert "test mode" in capsys.readouterr().err
    assert cli.main(["testmode", "on"]) == 0
    assert settings.test_mode()
    cli.main(["testmode", "off"])
    assert not settings.test_mode()


def test_cli_uses_saved_settings(settings_file):
    settings.save_generator_config(GeneratorConfig().with_(limits=SearchLimits(max_cards=9)))
    args = cli.argparse.Namespace(config=None, max_cards=None, min_cards=None, max_depth=None,
                                  max_nodes=None, time_limit=None, allow_set=False)
    assert cli._config(args).limits.max_cards == 9


def test_parse_card_list():
    assert fmt.parse_card_list("2x Ash Blossom\n\n  Foolish Burial \n83764718\n3 × Kuriboh") == [
        "Ash Blossom", "Ash Blossom", "Foolish Burial", "83764718", "Kuriboh", "Kuriboh", "Kuriboh",
    ]
    names = {1: "A", 2: "B"}.get
    assert fmt.parse_card_list(fmt.card_list_text((1, 2, 1), names)) == ["A", "A", "B"]


def test_card_search(store):
    hits = store.search("monster reborn")
    assert hits[0].name == "Monster Reborn"
    assert store.search("100%") == []  # LIKE wildcards are escaped


def test_run_inspection_and_stop(store, code, tmp_path):
    repo = ComboRepository(tmp_path / "combos.sqlite")
    gen = ComboGenerator(store, repo, GeneratorConfig().with_(limits=SearchLimits(max_nodes=1)))
    setup = StartSetup(hand=(code("Foolish Burial"), code("Monster Reborn")), deck=(code("Blue-Eyes White Dragon"),))
    gen.search(setup)
    [run] = repo.recent_runs(incomplete_only=True)
    assert run["incomplete"] == {"max_nodes": 1} and StartSetup.from_dict(run["setup"]) == setup
    stats = repo.stats()
    assert stats["runs"] == 1 and stats["runs_complete"] == 0
    assert stats["incomplete"]["max_nodes"] == (1, 1)
    assert "Max. Knoten" in fmt.tuning_text(stats)
    assert "Foolish Burial" in fmt.run_text(run, store.name)


def test_global_run_stops_on_request(store, tmp_path):
    from deepyugioh.cards.graph import CardGraph
    from deepyugioh.generator import JobStore

    graph = CardGraph()
    if graph.version() == "unbuilt":
        pytest.skip("interaction graph missing")
    gen = ComboGenerator(store, ComboRepository(tmp_path / "c.sqlite"), GeneratorConfig(), graph=graph,
                         state=JobStore(tmp_path / "g.sqlite"))
    assert gen.run(tiers=[1], should_stop=lambda: True).jobs == 0


def test_gui_theme_is_persistent_and_keeps_generator_settings(settings_file):
    assert settings.gui_theme() == "dark"
    settings.save_generator_config(GeneratorConfig().with_(workers=5))
    settings.set_gui_theme("light")
    assert settings.gui_theme() == "light"
    assert settings.generator_config().workers == 5   # GUI section does not disturb the generator part
    settings.save_generator_config(GeneratorConfig())
    assert settings.gui_theme() == "light"            # and saving the generator keeps the GUI section
    with pytest.raises(ValueError):
        settings.set_gui_theme("neon")


class _ListSource:
    """Fixed candidate list for tier 1 (no interaction graph needed)."""

    def __init__(self, setups):
        from deepyugioh.contracts import Candidate

        self.items = [Candidate(s, 1) for s in setups]

    def version(self):
        return "test"

    def candidates(self, tier):
        return iter(self.items if tier == 1 else [])

    def tier_size(self, tier):
        return len(self.items) if tier == 1 else 0


@pytest.fixture
def target_gen(store, code, tmp_path):
    from deepyugioh.generator import JobStore

    setups = [
        StartSetup(hand=(code("Fissure"),)),                                                     # no combo
        StartSetup(hand=(code("Foolish Burial"), code("Monster Reborn")), deck=(code("Blue-Eyes White Dragon"),)),
        StartSetup(hand=(code("Pot of Greed"),)),                                                # no combo
    ]
    cfg = GeneratorConfig().with_(limits=SearchLimits(time_limit=10, max_nodes=2000))
    return lambda name: ComboGenerator(store, ComboRepository(tmp_path / f"{name}.sqlite"), cfg,
                                       source=_ListSource(setups), state=JobStore(tmp_path / f"{name}-jobs.sqlite"))


def test_global_run_ends_at_target(target_gen):
    report = target_gen("any").run(tiers=[1], target_combos=1)
    assert (report.jobs, report.target_hits, report.target_reached, report.stop_reason) == (2, 1, True, "target")
    counted = target_gen("field").run(tiers=[1], target_combos=1, target_benefit="field_delta")
    assert counted.target_reached and counted.jobs == 2


def test_global_run_target_not_reached_when_candidates_run_out(target_gen):
    report = target_gen("wins").run(tiers=[1], target_combos=5, target_benefit="wins")
    assert report.jobs == 3 and report.combos_new == 1     # the combo is stored, but it does not win
    assert (report.target_hits, report.target_reached, report.stop_reason) == (0, False, "exhausted")
    with pytest.raises(ValueError, match="target_benefit"):
        target_gen("bad").run(tiers=[1], target_combos=1, target_benefit="nope")


def test_run_options_are_persistent(settings_file):
    assert settings.run_options() == settings.RUN_DEFAULTS
    settings.save_generator_config(GeneratorConfig().with_(workers=2))
    settings.set_gui_theme("light")
    settings.set_run_options({"mode": "target", "target": 250, "benefit": "wins"})
    opts = settings.run_options()
    assert (opts["mode"], opts["target"], opts["benefit"]) == ("target", 250, "wins")
    assert settings.gui_theme() == "light" and settings.generator_config().workers == 2
    with pytest.raises(ValueError):
        settings.set_run_options({"mode": "forever"})
    assert fmt.target_text(100, "wins") == "100 neue Combos mit Sieg (Duell gewonnen)"
