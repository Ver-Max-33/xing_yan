"""Security identity (spec §4.1).

listing_id = "<market>:<code>", e.g. JP:7203, JP:130A, CN:600519.
Indices live in their own namespace: CN:IDX:000300, JP:IDX:TOPIX.
Each data source's code format is converted only here, never by ad-hoc string slicing elsewhere.
"""
import re

_JP_CODE = re.compile(r"^[0-9][0-9A-Z]{3}([0-9])?$")  # 4 chars, optional 5th digit (preferred shares)
_CN_CODE = re.compile(r"^[0-9]{6}$")

TUSHARE_SUFFIX_TO_MIC = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}
MIC_TO_TUSHARE_SUFFIX = {v: k for k, v in TUSHARE_SUFFIX_TO_MIC.items()}


def make_id(market: str, code: str) -> str:
    if market == "JP" and not _JP_CODE.match(code):
        raise ValueError(f"bad JP code {code!r}")
    if market == "CN" and not _CN_CODE.match(code):
        raise ValueError(f"bad CN code {code!r}")
    return f"{market}:{code}"


class BadListingId(ValueError):
    pass


def parse_id(listing_id: str) -> tuple[str, str]:
    market, _, code = listing_id.partition(":")
    if market not in ("CN", "JP") or not code:
        raise BadListingId(f"bad listing_id {listing_id!r}")
    return market, code


def index_id(market: str, name: str) -> str:
    return f"{market}:IDX:{name}"


def from_tushare(ts_code: str) -> str:
    """'600519.SH' -> 'CN:600519'."""
    code, _, suffix = ts_code.partition(".")
    if suffix not in TUSHARE_SUFFIX_TO_MIC:
        raise ValueError(f"bad tushare code {ts_code!r}")
    return make_id("CN", code)


def mic_from_tushare(ts_code: str) -> str:
    return TUSHARE_SUFFIX_TO_MIC[ts_code.partition(".")[2]]


def to_tushare(listing_id: str, mic: str) -> str:
    market, code = parse_id(listing_id)
    if market != "CN":
        raise ValueError(listing_id)
    return f"{code}.{MIC_TO_TUSHARE_SUFFIX[mic]}"


def from_jquants(code: str) -> str:
    """J-Quants uses 5 characters: the 4-char code plus '0' ('72030', '130A0').
    A non-zero 5th digit marks a separate issue (e.g. preferred shares) and is kept."""
    if len(code) == 5 and code.endswith("0"):
        code = code[:4]
    return make_id("JP", code)


def to_jquants(listing_id: str) -> str:
    market, code = parse_id(listing_id)
    if market != "JP":
        raise ValueError(listing_id)
    return code + "0" if len(code) == 4 else code


def from_yahoo(ticker: str) -> str:
    """'7203.T' -> 'JP:7203', '130A.T' -> 'JP:130A', '600519.SS' -> 'CN:600519'."""
    code, _, suffix = ticker.partition(".")
    if suffix == "T":
        return make_id("JP", code)
    if suffix in ("SS", "SZ", "BJ"):
        return make_id("CN", code)
    raise ValueError(f"unsupported yahoo ticker {ticker!r}")


def to_yahoo(listing_id: str) -> str:
    market, code = parse_id(listing_id)
    if market != "JP":
        raise ValueError("yahoo is only used for JP stocks in the test profile")
    return f"{code}.T"


# ---- vectorized forms for whole columns (same rules as above) -------------------------------
import polars as pl  # noqa: E402


def tushare_id_expr(col: str = "ts_code") -> pl.Expr:
    """Column of '600519.SH' -> 'CN:600519'; anything that is not a 6-digit A-share code -> null."""
    c = pl.col(col)
    return pl.when(c.str.contains(r"^\d{6}\.(SH|SZ|BJ)$")).then(pl.lit("CN:") + c.str.slice(0, 6))


def tushare_mic_expr(col: str = "ts_code") -> pl.Expr:
    return pl.col(col).str.slice(7).replace_strict(TUSHARE_SUFFIX_TO_MIC, default=None)


def jquants_id_expr(col: str = "Code") -> pl.Expr:
    """Column of '72030' / '130A0' / '25935' -> 'JP:7203' / 'JP:130A' / 'JP:25935'."""
    c = pl.col(col).cast(pl.Utf8)
    return pl.lit("JP:") + pl.when((c.str.len_chars() == 5) & c.str.ends_with("0")).then(c.str.slice(0, 4)).otherwise(c)
