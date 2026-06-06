# Cosmonapse 0.1.1

**Incremental release.** A follow-up to 0.1.0 that adds first-class LLM provider
Neurons to both SDKs and removes the HTTP-app Neuron type, sharpening the
conceptual model ahead of the stabilisation path to 1.0.0 in
[`ROADMAP.md`](./ROADMAP.md).

## New LLM provider Neurons (both SDKs)

The `Neuron(source=...)` factory gains several providers, so more models slot
into an Axon behind the uniform `async (input, context) -> dict` signature with
no protocol knowledge:

- **`"openai"`**  -  OpenAI Chat Completions, via `httpx` directly (no `openai`
  SDK dependency). Key from `api_key=` or `OPENAI_API_KEY`.
- **`"anthropic"`**  -  Anthropic Messages API. Key from `api_key=` or
  `ANTHROPIC_API_KEY`.
- **`"groq"`, `"openrouter"`, `"together"`, `"mistral"`**  -  OpenAI-compatible
  hosted providers, pre-configured on the existing HuggingFace neuron.

All are soft dependencies (lazy-imported, nothing added to the core install) and
return the standard `{"response": "<text>", "meta": <raw payload>}` shape.

```python
from cosmonapse import Axon, Neuron

Axon(neuron_id="chat", neuron_fn=Neuron(source="openai", model="gpt-4o-mini"))
Axon(neuron_id="chat", neuron_fn=Neuron(source="anthropic", model="claude-3-5-sonnet-latest"))
```

Both SDKs ship these at parity. In TypeScript the same sources are available through `neuron(source, opts)` (and the standalone `openaiNeuron` / `anthropicNeuron` exports), backed by the runtime `fetch`.

```ts
import { Axon, neuron } from "@cosmonapse/sdk";

new Axon({ neuronId: "chat", neuronFn: neuron("openai", { model: "gpt-4o-mini" }) });
new Axon({ neuronId: "chat", neuronFn: neuron("anthropic", { model: "claude-3-5-sonnet-latest" }) });
```

## What changed

- **Removed the HTTP-app Neuron type** in both SDKs:
  `Neuron(source="flask" | "wsgi" | "api")` and the `cosmonapse._neuron_http`
  module (Python), plus `expressNeuron` and the
  `neuron("express" | "http" | "api", …)` factory sources (TypeScript). An HTTP
  API is not a Neuron  -  a web app is an inbound request handler, not an
  `input -> output` worker.
- **Dropped the `[flask]` optional dependency** from the Python SDK.
- **Moved the shared `CloseableNeuronFn` type** from `neuron-express.ts` to
  `neuron.ts` in the TS SDK.

## Migration

The supported pattern is the reverse of the removed type: keep your web
framework (Flask, Express, …) on the outside as an HTTP boundary and dispatch
TASK Signals from its route handlers via an orchestrator Dendrite, wiring
`@dendrite.on_agent_output` directly in the app. The `neuron_real_world`
example and the quickstart now show this.

If you previously created a Neuron with `source="flask" | "wsgi" | "api"` (or
the TS `expressNeuron` / `neuron("express" | "http" | "api", …)`), move that web
framework outside the Neuron model and dispatch from its handlers instead. If
you depended on the `[flask]` extra, install Flask directly as a regular
dependency of your app.

See [`CHANGELOG.md`](./CHANGELOG.md) for the full itemised list.

## Install

```bash
pip install cosmonapse        # Python SDK + `cosmo` CLI (once published to PyPI)
npm install @cosmonapse/sdk   # TypeScript SDK (once published to npm)
```

For a git-only install, install from source:

```bash
pip install -e packages/python-sdk
```

---

## Pre-release checklist (git/GitHub)

Run through this before tagging.

**Verify the build is sound**

- [ ] `cd packages/python-sdk && pip install -e ".[dev]"`
- [ ] `ruff check .`  -  clean
- [ ] `mypy cosmonapse`  -  clean
- [ ] `pytest -q`  -  all green
- [ ] `cd packages/ts-sdk && npm install && npm run typecheck && npm run build && npm test`  -  all green
- [ ] `cosmo validate` runs and the examples under `examples/` execute against the memory synapse

**Confirm metadata is consistent**

- [ ] No stray uncommitted edits (the working tree is clean)
- [ ] `packages/ts-sdk/package.json` version = `0.1.1`
- [ ] `CHANGELOG.md` top dated entry = `[0.1.1]` with today's date
- [ ] `LICENSE` present (MIT) and referenced in both package manifests
- [ ] `README.md` quickstart runs as written

**Tag and publish the GitHub release**

```bash
git add -A
git commit -m "Release 0.1.1"
git tag -a v0.1.1 -m "Cosmonapse 0.1.1  -  OpenAI/Anthropic neurons; remove the HTTP-app Neuron type"
git push origin main --follow-tags
```

The Python version is derived from the `v0.1.1` tag at build time (hatch-vcs),
and the `Release` CI workflow is triggered by the `v*` tag push. Then create the
GitHub Release from tag `v0.1.1`, pasting this file's body above the checklist as
the release description.
