# Cosmonapse Quickstart — Hugging Face × Round-Robin Cortex

Two Hugging Face Neurons, two Axons, one Synapse, one Cortex.
The Cortex hands each incoming prompt to a different worker in turn —
classic round-robin load-balancing on the signal bus.

```
                     ┌─────────────────────────────────────────────────┐
                     │                  Synapse                        │
                     │       cosmo://127.0.0.1:7070  ns=quickstart     │
                     └─────────────────────────────────────────────────┘
                       ▲                ▲                ▲
              TASK     │   TASK         │   AGENT_OUTPUT │
         (round-robin) │                │                │
                       │                │                │
          ┌────────────┴────┐   ┌───────┴────────┐  ┌────┴────────────┐
          │   Cortex        │   │   Worker A      │  │   Worker B      │
          │   (Dendrite)    │   │   Dendrite      │  │   Dendrite      │
          │                 │   │  └ Axon         │  │  └ Axon         │
          │  itertools      │   │     └ Neuron    │  │     └ Neuron    │
          │  .cycle()       │   │       (HF)      │  │       (HF)      │
          └─────────────────┘   └─────────────────┘  └─────────────────┘
```

---

## Prerequisites

```bash
# SDK + bundled `cosmo` CLI (gives you `cosmo synapse start`)
pip install -e cosmonapse-core/packages/python-sdk

# HTTP client used by Neuron(source="huggingface")
pip install httpx
```

You also need a **Hugging Face access token**.
Create one at <https://huggingface.co/settings/tokens> (read access is enough),
then export it:

```bash
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## The four files

| File         | Role                                                    | Process |
| ------------ | ------------------------------------------------------- | ------- |
| *(CLI cmd)*  | Start the Synapse (`cosmo synapse start memory`)        | 1       |
| `worker_a.py`| Dendrite + Axon + **Neuron A** (`hf-worker-a`)          | 2       |
| `worker_b.py`| Dendrite + Axon + **Neuron B** (`hf-worker-b`)          | 3       |
| `cortex.py`  | Cortex (Dendrite) that round-robins between A and B     | 4       |

Open four terminals — one per process.

---

## Step 1 — Start the Synapse

```bash
cosmo synapse start memory --namespace=quickstart
```

```
  cosmo synapse start memory
  URL:        cosmo://127.0.0.1:7070
  Namespace:  quickstart
  Transport:  TCP + NDJSON  (single-host dev only)

  Connect a Dendrite or Cortex with:
    await connect_synapse('cosmo://127.0.0.1:7070')

  Ctrl-C  or  cosmo synapse stop  to stop.
  ────────────────────────────────────────────────
```

Leave this terminal open. Every Signal that crosses the bus will be printed
here — it doubles as your Doppler.

---

## Step 2 — Worker A (Hugging Face Neuron)

`worker_a.py`:

```python
import asyncio
import os

from cosmonapse import Axon, Dendrite, Neuron, connect_synapse

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"

# Any OpenAI-compatible Hugging Face endpoint. The SDK appends
# `/v1/chat/completions` for you, so pass the *base* URL only.
#   • Inference Providers router (recommended):  https://router.huggingface.co
#   • Dedicated Inference Endpoint:              https://<your-endpoint>.endpoints.huggingface.cloud
#   • Local TGI / vLLM:                          http://localhost:8080
HF_ENDPOINT = "https://router.huggingface.co"

# Model id goes in the request body. Optional provider suffix pins the
# upstream provider, e.g. ":cerebras", ":groq", ":fastest", ":cheapest".
HF_MODEL    = "meta-llama/Llama-3.1-8B-Instruct"


async def main() -> None:
    # 1. The Neuron — provider-backed async callable, zero protocol knowledge.
    neuron_fn = Neuron(
        source         = "huggingface",
        endpoint       = HF_ENDPOINT,
        model          = HF_MODEL,
        api_key        = os.environ["HF_TOKEN"],
        use_chat_api   = True,    # → /v1/chat/completions
        max_new_tokens = 128,
        temperature    = 0.7,
    )

    # 2. The Axon — gives the Neuron an identity on the bus.
    axon = Axon(
        neuron_id    = "hf-worker-a",
        neuron_fn    = neuron_fn,
        capabilities = ["text-generation", "chat"],
        version      = "0.0.1",
    )

    # 3. The Dendrite — connects the Axon to the Synapse.
    synapse  = await connect_synapse(SYNAPSE_URL)
    dendrite = Dendrite(
        synapse     = synapse,
        namespace   = NAMESPACE,
        dendrite_id = "worker-a",
    )
    dendrite.attach_axon(axon)

    try:
        async with dendrite:
            print("worker-a ready  (neuron_id=hf-worker-a)  — Ctrl-C to stop")
            await asyncio.Event().wait()
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
```

Run it:

```bash
python worker_a.py
# worker-a ready  (neuron_id=hf-worker-a)  — Ctrl-C to stop
```

In the **synapse terminal** you'll see worker A's `REGISTER` go out:

```
REGISTER      hf-worker-a        cosmonapse.quickstart.REGISTER
```

---

## Step 3 — Worker B (a second Hugging Face Neuron)

`worker_b.py` is the same shape as A — different `neuron_id`, and optionally
a different model so you can tell the responses apart:

```python
import asyncio
import os

from cosmonapse import Axon, Dendrite, Neuron, connect_synapse

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"

HF_ENDPOINT = "https://router.huggingface.co"
# Different model than worker_a so you can tell the responses apart.
HF_MODEL    = "Qwen/Qwen2.5-7B-Instruct"


async def main() -> None:
    neuron_fn = Neuron(
        source         = "huggingface",
        endpoint       = HF_ENDPOINT,
        model          = HF_MODEL,
        api_key        = os.environ["HF_TOKEN"],
        use_chat_api   = True,
        max_new_tokens = 128,
        temperature    = 0.7,
    )

    axon = Axon(
        neuron_id    = "hf-worker-b",
        neuron_fn    = neuron_fn,
        capabilities = ["text-generation", "chat"],
        version      = "0.0.1",
    )

    synapse  = await connect_synapse(SYNAPSE_URL)
    dendrite = Dendrite(
        synapse     = synapse,
        namespace   = NAMESPACE,
        dendrite_id = "worker-b",
    )
    dendrite.attach_axon(axon)

    try:
        async with dendrite:
            print("worker-b ready  (neuron_id=hf-worker-b)  — Ctrl-C to stop")
            await asyncio.Event().wait()
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
```

Run it:

```bash
python worker_b.py
# worker-b ready  (neuron_id=hf-worker-b)  — Ctrl-C to stop
```

The synapse terminal now shows both workers registered.

---

## Step 4 — The Cortex (round-robin Dendrite)

The Cortex is a Dendrite with **no Axon of its own**. Its only job is to
dispatch `TASK` Signals and collect `AGENT_OUTPUT` Signals.

Two ingredients carry the round-robin logic:

1. `itertools.cycle((...))` — an infinite iterator that yields the next
   `neuron_id` on every call.
2. `dendrite.dispatch_task(neuron=<id>, input=..., trace_id=...)` — sends
   the TASK to whichever Axon is registered under that `neuron_id`.

A `trace_id → asyncio.Future` map lets `on_agent_output` resolve the
caller that fired the prompt.

`cortex.py`:

```python
import asyncio
import itertools

from cosmonapse import Dendrite, connect_synapse, new_trace_id

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"

# Round-robin pool. Add another worker → add its neuron_id here.
WORKERS = ("hf-worker-a", "hf-worker-b")


class RoundRobinCortex:
    """A Dendrite wrapper that round-robins requests across a worker pool."""

    def __init__(self, dendrite: Dendrite, workers: tuple[str, ...]) -> None:
        self._dendrite = dendrite
        self._cycle    = itertools.cycle(workers)
        self._pending: dict[str, asyncio.Future[dict]] = {}

        @dendrite.on_agent_output
        async def _on_output(sig):
            fut = self._pending.pop(sig.trace_id, None)
            if fut and not fut.done():
                fut.set_result(sig.payload.get("output", {}))

        @dendrite.on_error_signal
        async def _on_error(sig):
            fut = self._pending.pop(sig.trace_id, None)
            if fut and not fut.done():
                fut.set_exception(
                    RuntimeError(sig.payload.get("message", "neuron error"))
                )

    async def ask(self, prompt: str, *, timeout: float = 60.0) -> dict:
        target   = next(self._cycle)           # ← round-robin pick
        trace_id = new_trace_id()
        fut      = asyncio.get_running_loop().create_future()
        self._pending[trace_id] = fut

        await self._dendrite.dispatch_task(
            neuron   = target,
            input    = {"prompt": prompt},
            trace_id = trace_id,
        )
        print(f"→ dispatched to {target}  trace={trace_id[4:12]}")
        return await asyncio.wait_for(fut, timeout=timeout)


async def main() -> None:
    synapse  = await connect_synapse(SYNAPSE_URL)
    dendrite = Dendrite(
        synapse     = synapse,
        namespace   = NAMESPACE,
        dendrite_id = "cortex",
        heartbeat_s = 0,           # the cortex hosts no axons
    )
    cortex = RoundRobinCortex(dendrite, WORKERS)

    prompts = [
        "Write a one-line haiku about the sun.",
        "Write a one-line haiku about the moon.",
        "Write a one-line haiku about the sea.",
        "Write a one-line haiku about the wind.",
    ]

    try:
        async with dendrite:
            for p in prompts:
                result = await cortex.ask(p)
                print(f"   ← {result.get('response', '').strip()}\n")
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
```

Run it:

```bash
python cortex.py
```

Output:

```
→ dispatched to hf-worker-a  trace=a3f2c1d8
   ← Golden disc ascends — silence breaks into light.

→ dispatched to hf-worker-b  trace=7b1e0942
   ← Pale lantern in the dark — tides remember her face.

→ dispatched to hf-worker-a  trace=11ce88a4
   ← Salt sighs against stone, an old song the wind forgot.

→ dispatched to hf-worker-b  trace=92aa5b30
   ← Invisible river — it bends the wheat into prayer.
```

Notice the alternation:
prompt 1 → A, prompt 2 → B, prompt 3 → A, prompt 4 → B.

In the **synapse terminal** you'll see the full Signal trace for every
prompt:

```
TASK          hf-worker-a   cosmonapse.quickstart.TASK
AGENT_OUTPUT  hf-worker-a   cosmonapse.quickstart.AGENT_OUTPUT
TASK          hf-worker-b   cosmonapse.quickstart.TASK
AGENT_OUTPUT  hf-worker-b   cosmonapse.quickstart.AGENT_OUTPUT
…
```

---

## What just happened

```
cortex.py  ──TASK(neuron=hf-worker-a)──▶  Synapse  ──▶  worker_a.py
                                                         └─▶ Axon ─▶ Neuron(HF) ─▶ HuggingFace
cortex.py  ◀──AGENT_OUTPUT───────────────  Synapse  ◀──  worker_a.py
cortex.py  ──TASK(neuron=hf-worker-b)──▶  Synapse  ──▶  worker_b.py
                                                         └─▶ Axon ─▶ Neuron(HF) ─▶ HuggingFace
cortex.py  ◀──AGENT_OUTPUT───────────────  Synapse  ◀──  worker_b.py
…
```

Four processes. One Synapse. Two Neurons. Two Axons. One Cortex.
The Cortex never imports `httpx`, never sees a Hugging Face URL, never
touches an API key — it just emits TASK Signals at neuron IDs. The
workers are interchangeable: kill `worker_b.py` and add `worker_c.py`,
update `WORKERS` in `cortex.py`, and you've rebalanced the pool.

---

## Extending the example

**More workers.** Add `hf-worker-c`, `hf-worker-d`, … and extend the
`WORKERS` tuple in `cortex.py`. `itertools.cycle` handles any length.

**Weighted round-robin.** Replace `itertools.cycle(WORKERS)` with a
custom generator, e.g. `cycle(["a", "a", "b"])` to send 2-of-3 to A.

**Capability-based routing.** Drop the static tuple and ask the
registry instead. Pass `registry_store=MemoryRegistryStore()` to the
Cortex's Dendrite and call:

```python
neurons = await dendrite.find_neurons(capability="chat")
target  = neurons[next(self._cycle_idx) % len(neurons)].neuron_id
```

The Cortex now round-robins across whatever's currently online with
the `"chat"` capability — workers can join and leave at runtime.

**Production transport.** Swap `cosmo://127.0.0.1:7070` for
`nats://localhost:4222` everywhere. Worker, Cortex, and Neuron code
are unchanged — only the synapse URL moves.
