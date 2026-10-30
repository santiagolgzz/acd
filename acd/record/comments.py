import re
import struct
from dataclasses import dataclass
from sqlite3 import Cursor
from typing import Optional, Tuple

from acd.database.dbextract import DatRecord
from acd.generated.comments.fafa_coments import FafaComents


@dataclass
class CommentsRecord:
    _cur: Cursor
    dat_record: DatRecord

    def __post_init__(self):
        entry = CommentsRecord.parse(self.dat_record)
        if entry is not None:
            self._cur.execute("INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", entry)

    @staticmethod
    def _parse_udi_body(body: bytes) -> Optional[tuple]:
        """Parse a UDI (type-12) fafa record body.

        UDI records store metadata like the AOI RevisionNote.  The body layout is:
          [0:8]   8 bytes unknown
          [8:12]  4 bytes some_id
          [12:16] 4 bytes flags
          [16:]   UTF-16LE null-terminated UDI-type string (e.g. "UDI_HISTORY")
                  followed by null padding, then a null-terminated ASCII text string.

        Returns (udi_type, text) or None if the structure is not recognized.
        """
        if len(body) < 20:
            return None
        try:
            # UDI type string starts at offset 16 (after 8 unknown + 4 id + 4 flags).
            utf16_start = 16
            pos = utf16_start
            code_units = []
            while pos + 1 < len(body):
                cu = struct.unpack_from("<H", body, pos)[0]
                if cu == 0:
                    break
                code_units.append(cu)
                pos += 2
            udi_type = "".join(chr(cu) for cu in code_units)
            # Skip null terminator and any subsequent null padding.
            pos += 2
            while pos < len(body) and body[pos] == 0:
                pos += 1
            # Read null-terminated ASCII text.
            text_end = body.find(b"\x00", pos)
            if text_end <= pos:
                return None
            text = body[pos:text_end].decode("utf-8", errors="replace")
            return (udi_type, text)
        except Exception:
            return None

    @staticmethod
    def _utf16z(buf: bytes, pos: int) -> Tuple[str, int]:
        """Read a null-terminated UTF-16LE string; return it and the offset after the null."""
        end = pos
        while end + 1 < len(buf) and buf[end : end + 2] != b"\0\0":
            end += 2
        return buf[pos:end].decode("utf-16-le", errors="replace"), end + 2

    @staticmethod
    def parse_legacy(dat_record: DatRecord) -> Optional[tuple]:
        """Parse a comment record of the legacy (v19/v20) layout.

        The header has no sub_record_length: u32 record_length, u16 seq_number,
        u16 record_type, u32 parent. Text is UTF-16LE. Keys match the current
        layout: parent is the owning scope, the next u32 the member (routine or
        tag) key, and for rung comments the u32 after it the rung key.
          type 1:    member_ref @12, rung_content @16, text @29
          type 3, 4: member_ref @12, tag_reference @27, then text
        Other record types are not decoded.
        """
        if dat_record.identifier != 64250:
            return None
        buf = bytes(dat_record.record.record_buffer)
        if len(buf) < 30:
            return None
        seq_number, record_type, parent, member_ref, rung_content = struct.unpack_from(
            "<HHIII", buf, 4
        )
        if record_type == 1:
            text, _ = CommentsRecord._utf16z(buf, 29)
            tag_ref = ""
        elif record_type in (3, 4):
            tag_ref, pos = CommentsRecord._utf16z(buf, 27)
            text, _ = CommentsRecord._utf16z(buf, pos)
            rung_content = 0
        else:
            return None
        return (
            seq_number, 0, 1, text, record_type, parent, tag_ref, rung_content, member_ref,
        )

    @staticmethod
    def parse(dat_record: DatRecord) -> Optional[tuple]:
        if dat_record.identifier != 64250:
            return None
        try:
            r = FafaComents.from_bytes(dat_record.record.record_buffer)
            # Type-12 (0x0C) records carry UDI metadata such as the AOI RevisionNote.
            # The body is raw bytes; parse it to extract the text.
            if r.header.record_type == 12:
                parsed = CommentsRecord._parse_udi_body(bytes(r.body))
                if parsed is None:
                    return None
                udi_type, text = parsed
                # Only store UDI_HISTORY records (RevisionNote) for now.
                if udi_type != "UDI_HISTORY":
                    return None
                return (
                    r.header.seq_number,
                    r.header.sub_record_length,
                    1,              # object_id placeholder (not used for lookup)
                    text,
                    r.header.record_type,
                    r.header.parent,
                    "__REVISION_NOTE__",
                    0,              # rung_content
                    0,              # member_ref
                )
            # A type-1 record's object_id says what kind of text it holds:
            # 1 is a user comment; other values are internal strings (member
            # names, localization metadata) laid out differently, which would
            # otherwise be read as a truncated description.
            if r.header.record_type == 0x01 and r.body.object_id != 1:
                return None
            if r.header.record_type in (0x03, 0x04, 0x0D, 0x0E):
                tag_ref = r.body.tag_reference.value
            else:
                tag_ref = ""
            # For AsciiRecord (type 1 or 2), extract bytes [4:8] of unknown_1.
            # This value is non-zero for rung-level comments and zero for internal
            # metadata strings (FBDRoutineDescription, MainProgramLocalTagDescription, etc.).
            if r.header.record_type in (0x01, 0x02) and len(bytes(r.body.unknown_1)) >= 8:
                rung_content = struct.unpack_from("<I", bytes(r.body.unknown_1), 4)[0]
            else:
                rung_content = 0
            # Extract bytes [0:4] of unknown_1 as member_ref.
            # For the object's own description (DataType, AOI, etc.) this is zero.
            # For sub-element descriptions (UDT members, AOI parameters/local tags)
            # this is non-zero, enabling callers to filter to just the object-level description.
            if r.header.record_type in (0x01, 0x02) and len(bytes(r.body.unknown_1)) >= 4:
                member_ref = struct.unpack_from("<I", bytes(r.body.unknown_1), 0)[0]
            else:
                member_ref = 0
            return (
                r.header.seq_number,
                r.header.sub_record_length,
                r.body.object_id,
                r.body.record_string,
                r.header.record_type,
                r.header.parent,
                tag_ref,
                rung_content,
                member_ref,
            )
        except Exception:
            return None

    def replace_tag_references(self, sb_rec):
        m = re.findall("@[A-Za-z0-9]*@", sb_rec)
        for tag in m:
            tag_no = tag[1:-1]
            tag_id = int(tag_no, 16)
            self._cur.execute(
                "SELECT object_id, comp_name FROM comps WHERE object_id=" + str(tag_id)
            )
            results = self._cur.fetchall()
            sb_rec = sb_rec.replace(tag, results[0][1])
        return sb_rec
