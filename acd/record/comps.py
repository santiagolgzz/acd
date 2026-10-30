import struct
from dataclasses import dataclass
from io import BytesIO
from sqlite3 import Cursor
from typing import Optional, Tuple

from acd.database.dbextract import DatRecord
from kaitaistruct import KaitaiStream

from acd.generated.comps.fafa_comps import FafaComps
from acd.generated.comps.fdfd_comps import FdfdComps


@dataclass
class RecordData:
    object_id: int
    record_length: int
    seq_number: int
    record_type: int
    dat_record: DatRecord


@dataclass
class CompsRecord:
    _cur: Cursor
    dat_record: DatRecord

    def __post_init__(self):
        entry = CompsRecord.parse(self.dat_record)
        if entry is None:
            return
        self._cur.execute(f"DELETE FROM comps WHERE object_id={entry[0]}")
        self._cur.execute("INSERT INTO comps VALUES (?, ?, ?, ?, ?, ?)", entry)

    @staticmethod
    def legacy_comment_keys(dat_record: DatRecord) -> Optional[Tuple[int, int]]:
        """(scope key, member key) of a legacy (v20 and earlier) FAFA record.

        The legacy record keeps the same Rx header fields as the current
        layout's 0x08-0x12 at raw 0x66-0x70, inside the area parse() skips:
        u16 format, then (cip_type, comment_id) of the owning scope and of the
        object itself. Comments are filed under parent = scope key and
        member_ref = own key; objects without a scope (controller tags) carry
        their own key first and member key 0.
        """
        if dat_record.identifier != 64250:
            return None
        raw = bytes(dat_record.record.record_buffer)
        if len(raw) < 0x70 or struct.unpack_from("<I", raw, 0x0C)[0] == 0:
            return None
        scope_cip, scope_cid, own_cip, own_cid = struct.unpack_from("<HHHH", raw, 0x68)
        if not scope_cip:
            return None
        member_key = (own_cid * 0x10000) + own_cip if own_cip else 0
        return (scope_cid * 0x10000) + scope_cip, member_key

    @staticmethod
    def data_value(dat_record: DatRecord) -> Optional[bytes]:
        """Return the stored tag data (Rx attribute 0x66) of a FAFA record.

        Tag data objects (children of RxDataCollection, referenced by a tag's
        data_table_instance) keep the tag's raw value in attribute 0x66, the
        record's last attribute (kept whole by parse()).
        """
        if dat_record.identifier != 64250:
            return None
        entry = CompsRecord.parse(dat_record)
        if entry is None:
            return None
        raw = bytes(dat_record.record.record_buffer)
        # Attributes start at 32 of the legacy payload (see LegacyRxGeneric)
        # and at 82 of the Rx payload (74-byte header and main record, then
        # len_record and count_record).
        pos = 32 if struct.unpack_from("<I", raw, 0x0C)[0] != 0 else 82
        return CompsRecord._attribute(bytes(entry[5]), pos, 0x66)

    @staticmethod
    def alias_path(dat_record: DatRecord) -> Optional[bytes]:
        """Return the alias path (Rx attribute 0x65) of a current-layout tag.

        Alias tags keep their target as a utf-16 path of object IDs, the same
        form legacy files use (e.g. "@<tag id>@.@<member id>@.13"), as the tag
        record's last attribute. Legacy records are read through
        LegacyRxGeneric instead and return None here.
        """
        if dat_record.identifier != 64250:
            return None
        raw = bytes(dat_record.record.record_buffer)
        if len(raw) < 158 or struct.unpack_from("<I", raw, 0x0C)[0] != 0:
            return None
        entry = CompsRecord.parse(dat_record)
        if entry is None:
            return None
        payload = bytes(entry[5])
        if struct.unpack_from("<H", payload, 10)[0] not in (0x68, 0x6B):
            return None
        return CompsRecord._attribute(payload, 82, 0x65)

    @staticmethod
    def _attribute(payload: bytes, pos: int, wanted: int) -> Optional[bytes]:
        """Value of attribute `wanted` in the (u32 id, u32 length, value)
        list starting at `pos`; None when absent or cut short."""
        while pos + 8 <= len(payload):
            attribute_id, length = struct.unpack_from("<II", payload, pos)
            if attribute_id == wanted:
                value = payload[pos + 8 : pos + 8 + length]
                return value if len(value) == length else None
            pos += 8 + length
        return None

    @staticmethod
    def parse(dat_record: DatRecord) -> Optional[tuple]:
        raw = bytes(dat_record.record.record_buffer)

        # The v20 Comps layout omits the reserved four bytes at offset 0x0C
        # that are present in newer files. In that layout, the nonzero object
        # ID occupies the reserved field and shifts the remaining header and
        # payload four bytes earlier.
        if dat_record.identifier in (64250, 65021) and struct.unpack_from(
            "<I", raw, 0x0C
        )[0] != 0:
            object_id, parent_id = struct.unpack_from("<II", raw, 0x0C)

            # 0xFFFFFFFF is the sentinel Rockwell uses for "no object" --
            # observed on real project files as a deleted/tombstoned legacy
            # record left behind in Comps.Dat with a minimal, otherwise
            # inconsistent payload (its length fields don't describe a real
            # object). It carries nothing worth exporting, so skip it instead
            # of validating a payload that was never meant to be read.
            if object_id == 0xFFFFFFFF:
                return None

            name_buffer = raw[0x14:0x90]
            name_length = next(
                (
                    index
                    for index in range(0, len(name_buffer) - 1, 2)
                    if name_buffer[index : index + 2] == b"\0\0"
                ),
                len(name_buffer),
            )
            record_name = name_buffer[:name_length].decode(
                "utf-16-le", errors="replace"
            )
            seq_offset = 0x08 if dat_record.identifier == 64250 else 0x04
            seq_number = struct.unpack_from("<H", raw, seq_offset)[0]
            record_type = struct.unpack_from("<H", raw, 0x0A)[0]

            if dat_record.identifier == 64250:
                payload_start = 0x90
                payload_end = struct.unpack_from("<I", raw, 0)[0]
                # Legacy FAFA lengths stop four bytes before the physical
                # record. Those bytes contain the end of the final Rx
                # attribute, so retain them when that exact trailer exists.
                # The physical record can also be longer than that (other
                # data follows), so keep the four bytes whenever they exist.
                if payload_end + 4 <= len(raw):
                    payload_end += 4
            else:
                payload_start = 0x97
                payload_end = dat_record.len_record - 8

            if payload_end < payload_start or payload_end > len(raw):
                raise ValueError("Invalid legacy Comps record length")

            return (
                object_id,
                parent_id,
                record_name,
                seq_number,
                record_type,
                raw[payload_start:payload_end],
            )

        if dat_record.identifier == 64250:
            r = FafaComps.from_bytes(raw)
            # The declared length stops four bytes short of the record's last
            # Rx attribute (the same quirk as the legacy layout above); keep
            # those bytes so the final attribute is complete.
            end = 148 + len(r.record_buffer) + 4
            buffer = raw[148:end] if end <= len(raw) else r.record_buffer
            return (
                r.header.object_id,
                r.header.parent_id,
                r.header.record_name.value,
                r.header.seq_number,
                r.header.record_type,
                buffer,
            )
        elif dat_record.identifier == 65021:
            r = FdfdComps(
                dat_record.len_record,
                KaitaiStream(BytesIO(raw)),
            )
        else:
            return None
        return (
            r.header.object_id,
            r.header.parent_id,
            r.header.record_name.value,
            r.header.seq_number,
            r.header.record_type,
            r.record_buffer,
        )
