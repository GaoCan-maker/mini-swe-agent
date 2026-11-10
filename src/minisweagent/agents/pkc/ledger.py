import re
from dataclasses import dataclass, field

from minisweagent.agents.pkc.commands import Segment, parse_command, record_category


@dataclass
class Record:
    """One tool call: command, full output and the messages that show it."""

    oid: int
    step: int
    cmd: str
    segments: list[Segment]
    category: str
    output: dict
    action: dict
    eid: int | None = None
    edit_files: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    obs_msg: dict | None = None
    act_msg: dict | None = None
    base_content: str = ""
    appendix: list = field(default_factory=list)
    hidden_at: int | None = None
    """Step of the fold that removed this record's turn from the context."""

    @property
    def id(self) -> str:
        return f"o{self.oid}"

    @property
    def raw(self) -> str:
        return self.output.get("output", "") or ""

    @property
    def rc(self) -> int | None:
        return self.output.get("returncode")

    @property
    def read_paths(self) -> list[str]:
        return list(dict.fromkeys(s.read[0] for s in self.segments if s.read))

    def render(self) -> None:
        if self.obs_msg is not None:
            self.obs_msg["content"] = self.base_content + "".join(map(str, self.appendix))


class Ledger:
    def __init__(self):
        self.records: list[Record] = []
        self.n_edits = 0

    def add(self, step: int, action: dict, output: dict, root: str) -> Record:
        cmd = action.get("command", "")
        segments = parse_command(cmd, root)
        rec = Record(len(self.records), step, cmd, segments, record_category(segments), output, action)
        self.records.append(rec)
        return rec

    def next_eid(self) -> int:
        self.n_edits += 1
        return self.n_edits

    def get(self, ref: str) -> Record | None:
        if m := re.fullmatch(r"#?o(\d+)", ref):
            return self.records[int(m.group(1))] if int(m.group(1)) < len(self.records) else None
        if m := re.fullmatch(r"#?e(\d+)", ref):
            return next((r for r in self.records if r.eid == int(m.group(1))), None)
        return None

    def restore_text(self, ref: str) -> str | None:
        """Exact original text behind an id: output for oK, command for eK."""
        if (rec := self.get(ref)) is None:
            return None
        return rec.cmd if ref.lstrip("#").startswith("e") else rec.raw

    def latest_with(self, needle: str) -> Record | None:
        return next((r for r in reversed(self.records) if needle in r.raw or needle in r.cmd), None)
