from minisweagent.synapse.bash import analyze


def test_numbered_view():
    info = analyze("cat -n src/a.py")
    assert info.pure_read
    assert [r.path for r in info.reads] == ["src/a.py"]
    assert info.reads[0].numbered


def test_sed_print_range():
    info = analyze("sed -n '10,20p' a.py")
    assert info.pure_read
    assert (info.reads[0].lo, info.reads[0].hi) == (10, 20)


def test_head_tail_and_pipes():
    info = analyze("cat -n a.py | sed -n '5,9p'")
    assert info.reads[0].path == "a.py"
    assert (info.reads[0].lo, info.reads[0].hi) == (5, 9)
    assert analyze("tail -n 30 a.py").reads[0].tail == 30


def test_cd_prefix():
    info = analyze("cd sub && cat x.py")
    assert info.reads[0].path == "sub/x.py"


def test_sed_inplace_line_edit():
    info = analyze("sed -i '12s/foo/bar/' pkg/m.py")
    assert not info.pure_read
    assert info.inplace_targets == ["pkg/m.py"]
    assert info.line_edits and info.line_edits[0].lines == [12]


def test_search():
    info = analyze("grep -rn compute src tests")
    assert info.search and info.pure_read
    assert info.search_paths == ["src", "tests"]
    single = analyze("grep -n compute src/a.py")
    assert single.search_single_file == "src/a.py"


def test_heredoc_write():
    info = analyze("cat > new.py <<'EOF'\nprint('cat > other.py')\nEOF")
    assert info.write_targets == ["new.py"]
    assert not info.pure_read


def test_classification():
    assert analyze("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && git diff").is_submit
    assert analyze("python -m pytest tests/test_x.py -q").is_test
    assert analyze("go test ./pkg/...").is_test
    assert analyze("pip install -e .").is_install
    assert analyze("python repro.py").is_exec
    assert not analyze("python repro.py").pure_read
    assert analyze("git diff").pure_read
