import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk.config import DeskConfig, Paths  # noqa: E402
from desk.synth import synth_tape  # noqa: E402


@pytest.fixture(scope="session")
def cfg() -> DeskConfig:
    # A low-fee tier so Jev actually trades on synthetic tape; liquidations under $500k don't pull.
    return (DeskConfig()
            .with_("market", maker_fee=0.0)
            .with_("pull", liq_notional_usd=500_000.0)
            .with_("pricing", base_size=0.01, max_inventory=0.05))


@pytest.fixture(scope="session")
def hour_tape():
    return synth_tape(3600, seed=11)


@pytest.fixture
def tmp_cfg(cfg, tmp_path) -> DeskConfig:
    from dataclasses import replace
    return replace(cfg, paths=Paths(root=tmp_path))
