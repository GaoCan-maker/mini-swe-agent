from pydantic import BaseModel


class ReqConfig(BaseModel):
    enabled: bool = True
    gate: bool = True
    """Hold back the first submission once and list task requirements that no passing check has exercised."""

    # Matching
    anchor_threshold: float = 0.5
    """Weighted fraction of a requirement's anchors that a text must contain to count as matching it."""
    keyword_threshold: float = 0.6
    """For requirements without anchors: fraction of their keywords a text must contain."""
    min_keywords: int = 2
    max_clauses: int = 80

    # Gate message
    max_gate_chars: int = 6000
    max_clause_chars: int = 400

    # Probing
    probe_timeout: int = 30
    max_paths: int = 3000
    max_diff_bytes: int = 3_000_000
    max_file_bytes: int = 200_000
    max_scripts_per_run: int = 3
    max_test_files_per_run: int = 3
    max_evidence_chars: int = 60_000

    boilerplate_patterns: list[str] = [
        r"work on this in a new branch",
        r"commit everything when you are done",
        r"^please solve this issue:?$",
        r"^you can execute bash commands and edit files",
    ]
    meta_patterns: list[str] = [
        r"implementation[- ]defined",
        r"implementation details?\b",
        r"\bup to you\b",
        r"\bfree to (choose|pick|use)\b",
        r"keep the implementation focused",
        r"flexibility does not apply",
    ]
    """Clauses that grant freedom rather than require behavior; they are never listed."""
    test_path_patterns: list[str] = [
        r"(^|/)(tests?|__tests__|spec|specs|testdata|fixtures?)(/|$)",
        r"_test\.(go|py)$",
        r"(^|/)test_[^/]*\.py$",
        r"\.(test|spec)\.[cm]?[jt]sx?$",
        r"_spec\.rb$",
    ]
    scratch_patterns: list[str] = [
        r"(^|/)(repro|reproduce|debug|scratch|tmp|temp|check|verify|try|demo|sandbox)[^/]*$",
        r"(^|/)scratchpad/",
    ]
    ignored_path_patterns: list[str] = [
        r"(^|/)(node_modules|vendor|third_party|\.git|__pycache__|\.pytest_cache|target|dist|build|\.venv|venv)/",
        r"\.(pyc|min\.js|map|lock|sum)$",
        r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$",
    ]
