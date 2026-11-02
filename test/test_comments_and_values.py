import os
import sqlite3
import struct
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from acd.api import ImportProjectFromFile
from acd.l5x.elements import _USER_COMMENT, scoped_comment_keys
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
