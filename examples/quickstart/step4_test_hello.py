"""
step4_test_hello.py
~~~~~~~~~~~~~~~~~~~
The simplest possible end-to-end smoke test — no HTTP, no Flask.

Dispatches a TASK Signal directly from an orchestrator Dendrite and waits
for the AGENT_OUTPUT to come back from the worker.

Run after synapse (step1) and worker (step2) are both running:

    python step4_test_hello.py

Expected output:
    → dispatching TASK  trace=trc_<…>  neuron=hello-neuron  input={'name': 'Cosmonapse'}
    ✓  result: {'message': 'Hello, Cosmonapse!'}
    ✓  test passed
"""

import asyncio

from cosmonapse import Dendrite, connect_synapse, new_trace_id

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"


async def main() -> None:
    synapse = await connect_synapse(SYNAPSE_URL)

    try:
        result_future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        trace_id = new_trace_id()

        orch = Dendrite(
            synapse     = synapse,
            namespace   = NAMESPACE,
            dendrite_id = "test-orch",
            heartbeat_s = 0,
        )

        @orch.on_agent_output
        async def _on_output(sig):
            if sig.trace_id == trace_id and not result_future.done():
                result_future.set_result(sig.payload.get("output", {}))

        @orch.on_error_signal
        async def _on_error(sig):
            if sig.trace_id == trace_id and not result_future.done():
                result_future.set_exception(
                    RuntimeError(sig.payload.get("message", "error"))
                )

        async with orch:
            input_data = {"name": "Cosmonapse"}
            print(f"→  dispatching TASK  trace={trace_id}  "
                  f"neuron=hello-neuron  input={input_data}")

            await orch.dispatch_task(
                neuron   = "hello-neuron",
                input    = input_data,
                trace_id = trace_id,
            )

            result = await asyncio.wait_for(result_future, timeout=5.0)

        print(f"✓  result: {result}")

        # Assertion
        expected = "Hello, Cosmonapse!"
        assert result.get("message") == expected, (
            f"Expected {expected!r}, got {result.get('message')!r}"
        )
        print("✓  test passed")

    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
