from deepyugioh.contracts import OpponentBoard, OpponentCard, StartSetup


def test_start_setup_is_order_independent():
    a = StartSetup(hand=(3, 1, 2), deck=(9, 8))
    b = StartSetup(hand=(1, 2, 3), deck=(8, 9))
    assert a == b and a.id == b.id and a.hand == (1, 2, 3)


def test_start_setup_roundtrip():
    s = StartSetup(hand=(1,), extra=(5,), opponent=OpponentBoard("x", (OpponentCard(7, "szone", "facedown"),)), branch_zones=True)
    assert StartSetup.from_dict(s.to_dict()) == s


def test_opponent_card_validation():
    import pytest

    with pytest.raises(ValueError):
        OpponentCard(1, zone="grave")
