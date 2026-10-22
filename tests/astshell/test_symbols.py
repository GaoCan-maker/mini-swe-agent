import pytest

from minisweagent.astshell.symbols import ParseCache, parse_file
from tests.astshell.conftest import GO_SOURCE, RUST_SOURCE, TS_SOURCE, _python_source


def _summary(source: str, lang: str) -> list[tuple]:
    return [(s.name, s.kind, s.doc_start, s.start, s.sig_end, s.end) for s in parse_file(source.encode(), lang).symbols]


@pytest.mark.parametrize(
    ("source", "lang", "expected"),
    [
        (
            GO_SOURCE,
            "go",
            [
                ("ContainerService", "struct", 5, 5, 5, 8),
                ("(*ContainerService).CreateContainer", "method", 10, 11, 12, 17),
                ("NewContainerService", "function", 19, 19, 19, 21),
            ],
        ),
        (
            TS_SOURCE,
            "typescript",
            [
                ("Store", "class", 3, 3, 3, 9),
                ("Store.add", "method", 6, 6, 6, 8),
                ("handler", "function", 11, 11, 11, 13),
                ("Shape", "interface", 15, 15, 15, 17),
            ],
        ),
        (
            RUST_SOURCE,
            "rust",
            [
                ("Point", "struct", 3, 5, 5, 7),
                ("impl fmt::Display for Point", "impl", 9, 9, 9, 13),
                ("Point::fmt", "method", 10, 10, 10, 12),
                ("helper", "function", 15, 15, 15, 17),
            ],
        ),
    ],
)
def test_symbols(source, lang, expected):
    assert _summary(source, lang) == expected


def test_python_symbols():
    pf = parse_file(_python_source().encode(), "python")
    assert [(s.name, s.doc_start, s.start, s.sig_end, s.end) for s in pf.symbols[:3]] == [
        ("helper", 5, 5, 5, 6),
        ("Service", 9, 10, 10, 730),
        ("Service.prop_0", 13, 13, 14, 28),
    ]
    prop = pf.symbols[2]
    assert prop.parent is pf.symbols[1] and prop.doc_line == 15 and prop.is_leaf and not pf.symbols[1].is_leaf
    assert pf.leaf_at(20) is prop and pf.leaf_at(11) is None and 9 in pf.comment_only


@pytest.mark.parametrize(
    ("content", "reason"),
    [(b"x = 1\n" * 20001, "too_many_lines"), (b"x = '" + b"a" * 3000 + b"'\n", "long_line")],
)
def test_skip_reasons(content, reason):
    assert parse_file(content, "python").skip_reason == reason


def test_parse_cache_is_content_addressed():
    cache = ParseCache(max_entries=2)
    first = cache.get(b"def a():\n    pass\n", "python")
    assert cache.get(b"def a():\n    pass\n", "python") is first
    assert cache.get(b"def b():\n    pass\n", "python") is not first
    cache.get(b"def c():\n    pass\n", "python")
    assert cache.get(b"def a():\n    pass\n", "python") is not first
