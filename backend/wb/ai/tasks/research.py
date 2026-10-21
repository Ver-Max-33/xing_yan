"""个股研究 (spec §10.3): the model reads a stock's evidence pack (wb.ai.evidence) and answers in a fixed shape — a summary,
what supports and what argues against (each citing the facts it rests on; an inference says it is one and cites what it
is inferred from), what to check, what would show it wrong, when to look again; which way the evidence leans (a summary
of the evidence, the thing §10.4 settles — not advice); and against the last research, whether this one goes the same
way (同向), the other way (反手), or a condition the last one named has happened (失效). The shape has no rating, target
price, position or stop: the system does no trading (§1.6, principle 7). A citation of a fact the pack does not have, or
a relation that does not fit the last research, goes back to the model once with the reason; numbers that no cited fact
holds, a cited fact dated after the pack's day, or wording that advises a trade mark the answer 需复核: kept and shown
as such, never as a conclusion."""
import hashlib
import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from wb.ai.client import LLM

REFS = {"type": "array", "items": {"type": "string"}}
ITEM = {"type": "object", "additionalProperties": False, "required": ["text", "refs", "inference"],
        "properties": {"text": {"type": "string"}, "refs": REFS, "inference": {"type": "boolean"}}}
LEANS = ("支持", "反对", "相当")
VERDICTS = ("首次", "同向", "反手", "失效")
OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "support", "against", "to_verify", "invalidation", "next_review", "lean", "continuity"],
    "properties": {"summary": {"type": "string"}, "support": {"type": "array", "items": ITEM},
                   "against": {"type": "array", "items": ITEM}, "to_verify": {"type": "array", "items": {"type": "string"}},
                   "invalidation": {"type": "array", "items": ITEM}, "next_review": {"type": "array", "items": {"type": "string"}},
                   "lean": {"type": "string", "enum": list(LEANS)},
                   "continuity": {"type": "object", "additionalProperties": False, "required": ["verdict", "text", "refs"],
                                  "properties": {"verdict": {"type": "string", "enum": list(VERDICTS)}, "text": {"type": "string"},
                                                 "refs": REFS}}},
}
CITED = ("support", "against", "invalidation")
TRADE = ("买入", "卖出", "加仓", "减仓", "建仓", "清仓", "止损", "止盈", "目标价", "仓位", "评级", "抄底", "低吸", "追高", "做多", "做空")
SYSTEM = """你是个股研究助手。只根据用户给的证据包回答，证据包里每条事实前面有 [编号]。

要求：
- 支持、反对、失效条件的每一条都要在 refs 里写出它依据的事实编号（证据包里有的编号），可以写多个；
- 文字里出现的数字要和所引用事实里的数字一致，不要自己算新的数，不要用证据包以外的信息；
- 推断（“可能”“如果……”）把 inference 写成 true，refs 写它从哪些事实推出来；直接陈述事实的写 false；
- lean：把支持和反对放在一起掂量，哪边的理由更强（支持 / 反对 / 相当）——这是对证据的概括，不是买卖建议；
- continuity：没有上次研究时写 {"verdict": "首次", "text": "", "refs": []}；有上次研究时对照它：同向（倾向没变，上次的失效条件都没发生）、反手（倾向反过来了，说明是什么变了）、失效（上次的某条失效条件已经发生），text 写理由，refs 写依据的事实编号；
- 不写买入、卖出、加仓、减仓、建仓、清仓、评级、目标价、仓位、止损止盈价位——这个系统不做交易，只做研究；
- 证据包说某个来源的资料没给你时，写“资料不足”，不要编；
- 日文资料用中文转述，需要时在括号里保留原文的关键词；
- 用中文。

只输出 json，格式：
{"summary": "两三句话的结论摘要", "support": [{"text": "支持的事实和理由", "refs": ["px.close"], "inference": false}],
 "against": [{"text": "反对的事实或风险", "refs": ["fin.2026H1"], "inference": false}], "to_verify": ["需要另外核实的问题"],
 "invalidation": [{"text": "什么情况说明这个判断错了", "refs": ["box.long"], "inference": false}], "next_review": ["下次在什么情况下再看"],
 "lean": "相当", "continuity": {"verdict": "首次", "text": "", "refs": []}}"""
# a number; a hyphen right after a digit is a separator ("2026-08-15", "1.5-2.0"), not a minus sign
NUMBER = re.compile(r"(?:(?<![\d.])-)?(?<![\d.,])\d[\d,]*(?:\.\d+)?")


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str
    refs: list[str]
    inference: bool


class _Relation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    verdict: Literal["首次", "同向", "反手", "失效"]
    text: str
    refs: list[str]


class _Answer(BaseModel):
    """OUTPUT as types: DeepSeek's JSON mode does not hold the model to the schema, so each field is checked here."""
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str
    support: list[_Item]
    against: list[_Item]
    to_verify: list[str]
    invalidation: list[_Item]
    next_review: list[str]
    lean: Literal["支持", "反对", "相当"]
    continuity: _Relation


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _user(pack: dict, previous: dict | None) -> str:
    lines = [f"股票：{pack.get('name') or ''} {pack['listing_id']}；证据截至 {pack['day']}。", "证据包："]
    lines += [f"[{f['id']}] {f['text']}（{f['as_of']}）" for f in pack["facts"]]
    if pack.get("left_out"):
        lines.append(f"以下来源的资料没有给你（按设置没给，或这次没取到），涉及它们的写“资料不足”：{'、'.join(pack['left_out'])}")
    if previous:
        o = previous["output"]
        lines += [f"上次研究（{previous['day']}）：", f"- 结论摘要：{o['summary']}", f"- 倾向：{o['lean']}",
                  *[f"- 失效条件：{i['text']}" for i in o.get("invalidation", [])],
                  "请在 continuity 里对照上次研究。"]
    else:
        lines.append("这是这只股票的第一次研究，continuity 写首次。")
    lines.append("请按要求输出 json。")
    return "\n".join(lines)


def _found(text: str) -> list[tuple[str, float]]:
    """Each number as written and its value; full-width figures (１２，３４５ in Japanese material) read as 12,345."""
    return [(x, float(x.replace(",", ""))) for x in NUMBER.findall(unicodedata.normalize("NFKC", text))]


def _numbers(text: str) -> list[float]:
    return [n for _, n in _found(text)]


def _held(n: float, held: list[float]) -> bool:
    """`n` is one of `held` (within 0.5%); a sign said in words ("下滑 2.0%" for -2.0%) is the same number."""
    return any(abs(abs(n) - abs(h)) <= max(abs(h) * 0.005, 0.05) for h in held)


def _check_shape(out: dict, ids: set[str], previous: dict | None) -> dict:
    """What goes back to the model: the shape, every citation one the pack has, and a relation that fits the last
    research."""
    if not isinstance(out, dict) or set(OUTPUT["required"]) - set(out):
        raise ValueError(f"要有这几项：{'、'.join(OUTPUT['required'])}")
    try:
        _Answer.model_validate(out)
    except ValidationError as e:
        raise ValueError("格式不对：" + "；".join(f"{'.'.join(map(str, x['loc']))} {x['msg']}" for x in e.errors()[:6])) from None
    c = out["continuity"]
    for k in CITED:
        for item in out[k]:
            if not item.get("refs"):
                raise ValueError(f"{k} 的每一条都要写出依据的事实编号（refs），推断也要写它从哪些事实推出来")
    for refs in [i["refs"] for k in CITED for i in out[k]] + [c.get("refs") or []]:
        bad = [r for r in refs if r not in ids]
        if bad:
            raise ValueError(f"引用了证据包里没有的编号：{'、'.join(bad)}；只能用证据包里的编号")
    if previous is None and c["verdict"] != "首次":
        raise ValueError("这是第一次研究，continuity.verdict 写首次")
    if previous is not None:
        last = previous["output"]["lean"]
        if c["verdict"] == "首次":
            raise ValueError(f"有上次研究（{previous['day']}），continuity 要对照它写同向、反手或失效")
        if c["verdict"] == "同向" and out["lean"] != last:
            raise ValueError(f"倾向从上次的“{last}”变成了“{out['lean']}”，不是同向")
        if c["verdict"] == "反手" and out["lean"] == last:
            raise ValueError(f"倾向和上次一样是“{last}”，不是反手")
        if c["verdict"] in ("反手", "失效") and not c.get("refs"):
            raise ValueError("反手、失效要在 continuity.refs 里写出说明它的事实编号")
    return out


def flags(out: dict, pack: dict) -> list[str]:
    """Why the answer needs a second look: a number no cited fact holds (for uncited text: no fact at all), or wording
    that advises a trade."""
    facts = {f["id"]: f for f in pack["facts"]}
    every = [n for f in pack["facts"] for n in _numbers(f["text"])]
    out_flags = []
    c = out["continuity"]
    cites = [i["refs"] for k in CITED for i in out[k]] + [c.get("refs") or []]
    for r in sorted({r for refs in cites for r in refs}):
        if facts[r]["source"] != "user" and facts[r]["as_of"] > pack["day"]:   # your notes are yours, not data known later
            out_flags.append(f"引用的事实 {r} 晚于证据包的日子（{facts[r]['as_of']} > {pack['day']}）")
    held = lambda refs: [n for r in refs for n in _numbers(facts[r]["text"])]  # noqa: E731
    texts = [(out["summary"], every), (c.get("text") or "", held(c["refs"]) if c.get("refs") else every)]
    for k in CITED:
        texts += [(i["text"], held(i["refs"])) for i in out[k]]
    texts += [(t, every) for k in ("to_verify", "next_review") for t in out[k]]
    for text, held in texts:
        for raw, n in _found(text):
            if not _held(n, held):
                out_flags.append(f"数字 {raw} 在所引用的事实里找不到：{text}")
        for w in TRADE:
            if any(not _quoted(text, i, len(w), pack["facts"]) for i in _at(text, w)):
                out_flags.append(f"含交易建议（{w}）：{text}")
    return out_flags


def _at(text: str, w: str) -> list[int]:
    return [m.start() for m in re.finditer(re.escape(w), text)]


def _quoted(text: str, i: int, n: int, facts: list[dict]) -> bool:
    """The word at text[i:i+n] with a few characters around it is word for word in a fact (an announcement title such
    as 主体信用评级结果): quoted evidence, not advice."""
    span = text[max(0, i - 4): i + n + 4]
    return any(span in f["text"] for f in facts)


def research(llm: LLM, pack: dict, previous: dict | None = None) -> dict:
    """The model's answer on the pack, checked against it and the last research ({day, output}) if there is one:
    {output, status (ok | 需复核), flags, call_id, cost, model}."""
    ids = {f["id"] for f in pack["facts"]}
    user = _user(pack, previous)
    a = llm.chat_json("research", SYSTEM, user, OUTPUT, "stock_research", lambda out: _check_shape(out, ids, previous),
                      template_hash=_hash(SYSTEM), retries=1,   # the inputs: the pack, and the last research when there is one
                      evidence_hash=pack["hash"] if previous is None else _hash(pack["hash"] + json.dumps(previous, sort_keys=True, ensure_ascii=False)))
    found = flags(a.value, pack)
    return {"output": a.value, "status": "需复核" if found else "ok", "flags": found, "call_id": a.call_id,
            "cost": a.cost, "model": a.model}
