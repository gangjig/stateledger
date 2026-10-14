import pytest

from stateledger import Ledger


class Clock:
    """Deterministic, manually advanced clock for tests."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ledger(clock) -> Ledger:
    return Ledger(now_fn=clock)
