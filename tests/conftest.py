from datetime import date
from pathlib import Path

import pytest

from nseql.data.fixtures import generate_fixture


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    """A reduced synthetic fixture (18 months, options for the last 6) shared by the session."""
    d = tmp_path_factory.mktemp("datasets")
    generate_fixture(d, start=date(2022, 1, 3), end=date(2023, 6, 30), options_from=date(2023, 1, 1))
    d.chmod(0o755)      # the sandbox runs as an unprivileged user and must be able to read datasets
    return d
