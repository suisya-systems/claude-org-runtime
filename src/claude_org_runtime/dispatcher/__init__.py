"""Dispatcher state-machine helpers ported from claude-org-ja ``tools/dispatcher_runner.py``.

Public surface (lazy-imported to avoid ``runpy`` double-load warnings
when invoked as ``python -m claude_org_runtime.dispatcher.runner``):

- :mod:`runner` -- :func:`runner.build_plan`, :func:`runner.choose_split`,
  :func:`runner.main`.
"""

__all__ = ["runner"]


def __getattr__(name: str):  # pragma: no cover - thin lazy bridge
    if name == "runner":
        import importlib
        return importlib.import_module(f"{__name__}.runner")
    raise AttributeError(name)
