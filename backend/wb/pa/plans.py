"""价格行为分析's plans (design 2026-10-20 §3, §5.1): the AI's own plan out of its accepted stage-2 answer, what PA's
program changed and the upstream rule behind each change, and the A-share guards on the final plan."""
import copy
import json
import math
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from pa_agent.ai import stage2_normalizer as norm
from pa_agent.ai.json_validator import (_escape_control_chars_in_json_strings, _strip_fences, _try_repair_json_syntax,
                                        coalesce_model_json_text)

ORDERS = ("限价单", "突破单", "市价单")
GATE = 40    # PA's window counts a plan below this confidence as no order opportunity (§2)
PRICES = ("entry_price", "stop_loss_price", "take_profit_price", "take_profit_price_2")
METRICS = "程序按 decision 三价校验未通过："     # PA's note on §10.3 when the prices fail its trade metrics
CENT = Decimal("0.01")

Change = TypedDict("Change", {"field": str, "before": object, "after": object, "reason": str})


def is_order(d: dict | None) -> bool:
    return isinstance(d, dict) and d.get("order_type") in ORDERS


def cents(x: float) -> float:
    return float(Decimal(str(x)).quantize(CENT, ROUND_HALF_UP))


def parse_reply(content: str, reasoning: str = "") -> dict:
    """A stage-2 reply read the way PA's validator reads it: fences off, then control characters escaped inside
    strings, then its syntax repair."""
    text = _strip_fences(coalesce_model_json_text(content, reasoning))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return json.loads(_escape_control_chars_in_json_strings(text))
        except json.JSONDecodeError:
            return json.loads(_try_repair_json_syntax(text, "stage2", allow_tail_inject=False) or text)


def ai_original(stage2_obj: dict, stage1_json: dict) -> dict:
    """§5.1: the plan in the AI's accepted answer before the program moves a price or changes the order — the head of
    PA's normalize_stage2, which only tidies the format (aliases, defaults) and honours a no the AI gave itself in its
    decision path (wait or reject at the end, 否 at §10.3, the §14 list)."""
    out = copy.deepcopy(stage2_obj)
    norm._unwrap_flat_stage2_decision(out)
    norm._hoist_terminal_from_decision(out)
    norm._repair_diagnosis_summary_and_decision(out, stage1_json=stage1_json)
    if isinstance(out.get("decision"), dict):
        norm._normalize_order_type_aliases(out["decision"])
    norm._ensure_decision_required_fields(out, stage1_json=stage1_json)
    if isinstance(out.get("decision"), dict):
        norm._truncate_decision_reasoning(out["decision"])
    norm._normalize_stage2_enum_aliases(out)
    norm._normalize_stage2_bar_analysis_enums(out, stage1_json=stage1_json)
    norm._coerce_decision_no_order(out)
    norm._repair_terminal_trade_node(out)
    return out["decision"]


def _moved(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) > 1e-9
    return a != b


def _node_reason(stage2_obj: dict, node: str, answer: str | None = None) -> str:
    """The reason on the first trace node with that id (or id prefix, ending in "."), answered `answer` if given."""
    for t in stage2_obj.get("decision_trace") or []:
        nid = str(t.get("node_id", "")).strip() if isinstance(t, dict) else ""
        if (nid.startswith(node) if node.endswith(".") else nid == node) and answer in (None, str(t.get("answer", "")).strip()):
            return str(t.get("reason") or "")
    return ""


def _no_reason(pa_final: dict, stage2_final: dict) -> str:
    end = str((stage2_final.get("terminal") or {}).get("node_id") or "").strip()
    if end == "continuity":
        why = str(pa_final.get("reasoning") or "").partition("【程序连续性守卫】")[2].partition("；改为不下单")[0]
        return f"前后衔接守卫：{why}"
    if end == "10.3":
        why = _node_reason(stage2_final, "10.3").partition(METRICS)[2].removesuffix("，已改为不下单。）")
        if why.startswith("limit "):   # K1 already traded through the plan's prices: its own words say it (pa.ts)
            return why
        return "盈亏比、胜率或价格顺序没过" + (f"：{why}" if why else "")
    label = str((stage2_final.get("terminal") or {}).get("label") or "")
    if end in ("6.3", "11.3") and label.startswith(("区间中部不交易", "买入价在区间中部")):   # the close, or the buy price,
        return label                                                                    # mid-range: said in its numbers
    return "程序改成不买"


def program_changes(ai: dict, pa_final: dict, stage2_final: dict) -> list[Change]:
    """§5.1: each place PA's program overrode the AI's own plan, named by the upstream rule that did it."""
    if is_order(ai) and not is_order(pa_final):
        return [Change(field="order_type", before=ai["order_type"], after=pa_final.get("order_type"),
                       reason=_no_reason(pa_final, stage2_final))]
    changes = []
    for f in ("order_type", *PRICES):
        before, after = ai.get(f), pa_final.get(f)
        if not _moved(before, after):
            continue
        if f == "order_type" and before == "突破单" and after == "限价单" and not (ai.get("entry_basis_bar")
                                                                                and ai.get("entry_basis_extreme")):
            reason = "突破单没写依据哪根 K 线，程序按规定改成限价单"   # stage2_normalizer._coerce_breakout_without_basis
        elif f == "order_type" and is_order(ai):
            why = _node_reason(stage2_final, "11.", "是")
            reason = "按阶段定买法" + (f"：{why}" if why else "")
        elif f == "entry_price" and pa_final.get("order_type") == "突破单":
            reason = "突破买入价对齐到信号棒最高价加一跳"
        elif f == "stop_loss_price":   # PA's 1:1 cap: the stop as far from the entry as TP1 is, said in the stock's numbers
            reason = (f"第一止盈离买入价 {abs(pa_final['take_profit_price'] - pa_final['entry_price']):.2f} 元，"
                      "PA 规定止损也放这么远（第一止盈正好赚 1 倍风险）")
        else:
            reason = "程序调整"
        changes.append(Change(field=f, before=before, after=after, reason=reason))
    return changes


def _confidence(d: dict) -> int | None:
    """trade_confidence read as PA's window reads it (0–100), None when it is not a number."""
    try:
        return max(0, min(100, int(float(str(d.get("trade_confidence")).strip()))))
    except (ValueError, OverflowError):
        return None


def a_share_guard(decision: dict, *, next_limit_up: float | None) -> tuple[dict, list[Change]]:
    """§3: the A-share checks on PA's final plan — long only, its confidence gate, prices in cents (a breakout's entry
    rounded up, so it stays above the signal bar's high), no buy above the next trading day's limit-up. A 不买 clears
    the prices and the direction the way PA's own no-order rule does."""
    d, changes = copy.deepcopy(decision), []

    def no(reason: str) -> None:
        changes.append(Change(field="order_type", before=d["order_type"], after="不下单", reason=reason))
        norm._clear_decision_to_no_order(d)

    if not is_order(d):
        return d, changes
    conf = _confidence(d)
    if d.get("order_direction") == "做空":
        no("A 股不能做空")
    elif conf is None or conf < GATE:
        no(f"信心不到 {GATE}，它的界面里不算下单机会")
    else:
        for f in PRICES:
            v = d.get(f)
            if not isinstance(v, (int, float)):
                continue
            d[f] = math.ceil(round(v * 100, 6)) / 100 if f == "entry_price" and d["order_type"] == "突破单" else cents(v)
            if abs(d[f] - v) > 1e-9:
                changes.append(Change(field=f, before=v, after=d[f], reason="取到 0.01 元"))
        if next_limit_up is not None and (d.get("entry_price") or 0) > next_limit_up:
            no(f"买入价高于下一个交易日的涨停价 {next_limit_up:.2f}")
    return d, changes
