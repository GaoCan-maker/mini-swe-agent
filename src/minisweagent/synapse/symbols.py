"""Symbol extraction with contract / body / behavior fingerprints (tree-sitter).

A symbol's *contract* is what its users depend on: the header (name, parameters, return type, decorators,
visibility) and, for containers, the set of non-method members (fields, interface methods, enum variants).
The *body* fingerprint ignores comments, docstrings and whitespace. The *behavior* fingerprint is the set of
exit kinds in the body (raise/throw/panic/error returns/yield), used to detect behavioral contract changes.
"""

import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from functools import cache

LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".go": "go", ".ts": "typescript", ".mts": "typescript",
    ".cts": "typescript", ".tsx": "tsx", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".rs": "rust",
}  # fmt: skip
CONTAINER_KINDS = {"class", "struct", "interface", "enum", "trait", "impl"}
_COMMENT_TYPES = {"comment", "line_comment", "block_comment"}
_IMPORT_TYPES = {
    "import_statement", "import_from_statement", "future_import_statement", "import_declaration",
    "use_declaration", "extern_crate_declaration",
}  # fmt: skip
_BEHAVIOR_RE = re.compile(
    r"\b(raise\s+[\w.]+|raise\b|throw\s+(?:new\s+)?[\w.]+|panic!?|unreachable!|unimplemented!|todo!|Err\s*\(|"
    r"errors\.New|fmt\.Errorf|yield|return\s+(?:None|nil|null|undefined)\b|assert\b)"
)
MAX_LINES = 20000


def language_of(path: str) -> str | None:
    return LANG_BY_EXT.get(posixpath.splitext(path)[1].lower())


@cache
def get_parser(lang: str):
    try:
        from tree_sitter_language_pack import get_parser as _get_parser

        return _get_parser(lang)
    except Exception:
        return None


def tree_sitter_available() -> bool:
    return get_parser("python") is not None


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _end_row(node) -> int:
    row = node.end_point[0]
    if node.end_point[1] == 0 and row > node.start_point[0]:
        row -= 1
    return row + 1


@dataclass(eq=False)
class Symbol:
    name: str
    kind: str
    start: int
    end: int
    header: str
    frame: str
    """Header with the parameter list blanked out (used to judge compatibility of parameter changes)."""
    body_hash: str
    raw_hash: str
    behavior: tuple = ()
    params: tuple | None = None
    returns: str | None = None
    members: frozenset | None = None
    parent: str | None = None
    children: list[str] = field(default_factory=list)

    @property
    def short_name(self) -> str:
        return re.split(r"[.:]+", self.name)[-1]

    @property
    def is_container(self) -> bool:
        return self.kind in CONTAINER_KINDS


@dataclass
class FileSymbols:
    lang: str
    lines: list[str]
    symbols: list[Symbol] = field(default_factory=list)
    error_lines: list[int] = field(default_factory=list)
    imports: str = ""
    ok: bool = True

    def by_name(self) -> dict[str, Symbol]:
        out: dict[str, Symbol] = {}
        for sym in self.symbols:
            name, i = sym.name, 2
            while name in out:
                name, i = f"{sym.name}#{i}", i + 1
            out[name] = sym
        return out

    def innermost(self, line: int) -> Symbol | None:
        best = None
        for sym in self.symbols:
            if sym.start <= line <= sym.end and (best is None or sym.end - sym.start < best.end - best.start):
                best = sym
        return best

    def overlapping(self, lo: int, hi: int) -> list[Symbol]:
        return [s for s in self.symbols if s.start <= hi and s.end >= lo]


class _Builder:
    def __init__(self, src: bytes, lang: str):
        self.src, self.lang = src, lang
        self.symbols: list[Symbol] = []

    def text(self, node) -> str:
        return self.src[node.start_byte : node.end_byte].decode("utf-8", "replace") if node is not None else ""

    def field_text(self, node, name: str) -> str:
        child = node.child_by_field_name(name) if node is not None else None
        return self.text(child) if child is not None else "?"

    def clean(self, node, start_byte: int, end_byte: int, drop=None) -> str:
        """Text of [start_byte, end_byte) with comments (and the `drop` node) removed, whitespace-normalized."""
        cuts = []
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur.end_byte <= start_byte or cur.start_byte >= end_byte:
                continue
            if cur.type in _COMMENT_TYPES or (drop is not None and cur.id == drop.id):
                cuts.append((max(cur.start_byte, start_byte), min(cur.end_byte, end_byte)))
                continue
            stack.extend(cur.children)
        out, pos = [], start_byte
        for a, b in sorted(cuts):
            if a > pos:
                out.append(self.src[pos:a])
            pos = max(pos, b)
        out.append(self.src[pos:end_byte])
        return _norm(b"".join(out).decode("utf-8", "replace"))

    def docstring(self, body):
        if self.lang != "python" or body is None or not body.named_children:
            return None
        first = body.named_children[0]
        if first.type == "expression_statement" and first.named_children and first.named_children[0].type == "string":
            return first
        return None

    def add(
        self,
        outer,
        kind: str,
        name: str,
        *,
        body=None,
        params=None,
        returns=None,
        members: frozenset | None = None,
        parent: Symbol | None = None,
        header_override: str | None = None,
    ) -> Symbol:
        header_end = body.start_byte if body is not None else outer.end_byte
        header = header_override if header_override is not None else self.clean(outer, outer.start_byte, header_end)
        frame = header
        if params is not None:
            ptext = self.clean(params, params.start_byte, params.end_byte)
            if ptext and ptext in header:
                frame = header.replace(ptext, "()", 1)
        body_text = self.clean(body, body.start_byte, body.end_byte, drop=self.docstring(body)) if body else ""
        param_list = None
        if params is not None:
            param_list = tuple(
                _norm(self.text(c)) for c in params.named_children if c.type not in _COMMENT_TYPES and self.text(c)
            )
        sym = Symbol(
            name=name,
            kind=kind,
            start=outer.start_point[0] + 1,
            end=_end_row(outer),
            header=header,
            frame=frame,
            body_hash=_sha(body_text) if body is not None else "",
            raw_hash=_sha(self.text(outer)),
            behavior=tuple(sorted({_norm(m.group(1)) for m in _BEHAVIOR_RE.finditer(body_text)})),
            params=param_list,
            returns=_norm(self.text(returns)) if returns is not None else None,
            members=members,
            parent=parent.name if parent else None,
        )
        if parent is not None:
            parent.children.append(sym.name)
        self.symbols.append(sym)
        return sym

    def members_of(self, body, exclude_types: set[str]) -> frozenset:
        if body is None:
            return frozenset()
        doc = self.docstring(body)
        out = set()
        for child in body.named_children:
            if child.type in _COMMENT_TYPES or child.type in exclude_types or (doc is not None and child.id == doc.id):
                continue
            if child.type == "decorated_definition":
                continue
            text = self.clean(child, child.start_byte, child.end_byte)
            if text:
                out.add(text)
        return frozenset(out)

    # --- python ---
    def walk_python(self, node, parent: Symbol | None = None) -> None:
        for ch in node.named_children:
            target = ch.child_by_field_name("definition") if ch.type == "decorated_definition" else ch
            if target is None:
                continue
            if target.type == "function_definition":
                name = self.field_text(target, "name")
                self.add(
                    ch,
                    "method" if parent else "function",
                    f"{parent.name}.{name}" if parent else name,
                    body=target.child_by_field_name("body"),
                    params=target.child_by_field_name("parameters"),
                    returns=target.child_by_field_name("return_type"),
                    parent=parent,
                )
            elif target.type == "class_definition":
                name = self.field_text(target, "name")
                body = target.child_by_field_name("body")
                members = self.members_of(body, {"function_definition", "class_definition"})
                sym = self.add(
                    ch, "class", f"{parent.name}.{name}" if parent else name, body=body, members=members, parent=parent
                )
                sym.body_hash = ""
                if body is not None:
                    self.walk_python(body, sym)
            elif parent is None and ch.type == "expression_statement" and ch.named_children:
                assign = ch.named_children[0]
                if assign.type != "assignment":
                    continue
                left, right = assign.child_by_field_name("left"), assign.child_by_field_name("right")
                if left is None or left.type != "identifier":
                    continue
                self.add(ch, "variable", self.text(left), body=right)

    # --- go ---
    def walk_go(self, node) -> None:
        for ch in node.named_children:
            if ch.type == "function_declaration":
                self.add(
                    ch,
                    "function",
                    self.field_text(ch, "name"),
                    body=ch.child_by_field_name("body"),
                    params=ch.child_by_field_name("parameters"),
                    returns=ch.child_by_field_name("result"),
                )
            elif ch.type == "method_declaration":
                recv = ch.child_by_field_name("receiver")
                decl = (
                    next((c for c in recv.named_children if c.type == "parameter_declaration"), None) if recv else None
                )
                rtype = self.field_text(decl, "type").lstrip("*") if decl is not None else "?"
                rtype = re.sub(r"\[.*", "", rtype)
                self.add(
                    ch,
                    "method",
                    f"{rtype}.{self.field_text(ch, 'name')}",
                    body=ch.child_by_field_name("body"),
                    params=ch.child_by_field_name("parameters"),
                    returns=ch.child_by_field_name("result"),
                )
            elif ch.type == "type_declaration":
                for spec in [c for c in ch.named_children if c.type in ("type_spec", "type_alias")]:
                    tnode = spec.child_by_field_name("type")
                    ttype = getattr(tnode, "type", "")
                    name = self.field_text(spec, "name")
                    if ttype in ("struct_type", "interface_type"):
                        kind = "struct" if ttype == "struct_type" else "interface"
                        body = next((c for c in tnode.named_children if c.type == "field_declaration_list"), tnode)
                        members = self.members_of(body, set())
                        self.add(
                            spec, kind, name, body=body, members=members, header_override=_norm(f"type {name} {kind}")
                        )
                        self.symbols[-1].body_hash = ""
                    else:
                        self.add(spec, "type", name)
            elif ch.type in ("const_declaration", "var_declaration"):
                for spec in [c for c in ch.named_children if c.type in ("const_spec", "var_spec")]:
                    value = spec.child_by_field_name("value")
                    for ident in [c for c in spec.children if c.type == "identifier"]:
                        self.add(spec, "variable", self.text(ident), body=value)

    # --- typescript / javascript ---
    def walk_ts(self, node) -> None:
        for ch in node.named_children:
            target = ch
            if ch.type == "export_statement":
                target = ch.child_by_field_name("declaration")
                if target is None:
                    target = next((c for c in ch.named_children if c.type.endswith("declaration")), None)
                if target is None:
                    continue
            self._ts_decl(ch, target)

    def _ts_function(self, outer, name: str, fn, kind: str, parent: Symbol | None = None) -> None:
        params = fn.child_by_field_name("parameters") or fn.child_by_field_name("parameter")
        self.add(
            outer,
            kind,
            name,
            body=fn.child_by_field_name("body"),
            params=params,
            returns=fn.child_by_field_name("return_type"),
            parent=parent,
        )

    def _ts_decl(self, outer, target) -> None:
        t = target.type
        if t in ("function_declaration", "generator_function_declaration", "function_signature"):
            self._ts_function(outer, self.field_text(target, "name"), target, "function")
        elif t in ("class_declaration", "abstract_class_declaration", "class"):
            name = self.field_text(target, "name")
            body = target.child_by_field_name("body")
            method_types = {"method_definition", "method_signature", "abstract_method_signature"}
            members = frozenset(
                m
                for m in self.members_of(body, method_types)
                if not re.search(r"=\s*(async\s+)?(\([^)]*\)|\w+)\s*=>", m)
            )
            sym = self.add(outer, "class", name, body=body, members=members)
            sym.body_hash = ""
            for member in body.named_children if body is not None else []:
                if member.type in method_types:
                    self._ts_function(member, f"{name}.{self.field_text(member, 'name')}", member, "method", sym)
                elif member.type in ("public_field_definition", "field_definition"):
                    value = member.child_by_field_name("value")
                    if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                        fname = self.field_text(member, "name")
                        if fname == "?":
                            fname = self.field_text(member, "property")
                        self._ts_function(member, f"{name}.{fname}", value, "method", sym)
        elif t in ("interface_declaration", "enum_declaration"):
            body = target.child_by_field_name("body")
            sym = self.add(outer, t.split("_")[0], self.field_text(target, "name"), body=body,
                           members=self.members_of(body, set()))  # fmt: skip
            sym.body_hash = ""
        elif t == "type_alias_declaration":
            self.add(outer, "type", self.field_text(target, "name"))
        elif t in ("lexical_declaration", "variable_declaration"):
            for decl in [c for c in target.named_children if c.type == "variable_declarator"]:
                name = self.field_text(decl, "name")
                value = decl.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                    self._ts_function(outer, name, value, "function")
                else:
                    self.add(outer, "variable", name, body=value)

    # --- rust ---
    def walk_rust(self, node, parent: Symbol | None = None, prefix: str = "") -> None:
        for ch in node.named_children:
            t = ch.type
            if t in ("function_item", "function_signature_item"):
                name = self.field_text(ch, "name")
                self.add(
                    ch,
                    "method" if prefix else "function",
                    f"{prefix}::{name}" if prefix else name,
                    body=ch.child_by_field_name("body"),
                    params=ch.child_by_field_name("parameters"),
                    returns=ch.child_by_field_name("return_type"),
                    parent=parent,
                )
            elif t == "impl_item":
                rtype = self.field_text(ch, "type")
                trait = ch.child_by_field_name("trait")
                name = f"impl {self.text(trait)} for {rtype}" if trait is not None else f"impl {rtype}"
                body = ch.child_by_field_name("body")
                sym = self.add(ch, "impl", name, body=body, members=frozenset(), parent=parent)
                sym.body_hash = ""
                if body is not None:
                    self.walk_rust(body, sym, re.sub(r"<.*", "", rtype))
            elif t == "trait_item":
                name = self.field_text(ch, "name")
                body = ch.child_by_field_name("body")
                sigs = frozenset(
                    self.clean(c, c.start_byte, (c.child_by_field_name("body") or c).start_byte)
                    if c.type == "function_item"
                    else self.clean(c, c.start_byte, c.end_byte)
                    for c in (body.named_children if body is not None else [])
                    if c.type not in _COMMENT_TYPES
                )
                sym = self.add(ch, "trait", name, body=body, members=sigs, parent=parent)
                sym.body_hash = ""
                if body is not None:
                    self.walk_rust(body, sym, name)
            elif t in ("struct_item", "enum_item", "union_item"):
                body = ch.child_by_field_name("body")
                kind = "enum" if t == "enum_item" else "struct"
                sym = self.add(ch, kind, self.field_text(ch, "name"), body=body, members=self.members_of(body, set()),
                               parent=parent)  # fmt: skip
                sym.body_hash = ""
            elif t == "type_item":
                self.add(ch, "type", self.field_text(ch, "name"), parent=parent)
            elif t in ("const_item", "static_item"):
                self.add(
                    ch, "variable", self.field_text(ch, "name"), body=ch.child_by_field_name("value"), parent=parent
                )
            elif t == "mod_item" and (body := ch.child_by_field_name("body")) is not None:
                self.walk_rust(body, parent, self.field_text(ch, "name"))


def _error_lines(root, limit: int = 20) -> list[int]:
    rows, stack = set(), [root]
    while stack and len(rows) < limit:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            rows.add(node.start_point[0] + 1)
            continue
        stack.extend(c for c in node.children if c.has_error or c.is_missing)
    return sorted(rows)


def parse_symbols(content: str, lang: str) -> FileSymbols:
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    fs = FileSymbols(lang=lang, lines=lines)
    parser = get_parser(lang)
    if parser is None or len(lines) > MAX_LINES:
        fs.ok = False
        return fs
    src = content.encode("utf-8", "replace")
    try:
        root = parser.parse(src).root_node
    except Exception:
        fs.ok = False
        return fs
    fs.error_lines = _error_lines(root) if root.has_error else []
    error_rows = sum(_end_row(c) - c.start_point[0] for c in root.children if c.type == "ERROR")
    if lines and error_rows > 0.3 * len(lines):
        fs.ok = False
        return fs
    builder = _Builder(src, lang)
    walker = {"python": builder.walk_python, "go": builder.walk_go, "rust": builder.walk_rust}.get(
        lang, builder.walk_ts
    )
    walker(root)
    fs.symbols = sorted(builder.symbols, key=lambda s: (s.start, -s.end))
    fs.imports = _norm(" ".join(builder.text(c) for c in root.named_children if c.type in _IMPORT_TYPES))
    return fs
