"""价格行为分析 (design 2026-10-20 §3): the four A-share changes inside PA_Agent's prompts and code — the A-share rules
first in the system prompt, only 做多 offered, no short playbooks for a bearish call, cents / dates / limit marks /
next-day limits in the K-line table, and the workbench's previous plan counted in trading days."""
import re

from pa_agent.ai import decision_continuity, prompt_assembler
from pa_agent.ai.decision_continuity import build_continuity_context, render_continuity_prompt_block
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.router import route_strategy_files
from pa_agent.config.paths import PROMPT_DIR
from pa_agent.config.settings import Settings
from pa_agent.data.datetime_ts import format_epoch_for_display
from tests.pa_fixtures import STAGE1_BEARISH_CHANNEL, STAGE1_BEARISH_SPIKE, synthetic_frame


def boom(*args, **kwargs):
    raise AssertionError("PA's trade CSV was read")


def test_the_system_prompt_starts_with_the_a_share_rules():
    s = PromptAssembler(PROMPT_DIR, prompt_settings=Settings().prompt)._shared_system_prompt
    assert s.startswith("## A 股规则") and "T+1" in s and "0.01 元" in s and "不能做空" in s


def test_the_output_skeleton_offers_only_long():
    text = prompt_assembler._STAGE2_OUTPUT_CONTRACT          # the stage-2 skeleton, appended by _build_stage2_user_prompt
    assert '"order_direction": "做多|null' in text and "做多|做空|null" not in text


def test_bearish_routes_keep_identification_files_but_not_short_playbooks():
    files = route_strategy_files({**STAGE1_BEARISH_CHANNEL})
    assert "下跌通道分析识别.txt" in files and "下跌通道交易策略.txt" not in files
    files = route_strategy_files({**STAGE1_BEARISH_SPIKE})
    assert "极速下跌分析识别.txt" in files and "极速下跌交易策略.txt" not in files


def test_the_kline_table_has_cents_dates_limit_marks_and_next_day_limits():
    f = synthetic_frame(limit_marks=("涨停",) + ("",) * 99, next_limit_up=11.0, next_limit_down=9.0)
    t = PromptAssembler._render_kline_table(f)
    assert t.splitlines()[0] == "下一个交易日：涨停价 11.00，跌停价 9.00（A 股涨跌幅限制）"
    row1 = next(line for line in t.splitlines() if line.startswith("1 "))
    assert row1.split("|")[1].strip() == format_epoch_for_display(f.bars[0].ts_open)[:10]
    assert row1.rstrip().endswith("涨停")
    assert re.search(r"\| \d+\.\d\d +\|", row1)                    # prices with two decimals
    h = PromptAssembler._render_kline_table(synthetic_frame(timeframe="1h"))
    assert re.search(r"\| \d{4}-\d\d-\d\d \d\d:\d\d +\|", h)      # other periods keep their time


def test_continuity_uses_the_workbench_previous_plan_and_trading_day_count(monkeypatch):
    monkeypatch.setattr(decision_continuity, "load_last_trade_csv_row", boom)   # the CSV is never read
    plan = (("time", "2026-10-16T20:00:00+08:00"), ("order_direction", "做多"), ("order_type", "限价单"),
            ("entry_price", 9.5), ("stop_loss_price", 9.0), ("take_profit_price", 10.0), ("invalidation_condition", "x"))
    ctx = build_continuity_context(frame=synthetic_frame(previous_plan_known=True, previous_plan=plan, previous_plan_bars=2),
                                   stage1_json={"direction": "bullish"})
    assert ctx["previous_source"] == "workbench" and ctx["bars_since"] == 2 and ctx["has_previous_plan"]
    none = build_continuity_context(frame=synthetic_frame(previous_plan_known=True), stage1_json={"direction": "neutral"})
    assert none["has_previous_plan"] is False


def test_a_one_sided_down_market_says_no_buy_not_short():
    text = render_continuity_prompt_block({"has_previous_plan": False, "direction": "neutral", "always_in_branch": "AIS"})
    assert "A 股不能做空：**不下单**" in text and "仅允许做空侧" not in text
