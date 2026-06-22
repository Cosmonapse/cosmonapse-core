# Changelog

All notable changes to Cosmonapse are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.6] - 2026-06-22

### Fixed
- **`on_task_offer(capability=...)` / `onTaskOffer(.., {capability})` silently
  dropped every offer.** The handler's capability filter resolved the offer's
  *directed neuron*, but a TASK_OFFER is a broadcast that carries its required
  capabilities in `payload.capabilities` and has no directed neuron  -  so the
  filter always failed and no BID was ever emitted (the `10-bidding` example
  timed out). The filter now narrows against the offer's requested capability
  set; an offer with no capabilities stays open to all. Fixed at parity in both
  SDKs.

## [0.1.1] - 2026-06-05

### Added
- **New LLM provider Neurons** in both SDKs' `Neuron(source=...)` / `neuron(source, opts)` factory. First-class `"openai"` (Chat Completions) and `"anthropic"` (Messages API) sources, each a dedicated provider class (`httpx` in Python, the runtime `fetch` in TypeScript  -  no provider SDK dependency) resolving credentials from an explicit key or the `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env vars. Plus OpenAI-compatible hosted aliases  -  `"groq"`, `"openrouter"`, `"together"`, and `"mistral"`  -  pre-configured on the existing HuggingFace neuron. All are soft dependencies (lazy-imported) and return the standard `{"response": ..., "meta": ...}` shape. The TypeScript SDK ships these at parity with the Python reference (`packages/ts-sdk/src/neuron-openai.ts`).

### Removed
- **The HTTP-app Neuron type** (`Neuron(source="flask" | "wsgi" | "api")` and
  the module `cosmonapse._neuron_http`; the TypeScript `expressNeuron` and the
  `neuron("express" | "http" | "api", …)` factory sources). An HTTP API is not
  a Neuron  -  a web app is an inbound request handler, not an `input -> output`
  worker. The supported pattern is the reverse: keep your web framework (Flask,
  Express, …) on the outside as an HTTP boundary and dispatch TASK Signals from
  its route handlers via an orchestrator Dendrite, wiring
  `@dendrite.on_agent_output` directly in the app. The `neuron_real_world`
  example and the quickstart now show this.
- The `[flask]` optional dependency was dropped from the Python SDK; the shared
  `CloseableNeuronFn` type moved from `neuron-express.ts` to `neuron.ts` in the
  TS SDK.

## [0.1.0] - 2026-05-30

First feature release. Adds shared memory (Engram), per-trace event handles
(Pathway), the full cognition signal family, capability-routed dispatch with
competitive bidding, and a richer `cosmo` CLI. The TypeScript SDK ships its
first published version alongside the Python SDK  -  parity gaps relative to
Python are tracked in `packages/ts-sdk/PORTING_STATUS.md`.

### Added
- **Engram**  -  shared memory subsystem for Neurons. New `cosmonapse.engram`
  package with the `Engram` ABC, `EngramBinding` for declarative wiring on an
  Axon, `EngramClient` for in-Neuron access, and three backends:
  `InMemoryEngram`, `SqliteEngram`, and `PostgresEngram` (lazy-imports
  `asyncpg`). New wire types `RECALL` / `RECALLED` / `IMPRINT` / `IMPRINTED`
  with matching signal builders (`recall_signal`, `recalled_signal`,
  `imprint_signal`, `imprinted_signal`) and `new_engram_id()` ULID helper.
  Errors: `EngramTimeout`, `EngramCancelled`, `EngramNotBound`,
  `EngramOverloaded`. See `design/ENGRAM_DESIGN.md`.
- **Pathway**  -  `cosmonapse.pathway` exposes `Pathway` and `PathwayClosedError`.
  `Dendrite.dispatch(...)` and `observe_pathway(trace_id)` return a per-trace
  event handle supporting three consumption shapes on one primitive:
  `await pw.wait()`, `@pw.on(SignalType.X)`, and `async for sig in pw`.
  `Pathway(scope="all" | "terminal")` filters which signal types are delivered;
  pathways auto-close on FINAL / ERROR.
- **Cognition signal family**  -  `PLAN`, `THOUGHT_DELTA`, `TOOL_CALL`,
  `TOOL_RESULT`, `MEMORY_APPEND`, `CRITIQUE`, `ESCALATION`, `CONSENSUS`,
  `CONTEXT_SYNC`. Each has a matching `emit_*` method and `on_*` decorator on
  `Dendrite`. Decorators accept `neuron=` / `capability=` / `trace_id=` filter
  kwargs and `on_trace(trace_id, *types)` narrows a handler to a single
  workflow.
- **Capability-routed dispatch**  -  `dispatch(capabilities=..., ...)` publishes
  on `cosmonapse.<ns>.TASK.routed` with a queue group keyed on each Dendrite's
  aggregate capabilities, so identical-cap-profile Dendrites load-balance and
  the broker delivers each TASK exactly once within the group.
- **Competitive bidding**  -  `dispatch_offer(input=..., capabilities=...,
  deadline_ms=..., select=...)` runs the `TASK_OFFER` / `BID` / `TASK_AWARDED`
  flow. Selection strategies: `"first_bid"`, `"lowest_cost"`,
  `"highest_confidence"`. Returns a Pathway scoped to the awarded workflow.
- **Dispatch sugar**  -  `dispatch_and_wait(...)` (dispatch, await first
  terminal signal, return it) and `dispatch_and_subscribe(...)` (dispatch and
  return the live Pathway).
- **CLI**  -  new `cosmo init` command scaffolds a minimal Axon + Dendrite
  project. New `cosmo completion` prints a bash/zsh/fish completion script.
  `cosmo synapse view` gained namespace listing and per-namespace signal
  streaming. Internal `_prism` / `_prism_view` / `_prism_hero` modules back the
  richer signal-tree rendering used by `synapse view` and `doppler`.
- **TypeScript SDK** (`@cosmonapse/sdk`)  -  first published version (`0.1.0`).
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
  `design/ENGRAM_DESIGN.md`.
- A real test suite covering storage backends, the in-memory and dev synapses,
  Dendrite protocol behaviour, the cognition API, event-driven flows, Pathway,
  Engram, and the Neuron factories. Adapter tests that require live
  infrastructure (NATS / Kafka / Postgres) skip automatically when it is
  unavailable.

### Changed
- `Dendrite` is now the canonical orchestrator type. `Cortex` remains as a
  back-compat alias; the public `Dendrite.cortex_id` attribute is gone  -  use
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
- The deprecat