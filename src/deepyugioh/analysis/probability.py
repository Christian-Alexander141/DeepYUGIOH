"""Exact opening-hand probabilities (multivariate hypergeometric), no sampling.

Deck model: `deck_size` cards, `copies` copies of every card a combo needs in
the hand, all other cards are irrelevant. The probability that an opening
hand of `hand_size` cards contains a requirement (card -> copies needed), or
at least one of several requirements (a combo family), is computed by
enumerating how many copies of each relevant card are drawn.
"""

from __future__ import annotations

from collections import Counter
from math import comb
from typing import Iterable


def _enumerate(codes: list[int], copies: int, hand_size: int):
    """Yield (draw counts per code, number of ways) for every way to draw the relevant cards."""
    counts = [0] * len(codes)

    def rec(i: int, left: int, ways: int):
        if i == len(codes):
            yield tuple(counts), ways, hand_size - left
            return
        for k in range(0, min(copies, left) + 1):
            counts[i] = k
            yield from rec(i + 1, left - k, ways * comb(copies, k))
        counts[i] = 0

    yield from rec(0, hand_size, 1)


def any_requirement(requirements: Iterable[Counter], *, deck_size: int = 40, copies: int = 3, hand_size: int = 5) -> float:
    """P(the opening hand satisfies at least one requirement). An empty requirement is always satisfied."""
    reqs = [Counter(r) for r in requirements]
    if any(not r for r in reqs):
        return 1.0
    reqs = [r for r in reqs if all(n <= copies for n in r.values())]
    if not reqs:
        return 0.0
    # a requirement that contains another one adds nothing to the union
    reqs = [r for r in reqs if not any(o != r and not (o - r) for o in reqs)]
    codes = sorted({c for r in reqs for c in r})
    others = deck_size - copies * len(codes)
    if others < 0:
        raise ValueError("deck too small for the relevant cards")
    total = comb(deck_size, hand_size)
    index = {c: i for i, c in enumerate(codes)}
    req_idx = [[(index[c], n) for c, n in r.items()] for r in reqs]
    good = 0
    for counts, ways, drawn in _enumerate(codes, copies, hand_size):
        if any(all(counts[i] >= n for i, n in r) for r in req_idx):
            rest = hand_size - drawn
            good += ways * comb(others, rest)
    return good / total


def requirement(req: Counter, **kw) -> float:
    return any_requirement([req], **kw)
