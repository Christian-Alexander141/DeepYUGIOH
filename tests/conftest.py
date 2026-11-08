import pytest

from deepyugioh import paths


@pytest.fixture(scope="session")
def store():
    if not paths.ocgcore_library().exists():
        pytest.skip("libocgcore not built - run scripts/build_core.sh")
    if not paths.card_db().exists():
        pytest.skip("card database missing - run `deepyugioh import-cards`")
    from deepyugioh.cards.store import CardStore

    return CardStore()


@pytest.fixture(scope="session")
def code(store):
    return lambda name: store.by_name(name).code
