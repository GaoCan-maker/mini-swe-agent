"""Static analysis of the bash commands issued by the agent.

The analysis is deliberately conservative: anything it does not understand is treated as a possible
mutation (so the repository is re-snapshotted) and as an unknown read (so the output is content-anchored).
"""

import posixpath
import re
import shlex
from dataclasses import dataclass, field

SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

_HEREDOC_RE = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_][\w-]*)\1")
_SEPARATOR_CHARS = set(";&|()")
_WORD_SEPARATORS = {"{", "}", "!", "then", "do", "else", "fi", "done", "if", "while", "until", "elif"}
_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
_PREFIXES = {"sudo", "time", "nohup", "env", "command", "builtin", "exec", "stdbuf", "nice", "xargs"}
_READ_PROGRAMS = {
    "cat", "nl", "head", "tail", "sed", "awk", "gawk", "grep", "egrep", "fgrep", "rg", "ag", "ls", "find", "tree",
    "wc", "file", "stat", "less", "more", "sort", "uniq", "cut", "diff", "echo", "printf", "pwd", "true", "false",
    "which", "type", "basename", "dirname", "realpath", "readlink", "du", "tr", "column", "jq", "xxd", "od",
    "hexdump", "git", "tac", "bat", "test", "[", "date", "whoami", "env", "printenv", "cd",
}  # fmt: skip
_GIT_READ = {"diff", "show", "log", "status", "grep", "ls-files", "blame", "rev-parse", "branch", "ls-tree", "cat-file"}
_SEARCH_PROGRAMS = {"grep", "egrep", "fgrep", "rg", "ag"}
_VIEW_PROGRAMS = {"cat", "nl", "less", "more", "bat", "tac"}
_MUTATING_PROGRAMS = {
    "cp", "mv", "rm", "touch", "mkdir", "rmdir", "truncate", "ln", "chmod", "chown", "patch", "tee", "dd",
    "install", "rsync", "unzip", "tar",
}  # fmt: skip

TEST_RE = re.compile(
    r"(^|[\s;&|(])(pytest|py\.test|tox|nox|go\s+test|cargo\s+(test|nextest)|jest|vitest|mocha|"
    r"(npm|pnpm|yarn|bun)\s+(run\s+)?test\b|npx\s+(jest|vitest|mocha|playwright)|"
    r"python[\d.]*\s+-m\s+(pytest|unittest)|make\s+(test|check)\b|deno\s+test)"
)
RUN_RE = re.compile(
    r"(^|[\s;&|(])(python[\d.]*|node|ts-node|tsx|deno|go\s+(run|build|vet)|cargo\s+(run|build|check|clippy)|"
    r"tsc|npx\s+tsc|ruby|bun)(\s|$)"
)
INSTALL_RE = re.compile(
    r"(pip[\d.]*\s+install|uv\s+(pip\s+install|add|sync)|poetry\s+(add|install)|conda\s+install|"
    r"(npm|pnpm|yarn|bun)\s+(install|i|add|ci)(\s|$)|go\s+(get|mod\s+(tidy|download))|cargo\s+(add|update|fetch)|"
    r"apt(-get)?\s+install)"
)
_SED_PRINT_RE = re.compile(r"^\s*(\d+)\s*(?:,\s*(\d+|\$))?\s*p\s*$")
_SED_ADDR_RE = re.compile(r"^\s*(\d+)\s*(?:,\s*(\d+|\$))?\s*!?\s*([a-zA-Z{=])")
_AWK_RANGE_RE = re.compile(r"NR\s*(>=|>|==)\s*(\d+)(?:.*?NR\s*(<=|<|==)\s*(\d+))?")


@dataclass
class ReadSpec:
    path: str
    lo: int | None = None
    hi: int | None = None
    tail: int | None = None
    numbered: bool = False
    exact: bool = True
    """False if the output is a transformation of the file (e.g. sed without -n), so only anchoring is reliable."""


@dataclass
class LineEdit:
    path: str
    lines: list[int]


@dataclass
class CommandInfo:
    raw: str
    reads: list[ReadSpec] = field(default_factory=list)
    search: bool = False
    search_paths: list[str] = field(default_factory=list)
    search_single_file: str | None = None
    write_targets: list[str] = field(default_factory=list)
    inplace_targets: list[str] = field(default_factory=list)
    line_edits: list[LineEdit] = field(default_factory=list)
    path_args: list[str] = field(default_factory=list)
    is_test: bool = False
    is_exec: bool = False
    is_install: bool = False
    is_submit: bool = False
    pure_read: bool = True


def _strip_heredocs(text: str) -> str:
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for match in _HEREDOC_RE.finditer(line):
            delim = match.group(2)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1
    return "\n".join(out)


def _tokenize(text: str) -> list[str]:
    text = _strip_heredocs(text).replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _segments(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Split tokens into simple commands. Returns (tokens, receives_piped_input)."""
    segments, current, piped = [], [], False
    for tok in tokens:
        if (tok and set(tok) <= _SEPARATOR_CHARS) or (not current and tok in _WORD_SEPARATORS) or tok in ("{", "}"):
            if current:
                segments.append((current, piped))
            current = []
            piped = tok in ("|", "|&")
            continue
        current.append(tok)
    if current:
        segments.append((current, piped))
    return segments


def _split_redirects(tokens: list[str]) -> tuple[list[str], list[str]]:
    args, writes, i = [], [], 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in _WRITE_REDIRECTS or tok in (">&", "<&", "<", "<<", "<<-", "<<<", "<>"):
            if args and args[-1].isdigit() and tok[0] in "<>":
                args.pop()
            if tok in _WRITE_REDIRECTS and nxt and nxt != "/dev/null" and not nxt.startswith("&"):
                writes.append(nxt)
            i += 2
            continue
        args.append(tok)
        i += 1
    return args, writes


def _strip_prefixes(args: list[str]) -> list[str]:
    while args:
        head = posixpath.basename(args[0])
        if re.match(r"^[A-Za-z_]\w*=", args[0]):
            args = args[1:]
        elif head == "timeout":
            args = args[1:]
            while args and args[0].startswith("-"):
                args = args[1:]
            args = args[1:]
        elif head in _PREFIXES:
            args = args[1:]
            while args and args[0].startswith("-"):
                takes_value = args[0] in ("-n", "-I", "-L", "-P", "-d", "-u", "-s")
                args = args[2:] if takes_value else args[1:]
        else:
            break
    return args


def _join(cd: str, path: str) -> str:
    if path.startswith("/"):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(cd, path)) if cd else posixpath.normpath(path)


def _looks_like_path(tok: str) -> bool:
    if not tok or tok.startswith("-") or tok.startswith("$") or "=" in tok or len(tok) > 300:
        return False
    if any(c in tok for c in "*?[]{}'\"`;|&<>()\\ "):
        return False
    return "/" in tok or bool(re.search(r"\.[A-Za-z0-9]{1,5}$", tok))


def _parse_count(rest: list[str], default: int = 10) -> tuple[int | None, bool, list[str]]:
    """Parse head/tail arguments. Returns (count, from_start_plus, files); count None for byte mode."""
    count, plus, files, i = default, False, [], 0
    while i < len(rest):
        arg = rest[i]
        value = None
        if arg in ("-n", "--lines"):
            value = rest[i + 1] if i + 1 < len(rest) else ""
            i += 2
        elif arg.startswith("--lines="):
            value, i = arg.split("=", 1)[1], i + 1
        elif re.fullmatch(r"-n[+-]?\d+", arg):
            value, i = arg[2:], i + 1
        elif re.fullmatch(r"-\d+", arg):
            value, i = arg[1:], i + 1
        elif arg in ("-c", "--bytes") or arg.startswith("-c") or arg.startswith("--bytes"):
            return None, False, []
        elif arg.startswith("-"):
            i += 1
            continue
        else:
            files.append(arg)
            i += 1
            continue
        if value and re.fullmatch(r"[+-]?\d+", value):
            plus = value.startswith("+")
            count = int(value.lstrip("+-"))
    return count, plus, files


def _parse_sed(rest: list[str]) -> tuple[bool, bool, list[str], list[str]]:
    """Returns (inplace, quiet, scripts, files)."""
    inplace, quiet, scripts, files, explicit, i = False, False, [], [], False, 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-e", "--expression"):
            scripts.append(rest[i + 1] if i + 1 < len(rest) else "")
            explicit, i = True, i + 2
            continue
        if arg.startswith("--expression="):
            scripts.append(arg.split("=", 1)[1])
            explicit, i = True, i + 1
            continue
        if arg in ("-f", "--file"):
            explicit, i = True, i + 2
            continue
        if arg.startswith("--in-place"):
            inplace, i = True, i + 1
            continue
        if arg in ("--quiet", "--silent"):
            quiet, i = True, i + 1
            continue
        if arg.startswith("-") and len(arg) > 1 and not arg.startswith("--"):
            flags = arg[1:]
            if flags.startswith("i"):
                inplace = True
                if i + 1 < len(rest) and rest[i + 1] == "":
                    i += 1
                i += 1
                continue
            quiet = quiet or "n" in flags
            inplace = inplace or "i" in flags
            if flags.endswith("e"):
                scripts.append(rest[i + 1] if i + 1 < len(rest) else "")
                explicit, i = True, i + 2
                continue
            i += 1
            continue
        if not explicit and not scripts:
            scripts.append(arg)
            explicit = True
        else:
            files.append(arg)
        i += 1
    return inplace, quiet, scripts, files


def sed_line_addresses(scripts: list[str]) -> list[int]:
    lines: list[int] = []
    for script in scripts:
        for part in re.split(r"[;\n]", script):
            match = _SED_ADDR_RE.match(part)
            if not match:
                continue
            start = int(match.group(1))
            end = match.group(2)
            if end and end != "$":
                lines.extend(range(start, min(int(end), start + 2000) + 1))
            else:
                lines.append(start)
    return lines


def _sed_print_range(scripts: list[str]) -> tuple[int, int | None] | None:
    ranges = []
    for script in scripts:
        for part in [p for p in re.split(r"[;\n]", script) if p.strip()]:
            match = _SED_PRINT_RE.match(part)
            if not match:
                return None
            start, end = int(match.group(1)), match.group(2)
            ranges.append((start, None if end == "$" else int(end) if end else start))
    if not ranges:
        return None
    his = [hi for _, hi in ranges]
    return min(lo for lo, _ in ranges), None if None in his else max(his)


def _narrow(spec: ReadSpec, lo: int, hi: int | None) -> None:
    """Narrow a piped view to lines lo..hi of its own output."""
    if spec.tail is not None:
        spec.exact = False
        return
    base = spec.lo or 1
    spec.lo = base + lo - 1
    new_hi = None if hi is None else base + hi - 1
    spec.hi = new_hi if spec.hi is None else (spec.hi if new_hi is None else min(spec.hi, new_hi))


def _search_paths(prog: str, rest: list[str]) -> tuple[list[str], bool]:
    """Returns (paths, recursive) for grep-like commands."""
    takes_value = {"-e", "-f", "-A", "-B", "-C", "-m", "--max-count", "-g", "--glob", "-t", "--type", "--include",
                   "--exclude", "--exclude-dir", "-d", "-D"}  # fmt: skip
    pattern_given, recursive, positional, i = False, prog in ("rg", "ag"), [], 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-e", "-f", "--regexp", "--file"):
            pattern_given, i = True, i + 2
            continue
        if arg in takes_value:
            i += 2
            continue
        if arg.startswith("-"):
            if not arg.startswith("--") and ("r" in arg[1:] or "R" in arg[1:]):
                recursive = True
            if arg in ("--recursive", "--dereference-recursive"):
                recursive = True
            i += 1
            continue
        positional.append(arg)
        i += 1
    paths = positional if pattern_given else positional[1:]
    return paths, recursive


def analyze(command: str) -> CommandInfo:
    info = CommandInfo(raw=command)
    info.is_submit = SUBMIT_MARKER in command
    info.is_test = bool(TEST_RE.search(command))
    info.is_exec = info.is_test or bool(RUN_RE.search(command))
    info.is_install = bool(INSTALL_RE.search(command))
    if info.is_install or info.is_exec:
        info.pure_read = False

    cd = ""
    pending: ReadSpec | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            info.reads.append(pending)
            pending = None

    for tokens, piped_in in _segments(_tokenize(command)):
        if not piped_in:
            flush()
        args, writes = _split_redirects(tokens)
        args = _strip_prefixes(args)
        if writes:
            info.write_targets.extend(_join(cd, w) for w in writes)
            info.pure_read = False
        if not args:
            continue
        prog, rest = posixpath.basename(args[0]), args[1:]
        info.path_args.extend(_join(cd, a) for a in rest if _looks_like_path(a))
        if prog not in _READ_PROGRAMS:
            info.pure_read = False

        if prog == "cd":
            cd = _join(cd, rest[0]) if rest and not rest[0].startswith("-") else ""
        elif prog in _VIEW_PROGRAMS:
            skip_next = {"-b", "-w", "-s", "-v", "-i", "-n"} if prog == "nl" else set()
            files, i = [], 0
            while i < len(rest):
                if rest[i] in skip_next:
                    i += 2
                    continue
                if not rest[i].startswith("-"):
                    files.append(_join(cd, rest[i]))
                i += 1
            numbered = prog == "nl" or any(a.startswith("-") and "n" in a for a in rest if prog == "cat")
            if piped_in or not files:
                continue
            if len(files) == 1 and prog != "tac":
                pending = ReadSpec(files[0], numbered=numbered)
            else:
                info.reads.extend(ReadSpec(f, numbered=numbered, exact=False) for f in files)
        elif prog in ("head", "tail"):
            count, plus, files = _parse_count(rest)
            if count is None:
                continue
            if files:
                for f in files:
                    path = _join(cd, f)
                    if prog == "head":
                        info.reads.append(ReadSpec(path, lo=1, hi=count))
                    elif plus:
                        info.reads.append(ReadSpec(path, lo=count))
                    else:
                        info.reads.append(ReadSpec(path, tail=count))
            elif piped_in and pending is not None:
                if prog == "head":
                    _narrow(pending, 1, count)
                elif plus:
                    _narrow(pending, count, None)
                elif pending.lo is None and pending.hi is None:
                    pending.tail = count
                else:
                    pending.exact = False
        elif prog == "sed":
            inplace, quiet, scripts, files = _parse_sed(rest)
            files = [_join(cd, f) for f in files]
            if inplace:
                info.pure_read = False
                info.write_targets.extend(files)
                info.inplace_targets.extend(files)
                lines = sed_line_addresses(scripts)
                if lines:
                    info.line_edits.extend(LineEdit(f, lines) for f in files)
                continue
            rng = _sed_print_range(scripts) if quiet else None
            if files:
                for f in files:
                    if rng:
                        info.reads.append(ReadSpec(f, lo=rng[0], hi=rng[1]))
                    else:
                        info.reads.append(ReadSpec(f, exact=False))
            elif piped_in and pending is not None:
                if rng:
                    _narrow(pending, rng[0], rng[1])
                else:
                    pending.exact = False
        elif prog in ("awk", "gawk"):
            script = next((a for a in rest if not a.startswith("-")), "")
            files = [_join(cd, a) for a in rest[rest.index(script) + 1 :]] if script in rest else []
            if "inplace" in rest or "system(" in script or re.search(r"print[^;}]*>", script):
                info.pure_read = False
                info.write_targets.extend(files)
                continue
            match = _AWK_RANGE_RE.search(script)
            lo = hi = None
            if match:
                lo = int(match.group(2)) + (1 if match.group(1) == ">" else 0)
                if match.group(1) == "==":
                    hi = lo
                if match.group(4):
                    hi = int(match.group(4)) - (1 if match.group(3) == "<" else 0)
            if files:
                info.reads.extend(ReadSpec(f, lo=lo, hi=hi, exact=lo is not None) for f in files)
            elif piped_in and pending is not None:
                if lo is not None:
                    _narrow(pending, lo, hi)
                else:
                    pending.exact = False
        elif prog in _SEARCH_PROGRAMS or (prog == "git" and rest[:1] == ["grep"]):
            sub_rest = rest[1:] if prog == "git" else rest
            if piped_in:
                if pending is not None:
                    info.search = True
                    info.search_single_file = pending.path
                    info.search_paths.append(pending.path)
                    pending = None
                continue
            paths, recursive = _search_paths("rg" if prog == "git" else prog, sub_rest)
            if not paths and not recursive and prog != "git":
                continue
            info.search = True
            info.search_paths.extend(_join(cd, p) for p in paths or ["."])
            if len(paths) == 1 and not recursive and _looks_like_path(paths[0]):
                info.search_single_file = _join(cd, paths[0])
        elif prog == "git":
            sub = next((a for a in rest if not a.startswith("-")), "")
            if sub not in _GIT_READ:
                info.pure_read = False
        elif prog == "find":
            if any(a in ("-exec", "-execdir", "-delete", "-ok", "-fprint", "-fprintf", "-fls") for a in rest):
                info.pure_read = False
        elif prog in ("perl", "ruby"):
            info.pure_read = False
            if any(a.startswith("-") and "i" in a[1:] for a in rest):
                files = [_join(cd, a) for a in rest if _looks_like_path(a)]
                info.write_targets.extend(files)
                info.inplace_targets.extend(files)
        elif prog == "tee":
            info.pure_read = False
            info.write_targets.extend(_join(cd, a) for a in rest if not a.startswith("-"))
        elif prog in _MUTATING_PROGRAMS:
            info.pure_read = False
            targets = [a for a in rest if not a.startswith("-")]
            if prog in ("cp", "install", "rsync") and targets:
                targets = targets[-1:]
            info.write_targets.extend(_join(cd, a) for a in targets)
    flush()
    if info.is_submit:
        info.pure_read = False
    return info
