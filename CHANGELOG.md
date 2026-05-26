# Changelog

All notable changes to Cosmonapse are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `py.typed` marker so type checkers honour the package's annotations (PEP 561).
- Optional dependency extras: `[kafka]` (aiokafka), `[postgres]` (asyncpg), and
  `[flask]` (Flask/WSGI Neuron factory), alongside the existing `[nats]`.
- `ContextFetcher` is now exported from the top-level package and `__all__`.
- Top-level `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, and `LICENSE`.
- A real test suite covering storage backends, the in-memory and dev synapses,
  Dendrite protocol behaviour, and the Neuron factories. Adapter tests that
  require live infrastructure (NATS/Kafka/Postgres) skip automatically when it
  is unavailable.

### Changed
- Flask is no longer a core runtime dependency; it moved to the `[flask]` extra.
  `pip install cosmonapse` no longer pulls in Flask.
- `Dendrite.publish()` is now private (`_publish()`). All public outbound
  signals must go through `emit()`, which enforces the `SYNAPSE_TYPES` guard,
  so the protocol can no longer be bypassed accidentally.
- The `_signal`-suffixed handler decorators (`on_error_signal`, etc.) are now
  the documented canonical names.
- `_install.py` (PATH manipulation) moved from the `cosmonapse` SDK package into
  the `cosmo` CLI package, where shell-config changes belong.

### Deprecated
- The short handler aliases `on_error`, `on_register`, `on_deregister`, and
  `on_heartbeat` now emit a `DeprecationWarning`. Use the `_signal` forms.

### Removed
- Unused `anyio` core dependency.
- The deprecated `cosmonapse.transport` compatibility shim.
- The public `Dendrite.cortex_id` back-compat attribute (use `dendrite_id`).
- The deprecated standalone `packages/cli` directory (the canonical `cosmo`
  CLI ships inside the SDK distribution).
- Committed `__pycache__` / `*.pyc` artifacts; added a repository `.gitignore`.

### Fixed
- `MemorySynapse.request()`, `DevSynapse.request()`, `KafkaSynapse.request()`,
  and `SqliteRegistryStore` now use `asyncio.get_running_loop()` instead of the
  deprecated `asyncio.get_event_loop()`.
- `DevSynapse(port=0)` now correctly requests an OS-assigned port instead of
  silently falling back to 7070.
- `DevSynapseServer.on_signal` is typed `Callable[[str, str], None] | None`
  rather than the bare builtin `callable`.

## [0.0.1]
- Initial development release.
