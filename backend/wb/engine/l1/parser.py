"""通达信-style formula parser (spec §5.3): a tokenizer plus a precedence-climbing parser.

    OR < AND < NOT < comparison < + - < * / < unary minus < postfix (.attr, @stage) < atom

`=` and `<>` are accepted as 通达信 writes them; keywords are case-insensitive. Errors name the
character position in Chinese, because rules are written by hand and by the AI assistant."""
import re

from wb.engine.l1.ast import At, Attr, Binary, Call, Name, Node, Num, Param, Unary


class ParseError(ValueError):
    def __init__(self, msg: str, pos: int):
        super().__init__(f"第 {pos + 1} 个字符：{msg}")
        self.pos = pos


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|\d+(?:[eE][-+]?\d+)?)
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<param>\$[A-Za-z_][A-Za-z0-9_]*)
  | (?P<name>[A-Za-z_\u3400-\u9fff][A-Za-z0-9_\u3400-\u9fff]*)   # 中文名字 too: an indicator's variables (指标库)
  | (?P<op><=|>=|==|!=|<>|[-+*/<>=(),.\[\]@:])
""", re.X)
_KEYWORDS = {"AND", "OR", "NOT"}
_CMP = {">": ">", "<": "<", ">=": ">=", "<=": "<=", "==": "==", "=": "==", "!=": "!=", "<>": "!="}


def _tokens(text: str) -> list[tuple[str, str, int]]:
    out, i = [], 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            raise ParseError(f"不认识的字符 {text[i]!r}", i)
        kind = m.lastgroup
        if kind != "ws":
            val = m.group()
            if kind == "name" and val.upper() in _KEYWORDS:
                kind, val = "kw", val.upper()
            out.append((kind, val, i))
        i = m.end()
    out.append(("end", "", len(text)))
    return out


class _Parser:
    def __init__(self, text: str, known: frozenset[str] = frozenset()):
        self.toks = _tokens(text)
        self.i = 0
        self.known = known   # an indicator's variables and parameters (upper case): `涨停=1` compares, it names nothing

    @property
    def tok(self):
        return self.toks[self.i]

    def take(self, kind: str, val: str | None = None):
        k, v, p = self.tok
        if k != kind or (val is not None and v != val):
            if k == "end":
                raise ParseError("公式没有写完" + (f"，缺少 {val!r}" if val else ""), p)
            raise ParseError(f"这里应该是 {val!r}，却是 {v!r}" if val else f"这里不应出现 {v!r}", p)
        self.i += 1
        return v, p

    def at(self, kind: str, *vals: str) -> bool:
        k, v, _ = self.tok
        return k == kind and (not vals or v in vals)

    def parse(self) -> Node:
        node = self.or_()
        if not self.at("end"):
            _, v, p = self.tok
            extra = {")": "（括号不配对）", ":": "（函数的选项写成 名字=值，例如 MEAN(V, of=rally)）"}.get(v, "")
            raise ParseError(f"多余的 {v!r}{extra}", p)
        return node

    def or_(self):
        node = self.and_()
        while self.at("kw", "OR"):
            _, p = self.take("kw")
            node = Binary("OR", node, self.and_(), pos=p)
        return node

    def and_(self):
        node = self.not_()
        while self.at("kw", "AND"):
            _, p = self.take("kw")
            node = Binary("AND", node, self.not_(), pos=p)
        return node

    def not_(self):
        if self.at("kw", "NOT"):
            _, p = self.take("kw")
            return Unary("NOT", self.not_(), pos=p)
        return self.cmp()

    def cmp(self):
        node = self.add()
        if self.at("op", *_CMP):
            v, p = self.take("op")
            node = Binary(_CMP[v], node, self.add(), pos=p)
        return node

    def add(self):
        node = self.mul()
        while self.at("op", "+", "-"):
            v, p = self.take("op")
            node = Binary(v, node, self.mul(), pos=p)
        return node

    def mul(self):
        node = self.unary()
        while self.at("op", "*", "/"):
            v, p = self.take("op")
            node = Binary(v, node, self.unary(), pos=p)
        return node

    def unary(self):
        if self.at("op", "-"):
            _, p = self.take("op")
            return Unary("-", self.unary(), pos=p)
        if self.at("op", "+"):
            self.take("op")
            return self.unary()
        return self.postfix()

    def postfix(self):
        node = self.atom()
        while True:
            if self.at("op", "."):
                self.take("op", ".")
                v, p = self.take("name")
                node = Attr(node, v, pos=p)
            elif self.at("op", "@"):
                self.take("op", "@")
                v, p = self.take("name")
                node = At(node, v, pos=p)
            else:
                return node

    def atom(self):
        k, v, p = self.tok
        if k == "num":
            self.i += 1
            return Num(float(v) if any(c in v for c in ".eE") else int(v), pos=p)
        if k == "param":
            self.i += 1
            return Param(v[1:], pos=p)
        if k == "str":  # TF("W", ...): a quoted word
            self.i += 1
            return Name(v[1:-1], pos=p)
        if k == "name":
            self.i += 1
            if self.at("op", "("):
                return self.call(v, p)
            return Name(v, pos=p)
        if self.at("op", "["):
            return self.list_()
        if self.at("op", "("):
            self.take("op", "(")
            node = self.or_()
            if not self.at("op", ")"):
                raise ParseError("缺少右括号", self.tok[2])
            self.take("op", ")")
            return node
        if k == "end":
            raise ParseError("公式没有写完", p)
        if v == ":":
            raise ParseError("这里不应出现 ':'（函数的选项写成 名字=值，例如 MEAN(V, of=rally)、MEAN(V, last=10)）", p)
        raise ParseError(f"这里不应出现 {v!r}", p)

    def call(self, name: str, pos: int):
        self.take("op", "(")
        args, kwargs = [], []
        while not self.at("op", ")"):
            sep = self.toks[self.i + 1] if self.i + 1 < len(self.toks) else ("", "", 0)
            if (self.at("name") and sep[0] == "op" and sep[1] in ("=", ":") and not _is_value(self.tok[1])
                    and self.tok[1].upper() not in self.known):
                key, _ = self.take("name")
                self.take("op", sep[1])   # `of=rally`, or `of: rally` as YAML habits write it
                kwargs.append((key, self.or_()))
            else:
                if kwargs:
                    raise ParseError("位置参数要写在 key=value 参数前面", self.tok[2])
                args.append(self.or_())
            if not self.at("op", ")"):
                self.take("op", ",")
        self.take("op", ")")
        return Call(name, tuple(args), tuple(kwargs), pos=pos)

    def list_(self):
        _, p = self.take("op", "[")
        items = []
        while not self.at("op", "]"):
            tok_k, tok_v, tok_p = self.tok
            neg = False
            if self.at("op", "-"):
                self.take("op", "-")
                neg = True
                tok_k, tok_v, tok_p = self.tok
            if tok_k != "num":
                raise ParseError("列表里只能写数字", tok_p)
            self.i += 1
            val = float(tok_v) if any(c in tok_v for c in ".eE") else int(tok_v)
            items.append(-val if neg else val)
            if not self.at("op", "]"):
                self.take("op", ",")
        self.take("op", "]")
        return Num(tuple(items), pos=p)


def _is_value(name: str) -> bool:
    """A field or a function: `IF(C = O, 1, 0)` compares, `MEAN(V, last=10)` names an argument."""
    from wb.engine.l1.fields import lookup
    from wb.registry import FUNCTIONS, autoload

    autoload("wb.engine.l1.functions")
    return lookup(name) is not None or name.upper() in FUNCTIONS


def parse(text: str, known: frozenset[str] = frozenset()) -> Node:
    """`known`: names that are values here besides fields and functions (an indicator's variables and parameters)."""
    return _Parser(text, known).parse()
