# Cosmonapse 0.1.0

**First public release.** An event-driven Agent-to-Agent (A2A) protocol with a
Python SDK (reference implementation), a TypeScript SDK at full parity, and the
`cosmo` developer CLI.

This is an early alpha aimed at early adopters who want full control over
multi-agent message flow. The protocol surface is substantial; both SDKs ship at
feature parity. The stabilisation path to a frozen 1.0.0 is in
[`ROADMAP.md`](./ROADMAP.md).

## Highlights

- **Protocol** — a minimal, extensible envelope (`ENVELOPE_SPEC.md`) with a
  complete 26-type signal taxonomy across lifecycle, routing, cognition, memory,
  coordination, management, and discovery. `v="1"` wire contract.
- **Neuron / Axon / Dendrite model** — agents are pure functions (Neurons),
  wrapped by Axons into protocol-valid Signals, attached to Dendrites that own
  the wire. No central orchestrator class — any Dendrite can dispatch and react.
- **Engram** — shared-memory subsystem with `InMemory`, `Sqlite`, and `Postgres`
  backends and `RECALL` / `IMPRINT` wire types.
- **Pathway** — per-trace event handles supporting three consumption shapes on
  one primitive: `await pw.wait()`, `@pw.on(...)`, and `async for sig in pw`.
- **Capability-routed dispatch** and **competitive bidding**
  (`TASK_OFFER` / `BID` / `TASK_AWARDED`) with `first_bid` / `lowest_cost` /
  `highest_confidence` selection.
- **Transports** — in-memory, local dev TCP broker, NATS, and Kafka synapses.
- **`cosmo` CLI** — `init`, `synapse`, `doppler`, `validate`, `completion`.
- **TypeScript SDK** (`@cosmonapse/sdk`) — envelope/codec, signal builders,
  Memory + NATS + Dev + Kafka synapses, URL factory, sqlite/postgres registry
  stores, LifecycleHooks, Neuron/Axon/Dendrite, Express / MCP / Ollama /
  HuggingFace / unified neuron factories. All parity gaps from the Python
  reference are closed; intentional differences are documented in
  `packages/ts-sdk/PORTING_STATUS.md`.

See [`CHANGELOG.md`](./CHANGELOG.md) for the full itemised list.

## Known limitations

- NATS / Kafka / Postgres code paths are unit-tested but not yet
  integration-tested against live brokers in CI (planned for 0.4.0).
- The envelope spec is still labelled `1.0.0-draft`; no machine-readable JSON
  Schema is published yet (planned for 0.3.0).

## Install

```bash
pip install cosmonapse        # Python SDK + `cosmo` CLI (once published to PyPI)
npm install @cosmonapse/sdk   # TypeScript SDK (once published to npm)
```

For this git-only release, install from source:

```bash
pip install -e packages/python-sdk
```

---

## Pre-release checklist (git/GitHub)

Run through this before tagging. Registry publishing (PyPI/npm) is deferred to
1.0.0 per the roadmap.

**Verify the build is sound**

- [ ] `cd packages/python-sdk && pip install -e ".[dev]"`
- [ ] `ruff check .` — clean
- [ ] `mypy cosmonapse` — clean
- [ ] `pytest -q` — all green (≈133 tests)
- [ ] `cd packages/ts-sdk && npm install && npm run typecheck && npm run build && npm test` — all green
- [ ] `cosmo validate` runs and the examples under `examples/` execute against the memory synapse

**Confirm metadata is consistent**

- [ ] `packages/python-sdk/pyproject.toml` version = `0.1.0`
- [ ] `packages/ts-sdk/package.json` version = `0.1.0`
- [ ] `CHANGELOG.md` top entry = `[0.1.0]` with today's date
- [ ] `DECISIONS.md` roadmap reflects 0.1.0 (reconciled)
- [ ] `LICENSE` present (MIT) and referenced in both package manifests
- [ ] `README.md` quickstart runs as written

**Tag and publish the GitHub release**

```bash
git add -A
git commit -m "Release 0.1.0"
git tag -a v0.1.0 -m "Cosmonapse 0.1.0 — first public release"
git push origin main --follow-tags
```

Then create the GitHub Release from tag `v0.1.0`, pasting this file's body above
the checklist as the release description.

**Immediately after release**

- [ ] Open tracking issues for each `ROADMAP.md` milestone item.
- [ ] Enable branch protection requiring the new CI workflow on `main`.
