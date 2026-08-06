"""Cross-platform portability guards for the runtime #158 test suite.

These tests assert things about the SOURCE of the other test modules, not
about the runtime. That is deliberate, and it is the only mechanism that can
catch this failure class before CI does: a test that hardcodes a POSIX
absolute path passes on Linux and macOS and fails only on windows-latest, so
a developer running the suite locally on Linux gets no signal at all.

The concrete incident (PR #159): a `build_plan` call was written with
``{"task_id": "demo", "worker_dir": "/tmp"}``. ``build_plan`` runs
``validate_cwd`` on ``worker_dir``, and ``/tmp`` does not exist on Windows, so
the plan came back ``input_invalid`` instead of the ``split_capacity_exceeded``
the test was pinning. All three Windows jobs failed; ubuntu and macOS passed.
Because CI runs ``pytest -x``, that one assertion also masked every test
ordered after it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# The #158 modules. Deliberately an explicit list rather than a glob: a new
# test module should be added here consciously, with its author having read
# why the guard exists.
_GUARDED_MODULES = (
    "test_dispatcher_multitab_geometry.py",
    "test_dispatcher_multitab_placement.py",
    "test_dispatcher_multitab_population.py",
    "test_dispatcher_multitab_portability.py",
)

# Task keys whose value build_plan resolves against the real filesystem
# (``validate_cwd``). A POSIX literal here is always a portability bug.
_FILESYSTEM_TASK_KEYS = frozenset({"worker_dir", "cwd"})


def _module_paths() -> list[Path]:
    here = Path(__file__).parent
    return [here / name for name in _GUARDED_MODULES]


def _is_posix_absolute(value: object) -> bool:
    """True for a literal like ``/tmp`` that only resolves on POSIX.

    ``PurePosixPath.is_absolute`` is not used on purpose: this must give the
    same answer no matter which platform the guard itself runs on, since the
    whole point is to fail on Linux for a Windows-only defect.
    """
    return isinstance(value, str) and value.startswith("/") and len(value) > 1


@pytest.mark.parametrize("module", _module_paths(), ids=lambda p: p.name)
def test_no_posix_absolute_path_reaches_the_filesystem(module: Path) -> None:
    """A validated path argument must come from ``tmp_path``, never a literal.

    Two shapes are checked, and they are the two that actually reach
    ``validate_cwd`` / ``Path.exists``:

    * a dict entry ``{"worker_dir": "/..."}`` / ``{"cwd": "/..."}``
    * ``Path("/...")``, which the same call sites pass as ``state_dir``

    An opaque wire value is NOT flagged, and must not be: ``Peer.cwd`` is
    carried through verbatim and never resolved, so ``{"cwd": "/repo"}`` inside
    a peer dict is legitimate test data. The dict-key rule below would flag it,
    which is why peer fixtures build their cwd through ``_peer`` / raw dicts
    handed to ``_parse_peers`` rather than through a task dict -- and why the
    one intentional exception is spelled out here rather than pattern-matched.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in _FILESYSTEM_TASK_KEYS
                    and isinstance(value, ast.Constant)
                    and _is_posix_absolute(value.value)
                ):
                    # Peer wire dicts legitimately carry an opaque cwd string.
                    # A task dict is identified by carrying task_id alongside.
                    keys = {
                        k.value for k in node.keys
                        if isinstance(k, ast.Constant)
                    }
                    if "task_id" not in keys and key.value == "cwd":
                        continue
                    offenders.append(
                        f"{module.name}:{value.lineno} "
                        f'{{"{key.value}": {value.value!r}}}'
                    )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Path"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and _is_posix_absolute(node.args[0].value)
        ):
            offenders.append(
                f"{module.name}:{node.lineno} Path({node.args[0].value!r})"
            )

    assert not offenders, (
        "POSIX-absolute literals reach the filesystem in these tests, so they "
        "pass on Linux/macOS and fail on windows-latest:\n  "
        + "\n  ".join(offenders)
        + "\nUse the tmp_path fixture instead (str(tmp_path) for worker_dir, "
        "tmp_path / '.state' for state_dir)."
    )


def test_the_guard_actually_catches_the_shape_that_broke_ci() -> None:
    """Prove the guard is not vacuous.

    A structural test that scans source is worthless if its matcher silently
    stops matching. This feeds it the exact code that failed CI and the fixed
    form, and requires opposite verdicts.
    """
    broke_ci = ast.parse(
        'build_plan({"task_id": "demo", "worker_dir": "/tmp"}, [],'
        ' Path("/nonexistent-state"))'
    )
    fixed = ast.parse(
        'build_plan({"task_id": "demo", "worker_dir": str(tmp_path)}, [],'
        ' tmp_path / ".state")'
    )

    def offences(tree: ast.AST) -> int:
        n = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {
                    k.value for k in node.keys if isinstance(k, ast.Constant)
                }
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value in _FILESYSTEM_TASK_KEYS
                        and isinstance(value, ast.Constant)
                        and _is_posix_absolute(value.value)
                        and not ("task_id" not in keys and key.value == "cwd")
                    ):
                        n += 1
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Path"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and _is_posix_absolute(node.args[0].value)
            ):
                n += 1
        return n

    assert offences(broke_ci) == 2, "worker_dir literal AND Path literal"
    assert offences(fixed) == 0


def test_peer_wire_cwd_is_not_flagged() -> None:
    """The intentional exception, pinned so nobody 'fixes' it later.

    ``Peer.cwd`` is opaque data copied from the renga wire and never resolved
    against the filesystem, so a peer fixture may carry any string. Flagging it
    would push authors toward tmp_path values that make the parsing tests less
    faithful to the wire shape they exist to pin.
    """
    peer_dict = ast.parse('{"id": 5, "name": "w", "cwd": "/repo"}')
    offenders = 0
    for node in ast.walk(peer_dict):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in _FILESYSTEM_TASK_KEYS
                    and isinstance(value, ast.Constant)
                    and _is_posix_absolute(value.value)
                    and not ("task_id" not in keys and key.value == "cwd")
                ):
                    offenders += 1
    assert offenders == 0
