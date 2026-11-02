"""Older (v19/v20, legacy-layout) project files, on synthetic records."""

import sqlite3
import struct

from acd.l5x.elements import RoutineBuilder


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


def _database():
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("CREATE TABLE comps(object_id int, parent_id int, comp_name text, seq_number int, record_type int, record BLOB)")
    cur.execute("CREATE TABLE comments(seq_number int, sub_record_length int, object_id int, record_string text,"
                " record_type int, parent int, tag_reference text, rung_content int, member_ref int)")
    cur.execute("CREATE TABLE region_map(object_id int, parent_id int, unknown int, seq_no int, record BLOB)")
    cur.execute("CREATE TABLE rungs(object_id int, rung text, seq_number int)")
    return db, cur


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
