"""The Ledger: a structured, append-only state store for agents.

Free-form agent "memory" (one growing text blob, or a chat history the
model re-reads) drifts silently: a belief formed on Monday survives in
the text long after Tuesday's evidence killed it, and nothing records
*why* the agent believed anything. stateledger takes the LongHarness
approach instead: state is an explicit store with two strictly separate
layers — evidence (raw, append-only) and conclusions (derived, with
provenance and a lifecycle) — and a reconciler that reviews outcomes
and supersedes or retracts stale conclusions instead of editing
history.

Design rules enforced here:

1. Evidence is append-only and deduped by content hash.
2. A conclusion's statement / evidence / confidence never change after
   creation. Only its status changes, and every transition is recorded
   in an append-only audit log (``Ledger.events``).
3. You cannot retract a conclusion without a reason and supporting
   evidence, and you cannot supersede one silently — the link is kept
   in both directions, so the full provenance trail stays walkable.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from .errors import (
    InvalidTransitionError,
    UnknownConclusionError,
    UnknownEvidenceError,
    ValidationError,
)
from .models import (
    Conclusion,
    ConclusionStatus,
    Evidence,
    ProvenanceTrail,
    StaleFinding,
    TrailEntry,
    compute_content_hash,
)

_SCHEMA_VERSION = 1


class Ledger:
    """An explicit-state store: evidence + conclusions + audit log."""

    def __init__(self, now_fn: Optional[Callable[[], float]] = None):
        # now_fn is injectable so tests (and agents replaying a log) can
        # run on a deterministic clock.
        self._now_fn = now_fn or time.time
        self._evidence: dict[str, Evidence] = {}
        self._evidence_by_hash: dict[str, str] = {}
        self._conclusions: dict[str, Conclusion] = {}
        self._events: list[dict] = []
        self._evidence_counter = 0
        self._conclusion_counter = 0

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> float:
        return float(self._now_fn())

    def _log(self, event_type: str, **fields) -> None:
        self._events.append(
            {"type": event_type, "timestamp": self._now(), **fields}
        )

    def _check_evidence_ids(self, evidence_ids: Sequence[str]) -> tuple[str, ...]:
        ids = tuple(dict.fromkeys(evidence_ids))  # dedupe, keep order
        if not ids:
            raise ValidationError("at least one evidence id is required")
        missing = [i for i in ids if i not in self._evidence]
        if missing:
            raise UnknownEvidenceError(f"unknown evidence id(s): {missing}")
        return ids

    @staticmethod
    def _check_confidence(confidence: float) -> float:
        c = float(confidence)
        if not 0.0 <= c <= 1.0:
            raise ValidationError(f"confidence must be within 0..1, got {confidence!r}")
        return c

    # ------------------------------------------------------------------
    # evidence
    # ------------------------------------------------------------------

    def add_evidence(
        self,
        kind: str,
        content: Optional[str] = None,
        ref: Optional[str] = None,
        source: str = "unknown",
        timestamp: Optional[float] = None,
        contradicts: Iterable[str] = (),
    ) -> Evidence:
        """Record one observation. Append-only and deduped.

        If an evidence item with the same content hash already exists,
        the existing item is returned and nothing is stored (a retried
        tool call must not double-count as two observations).
        """
        if not kind or not kind.strip():
            raise ValidationError("evidence kind must be a non-empty string")
        if (content is None or content == "") and (ref is None or ref == ""):
            raise ValidationError("evidence needs a content payload or a ref")
        digest = compute_content_hash(kind, content, ref)
        existing_id = self._evidence_by_hash.get(digest)
        if existing_id is not None:
            return self._evidence[existing_id]

        self._evidence_counter += 1
        ev = Evidence(
            id=f"ev_{self._evidence_counter:04d}",
            kind=kind,
            content=content,
            ref=ref,
            source=source,
            timestamp=float(timestamp) if timestamp is not None else self._now(),
            hash=digest,
            contradicts=tuple(contradicts),
        )
        self._evidence[ev.id] = ev
        self._evidence_by_hash[digest] = ev.id
        self._log("evidence_added", evidence_id=ev.id, kind=kind)
        return ev

    def get_evidence(self, evidence_id: str) -> Evidence:
        try:
            return self._evidence[evidence_id]
        except KeyError:
            raise UnknownEvidenceError(f"unknown evidence id: {evidence_id}") from None

    def all_evidence(self) -> tuple[Evidence, ...]:
        """Every evidence item, in recording order."""
        return tuple(self._evidence.values())

    # ------------------------------------------------------------------
    # conclusions
    # ------------------------------------------------------------------

    def add_conclusion(
        self,
        statement: str,
        evidence_ids: Sequence[str],
        confidence: float,
    ) -> Conclusion:
        """Derive a new active conclusion from existing evidence."""
        if not statement or not statement.strip():
            raise ValidationError("conclusion statement must be non-empty")
        ids = self._check_evidence_ids(evidence_ids)
        conf = self._check_confidence(confidence)
        now = self._now()
        self._conclusion_counter += 1
        c = Conclusion(
            id=f"c_{self._conclusion_counter:04d}",
            statement=statement,
            evidence_ids=ids,
            confidence=conf,
            created_at=now,
            updated_at=now,
        )
        self._conclusions[c.id] = c
        self._log("conclusion_added", conclusion_id=c.id)
        return c

    def get_conclusion(self, conclusion_id: str) -> Conclusion:
        try:
            return self._conclusions[conclusion_id]
        except KeyError:
            raise UnknownConclusionError(
                f"unknown conclusion id: {conclusion_id}"
            ) from None

    def all_conclusions(self) -> tuple[Conclusion, ...]:
        """Every conclusion ever made, in creation order, any status."""
        return tuple(self._conclusions.values())

    def active_conclusions(self) -> tuple[Conclusion, ...]:
        """Conclusions currently believed (status == active)."""
        return tuple(
            c for c in self._conclusions.values()
            if c.status is ConclusionStatus.ACTIVE
        )

    def supersede_conclusion(
        self,
        old_id: str,
        statement: str,
        evidence_ids: Sequence[str],
        confidence: float,
    ) -> Conclusion:
        """Replace an active conclusion with a new one.

        The old conclusion is not edited: its stored record is swapped
        for a copy with status=superseded and a link to the new
        conclusion, and the new conclusion links back via ``supersedes``.
        Returns the new conclusion.
        """
        old = self.get_conclusion(old_id)
        if old.status is not ConclusionStatus.ACTIVE:
            raise InvalidTransitionError(
                f"cannot supersede {old_id}: status is {old.status.value}, not active"
            )
        if not statement or not statement.strip():
            raise ValidationError("conclusion statement must be non-empty")
        ids = self._check_evidence_ids(evidence_ids)
        conf = self._check_confidence(confidence)
        now = self._now()

        self._conclusion_counter += 1
        new = Conclusion(
            id=f"c_{self._conclusion_counter:04d}",
            statement=statement,
            evidence_ids=ids,
            confidence=conf,
            supersedes=old.id,
            created_at=now,
            updated_at=now,
        )
        self._conclusions[new.id] = new
        self._conclusions[old.id] = replace(
            old,
            status=ConclusionStatus.SUPERSEDED,
            superseded_by=new.id,
            updated_at=now,
        )
        self._log(
            "conclusion_superseded",
            conclusion_id=old.id,
            superseded_by=new.id,
        )
        return new

    def retract_conclusion(
        self,
        conclusion_id: str,
        reason: str,
        evidence_ids: Sequence[str],
    ) -> Conclusion:
        """Withdraw an active conclusion, with a reason and evidence.

        A retraction without a stated reason and at least one piece of
        supporting evidence is refused: silently dropping a belief is
        exactly the failure mode this library exists to prevent.
        Returns the updated (retracted) conclusion record.
        """
        old = self.get_conclusion(conclusion_id)
        if old.status is not ConclusionStatus.ACTIVE:
            raise InvalidTransitionError(
                f"cannot retract {conclusion_id}: status is {old.status.value}, not active"
            )
        if not reason or not reason.strip():
            raise ValidationError("retraction requires a non-empty reason")
        ids = self._check_evidence_ids(evidence_ids)
        now = self._now()
        updated = replace(
            old,
            status=ConclusionStatus.RETRACTED,
            retraction_reason=reason,
            retraction_evidence_ids=ids,
            updated_at=now,
        )
        self._conclusions[conclusion_id] = updated
        self._log(
            "conclusion_retracted",
            conclusion_id=conclusion_id,
            reason=reason,
            evidence_ids=list(ids),
        )
        return updated

    def note_outcome_confirmed(
        self, conclusion_id: str, evidence_id: Optional[str] = None
    ) -> None:
        """Record (in the audit log only) that an outcome confirmed a
        conclusion. The conclusion itself is not modified."""
        self.get_conclusion(conclusion_id)
        if evidence_id is not None:
            self.get_evidence(evidence_id)
        self._log(
            "outcome_confirmed",
            conclusion_id=conclusion_id,
            evidence_id=evidence_id,
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def conclusions_for_evidence(self, evidence_id: str) -> tuple[Conclusion, ...]:
        """All conclusions (any status) that cite this evidence,
        either as support or as retraction evidence."""
        self.get_evidence(evidence_id)
        return tuple(
            c
            for c in self._conclusions.values()
            if evidence_id in c.evidence_ids
            or evidence_id in c.retraction_evidence_ids
        )

    def evidence_trail(self, conclusion_id: str) -> ProvenanceTrail:
        """The full provenance chain for a conclusion.

        Walks ``supersedes`` links back to the original conclusion,
        newest first, attaching each conclusion's supporting evidence —
        so "why do we believe this, and what did we believe before?"
        is always answerable.
        """
        entries: list[TrailEntry] = []
        seen: set[str] = set()
        current: Optional[Conclusion] = self.get_conclusion(conclusion_id)
        while current is not None:
            if current.id in seen:  # defensive: a corrupt file could cycle
                break
            seen.add(current.id)
            entries.append(
                TrailEntry(
                    conclusion=current,
                    evidence=tuple(
                        self._evidence[i] for i in current.evidence_ids
                    ),
                )
            )
            current = (
                self._conclusions.get(current.supersedes)
                if current.supersedes
                else None
            )
        return ProvenanceTrail(conclusion_id=conclusion_id, entries=tuple(entries))

    def find_stale(
        self,
        now: Optional[float] = None,
        max_evidence_age_seconds: Optional[float] = None,
    ) -> tuple[StaleFinding, ...]:
        """Active conclusions that probably need review.

        The rule is deliberately simple and deterministic. An active
        conclusion is stale when either:

        * contradiction — some evidence in the ledger lists this
          conclusion in its ``contradicts`` field, or
        * age — ``max_evidence_age_seconds`` is given and the newest
          evidence supporting the conclusion is older than that
          (relative to ``now``, default: the ledger clock).

        Superseded and retracted conclusions are never reported: the
        lifecycle already handled them.
        """
        ref_now = float(now) if now is not None else self._now()
        contradicted_by: dict[str, list[str]] = {}
        for ev in self._evidence.values():
            for cid in ev.contradicts:
                contradicted_by.setdefault(cid, []).append(ev.id)

        findings: list[StaleFinding] = []
        for c in self.active_conclusions():
            reasons: list[str] = []
            for ev_id in contradicted_by.get(c.id, []):
                reasons.append(f"contradicted by evidence {ev_id}")
            if max_evidence_age_seconds is not None and c.evidence_ids:
                newest = max(self._evidence[i].timestamp for i in c.evidence_ids)
                age = ref_now - newest
                if age > max_evidence_age_seconds:
                    reasons.append(
                        f"newest supporting evidence is {age:.0f}s old "
                        f"(limit {max_evidence_age_seconds:.0f}s)"
                    )
            if reasons:
                findings.append(StaleFinding(conclusion=c, reasons=tuple(reasons)))
        return tuple(findings)

    # ------------------------------------------------------------------
    # audit log
    # ------------------------------------------------------------------

    @property
    def events(self) -> tuple[dict, ...]:
        """The append-only audit log of everything that happened."""
        return tuple(self._events)

    # ------------------------------------------------------------------
    # persistence (JSON, atomic writes)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": _SCHEMA_VERSION,
            "evidence_counter": self._evidence_counter,
            "conclusion_counter": self._conclusion_counter,
            "evidence": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "content": e.content,
                    "ref": e.ref,
                    "source": e.source,
                    "timestamp": e.timestamp,
                    "hash": e.hash,
                    "contradicts": list(e.contradicts),
                }
                for e in self._evidence.values()
            ],
            "conclusions": [
                {
                    "id": c.id,
                    "statement": c.statement,
                    "evidence_ids": list(c.evidence_ids),
                    "confidence": c.confidence,
                    "status": c.status.value,
                    "supersedes": c.supersedes,
                    "superseded_by": c.superseded_by,
                    "retraction_reason": c.retraction_reason,
                    "retraction_evidence_ids": list(c.retraction_evidence_ids),
                    "created_at": c.created_at,
                    "updated_at": c.updated_at,
                }
                for c in self._conclusions.values()
            ],
            "events": list(self._events),
        }

    @classmethod
    def from_dict(cls, data: dict, now_fn: Optional[Callable[[], float]] = None) -> "Ledger":
        if data.get("schema_version") != _SCHEMA_VERSION:
            raise ValidationError(
                f"unsupported ledger schema version: {data.get('schema_version')!r}"
            )
        ledger = cls(now_fn=now_fn)
        for e in data["evidence"]:
            ev = Evidence(
                id=e["id"],
                kind=e["kind"],
                content=e["content"],
                ref=e["ref"],
                source=e["source"],
                timestamp=e["timestamp"],
                hash=e["hash"],
                contradicts=tuple(e.get("contradicts", [])),
            )
            ledger._evidence[ev.id] = ev
            ledger._evidence_by_hash[ev.hash] = ev.id
        for c in data["conclusions"]:
            con = Conclusion(
                id=c["id"],
                statement=c["statement"],
                evidence_ids=tuple(c["evidence_ids"]),
                confidence=c["confidence"],
                status=ConclusionStatus(c["status"]),
                supersedes=c["supersedes"],
                superseded_by=c["superseded_by"],
                retraction_reason=c["retraction_reason"],
                retraction_evidence_ids=tuple(c["retraction_evidence_ids"]),
                created_at=c["created_at"],
                updated_at=c["updated_at"],
            )
            ledger._conclusions[con.id] = con
        ledger._events = [dict(e) for e in data.get("events", [])]
        ledger._evidence_counter = data["evidence_counter"]
        ledger._conclusion_counter = data["conclusion_counter"]
        return ledger

    def save(self, path: str | Path) -> Path:
        """Write the ledger to ``path`` as JSON, atomically: the data
        goes to a temp file in the same directory and is renamed over
        the target, so a crash mid-save never leaves a torn file."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return target

    @classmethod
    def load(cls, path: str | Path, now_fn: Optional[Callable[[], float]] = None) -> "Ledger":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data, now_fn=now_fn)
