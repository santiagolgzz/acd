import struct
from dataclasses import dataclass
from typing import Dict, List, Optional

from acd.generated.comps.rx_generic import RxGeneric as GeneratedRxGeneric

# Source protection stores a record's attribute block as an encrypted
# envelope. The envelope starts with this u32 where a plain record has its
# attribute count (offset 78, right after the 60-byte main record and the
# length field); the main record before it stays readable. Nameless records
# carry the same envelope after their 24-byte header.
ENCRYPTED_ENVELOPE_MAGIC = 0x0AAA96AA


@dataclass
class LegacyAttributeRecord:
    attribute_id: int
    len_value: int
    value: bytes


class LegacyMainRecord:
    def __init__(self, raw: bytes, attributes: List[LegacyAttributeRecord]):
        self.body = raw
        self.runtime_type = struct.unpack_from("<I", raw, 4)[0]
        self.data_type = self.runtime_type
        self.cip_data_type = self.runtime_type & 0xFFFF
        self.dimension_1 = 0
        self.dimension_2 = 0
        self.dimension_3 = 0
        self.radix = 0
        self.external_access = 0
        self.data_table_instance = 0

        metadata = next(
            (attribute.value for attribute in attributes if attribute.attribute_id == 0x01),
            b"",
        )
        if len(metadata) >= 0x220:
            self.runtime_type = struct.unpack_from("<H", metadata, 0x100)[0]
            self.data_type = self.runtime_type
            self.cip_data_type = self.runtime_type
            self.data_table_instance = struct.unpack_from("<I", metadata, 0x102)[0]
            self.radix = metadata[0x20F] >> 4
            self.dimension_1, self.dimension_2, self.dimension_3 = struct.unpack_from(
                "<III", metadata, 0x212
            )
            self.external_access = struct.unpack_from("<H", metadata, 0x21E)[0]


class _RxAttributes:
    """Attribute lookups shared by both Rx layouts."""

    extended_records: list
    # True for a source-protected record: only the header and main record
    # are readable, and extended_records is empty.
    protected = False

    @property
    def attributes(self) -> Dict[int, bytes]:
        """Every attribute's value by id (the last one wins on a repeat)."""
        return {item.attribute_id: bytes(item.value) for item in self.extended_records}

    def attribute(self, attribute_id: int) -> Optional[bytes]:
        """The first value of attribute `attribute_id`, or None when absent."""
        return next(
            (bytes(item.value) for item in self.extended_records if item.attribute_id == attribute_id),
            None,
        )


class LegacyRxGeneric(_RxAttributes):
    def __init__(self, raw: bytes):
        self.is_legacy = True
        self.data_instance_id, self.unique_tag_identifier = struct.unpack_from(
            "<II", raw, 0
        )
        # Keep the generated parser's attribute for callers that consume the
        # common interface. In this layout it references tag data, not scope.
        self.parent_id = self.data_instance_id
        self.record_format_version = 0
        self.cip_type, self.comment_id = struct.unpack_from("<HH", raw, 8)
        # A routine's type follows its cip_type.
        self.routine_type = struct.unpack_from("<H", raw, 0x0C)[0]
        self.object_id = struct.unpack_from("<I", raw, 20)[0]
        self.record_buffer = raw[14:74]
        self.len_record, self.count_record = struct.unpack_from("<II", raw, 24)
        self.extended_records: List[LegacyAttributeRecord] = []

        offset = 32
        for index in range(self.count_record):
            if offset + 8 > len(raw):
                raise ValueError("Invalid legacy Rx attribute header")
            attribute_id, length = struct.unpack_from("<II", raw, offset)
            value_end = offset + 8 + length
            if value_end > len(raw):
                if index == self.count_record - 1 and value_end == len(raw) + 4:
                    value_end = len(raw)
                else:
                    raise ValueError("Invalid legacy Rx attribute length")
            self.extended_records.append(
                LegacyAttributeRecord(attribute_id, length, raw[offset + 8 : value_end])
            )
            offset = value_end
        if offset != len(raw):
            raise ValueError("Invalid legacy Rx record length")

        self.main_record = LegacyMainRecord(raw[12:24], self.extended_records)

    @property
    def data_object_id(self) -> int:
        """Object id of the tag's data object."""
        return self.data_instance_id

    @property
    def logical_name(self):
        metadata = self.attribute(0x01) or b""
        if len(metadata) < 2:
            return ""
        length = struct.unpack_from("<H", metadata, 0)[0]
        if length > len(metadata) - 2:
            return ""
        return metadata[2 : 2 + length].decode("utf-8", errors="replace")


class CurrentRxGeneric(GeneratedRxGeneric, _RxAttributes):
    """The current-layout Rx record, read by the generated parser."""

    @property
    def data_object_id(self) -> int:
        """Object id of the tag's data object."""
        return self.main_record.data_table_instance

    @property
    def routine_type(self) -> int:
        """A routine's type index, at 0x30 of the main record."""
        return struct.unpack_from("<H", self.record_buffer, 0x30)[0]


class RxGeneric:
    @staticmethod
    def _current(raw: bytes) -> CurrentRxGeneric:
        try:
            return CurrentRxGeneric.from_bytes(raw)
        except EOFError:
            # The final attribute is cut short (the record lacks the four
            # bytes past its stored length): read the ones before it.
            count = struct.unpack_from("<I", raw, 78)[0]
            parsed = CurrentRxGeneric.from_bytes(raw[:78] + struct.pack("<I", max(count - 1, 0)) + raw[82:])
            parsed.count_record = count
            return parsed

    @staticmethod
    def from_bytes(raw: bytes):
        raw = bytes(raw)
        if len(raw) >= 82:
            stored_length, count = struct.unpack_from("<II", raw, 74)
            if count == ENCRYPTED_ENVELOPE_MAGIC:
                # Read the header and main record only (as a record with no
                # attributes); the attributes are encrypted.
                parsed = CurrentRxGeneric.from_bytes(raw[:78] + bytes(4))
                parsed.protected = True
                return parsed
            # The stored length excludes the last four bytes of the final
            # attribute, which CompsRecord.parse keeps.
            if stored_length in (len(raw) - 74, len(raw) - 78):
                return RxGeneric._current(raw)
        if len(raw) >= 32 and struct.unpack_from("<I", raw, 24)[0] in (
            len(raw) - 28,
            len(raw) - 24,
        ):
            return LegacyRxGeneric(raw)
        return RxGeneric._current(raw)
