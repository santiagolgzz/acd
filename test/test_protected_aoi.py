import struct

from acd.l5x.conversion_notes import ConversionNotes
from acd.l5x.elements import AoiBuilder
from acd.record.rx import RxGeneric

from .rx_fixtures import comps_database, protected_rx_record, rx_record


def _cursor():
    connection, cur = comps_database()
    data_type = rx_record(0x6C, 0x0123, struct.pack("<II", 1, 4) + b"\x00" * 4, 1)
    cur.executemany(
        "INSERT INTO comps VALUES (?, ?, ?, ?, ?, ?)",
        [
            (2, 1, "RxUDIDefinitionCollection", 0, 0, b"aois"),
            (3, 1, "RxDataTypeCollection", 0, 0, b"types"),
            (10, 2, "Widget", 0, 256, protected_rx_record(0x338, 0x4242)),
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
    # Only callers that handle protection opt in; for the rest the record
    # is unreadable.
    try:
        RxGeneric.from_bytes(protected_rx_record(0x338, 0x4242))
        raise AssertionError("a protected record parsed without opting in")
    except ValueError:
        pass
    protected = RxGeneric.from_bytes(protected_rx_record(0x338, 0x4242), allow_protected=True)
    assert protected.protected
    assert (protected.cip_type, protected.comment_id, protected.extended_records) == (0x338, 0x4242, [])
    plain = rx_record(0x338, 0x4242, struct.pack("<II", 1, 4) + b"\x00" * 4, 1)
    assert not RxGeneric.from_bytes(plain).protected
    # A record too short to hold the marker is never read as protected.
    try:
        short = RxGeneric.from_bytes(b"short", allow_protected=True)
    except Exception:
        short = None
    assert short is None or not short.protected


def test_protected_aoi_exports_only_readable_fields():
    connection, cur = _cursor()
    notes = ConversionNotes()
    try:
        xml = AoiBuilder(cur, 10, _notes=notes).build().to_xml()
    finally:
        connection.close()
    assert xml == (
        '<AddOnInstructionDefinition Name="Widget">'
        "<Description>\n<![CDATA[Moves a widget]]>\n</Description>"
        "<Routines></Routines></AddOnInstructionDefinition>"
    )
    assert len(notes.current()) == 1
    assert "'Widget' is source-protected" in notes.current()[0]
