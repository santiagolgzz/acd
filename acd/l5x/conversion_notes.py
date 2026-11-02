"""Short notes about fallbacks taken while converting one ACD file.

When the converter replaces a failed read with a quiet fallback, it logs a
warning and also records a note here. The notes are written into the L5X as
one XML comment, so someone reading the finished file can tell "couldn't
read" apart from "really empty".

Conversions run one at a time, so a single module-level list is enough.
ExportL5x calls reset() when it starts.
"""

import re
from typing import List

# Characters that are illegal in XML 1.0. acd.l5x.elements imports this pattern.
_XML_ILLEGAL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
# XML forbids "--" inside a comment.
_DOUBLE_HYPHEN_RE = re.compile("-{2,}")

_notes: List[str] = []


def reset() -> None:
    """Forget the notes from the previous conversion."""
    _notes.clear()


def add(note: str) -> None:
    """Record one plain-English note about a fallback."""
    _notes.append(note)


def current() -> List[str]:
    """Return a copy of the notes recorded since the last reset()."""
    return list(_notes)


def xml_comment(notes: List[str]) -> str:
    """Return the notes as one XML comment, or "" when there are none.

    Each note gets its own line. Line breaks, illegal XML characters and
    "--" runs are removed or replaced so the comment stays well-formed.
    """
    if not notes:
        return ""
    lines = []
    for note in notes:
        text = _XML_ILLEGAL_RE.sub("", " ".join(note.splitlines()))
        lines.append(_DOUBLE_HYPHEN_RE.sub("-", text))
    return "<!-- acd-tools conversion notes:\n" + "\n".join(lines) + "\n-->\n"
