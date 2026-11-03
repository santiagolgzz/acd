"""Tag attributes, atomic Decorated values and descriptions, checked against
Rockwell's own exports of the public test projects and against made-up values."""
import os
import re
import struct
import xml.etree.ElementTree as ET

import pytest

from acd.api import ImportProjectFromFile
from acd.l5x.elements import Tag, _format_time, _radix_value, radix_enum

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


def test_operand_comment_with_cdata_end_marker():
    tag = Tag("T", "T", "Base", "MyType", None, "Read/Write", None, None, 0, [("2", "a]]>b")])
    element = ET.fromstring(tag.to_xml())
    assert element.find("Comments/Comment").text.strip() == "a]]>b"
