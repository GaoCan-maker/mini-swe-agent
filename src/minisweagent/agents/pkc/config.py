from typing import Literal

from pydantic import BaseModel


class CtxConfig(BaseModel):
    dir: str = "/tmp/.pkc"
    """Sandbox directory for the ctx script and materialized items. Must be outside the repository."""


class HotConfig(BaseModel):
    restore_count: int = 2
    """A file restored this many times within `restore_window` steps becomes hot."""
    restore_window: int = 10
    reread_window: int = 5
    """A file re-read within this many steps after its read was folded becomes hot."""
    pff_max: int = 3
    """More restores than this within 10 steps after a keyframe widens the keep window and doubles min_gain."""
    disable_after: int = 10
    """Disable further folding after this many restores in one run."""


class PKCConfig(BaseModel):
    enabled: bool = True
    triggers: list[Literal["first_green", "new_target", "revert"]] = ["first_green", "new_target", "revert"]
    min_steps: int = 10
    min_gap: int = 5
    min_gain: int = 1500
    """Minimum number of tokens a fold must remove."""
    keep_turns: int = 3
    e1_checkpoint: bool = True
    fold_edit_commands: bool = True
    edit_stub_min_chars: int = 400
    """Only fold edit commands longer than this."""
    exemplar_mode: Literal["working_set", "keep"] = "working_set"
    break_even: bool = True
    hit_ratio: float = 0.1
    write_ratio: float = 1.0
    """Cache write price relative to an uncached input token (1.25 for Anthropic)."""
    chars_per_token: float = 3.5
    """Initial estimate; recalibrated from the API usage after every call."""
    ws_budget: int = 8000
    ws_symbol_max_lines: int = 150
    checkpoint_max_tokens: int = 400
    delta_max_tokens: int = 300
    search_hits_per_stub: int = 8
    outline_max_defs: int = 15
    max_unanswered_requests: int = 2
    ctx: CtxConfig = CtxConfig()
    hot: HotConfig = HotConfig()
