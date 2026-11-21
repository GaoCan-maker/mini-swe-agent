from pydantic import BaseModel

DEFAULT_PROMPT_ADDENDUM = """\
Some command outputs end with a <synapse> block. It reports what your command actually changed in the repository \
(with current line numbers), which earlier outputs no longer match the files, and code locations that may need \
updating after a signature change. Treat outdated outputs as no longer reflecting the files. Occasionally a command \
is held back with an explanation; re-issue it unchanged to run it anyway."""


class SynapseConfig(BaseModel):
    enabled: bool = True

    # Feature switches (one per component, for ablations)
    symbol_level: bool = True
    """Symbol-level AST diffing. False degrades to file-level invalidation."""
    contract_classifier: bool = True
    """Separate body-local from contract changes. False treats every non-cosmetic change as a breaking contract change."""
    propagate: bool = True
    """Transitive invalidation along references. False keeps invalidation lexical."""
    edit_echo: bool = True
    sync_events: bool = True
    footer: bool = True
    line_guard: bool = True
    submit_gate: bool = True
    delta_read: bool = True
    evict: bool = True
    prompt_addendum: str = DEFAULT_PROMPT_ADDENDUM

    # Budgets
    echo_lines_per_symbol: int = 40
    echo_lines_per_step: int = 80
    echo_context_lines: int = 2
    event_char_budget: int = 1200
    footer_char_budget: int = 600
    footer_min_age: int = 2
    footer_max_shows: int = 6
    """An obligation that was listed this many times without being addressed is no longer shown (gate still checks)."""
    max_refs_listed: int = 5
    max_notes_per_kind: int = 3
    observation_char_limit: int = 10000

    # Propagation
    propagation_depth: int = 1
    depth_decay: float = 0.5
    max_ref_hits: int = 200
    max_ref_files: int = 40
    min_name_length: int = 3
    severity_threshold: float = 0.25
    heat_decay: float = 0.85
    heat_floor: float = 0.7

    # Evidence binding / reuse
    anchor_min_lines: int = 3
    max_bound_ranges: int = 5
    max_hits_per_search: int = 50
    delta_read_min_chars: int = 400
    delta_read_max_steps: int = 15

    # Cache-aware eviction (prices relative to uncached input)
    cache_write_price: float = 1.25
    cache_read_price: float = 0.1
    expected_steps: int = 60
    min_expected_remaining: int = 5
    evict_keep_last: int = 6
    evict_min_chars: int = 800

    # Probing
    probe_timeout: int = 20
    replay_timeout: int = 10
    max_replays_per_step: int = 5
    max_changed_files_per_step: int = 20
    max_file_bytes: int = 1_000_000
    max_status_lines: int = 3000

    test_path_patterns: list[str] = [
        r"(^|/)(tests?|__tests__|spec|testdata|fixtures?)(/|$)",
        r"_test\.(go|py)$",
        r"(^|/)test_[^/]*\.py$",
        r"\.(test|spec)\.[cm]?[jt]sx?$",
    ]
    env_file_patterns: list[str] = [
        r"(^|/)(pyproject\.toml|setup\.py|setup\.cfg|requirements[^/]*\.txt|Pipfile(\.lock)?|poetry\.lock|uv\.lock)$",
        r"(^|/)(tox\.ini|pytest\.ini|conftest\.py|noxfile\.py)$",
        r"(^|/)(package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|tsconfig[^/]*\.json|(jest|vitest|babel)\.config\.[^/]+)$",
        r"(^|/)(go\.mod|go\.sum|Cargo\.toml|Cargo\.lock|Makefile|CMakeLists\.txt)$",
    ]
    ignored_path_patterns: list[str] = [
        r"(^|/)(node_modules|vendor|third_party|\.git|__pycache__|\.pytest_cache|target|dist|build|\.venv|venv)/",
        r"\.(pyc|min\.js|map)$",
    ]
