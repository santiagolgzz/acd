import os
import re
import sqlite3
import struct
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from acd.api import ImportProjectFromFile
from acd.l5x.elements import (
    _decorated_from_data,
    _format_time,
    _l5k_from_data,
    _logix_string,
    radix_enum,
    _radix_value,
    scoped_comment_keys,
    Tag,
    _USER_COMMENT,
)
from acd.l5x.export_l5x import ExportL5x
from acd.record.comments import CommentsRecord

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


def test_rung_comment_without_rung_uids(monkeypatch):
    # When RegnLink.Dat gives no rung uids for a routine, its rung comments
    # are still looked up by rung position.
    monkeypatch.setattr(ExportL5x, "_parse_rung_uids", staticmethod(lambda data, rungs: []))
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


def _dat(buf: bytes):
    return SimpleNamespace(identifier=64250, record=SimpleNamespace(record_buffer=buf))


def _type1_comment(kind: int, text: bytes) -> bytes:
    # u16 seq, u16 type 1, u16 sub-record length, parent key, member and rung
    # refs, 5 zero bytes, u32 kind (offset 27), 13 zero bytes, text.
    body = (
        struct.pack("<HHHIIi", 0, 1, len(text) + 13, 0x1A2B0068, 0x3C4D006D, 0)
        + bytes(5) + struct.pack("<I", kind) + bytes(13) + text + b"\0"
    )
    return struct.pack("<I", len(body)) + body


def test_type1_internal_string_records_are_left_out_of_descriptions():
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
    cur.execute(
        f"SELECT record_string FROM comments WHERE parent=? AND member_ref=? AND {_USER_COMMENT} LIMIT 1",
        (0x1A2B0068, 0x3C4D006D),
    )
    assert cur.fetchall() == [("Made up note",)]
    db.close()


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


def test_string_value_with_cdata_end_marker():
    data = struct.pack("<i", 3) + b"]]>" + bytes(84 - 3)
    xml = _decorated_from_data("STRING", None, data)
    assert "<![CDATA[']]]]><![CDATA[>']]>" in xml


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



def test_largest_real_value():
    # Rounding the largest finite REAL to 4 digits overflows a 32-bit float.
    raw = struct.pack("<I", 0x7F7FFFFF)
    xml = _decorated_from_data("REAL", "1", raw)
    assert '<Element Index="[0]" Value="3.4028235e+38"/>' in xml

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
    attributes = {a.attribute_id: bytes(a.value) for a in RxGeneric.from_bytes(entry[5]).extended_records}
    assert attributes[0x65] == path


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


RESOURCES = os.path.join("..", "resources")


def _pair(name: str):
    ours = ImportProjectFromFile(os.path.join(RESOURCES, name + ".ACD")).import_project().to_xml()
    ref = ET.parse(os.path.join(RESOURCES, name + ".L5X")).getroot()
    return ET.fromstring(ours).find("Controller"), ref.find("Controller")


@pytest.fixture(scope="module")
def aoi_pair():
    return _pair("ACDTestsWithAOI")


def _tags(controller):
    return {t.get("Name"): t for t in controller.iter("Tag")}


def test_tag_set_and_attributes_match_reference(aoi_pair):
    ours, ref = aoi_pair
    ours_tags, ref_tags = _tags(ours), _tags(ref)
    assert sorted(ours_tags) == sorted(ref_tags)
    for name, ref_tag in ref_tags.items():
        # Same attributes, in the same order.
        assert list(ours_tags[name].attrib.items()) == list(ref_tag.attrib.items()), name


def test_atomic_decorated_values_match_reference(aoi_pair):
    ours, ref = aoi_pair
    ours_tags = _tags(ours)
    for name, ref_tag in _tags(ref).items():
        ref_value = ref_tag.find("Data[@Format='Decorated']/DataValue")
        if ref_value is None:
            continue
        value = ours_tags[name].find("Data[@Format='Decorated']/DataValue")
        assert value is not None, name
        assert value.attrib == ref_value.attrib, name


def test_atomic_tags_keep_l5k_data_first(aoi_pair):
    ours, _ = aoi_pair
    formats = [d.get("Format") for d in _tags(ours)["RadixHex"].findall("Data")]
    assert formats == ["L5K", "Decorated"]


@pytest.mark.parametrize("name", ["ACDTestsWithAOI", "ACDTestsEmptyRedundant"])
def test_controller_description_matches_reference(name):
    ours, ref = _pair(name)
    assert ours.findtext("Description").strip() == ref.findtext("Description").strip() != ""


def test_routine_descriptions_match_reference(aoi_pair):
    ours, ref = aoi_pair

    def descriptions(controller):
        return {
            r.get("Name"): r.findtext("Description").strip()
            for r in controller.iter("Routine")
            if r.find("Description") is not None
        }

    assert descriptions(ours) == descriptions(ref)
    assert "STRoutine" in descriptions(ours)


def test_time_radixes():
    assert [radix_enum(i) for i in (10, 11, 12, 13, 14)] == [
        "Date/Time", "Date/Time (ns)", "Time32 (us)", "Time (us)", "LTime (ns)",
    ]


@pytest.mark.parametrize("dt, radix, raw, text", [
    ("DINT", "Decimal", struct.pack("<i", -42), "-42"),
    ("DINT", "Hex", struct.pack("<i", 0x12AB), "16#0000_12ab"),
    ("SINT", "Hex", struct.pack("<b", -1), "16#ff"),
    ("SINT", "Binary", struct.pack("<b", 5), "2#0000_0101"),
    ("INT", "Octal", struct.pack("<h", 15), "8#000_017"),
    ("DINT", "Octal", struct.pack("<i", 8), "8#00_000_000_010"),
    ("DINT", "ASCII", struct.pack("<i", 0x41422443), "'AB$$C'"),
    ("REAL", "Float", struct.pack("<f", 2.5), "2.5"),
    ("REAL", "Exponential", struct.pack("<f", 2.5), "2.50000000e+000"),
    ("BOOL", "Decimal", b"\x01", "1"),
    ("TIME32", "Time32 (us)", struct.pack("<i", -5), "T32#-5us"),
])
def test_radix_value(dt, radix, raw, text):
    assert _radix_value(dt, radix, raw) == text


def test_format_time():
    assert _format_time("TIME", 90_061_001_001) == "T#1d_1h_1m_1s_1ms_1us"
    assert _format_time("LTIME", 1_500) == "LT#1us_500ns"
    assert _format_time("LTIME", 0) == "LT#0ns"
    assert _format_time("DT", 86_400_000_000 + 1_234) == "DT#1970-01-02-00:00:00.001_234Z"
    assert _format_time("LDT", 7) == "LDT#1970-01-01-00:00:00.000_000_007Z"


def _opening(tag: Tag) -> str:
    match = re.match(r"<Tag [^>]*>", tag.to_xml())
    assert match is not None
    return match.group(0)


def test_tag_attribute_order_constant_and_usage():
    tag = Tag("P", "P", "Base", "DINT", "Hex", "Read Only", None, None, 0, [])
    tag.usage = "Output"
    assert _opening(tag) == (
        '<Tag Name="P" TagType="Base" DataType="DINT" Radix="Hex" Usage="Output" '
        'Constant="false" ExternalAccess="Read Only">'
    )
    assert '<DataValue DataType="DINT" Radix="Hex" Value="16#0000_0000"/>' in tag.to_xml()


def test_array_and_structure_tags():
    array = Tag("A", "A", "Base", "INT", "Decimal", "Read/Write", "true", "4", 0, [])
    assert _opening(array) == (
        '<Tag Name="A" TagType="Base" DataType="INT" Dimensions="4" Radix="Decimal" '
        'Constant="true" ExternalAccess="Read/Write">'
    )
    assert "<DataValue " not in array.to_xml()
    struct_tag = Tag("S", "S", "Base", "TIMER", "NullType", "Read/Write", None, None, 0, [])
    assert "Radix" not in _opening(struct_tag)


def test_nonfinite_real():
    assert _l5k_from_data("REAL", struct.pack("<f", float("nan"))) == "1.#QNAN000e+000"
    assert _l5k_from_data("REAL", struct.pack("<f", float("-inf"))) == "-1.#INF0000e+000"


def test_own_description_skips_internal_type1_records():
    from acd.l5x.elements import _own_description
    from .rx_fixtures import comps_database

    connection, cur = comps_database()
    rows = [
        # (object_id, text, record_type): an internal type-1 string (object_id 4),
        # then the user's description (object_id 1).
        (4, "SomeMember", 1),
        (1, "Made up note", 1),
    ]
    for object_id, text, record_type in rows:
        cur.execute(
            "INSERT INTO comments VALUES (0, 0, ?, ?, ?, ?, '', 0, 0)",
            (object_id, text, record_type, 0x00010068),
        )
    try:
        assert _own_description(cur, 0x00010068, 0) == "Made up note"
        assert _own_description(cur, 0x00020068, 0) is None
    finally:
        connection.close()
