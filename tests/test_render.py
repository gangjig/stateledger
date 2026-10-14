"""Rendering: Markdown export and budget-limited context."""

from stateledger import export_markdown, render_context


def _populated(ledger):
    e1 = ledger.add_evidence("tool-output", content="GET /v1 -> 200", source="smoke")
    c1 = ledger.add_conclusion("Base is /v1", [e1.id], confidence=0.8)
    e2 = ledger.add_evidence("tool-output", content="GET /v1 -> 404", source="smoke")
    c2 = ledger.supersede_conclusion(c1.id, "Base is /v2", [e2.id], confidence=0.95)
    return c1, c2


def test_markdown_export_sections_and_provenance(ledger):
    c1, c2 = _populated(ledger)
    md = export_markdown(ledger)
    assert "## Evidence log" in md
    assert "## Active conclusions" in md
    assert "## Superseded & retracted conclusions" in md
    # Active belief shown with its evidence pointer...
    assert "Base is /v2" in md
    assert c2.evidence_ids[0] in md
    # ...and the superseded belief is still auditable, with the link.
    assert "Base is /v1" in md
    assert "superseded" in md
    assert c2.id in md


def test_markdown_export_writes_file(ledger, tmp_path):
    _populated(ledger)
    path = tmp_path / "ledger.md"
    md = export_markdown(ledger, path)
    assert path.read_text(encoding="utf-8") == md


def test_markdown_export_empty_ledger(ledger):
    md = export_markdown(ledger)
    assert "no evidence recorded" in md


def test_render_context_includes_everything_when_budget_allows(ledger):
    _populated(ledger)
    result = render_context(ledger, max_chars=100_000)
    assert result.included_conclusion_ids == ["c_0002"]
    assert result.omitted_conclusions == []
    assert "Base is /v2" in result.text
    assert "ev_0002 (tool-output)" in result.text  # evidence pointer
    assert result.char_count == len(result.text)


def test_render_context_orders_by_confidence(ledger):
    ev = ledger.add_evidence("note", content="basis")
    ledger.add_conclusion("Low", [ev.id], confidence=0.3)
    ledger.add_conclusion("High", [ev.id], confidence=0.9)
    ledger.add_conclusion("Mid", [ev.id], confidence=0.6)
    result = render_context(ledger, max_chars=100_000)
    assert result.included_conclusion_ids == ["c_0002", "c_0003", "c_0001"]
    assert result.text.index("High") < result.text.index("Mid") < result.text.index("Low")


def test_render_context_omission_is_reported_never_silent(ledger):
    ev = ledger.add_evidence("note", content="basis")
    c_low = ledger.add_conclusion("A low-confidence belief", [ev.id], confidence=0.2)
    c_high = ledger.add_conclusion("A high-confidence belief", [ev.id], confidence=0.9)
    # Budget fits the header + roughly one conclusion block.
    result = render_context(ledger, max_chars=170)
    assert result.char_count <= 170
    assert result.included_conclusion_ids == [c_high.id]
    omitted_ids = [o.id for o in result.omitted_conclusions]
    assert omitted_ids == [c_low.id]
    assert result.omitted_conclusions[0].reason
    # Nothing vanishes: included + omitted == all active conclusions.
    assert set(result.included_conclusion_ids) | set(omitted_ids) == {
        c_low.id,
        c_high.id,
    }
    assert c_low.statement not in result.text


def test_render_context_tiny_budget_omits_everything_but_reports(ledger):
    ev = ledger.add_evidence("note", content="basis")
    c = ledger.add_conclusion("Some belief", [ev.id], confidence=0.9)
    result = render_context(ledger, max_chars=40)
    assert result.included_conclusion_ids == []
    assert [o.id for o in result.omitted_conclusions] == [c.id]


def test_render_context_rejects_nonpositive_budget(ledger):
    import pytest

    with pytest.raises(ValueError):
        render_context(ledger, max_chars=0)
