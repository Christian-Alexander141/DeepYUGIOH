"""Analyzer System (Stufe 2) - public interface.

    from deepyugioh.analysis import AnalyzerSystem, AnalysisRepository, DeckModel, default_analyzers

    system = AnalyzerSystem(ComboRepository(), AnalysisRepository(), default_analyzers(pool, knowledge))
    system.run()                                    # incremental, versioned, normalised
    AnalysisRepository().profile(signature)         # {axis: AxisScore}

Concept and roadmap: docs/stufe2-konzept.md. Database 1 is only read.
"""

from deepyugioh.analysis.axes import (
    ConsistencyAnalyzer, CostAnalyzer, DeckModel, OutcomeAnalyzer, default_analyzers, family_key,
)
from deepyugioh.analysis.specificity import (
    CardProfiler, CardUniverse, DeckProfile, GenericnessAnalyzer, deck_fit, generic_scale,
)
from deepyugioh.analysis.store import AnalysisRepository, SchemaMismatch
from deepyugioh.analysis.strength import DEFAULT_SCORINGS, Rescorer, Scoring, scorings_from_dict
from deepyugioh.analysis.system import AnalysisReport, AnalyzerSystem, analyse_combos

__all__ = [
    "DEFAULT_SCORINGS", "Rescorer", "Scoring", "scorings_from_dict",
    "AnalysisReport", "AnalysisRepository", "AnalyzerSystem", "CardProfiler", "CardUniverse", "ConsistencyAnalyzer",
    "CostAnalyzer", "DeckModel", "DeckProfile", "GenericnessAnalyzer", "OutcomeAnalyzer", "SchemaMismatch",
    "analyse_combos", "deck_fit", "default_analyzers", "family_key", "generic_scale",
]
