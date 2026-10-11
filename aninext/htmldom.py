"""A tiny HTML DOM + CSS selector engine, pure stdlib.

scraping without lxml/bs4 is entirely doable: `html.parser` gives us a token
stream and a few hundred lines give us a queryable tree.  Supported selector
syntax (a deliberate, useful subset of CSS):

    tag  *  .class  #id
    [attr]  [attr=value]  [attr*=sub]  [attr^=pre]  [attr$=suf]  [attr~=word]
    div p        (descendant)
    div > p      (child)
    h3 + p       (adjacent sibling)
    h3 ~ p       (general sibling)
    :first-child :last-child :nth-child(n|odd|even|an+b) :nth-of-type(...)
    :first-of-type :last-of-type :only-child :empty :root
    :not(sel)  :contains("text")  :matches(regex,flags)  :has(sel)
    a, b         (selector list)

Extraction expressions accepted by `extract()`:

    "h1.title"          -> text of first match
    "h1.title @href"    -> attribute of first match
    "img @data-src"     -> attributes fall through to src if missing
    "::text"            -> own text content
    "html"              -> inner HTML of node
"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser
from typing import Any, Iterable, Iterator, Sequence

VOID = {
    "area", "base", "basefont", "br", "col", "embed", "frame", "hr", "img",
    "input", "isindex", "keygen", "link", "meta", "param", "source", "track", "wbr",
}

_AUTO_CLOSE = {
    # tag -> tags that implicitly close it when the tag is opened
    "li": {"li"},
    "p": {"p"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr", "td", "th"},
    "option": {"option"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "thead": {"tbody", "tfoot"},
}

# block level elements implicitly terminate an open <p>
for _block in (
    "div", "ul", "ol", "dl", "table", "section", "article", "aside", "header",
    "footer", "nav", "blockquote", "pre", "form", "fieldset", "figure",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "address", "main",
):
    _AUTO_CLOSE.setdefault(_block, set()).add("p")
del _block


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "data", "is_text")

    def __init__(self, tag: str = "", attrs: dict[str, str] | None = None, is_text: bool = False) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node] = []
        self.parent: Node | None = None
        self.data = ""
        self.is_text = is_text

    # -- tree helpers ------------------------------------------------------

    def append(self, child: "Node") -> None:
        child.parent = self
        self.children.append(child)

    @property
    def elements(self) -> list["Node"]:
        return [c for c in self.children if not c.is_text]

    def child_elements(self) -> list["Node"]:
        return self.elements

    def iter(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            if not child.is_text:
                yield from child.iter()

    def descendants(self) -> Iterator["Node"]:
        for child in self.children:
            if child.is_text:
                continue
            yield child
            yield from child.descendants()

    # -- content -----------------------------------------------------------

    @property
    def classes(self) -> list[str]:
        return str(self.attrs.get("class", "")).split()

    def text_content(self, separator: str = " ") -> str:
        """All descendant text, de-duplicated whitespace."""
        chunks: list[str] = []

        def walk(n: Node) -> None:
            for c in n.children:
                if c.is_text:
                    if c.data.strip():
                        chunks.append(c.data)
                elif c.tag in ("br",):
                    chunks.append(separator)
                elif c.tag in ("script", "style"):
                    continue
                else:
                    walk(c)

        walk(self)
        text = separator.join(chunks)
        text = _html.unescape(text)
        text = re.sub(r"[ \t\r\f\v\u00a0]+", " ", text)
        text = re.sub(r"\s*\n\s*", separator if separator != " " else " ", text)
        return text.strip()

    def get(self, name: str, default: str = "") -> str:
        """Attribute lookup with the usual scrape-friendly fallbacks."""
        name = name.lower()
        if name in ("text", "textcontent"):
            return self.text_content()
        v = self.attrs.get(name)
        if v is None:
            for alt in _ATTR_ALIASES.get(name, ()):  # lazy-loading images, etc.
                if alt in self.attrs:
                    v = self.attrs[alt]
                    break
        if v is None and name in ("href", "src"):
            for style_attr in ("data-href", "data-url", "data-link", "data-lazy-src"):
                if style_attr in self.attrs:
                    v = self.attrs[style_attr]
                    break
        if v is None:
            return default
        return re.sub(r"\s+", " ", v).strip()

    def get_any(self, *names: str, default: str = "") -> str:
        for n in names:
            v = self.get(n)
            if v:
                return v
        return default

    @property
    def inner_html(self) -> str:
        return "".join(render(c) for c in self.children)

    def __repr__(self) -> str:  # pragma: no cover
        ident = f"#{self.attrs['id']}" if "id" in self.attrs else ""
        cls = "".join(f".{c}" for c in self.classes[:2])
        return f"<{self.tag}{ident}{cls}>"


_ATTR_ALIASES: dict[str, tuple[str, ...]] = {
    "src": ("data-src", "data-lazy-src", "data-original", "data-echo", "data-image", "srcset", "data-srcset"),
    "href": ("data-href", "data-url", "data-link"),
    "poster": ("data-poster", "data-image", "data-src", "data-lazy-src"),
    "alt": ("data-alt", "title"),
}


def render(node: Node) -> str:
    if node.is_text:
        return node.data
    tag = node.tag
    attrs = "".join(f' {k}="{_html.escape(str(v), quote=True)}"' for k, v in node.attrs.items())
    if tag in VOID:
        return f"<{tag}{attrs}>"
    return f"<{tag}{attrs}>{''.join(render(c) for c in node.children)}</{tag}>"


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]

    @property
    def current(self) -> Node:
        return self.stack[-1]

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        closers = _AUTO_CLOSE.get(tag)
        if closers:
            while len(self.stack) > 1 and self.stack[-1].tag in closers:
                self.stack.pop()

        node = Node(tag, {k.lower(): (v if v is not None else "") for k, v in attrs})
        self.current.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        node = Node(tag.lower(), {k.lower(): (v if v is not None else "") for k, v in attrs})
        self.current.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # stray end tag - ignore

    def handle_data(self, data: str) -> None:
        if data:
            text = Node(is_text=True)
            text.data = data
            self.current.append(text)

    def handle_comment(self, data: str) -> None:
        # keep comments out of text_content but remember hidden payloads
        if data and ("source" in data or "m3u8" in data):
            node = Node("#comment", is_text=False)
            node.data = data
            self.current.append(node)


def parse(source: str | bytes) -> Node:
    """Parse HTML into a Node tree. Never raises on malformed markup."""
    if isinstance(source, bytes):
        source = source.decode("utf-8", "replace")
    builder = _Builder()
    try:
        builder.feed(source)
        builder.close()
    except Exception:  # noqa: BLE001 - malformed html shouldn't kill a scrape
        pass
    return builder.root


# --------------------------------------------------------------------------
# selector engine
# --------------------------------------------------------------------------

class SelectorError(ValueError):
    pass


_SIMPLE_RE = re.compile(
    r"""
    (?P<tag>[*a-zA-Z][\w\-]*)                       # tag or *
    | \.(?P<cls>[-\w\u00a0-\uffff]+)                # .class
    | \#(?P<id>[-\w\u00a0-\uffff]+)                 # #id
    | \[(?P<attr>[^\]]+)\]                          # [attr op value]
    | :(?P<pseudo>[\w\-]+)(?:\((?P<arg>(?:[^()]|\([^()]*\))*)\))?
    """,
    re.VERBOSE,
)

_ATTR_RE = re.compile(
    r"""^\s*(?P<name>[-\w:]+)\s*(?:(?P<op>[~^$*|]?=)\s*(?P<val>.*?)\s*)?$"""
)


class _Simple:
    __slots__ = ("tag", "classes", "id_", "attrs", "pseudos")

    def __init__(self) -> None:
        self.tag: str | None = None
        self.classes: list[str] = []
        self.id_: str | None = None
        self.attrs: list[tuple[str, str | None, str | None]] = []
        self.pseudos: list[tuple[str, str | None]] = []

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Simple {self.tag} {self.classes} {self.id_}>"

    # -- matching ----------------------------------------------------------

    def matches(self, node: Node) -> bool:
        if node.is_text:
            return False
        if self.tag and self.tag != "*" and node.tag != self.tag:
            return False
        if self.id_ and node.attrs.get("id") != self.id_:
            return False
        if self.classes:
            have = set(node.classes)
            if not all(c in have for c in self.classes):
                return False
        for name, op, val in self.attrs:
            actual = node.attrs.get(name)
            if op is None:
                if actual is None:
                    return False
                continue
            if actual is None:
                return False
            actual_l = actual.lower()
            val_l = (val or "").lower()
            if op == "=" and actual_l != val_l:
                return False
            if op == "*=" and val_l not in actual_l:
                return False
            if op == "^=" and not actual_l.startswith(val_l):
                return False
            if op == "$=" and not actual_l.endswith(val_l):
                return False
            if op == "~=" and val_l not in actual_l.split():
                return False
            if op == "|=" and not (actual_l == val_l or actual_l.startswith(val_l + "-")):
                return False
        for name, arg in self.pseudos:
            if not _match_pseudo(node, name, arg):
                return False
        return True


_selectors_cache: dict[str, list[tuple[str, list[_Simple]]]] = {}


def _split_selector_list(selector: str) -> list[str]:
    parts, depth, quote, cur = [], 0, "", []
    for ch in selector:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _tokenize_compound(text: str) -> _Simple:
    simple = _Simple()
    pos = 0
    text = text.strip()
    if not text:
        raise SelectorError("empty selector")
    while pos < len(text):
        m = _SIMPLE_RE.match(text, pos)
        if not m or m.end() == pos:
            raise SelectorError(f"bad selector near {text[pos:]!r}")
        pos = m.end()
        if m.group("tag"):
            simple.tag = m.group("tag").lower()
        elif m.group("cls"):
            simple.classes.append(m.group("cls"))
        elif m.group("id"):
            simple.id_ = m.group("id")
        elif m.group("attr"):
            am = _ATTR_RE.match(m.group("attr"))
            if not am:
                raise SelectorError(f"bad attribute selector [{m.group('attr')}]")
            raw_val = am.group("val")
            if raw_val is not None and len(raw_val) >= 2 and raw_val[0] in "\"'" and raw_val[-1] == raw_val[0]:
                raw_val = raw_val[1:-1]
            simple.attrs.append((am.group("name").lower(), am.group("op"), raw_val))
        elif m.group("pseudo"):
            simple.pseudos.append((m.group("pseudo").lower(), m.group("arg")))
    return simple


def parse_selector(selector: str) -> list[tuple[str, list[_Simple]]]:
    """Compile a selector into a list of (combinator, [compound...]) groups."""
    selector = re.sub(r"\s+", " ", selector.strip())
    cached = _selectors_cache.get(selector)
    if cached is not None:
        return cached

    compiled: list[tuple[str, list[_Simple]]] = []
    for group in _split_selector_list(selector):
        parts: list[str] = []
        combinators: list[str] = []
        buf: list[str] = []
        depth = 0
        quote = ""
        i = 0
        while i < len(group):
            ch = group[i]
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = ""
                i += 1
                continue
            if ch in "\"'":
                quote = ch
                buf.append(ch)
            elif ch in "([":
                depth += 1
                buf.append(ch)
            elif ch in ")]":
                depth -= 1
                buf.append(ch)
            elif depth == 0 and ch in " >+~":
                if buf:
                    parts.append("".join(buf).strip())
                    buf = []
                j = i
                while j < len(group) and group[j] in " >+~":
                    j += 1
                comb = group[i:j].strip() or " "
                combinators.append(comb)
                i = j - 1
            else:
                buf.append(ch)
            i += 1
        if buf:
            parts.append("".join(buf).strip())
        parts = [p for p in parts if p]
        if not parts:
            raise SelectorError(f"bad selector: {selector!r}")

        sequence: list[Any] = []
        for idx, part in enumerate(parts):
            comb = combinators[idx - 1] if idx > 0 and idx - 1 < len(combinators) else None
            if comb is None:
                comb = " " if idx > 0 else ""
            if comb:
                sequence.append(comb)
            sequence.append(_tokenize_compound(part))
        compiled.append(("", sequence))  # type: ignore[arg-type]

    _selectors_cache[selector] = compiled  # type: ignore[assignment]
    return compiled  # type: ignore[return-value]


def _match_pseudo(node: Node, name: str, arg: str | None) -> bool:
    parent = node.parent
    siblings = [c for c in (parent.children if parent else []) if not c.is_text]

    if name == "first-child":
        return bool(siblings) and siblings[0] is node
    if name == "last-child":
        return bool(siblings) and siblings[-1] is node
    if name == "only-child":
        return len(siblings) == 1 and siblings[0] is node
    if name in ("nth-child", "nth-of-type", "nth-last-child"):
        same = siblings
        if name == "nth-of-type":
            same = [c for c in siblings if c.tag == node.tag]
        if node not in same:
            return False
        idx = same.index(node) + 1
        if name == "nth-last-child":
            idx = len(same) - idx + 1
        return _nth_matches(idx, arg or "")
    if name in ("first-of-type", "last-of-type"):
        same = [c for c in siblings if c.tag == node.tag]
        if not same:
            return False
        return same[0] is node if name == "first-of-type" else same[-1] is node
    if name == "empty":
        return not node.children or all(c.is_text and not c.data.strip() for c in node.children)
    if name == "root" or name == "document":
        return node.parent is not None and node.parent.tag == "#document"
    if name == "not":
        return not _node_matches(node, arg or "")
    if name == "is" or name == "matches":
        if arg and arg.strip().startswith("/"):  # regex form :matches(/re/i)
            return _regex_match(node, arg)
        return _node_matches(node, arg or "")
    if name == "has":
        return bool(select(node, arg or ""))
    if name == "contains":
        needle = _unquote(arg or "")
        return needle.lower() in node.text_content().lower()
    if name == "regex":
        return _regex_match(node, arg or "")
    if name in ("checkbox", "selected", "checked", "disabled", "hidden", "visible"):
        if name == "hidden":
            return node.attrs.get("type") == "hidden" or "hidden" in node.attrs
        if name in ("selected", "checked", "disabled"):
            return name in node.attrs
        return True
    return False  # unknown pseudo never matches


def _document(node: Node) -> Node:
    cur = node
    while cur.parent is not None:
        cur = cur.parent
    return cur


def _node_matches(node: Node, selector: str) -> bool:
    """True when `node` itself satisfies `selector` (whole-document evaluated)."""
    if not selector:
        return False
    try:
        groups = parse_selector(selector)
    except SelectorError:
        return False
    for _comb, sequence in groups:  # type: ignore[misc]
        if len(sequence) == 1 and sequence[0].matches(node):  # type: ignore[union-attr]
            return True
        if len(sequence) > 1 and node in _eval_sequence(_document(node), sequence):
            return True
    return False


def _regex_match(node: Node, spec: str) -> bool:
    flags = re.I
    body = spec.strip()
    if body.startswith("/"):
        end = body.rfind("/")
        if end > 0:
            fl = body[end + 1:]
            body = body[1:end]
            if "i" in fl:
                flags |= re.I
            if "s" in fl:
                flags |= re.S
    try:
        return re.search(body, node.text_content(), flags) is not None
    except re.error:
        return False


def _nth_matches(idx: int, arg: str) -> bool:
    arg = arg.strip().lower()
    if arg == "odd":
        return idx % 2 == 1
    if arg == "even":
        return idx % 2 == 0
    if "n" in arg:
        m = re.match(r"^\s*([+-]?\d*)n\s*(?:([+-])\s*(\d+))?\s*$", arg)
        if not m:
            return False
        a_raw = m.group(1)
        a = 1 if a_raw in ("", "+") else (-1 if a_raw == "-" else int(a_raw))
        b = int(m.group(3) or 0)
        if m.group(2) == "-":
            b = -b
        if a == 0:
            return idx == b
        k = (idx - b) / a
        return k >= 0 and float(k).is_integer()
    try:
        return idx == int(arg)
    except ValueError:
        return False


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    return text


def _prepare(compiled) -> tuple[str, list[_Simple]]:
    return compiled


def select(root: Node, selector: str) -> list[Node]:
    """Return every element under `root` matching `selector`, in doc order."""
    if not selector:
        return []
    groups = parse_selector(selector)
    seen: set[int] = set()
    out: list[Node] = []
    for _comb, sequence in groups:  # type: ignore[misc]
        for node in _eval_sequence(root, sequence):
            if id(node) not in seen:
                seen.add(id(node))
                out.append(node)
    out.sort(key=_document_order)
    return out


def _document_order(node: Node) -> tuple:
    chain: list[Any] = []
    cur: Node | None = node
    while cur is not None and cur.parent is not None:
        chain.append(cur.parent.children.index(cur))
        cur = cur.parent
    return tuple(reversed(chain))


def _eval_sequence(root: Node, sequence: Sequence[Any]) -> list[Node]:
    # start: all elements in the tree
    first = sequence[0]
    current = [n for n in root.iter() if not n.is_text and first.matches(n)]  # type: ignore[union-attr]
    idx = 1
    while idx < len(sequence):
        comb = sequence[idx]
        simple = sequence[idx + 1]
        idx += 2
        nxt: list[Node] = []
        if comb == " ":
            for node in current:
                for d in node.descendants():
                    if simple.matches(d):
                        nxt.append(d)  # type: ignore[union-attr]
        elif comb == ">":
            for node in current:
                for d in node.child_elements():
                    if simple.matches(d):
                        nxt.append(d)  # type: ignore[union-attr]
        elif comb == "+":
            for node in current:
                sibs = [c for c in (node.parent.children if node.parent else []) if not c.is_text]
                if node in sibs:
                    i = sibs.index(node)
                    if i + 1 < len(sibs) and simple.matches(sibs[i + 1]):
                        nxt.append(sibs[i + 1])  # type: ignore[union-attr]
        elif comb == "~":
            for node in current:
                sibs = [c for c in (node.parent.children if node.parent else []) if not c.is_text]
                if node in sibs:
                    for sib in sibs[sibs.index(node) + 1:]:
                        if simple.matches(sib):
                            nxt.append(sib)  # type: ignore[union-attr]
        current = nxt
    return current


def select_one(root: Node, selector: str) -> Node | None:
    found = select(root, selector)
    return found[0] if found else None


# --------------------------------------------------------------------------
# extraction expressions
# --------------------------------------------------------------------------

_ATTR_SUFFIX = re.compile(r"\s*@([\w\-]+)\s*$")
_MODE_SUFFIX = re.compile(r"(?:::|[ \t])(text|textcontent|html|innerhtml|outerhtml)\s*$", re.I)


_FALLBACK_SPLIT = re.compile(r"\s+\|\s+|\|\|")


def split_fallbacks(expr: str) -> list[str]:
    """'a @href | b @href' -> ['a @href', 'b @href'] (never splits `|=`)."""
    return [part.strip() for part in _FALLBACK_SPLIT.split(expr or "") if part.strip()]


def extract(node: Node | None, expr: str, default: str = "") -> str:
    """Get a single value from `node` using `expr` (see module docstring)."""
    if node is None or not expr:
        return default
    for part in split_fallbacks(expr):
        values = extract_all(node, part)
        if values:
            return values[0]
    return default


def extract_all(node: Node | None, expr: str) -> list[str]:
    if node is None or not expr:
        return []
    expr = expr.strip()
    alternatives = split_fallbacks(expr)
    if len(alternatives) > 1:
        for part in alternatives:
            values = extract_all(node, part)
            if values:
                return values
        return []

    if expr in ("::text", "text", "textContent", "self"):
        text = node.text_content()
        return [text] if text else []
    if expr in ("html", "inner_html"):
        return [node.inner_html]
    if expr == "outer_html":
        return [render(node)]
    if expr.startswith("::attr(") and expr.endswith(")"):
        v = node.get(expr[7:-1])
        return [v] if v else []

    attr: str | None = None
    m = _ATTR_SUFFIX.search(expr)
    if m:
        attr = m.group(1)
        expr = expr[: m.start()].strip()

    mode: str | None = None
    m2 = _MODE_SUFFIX.search(expr)
    if m2:
        mode = m2.group(1).lower()
        expr = expr[: m2.start()].strip()

    nodes = [node] if not expr else select(node, expr)
    out: list[str] = []
    for n in nodes:
        if mode in ("html", "innerhtml"):
            v = n.inner_html
        elif mode == "outerhtml":
            v = render(n)
        elif mode == "text" or mode == "textcontent":
            v = n.text_content()
        elif attr:
            v = n.get(attr)
        else:
            v = n.text_content() or n.get_any("content", "value", "title", "alt", "data-src", "src")
        if v:
            out.append(v.strip())
    return out


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------

def soup(source: str | bytes) -> Node:
    """bs4-flavoured alias for `parse`."""
    return parse(source)


def find_all(source: str | Node, selector: str) -> list[Node]:
    root = source if isinstance(source, Node) else parse(source)
    return select(root, selector)


def find(source: str | Node, selector: str) -> Node | None:
    return select_one(source if isinstance(source, Node) else parse(source), selector)


def table_to_dicts(table: Node) -> list[dict[str, str]]:
    """Turn an <table> into a list of row dicts keyed by <th>/<td>."""
    rows = select(table, "tr")
    if not rows:
        return []
    header: list[str] = []
    first_cells = [c for c in rows[0].child_elements() if c.tag in ("th", "td")]
    if all(c.tag == "th" for c in first_cells) and first_cells:
        header = [c.text_content() for c in first_cells]
        rows = rows[1:]
    out: list[dict[str, str]] = []
    for row in rows:
        cells = [c for c in row.child_elements() if c.tag in ("th", "td")]
        values = [c.text_content() for c in cells]
        if header:
            out.append({h: (values[i] if i < len(values) else "") for i, h in enumerate(header)})
        else:
            out.append({str(i): v for i, v in enumerate(values)})
    return out
