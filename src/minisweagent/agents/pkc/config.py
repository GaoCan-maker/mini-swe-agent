from typing import Literal

from pydantic import BaseModel


class CtxConfig(BaseModel):
    dir: str = "/tmp/.pkc"
    """Sandbox directory for the ctx script and materialized items. Must be outside the repository."""


class HotConfig(BaseModel):
    restore_count: int = 2
    """A file restored this many times within `restore_window` steps becomes hot."""
    restore_window: int = 10
    reread_window: int = 10
    """A file re-read within this many steps after its turn was removed becomes hot."""


class PKCConfig(BaseModel):
    enabled: bool = True
    triggers: list[Literal["first_green", "new_target", "revert"]] = ["first_green", "new_target", "revert"]
    """Phase events that mark the history before them as stale."""
    min_steps: int = 10
    min_gap: int = 5
    """Steps between two folds, and between two attempts that were too shallow."""
    min_prompt_tokens: int = 64000
    """Phase folds wait until the prompt reaches this size. 0 disables the gate."""
    min_fold_fraction: float = 0.2
    """A phase fold must remove at least this share of the prompt; otherwise its mark waits for a later fold."""
    high_water: int = 120000
    """Fold regardless of phase marks when the prompt reaches this size. 0 disables."""
    low_water: int = 45000
    """A high-water fold must bring the prompt below this size, or at least halve it."""
    defer_edit_burst: bool = True
    """Postpone a due fold while the agent is editing; fold at the first step without edits or with a test run."""
    max_defer: int = 8
    """Fold anyway after postponing this many steps."""
    keep_turns: int = 2
    """Most recent turns that are never removed."""
    recent_budget: int = 12000
    """Older turns stay verbatim while the kept turns fit in this many tokens."""
    checkpoint_at: Literal["e1", "first_fold", "never"] = "first_fold"
    """When to ask for the <checkpoint>: at the first change (E1) or together with the first <delta>."""
    chars_per_token: float = 3.5
    """Initial estimate; recalibrated from the API usage after every call."""
    ws_budget: int = 8000
    ws_symbol_max_lines: int = 150
    ws_hunk_context: int = 10
    """Context lines around a changed region that lies outside any indexed symbol."""
    history_budget: int = 800
    keep_budget: int = 1500
    """Tokens for the outputs the model listed under KEEP in its last delta."""
    checkpoint_max_tokens: int = 400
    delta_max_tokens: int = 300
    search_hits_per_stub: int = 8
    outline_max_defs: int = 15
    max_unanswered_requests: int = 2
    ctx: CtxConfig = CtxConfig()
    hot: HotConfig = HotConfig()
