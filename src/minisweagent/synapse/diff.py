"""Line maps between file versions and symbol-level change classification."""

import difflib
from dataclasses import dataclass

from minisweagent.synapse.symbols import FileSymbols, Symbol

C0, C1, C1P, C2, C3, C4 = "C0", "C1", "C1+", "C2", "C3", "C4"
CLASS_LABELS = {
    C0: "formatting/comments only",
    C1: "body changed",
    C1P: "behavior changed (exit paths)",
    C2: "signature/contract changed",
    C3: "added/removed/renamed",
    C4: "environment/config changed",
}


class LineMap:
    """Maps 1-based line numbers of an old file version to the new version."""

    def __init__(self, old_lines: list[str], new_lines: list[str]):
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=len(old_lines) > 5000)
        self.ops = matcher.get_opcodes()
        self.n_old, self.n_new = len(old_lines), len(new_lines)

    def map_line(self, line: int) -> int | None:
        """New line number of an unchanged old line, None if the line was edited or deleted."""
        i = line - 1
        for tag, i1, i2, j1, _ in self.ops:
            if i1 <= i < i2:
                return j1 + (i - i1) + 1 if tag == "equal" else None
        return line + self.n_new - self.n_old

    def nearest(self, line: int) -> int:
        """Best-effort new position of an old line, also for edited lines."""
        i = line - 1
        for tag, i1, i2, j1, j2 in self.ops:
            if i1 <= i < i2:
                return j1 + min(i - i1, max(j2 - j1 - 1, 0)) + 1 if tag != "delete" else j1 + 1
        return line + self.n_new - self.n_old

    def map_range(self, lo: int, hi: int) -> tuple[int, int, bool]:
        """Returns (new_lo, new_hi, intact). Intact means every line in [lo, hi] survives unchanged and contiguous."""
        intact = True
        for tag, i1, i2, _, _ in self.ops:
            if tag == "equal":
                continue
            if i1 == i2:
                if lo - 1 < i1 < hi:
                    intact = False
            elif i1 < hi and i2 > lo - 1:
                intact = False
        return self.nearest(lo), self.nearest(hi), intact

    def changed_new_ranges(self) -> list[tuple[int, int]]:
        out = []
        for tag, _, _, j1, j2 in self.ops:
            if tag == "equal":
                continue
            out.append((j1 + 1, max(j2, j1 + 1)))
        return out

    def changed_old_ranges(self) -> list[tuple[int, int]]:
        return [(i1 + 1, max(i2, i1 + 1)) for tag, i1, i2, _, _ in self.ops if tag != "equal"]

    def first_shift(self) -> tuple[int, int] | None:
        """(old line after which numbering shifts, delta) for the first change that alters the line count."""
        for tag, i1, i2, j1, j2 in self.ops:
            if tag != "equal" and (i2 - i1) != (j2 - j1):
                return i1 + 1, (j2 - j1) - (i2 - i1)
        return None

    @property
    def unchanged(self) -> bool:
        return all(op[0] == "equal" for op in self.ops)


def map_through(maps: list[LineMap], line: int) -> int | None:
    for lm in maps:
        mapped = lm.map_line(line)
        if mapped is None:
            return None
        line = mapped
    return line


@dataclass
class SymbolChange:
    name: str
    kind: str
    cls: str
    compatible: bool = True
    old: Symbol | None = None
    new: Symbol | None = None
    renamed_from: str | None = None

    @property
    def short_name(self) -> str:
        sym = self.old or self.new
        return sym.short_name if sym else self.name

    @property
    def detail(self) -> str:
        if self.renamed_from:
            return f"renamed from {self.renamed_from}"
        if self.old is None:
            return "added"
        if self.new is None:
            return "removed"
        label = CLASS_LABELS.get(self.cls, self.cls)
        if self.cls == C2:
            label += " (backward compatible)" if self.compatible else " (breaking)"
        return label


def _params_compatible(old: tuple | None, new: tuple | None, lang: str) -> bool:
    if old == new:
        return True
    if old is None or new is None or len(new) < len(old) or tuple(new[: len(old)]) != tuple(old):
        return False
    for param in new[len(old) :]:
        if lang == "python" and ("=" in param or param.startswith("*")):
            continue
        if lang in ("typescript", "tsx", "javascript") and (
            "=" in param or param.startswith("...") or "?" in param.split(":")[0]
        ):
            continue
        return False
    return True


def classify_pair(old: Symbol, new: Symbol, lang: str, contract_classifier: bool = True) -> SymbolChange | None:
    contract_changed = (
        old.header != new.header or old.members != new.members or old.params != new.params or old.returns != new.returns
    )
    if contract_changed:
        if old.members is not None and new.members is not None:
            compatible = old.header == new.header and old.members <= new.members
        elif old.kind == "variable":
            compatible = False
        else:
            compatible = (
                old.frame == new.frame
                and old.returns == new.returns
                and _params_compatible(old.params, new.params, lang)
            )
        cls = C2
    elif old.body_hash != new.body_hash:
        cls, compatible = (C1P if old.behavior != new.behavior else C1), True
    elif old.raw_hash != new.raw_hash:
        cls, compatible = C0, True
    else:
        return None
    if not contract_classifier and cls != C0:
        cls, compatible = C2, False
    return SymbolChange(new.name, new.kind, cls, compatible, old, new)


def diff_symbols(old_fs: FileSymbols, new_fs: FileSymbols, contract_classifier: bool = True) -> list[SymbolChange]:
    old_by, new_by = old_fs.by_name(), new_fs.by_name()
    changes: list[SymbolChange] = []
    for name in new_by.keys() & old_by.keys():
        change = classify_pair(old_by[name], new_by[name], new_fs.lang, contract_classifier)
        if change is not None:
            changes.append(change)
    removed = [old_by[n] for n in old_by.keys() - new_by.keys()]
    added = [new_by[n] for n in new_by.keys() - old_by.keys()]
    for sym in list(removed):
        twin = next(
            (
                a
                for a in added
                if a.kind == sym.kind and a.body_hash and a.body_hash == sym.body_hash and a.parent == sym.parent
            ),
            None,
        )
        if twin is not None:
            removed.remove(sym)
            added.remove(twin)
            changes.append(SymbolChange(twin.name, twin.kind, C3, False, sym, twin, renamed_from=sym.name))
    changes.extend(SymbolChange(s.name, s.kind, C3, False, s, None) for s in removed)
    changes.extend(SymbolChange(s.name, s.kind, C3, True, None, s) for s in added)
    return sorted(changes, key=lambda c: (c.new or c.old).start)
