/**
 * @cosmonapse/sdk  -  cognition  -  REMOVED.
 *
 * The CognitionClient (a blocking caller-side correlation table for
 * ask()/requestPermission()) was intentionally removed: it duplicated the
 * Engram round-trip machinery and added per-Dendrite state for no gain.
 *
 * The replacement needs no client. Clarification and permission ride the same
 * return-marker + Engram recall/imprint flow the developer wires:
 *
 *   1. The Neuron tries recall first (check the Engram for a standing answer /
 *      grant).
 *   2. On a miss it returns a marker - clarify(...) or permissionRequest(...) -
 *      which the Axon turns into a CLARIFICATION / PERMISSION signal.
 *   3. A Cortex (centralised) or any peer (decentralised) handles it via
 *      onClarification / onPermission, then either imprints the decision and/or
 *      sends it back via respondToPermission (re-dispatch TASK) or the discrete
 *      grantPermission / denyPermission / answerClarification emit helpers.
 *
 * This module is intentionally empty. Delete the file.
 */
export {};
