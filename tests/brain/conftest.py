from pathlib import Path

import pytest

from nseql.brain import benchmark as bench
from nseql.data.store import Dataset


@pytest.fixture(scope="session")
def screened(data_dir, tmp_path_factory):
    """Shared indicator cache + all 49,728 configurations evaluated once on the 18-month fixture."""
    ds = Dataset(Path(data_dir) / "synthetic_v1")
    work = tmp_path_factory.mktemp("brainwork")
    P, res, cost, stats = bench.prepare(ds, work)
    return {"ds": ds, "work": work, "P": P, "res": res, "cost": cost, "stats": stats}
