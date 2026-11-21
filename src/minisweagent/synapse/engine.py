"""SYNAPSE engine: tracks what the agent has seen, what actually changed in the repository, and what a change
still requires, and turns that into short notes, guards and context edits."""

import difflib
import hashlib
import logging
import posixpath
import re
import shlex
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from minisweagent.synapse import render
from minisweagent.synapse.bash import CommandInfo, ReadSpec, analyze
from minisweagent.synapse.config import SynapseConfig
from minisweagent.synapse.diff import C0, C1, C1P, C2, C3, LineMap, SymbolChange, diff_symbols, map_through
from minisweagent.synapse.execparse import parse_frames, parse_verdict
from minisweagent.synapse.probe import Probe, fetch_file, files_mentioning, git_grep, locate, read_status
from minisweagent.synapse.state import (
    ECHO,
    EVICTABLE_STATES,
    EXEC,
    EXEC_STALE,
    FRESH,
    HIT,
    LEX_STALE,
    LOCATED_KINDS,
    OPEN,
    SEARCH,
    SEM_STALE,
    SHIFTED,
    SUPERSEDED,
    VALID_STATES,
    VIEW,
    WAIVED,
    Evidence,
    Obligation,
)
from minisweagent.synapse.symbols import FileSymbols, language_of, parse_symbols, tree_sitter_available

logger = logging.getLogger("synapse")

_NUMBERED_RE = re.compile(r"^\s*(\d+)(?:\t| \| ?|:|  )(.*)$")
_HIT_RE = re.compile(r"^(?P<path>[^\s:][^:\n]*?):(?P<line>\d+)[:-](?P<text>.*)$")
_COMMENT_LINE_RE = re.compile(r"^\s*(#|//|/\*|\*|$)")
_IMPORT_RE = re.compile(r"^\s*(from\s+\S+\s+import\b|import\b|use\s|export\s+(\{|\*)|pub\s+use\s)")
_STRING_RE = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`[^`\n]*`")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_SCRATCH_RE = re.compile(r"(repro|reproduce|debug|scratch|tmp|temp|check|verify|try|demo)", re.IGNORECASE)
_CTOR_NAMES = {"__init__", "__new__", "__post_init__", "constructor", "new"}
_COMMON_NAMES = {
    "get", "set", "run", "main", "init", "load", "save", "read", "write", "open", "close", "start", "stop", "update",
    "create", "delete", "parse", "format", "value", "name", "data", "config", "test", "call", "apply", "process",
}  # fmt: skip
_SEVERITY_WEIGHTS = {C3: 1.0, C2: 0.8, C1P: 0.4, C1: 0.2}


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def _split_lines(content: str) -> list[str]:
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _merge(ranges: list[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1 + gap:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def _to_ranges(numbers: list[int]) -> list[tuple[int, int]]:
    return _merge([(n, n) for n in numbers])


def _only_comments(lm: LineMap | None, old_lines: list[str] | None, new_lines: list[str] | None) -> bool:
    if lm is None or old_lines is None or new_lines is None:
        return False
    changed = []
    for tag, i1, i2, j1, j2 in lm.ops:
        if tag != "equal":
            changed += old_lines[i1:i2] + new_lines[j1:j2]
    return all(_COMMENT_LINE_RE.match(line) for line in changed)


def _mentions(text: str, name: str) -> bool:
    """True if `name` occurs in code (not only in comments or string literals) on this line."""
    s = text.strip()
    if _COMMENT_LINE_RE.match(s) or s.startswith(('"""', "'''")):
        return False
    s = _STRING_RE.sub('""', s)
    s = re.sub(r"//.*$|\s#.*$", "", s)
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", s) is not None


def _distinctive(name: str) -> bool:
    if name.lower() in _COMMON_NAMES:
        return False
    return len(name) >= 8 or "_" in name.strip("_") or re.search(r"[a-z][A-Z]", name) is not None


def _content_size(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(part.get("text", "")) for part in content if isinstance(part, dict))
    return 0


@dataclass
class Note:
    """What SYNAPSE appends to one observation."""

    echo: list[str] = field(default_factory=list)
    sync: list[str] = field(default_factory=list)
    eids: list[int] = field(default_factory=list)
    flags: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return render.block(self.echo + self.sync)


class SynapseEngine:
    def __init__(self, config: SynapseConfig):
        self.config = config
        self._test_res = [re.compile(p) for p in config.test_path_patterns]
        self._env_res = [re.compile(p) for p in config.env_file_patterns]
        self._ignored_res = [re.compile(p) for p in config.ignored_path_patterns]
        self.reset()

    def reset(self) -> None:
        self.stats: Counter = Counter()
        self.ready = False
        self.disabled = "" if self.config.enabled else "config"
        self.cwd = self.cwd_physical = self.root = self.cwd_rel = ""
        self.status: dict[str, tuple[str, str]] = {}
        self.initial_status: dict[str, tuple[str, str]] = {}
        self.contents: dict[str, str] = {}
        self.content_keys: dict[str, str] = {}
        self._parsed: dict[tuple[str, str], FileSymbols | None] = {}
        self.history: dict[str, list[tuple[int, LineMap]]] = {}
        self.evidence: list[Evidence] = []
        self.ev_by_id: dict[int, Evidence] = {}
        self.obligations: list[Obligation] = []
        self.heat: dict[str, float] = {}
        self.syntax_issues: dict[str, tuple[int, list[int]]] = {}
        self.baseline_errors: dict[str, int] = {}
        self.modified: dict[str, int] = {}
        self.scratch: set[str] = set()
        self.last_change_seq = self.last_change_step = self.last_env_seq = -1
        self.released: set[str] = set()
        self.gate_fired: set[str] = set()
        self.last_stub: str | None = None
        self.step = self.seq = self.cmd_seq0 = 0
        self._ts = self.config.symbol_level and tree_sitter_available()

    # ------------------------------------------------------------------ public API

    def run_action(self, action: dict, execute: Callable, step: int) -> tuple[dict, Note | None]:
        """Execute one action with SYNAPSE bookkeeping. Exceptions from `execute` (e.g. Submitted) propagate."""
        if self.disabled:
            return execute(action), None
        self.step = step
        probe = Probe(execute, timeout=self.config.probe_timeout, stats=self.stats)
        try:
            info = analyze(action.get("command", ""))
            self._ensure_ready(probe)
        except Exception:
            logger.exception("synapse: analysis failed")
            self.stats["errors"] += 1
            return execute(action), None
        if self.disabled:
            return execute(action), None
        self.stats["actions"] += 1
        note = Note()
        try:
            blocked = self._govern(info, probe)
        except Exception:
            logger.exception("synapse: governance failed")
            self.stats["errors"] += 1
            blocked = None
        if blocked is not None:
            return {"output": blocked, "returncode": 1, "exception_info": "", "extra": {"synapse_blocked": True}}, note
        output = execute(action)
        try:
            output = self._after(info, output, probe, note)
        except Exception:
            logger.exception("synapse: post-processing failed")
            self.stats["errors"] += 1
        return output, note

    def footer(self, step: int) -> str:
        cfg = self.config
        if self.disabled or not cfg.footer or not self.ready:
            return ""
        syntax = [(p, lines) for p, (s, lines) in sorted(self.syntax_issues.items()) if step - s >= cfg.footer_min_age]
        obs = []
        for o in self.obligations:
            if o.state != OPEN or o.is_test or step - o.step < cfg.footer_min_age:
                continue
            if o.shown >= cfg.footer_max_shows:
                o.state, o.reason = WAIVED, "footer"
                continue
            obs.append(o)
        obs.sort(key=lambda o: -o.severity)
        text = render.footer(obs, syntax, cfg.footer_char_budget)
        if text:
            for o in obs:
                o.shown += 1
            self.stats["footer_turns"] += 1
            self.stats["footer_chars"] += len(text)
        return text

    def observe_text(self, text: str) -> None:
        """Assistant reasoning mentions a symbol: it is being worked on."""
        if not text or not self.heat:
            return
        words = set(_WORD_RE.findall(text))
        for key in list(self.heat):
            name = key.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
            if name in words:
                self._touch(key, 0.5)

    def plan_eviction(self, messages: list[dict], step: int) -> list[tuple[int, str]]:
        """Returns (message index, replacement content) for outdated outputs whose removal pays for the cache rewrite."""
        cfg = self.config
        if self.disabled or not cfg.evict:
            return []
        sizes = [_content_size(m) for m in messages]
        cands = []
        for i, msg in enumerate(messages[: max(len(messages) - cfg.evict_keep_last, 0)]):
            extra = msg.get("extra") or {}
            eids = extra.get("synapse_eids")
            if not eids or extra.get("synapse_evicted") or not isinstance(msg.get("content"), str):
                continue
            if sizes[i] < cfg.evict_min_chars:
                continue
            evs = [self.ev_by_id[e] for e in eids if e in self.ev_by_id]
            main = [e for e in evs if e.kind in (VIEW, SEARCH, EXEC)]
            if not main or not all(e.state in EVICTABLE_STATES for e in main):
                continue
            if any(e.kind == ECHO and e.state in VALID_STATES for e in evs):
                continue
            cands.append(i)
        if not cands:
            return []
        prefix = [0]
        for s in sizes:
            prefix.append(prefix[-1] + s)
        total = prefix[-1]
        remaining = max(cfg.min_expected_remaining, cfg.expected_steps - step)
        cr, cw = cfg.cache_read_price, cfg.cache_write_price
        best, best_gain = None, 0.0
        for k, i in enumerate(cands):
            removed = sum(sizes[j] for j in cands[k:])
            tail = total - prefix[i] - removed
            gain = removed * cr * remaining - tail * (cw - cr)
            if gain > best_gain:
                best, best_gain = k, gain
        if best is None:
            return []
        plan = []
        for i in cands[best:]:
            evs = [self.ev_by_id[e] for e in messages[i]["extra"]["synapse_eids"] if e in self.ev_by_id]
            main = [e for e in evs if e.kind in (VIEW, SEARCH, EXEC)]
            for e in evs:
                e.evicted = True
            plan.append((i, render.evicted(main)))
            self.stats["evicted_messages"] += 1
            self.stats["evicted_chars"] += sizes[i]
        self.stats["eviction_rounds"] += 1
        return plan

    def summary(self) -> dict:
        out = dict(self.stats)
        out["disabled"] = self.disabled
        out["tree_sitter"] = self._ts
        out["evidence_states"] = dict(Counter(e.state for e in self.evidence))
        out["evidence_kinds"] = dict(Counter(e.kind for e in self.evidence))
        out["obligations"] = dict(Counter(o.state for o in self.obligations))
        out["modified_files"] = sorted(self.modified)
        return out

    # ------------------------------------------------------------------ setup and helpers

    def _ensure_ready(self, probe: Probe) -> None:
        if self.ready:
            return
        self.ready = True
        loc = locate(probe)
        if loc is None:
            self.disabled = "no_git"
            return
        self.cwd, self.cwd_physical, self.root = loc
        rel = posixpath.relpath(self.cwd_physical, self.root)
        if rel.startswith(".."):
            self.disabled = "cwd_outside_repo"
            return
        self.cwd_rel = "" if rel == "." else rel
        status = read_status(probe, self.root, self.config.max_status_lines)
        if status is None:
            self.disabled = "status_failed"
            return
        self.status = status
        self.initial_status = dict(status)

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _key(self, path: str, status: dict | None = None) -> str:
        entry = (self.status if status is None else status).get(path)
        return entry[1] if entry else "HEAD"

    def _is_test(self, path: str) -> bool:
        return any(r.search(path) for r in self._test_res)

    def _is_env(self, path: str) -> bool:
        return any(r.search(path) for r in self._env_res)

    def _ignored(self, path: str) -> bool:
        return any(r.search(path) for r in self._ignored_res)

    def _repo_path(self, path: str | None) -> str | None:
        """Repository-relative path ('' for the root), None if outside the repository."""
        if not path or not self.root:
            return None
        if path.startswith("/"):
            for base, prefix in ((self.root, ""), (self.cwd, self.cwd_rel), (self.cwd_physical, self.cwd_rel)):
                base = base.rstrip("/")
                if path == base:
                    rel = prefix
                elif path.startswith(base + "/"):
                    rel = posixpath.join(prefix, path[len(base) + 1 :]) if prefix else path[len(base) + 1 :]
                else:
                    continue
                rel = posixpath.normpath(rel) if rel else ""
                return "" if rel == "." else rel
            return None
        rel = posixpath.normpath(posixpath.join(self.cwd_rel, path) if self.cwd_rel else path)
        if rel == ".":
            return ""
        return None if rel.startswith("..") else rel

    def _content(self, path: str, probe: Probe) -> str | None:
        key = self._key(path)
        if path in self.contents and self.content_keys.get(path) == key:
            return self.contents[path]
        content = fetch_file(probe, self.root, path, self.config.max_file_bytes)
        if content is not None:
            self._store(path, content)
        return content

    def _store(self, path: str, content: str | None) -> None:
        if content is None:
            self.contents.pop(path, None)
            self.content_keys.pop(path, None)
        else:
            self.contents[path] = content
            self.content_keys[path] = self._key(path)

    def _symbols(self, path: str, content: str | None) -> FileSymbols | None:
        if content is None or not self._ts:
            return None
        lang = language_of(path)
        if lang is None:
            return None
        key = (path, _sha(content))
        if key not in self._parsed:
            if len(self._parsed) > 256:
                self._parsed.clear()
            try:
                self._parsed[key] = parse_symbols(content, lang)
            except Exception:
                logger.exception("synapse: parse failed for %s", path)
                self._parsed[key] = None
        return self._parsed[key]

    def _touch(self, key: str, amount: float = 1.0) -> None:
        self.heat[key] = self.heat.get(key, 0.0) + amount

    def _decay_heat(self) -> None:
        for key in list(self.heat):
            self.heat[key] *= self.config.heat_decay
            if self.heat[key] < 0.05:
                del self.heat[key]

    def _new_evidence(self, kind: str, path: str | None, lo: int, hi: int, command: str, **kwargs) -> Evidence:
        ev = Evidence(
            eid=len(self.evidence) + 1,
            kind=kind,
            step=self.step,
            seq=self._next_seq(),
            command=command,
            file=path,
            lo0=lo,
            hi0=hi,
            lo=lo,
            hi=hi,
            **kwargs,
        )
        self.evidence.append(ev)
        self.ev_by_id[ev.eid] = ev
        return ev

    # ------------------------------------------------------------------ governance (before execution)

    def _govern(self, info: CommandInfo, probe: Probe) -> str | None:
        cfg = self.config
        if info.is_submit and cfg.submit_gate:
            msg = self._submit_gate(probe)
            if msg:
                self.stats["gate_holds"] += 1
                return msg
        if cfg.line_guard and info.line_edits and info.raw not in self.released:
            for edit in info.line_edits:
                path = self._repo_path(edit.path)
                msg = self._check_line_edit(path, edit.lines) if path else None
                if msg:
                    self.released.add(info.raw)
                    self.stats["line_guard_blocks"] += 1
                    return msg
        return None

    def _check_line_edit(self, path: str, lines: list[int]) -> str | None:
        history = self.history.get(path)
        targets = sorted(set(lines))[:200]
        if not history or not targets:
            return None
        located = [e for e in self.evidence if e.file == path and e.kind in LOCATED_KINDS]
        ref = max((e for e in located if e.lo0 <= targets[0] <= e.hi0), key=lambda e: e.seq, default=None)
        if ref is None:
            return None
        maps = [lm for seq, lm in history if seq > ref.seq]
        if not maps:
            return None
        mapped = [map_through(maps, n) for n in targets]
        if all(m == n for m, n in zip(mapped, targets)):
            return None
        content = self.contents.get(path)
        return render.line_guard(path, ref, targets, mapped, _split_lines(content) if content is not None else [])

    def _submit_gate(self, probe: Probe) -> str | None:
        issues = []
        if self.syntax_issues and "syntax" not in self.gate_fired:
            self.gate_fired.add("syntax")
            issues.append(render.gate_syntax([(p, v[1]) for p, v in sorted(self.syntax_issues.items())]))
        if "obligations" not in self.gate_fired:
            pending = [o for o in self.obligations if not o.is_test and o.state in (OPEN, WAIVED)]
            pending = self._recheck(pending, probe)
            if pending:
                self.gate_fired.add("obligations")
                issues.append(render.gate_obligations(pending, len(pending)))
        execs = [e for e in self.evidence if e.kind == EXEC]
        last_exec = max((e.seq for e in execs), default=-1)
        if self.modified and last_exec < self.last_change_seq and "tests" not in self.gate_fired:
            self.gate_fired.add("tests")
            tests = [e for e in execs if e.verdict and e.verdict.get("is_test")]
            suggestion = (tests or execs)[-1].command if (tests or execs) else None
            issues.append(render.gate_tests(self.last_change_step, suggestion))
        elif self.last_env_seq > last_exec >= 0 and "env" not in self.gate_fired:
            self.gate_fired.add("env")
            issues.append(render.gate_env())
        return render.gate(issues) if issues else None

    def _recheck(self, obligations: list[Obligation], probe: Probe) -> list[Obligation]:
        """Drop obligations whose reference is gone (the agent may have fixed it with a command we could not attribute)."""
        out = []
        for o in obligations[:12]:
            content = self._content(o.file, probe)
            lines = _split_lines(content) if content is not None else []
            if 1 <= o.line <= len(lines) and _mentions(lines[o.line - 1], o.cause_name):
                out.append(o)
            else:
                o.resolve("reference gone")
        return out + obligations[12:]

    # ------------------------------------------------------------------ after execution

    def _after(self, info: CommandInfo, output: dict, probe: Probe, note: Note) -> dict:
        self.cmd_seq0 = self.seq
        self._decay_heat()
        text = output.get("output", "") or ""
        explicit = {p for p in (self._repo_path(t) for t in info.write_targets + info.inplace_targets) if p}
        changed = {} if info.pure_read else self._snapshot(probe)
        if info.is_install and output.get("returncode") == 0:
            self._env_change("installed packages", note)
        stale_searches: set[int] = set()
        for path, (old, new, code) in changed.items():
            self._file_changed(path, old, new, code, explicit, probe, note, stale_searches)
        if stale_searches:
            self._replay_searches(stale_searches, probe, note)
        self._noop_check(info, explicit, changed, output, note)
        self._report_stale(note, explicit)
        stub = self._delta_read(info, output, probe)
        if stub is not None:
            output = {
                **output,
                "output": stub,
                "extra": {**(output.get("extra") or {}), "synapse_original_output": text},
            }
            note.flags["delta_read"] = True
        else:
            self._bind(info, text, output.get("returncode"), probe, note)
        if info.is_exec or info.is_test:
            self._bind_exec(info, text, output.get("returncode"), note)
        self._trim_note(note)
        if note.echo:
            self.stats["echo_notes"] += 1
            self.stats["echo_chars"] += sum(len(x) for x in note.echo)
        if note.sync:
            self.stats["sync_notes"] += 1
            self.stats["sync_chars"] += sum(len(x) for x in note.sync)
        return output

    def _snapshot(self, probe: Probe) -> dict[str, tuple[str | None, str | None, str]]:
        cfg = self.config
        new_status = read_status(probe, self.root, cfg.max_status_lines)
        if new_status is None:
            self.stats["snapshot_failures"] += 1
            return {}
        old_status = self.status
        paths = [p for p, (_, h) in new_status.items() if p not in old_status or old_status[p][1] != h or h == "?"]
        paths += [p for p in old_status if p not in new_status]
        self.status = new_status
        result = {}
        for path in [p for p in paths if not self._ignored(p)][: cfg.max_changed_files_per_step]:
            old = self.contents.get(path) if self.content_keys.get(path) == self._key(path, old_status) else None
            if old is None and path not in old_status:
                old = fetch_file(probe, self.root, path, cfg.max_file_bytes, head=True)
            entry = new_status.get(path)
            new = None if entry and entry[1] == "-" else fetch_file(probe, self.root, path, cfg.max_file_bytes)
            if old is not None and new is not None and old == new:
                self._store(path, new)
                continue
            result[path] = (old, new, entry[0] if entry else "")
        if len(paths) > cfg.max_changed_files_per_step:
            self.stats["snapshot_truncated"] += 1
        return result

    def _file_changed(
        self,
        path: str,
        old: str | None,
        new: str | None,
        code: str,
        explicit: set[str],
        probe: Probe,
        note: Note,
        stale_searches: set[int],
    ) -> None:
        cfg = self.config
        seq = self._next_seq()
        self.stats["file_changes"] += 1
        if old is None and code.startswith("??") and ("/" not in path or _SCRATCH_RE.search(posixpath.basename(path))):
            self.scratch.add(path)
        scratch = path in self.scratch
        is_test = self._is_test(path)
        old_lines = _split_lines(old) if old is not None else None
        new_lines = _split_lines(new) if new is not None else None
        lm = LineMap(old_lines, new_lines) if old_lines is not None and new_lines is not None else None
        if lm is not None:
            self.history.setdefault(path, []).append((seq, lm))
        self._store(path, new)

        self._update_located(path, lm, new is None, path in explicit, note)
        self._update_obligations(path, lm, new is None, seq)
        for ev in self.evidence:
            if (
                ev.kind == SEARCH
                and ev.state == FRESH
                and (path in ev.hit_files or any(s == "" or path == s or path.startswith(s + "/") for s in ev.scope))
            ):
                stale_searches.add(ev.eid)

        old_fs, new_fs = self._symbols(path, old), self._symbols(path, new)
        symbol_level = bool(old_fs and new_fs and old_fs.ok and new_fs.ok)
        changes: list[SymbolChange] = []
        if symbol_level:
            changes = diff_symbols(old_fs, new_fs, cfg.contract_classifier)
        elif new is None and old_fs and old_fs.ok:
            changes = [SymbolChange(s.name, s.kind, C3, False, s, None) for s in old_fs.symbols if s.parent is None]
        if self._is_env(path):
            self._env_change(path, note)
        if symbol_level and not changes:
            cosmetic = old_fs.imports == new_fs.imports and _only_comments(lm, old_lines, new_lines)  # type: ignore[union-attr]
        else:
            cosmetic = bool(changes) and all(c.cls == C0 for c in changes)
        if not cosmetic:
            self._exec_stale(path, scratch, seq, note)
            if not scratch:
                self.modified[path] = seq
                self.last_change_seq, self.last_change_step = seq, self.step

        if new_fs is not None:
            base = self.baseline_errors.setdefault(path, len(old_fs.error_lines) if old_fs else 0)
            if len(new_fs.error_lines) > base:
                self.syntax_issues[path] = (self.step, new_fs.error_lines)
            else:
                self.syntax_issues.pop(path, None)
        elif new is None:
            self.syntax_issues.pop(path, None)

        if cfg.propagate and not scratch and not is_test:
            for change in [c for c in changes if c.cls in (C2, C3, C1P) and c.old is not None][:8]:
                self._propagate(path, change, probe, note, seq)
        for change in changes:
            self._touch(f"{path}::{change.name}", 2.0)
        self._touch(path, 2.0)
        if cfg.edit_echo:
            note.echo.extend(self._echo(path, old, new_lines, lm, changes, new_fs, cosmetic, explicit, note))

    def _update_located(self, path: str, lm: LineMap | None, deleted: bool, explicit: bool, note: Note) -> None:
        shifted = []
        for ev in self.evidence:
            if ev.file != path or ev.kind not in LOCATED_KINDS or ev.state not in (FRESH, SHIFTED, SEM_STALE):
                continue
            if deleted or lm is None:
                ev.mark(LEX_STALE, "deleted" if deleted else "changed", self.step)
                continue
            nlo, nhi, intact = lm.map_range(ev.lo, ev.hi)
            if intact:
                if (nlo, nhi) != (ev.lo, ev.hi):
                    ev.lo, ev.hi = nlo, nhi
                    if ev.state == FRESH:
                        ev.state = SHIFTED
                    shifted.append(ev)
            else:
                ev.mark(LEX_STALE, "changed", self.step)
                ev.lo, ev.hi = nlo, nhi
        shift = lm.first_shift() if lm else None
        visible = [e for e in shifted if not e.evicted and e.kind == VIEW and e.numbered and e.step < self.step]
        if self.config.sync_events and shift and visible and explicit:
            note.sync.append(render.shifted(path, shift, visible))
            note.flags["shifted"] = note.flags.get("shifted", 0) + 1

    def _update_obligations(self, path: str, lm: LineMap | None, deleted: bool, seq: int) -> None:
        for o in self.obligations:
            if o.file != path or o.state not in (OPEN, WAIVED) or o.seq >= seq:
                continue
            if deleted:
                o.resolve("file deleted")
            elif lm is None:
                o.resolve("file changed")
            else:
                mapped = lm.map_line(o.line)
                if mapped is None:
                    o.resolve("edited")
                else:
                    o.line = mapped

    def _changed_this_command(self, path: str, line: int) -> bool:
        for seq, lm in self.history.get(path, []):
            if seq > self.cmd_seq0 and any(a <= line <= b for a, b in lm.changed_new_ranges()):
                return True
        return False

    def _exec_stale(self, path: str, scratch: bool, seq: int, note: Note) -> None:
        for ev in self.evidence:
            if ev.kind != EXEC or ev.state != FRESH or ev.seq >= seq:
                continue
            if ev.depends_on(path) or (ev.global_dep and not scratch and not self._is_test(path)):
                if ev.mark(EXEC_STALE, f"{path} changed", self.step) and self.config.sync_events and not ev.evicted:
                    note.sync.append(render.exec_outdated(ev, path))
                    ev.reported = True

    def _env_change(self, reason: str, note: Note) -> None:
        self.last_env_seq = self._next_seq()
        stale = [e for e in self.evidence if e.kind == EXEC and e.state == FRESH]
        for ev in stale:
            ev.mark(EXEC_STALE, f"{reason} changed", self.step)
        if stale and self.config.sync_events:
            note.sync.append(render.env_changed(reason))

    def _propagate(self, path: str, change: SymbolChange, probe: Probe, note: Note, seq: int) -> None:
        cfg = self.config
        sym = change.old
        assert sym is not None
        name = sym.short_name
        if name in _CTOR_NAMES and sym.parent:
            name = re.split(r"[.:]+", sym.parent)[-1]
        if len(name) < cfg.min_name_length or (name.startswith("__") and name.endswith("__")):
            return
        breaking = change.cls == C3 or (change.cls == C2 and not change.compatible)
        self.stats["propagations"] += 1
        refs = []
        for f, line, text in git_grep(probe, self.root, name, cfg.max_ref_hits):
            if self._ignored(f) or language_of(f) is None or f in self.scratch:
                continue
            if f == path and change.new and change.new.start <= line <= change.new.end:
                continue
            if not _mentions(text, name) or (change.cls != C3 and _IMPORT_RE.match(text)):
                continue
            refs.append((f, line, text))
        files = sorted({f for f, _, _ in refs})[: cfg.max_ref_files]
        stem = posixpath.splitext(posixpath.basename(path))[0]
        if stem in ("__init__", "index", "mod", "lib", "main") or language_of(path) == "go":
            stem = posixpath.basename(posixpath.dirname(path)) or stem
        others = [f for f in files if f != path]
        mention = files_mentioning(probe, self.root, stem, others) if others and len(stem) >= 3 else set()
        heat_scale = cfg.heat_floor + (1 - cfg.heat_floor) * min(1.0, self.heat.get(f"{path}::{change.name}", 0.0) / 2)
        scored = []
        for f, line, text in refs:
            if f not in files:
                continue
            if f == path:
                conf = 1.0
            elif language_of(path) == "go" and posixpath.dirname(f) == posixpath.dirname(path):
                conf = 0.95
            elif f in mention:
                conf = 0.9
            else:
                conf = 0.5 if _distinctive(name) else 0.2
            sev = _SEVERITY_WEIGHTS.get(change.cls, 0.2) * heat_scale * conf
            scored.append((sev, f, line, text.strip(), conf))
        scored.sort(key=lambda r: (-r[0], r[1], r[2]))
        self.stats["refs_found"] += len(scored)

        for _, f, line, _, conf in scored:
            if conf < 0.3:
                continue
            for ev in self.evidence:
                if ev.file == f and ev.kind in (VIEW, ECHO) and ev.state in VALID_STATES and ev.covers(line):
                    if ev.seq < seq and ev.mark(SEM_STALE, f"uses {name}", self.step):
                        self.stats["sem_stale"] += 1

        cause = "renamed" if change.renamed_from else ("removed" if change.new is None else "signature changed")
        if breaking:
            for sev, f, line, text, _ in scored:
                if sev < cfg.severity_threshold or self._changed_this_command(f, line):
                    continue
                if any(o.file == f and o.line == line and o.cause_name == name for o in self.obligations):
                    continue
                self.obligations.append(
                    Obligation(
                        oid=len(self.obligations) + 1,
                        step=self.step,
                        seq=seq,
                        file=f,
                        line=line,
                        text=text,
                        cause=cause,
                        cause_name=name,
                        cls=change.cls,
                        severity=sev,
                        is_test=self._is_test(f),
                    )
                )
                self.stats["obligations_created"] += 1
        if cfg.sync_events and change.cls in (C2, C3):
            shown = [r for r in scored if r[0] >= cfg.severity_threshold and not self._changed_this_command(r[1], r[2])]
            if shown or breaking:
                note.sync.extend(render.references(path, change, shown, cfg.max_refs_listed))

    def _echo(
        self,
        path: str,
        old: str | None,
        new_lines: list[str] | None,
        lm: LineMap | None,
        changes: list[SymbolChange],
        new_fs: FileSymbols | None,
        cosmetic: bool,
        explicit: set[str],
        note: Note,
    ) -> list[str]:
        cfg = self.config
        if new_lines is None:
            return [render.deleted(path)]
        if old is None:
            names = [s.name for s in new_fs.symbols if s.parent is None][:12] if new_fs and new_fs.ok else []
            if path in explicit and new_lines:
                note.eids.append(
                    self._new_evidence(ECHO, path, 1, len(new_lines), "(file creation)", numbered=False).eid
                )
            return [render.created(path, len(new_lines), names)]
        if lm is None:
            return [render.changed_unknown(path)]
        if cosmetic:
            return [render.cosmetic_only(path)]
        out = [render.edit_header(path, lm)]
        for change in changes:
            if change.cls != C0:
                out.extend(render.symbol_change(change))
        out = out[:12]
        ranges = lm.changed_new_ranges()
        n_changed = sum(b - a + 1 for a, b in ranges)
        budget = cfg.echo_lines_per_step - note.flags.get("echo_lines", 0)
        if n_changed > 2 * cfg.echo_lines_per_symbol or budget <= 0:
            out.append(render.large_change(n_changed))
        else:
            ctx = cfg.echo_context_lines
            slices = _merge([(max(1, a - ctx), min(len(new_lines), b + ctx)) for a, b in ranges])
            for lo, hi in slices:
                if lo > len(new_lines):
                    continue
                hi = min(hi, lo + min(cfg.echo_lines_per_symbol, budget) - 1)
                if hi < lo:
                    break
                budget -= hi - lo + 1
                out.extend(render.slice_lines(new_lines, lo, hi))
                note.flags.setdefault("echo_ranges", {}).setdefault(path, []).append((lo, hi))
                note.eids.append(self._new_evidence(ECHO, path, lo, hi, "(edit echo)", numbered=True).eid)
            note.flags["echo_lines"] = cfg.echo_lines_per_step - budget
        if path in self.syntax_issues and self.syntax_issues[path][0] == self.step:
            out.append(render.syntax_error(path, self.syntax_issues[path][1]))
        return out

    def _replay_searches(self, eids: set[int], probe: Probe, note: Note) -> None:
        cfg = self.config
        evs = sorted((self.ev_by_id[e] for e in eids), key=lambda e: -e.seq)
        for i, ev in enumerate(evs):
            if ev.state != FRESH:
                continue
            if not ev.replay or i >= cfg.max_replays_per_step:
                if ev.hit_files & set(self.history):
                    ev.mark(LEX_STALE, "files changed", self.step)
                continue
            out, _ = probe.run(f"cd {shlex.quote(self.cwd)} && {{ {ev.replay}\n}}", timeout=cfg.replay_timeout)
            self.stats["replays"] += 1
            if _sha(out) != ev.replay_hash:
                ev.mark(LEX_STALE, "results changed", self.step)
                if cfg.sync_events and not ev.evicted:
                    note.sync.append(render.search_changed(ev))
                    ev.reported = True

    def _noop_check(self, info: CommandInfo, explicit: set[str], changed: dict, output: dict, note: Note) -> None:
        if output.get("returncode") != 0 or not self.config.edit_echo:
            return
        for target in info.inplace_targets:
            path = self._repo_path(target)
            if path and path not in changed and not self._ignored(path) and "*" not in path:
                note.echo.append(render.noop(path, info.raw))
                self.stats["noop_edits"] += 1

    def _report_stale(self, note: Note, explicit: set[str]) -> None:
        if not self.config.sync_events:
            return
        echoed = note.flags.get("echo_ranges", {})
        n = 0
        for ev in self.evidence:
            if ev.kind != VIEW or ev.state != LEX_STALE or ev.reported or ev.evicted or ev.step >= self.step:
                continue
            ev.reported = True
            if ev.file in explicit or ev.reason_step != self.step:
                continue
            if any(lo <= ev.hi and ev.lo <= hi for lo, hi in echoed.get(ev.file, [])):
                continue
            if n < self.config.max_notes_per_kind:
                note.sync.append(render.outdated(ev))
                n += 1

    def _trim_note(self, note: Note) -> None:
        budget, kept = self.config.event_char_budget, []
        size = 0
        for line in note.sync:
            if size + len(line) > budget:
                kept.append(f"(+{len(note.sync) - len(kept)} more notes omitted)")
                break
            kept.append(line)
            size += len(line)
        note.sync = kept

    # ------------------------------------------------------------------ evidence binding

    def _bound_ranges(
        self, spec: ReadSpec, text: str, content: str, single: bool
    ) -> tuple[list[tuple[int, int]], bool]:
        """Line ranges of `content` that are visible in `text`, and whether they were shown with line numbers."""
        cfg = self.config
        lines = _split_lines(content)
        n = len(lines)
        limit = cfg.observation_char_limit
        truncated = len(text) >= limit
        out_lines = (text[: limit // 2] + "\n" + text[-limit // 2 :] if truncated else text).split("\n")
        numbered = []
        for line in out_lines:
            m = _NUMBERED_RE.match(line)
            if m:
                numbered.append((int(m.group(1)), m.group(2)))
        non_empty = sum(1 for line in out_lines if line.strip())
        if numbered and (spec.numbered or len(numbered) >= 0.8 * max(non_empty, 1)):
            matched = [k for k, t in numbered if 1 <= k <= n and t.strip() == lines[k - 1].strip()]
            if matched:
                return _merge(_to_ranges(matched), gap=1)[: cfg.max_bound_ranges], True
        if single and spec.exact and not truncated and n:
            if spec.tail is not None:
                lo, hi = max(1, n - spec.tail + 1), n
            else:
                lo, hi = spec.lo or 1, min(spec.hi or n, n)
            first = next((line for line in out_lines if line.strip()), None)
            if lo <= hi and (first is None or first.strip() == lines[lo - 1].strip()):
                return [(lo, hi)], False
        return self._anchor(out_lines, lines), False

    def _anchor(self, out_lines: list[str], file_lines: list[str]) -> list[tuple[int, int]]:
        cfg = self.config
        a = [line.strip() for line in out_lines]
        b = [line.strip() for line in file_lines]
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        ranges = []
        for blk in matcher.get_matching_blocks():
            if blk.size >= cfg.anchor_min_lines and sum(1 for x in a[blk.a : blk.a + blk.size] if x) >= 2:
                ranges.append((blk.b + 1, blk.b + blk.size))
        return _merge(ranges)[: cfg.max_bound_ranges]

    def _delta_read(self, info: CommandInfo, output: dict, probe: Probe) -> str | None:
        cfg = self.config
        if not (cfg.delta_read and info.pure_read and len(info.reads) == 1 and not info.search):
            return None
        text = output.get("output", "") or ""
        if output.get("returncode") != 0 or len(text) < cfg.delta_read_min_chars:
            return None
        if self.last_stub == info.raw:
            self.last_stub = None
            return None
        spec = info.reads[0]
        path = self._repo_path(spec.path)
        content = self._content(path, probe) if path else None
        if content is None:
            return None
        ranges, numbered = self._bound_ranges(spec, text, content, single=True)
        if len(ranges) != 1:
            return None
        lo, hi = ranges[0]
        prior = [
            e
            for e in self.evidence
            if e.file == path
            and e.kind in (VIEW, ECHO)
            and e.state == FRESH
            and not e.evicted
            and e.lo <= lo
            and e.hi >= hi
            and e.step < self.step
            and self.step - e.step <= cfg.delta_read_max_steps
            and (e.numbered or not numbered)
        ]
        if not prior:
            return None
        ev = max(prior, key=lambda e: e.seq)
        self.last_stub = info.raw
        self.stats["delta_reads"] += 1
        self.stats["delta_read_chars_saved"] += len(text)
        self._touch(path, 1.0)
        return render.delta_read_stub(path, lo, hi, ev)

    def _bind(self, info: CommandInfo, text: str, returncode, probe: Probe, note: Note) -> None:
        if not text.strip():
            return
        single = len(info.reads) == 1 and not info.search
        bound = set()
        for spec in info.reads[:5]:
            path = self._repo_path(spec.path)
            if not path or self._ignored(path):
                continue
            content = self._content(path, probe)
            if content is None:
                continue
            ranges, numbered = self._bound_ranges(spec, text, content, single)
            replay = info.raw if info.pure_read and single else None
            self._bind_views(path, ranges, numbered or spec.numbered, info.raw, content, replay, note)
            bound.add(path)
        if info.search:
            self._bind_search(info, text, note)
        elif not info.reads and not info.is_exec and not info.is_test and info.path_args:
            for arg in info.path_args[:3]:
                path = self._repo_path(arg)
                if not path or path in bound or self._ignored(path) or language_of(path) is None:
                    continue
                content = self._content(path, probe)
                if content is None:
                    continue
                ranges = self._anchor(text.split("\n"), _split_lines(content))
                self._bind_views(path, ranges, False, info.raw, content, None, note)

    def _bind_views(
        self,
        path: str,
        ranges: list[tuple[int, int]],
        numbered: bool,
        command: str,
        content: str,
        replay: str | None,
        note: Note,
    ) -> None:
        fs = self._symbols(path, content)
        for lo, hi in ranges:
            for old in self.evidence:
                if (
                    old.file == path
                    and old.kind in (VIEW, ECHO)
                    and old.state in (FRESH, SHIFTED, SEM_STALE)
                    and lo <= old.lo
                    and old.hi <= hi
                ):
                    old.mark(SUPERSEDED, "re-read", self.step)
            ev = self._new_evidence(VIEW, path, lo, hi, command, replay=replay, numbered=numbered)
            if fs is not None:
                ev.symbols = {s.name for s in fs.overlapping(lo, hi)[:20]}
                for name in ev.symbols:
                    self._touch(f"{path}::{name}", 0.3)
            note.eids.append(ev.eid)
            for o in self.obligations:
                if o.file == path and o.state in (OPEN, WAIVED) and o.cls != C3 and lo <= o.line <= hi:
                    o.resolve("reviewed")
            self.stats["views_bound"] += 1

    def _bind_search(self, info: CommandInfo, text: str, note: Note) -> None:
        single = self._repo_path(info.search_single_file) if info.search_single_file else None
        hits = []
        for line in text.split("\n")[:2000]:
            if single:
                m = re.match(r"^(\d+)[:-]", line)
                if m:
                    hits.append((single, int(m.group(1))))
                    continue
            m = _HIT_RE.match(line)
            if m:
                path = self._repo_path(m.group("path"))
                if path:
                    hits.append((path, int(m.group("line"))))
        scope = [s for s in (self._repo_path(p) for p in (info.search_paths or ["."])) if s is not None]
        ev = self._new_evidence(
            SEARCH,
            None,
            0,
            0,
            info.raw,
            replay=info.raw if info.pure_read else None,
            replay_hash=_sha(text),
            scope=scope,
            hit_files={p for p, _ in hits},
        )
        for old in self.evidence:
            if old is not ev and old.kind == SEARCH and old.command == info.raw and old.state == FRESH:
                old.mark(SUPERSEDED, "re-run", self.step)
        note.eids.append(ev.eid)
        for path, line in hits[: self.config.max_hits_per_search]:
            hit = self._new_evidence(HIT, path, line, line, info.raw, parent=ev.eid, numbered=True)
            note.eids.append(hit.eid)
        self.stats["searches_bound"] += 1

    def _bind_exec(self, info: CommandInfo, text: str, returncode, note: Note) -> None:
        dep_files, dep_dirs = set(), set()
        for arg in info.path_args:
            arg = arg.split("::", 1)[0]
            is_dir = arg.endswith("/...") or language_of(arg) is None
            path = self._repo_path(arg[:-4] if arg.endswith("/...") else arg)
            if path is None or self._ignored(path):
                continue
            if is_dir:
                if path:
                    dep_dirs.add(path)
            else:
                dep_files.add(path)
        for frame_file, frame_line in parse_frames(text):
            path = self._repo_path(frame_file)
            if path and not self._ignored(path) and path in self.contents:
                fs = self._symbols(path, self.contents[path])
                sym = fs.innermost(frame_line) if fs else None
                if sym is not None:
                    self._touch(f"{path}::{sym.name}", 1.0)
        verdict = parse_verdict(text, returncode)
        verdict["is_test"] = info.is_test
        normalized = " ".join(info.raw.split())
        for old in self.evidence:
            if old.kind == EXEC and " ".join(old.command.split()) == normalized and old.state in (FRESH, EXEC_STALE):
                old.state, old.reason, old.reason_step = SUPERSEDED, "re-run", self.step
        ev = self._new_evidence(
            EXEC,
            None,
            0,
            0,
            info.raw,
            dep_files=dep_files,
            dep_dirs=dep_dirs,
            global_dep=not dep_dirs,
            verdict=verdict,
        )
        note.eids.append(ev.eid)
        self.stats["execs_bound"] += 1
