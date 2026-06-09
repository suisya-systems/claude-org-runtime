"""Public surface of the ``claude-org-runtime`` package.

Importing the top-level package exposes the per-feature subpackages
(:mod:`dispatcher`, :mod:`settings`, :mod:`prompts`, :mod:`schema`,
:mod:`migrate`, :mod:`terminal`) and the package version SoT
(:data:`__version__`).
"""

from .__about__ import __version__
from . import dispatcher, migrate, prompts, schema, settings, terminal

__all__ = [
    "__version__",
    "dispatcher",
    "migrate",
    "prompts",
    "schema",
    "settings",
    "terminal",
]
