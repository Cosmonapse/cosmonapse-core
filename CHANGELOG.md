# Changelog

All notable changes to Cosmonapse are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-30

First feature release. Adds shared memory (Engram), per-trace event handles
(Pathway), the full cognition signal family, capability-routed dispatch with
competitive bidding, and a richer `cosmo` CLI. The TypeScript SDK ships its
first published version alongside the Python SDK — parity gaps relative to
Python are tracked in `packages/ts-sdk/PORTING_STATUS.md`.

### Added
- **Engram** — shared memory subsystem for Neurons. New `cosmonapse.engram`
  package with the `Engram` ABC, `EngramBinding` for declarative wiring on an
  Axon, `EngramClient` for in-Neuron access, and three backends:
  `InMemoryEngram`, `SqliteEngram`, and `PostgresEngram` (lazy-imports
  `asyncpg`). New wire types `RECALL` / `RECALLED` / `IMPRINT` / `IMPRINTED`
  with matching signal builders (`recall_signal`, `recalled_signal`,
  `imprint_signal`, `imprinted_signal`) and `new_engram_id()` ULID helper.
  Errors: `EngramTimeout`, `EngramCancelled`, `EngramNotBound`,
  `EngramOverloaded`. See `ENGRAM_DESIGN.md`.
- **Pathway** — `cosmonapse.pathway` exposes `Pathway` and `PathwayClosedError`.
  `Dendrite.dispatch(...)` and `observe_pathway(trace_id)` return a per-trace
  event handle supporting three consumption shapes on one primitive:
  `await pw.wait()`, `@pw.on(SignalType.X)`, and `async for sig in pw`.
  `Pathway(scope="all" | "terminal")` filters which signal types are delivered;
  pathways auto-close on FINAL / ERROR.
- **Cognition signal family** — `PLAN`, `THOUGHT_DELTA`, `TOOL_CALL`,
  `TOOL_RESULT`, `MEMORY_APPEND`, `CRITIQUE`, `ESCALATION`, `CONSENSUS`,
  `CONTEXT_SYNC`. Each has a matching `emit_*` method and `on_*` decorator on
  `Dendrite`. Decorators accept `neuron=` / `capability=` / `trace_id=` filter
  kwargs and `on_trace(trace_id, *types)` narrows a handler to a single
  workflow.
- **Capability-routed dispatch** — `dispatch(capabilities=..., ...)` publishes
  on `cosmonapse.<ns>.TASK.routed` with a queue group keyed on each Dendrite's
  aggregate capabilities, so identical-cap-profile Dendrites load-balance and
  the broker delivers each TASK exactly once within the group.
- **Competitive bidding** — `dispatch_offer(input=..., capabilities=...,
  deadline_ms=..., select=...)` runs the `TASK_OFFER` / `BID` / `TASK_AWARDED`
  flow. Selection strategies: `"first_bid"`, `"lowest_cost"`,
  `"highest_confidence"`. Returns a Pathway scoped to the awarded workflow.
- **Dispatch sugar** — `dispatch_and_wait(...)` (dispatch, await first
  terminal signal, return it) and `dispatch_and_subscribe(...)` (dispatch and
  return the live Pathway).
- **CLI** — new `cosmo init` command scaffolds a minimal Axon + Dendrite
  project. New `cosmo completion` prints a bash/zsh/fish completion script.
  `cosmo synapse view` gained namespace listing and per-namespace signal
  streaming. Internal `_prism` / `_prism_view` / `_prism_hero` modules back the
  richer signal-tree rendering used by `synapse view` and `doppler`.
- **TypeScript SDK** (`@cosmonapse/sdk`) — first published version (`0.1.0`).
  Envelope types and codec, typed signal builders, `Synapse` interface plus
  in-process `MemorySynapse` and networked `NatsSynapse`, `RegistryStore` with
  `MemoryRegistryStore`, `Neuron` / `Axon` / `Dendrite`, and the
  `expressNeuron` / `mcpNeuron` / unified `neuron()` factories. Parity gaps
  versus the Python SDK are tracked in `packages/ts-sdk/PORTING_STATUS.md`.
- `py.typed` marker so type checkers honour the package's annotations
  (PEP 561).
- Optional dependency extras: `[kafka]` (aiokafka), `[postgres]` (asyncpg), and
  `[flask]` (Flask/WSGI Neuron factory), alongside the existing `[nats]`.
- `ContextFetcher` is now exported from the top-level package and `__all__`.
- Top-level `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `LICENSE`, and
  `ENGRAM_DESIGN.md`.
- A real test suite covering storage backends, the in-memory and dev synapses,
  Dendrite protocol behaviour, the cognition API, event-driven flows, Pathway,
  Engram, and the Neuron factories. Adapter tests that require live
  infrastructure (NATS / Kafka / Postgres) skip automatically when it is
  unavailable.

### Changed
- `Dendrite` is now the canonical orchestrator type. `Cortex` remains as a
  back-compat alias; the public `Dendrite.cortex_id` attribute is gone — use
  `dendrite_id`. The role guard now sits on `emit()` itself, so every cognition
  emitter funnels through it and worker-role Dendrites are blocked from
  emitting orchestration signals (except `bid()`, which uses the private
  publish path because bidding is how workers participate in capability
  routing).
- Flask is no longer a core runtime dependency; it moved to the `[flask]`
  extra. `pip install cosmonapse` no longer pulls in Flask.
- `Dendrite.publish()` is now private (`_publish()`). All public outbound
  signals must go through `emit()`, which enforces the `SYNAPSE_TYPES` guard,
  so the protocol can no longer be bypassed accidentally.
- The `_signal`-suffixed handler decorators (`on_error_signal`, etc.) are now
  the documented canonical names.
- `_install.py` (PATH manipulation) moved from the `cosmonapse` SDK package
  into the `cosmo` CLI package, where shell-config changes belong.

### Deprecated
- The short handler aliases `on_error`, `on_register`, `on_deregister`, and
  `on_heartbeat` now emit a `DeprecationWarning`. Use the `_signal` forms.

### Removed
- The standalone `cosmo dev` command (the dev synapse is now managed through
  `cosmo synapse start memory`).
- Unused `anyio` core dependency.
- The deprecated `cosmonapse.transport` compatibility shim.
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
- The Flask/WSGI Neuron factory no longer uses `Response.charset`, which
  Werkzeug 3.x removed; it now decodes responses via `get_data(as_text=True)`,
  working across Werkzeug 2.x and 3.x.

## [0.0.1]
- Initial development release.
