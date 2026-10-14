"""Store behaviour: evidence, conclusions, lifecycle, queries, staleness."""

import dataclasses

import pytest

from stateledger import (
    ConclusionStatus,
    InvalidTransitionError,
    UnknownConclusionError,
    UnknownEvidenceError,
    ValidationError,
)


# ---------------- evidence ----------------


def test_add_evidence_assigns_ids_and_fields(ledger):
    ev = ledger.add_evidence("tool-output", content="ok", source="pytest")
    assert ev.id == "ev_0001"
    assert ev.kind == "tool-output"
    assert ev.content == "ok"
    assert ev.source == "pytest"
    assert ev.hash


def test_evidence_dedupe_by_content_hash(ledger):
    first = ledger.add_evidence("tool-output", content="same output", source="run-1")
    second = ledger.add_evidence("tool-output", content="same output", source="run-2")
    assert second.id == first.id
    assert len(ledger.all_evidence()) == 1


def test_evidence_dedupe_ignores_source_but_not_kind(ledger):
    a = ledger.add_evidence("tool-output", content="x", source="s1")
    b = ledger.add_evidence("file-read", content="x", source="s1")
    assert a.id != b.id
    assert len(ledger.all_evidence()) == 2


def test_evidence_ref_only_is_allowed(ledger):
    ev = ledger.add_evidence("file", ref="/tmp/big.log", source="fs")
    assert ev.content is None
    assert ev.ref == "/tmp/big.log"


def test_evidence_requires_content_or_ref(ledger):
    with pytest.raises(ValidationError):
        ledger.add_evidence("tool-output", source="s")


def test_evidence_requires_kind(ledger):
    with pytest.raises(ValidationError):
        ledger.add_evidence("  ", content="x")


def test_get_unknown_evidence_raises(ledger):
    with pytest.raises(UnknownEvidenceError):
        ledger.get_evidence("ev_9999")


# ---------------- conclusions & validation ----------------


def test_add_conclusion_happy_path(ledger):
    ev = ledger.add_evidence("test-run", content="42 passed", source="pytest")
    c = ledger.add_conclusion("The suite is green", [ev.id], confidence=0.9)
    assert c.id == "c_0001"
    assert c.status is ConclusionStatus.ACTIVE
    assert c.evidence_ids == (ev.id,)
    assert ledger.active_conclusions() == (c,)


def test_add_conclusion_unknown_evidence_raises(ledger):
    with pytest.raises(UnknownEvidenceError):
        ledger.add_conclusion("Belief", ["ev_9999"], confidence=0.5)


def test_add_conclusion_empty_evidence_raises(ledger):
    with pytest.raises(ValidationError):
        ledger.add_conclusion("Belief from nothing", [], confidence=0.5)


def test_add_conclusion_confidence_bounds(ledger):
    ev = ledger.add_evidence("note", content="x")
    with pytest.raises(ValidationError):
        ledger.add_conclusion("Too sure", [ev.id], confidence=1.5)
    with pytest.raises(ValidationError):
        ledger.add_conclusion("Negative", [ev.id], confidence=-0.1)


def test_add_conclusion_empty_statement_raises(ledger):
    ev = ledger.add_evidence("note", content="x")
    with pytest.raises(ValidationError):
        ledger.add_conclusion("   ", [ev.id], confidence=0.5)


def test_get_unknown_conclusion_raises(ledger):
    with pytest.raises(UnknownConclusionError):
        ledger.get_conclusion("c_9999")


# ---------------- supersede / retract lifecycle ----------------


def _belief(ledger, statement="The API base is /v1"):
    ev = ledger.add_evidence("tool-output", content="GET /v1 -> 200", source="smoke")
    return ledger.add_conclusion(statement, [ev.id], confidence=0.8)


def test_supersede_links_both_directions(ledger):
    old = _belief(ledger)
    ev2 = ledger.add_evidence("tool-output", content="GET /v2 -> 200", source="smoke")
    new = ledger.supersede_conclusion(
        old.id, "The API base is /v2", [ev2.id], confidence=0.9
    )
    assert new.supersedes == old.id
    stored_old = ledger.get_conclusion(old.id)
    assert stored_old.status is ConclusionStatus.SUPERSEDED
    assert stored_old.superseded_by == new.id
    assert ledger.active_conclusions() == (new,)


def test_supersede_never_mutates_the_object_you_hold(ledger):
    old = _belief(ledger)
    ev2 = ledger.add_evidence("tool-output", content="new", source="s")
    ledger.supersede_conclusion(old.id, "Replacement", [ev2.id], confidence=0.5)
    # The Conclusion returned earlier is immutable history: it still
    # says ACTIVE, because transitions swap in a copy instead of
    # editing in place.
    assert old.status is ConclusionStatus.ACTIVE
    with pytest.raises(dataclasses.FrozenInstanceError):
        old.status = ConclusionStatus.RETRACTED  # type: ignore[misc]


def test_supersede_non_active_raises(ledger):
    old = _belief(ledger)
    ev2 = ledger.add_evidence("tool-output", content="retraction proof", source="s")
    ledger.retract_conclusion(old.id, reason="wrong", evidence_ids=[ev2.id])
    with pytest.raises(InvalidTransitionError):
        ledger.supersede_conclusion(old.id, "Again", [ev2.id], confidence=0.5)


def test_retract_records_reason_and_evidence(ledger):
    old = _belief(ledger)
    proof = ledger.add_evidence("tool-output", content="GET /v1 -> 404", source="smoke")
    updated = ledger.retract_conclusion(
        old.id, reason="Endpoint returns 404", evidence_ids=[proof.id]
    )
    assert updated.status is ConclusionStatus.RETRACTED
    assert updated.retraction_reason == "Endpoint returns 404"
    assert updated.retraction_evidence_ids == (proof.id,)
    assert ledger.active_conclusions() == ()


def test_retract_requires_reason(ledger):
    old = _belief(ledger)
    ev = ledger.add_evidence("note", content="proof")
    with pytest.raises(ValidationError):
        ledger.retract_conclusion(old.id, reason="  ", evidence_ids=[ev.id])


def test_retract_requires_existing_evidence(ledger):
    old = _belief(ledger)
    with pytest.raises(UnknownEvidenceError):
        ledger.retract_conclusion(old.id, reason="wrong", evidence_ids=["ev_9999"])


def test_retract_non_active_raises(ledger):
    old = _belief(ledger)
    ev2 = ledger.add_evidence("tool-output", content="v2", source="s")
    ledger.supersede_conclusion(old.id, "New belief", [ev2.id], confidence=0.7)
    with pytest.raises(InvalidTransitionError):
        ledger.retract_conclusion(old.id, reason="too late", evidence_ids=[ev2.id])


# ---------------- queries & provenance ----------------


def test_conclusions_for_evidence(ledger):
    ev = ledger.add_evidence("note", content="shared")
    c1 = ledger.add_conclusion("One", [ev.id], confidence=0.5)
    c2 = ledger.add_conclusion("Two", [ev.id], confidence=0.6)
    found = ledger.conclusions_for_evidence(ev.id)
    assert {c.id for c in found} == {c1.id, c2.id}


def test_evidence_trail_walks_the_whole_chain(ledger):
    e1 = ledger.add_evidence("note", content="v1 era")
    c1 = ledger.add_conclusion("Base is /v1", [e1.id], confidence=0.6)
    e2 = ledger.add_evidence("note", content="v2 era")
    c2 = ledger.supersede_conclusion(c1.id, "Base is /v2", [e2.id], confidence=0.7)
    e3 = ledger.add_evidence("note", content="v3 era")
    c3 = ledger.supersede_conclusion(c2.id, "Base is /v3", [e3.id], confidence=0.9)

    trail = ledger.evidence_trail(c3.id)
    assert [entry.conclusion.id for entry in trail.entries] == [c3.id, c2.id, c1.id]
    assert [entry.evidence[0].id for entry in trail.entries] == [e3.id, e2.id, e1.id]
    # The oldest link is the original belief, marked superseded.
    assert trail.entries[-1].conclusion.status is ConclusionStatus.SUPERSEDED


def test_evidence_trail_single_conclusion(ledger):
    c = _belief(ledger)
    trail = ledger.evidence_trail(c.id)
    assert len(trail.entries) == 1
    assert trail.entries[0].conclusion.id == c.id


# ---------------- stale finder ----------------


def test_find_stale_by_contradiction(ledger):
    c = _belief(ledger)
    ledger.add_evidence(
        "tool-output",
        content="GET /v1 -> 404",
        source="smoke",
        contradicts=[c.id],
    )
    findings = ledger.find_stale()
    assert len(findings) == 1
    assert findings[0].conclusion.id == c.id
    assert any("contradicted" in r for r in findings[0].reasons)


def test_find_stale_by_age(ledger, clock):
    ev = ledger.add_evidence("note", content="old news")
    c = ledger.add_conclusion("Old belief", [ev.id], confidence=0.5)
    clock.advance(3600)
    assert ledger.find_stale(max_evidence_age_seconds=7200) == ()
    findings = ledger.find_stale(max_evidence_age_seconds=1800)
    assert [f.conclusion.id for f in findings] == [c.id]


def test_find_stale_skips_closed_conclusions(ledger):
    c = _belief(ledger)
    proof = ledger.add_evidence(
        "note", content="proof", contradicts=[c.id]
    )
    ledger.retract_conclusion(c.id, reason="dead", evidence_ids=[proof.id])
    assert ledger.find_stale() == ()


def test_audit_log_records_the_lifecycle(ledger):
    c = _belief(ledger)
    ev2 = ledger.add_evidence("note", content="v2")
    ledger.supersede_conclusion(c.id, "New", [ev2.id], confidence=0.9)
    types = [e["type"] for e in ledger.events]
    assert types == [
        "evidence_added",
        "conclusion_added",
        "evidence_added",
        "conclusion_superseded",
    ]
