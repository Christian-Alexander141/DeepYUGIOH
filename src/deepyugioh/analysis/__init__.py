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
from deepyugioh.analysis.store import AnalysisRepository, SchemaMismatch
from deepyugioh.analysis.system import AnalysisReport, AnalyzerSystem, analyse_combos

__all__ = [
    "AnalysisReport", "AnalysisRepository", "AnalyzerSystem", "ConsistencyAnalyzer", "CostAnalyzer", "DeckModel",
    "OutcomeAnalyzer", "SchemaMismatch", "analyse_combos", "default_analyzers", "family_key",
]
