import os
import struct
import xml.etree.ElementTree as ET

from acd.api import ImportProjectFromFile
from acd.l5x import conversion_notes
from acd.l5x.elements import (
    L5xElement,
    _decorated_from_data,
    _l5k_from_data,
    _logix_string,
    scoped_comment_keys,
)
from acd.l5x.export_l5x import ExportL5x

AOI_ACD = os.path.join("..", "resources", "ACDTestsWithAOI.ACD")
AOI_L5X = os.path.join("..", "resources", "ACDTestsWithAOI.L5X")


def _controller(xml: str):
    return ET.fromstring(xml).find("Controller")


def test_scoped_comment_keys_reads_scope_and_own_key():
    raw = bytes(10) + struct.pack("<HHHH", 0x68, 0x1A2B, 0x6D, 0x3C4D) + bytes(8)
    assert scoped_comment_keys(raw) == (0x1A2B0068, 0x3C4D006D)


def test_scoped_comment_keys_without_scope():
    raw = bytes(10) + struct.pack("<HHHH", 0x6B, 0x0A0B, 0, 0) + bytes(8)
    assert scoped_comment_keys(raw) == (0x0A0B006B, 0)


def test_parse_rung_uids_skips_routine_head_record():
    routine, first, second = 0xAABBCCDD, 0x11111111, 0x22222222
    head = bytes(6) + b"\xff\xff\xff\xff" + struct.pack("<III", routine, routine, first)
    link1 = bytes.fromhex("000002000001") + struct.pack("<HHIII", 0x0ABC, 0, routine, first, second)
    link2 = bytes.fromhex("000002000001") + struct.pack("<HHIII", 0x1234, 0, routine, second, 0)
    entries = ExportL5x._parse_rung_uids(head + link1 + link2, [(first, routine), (second, routine)])
    assert sorted(entries) == [(first, routine, 0x0ABC), (second, routine, 0x1234)]


def test_rung_comment_matches_reference_export():
    project = ImportProjectFromFile(AOI_ACD).import_project()
    controller = _controller(project.to_xml())
    commented = [
        (routine.get("Name"), rung.get("Number"), rung.find("Comment").text.strip())
        for routine in controller.iter("Routine")
        for rung in routine.iter("Rung")
        if rung.find("Comment") is not None
    ]
    assert commented == [("MainRoutine", "0", "Im a Rung Comment")]


def test_tag_descriptions_match_reference_export():
    def descriptions(controller):
        return {
            tag.get("Name"): tag.find("Description").text.strip()
            for tag in controller.iter("Tag")
            if tag.find("Description") is not None
        }

    project = ImportProjectFromFile(AOI_ACD).import_project()
    expected = descriptions(ET.parse(AOI_L5X).getroot().find("Controller"))
    assert descriptions(_controller(project.to_xml())) == expected


def test_st_routine_lines_match_reference_export():
    def st_lines(controller):
        routine = next(r for r in controller.iter("Routine") if r.get("Name") == "STRoutine")
        assert routine.get("Type") == "ST"
        # Rockwell's export puts a newline either side of each CDATA section;
        # strip that framing so only the source text is compared.
        return [(line.text or "").strip("\n") for line in routine.find("STContent").findall("Line")]

    project = ImportProjectFromFile(AOI_ACD).import_project()
    expected = st_lines(ET.parse(AOI_L5X).getroot().find("Controller"))
    assert len(expected) == 6
    assert st_lines(_controller(project.to_xml())) == expected


def test_l5k_scalar_values():
    assert _l5k_from_data("DINT", struct.pack("<i", -42)) == "-42"
    assert _l5k_from_data("INT", struct.pack("<h", 1234) + bytes(2)) == "1234"
    assert _l5k_from_data("REAL", struct.pack("<f", 90.0)) == "9.00000000e+001"
    assert _l5k_from_data("BOOL", b"\x01") == "1"
    assert _l5k_from_data("BOOL", b"\x00") == "0"
    assert _l5k_from_data("BOOL", bytes(4)) is None
    assert _l5k_from_data("BOOL", b"\x07") is None
    assert _l5k_from_data("DINT", bytes(2)) is None


def test_decorated_timer_value():
    data = struct.pack("<Iii", 1 << 29, 4000, 1500)
    xml = _decorated_from_data("TIMER", None, data)
    assert 'Name="PRE" DataType="DINT" Radix="Decimal" Value="4000"' in xml
    assert 'Name="ACC" DataType="DINT" Radix="Decimal" Value="1500"' in xml
    assert 'Name="DN" DataType="BOOL" Value="1"' in xml
    assert 'Name="EN" DataType="BOOL" Value="0"' in xml


def test_decorated_string_value():
    data = struct.pack("<i", 4) + b"A$'B" + bytes(84 - 4)
    xml = _decorated_from_data("STRING", None, data)
    assert 'Value="4"' in xml
    assert "<![CDATA['A$$$'B']]>" in xml


def test_decorated_arrays():
    xml = _decorated_from_data("REAL", "2", struct.pack("<ff", 1.5, 2.0))
    assert '<Element Index="[0]" Value="1.5"/>' in xml
    assert '<Element Index="[1]" Value="2.0"/>' in xml
    xml = _decorated_from_data("BOOL", "32", struct.pack("<I", 0b101))
    assert '<Element Index="[0]" Value="1"/>' in xml
    assert '<Element Index="[1]" Value="0"/>' in xml
    assert '<Element Index="[2]" Value="1"/>' in xml
    xml = _decorated_from_data("DINT", "2,2", struct.pack("<4i", 1, 2, 3, 4))
    assert '<Element Index="[1][0]" Value="3"/>' in xml


def test_decorated_size_mismatch_falls_back():
    assert _decorated_from_data("DINT", "4", bytes(8)) is None
    assert _decorated_from_data("MY_UDT", None, bytes(8)) is None


def test_logix_string_escapes():
    assert _logix_string(b"a\r\n") == "'a$0D$0A'"


def test_decorated_udt_value():
    from acd.l5x.elements import DataType, Member

    def member(name, dt, offset, bit=0, hidden=False, dim=0):
        return Member(name, name, dt, dim, "Decimal", hidden, None, None, "Read/Write",
                      _offset=offset, _bit=bit)

    udt = DataType("Seat", "Seat", "NoFamily", "User", [
        member("ZZZZZZZZZZSeat0", "SINT", 0, hidden=True),
        member("Extend", "BIT", 0, bit=0),
        member("Retract", "BIT", 0, bit=1),
        member("Pos", "REAL", 4),
        member("Delay", "TIMER", 8),
    ], _size=20)
    data = bytes([0b10, 0, 0, 0]) + struct.pack("<f", 2.5) + struct.pack("<Iii", 0, 500, 0)
    xml = _decorated_from_data("SEAT", None, data, {"SEAT": udt})
    assert '<Structure DataType="Seat">' in xml
    assert 'Name="Extend" DataType="BOOL" Value="0"' in xml
    assert 'Name="Retract" DataType="BOOL" Value="1"' in xml
    assert 'Name="Pos" DataType="REAL" Radix="Float" Value="2.5"' in xml
    assert '<StructureMember Name="Delay" DataType="TIMER">' in xml
    assert 'Name="PRE" DataType="DINT" Radix="Decimal" Value="500"' in xml
    assert "ZZZZZZZZZZSeat0" not in xml


def _dat(buf: bytes):
    from types import SimpleNamespace
    return SimpleNamespace(identifier=64250, record=SimpleNamespace(record_buffer=buf))


def test_legacy_rung_comment_record():
    from acd.record.comments import CommentsRecord

    text = "Step one\r\nStep two".encode("utf-16-le") + b"\0\0"
    body = struct.pack("<HHIII", 7, 1, 0x5E6F0068, 0x1A2B006D, 0x4321FFEC) + bytes(9) + text
    buf = struct.pack("<I", len(body) + 4) + body
    entry = CommentsRecord.parse_legacy(_dat(buf))
    assert entry[3] == "Step one\r\nStep two"
    assert (entry[4], entry[5], entry[7], entry[8]) == (1, 0x5E6F0068, 0x4321FFEC, 0x1A2B006D)


def test_legacy_bit_comment_record():
    from acd.record.comments import CommentsRecord

    body = (
        struct.pack("<HHIII", 3, 3, 0x2B3C0068, 0x7A8B006B, 0)
        + struct.pack("<I", 0x0100) + bytes(3)
        + ".0".encode("utf-16-le") + b"\0\0"
        + "Bit zero note".encode("utf-16-le") + b"\0\0"
    )
    entry = CommentsRecord.parse_legacy(_dat(struct.pack("<I", len(body) + 4) + body))
    assert (entry[3], entry[6], entry[8]) == ("Bit zero note", ".0", 0x7A8B006B)


def test_taginfo_layouts():
    from acd.l5x.elements import taginfo_layouts

    xml = (
        '<LogixTagInfo><DataTypes><DataType Name="Seat" Size="12"><Members>'
        '<Member Name="ZZZZZZZZZZSeat0" DataType="SINT" Offset="0" Hidden="true"/>'
        '<Member Name="Extend" DataType="BOOL" Offset="0" Bit="1"/>'
        '<Member Name="Pos" DataType="REAL" Offset="4"><Dimensions><Dim Size="2"/></Dimensions></Member>'
        '</Members></DataType></DataTypes></LogixTagInfo>'
    )
    seat = taginfo_layouts(ET.fromstring(xml))["SEAT"]
    assert seat._size == 12
    extend, pos = seat.members[1], seat.members[2]
    assert (extend._offset, extend._bit, extend.hidden) == (0, 1, False)
    assert (pos.data_type, pos.dimension, pos._offset) == ("REAL", 2, 4)
    data = bytes([0b10, 0, 0, 0]) + struct.pack("<ff", 1.5, 2.0)
    xml_out = _decorated_from_data("SEAT", None, data, taginfo_layouts(ET.fromstring(xml)))
    assert 'Name="Extend" DataType="BOOL" Value="1"' in xml_out
    assert '<Element Index="[1]" Value="2.0"/>' in xml_out


def test_nonfinite_real():
    assert _l5k_from_data("REAL", struct.pack("<f", float("nan"))) == "1.#QNAN000e+000"
    assert _l5k_from_data("REAL", struct.pack("<f", float("-inf"))) == "-1.#INF0000e+000"


def _type1_comment(kind: int, text: bytes) -> bytes:
    # u16 seq, u16 type 1, u16 sub-record length, parent key, member and rung
    # refs, 5 zero bytes, u32 kind (offset 27), 13 zero bytes, text.
    body = (
        struct.pack("<HHHIIi", 0, 1, len(text) + 13, 0x1A2B0068, 0x3C4D006D, 0)
        + bytes(5) + struct.pack("<I", kind) + bytes(13) + text + b"\0"
    )
    return struct.pack("<I", len(body)) + body


def test_type1_internal_string_records_are_left_out_of_descriptions():
    import sqlite3

    from acd.l5x.elements import _own_description
    from acd.record.comments import CommentsRecord

    real = CommentsRecord.parse(_dat(_type1_comment(1, b"Made up note")))
    member_name = CommentsRecord.parse(_dat(_type1_comment(4, b"SomeMember")))
    assert real is not None and member_name is not None
    assert (real[2], real[3]) == (1, "Made up note")
    # Kept in the table (older rung comments number rungs in object_id) ...
    assert (member_name[2], member_name[3]) == (4, "SomeMember")

    # ... but never read as a description.
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("CREATE TABLE comments(seq_number int, sub_record_length int, object_id int, record_string text,"
                " record_type int, parent int, tag_reference text, rung_content int, member_ref int)")
    cur.executemany("INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?)", [member_name, real])
    assert _own_description(cur, 0x1A2B0068, None, 0x3C4D006D) == "Made up note"


def test_notes_added_before_project_is_read_appear_in_comment():
    export = ExportL5x(AOI_ACD)
    export._notes.add("Made up note: tag 'Fake' used a stand-in value")
    xml_out = export.project.to_xml()
    assert xml_out.startswith(
        "<!-- acd-tools conversion notes:\n"
        "Made up note: tag 'Fake' used a stand-in value\n-->\n<RSLogix5000Content"
    )
    assert _controller(xml_out) is not None


def test_notes_do_not_carry_over_to_another_export():
    ExportL5x(AOI_ACD)._notes.add("Made up stale note")
    xml_out = ExportL5x(AOI_ACD).project.to_xml()
    assert "Made up stale note" not in xml_out


def test_no_notes_leaves_output_unchanged():
    project = ExportL5x(AOI_ACD).project
    project.conversion_notes = []
    assert project.to_xml() == L5xElement.to_xml(project)


def test_note_text_is_made_safe_for_xml_comments():
    note = "Tag 'A--B': x -- y ---> z\nsecond line"
    comment = conversion_notes.xml_comment([note])
    assert comment == "<!-- acd-tools conversion notes:\nTag 'A-B': x - y -> z second line\n-->\n"
    assert ET.fromstring(comment + "<Root/>").tag == "Root"




def test_current_alias_path_read_past_declared_length():
    from acd.record.comps import CompsRecord
    from acd.record.rx import RxGeneric

    path = "@0a0b0c0d@.@01020304@.7\0".encode("utf-16-le")
    payload = bytearray(82)
    struct.pack_into("<H", payload, 10, 0x6B)
    payload += struct.pack("<II", 0x01, 2) + b"\0\0"
    payload += struct.pack("<II", 0x65, len(path)) + path
    # The stored attribute length leaves out the last four bytes.
    struct.pack_into("<II", payload, 74, len(payload) - 78, 2)
    raw = bytearray(148) + payload
    # The declared length stops four bytes short of the physical record.
    struct.pack_into("<I", raw, 0, len(raw) - 4)
    entry = CompsRecord.parse(_dat(bytes(raw)))
    assert entry is not None
    assert RxGeneric.from_bytes(entry[5]).attribute(0x65) == path


def test_current_alias_resolves_module_tag_names():
    import sqlite3

    import pytest

    from acd.l5x.elements import TagBuilder

    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("CREATE TABLE comps(object_id int, parent_id int, comp_name text,"
                " seq_number int, record_type int, record BLOB)")
    rows = [(0x11223344, "Rack7"), (0x0A0B0C0D, "&11223344:5:O"), (0x01020304, "Data")]
    for object_id, name in rows:
        cur.execute("INSERT INTO comps VALUES (?, 0, ?, 0, 0, ?)", (object_id, name, b""))
    path = "@0a0b0c0d@.@01020304@.7\0".encode("utf-16-le")
    assert TagBuilder(cur, 99)._resolve_alias_path(path) == "Rack7:5:O.Data.7"

    # An unresolvable target raises; the builder then exports a Base tag.
    with pytest.raises(ValueError):
        TagBuilder(cur, 97)._resolve_alias_path("@0f0f0f0f@\0".encode("utf-16-le"))


def _current_tag_record(name: str, alias_path=None) -> bytes:
    from .rx_fixtures import rx_record

    encoded = name.encode("utf-8")
    attributes = struct.pack("<II", 0x01, 2 + len(encoded)) + struct.pack("<H", len(encoded)) + encoded
    count = 1
    if alias_path is not None:
        attributes += struct.pack("<II", 0x65, len(alias_path)) + alias_path
        count = 2
    return rx_record(0x6B, 0x0123, attributes, count, length_extra=8)


def test_current_alias_tags_through_tag_builder():
    from loguru import logger

    from acd.l5x.elements import TagBuilder
    from .rx_fixtures import comps_database, insert_comp

    connection, cur = comps_database()
    cur.execute("CREATE TABLE taginfo_tags(program text, name text, data_type text)")
    for object_id, name in [(0x11223344, "Rack7"), (0x0A0B0C0D, "&11223344:5:O"), (0x01020304, "Data")]:
        insert_comp(cur, object_id, name, b"")
    insert_comp(cur, 99, "MyAlias", _current_tag_record(
        "MyAlias", "@0a0b0c0d@.@01020304@.7\0".encode("utf-16-le")))
    insert_comp(cur, 98, "Plain", _current_tag_record("Plain"))
    insert_comp(cur, 97, "Broken", _current_tag_record("Broken", "@0f0f0f0f@\0".encode("utf-16-le")))
    warnings = []
    sink = logger.add(warnings.append, level="WARNING")
    try:
        alias = TagBuilder(cur, 99).build()
        assert (alias.tag_type, alias.alias_for) == ("Alias", "Rack7:5:O.Data.7")

        # A tag with no alias path is not an alias.
        plain = TagBuilder(cur, 98).build()
        assert (plain.tag_type, plain.alias_for) == ("Base", None)
        assert warnings == []

        # An unresolvable alias falls back to a Base tag with a warning.
        broken = TagBuilder(cur, 97).build()
        assert (broken.tag_type, broken.alias_for) == ("Base", None)
        assert len(warnings) == 1 and "Broken" in warnings[0]
    finally:
        logger.remove(sink)
        connection.close()


def test_current_alias_tag_matches_reference_export():
    project = ImportProjectFromFile(AOI_ACD).import_project()
    tag = next(
        t for t in _controller(project.to_xml()).iter("Tag")
        if t.get("Name") == "AliasTag5000String32"
    )
    assert tag.get("TagType") == "Alias"
    assert tag.get("AliasFor") == "a5000_String32.LEN"
    assert tag.get("DataType") is None


def test_taginfo_tags():
    from acd.l5x.elements import taginfo_tags

    xml = (
        '<LogixTagInfo><Tags><Tag Name="CtlTag" DataType="ALARM_DIGITAL"/></Tags>'
        '<Programs><Program Name="Prog"><Tags><Tag Name="Local1" DataType="TIMER"/>'
        '</Tags></Program></Programs></LogixTagInfo>'
    )
    assert sorted(taginfo_tags(ET.fromstring(xml))) == [("", "CtlTag", "ALARM_DIGITAL"), ("Prog", "Local1", "TIMER")]




def _legacy_member(name: str, radix: int, type_id: int, offset: int, bit: int, host: int, hidden: int) -> bytes:
    # Name (UTF-16LE, 0x54 bytes), then radix, type object, dimension,
    # offset, bit, host index (0x800: not a BIT), 0x6C, hidden, access.
    return name.encode("utf-16-le").ljust(0x54, b"\0") + struct.pack(
        "<9I", radix, type_id, 0, offset, bit, host, 0xFFFFFFFF, hidden, 1
    ) + bytes(0x30)


def test_legacy_data_type_members():
    import sqlite3

    from acd.l5x.elements import DataTypeBuilder

    members = [
        _legacy_member("ZZZZZZZZZZPump0", 4, 0x51, 0, 0, 0x800, 1),
        _legacy_member("Run", 4, 0x52, 0, 3, 0, 0),
        _legacy_member("Speed", 7, 0x53, 4, 0, 0x800, 0),
    ]
    attrs = [(0x64, struct.pack("<I", len(members))), (0x65, struct.pack("<I", 8))]
    attrs += [(0x6E + i, m) for i, m in enumerate(members)]
    body = b"".join(struct.pack("<II", k, len(v)) + v for k, v in attrs)
    # Legacy header: cip type 0x6C at 8, object id at 20, body length and
    # attribute count at 24.
    record = bytearray(32) + body
    struct.pack_into("<HHHI", record, 8, 0x6C, 0, 0, 0)
    struct.pack_into("<III", record, 20, 0x99, len(body) + 4, len(attrs))

    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("CREATE TABLE comps(object_id int, parent_id int, comp_name text, seq_number int, record_type int, record BLOB)")
    cur.execute("CREATE TABLE comment_keys(object_id int, scope_key int, member_key int)")
    cur.execute("CREATE TABLE comments(seq_number int, sub_record_length int, object_id int, record_string text,"
                " record_type int, parent int, tag_reference text, rung_content int, member_ref int)")
    cur.executemany("INSERT INTO comps VALUES (?,?,?,0,0,?)", [
        (0x51, 0, "SINT", b""), (0x52, 0, "BOOL", b""), (0x53, 0, "REAL", b""),
        (0x99, 0, "Pump", bytes(record)),
    ])
    cur.execute("INSERT INTO comment_keys VALUES (0x99, 0x1234006C, 0)")
    cur.execute("INSERT INTO comments VALUES (0, 0, 1, 'Made up pump', 1, 0x1234006C, '', 0, 0)")

    pump = DataTypeBuilder(cur, 0x99).build()
    assert (pump.cls, pump._size, pump._description) == ("User", 8, "Made up pump")
    host, run, speed = pump.members
    assert (host.data_type, host.hidden, host.radix) == ("SINT", True, "Decimal")
    assert (run.data_type, run.target, run.bit_number, run.dimension) == ("BIT", "ZZZZZZZZZZPump0", 3, 0)
    assert (run._offset, run._bit) == (0, 3)
    assert (speed.name, speed.data_type, speed.radix, speed._offset) == ("Speed", "REAL", "Float", 4)
    assert speed.external_access == "Read/Write"


def test_rung_text_with_invalid_utf16_is_kept(monkeypatch):
    from types import SimpleNamespace
    from acd.record import sbregion

    fake = SimpleNamespace(
        header=SimpleNamespace(language_type="Rung NT", identifier=7),
        record_buffer="NOP();".encode("utf-16-le") + b"\x00\xd8",  # lone surrogate
    )
    monkeypatch.setattr(sbregion.FafaSbregions, "from_bytes", lambda _: fake)
    notes = []
    entry = sbregion.SbRegionRecord.parse(_dat(b""), {}, notes.append)
    assert entry[1].startswith("NOP();")
    assert "not valid UTF-16" in notes[0]
