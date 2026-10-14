"""Data model for stateledger.

The core design rule: EVIDENCE and CONCLUSIONS are different kinds of
things and are stored differently.

* Evidence is a raw observation (a tool output, a file reference, a test
  result). Evidence is append-only: once recorded it is never edited or
  deleted. It carries a content hash so the same observation recorded
  twice is stored once.
* A Conclusion is a derived belief: a statement plus the ids of the
  evidence that supports it, a confidence, and a lifecycle status
  (active / superseded / retracted). Conclusions are immutable value
  objects: the store never edits one in place. A lifecycle transition
  *replaces* the stored record with a copy whose status (and only its
  status-related fields) changed, so any Conclusion object you already
  hold keeps the state it had when you fetched it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum


class ConclusionStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


def compute_content_hash(kind: str, content: str | None, ref: str | None) -> str:
    """Stable hash of an evidence payload, used for dedupe.

    The hash covers kind + content + ref (but not source or timestamp):
    the same observation arriving twice, even from a retried tool call,
    is the same evidence.
    """
    payload = json.dumps(
        {"kind": kind, "content": content, "ref": ref},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Evidence:
    """One raw observation. Append-only; never rewritten after creation."""

    id: str
    kind: str  # e.g. "tool-output", "file", "test-run", "user-message"
    content: str | None  # inline payload (small observations)
    ref: str | None  # external reference (path, URL) for large payloads
    source: str  # who/what produced it, e.g. "pytest", "deploy-script"
    timestamp: float  # epoch seconds
    hash: str  # content hash, see compute_content_hash
    # Ids of conclusions this evidence contradicts, if known at record
    # time. Used by the stale-conclusion finder (see Ledger.find_stale).
    contradicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conclusion:
    """A derived belief with provenance and a lifecycle status.

    Instances are immutable. Status transitions are performed by the
    store, which swaps in a modified copy; the statement, evidence list
    and confidence of an existing conclusion never change.
    """

    id: str
    statement: str
    evidence_ids: tuple[str, ...]  # provenance: what supports this belief
    confidence: float  # 0.0 .. 1.0, the agent's own estimate
    status: ConclusionStatus = ConclusionStatus.ACTIVE
    supersedes: str | None = None  # id of the conclusion this one replaced
    superseded_by: str | None = None  # id of the conclusion that replaced this
    retraction_reason: str | None = None
    retraction_evidence_ids: tuple[str, ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class TrailEntry:
    """One link of a provenance chain: a conclusion and its evidence."""

    conclusion: Conclusion
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class ProvenanceTrail:
    """Full provenance for one conclusion.

    ``entries`` walks the supersede chain newest-first: entry 0 is the
    conclusion asked about, later entries are the conclusions it
    superseded, each with the evidence that supported it at the time.
    """

    conclusion_id: str
    entries: tuple[TrailEntry, ...]


@dataclass(frozen=True)
class StaleFinding:
    """An active conclusion the stale finder thinks needs review."""

    conclusion: Conclusion
    reasons: tuple[str, ...]
