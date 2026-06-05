# Integrating an Engram

**Difficulty:** Intermediate · **Primitives:** Engram, EngramBinding, Axon, Dendrite, Synapse

Bind shared memory to a Neuron so it can `recall()` context and `imprint()`
results without ever touching the protocol. The Neuron stays a pure async
function  -  the Axon and Engram do the wire work.

This is the example to read after [`building_a_neuron`](../building_a_neuron/),
once you want a Neuron to remember things across invocations.

## Run

```bash
pip install cosmonapse
python examples/engram_integration/main.py
```

Expected output:

```
[first call ] computed  →  Answer to 'what is the meaning of life?': 42
[second call]    cache  →  Answer to 'what is the meaning of life?': 42
```

The second call returns from cache, proving the imprint from the first call
landed in the Engram and was visible to the recall on the second.

## How the pieces wire together

1. **Engram backend**  -  `InMemoryEngram(engram_id="ctx", engram_kind="context")`
   answers RECALL / IMPRINT signals on the bus. Mounted on a host Dendrite
   via `dendrite.attach_engram(engram)`. Swap in `SqliteEngram(path=...)` or
   `PostgresEngram(dsn=...)` for durable storage  -  same API.

2. **EngramBinding**  -  `EngramBinding(name="ctx", engram_id="ctx")` is the
   declarative wiring stored on the Axon. The Neuron addresses memory by
   the local name (`"ctx"`); the binding translates that to the wire-level
   `engram_id`. Deployments can repoint to a different backend without
   editing Neuron code.

3. **Injected helpers**  -  because the Axon declares `engrams=[...]`, the
   Neuron gets `recall` and `imprint` as keyword-only parameters at call
   time. Under the hood they emit RECALL / IMPRINT under the current
   `trace_id` and await the matching reply.

## Operations

`imprint(name, op=..., entry=...)` supports five operations:

| `op` | Meaning |
|---|---|
| `add` | Store a new entry (fails if `merge_key` already exists). |
| `append` | Append a new entry (always grows). |
| `merge` | Combine with existing entry by `merge_key`. |
| `upsert` | Insert or replace by `merge_key`. |
| `delete` | Remove an entry. |

`recall(name, query=...)` returns a `RecallResult` with a list of `Hit`s.
Recall modes: `first` (default), `merge`, `all`. Configure default mode on
the binding via `EngramBinding(default_recall_mode="merge")`.

## Where to go next

- [`building_a_neuron/`](../building_a_neuron/)  -  the same shape without the Engram.
- [`parallel_build/`](../parallel_build/)  -  multiple Neurons sharing an Engram.
- `design/ENGRAM_DESIGN.md` (repo root)  -  full design rationale and signal semantics.
