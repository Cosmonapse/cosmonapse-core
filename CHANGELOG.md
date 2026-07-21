# Changelog

All notable changes to Cosmonapse are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`neuron=` attribution on `emit_final` / `emit_error` (python-sdk +
  ts-sdk).** The reply emit helpers now take the same `neuron=` override
  the cognition helpers (`emit_plan`, `emit_tool_call`, ...) always had:
  `emit_final(..., neuron="assistant")` attributes the FINAL to the
  producing Neuron instead of the emitting Dendrite - matching
  terminal-handler promotion, so observers (doppler, Prism) keep the
  lineage TASK -> AGENT_OUTPUT -> FINAL on one participant. Default
  (omitted) is unchanged: the Dendrite's id.
- **Raw `/v1/completions` support on the `huggingface` Neuron source
  (python-sdk).** `Neuron(source="huggingface", use_completions_api=True,
  stop=[...])` posts to the OpenAI-compatible `/v1/completions` path - for
  vLLM / llama.cpp / proxy deployments that expose no chat route. The path
  takes a rendered `prompt` string ONLY: the Neuron will not guess a
  model's chat template - render it caller-side (ChatML, Llama, ...) and
  pass `prompt=`; `messages` input raises. `stop=` is also forwarded on
  the chat and TGI `/generate` paths. `use_chat_api` and
  `use_completions_api` are mutually exclusive.

### Changed
- npm package version aligned to `0.1.8` (`package.json` was left at
  `0.1.7` when the 0.1.8 release was tagged).

### Removed
- The GitLab CI config (`.gitlab-ci.yml`). GitHub Actions (`ci.yml` /
  `release.yml`) is the single CI going forward.

## [0.1.8] - 2026-07-13

The first public release of Cosmonapse. Earlier 0.1.x entries below were
internal milestones on the way here.

### Added
- **`Axon.host` - deferred Dendrite decorators (python-sdk + ts-sdk).** The
  standard way to declare host-side behaviour in a Neuron's module. Python:
  `@AXON.host.on_agent_output(neuron=...)` / `@AXON.host.on_tool_call(...)`
  queue a handler at module level; the Axon applies it to the **hosting
  Dendrite** right after that Dendrite emits REGISTER for it (before
  `@axon.on_connect` hooks fire) and ensures the inbound subscription.
  Replaces the hand-written `on_connect -> node = a.dendrite -> @node.on_* ->
  ensure_subscribed` wiring; any `Dendrite.on_*` decorator with the standard
  `(fn, *, neuron=, capability=, trace_id=)` shape is supported, names are
  validated eagerly at import time. TypeScript: `axon.host.onToolCall(fn,
  { neuron })` and the full cognition/reply `on*` family (plus the generic
  `axon.host.onSignal(type, fn, filter)`), replayed by the hosting Dendrite
  in `start()` and `addAxon()`; `AxonHost` is exported from the package
  root.
- **`cosmo` for npm users - one CLI build, two registries.** The npm package
  now installs a `cosmo` command (`npm install -g @cosmonapse/sdk`). It is a
  self-bootstrapping launcher (`bin/cosmo.js`, zero dependencies): it
  delegates to `$COSMO_PYTHON` or any system Python that already has the
  `cosmonapse` package; failing that it creates a private venv under
  `~/.cosmonapse/cli-venv` and pip-installs `cosmonapse` pinned to the npm
  package's own version (one-time, refreshed on version change). Every path
  runs `python -m cosmo`, so pip and npm users always run the single
  canonical CLI implementation from the Python package - by design there is
  no second CLI codebase to drift. Added `cosmo/__main__.py` as the
  delegation target. Requires a Python 3.11+ interpreter on PATH; without
  one the launcher exits 127 with clear instructions.

### Changed
- **`cosmo init` scaffolds the standard package skeleton.** New projects get
  `config.py` + `neurons/hello.py` + `brain.py` (wiring) + `demo.py` +
  README (entries stay thin; behaviour lives in neurons/, deployment in
  brain.py).
  `python demo.py` is self-contained: it hosts BOTH sides, and SYNAPSE_URL
  only swaps the transport (in-process MemorySynapse vs a running synapse).
  No worker.py is generated - the README carries the 10-line entry to add
  when workers should become their own processes. Replaces the old
  two-file `worker.py` + `orchestrator.py` scaffold.

### Known limitations
- **Open models drift from strict action schemas.** A harness that asks a
  plain-chat LLM for `{"tool", "args"}` JSON will eventually get shorthand
  variants or truncated objects back; today `@detects_output` can only
  transform or error, so every harness must carry its own fallback parsing. The planned fix is an Axon-level reject-and-repair contract
  (`InvalidOutput(reason, hint)` + bounded `output_retries`, plus
  `response_format`/`tools` passthrough on OpenAI-compatible providers) -
  scheduled for 0.2.0; see the roadmap.

## [0.1.6] - 2026-06-22

### Fixed
- **`on_task_offer(capability=...)` / `onTaskOffer(.., {capability})` silently
  dropped every offer.** The handler's capability filter resolved the offer's
  *directed neuron*, but a TASK_OFFER is a broadcast that carries its required
  capabilities in `payload.capabilities` and has no directed neuron  -  so the
  filter always failed and no BID was ever emitted (competitive bidding
  simply timed out). The filter now narrows against the offer's requested capability
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
  `@dendrite.on_agent_output` directly in the app.
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