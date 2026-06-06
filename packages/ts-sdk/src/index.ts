/**
 * @cosmonapse/sdk
 *
 * TypeScript surface of the Cosmonapse envelope protocol. This entry point
 * re-exports the envelope types/codec and the typed signal builders. It is the
 * 1:1 counterpart to the Python `cosmonapse` package's envelope module.
 *
 * Status: v0.3  -  envelope, builders, MemorySynapse, NatsSynapse, DevSynapse,
 * KafkaSynapse, the RegistryStore (in-memory + sqlite + postgres) + Dendrite
 * registry mirror, LifecycleHooks, connectSynapse, Neuron (MCP / Ollama /
 * HuggingFace), Axon and Dendrite are ported and functional. Any remaining
 * intentional differences are documented in PORTING_STATUS.md.
 */

export const VERSION = "0.1.0";

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
  clarificationAnswerSignal,
  permissionSignal,
  permissionDecisionSignal,
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
  DevSynapse,
  DevSynapseServer,
  type DevSynapseOptions,
  type DevSynapseServerOptions,
} from "./synapse-dev.js";

export { KafkaSynapse, type KafkaSynapseOptions } from "./synapse-kafka.js";

export { synapseFromUrl, connectSynapse } from "./url.js";

export {
  MemoryRegistryStore,
  neuronRecord,
  type RegistryStore,
  type NeuronRecord,
  type NeuronRecordInit,
  type NeuronStatus,
  type ListOptions,
} from "./storage.js";

export { SqliteRegistryStore } from "./storage-sqlite.js";
export { PostgresRegistryStore, type PostgresRegistryStoreOptions } from "./storage-postgres.js";

export {
  LifecycleHooks,
  type RefreshEvent,
  type ConnectHook,
  type RefreshHook,
  type ScheduleHook,
} from "./hooks.js";

export {
  clarify,
  isClarification,
  permissionRequest,
  isPermissionRequest,
  type NeuronFn,
  type CloseableNeuronFn,
  type ContextFetcher,
  type ClarificationOutput,
  type PermissionRequestOutput,
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
// The standalone `mcpNeuron` / `ollamaNeuron` / `huggingFaceNeuron` exports are
// the lower-level primitives it delegates to  -  use them only when you want one
// source's exact option type or to tree-shake the others. See neuron-factory.ts.
//
// NOTE  -  there is no Express / HTTP / API neuron source. An HTTP API is not a
// Neuron; front an orchestrator Dendrite with your web framework instead.

export {
  mcpNeuron,
  standardMcpServers,
  type McpNeuronOptions,
} from "./neuron-mcp.js";

export {
  ollamaNeuron,
  huggingFaceNeuron,
  type OllamaNeuronOptions,
  type HuggingFaceNeuronOptions,
} from "./neuron-http.js";

export {
  openaiNeuron,
  anthropicNeuron,
  type OpenAINeuronOptions,
  type AnthropicNeuronOptions,
} from "./neuron-openai.js";

/** Recommended entry point for building Neurons (see note above). */
export {
  neuron,
  type NeuronSource,
  type OpenAICompatNeuronOptions,
} from "./neuron-factory.js";
