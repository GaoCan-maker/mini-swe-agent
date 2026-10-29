"""Phase-Keyframe Compaction (PKC): append-only history, folded into stubs plus a keyframe at a few phase events."""

import copy
import json
import re
import shlex

from minisweagent import Environment, Model
from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.agents.pkc import ctx
from minisweagent.agents.pkc.commands import CTX_USE, REVERT, is_green, norm_path, test_key, test_summary
from minisweagent.agents.pkc.config import PKCConfig
from minisweagent.agents.pkc.files import FileTracker
from minisweagent.agents.pkc.fold import FoldPlan, Labels, Planner, search_summary, short
from minisweagent.agents.pkc.keyframe import FOOTER, Candidate, Keyframe, WorkingSet, build_working_set, frozen_text
from minisweagent.agents.pkc.ledger import Ledger, Record
from minisweagent.agents.pkc.notes import FIELDS, Item, NoteContext, parse_block, parse_items, validate
from minisweagent.agents.pkc.symbols import Symbol, find, index, strip_numbers, syntax_ok
from minisweagent.agents.pkc.templates import T0, T1, T2, repeated_edit, reverted, t4

WORD = re.compile(r"[A-Za-z_]\w{3,}")


class PKCAgentConfig(AgentConfig):
    pkc: PKCConfig = PKCConfig()


def _text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content or ""


def _chars(msg: dict) -> int:
    return len(_text(msg)) + sum(len(tc.get("function", {}).get("arguments", "")) for tc in msg.get("tool_calls") or [])


def _save_original(msg: dict) -> None:
    msg.setdefault("extra", {}).setdefault("pkc_original_content", msg.get("content"))


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
        self.pending: tuple[str, int] | None = None
        self.awaiting: str | None = None
        self.unanswered = {"checkpoint": 0, "delta": 0}
        self.e1_step: int | None = None
        self.g1_step: int | None = None
        self.last_kf_step = 0
        self.edited_targets: set[str] = set()
        self.step_cats: dict[int, set[str]] = {}
        self.restores: list[tuple[int, str, str | None]] = []
        self.hot: set[str] = set()
        self.folded_reads: dict[str, int] = {}
        self.fold_disabled = False
        self.ready = False
        self.cpt = self.pc.chars_per_token
        self.prompt_tokens = 0
        self.reported_stale: set[str] = set()
        self.stats = dict.fromkeys(
            ["keyframes", "folded_outputs", "folded_edits", "saved_chars", "restores", "checkpoints", "deltas"], 0
        ) | {"skipped_break_even": 0}

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

    def query(self) -> dict:
        if self.pc.enabled:
            self._mark_breakpoints()
        message = super().query()
        response = message.get("extra", {}).get("response")
        usage = (response.get("usage") or {}) if isinstance(response, dict) else {}
        if prompt_tokens := usage.get("prompt_tokens"):
            self.prompt_tokens = prompt_tokens
            self.cpt = min(6.0, max(1.5, sum(_chars(m) for m in self.messages[:-1]) / prompt_tokens))
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
        self._after_step(step, recs)
        return self.add_messages(*observations)

    def serialize(self, *extra_dicts) -> dict:
        pkc = self.stats | {
            "e1_step": self.e1_step,
            "g1_step": self.g1_step,
            "keyframe_steps": [k.step for k in self.keyframes],
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
        if prev := next((r for r in self.ledger.records[:-1] if r.cmd_stub and r.cmd == cmd), None):
            rec.appendix.append(repeated_edit(prev.eid))
        return output, rec

    @staticmethod
    def _inject(content: str, rec: Record) -> str:
        if content.lstrip().startswith("{"):
            i = content.index("{") + 1
            return content[:i] + f' "id": "{rec.id}",' + (f' "edit_id": "e{rec.eid}",' if rec.eid else "") + content[i:]
        return f"#{rec.id}" + (f" #e{rec.eid}" if rec.eid else "") + "\n" + content

    def _render_output(self, rec: Record, text: str) -> str:
        msg = self.model.format_observation_messages(
            {"extra": {"actions": [rec.action]}}, [{**rec.output, "output": text}], self.get_template_vars()
        )[0]
        return self._inject(msg["content"], rec)

    def _path(self, p: str) -> str:
        return norm_path(p, None, self.files.root)

    def _abs(self, p: str) -> str:
        return f"{self.files.root}/{p}" if self.files.root else p

    def _materialize(self, cmd: str) -> None:
        args, dynamic = ctx.refs(cmd)
        records = self.ledger.records
        files = {"bin/ctx": ctx.SCRIPT.replace("@DIR@", shlex.quote(self.pc.ctx.dir))}
        ids = [a for a in args if re.fullmatch(r"[oe]\d+(?:\.\d+)?", a)]
        syms = [a for a in args if "::" in a]
        if dynamic:
            ids += [r.id for r in records if r.stub is not None] + [f"e{r.eid}" for r in records if r.cmd_stub]
            ids += [p.pid for r in records for p in r.parts or []]
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
        out = [f"{r.id}  {r.category}  {short(r.cmd, 80)}" for r in self.ledger.records if r.stub is not None]
        out += [f"e{r.eid}  edit  {short(r.cmd, 80)}" for r in self.ledger.records if r.cmd_stub]
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
                if self.awaiting == kind:
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
        self.awaiting = None

    def _cp(self, field: str) -> list[Item]:
        return [i for i in self.checkpoint if i.field == field]

    # ---- phase events ----

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
            if self.pc.e1_checkpoint and self.unanswered["checkpoint"] < self.pc.max_unanswered_requests:
                host.appendix.append(T1)
                self.awaiting = "checkpoint"
        triggers = self._triggers(recs, step, targets)
        if self.pending and step > self.pending[1]:
            self._fold(step, host, self.pending[0])
            self.pending = None
        elif triggers and self._throttle(step):
            plan, ws, frozen = self._prepare(step, triggers[0])
            if self._break_even(plan, ws, frozen):
                self.pending = (triggers[0], step)
                if self.unanswered["delta"] < self.pc.max_unanswered_requests:
                    host.appendix.append(T2)
                    self.awaiting = "delta"
            else:
                self.stats["skipped_break_even"] += 1
                if self.keyframes and (stale := self._reverted_in_ws(recs)):
                    host.appendix.append(reverted(stale, self.keyframes[-1].n))
        host.render()

    def _reverted_in_ws(self, recs: list[Record]) -> list[str]:
        shown = {s.split("::", 1)[0] for s in self.keyframes[-1].ws.symbols}
        return [p for r in recs if REVERT.search(r.cmd) for p in r.edit_files if p in shown]

    def _is_e1(self, rec: Record, targets: set[str]) -> bool:
        changed = [p for p in rec.edit_files if p in targets]
        contents = self.files.read(changed)
        return bool(changed) and all(syntax_ok(p, contents.get(p, "")) for p in changed)

    def _triggers(self, recs: list[Record], step: int, targets: set[str]) -> list[str]:
        out, enabled = [], set(self.pc.triggers)
        for rec in recs:
            new = [p for p in rec.edit_files if p in targets and p not in self.edited_targets]
            self.edited_targets.update(p for p in rec.edit_files if p in targets)
            if self.e1_step is None:
                continue
            if "revert" in enabled and REVERT.search(rec.cmd):
                out.append("revert")
            if (
                "first_green" in enabled
                and self.g1_step is None
                and rec.category == "test"
                and is_green(rec.cmd, rec.rc, rec.raw)
            ):
                self.g1_step = step
                out.append("first_green")
            explored = sum(bool(self.step_cats.get(s, set()) & {"read", "search"}) for s in range(step - 3, step))
            if "new_target" in enabled and new and step != self.e1_step and explored >= 2:
                out.append("new_target")
        return out

    def _throttle(self, step: int) -> bool:
        return (
            not self.fold_disabled
            and self.pending is None
            and step >= self.pc.min_steps
            and step - self.last_kf_step >= self.pc.min_gap
        )

    def _update_hot(self, step: int, recs: list[Record]) -> None:
        hc = self.pc.hot
        for _, path, _ in self.restores:
            recent = [s for s, p, _ in self.restores if p == path and step - s < hc.restore_window]
            if path and len(recent) >= hc.restore_count:
                self.hot.add(path)
        for rec in recs:
            self.hot.update(
                p for p in rec.read_paths if p in self.folded_reads and step - self.folded_reads[p] <= hc.reread_window
            )
        if len(self.restores) > hc.disable_after:
            self.fold_disabled = True

    def _pff_high(self) -> bool:
        lo = self.last_kf_step
        return bool(self.keyframes) and sum(lo < s <= lo + 10 for s, _, _ in self.restores) > self.pc.hot.pff_max

    # ---- folding ----

    def _labels(self) -> Labels:
        ruled = {}
        for k, item in enumerate(self._cp("RULED_OUT")):
            for r in item.refs:
                ruled.setdefault(r.split(".")[0], f"CP.RULED_OUT[{k + 1}]")
        for n, (_, items) in enumerate(self.deltas):
            for k, item in enumerate(i for i in items if i.field == "REJECTED"):
                for r in item.refs:
                    ruled.setdefault(r.split(".")[0], f"delta{n + 1}.REJECTED[{k + 1}]")
        last_delta = self.deltas[-1][1] if self.deltas and self.deltas[-1][0] > self.last_kf_step else []
        exemplars = self._cp("EXEMPLARS")
        return Labels(
            targets=self.files.targets() | {p for i in self._cp("TARGETS") for p in i.paths},
            exemplars={p for i in exemplars for p in i.paths},
            has_exemplars=bool(exemplars),
            ruled_out=ruled,
            keep={r for i in last_delta if i.field == "KEEP" for r in i.refs},
            hot=set(self.hot),
            first_keyframe=not self.keyframes,
            exemplar_mode=self.pc.exemplar_mode,
        )

    def _edit_stats(self) -> dict[int, str]:
        out: dict[int, list[str]] = {}
        for e in self.files.edits:
            if e.eid:
                out.setdefault(e.eid, []).append(f"{e.path} (+{e.added}/-{e.removed})")
        return {k: ", ".join(v[:3]) + (" ..." if len(v) > 3 else "") for k, v in out.items()}

    def _prepare(self, step: int, reason: str, commit: bool = False) -> tuple[FoldPlan, WorkingSet, str]:
        labels = self._labels()
        hi = step - self.pc.keep_turns - (2 if self._pff_high() else 0)
        planner = Planner(
            self.ledger.records, labels, self.pc.search_hits_per_stub, self.pc.outline_max_defs, self._edit_stats()
        )
        plan = planner.plan(self.last_kf_step, hi, self.pc.fold_edit_commands, self.pc.edit_stub_min_chars)
        ws = self._working_set(step, plan)
        return plan, ws, self._frozen(len(self.keyframes) + 1, step, reason, plan, labels, commit)

    def _break_even(self, plan: FoldPlan, ws: WorkingSet, frozen: str) -> bool:
        min_gain = self.pc.min_gain * (2 if self._pff_high() else 1)
        R = plan.saved_chars / self.cpt
        if R < min_gain:
            return False
        if not self.pc.break_even:
            return True
        h, w = self.pc.hit_ratio, self.pc.write_ratio
        replaced = (
            len(self.keyframes[-1].ws.text) - 200 if self.keyframes and self.keyframes[-1].superseded is None else 0
        )
        W = (len(frozen) + len(ws.text) + len(FOOTER) - replaced) / self.cpt
        P = self.prompt_tokens or sum(map(_chars, self.messages)) / self.cpt
        pos = self._position(plan) / self.cpt
        return (R - W) * h * self.n_calls >= (w - h) * max(0.0, P - R - pos) + max(0.0, W) * (1 - h)

    def _position(self, plan: FoldPlan) -> int:
        index_of = {id(m): i for i, m in enumerate(self.messages)}
        offsets, total = [], 0
        for m in self.messages:
            offsets.append(total)
            total += _chars(m)
        cands = [
            offsets[index_of[id(m)]]
            for item in plan.items
            if (m := item.rec.obs_msg if item.kind == "obs" else item.rec.act_msg) is not None and id(m) in index_of
        ]
        if self.keyframes and id(host := self.hosts[-1].obs_msg) in index_of:
            ws_chars = len(self.keyframes[-1].ws.text) + len(FOOTER)
            cands.append(offsets[index_of[id(host)]] + max(0, _chars(host) - ws_chars))
        return min(cands, default=total)

    def _fold(self, step: int, host: Record, reason: str) -> None:
        plan, ws, frozen = self._prepare(step, reason, commit=True)
        n = len(self.keyframes) + 1
        for item in plan.items:
            rec = item.rec
            if item.kind == "obs":
                _save_original(rec.obs_msg)
                rec.stub, rec.folded_at = item.text, step
                rec.base_content = self._render_output(rec, item.text)
                rec.render()
                self.folded_reads.update(dict.fromkeys(rec.read_paths, step))
                self.stats["folded_outputs"] += 1
            elif self._stub_command(rec, item.text):
                self.stats["folded_edits"] += 1
        if self.keyframes:
            _save_original(self.hosts[-1].obs_msg)
            self.stats["saved_chars"] += self.keyframes[-1].supersede(n)
            self.hosts[-1].render()
        kf = Keyframe(n, step, frozen, ws)
        host.appendix.append(kf)
        self.keyframes.append(kf)
        self.hosts.append(host)
        self.last_kf_step = step
        self.stats["keyframes"] += 1
        self.stats["saved_chars"] += plan.saved_chars

    def _stub_command(self, rec: Record, stub: str) -> bool:
        msg = rec.act_msg
        call = next((tc for tc in msg.get("tool_calls") or [] if tc.get("id") == rec.action.get("tool_call_id")), None)
        if call is not None:
            msg.setdefault("extra", {}).setdefault("pkc_original_tool_calls", copy.deepcopy(msg["tool_calls"]))
            call["function"]["arguments"] = json.dumps({"command": stub})
        elif isinstance(msg.get("content"), str) and rec.cmd in msg["content"]:
            _save_original(msg)
            msg["content"] = msg["content"].replace(rec.cmd, stub, 1)
        else:
            return False
        rec.cmd_stub = stub
        return True

    # ---- keyframe content ----

    def _working_set(self, step: int, plan: FoldPlan) -> WorkingSet:
        last_edit = {e.path: e.step for e in self.files.edits}
        targets = sorted(self.files.targets(), key=lambda p: (-last_edit.get(p, 0), p))
        cp_targets = [(p, n) for i in self._cp("TARGETS") for p, n in i.symbols]
        cp_exemplars = [(p, n) for i in self._cp("EXEMPLARS") for p, n in i.symbols]
        hot_syms = [s.split("::", 1) for _, _, s in self.restores[-10:] if s]
        hot_syms = [(self._path(p), n) for p, n in hot_syms if self._path(p) in self.hot]
        contents = self.files.read(targets + [p for p, _ in cp_targets + cp_exemplars + hot_syms])
        idx = {p: index(p, t) for p, t in contents.items()}
        cands = []
        for p in targets:
            n_lines = len(contents.get(p, "").splitlines())
            for a, b in self.files.changed_lines(p):
                inside = [s for s in idx.get(p, []) if s.start <= b and a <= s.end]
                lo, hi = max(1, a - 3), min(n_lines, b + 3)
                cands += [Candidate(1, p, s) for s in inside] or [Candidate(1, p, Symbol(f"<lines {lo}-{hi}>", lo, hi))]
        for prio, pairs in ((2, cp_targets), (3, cp_exemplars), (5, hot_syms)):
            cands += [Candidate(prio, p, s) for p, n in pairs if (s := find(idx.get(p, []), n))]
        recent = " ".join(_text(m) for m in self.messages[-2 * self.pc.keep_turns :] if m.get("role") == "assistant")
        words = set(WORD.findall(recent))
        cands += [Candidate(4, p, s) for p in targets for s in idx.get(p, []) if s.name.split(".")[-1] in words]
        folded = {i.rec.oid for i in plan.items if i.kind == "obs"}
        cmd_folded = {i.rec.oid for i in plan.items if i.kind == "edit"}
        visible = [
            (r.id, strip_numbers(r.raw if len(r.raw) < 10000 else r.raw[:5000] + "\n" + r.raw[-5000:]))
            for r in self.ledger.records
            if r.stub is None and r.oid not in folded and r.obs_msg is not None
        ]
        visible += [
            (f"e{r.eid}", r.cmd) for r in self.ledger.records if r.eid and not r.cmd_stub and r.oid not in cmd_folded
        ]
        return build_working_set(
            cands,
            contents,
            visible,
            budget_chars=int(self.pc.ws_budget * self.cpt),
            max_lines=self.pc.ws_symbol_max_lines,
            step=step,
            sha=self.files.cur,
        )

    def _frozen(self, n: int, step: int, reason: str, plan: FoldPlan, labels: Labels, commit: bool) -> str:
        records = self.ledger.records
        seg = [r for r in records if plan.lo < r.step <= plan.hi]
        paths = list(dict.fromkeys(p for r in seg for p in [*r.read_paths, *r.edit_files]))
        locations = []
        for p in paths:
            eids = sorted({e.eid for e in self.files.edits if e.path == p and e.eid and plan.lo < e.step <= step})
            reads = [r.id for r in seg if p in r.read_paths]
            if p in self.files.content:
                defs = [(s.name, s.start, s.end) for s in index(p, self.files.content[p]) if "." not in s.name]
            else:
                defs = next((r.outline for r in reversed(seg) if p in r.read_paths and r.outline), [])
            shown = ", ".join(f"{name} L{a}-{b}" for name, a, b in defs[:4])
            if eids:
                status = f"edited ({', '.join(f'e{e}' for e in eids)})"
            elif p in labels.exemplars:
                status = "exemplar (CP)"
            elif p in labels.targets:
                status = "target (CP)"
            else:
                status = f"read {', '.join(reads[:3])}, not edited" if reads else "touched"
            locations.append(f"{p}   {shown}   {status}".replace("      ", "   "))
        for item in plan.items:
            if item.kind == "obs" and item.rec.category == "search":
                total, hits = search_summary(item.rec.raw, self.pc.search_hits_per_stub)
                if total:
                    locations.append(f"{short(item.rec.cmd, 60)} -> {hits} ({item.rec.id})")
        edits = []
        for e in self.files.edits:
            if self.last_kf_step < e.step <= step:
                syms = index(e.path, self.files.content.get(e.path, ""))
                at = next((s.name for s in reversed(syms) if s.start <= e.first_line <= s.end), None)
                label = f"e{e.eid}" if e.eid else f"({e.oid}, failed command)"
                edits.append(
                    f"{label} {e.path}  +{e.added}/-{e.removed}"
                    + (" (new file)" if e.new_file else "")
                    + (f"  hunk @ {at} L{e.first_line}" if at else f"  hunk @ L{e.first_line}")
                )
        if edits:
            edits.append(f"diff vs HEAD: {self.files.shortstat()}")
        verification = []
        tests: dict[str, list[Record]] = {}
        for r in records:
            if r.category == "test":
                tests.setdefault(test_key(r.cmd), []).append(r)
        for key, rs in list(tests.items())[-6:]:
            last, summary = rs[-1], test_summary(rs[-1].raw, rs[-1].rc)
            prev = next((x for x in reversed(rs[:-1]) if test_summary(x.raw, x.rc) != summary), None)
            was = f"   was {test_summary(prev.raw, prev.rc)} at {prev.id}" if prev else ""
            verification.append(f"{short(key, 70)}   {summary} ({last.id}, step {last.step}){was}")
        verification += [f"CP.DONE_WHEN: {i.text}" for i in self._cp("DONE_WHEN")]
        delta = self.deltas[-1][1] if self.deltas and self.deltas[-1][0] > self.last_kf_step else None
        hypotheses = (
            [
                f"{i.field:<10} {i.text}" + ("" if all(r in i.text for r in i.refs) else f" ({', '.join(i.refs)})")
                for i in delta
            ]
            if delta
            else ["no model delta"]
        )
        c = self.cpt
        sections = [
            (f"Files and locations (segment {plan.lo + 1}-{plan.hi})   [frozen]", locations, int(1600 * c)),
            (f"Edits since {f'K{n - 1}' if n > 1 else 'start'}   [frozen]", edits, int(600 * c)),
            ("Verification   [frozen]", verification, int(400 * c)),
            ("Hypotheses and plan (model delta, validated)   [frozen]", hypotheses, int(300 * c)),
            ("Errata", self._errata(commit), int(200 * c)),
        ]
        return frozen_text(n, step, reason, plan.lo, plan.hi, sections)

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

    def _mark_breakpoints(self) -> None:
        for m in self.messages:
            m.get("extra", {}).pop("cache_breakpoint", None)
        index_of = {id(m): i for i, m in enumerate(self.messages)}
        marks = self.messages[1:2] + [
            self.messages[i - 1] for h in self.hosts[-2:] if (i := index_of.get(id(h.obs_msg), 0)) > 0
        ]
        for m in marks:
            m.setdefault("extra", {})["cache_breakpoint"] = True
