"""JSON persistence: round-trip fidelity and atomic saves."""

import json

import pytest

from stateledger import ConclusionStatus, Ledger, ValidationError


def _populated(ledger) -> Ledger:
    e1 = ledger.add_evidence("tool-output", content="GET /v1 -> 200", source="smoke")
    c1 = ledger.add_conclusion("Base is /v1", [e1.id], confidence=0.8)
    e2 = ledger.add_evidence("tool-output", content="GET /v1 -> 404", source="smoke")
    ledger.supersede_conclusion(c1.id, "Base is /v2", [e2.id], confidence=0.95)
    e3 = ledger.add_evidence("note", content="scratch belief source")
    c3 = ledger.add_conclusion("Scratch belief", [e3.id], confidence=0.3)
    ledger.retract_conclusion(c3.id, reason="never true", evidence_ids=[e2.id])
    return ledger


def test_round_trip_dict_equality(ledger):
    _populated(ledger)
    restored = Ledger.from_dict(ledger.to_dict())
    assert restored.to_dict() == ledger.to_dict()


def test_round_trip_preserves_statuses_and_links(ledger):
    _populated(ledger)
    restored = Ledger.from_dict(ledger.to_dict())
    statuses = {c.id: c.status for c in restored.all_conclusions()}
    assert statuses == {
        "c_0001": ConclusionStatus.SUPERSEDED,
        "c_0002": ConclusionStatus.ACTIVE,
        "c_0003": ConclusionStatus.RETRACTED,
    }
    assert restored.get_conclusion("c_0002").supersedes == "c_0001"
    assert restored.get_conclusion("c_0003").retraction_reason == "never true"
    assert len(restored.all_evidence()) == 3
    assert len(restored.events) == len(ledger.events)


def test_save_and_load_file(ledger, tmp_path):
    _populated(ledger)
    path = ledger.save(tmp_path / "ledger.json")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    restored = Ledger.load(path)
    assert restored.to_dict() == ledger.to_dict()


def test_save_is_atomic_no_temp_files_left(ledger, tmp_path):
    _populated(ledger)
    ledger.save(tmp_path / "ledger.json")
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_counters_continue_after_load(ledger):
    _populated(ledger)
    restored = Ledger.from_dict(ledger.to_dict())
    ev = restored.add_evidence("note", content="fresh")
    assert ev.id == "ev_0004"  # no collision with loaded ids
    c = restored.add_conclusion("Fresh belief", [ev.id], confidence=0.5)
    assert c.id == "c_0004"


def test_dedupe_still_works_after_load(ledger):
    ev = ledger.add_evidence("note", content="same")
    restored = Ledger.from_dict(ledger.to_dict())
    again = restored.add_evidence("note", content="same")
    assert again.id == ev.id
    assert len(restored.all_evidence()) == 1


def test_from_dict_rejects_unknown_schema():
    with pytest.raises(ValidationError):
        Ledger.from_dict({"schema_version": 999})
