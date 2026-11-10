"""Evidence forms: one stored observation, several renderings.

An evidence item may carry a summary recorded with it. render_context
then chooses how much of each item to inject — pointer, summary,
excerpt, full text, or AUTO (richest form that fits the remaining
budget) — instead of every caller paying for one fixed representation.
"""

import pytest

from stateledger import (
    EvidenceForm,
    Ledger,
    ValidationError,
    evidence_form_text,
    export_markdown,
    render_context,
)

LONG_CONTENT = " ".join(f"line-{i} payload data" for i in range(60))  # ~1400 chars


# ----------------------------------------------------------------------
# recording summaries
# ----------------------------------------------------------------------


def test_summary_is_stored_on_the_evidence(ledger):
    ev = ledger.add_evidence(
        "tool-output", content="GET /v1 -> 404", summary="v1 endpoint is gone"
    )
    assert ev.summary == "v1 endpoint is gone"
    assert ledger.get_evidence(ev.id).summary == "v1 endpoint is gone"


def test_summary_defaults_to_none(ledger):
    ev = ledger.add_evidence("tool-output", content="GET /v1 -> 200")
    assert ev.summary is None


def test_blank_summary_is_rejected(ledger):
    with pytest.raises(ValidationError):
        ledger.add_evidence("tool-output", content="x", summary="   ")


def test_summary_is_not_part_of_the_dedupe_hash(ledger):
    first = ledger.add_evidence(
        "tool-output", content="same observation", summary="first summary"
    )
    second = ledger.add_evidence(
        "tool-output", content="same observation", summary="another summary"
    )
    # Same observation: dedupes to the first record, summary included.
    assert second.id == first.id
    assert second.summary == "first summary"
    assert len(ledger.all_evidence()) == 1


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------


def test_summary_survives_a_save_load_round_trip(ledger, tmp_path):
    ev = ledger.add_evidence(
        "tool-output", content="payload", summary="what the payload shows"
    )
    path = ledger.save(tmp_path / "ledger.json")
    loaded = Ledger.load(path)
    assert loaded.get_evidence(ev.id).summary == "what the payload shows"


def test_ledger_saved_before_summaries_existed_still_loads(ledger):
    ledger.add_evidence("tool-output", content="payload")
    data = ledger.to_dict()
    for item in data["evidence"]:
        item.pop("summary", None)  # simulate the pre-0.2 file shape
    loaded = Ledger.from_dict(data)
    assert loaded.all_evidence()[0].summary is None


# ----------------------------------------------------------------------
# the forms themselves
# ----------------------------------------------------------------------


def test_pointer_form_has_no_payload(ledger):
    ev = ledger.add_evidence("tool-output", content=LONG_CONTENT, summary="s")
    assert evidence_form_text(ev, EvidenceForm.POINTER) == ""


def test_summary_form_prefers_the_recorded_summary(ledger):
    ev = ledger.add_evidence(
        "tool-output", content=LONG_CONTENT, summary="the recorded summary"
    )
    assert evidence_form_text(ev, "summary") == "the recorded summary"


def test_summary_form_falls_back_to_a_snippet(ledger):
    ev = ledger.add_evidence("tool-output", content=LONG_CONTENT)
    text = evidence_form_text(ev, EvidenceForm.SUMMARY)
    assert text.startswith("line-0 payload data")
    assert len(text) <= 80


def test_excerpt_form_truncates_between_summary_and_full(ledger):
    ev = ledger.add_evidence("tool-output", content=LONG_CONTENT)
    excerpt = evidence_form_text(ev, EvidenceForm.EXCERPT)
    full = evidence_form_text(ev, EvidenceForm.FULL)
    assert full == LONG_CONTENT
    assert 80 < len(excerpt) < len(full)
    assert excerpt.endswith("…")


def test_full_form_of_ref_only_evidence_is_the_ref_pointer(ledger):
    ev = ledger.add_evidence("file", ref="/var/log/app.log")
    assert evidence_form_text(ev, EvidenceForm.FULL) == "ref: /var/log/app.log"
    assert evidence_form_text(ev, EvidenceForm.EXCERPT) == "ref: /var/log/app.log"


def test_auto_is_not_a_form(ledger):
    ev = ledger.add_evidence("tool-output", content="x")
    with pytest.raises(ValueError):
        evidence_form_text(ev, EvidenceForm.AUTO)


# ----------------------------------------------------------------------
# render_context form selection
# ----------------------------------------------------------------------


def test_render_context_default_uses_the_recorded_summary(ledger):
    ledger.add_evidence(
        "tool-output", content=LONG_CONTENT, summary="short and cheap"
    )
    result = render_context(ledger, max_chars=100_000)
    assert "short and cheap" in result.text
    assert LONG_CONTENT not in result.text


def test_render_context_full_form_injects_the_whole_content(ledger):
    ledger.add_evidence("tool-output", content=LONG_CONTENT, summary="s")
    result = render_context(
        ledger, max_chars=100_000, evidence_form=EvidenceForm.FULL
    )
    assert LONG_CONTENT in result.text


def test_render_context_pointer_form_injects_no_payload(ledger):
    ledger.add_evidence("tool-output", content=LONG_CONTENT, summary="s")
    result = render_context(
        ledger, max_chars=100_000, evidence_form="pointer"
    )
    assert "ev_0001 · tool-output" in result.text
    assert "payload data" not in result.text
    assert result.included_evidence_ids == ["ev_0001"]


def test_render_context_rejects_an_unknown_form(ledger):
    ledger.add_evidence("tool-output", content="x")
    with pytest.raises(ValueError):
        render_context(ledger, evidence_form="everything")


def test_auto_picks_full_when_the_budget_is_roomy(ledger):
    ledger.add_evidence("tool-output", content=LONG_CONTENT, summary="s")
    result = render_context(ledger, max_chars=100_000, evidence_form="auto")
    assert LONG_CONTENT in result.text
    assert result.included_evidence_ids == ["ev_0001"]


def test_auto_degrades_to_the_summary_when_the_budget_is_tight(ledger):
    ledger.add_evidence(
        "tool-output", content=LONG_CONTENT, summary="cheap summary"
    )
    # Room for the header + section header + the summary line, but not
    # for the ~1400-char full text or the ~400-char excerpt.
    result = render_context(ledger, max_chars=200, evidence_form="auto")
    assert "cheap summary" in result.text
    assert LONG_CONTENT not in result.text
    assert result.included_evidence_ids == ["ev_0001"]
    assert result.omitted_evidence_ids == []


def test_auto_omits_and_reports_when_not_even_the_summary_fits(ledger):
    ledger.add_evidence(
        "tool-output", content=LONG_CONTENT, summary="cheap summary"
    )
    result = render_context(ledger, max_chars=40, evidence_form="auto")
    assert result.included_evidence_ids == []
    assert result.omitted_evidence_ids == ["ev_0001"]


def test_fixed_form_still_omits_whole_items_that_do_not_fit(ledger):
    ledger.add_evidence("tool-output", content=LONG_CONTENT, summary="s")
    result = render_context(
        ledger, max_chars=200, evidence_form=EvidenceForm.FULL
    )
    assert result.included_evidence_ids == []
    assert result.omitted_evidence_ids == ["ev_0001"]


# ----------------------------------------------------------------------
# markdown export
# ----------------------------------------------------------------------


def test_markdown_export_shows_the_summary(ledger):
    ledger.add_evidence(
        "tool-output", content="GET /v1 -> 404", summary="v1 endpoint is gone"
    )
    md = export_markdown(ledger)
    assert "summary: v1 endpoint is gone" in md
