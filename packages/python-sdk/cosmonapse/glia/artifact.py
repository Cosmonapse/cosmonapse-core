"""
cosmonapse.glia.artifact
~~~~~~~~~~~~~~~~~~~~~~~~
The declarative policy bundle: parse, validate, version.

Decorators alone do not survive a fleet. With twenty components nobody
can answer "what is enforced right now" by reading twenty modules. So a
card is primarily a versioned artifact: content-addressed, parsed at
load, and reporting its version on REGISTER so an audit record can be
attributed to a policy version. Decorators cover what the artifact
cannot express; both are evaluated, the artifact first.

What the artifact can express is deliberately small. Cosmonapse ships
the seam, not a classifier: a rule has a scope (``direction`` and
``types``, default every signal both ways), narrows by tool / op /
component name, matches the signal's payload with regexes the policy
author wrote or by the presence of named keys, returns one of the four
verdicts, and says what a violation does next (``retry``). An empty
artifact does nothing.

Format (JSON)::

    {
      "card_id": "corp-2026-09",
      "version": "3",
      "issued_to": "planner",
      "issuer": "acme-sec",
      "issued_at": "2026-09-17T00:00:00Z",
      "mode": "audit",
      "fail_open": false,
      "sample_pass": 0,
      "journal": {"path": "/var/log/cosmonapse/glia.jsonl"},
      "repair": {"max_attempts": 2, "live_deltas": false},
      "limits": {"mode": "component", "max_tool_calls": 40},
      "policies": [
        {"id": "no-keys-either-way", "verdict": "redact",
         "match": ["sk-[A-Za-z0-9]{16,}"], "replacement": "[redacted]",
         "reason": "signals must not carry provider keys"},
        {"id": "no-rm-rf", "direction": "outbound", "types": ["TOOL_CALL"],
         "verdict": "deny", "tools": ["shell"], "match": ["rm\\\\s+-rf"],
         "retry": "reask", "max_attempts": 2,
         "reason": "recursive delete is not permitted from a tool call"},
        {"id": "no-instructions-from-memory", "direction": "inbound",
         "types": ["RECALLED"], "verdict": "deny", "retry": "resend",
         "match": ["(?i)ignore (all|previous) instructions"],
         "reason": "recalled memory must not carry instructions"}
      ]
    }
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cosmonapse.envelope import SignalType
from cosmonapse.glia.base import (
    DEFAULT_REPLACEMENT,
    Decision,
    Direction,
    Glia,
    Mode,
    PolicyError,
    RepairPolicy,
    Retry,
    TraceLimits,
    Verdict,
    _validate_scope,
    canonical_text,
    coerce_types,
    content_hash,
    redact_text_leaves,
)

#: Every field a rule may carry, for ``policy.describe()``. A card editor
#: builds a valid rule from this without reading this module.
RULE_FIELDS: dict[str, str] = {
    "id": "str, required. Stable identifier, reported on the bus record.",
    "direction": (
        "str. inbound / outbound / both. Default both. Inbound is a "
        "signal arriving at the component the card is mounted on; "
        "outbound is one leaving it."
    ),
    "types": (
        "list[str]. Signal type names the rule applies to, or ['*']. "
        "Default every type."
    ),
    "verdict": "str, required. allow / deny / redact / escalate.",
    "reason": (
        "str. Author-written static text. It is what a refusal shows the "
        "model, so it describes the rule and never quotes what matched."
    ),
    "match": (
        "list[str]. Regexes over the payload's text rendering. Any match "
        "fires the rule. Omit (with no keys and always unset) and the "
        "rule never fires."
    ),
    "keys": (
        "list[str]. Keys whose presence anywhere in the payload fires the "
        "rule. Under a redact verdict these keys are dropped from the copy."
    ),
    "always": "bool. Fire unconditionally for every selected signal.",
    "tools": (
        "list[str]. Narrow to signals whose payload.tool is one of these "
        "(TOOL_CALL, TOOL_RESULT)."
    ),
    "ops": (
        "list[str]. Narrow to signals whose payload.op is one of these "
        "(IMPRINT, IMPRINTED): add/append/merge/upsert/delete."
    ),
    "components": (
        "list[str]. Narrow to these component identities (neuron_id, "
        "effector_id, engram_id)."
    ),
    "replacement": (
        f"str. redact only: substituted into each matched span. Default "
        f"{DEFAULT_REPLACEMENT!r}."
    ),
    "retry": (
        "str. deny only: what the violation does next. reask (re-run the "
        "Axon's Neuron with the refusal), resend (repeat the request "
        "behind a TOOL_RESULT or RECALLED), or none. Default none."
    ),
    "max_attempts": (
        "int. How many retries this policy allows. Default 2. The card's "
        "repair.max_attempts is still the ceiling for reask."
    ),
}

_ARTIFACT_FIELDS = frozenset({
    "card_id", "version", "issued_to", "issuer", "issued_at", "not_after",
    "mode", "fail_open", "sample_pass", "journal", "repair", "limits",
    "policies",
})


@dataclass(frozen=True)
class PolicyRule:
    """One declarative rule. Immutable once parsed."""

    id: str
    decision: Decision
    direction: Direction = Direction.BOTH
    #: None means every signal type.
    types: frozenset[SignalType] | None = None
    reason: str | None = None
    patterns: tuple[re.Pattern[str], ...] = ()
    keys: frozenset[str] = frozenset()
    always: bool = False
    tools: frozenset[str] = frozenset()
    ops: frozenset[str] = frozenset()
    components: frozenset[str] = frozenset()
    replacement: str = DEFAULT_REPLACEMENT
    retry: Retry = Retry.NONE
    max_attempts: int = 2

    @property
    def selective(self) -> bool:
        """Whether the rule can ever fire. A rule with no match, no keys
        and ``always`` unset is inert, and is rejected at parse time."""
        return bool(self.patterns or self.keys or self.always)

    def scope(self) -> set[tuple[Direction, SignalType]]:
        """Every (direction, type) pair this rule selects."""
        return {
            (d, st)
            for d in self.direction.crossings()
            for st in (self.types or SignalType)
        }

    def selects(
        self,
        direction: Direction,
        signal_type: SignalType,
        payload: dict[str, Any],
        *,
        component: str | None,
    ) -> bool:
        if not self.direction.covers(direction):
            return False
        if self.types is not None and signal_type not in self.types:
            return False
        if self.tools and payload.get("tool") not in self.tools:
            return False
        if self.ops and payload.get("op") not in self.ops:
            return False
        return not (
            self.components
            and (component is None or component not in self.components)
        )

    def fires(self, subject: Any) -> str | None:
        """The matched text when the rule fires, else None.

        The returned text is journal-only. It never reaches the bus.
        """
        if self.keys:
            hit = sorted(_keys_present(subject, self.keys))
            if hit:
                return f"keys={hit}"
        if self.patterns:
            text = canonical_text(subject)
            for pat in self.patterns:
                m = pat.search(text)
                if m is not None:
                    return m.group(0)
        if self.always:
            return "always"
        return None

    def verdict_for(self, subject: Any, matched: str) -> Verdict:
        if self.decision is Decision.ALLOW:
            return Verdict.allow(policy_id=self.id)
        if self.decision is Decision.DENY:
            return Verdict.deny(
                self.reason or f"refused by policy {self.id!r}",
                policy_id=self.id, matched=matched,
            )
        if self.decision is Decision.ESCALATE:
            return Verdict.escalate(
                self.reason or f"escalated by policy {self.id!r}",
                policy_id=self.id, matched=matched,
            )
        replaced, subs = redact_text_leaves(
            subject, self.patterns, self.replacement, self.keys,
        )
        return Verdict.redact(
            replaced, reason=self.reason, policy_id=self.id,
            matched=matched, substitutions=subs,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "direction": self.direction.value,
            "types": (
                None if self.types is None
                else sorted(t.value for t in self.types)
            ),
            "verdict": self.decision.value,
            "reason": self.reason,
            "match": [p.pattern for p in self.patterns],
            "keys": sorted(self.keys),
            "always": self.always,
            "tools": sorted(self.tools),
            "ops": sorted(self.ops),
            "components": sorted(self.components),
            "replacement": self.replacement,
            "retry": self.retry.value,
            "max_attempts": self.max_attempts,
        }


def _keys_present(obj: Any, keys: frozenset[str]) -> set[str]:
    """Which of ``keys`` appear as a mapping key anywhere in ``obj``."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k in keys:
                found.add(k)
            found |= _keys_present(v, keys)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= _keys_present(v, keys)
    return found


class PolicyArtifact:
    """A parsed, validated, content-addressed policy bundle."""

    def __init__(
        self,
        *,
        rules: list[PolicyRule],
        raw: dict[str, Any],
        source_text: str,
    ) -> None:
        self._rules = list(rules)
        self._raw = dict(raw)
        self._declared: set[tuple[Direction, SignalType]] = set()
        for r in self._rules:
            self._declared |= r.scope()
        #: The artifact's content address. Two cards with the same hash
        #: carry the same policies, whatever they were named.
        self.content_hash = content_hash(source_text)

    # -- parsing -------------------------------------------------------

    @classmethod
    def load(cls, source: str | bytes | os.PathLike[str]) -> PolicyArtifact:
        """Parse from a file path, raw bytes, or an env var name.

        A ``str`` naming an existing file is read as one; a ``str`` naming
        a set environment variable is read from it; otherwise the string
        is the artifact text. ``bytes`` is always the artifact.
        """
        if isinstance(source, bytes):
            return cls.parse(source.decode("utf-8"))
        if isinstance(source, os.PathLike):
            return cls.parse(Path(source).read_text(encoding="utf-8"))
        text = source
        candidate = Path(text) if len(text) < 4096 and "\n" not in text else None
        if candidate is not None:
            try:
                if candidate.is_file():
                    return cls.parse(candidate.read_text(encoding="utf-8"))
            except OSError:
                pass
            env = os.environ.get(text)
            if env:
                return cls.parse(env)
        return cls.parse(text)

    @classmethod
    def parse(cls, text: str) -> PolicyArtifact:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"policy artifact is not valid JSON: {exc}") from None
        if not isinstance(raw, dict):
            raise PolicyError(
                f"policy artifact must be a JSON object, got "
                f"{type(raw).__name__}"
            )
        unknown = sorted(set(raw) - _ARTIFACT_FIELDS)
        if unknown:
            raise PolicyError(
                f"policy artifact has unknown field(s) {unknown}; known: "
                f"{sorted(_ARTIFACT_FIELDS)}"
            )
        entries = raw.get("policies") or []
        if not isinstance(entries, list):
            raise PolicyError("policies must be a list")
        seen: set[str] = set()
        rules: list[PolicyRule] = []
        for i, entry in enumerate(entries):
            rule = _parse_rule(entry, i)
            if rule.id in seen:
                raise PolicyError(f"duplicate policy id {rule.id!r}")
            seen.add(rule.id)
            rules.append(rule)
        return cls(rules=rules, raw=raw, source_text=text)

    # -- the card ------------------------------------------------------

    def to_card(self) -> Glia:
        """Build the :class:`Glia` this artifact describes."""
        raw = self._raw
        journal = None
        jcfg = raw.get("journal")
        if jcfg:
            from cosmonapse.glia.journal import Journal

            if not isinstance(jcfg, dict) or "path" not in jcfg:
                raise PolicyError(
                    'journal must be an object carrying "path"'
                )
            journal = Journal(
                jcfg["path"], max_bytes=jcfg.get("max_bytes"),
            )
        return Glia(
            card_id=str(raw.get("card_id") or f"card-{self.content_hash}"),
            issued_to=_opt_str(raw, "issued_to"),
            version=_opt_str(raw, "version"),
            issuer=_opt_str(raw, "issuer"),
            issued_at=_opt_str(raw, "issued_at"),
            not_after=_opt_str(raw, "not_after"),
            mode=Mode.coerce(raw.get("mode")),
            fail_open=bool(raw.get("fail_open", False)),
            artifact=self,
            journal=journal,
            repair=_parse_repair(raw.get("repair")),
            limits=_parse_limits(raw.get("limits")),
            sample_pass=int(raw.get("sample_pass") or 0),
            card_hash=self.content_hash,
        )

    # -- evaluation ----------------------------------------------------

    def declared(self) -> set[tuple[Direction, SignalType]]:
        """Every (direction, type) pair some rule selects."""
        return set(self._declared)

    def evaluate(
        self,
        direction: Direction,
        signal_type: SignalType,
        subject: dict[str, Any],
        *,
        component: str | None = None,
    ) -> tuple[Verdict, Retry, int] | None:
        """First firing rule wins, in declaration order. None means no rule
        selecting this signal had anything to say."""
        if (direction, signal_type) not in self._declared:
            return None
        for rule in self._rules:
            if not rule.selects(
                direction, signal_type, subject, component=component,
            ):
                continue
            matched = rule.fires(subject)
            if matched is None:
                continue
            return rule.verdict_for(subject, matched), rule.retry, rule.max_attempts
        return None

    # -- introspection -------------------------------------------------

    @property
    def rules(self) -> list[PolicyRule]:
        return list(self._rules)

    def describe(self) -> list[dict[str, Any]]:
        return [r.describe() for r in self._rules]

    def __repr__(self) -> str:
        return (
            f"PolicyArtifact(rules={len(self._rules)}, "
            f"hash={self.content_hash!r})"
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _opt_str(raw: dict[str, Any], key: str) -> str | None:
    v = raw.get(key)
    return None if v is None else str(v)


def _str_set(entry: dict[str, Any], key: str, rule_id: str) -> frozenset[str]:
    v = entry.get(key)
    if v is None:
        return frozenset()
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise PolicyError(
            f"policy {rule_id!r}: {key} must be a list of strings"
        )
    return frozenset(v)


def _parse_rule(entry: Any, index: int) -> PolicyRule:
    if not isinstance(entry, dict):
        raise PolicyError(
            f"policies[{index}] must be an object, got {type(entry).__name__}"
        )
    if "checkpoint" in entry:
        raise PolicyError(
            f"policies[{index}]: there are no checkpoints. Scope a rule with "
            f"direction (inbound / outbound / both) and types (signal type "
            f"names); on_tool_call is direction=outbound, types=[TOOL_CALL] "
            f"on an Axon and direction=inbound on an Effector."
        )
    unknown = sorted(set(entry) - set(RULE_FIELDS))
    if unknown:
        raise PolicyError(
            f"policies[{index}] has unknown field(s) {unknown}; known: "
            f"{sorted(RULE_FIELDS)}"
        )
    rule_id = entry.get("id")
    if not rule_id or not isinstance(rule_id, str):
        raise PolicyError(f'policies[{index}] needs a string "id"')
    if "verdict" not in entry:
        raise PolicyError(f'policy {rule_id!r} needs a "verdict"')
    decision = Decision.coerce(entry["verdict"])
    direction = Direction.coerce(entry.get("direction"))
    types = coerce_types(entry.get("types"))
    retry = Retry.coerce(entry.get("retry"))
    raw_attempts = entry.get("max_attempts", 2)
    if isinstance(raw_attempts, bool) or not isinstance(raw_attempts, int):
        raise PolicyError(f"policy {rule_id!r}: max_attempts must be an int")
    if retry is not Retry.NONE and decision is not Decision.DENY:
        raise PolicyError(
            f"policy {rule_id!r}: retry only applies to a deny; a "
            f"{decision.value} is not a violation"
        )
    _validate_scope(
        f"policy {rule_id!r}", direction, types, decision, retry, raw_attempts,
    )

    match = entry.get("match")
    if match is None:
        patterns: tuple[re.Pattern[str], ...] = ()
    else:
        if isinstance(match, str):
            match = [match]
        if not isinstance(match, list) or any(
            not isinstance(x, str) for x in match
        ):
            raise PolicyError(
                f"policy {rule_id!r}: match must be a string or a list of "
                f"strings"
            )
        compiled = []
        for pat in match:
            try:
                compiled.append(re.compile(pat))
            except re.error as exc:
                raise PolicyError(
                    f"policy {rule_id!r}: bad regex {pat!r}: {exc}"
                ) from None
        patterns = tuple(compiled)

    replacement = entry.get("replacement", DEFAULT_REPLACEMENT)
    if not isinstance(replacement, str):
        raise PolicyError(
            f"policy {rule_id!r}: replacement must be a string. An artifact "
            f"rule substitutes into matched spans; a decorator handler "
            f"returning Verdict.redact(...) replaces the payload wholesale"
        )

    tools = _str_set(entry, "tools", rule_id)
    ops = _str_set(entry, "ops", rule_id)
    if tools and types is not None and not (
        types & {SignalType.TOOL_CALL, SignalType.TOOL_RESULT}
    ):
        raise PolicyError(
            f"policy {rule_id!r}: tools= narrows TOOL_CALL and TOOL_RESULT, "
            f"and types names neither"
        )
    if ops and types is not None and not (
        types & {SignalType.IMPRINT, SignalType.IMPRINTED}
    ):
        raise PolicyError(
            f"policy {rule_id!r}: ops= narrows IMPRINT and IMPRINTED, and "
            f"types names neither"
        )

    rule = PolicyRule(
        id=rule_id,
        decision=decision,
        direction=direction,
        types=types,
        reason=_opt_str(entry, "reason"),
        patterns=patterns,
        keys=_str_set(entry, "keys", rule_id),
        always=bool(entry.get("always", False)),
        tools=tools,
        ops=ops,
        components=_str_set(entry, "components", rule_id),
        replacement=replacement,
        retry=retry,
        max_attempts=raw_attempts,
    )
    if not rule.selective:
        raise PolicyError(
            f"policy {rule_id!r} can never fire: give it match=, keys= or "
            f"always=true. An inert rule in a card is worse than no rule, "
            f"because the fleet view reports it as enforced."
        )
    if decision is Decision.REDACT and not (rule.patterns or rule.keys):
        raise PolicyError(
            f"policy {rule_id!r}: a redact rule needs match= or keys= to "
            f"know what to replace"
        )
    return rule


def _parse_repair(raw: Any) -> RepairPolicy:
    if raw is None:
        return RepairPolicy()
    if not isinstance(raw, dict):
        raise PolicyError("repair must be an object")
    known = {
        "max_attempts", "emit_audit", "deadline_s", "max_recorded",
        "transient_attempts", "transient_backoff_s",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise PolicyError(f"repair has unknown field(s) {unknown}")
    deadline = raw.get("deadline_s")
    return RepairPolicy(
        max_attempts=int(raw.get("max_attempts", 2)),
        emit_audit=bool(raw.get("emit_audit", True)),
        deadline_s=None if deadline is None else float(deadline),
        max_recorded=int(raw.get("max_recorded", 8)),
        transient_attempts=int(raw.get("transient_attempts", 0)),
        transient_backoff_s=float(raw.get("transient_backoff_s", 0.2)),
    )


def _parse_limits(raw: Any) -> TraceLimits:
    if raw is None:
        return TraceLimits()
    if not isinstance(raw, dict):
        raise PolicyError("limits must be an object")
    known = {
        "mode", "max_actions", "max_tool_calls", "max_imprints", "on_exceed",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise PolicyError(f"limits has unknown field(s) {unknown}")

    def _cap(key: str) -> int | None:
        v = raw.get(key)
        return None if v is None else int(v)

    return TraceLimits(
        mode=str(raw.get("mode") or "component"),
        max_actions=_cap("max_actions"),
        max_tool_calls=_cap("max_tool_calls"),
        max_imprints=_cap("max_imprints"),
        on_exceed=str(raw.get("on_exceed") or "refuse"),
    )
