"""AIDE - Algorithmic Interaction Discovery Engine.

Proposes multi-card start setups from structured effect knowledge instead of
pairs of interacting cards:

    index.py        producer / consumer index over EffectSignatures
    constraints.py  deterministic rule checks (what is certainly impossible in a first turn)
    planner.py      forward and backward planning on an abstract first-turn state
    hypotheses.py   candidate pool with status (GENERATED ... VALIDATED) and provenance
    source.py       CandidateSource for tiers 5/6 and the hybrid tier 7

Plans are hypotheses only. They become start setups that the existing search
explores; only combos the engine finds and validates reach Database 1.
"""
