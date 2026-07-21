/**
 * @cosmonapse/sdk
 *
 * TypeScript surface of the Cosmonapse envelope protocol. This entry point
 * re-exports the envelope types/codec and the typed signal builders. It is the
 * 1:1 counterpart to the Python `cosmonapse` package's envelope module.
 *
 * Ported and functional: envelope, builders, MemorySynapse, NatsSynapse,
 * DevSynapse, KafkaSynapse, the RegistryStore (in-memory + sqlite + postgres) +
 * Dendrite registry mirror, LifecycleHooks, connectSynapse, Neuron (MCP /
 * Ollama / HuggingFace), Axon and Dendrite. Any remaining intentional
 * differences are documented in PORTING_STATUS.md.
 */

// `__PKG_VERSION__` is replaced at build time by tsup (see tsup.config.ts)
// with package.json's `version`, which the release workflow sets from the
// `vX.Y.Z` git tag. `typeof` keeps this safe when the source is run
// un-bundled (e.g. tests via tsx), where the define is absent.
declare const __PKG_VERSION__: string | undefined;
export const VERSION: string =
  typeof __PKG_VERSION__ === "string" ? __PKG_VERSION__ : "0.0.0-dev";

export {
  SignalType,
  AXON_TYPES,
  SYNAPSE_TYPES,
  newEventId,
  newTraceId,
  newEngramId,
  createSignal,
  validateSignal,
  normalizeDirected,
  directedTo,
  encode,
  decode,
  reply,
  type Signal,
  type NewSignalInput,
  type Directed,
  type DirectedInput,
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
  taskAwardedSignal,
  taskDeclinedSignal,
  critiqueSignal,
  planSignal,
  thoughtDeltaSignal,
  toolCallSignal,
  toolResultSignal,
  escalationSignal,
  consensusSignal,
  contextSyncSignal,
  discoverSignal,
  recallSignal,
  recalledSignal,
  imprintSignal,
  imprintedSignal,
  stopSignal,
  stoppedSignal,
} from "./signals.js";
export { defaultRetryOn } from "./retry.js";
export type { RetryStrategy, RetryOutcome } from "./retry.js";

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

export { ambientTrace, runWithTraceContext } from "./trace-context.js";

export {
  clarify,
  isClarification,
  permissionRequest,
  isPermissionRequest,
  errorResult,
  isErrorOutput,
  type NeuronFn,
  type NeuronHelpers,
  type CloseableNeuronFn,
  type ContextFetcher,
  type ClarificationOutput,
  type PermissionRequestOutput,
  type ErrorOutput,
} from "./neuron.js";

export {
  Axon,
  AxonHost,
  parseLlmIntents,
  parseMcpIntents,
  COSMO_INTENT_SYSTEM_PROMPT,
  DEFAULT_TOOL_DEADLINE_MS,
  type AxonOptions,
  type AxonExtra,
  type OutputParser,
  type Recogniser,
} from "./axon.js";

export {
  Effector,
  ServedEffector,
  EffectorHost,
  EffectorBinding,
  ToolOutcome,
  EffectorError,
  EffectorTimeout,
  EffectorCancelled,
  EffectorNotBound,
  EffectorOverloaded,
  type EffectorBindingInit,
  type ToolOutcomeInit,
  type InvokeOptions,
  type ToolCallContext,
  type ToolCallHandler,
} from "./effector.js";

export {
  TOOL_STANDARDS,
  extractToolCall,
  parseHermes,
  parseClaude,
  parseCodex,
  type NativeToolCall,
  type ToolCallParser,
} from "./effector-standards.js";

export {
  EffectorClient,
  type EffectorPublisher,
  type ToolCallArgs,
} from "./effector-client.js";

export {
  Dendrite,
  Cortex,
  DendriteProtocolError,
  CortexProtocolError,
  type DendriteOptions,
  type DendriteRole,
  type SignalHandler,
  type HandlerFilter,
} from "./dendrite.js";

export {
  Pathway,
  PathwayClosedError,
  PATHWAY_TYPES,
  TERMINAL_TYPES,
  type PathwayOptions,
  type PathwayRole,
  type PathwayScope,
  type PathwaySignalHandler,
  type PathwayCloseHook,
} from "./pathway.js";

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
  followupPrompt,
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

// --- engram: shared memory ---

export {
  Engram,
  InMemoryEngram,
  EngramBinding,
  EngramError,
  EngramTimeout,
  EngramCancelled,
  EngramNotBound,
  EngramOverloaded,
  deepMerge,
  type Hit,
  type RecallResult,
  type ImprintReceipt,
  type RecallMode,
  type ImprintOp,
  type RecallOptions,
  type ImprintOptions,
  type EngramBindingInit,
  type InMemoryEngramInit,
} from "./engram.js";

export {
  EngramClient,
  type EngramPublisher,
  type RecallCallArgs,
  type ImprintCallArgs,
} from "./engram-client.js";

export { SqliteEngram, type SqliteEngramInit } from "./engram-sqlite.js";
export { PostgresEngram, type PostgresEngramInit } from "./engram-postgres.js";
