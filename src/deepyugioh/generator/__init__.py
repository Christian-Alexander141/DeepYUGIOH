"""Combo Generator (Phase 1) - public interface.

Only the names exported here are the module's interface. Everything in
`generator.search` and `generator.execution` is internal and may change.

    from deepyugioh.generator import ComboGenerator, GeneratorConfig
    from deepyugioh.contracts import StartSetup

    gen = ComboGenerator(pool, sink, GeneratorConfig(), graph=graph)
    report = gen.search(StartSetup(hand=(...), deck=(...), extra=(...)))   # one setup
    summary = gen.run(tiers=[1, 2], max_jobs=1000)                         # global, resumable
    gen.coverage()                                                         # how much is done

See docs/schnittstellen.md for the full contract.
"""

from deepyugioh.generator.candidates import CandidateSource, GraphCandidateSource
from deepyugioh.generator.config import TIERS, CandidateConfig, GeneratorConfig
from deepyugioh.generator.jobs import JobReport, JobStore
from deepyugioh.generator.search.dfs import SearchLimits
from deepyugioh.generator.service import ComboGenerator, GenerationReport, ProgressListener, SearchReport

__all__ = [
    "TIERS", "CandidateConfig", "CandidateSource", "ComboGenerator", "GenerationReport", "GeneratorConfig",
    "GraphCandidateSource", "JobReport", "JobStore", "ProgressListener", "SearchLimits", "SearchReport",
]
