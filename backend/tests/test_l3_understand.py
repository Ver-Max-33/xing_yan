"""「我的理解」(spec §6.1): the program reads a rule back in plain Chinese from the primitives' and
functions' own descriptions, so what you confirm is what runs."""
from tests.test_l3_rule import EXAMPLE
from tests.test_l3_run import ABOVE_MA
from wb.engine.l3.rule import load_rule
from wb.engine.l3.understand import as_text, understand


def lines(rule_text: str) -> list[str]:
    return [x for s in as_text(understand(load_rule(rule_text)))["sections"] for x in s["lines"]]


def test_each_stage_reads_back_with_its_place_settings_and_conditions():
    got = lines(EXAMPLE)
    assert "① rally（时间窗）：在 box 开始之前 1–60 根之内" in got
    assert any(x.startswith("② 中箱（box，结构）：中箱（BOX") for x in got)   # a stage leads with its name, its id beside
    assert any("收盘超出箱沿多少（比例）才算破箱（越线，不是贴线）= $box_tol（现在 0.015）" in x for x in got)
    assert any("上沿至少 2 个触点、下沿至少 2 个触点" in x for x in got)
    assert "   期间：shrink — MEAN(V, last=10) / MEAN(V, of=rally) < 0.7" in got
    assert "③ 向上突破（breakout，事件）：box 向上突破的那一根" in got
    assert "   那一根：volume — BAND_VOL_RATIO(box) ≥ $vol_spike（现在 2）" in got
    assert "   那一根：macd — BARSLAST(CROSS(MACD.DIF, MACD.DEA)) ≤ 3 且 MACD.DIF > 0" in got
    assert "④ hold（时间窗）：在 breakout 之后 1–3 根" in got
    assert "   每一根都满足：C > box.top" in got


def test_signal_watch_ranking_and_a_glossary_of_what_the_formulas_use():
    u = as_text(understand(load_rule(EXAMPLE)))
    by = {s["title"]: s["lines"] for s in u["sections"]}
    assert by["信号"] == ["hold 结束的那一根收盘后出信号"]
    assert by["潜伏名单"] == ["正在 box 里（还没结束）的股票进潜伏名单，并给出离下一步的距离"]
    assert by["排序"] == ["box.width 从小到大", "BAND_VOL_RATIO(box)@breakout 从大到小"]
    glossary = by["公式里用到的"]
    assert any(x.startswith("BAND_VOL_RATIO：") for x in glossary)
    assert any(x.startswith("MEAN：") for x in glossary)
    assert any(x.startswith("MACD：") for x in glossary)
    assert "box.top：箱体的上沿" in glossary


def test_the_scope_and_parameters_read_back():
    u = as_text(understand(load_rule(EXAMPLE)))
    by = {s["title"]: s["lines"] for s in u["sections"]}
    assert by["适用"][0] == "A 股和日股"
    assert "排除 ST、停牌、上市不足 120 天的股票" in by["适用"]
    assert "近 20 日平均成交额：A 股至少 5,000 万元，日股至少 3 亿日元" in by["适用"]
    assert "box_max_width = 0.15（可调 0.08–0.3，步长 0.01，校准时会调）" in by["参数"]
    assert understand(load_rule(ABOVE_MA))["summary"] == "站上 20 日线 · A 股和日股 · 1 个阶段"


def test_every_built_in_rule_reads_back_without_raw_names():
    from pathlib import Path

    import wb.engine.l3 as l3
    for f in sorted((Path(l3.__file__).parent / "builtin").glob("*.yaml")):
        u = as_text(understand(load_rule(f.read_text(encoding="utf-8"))))
        text = "\n".join(x for s in u["sections"] for x in s["lines"])
        assert u["sections"] and "{" not in text and "None" not in text, f.name


def test_a_word_is_explained_as_it_is_used_here():
    from pathlib import Path

    import wb.engine.l3 as l3
    from wb.registry import FUNCTIONS
    builtin = Path(l3.__file__).parent / "builtin"
    long13 = as_text(understand(load_rule((builtin / "long13.yaml").read_text(encoding="utf-8"))))
    by = {s["title"]: s["lines"] for s in long13["sections"]}
    assert f"COUNT：{FUNCTIONS['COUNT'].doc}" in by["公式里用到的"]       # COUNT(cond, 60) is the L1 count, not the aggregation
    assert "C：收盘价（后复权）" in by["公式里用到的"]
    assert "排除 ST、未股改（名称以 S 开头）的股票" in by["适用"]
    streak = as_text(understand(load_rule((builtin / "limit_streak.yaml").read_text(encoding="utf-8"))))
    words = [x for s in streak["sections"] if s["title"] == "公式里用到的" for x in s["lines"]]
    assert any(x.startswith("LIMIT_UP_STREAK：") for x in words)


def test_parameters_are_chips_the_page_can_change():
    u = understand(load_rule(EXAMPLE))
    chips = [x for s in u["sections"] for line in s["lines"] for x in line if isinstance(x, dict)]
    assert {"param": "vol_spike", "value": 2.0, "unit": "", "text": "$vol_spike（现在 2）"} in chips
    assert {"param": "box_max_width", "value": 0.15, "unit": "", "text": "0.15"} in chips      # the parameter list


READBACK = """
id: t
name: 读回
markets: [CN]
params:
  - {id: sc, value: mid}
stages:
  - id: a
    kind: structure
    use: BOX
    with: {scale: $sc, ret: 7}
    during:
      - {id: up_days, expr: "COUNT(C > O) >= 10"}
  - {id: b, kind: structure, use: BOX, with: {scale: short}, after: {stage: a, gap: [1, 30]}}
  - {id: e, kind: event, on: b.break_up, at: [{id: heat, expr: "COUNT(C > O, 60) >= 30"}]}
signal_at: e.at
"""


def test_the_two_counts_a_parameter_scale_and_a_gap_read_back_as_they_run():
    u = understand(load_rule(READBACK))
    text = as_text(u)
    lines = [x for s in text["sections"] for x in s["lines"]]
    glossary = [x for x in lines if x.startswith("COUNT：")]
    assert len(glossary) == 2                                      # the aggregation over a stage, and the L1 count
    head = next(line for s in u["sections"] for line in s["lines"] if isinstance(line[0], str) and line[0].startswith("① 箱体（a"))
    assert {"param": "sc", "value": "mid", "unit": "", "text": "$sc（现在 mid）"} in head   # the scale is a chip you can change
    assert any("破箱后几根内回到箱里算回箱（R）= 7" in x for x in lines)
    assert any(x.startswith("② 小箱（b，结构）：小箱（BOX）；在 a 之后（a 结束后隔 1–30 根开始）") for x in lines)


def test_a_window_length_parameter_reads_back_as_a_chip():
    from tests.test_l3_rule import HOLD
    u = understand(load_rule(HOLD))
    keep = next(line for s in u["sections"] if s["title"] == "阶段" for line in s["lines"] if "keep" in "".join(x if isinstance(x, str) else x["text"] for x in line))
    assert {"param": "hold", "value": 3, "unit": "", "text": "$hold（现在 3）"} in keep
    assert "② keep（时间窗）：在 today 之后 1–$hold（现在 3） 根" in [x for t in as_text(u)["sections"] for x in t["lines"]]


def test_the_pool_options_read_back():
    from tests.test_l3_rule import HOLD
    u = as_text(understand(load_rule(HOLD.replace("signal_at:", "universe: {boards: [创业板, 科创板], tags: [锂电], watchlist: 重点}\nsignal_at:"))))
    scope = next(s["lines"] for s in u["sections"] if s["title"] == "适用")
    assert "只看板块：创业板、科创板" in scope and "只看带题材标签：锂电" in scope and "只看自选分组「重点」里的股票" in scope


def test_a_named_rule_reads_back_by_its_names_and_in_its_units():
    from tests.test_l3_explain import LABELED
    u = as_text(understand(load_rule(LABELED)))
    by = {s["title"]: s["lines"] for s in u["sections"]}
    assert any(x.startswith("① 上涨段（rally，时间窗）：") for x in by["阶段"])          # its label, its id beside
    assert "   期间：进箱前涨幅 — RUNUP(H, L) ≥ $rise（现在 15%）" in by["阶段"]          # a share reads %, and edits in it
    assert "spike = 2 倍" in by["参数"] and "rise = 15%" in by["参数"]
    tuned = as_text(understand(load_rule(
        LABELED.replace('{id: rise, value: 0.15, unit: "%"}', '{id: rise, value: 0.15, range: [0.05, 0.3], step: 0.005, unit: "%"}'))))
    assert "rise = 15%（可调 5%–30%，步长 0.5%）" in next(s["lines"] for s in tuned["sections"] if s["title"] == "参数")


def test_the_close_mas_a_rule_reads_are_listed_for_the_chart():
    from wb.engine.l3.understand import price_mas
    rule = load_rule("\n".join([
        "id: m", "name: m", "markets: [CN]", "params:", "  - {id: n, value: 30}", "stages:",
        '  - {id: today, kind: event, when: "C > MA(C, $n) AND MA(V, 5) > 0 AND MA_SLOPE_PCT(60, 5) > 0 AND MA_ALIGN([5, 10, 20], up)"}',
        "signal_at: today.at", ""]))
    assert price_mas(rule) == [5, 10, 20, 30, 60]   # a parameter's value; a volume MA is not a price line
