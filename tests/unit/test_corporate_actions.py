"""Finding unlisted splits/bonuses in price data and attaching confirmed ones as a new dataset version."""
from datetime import date

import pytest
from nseql.data.corporate import (
    _parse_ratio,
    attach_actions,
    check_action,
    read_actions_csv,
    scan_dataset,
    suspected_actions,
)
from nseql.data.quality import dataset_quality
from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.models import CorporateAction


def _strip_actions(src: Dataset, data_dir, new_id: str, *, adjust: bool = False) -> Dataset:
    """Copy of `src` with no corporate actions — what a Kite import looks like. adjust=True back-adjusts
    the bars instead (what an already-adjusted vendor series looks like)."""
    w = DatasetWriter(data_dir, new_id, description="test copy", source="test", licence="test", synthetic=True, overwrite=True)
    for i in src.instruments.values():
        w.add_instrument(i)
    splits = [a for a in src.corporate_actions() if a.kind in ("split", "bonus")]
    for s in src.symbols("1d"):
        df = src.bars(s, "1d")
        if adjust:
            for a in splits:
                if a.symbol == s:
                    m = df["ts"].dt.date < a.ex_date
                    for c in ("open", "high", "low", "close"):
                        df.loc[m, c] = df.loc[m, c] / a.ratio
        w.write_bars(s, "1d", df)
    return w.finish()


@pytest.fixture(scope="module")
def plain(data_dir):
    return _strip_actions(Dataset(data_dir / "synthetic_v1"), data_dir, "ca_plain")


def _split(data_dir):
    return next(a for a in Dataset(data_dir / "synthetic_v1").corporate_actions() if a.kind == "split")


def test_an_unlisted_split_is_found_with_its_ratio(plain, data_dir):
    sp = _split(data_dir)
    found = scan_dataset(plain)
    assert [(x["symbol"], x["ex_date"], x["ratio"]) for x in found] == [(sp.symbol, str(sp.ex_date), sp.ratio)]
    assert found[0]["evidence"] == "strong"
    rep = dataset_quality(plain)
    assert [x["symbol"] for x in rep["suspected_corporate_actions"]] == [sp.symbol]


def test_listed_splits_are_not_reported(data_dir):
    assert scan_dataset(Dataset(data_dir / "synthetic_v1")) == []
    assert dataset_quality(Dataset(data_dir / "synthetic_v1"))["suspected_corporate_actions"] == []


def test_attaching_the_confirmed_split_writes_a_new_version_and_clears_the_warning(plain, data_dir, tmp_path):
    sp = _split(data_dir)
    csv = tmp_path / "ca.csv"
    csv.write_text(f"symbol,ex_date,kind,ratio,amount\n{sp.symbol},{sp.ex_date},split,10->5,\n")
    new = attach_actions(plain, read_actions_csv(csv), data_dir, "ca_fixed", source="exchange announcement (test)")
    assert new.id == "ca_fixed" and plain.corporate_actions() == []          # the original is untouched
    assert [(a.symbol, a.ex_date, a.ratio) for a in new.corporate_actions()] == [(sp.symbol, sp.ex_date, 2.0)]
    assert scan_dataset(new) == [] and new.verify() == []
    audit = new.manifest.audit[-1]
    assert audit["action"] == "corporate_actions_attached" and audit["parent_content_hash"] == plain.content_hash
    assert new.bars(sp.symbol, "1d").equals(plain.bars(sp.symbol, "1d"))      # prices are not rewritten


def test_a_wrong_ratio_or_date_is_refused(plain, data_dir):
    sp = _split(data_dir)
    assert "x2.00" in check_action(plain, CorporateAction(sp.symbol, sp.ex_date, "split", 5.0))
    assert check_action(plain, CorporateAction(sp.symbol, date(2022, 6, 1), "split", 2.0)) is not None
    with pytest.raises(ValueError, match="refused"):
        attach_actions(plain, [CorporateAction(sp.symbol, sp.ex_date, "split", 5.0)], data_dir, "ca_bad", source="x")
    assert not (data_dir / "ca_bad").exists()


def test_a_split_is_refused_on_data_that_is_already_adjusted(data_dir):
    sp = _split(data_dir)
    adj = _strip_actions(Dataset(data_dir / "synthetic_v1"), data_dir, "ca_adjusted", adjust=True)
    assert scan_dataset(adj) == []                                            # nothing to find
    assert "already adjusted" in check_action(adj, CorporateAction(sp.symbol, sp.ex_date, "split", 2.0))


def test_ratio_notation():
    assert _parse_ratio("split", "10->2") == 5.0 and _parse_ratio("split", "5") == 5.0
    assert _parse_ratio("bonus", "1:1") == 2.0 and _parse_ratio("bonus", "1:2") == 1.5 and _parse_ratio("bonus", "2:1") == 3.0
    with pytest.raises(ValueError, match="bonus notation"):
        _parse_ratio("split", "1:5")


def test_csv_validation(tmp_path):
    p = tmp_path / "ca.csv"
    p.write_text("symbol,ex_date,kind,ratio,amount\nabc,2024-01-02,merger,2,\n")
    with pytest.raises(ValueError, match="kind must be"):
        read_actions_csv(p)
    p.write_text("symbol,ex_date,kind,ratio,amount\nabc,2024-01-02,dividend,,\n")
    with pytest.raises(ValueError, match="positive amount"):
        read_actions_csv(p)
    p.write_text("symbol,ex_date,kind,ratio,amount\nabc,2024-01-02,dividend,,12.5\nxyz,2024-02-01,bonus,1:1,\n")
    a, b = read_actions_csv(p)
    assert (a.symbol, a.kind, a.amount) == ("ABC", "dividend", 12.5) and (b.kind, b.ratio) == ("bonus", 2.0)


def test_ordinary_volatility_is_not_mistaken_for_a_split():
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(1)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, 600)))
    df = pd.DataFrame({"ts": pd.bdate_range("2022-01-03", periods=600), "open": c * (1 + rng.normal(0, 0.01, 600)), "close": c,
                       "volume": rng.integers(1e5, 2e5, 600)})
    assert suspected_actions(df, "X") == []
