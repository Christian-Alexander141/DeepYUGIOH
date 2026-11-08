"""Step 5: independent validation of a found combo.

The line is replayed in a fresh duel with the exact setup it was found in.
It is valid if the engine accepts every response and the resulting board
equals the recorded end board.
"""

from __future__ import annotations

from deepyugioh.contracts.cards import CardPool
from deepyugioh.contracts.setups import StartSetup
from deepyugioh.engine.duel import Duel, EngineError
from deepyugioh.generator.search.actions import Enumerator, Unsupported


def replay_board_key(store: CardPool, setup: StartSetup, responses: list[bytes]) -> tuple | None:
    enum = Enumerator(names=store.name)
    try:
        with Duel(store, setup) as duel:
            for resp in responses:
                if duel.advance(enum.default_response) is None:
                    return None  # duel/turn ended before the line was complete
                duel.respond(resp)
            duel.advance(enum.default_response)
            return duel.board().key()
    except (EngineError, Unsupported):
        return None


def validate(store: CardPool, setup: StartSetup, responses: list[bytes], expected_key: tuple) -> bool:
    return replay_board_key(store, setup, responses) == expected_key
