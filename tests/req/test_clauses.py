from minisweagent.req.clauses import extract_anchors, extract_clauses
from minisweagent.req.config import ReqConfig

BOILERPLATE = ReqConfig().boilerplate_patterns


def _anchors(text: str, code: bool = False) -> dict[str, float]:
    return {a.text: a.weight for a in extract_anchors(text, code=code)}


def test_titles_become_context_and_boilerplate_is_dropped():
    task = """Please solve this issue: Improve module loading so `require()` is deterministic.

Expected outcomes
1. Cache visibility and reset
- Expose cache stats via `require_cache_info()` with numeric fields: `hits` and `misses`.
- Expose `reset_require_cache()` to clear module cache and loader state.

2. Cycle handling
- Cyclic imports fail with an error whose message starts with `cyclic module import detected:`.

You can execute bash commands and edit files to implement the necessary changes.
IMPORTANT: Please work on this in a new branch from main and commit everything when you are done.
"""
    clauses = extract_clauses(task, BOILERPLATE)
    texts = [c.text for c in clauses]
    assert texts[0].startswith("Improve module loading")
    assert not any("Cache visibility" == t or "Cycle handling" == t for t in texts)
    assert not any("new branch" in t or "bash commands" in t for t in texts)
    info = next(c for c in clauses if "require_cache_info" in c.text)
    assert info.context == "Cache visibility and reset"
    assert {"require_cache_info", "hits", "misses"} <= {a.text for a in info.anchors}
    cycle = next(c for c in clauses if "Cyclic" in c.text)
    assert cycle.context == "Cycle handling"
    assert [a.text for a in cycle.anchors] == ["cyclic module import detected:"]


def test_nested_bullets_carry_parent_context():
    task = """## Expected Behavior

- New stepped range behavior must work in both directions:
  - Positive step iterates forward.
  - Negative step iterates backward.
- A step of `0` must raise an error.
"""
    clauses = extract_clauses(task, BOILERPLATE)
    forward = next(c for c in clauses if "forward" in c.text)
    assert forward.context.startswith("Expected Behavior > New stepped range behavior")
    zero = next(c for c in clauses if "step of" in c.text)
    assert zero.context == "Expected Behavior"


def test_paragraph_sentences_and_code_blocks():
    task = """Add a partial mode, e.g. for drafts. It must keep `strict_mode` working.

```go
type Result interface {
    Errors() []Conflict
}
```
"""
    clauses = extract_clauses(task, BOILERPLATE)
    texts = [c.text for c in clauses]
    assert texts[0] == "Add a partial mode, e.g. for drafts."
    assert texts[1] == "It must keep `strict_mode` working."
    errors = next(c for c in clauses if c.text.startswith("Errors()"))
    assert errors.context == "type Result interface {"
    assert {"Errors", "Conflict"} <= {a.text for a in errors.anchors}


def test_message_spans_become_fixed_fragments():
    anchors = _anchors('`index ranges can only be numerical: got "<inspect>" (type <TYPE>)`')
    assert anchors == {"index ranges can only be numerical: got": 1.0}
    anchors = _anchors("`range assignment size mismatch: target=<X> value=<Y>`")
    assert "range assignment size mismatch: target=" in anchors


def test_code_spans_flags_and_kebab_names():
    anchors = _anchors("Accept `value[start:end:step]` and `--module-debug` via `action-pinning`.")
    assert anchors["--module-debug"] == 1.0
    assert anchors["action-pinning"] == 1.0
    assert "start" in anchors and "step" in anchors
    assert anchors["value"] < 0.5


def test_prose_identifiers():
    anchors = _anchors("Make BaseConverter call structure_attrs() and honour MAX_DEPTH and obj.refine values.")
    assert {"BaseConverter", "structure_attrs", "MAX_DEPTH", "refine"} <= set(anchors)
    assert "Make" not in anchors and "values" not in anchors


def test_coverage_matches_alternate_identifier_forms():
    [clause] = extract_clauses("Add `PartialResult` support for drafts.", BOILERPLATE)
    assert clause.coverage("def test_partial_result(): ...") == 1.0
    assert clause.coverage("class PartialResultX: ...") == 0.0
    [kebab] = extract_clauses("Add the `action-pinning` rule.", BOILERPLATE)
    assert kebab.coverage("type ActionPinningConfig struct") == 0.0
    assert kebab.coverage("func ActionPinning() {}") == 1.0
    assert kebab.coverage('cfg["action_pinning"]') == 1.0


def test_keyword_clauses():
    [clause] = extract_clauses("Negative step iterates backward.", BOILERPLATE)
    assert not clause.anchors
    assert clause.keywords == ["negative", "step", "iterates", "backward"]
    assert clause.accepts(clause.coverage("assert negative_step_iteration_backwards()"), 0.5, 0.6, 2)
    assert not clause.accepts(clause.coverage("step"), 0.5, 0.6, 2)
