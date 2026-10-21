from datetime import time

import pytest

from wb import listing as L
from wb.markets import load_market


def test_market_profiles_load():
    jp = load_market("JP")
    assert jp.sessions == ((time(9, 0), time(11, 30)), (time(12, 30), time(15, 30)))
    assert jp.ready_start == time(16, 30) and jp.limit_rule == "flag"
    cn = load_market("CN")
    assert cn.ready_start == time(16, 0) and cn.benchmark["prod"] == "CN:IDX:000300"


def test_letter_codes_everywhere():
    # Anti-example from spec §4.1: daily_stock_analysis returns None for '130A.T'.
    assert L.from_yahoo("130A.T") == "JP:130A"
    assert L.from_jquants("130A0") == "JP:130A"
    assert L.to_jquants("JP:130A") == "130A0"
    assert L.to_yahoo("JP:130A") == "130A.T"


def test_jquants_fifth_digit_kept():
    assert L.from_jquants("72030") == "JP:7203"
    assert L.from_jquants("25935") == "JP:25935"
    assert L.to_jquants("JP:25935") == "25935"


def test_tushare_roundtrip():
    assert L.from_tushare("600519.SH") == "CN:600519"
    assert L.mic_from_tushare("830799.BJ") == "XBSE"
    assert L.to_tushare("CN:000001", "XSHE") == "000001.SZ"


def test_index_namespace_does_not_collide():
    assert L.index_id("CN", "000001") == "CN:IDX:000001" != L.make_id("CN", "000001")


@pytest.mark.parametrize("bad", ["JP:72", "CN:60051", "XX:1", "JP:"])
def test_bad_ids_rejected(bad):
    with pytest.raises(ValueError):
        market, _, code = bad.partition(":")
        L.make_id(market, code) if market in ("CN", "JP") else L.parse_id(bad)
