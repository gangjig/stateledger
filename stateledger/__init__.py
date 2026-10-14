"""stateledger — explicit-state memory for long-running agents.

Evidence (raw observations, append-only) is kept strictly separate
from conclusions (derived beliefs with provenance, confidence, and a
lifecycle). A reconciler reviews outcomes and supersedes or retracts
stale conclusions instead of silently editing history — the
LongHarness pattern: state you can audit beats memory that drifts.

Quickstart::

    from stateledger import Ledger, rewrite_from_outcomes

    ledger = Ledger()
    ev = ledger.add_evidence("tool-output", content="GET /v1/users -> 200",
                             source="smoke-test")
    belief = ledger.add_conclusion("The API base path is /v1",
                                   [ev.id], confidence=0.8)
"""

from .errors import (
    InvalidTransitionError,
    LedgerError,
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
)
from .reconciler import OutcomeResult, ReconcileReport, Reconciler, rewrite_from_outcomes
from .render import OmittedItem, RenderResult, export_markdown, render_context
from .store import Ledger

__version__ = "0.1.0"

__all__ = [
    "Ledger",
    "Evidence",
    "Conclusion",
    "ConclusionStatus",
    "ProvenanceTrail",
    "TrailEntry",
    "StaleFinding",
    "Reconciler",
    "OutcomeResult",
    "ReconcileReport",
    "rewrite_from_outcomes",
    "export_markdown",
    "render_context",
    "RenderResult",
    "OmittedItem",
    "LedgerError",
    "ValidationError",
    "UnknownEvidenceError",
    "UnknownConclusionError",
    "InvalidTransitionError",
]
