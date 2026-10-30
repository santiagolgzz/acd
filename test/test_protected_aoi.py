import sqlite3
import struct

from acd.l5x import conversion_notes
from acd.l5x.elements import AoiBuilder
from acd.record.rx import RxGeneric


def _rx_record(cip_type: int, comment_id: int, attributes: bytes, count: int) -> bytes:
    head = struct.pack("<IIHHH", 0, 0, 0x10, cip_type, comment_id) + bytes(60)
    return head + struct.pack("<II", len(attributes) + 4, count) + attributes


def _protected_record() -> bytes:
    # Readable header, then the encrypted envelope in place of the attributes.
    envelope = struct.pack("<II", 0x0AAA96AA, 0xA0000010) + bytes(range(0x80, 0xC0))
    head = struct.pack("<IIHHH", 0, 0, 0x10, 0x338, 0x4242) + bytes(60)
    return head + struct.pack("<I", 0x40) + envelope


def _cursor():
    connection = sqlite3.connect(":memory:")
    cur = connection.cursor()
    cur.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    cur.execute(
        "CREATE TABLE comments(seq_number int, sub_record_length int, object_id int, "
        "record_string text, record_type int, parent int, tag_reference text, "
        "rung_content int, member_ref int)"
    )
    data_type = _rx_record(0x6C, 0x0123, struct.pack("<II", 1, 4) + b"\x00" * 4, 1)
    cur.executemany(
        "INSERT INTO comps VALUES (?, ?, ?, ?, ?, ?)",
        [
            (2, 1, "RxUDIDefinitionCollection", 0, 0, b"aois"),
            (3, 1, "RxDataTypeCollection", 0, 0, b"types"),
            (10, 2, "Widget", 0, 256, _protected_record()),
            (11, 3, "Widget", 0, 256, data_type),
        ],
    )
    cur.executemany(
        "INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (0, 20, 1, "Moves a widget", 1, 0x0123006C, "", 0, 0),
            (0, 20, 1, "A parameter note", 1, 0x0123006C, "", 0, 0x55AA),
        ],
    )
    return connection, cur


def test_rx_reads_envelope_marker():
    protected = RxGeneric.from_bytes(_protected_record())
    assert protected.protected
    assert (protected.cip_type, protected.comment_id, protected.extended_records) == (0x338, 0x4242, [])
    plain = _rx_record(0x338, 0x4242, struct.pack("<II", 1, 4) + b"\x00" * 4, 1)
    assert not RxGeneric.from_bytes(plain).protected


def test_protected_aoi_exports_only_readable_fields():
    connection, cur = _cursor()
    conversion_notes.reset()
    try:
        xml = AoiBuilder(cur, 10).build().to_xml()
    finally:
        connection.close()
    assert xml == (
        '<AddOnInstructionDefinition Name="Widget">'
        "<Description>\n<![CDATA[Moves a widget]]>\n</Description>"
        "<Routines></Routines></AddOnInstructionDefinition>"
    )
    notes = conversion_notes.current()
    conversion_notes.reset()
    assert len(notes) == 1
    assert "'Widget' is source-protected" in notes[0]
