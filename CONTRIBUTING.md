# Contributing to Cosmonapse

Thanks for your interest in contributing! Cosmonapse is an early-stage
(v0.0.1) project and feedback, bug reports, and pull requests are all welcome.

## Development setup

```bash
git clone <repo-url>
cd cosmonapse-core

# Editable install of the Python SDK + bundled `cosmo` CLI, with dev tools
pip install -e "packages/python-sdk[dev]"
```

The `[dev]` extra pulls in `pytest`, `pytest-asyncio`, `ruff`, and `mypy`, plus
the optional libraries (`flask`, `aiokafka`, `asyncpg`) so the full test suite
can exercise every code path.

## Running the checks

```bash
cd packages/python-sdk

pytest            # test suite
ruff check .      # lint
mypy cosmonapse   # type-check (the package ships a py.typed marker)
```

Tests for adapters that need live infrastructure (NATS, Kafka, Postgres) are
written to **skip automatically** when the broker/driver isn't available, so a
plain `pytest` run is green with zero external services.

## Project conventions

- **Python 3.11+.** Use modern typing (`X | None`, `list[str]`, etc.).
- **Line length 100**, enforced by ruff (see `pyproject.toml`).
- **Public API lives in `cosmonapse/__init__.py`'s `__all__`.** Anything
  prefixed with `_` is private and may change without notice.
- **The protocol guard matters.** Outbound signals from a `Dendrite` must go
  through `emit()`, which validates against `SYNAPSE_TYPES`. `_publish()` is
  private precisely so the guard can't be bypassed.
- **Handler decorators:** the `_signal`-suffixed forms (`on_error_signal`,
  `on_register_signal`, …) are canonical. The short aliases are deprecated.

## Pull requests

1. Open an issue first for anything non-trivial so we can agree on the approach.
2. Add or update tests for your change.
3. Make sure `pytest`, `ruff`, and `mypy` pass.
4. Keep commits focused and write a clear description.

## Reporting bugs

Please include the Cosmonapse version, your Python version, the synapse adapter
in use, and a minimal reproduction if possible.
