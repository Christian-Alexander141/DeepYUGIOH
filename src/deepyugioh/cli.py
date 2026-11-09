"""Command line interface - a thin layer over the module interfaces.

    deepyugioh import-cards                 Steps 1-3: cards, effects, interaction graph
    deepyugioh plan                         candidate tiers and coverage of the global search
    deepyugioh generate --tiers 1 2         global, resumable search into Database 1
    deepyugioh search --hand ... [--deck ...] [--extra ...] [--opponent standard]
    deepyugioh combos [--card NAME] [--positive opp_field_loss] [--lines]
    deepyugioh testmode [on|off]            show or switch test mode
    deepyugioh gui                          test GUI for tuning the generator (test mode only)

Without --config the generator settings come from the local settings file
(deepyugioh.toml, written by the test GUI); missing keys use the defaults.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

_HIDDEN_STEPS = {"no chain", "place", "position", "keep order"}


def _config(args):
    from deepyugioh import settings
    from deepyugioh.generator import GeneratorConfig

    if getattr(args, "config", None):
        cfg = GeneratorConfig.load(Path(args.config))
    else:
        cfg = settings.generator_config()  # defaults if the settings file does not exist
    if getattr(args, "workers", None):
        cfg = cfg.with_(workers=args.workers)
    overrides = {
        k: getattr(args, k) for k in ("max_cards", "min_cards", "max_depth", "max_nodes", "time_limit")
        if getattr(args, k, None) is not None
    }
    if getattr(args, "allow_set", False):
        overrides.update(allow_mset=True, allow_sset=True)
    if overrides:
        cfg = cfg.with_(limits=replace(cfg.limits, **overrides))
    return cfg


def _print_line(line, indent="     "):
    for step in line:
        if step.label not in _HIDDEN_STEPS:
            print(f"{indent}- {step.label}")


def cmd_import(args) -> int:
    from deepyugioh.cards.graph import CardGraph
    from deepyugioh.cards.importer import import_cards

    stats = import_cards()
    print(f"imported {stats.cards} cards ({stats.alt_arts} alternative artworks), "
          f"{stats.with_script} scripts, {stats.effects} effects")
    g = CardGraph().build()
    print(f"interaction graph {g['version']}: {g['starters']} starters, interactions {g['interactions']}")
    print(f"enablers: {g['enablers']}")
    return 0


def _generator(args, cfg):
    from deepyugioh.cards.graph import CardGraph
    from deepyugioh.cards.store import CardStore
    from deepyugioh.generator import ComboGenerator
    from deepyugioh.repository.combo_db import ComboRepository

    graph = CardGraph()
    if graph.version() == "unbuilt":
        raise FileNotFoundError("interaction graph missing - run `deepyugioh import-cards`")
    return ComboGenerator(CardStore(), ComboRepository(), cfg, graph=graph, pool_factory=CardStore)


def cmd_plan(args) -> int:
    from deepyugioh.generator import TIERS

    gen = _generator(args, _config(args))
    print(f"candidate source {gen.source.version()}, opponent boards: {', '.join(gen.config.candidates.opponent_boards)}")
    print(f"{'tier':<5}{'candidates':>12}{'done':>10}{'complete':>10}{'incompl.':>10}{'errors':>8}{'combos':>9}  description")
    for tier, row in gen.coverage().items():
        total = row["total"]
        done = row["complete"] + row["incomplete"] + row["error"]
        print(f"{tier:<5}{(total if total is not None else '?'):>12}{done:>10}{row['complete']:>10}"
              f"{row['incomplete']:>10}{row['error']:>8}{row['combos_new']:>9}  {TIERS.get(tier, '')}")
    return 0


class _Printer:
    def __init__(self, every: int):
        self.every, self.n = every, 0

    def on_job(self, job):
        self.n += 1
        if job.status == "error" or job.combos_new or self.n % self.every == 0:
            extra = f" ERROR {job.error}" if job.error else ""
            print(f"[tier {job.tier} #{job.position}] {job.status:<10} {job.elapsed_s:6.1f}s "
                  f"combos {job.combos_found} (new {job.combos_new}){extra}", flush=True)


def cmd_generate(args) -> int:
    gen = _generator(args, _config(args))
    if args.benefit and args.target is None:
        raise ValueError("--benefit only applies together with --target")
    report = gen.run(tiers=args.tiers, max_jobs=args.max_jobs, time_budget=args.time_budget,
                     progress=_Printer(args.report_every), target_combos=args.target, target_benefit=args.benefit)
    print(f"\n{report.jobs} jobs in {report.elapsed_s:.0f}s: {report.complete} complete, "
          f"{report.incomplete} incomplete, {report.errors} errors, {report.skipped} already done; "
          f"{report.combos_found} combos found, {report.combos_new} new in Database 1")
    if args.target is not None:
        what = f"new combos with {args.benefit}" if args.benefit else "new combos"
        state = "reached" if report.target_reached else f"not reached ({report.stop_reason})"
        print(f"target: {report.target_hits}/{args.target} {what} - {state}")
    return 0


def cmd_search(args) -> int:
    from deepyugioh.cards.store import CardStore
    from deepyugioh.contracts import StartSetup
    from deepyugioh.generator import ComboGenerator
    from deepyugioh.repository.combo_db import ComboRepository

    cfg = _config(args)
    store = CardStore()
    boards = cfg.boards(store)
    if args.opponent not in boards:
        raise KeyError(f"unknown opponent board {args.opponent!r} (known: {', '.join(boards)})")
    codes = lambda refs: tuple(store.resolve(r).code for r in refs)
    setup = StartSetup(
        hand=codes(args.hand), deck=codes(args.deck), extra=codes(args.extra),
        opponent=boards[args.opponent], branch_zones=args.branch_zones,
    )
    repo = None if args.no_store else ComboRepository()
    gen = ComboGenerator(store, repo, cfg)
    rep = gen.search(setup)
    run = rep.run
    status = "complete" if run.complete else f"INCOMPLETE {run.incomplete}"
    print(f"{run.nodes} nodes, {run.replays} replays, {run.elapsed_s:.2f}s - {status}")
    if run.pruned:
        print(f"pruned: {run.pruned}")
    print(f"{run.combos_found} combos found, {len(rep.combos)} validated by replay")
    for i, c in enumerate(rep.combos[: args.show]):
        names = ", ".join(store.name(x) for x in c.cards)
        own = c.end_board["players"][0]
        field = ", ".join(store.name(x["code"]) for x in own["mzone"] + own["szone"])
        print(f"\n#{i + 1} [{c.card_count} cards] {names}")
        print(f"   benefit: {', '.join(c.positive)}  {c.benefit}")
        print(f"   field:   {field or '-'}")
        if args.lines:
            _print_line(c.line)
    if rep.run_id is not None:
        print(f"\nstored as run {rep.run_id}: {rep.new_combos} new combos in Database 1")
    return 0


def cmd_combos(args) -> int:
    from deepyugioh.cards.store import CardStore
    from deepyugioh.repository.combo_db import ComboRepository

    store = CardStore()
    repo = ComboRepository()
    card = store.resolve(args.card).code if args.card else None
    print(f"{repo.count()} combos in Database 1")
    for c in repo.iter_combos(card=card, max_cards=args.max_cards, positive=args.positive, limit=args.limit):
        names = ", ".join(store.name(x) for x in c.cards)
        print(f"#{c.id} [{c.card_count} cards, {len(c.line)} actions, vs {c.setup.opponent.name}] {names}"
              f"  -> {', '.join(c.positive)}")
        if args.lines:
            _print_line(c.line)
    return 0


def cmd_testmode(args) -> int:
    from deepyugioh import settings

    if args.state is not None:
        path = settings.set_test_mode(args.state == "on")
        print(f"test mode {args.state} (stored in {path})")
    active = settings.test_mode()
    print(f"test mode is {'ACTIVE' if active else 'off'}" + (" - start the GUI with `deepyugioh gui`" if active else ""))
    if settings.TEST_MODE_ENV in os.environ:
        print(f"note: {settings.TEST_MODE_ENV}={os.environ[settings.TEST_MODE_ENV]} overrides the settings file")
    return 0


def cmd_gui(args) -> int:
    from deepyugioh import settings

    if not settings.test_mode():
        print("error: the test GUI is only available in test mode - run `deepyugioh testmode on` "
              f"(or set {settings.TEST_MODE_ENV}=1)", file=sys.stderr)
        return 2
    import tkinter

    from deepyugioh.gui import run_gui

    try:
        return run_gui()
    except tkinter.TclError as e:
        print(f"error: cannot open the GUI ({e}) - is a display available?", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    from deepyugioh.contracts.combos import BENEFIT_CRITERIA  # light: no engine import

    p = argparse.ArgumentParser(prog="deepyugioh")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("import-cards", help="Steps 1-3: import cards, analyse scripts, build the interaction graph").set_defaults(fn=cmd_import)

    def common(sp):
        sp.add_argument("--config", help="TOML generator configuration")
        sp.add_argument("--max-cards", type=int)
        sp.add_argument("--min-cards", type=int)
        sp.add_argument("--max-depth", type=int)
        sp.add_argument("--max-nodes", type=int)
        sp.add_argument("--time-limit", type=float, help="seconds per search")
        sp.add_argument("--allow-set", action="store_true", help="also branch on setting cards")

    pl = sub.add_parser("plan", help="show candidate tiers and coverage")
    common(pl)
    pl.set_defaults(fn=cmd_plan)

    g = sub.add_parser("generate", help="global search over generated start setups (resumable)")
    common(g)
    g.add_argument("--tiers", type=int, nargs="+", help="tiers to process (default: config)")
    g.add_argument("--max-jobs", type=int, help="stop after this many searches")
    g.add_argument("--time-budget", type=float, help="stop starting new searches after this many seconds")
    g.add_argument("--target", type=int, metavar="N",
                   help="stop once N new combos are in Database 1, however long it takes")
    g.add_argument("--benefit", choices=(*BENEFIT_CRITERIA, "wins"),
                   help="with --target: only count combos with this benefit, e.g. wins")
    g.add_argument("--workers", type=int, help="parallel worker processes")
    g.add_argument("--report-every", type=int, default=50)
    g.set_defaults(fn=cmd_generate)

    s = sub.add_parser("search", help="search all combos from one start setup")
    common(s)
    s.add_argument("--hand", nargs="+", required=True, help="card names or codes")
    s.add_argument("--deck", nargs="*", default=[])
    s.add_argument("--extra", nargs="*", default=[])
    s.add_argument("--opponent", default="empty", help="opponent board name (empty, standard, or from --config)")
    s.add_argument("--branch-zones", action="store_true", help="branch over monster zones (Link)")
    s.add_argument("--show", type=int, default=10)
    s.add_argument("--lines", action="store_true")
    s.add_argument("--no-store", action="store_true", help="do not write to Database 1")
    s.set_defaults(fn=cmd_search)

    c = sub.add_parser("combos", help="list combos stored in Database 1")
    c.add_argument("--card")
    c.add_argument("--positive", help="only combos with this benefit, e.g. opp_field_loss")
    c.add_argument("--max-cards", type=int)
    c.add_argument("--limit", type=int, default=50)
    c.add_argument("--lines", action="store_true")
    c.set_defaults(fn=cmd_combos)

    t = sub.add_parser("testmode", help="show or switch test mode (enables the test GUI)")
    t.add_argument("state", nargs="?", choices=["on", "off"])
    t.set_defaults(fn=cmd_testmode)

    sub.add_parser("gui", help="test GUI: tune the generator, read results (test mode only)").set_defaults(fn=cmd_gui)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (KeyError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
