# Cosmonapse  -  Road to 1.0.0

This document is the milestone-by-milestone plan from the current public alpha
(**0.1.0**) to a stable **1.0.0**. The theme of the 0.x → 1.0 line is
**stabilisation, not new surface area**: the protocol and SDK shapes are
substantially in place; 1.0 is about making them provably correct, observable in
CI, and trustworthy for third-party implementers.

1.0.0 means: the envelope spec is frozen and machine-checkable, both SDKs are at
parity on the core contract, every wire transport is integration-tested against a
real broker, and the whole thing is enforced by CI on every commit.

Status legend: ☐ not started · ◐ in progress · ☑ done

---

## Where 0.1.0 stands

The protocol is the most mature layer (clear envelope spec, complete 26-type
signal taxonomy, executable validator). The Python SDK is the reference
implementation and is broad and clean (~133 tests, zero TODO/FIXME markers in
`cosmonapse/`). The TypeScript SDK ships as a **preview** with known parity gaps
(`packages/ts-sdk/PORTING_STATUS.md`). The `cosmo` CLI is functional and
appropriately scoped as a developer tool.

The gaps that 1.0 must close, in priority order, are below.

---

## Milestone 0.2.0  -  "Provable" (engineering hygiene)

The single most important gap: nothing currently enforces the tests. Close that
first so every subsequent change lands on a green baseline.

- ☑ GitHub Actions CI running Python tests + ruff + mypy across 3.11/3.12/3.13,
      TS typecheck/build/test, and a spec validator smoke check
      (`.github/workflows/ci.yml`).
- ☐ Make CI required for merge to `main` (branch protection).
- ☐ Add a coverage gate for the Python SDK (target ≥85% on `cosmonapse/`,
      excluding optional broker adapters that need external services).
- ☐ Pin a lockfile for the TS SDK (`package-lock.json`) so `npm ci` is
      reproducible in CI.
- ☐ Reconcile all version references project-wide (done for `DECISIONS.md`;
      audit `README`, examples, and docstrings for stale `v0.x` mentions).

**Exit criteria:** a red build blocks merge; coverage reported on every PR.

---

## Milestone 0.3.0  -  "Frozen contract" (protocol)

Make the spec something a third party can implement against without reading
Python.

- ☐ Publish a **JSON Schema** for the envelope, generated from / checked against
      `envelope.py`, committed to the repo and referenced by `ENVELOPE_SPEC.md`.
- ☐ Add a CI job that validates the schema against a corpus of golden envelopes
      (one valid + one invalid fixture per signal type).
- ☐ Move `ENVELOPE_SPEC.md` from **`1.0.0-draft` / Draft** to **`1.0.0` /
      Stable**; document the compatibility promise (additive-only within a major
      `v`).
- ☐ Cross-language conformance: the same golden-envelope corpus must round-trip
      identically through the Python and TS codecs.

**Exit criteria:** spec is marked Stable; a non-Python implementer has a schema
+ fixtures to build against.

---

## Milestone 0.4.0  -  "Real transports" (reliability)

Today the memory and dev-TCP synapses are well tested; NATS/Kafka/Postgres paths
are not exercised against real services.

- ☐ Integration tests against a real **NATS** broker (CI service container),
      covering addressed + capability-routed (queue-group) delivery.
- ☐ Integration tests against a real **Kafka** broker.
- ☐ Integration tests for `SqliteEngram` and `PostgresEngram` /
      `PostgresRegistryStore` against a real Postgres service container.
- ☐ Document the at-least-once vs once-only delivery semantics per transport,
      with the test that demonstrates each.

**Exit criteria:** every shipped Synapse/Engram/Store backend has a passing
integration test in CI.

---

## Milestone 0.5.0  -  "Parity" (TypeScript SDK)

Promote the TS SDK from preview to first-class by closing the
`PORTING_STATUS.md` gaps that block authoring decentralised workflows.

- ☐ Lifecycle hooks (`on_connect` / `on_refresh` / `on_schedule`)  -  required for
      p2p workflows; currently the headline gap.
- ☐ `connectSynapse(url)` URL factory (parity with `connect_synapse`).
- ☐ `DevSynapse` / dev broker client so TS-first users need no Python process.
- ☐ LLM provider neuron factories (`ollama`, `huggingface`).
- ☐ `SqliteRegistryStore` (+ Postgres where a driver allows).
- ☐ Run the cross-language conformance corpus from 0.3.0 in TS CI.

**Exit criteria:** `PORTING_STATUS.md` "still to port" list is empty or contains
only items explicitly deferred past 1.0 with rationale.

---

## Milestone 1.0.0  -  "Stable"

- ☐ All of the above complete and green in CI.
- ☐ API reference docs generated for both SDKs.
- ☐ A documented deprecation / semver policy for the post-1.0 line.
- ☐ Final pass on examples: every example runs in CI against the memory synapse.
- ☐ Security / dependency audit (`pip-audit`, `npm audit`) wired into CI.
- ☐ Tag `v1.0.0`; publish to PyPI + npm (registry publishing, deferred from the
      0.1.0 git-only release).

**1.0.0 promise:** the envelope `v="1"` contract is stable; breaking changes
require `v="2"`; the Python and TS SDKs are at parity on the core contract;
every transport is integration-tested.

---

## Explicitly out of scope for 1.0

These remain post-1.0 (see `DECISIONS.md` §17): hosted platform / control plane,
a reference router implementation, namespace federation, billing beyond cost
annotation, and a Doppler GUI. 1.0 is about trustworthiness of the existing
surface, not expansion.
