T0 = """Context management in this environment:
- Every command output carries an id (e.g. "id": "o12" or a leading #o12). Outputs of commands that changed files
  also carry an edit id (e.g. "edit_id": "e7" or #e7).
- At some points the environment will fold old outputs into one-line stubs and add a <keyframe> message that
  contains the current code of the files you are working on (re-read from disk), a location map, an edit log
  and the test status. Code shown in a keyframe is exact and current.
- Any folded item can be restored exactly with:  ctx show o12 | ctx show e7 | ctx show <path>::<symbol> | ctx show <path>:<a>-<b>
  ctx is an ordinary shell command: you can pipe it, redirect it, or combine it with other commands.
- When asked for a <checkpoint> or <delta>, put it in your reasoning text, before your command. Do not include
  code or line numbers in it. As evidence, copy the id of the output you rely on (o12, e7) or name the file
  (path or path::symbol)."""

T1 = """

[environment] You made your first change to the repository. In your next reply, before your command, write:
<checkpoint>
TARGETS: every path::symbol you expect to modify, in order, including ones not touched yet
EXEMPLARS: up to 5 pointers (path::symbol + a few words) to existing code you will NOT modify but will imitate
           or call: similar implementations, helper signatures, test cases to copy, registration tables. No code.
RULED_OUT: hypotheses or locations you rejected; evidence = output id (e.g. o14) or path
UNVERIFIED: assumptions you have not checked yet; evidence = output id or path
DONE_WHEN: observable behaviour and the test command that shows the task is done
</checkpoint>
One line per item, at most 400 tokens, no code, no line numbers. Then continue normally."""

T2 = """

[environment] Older context will be folded after your next reply. Before your command, write:
<delta>
DONE: checkpoint TARGETS that are finished; evidence = edit id (e.g. e7) or path::symbol
CONFIRMED: new findings since the last checkpoint or keyframe; evidence = output id or path
REJECTED: hypotheses you now rejected; evidence = output id or path
TODO: at most 3 remaining steps
KEEP: up to 5 output ids whose exact text you still need
UNVERIFIED: open assumptions
</delta>
At most 300 tokens, no code, no line numbers."""


def t4(kind: str, problems: list[str]) -> str:
    return f"\n\n[environment] {len(problems)} {kind} lines had problems: " + "; ".join(problems[:8]) + "."


def repeated_edit(eid: int) -> str:
    return f"\n\n[environment] This command is identical to e{eid}, which was already applied; see the keyframe working set for the current code."


def reverted(paths: list[str], kf: int) -> str:
    return f"\n\n[environment] Reverted: {', '.join(paths)}. Code for these files shown in K{kf} is stale."
