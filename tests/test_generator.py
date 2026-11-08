"""The Combo Generator module through its public interface."""

import textwrap

import pytest

from deepyugioh.cards.graph import CardGraph
from deepyugioh.contracts import CardPool, ComboReader, ComboSink, EnablerRole, InteractionGraph, StartSetup
from deepyugioh.generator import ComboGenerator, GeneratorConfig, GraphCandidateSource, JobStore, SearchLimits
from deepyugioh.repository.combo_db import ComboRepository


@pytest.fixture(scope="module")
def graph(store):
    g = CardGraph()
    if g.version() == "unbuilt":
        pytest.skip("interaction graph missing - run `deepyugioh import-cards`")
    return g


def test_implementations_satisfy_contracts(store, graph, tmp_path):
    repo = ComboRepository(tmp_path / "c.sqlite")
    assert isinstance(store, CardPool)
    assert isinstance(graph, InteractionGraph)
    assert isinstance(repo, ComboSink) and isinstance(repo, ComboReader)


def test_graph_finds_helpers(store, graph, code):
    assert graph.enablers[EnablerRole.SEND_GY_FROM_DECK] == code("Foolish Burial")
    assert graph.enablers[EnablerRole.SPSUMMON_FROM_GY] == code("Monster Reborn")
    assert EnablerRole.SEND_GY_FROM_DECK in graph.needs(code("Junk Synchron"))


def test_start_setup_for_junk_synchron(store, graph, code):
    cfg = GeneratorConfig()
    src = GraphCandidateSource(graph, store, cfg.candidates, cfg.boards(store))
    hand, deck, extra, truncated, helpers = src.build((code("Junk Synchron"),))
    assert code("Foolish Burial") in hand          # Junk Synchron revives from the GY
    assert code("Junk Synchron") in deck            # second copy to send / search
    assert code("Junk Warrior") in extra            # names Junk Synchron as material
    assert len(extra) <= cfg.candidates.max_extra


def test_extra_deck_core_gets_its_materials(store, graph, code):
    cfg = GeneratorConfig()
    src = GraphCandidateSource(graph, store, cfg.candidates, cfg.boards(store))
    hand, deck, extra, _, _ = src.build((code("Junk Warrior"),))
    assert code("Junk Warrior") in extra and code("Junk Synchron") in hand


def test_search_stores_through_sink(store, code, tmp_path):
    repo = ComboRepository(tmp_path / "combos.sqlite")
    gen = ComboGenerator(store, repo)
    setup = StartSetup(hand=(code("Foolish Burial"), code("Monster Reborn")), deck=(code("Blue-Eyes White Dragon"),))
    first = gen.search(setup)
    second = gen.search(setup)
    assert (first.new_combos, second.new_combos) == (1, 0)
    [rec] = list(repo.iter_combos(card=code("Monster Reborn")))
    assert rec.validated and rec.card_count == 3 and rec.setup == setup
    assert repo.get(rec.id) == rec
    assert len(repo.runs_for(rec.id)) == 2


def test_global_run_is_resumable(store, graph, tmp_path):
    cfg = GeneratorConfig().with_(limits=SearchLimits(time_limit=10, max_nodes=2000))
    repo = ComboRepository(tmp_path / "combos.sqlite")
    state = JobStore(tmp_path / "gen.sqlite")
    gen = ComboGenerator(store, repo, cfg, graph=graph, state=state)
    a = gen.run(tiers=[1], max_jobs=3)
    b = gen.run(tiers=[1], max_jobs=3)
    assert a.jobs == b.jobs == 3
    cov = gen.coverage()[1]
    assert cov["complete"] + cov["incomplete"] + cov["error"] == 6
    assert cov["cursor"] == 6
    assert cov["total"] == len(graph.starters()) * len(cfg.candidates.opponent_boards)


def test_config_from_toml(tmp_path, store):
    path = tmp_path / "gen.toml"
    path.write_text(textwrap.dedent("""
        workers = 4
        [limits]
        max_cards = 4
        [candidates]
        tiers = [1, 2]
        opponent_boards = ["wall"]
        [opponent_boards.wall]
        cards = [{ card = "Mystical Elf", zone = "mzone", position = "faceup_defense" }]
    """))
    cfg = GeneratorConfig.load(path)
    assert cfg.workers == 4 and cfg.limits.max_cards == 4 and cfg.candidates.tiers == (1, 2)
    board = cfg.boards(store)["wall"]
    assert board.cards[0].position == "faceup_defense"
