"""
examples/parallel_build/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"Build a website"  -  one task, multiple Neurons working in parallel.

This is the core Cosmonapse use case: a single high-level task fans out
across a team of specialised Neurons. Each Neuron writes its output to
the shared Engram. Downstream Neurons read from the Engram.
The Cortex coordinates everything, emitting FINAL when complete.

Architecture (new vocabulary)
-----------------------------

    [trigger]
        │ TASK: build_website
        ▼
    ┌──────────────────────────────────────┐
    │              Cortex                  │
    │   (Dendrite + orchestration logic)   │
    └──────────────┬───────────────────────┘
                   │ dispatches parallel TASKs onto the Synapse
                   ▼
            ┌──────────────────┐
            │  one Dendrite    │  (hosts every Axon in this example
            │  per process     │   for brevity  -  in production each
            └────────┬─────────┘   Neuron has its own Dendrite)
                     │
            ┌────────┴──────────────────────────────┐
            ▼          ▼          ▼        ▼   ▼   ▼
         design     arch      frontend   backend qa devops
         Axon →     Axon →     Axon →      …
         Neuron     Neuron     Neuron

Each Axon turns its Neuron's return value into an AGENT_OUTPUT and hands
it to the Dendrite, which publishes on the Synapse. The Cortex sees
each AGENT_OUTPUT and decides what to do next.

Run it:
    cd packages/python-sdk
    pip install -e ".[dev]"
    python ../../examples/parallel_build/main.py
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from cosmonapse import (
    Axon,
    Cortex,
    Dendrite,
    MemoryRegistryStore,
    MemorySynapse,
    Signal,
    new_trace_id,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("cortex")


# ---------------------------------------------------------------------------
# Neurons  -  pure functions, zero protocol knowledge
# ---------------------------------------------------------------------------


async def design_neuron(input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
    logger.info("[DesignNeuron] designing for: %s", input.get("brief"))
    await asyncio.sleep(0.05)
    return {
        "color_palette": ["#0F172A", "#6366F1", "#FFFFFF"],
        "typography": "Inter, sans-serif",
        "layout": "hero + 3-column features + CTA footer",
        "components": ["Navbar", "HeroSection", "FeatureGrid", "ContactForm", "Footer"],
        "responsive": True,
    }


async def arch_neuron(input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
    logger.info("[ArchNeuron] designing architecture for: %s", input.get("brief"))
    await asyncio.sleep(0.06)
    return {
        "stack": {"frontend": "Next.js 14", "backend": "FastAPI", "db": "Postgres"},
        "hosting": "Vercel (frontend) + Railway (backend)",
        "auth": "Clerk",
        "api_style": "REST + OpenAPI",
        "cdn": "Cloudflare",
    }


async def frontend_neuron(input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
    design = context[0] if context else {}
    logger.info(
        "[FrontendNeuron] implementing design: %s components",
        len(design.get("components", [])),
    )
    await asyncio.sleep(0.08)
    return {
        "pages": ["app/page.tsx", "app/layout.tsx"],
        "components": [f"components/{c}.tsx" for c in design.get("components", [])],
        "styles": "Tailwind CSS with custom theme tokens",
        "state": "Zustand",
        "status": "scaffold complete",
    }


async def backend_neuron(input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
    arch = context[0] if context else {}
    logger.info(
        "[BackendNeuron] implementing %s stack",
        arch.get("stack", {}).get("backend"),
    )
    await asyncio.sleep(0.09)
    return {
        "routes": [
            "GET /api/health",
            "POST /api/contact",
            "GET /api/features",
        ],
        "models": ["User", "ContactSubmission"],
        "migrations": ["001_init.sql"],
        "auth_middleware": "Clerk JWT validation",
        "status": "scaffold complete",
    }


async def qa_neuron(input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
    logger.info("[QANeuron] running tests")
    await asyncio.sleep(0.04)
    return {
        "tests_run": 12,
        "tests_passed": 12,
        "coverage": "84%",
        "accessibility": "WCAG AA",
        "performance_score": 91,
        "status": "all checks green",
    }


async def devops_neuron(input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
    logger.info("[DevOpsNeuron] deploying")
    await asyncio.sleep(0.05)
    return {
        "frontend_url": "https://my-site.vercel.app",
        "backend_url": "https://my-api.railway.app",
        "preview_url": "https://my-site-git-main.vercel.app",
        "status": "deployed",
    }


# ---------------------------------------------------------------------------
# Simple in-memory Engram (shared context store)
# ---------------------------------------------------------------------------


class Engram:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def write(self, key: str, value: Any) -> None:
        self._store[key] = value
        logger.info("[Engram] wrote key=%r", key)

    def read(self, key: str) -> Any:
        return self._store.get(key)


def make_context_fetcher(engram: Engram):
    async def fetch(ref: str) -> list[Any]:
        value = engram.read(ref)
        return [value] if value is not None else []
    return fetch


# ---------------------------------------------------------------------------
# Workflow  -  built on top of the Cortex
# ---------------------------------------------------------------------------


class BuildSiteWorkflow:
    """
    The developer's workflow logic. The Cortex provides the primitives;
    this class wires them into the multi-phase build pipeline.
    """

    def __init__(self, cortex: Cortex, engram: Engram) -> None:
        self.cortex = cortex
        self.engram = engram
        self.trace_id = new_trace_id()

        self._phase1: dict[str, Signal] = {}
        self._phase2: dict[str, Signal] = {}
        self._phase3: Signal | None = None
        self._phase4: Signal | None = None

        self._p1_done = asyncio.Event()
        self._p2_done = asyncio.Event()
        self._p3_done = asyncio.Event()
        self._p4_done = asyncio.Event()

        # Hook the Cortex
        cortex.on_agent_output(self._on_agent_output)

    async def _on_agent_output(self, sig: Signal) -> None:
        neuron = sig.neuron or "unknown"
        logger.info("Cortex ← AGENT_OUTPUT from %s", neuron)

        if neuron in ("design-neuron", "arch-neuron"):
            self._phase1[neuron] = sig
            if len(self._phase1) == 2:
                self._p1_done.set()
        elif neuron in ("frontend-neuron", "backend-neuron"):
            self._phase2[neuron] = sig
            if len(self._phase2) == 2:
                self._p2_done.set()
        elif neuron == "qa-neuron":
            self._phase3 = sig
            self._p3_done.set()
        elif neuron == "devops-neuron":
            self._phase4 = sig
            self._p4_done.set()

    async def _dispatch(
        self,
        neuron: str,
        input_data: dict[str, Any],
        context_ref: str | None = None,
    ) -> None:
        await self.cortex.dispatch_task(
            neuron=neuron,
            input=input_data,
            trace_id=self.trace_id,
            context_ref=context_ref,
        )
        logger.info("Cortex → TASK to %s (trace=%s)", neuron, self.trace_id[:16])

    async def run(self, brief: str) -> dict[str, Any]:
        # ── Phase 1: Design + Architecture (parallel) ────────────────────
        logger.info("═══ Phase 1: Design + Architecture (parallel) ═══")
        await asyncio.gather(
            self._dispatch("design-neuron", {"brief": brief}),
            self._dispatch("arch-neuron", {"brief": brief}),
        )
        await self._p1_done.wait()

        design_out = self._phase1["design-neuron"].payload["output"]
        arch_out = self._phase1["arch-neuron"].payload["output"]
        self.engram.write("design_spec", design_out)
        self.engram.write("arch_spec", arch_out)

        # ── Phase 2: Frontend + Backend (parallel) ───────────────────────
        logger.info("═══ Phase 2: Frontend + Backend (parallel) ═══")
        await asyncio.gather(
            self._dispatch("frontend-neuron", {"brief": brief},
                           context_ref="design_spec"),
            self._dispatch("backend-neuron", {"brief": brief},
                           context_ref="arch_spec"),
        )
        await self._p2_done.wait()

        frontend_out = self._phase2["frontend-neuron"].payload["output"]
        backend_out = self._phase2["backend-neuron"].payload["output"]
        self.engram.write("frontend_scaffold", frontend_out)
        self.engram.write("backend_scaffold", backend_out)

        # ── Phase 3: QA ───────────────────────────────────────────────────
        logger.info("═══ Phase 3: QA ═══")
        await self._dispatch("qa-neuron", {"brief": brief})
        await self._p3_done.wait()
        qa_out = self._phase3.payload["output"]                       # type: ignore[union-attr]
        self.engram.write("qa_report", qa_out)

        # ── Phase 4: Deploy ───────────────────────────────────────────────
        logger.info("═══ Phase 4: DevOps ═══")
        await self._dispatch("devops-neuron", {"brief": brief})
        await self._p4_done.wait()
        devops_out = self._phase4.payload["output"]                   # type: ignore[union-attr]
        self.engram.write("deployment", devops_out)

        # ── FINAL ─────────────────────────────────────────────────────────
        result = {
            "design": design_out,
            "architecture": arch_out,
            "frontend": frontend_out,
            "backend": backend_out,
            "qa": qa_out,
            "deployment": devops_out,
            "status": "complete",
        }
        await self.cortex.emit_final(
            trace_id=self.trace_id,
            parent_id=self._phase4.id,                                # type: ignore[union-attr]
            result=result,
        )
        logger.info("Cortex → FINAL (trace=%s)", self.trace_id[:16])
        return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    synapse = MemorySynapse()
    await synapse.connect()

    engram = Engram()
    fetch = make_context_fetcher(engram)

    # All Axons for this team  -  in production each lives in its own process.
    axons = [
        Axon(neuron_id="design-neuron",   neuron_fn=design_neuron,
             capabilities=["ui-design", "ux"],          context_fetcher=fetch),
        Axon(neuron_id="arch-neuron",     neuron_fn=arch_neuron,
             capabilities=["system-architecture"],      context_fetcher=fetch),
        Axon(neuron_id="frontend-neuron", neuron_fn=frontend_neuron,
             capabilities=["frontend", "react", "nextjs"], context_fetcher=fetch),
        Axon(neuron_id="backend-neuron",  neuron_fn=backend_neuron,
             capabilities=["backend", "api", "fastapi"],   context_fetcher=fetch),
        Axon(neuron_id="qa-neuron",       neuron_fn=qa_neuron,
             capabilities=["testing", "qa", "accessibility"], context_fetcher=fetch),
        Axon(neuron_id="devops-neuron",   neuron_fn=devops_neuron,
             capabilities=["deployment", "ci-cd", "devops"],  context_fetcher=fetch),
    ]

    # A Dendrite hosts the Axons that serve the team's work.
    # In production each Axon typically lives in its own process and the
    # Dendrite is built via `await Dendrite.connect("cosmo://...", registry_store=...)`.
    worker_dendrite = Dendrite(synapse=synapse, namespace="default")
    for axon in axons:
        worker_dendrite.attach_axon(axon)

    # A Cortex on the same namespace drives the workflow.
    cortex = Cortex(synapse=synapse, registry_store=MemoryRegistryStore(),
                    namespace="default", dendrite_id="build-site-cortex")

    print("\n" + "═" * 60)
    print("  Cosmonapse  -  parallel_build example")
    print("  Brief: 'Build a SaaS landing page for a dev tool'")
    print("═" * 60 + "\n")

    async with worker_dendrite, cortex:
        workflow = BuildSiteWorkflow(cortex, engram)
        result = await workflow.run("Build a SaaS landing page for a dev tool")

    print("\n" + "=" * 60)
    print("  FINAL RESULT")
    print("=" * 60)
    print(json.dumps(result, indent=2))

    await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
