"""AST-Shell: rewrite the output of file reads and searches so that it follows symbol boundaries."""

import posixpath
import re
import shlex
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from minisweagent.astshell.command import (
    Block,
    ReadSpec,
    SearchSpec,
    decode_b64,
    lang_of,
    parse_markers,
    plan_command,
    select_lines,
    strip_markers,
)
from minisweagent.astshell.config import ASTShellConfig
from minisweagent.astshell.render import (
    ReadCtx,
    Rendered,
    parse_search,
    render_read,
    render_search,
    runs_of,
    search_fallback,
    symbol_sha,
    verify,
)
from minisweagent.astshell.symbols import FUNCTION_KINDS, ParseCache, ParsedFile, Symbol, get_parser
from minisweagent.utils.log import logger

_FETCH = (
    "for f in {paths}; do printf '\\036AST:F %s ' \"$f\"; "
    'if [ -f "$f" ] && [ "$(wc -c < "$f" | tr -d \' \')" -le {max} ]; then '
    "base64 -w0 \"$f\" 2>/dev/null || base64 \"$f\" | tr -d '\\n'; fi; printf '\\n'; done"
)


@dataclass
class SessionState:
    action_index: int = 0
    hits: dict[str, tuple[str, set[int], int]] = field(default_factory=dict)
    seen: dict[tuple[str, str, str], tuple[int, int]] = field(default_factory=dict)
    read_ranges: dict[str, list[tuple]] = field(default_factory=dict)


@dataclass
class _BlockResult:
    block: Block
    spec: ReadSpec | SearchSpec
    fallback: str
    rendered: Rendered | None = None
    pf: ParsedFile | None = None
    path: str = ""
    seq: list[int] | None = None
    reason: str = ""
    ops: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.rendered.text if self.rendered is not None else self.fallback


class ASTShell:
    def __init__(self, config: ASTShellConfig):
        self.config = config
        self.cache = ParseCache()
        self.stats: Counter = Counter()
        if config.enabled:
            try:
                get_parser("python")
            except Exception as e:  # missing parsers must degrade to passthrough, never break the agent
                logger.warning(f"AST-Shell disabled, tree-sitter parsers unavailable: {type(e).__name__}: {e}")
                self.config = config.model_copy(update={"enabled": False})
                self.stats["disabled_no_parser"] = 1
        self.reset()

    def reset(self, *, task: str = "") -> None:
        self.state = SessionState()
        self.task_names = set(re.findall(r"[A-Za-z_]\w{3,}", task))

    def run(self, action: dict, execute: Callable[[dict], dict], *, turn: int = 0) -> dict:
        if not self.config.enabled:
            return execute(action)
        start = time.perf_counter()
        self.state.action_index += 1
        self.stats["actions"] += 1
        plan = plan_command(action.get("command", ""), self.config)
        if plan is None:
            return execute(action)
        out = execute({**action, "command": plan.command})
        if out.get("returncode") == -1 and out.get("exception_info"):
            self.stats["fallback_exception"] += 1
            return {**out, "output": strip_markers(out.get("output", ""))}
        pieces = parse_markers(out.get("output", ""))
        results = [self._result(p, plan.specs[p.id], p.id in plan.enlarged) for p in pieces if isinstance(p, Block)]
        reads = [r for r in results if isinstance(r.spec, ReadSpec)]
        for r in reads:
            self._guarded(r, self._prepare_read, r)
        for r in reads:
            if r.seq is not None:
                self._guarded(r, self._read, r, reads, turn)
        searches = [r for r in results if isinstance(r.spec, SearchSpec)]
        if searches:
            self._searches(searches, execute)
        self._budget(pieces, results)
        for r in results:
            rendered = r.rendered
            if rendered and r.pf and rendered.ops and not verify(rendered.text, r.pf.lines, rendered.start):
                r.rendered, r.reason = None, "verify_failed"
        by_block = {id(r.block): r for r in results}
        original = "".join(by_block[id(p)].fallback if isinstance(p, Block) else p for p in pieces)
        final = "".join(by_block[id(p)].text if isinstance(p, Block) else p for p in pieces)
        if len(final) <= self.config.observation_char_limit:
            self._record_seen(results, turn)
        log = self._log(results, original, final, start)
        return {**out, "output": final, "extra": {**out.get("extra", {}), "ast_shell": log}}

    @staticmethod
    def _result(block: Block, spec: ReadSpec | SearchSpec, enlarged: bool) -> _BlockResult:
        fallback = search_fallback(block.raw, spec, enlarged) if isinstance(spec, SearchSpec) else block.raw
        return _BlockResult(block, spec, fallback)

    @staticmethod
    def _guarded(r: _BlockResult, fn: Callable, *args):
        try:
            return fn(*args)
        except Exception as e:  # a single block must never break the command
            r.rendered, r.reason = None, f"error: {type(e).__name__}: {e}"

    # ------------------------------------------------------------ reads

    def _hits(self, path: str, sha: str) -> set[int]:
        entry = self.state.hits.get(path)
        if entry is None or entry[0] != sha or self.state.action_index - entry[2] > self.config.hits_window:
            return set()
        return entry[1]

    def _seen_ago(self, path: str, pf: ParsedFile, sym: Symbol, turn: int) -> int | None:
        entry = self.state.seen.get((path, sym.name, symbol_sha(pf, sym)))
        if entry is None or self.state.action_index - entry[1] > self.config.seen_window:
            return None
        return turn - entry[0]

    def _prepare_read(self, r: _BlockResult) -> None:
        blk, spec = r.block, r.spec
        if blk.rc != 0 or blk.content is None:
            r.reason = "nonzero_rc" if blk.rc != 0 else "no_snapshot"
            return
        pf = r.pf = self.cache.get(blk.content, spec.lang)
        if pf.skip_reason:
            r.reason = pf.skip_reason
            return
        seq = select_lines(len(pf.lines), spec.ops)
        expected = "\n".join(f"{i:>6}\t{pf.lines[i - 1]}" if spec.numbered else pf.lines[i - 1] for i in seq)
        if blk.raw.rstrip("\n") != expected.rstrip("\n"):
            r.reason = "raw_mismatch"
            return
        r.path, r.seq = posixpath.normpath(posixpath.join(blk.pwd, spec.path)), seq

    @staticmethod
    def _elsewhere(r: _BlockResult, reads: list[_BlockResult]) -> dict[int, str]:
        """Lines of the same file content that the other reads of this command show."""
        out: dict[int, str] = {}
        before = True
        for o in reads:
            if o is r:
                before = False
                continue
            if o.seq is None or (o.path, o.pf.sha1) != (r.path, r.pf.sha1):
                continue
            shown = set(o.seq) | (
                {i for i in o.rendered.items if isinstance(i, int)} if before and o.rendered else set()
            )
            for i in shown:
                out.setdefault(i, "shown above" if before else "shown below")
        return out

    def _read(self, r: _BlockResult, reads: list[_BlockResult], turn: int) -> None:
        spec, pf, seq = r.spec, r.pf, r.seq
        runs = tuple(runs_of(seq))
        previous = self.state.read_ranges.setdefault(r.path, [])
        repeat = runs in previous
        previous.append(runs)
        ctx = ReadCtx(
            pf, r.path, self.config, self._hits(r.path, pf.sha1), self.task_names,
            lambda sym: self._seen_ago(r.path, pf, sym, turn), self._elsewhere(r, reads),
        )  # fmt: skip
        start_known = seq and not spec.numbered and not any(op == "tail" for op, _ in spec.ops)
        r.rendered = render_read(ctx, seq, r.block.raw, start=seq[0] if start_known else None, repeat=repeat)

    def _record_seen(self, results: list[_BlockResult], turn: int) -> None:
        for r in results:
            if r.pf is None or r.rendered is None or not r.path:
                continue
            shown = {i for i in r.rendered.items if isinstance(i, int)}
            for sym in r.pf.symbols:
                if (
                    sym.is_leaf
                    and sym.kind in FUNCTION_KINDS
                    and all(i in shown for i in range(sym.start, sym.end + 1))
                ):
                    self.state.seen[(r.path, sym.name, symbol_sha(r.pf, sym))] = (turn, self.state.action_index)

    # ------------------------------------------------------------ searches

    def _fetch(self, paths: list[str], execute: Callable[[dict], dict]) -> dict[str, bytes | None]:
        if not paths:
            return {}
        command = _FETCH.format(paths=" ".join(shlex.quote(p) for p in paths), max=self.config.max_file_bytes)
        out = execute({"command": command})
        files = {}
        for line in out.get("output", "").split("\n"):
            if line.startswith("\x1eAST:F "):
                path, _, b64 = line[7:].rpartition(" ")
                files[path] = decode_b64(b64)
        return files

    def _searches(self, results: list[_BlockResult], execute: Callable[[dict], dict]) -> None:
        parsed = {}
        for r in results:
            if r.block.rc not in (0, 1):
                r.reason = "nonzero_rc"
                continue
            if (matches := self._guarded(r, parse_search, r.block.raw, r.spec, r.block.pwd)) is not None:
                parsed[id(r)] = matches
        counts = Counter(m.abspath for matches in parsed.values() for m in matches if m.kind == "match")
        paths = [p for p, _ in counts.most_common() if lang_of(p) in self.config.languages][
            : self.config.grep_max_files
        ]
        files: dict[str, ParsedFile | None] = {}
        for path, content in self._fetch(paths, execute).items():
            pf = self.cache.get(content, lang_of(path)) if content else None
            files[path] = pf if pf is not None and not pf.skip_reason else None
        for r in results:
            if id(r) not in parsed:
                continue
            matches = parsed[id(r)]
            truncated = r.block.raw.count("\n") >= self.config.grep_head_max
            r.rendered = self._guarded(r, render_search, matches, r.spec, files, self.config, truncated)
            r.reason = r.reason or ("" if r.rendered else "no_annotation")
            for m in matches:
                if m.kind == "match" and m.valid and (pf := files.get(m.abspath)) is not None:
                    sha, lines, _ = self.state.hits.get(m.abspath, (pf.sha1, set(), 0))
                    lines = lines if sha == pf.sha1 else set()
                    self.state.hits[m.abspath] = (pf.sha1, lines | {m.line}, self.state.action_index)

    # ------------------------------------------------------------ budget & logging

    def _budget(self, pieces: list, results: list[_BlockResult]) -> None:
        if not self.config.budget_guard:
            return
        budget = int(self.config.budget_ratio * self.config.observation_char_limit)
        other = sum(len(p) for p in pieces if isinstance(p, str))
        while other + sum(len(r.text) for r in results) > budget:
            candidates = [r for r in results if r.rendered is not None and r.rendered.alts]
            if not candidates:
                return
            r = max(candidates, key=lambda r: len(r.text))
            nxt, rest = r.rendered.alts[0], r.rendered.alts[1:]
            nxt.alts = rest
            if len(nxt.text) < len(r.text):
                r.ops.append("budget")
                r.rendered = nxt
            else:
                r.rendered.alts = rest

    def _log(self, results: list[_BlockResult], original: str, final: str, start: float) -> dict:
        blocks = []
        for r in results:
            ops = (r.rendered.ops if r.rendered is not None else []) + r.ops
            changed = r.rendered is not None and r.text != r.fallback
            status = "rewritten" if changed else "fallback" if r.reason else "unchanged"
            self.stats[f"blocks_{status}"] += 1
            for op in ops if changed else []:
                self.stats[f"op_{op}"] += 1
            if r.reason:
                self.stats[f"reason_{r.reason.split(':')[0]}"] += 1
            fmt = ("anchored" if r.rendered.start is not None else "numbered") if changed and r.pf else ""
            if fmt:
                self.stats[f"format_{fmt}"] += 1
            blocks.append(
                {
                    "id": r.block.id,
                    "type": "read" if isinstance(r.spec, ReadSpec) else "search",
                    "path": r.path or getattr(r.spec, "path", ""),
                    "status": status,
                    "reason": r.reason,
                    "ops": ops if changed else [],
                    "format": fmt,
                    "orig_chars": len(r.fallback),
                    "final_chars": len(r.text),
                }
            )
        self.stats["chars_original"] += len(original)
        self.stats["chars_final"] += len(final)
        log = {
            "v": 1,
            "status": "rewritten" if final != original else "unchanged",
            "orig_chars": len(original),
            "final_chars": len(final),
            "latency_ms": round(1000 * (time.perf_counter() - start), 1),
            "blocks": blocks,
        }
        if self.config.log_original:
            log["original"] = original
        return log
