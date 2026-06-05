# Cosmonapse Quickstart  -  Hugging Face × Round-Robin Neuron

Two Hugging Face Neurons, two Axons, one Synapse, and a third Neuron  - 
`roundrobin`  -  that load-balances. The `roundrobin` Neuron is attached to
an orchestrator Dendrite and hands each incoming prompt to a different
worker in turn by forwarding it over the signal bus. Classic round-robin
load-balancing, expressed as just another Neuron.

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
          │   Orchestrator  │   │   Worker A      │  │   Worker B      │
          │   Dendrite      │   │   Dendrite      │  │   Dendrite      │
          │  └ Axon         │   │  └ Axon         │  │  └ Axon         │
          │    └ Neuron     │   │     └ Neuron    │  │     └ Neuron    │
          │   (roundrobin)  │   │       (HF)      │  │       (HF)      │
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
| `cortex.py`  | Orchestrator Dendrite hosting the `roundrobin` Neuron   | 4       |

Open four terminals  -  one per process.

---

## Step 1  -  Start the Synapse

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
here  -  it doubles as your Doppler.

### Watch it live with Prism

For a richer view, open the **Prism** browser visualization in a second
terminal right after the Synapse is up:

```bash
cosmo doppler --prism --url=cosmo://127.0.0.1:7070 -n quickstart
```

Prism serves a live SPA (default at <http://127.0.0.1:7071>) and opens it in
your browser. As the two Hugging Face workers and the Cortex come online below,
you'll watch every TASK, AGENT_OUTPUT, and FINAL Signal animate across the bus
in real time.

---

## Step 2  -  Worker A (Hugging Face Neuron)

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
    # 1. The Neuron  -  provider-backed async callable, zero protocol knowledge.
    neuron_fn = Neuron(
        source         = "huggingface",
        endpoint       = HF_ENDPOINT,
        model          = HF_MODEL,
        api_key        = os.environ["HF_TOKEN"],
        use_chat_api   = True,    # → /v1/chat/completions
        max_new_tokens = 128,
        temperature    = 0.7,
    )

    # 2. The Axon  -  gives the Neuron an identity on the bus.
    axon = Axon(
        neuron_id    = "hf-worker-a",
        neuron_fn    = neuron_fn,
        capabilities = ["text-generation", "chat"],
        version      = "0.0.1",
    )

    # 3. The Dendrite  -  connects the Axon to the Synapse.
    synapse  = await connect_synapse(SYNAPSE_URL)
    dendrite = Dendrite(
        synapse     = synapse,
        namespace   = NAMESPACE,
        dendrite_id = "worker-a",
    )
    dendrite.attach_axon(axon)

    try:
        async with dendrite:
            print("worker-a ready  (neuron_id=hf-worker-a)   -  Ctrl-C to stop")
            await asyncio.Event().wait()
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
```

Run it:

```bash
python worker_a.py
# worker-a ready  (neuron_id=hf-worker-a)   -  Ctrl-C to stop
```

In the **synapse terminal** you'll see worker A's `REGISTER` go out:

```
REGISTER      hf-worker-a        cosmonapse.quickstart.REGISTER
```

---

## Step 3  -  Worker B (a second Hugging Face Neuron)

`worker_b.py` is the same shape as A  -  different `neuron_id`, and optionally
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
            print("worker-b ready  (neuron_id=hf-worker-b)   -  Ctrl-C to stop")
            await asyncio.Event().wait()
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
```

Run it:

```bash
python worker_b.py
# worker-b ready  (neuron_id=hf-worker-b)   -  Ctrl-C to stop
```

The synapse terminal now shows both workers registered.

---

## Step 4  -  The `roundrobin` Neuron

Round-robin is just a **Neuron**. `RoundRobinNeuron` satisfies the same
`(input, context) -> dict` contract as the workers  -  it simply forwards
each call to the next worker in the pool. Attach it to an orchestrator
Dendrite under the `neuron_id` `"roundrobin"` and anything can dispatch
to it like a leaf worker, never knowing it fans out.

Two ingredients carry the round-robin logic:

1. `itertools.cycle((...))`  -  an infinite iterator that yields the next
   `neuron_id` on every call.
2. `dendrite.dispatch_and_wait(neuron=<id>, input=...)`  -  forwards the
   TASK to that worker over the Synapse and returns its reply Signal.

The Dendrite keeps its default `role="orchestrator"`, so it can both
**host** the `roundrobin` Axon and **dispatch** the forwarded sub-tasks.
`attach_axon` carries no role guard, so the two responsibilities live on
one Dendrite  -  no `trace_id → Future` bookkeeping required; `dispatch_and_wait`
owns the correlation.

`cortex.py`:

```python
import asyncio
import itertools

from cosmonapse import Axon, Dendrite, connect_synapse

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"

# Round-robin pool. Add another worker → add its neuron_id here.
WORKERS = ("hf-worker-a", "hf-worker-b")


class RoundRobinNeuron:
    """A Neuron that load-balances across a worker pool by forwarding."""

    def __init__(self, dendrite: Dendrite, workers: tuple[str, ...]) -> None:
        self._dendrite = dendrite
        self._cycle    = itertools.cycle(workers)

    async def __call__(self, input: dict, context: list) -> dict:
        target = next(self._cycle)               # ← round-robin pick
        print(f"→ forwarding to {target}")
        sig = await self._dendrite.dispatch_and_wait(
            neuron    = target,
            input     = input,
            timeout_s = 60.0,
        )
        return sig.payload.get("output", {})


async def main() -> None:
    synapse  = await connect_synapse(SYNAPSE_URL)
    dendrite = Dendrite(
        synapse     = synapse,
        namespace   = NAMESPACE,
        dendrite_id = "cortex",
    )

    # The router is just a Neuron behind an Axon  -  connected to the Dendrite.
    rr = RoundRobinNeuron(dendrite, WORKERS)
    dendrite.attach_axon(Axon(
        neuron_id    = "roundrobin",
        neuron_fn    = rr,
        capabilities = ["route", "load-balance"],
    ))

    prompts = [
        "Write a one-line haiku about the sun.",
        "Write a one-line haiku about the moon.",
        "Write a one-line haiku about the sea.",
        "Write a one-line haiku about the wind.",
    ]

    try:
        async with dendrite:
            for p in prompts:
                sig = await dendrite.dispatch_and_wait(
                    neuron    = "roundrobin",
                    input     = {"prompt": p},
                    timeout_s = 90.0,
                )
                result = sig.payload.get("output", {})
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
→ forwarding to hf-worker-a
   ← Golden disc ascends  -  silence breaks into light.

→ forwarding to hf-worker-b
   ← Pale lantern in the dark  -  tides remember her face.

→ forwarding to hf-worker-a
   ← Salt sighs against stone, an old song the wind forgot.

→ forwarding to hf-worker-b
   ← Invisible river  -  it bends the wheat into prayer.
```

Notice the alternation:
prompt 1 → A, prompt 2 → B, prompt 3 → A, prompt 4 → B.

In the **synapse terminal** you'll see the full Signal trace for every
prompt  -  each one hits `roundrobin` first, which forwards to a worker:

```
TASK          roundrobin    cosmonapse.quickstart.TASK
TASK          hf-worker-a   cosmonapse.quickstart.TASK
AGENT_OUTPUT  hf-worker-a   cosmonapse.quickstart.AGENT_OUTPUT
AGENT_OUTPUT  roundrobin    cosmonapse.quickstart.AGENT_OUTPUT
TASK          roundrobin    cosmonapse.quickstart.TASK
TASK          hf-worker-b   cosmonapse.quickstart.TASK
AGENT_OUTPUT  hf-worker-b   cosmonapse.quickstart.AGENT_OUTPUT
AGENT_OUTPUT  roundrobin    cosmonapse.quickstart.AGENT_OUTPUT
…
```

---

## What just happened

```
driver  ──TASK(neuron=roundrobin)──▶  Synapse  ──▶  cortex.py
                                                     └─▶ Axon ─▶ RoundRobinNeuron
                                                                 │
                              ──TASK(neuron=hf-worker-a)──▶  Synapse  ──▶  worker_a.py
                                                                           └─▶ Axon ─▶ Neuron(HF) ─▶ HuggingFace
                              ◀──AGENT_OUTPUT───────────────  Synapse  ◀──  worker_a.py
driver  ◀──AGENT_OUTPUT───────────────  Synapse  ◀──  cortex.py
…  (next prompt → hf-worker-b, and so on)
```

Four processes. One Synapse. Three Neurons (two HF workers + `roundrobin`).
Three Axons. The `roundrobin` Neuron never imports `httpx`, never sees a
Hugging Face URL, never touches an API key  -  it just forwards TASK Signals
at neu