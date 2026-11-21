"""Evidence records (what the agent has observed) and obligations (what a contract change still requires)."""

from dataclasses import dataclass, field

FRESH, SHIFTED, LEX_STALE, SEM_STALE, EXEC_STALE, SUPERSEDED = (
    "FRESH",
    "SHIFTED",
    "LEX_STALE",
    "SEM_STALE",
    "EXEC_STALE",
    "SUPERSEDED",
)
VALID_STATES = {FRESH, SHIFTED}
EVICTABLE_STATES = {LEX_STALE, SUPERSEDED}

VIEW, ECHO, HIT, SEARCH, EXEC = "view", "echo", "hit", "search", "exec"
LOCATED_KINDS = {VIEW, ECHO, HIT}


@dataclass(eq=False)
class Evidence:
    eid: int
    kind: str
    step: int
    seq: int
    command: str
    file: str | None = None
    lo0: int = 0
    hi0: int = 0
    lo: int = 0
    hi: int = 0
    state: str = FRESH
    reason: str = ""
    reason_step: int = -1
    symbols: set[str] = field(default_factory=set)
    parent: int | None = None
    replay: str | None = None
    replay_hash: str | None = None
    scope: list[str] = field(default_factory=list)
    hit_files: set[str] = field(default_factory=set)
    dep_files: set[str] = field(default_factory=set)
    dep_dirs: set[str] = field(default_factory=set)
    global_dep: bool = False
    verdict: dict | None = None
    numbered: bool = False
    evicted: bool = False
    reported: bool = False

    def covers(self, line: int) -> bool:
        return self.lo <= line <= self.hi

    def mark(self, state: str, reason: str, step: int) -> bool:
        """Move to a stale state. Returns True if the state changed."""
        if self.state == state or self.state in (LEX_STALE, SUPERSEDED):
            return False
        self.state, self.reason, self.reason_step, self.reported = state, reason, step, False
        return True

    def depends_on(self, path: str) -> bool:
        if path in self.dep_files:
            return True
        return any(path == d or path.startswith(d.rstrip("/") + "/") for d in self.dep_dirs)


OPEN, RESOLVED, WAIVED = "OPEN", "RESOLVED", "WAIVED"


@dataclass(eq=False)
class Obligation:
    oid: int
    step: int
    seq: int
    file: str
    line: int
    text: str
    cause: str
    cause_name: str
    cls: str
    severity: float
    is_test: bool = False
    state: str = OPEN
    reason: str = ""
    shown: int = 0

    def resolve(self, reason: str) -> None:
        if self.state in (OPEN, WAIVED):
            self.state, self.reason = RESOLVED, reason
