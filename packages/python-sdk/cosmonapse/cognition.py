"""
cosmonapse.cognition  -  REMOVED.

The CognitionClient (a blocking caller-side correlation table for
ask(...)/request_permission(...)) was intentionally removed: it duplicated
the Engram round-trip machinery and added per-Dendrite state for no gain.

The replacement needs no client. Clarification and permission ride the same
return-marker + Engram recall/imprint flow the developer wires:

  1. The Neuron tries ``recall(...)`` first (check the Engram for a standing
     answer / grant).
  2. On a miss it returns a marker - ``{"__clarification__": True, ...}`` or
     ``{"__permission__": True, "action": ...}`` - which the Axon turns into a
     CLARIFICATION / PERMISSION signal on the bus.
  3. A Cortex (centralised) or any peer (decentralised) handles it via
     ``@dendrite.on_clarification`` / ``@dendrite.on_permission``, then either
     imprints the decision into an Engram and/or sends it back:
       - ``respond_to_clarification`` / ``respond_to_permission`` re-dispatch a
         TASK carrying the answer/verdict (the Neuron resumes and can imprint),
         or
       - ``answer_clarification`` / ``grant_permission`` / ``deny_permission``
         emit a discrete CLARIFICATION_ANSWER / PERMISSION_DECISION signal.

This module is intentionally empty. Delete the file.
"""
