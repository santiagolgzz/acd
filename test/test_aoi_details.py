import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from acd.l5x.conversion_notes import ConversionNotes
from acd.l5x.elements import (
    LocalTagBuilder,
    Member,
    ParameterBuilder,
    _aoi_tag_usage_flags,
    _default_data_xml,
    _legacy_aoi_tag_type,
)
from acd.l5x.export_l5x import ExportL5x
from acd.record.rx import RxGeneric

from .rx_fixtures import comps_database, insert_comp
from .test_rx import _legacy_rx

PROJECT = Path("..") / "resources" / "ACDTestsWithAOI.ACD"
REFERENCE = Path("..") / "resources" / "ACDTestsWithAOI.L5X"


def _legacy_tag(usage=0, visibility=0, runtime_type=0xC4, access=0):
    """A legacy-layout AOI tag record with its 544-byte attribute 0x01."""
    metadata = bytearray(544)
    struct.pack_into("<H", metadata, 0x100, runtime_type)
    metadata[0x105] = visibility
    struct.pack_into("<H", metadata, 0x20E, usage)
    struct.pack_into("<H", metadata, 0x21E, access)
    return _legacy_rx(
        cip_type=0x6B,
        attributes=[(0x01, bytes(metadata)), (0x66, b"\xff\xff\xff\xff")],
    )


@pytest.mark.parametrize(
    "usage, visibility, expected",
    [
        (0x4480, 0x00, 0x04),  # EnableIn: input, neither required nor visible
        (0x4500, 0x00, 0x08),  # EnableOut: output
        (0x7480, 0xC0, 0x04 | 0x20 | 0x40),  # required input
        (0x4480, 0x40, 0x04 | 0x40),  # visible, not required
        (0x0580, 0xC0, 0x0C | 0x20 | 0x40),  # InOut sets both usage bits
        (0x0600, 0x00, 0),  # local tag
    ],
)
def test_legacy_usage_flags_map_to_the_current_bits(usage, visibility, expected):
    ext01 = RxGeneric.from_bytes(_legacy_tag(usage, visibility)).attribute(0x01)
    assert _aoi_tag_usage_flags(ext01, legacy=True) == expected


def test_current_usage_flags_are_unchanged():
    ext01 = bytearray(0x220)
    ext01[0x20E] = 0x64
    assert _aoi_tag_usage_flags(bytes(ext01)) == 0x64


def _member(data_type, dimension=0, offset=0, bit=0):
    return Member("M", "M", data_type, dimension, "Decimal", False, None, None,
                  "Read/Write", _offset=offset, _bit=bit)


@pytest.mark.parametrize(
    "runtime_type, member, expected",
    [
        (0xC4, None, ("DINT", None)),
        (0x1C1, None, ("BOOL", None)),  # EnableOut's runtime type
        (0x8FCE, None, ("STRING", None)),  # an InOut STRING has no backing member
        (0x8F83, None, ("TIMER", None)),
        (0x87DF, None, ("", None)),  # an InOut user type stores only a handle
        (0x8FCE, _member("STRING"), ("STRING", None)),
        (0xC4, _member("DINT", dimension=7), ("DINT", "7")),
        (0xC1, _member("BIT"), ("BOOL", None)),
    ],
)
def test_legacy_tag_type(runtime_type, member, expected):
    record = RxGeneric.from_bytes(_legacy_tag(runtime_type=runtime_type))
    assert _legacy_aoi_tag_type(record, member) == expected


def test_legacy_parameter_gets_usage_type_and_flags():
    _, cursor = comps_database()
    insert_comp(cursor, 10, "Speed", _legacy_tag(0x7480, 0xC0, runtime_type=0xCA))
    parameter = ParameterBuilder(cursor, 10, _members={"Speed": _member("REAL")}).build()
    assert (parameter.usage, parameter.data_type, parameter.dimensions) == ("Input", "REAL", None)
    assert (parameter.required, parameter.visible, parameter.radix) == ("true", "true", "Float")


def test_legacy_local_tag_takes_its_array_size_from_the_backing_member():
    _, cursor = comps_database()
    insert_comp(cursor, 10, "Counts", _legacy_tag(0x4600, access=3))
    local_tag = LocalTagBuilder(cursor, 10, _members={"Counts": _member("DINT", dimension=7)}).build()
    assert (local_tag.data_type, local_tag.dimensions) == ("DINT", "7")
    assert local_tag.external_access == "None"


def test_unreadable_inout_type_is_noted_not_guessed():
    _, cursor = comps_database()
    insert_comp(cursor, 10, "Pump", _legacy_tag(0x0580, 0xC0, runtime_type=0x87DF, access=1))
    notes = ConversionNotes()
    parameter = ParameterBuilder(cursor, 10, _notes=notes).build()
    assert parameter.usage == "InOut"
    assert parameter.data_type == ""
    assert len(notes.current()) == 1
    assert "'Pump'" in notes.current()[0]


def test_default_data_of_atomic_uses_the_tag_radix_and_no_name():
    raw = struct.pack("<II", 0, 42)
    members = {"Level": _member("DINT", offset=4)}
    xml = _default_data_xml("Level", "Hex", members, raw, {})
    element = ET.fromstring(xml)
    assert element.get("Format") == "Decorated"
    value = element.find("DataValue")
    assert value is not None
    assert (value.get("Name"), value.get("Radix"), value.get("Value")) == (None, "Hex", "16#0000_002a")


def test_default_data_is_left_out_when_it_cannot_be_decoded():
    members = {"Level": _member("DINT", offset=40)}
    assert _default_data_xml("Level", "Decimal", members, bytes(8), {}) == ""
    assert _default_data_xml("Missing", "Decimal", members, bytes(8), {}) == ""


def _aoi(root):
    return root.find("Controller/AddOnInstructionDefinitions/AddOnInstructionDefinition")


@pytest.fixture(scope="module")
def converted():
    return ET.fromstring(ExportL5x(PROJECT).project.to_xml())


def test_public_aoi_matches_the_reference_export(converted):
    ours, reference = _aoi(converted), _aoi(ET.parse(REFERENCE).getroot())
    for path in ("Parameters/Parameter", "LocalTags/LocalTag"):
        assert [(t.get("Name"), t.get("DataType")) for t in ours.findall(path)] == [
            (t.get("Name"), t.get("DataType")) for t in reference.findall(path)
        ]


def test_public_aoi_default_matches_the_reference_export(converted):
    tag = _aoi(converted).find("LocalTags/LocalTag[@Name='AOIDINTLocalTag']")
    value = tag.find("DefaultData[@Format='Decorated']/DataValue")
    assert (value.get("DataType"), value.get("Radix"), value.get("Value")) == ("DINT", "Decimal", "666")


def test_system_parameters_have_no_default(converted):
    for parameter in _aoi(converted).findall("Parameters/Parameter"):
        assert parameter.find("DefaultData") is None


def test_only_real_tags_reach_the_controller_scope(converted):
    reference = ET.parse(REFERENCE).getroot()
    assert {t.get("Name") for t in converted.findall("Controller/Tags/Tag")} == {
        t.get("Name") for t in reference.findall("Controller/Tags/Tag")
    }


def test_placeholder_tag_records_are_skipped_on_any_name():
    # A type-512 record can carry a name that would otherwise pass as a tag.
    project = ExportL5x(PROJECT)
    cursor = project._cur
    collection = cursor.execute(
        "SELECT c.object_id FROM comps c JOIN comps p ON p.object_id = c.parent_id "
        "WHERE c.comp_name='RxTagCollection' AND p.parent_id=0"
    ).fetchone()[0]
    record = _legacy_tag(0x0600)
    cursor.execute(
        "INSERT INTO comps VALUES (?, ?, ?, ?, ?, ?)",
        (424242, collection, "PlaceholderName", 0, 512, record),
    )
    names = {t.name for t in project.project.controller.tags}
    assert "PlaceholderName" not in names


