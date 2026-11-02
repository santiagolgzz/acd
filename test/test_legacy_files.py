"""Older (v19/v20, legacy-layout) project files, on synthetic records."""

import sqlite3
import struct
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from acd.l5x.elements import (
    ControllerBuilder,
    DataTypeBuilder,
    RoutineBuilder,
    TagBuilder,
    _decorated_from_data,
    _own_description,
    merge_value_types,
    taginfo_layouts,
    taginfo_tags,
)
from acd.record.comments import CommentsRecord
from acd.record.comps import CompsRecord

PROGRAM_KEY = 0x5E6F0068
ROUTINE_KEY = 0x1A2B006D
TAG_KEY = 0x7A8B006B


def _dat(buf: bytes):
    return SimpleNamespace(identifier=64250, record=SimpleNamespace(record_buffer=buf))


def _legacy_rx(cip_type, object_id, attributes=(), data_instance_id=0, head=b""):
    """A legacy-layout Rx record; `head` overwrites the bytes from offset 12."""
    raw = bytearray(32)
    struct.pack_into("<II", raw, 0, data_instance_id, 0)
    struct.pack_into("<H", raw, 8, cip_type)
    raw[12 : 12 + len(head)] = head
    struct.pack_into("<I", raw, 20, object_id)
    struct.pack_into("<I", raw, 28, len(attributes))
    for attribute_id, value in attributes:
        raw += struct.pack("<II", attribute_id, len(value)) + value
    struct.pack_into("<I", raw, 24, len(raw) - 28)
    return bytes(raw)


def _metadata(name, runtime_type):
    value = bytearray(544)
    struct.pack_into("<H", value, 0, len(name))
    value[2 : 2 + len(name)] = name.encode()
    struct.pack_into("<H", value, 0x100, runtime_type)
    return bytes(value)


def _database():
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("CREATE TABLE comps(object_id int, parent_id int, comp_name text, seq_number int, record_type int, record BLOB)")
    cur.execute("CREATE TABLE comment_keys(object_id int, scope_key int, member_key int)")
    cur.execute("CREATE TABLE comments(seq_number int, sub_record_length int, object_id int, record_string text,"
                " record_type int, parent int, tag_reference text, rung_content int, member_ref int)")
    cur.execute("CREATE TABLE region_map(object_id int, parent_id int, unknown int, seq_no int, record BLOB)")
    cur.execute("CREATE TABLE rungs(object_id int, rung text, seq_number int)")
    cur.execute("CREATE TABLE rung_uids(object_id int, parent_id int, uid int)")
    cur.execute("CREATE TABLE taginfo_tags(program text, name text, data_type text)")
    return db, cur


def _comment(cur, parent, member_ref, text, record_type=3, tag_reference="", rung_content=0):
    cur.execute(
        "INSERT INTO comments VALUES (0, 0, 1, ?, ?, ?, ?, ?, ?)",
        (text, record_type, parent, tag_reference, rung_content, member_ref),
    )


def _routine(cur, routine_type=1):
    """A legacy routine (object 0x40) with two rungs (0x41, 0x42)."""
    record = _legacy_rx(0x6D, 0x40, head=struct.pack("<H", routine_type))
    cur.execute("INSERT INTO comps VALUES (0x40, 0, 'MainRoutine', 0, 0, ?)", (record,))
    cur.executemany("INSERT INTO region_map VALUES (?, 0x40, ?, 0, x'')", [(0x41, 0), (0x42, 1)])
    cur.executemany("INSERT INTO rungs VALUES (?, ?, 0)", [(0x41, "NOP();"), (0x42, "AFI();")])


def test_routine_type_follows_cip_type():
    db, cur = _database()
    _routine(cur, routine_type=1)
    assert RoutineBuilder(cur, 0x40).build().type == "RLL"
    db.close()


def test_legacy_rung_comment_record():
    text = "Step one\r\nStep two".encode("utf-16-le") + b"\0\0"
    body = struct.pack("<HHIII", 7, 1, PROGRAM_KEY, ROUTINE_KEY, 0x4321FFEC) + bytes(9) + text
    entry = CommentsRecord.parse_legacy(_dat(struct.pack("<I", len(body) + 4) + body))
    assert entry[3] == "Step one\r\nStep two"
    assert (entry[4], entry[5], entry[7], entry[8]) == (1, PROGRAM_KEY, 0x4321FFEC, ROUTINE_KEY)


def test_legacy_bit_comment_record():
    body = (
        struct.pack("<HHIII", 3, 3, PROGRAM_KEY, TAG_KEY, 0)
        + struct.pack("<I", 0x0100) + bytes(3)
        + ".0".encode("utf-16-le") + b"\0\0"
        + "Bit zero note".encode("utf-16-le") + b"\0\0"
    )
    entry = CommentsRecord.parse_legacy(_dat(struct.pack("<I", len(body) + 4) + body))
    assert (entry[3], entry[6], entry[8]) == ("Bit zero note", ".0", TAG_KEY)


def test_legacy_text_without_terminator_runs_to_the_end():
    assert CommentsRecord._utf16z("ab".encode("utf-16-le"), 0) == ("ab", 6)


def test_legacy_comment_keys_from_record_header():
    raw = bytearray(0x90)
    struct.pack_into("<I", raw, 0x0C, 0x40)  # legacy: object id fills 0x0C
    struct.pack_into("<HHHH", raw, 0x68, 0x68, 0x5E6F, 0x6D, 0x1A2B)
    assert CompsRecord.legacy_comment_keys(_dat(bytes(raw))) == (PROGRAM_KEY, ROUTINE_KEY)
    struct.pack_into("<HHHH", raw, 0x68, 0x6B, 0x7A8B, 0, 0)
    assert CompsRecord.legacy_comment_keys(_dat(bytes(raw))) == (TAG_KEY, 0)
    struct.pack_into("<I", raw, 0x0C, 0)  # current layout: keys are read elsewhere
    assert CompsRecord.legacy_comment_keys(_dat(bytes(raw))) is None


def test_legacy_rung_comments_follow_rung_uids():
    db, cur = _database()
    _routine(cur)
    cur.execute("INSERT INTO comment_keys VALUES (0x40, ?, ?)", (PROGRAM_KEY, ROUTINE_KEY))
    cur.executemany("INSERT INTO rung_uids VALUES (?, 0x40, ?)", [(0x41, 0x0B), (0x42, 0x0A)])
    _comment(cur, PROGRAM_KEY, ROUTINE_KEY, "Second rung", record_type=1, rung_content=0x000AFFEC)
    _comment(cur, PROGRAM_KEY, 0x99, "Other routine", record_type=1, rung_content=0x000BFFEC)
    routine = RoutineBuilder(cur, 0x40).build()
    assert routine._rung_comments == {1: "Second rung"}
    db.close()


def _controller_tag(cur):
    record = _legacy_rx(0x6B, 0x50, attributes=[(0x01, _metadata("Flags", 0xC4))])
    cur.execute("INSERT INTO comps VALUES (0x50, 0, 'Flags', 0, 0, ?)", (record,))
    cur.execute("INSERT INTO comment_keys VALUES (0x50, ?, 0)", (TAG_KEY,))


def test_legacy_tag_description_and_bit_comments():
    db, cur = _database()
    _controller_tag(cur)
    _comment(cur, TAG_KEY, 0, "Status flags")
    _comment(cur, TAG_KEY, 0, "Bit zero note", tag_reference=".0")
    _comment(cur, TAG_KEY, 0x99, "Another object's note", tag_reference=".1")
    xml = TagBuilder(cur, 0x50).build().to_xml()
    assert "<Description>\n<![CDATA[Status flags]]>\n</Description>" in xml
    assert '<Comments><Comment Operand=".0">\n<![CDATA[Bit zero note]]>\n</Comment></Comments>' in xml
    assert "Another object" not in xml
    db.close()


def _legacy_member(name: str, radix: int, type_id: int, offset: int, bit: int, host: int, hidden: int) -> bytes:
    # Name (UTF-16LE, 0x54 bytes), then radix, type object, dimension,
    # offset, bit, host index (0x800: not a BIT), 0x6C, hidden, access.
    return name.encode("utf-16-le").ljust(0x54, b"\0") + struct.pack(
        "<9I", radix, type_id, 0, offset, bit, host, 0xFFFFFFFF, hidden, 1
    ) + bytes(0x30)


def test_legacy_data_type_members():
    members = [
        _legacy_member("ZZZZZZZZZZPump0", 4, 0x51, 0, 0, 0x800, 1),
        _legacy_member("Run", 4, 0x52, 0, 3, 0, 0),
        _legacy_member("Speed", 7, 0x53, 4, 0, 0x800, 0),
    ]
    attrs = [(0x64, struct.pack("<I", len(members))), (0x65, struct.pack("<I", 8))]
    attrs += [(0x6E + i, m) for i, m in enumerate(members)]
    db, cur = _database()
    cur.executemany("INSERT INTO comps VALUES (?,?,?,0,0,?)", [
        (0x51, 0, "SINT", b""), (0x52, 0, "BOOL", b""), (0x53, 0, "REAL", b""),
        (0x99, 0, "Pump", _legacy_rx(0x6C, 0x99, attrs)),
    ])
    cur.execute("INSERT INTO comment_keys VALUES (0x99, 0x1234006C, 0)")
    _comment(cur, 0x1234006C, 0, "Made up pump", record_type=1)

    pump = DataTypeBuilder(cur, 0x99).build()
    assert (pump.cls, pump._description) == ("User", "Made up pump")
    host, run, speed = pump.members
    assert (host.data_type, host.hidden, host.radix) == ("SINT", True, "Decimal")
    assert (run.data_type, run.target, run.bit_number, run.dimension) == ("BIT", "ZZZZZZZZZZPump0", 3, 0)
    assert (speed.name, speed.data_type, speed.radix) == ("Speed", "REAL", "Float")
    assert speed.external_access == "Read/Write"
    assert pump._size == 8
    assert (run._offset, run._bit) == (0, 3)
    assert speed._offset == 4
    db.close()


def test_taginfo_tags():
    xml = (
        '<LogixTagInfo><Tags><Tag Name="CtlTag" DataType="ALARM_DIGITAL"/></Tags>'
        '<Programs><Program Name="Prog"><Tags><Tag Name="Local1" DataType="TIMER"/>'
        '</Tags></Program></Programs></LogixTagInfo>'
    )
    assert sorted(taginfo_tags(ET.fromstring(xml))) == [("", "CtlTag", "ALARM_DIGITAL"), ("Prog", "Local1", "TIMER")]


def test_unresolved_tag_type_comes_from_taginfo():
    db, cur = _database()
    cur.executemany("INSERT INTO taginfo_tags VALUES (?, ?, ?)", [
        ("", "Alarm", "ALARM_DIGITAL"), ("", "Shared", "DINT"),
        ("ProgA", "Step", "TIMER"), ("ProgB", "Step", "COUNTER"), ("ProgA", "Other", "REAL"),
    ])
    # The controller (0x50, a root comp) owns tag collection 0x51; program
    # ProgB (0x60, under program collection 0x5F) owns tag collection 0x61.
    cur.execute("INSERT INTO comps VALUES (0x50, 0, 'Ctrl', 0, 256, x'')")
    cur.execute("INSERT INTO comps VALUES (0x51, 0x50, 'Tags', 0, 0, x'')")
    cur.execute("INSERT INTO comps VALUES (0x5F, 0x50, 'RxProgramCollection', 0, 0, x'')")
    cur.execute("INSERT INTO comps VALUES (0x60, 0x5F, 'ProgB', 0, 256, x'')")
    cur.execute("INSERT INTO comps VALUES (0x61, 0x60, 'Tags', 0, 0, x'')")
    tags = [
        (0x52, 0x51, "Alarm"), (0x53, 0x51, "Step"),
        (0x62, 0x61, "Step"), (0x63, 0x61, "Shared"), (0x64, 0x61, "Other"),
    ]
    for oid, collection, name in tags:
        # Runtime type 0 resolves no type from the tag's own records.
        record = _legacy_rx(0x6B, oid, attributes=[(0x01, _metadata(name, 0))])
        cur.execute("INSERT INTO comps VALUES (?, ?, ?, 0, 0, ?)", (oid, collection, name, record))
    types = {oid: TagBuilder(cur, oid).build().data_type for oid, _, _ in tags}
    assert types == {0x52: "ALARM_DIGITAL", 0x53: "", 0x62: "COUNTER", 0x63: "", 0x64: ""}
    db.close()


def test_legacy_routine_description():
    db, cur = _database()
    _routine(cur)
    cur.execute("INSERT INTO comment_keys VALUES (0x40, ?, ?)", (PROGRAM_KEY, ROUTINE_KEY))
    _comment(cur, PROGRAM_KEY, ROUTINE_KEY, "Rung note", record_type=1, rung_content=0x000AFFEC)
    _comment(cur, PROGRAM_KEY, ROUTINE_KEY, "Fills the hopper", record_type=1)
    xml = RoutineBuilder(cur, 0x40).build().to_xml()
    assert xml.startswith(
        '<Routine Name="MainRoutine" Type="RLL"><Description>\n<![CDATA[Fills the hopper]]>\n</Description><RLLContent>'
    )
    db.close()


def test_legacy_controller_description_under_its_own_key():
    db, cur = _database()
    controller_key, own_key = 0x00010069, 0x00020069
    _comment(cur, own_key, 0, "Bit note", tag_reference=".0")
    _comment(cur, own_key, 0, "Line 4 filler")
    assert _own_description(cur, controller_key, own_key, 0) == "Line 4 filler"
    assert _own_description(cur, controller_key, None, 0) is None
    db.close()


CONTROLLER_OWN_KEY = 0x00020069


def _legacy_controller(cur):
    """A legacy controller (object 1) with empty type, tag (3) and program
    collections, whose own comment key comes from the record header."""
    dates = [(0x65, bytes(8)), (0x66, bytes(8))]
    record = _legacy_rx(0x69, 1, dates)
    cur.execute("INSERT INTO comps VALUES (1, 0, 'Line4', 0, 256, ?)", (record,))
    cur.execute("INSERT INTO comment_keys VALUES (1, ?, 0)", (CONTROLLER_OWN_KEY,))
    for oid, name in ((2, "RxDataTypeCollection"), (3, "RxTagCollection"), (4, "RxProgramCollection")):
        cur.execute("INSERT INTO comps VALUES (?, 1, ?, 0, 0, x'')", (oid, name))


def test_legacy_controller_description_through_controller_builder():
    db, cur = _database()
    _legacy_controller(cur)
    _comment(cur, CONTROLLER_OWN_KEY, 0, "Line 4 filler")
    controller = ControllerBuilder(cur).build()
    assert "<Description>\n<![CDATA[Line 4 filler]]>\n</Description>" in controller.to_xml()
    db.close()


SEAT_TAGINFO = (
    '<LogixTagInfo><DataTypes><DataType Name="Seat" Size="12"><Members>'
    '<Member Name="ZZZZZZZZZZSeat0" DataType="SINT" Offset="0" Hidden="true"/>'
    '<Member Name="Extend" DataType="BOOL" Offset="0" Bit="1"/>'
    '<Member Name="Pos" DataType="REAL" Offset="4"><Dimensions><Dim Size="2"/></Dimensions></Member>'
    '</Members></DataType></DataTypes></LogixTagInfo>'
)
SEAT_VALUE = bytes([0b10, 0, 0, 0]) + struct.pack("<ff", 1.5, 2.0)


def _tag_with_value(cur, object_id, name, runtime_type, value, parent_id=0):
    """A legacy tag whose data object (object_id + 1) holds `value`."""
    record = _legacy_rx(
        0x6B, object_id, attributes=[(0x01, _metadata(name, runtime_type))],
        data_instance_id=object_id + 1,
    )
    cur.execute("INSERT INTO comps VALUES (?, ?, ?, 0, 0, ?)", (object_id, parent_id, name, record))
    data_object = _legacy_rx(0x6E, object_id + 1, [(0x66, value)])
    cur.execute("INSERT INTO comps VALUES (?, 0, 'data', 0, 0, ?)", (object_id + 1, data_object))


def test_legacy_tag_value_comes_from_its_data_object():
    db, cur = _database()
    _tag_with_value(cur, 0x50, "Count", 0xC4, struct.pack("<i", 42))
    tag = TagBuilder(cur, 0x50).build()
    assert tag._data == struct.pack("<i", 42)
    assert '<Data Format="L5K">\n42\n</Data>' in tag.to_xml()
    db.close()


def test_taginfo_layouts():
    seat = taginfo_layouts(ET.fromstring(SEAT_TAGINFO))["SEAT"]
    assert seat._size == 12
    extend, pos = seat.members[1], seat.members[2]
    assert (extend._offset, extend._bit, extend.hidden) == (0, 1, False)
    assert (pos.data_type, pos.dimension, pos._offset) == ("REAL", 2, 4)
    xml = _decorated_from_data("SEAT", None, SEAT_VALUE, taginfo_layouts(ET.fromstring(SEAT_TAGINFO)))
    assert 'Name="Extend" DataType="BOOL" Value="1"' in xml
    assert '<Element Index="[1]" Value="2.0"/>' in xml


def test_taginfo_layouts_skip_unusable_sizes_and_offsets():
    root = ET.fromstring(SEAT_TAGINFO)
    root.append(ET.fromstring('<DataType Name="NoSize" Size=""/>'))
    root.append(ET.fromstring(
        '<DataType Name="BadOffset" Size="4"><Members>'
        '<Member Name="A" DataType="DINT" Offset="x"/></Members></DataType>'
    ))
    assert sorted(taginfo_layouts(root)) == ["SEAT"]


def test_merge_value_types_keeps_complete_comps_types():
    layouts = taginfo_layouts(ET.fromstring(SEAT_TAGINFO))
    complete = SimpleNamespace(_size=12, members=[object()])
    empty = SimpleNamespace(_size=0, members=[])
    assert merge_value_types({"SEAT": complete}, layouts)["SEAT"] is complete
    assert merge_value_types({"SEAT": empty}, layouts)["SEAT"] is layouts["SEAT"]


def test_legacy_tag_values_decode_with_taginfo_layouts():
    # The Comps type is missing; the controller decodes the value with the
    # layout from TagInfo.XML.
    db, cur = _database()
    _legacy_controller(cur)
    _tag_with_value(cur, 0x50, "Seat1", 0, SEAT_VALUE, parent_id=3)
    cur.execute("INSERT INTO taginfo_tags VALUES ('', 'Seat1', 'Seat')")
    layouts = taginfo_layouts(ET.fromstring(SEAT_TAGINFO))
    (tag,) = ControllerBuilder(cur, _taginfo_layouts=layouts).build().tags
    xml = tag.to_xml()
    assert '<DataValueMember Name="Extend" DataType="BOOL" Value="1"/>' in xml
    assert '<Element Index="[0]" Value="1.5"/>' in xml
    db.close()
