"""Rendering: human-readable exports and prompt-ready context.

Two output shapes:

* ``export_markdown`` — the whole ledger as a readable document: the
  evidence log and every conclusion with its provenance. This is the
  artifact a human audits.
* ``render_context`` — a budget-limited state summary to put in a
  prompt. Active conclusions come first, highest confidence first,
  each with short evidence pointers. Anything that does not fit is
  *reported* in the result, never silently dropped: an agent that
  cannot see part of its state should at least know that it cannot.

Evidence is stored once but can be *rendered* in several forms (see
``EvidenceForm``): a bare pointer, a one-line summary, an excerpt, or
the full text. ``render_context`` picks the form per call — or, with
``EvidenceForm.AUTO``, per item: the richest form that still fits the
remaining budget. The same observation can therefore cost a handful of
characters in a tight prompt and its full text in a roomy one, instead
of every caller paying for one fixed representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Union

from .models import ConclusionStatus, Evidence
from .store import Ledger

_SNIPPET_CHARS = 80
_EXCERPT_CHARS = 400


class EvidenceForm(str, Enum):
    """How much of an evidence item a rendering includes.

    POINTER — id and kind only; the reader knows the observation
        exists and can fetch it, but sees none of its payload.
    SUMMARY — the summary recorded with the evidence, or (when none
        was recorded) a one-line snippet of the content.
    EXCERPT — the content with whitespace collapsed, truncated to
        ``_EXCERPT_CHARS`` characters.
    FULL — the entire inline content, verbatim. Ref-only evidence has
        no inline payload, so FULL renders its ref pointer.
    AUTO — not a form itself: ``render_context`` resolves it per item
        to the richest of FULL / EXCERPT / SUMMARY that fits the
        remaining budget, and reports the item as omitted when not
        even SUMMARY fits.
    """

    POINTER = "pointer"
    SUMMARY = "summary"
    EXCERPT = "excerpt"
    FULL = "full"
    AUTO = "auto"


# Richest-first order AUTO degrades through.
_FORM_RICHEST_FIRST = (EvidenceForm.FULL, EvidenceForm.EXCERPT, EvidenceForm.SUMMARY)


def _error_note_suffix(ev: Evidence) -> str:
    """The error-note suffix for payload-bearing forms, or "".

    A failed step's note travels with the observation at every form,
    and is appended *after* any truncation, so compressing a history
    for handoff can never silently drop the fact that the step failed
    (or what the correct value was).
    """
    note = ev.error_note
    if note is None:
        return ""
    parts = [" ".join(note.what_went_wrong.split())]
    if note.correct_value:
        parts.append(f"correct value: {' '.join(note.correct_value.split())}")
    return " [error noted: " + "; ".join(parts) + "]"


def evidence_form_text(ev: Evidence, form: Union[EvidenceForm, str]) -> str:
    """The payload text of one evidence item in one concrete form.

    ``form`` must be a concrete form (POINTER, SUMMARY, EXCERPT or
    FULL); AUTO is a per-render policy, not a form, and is rejected.
    POINTER returns an empty string: the pointer is the id/kind framing
    the caller adds, with no payload at all.
    """
    form = EvidenceForm(form)
    if form is EvidenceForm.AUTO:
        raise ValueError("AUTO is a render policy, not a form; "
                         "resolve it against a budget first")
    if form is EvidenceForm.POINTER:
        return ""
    if form is EvidenceForm.SUMMARY:
        if ev.summary:
            return " ".join(ev.summary.split()) + _error_note_suffix(ev)
        return _snippet(ev.content, ev.ref) + _error_note_suffix(ev)
    if ev.content is None:
        # Ref-only evidence: no inline payload exists at any form.
        base = f"ref: {ev.ref}" if ev.ref else ""
        return base + _error_note_suffix(ev)
    if form is EvidenceForm.EXCERPT:
        body = " ".join(ev.content.split())
        if len(body) > _EXCERPT_CHARS:
            body = body[: _EXCERPT_CHARS - 1] + "…"
        return body + _error_note_suffix(ev)
    return ev.content + _error_note_suffix(ev)  # FULL: verbatim + note


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _snippet(text: Optional[str], ref: Optional[str]) -> str:
    body = text if text is not None else (f"ref: {ref}" if ref else "")
    body = " ".join(body.split())
    if len(body) > _SNIPPET_CHARS:
        body = body[: _SNIPPET_CHARS - 1] + "…"
    return body


# ----------------------------------------------------------------------
# Markdown export
# ----------------------------------------------------------------------


def export_markdown(ledger: Ledger, path: Optional[str | Path] = None) -> str:
    """Render the full ledger as Markdown. Writes to ``path`` if given."""
    lines: list[str] = ["# stateledger export", ""]

    lines.append("## Evidence log")
    lines.append("")
    evidence = ledger.all_evidence()
    if not evidence:
        lines.append("_(no evidence recorded)_")
    for ev in evidence:
        lines.append(
            f"- **{ev.id}** · {ev.kind} · {_ts(ev.timestamp)} · source: {ev.source}"
        )
        body = _snippet(ev.content, ev.ref)
        if body:
            lines.append(f"  - {body}")
        if ev.summary:
            lines.append(f"  - summary: {' '.join(ev.summary.split())}")
        if ev.error_note:
            note = ev.error_note
            line = f"  - error noted: {' '.join(note.what_went_wrong.split())}"
            if note.correct_value:
                line += f"; correct value: {' '.join(note.correct_value.split())}"
            lines.append(line)
        if ev.contradicts:
            lines.append(
                f"  - contradicts: {', '.join(ev.contradicts)}"
            )
    lines.append("")

    lines.append("## Active conclusions")
    lines.append("")
    active = ledger.active_conclusions()
    if not active:
        lines.append("_(none)_")
    for c in active:
        lines.append(f"- **{c.id}** (confidence {c.confidence:.2f}) {c.statement}")
        lines.append(f"  - evidence: {', '.join(c.evidence_ids)}")
        if c.supersedes:
            lines.append(f"  - supersedes: {c.supersedes}")
    lines.append("")

    closed = [
        c
        for c in ledger.all_conclusions()
        if c.status is not ConclusionStatus.ACTIVE
    ]
    lines.append("## Superseded & retracted conclusions")
    lines.append("")
    if not closed:
        lines.append("_(none)_")
    for c in closed:
        lines.append(f"- **{c.id}** [{c.status.value}] {c.statement}")
        lines.append(f"  - evidence: {', '.join(c.evidence_ids)}")
        if c.superseded_by:
            lines.append(f"  - superseded by: {c.superseded_by}")
        if c.retraction_reason:
            lines.append(f"  - retracted: {c.retraction_reason}")
            lines.append(
                f"  - retraction evidence: {', '.join(c.retraction_evidence_ids)}"
            )
    lines.append("")

    text = "\n".join(lines)
    if path is not None:
        Path(path).write_text(text, encoding="utf-8")
    return text


# ----------------------------------------------------------------------
# Budget-limited context rendering
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class OmittedItem:
    id: str
    reason: str


@dataclass
class RenderResult:
    text: str
    included_conclusion_ids: list[str] = field(default_factory=list)
    omitted_conclusions: list[OmittedItem] = field(default_factory=list)
    included_evidence_ids: list[str] = field(default_factory=list)
    omitted_evidence_ids: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)


_HEADER = "## Current state (stateledger)\n"


def _conclusion_block(ledger: Ledger, conclusion) -> str:
    pointers = []
    for ev_id in conclusion.evidence_ids:
        ev = ledger.get_evidence(ev_id)
        pointers.append(f"{ev.id} ({ev.kind})")
    return (
        f"- [{conclusion.id}] (confidence {conclusion.confidence:.2f}) "
        f"{conclusion.statement}\n"
        f"  evidence: {', '.join(pointers)}\n"
    )


def _evidence_line(ev: Evidence, form: EvidenceForm) -> str:
    payload = evidence_form_text(ev, form)
    if payload:
        return f"- {ev.id} · {ev.kind} · {payload}\n"
    if ev.error_note is not None:
        # POINTER carries no payload, but a failed step must still be
        # flagged: the reader knows an error note exists and can fetch
        # the observation to read it.
        return f"- {ev.id} · {ev.kind} · [error noted]\n"
    return f"- {ev.id} · {ev.kind}\n"


def render_context(
    ledger: Ledger,
    max_chars: int = 4000,
    evidence_form: Union[EvidenceForm, str] = EvidenceForm.SUMMARY,
) -> RenderResult:
    """Render a prompt-ready state summary within ``max_chars``.

    Layout: active conclusions first, highest confidence first (ties
    broken by creation order), each with short evidence pointers; then,
    with whatever budget remains, the most recent evidence not already
    pointed at, rendered in ``evidence_form``. Greedy and
    deterministic: an item is included only if it fits whole.
    Everything left out is listed in the result with a reason —
    omission is reported, never silent.

    ``evidence_form`` selects how much of each evidence item is
    injected (see ``EvidenceForm``). The default, SUMMARY, matches the
    historical one-line-snippet behaviour, except that an item with a
    recorded summary renders that summary instead of a raw snippet.
    AUTO resolves per item to the richest form that fits the budget
    left at that point, so early (newer) items get more room than
    later ones — evidence on demand, priced by the budget.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    form = EvidenceForm(evidence_form)

    result = RenderResult(text="")
    parts: list[str] = [_HEADER]
    used = len(_HEADER)

    ordered = sorted(
        ledger.active_conclusions(),
        key=lambda c: (-c.confidence, c.created_at, c.id),
    )
    for c in ordered:
        block = _conclusion_block(ledger, c)
        if used + len(block) <= max_chars:
            parts.append(block)
            used += len(block)
            result.included_conclusion_ids.append(c.id)
        else:
            result.omitted_conclusions.append(
                OmittedItem(id=c.id, reason="did not fit in remaining budget")
            )

    referenced = {
        ev_id
        for cid in result.included_conclusion_ids
        for ev_id in ledger.get_conclusion(cid).evidence_ids
    }
    evidence_section_open = False
    for ev in reversed(ledger.all_evidence()):  # newest first
        if ev.id in referenced:
            continue  # already pointed at from a conclusion
        header_cost = 0
        if not evidence_section_open:
            header_cost = len("\n### Recent evidence\n")
        if form is EvidenceForm.AUTO:
            # Richest form that fits wins; if not even SUMMARY fits,
            # the item is omitted (and reported) rather than shrunk
            # below its cheapest meaningful form.
            line = None
            for candidate in _FORM_RICHEST_FIRST:
                attempt = _evidence_line(ev, candidate)
                if used + header_cost + len(attempt) <= max_chars:
                    line = attempt
                    break
        else:
            attempt = _evidence_line(ev, form)
            line = attempt if used + header_cost + len(attempt) <= max_chars else None
        if line is not None:
            if not evidence_section_open:
                parts.append("\n### Recent evidence\n")
                used += header_cost
                evidence_section_open = True
            parts.append(line)
            used += len(line)
            result.included_evidence_ids.append(ev.id)
        else:
            result.omitted_evidence_ids.append(ev.id)

    result.text = "".join(parts)
    return result
