"""Parsing and validation of the model-written <checkpoint> and <delta> blocks."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from minisweagent.agents.pkc.commands import PATH_RE
from minisweagent.agents.pkc.symbols import Symbol, find

FIELDS = {
    "checkpoint": ["TARGETS", "EXEMPLARS", "RULED_OUT", "UNVERIFIED", "DONE_WHEN"],
    "delta": ["DONE", "CONFIRMED", "REJECTED", "TODO", "KEEP", "UNVERIFIED"],
}
TRUNCATE_ORDER = ["UNVERIFIED", "EXEMPLARS", "DONE", "TODO", "TARGETS", "CONFIRMED", "REJECTED", "RULED_OUT", "KEEP"]
LINE_NO = re.compile(
    r"(?<=[\w)]):\d+(?:-\d+)?\b|\bL\d+(?:\s*[-–]\s*L?\d+)?\b|\blines?\s+\d+(?:\s*[-–]\s*\d+)?\b|\b\d{2,}\s*[-–]\s*\d{2,}\b",
    re.I,
)
CODE = re.compile(r"```|[{}();=<>\[\]]{3,}")
ID_RE = re.compile(r"(?<![\w#])#?([oe]\d+(?:\.\d+)?)\b")
SYM_RE = re.compile(r"([\w./-]+\.[A-Za-z]\w*)::([A-Za-z_][\w.]*)")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
STOP = {"this", "that", "with", "from", "only", "when", "then", "they", "have", "does", "into", "used", "uses", "file"}


@dataclass
class Item:
    field: str
    text: str
    refs: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    symbols: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class NoteContext:
    exists: Callable[[str], bool]
    symbols: Callable[[str], list[Symbol]]
    record: Callable[[str], object]
    latest_with: Callable[[str], object]
    resolve: Callable[[str], str]


def parse_block(text: str, tag: str) -> str | None:
    blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", text or "", re.S)
    return blocks[-1] if blocks else None


def parse_items(block: str, fields: list[str]) -> list[tuple[str, str]]:
    head = re.compile(rf"^({'|'.join(fields)})\s*:\s*(.*)$")
    out, current = [], None
    for raw in block.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line:
            continue
        if m := head.match(line):
            current, line = m.group(1), m.group(2).strip()
            if not line or line.lower() in ("none", "n/a", "-"):
                continue
        if current:
            out.append((current, line))
    return out


def validate(kind: str, block: str, ctx: NoteContext, max_chars: int) -> tuple[list[Item], list[str]]:
    """Returns the accepted items and human-readable problems for the T4 feedback."""
    items, problems = [], []
    for name, text in parse_items(block, FIELDS[kind]):
        text = re.sub(r"\s{2,}", " ", LINE_NO.sub("", text)).strip(" ,;")[:160]
        if CODE.search(text):
            problems.append(f'"{name} {text[:60]}" (contains code)')
            continue
        item, problem = _check(Item(name, text), ctx)
        if problem:
            problems.append(f'"{name} {text[:60]}" ({problem})')
        if item is not None:
            items.append(item)
    while sum(len(i.text) + len(i.field) + 2 for i in items) > max_chars:
        victim = next(f for f in TRUNCATE_ORDER if any(i.field == f for i in items))
        items.remove(next(i for i in reversed(items) if i.field == victim))
    return items, problems


def _check(item: Item, ctx: NoteContext) -> tuple[Item | None, str]:
    if item.field in ("DONE_WHEN", "TODO"):
        return item, ""
    ids = [m for m in ID_RE.findall(item.text) if ctx.record(m) is not None]
    bad_ids = [m for m in ID_RE.findall(item.text) if m not in ids]
    if item.field == "KEEP":
        item.refs = [i for i in ids if i.startswith("o")][:5]
        return (item, "") if item.refs else (None, "no valid output id")
    for path, name in SYM_RE.findall(item.text):
        path = ctx.resolve(path)
        if not ctx.exists(path):
            if item.field == "TARGETS":
                continue
            return None, f"no such file {path}"
        if find(ctx.symbols(path), name) is None:
            if item.field in ("EXEMPLARS", "DONE"):
                return None, f"no such symbol {path}::{name}"
        else:
            item.symbols.append((path, name))
        item.paths.append(path)
    for path in PATH_RE.findall(SYM_RE.sub("", item.text)):
        if ctx.exists(p := ctx.resolve(path)):
            item.paths.append(p)
    item.paths = list(dict.fromkeys(item.paths))
    if item.field in ("TARGETS", "EXEMPLARS"):
        return item, "" if item.paths else "kept as text only: no path::symbol"
    mentioned = set(item.paths) | {n for _, n in item.symbols}
    mentioned |= {w for w in WORD.findall(ID_RE.sub("", item.text)) if w.lower() not in STOP}
    for ref in ids:
        rec = ctx.record(ref)
        if ref.startswith("e") or not mentioned or any(m in rec.raw or m in rec.cmd for m in mentioned):
            item.refs.append(ref)
        else:
            bad_ids.append(ref)
    if not item.refs:
        item.refs = [r.id for p in item.paths if (r := ctx.latest_with(p)) is not None][:3]
    bad = f"citation {', '.join(bad_ids)} does not match" if bad_ids else ""
    if not item.refs and not item.paths:
        return item, f"kept as text only: {bad or 'no evidence'}"
    return item, f"{bad}, ignored" if bad else ""
