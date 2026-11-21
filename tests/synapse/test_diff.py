from minisweagent.synapse.diff import C0, C1, C2, C3, LineMap, diff_symbols, map_through


def test_line_map_shift_and_edit():
    old = ["a", "b", "c", "d"]
    new = ["x", "a", "b", "C", "d"]
    lm = LineMap(old, new)
    assert lm.map_line(1) == 2
    assert lm.map_line(3) is None
    assert lm.map_line(4) == 5
    assert lm.map_range(1, 2) == (2, 3, True)
    assert lm.map_range(1, 4)[2] is False
    assert lm.first_shift() == (1, 1)
    assert map_through([lm, LineMap(new, ["y", *new])], 1) == 3


def _changes(old: str, new: str, lang: str = "python"):
    from minisweagent.synapse.symbols import parse_symbols

    return {c.name: c for c in diff_symbols(parse_symbols(old, lang), parse_symbols(new, lang))}


def test_go_classification(tree_sitter):
    base = "package config\n\nfunc Load(path string) (*Config, error) {\n\treturn nil, nil\n}\n"
    breaking = _changes(base, base.replace("Load(path string)", "Load(ctx context.Context, path string)"), "go")
    assert breaking["Load"].cls == C2 and not breaking["Load"].compatible
    body = _changes(base, base.replace("return nil, nil", "return &Config{}, nil"), "go")
    assert body["Load"].cls in (C1, "C1+")


def test_typescript_optional_param_is_compatible(tree_sitter):
    base = "export function render(node: Node): string {\n  return node.text;\n}\n"
    new = base.replace("render(node: Node)", "render(node: Node, opts?: Options)")
    change = _changes(base, new, "typescript")["render"]
    assert change.cls == C2 and change.compatible


def test_python_classification(tree_sitter):
    base = "def f(a, b):\n    return a + b\n\n\ndef g(x):\n    # note\n    return x\n"
    body = _changes(base, base.replace("a + b", "a - b"))
    assert body["f"].cls == C1
    cosmetic = _changes(base, base.replace("# note", "# other note"))
    assert cosmetic["g"].cls == C0
    compatible = _changes(base, base.replace("def f(a, b):", "def f(a, b, c=1):"))
    assert compatible["f"].cls == C2 and compatible["f"].compatible
    breaking = _changes(base, base.replace("def f(a, b):", "def f(a, b, c):"))
    assert breaking["f"].cls == C2 and not breaking["f"].compatible
    renamed = _changes(base, base.replace("def g(x):", "def h(x):"))
    assert renamed["h"].cls == C3 and renamed["h"].renamed_from == "g"
