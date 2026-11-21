"""Text shown to the model. Everything here is short, stable (no timestamps) and refers to earlier outputs by
the command that produced them, which the model can see in its history."""

from minisweagent.synapse.diff import C2, LineMap, SymbolChange
from minisweagent.synapse.execparse import verdict_summary
from minisweagent.synapse.state import Evidence, Obligation


def cmd(command: str, limit: int = 70) -> str:
    text = " ".join(command.split())
    return f"`{text[: limit - 3]}...`" if len(text) > limit else f"`{text}`"


def clip(text: str, limit: int = 140) -> str:
    text = text.strip()
    return text[: limit - 3] + "..." if len(text) > limit else text


def numbered(lines: list[str], lo: int, hi: int, indent: str = "    ") -> list[str]:
    width = len(str(hi))
    return [f"{indent}{i:>{width}}  {lines[i - 1]}" for i in range(lo, min(hi, len(lines)) + 1)]


def block(lines: list[str]) -> str:
    return "<synapse>\n" + "\n".join(lines) + "\n</synapse>" if lines else ""


# --- edit echo ---


def edit_header(path: str, lm: LineMap) -> str:
    added = sum(j2 - j1 for tag, _, _, j1, j2 in lm.ops if tag != "equal")
    removed = sum(i2 - i1 for tag, i1, i2, _, _ in lm.ops if tag != "equal")
    return f"[edit] {path}: +{added} -{removed} lines"


def symbol_change(change: SymbolChange) -> list[str]:
    where = f" (now L{change.new.start}-{change.new.end})" if change.new else ""
    out = [f"  {change.name}: {change.detail}{where}"]
    if change.cls == C2 and change.old and change.new and change.old.header != change.new.header:
        out.append(f"    - {clip(change.old.header)}")
        out.append(f"    + {clip(change.new.header)}")
    elif change.cls == C2 and change.old and change.new and change.old.members != change.new.members:
        gone = sorted((change.old.members or frozenset()) - (change.new.members or frozenset()))
        new = sorted((change.new.members or frozenset()) - (change.old.members or frozenset()))
        out.extend(f"    - {clip(m)}" for m in gone[:3])
        out.extend(f"    + {clip(m)}" for m in new[:3])
    return out


def cosmetic_only(path: str) -> str:
    return f"[edit] {path}: formatting/comments only"


def slice_lines(lines: list[str], lo: int, hi: int) -> list[str]:
    return [f"  current L{lo}-{min(hi, len(lines))}:", *numbered(lines, lo, hi)]


def large_change(n_lines: int) -> str:
    return f"  ({n_lines} changed lines; not echoed - view specific ranges if needed)"


def created(path: str, n_lines: int, names: list[str]) -> str:
    defs = f"; defines {', '.join(names)}" if names else ""
    return f"[created] {path} ({n_lines} lines{defs})"


def deleted(path: str) -> str:
    return f"[deleted] {path}"


def changed_unknown(path: str) -> str:
    return f"[edit] {path} changed (previous content unknown)"


def syntax_error(path: str, lines: list[int]) -> str:
    where = ", ".join(f"L{n}" for n in lines[:3])
    return f"  WARNING: syntax error introduced in {path} near {where}"


def noop(path: str, command: str) -> str:
    return f"[no change] {cmd(command, 50)} exited 0 but {path} is unchanged (pattern or address did not match?)"


# --- sync events ---


def outdated(ev: Evidence) -> str:
    rng = f" L{ev.lo0}-{ev.hi0}" if ev.file else ""
    return f"[outdated] earlier output of {cmd(ev.command)} ({ev.file}{rng}) no longer matches the file"


def shifted(path: str, shift: tuple[int, int], views: list[Evidence]) -> str:
    after, delta = shift
    sources = ", ".join(sorted({cmd(v.command, 40) for v in views})[:2])
    return f"[shifted] line numbers in {path} after L{after} moved by {delta:+d} (earlier views: {sources})"


def search_changed(ev: Evidence) -> str:
    return f"[outdated] {cmd(ev.command)} would now return different results"


def exec_outdated(ev: Evidence, path: str) -> str:
    return f"[tests outdated] {cmd(ev.command)} ({verdict_summary(ev.verdict)}) ran before {path} changed"


def env_changed(reason: str) -> str:
    return f"[environment] {reason} changed; earlier test/run results may no longer hold"


def references(path: str, change: SymbolChange, refs: list[tuple], limit: int) -> list[str]:
    if change.renamed_from:
        what = f"{change.renamed_from} was renamed to {change.new.short_name if change.new else '?'}"
        label = "old name still referenced at"
    elif change.new is None:
        what, label = f"{change.name} was removed from {path}", "still referenced at"
    elif change.compatible:
        what, label = f"{change.name} changed its signature compatibly", "references (likely fine)"
    else:
        what, label = f"{change.name} changed its signature (breaking)", "references that may need updating"
    if not refs:
        return [f"[contract] {what}; no references found"]
    out = [f"[contract] {what}; {label}:"]
    out.extend(f"    {f}:{line}  {clip(text, 100)}" for _, f, line, text, _ in refs[:limit])
    if len(refs) > limit:
        out.append(f"    (+{len(refs) - limit} more)")
    return out


# --- governance ---


def line_guard(path: str, ref: Evidence, targets: list[int], mapped: list[int | None], lines: list[str]) -> str:
    pairs = []
    for old, new in list(zip(targets, mapped))[:6]:
        pairs.append(f"L{old} -> {'L' + str(new) if new else 'edited/removed'}")
    shown = [m for m in mapped if m]
    lo = max(1, min(shown or targets) - 2)
    hi = min(len(lines), max(shown or targets) + 2, lo + 30)
    out = [
        "[synapse] Not executed: the line numbers in this command appear to come from your earlier output of "
        f"{cmd(ref.command)}, but {path} has changed since then.",
        "Where those lines are now: " + ", ".join(pairs) + ("" if len(targets) <= 6 else ", ..."),
    ]
    if lines:
        out.append(f"Current content of {path} L{lo}-{hi}:")
        out.extend(numbered(lines, lo, hi, indent="  "))
    out.append("Re-issue the command with updated line numbers, or re-run it unchanged to execute it as is.")
    return "\n".join(out)


def delta_read_stub(path: str, lo: int, hi: int, ev: Evidence) -> str:
    return (
        f"[synapse] {path} L{lo}-{hi} is unchanged since your earlier output of {cmd(ev.command)}, "
        "which is still valid. Re-run the same command to see the full output again."
    )


def evicted(evs: list[Evidence]) -> str:
    ev = evs[0]
    if ev.state == "SUPERSEDED":
        why = "it was superseded by a newer output of the same content"
    elif ev.kind == "search":
        why = "its results have changed since"
    elif ev.file:
        why = f"{ev.file} has changed since"
    else:
        why = "it is outdated"
    return f"[synapse] Output of {cmd(ev.command)} removed from context: {why}. Re-run the command if you need it."


def gate(issues: list[str]) -> str:
    return "\n".join(
        [
            "[synapse] Submission held back once. Please check:",
            *[f"- {issue}" for issue in issues],
            "If everything is intended, submit again with the same command.",
        ]
    )


def gate_syntax(items: list[tuple[str, list[int]]]) -> str:
    return "syntax errors introduced by your edits: " + ", ".join(
        f"{p} near L{lines[0]}" for p, lines in items[:4] if lines
    )


def gate_obligations(obs: list[Obligation], total: int) -> str:
    listed = "; ".join(f"{o.file}:{o.line} uses {o.cause_name}" for o in obs[:6])
    more = f" (+{total - 6} more)" if total > 6 else ""
    return f"{total} reference(s) to symbols whose contract you changed were not updated or re-checked: {listed}{more}"


def gate_tests(step: int, suggestion: str | None) -> str:
    hint = f"; e.g. re-run {cmd(suggestion, 80)}" if suggestion else "; run the relevant tests or your reproduction"
    return f"no test or reproduction was run after your last code change (step {step}){hint}"


def gate_env() -> str:
    return "the environment/config changed after your last test run"


def footer(obs: list[Obligation], syntax: list[tuple[str, list[int]]], budget: int) -> str:
    lines = []
    for path, errs in syntax:
        lines.append(f"- syntax error still present in {path} near L{errs[0]}")
    for o in obs:
        lines.append(f"- {o.file}:{o.line} still uses {o.cause_name} ({o.cause}): {clip(o.text, 80)}")
    out, size = [], 0
    for line in lines:
        if size + len(line) > budget:
            out.append(f"- (+{len(lines) - len(out)} more)")
            break
        out.append(line)
        size += len(line)
    if not out:
        return ""
    return "<synapse_status>\nOpen items from your earlier changes:\n" + "\n".join(out) + "\n</synapse_status>"
