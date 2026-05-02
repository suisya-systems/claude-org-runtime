# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.1] - 2026-05-02

Initial skeleton (no public API).

- Package metadata in `pyproject.toml` (name `claude-org-runtime`, MIT, py>=3.10).
- `src/claude_org_runtime` package with version SoT in `__about__.py`.
- Smoke test asserting the exposed `__version__`.
- Pytest matrix CI (`.github/workflows/test.yml`) on ubuntu/macos/windows × py3.10–3.12.
- Trusted Publisher release skeleton (`.github/workflows/release.yml`), tag-triggered only.
- README, LICENSE, and `.gitignore`.
