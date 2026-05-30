/**
 * @cosmonapse/sdk
 *
 * TypeScript surface of the Cosmonapse envelope protocol. This entry point
 * re-exports the envelope types/codec and the typed signal builders. It is the
 * 1:1 counterpart to the Python `cosmonapse` package's envelope module.
 *
 * Status: v0.2 — envelope, builders, MemorySynapse, NatsSynapse, the
 * RegistryStore (in-memory) + Dendrite registry mirror, Neuron, Axon and
 * Dendrite are ported and functional. The remaining parity gaps (Kafka adapter,
 * sqlite/postgres RegistryStore backends, provider-backed Neuron factories,
 * DevSynapse, connectSynapse, LifecycleHooks) are tracked — with Python
 * references and status — in PORTING_STATUS.md, not in scattered source
 * comments.
 */

export const VERSION = "0.0.1";

export {
  SignalType,
  AXON_TYPES,
  SYNAPSE_TYPES,
  newEventId,
  newTraceId,
  createSignal,
  validateSignal,
  encode,
  decode,
  reply,
  type Signal,
  type NewSignalInput,
  type Json,
} from "./envelope.js";

export {
  taskSignal,
  agentOutputSignal,
  clarificationSignal,
  finalSignal,
  errorSignal,
  registerSignal,
  deregisterSignal,
  heartbeatSignal,
  memoryAppendSignal,
  taskOfferSignal,
  bidSignal,
  critiqueSignal,
} from "./signals.js";

// --- runtime primitives ---

export {
  MemorySynapse,
  type Synapse,
  type Subscription,
  type MessageHandler,
  type SubscribeOptions,
  type RequestOptions,
} from "./synapse.js";

export { NatsSynapse, type NatsSynapseOptions } from "./synapse-nats.js";

export {
  MemoryRegistryStore,
  neuronRecord,
  type RegistryStore,
  type NeuronRecord,
  type NeuronRecordInit,
  type NeuronStatus,
  type ListOptions,
} from "./storage.js";

export {
  clarify,
  isClarification,
  type NeuronFn,
  type ContextFetcher,
  type ClarificationOutput,
} from "./neuron.js";

export { Axon, type AxonOptions } from "./axon.js";

export {
  Dendrite,
  Cortex,
  DendriteProtocolError,
  CortexProtocolError,
  type DendriteOptions,
  type SignalHandler,
} from "./dendrite.js";

// --- neuron sources: wrap anything that interacts with the real world ---
//
// Prefer the unified `neuron(source, opts)` factory below in application code.
// The standalone `expressNeuron` / `mcpNeuron` exports are the lower-level
// primitives it delegates to — use them only when you want one source's exact
// option type or to tree-shake the other. See neuron-factory.ts for guidance.

export {
  expressNeuron,
  type ExpressNeuronOptions,
  type CloseableNeuronFn,
} from "./neuron-express.js";

export {
  mcpNeuron,
  standardMcpServers,
  type McpNeuronOptions,
} from "./neuron-mcp.js";

/** Recommended entry point for building Neurons (see note above). */
export { neuron, type NeuronSource, type ExpressSourceOptions } from "./neuron-factory.js";
