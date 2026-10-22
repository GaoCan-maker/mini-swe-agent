"""Symbol extraction with tree-sitter."""

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import cache
from typing import Any

FUNCTION_KINDS = {"function", "method"}
CONTAINER_KINDS = {"class", "impl", "trait", "struct", "interface", "enum", "mod"}
_COMMENT_TYPES = {"comment", "line_comment", "block_comment"}
_IGNORED_CALLS = {
    "python": {"len", "str", "int", "float", "bool", "list", "dict", "set", "tuple", "isinstance", "print", "super",
               "getattr", "setattr", "hasattr", "range", "enumerate", "zip", "sorted", "append", "format", "join"},
    "go": {"Errorf", "Sprintf", "New", "append", "len", "make", "Error", "Wrap", "Wrapf", "Printf", "Println"},
    "rust": {"clone", "unwrap", "to_string", "into", "expect", "Ok", "Err", "Some", "iter", "collect", "new"},
}  # fmt: skip
_IGNORED_CALLS["typescript"] = _IGNORED_CALLS["tsx"] = _IGNORED_CALLS["javascript"] = {
    "log", "error", "warn", "stringify", "parse", "push", "map", "filter", "forEach", "then", "toString",
}  # fmt: skip
_IGNORED_RECEIVERS = {"logger", "logging", "log", "console", "slog", "fmt"}


@dataclass(eq=False)
class Symbol:
    name: str
    kind: str
    start: int
    doc_start: int
    sig_end: int
    end: int
    parent: "Symbol | None" = None
    children: list["Symbol"] = field(default_factory=list)
    has_error: bool = False
    doc_line: int | None = None
    node: Any = field(default=None, repr=False)

    @property
    def is_leaf(self) -> bool:
        return not self.children and self.kind not in CONTAINER_KINDS

    @property
    def short_name(self) -> str:
        return re.split(r"[.:]+", self.name)[-1]


@dataclass
class ParsedFile:
    sha1: str
    lang: str
    lines: list[str]
    symbols: list[Symbol]
    top: list[Symbol]
    innermost: list[Symbol | None]
    comment_only: set[int]
    skip_reason: str = ""

    def leaf_at(self, line: int) -> Symbol | None:
        s = self.innermost[line] if 0 < line < len(self.innermost) else None
        return s if s is not None and s.is_leaf else None

    def text(self, a: int, b: int) -> str:
        return "\n".join(self.lines[a - 1 : b])


@cache
def get_parser(lang: str):
    from tree_sitter_language_pack import get_parser as _get_parser

    return _get_parser(lang)


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _end_row(node) -> int:
    row = node.end_point[0]
    if node.end_point[1] == 0 and row > node.start_point[0]:
        row -= 1
    return row + 1


class _Builder:
    def __init__(self, src: bytes, lang: str):
        self.src, self.lang, self.symbols = src, lang, []

    def name(self, node, fieldname: str = "name") -> str:
        child = node.child_by_field_name(fieldname)
        return _text(child, self.src) if child is not None else "?"

    def add(self, outer, target, kind: str, name: str, parent: Symbol | None, body=None) -> Symbol:
        body = body if body is not None else target.child_by_field_name("body")
        start, end = outer.start_point[0] + 1, _end_row(outer)
        if body is None:
            sig_end = end
        elif self.lang == "python":
            prev = body.prev_sibling
            sig_end = prev.end_point[0] + 1 if prev is not None else body.start_point[0]
        else:
            sig_end = body.start_point[0] + 1
        sym = Symbol(name, kind, start, start, min(max(sig_end, start), end), end, parent, has_error=outer.has_error)
        sym.node = body
        if self.lang == "python" and body is not None and body.named_children:
            first = body.named_children[0]
            if first.type == "expression_statement" and first.named_children:
                first = first.named_children[0]
            if first.type == "string":
                sym.doc_line = first.start_point[0] + 1
        if parent is not None:
            parent.children.append(sym)
        self.symbols.append(sym)
        return sym

    def walk_python(self, node, parent: Symbol | None = None):
        for ch in node.named_children:
            target = ch.child_by_field_name("definition") if ch.type == "decorated_definition" else ch
            if target is None:
                continue
            name = self.name(target)
            if parent is not None:
                name = f"{parent.name}.{name}"
            if target.type == "class_definition":
                sym = self.add(ch, target, "class", name, parent)
                if (body := target.child_by_field_name("body")) is not None:
                    self.walk_python(body, sym)
            elif target.type == "function_definition":
                self.add(ch, target, "method" if parent else "function", name, parent)

    def walk_go(self, node):
        for ch in node.named_children:
            if ch.type == "function_declaration":
                self.add(ch, ch, "function", self.name(ch), None)
            elif ch.type == "method_declaration":
                recv = ch.child_by_field_name("receiver")
                decl = (
                    next((c for c in recv.named_children if c.type == "parameter_declaration"), None) if recv else None
                )
                rtype = _text(decl.child_by_field_name("type"), self.src) if decl is not None else "?"
                name = f"({rtype}).{self.name(ch)}" if rtype.startswith("*") else f"{rtype}.{self.name(ch)}"
                self.add(ch, ch, "method", name, None)
            elif ch.type == "type_declaration":
                specs = [c for c in ch.named_children if c.type in ("type_spec", "type_alias")]
                for spec in specs:
                    tnode = spec.child_by_field_name("type")
                    kind = {"struct_type": "struct", "interface_type": "interface"}.get(
                        getattr(tnode, "type", ""), "type"
                    )
                    body = (
                        next((c for c in tnode.children if c.type in ("field_declaration_list", "{")), None)
                        if tnode
                        else None
                    )
                    if kind == "interface" and tnode is not None:
                        body = tnode
                    self.add(
                        ch if len(specs) == 1 else spec,
                        spec,
                        kind,
                        self.name(spec),
                        None,
                        body=body if kind != "type" else None,
                    )
            elif ch.type in ("const_declaration", "var_declaration") and _end_row(ch) - ch.start_point[0] >= 10:
                self.add(ch, ch, "block", ch.type.split("_")[0], None, body=None)

    def walk_ts(self, node, parent: Symbol | None = None):
        for ch in node.named_children:
            target = ch
            if ch.type == "export_statement":
                target = ch.child_by_field_name("declaration")
                if target is None:
                    target = next((c for c in ch.named_children if c.type.endswith("declaration")), None)
                if target is None:
                    continue
            t = target.type
            if t in ("function_declaration", "generator_function_declaration", "function_signature"):
                self.add(ch, target, "function", self.name(target), parent)
            elif t in ("class_declaration", "abstract_class_declaration"):
                sym = self.add(ch, target, "class", self.name(target), parent)
                if (body := target.child_by_field_name("body")) is not None:
                    self.walk_ts_class(body, sym)
            elif t in ("interface_declaration", "enum_declaration"):
                self.add(ch, target, t.split("_")[0], self.name(target), parent)
            elif t == "type_alias_declaration":
                self.add(ch, target, "type", self.name(target), parent, body=None)
            elif t in ("lexical_declaration", "variable_declaration"):
                decls = [c for c in target.named_children if c.type == "variable_declarator"]
                value = decls[0].child_by_field_name("value") if len(decls) == 1 else None
                if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                    body = value.child_by_field_name("body")
                    body = body if body is not None and body.type == "statement_block" else None
                    self.add(ch, target, "function", self.name(decls[0]), parent, body=body)

    def walk_ts_class(self, body, parent: Symbol):
        for ch in body.named_children:
            if ch.type in ("method_definition", "abstract_method_signature", "method_signature"):
                self.add(ch, ch, "method", f"{parent.name}.{self.name(ch)}", parent)
            elif ch.type in ("public_field_definition", "field_definition"):
                value = ch.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression"):
                    fn_body = value.child_by_field_name("body")
                    fn_body = fn_body if fn_body is not None and fn_body.type == "statement_block" else None
                    name = self.name(ch) if ch.child_by_field_name("name") is not None else self.name(ch, "property")
                    self.add(ch, ch, "method", f"{parent.name}.{name}", parent, body=fn_body)

    def walk_rust(self, node, parent: Symbol | None = None, prefix: str = ""):
        for ch in node.named_children:
            t = ch.type
            if t in ("function_item", "function_signature_item"):
                name = self.name(ch)
                self.add(ch, ch, "method" if prefix else "function", f"{prefix}::{name}" if prefix else name, parent)
            elif t == "impl_item":
                rtype = self.name(ch, "type")
                trait = ch.child_by_field_name("trait")
                name = f"impl {_text(trait, self.src)} for {rtype}" if trait is not None else f"impl {rtype}"
                sym = self.add(ch, ch, "impl", name, parent)
                if (body := ch.child_by_field_name("body")) is not None:
                    self.walk_rust(body, sym, rtype.split("<")[0])
            elif t == "trait_item":
                sym = self.add(ch, ch, "trait", self.name(ch), parent)
                if (body := ch.child_by_field_name("body")) is not None:
                    self.walk_rust(body, sym, self.name(ch))
            elif t in ("struct_item", "enum_item", "union_item"):
                self.add(ch, ch, {"struct_item": "struct", "enum_item": "enum"}.get(t, "struct"), self.name(ch), parent)
            elif t == "mod_item" and (body := ch.child_by_field_name("body")) is not None:
                sym = self.add(ch, ch, "mod", f"mod {self.name(ch)}", parent)
                self.walk_rust(body, sym)
            elif t == "type_item":
                self.add(ch, ch, "type", self.name(ch), parent, body=None)


def _comment_lines(root, lines: list[str]) -> tuple[set[int], set[int]]:
    """Lines fully covered by comments, and lines holding rust attributes."""
    comments, attrs, stack = set(), set(), [root]
    while stack:
        node = stack.pop()
        if node.type in _COMMENT_TYPES:
            (sr, sc), (er, ec) = node.start_point, node.end_point
            for row in range(sr, er + 1):
                line = lines[row] if row < len(lines) else ""
                if (row > sr or not line[:sc].strip()) and (row < er or not line[ec:].strip()):
                    comments.add(row + 1)
        elif node.type == "attribute_item":
            attrs.update(range(node.start_point[0] + 1, _end_row(node) + 1))
        else:
            stack.extend(node.children)
    return comments, attrs


def parse_file(content: bytes, lang: str) -> ParsedFile:
    text = content.decode("utf-8", "replace")
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    sha = hashlib.sha1(content).hexdigest()
    pf = ParsedFile(sha, lang, lines, [], [], [None] * (len(lines) + 2), set())
    if len(lines) > 20000:
        pf.skip_reason = "too_many_lines"
    elif any(len(line) > 2000 for line in lines):
        pf.skip_reason = "long_line"
    if pf.skip_reason:
        return pf
    root = get_parser(lang).parse(content).root_node
    error_rows = sum(_end_row(c) - c.start_point[0] for c in root.children if c.type == "ERROR")
    if lines and error_rows > 0.3 * len(lines):
        pf.skip_reason = "parse_errors"
        return pf
    builder = _Builder(content, lang)
    {"python": builder.walk_python, "go": builder.walk_go, "rust": builder.walk_rust}.get(lang, builder.walk_ts)(root)
    pf.comment_only, attrs = _comment_lines(root, lines)
    for sym in builder.symbols:
        while sym.doc_start > 1 and (sym.doc_start - 1 in pf.comment_only or sym.doc_start - 1 in attrs):
            sym.doc_start -= 1
    pf.symbols = sorted(builder.symbols, key=lambda s: (s.start, -s.end))
    pf.top = [s for s in pf.symbols if s.parent is None]
    for sym in pf.symbols:
        for i in range(sym.start, min(sym.end, len(lines)) + 1):
            pf.innermost[i] = sym
    return pf


def calls_of(sym: Symbol, pf: ParsedFile, limit: int) -> list[str]:
    """Names of functions called in the body of sym, most informative first."""
    if sym.node is None:
        return []
    call_type = "call" if pf.lang == "python" else "call_expression"
    ignored, names, stack = _IGNORED_CALLS.get(pf.lang, set()), [], [sym.node]
    while stack and len(names) < limit:
        node = stack.pop(0)
        if node.type == call_type and (fn := node.child_by_field_name("function")) is not None:
            raw = re.sub(r"<.*?>|::<.*?>", "", fn.text.decode("utf-8", "replace"))
            parts = [p for p in re.split(r"\.|::", raw) if p]
            name = ".".join(parts[-2:])
            if parts and parts[-1] not in ignored and parts[0] not in _IGNORED_RECEIVERS and name not in names:
                if re.fullmatch(r"[\w.]+", name):
                    names.append(name)
        stack.extend(node.children)
    out, total = [], 0
    for name in names:
        if total + len(name) > 100:
            break
        out.append(name)
        total += len(name) + 2
    return out


class ParseCache:
    def __init__(self, max_entries: int = 256):
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], ParsedFile] = OrderedDict()

    def get(self, content: bytes, lang: str) -> ParsedFile:
        key = (lang, hashlib.sha1(content).hexdigest())
        if key in self._entries:
            self._entries.move_to_end(key)
            return self._entries[key]
        pf = self._entries[key] = parse_file(content, lang)
        if len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return pf
