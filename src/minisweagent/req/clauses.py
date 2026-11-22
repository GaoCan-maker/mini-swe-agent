"""Split a task description into individual requirements ("clauses") and extract lexical anchors for each.

Anchors are the tokens most likely to appear in code or tests that implement or exercise a clause:
identifiers in backticks, quoted literals, CamelCase/snake_case names and API lines in code blocks.
Matching is approximate on purpose; it is only used to decide which clauses to show the agent again.
"""

import re
from dataclasses import dataclass, field

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
_BOLD_LABEL_RE = re.compile(r"^\*\*(.+?)\*\*")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_DQUOTE_RE = re.compile(r"\"([^\"\n]{2,60})\"")
_KEBAB_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+")
_PLACEHOLDER_RE = re.compile(r"<[^<>]*>|\{[^{}]*\}|%[-+ #0-9.]*[a-zA-Z]|\.\.\.|\"|'")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PROSE_TOKEN_RE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(\()?")
_WORD_RE = re.compile(r"[A-Za-z]{4,}")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'(*_\[])")
_ABBREVIATIONS = ["e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.", "incl.", "resp."]

_GENERIC_CODE = {
    "string", "str", "int", "bool", "boolean", "float", "double", "byte", "bytes", "rune", "uint", "int64", "int32",
    "error", "func", "fn", "struct", "type", "interface", "return", "true", "false", "nil", "null", "None", "True",
    "False", "self", "this", "const", "var", "let", "def", "class", "import", "from", "export", "any", "void",
    "number", "object", "async", "await", "new", "map", "list", "dict", "set", "tuple", "pub", "impl", "mut",
    "static", "public", "private", "protected", "undefined", "unknown", "never", "Promise", "Array", "Record",
    "Optional", "Union", "List", "Dict", "Any", "and", "the", "for", "with", "not",
}  # fmt: skip
_GENERIC_WORDS = {
    "value", "values", "data", "name", "names", "key", "keys", "items", "item", "result", "results", "errors",
    "options", "option", "config", "default", "defaults", "message", "index", "count", "size", "path", "file",
    "text", "args", "kwargs", "params", "type", "types", "field", "fields", "get", "set", "run", "add", "new",
}  # fmt: skip
_GENERIC_CAPS = {
    "IMPORTANT", "NOT", "AND", "OR", "ALL", "MUST", "SHOULD", "MAY", "API", "URL", "ID", "OK", "TODO", "NOTE", "JSON",
    "HTTP", "HTTPS", "CLI", "UI", "IO", "NO", "YES", "IF", "THE", "NEW",
}  # fmt: skip
_STOPWORDS = {
    "this", "that", "with", "from", "into", "when", "then", "than", "them", "they", "their", "there", "these", "those",
    "have", "has", "been", "being", "were", "will", "would", "should", "must", "shall", "could", "also", "only",
    "each", "every", "both", "either", "neither", "other", "same", "such", "some", "more", "most", "less", "many",
    "much", "very", "just", "even", "still", "already", "always", "never", "where", "which", "what", "while",
    "whose", "after", "before", "about", "above", "below", "under", "over", "between", "without", "within",
    "via", "using", "used", "uses", "make", "makes", "made", "does", "done", "doing", "like", "need", "needs",
    "ensure", "existing", "support", "supports", "supported", "allow", "allows", "allowed", "add", "adds",
    "added", "return", "returns", "returned", "provide", "provides", "instead", "otherwise", "example",
    "instance", "case", "cases", "value", "values", "true", "false", "none", "null", "given", "keep", "kept",
    "work", "works", "your", "you", "please", "new", "code", "change", "changes", "behavior", "behaviour",
}  # fmt: skip


@dataclass
class Anchor:
    text: str
    kind: str
    weight: float
    regex: re.Pattern = field(repr=False, compare=False, default=None)  # type: ignore[assignment]


@dataclass
class Clause:
    cid: int
    text: str
    context: str = ""
    anchors: list[Anchor] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    _keyword_res: list[re.Pattern] = field(default_factory=list, repr=False, compare=False)

    def coverage(self, text: str) -> float:
        """Weighted share of anchors (or keywords, if there are no anchors) found in `text`."""
        if not text:
            return 0.0
        if self.anchors:
            total = sum(a.weight for a in self.anchors)
            return sum(a.weight for a in self.anchors if a.regex.search(text)) / total if total else 0.0
        if not self._keyword_res:
            return 0.0
        return sum(1 for r in self._keyword_res if r.search(text)) / len(self._keyword_res)

    def accepts(self, score: float, anchor_threshold: float, keyword_threshold: float, min_keywords: int) -> bool:
        """Whether a coverage score is high enough to count as matching this clause."""
        if self.anchors:
            return score >= anchor_threshold
        found = round(score * len(self._keyword_res))
        return score >= keyword_threshold and found >= min_keywords

    def touches(self, text: str) -> bool:
        """True if any anchor occurs in `text` (used to find the files that implement a clause)."""
        return any(a.regex.search(text) for a in self.anchors)


def _weight(token: str) -> float:
    if token in _GENERIC_WORDS or token.lower() in _GENERIC_WORDS:
        return 0.3
    if len(token) >= 6 or "_" in token or re.search(r"[a-z][A-Z]|\d", token):
        return 1.0
    return 0.6


def _ident_regex(token: str) -> re.Pattern:
    forms = {token}
    if len(token) >= 6 and re.search(r"[a-z]", token) and re.search(r"[A-Z]", token):
        snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", token).lower()
        forms |= {snake, token.lower()}
    strict = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True) if "_" not in f)
    snake = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True) if "_" in f)
    parts = []
    if strict:
        parts.append(rf"(?<![A-Za-z0-9_])(?:{strict})(?![A-Za-z0-9_])")
    if snake:
        parts.append(rf"(?<![A-Za-z0-9])(?:{snake})(?![A-Za-z0-9])")
    return re.compile("|".join(parts))


def _add(anchors: dict[str, Anchor], text: str, kind: str, weight: float) -> None:
    key = f"{kind}:{text}"
    if key in anchors:
        anchors[key].weight = max(anchors[key].weight, weight)
        return
    if kind == "ident":
        regex = _ident_regex(text)
    elif kind == "kebab":
        forms = [re.escape(text), re.escape(text.replace("-", "_")), re.escape(text.replace("-", ""))]
        regex = re.compile(rf"(?<![A-Za-z0-9])(?:{'|'.join(forms)})(?![A-Za-z0-9])", re.IGNORECASE)
    else:
        regex = re.compile(re.escape(text))
    anchors[key] = Anchor(text, kind, weight, regex)


def _add_idents(anchors: dict[str, Anchor], text: str) -> None:
    for token in _IDENT_RE.findall(text):
        if len(token) >= 3 and token not in _GENERIC_CODE:
            _add(anchors, token, "ident", _weight(token))


def _is_message(span: str) -> bool:
    """Backtick span that is human-readable text (an error message, output line) rather than code."""
    if " " not in span or re.search(r"[\[\]]|\w\(| = ", span):
        return False
    return len(re.findall(r"(?<![\w.])[A-Za-z]{2,}(?![\w(])", span)) >= 3


def extract_anchors(text: str, code: bool = False) -> list[Anchor]:
    anchors: dict[str, Anchor] = {}
    plain = text.replace("**", "")
    for span in _BACKTICK_RE.findall(plain):
        stripped = span.strip()
        if stripped.startswith("-") and re.fullmatch(r"-{1,2}[\w-]+", stripped):
            _add(anchors, stripped, "literal", 1.0)
            continue
        if _KEBAB_RE.fullmatch(stripped):
            _add(anchors, stripped, "kebab", 1.0)
            continue
        if _is_message(stripped):
            for fragment in _PLACEHOLDER_RE.split(stripped):
                fragment = fragment.strip()
                if len(fragment) >= 8 and re.search(r"[A-Za-z]{3}", fragment):
                    _add(anchors, fragment, "literal", 1.0)
            continue
        _add_idents(anchors, span)
        if not _IDENT_RE.fullmatch(stripped) and 3 <= len(stripped) <= 40 and re.search(r"[A-Za-z]", stripped):
            if not re.fullmatch(r"[\w.()\[\]]+", stripped):
                _add(anchors, stripped, "literal", 0.5)
    for literal in _DQUOTE_RE.findall(plain if code else _BACKTICK_RE.sub(" ", plain)):
        if re.search(r"[A-Za-z]", literal) and len(literal.split()) <= 6:
            _add(anchors, literal, "literal", 1.0)
    if code:
        _add_idents(anchors, _DQUOTE_RE.sub(" ", plain))
    else:
        prose = _DQUOTE_RE.sub(" ", _BACKTICK_RE.sub(" ", plain))
        for match in _PROSE_TOKEN_RE.finditer(prose):
            token, call = match.group(1), match.group(2)
            parts = token.split(".")
            if len(parts) > 1 and all(len(p) > 1 for p in parts) and f"{token}." not in _ABBREVIATIONS:
                for part in parts:
                    if len(part) >= 3 and part not in _GENERIC_CODE:
                        _add(anchors, part, "ident", _weight(part))
                continue
            if token in _GENERIC_CODE or len(token) < 2:
                continue
            if call and len(token) >= 3:
                _add(anchors, token, "ident", _weight(token))
            elif re.search(r"[A-Za-z0-9]_[A-Za-z0-9]|^_[a-z]", token) or re.search(
                r"[a-z][A-Z]|^[A-Z][a-z]+[A-Z]", token
            ):
                _add(anchors, token, "ident", _weight(token))
            elif re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", token) and token not in _GENERIC_CAPS:
                _add(anchors, token, "ident", 0.5)
    return list(anchors.values())


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    prose = _DQUOTE_RE.sub(" ", _BACKTICK_RE.sub(" ", text))
    out: list[str] = []
    for word in _WORD_RE.findall(prose):
        w = word.lower()
        if w not in _STOPWORDS and w not in out:
            out.append(w)
        if len(out) >= limit:
            break
    return out


def _keyword_regex(word: str) -> re.Pattern:
    stem = word[: max(5, len(word) - 3)] if len(word) > 5 else word
    return re.compile(rf"(?<![A-Za-z]){re.escape(stem)}", re.IGNORECASE)


def _split_sentences(text: str) -> list[str]:
    spans: list[str] = []

    def protect(match: re.Match) -> str:
        spans.append(match.group(0))
        return f"\x00{len(spans) - 1}\x00"

    protected = _BACKTICK_RE.sub(protect, text)
    for abbr in _ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", "\x01"))
    out = []
    for part in _SENTENCE_SPLIT_RE.split(protected):
        part = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], part).replace("\x01", ".").strip()
        if part:
            out.append(part)
    return out


def _is_title(text: str) -> bool:
    """Short label without sentence punctuation, e.g. 'Cache visibility and reset'."""
    text = text.strip().strip("*").strip()
    return bool(text) and len(text.split()) <= 8 and "`" not in text and not re.search(r"[.:;!?,)\"']$", text)


def _join(*parts: str) -> str:
    return " > ".join(p for p in parts if p)


def _blocks(task: str) -> list[tuple[str, str, bool]]:
    """Returns (text, context, is_code) units in document order."""
    lines = [line.rstrip() for line in task.replace("\r\n", "\n").split("\n")]
    units: list[tuple[str, str, bool]] = []
    section = heading = ""
    paragraph: list[str] = []
    item: list[str] = []
    item_context = ""
    item_indent = 0
    parents: list[tuple[int, str]] = []
    in_code = False
    code_parent = ""

    def next_line(i: int) -> str:
        for line in lines[i + 1 :]:
            if line.strip():
                return line
        return ""

    def flush() -> None:
        if paragraph:
            units.append((" ".join(paragraph), heading, False))
            paragraph.clear()
        if item:
            units.append((" ".join(item), item_context, False))
            item.clear()

    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            flush()
            in_code, code_parent = not in_code, ""
            continue
        if in_code:
            if not line.strip():
                continue
            if line[:1] in (" ", "\t") and code_parent:
                units.append((line.strip(), _join(heading, code_parent), True))
            else:
                code_parent = line.strip()[:60]
                units.append((line.strip(), heading, True))
            continue
        if not line.strip():
            flush()
            continue
        if match := _HEADING_RE.match(line):
            flush()
            section = heading = match.group(1).strip()
            parents = []
            continue
        bullet = _BULLET_RE.match(line)
        text = bullet.group(2).strip() if bullet else line.strip()
        indent = len(bullet.group(1)) if bullet else len(line) - len(line.lstrip())
        if _is_title(text) and (bullet or not (paragraph or item)):
            following = _BULLET_RE.match(next_line(i))
            if following and len(following.group(1)) >= indent:
                flush()
                heading = _join(section, text.strip("*").strip())
                parents = []
                continue
        if bullet:
            flush()
            while parents and parents[-1][0] >= indent:
                parents.pop()
            item_context = _join(heading, parents[-1][1][:80] if parents else "")
            parents.append((indent, text))
            item_indent = indent
            item.append(text)
            continue
        if item and (indent > item_indent or not paragraph):
            item.append(line.strip())
            continue
        paragraph.append(line.strip())
    flush()
    return units


def extract_clauses(task: str, boilerplate: list[str] | None = None, max_clauses: int = 80) -> list[Clause]:
    task = re.sub(r"^\s*please solve this issue:\s*", "", task, flags=re.IGNORECASE)
    skip = [re.compile(p, re.IGNORECASE) for p in (boilerplate or [])]
    clauses: list[Clause] = []
    for text, context, code in _blocks(task):
        if code:
            pieces = [text]
        else:
            label = _BOLD_LABEL_RE.match(text)
            if label:
                context = f"{context} / {label.group(1)}" if context else label.group(1)
            pieces = _split_sentences(text)
        for piece in pieces:
            piece = piece.strip()
            if len(piece) < 6 or not re.search(r"[A-Za-z]", piece) or any(r.search(piece) for r in skip):
                continue
            anchors = extract_anchors(piece, code=code)
            keywords = [] if anchors else extract_keywords(piece)
            if not anchors and clauses and len(piece) < 25 and clauses[-1].context == context:
                clauses[-1].text += " " + piece
                continue
            clause = Clause(len(clauses) + 1, piece, context, anchors, keywords)
            clause._keyword_res = [_keyword_regex(w) for w in keywords]
            clauses.append(clause)
            if len(clauses) >= max_clauses:
                return clauses
    return clauses
