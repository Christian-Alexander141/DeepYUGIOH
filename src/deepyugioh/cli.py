"""Command line interface - a thin layer over the module interfaces.

    deepyugioh import-cards                 Steps 1-3: cards, effects, interaction graph
    deepyugioh plan                         candidate tiers and coverage of the global search
    deepyugioh generate --tiers 1 2         global, resumable search into Database 1
    deepyugioh search --hand ... [--deck ...] [--extra ...] [--opponent standard]
    deepyugioh combos [--card NAME] [--positive opp_field_loss] [--lines]
    deepyugioh aide build|stats|explain|plan|report   AIDE: effect signatures, plans, hypothesis status
    deepyugioh benchmark [--budget 300s]    tiers 1-4 against AIDE and the hybrid tier on a card subset
    deepyugioh analyze [--full]             Stufe 2: evaluate the combos of Database 1 into Database 2
    deepyugioh analysis stats|show|top|card|fit|compat|robust   read Database 2
    deepyugioh robustness run|stats         Stufe 2: handtrap search per engine (resumable, parallel)
    deepyugioh filter list|save|run|show|export|delete   quality filter: stored profiles and their combos
    deepyugioh recheck [--apply]            replay Database 1 and correct the required cards (after a rule fix)
    deepyugioh testmode [on|off]            show or switch test mode
    deepyugioh gui [--stage 2]              test GUI: Combo Generator (stage 1) or Analyzer System (stage 2), test mode only

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
    from deepyugioh.cards.signatures import SignatureStore

    sig = SignatureStore(build_if_missing=False).build()
    print(f"effect signatures {sig['version']}: {sig['usable']} usable effects, "
          f"{sig['unknown_share']:.0%} with unknown parts ({sig['unknown_share_without_conditions']:.0%} without conditions)")
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


def _card_line(store, sig) -> list[str]:
    def what(m):
        if m.is_self:
            return "this card"
        if not m.what.any_of:
            return "any card"
        return " OR ".join(", ".join(f"{k}={v}" for k, v in f.to_dict().items() if k != "unknown") or "any"
                           for f in m.what.any_of)

    out = [f"  [{sig.kind}] in {'/'.join(sig.location) or '?'}"
           + (f", trigger {sig.trigger.event}{' (this card)' if sig.trigger.self_only else ''}" if sig.trigger else "")
           + (f", {sig.count_limit}" if sig.count_limit else "") + f"  confidence {sig.confidence:.2f}"]
    for c in sig.conditions:
        out.append(f"      condition {c}")
    for m in sig.costs:
        out.append(f"      cost   {what(m)}: {'/'.join(m.src) or '?'} -> {m.dst}")
    for m in sig.moves:
        out.append(f"      effect {what(m)}: {'/'.join(m.src) or '?'} -> {m.dst}" + (" (opponent)" if m.opponent else ""))
    if sig.draws:
        out.append(f"      draws {sig.draws}")
    if sig.unknown:
        out.append(f"      UNKNOWN: {'; '.join(sig.unknown)}")
    return out


def cmd_aide(args) -> int:
    from deepyugioh.cards.signatures import SignatureStore
    from deepyugioh.cards.store import CardStore

    store = CardStore()
    if args.action == "build":
        stats = SignatureStore(build_if_missing=False).build()
        print(f"effect signatures {stats['version']}: {stats}")
        return 0
    knowledge = SignatureStore()
    if args.action == "stats":
        from deepyugioh.generator.aide.index import EffectIndex

        print(f"effect signatures {knowledge.version()}: {knowledge.meta()}")
        print(f"producer/consumer index: {EffectIndex(knowledge).stats()}")
        return 0
    if args.action == "explain":
        if not args.card:
            raise ValueError("aide explain needs --card NAME")
        card = store.resolve(args.card)
        print(f"{card.name} ({card.code})")
        for sig in knowledge.signatures(card.code):
            print("\n".join(_card_line(store, sig)))
        return 0
    if args.action == "plan":
        if not args.card:
            raise ValueError("aide plan needs --card NAME")
        from deepyugioh.generator.aide.index import EffectIndex
        from deepyugioh.generator.aide.planner import Planner

        cfg = _config(args).candidates
        card = store.resolve(args.card)
        planner = Planner(EffectIndex(knowledge), max_cards=_config(args).limits.max_cards,
                          branching=cfg.aide_branching, plans_per_root=cfg.aide_plans_per_card)
        forward = planner.forward(card.code)
        backward, rejected = planner.backward(card.code)
        for plan in forward + backward:
            print(f"\n{plan.planner} plan {plan.id}: {plan.links} links - "
                  + ", ".join(f"{store.name(c)} ({z})" for c, z in plan.start))
            for st in plan.steps:
                moved = "; ".join(f"{store.name(c)} {a}->{b}" for c, a, b in st.moved)
                action = "Normal Summon" if st.action == "normal_summon" else f"effect {st.action}"
                print(f"   {store.name(st.code)}: {action}  [{moved}]" + ("  LINK" if st.links else ""))
        for r in rejected:
            print(f"\nrejected: {r['reason']}")
        if not forward and not backward:
            print("no plan found")
        return 0
    if args.action == "report":
        from deepyugioh.generator import JobStore
        from deepyugioh.generator.aide.hypotheses import HypothesisStore
        from deepyugioh.repository.combo_db import ComboRepository

        hyp = HypothesisStore()
        counts = hyp.update_status(JobStore().jobs_for, ComboRepository().cards_for_runs)
        print("AIDE hypotheses: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none yet"))
        for row in hyp.list(status=args.status, limit=args.limit):
            names = ", ".join(f"{store.name(c)} ({z})" for c, z in row["cards"])
            print(f"  {row['status']:<18} {row['planner']:<8} {row['links']} links  {names}"
                  + (f"  - {row['reason']}" if row["reason"] else ""))
        return 0
    raise ValueError(f"unknown aide action {args.action}")


def cmd_benchmark(args) -> int:
    from deepyugioh import paths
    from deepyugioh.benchmark import BenchmarkSettings, run_benchmark

    raw = args.budget.strip()
    kind = "seconds" if raw.endswith("s") else "searches"
    settings = BenchmarkSettings(budget=int(raw.rstrip("s")), budget_kind=kind, workers=args.workers or 4,
                                 variants=tuple(args.variants))
    out = Path(args.out) if args.out else paths.DATA / "benchmark" / __import__("time").strftime("%Y%m%d-%H%M%S")
    run_benchmark(settings, out)
    print((out / "benchmark.md").read_text())
    print(f"written to {out}")
    return 0


def cmd_analyze(args) -> int:
    from deepyugioh.analysis.pipeline import run_analysis

    run_analysis(full=args.full, engine=not args.no_engine, workers=args.workers,
                 progress=lambda what, i, n: print(f"  {what} {i}/{n}", flush=True))
    return 0


def _rescore(analysis=None):
    from deepyugioh.analysis.pipeline import rescore

    rescore()


def _run_filters(qf, name: str | None = None):
    from deepyugioh.analysis.pipeline import run_filters

    run_filters(qf, name)


def _print_profile(store, combo, prof):
    names = ", ".join(store.name(c) for c in combo.cards) if combo else "?"
    print(f"\n{names}" + (f"  [#{combo.id}, {combo.card_count} cards]" if combo else ""))
    order = ("strength", "opening", "breaking", "late", "outcome", "cost", "consistency", "robustness", "genericness",
             "compatibility", "line")
    for axis in (*order, *sorted(set(prof) - set(order))):
        if axis in prof:
            a = prof[axis]
            score = "-" if a.score is None else f"{a.score:5.1f}"
            print(f"   {axis:<12} {score}  {a.explanation}")


def cmd_analysis(args) -> int:
    from deepyugioh.analysis import AnalysisRepository
    from deepyugioh.cards.store import CardStore
    from deepyugioh.repository.combo_db import ComboRepository

    analysis = AnalysisRepository()
    if args.action == "stats":
        import json

        print(json.dumps(analysis.stats(), indent=2, ensure_ascii=False))
        return 0
    store, combos = CardStore(), ComboRepository()
    by_sig = lambda sig: next(iter(combos.db.execute("SELECT id FROM combos WHERE signature=?", (sig,))), (None,))[0]

    def need_combo():
        if args.combo is None:
            raise ValueError(f"analysis {args.action} needs --combo ID")
        combo = combos.get(args.combo)
        if combo is None:
            raise KeyError(f"no combo #{args.combo} in Database 1")
        return combo

    def short(sig):
        c = combos.by_signature(sig)
        return f"#{c.id} {', '.join(store.name(x) for x in c.cards)}" if c else sig[:12]

    if args.action == "compat":
        from deepyugioh.analysis.compatibility import KINDS, conflict_text, load_index

        combo = need_combo()
        if args.other is not None:
            other = combos.get(args.other)
            if other is None:
                raise KeyError(f"no combo #{args.other} in Database 1")
            from deepyugioh.cards.signatures import SignatureStore

            rel = load_index(analysis, SignatureStore().card).relation(combo.signature, other.signature)
            print(f"{short(combo.signature)}\n{short(other.signature)}")
            print(f"  gemeinsame Karten ({rel['shared_strength']:.2f}): "
                  f"{', '.join(store.name(c) for c in rel['shared_cards']) or '-'}")
            print(f"  gemeinsamer Archetyp ({rel['archetype_strength']:.2f}): {', '.join(rel['archetypes']) or '-'}")
            for label, found in (("erste sperrt zweite", rel["a_forbids_b"]), ("zweite sperrt erste", rel["b_forbids_a"])):
                for f in found:
                    print(f"  Sperrkonflikt ({label}, {f['strength']:.2f}): {conflict_text(f, store.name)}")
                if not found:
                    print(f"  kein Sperrkonflikt ({label})")
            return 0
        _print_profile(store, combo, {k: v for k, v in analysis.profile(combo.signature).items() if k == "compatibility"})
        for e in analysis.edges(combo.signature, kind=args.kind, limit=args.limit):
            d = e["data"]
            if e["kind"] == "shared_cards":
                detail = ", ".join(store.name(c) for c in d["cards"])
            elif e["kind"] == "archetype":
                detail = d["archetype"]
            else:
                detail = ("sperrt: " if e["direction"] == "out" else "wird gesperrt: ") + "; ".join(
                    ", ".join(store.name(c) for c in f["cards"]) for f in d["conflicts"])
            print(f"   {KINDS[e['kind']]:<22} {e['strength']:.2f}  {short(e['other'])}  [{detail}]")
        return 0
    if args.action == "robust":
        from deepyugioh import settings

        combo = need_combo()
        rs = settings.robustness_settings()
        rows = list(analysis.robustness_rows(combo.signature, rs.search_version()))
        _print_profile(store, combo, {k: v for k, v in analysis.profile(combo.signature).items() if k == "robustness"})
        if not rows:
            print(f"   noch nicht gesucht - `deepyugioh robustness run --combo {combo.id}`")
        for _, h, status, value, data in rows:
            label = "ungestört" if h == "-" else rs.catalog.get(h, {}).get("card", h)
            crit = data.get("critical")
            where = f"  nach „{crit['after']}“" if crit else ("" if h == "-" else "  nicht eingesetzt")
            val = "-" if value is None else f"{value:.2f}"
            reasons = f"  {data['incomplete']}" if data.get("incomplete") else ""
            print(f"   {label:<32} {status:<10} garantiert {val:>7}  {data['nodes']} Knoten{where}{reasons}")
        return 0
    if args.action == "show":
        combo = need_combo()
        _print_profile(store, combo, analysis.profile(combo.signature))
        return 0
    if args.action == "top":
        for sig, _ in analysis.top(args.axis, limit=args.limit):
            cid = by_sig(sig)
            _print_profile(store, combos.get(cid) if cid else None, analysis.profile(sig))
        return 0
    if args.action == "rescore":
        _rescore(analysis)
        return 0
    if args.action == "weights":
        from deepyugioh import settings
        from deepyugioh.analysis.strength import component_label

        for sc in settings.analysis_scorings():
            print(f"{sc.name} - {sc.label} ({sc.version})")
            total = sum(sc.weights.values())
            for comp, w in sorted(sc.weights.items(), key=lambda kv: -kv[1]):
                print(f"   {w / total:5.1%}  {comp:<28} {component_label(comp)}")
        print(f"\nchange them in {settings.settings_path()} under [analysis.scoring.NAME.weights], "
              "then `deepyugioh analysis rescore`")
        return 0
    if args.action == "card":
        if not args.card:
            raise ValueError("analysis card needs --card NAME")
        card = store.resolve(args.card)
        prof = analysis.card_profile(card.code)
        if prof is None:
            from deepyugioh.analysis import CardProfiler
            from deepyugioh.cards.signatures import SignatureStore

            prof = CardProfiler(SignatureStore()).profile(card.code).to_dict()
        print(f"{prof['name']}: Generizität {prof['genericness']:.0f}")
        print(f"   {prof['explanation']}")
        print(f"   Archetypen: {', '.join(prof['archetypes']) or '-'}; Typ: {prof['race'] or '-'}; Attribut: {prof['attribute'] or '-'}")
        return 0
    if args.action == "fit":
        from deepyugioh.analysis import deck_fit

        deck = _deck_profile(args)
        outcome = {sig: score for sig, _, score in analysis.axis_rows("outcome")}
        ranked = []
        for sig, raw, _ in analysis.axis_rows("genericness"):
            ranked.append((deck_fit(raw, deck), outcome.get(sig) or 0.0, sig))
        ranked.sort(reverse=True)
        print(f"Deckprofil {deck.name}: {len(ranked)} Combos bewertet")
        for fit, out, sig in ranked[: args.limit]:
            cid = by_sig(sig)
            combo = combos.get(cid) if cid else None
            print(f"\n  Passung {fit:5.1f}, Ergebnis {out:5.1f}")
            _print_profile(store, combo, analysis.profile(sig))
        return 0
    raise ValueError(f"unknown analysis action {args.action}")


def _deck_profile(args):
    from deepyugioh.analysis import deck_profile_from_names

    if not (args.archetype or args.race or args.attribute):
        raise ValueError("a deck profile needs --archetype, --race or --attribute")
    return deck_profile_from_names(args.archetype, args.race, args.attribute)


def cmd_robustness(args) -> int:
    from deepyugioh import settings
    from deepyugioh.analysis import AnalysisRepository
    from deepyugioh.analysis.pipeline import run_robustness_search

    if args.action == "stats":
        analysis = AnalysisRepository()
        rs = settings.robustness_settings()
        stats = analysis.robustness_stats()
        print(f"search rules {rs.search_version()}, {len(rs.catalog)} handtraps: "
              + ", ".join(f"{v['card']} ({v.get('weight', 1.0):g})" for v in rs.catalog.values()))
        for status, row in stats.items():
            print(f"  {status:<11} {row['jobs']} searches over {row['combos']} combos, {row['seconds']:.0f}s engine time")
        if not stats:
            print("  nothing searched yet - `deepyugioh robustness run`")
        print(f"  axis robustness: {len(analysis.scores('robustness'))} combos")
        return 0

    def progress(s):
        if s["combos"] % args.report_every == 0:
            print(f"  {s['combos']}/{s['pending']} combos ({s['complete']} complete, {s['incomplete']} incomplete, "
                  f"{len(s['errors'])} errors), {s['searches']} searches, {s['elapsed']:.0f}s", flush=True)

    run_robustness_search(workers=args.workers, max_combos=args.max_combos, time_budget=args.time_budget,
                          retry_incomplete=args.retry_incomplete, combo_ids=args.combo, progress=progress)
    return 0


def _pairs(items, what) -> dict[str, float]:
    out = {}
    for item in items or ():
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"{what}: expected NAME=NUMBER, got {item!r}")
        out[key] = float(value)
    return out


def cmd_filter(args) -> int:
    from deepyugioh.analysis import AnalysisRepository, FilterProfile, QualityFilter
    from deepyugioh.cards.store import CardStore
    from deepyugioh.repository.combo_db import ComboRepository

    analysis = AnalysisRepository()
    qf = QualityFilter(analysis)
    qf.ensure_defaults()
    need_name = lambda: args.name or (_ for _ in ()).throw(ValueError(f"filter {args.action} needs a profile NAME"))
    if args.action == "list":
        for name, info in analysis.filters().items():
            spec = info["spec"]
            state = "not applied yet" if info["applied_at"] is None else (
                f"{info['members']} of {info['considered']} combos" + (" - STALE, run `filter run`" if info["stale"] else ""))
            print(f"{name:<16} {spec.get('label', ''):<44} {state}")
        return 0
    if args.action == "save":
        late = True if args.late else False if args.no_late else None
        profile = FilterProfile(
            need_name(), args.label or "", _pairs(args.min, "--min"), args.rank_by, _pairs(args.weight, "--weight"),
            tuple(args.archetype or ()), tuple(args.race or ()), tuple(args.attribute or ()), args.min_fit, args.locks,
            args.min_cards, args.max_cards, late, not args.keep_dominated, args.limit)
        if profile.has_deck:
            _deck_profile(args)   # unknown archetype / type / attribute names fail here, not at the first run
        qf.save(profile)
        _run_filters(qf, profile.name)
        return 0
    if args.action == "run":
        _run_filters(qf, args.name)
        return 0
    if args.action == "delete":
        analysis.delete_filter(need_name())
        print(f"filter {args.name} deleted")
        return 0
    store, combos = CardStore(), ComboRepository()
    if args.action == "show":
        info = analysis.filters().get(need_name())
        if info is None:
            raise KeyError(f"no filter profile {args.name!r}")
        print(f"{args.name}: {info['spec']}")
        print(f"{info['members']} of {info['considered']} combos, removed: {info['dropped'] or 'nothing'}"
              + ("  - STALE: Database 2 changed since, run `deepyugioh filter run`" if info["stale"] else ""))
        for sig, rank, value, fit in analysis.filter_results(args.name, args.limit):
            combo = combos.by_signature(sig)
            print(f"\n  Rang {rank}: {value:.1f}" + (f", Passung {fit:.0f}" if fit is not None else ""))
            _print_profile(store, combo, analysis.profile(sig))
        return 0
    if args.action == "export":
        from deepyugioh import paths

        out = Path(args.out) if args.out else paths.DATA / "filters" / f"{need_name()}.json"
        n = qf.export(need_name(), out, combos.by_signature, store.name)
        print(f"{n} combos of filter {args.name} written to {out}")
        return 0
    raise ValueError(f"unknown filter action {args.action}")


def cmd_recheck(args) -> int:
    from deepyugioh import settings
    from deepyugioh.cards.store import CardStore
    from deepyugioh.generator.recheck import ZONES, recheck
    from deepyugioh.repository.combo_db import ComboRepository

    repo, store = ComboRepository(), CardStore()
    workers = args.workers or settings.analysis_workers()
    out = recheck(repo, CardStore, workers=workers, apply=args.apply, repo=repo,
                  progress=lambda i, n: print(f"  {i}/{n} combos replayed", flush=True))
    print(f"{out['checked']} combos replayed: {out['unchanged']} unchanged, {len(out['changed'])} with other requirements, "
          f"{len(out['drift'])} with another signature only, {len(out['errors'])} errors")
    for ch in out["changed"][: args.limit]:
        diff = []
        for z in ZONES:
            if ch["old"][z] != ch["new"][z]:
                diff.append(f"{z}: {', '.join(store.name(c) for c in ch['old'][z]) or '-'} -> "
                            f"{', '.join(store.name(c) for c in ch['new'][z]) or '-'}")
        print(f"  #{ch['id']} {'; '.join(diff)}" + (f"  [{ch['result']}]" if "result" in ch else ""))
    if len(out["changed"]) > args.limit:
        print(f"  ... {len(out['changed']) - args.limit} more")
    for e in out["errors"][:10]:
        print(f"  error: {e}")
    if out["drift"]:
        print(f"  signature only: {out['drift'][:20]}")
    if args.apply:
        print(f"Database 1 corrected: {out['updated']} updated, {out['merged']} merged into an existing combo - "
              "run `deepyugioh analyze` to evaluate them again")
    elif out["changed"]:
        print("nothing written - run again with --apply to correct Database 1")
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
        return run_gui(args.stage)
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

    a = sub.add_parser("aide", help="AIDE: effect signatures, plans and hypothesis status")
    a.add_argument("action", choices=["build", "stats", "explain", "plan", "report"])
    a.add_argument("--card", help="card name or code (explain, plan)")
    a.add_argument("--status", help="report: only hypotheses with this status, e.g. VALIDATED")
    a.add_argument("--limit", type=int, default=30)
    a.add_argument("--config", help="TOML generator configuration")
    a.set_defaults(fn=cmd_aide)

    b = sub.add_parser("benchmark", help="compare tiers 1-4, AIDE and the hybrid tier on a fixed card subset")
    b.add_argument("--budget", default="300s", help="per variant: number of searches, or seconds with suffix s (default 300s)")
    b.add_argument("--workers", type=int)
    b.add_argument("--variants", nargs="+", default=["baseline", "hybrid", "aide"], choices=["baseline", "hybrid", "aide"])
    b.add_argument("--out", help="output directory (default data/benchmark/<time>)")
    b.set_defaults(fn=cmd_benchmark)

    an = sub.add_parser("analyze", help="Stufe 2: evaluate the combos of Database 1 (incremental) into Database 2")
    an.add_argument("--full", action="store_true", help="recompute every axis for every combo")
    an.add_argument("--no-engine", action="store_true", help="skip the engine replay of the lines (line facts)")
    an.add_argument("--workers", type=int, help="worker processes for the engine replay")
    an.set_defaults(fn=cmd_analyze)

    rc = sub.add_parser("recheck", help="replay every combo of Database 1 and determine its required cards again")
    rc.add_argument("--apply", action="store_true", help="correct Database 1 (default: only report)")
    rc.add_argument("--workers", type=int)
    rc.add_argument("--limit", type=int, default=40, help="changed combos listed")
    rc.set_defaults(fn=cmd_recheck)

    rb = sub.add_parser("robustness", help="Stufe 2: search every combo against handtraps with the engine (resumable)")
    rb.add_argument("action", choices=["run", "stats"])
    rb.add_argument("--max-combos", type=int, help="stop after this many combos (strongest first)")
    rb.add_argument("--time-budget", type=float, help="stop starting new combos after this many seconds")
    rb.add_argument("--combo", type=int, nargs="+", help="only these combo ids of Database 1")
    rb.add_argument("--retry-incomplete", action="store_true", help="search incomplete results again (e.g. with higher limits)")
    rb.add_argument("--workers", type=int)
    rb.add_argument("--report-every", type=int, default=10)
    rb.set_defaults(fn=cmd_robustness)

    fl = sub.add_parser("filter", help="quality filter: stored filter profiles and their ranked combos")
    fl.add_argument("action", choices=["list", "save", "run", "show", "export", "delete"])
    fl.add_argument("name", nargs="?", help="profile name")
    fl.add_argument("--label")
    fl.add_argument("--min", nargs="+", metavar="AXIS=VALUE",
                    help="least score 0-100 of an axis or scoring (strength=70) or least raw value (outcome.disruptions=1)")
    fl.add_argument("--rank-by", default="strength", help="axis or scoring the members are ordered by")
    fl.add_argument("--weight", nargs="+", metavar="COMPONENT=WEIGHT", help="own ranking instead of --rank-by")
    fl.add_argument("--archetype", nargs="+")
    fl.add_argument("--race", nargs="+")
    fl.add_argument("--attribute", nargs="+")
    fl.add_argument("--min-fit", type=float, help="least fit 0-100 to the deck profile")
    fl.add_argument("--locks", choices=["any", "none", "fitting"], default="any",
                    help="none: no certain restriction; fitting: only restrictions the deck profile satisfies")
    fl.add_argument("--min-cards", type=int)
    fl.add_argument("--max-cards", type=int)
    fl.add_argument("--late", action="store_true", help="only combos from later game situations")
    fl.add_argument("--no-late", action="store_true", help="only combos that start from the opening hand")
    fl.add_argument("--keep-dominated", action="store_true", help="keep combos another combo of their family beats")
    fl.add_argument("--limit", type=int, help="save: keep only the best N; show: rows shown")
    fl.add_argument("--out", help="export: file (default data/filters/NAME.json)")
    fl.set_defaults(fn=cmd_filter)

    ar = sub.add_parser("analysis", help="read Database 2: stats, the profile of one combo, best combos of an axis")
    ar.add_argument("action", choices=["stats", "show", "top", "card", "fit", "rescore", "weights", "compat", "robust"])
    ar.add_argument("--combo", type=int, help="show, compat, robust: combo id in Database 1")
    ar.add_argument("--with", dest="other", type=int, help="compat: second combo id - the exact relation of the pair")
    ar.add_argument("--kind", choices=["shared_cards", "archetype", "lock_conflict"], help="compat: only this kind of edge")
    ar.add_argument("--card", help="card: card name or code")
    ar.add_argument("--archetype", nargs="+", help="fit: deck archetypes, e.g. Blue-Eyes")
    ar.add_argument("--race", nargs="+", help="fit: monster types, e.g. Dragon")
    ar.add_argument("--attribute", nargs="+", help="fit: attributes, e.g. LIGHT")
    ar.add_argument("--axis", default="strength",
                    help="top: outcome, cost, consistency, genericness, strength, opening, breaking or an own scoring")
    ar.add_argument("--limit", type=int, default=10)
    ar.set_defaults(fn=cmd_analysis)

    t = sub.add_parser("testmode", help="show or switch test mode (enables the test GUI)")
    t.add_argument("state", nargs="?", choices=["on", "off"])
    t.set_defaults(fn=cmd_testmode)

    gu = sub.add_parser("gui", help="test GUI (test mode only): stage 1 = Combo Generator, stage 2 = Analyzer System")
    gu.add_argument("--stage", type=int, choices=[1, 2], default=1)
    gu.set_defaults(fn=cmd_gui)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (KeyError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
