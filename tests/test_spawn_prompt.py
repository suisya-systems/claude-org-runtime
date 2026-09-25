"""Tests for the spawn startup-prompt decision core."""

from __future__ import annotations

import json

import pytest

from claude_org_runtime.dispatcher.spawn_prompt import (
    ACTION_DONE,
    ACTION_ESCALATE,
    ACTION_SEND_KEYS,
    ACTION_WAIT,
    DEFAULT_APPROVAL_DEADLINE_MS,
    STATE_DEV_CHANNEL,
    STATE_DONE,
    STATE_FOLDER_TRUST,
    STATE_NOT_SHOWN,
    STATE_UNKNOWN,
    decide,
    screen_lines,
)

FOLDER_TRUST_NO_FIRST = [
    "Accessing workspace:",
    "/path/to/worker",
    "Quick safety check: Is this a project you created or one you trust? ...",
    "Claude Code'll be able to read, edit, and execute files here.",
    "❯ No, exit",
    "  Yes, I trust this folder",
    "Enter to confirm · Esc to cancel",
]
FOLDER_TRUST_YES_SELECTED = [
    ln.replace("❯ No, exit", "  No, exit").replace("  Yes, I trust", "❯ Yes, I trust")
    for ln in FOLDER_TRUST_NO_FIRST
]
FOLDER_TRUST_OLD_ON_NO = [
    "Do you trust the files in this folder?",
    "  1. Yes, proceed",
    "❯ 2. No, exit",
    "Enter to confirm · Esc to cancel",
]
DEV_CHANNEL = [
    "WARNING: Loading development channels",
    "--dangerously-load-development-channels is for local channel development only.",
    "Channels: server:renga-peers",
    "❯ 1. I am using this for local development",
    "  2. Exit",
    "Enter to confirm · Esc to cancel",
]
DEV_CHANNEL_ON_EXIT = [
    "WARNING: Loading development channels",
    "  1. I am using this for local development",
    "❯ 2. Exit",
    "Enter to confirm · Esc to cancel",
]
COMPOSER = [
    "────────────────────────",
    "❯ ",
    "────────────────────────",
    "  ? for shortcuts",
]


# --- screen_lines -----------------------------------------------------------


def test_screen_lines_renga_lines() -> None:
    obj = {"lines": [{"row": 0, "text": "a"}, {"row": 1, "text": "b"}]}
    assert screen_lines(obj) == ["a", "b"]


def test_screen_lines_broker_grid_sorted_by_row() -> None:
    obj = {"grid": [{"row": 2, "text": "c"}, {"row": 0, "text": "a"}, {"row": 1, "text": "b"}]}
    assert screen_lines(obj) == ["a", "b", "c"]


def test_screen_lines_text_and_plain_str() -> None:
    assert screen_lines({"text": "a\nb"}) == ["a", "b"]
    assert screen_lines("a\nb") == ["a", "b"]


def test_screen_lines_structured_content_envelope() -> None:
    obj = {"structuredContent": {"lines": [{"row": 0, "text": "x"}]}}
    assert screen_lines(obj) == ["x"]


def test_screen_lines_envelope_content_json() -> None:
    payload = json.dumps({"grid": [{"row": 1, "text": "b"}, {"row": 0, "text": "a"}]})
    obj = {"content": [{"type": "text", "text": payload}]}
    assert screen_lines(obj) == ["a", "b"]


def test_screen_lines_envelope_content_plain_text() -> None:
    obj = {"content": [{"type": "text", "text": "❯ No, exit\n  Yes"}]}
    assert screen_lines(obj) == ["❯ No, exit", "  Yes"]


def test_screen_lines_list_of_str_and_bare_rows() -> None:
    assert screen_lines(["a", "b"]) == ["a", "b"]
    assert screen_lines([{"row": 1, "text": "b"}, {"row": 0, "text": "a"}]) == ["a", "b"]


def test_screen_lines_envelope_content_json_list_and_non_screen() -> None:
    obj = {"content": [{"type": "text", "text": json.dumps(["a", "b"])}]}
    assert screen_lines(obj) == ["a", "b"]
    # JSON that is not a screen shape falls back to plain text.
    obj = {"content": [{"type": "text", "text": '{"foo":1}'}]}
    assert screen_lines(obj) == ['{"foo":1}']


@pytest.mark.parametrize("bad", [42, {"foo": 1}, [1, 2], None])
def test_screen_lines_bad_shape(bad: object) -> None:
    with pytest.raises(ValueError):
        screen_lines(bad)


# --- decide: dialogs --------------------------------------------------------


def test_folder_trust_cursor_on_no_first_moves_down() -> None:
    r = decide(FOLDER_TRUST_NO_FIRST, list(FOLDER_TRUST_NO_FIRST), 0)
    assert r["state"] == STATE_FOLDER_TRUST
    assert r["action"] == ACTION_SEND_KEYS
    assert r["send_keys"] == {"keys": ["Down"]}


def test_move_key_needs_settled_screen_too() -> None:
    # A not-yet-redrawn frame after a Down must not trigger a second Down.
    r = decide(FOLDER_TRUST_NO_FIRST, None, 0)
    assert (r["state"], r["action"]) == (STATE_FOLDER_TRUST, ACTION_WAIT)
    assert "send_keys" not in r


def test_folder_trust_older_ordering_moves_up() -> None:
    r = decide(FOLDER_TRUST_OLD_ON_NO, list(FOLDER_TRUST_OLD_ON_NO), 0)
    assert r["state"] == STATE_FOLDER_TRUST
    assert r["send_keys"] == {"keys": ["Up"]}


def test_folder_trust_on_yes_without_previous_waits() -> None:
    r = decide(FOLDER_TRUST_YES_SELECTED, None, 0)
    assert (r["state"], r["action"]) == (STATE_FOLDER_TRUST, ACTION_WAIT)
    assert "send_keys" not in r


def test_folder_trust_on_yes_with_identical_previous_enters() -> None:
    r = decide(FOLDER_TRUST_YES_SELECTED, list(FOLDER_TRUST_YES_SELECTED), 0)
    assert r["action"] == ACTION_SEND_KEYS
    assert r["send_keys"] == {"enter": True}


def test_folder_trust_on_yes_with_different_previous_waits() -> None:
    r = decide(FOLDER_TRUST_YES_SELECTED, FOLDER_TRUST_NO_FIRST, 0)
    assert r["action"] == ACTION_WAIT


def test_nbsp_after_cursor_glyph() -> None:
    screen = [ln.replace("❯ ", "❯ ") for ln in FOLDER_TRUST_YES_SELECTED]
    r = decide(screen, list(screen), 0)
    assert r["send_keys"] == {"enter": True}
    screen = [ln.replace("❯ ", "❯ ") for ln in FOLDER_TRUST_NO_FIRST]
    assert decide(screen, list(screen), 0)["send_keys"] == {"keys": ["Down"]}


def test_dev_channel_on_accept_with_identical_previous_enters() -> None:
    r = decide(DEV_CHANNEL, list(DEV_CHANNEL), 0)
    assert r["state"] == STATE_DEV_CHANNEL
    assert r["send_keys"] == {"enter": True}


def test_dev_channel_on_exit_moves_up() -> None:
    r = decide(DEV_CHANNEL_ON_EXIT, list(DEV_CHANNEL_ON_EXIT), 0)
    assert r["state"] == STATE_DEV_CHANNEL
    assert r["send_keys"] == {"keys": ["Up"]}


def test_dialog_without_cursor_waits() -> None:
    screen = [ln.replace("❯ ", "  ") for ln in FOLDER_TRUST_NO_FIRST]
    r = decide(screen, list(screen), 0)
    assert (r["state"], r["action"]) == (STATE_FOLDER_TRUST, ACTION_WAIT)


def test_leftover_dialog_above_unrecognized_dialog_is_unknown() -> None:
    # Leftover folder-trust Yes cursor above a live dialog whose cursor is on
    # "No, exit": taking the first cursor row would Enter into "No, exit".
    screen = FOLDER_TRUST_YES_SELECTED + [
        "Bypass Permissions mode",
        "❯ 1. No, exit",
        "  2. Yes, I accept",
        "Enter to confirm",
    ]
    r = decide(screen, list(screen), 0)
    assert (r["state"], r["action"]) == (STATE_UNKNOWN, ACTION_WAIT)


def test_second_cursor_row_below_dialog_is_unknown() -> None:
    screen = FOLDER_TRUST_YES_SELECTED + ["Pick one", "❯ 1. Cancel", "  2. Continue"]
    r = decide(screen, list(screen), 0)
    assert (r["state"], r["action"]) == (STATE_UNKNOWN, ACTION_WAIT)


def test_shell_prompt_above_dialog_is_ignored() -> None:
    screen = ["❯ claude --dangerously-load-development-channels"] + DEV_CHANNEL
    assert decide(screen, list(screen), 0)["send_keys"] == {"enter": True}


def test_boxed_dialog_is_recognized() -> None:
    screen = ["│ Do you trust this folder? │", "│ ❯ 1. Yes, proceed │", "│   2. No, exit      │"]
    assert decide(screen, list(screen), 0)["send_keys"] == {"enter": True}
    screen = ["│   1. Yes, proceed │", "│ ❯ 2. No, exit      │"]
    assert decide(screen, list(screen), 0)["send_keys"] == {"keys": ["Up"]}


def test_ascii_cursor_fallback() -> None:
    screen = ["> No, exit", "  Yes, I trust this folder"]
    assert decide(screen, list(screen), 0)["send_keys"] == {"keys": ["Down"]}


def test_both_dialogs_visible_is_unknown() -> None:
    r = decide(FOLDER_TRUST_YES_SELECTED + DEV_CHANNEL, None, 0)
    assert (r["state"], r["action"]) == (STATE_UNKNOWN, ACTION_WAIT)


# --- decide: non-dialog screens ---------------------------------------------


def test_blank_screen_is_not_shown() -> None:
    r = decide(["", "  "], None, 0)
    assert (r["state"], r["action"]) == (STATE_NOT_SHOWN, ACTION_WAIT)


def test_shell_prompt_without_fence_is_not_shown() -> None:
    r = decide(["user@host ~/worker", "❯ "], None, 0)
    assert r["state"] == STATE_NOT_SHOWN


def test_composer_with_fences_is_done() -> None:
    r = decide(COMPOSER, None, 0)
    assert (r["state"], r["action"]) == (STATE_DONE, ACTION_DONE)


def test_unknown_dialog_footer_waits() -> None:
    r = decide(["Pick a theme", "❯ 1. Dark", "  2. Light", "Enter to confirm · Esc to cancel"], None, 0)
    assert (r["state"], r["action"]) == (STATE_UNKNOWN, ACTION_WAIT)


@pytest.mark.parametrize("footer", ["Enter to select", "↑/↓ to navigate"])
def test_other_selection_footers(footer: str) -> None:
    assert decide(["Pick", "❯ a", footer], None, 0)["state"] == STATE_UNKNOWN


def test_boxed_composer_is_done() -> None:
    assert decide(["╭────────────╮", "│ > ", "╰────────────╯"], None, 0)["state"] == STATE_DONE


def test_fence_must_be_within_two_rows() -> None:
    fence = "────────────"
    assert decide([fence, "", "❯ "], None, 0)["state"] == STATE_DONE
    assert decide([fence, "", "", "❯ "], None, 0)["state"] == STATE_NOT_SHOWN


def test_trailing_blank_rows_ignored_when_settling() -> None:
    r = decide(FOLDER_TRUST_YES_SELECTED + ["", ""], FOLDER_TRUST_YES_SELECTED, 0)
    assert r["send_keys"] == {"enter": True}


def test_esc_to_cancel_alone_is_not_a_dialog() -> None:
    assert decide(["Thinking… (esc to cancel)"], None, 0)["state"] == STATE_NOT_SHOWN


# --- decide: deadline -------------------------------------------------------


@pytest.mark.parametrize(
    ("screen", "previous", "state"),
    [
        ([""], None, STATE_NOT_SHOWN),
        (["Pick", "Enter to confirm"], None, STATE_UNKNOWN),
        (FOLDER_TRUST_YES_SELECTED, FOLDER_TRUST_YES_SELECTED, STATE_FOLDER_TRUST),
        (FOLDER_TRUST_NO_FIRST, None, STATE_FOLDER_TRUST),
    ],
)
def test_deadline_escalates_and_never_enters(
    screen: list[str], previous: list[str] | None, state: str
) -> None:
    r = decide(screen, previous, DEFAULT_APPROVAL_DEADLINE_MS)
    assert (r["state"], r["action"]) == (state, ACTION_ESCALATE)
    assert "send_keys" not in r


def test_done_after_deadline_stays_done() -> None:
    assert decide(COMPOSER, None, 10**9)["action"] == ACTION_DONE


# --- two-prompt sequence ----------------------------------------------------


def test_two_prompt_sequence_enters_exactly_twice() -> None:
    # Each screen is what inspect_pane returns after the previous action; the
    # loop follows the caller contract (previous = None after any send_keys).
    script = [
        [""],  # not rendered yet
        FOLDER_TRUST_NO_FIRST,  # -> wait (confirm)
        FOLDER_TRUST_NO_FIRST,  # -> Down
        FOLDER_TRUST_NO_FIRST,  # stale frame, Down not drawn yet -> wait
        FOLDER_TRUST_YES_SELECTED,  # -> wait (confirm)
        FOLDER_TRUST_YES_SELECTED,  # -> Enter
        FOLDER_TRUST_YES_SELECTED,  # stale frame, Enter not drawn yet -> wait
        DEV_CHANNEL,  # -> wait (confirm)
        DEV_CHANNEL,  # -> Enter
        COMPOSER,  # -> done
    ]
    previous: list[str] | None = None
    actions = []
    enter_screens = []
    for i, screen in enumerate(script):
        r = decide(screen, previous, i * 1000)
        actions.append((r["state"], r["action"], r.get("send_keys")))
        if r.get("send_keys", {}).get("enter"):
            enter_screens.append(screen)
        previous = None if r["action"] == ACTION_SEND_KEYS else screen
    assert actions == [
        (STATE_NOT_SHOWN, ACTION_WAIT, None),
        (STATE_FOLDER_TRUST, ACTION_WAIT, None),
        (STATE_FOLDER_TRUST, ACTION_SEND_KEYS, {"keys": ["Down"]}),
        (STATE_FOLDER_TRUST, ACTION_WAIT, None),
        (STATE_FOLDER_TRUST, ACTION_WAIT, None),
        (STATE_FOLDER_TRUST, ACTION_SEND_KEYS, {"enter": True}),
        (STATE_FOLDER_TRUST, ACTION_WAIT, None),
        (STATE_DEV_CHANNEL, ACTION_WAIT, None),
        (STATE_DEV_CHANNEL, ACTION_SEND_KEYS, {"enter": True}),
        (STATE_DONE, ACTION_DONE, None),
    ]
    assert len(enter_screens) == 2
    assert "❯ Yes, I trust this folder" in enter_screens[0]


# --- CLI: spawn-prompt-step -------------------------------------------------


def _run_step(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: str,
    *extra: str,
    via_cli: bool = False,
) -> tuple[int, dict]:
    import io

    from claude_org_runtime import cli
    from claude_org_runtime.dispatcher import runner

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    if via_cli:
        code = cli.main(["dispatcher", "spawn-prompt-step", *extra])
    else:
        code = runner.main(["spawn-prompt-step", *extra])
    out = capsys.readouterr().out
    assert out.isascii()
    return code, json.loads(out)


def _grid(screen: list[str]) -> dict:
    return {"grid": [{"row": i, "text": t} for i, t in enumerate(screen)]}


@pytest.mark.parametrize(
    ("screen", "previous", "action"),
    [
        ([""], None, ACTION_WAIT),
        (FOLDER_TRUST_NO_FIRST, FOLDER_TRUST_NO_FIRST, ACTION_SEND_KEYS),
        (COMPOSER, None, ACTION_DONE),
    ],
)
def test_cli_exit_0_for_emitted_decisions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    screen: list[str], previous: list[str] | None, action: str,
) -> None:
    stdin = json.dumps({"screen": _grid(screen),
                        "previous_screen": None if previous is None else _grid(previous),
                        "elapsed_ms": 5})
    code, out = _run_step(monkeypatch, capsys, stdin)
    assert code == 0
    assert out["action"] == action
    assert (out["elapsed_ms"], out["deadline_ms"]) == (5, DEFAULT_APPROVAL_DEADLINE_MS)


def test_cli_exit_10_on_escalate_and_deadline_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    stdin = json.dumps({"screen": [""], "previous_screen": None, "elapsed_ms": 500})
    code, out = _run_step(monkeypatch, capsys, stdin, "--deadline-ms", "500")
    assert code == 10
    assert (out["action"], out["deadline_ms"]) == (ACTION_ESCALATE, 500)
    assert "send_keys" not in out


@pytest.mark.parametrize(
    "stdin",
    [
        "not json",
        "[]",
        json.dumps({"elapsed_ms": 0}),
        json.dumps({"screen": {"foo": 1}, "elapsed_ms": 0}),
        json.dumps({"screen": [""], "previous_screen": 42, "elapsed_ms": 0}),
        json.dumps({"screen": [""]}),
        json.dumps({"screen": [""], "elapsed_ms": -1}),
        json.dumps({"screen": [""], "elapsed_ms": True}),
        # unsortable row keys (TypeError) and pathological nesting (RecursionError)
        json.dumps({"screen": [{"text": "a", "row": "x"}, {"text": "b", "row": 1}],
                    "elapsed_ms": 0}),
        # Explicit id: the default id embeds the 200k-char value into the node
        # id, and pytest exports that as PYTEST_CURRENT_TEST, which exceeds
        # Windows' 32767-char environment variable limit.
        pytest.param("[" * 100000 + "]" * 100000, id="deep-nesting"),
    ],
)
def test_cli_exit_2_on_invalid_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str,
) -> None:
    code, out = _run_step(monkeypatch, capsys, stdin)
    assert code == 2
    assert "error" in out


def test_cli_mounted_under_top_level_dispatcher(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    stdin = json.dumps({"screen": COMPOSER, "elapsed_ms": 0})
    code, out = _run_step(monkeypatch, capsys, stdin, via_cli=True)
    assert (code, out["action"]) == (0, ACTION_DONE)


# --- end to end: the plan's "instructions" contract -------------------------


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    inspects: list[list[str]],
    deadline_ms: int = DEFAULT_APPROVAL_DEADLINE_MS,
) -> tuple[int, list[dict]]:
    """Follow the approve_spawn_prompts instructions against scripted inspects.

    Returns (last exit code, send_keys calls). Each iteration is 1000ms apart,
    the step's poll_interval_ms. The last scripted inspect repeats forever.
    """
    sent: list[dict] = []
    previous = None
    for i in range(10_000):
        screen = _grid(inspects[min(i, len(inspects) - 1)])
        stdin = json.dumps({"screen": screen, "previous_screen": previous,
                            "elapsed_ms": i * 1000})
        code, out = _run_step(monkeypatch, capsys, stdin, "--deadline-ms", str(deadline_ms))
        if code != 0 or out["action"] == ACTION_DONE:
            return code, sent
        if out["action"] == ACTION_SEND_KEYS:
            sent.append(out["send_keys"])
            previous = None
        else:
            previous = screen
    raise AssertionError("loop did not terminate")


def test_e2e_folder_trust_then_dev_channel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    inspects = [
        [""],
        FOLDER_TRUST_NO_FIRST, FOLDER_TRUST_NO_FIRST,  # settle -> Down
        FOLDER_TRUST_NO_FIRST,  # stale frame
        FOLDER_TRUST_YES_SELECTED, FOLDER_TRUST_YES_SELECTED,  # settle -> Enter
        [""],
        DEV_CHANNEL, DEV_CHANNEL,  # settle -> Enter
        COMPOSER,
    ]
    code, sent = _drive(monkeypatch, capsys, inspects)
    assert code == 0
    assert sent == [{"keys": ["Down"]}, {"enter": True}, {"enter": True}]


def test_e2e_never_resolves_escalates_without_enter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # Folder-trust with no cursor on an option row never becomes actionable.
    stuck = [ln.replace("❯ ", "  ") for ln in FOLDER_TRUST_NO_FIRST]
    code, sent = _drive(monkeypatch, capsys, [stuck], deadline_ms=5000)
    assert code == 10
    assert sent == []
