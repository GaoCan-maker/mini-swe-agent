"""Phase-Keyframe Compaction (PKC): at a few calm points, remove older turns and keep one keyframe in their place."""

import re
import shlex

from minisweagent import Environment, Model
from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.agents.pkc import ctx
from minisweagent.agents.pkc.commands import CTX_USE, REVERT, SRC_EXT, is_green, norm_path, test_key, test_summary
from minisweagent.agents.pkc.config import PKCConfig
from minisweagent.agents.pkc.files import FileTracker
from minisweagent.agents.pkc.fold import history_lines, search_summary, short
from minisweagent.agents.pkc.keyframe import Candidate, Keyframe, WorkingSet, build_working_set, frozen_text
from minisweagent.agents.pkc.ledger import Ledger, Record
from minisweagent.agents.pkc.notes import FIELDS, Item, NoteContext, parse_block, parse_items, validate
from minisweagent.agents.pkc.symbols import Symbol, find, index, outline, strip_numbers, syntax_ok
from minisweagent.agents.pkc.templates import T0, T1, T2, T2_FIRST, repeated_edit, reverted, t4

WORD = re.compile(r"[A-Za-z_]\w{3,}")
EVENT_LABELS = {"first_green": "first green test", "revert": "revert", "new_target": "new target"}


class PKCAgentConfig(AgentConfig):
    pkc: PKCConfig = PKCConfig()


def _text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content or ""


def _chars(msg: dict) -> int:
    return (
        len(_text(msg))
        + len(msg.get("reasoning_content") or "")
        + sum(len(tc.get("function", {}).get("arguments", "")) for tc in msg.get("tool_calls") or [])
    )


def _hidden(msg: dict | None) -> bool:
    return msg is not None and "pkc_hidden" in msg.get("extra", {})


def _save_original(msg: dict) -> None:
    msg.setdefault("extra", {}).setdefault("pkc_original_content", msg.get("content"))


def _tail_fit(lines: list[str], budget: int, what: str) -> list[str]:
    """Latest lines that fit the budget, with a note on how many earlier ones were dropped."""
    out, used = [], 0
    for line in reversed(lines):
        if used + len(line) + 1 > budget - 60:
            break
        out.append(line)
        used += len(line) + 1
    dropped = len(lines) - len(out)
    return ([f"({dropped} earlier {what} omitted; ctx list)"] if dropped else []) + out[::-1]


class PKCAgent(DefaultAgent):
    def __init__(self, model: Model, env: Environment, *, config_class: type = PKCAgentConfig, **kwargs):
        super().__init__(model, env, config_class=config_class, **kwargs)
        self._reset()

    def _reset(self) -> None:
        self.pc: PKCConfig = self.config.pkc
        self.ledger = Ledger()
        self.files = FileTracker(self.env)
        self.keyframes: list[Keyframe] = []
        self.hosts: list[Record] = []
        self.checkpoint: list[Item] = []
        self.deltas: list[tuple[int, list[Item]]] = []
        self.feedback: list[str] = []
        self.pending: tuple[str, int, bool] | None = None
        self.marks: list[tuple[str, int]] = []
        self.events: list[tuple[int, str]] = []
        self.due_since: int | None = None
        self.last_try = 0
        self.awaiting: set[str] = set()
        self.unanswered = {"checkpoint": 0, "delta": 0}
        self.e1_step: int | None = None
        self.g1_step: int | None = None
        self.last_kf_step = 0
        self.fold_hi = 0
        self.kf_reasons: list[str] = []
        self.fold_log: list[dict] = []
        self.step_msgs: dict[int, dict] = {}
        self.edited_targets: set[str] = set()
        self.step_cats: dict[int, set[str]] = {}
        self.restores: list[tuple[int, str, str | None]] = []
        self.hot: set[str] = set()
        self.hot_ranges: dict[str, tuple[int, int] | None] = {}
        self.folded_reads: dict[str, int] = {}
        self.ready = False
        self.cpt = self.pc.chars_per_token
        self.prompt_tokens = 0
        self.reported_stale: set[str] = set()
        self.stats = dict.fromkeys(
            [
                "keyframes",
                "phase_folds",
                "watermark_folds",
                "hidden_turns",
                "hidden_chars",
                "restores",
                "checkpoints",
                "deltas",
                "skipped_shallow",
                "rereads_after_fold",
            ],
            0,
        )

    # ---- agent loop hooks ----

    def run(self, task: str = "", **kwargs) -> dict:
        self._reset()
        return super().run(task, **kwargs)

    def add_messages(self, *messages: dict) -> list[dict]:
        first = messages[0] if messages else {}
        if (
            self.pc.enabled
            and not self.messages
            and first.get("role") == "system"
            and isinstance(first.get("content"), str)
        ):
            messages = ({**messages[0], "content": messages[0]["content"] + "\n\n" + T0}, *messages[1:])
        return super().add_messages(*messages)

    def context_messages(self) -> list[dict]:
        """The messages that are sent to the model: all messages except the removed turns."""
        return [m for m in self.messages if not _hidden(m)]

    def query(self) -> dict:
        if not self.pc.enabled:
            return super().query()
        full, visible = self.messages, self.context_messages()
        n_sent = len(visible)
        self._mark_breakpoints(visible)
        self.messages = visible
        try:
            message = super().query()
        finally:
            self.messages = full
            full.extend(visible[n_sent:])
        self.step_msgs[self.n_calls] = message
        response = message.get("extra", {}).get("response")
        usage = (response.get("usage") or {}) if isinstance(response, dict) else {}
        if prompt_tokens := usage.get("prompt_tokens"):
            self.prompt_tokens = prompt_tokens
            self.cpt = min(6.0, max(1.5, sum(_chars(m) for m in visible[:n_sent]) / prompt_tokens))
            if self.fold_log and self.fold_log[-1]["after"] is None:
                self.fold_log[-1]["after"] = prompt_tokens
        return message

    def execute_actions(self, message: dict) -> list[dict]:
        if not self.pc.enabled:
            return super().execute_actions(message)
        if not self.ready:
            self.files.setup()
            self.ready = True
        step = self.n_calls
        self._read_notes(message)
        recs, outputs = [], []
        for action in message.get("extra", {}).get("actions", []):
            output, rec = self._execute(action, step, message)
            outputs.append(output)
            recs.append(rec)
        observations = self.model.format_observation_messages(message, outputs, self.get_template_vars())
        for rec, msg in zip(recs, observations):
            if isinstance(msg.get("content"), str):
                rec.obs_msg, rec.base_content = msg, self._inject(msg["content"], rec)
                rec.render()
        added = self.add_messages(*observations)
        self._after_step(step, recs)
        return added

    def serialize(self, *extra_dicts) -> dict:
        pkc = self.stats | {
            "e1_step": self.e1_step,
            "g1_step": self.g1_step,
            "keyframe_steps": [k.step for k in self.keyframes],
            "keyframe_reasons": self.kf_reasons,
            "fold_log": self.fold_log,
            "hot_files": sorted(self.hot),
        }
        return super().serialize({"info": {"pkc": pkc}}, *extra_dicts)

    # ---- execution, ids, ctx ----

    def _run(self, command: str) -> str:
        return self.env.execute({"command": command}).get("output", "")

    def _execute(self, action: dict, step: int, message: dict) -> tuple[dict, Record]:
        cmd, d = action.get("command", ""), self.pc.ctx.dir
        uses_ctx = bool(CTX_USE.search(cmd))
        if uses_ctx:
            self._materialize(cmd)
        output = self.env.execute({**action, "command": ctx.wrap(cmd, d)} if uses_ctx else action)
        rec = self.ledger.add(step, action, output, self.files.root)
        rec.act_msg = message
        if uses_ctx:
            self._note_restores(self._run(ctx.read_log(d)), step)
        rec.edit_files = self.files.snapshot(step, rec.oid, self.ledger.n_edits + 1 if rec.rc == 0 else None)
        if rec.edit_files and rec.rc == 0:
            rec.eid = self.ledger.next_eid()
        rec.versions = {p: self.files.sha(p) for p in rec.read_paths}
        prev = next((r for r in reversed(self.ledger.records[:-1]) if r.hidden_at and r.eid and r.cmd == cmd), None)
        if prev is not None:
            rec.appendix.append(repeated_edit(prev.eid))
        return output, rec

    @staticmethod
    def _inject(content: str, rec: Record) -> str:
        if content.lstrip().startswith("{"):
            i = content.index("{") + 1
            return content[:i] + f' "id": "{rec.id}",' + (f' "edit_id": "e{rec.eid}",' if rec.eid else "") + content[i:]
        return f"#{rec.id}" + (f" #e{rec.eid}" if rec.eid else "") + "\n" + content

    def _path(self, p: str) -> str:
        return norm_path(p, None, self.files.root)

    def _abs(self, p: str) -> str:
        return f"{self.files.root}/{p}" if self.files.root else p

    def _materialize(self, cmd: str) -> None:
        args, dynamic = ctx.refs(cmd)
        records = self.ledger.records
        files = {"bin/ctx": ctx.SCRIPT.replace("@DIR@", shlex.quote(self.pc.ctx.dir))}
        ids = [a for a in args if re.fullmatch(r"[oe]\d+", a)]
        syms = [a for a in args if "::" in a]
        if dynamic:
            ids += [r.id for r in records if r.hidden_at] + [f"e{r.eid}" for r in records if r.hidden_at and r.eid]
            syms += [f"{p}::{n}" for i in self.checkpoint for p, n in i.symbols]
        for ref in dict.fromkeys(ids):
            if (text := self.ledger.restore_text(ref)) is not None:
                files[f"store/{ref}"], files[f"store/{ref}.h"] = text, self._restore_header(ref)
        entries = []
        for arg in dict.fromkeys(syms):
            path, _, name = arg.partition("::")
            p = self._path(path)
            if (content := self.files.read([p]).get(p)) is not None and (s := find(index(p, content), name)):
                entries.append((arg, self._abs(p), s.start, s.end))
        if dynamic or re.search(r"\bctx\s+list\b", cmd):
            files["list"] = self._ctx_list()
        for command in ctx.write_commands(self.pc.ctx.dir, files, entries):
            self._run(command)

    def _restore_header(self, ref: str) -> str:
        rec = self.ledger.get(ref)
        if ref.startswith("e"):
            return f"[{ref}: original command of {rec.id}, step {rec.step}]\n"
        changed = [p for p, v in rec.versions.items() if self.files.sha(p) != v]
        state = f"NOTE: {', '.join(changed)} changed since then" if changed else "file versions unchanged"
        return f"[{ref} from step {rec.step}; {state}]\n"

    def _ctx_list(self) -> str:
        out = [
            f"{r.id}{f' e{r.eid}' if r.eid else ''}  step {r.step}  {r.category}  {short(r.cmd, 80)}"
            for r in self.ledger.records
            if r.hidden_at
        ]
        return "\n".join(out) + "\n" if out else "(nothing folded)\n"

    def _note_restores(self, log: str, step: int) -> None:
        for line in log.splitlines():
            words = line.split()
            if len(words) < 2 or words[0] != "show":
                continue
            arg, sym = words[1], None
            if "::" in arg:
                path, sym = arg.split("::", 1)[0], arg
            elif re.fullmatch(r".+:\d+-\d+", arg):
                path = arg.rsplit(":", 1)[0]
            else:
                rec = self.ledger.get(arg)
                path = rec.read_paths[0] if rec and rec.read_paths else ""
            self.restores.append((step, self._path(path) if path else "", sym))
            self.stats["restores"] += 1

    # ---- notes ----

    def _note_ctx(self) -> NoteContext:
        return NoteContext(
            exists=self.files.exists if self.files.enabled else (lambda p: True),
            symbols=lambda p: index(p, self.files.read([p]).get(p, "")),
            record=self.ledger.get,
            latest_with=self.ledger.latest_with,
            resolve=self._path,
        )

    def _read_notes(self, message: dict) -> None:
        text = _text(message) + "\n" + str(message.get("reasoning_content") or "")
        for kind, budget in (("checkpoint", self.pc.checkpoint_max_tokens), ("delta", self.pc.delta_max_tokens)):
            if (block := parse_block(text, kind)) is None:
                if kind in self.awaiting:
                    self.unanswered[kind] += 1
                continue
            self.unanswered[kind] = 0
            items, problems = validate(kind, block, self._note_ctx(), int(budget * self.cpt))
            if 2 * len(problems) > len(parse_items(block, FIELDS[kind])):
                items = [i for i in items if i.field not in ("KEEP", "REJECTED", "RULED_OUT")]
            if kind == "checkpoint":
                self.checkpoint = items
            else:
                self.deltas.append((self.n_calls, items))
            self.stats[kind + "s"] += 1
            if problems:
                self.feedback.append(t4(kind, problems))
        self.awaiting = set()

    def _cp(self, field: str) -> list[Item]:
        return [i for i in self.checkpoint if i.field == field]

    # ---- phase events and scheduling ----

    def _after_step(self, step: int, recs: list[Record]) -> None:
        self.step_cats[step] = {r.category for r in recs}
        host = next((r for r in reversed(recs) if r.obs_msg is not None), None)
        if host is None:
            return
        self._update_hot(step, recs)
        host.appendix += self.feedback
        self.feedback = []
        targets = self.files.targets()
        if self.e1_step is None and any(self._is_e1(r, targets) for r in recs):
            self.e1_step = step
            self.events.append((step, "first edit"))
            if self.pc.checkpoint_at == "e1" and self._may_ask("checkpoint"):
                host.appendix.append(T1)
                self.awaiting.add("checkpoint")
        for reason, mark in self._triggers(recs, step, targets):
            self.marks.append((reason, mark))
            self.events.append((step, EVENT_LABELS[reason]))
        folded = False
        if self.pending and step > self.pending[1]:
            folded = self._fold(step, host, self.pending[0], self.pending[2])
            self.pending = None
        elif due := self._due(step, recs):
            reason, size = due
            tight = self._plan(step, reason, size)
            if tight is None and reason == "watermark" and self.marks and size >= self.pc.min_prompt_tokens:
                reason = self.marks[-1][0]
                tight = self._plan(step, reason, size)
            if tight is None:
                self.stats["skipped_shallow"] += 1
                self.last_try = step
            else:
                self.pending = (reason, step, tight)
                self._request_notes(host)
        if not folded and self.pending is None and self.keyframes and (stale := self._reverted_in_ws(recs)):
            host.appendix.append(reverted(stale, self.keyframes[-1].n))
        host.render()

    def _may_ask(self, kind: str) -> bool:
        return self.unanswered[kind] < self.pc.max_unanswered_requests

    def _request_notes(self, host: Record) -> None:
        ask_cp = self.pc.checkpoint_at == "first_fold" and not self.checkpoint and self._may_ask("checkpoint")
        if ask_cp:
            host.appendix.append(T2_FIRST)
            self.awaiting |= {"checkpoint", "delta"}
        elif self._may_ask("delta"):
            host.appendix.append(T2)
            self.awaiting.add("delta")

    def _size(self) -> float:
        """Tokens the next call will send: the last API count or the character estimate, whichever is larger."""
        return max(self.prompt_tokens, sum(map(_chars, self.context_messages())) / self.cpt)

    def _due(self, step: int, recs: list[Record]) -> tuple[str, float] | None:
        """(reason, prompt size) for a fold that should be planned now, if any."""
        if self.pending is not None or step < self.pc.min_steps:
            return None
        if step - max(self.last_kf_step, self.last_try) < self.pc.min_gap:
            return None
        self.marks = [m for m in self.marks if m[1] > self.fold_hi]
        size = self._size()
        if 0 < self.pc.high_water <= size:
            reason = "watermark"
        elif self.marks and size >= self.pc.min_prompt_tokens:
            reason = self.marks[-1][0]
        else:
            self.due_since = None
            return None
        if self.due_since is None:
            self.due_since = step
        if self.pc.defer_edit_burst and not self._calm(recs) and step - self.due_since < self.pc.max_defer:
            return None
        self.due_since = None
        return reason, size

    def _plan(self, step: int, reason: str, size: float) -> bool | None:
        """Whether the fold must keep only `keep_turns` turns to be deep enough; None if no fold is deep enough."""
        for tight in (False, True) if reason == "watermark" else (False,):
            hi = self._fold_hi(reason, tight)
            if hi <= self.fold_hi:
                continue
            if self._depth_ok(reason, size, self._estimate(hi, size, self._build(step, reason, hi, commit=False))):
                return tight
        return None

    def _depth_ok(self, reason: str, size: float, after: float) -> bool:
        if reason == "watermark":
            return after <= self.pc.low_water or after <= 0.5 * size
        return size - after >= self.pc.min_fold_fraction * size

    @staticmethod
    def _calm(recs: list[Record]) -> bool:
        return any(r.category == "test" for r in recs) or not any(r.edit_files for r in recs)

    def _reverted_in_ws(self, recs: list[Record]) -> list[str]:
        shown = {s.split("::", 1)[0] for s in self.keyframes[-1].ws.symbols}
        return [p for r in recs if REVERT.search(r.cmd) for p in r.edit_files if p in shown]

    def _is_e1(self, rec: Record, targets: set[str]) -> bool:
        changed = [p for p in rec.edit_files if p in targets]
        contents = self.files.read(changed)
        return bool(changed) and all(syntax_ok(p, contents.get(p, "")) for p in changed)

    def _triggers(self, recs: list[Record], step: int, targets: set[str]) -> list[tuple[str, int]]:
        """(reason, last stale step) for every phase event of this step."""
        out, enabled = [], set(self.pc.triggers)
        for rec in recs:
            new = [p for p in rec.edit_files if p in targets and p not in self.edited_targets]
            self.edited_targets.update(p for p in rec.edit_files if p in targets)
            if self.e1_step is None:
                continue
            if "revert" in enabled and REVERT.search(rec.cmd):
                out.append(("revert", step))
            if (
                "first_green" in enabled
                and self.g1_step is None
                and rec.category == "test"
                and is_green(rec.cmd, rec.rc, rec.raw)
            ):
                self.g1_step = step
                out.append(("first_green", step))
            explored = sum(bool(self.step_cats.get(s, set()) & {"read", "search"}) for s in range(step - 3, step))
            if "new_target" in enabled and new and step != self.e1_step and explored >= 2:
                old = [e.step for e in self.files.edits if e.path not in new and e.step < step]
                if old:
                    out.append(("new_target", max(old)))
        return out

    def _update_hot(self, step: int, recs: list[Record]) -> None:
        hc = self.pc.hot
        for path in {p for _, p, _ in self.restores if p}:
            recent = [s for s, p, _ in self.restores if p == path and step - s < hc.restore_window]
            if len(recent) >= hc.restore_count:
                self.hot.add(path)
        for rec in recs:
            for seg in rec.segments:
                if not seg.read or (p := seg.read[0]) not in self.folded_reads:
                    continue
                if step - self.folded_reads[p] <= hc.reread_window:
                    a, b = seg.read[1] or 1, seg.read[2]
                    self.hot.add(p)
                    self.hot_ranges[p] = (a, b) if b else (a, a + self.pc.ws_symbol_max_lines) if a > 1 else None
                    self.stats["rereads_after_fold"] += 1

    # ---- turns and folding ----

    def _turns(self) -> list[tuple[int, int, int]]:
        """(step, first message index, end index) of every step: its assistant message and all observations after it."""
        index_of = {id(m): i for i, m in enumerate(self.messages)}
        starts = sorted((index_of[id(m)], s) for s, m in self.step_msgs.items() if id(m) in index_of)
        ends = [i for i, _ in starts[1:]] + [len(self.messages)]
        return [(s, i, end) for (i, s), end in zip(starts, ends)]

    def _turn_chars(self, a: int, b: int) -> int:
        return sum(_chars(m) for m in self.messages[a:b] if not _hidden(m))

    def _recent_hi(self, budget_chars: int) -> int:
        """Last step to remove so that the kept turns fit the budget, keeping at least `keep_turns` turns."""
        kept = 0
        for k, (s, a, b) in enumerate(reversed([t for t in self._turns() if t[0] > self.fold_hi])):
            c = self._turn_chars(a, b)
            if k >= max(1, self.pc.keep_turns) and kept + c > budget_chars:
                return s
            kept += c
        return self.fold_hi

    def _fold_hi(self, reason: str, tight: bool) -> int:
        hi = self._recent_hi(0 if tight else int(self.pc.recent_budget * self.cpt))
        if reason != "watermark" and (marks := [s for _, s in self.marks if s > self.fold_hi]):
            hi = min(hi, max(marks))
        return hi

    def _prev_host_visible(self, hi: int) -> bool:
        return bool(self.keyframes) and self.hosts[-1].step > hi and self.keyframes[-1].superseded is None

    def _estimate(self, hi: int, size: float, kf: Keyframe) -> float:
        """Prompt tokens after removing the turns up to `hi` and adding `kf`."""
        removed = sum(self._turn_chars(a, b) for s, a, b in self._turns() if self.fold_hi < s <= hi)
        if self._prev_host_visible(hi):
            removed += len(str(self.keyframes[-1])) - 40
        return size - (removed - len(str(kf))) / self.cpt

    def _fold(self, step: int, host: Record, reason: str, tight: bool) -> bool:
        hi = self._fold_hi(reason, tight)
        if hi <= self.fold_hi:
            self.last_try = step
            return False
        before = self._size()
        kf = self._build(step, reason, hi, commit=True)
        after_est = self._estimate(hi, before, kf)
        for s, a, b in self._turns():
            if self.fold_hi < s <= hi:
                self.stats["hidden_turns"] += 1
                self.stats["hidden_chars"] += self._turn_chars(a, b)
                for m in self.messages[a:b]:
                    m.setdefault("extra", {})["pkc_hidden"] = step
        for rec in self.ledger.records:
            if rec.step <= hi and rec.hidden_at is None:
                rec.hidden_at = step
                self.folded_reads.update(dict.fromkeys(rec.read_paths, step))
        if self._prev_host_visible(hi):
            _save_original(self.hosts[-1].obs_msg)
            self.keyframes[-1].supersede(kf.n)
            self.hosts[-1].render()
        host.appendix.append(kf)
        self.keyframes.append(kf)
        self.hosts.append(host)
        self.events.append((step, f"K{kf.n}"))
        self.marks = [m for m in self.marks if m[1] > hi]
        self.last_kf_step, self.fold_hi = step, hi
        self.kf_reasons.append(reason)
        self.stats["keyframes"] += 1
        self.stats["watermark_folds" if reason == "watermark" else "phase_folds"] += 1
        self.fold_log.append(
            {
                "step": step,
                "reason": reason,
                "hi": hi,
                "before": round(before),
                "after_est": round(after_est),
                "after": None,
            }
        )
        self.prompt_tokens = 0
        return True

    # ---- keyframe content ----

    def _build(self, step: int, reason: str, hi: int, commit: bool) -> Keyframe:
        n = len(self.keyframes) + 1
        return Keyframe(n, step, self._frozen(n, step, reason, hi, commit), self._working_set(step, hi))

    def _recent_words(self, hi: int) -> set[str]:
        texts = []
        for s, m in self.step_msgs.items():
            if s > hi:
                texts += [_text(m), str(m.get("reasoning_content") or "")]
                texts += [a.get("command", "") for a in m.get("extra", {}).get("actions", [])]
        return set(WORD.findall(" ".join(texts)))

    def _working_set(self, step: int, hi: int) -> WorkingSet:
        last_edit = {e.path: e.step for e in self.files.edits}
        targets = sorted(self.files.targets(), key=lambda p: (-last_edit.get(p, 0), p))
        cp_targets = [(p, n) for i in self._cp("TARGETS") for p, n in i.symbols]
        cp_exemplars = [(p, n) for i in self._cp("EXEMPLARS") for p, n in i.symbols]
        hot_syms = [s.split("::", 1) for _, _, s in self.restores[-10:] if s]
        hot_syms = [(self._path(p), n) for p, n in hot_syms if self._path(p) in self.hot]
        hidden_reads = [s.read for r in self.ledger.records if r.step <= hi for s in r.segments if s.read]
        fallback = list(dict.fromkeys(p for p, _, _ in reversed(hidden_reads) if SRC_EXT.search(p)))[:2]
        paths = targets + [p for p, _ in cp_targets + cp_exemplars + hot_syms] + list(self.hot_ranges) + fallback
        contents = self.files.read(list(dict.fromkeys(paths)))
        idx = {p: index(p, t) for p, t in contents.items()}
        n_lines = {p: len(t.splitlines()) for p, t in contents.items()}
        words = self._recent_words(hi)

        def lines(prio: int, p: str, a: int, b: int) -> Candidate:
            a, b = max(1, a), min(n_lines.get(p, b), b)
            return Candidate(prio, p, Symbol(f"<lines {a}-{b}>", a, b))

        cands = []
        for p in targets:
            for a, b in self.files.changed_lines(p):
                inside = [s for s in idx.get(p, []) if s.start <= b and a <= s.end]
                k = self.pc.ws_hunk_context
                cands += [Candidate(1, p, s) for s in inside] or [lines(1, p, a - k, b + k)]
        for prio, pairs in ((2, cp_targets), (3, cp_exemplars), (5, hot_syms)):
            cands += [Candidate(prio, p, s) for p, n in pairs if (s := find(idx.get(p, []), n))]
        for p, rng in self.hot_ranges.items():
            if p not in contents:
                continue
            if rng is not None:
                cands.append(lines(2, p, *rng))
            elif n_lines[p] <= self.pc.ws_symbol_max_lines:
                cands.append(lines(2, p, 1, n_lines[p]))
            else:
                cands += [Candidate(2, p, s) for s in idx.get(p, []) if s.name.split(".")[-1] in words]
        cands += [Candidate(4, p, s) for p in targets for s in idx.get(p, []) if s.name.split(".")[-1] in words]
        if not cands:
            for p in fallback:
                rng = next(((a or 1, b) for q, a, b in reversed(hidden_reads) if q == p), (1, None))
                if p in contents:
                    cands.append(lines(6, p, rng[0], rng[1] or rng[0] + self.pc.ws_symbol_max_lines - 1))
        visible = [
            (r.id, strip_numbers(r.raw if len(r.raw) < 10000 else r.raw[:5000] + "\n" + r.raw[-5000:]))
            for r in self.ledger.records
            if r.step > hi and r.obs_msg is not None
        ]
        visible += [(f"e{r.eid}", r.cmd) for r in self.ledger.records if r.eid and r.step > hi]
        return build_working_set(
            cands,
            contents,
            visible,
            budget_chars=int(self.pc.ws_budget * self.cpt),
            max_lines=self.pc.ws_symbol_max_lines,
            step=step,
            sha=self.files.cur,
        )

    def _locations(self, seg: list[Record]) -> list[str]:
        targets = self.files.targets() | {p for i in self._cp("TARGETS") for p in i.paths}
        exemplars = {p for i in self._cp("EXEMPLARS") for p in i.paths}
        ranked = []
        for p in dict.fromkeys(p for r in seg for p in [*r.read_paths, *r.edit_files]):
            eids = sorted({e.eid for e in self.files.edits if e.path == p and e.eid})
            reads = [r for r in seg if p in r.read_paths]
            if p in self.files.content:
                defs = [(s.name, s.start, s.end) for s in index(p, self.files.content[p]) if "." not in s.name]
            else:
                last = next((r for r in reversed(reads) if len(r.read_paths) == 1), None)
                start = next((s.read[1] for s in last.segments if s.read), 1) if last else 1
                defs = outline(last.raw, start or 1, self.pc.outline_max_defs) if last else []
            shown = ", ".join(f"{name} L{a}-{b}" for name, a, b in defs[:4])
            if eids:
                rank, status = 0, f"edited ({', '.join(f'e{e}' for e in eids[-4:])})"
            elif p in targets:
                rank, status = 1, "target (CP)"
            elif p in exemplars:
                rank, status = 2, "exemplar (CP)"
            else:
                rank = 3
                status = f"read {', '.join(r.id for r in reads[-3:])}, not edited" if reads else "touched"
            last_step = max(r.step for r in seg if p in r.read_paths or p in r.edit_files)
            ranked.append((rank, -last_step, f"{p}   {shown}   {status}".replace("      ", "   ")))
        out = [line for _, _, line in sorted(ranked, key=lambda x: x[:2])]
        for r in [r for r in seg if r.category == "search"][-6:]:
            total, hits = search_summary(r.raw, self.pc.search_hits_per_stub)
            if total:
                out.append(f"{short(r.cmd, 60)} -> {hits} ({r.id})")
        return out

    def _edits(self, step: int) -> list[str]:
        out = []
        for e in self.files.edits:
            if e.step <= step:
                syms = index(e.path, self.files.content.get(e.path, ""))
                at = next((s.name for s in reversed(syms) if s.start <= e.first_line <= s.end), None)
                label = f"e{e.eid}" if e.eid else f"(o{e.oid}, failed command)"
                out.append(
                    f"{label} {e.path}  +{e.added}/-{e.removed}"
                    + (" (new file)" if e.new_file else "")
                    + (f"  hunk @ {at} L{e.first_line}" if at else f"  hunk @ L{e.first_line}")
                )
        return _tail_fit(out, int(600 * self.cpt), "edits") + (
            [f"diff vs HEAD: {self.files.shortstat()}"] if out else []
        )

    def _verification(self) -> list[str]:
        out, tests = [], {}
        for r in self.ledger.records:
            if r.category == "test":
                tests.setdefault(test_key(r.cmd), []).append(r)
        for key, rs in list(tests.items())[-6:]:
            last, summary = rs[-1], test_summary(rs[-1].raw, rs[-1].rc)
            prev = next((x for x in reversed(rs[:-1]) if test_summary(x.raw, x.rc) != summary), None)
            was = f"   was {test_summary(prev.raw, prev.rc)} at {prev.id}" if prev else ""
            out.append(f"{short(key, 70)}   {summary} ({last.id}, step {last.step}){was}")
        return out + [f"CP.DONE_WHEN: {i.text}" for i in self._cp("DONE_WHEN")]

    @staticmethod
    def _item(prefix: str, i: Item) -> str:
        refs = "" if all(r in i.text for r in i.refs) else f" ({', '.join(i.refs)})"
        return f"{prefix}{i.field:<10} {i.text}{refs}"

    def _notes(self) -> list[str]:
        out = [self._item("CP.", i) for i in self.checkpoint]
        if self.deltas:
            out += [self._item("", i) for i in self.deltas[-1][1]]
        seen = {i.text for i in self.deltas[-1][1]} if self.deltas else set()
        for _, items in self.deltas[:-1]:
            for i in items:
                if i.field == "REJECTED" and i.text not in seen:
                    seen.add(i.text)
                    out.append(self._item("earlier ", i))
        return out or ["no model notes"]

    def _kept_outputs(self, hi: int) -> list[str]:
        refs = [r for i in self.deltas[-1][1] if i.field == "KEEP" for r in i.refs] if self.deltas else []
        recs = [rec for ref in dict.fromkeys(refs) if (rec := self.ledger.get(ref)) and rec.step <= hi]
        out = []
        for rec in recs:
            per = int(self.pc.keep_budget * self.cpt) // len(recs)
            text = rec.raw
            if len(text) > per:
                text = text[: per // 2] + f"\n... (middle omitted; ctx show {rec.id})\n" + text[-per // 2 :]
            out.append(f"--- {rec.id} (step {rec.step}) {short(rec.cmd, 60)}\n{text.rstrip()}")
        return out

    def _frozen(self, n: int, step: int, reason: str, hi: int, commit: bool) -> str:
        c = self.cpt
        seg = [r for r in self.ledger.records if r.step <= hi]
        history = [f"Steps 1-{hi} were removed. Restore any output exactly with ctx show <id>; ctx list lists them."]
        history += history_lines(seg, self.events, hi, int(self.pc.history_budget * c))
        sections = [
            ("History index", history, int(self.pc.history_budget * c) + 200),
            ("Files and locations", self._locations(seg), int(1000 * c)),
            ("Edits", self._edits(step), int(600 * c) + 200),
            ("Verification", self._verification(), int(400 * c)),
            ("Your notes (checkpoint, latest delta, earlier rejections)", self._notes(), int(900 * c)),
            ("Kept outputs (KEEP in your delta)", self._kept_outputs(hi), int(self.pc.keep_budget * c) + 400),
            ("Errata", self._errata(commit), int(200 * c)),
        ]
        return frozen_text(n, step, reason, hi, sections)

    def _errata(self, commit: bool) -> list[str]:
        out = []
        for item in self.checkpoint + [i for _, d in self.deltas for i in d]:
            for ref in item.refs:
                rec = self.ledger.get(ref)
                for p, v in rec.versions.items() if rec else []:
                    if self.files.sha(p) != v and (key := f"{ref}:{p}:{item.text}") not in self.reported_stale:
                        out.append(f'stale: {item.field} "{item.text[:50]}" cites {ref}; {p} changed since')
                        if commit:
                            self.reported_stale.add(key)
        return out

    def _mark_breakpoints(self, visible: list[dict]) -> None:
        for m in self.messages:
            m.get("extra", {}).pop("cache_breakpoint", None)
        index_of = {id(m): i for i, m in enumerate(visible)}
        marks = visible[1:2] + [visible[i - 1] for h in self.hosts[-2:] if (i := index_of.get(id(h.obs_msg), 0)) > 0]
        for m in marks:
            m.setdefault("extra", {})["cache_breakpoint"] = True
