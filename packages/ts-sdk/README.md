# @cosmonapse/sdk

Event-driven Agent-to-Agent protocol SDK for TypeScript, plus the `cosmo`
developer CLI — the TypeScript surface of the Cosmonapse envelope spec, at
parity with the Python reference implementation.

## Install

```bash
npm install @cosmonapse/sdk        # SDK for your project
npm install -g @cosmonapse/sdk    # also puts the `cosmo` launcher on your PATH
```

Optional peers (lazy-imported, only needed for the backends you use):
`nats`, `kafkajs`, `better-sqlite3`, `pg`, `@modelcontextprotocol/sdk`.

## Quick start

```ts
import { Axon, Dendrite, connectSynapse } from "@cosmonapse/sdk";

const synapse = await connectSynapse("cosmo://127.0.0.1:7070");

// A Neuron is just an async function - zero protocol knowledge.
const worker = new Dendrite({ synapse, namespace: "demo" });
worker.attachAxon(new Axon({
  neuronId: "answerer",
  neuronFn: async (input) => ({ answer: (input as { q: string }).q }),
}));

const orch = new Dendrite({ synapse, namespace: "demo", heartbeatMs: 0 });

await worker.start();
await orch.start();
const sig = await orch.dispatchAndWait({
  neuron: "answerer",
  input: { q: "hi" },
  scope: "terminal",
  timeoutMs: 5000,
});
console.log(sig.payload);
await orch.stop(); await worker.stop(); await synapse.close();
```

Start the dev synapse first:

```bash
cosmo synapse start memory --namespace=demo
```

## The `cosmo` CLI

The CLI has **one implementation**, shipped in the Python package
([`cosmonapse` on PyPI](https://pypi.org/project/cosmonapse/)) — a deliberate
single-build design so pip and npm users can never drift apart. This package's
`cosmo` bin installs and runs it for you:

```bash
npm install -g @cosmonapse/sdk
cosmo --help
```

Resolution order on each run: `$COSMO_PYTHON`; any system Python that can
already `import cosmo`; otherwise a one-time bootstrap that creates a private
venv under `~/.cosmonapse/cli-venv` and pip-installs `cosmonapse` pinned to
this package's version (npm and PyPI releases are cut from the same git tag).
Every path ends in `python -m cosmo`. The only requirement is a Python 3.11+
interpreter on PATH. Commands: `init`, `synapse start|view|stop`, `dispatch`,
`registry list`, `answer`, `schema`, `doppler` (incl. `--prism`), `validate`,
`completion`.

## Building from a checkout

```bash
npm install
npm run typecheck   # tsc --noEmit
npm run test        # node --test via tsx
npm run build       # tsup -> dist/index.{js,cjs,d.ts}
```

## Documentation

- [Envelope spec](https://github.com/Cosmonapse/cosmonapse-core/blob/main/design/ENVELOPE_SPEC.md) — the Signal wire format
- [SDK design](https://github.com/Cosmonapse/cosmonapse-core/blob/main/design/SDK_DESIGN.md) — design rationale
- [Python SDK](https://pypi.org/project/cosmonapse/) — the reference implementation

Apache-2.0
