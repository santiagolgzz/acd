import sqlite3
from pathlib import Path

from acd.l5x.export_l5x import ExportL5x

RESOURCES = Path(__file__).resolve().parents[1] / "resources"


def test_modern_region_map_keeps_last_entry_on_disk_layout():
    # Real files store u32@0x4A == len(record) - 0x4E (CompsRecord.parse
    # keeps the 4 bytes past the stored length), so every byte from 0x4E to
    # the end is whole 16-byte entries.
    entries = [(1, 0, 2, 0x10), (1, 0, 3, 0x20), (1, 0, 0xFFFFFFFF, 0x30)]
    record = bytearray(0x4E)
    for parent, unknown, seq, oid in entries:
        record += b"".join(v.to_bytes(4, "little") for v in (parent, unknown, seq, oid))
    record[0x4A:0x4E] = (len(record) - 0x4E).to_bytes(4, "little")
    parsed = ExportL5x._parse_region_map(bytes(record))
    assert [(e[0], e[3]) for e in parsed] == [(0x10, 2), (0x20, 3), (0x30, 0xFFFFFFFF)]


def test_cutelogix_every_rung_is_in_region_map(tmp_path):
    export = ExportL5x(str(RESOURCES / "CuteLogix.ACD"), str(tmp_path))
    _ = export.project
    cur = sqlite3.connect(tmp_path / "acd.db").cursor()
    rungs = {r[0] for r in cur.execute("SELECT object_id FROM rungs")}
    mapped = {r[0] for r in cur.execute("SELECT object_id FROM region_map")}
    assert rungs - mapped == set()
