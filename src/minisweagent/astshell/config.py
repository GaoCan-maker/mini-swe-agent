from pydantic import BaseModel


class ASTShellConfig(BaseModel):
    enabled: bool = False
    snap_read: bool = True
    skeleton: bool = True
    skeleton_calls: bool = False
    smart_grep: bool = True
    grep_rerank: bool = True
    seen_fold: bool = False
    budget_guard: bool = True
    observation_char_limit: int = 10000
    budget_ratio: float = 0.92
    snap_up_max: int = 35
    snap_down_max: int = 60
    tail_fold_min: int = 6
    skeleton_min_chars: int = 6000
    block_max_chars: int = 7000
    body_keep_max: int = 12
    header_max_lines: int = 60
    gap_max_lines: int = 30
    calls_max: int = 6
    grep_head_max: int = 500
    grep_rerank_min: int = 40
    grep_max_files: int = 40
    hits_window: int = 10
    seen_window: int = 30
    max_file_bytes: int = 2_000_000
    max_command_chars: int = 20000
    languages: list[str] = ["python", "go", "typescript", "tsx", "javascript", "rust"]
    test_path_patterns: list[str] = [
        r"(^|/)(tests?|__tests__|spec|testdata|fixtures?)(/|$)",
        r"_test\.(go|py)$",
        r"(^|/)test_[^/]*\.py$",
        r"\.(test|spec)\.[jt]sx?$",
        r"(^|/)examples?/",
    ]
    log_original: bool = False
