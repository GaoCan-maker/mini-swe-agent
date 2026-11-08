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
    """Minimum net number of tokens a fold must remove (removed tokens minus the added keyframe)."""
    min_prompt_tokens: int = 64000
    """Phase triggers are dropped while the prompt is smaller than this. 0 disables the gate."""
    pressure_prompt_tokens: int = 100000
    """Fold whenever the prompt reaches this size, independent of phase triggers. 0 disables."""
    defer_edit_burst: bool = True
    """Postpone a due fold while the agent is editing; fold at the first step without edits or with a test run."""
    max_defer: int = 8
    """Drop a postponed phase trigger after this many steps."""
    keep_turns: int = 3
    keep_recent_steps: int = 5
    """Do not fold reads of non-target files mentioned in the commands or reasoning of this many recent steps."""
    checkpoint_at: Literal["e1", "first_fold", "never"] = "first_fold"
    """When to ask for the <checkpoint>: at the first change (E1) or together with the first <delta>."""
    fold_edit_commands: bool = True
    edit_stub_min_chars: int = 400
    """Only fold edit commands longer than this."""
    fold_reasoning: bool = True
    """Replace the reasoning of folded steps by a one-line stub; <checkpoint>/<delta> blocks are kept."""
    exemplar_mode: Literal["working_set", "keep"] = "working_set"
    break_even: bool = False
    """Also require the fold to pay off under prompt caching. Leave off to minimize context tokens instead of cost."""
    hit_ratio: float = 0.1
    write_ratio: float = 1.0
    """Cache write price relative to an uncached input token (1.25 for Anthropic)."""
    chars_per_token: float = 3.5
    """Initial estimate; recalibrated from the API usage after every call."""
    ws_budget: int = 4000
    ws_symbol_max_lines: int = 150
    ws_hunk_context: int = 10
    """Context lines around a changed region that lies outside any indexed symbol."""
    checkpoint_max_tokens: int = 400
    delta_max_tokens: int = 300
    search_hits_per_stub: int = 8
    outline_max_defs: int = 15
    max_unanswered_requests: int = 2
    ctx: CtxConfig = CtxConfig()
    hot: HotConfig = HotConfig()
