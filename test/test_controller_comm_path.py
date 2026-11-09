import struct
from pathlib import Path

from acd.l5x.elements import ControllerBuilder
from acd.l5x.export_l5x import ExportL5x

RESOURCES = Path(__file__).resolve().parents[1] / "resources"


def _with_final_attribute(record: bytes, attribute_id: int, value: bytes) -> bytes:
    # Current-layout Rx record: stored length at 74, attribute count at 78.
    stored_length, count = struct.unpack_from("<II", record, 74)
    extra = struct.pack("<II", attribute_id, len(value)) + value
    head = record[:74] + struct.pack("<II", stored_length + len(extra), count + 1)
    return head + record[82:] + extra


def test_comm_path_stored_with_slot_is_used_as_is(tmp_path):
    export = ExportL5x(str(RESOURCES / "CuteLogix.ACD"), str(tmp_path))
    cur = export._cur
    cur.execute("SELECT object_id, record FROM comps WHERE parent_id=0 AND record_type=256")
    object_id, record = cur.fetchone()
    path = "AB_ETHIP-1\\192.168.1.10\\Backplane\\0".encode("utf-16-le") + b"\0\0"
    cur.execute(
        "UPDATE comps SET record=? WHERE object_id=?",
        (_with_final_attribute(bytes(record), 0x6A, path), object_id),
    )

    controller = ControllerBuilder(cur).build()

    assert controller.comm_path == "AB_ETHIP-1\\192.168.1.10\\Backplane\\0"


def test_comm_path_read_when_final_attribute_is_cut_short(tmp_path):
    # A record without the four bytes past its stored length has a final
    # attribute four bytes shorter than its stated length; the CommPath is
    # still read from the bytes that are there.
    export = ExportL5x(str(RESOURCES / "CuteLogix.ACD"), str(tmp_path))
    cur = export._cur
    cur.execute("SELECT object_id, record FROM comps WHERE parent_id=0 AND record_type=256")
    object_id, record = cur.fetchone()
    path = "AB_ETHIP-1\\192.168.1.10\\Backplane\\0".encode("utf-16-le") + b"\0\0"
    record = _with_final_attribute(bytes(record), 0x6A, path + b"\0" * 4)[:-4]
    record = record[:74] + struct.pack("<I", len(record) - 74) + record[78:]
    cur.execute("UPDATE comps SET record=? WHERE object_id=?", (record, object_id))

    controller = ControllerBuilder(cur).build()

    assert controller.comm_path == "AB_ETHIP-1\\192.168.1.10\\Backplane\\0"
