"""Older (v19/v20, legacy-layout) project files, on synthetic records."""

import sqlite3
import struct
from types import SimpleNamespace

from acd.l5x.elements import RoutineBuilder, TagBuilder
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
