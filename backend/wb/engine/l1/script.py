"""通达信 / 同花顺 formula scripts (指标库, design 2026-10-10 §3): one statement per `;` —
`NAME:=expr` a variable, `NAME:expr, ATTR, …` a line drawn, a bare expression (a line named 线1, 线2 …) or a drawing
function (STICKLINE, DRAWICON, DRAWTEXT, DRAWNUMBER, DRAWBAND, and ours DRAWZONE, DRAWLEVEL). Names may be Chinese and
are not case-sensitive; a parameter is written by its bare name (N) or as $N; an output may take a function's name
(通达信's own MACD formula ends `MACD:(DIF-DEA)*2,COLORSTICK;`), not a field's. `{…}` and `//` are comments; full-width
；：，（） and `&&` / `||` are read as 通达信 writes them. The statements compile leniently (compiler.py: a condition is
1 / 0 …) into one Program; `Item`s say how each output is drawn.

Names the system has no data for (同花顺 Level-2 and the like) are collected over the whole script and said at once,
with the script as it could still be drawn without the statements that need them (`reduced`)."""
import re
from dataclasses import dataclass, field

from wb.engine.l1 import ast as A
from wb.engine.l1.compiler import CompileError, Compiler, Program, Val, _load_functions
from wb.engine.l1.fields import lookup
from wb.engine.l1.parser import ParseError, parse
from wb.engine.l1.units import U
from wb.registry import FUNCTIONS

NAME = r"[A-Za-z_㐀-鿿][A-Za-z0-9_㐀-鿿]*"
_HEAD = re.compile(rf"^\s*(?P<name>{NAME})\s*(?P<op>:=|:)")
_FULL = str.maketrans({"；": ";", "：": ":", "，": ",", "（": "(", "）": ")", "＝": "=", "＞": ">", "＜": "<", "＋": "+",
                       "－": "-", "＊": "*", "／": "/", "＇": "'", "＂": '"', "　": " "})

# 通达信's colours (as RGB); COLOR + six hex digits is 通达信's BGR order
COLORS = {"BLACK": "#000000", "BLUE": "#0000ff", "GREEN": "#00a000", "CYAN": "#00c0c0", "RED": "#ff0000",
          "MAGENTA": "#ff00ff", "BROWN": "#a52a2a", "LIGRAY": "#c0c0c0", "GRAY": "#808080", "LIBLUE": "#4080ff",
          "LIGREEN": "#40c040", "LICYAN": "#40e0e0", "LIRED": "#ff6060", "LIMAGENTA": "#ff80ff", "WHITE": "#ffffff",
          "YELLOW": "#e0b000", "UP": "up", "DOWN": "down"}   # COLORUP / COLORDOWN: your 涨 / 跌 colours (ours, not 通达信's)
STYLES = {"NODRAW", "DOTLINE", "DASHLINE", "POINTDOT", "CIRCLEDOT", "CROSSDOT", "STICK", "COLORSTICK", "VOLSTICK",
          "LINESTICK", "NOTEXT", "NOTITLE", "DRAWABOVE", "NOFRAME"}
# a drawing function's parts, and how many of them must be written
DRAWS = {"STICKLINE": (("COND", "P1", "P2", "WIDTH", "EMPTY"), 3), "DRAWICON": (("COND", "PRICE", "ICON"), 3),
         "DRAWTEXT": (("COND", "PRICE", "TEXT"), 3), "DRAWNUMBER": (("COND", "PRICE", "NUMBER"), 3),
         "DRAWBAND": (("V1", "COLOR1", "V2", "COLOR2"), 4), "DRAWZONE": (("COND", "TOP", "BOTTOM", "END", "OFFSET", "MAXN"), 3),
         "DRAWLEVEL": (("COND", "PRICE", "END", "OFFSET", "MAXN"), 2)}
NOT_YET = {"PARTLINE", "DRAWLINE", "PLOYLINE", "POLYLINE", "DRAWKLINE", "DRAWSL", "DRAWGBK", "DRAWBMP", "FILLRGN",
           "DRAWTEXT_FIX", "DRAWNUMBER_FIX", "DRAWRECTREL", "DRAWCOLORLINE", "DRAWCOLORKLINE", "STRIP", "DRAWSTICK"}

# names 通达信 / 同花顺 know that this system has no data for, and why (said once, with every name)
_L2 = "这个系统没有同花顺 / 通达信的 Level-2 数据（逐笔、委托队列、主动买卖），这些只能在同花顺里算"
UNSUPPORTED: dict[str, str] = {
    **{f"{p}{k}": _L2 for p in ("BIGBUYCOUNT", "BIGSELLCOUNT", "WAITBUYCOUNT", "WAITSELLCOUNT", "SMALLBUYCOUNT",
                                "SMALLSELLCOUNT", "BIGBUYTICK", "BIGSELLTICK", "WAITBUYTICK", "WAITSELLTICK",
                                "BIGBUYMONEY", "BIGSELLMONEY", "WAITBUYMONEY", "WAITSELLMONEY") for k in ("", "1", "2", "3", "4")},
    **{k: _L2 for k in ("ZJLR", "ZJLC", "L2_AMO", "L2_VOL", "BUYVOL", "SELLVOL", "TOTALBUYVOL", "TOTALSELLVOL",
                        "BIGBUYVOL", "BIGSELLVOL", "DDX", "DDY", "DDZ")},
    **{k: "内盘、外盘要逐笔成交数据，这个系统没有" for k in ("OUTVOL", "INVOL", "BUYVOLUME", "SELLVOLUME")},
    "ZYLTGB": "数据里没有自由流通股本；流通股本用 CAPITAL（股）",
    "FINANCE": "通达信的 FINANCE(n) 不支持；估值用 PE、PB、TOTAL_MV、CIRC_MV、CAPITAL 这些字段",
    "DYNAINFO": "实时行情函数 DYNAINFO 不支持（图上的最后一根就是盘中行情）",
    **{k: "大盘只有收盘 IDX_C" for k in ("INDEXC", "INDEXO", "INDEXH", "INDEXL", "INDEXV", "INDEXA")},
    "ISLASTBAR": "ISLASTBAR 会让历史上的值跟着最后一根变，不支持",
    "CURRBARSCOUNT": "CURRBARSCOUNT 要知道之后还有几根，会用到未来，不支持",
    "TOTALBARSCOUNT": "TOTALBARSCOUNT 不支持（用 BARSCOUNT(C)）",
    "DMA": "通达信的 DMA(X, A) 动态移动平均还不支持（指标 DMA 平行线差在内置指标里）",
}
ALIASES = {"HSL": "TURNOVER", "CAPITAL": "FLOAT_SHARE"}   # 通达信's field names spelled otherwise here (VOL is V: shares, not 手)


@dataclass
class Stmt:
    kind: str                 # "var" | "line" | "draw"
    name: str | None          # as written (a line without one: None)
    text: str                 # the expression, or the drawing call
    attrs: list[str]          # upper case
    line: int                 # 1-based line of the statement in the script
    func: str | None = None   # a drawing's function (upper case)
    node: A.Node | None = None

    def render(self) -> str:
        head = f"{self.name}:=" if self.kind == "var" else f"{self.name}:" if self.name else ""
        return f"{head}{self.text}{''.join(f',{a}' for a in self.attrs)};"


@dataclass
class Item:
    """One thing drawn: `cols` maps its parts (value; cond, price, p1, p2, top, bottom, offset, v1, v2) to outputs."""
    kind: str      # line | dots | stick | colorstick | volstick | linestick | stickline | icon | text | number | band | zone | level
    name: str
    cols: dict[str, str] = field(default_factory=dict)
    color: str | None = None
    color2: str | None = None
    width: int = 1
    dash: bool = False
    hidden: bool = False      # NODRAW: its value in the legend only
    icon: int | None = None
    text: str | None = None
    empty: bool = False       # STICKLINE hollow
    end: int = 1              # DRAWZONE / DRAWLEVEL: how it ends (0 never, 1 passed through, 2 touched, 3 closed beyond)
    maxn: int = 0             # DRAWZONE / DRAWLEVEL: only the latest N drawn (0: all)
    price: bool = False       # in prices: it can lie over the candles
    line: int = 0


@dataclass
class Compiled:
    program: Program | None             # None while anything is wrong
    items: list[Item]
    errors: list[dict]                  # {line, message}
    unsupported: dict[str, list[str]]   # why -> the names, in the order met
    reduced: str | None                 # the script without the statements that need what is missing
    price_only: bool = False            # every drawn thing is in prices: it can be laid over the candles


def strip(text: str) -> str:
    """Comments out (keeping line breaks), full-width punctuation and && / || as 通达信 writes them."""
    text = text.translate(_FULL)
    text = re.sub(r"\{[^}]*\}", lambda m: "\n" * m.group().count("\n"), text)
    text = re.sub(r"//[^\n]*", "", text)
    return text.replace("&&", " AND ").replace("||", " OR ")


def _top_split(text: str, sep: str) -> list[tuple[str, int]]:
    """Pieces of `text` split at `sep` outside brackets and quotes, each with its offset."""
    out, depth, quote, start = [], 0, None, 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == sep and depth == 0:
            out.append((text[start:i], start))
            start = i + 1
    out.append((text[start:], start))
    return out


def statements(text: str) -> tuple[list[Stmt], list[dict]]:
    """The script's statements, and the ones that do not read (their line and why)."""
    src = strip(text)
    out, errors = [], []
    for piece, at in _top_split(src, ";"):
        if not piece.strip():
            continue
        line = src.count("\n", 0, at + len(piece) - len(piece.lstrip())) + 1
        m = _HEAD.match(piece)
        name, kind = (m.group("name"), "var" if m.group("op") == ":=" else "line") if m else (None, "line")
        parts = _top_split(piece[m.end():] if m else piece, ",")
        expr, attrs = parts[0][0].strip(), [p.strip().upper() for p, _ in parts[1:] if p.strip()]
        if not expr:
            errors.append({"line": line, "message": f"{name or '这一句'} 后面没有公式"})
            continue
        call = re.match(rf"^({NAME})\s*\(", expr)
        func = call.group(1).upper() if call else None
        if func in NOT_YET:
            errors.append({"line": line, "message": f"画图函数 {func} 还不支持（支持 {'、'.join(DRAWS)}）"})
            continue
        if func in DRAWS:
            if kind == "var":
                errors.append({"line": line, "message": f"{func} 是画图函数，不能赋给变量"})
                continue
            kind = "draw"
        if bad := [a for a in attrs if not _attr_ok(a)]:
            errors.append({"line": line, "message": f"不认识的画线属性 {'、'.join(bad)}"})
            continue
        out.append(Stmt(kind, name if kind != "draw" else None, expr, attrs, line, func if kind == "draw" else None))
    return out, errors


def _attr_ok(a: str) -> bool:
    return a in STYLES or color_of(a) is not None or re.fullmatch(r"LINETHICK[0-9]", a) is not None


def color_of(word: str) -> str | None:
    """COLORRED → #ff0000; COLOR + BBGGRR (通达信's order) → #rrggbb; anything else None."""
    w = word.upper()
    if not w.startswith("COLOR"):
        return None
    rest = w[5:]
    if rest in COLORS:
        return COLORS[rest]
    if re.fullmatch(r"[0-9A-F]{6}", rest):
        return f"#{rest[4:6]}{rest[2:4]}{rest[0:2]}".lower()
    return None


def _names(node: A.Node, text: str) -> list[tuple[str, bool]]:
    """(name, is a call) of every name the node reads — not a quoted text (DRAWTEXT's), which parses as a name."""
    out = []

    def walk(n):
        if isinstance(n, A.Name):
            if text[n.pos:n.pos + 1] not in ("'", '"'):
                out.append((n.name, False))
        elif isinstance(n, A.Call):
            out.append((n.name, True))
            for a in n.args:
                walk(a)
            for _, v in n.kwargs:
                walk(v)
        elif isinstance(n, A.Attr):
            walk(n.obj)
        elif isinstance(n, (A.Unary, A.At)):
            walk(n.x)
        elif isinstance(n, A.Binary):
            walk(n.a)
            walk(n.b)
    walk(node)
    return out


def _field(name: str) -> bool:
    return lookup(ALIASES.get(name.upper(), name)) is not None


def _why_missing(name: str) -> str | None:
    key = name.upper()
    if _field(name) or key in FUNCTIONS:
        return None
    return UNSUPPORTED.get(key) or UNSUPPORTED.get(key.rstrip("0123456789"))


def compile_script(text: str, market: str, params: dict[str, float] | None = None, available: set[str] | None = None,
                   period: str = "D") -> Compiled:
    """Compile an indicator's formula for one market (and period: D, W or M) with its parameters' values."""
    _load_functions()
    params = {k.upper(): v for k, v in (params or {}).items()}
    stmts, errors = statements(text)
    for k in params:   # a parameter is read before a field of its name: C would be the number, not the close
        if _field(k):
            errors.append({"line": 0, "message": f"参数 {k} 和内置的字段同名：公式里的 {k} 会读成这个参数，换一个名字"})
    known = set(params)
    for s in stmts:   # `涨停=1` inside a call compares a variable: the parser is told which names are values
        try:
            s.node = parse(s.text, frozenset(known))
        except ParseError as e:
            errors.append({"line": s.line, "message": str(e)})
        if s.name:
            known.add(s.name.upper())
    broken = {s.name.upper() for s in stmts if s.node is None and s.name}   # its line said why; what reads it, not again
    stmts = [s for s in stmts if s.node is not None]

    # what each statement needs that the system does not have, directly or through a variable (said once, all of it)
    defined: set[str] = set(params) | broken
    unsupported: dict[str, list[str]] = {}
    blocked: set[int] = set()
    for i, s in enumerate(stmts):
        for name, _ in _names(s.node, s.text):
            key = name.upper()
            if key in defined:
                if key in tainted_names(stmts, blocked):
                    blocked.add(i)
                continue
            if (why := _why_missing(name)) is not None:
                names = unsupported.setdefault(why, [])
                if key not in names:
                    names.append(key)
                blocked.add(i)
        if s.name:
            key = s.name.upper()
            if key in defined:
                errors.append({"line": s.line, "message": f"{s.name} 前面已经定义过"})
            elif _field(s.name) or key in params:
                errors.append({"line": s.line, "message": f"{s.name} 是{'参数' if key in params else '内置的字段'}，换一个名字"})
            defined.add(key)

    env: dict[str, Val] = {k: Val("num", U.NUMBER, num=v, key=f"param:{k}={v!r}") for k, v in params.items()}
    comp = Compiler(market, params, available, period, lenient=True, env=env)
    for alias, target in ALIASES.items():
        if any(n.upper() == alias and not call for s in stmts for n, call in _names(s.node, s.text)):
            try:
                comp.env[alias] = comp.compile(A.Name(target))
            except CompileError as e:
                errors.append({"line": 0, "message": f"{alias}：{e}"})
    items: list[Item] = []
    failed = broken | {s.name.upper() for i, s in enumerate(stmts) if i in blocked and s.name}
    lines = 0
    for i, s in enumerate(stmts):
        if i in blocked or any(n.upper() in failed for n, _ in _names(s.node, s.text)):
            if s.name:
                failed.add(s.name.upper())
            continue
        try:
            if s.kind == "draw":
                items.append(_draw(comp, s, len(items)))
                continue
            raw = comp.visit(s.node)   # a variable may hold a function's several outputs: ST:=SUPERTREND(10,3); … ST.DIR
            v = raw if s.kind == "var" and raw.kind == "multi" else comp.value(raw, s.node)
            if s.kind == "var":
                comp.env[s.name.upper()] = v
                continue
            lines += 1
            col = f"o{len(items)}"
            comp.add_val(col, v)
            if s.name:
                comp.env[s.name.upper()] = v
            items.append(_line_item(s, s.name or f"线{lines}", col, v))
        except (CompileError, ValueError) as e:
            errors.append({"line": s.line, "message": str(e)})
            if s.name:
                failed.add(s.name.upper())
    keep = [s for i, s in enumerate(stmts) if i not in blocked and not (s.name and s.name.upper() in failed)]
    reduced = "\n".join(s.render() for s in keep) if unsupported and any(s.kind != "var" for s in keep) else None
    if not items and not errors and not unsupported:
        errors.append({"line": 0, "message": "没有要画的东西：至少写一条 名字:公式;（不是 := ）或一个画图函数"})
    program = comp.program() if not errors and not unsupported else None
    drawn = [it for it in items if not it.hidden]
    return Compiled(program, items, sorted(errors, key=lambda e: e["line"]), unsupported, reduced,
                    bool(drawn) and all(it.price for it in drawn))


def tainted_names(stmts: list[Stmt], blocked: set[int]) -> set[str]:
    return {stmts[i].name.upper() for i in blocked if stmts[i].name}


def _line_item(s: Stmt, name: str, col: str, v: Val) -> Item:
    a = s.attrs
    # a dot style wins over a stick: 通达信's own SAR is SAR(N,S,M),CIRCLEDOT,COLORSTICK; — dots on the candles, not columns
    if any(k in a for k in ("POINTDOT", "CIRCLEDOT", "CROSSDOT")):
        kind = "dots"
    else:
        kind = next((k.lower() for k in ("COLORSTICK", "VOLSTICK", "LINESTICK", "STICK") if k in a), "line")
    width = next((int(x[9:]) for x in a if x.startswith("LINETHICK")), 1)
    return Item(kind, name, {"value": col}, next((c for c in map(color_of, a) if c), None), None, max(1, width),
                "DOTLINE" in a or "DASHLINE" in a, "NODRAW" in a,
                price=v.unit in (U.PRICE, U.RAW_PRICE) and kind in ("line", "dots"), line=s.line)


_KIND = {"STICKLINE": "stickline", "DRAWICON": "icon", "DRAWTEXT": "text", "DRAWNUMBER": "number", "DRAWBAND": "band",
         "DRAWZONE": "zone", "DRAWLEVEL": "level"}


def _draw(comp: Compiler, s: Stmt, k: int) -> Item:
    roles, need = DRAWS[s.func]
    args = s.node.args
    if not need <= len(args) <= len(roles) or s.node.kwargs:
        written = f"{need}–{len(roles)}" if need < len(roles) else str(len(roles))
        raise CompileError(f"{s.func} 写 {written} 个参数（{', '.join(roles)}），这里有 {len(args)} 个")
    item = Item(_KIND[s.func], s.func, color=next((c for c in map(color_of, s.attrs) if c), None),
                price=s.func != "DRAWBAND", line=s.line)

    def number(role: str, arg: A.Node) -> float:
        v = comp.compile(arg)
        if v.kind != "num" or isinstance(v.num, tuple):
            raise CompileError(f"{s.func} 的 {role} 要写数字")
        return v.num

    for role, arg in zip(roles, args, strict=False):
        if role in ("COLOR1", "COLOR2"):
            c = color_of(arg.name) if isinstance(arg, A.Name) else None
            if c is None:
                raise CompileError(f"{s.func} 的 {role} 要写颜色，例如 COLORRED")
            setattr(item, "color" if role == "COLOR1" else "color2", c)
        elif role == "TEXT":
            if not (isinstance(arg, A.Name) and s.text[arg.pos:arg.pos + 1] in ("'", '"')):
                raise CompileError("DRAWTEXT 的文字要写在引号里，例如 '买'")
            item.text = arg.name
        elif role == "ICON":
            item.icon = int(number(role, arg))
        elif role == "WIDTH":
            item.width = max(1, int(number(role, arg)))
        elif role == "EMPTY":
            item.empty = number(role, arg) != 0
        elif role == "END":
            if (end := number(role, arg)) not in (0, 1, 2, 3):
                raise CompileError(f"{s.func} 的结束方式写 0、1、2 或 3")
            item.end = int(end)
        elif role == "MAXN":
            item.maxn = max(0, int(number(role, arg)))
        else:
            v = comp.compile(arg)
            v = comp._cond(v) if role == "COND" else comp._num(v)
            col = f"o{k}_{role.lower()}"
            comp.add_val(col, v)
            item.cols[role.lower()] = col
    return item
