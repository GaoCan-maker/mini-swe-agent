"""Requirements ledger: tracks which task requirements have been exercised by a passing check.

For every step it records (a) when each changed file last changed and (b) every check run
(tests or scripts) with its verdict and the requirements its command, output and script source mention.
On the first submission it compares the requirements against the final diff and the check history,
and holds the submission back once if some requirements were never covered by a passing check
after the code implementing them last changed.
"""

import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from minisweagent.req.bash import TEST_RE, CommandInfo, analyze
from minisweagent.req.clauses import Clause, extract_clauses
from minisweagent.req.config import ReqConfig
from minisweagent.req.probe import Probe, changed_code, locate, read_files, snapshot
from minisweagent.req.verdict import is_check, passed

logger = logging.getLogger("req")

_SCRIPT_EXT_RE = re.compile(r"\.(py|js|mjs|cjs|ts|tsx|jsx|go|rs|rb|sh|java|kt|php|c|cc|cpp|h)$")

MISSING = "missing"
STALE = "stale"
UNVERIFIED = "unverified"
UNCHECKED = "unchecked"
VERIFIED = "verified"
VAGUE = "vague"
_STATUS_ORDER = [MISSING, UNVERIFIED, UNCHECKED, STALE]
_STATUS_LABEL = {
    MISSING: "no matching code change found",
    UNVERIFIED: "implemented, but no passing check exercises it",
    UNCHECKED: "no passing check exercises it",
    STALE: "checked only before the related code changed again",
}


@dataclass
class CheckRun:
    step: int
    command: str
    passed: bool
    covers: dict[int, float] = field(default_factory=dict)
    is_test: bool = False


class RequirementLedger:
    def __init__(self, config: ReqConfig):
        self.config = config
        self._test_res = [re.compile(p) for p in config.test_path_patterns]
        self._scratch_res = [re.compile(p) for p in config.scratch_patterns]
        self._ignored_res = [re.compile(p) for p in config.ignored_path_patterns]
        self._meta_res = [re.compile(p, re.IGNORECASE) for p in config.meta_patterns]
        self.reset("")

    def reset(self, task: str) -> None:
        self.clauses: list[Clause] = (
            extract_clauses(task, self.config.boilerplate_patterns, self.config.max_clauses) if task else []
        )
        self.ready = False
        self.disabled = "" if self.config.enabled else "config"
        self.root = self.base = ""
        self.state: dict[str, str] = {}
        self.last_change: dict[str, int] = {}
        self.runs: list[CheckRun] = []
        self.gate_fired = False
        self.gate_message = ""
        self.statuses: dict[int, str] = {}
        self.stats: Counter = Counter()

    # ------------------------------------------------------------------ step hook

    def run_action(self, action: dict, execute: Callable, *, step: int) -> dict:
        if self.disabled:
            return execute(action)
        command = action.get("command", "")
        probe = Probe(execute, timeout=self.config.probe_timeout, stats=self.stats)
        try:
            info = analyze(command)
            self._ensure_ready(probe)
        except Exception:
            logger.exception("req: setup failed")
            self.stats["errors"] += 1
            return execute(action)
        if self.disabled:
            return execute(action)
        if info.is_submit and self.config.gate and not self.gate_fired:
            self.gate_fired = True
            try:
                message = self._gate(probe)
            except Exception:
                logger.exception("req: gate failed")
                self.stats["errors"] += 1
                message = ""
            if message:
                self.gate_message = message
                self.stats["gate_shown"] += 1
                return {"output": message, "returncode": 0, "exception_info": "", "extra": {"req_gate": True}}
        output = execute(action)
        try:
            self._after(info, output, probe, step)
        except Exception:
            logger.exception("req: bookkeeping failed")
            self.stats["errors"] += 1
        return output

    def _ensure_ready(self, probe: Probe) -> None:
        if self.ready:
            return
        self.ready = True
        if not self.clauses:
            self.disabled = "no_clauses"
            return
        located = locate(probe)
        if located is None:
            self.disabled = "no_git"
            return
        self.root, self.base = located
        self.state = snapshot(probe, self.root, self.base, self.config.max_paths) or {}

    def _after(self, info: CommandInfo, output: dict, probe: Probe, step: int) -> None:
        if not info.pure_read:
            new = snapshot(probe, self.root, self.base, self.config.max_paths)
            if new is None:
                self.stats["snapshot_failures"] += 1
            else:
                for path in set(new) | set(self.state):
                    if new.get(path) != self.state.get(path):
                        self.last_change[path] = step
                self.state = new
        if is_check(info):
            self._record_check(info, output, probe, step)

    def _record_check(self, info: CommandInfo, output: dict, probe: Probe, step: int) -> None:
        text = output.get("output", "") or ""
        ok = passed(text, output.get("returncode", -1))
        sources = self._check_sources(info, probe, step)
        limit = self.config.max_evidence_chars
        if len(text) > limit:
            text = text[: limit // 2] + "\n" + text[-limit // 2 :]
        evidence = "\n".join([info.raw, text, *sources])
        covers = {}
        for clause in self.clauses:
            score = clause.coverage(evidence)
            if score > 0:
                covers[clause.cid] = round(score, 3)
        self.runs.append(CheckRun(step, info.raw[:300], ok, covers, is_test=info.is_test))
        self.stats["checks_passed" if ok else "checks_failed"] += 1

    def _check_sources(self, info: CommandInfo, probe: Probe, step: int) -> list[str]:
        """Source of the scripts/test files a check executes, so their content counts as evidence."""
        scripts = []
        for path in info.path_args:
            path = path.split("::", 1)[0]
            if _SCRIPT_EXT_RE.search(path) and path not in scripts:
                scripts.append(path)
        scripts = scripts[: self.config.max_scripts_per_run]
        contents = list(read_files(probe, scripts, self.config.max_file_bytes).values()) if scripts else []
        if TEST_RE.search(info.raw):
            recent = sorted(
                (p for p in self.state if self.state[p] != "-" and self._is_test(p) and p not in scripts),
                key=lambda p: self.last_change.get(p, -1),
                reverse=True,
            )[: self.config.max_test_files_per_run]
            if recent:
                files = read_files(probe, recent, self.config.max_file_bytes, root=self.root)
                contents.extend(files.values())
        return contents

    # ------------------------------------------------------------------ classification

    def _is_test(self, path: str) -> bool:
        return any(r.search(path) for r in self._test_res)

    def _is_scratch(self, path: str, untracked: set[str]) -> bool:
        if any(r.search(path) for r in self._scratch_res):
            return True
        return "/" not in path and path in untracked and bool(_SCRIPT_EXT_RE.search(path))

    # ------------------------------------------------------------------ gate

    def evaluate(self, code: dict[str, str], untracked: set[str] = frozenset()) -> dict[int, str]:
        cfg = self.config
        impl = {
            p: c
            for p, c in code.items()
            if not self._is_test(p)
            and not self._is_scratch(p, untracked)
            and not any(r.search(p) for r in self._ignored_res)
        }
        all_impl = "\n".join(impl.values())
        global_last = max((self.last_change.get(p, -1) for p in impl), default=-1)
        suites = [r for r in self.runs if r.passed and r.is_test]
        statuses = {}
        for clause in self.clauses:
            files = [p for p, c in impl.items() if clause.touches(c)] if clause.anchors else []
            last = max((self.last_change.get(p, -1) for p in files), default=global_last)
            good = [r for r in self.runs if r.passed and self._accepts(clause, r.covers.get(clause.cid, 0.0))]
            if (not clause.anchors and len(clause.keywords) < cfg.min_keywords) or any(
                r.search(clause.text) for r in self._meta_res
            ):
                status = VAGUE
            elif any(r.step >= last for r in good) or (good and any(r.step >= last for r in suites)):
                status = VERIFIED
            elif good:
                status = STALE
            elif not clause.anchors:
                status = UNCHECKED
            elif self._accepts(clause, clause.coverage(all_impl)):
                status = UNVERIFIED
            else:
                status = MISSING
            statuses[clause.cid] = status
        return statuses

    def _accepts(self, clause: Clause, score: float) -> bool:
        cfg = self.config
        return clause.accepts(score, cfg.anchor_threshold, cfg.keyword_threshold, cfg.min_keywords)

    def _gate(self, probe: Probe) -> str:
        cfg = self.config
        code, untracked = changed_code(probe, self.root, self.base, cfg.max_diff_bytes, cfg.max_file_bytes)
        if not code:
            self.stats["gate_no_diff"] += 1
            return ""
        self.statuses = self.evaluate(code, untracked)
        return self.render(self.statuses)

    def render(self, statuses: dict[int, str]) -> str:
        pending = [c for c in self.clauses if statuses.get(c.cid, VERIFIED) in _STATUS_ORDER]
        if not pending:
            return ""
        verified = [c.cid for c in self.clauses if statuses.get(c.cid) == VERIFIED]
        lines = [
            "Submission held back once for a requirements review (this happens only once).",
            "",
            f"I split the task description into {len(self.clauses)} requirements and compared them, approximately, "
            "with your code changes and with the checks you ran (test runs and scripts that finished without "
            "failures). These requirements are not covered by a passing check made after the related code last "
            "changed:",
            "",
        ]
        shown, omitted = self._fit(pending, statuses, self.config.max_gate_chars - 1500)
        lines += self._render_items(shown, statuses)
        if omitted:
            lines.append("Not shown for space: " + ", ".join(f"#{c.cid}" for c in omitted))
        if verified:
            lines += ["", "Covered by a passing check: " + ", ".join(f"#{cid}" for cid in verified)]
        lines += [
            "",
            "The matching is lexical and can be wrong. For each item above, check it against your diff:",
            "- if it is not implemented or you are unsure, implement it and run a small targeted check that "
            "exercises exactly that behavior, then fix anything that fails;",
            "- if it is already handled, say briefly where (one line per item) and move on.",
            "Details stated explicitly in the task text are usually what the hidden tests check. "
            "When you are done, submit again; there is no further review.",
        ]
        return "\n".join(lines)

    def _render_items(self, clauses: list[Clause], statuses: dict[int, str]) -> list[str]:
        limit = self.config.max_clause_chars
        lines, context = [], None
        for clause in clauses:
            if clause.context != context:
                context = clause.context
                lines.append(f"[{context[:160]}]" if context else "[Task description]")
            text = clause.text if len(clause.text) <= limit else clause.text[:limit] + "..."
            lines.append(f"  #{clause.cid} ({_STATUS_LABEL[statuses[clause.cid]]}) {text}")
        return lines

    def _fit(self, pending: list[Clause], statuses: dict[int, str], budget: int) -> tuple[list[Clause], list[Clause]]:
        """Keep document order; drop the least actionable items first until the list fits the budget."""
        shown = list(pending)
        omitted: list[Clause] = []
        by_priority = sorted(pending, key=lambda c: (_STATUS_ORDER.index(statuses[c.cid]), c.cid), reverse=True)
        while len(shown) > 1 and len("\n".join(self._render_items(shown, statuses))) > budget:
            victim = by_priority.pop(0)
            shown.remove(victim)
            omitted.append(victim)
        return shown, sorted(omitted, key=lambda c: c.cid)

    # ------------------------------------------------------------------ reporting

    def summary(self) -> dict:
        return {
            "enabled": not self.disabled,
            "disabled_reason": self.disabled,
            "base": self.base,
            "n_clauses": len(self.clauses),
            "clauses": [
                {
                    "cid": c.cid,
                    "text": c.text[:300],
                    "context": c.context,
                    "anchors": [a.text for a in c.anchors],
                    "keywords": c.keywords,
                    "status": self.statuses.get(c.cid, ""),
                }
                for c in self.clauses
            ],
            "checks": [
                {"step": r.step, "command": r.command[:200], "passed": r.passed, "covers": r.covers} for r in self.runs
            ],
            "last_change": dict(self.last_change),
            "gate_fired": self.gate_fired,
            "gate_message": self.gate_message,
            "stats": {k: round(v, 3) if isinstance(v, float) else v for k, v in self.stats.items()},
        }
