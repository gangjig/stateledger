"""The Reconciler: turn real-world outcomes into ledger bookkeeping.

A long-running agent learns that something it believed was wrong. In a
free-form memory file, that moment usually becomes a silent edit (or
worse, the old belief stays). The reconciler makes it explicit: an
*outcome record* comes in, new evidence is filed, and the conclusion is
superseded or retracted through the store's lifecycle — never edited,
never dropped without a reason.

Outcome record schema (a plain dict, so it is trivial to produce from
a test run, a grader, or a human review)::

    {
        "conclusion_id": "c_0001",          # required
        "verdict": "wrong" | "outdated" | "confirmed",   # required
        "reason": "deploy logs show v2 since Tuesday",  # used for retract
        "evidence": {                        # optional: new observation
            "kind": "tool-output",
            "content": "GET /v1 -> 404",
            "source": "smoke-test",
        },
        "evidence_id": "ev_0007",            # optional: existing evidence
        "replacement": {                     # optional: corrected belief
            "statement": "The API base is /v2",
            "confidence": 0.9,
            "evidence_ids": ["ev_0003"],     # extra support, optional
        },
    }

Deterministic verdict rules:

* ``confirmed`` — the conclusion stays active; the outcome and its
  evidence are recorded in the audit log and nothing else changes.
* ``wrong`` / ``outdated`` with a ``replacement`` — the conclusion is
  superseded by the replacement, supported by the outcome's evidence
  plus any extra ids the replacement names.
* ``wrong`` / ``outdated`` without a ``replacement`` — the conclusion
  is retracted, with ``reason`` (or a default derived from the verdict).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .errors import LedgerError
from .store import Ledger

_VERDICTS = ("wrong", "outdated", "confirmed")


@dataclass(frozen=True)
class OutcomeResult:
    conclusion_id: Optional[str]
    action: str  # "superseded" | "retracted" | "confirmed" | "error"
    evidence_id: Optional[str] = None
    new_conclusion_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ReconcileReport:
    results: list[OutcomeResult] = field(default_factory=list)

    @property
    def superseded(self) -> int:
        return sum(1 for r in self.results if r.action == "superseded")

    @property
    def retracted(self) -> int:
        return sum(1 for r in self.results if r.action == "retracted")

    @property
    def confirmed(self) -> int:
        return sum(1 for r in self.results if r.action == "confirmed")

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.action == "error")


class Reconciler:
    """Applies outcome records to one ledger."""

    def __init__(self, ledger: Ledger):
        self._ledger = ledger

    def apply_outcome(self, outcome: dict) -> OutcomeResult:
        """Apply one outcome record. Bad records do not raise: the
        failure is reported in the returned OutcomeResult so batch
        processing can continue (and the failure itself is visible)."""
        conclusion_id = outcome.get("conclusion_id")
        try:
            return self._apply(outcome)
        except LedgerError as exc:
            return OutcomeResult(
                conclusion_id=conclusion_id, action="error", error=str(exc)
            )
        except (KeyError, TypeError, ValueError) as exc:
            return OutcomeResult(
                conclusion_id=conclusion_id,
                action="error",
                error=f"malformed outcome record: {exc}",
            )

    def _apply(self, outcome: dict) -> OutcomeResult:
        ledger = self._ledger
        conclusion_id = outcome["conclusion_id"]
        verdict = outcome["verdict"]
        if verdict not in _VERDICTS:
            raise ValueError(f"verdict must be one of {_VERDICTS}, got {verdict!r}")
        conclusion = ledger.get_conclusion(conclusion_id)

        # File the outcome's evidence first: the bookkeeping below
        # needs to point at it.
        evidence_id: Optional[str] = None
        ev_spec = outcome.get("evidence")
        if ev_spec is not None:
            ev = ledger.add_evidence(
                kind=ev_spec["kind"],
                content=ev_spec.get("content"),
                ref=ev_spec.get("ref"),
                source=ev_spec.get("source", "outcome"),
            )
            evidence_id = ev.id
        elif outcome.get("evidence_id") is not None:
            evidence_id = ledger.get_evidence(outcome["evidence_id"]).id

        if verdict == "confirmed":
            ledger.note_outcome_confirmed(conclusion_id, evidence_id)
            return OutcomeResult(
                conclusion_id=conclusion_id,
                action="confirmed",
                evidence_id=evidence_id,
            )

        replacement = outcome.get("replacement")
        if replacement is not None:
            support: list[str] = []
            if evidence_id is not None:
                support.append(evidence_id)
            support.extend(replacement.get("evidence_ids", []))
            new = ledger.supersede_conclusion(
                conclusion.id,
                statement=replacement["statement"],
                evidence_ids=support,
                confidence=replacement["confidence"],
            )
            return OutcomeResult(
                conclusion_id=conclusion_id,
                action="superseded",
                evidence_id=evidence_id,
                new_conclusion_id=new.id,
            )

        reason = outcome.get("reason") or f"conclusion marked {verdict} by outcome review"
        support = [evidence_id] if evidence_id is not None else list(conclusion.evidence_ids)
        ledger.retract_conclusion(conclusion_id, reason=reason, evidence_ids=support)
        return OutcomeResult(
            conclusion_id=conclusion_id,
            action="retracted",
            evidence_id=evidence_id,
        )


def rewrite_from_outcomes(
    ledger: Ledger, outcomes: Sequence[dict]
) -> ReconcileReport:
    """Process a batch of outcome records against a ledger, in order.

    This is the ReHarness-style "rewrite memory from outcomes" step,
    kept deterministic: same ledger + same outcomes = same result.
    Every outcome produces exactly one OutcomeResult, in input order;
    failures are collected in the report, never swallowed and never
    fatal to the rest of the batch.
    """
    reconciler = Reconciler(ledger)
    report = ReconcileReport()
    for outcome in outcomes:
        report.results.append(reconciler.apply_outcome(outcome))
    return report
