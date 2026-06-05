/**
 * @cosmonapse/sdk  -  synapse URL factory
 *
 * Synapse URL -> Synapse factory and connector. Ported 1:1 from
 * `cosmonapse._url`.
 *
 * A Dendrite (or Cortex) does NOT own the Synapse. It uses a Synapse that the
 * caller has built and connected. The caller is also responsible for closing it.
 *
 *   cosmo://host:port   -> DevSynapse (local dev / `cosmo dev synapse`)
 *   nats://host:port    -> NatsSynapse
 *   kafka://host:port   -> KafkaSynapse
 *
 * For the in-process MemorySynapse, construct it directly  -  a URL would be
 * ambiguous across processes.
 */

import { DevSynapse } from "./synapse-dev.js";
import { NatsSynapse } from "./synapse-nats.js";
import { KafkaSynapse } from "./synapse-kafka.js";
import type { Synapse } from "./synapse.js";

/** Build (but do not connect) a Synapse from a Cosmonapse synapse URL. */
export function synapseFromUrl(url: string): Synapse {
  const parsed = new URL(url);
  const scheme = parsed.protocol.replace(/:$/, "").toLowerCase();

  switch (scheme) {
    case "cosmo":
      return new DevSynapse({
        host: parsed.hostname || "127.0.0.1",
        port: parsed.port ? Number(parsed.port) : 7070,
      });
    case "nats":
      return new NatsSynapse({ url });
    case "kafka": {
      const host = parsed.hostname || "localhost";
      const port = parsed.port || "9092";
      return new KafkaSynapse({ bootstrapServers: `${host}:${port}` });
    }
    default:
      throw new Error(
        `Unknown synapse URL scheme '${scheme}'. ` +
          `Expected one of: cosmo, nats, kafka. ` +
          `For in-process MemorySynapse, instantiate it directly.`,
      );
  }
}

/**
 * Build a Synapse from `url` and `.connect()` it. Return the connected Synapse
 * so the caller can pass it to Dendrites / Cortices and close it when finished:
 *
 * ```ts
 * const synapse = await connectSynapse("cosmo://127.0.0.1:7070");
 * try {
 *   const dendrite = new Dendrite({ synapse, registryStore });
 *   await dendrite.start();
 *   // ...
 * } finally {
 *   await synapse.close();
 * }
 * ```
 *
 * Multiple Dendrites and Cortices can share the same Synapse instance. Closing
 * the Synapse is the caller's responsibility  -  no component will close it.
 */
export async function connectSynapse(url: string): Promise<Synapse> {
  const synapse = synapseFromUrl(url);
  await synapse.connect();
  return synapse;
}
