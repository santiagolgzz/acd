"""Short notes about fallbacks taken while converting one ACD file.

When the converter replaces a failed read with a quiet fallback, it records
a note in the ConversionNotes owned by that conversion's ExportL5x. The notes
are written into the L5X as one XML comment, so someone reading the finished
file can tell "couldn't read" apart from "really empty".
"""

import re
from typing import List

from loguru import logger as log

# Characters that are illegal in XML 1.0 (same set as elements._XML_ILLEGAL_RE).
_XML_ILLEGAL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
# XML forbids "--" inside a comment.
_DOUBLE_HYPHEN_RE = re.compile("-{2,}")


class ConversionNotes:
    """The notes recorded during one conversion, in the order they occurred."""

    def __init__(self) -> None:
        self._notes: List[str] = []

    def add(self, note: str) -> None:
        """Record one plain-English note about a fallback."""
        self._notes.append(note)

    def warn(self, note: str, detail: str = "") -> None:
        """Log a warning and record the note. ``detail`` is logged only."""
        log.warning(f"{note} ({detail})" if detail else note)
        self._notes.append(note)

    def current(self) -> List[str]:
        """Return a copy of the notes recorded so far."""
        return list(self._notes)


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
