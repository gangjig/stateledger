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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import ConclusionStatus
from .store import Ledger

_SNIPPET_CHARS = 80


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


def render_context(ledger: Ledger, max_chars: int = 4000) -> RenderResult:
    """Render a prompt-ready state summary within ``max_chars``.

    Layout: active conclusions first, highest confidence first (ties
    broken by creation order), each with short evidence pointers; then,
    with whatever budget remains, the most recent evidence as one-line
    snippets. Greedy and deterministic: an item is included only if it
    fits whole. Everything left out is listed in the result with a
    reason — omission is reported, never silent.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

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
        line = f"- {ev.id} · {ev.kind} · {_snippet(ev.content, ev.ref)}\n"
        header_cost = 0
        if not evidence_section_open:
            header_cost = len("\n### Recent evidence\n")
        if used + header_cost + len(line) <= max_chars:
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
