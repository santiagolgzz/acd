import pytest

from acd.l5x.export_l5x import ExportL5x


def test_parse_empty_modern_region_map():
    assert ExportL5x._parse_region_map(bytes(0x4A)) == []


@pytest.mark.parametrize("legacy", [False, True])
def test_parse_region_map_layout(legacy):
    entries = [
        (0x11111111, 1, 2, 3),
        (0x22222222, 4, 5, 6),
    ]

    if legacy:
        identifier_offset = 0x1C
        record = bytearray(identifier_offset + len(entries) * 16)
        record[0x18:0x1C] = (len(record) - 0x1C).to_bytes(4, "little")
    else:
        identifier_offset = 0x4E
        region_length = len(entries) * 16 + 4
        record = bytearray(identifier_offset + region_length - 4)
        record[0x4A:0x4E] = region_length.to_bytes(4, "little")

    expected = []
    for object_id, parent_id, unknown, sequence in entries:
        raw_entry = (
            parent_id.to_bytes(4, "little")
            + unknown.to_bytes(4, "little")
            + sequence.to_bytes(4, "little")
            + object_id.to_bytes(4, "little")
        )
        record[identifier_offset : identifier_offset + 16] = raw_entry
        expected.append((object_id, parent_id, unknown, sequence, raw_entry))
        identifier_offset += 16

    assert ExportL5x._parse_region_map(bytes(record)) == expected


def test_parse_region_map_rejects_truncated_record():
    record = bytearray(0x1C + 16)
    record[0x18:0x1C] = (32).to_bytes(4, "little")

    with pytest.raises(ValueError, match="Invalid Region Map length"):
        ExportL5x._parse_region_map(bytes(record))


def test_modern_region_map_wins_legacy_length_collision():
    record = bytearray(0x4E + 16)
    record[0x18:0x1C] = (len(record) - 0x1C).to_bytes(4, "little")
    record[0x4A:0x4E] = (20).to_bytes(4, "little")
    entry = (1, 2, 3, 4)
    record[0x4E:] = b"".join(value.to_bytes(4, "little") for value in entry)

    assert ExportL5x._parse_region_map(bytes(record)) == [
        (4, 1, 2, 3, bytes(record[0x4E:]))
    ]


def _populate(candidates, rung_ids=(), notes=None):
    import sqlite3
    from types import SimpleNamespace

    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("CREATE TABLE region_map (object_id, parent_id, unknown, seq_no, record)")
    cur.execute("CREATE TABLE rungs (object_id, rung, seq_number)")
    cur.executemany("INSERT INTO rungs VALUES (?, '', 0)", [(i,) for i in rung_ids])
    # The comps table keeps the largest copy.
    cur.execute("CREATE TABLE comps (object_id, parent_id, comp_name, record)")
    cur.execute(
        "INSERT INTO comps VALUES (1, 0, 'Region Map', ?)", (max(candidates, key=len),)
    )
    export = SimpleNamespace(
        _cur=cur,
        _db=db,
        _notes=SimpleNamespace(warn=(notes if notes is not None else []).append),
        _parse_region_map=ExportL5x._parse_region_map,
    )
    ExportL5x.populate_region_map(export, candidates)
    return [row[0] for row in cur.execute("SELECT object_id FROM region_map")]


def _modern_region_map(object_ids):
    import struct

    record = bytearray(0x4E)
    for seq, object_id in enumerate(object_ids):
        record += struct.pack("<IIII", 1, 0, seq, object_id)
    record[0x4A:0x4E] = (len(record) - 0x4E).to_bytes(4, "little")
    return bytes(record)


def _misaligned_region_map(object_ids):
    # Stored length doesn't match: entries are found by matching known rungs.
    import struct

    return bytes(0x50) + b"".join(
        struct.pack("<IIII", 1, 0, seq, object_id) for seq, object_id in enumerate(object_ids)
    )


def test_stale_region_map_copy_is_skipped():
    # A larger stale copy that fails validation must not hide the valid one.
    stale = _modern_region_map([0x10, 0x20]) + b"\x00" * 5
    assert _populate([_modern_region_map([0x30]), stale]) == [0x30]


def test_largest_copy_is_realigned_when_no_copy_validates():
    largest = _misaligned_region_map([0x41, 0x42, 0x43])
    assert _populate([bytes(0x4E + 3), largest], rung_ids=[0x41, 0x42, 0x43]) == [0x41, 0x42, 0x43]


def test_copy_placing_more_known_rungs_wins():
    # A shorter copy whose stored length checks out must not hide rungs
    # that the larger copy places once re-aligned.
    larger = _misaligned_region_map([0x41, 0x42, 0x43])
    shorter = _modern_region_map([0x41])
    assert _populate([shorter, larger], rung_ids=[0x41, 0x42, 0x43]) == [0x41, 0x42, 0x43]


def test_realignment_note_only_when_realigned_copy_is_used():
    notes = []
    stale = _misaligned_region_map([0x41])
    assert _populate([_modern_region_map([0x41]), stale], rung_ids=[0x41], notes=notes) == [0x41]
    assert notes == []
    rungs = [0x41, 0x42, 0x43]
    assert _populate([_misaligned_region_map(rungs)], rung_ids=rungs, notes=notes) == rungs
    assert len(notes) == 1
