"""Error notes: a failed step stays recorded, with a truthful note beside it.

The rule, from Pakhomov et al., "Cleaning an Agent's History Hurts When
the World Still Holds the Error" (arXiv:2611.14654): when a history is
compressed or handed off, a truthful note beside the preserved error
(what went wrong, what the correct value is) raised recovery from 51%
to 66%, while rewriting the step to look completed dropped it from 50%
to 44%. stateledger therefore offers no way to rewrite a failed step —
only to annotate it, write-once, with the error intact.
"""

import pytest

from stateledger import (
    ErrorAnnotation,
    EvidenceForm,
    InvalidTransitionError,
    Ledger,
    UnknownEvidenceError,
    ValidationError,
    evidence_form_text,
    export_markdown,
    render_context,
)


def _failed_step(ledger):
    return ledger.add_evidence(
        "tool-output",
        content="POST /orders -> 200, order_id=4812, qty=5",
        source="order-script",
    )


# ----------------------------------------------------------------------
# recording and annotating
# ----------------------------------------------------------------------


def test_add_evidence_with_error_note_at_record_time(ledger, clock):
    note = ErrorAnnotation(
        what_went_wrong="qty was 5, the order asked for 50",
        correct_value="qty=50",
    )
    ev = ledger.add_evidence(
        "tool-output",
        content="POST /orders -> 200",
        source="order-script",
        error_note=note,
    )
    assert ev.error_note is not None
    assert ev.error_note.what_went_wrong == "qty was 5, the order asked for 50"
    assert ev.error_note.correct_value == "qty=50"
    # annotated_at defaults to the ledger clock when not given.
    assert ev.error_note.annotated_at == clock.t


def test_annotate_error_after_the_fact(ledger):
    ev = _failed_step(ledger)
    updated = ledger.annotate_error(
        ev.id,
        what_went_wrong="qty was 5, the order asked for 50",
        correct_value="qty=50",
    )
    assert updated.error_note is not None
    assert updated.error_note.what_went_wrong == "qty was 5, the order asked for 50"
    assert updated.error_note.correct_value == "qty=50"
    # The observation itself is untouched: same payload, same hash.
    assert updated.content == ev.content
    assert updated.hash == ev.hash
    assert updated.id == ev.id
    # The object fetched earlier still shows the pre-annotation state,
    # like conclusion status transitions.
    assert ev.error_note is None
    assert ledger.get_evidence(ev.id).error_note is not None


def test_annotate_error_without_correct_value(ledger):
    ev = _failed_step(ledger)
    updated = ledger.annotate_error(ev.id, "qty was wrong, correct qty unknown")
    assert updated.error_note.what_went_wrong == "qty was wrong, correct qty unknown"
    assert updated.error_note.correct_value is None


def test_annotate_error_is_write_once(ledger):
    ev = _failed_step(ledger)
    ledger.annotate_error(ev.id, "qty was 5, the order asked for 50")
    with pytest.raises(InvalidTransitionError, match="already has an error note"):
        ledger.annotate_error(ev.id, "a different story")
    # The first note survives the refused second attempt.
    assert ledger.get_evidence(ev.id).error_note.what_went_wrong.startswith("qty was 5")


def test_annotate_error_unknown_evidence(ledger):
    with pytest.raises(UnknownEvidenceError):
        ledger.annotate_error("ev_9999", "something went wrong")


def test_error_note_requires_what_went_wrong(ledger):
    ev = _failed_step(ledger)
    with pytest.raises(ValidationError, match="what went wrong"):
        ledger.annotate_error(ev.id, "   ")
    with pytest.raises(ValidationError, match="what went wrong"):
        ledger.add_evidence(
            "tool-output",
            content="POST /orders -> 200",
            error_note=ErrorAnnotation(what_went_wrong=""),
        )


def test_error_note_rejects_blank_correct_value(ledger):
    with pytest.raises(ValidationError, match="correct_value"):
        ledger.add_evidence(
            "tool-output",
            content="POST /orders -> 200",
            error_note=ErrorAnnotation(
                what_went_wrong="qty was wrong", correct_value="  "
            ),
        )


def test_annotate_error_is_audit_logged(ledger):
    ev = _failed_step(ledger)
    ledger.annotate_error(ev.id, "qty was 5, the order asked for 50")
    event_types = [e["type"] for e in ledger.events]
    assert "evidence_error_noted" in event_types
    noted = ledger.events[event_types.index("evidence_error_noted")]
    assert noted["evidence_id"] == ev.id


# ----------------------------------------------------------------------
# dedupe: the note is metadata, not part of the observation
# ----------------------------------------------------------------------


def test_error_note_does_not_affect_dedupe(ledger):
    first = ledger.add_evidence(
        "tool-output",
        content="POST /orders -> 200",
        error_note=ErrorAnnotation(what_went_wrong="qty was wrong"),
    )
    # Same observation, no note: dedupes to the first record, note kept.
    second = ledger.add_evidence("tool-output", content="POST /orders -> 200")
    assert second.id == first.id
    assert second.error_note is not None
    assert len(ledger.all_evidence()) == 1


def test_dedupe_first_record_wins_for_notes_too(ledger):
    first = ledger.add_evidence("tool-output", content="POST /orders -> 200")
    second = ledger.add_evidence(
        "tool-output",
        content="POST /orders -> 200",
        error_note=ErrorAnnotation(what_went_wrong="qty was wrong"),
    )
    assert second.id == first.id
    assert second.error_note is None  # first record wins, like summary
    # The post-hoc path still works on the surviving record.
    ledger.annotate_error(first.id, "qty was wrong")
    assert ledger.get_evidence(first.id).error_note is not None


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------


def test_error_note_survives_save_load_round_trip(ledger, tmp_path):
    ev = _failed_step(ledger)
    ledger.annotate_error(
        ev.id, "qty was 5, the order asked for 50", correct_value="qty=50"
    )
    path = tmp_path / "ledger.json"
    ledger.save(path)
    loaded = Ledger.load(path)
    note = loaded.get_evidence(ev.id).error_note
    assert note is not None
    assert note.what_went_wrong == "qty was 5, the order asked for 50"
    assert note.correct_value == "qty=50"


def test_ledger_saved_before_error_notes_loads_with_none(ledger):
    _failed_step(ledger)
    data = ledger.to_dict()
    for item in data["evidence"]:
        del item["error_note"]  # shape of a pre-0.3.0 file
    loaded = Ledger.from_dict(data)
    assert loaded.all_evidence()[0].error_note is None
    # ... and the old record can still be annotated after loading.
    loaded.annotate_error(loaded.all_evidence()[0].id, "qty was wrong")
    assert loaded.all_evidence()[0].error_note is not None


# ----------------------------------------------------------------------
# rendering: the note travels with the observation at every form
# ----------------------------------------------------------------------


def test_form_text_carries_note_in_summary_excerpt_and_full(ledger):
    ev = _failed_step(ledger)
    ledger.annotate_error(
        ev.id, "qty was 5, the order asked for 50", correct_value="qty=50"
    )
    ev = ledger.get_evidence(ev.id)
    for form in (
        EvidenceForm.SUMMARY,
        EvidenceForm.EXCERPT,
        EvidenceForm.FULL,
    ):
        text = evidence_form_text(ev, form)
        assert "[error noted: qty was 5, the order asked for 50" in text
        assert "correct value: qty=50]" in text
    # POINTER carries no payload at all, note included — by design.
    assert evidence_form_text(ev, EvidenceForm.POINTER) == ""


def test_form_text_note_without_correct_value(ledger):
    ev = _failed_step(ledger)
    ledger.annotate_error(ev.id, "qty was wrong")
    text = evidence_form_text(ledger.get_evidence(ev.id), EvidenceForm.SUMMARY)
    assert text.endswith("[error noted: qty was wrong]")
    assert "correct value" not in text


def test_excerpt_truncation_never_cuts_the_note(ledger):
    ev = ledger.add_evidence("tool-output", content="x" * 2000, source="s")
    ledger.annotate_error(ev.id, "wrong payload entirely", correct_value="see ticket 12")
    text = evidence_form_text(ledger.get_evidence(ev.id), EvidenceForm.EXCERPT)
    assert text.endswith(
        "[error noted: wrong payload entirely; correct value: see ticket 12]"
    )


def test_render_context_flags_annotated_evidence_at_pointer_form(ledger):
    ev = _failed_step(ledger)
    ledger.annotate_error(ev.id, "qty was wrong")
    result = render_context(ledger, evidence_form=EvidenceForm.POINTER)
    assert f"- {ev.id} · tool-output · [error noted]" in result.text


def test_render_context_carries_note_at_summary_form(ledger):
    ev = _failed_step(ledger)
    ledger.annotate_error(ev.id, "qty was 5", correct_value="qty=50")
    result = render_context(ledger, evidence_form=EvidenceForm.SUMMARY)
    assert "[error noted: qty was 5; correct value: qty=50]" in result.text


def test_export_markdown_shows_the_note(ledger):
    ev = _failed_step(ledger)
    ledger.annotate_error(ev.id, "qty was 5", correct_value="qty=50")
    text = export_markdown(ledger)
    assert "error noted: qty was 5; correct value: qty=50" in text
    # The original (wrong) observation is still in the export, verbatim.
    assert "qty=5" in text
