import os
import struct
import xml.etree.ElementTree as ET

from acd.api import ImportProjectFromFile
from acd.l5x.elements import (
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
