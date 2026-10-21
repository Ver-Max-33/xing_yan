"""Security search (spec §4.1): code prefix, name, A-share pinyin initials, Japanese and English names."""
import re

import polars as pl
from pypinyin import Style, lazy_pinyin


_WIDE = str.maketrans({chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)})


def _initials(name: str | None) -> str:
    if not name:
        return ""
    return "".join(p[0] for p in lazy_pinyin(name, style=Style.FIRST_LETTER, errors="ignore") if p).lower()


def build_index(instruments: pl.DataFrame) -> pl.DataFrame:
    """instruments rows -> searchable rows (adds `py` initials for A shares, normalised names)."""
    df = instruments.select("listing_id", "market", "code", "name", "name_en", "board", "status")
    py = [(_initials(n) if m == "CN" else "") for n, m in zip(df["name"].to_list(), df["market"].to_list(), strict=True)]
    fold = [(n or "").translate(_WIDE).replace("\u3000", " ").lower() for n in df["name"].to_list()]
    return df.with_columns(
        pl.Series("py", py, dtype=pl.Utf8),
        pl.Series("_name", fold, dtype=pl.Utf8),
        pl.col("name_en").fill_null("").str.to_lowercase().alias("_en"))


def search(index: pl.DataFrame, q: str, limit: int = 20) -> list[dict]:
    """Ranked: exact code, code prefix, pinyin prefix, name prefix, name contains, English contains.
    Full-width letters/digits are folded so 'ｔｏｙｏｔａ' finds Toyota."""
    q = q.strip().translate(_WIDE).lower()
    if not q:
        return []
    code = pl.col("code").str.to_lowercase()
    lit = re.escape(q)
    rank = (pl.when(code == q).then(0)
            .when(code.str.starts_with(q)).then(1)
            .when(pl.col("py").str.starts_with(q)).then(2)
            .when(pl.col("_name").str.starts_with(q)).then(3)
            .when(pl.col("_name").str.contains(lit)).then(4)
            .when(pl.col("_en").str.contains(lit)).then(5))
    hits = (index.with_columns(rank.alias("_rank"))
            .filter(pl.col("_rank").is_not_null())
            .with_columns((pl.col("status") == "D").alias("_delisted"))
            .sort("_rank", "_delisted", "code")
            .head(limit))
    return hits.select("listing_id", "market", "code", "name", "name_en", "board", "status").to_dicts()
