"""The reconciler: outcomes become supersede / retract / confirm."""

from stateledger import ConclusionStatus, Reconciler, rewrite_from_outcomes


def _ledger_with_belief(ledger):
    ev = ledger.add_evidence("tool-output", content="GET /v1 -> 200", source="smoke")
    c = ledger.add_conclusion("Base is /v1", [ev.id], confidence=0.8)
    return ev, c


def test_wrong_with_replacement_supersedes(ledger):
    _, c = _ledger_with_belief(ledger)
    result = Reconciler(ledger).apply_outcome(
        {
            "conclusion_id": c.id,
            "verdict": "wrong",
            "reason": "v1 was decommissioned",
            "evidence": {
                "kind": "tool-output",
                "content": "GET /v1 -> 404",
                "source": "smoke",
            },
            "replacement": {"statement": "Base is /v2", "confidence": 0.9},
        }
    )
    assert result.action == "superseded"
    assert result.new_conclusion_id is not None
    assert ledger.get_conclusion(c.id).status is ConclusionStatus.SUPERSEDED
    new = ledger.get_conclusion(result.new_conclusion_id)
    assert new.statement == "Base is /v2"
    # The replacement is supported by the outcome's own evidence.
    assert result.evidence_id in new.evidence_ids


def test_wrong_without_replacement_retracts(ledger):
    _, c = _ledger_with_belief(ledger)
    result = Reconciler(ledger).apply_outcome(
        {
            "conclusion_id": c.id,
            "verdict": "wrong",
            "reason": "endpoint gone",
            "evidence": {"kind": "tool-output", "content": "404", "source": "smoke"},
        }
    )
    assert result.action == "retracted"
    stored = ledger.get_conclusion(c.id)
    assert stored.status is ConclusionStatus.RETRACTED
    assert stored.retraction_reason == "endpoint gone"
    assert stored.retraction_evidence_ids == (result.evidence_id,)


def test_outdated_without_reason_gets_default_reason(ledger):
    _, c = _ledger_with_belief(ledger)
    result = Reconciler(ledger).apply_outcome(
        {"conclusion_id": c.id, "verdict": "outdated"}
    )
    assert result.action == "retracted"
    assert "outdated" in ledger.get_conclusion(c.id).retraction_reason


def test_confirmed_keeps_conclusion_active_and_logs(ledger):
    _, c = _ledger_with_belief(ledger)
    result = Reconciler(ledger).apply_outcome(
        {
            "conclusion_id": c.id,
            "verdict": "confirmed",
            "evidence": {"kind": "tool-output", "content": "still 200", "source": "smoke"},
        }
    )
    assert result.action == "confirmed"
    assert ledger.get_conclusion(c.id).status is ConclusionStatus.ACTIVE
    assert any(e["type"] == "outcome_confirmed" for e in ledger.events)


def test_outcome_can_reference_existing_evidence(ledger):
    _, c = _ledger_with_belief(ledger)
    proof = ledger.add_evidence("tool-output", content="hard proof", source="audit")
    result = Reconciler(ledger).apply_outcome(
        {
            "conclusion_id": c.id,
            "verdict": "wrong",
            "reason": "see audit",
            "evidence_id": proof.id,
        }
    )
    assert result.action == "retracted"
    assert result.evidence_id == proof.id
    assert len(ledger.all_evidence()) == 2  # nothing new was fabricated


def test_unknown_conclusion_reports_error_not_raise(ledger):
    result = Reconciler(ledger).apply_outcome(
        {"conclusion_id": "c_9999", "verdict": "wrong", "reason": "x"}
    )
    assert result.action == "error"
    assert result.error


def test_bad_verdict_reports_error(ledger):
    _, c = _ledger_with_belief(ledger)
    result = Reconciler(ledger).apply_outcome(
        {"conclusion_id": c.id, "verdict": "shrug"}
    )
    assert result.action == "error"


def test_batch_rewrite_counts_and_continues_past_errors(ledger):
    _, c1 = _ledger_with_belief(ledger)
    e2 = ledger.add_evidence("note", content="second belief source")
    c2 = ledger.add_conclusion("Second belief", [e2.id], confidence=0.5)

    report = rewrite_from_outcomes(
        ledger,
        [
            {
                "conclusion_id": c1.id,
                "verdict": "wrong",
                "evidence": {"kind": "note", "content": "v1 dead"},
                "replacement": {"statement": "Base is /v2", "confidence": 0.9},
            },
            {"conclusion_id": "c_9999", "verdict": "wrong"},  # error, mid-batch
            {"conclusion_id": c2.id, "verdict": "confirmed"},
        ],
    )
    assert len(report.results) == 3
    assert report.superseded == 1
    assert report.confirmed == 1
    assert report.errors == 1
    assert report.retracted == 0
    # The error did not stop the batch: c2 was still processed.
    assert ledger.get_conclusion(c2.id).status is ConclusionStatus.ACTIVE
    assert [r.action for r in report.results] == ["superseded", "error", "confirmed"]
