"""Schema-driven worker ``settings.local.json`` generator.

Ported from claude-org-ja ``tools/generate_worker_settings.py`` +
``tools/role_configs_schema.json`` (the SoT now lives here).

Public surface (lazy-imported to avoid ``runpy`` double-load warnings
when invoked as ``python -m claude_org_runtime.settings.generator``):

- :mod:`generator` -- :func:`generator.render_role`,
  :func:`generator.load_schema`, :func:`generator.main`.
"""

__all__ = ["generator"]


def __getattr__(name: str):  # pragma: no cover - thin lazy bridge
    if name == "generator":
        import importlib
        return importlib.import_module(f"{__name__}.generator")
    raise AttributeError(name)
