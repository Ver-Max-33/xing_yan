"""Every registered primitive is checked the same way (spec §3.5: tests follow the registry):
declared fully, and prefix consistent — what it reports as known at t from the first t bars equals
what it reports as known at t from the whole history (spec §5.8 rule 2)."""
import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.test_l2_prefix import series
from wb.engine.l1.functions.trend import MACD
from wb.engine.l2.spec import compute
from wb.registry import PRIMITIVES, autoload

autoload("wb.engine.l2")


def with_dif(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(MACD(pl.col("close"))["dif"].alias("dif"))


def known_view(df: pl.DataFrame, spec, day) -> pl.DataFrame:
    out = df.filter(pl.col(spec.known) <= day)
    # one step, so every mask reads the original dates (masking ended_at first would hide the rest)
    out = out.with_columns([pl.when(pl.col(when) > day).then(None).otherwise(pl.col(col)).alias(col)
                            for col, when in spec.evolving.items() if col in out.columns])
    out = out.drop([c for c in spec.ignore if c in out.columns])
    return out.sort(out.columns)


@pytest.mark.parametrize("name", sorted(PRIMITIVES))
def test_primitive_is_declared(name):
    p = PRIMITIVES[name]
    assert p.doc and p.params and p.attributes and p.known_at and p.draw and p.understanding and p.tables
    assert p.draw.get("geometry") in {"rect", "polyline", "point", "band", "marker"}


@pytest.mark.parametrize("name", sorted(PRIMITIVES))
def test_primitive_is_prefix_consistent(name):
    p = PRIMITIVES[name]
    for seed in range(8):
        df = with_dif(series(seed, 260))
        t = 150 + 11 * seed
        day = df["date"][t]
        full, part = compute(df, [name])[name], compute(df.head(t + 1), [name])[name]
        for table, spec in p.tables.items():
            a, b = known_view(full[table], spec, day), known_view(part[table], spec, day)
            assert a.equals(b), f"{name}.{table} differs at seed {seed}"


@pytest.mark.parametrize("name", sorted(PRIMITIVES))
def test_parameters_declare_their_range(name):
    for prm in PRIMITIVES[name].params:
        if isinstance(prm.default, bool):
            continue
        if isinstance(prm.default, str):
            assert prm.default in prm.choices, f"{name}.{prm.name}"
        else:
            assert prm.lo is not None and prm.hi is not None and prm.lo <= prm.default <= prm.hi, f"{name}.{prm.name}"


@pytest.mark.parametrize("name", sorted(PRIMITIVES))
def test_understanding_renders_for_every_row(name):
    from wb.engine.l2.spec import describe

    p = PRIMITIVES[name]
    out = compute(with_dif(series(1, 400)), [name])[name]
    assert p.understanding
    for table in p.understanding:
        rows = out[table].to_dicts()
        assert rows, f"{name}.{table}: nothing to describe"
        for r in rows[:300]:
            text = describe(name, table, r)
            assert text and "{" not in text and "None" not in text, text


@pytest.mark.parametrize("n", [0, 1, 3])
def test_short_or_empty_histories_give_empty_tables(n):
    # a listing in its first days (or an empty market) has no structures yet, never an error
    out = compute(with_dif(series(0, 5)).head(n))
    for name, p in PRIMITIVES.items():
        assert set(out[name]) == set(p.tables), name


def test_finished_results_are_freed_without_waiting_for_the_garbage_collector():
    # a reference cycle around the results kept every chunk's tables alive in the workers until a
    # full collection (Polars data lives outside Python's allocation counts, so that comes late):
    # ~30 MB per chunk, ~0.7 GB per worker over a market
    import gc

    bars = with_dif(series(0, 300))
    compute(bars, ["BOX"])  # first call: imports and caches settle
    gc.collect()
    gc.disable()
    try:
        compute(bars, ["BOX_DERIVED"])
        assert gc.collect() == 0  # nothing left for the cycle collector: freed when it went out of use
    finally:
        gc.enable()


def test_known_view_treats_an_empty_table_like_one_filtered_to_nothing():
    # an empty prefix side must drop the same columns as a full side with no row known yet
    from datetime import timedelta

    spec = PRIMITIVES["BOX"].tables["boxes"]
    out = compute(with_dif(series(0, 300)), ["BOX"])["BOX"]["boxes"]
    before = out["known_at"].min() - timedelta(days=1)
    assert known_view(out.clear(), spec, before).columns == known_view(out, spec, before).columns


@pytest.mark.parametrize("name", sorted(PRIMITIVES))
@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 100_000), n=st.integers(60, 320))
def test_primitive_is_prefix_consistent_on_random_series(name, seed, n):
    # the hard gate (spec §5.8 rule 2) on random lengths and several cuts per series
    p = PRIMITIVES[name]
    df = with_dif(series(seed, n))
    full = compute(df, [name])[name]
    for frac in (0.4, 0.65, 0.9):
        t = int((n - 1) * frac)
        day = df["date"][t]
        part = compute(df.head(t + 1), [name])[name]
        for table, spec in p.tables.items():
            a, b = known_view(full[table], spec, day), known_view(part[table], spec, day)
            assert a.equals(b), f"{name}.{table} differs (seed {seed}, n {n}, t {t})"


def test_a_box_is_described_with_its_level_edges_and_the_day_it_was_drawn():
    from wb.engine.l2.spec import describe

    boxes = compute(with_dif(series(3, 300)), ["BOX"])["BOX"]["boxes"]
    est = boxes.filter(pl.col("scale") != "short").row(0, named=True)
    text = describe("BOX", "boxes", est)
    assert "成立" in text and f"{est['top']:.2f}" in text and str(est["known_at"]) in text
