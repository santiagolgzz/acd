import struct

import pytest

from acd.l5x.conversion_notes import ConversionNotes
from acd.l5x.elements import (
    ControllerBuilder,
    RoutineBuilder,
    TagBuilder,
    TaskBuilder,
    _program_schedule_id,
)
from acd.record.rx import LegacyRxGeneric, RxGeneric

from .rx_fixtures import comps_database, insert_comp, protected_rx_record, rx_header, rx_record


def _metadata(name, runtime_type=0xC4, dimensions=(0, 0, 0), schedule_id=None):
    value = bytearray(544)
    encoded_name = name.encode("utf-8")
    struct.pack_into("<H", value, 0, len(encoded_name))
    value[2 : 2 + len(encoded_name)] = encoded_name
    struct.pack_into("<H", value, 0x100, runtime_type)
    struct.pack_into("<I", value, 0x102, 123)
    value[0x20F] = 4 << 4
    struct.pack_into("<III", value, 0x212, *dimensions)
    struct.pack_into("<H", value, 0x21E, 0)
    if schedule_id is not None:
        struct.pack_into("<I", value, 267, schedule_id)
    return bytes(value)


def _legacy_rx(
    *,
    cip_type=0x6B,
    data_instance_id=0,
    object_id=1,
    runtime_type=0,
    comment_id=0,
    attributes=(),
):
    raw = bytearray(32)
    struct.pack_into("<II", raw, 0, data_instance_id, 0x12345678)
    struct.pack_into("<HH", raw, 8, cip_type, comment_id)
    struct.pack_into("<I", raw, 16, runtime_type)
    struct.pack_into("<I", raw, 20, object_id)
    struct.pack_into("<I", raw, 28, len(attributes))
    for attribute_id, value in attributes:
        raw.extend(struct.pack("<II", attribute_id, len(value)))
        raw.extend(value)
    struct.pack_into("<I", raw, 24, len(raw) - 28)
    return bytes(raw)


def test_parse_legacy_rx_generic():
    attributes = [
        (0x65, b"\x01\x02\x03\x04"),
        (0x66, b"\x05\x06\x07\x08"),
    ]
    raw = _legacy_rx(
        data_instance_id=0xFFFFFFFF,
        object_id=0x87654321,
        runtime_type=0xC4,
        comment_id=3,
        attributes=attributes,
    )

    result = RxGeneric.from_bytes(raw)

    assert isinstance(result, LegacyRxGeneric)
    assert result.data_instance_id == 0xFFFFFFFF
    assert result.unique_tag_identifier == 0x12345678
    assert result.record_format_version == 0
    assert result.cip_type == 0x6B
    assert result.comment_id == 3
    assert result.object_id == 0x87654321
    assert result.main_record.data_type == 0xC4
    assert result.record_buffer == raw[14:74]
    assert [
        (attribute.attribute_id, attribute.value)
        for attribute in result.extended_records
    ] == attributes


def test_modern_rx_layout_wins_legacy_length_collision():
    raw = bytearray(82)
    struct.pack_into("<H", raw, 10, 0x6B)
    struct.pack_into("<I", raw, 24, len(raw) - 24)
    struct.pack_into("<I", raw, 74, len(raw) - 74)
    struct.pack_into("<I", raw, 78, 1)

    result = RxGeneric.from_bytes(bytes(raw))

    assert not isinstance(result, LegacyRxGeneric)
    assert result.cip_type == 0x6B


def test_legacy_rx_accepts_short_final_attribute():
    raw = bytearray(
        _legacy_rx(attributes=[(0x01, b"\x01\x02\x03\x04")])
    )
    struct.pack_into("<I", raw, 36, 8)
    struct.pack_into("<I", raw, 24, len(raw) - 24)

    result = RxGeneric.from_bytes(bytes(raw))

    assert result.extended_records[0].len_value == 8
    assert result.extended_records[0].value == b"\x01\x02\x03\x04"


@pytest.mark.parametrize(
    "type_name,runtime_type,dimensions,expected_dimensions",
    [
        ("DINT", 0xC4, (0, 0, 0), None),
        ("REAL", 0x20CA, (10, 0, 0), "10"),
        ("strItemInfo", 0xA1E7, (200, 0, 0), "200"),
        ("BOOL", 0x20D3, (1, 0, 0), "32"),
    ],
)
def test_build_legacy_base_tag(
    type_name, runtime_type, dimensions, expected_dimensions
):
    database, cursor = comps_database()
    try:
        insert_comp(cursor, 300, type_name, b"")
        insert_comp(
            cursor,
            200,
            "DataInstance",
            _legacy_rx(cip_type=0x6A, data_instance_id=300, object_id=200),
        )
        insert_comp(
            cursor,
            100,
            "LegacyTag",
            _legacy_rx(
                data_instance_id=200,
                object_id=100,
                runtime_type=runtime_type,
                attributes=[
                    (0x01, _metadata("LegacyTag", runtime_type, dimensions))
                ],
            ),
        )

        tag = TagBuilder(cursor, 100).build()

        assert tag.name == "LegacyTag"
        assert tag.tag_type == "Base"
        assert tag.data_type == type_name
        assert tag.dimensions == expected_dimensions
        assert tag.radix == "Decimal"
        assert tag.external_access == "Read/Write"
    finally:
        database.close()


def test_build_legacy_message_tag():
    database, cursor = comps_database()
    try:
        insert_comp(
            cursor,
            200,
            "MessageData",
            _legacy_rx(cip_type=0x8D, data_instance_id=0, object_id=200),
        )
        insert_comp(
            cursor,
            100,
            "LegacyMessage",
            _legacy_rx(
                data_instance_id=200,
                object_id=100,
                attributes=[(0x01, _metadata("LegacyMessage"))],
            ),
        )

        assert TagBuilder(cursor, 100).build().data_type == "MESSAGE"
    finally:
        database.close()


@pytest.mark.parametrize(
    "target_class,runtime_type,expected_type",
    [
        (0xB0, 0x8FFD, "MOTION_GROUP"),
        (0xB1, 0x8FC8, "AXIS_SERVO"),
        (0xB1, 0x8FC7, "AXIS_SERVO_DRIVE"),
    ],
)
def test_build_legacy_motion_tag(target_class, runtime_type, expected_type):
    database, cursor = comps_database()
    try:
        insert_comp(
            cursor,
            200,
            "MotionData",
            _legacy_rx(cip_type=target_class, object_id=200),
        )
        metadata = bytearray(_metadata("LegacyMotion", runtime_type))
        metadata[0x20F] = 0
        insert_comp(
            cursor,
            100,
            "LegacyMotion",
            _legacy_rx(
                data_instance_id=200,
                object_id=100,
                attributes=[(0x01, bytes(metadata))],
            ),
        )

        tag = TagBuilder(cursor, 100).build()

        assert tag.data_type == expected_type
        assert tag.radix is None
    finally:
        database.close()


@pytest.mark.parametrize(
    "expected_tag_type,descriptor_length",
    [("Produced", 0x156), ("Produced", 0x316), ("Consumed", 0x150)],
)
def test_build_legacy_produced_and_consumed_tags(
    expected_tag_type, descriptor_length
):
    database, cursor = comps_database()
    try:
        insert_comp(cursor, 300, "DINT", b"")
        insert_comp(
            cursor,
            200,
            "DataInstance",
            _legacy_rx(cip_type=0x6A, data_instance_id=300, object_id=200),
        )
        linked_metadata = struct.pack("<IIII", 4, 3, 0, 2)
        insert_comp(
            cursor,
            201,
            "LinkedData",
            _legacy_rx(
                cip_type=0x6A,
                object_id=201,
                attributes=[(0x64, linked_metadata)],
            ),
        )

        tag_attributes = [
            (0x01, _metadata("LegacyTag")),
            (0x6B, struct.pack("<I", 201)),
        ]
        if expected_tag_type == "Produced":
            descriptor = bytearray(descriptor_length)
            struct.pack_into("<H", descriptor, 0, 0x0A)
            struct.pack_into("<I", descriptor, 0x16, 0x12345678)
            struct.pack_into("<I", descriptor, 0x1A, 4)
            struct.pack_into("<I", descriptor, 0x1E, 0x12345678)
            struct.pack_into("<H", descriptor, 0x141, 3)
            descriptor[0x144] = 1
            struct.pack_into("<I", descriptor, len(descriptor) - 0x10, 200)
            struct.pack_into(
                "<I", descriptor, len(descriptor) - 0x0C, 536_870_900
            )
            descriptor_attribute = 0x191
        else:
            descriptor = bytearray(0x150)
            struct.pack_into("<H", descriptor, 0, 0x09)
            struct.pack_into("<I", descriptor, 0x02, 500_000)
            struct.pack_into("<H", descriptor, 0x06, 7)
            struct.pack_into("<I", descriptor, 0x08, 0x12345678)
            struct.pack_into("<I", descriptor, 0x0C, 4)
            struct.pack_into("<I", descriptor, 0x10, 0x12345678)
            remote_tag = b"RemoteTag"
            struct.pack_into("<H", descriptor, 0x22, len(remote_tag))
            descriptor[0x24 : 0x24 + len(remote_tag)] = remote_tag
            descriptor[0x143] = 2
            descriptor_attribute = 0x190
            insert_comp(cursor, 400, "ProducerPLC", b"")
            tag_attributes.insert(1, (0x66, struct.pack("<I", 400)))

        insert_comp(
            cursor,
            500,
            "ConnectionDescriptor",
            _legacy_rx(
                cip_type=0x7E,
                object_id=500,
                attributes=(
                    [
                        (0x01, bytes(descriptor)),
                        (descriptor_attribute, struct.pack("<I", 100)),
                    ]
                    if descriptor_length == 0x316
                    else [
                        (descriptor_attribute, struct.pack("<I", 100)),
                        (0x01, bytes(descriptor)),
                    ]
                ),
            ),
        )
        insert_comp(
            cursor,
            100,
            "LegacyTag",
            _legacy_rx(
                data_instance_id=200,
                object_id=100,
                attributes=tag_attributes,
            ),
        )

        tag = TagBuilder(cursor, 100).build()

        assert tag.tag_type == expected_tag_type
        xml = tag.to_xml()
        if expected_tag_type == "Produced":
            assert tag.produce_info.produce_count == "3"
            assert tag.produce_info.minimum_rpi == "0.200"
            assert tag.constant == "false"
            assert 'ProduceCount="3"' in xml
            assert 'MaximumRPI="536870.900"' in xml
        else:
            assert tag.consume_info.producer == "ProducerPLC"
            assert tag.consume_info.remote_tag == "RemoteTag"
            assert tag.consume_info.remote_instance == "7"
            assert 'Producer="ProducerPLC"' in xml
            assert 'RPI="500"' in xml
    finally:
        database.close()


def test_legacy_processor_minimum_rpi():
    database, cursor = comps_database()
    try:
        metadata = bytearray(48)
        struct.pack_into("<HHH", metadata, 2, 1, 14, 72)
        insert_comp(
            cursor,
            600,
            "Local",
            _legacy_rx(
                cip_type=0x69,
                object_id=600,
                attributes=[(0x01, bytes(metadata))],
            ),
        )

        assert TagBuilder(cursor)._legacy_processor_minimum_rpi() == 1000
    finally:
        database.close()


def test_build_legacy_alias_tag():
    database, cursor = comps_database()
    try:
        target_id = 0x44CD5793
        insert_comp(
            cursor,
            target_id,
            "$Target$",
            _legacy_rx(
                object_id=target_id,
                attributes=[(0x01, _metadata("Automation_Bits"))],
            ),
        )
        alias_path = f"@{target_id:08x}@.11\0".encode("utf-16-le")
        insert_comp(
            cursor,
            100,
            "LegacyAlias",
            _legacy_rx(
                data_instance_id=target_id,
                object_id=100,
                attributes=[
                    (0x01, _metadata("LegacyAlias", 0xC1)),
                    (0x65, alias_path),
                ],
            ),
        )

        tag = TagBuilder(cursor, 100).build()

        assert tag.tag_type == "Alias"
        assert tag.data_type is None
        assert tag.alias_for == "Automation_Bits.11"
        xml = tag.to_xml()
        assert 'AliasFor="Automation_Bits.11"' in xml
        assert "DataType=" not in xml
    finally:
        database.close()


def test_legacy_alias_rejects_missing_target():
    database, cursor = comps_database()
    try:
        alias_path = "@44cd5793@.11\0".encode("utf-16-le")
        insert_comp(
            cursor,
            100,
            "LegacyAlias",
            _legacy_rx(
                data_instance_id=0x44CD5793,
                object_id=100,
                attributes=[
                    (0x01, _metadata("LegacyAlias", 0xC1)),
                    (0x65, alias_path),
                ],
            ),
        )

        with pytest.raises(ValueError, match="target does not exist"):
            TagBuilder(cursor, 100).build()
    finally:
        database.close()


def test_legacy_program_schedule_id_and_task():
    schedule_id = 0x2420
    program = _legacy_rx(
        cip_type=0x68,
        attributes=[
            (0x01, _metadata("MainProgram", schedule_id=schedule_id))
        ],
    )
    assert _program_schedule_id(RxGeneric.from_bytes(program)) == schedule_id

    task_data = bytearray(806)
    struct.pack_into("<H", task_data, 0, 1)
    struct.pack_into("<I", task_data, 2, schedule_id)
    struct.pack_into("<I", task_data, 514, 10_000)
    struct.pack_into("<H", task_data, 652, 2)
    struct.pack_into("<H", task_data, 654, 5)
    struct.pack_into("<I", task_data, 802, 500_000)
    task_record = _legacy_rx(
        cip_type=0x70,
        attributes=[(0x01, bytes(task_data))],
    )

    database, cursor = comps_database()
    try:
        insert_comp(cursor, 100, "MainTask", task_record)
        task = TaskBuilder(cursor, 100).build({schedule_id: "MainProgram"})
        assert task is not None

        assert task.type == "PERIODIC"
        assert task.rate == "10"
        assert task.priority == "5"
        assert task.watchdog == "500"
        assert [program.name for program in task.scheduled_programs] == ["MainProgram"]
    finally:
        database.close()


def test_legacy_task_ignores_excess_program_count():
    task_data = bytearray(806)
    struct.pack_into("<H", task_data, 0, 129)
    task_record = _legacy_rx(
        cip_type=0x70,
        attributes=[(0x01, bytes(task_data))],
    )
    database, cursor = comps_database()
    try:
        insert_comp(cursor, 100, "MainTask", task_record)
        # An implausible count means an unknown task layout: keep the task,
        # drop the unreadable schedule list.
        task = TaskBuilder(cursor, 100).build({})
        assert task is not None
        assert task.scheduled_programs == []
    finally:
        database.close()


def _rll_main_record() -> bytes:
    # 60-byte main record with routine type 1 (RLL) at 0x30.
    main = bytearray(60)
    struct.pack_into("<H", main, 0x30, 1)
    return bytes(main)


def test_protected_routine_keeps_its_type():
    # 14-byte header, the main record, then an extended-attribute area whose
    # lengths are unreadable (encrypted).
    header = struct.pack("<IIHHH", 0, 0, 0, 0x6D, 0)
    record = header + _rll_main_record() + struct.pack("<II", 999, 2) + struct.pack("<II", 0x01, 0x55555555)
    database, cursor = comps_database()
    try:
        insert_comp(cursor, 100, "Logic", record)
        notes = ConversionNotes()
        routine = RoutineBuilder(cursor, 100, _notes=notes).build()
        assert (routine.type, routine.rungs) == ("RLL", [])
        assert "could not be read" in notes.current()[-1]
    finally:
        database.close()


def test_protected_routine_with_envelope_marker_keeps_its_type():
    # The encrypted envelope in place of the attributes.
    record = protected_rx_record(0x6D, main=_rll_main_record())
    database, cursor = comps_database()
    try:
        insert_comp(cursor, 100, "Logic", record)
        notes = ConversionNotes()
        routine = RoutineBuilder(cursor, 100, _notes=notes).build()
        assert (routine.type, routine.rungs) == ("RLL", [])
        assert "could not be read" in notes.current()[-1]
    finally:
        database.close()


def _routine_tables(cursor):
    cursor.execute("CREATE TABLE region_map(object_id int, parent_id int, unknown int)")
    cursor.execute("CREATE TABLE rungs(object_id int, rung text)")
    cursor.execute("CREATE TABLE rung_uids(object_id int, parent_id int, uid int)")
    cursor.execute("CREATE TABLE comment_keys(object_id int, scope_key int, member_key int)")


def test_older_rung_comments_use_object_id_as_rung_number():
    # A routine with no owning scope key files rung comments under its own
    # key, numbering rungs from 1 in object_id.
    record = rx_header(0x6D, 0x0055, _rll_main_record(), unknown=0x10) + struct.pack("<II", 4, 0)
    key = 0x0055 * 0x10000 + 0x6D
    database, cursor = comps_database()
    try:
        _routine_tables(cursor)
        insert_comp(cursor, 100, "Logic", record)
        for index in range(3):
            cursor.execute("INSERT INTO region_map VALUES (?, 100, ?)", (200 + index, index))
            cursor.execute("INSERT INTO rungs VALUES (?, ?)", (200 + index, f"NOP();{index}"))
        cursor.executemany(
            "INSERT INTO comments VALUES (0, 0, ?, ?, 1, ?, '', ?, 0)",
            [(1, "First rung", key, 7), (3, "Third rung", key, 7), (2, "Internal", key, 0)],
        )
        routine = RoutineBuilder(cursor, 100, _notes=ConversionNotes()).build()
        assert routine._rung_comments == {0: "First rung", 2: "Third rung"}
        # The internal string (object_id 2, no rung) is not the description.
        assert routine._description is None
    finally:
        database.close()


def test_controller_tag_comments_keep_file_order():
    # The comments index sorts by (parent, member_ref); the tag still gets
    # its comments in the order they were stored.
    attributes = struct.pack("<II", 0x01, 3) + b"\x01\x00T"
    record = rx_record(0x6B, 0x0077, attributes, 1, length_extra=8)
    key = 0x0077 * 0x10000 + 0x6B
    database, cursor = comps_database()
    try:
        cursor.execute("CREATE TABLE taginfo_tags(program text, name text, data_type text)")
        cursor.execute("CREATE INDEX idx_comments_parent_member ON comments(parent, member_ref)")
        insert_comp(cursor, 100, "T", record)
        cursor.executemany(
            "INSERT INTO comments VALUES (0, 0, 1, ?, 3, ?, ?, 0, ?)",
            [("Stored first", key, ".A", 9), ("Stored second", key, ".B", 0)],
        )
        tag = TagBuilder(cursor, 100).build()
        assert [text for _, text in tag._comments] == ["Stored first", "Stored second"]
    finally:
        database.close()


def _program_collection(cursor, program_record: bytes):
    insert_comp(cursor, 50, "RxProgramCollection", b"", parent_id=1)
    insert_comp(cursor, 60, "MainProgram", program_record, parent_id=50)
    insert_comp(cursor, 61, "RxRoutineCollection", b"", parent_id=60)
    insert_comp(cursor, 62, "RxTagCollection", b"", parent_id=60)


def test_program_without_schedule_id_is_still_built():
    # Attribute 0x01 is too short for the schedule ID but holds the
    # disabled flag (u32 0xFFFFFFFF at 0x24).
    ext01 = bytearray(0x28)
    struct.pack_into("<I", ext01, 0x24, 0xFFFFFFFF)
    program = _legacy_rx(cip_type=0x68, attributes=[(0x01, bytes(ext01))])
    database, cursor = comps_database()
    try:
        _program_collection(cursor, program)
        notes = ConversionNotes()
        builder = ControllerBuilder(cursor, 1, _notes=notes)
        programs = builder._programs(50, {}, {}, False)
        assert [(p.name, p.disabled) for p in programs] == [("MainProgram", "true")]
        assert builder._program_schedule_ids(50) == {}
        assert len(notes.current()) == 1
        assert "task-schedule ID could not be read" in notes.current()[0]
    finally:
        database.close()


def test_unreadable_program_is_a_placeholder():
    database, cursor = comps_database()
    try:
        _program_collection(cursor, b"\x01\x02\x03")
        notes = ConversionNotes()
        programs = ControllerBuilder(cursor, 1, _notes=notes)._programs(50, {}, {}, False)
        assert [(p.name, p.disabled, p.routines) for p in programs] == [("MainProgram", "false", [])]
        assert "empty placeholder" in notes.current()[0]
    finally:
        database.close()


def test_unparseable_large_task_reads_fixed_offsets():
    # Attribute 0x01 starts at record offset 0x5A in the reference layout.
    record = bytearray(0x1200)
    struct.pack_into("<II", record, 74, 0x40, 5)
    struct.pack_into("<II", record, 82, 0x01, 0xFFFFFF00)  # unreadable length
    struct.pack_into("<HI", record, 0x5A, 1, 0x4242)
    struct.pack_into("<I", record, 0x5A + 0x1012, 20_000)
    struct.pack_into("<HH", record, 0x5A + 0x109C, 2, 7)
    struct.pack_into("<I", record, 0x5A + 0x10B0, 300_000)
    record[0x5A + 0x10D4] = 1
    with pytest.raises(Exception):
        RxGeneric.from_bytes(bytes(record))
    database, cursor = comps_database()
    try:
        insert_comp(cursor, 100, "FastTask", bytes(record))
        task = TaskBuilder(cursor, 100).build({0x4242: "MainProgram"})
        assert task is not None
        assert (task.type, task.rate, task.priority, task.watchdog) == ("PERIODIC", "20", "7", "300")
        assert task.disable_update_outputs == "true"
        assert [p.name for p in task.scheduled_programs] == ["MainProgram"]
    finally:
        database.close()


def test_unparseable_small_task_is_left_out():
    database, cursor = comps_database()
    try:
        insert_comp(cursor, 100, "BrokenTask", b"\x01\x02\x03")
        notes = ConversionNotes()
        assert TaskBuilder(cursor, 100, _notes=notes).build({}) is None
        assert "Task 'BrokenTask'" in notes.current()[0]
    finally:
        database.close()


def test_rx_reads_final_attribute():
    attributes = struct.pack("<II", 0x65, 4) + b"\x01\x02\x03\x04" + struct.pack("<II", 0x01, 3) + b"abc"
    raw = rx_record(0x68, 0, attributes, 2, length_extra=8)
    parsed = RxGeneric.from_bytes(raw)
    assert [(item.attribute_id, bytes(item.value)) for item in parsed.extended_records] == [
        (0x65, b"\x01\x02\x03\x04"),
        (0x01, b"abc"),
    ]


def test_unresolved_tag_type_comes_from_taginfo_own_scope_first():
    database, cursor = comps_database()
    try:
        cursor.execute("CREATE TABLE taginfo_tags(program text, name text, data_type text)")
        cursor.executemany("INSERT INTO taginfo_tags VALUES (?, ?, ?)", [
            ("", "Alarm", "ALARM_DIGITAL"), ("", "Shared", "DINT"),
            ("ProgA", "Step", "TIMER"), ("ProgB", "Step", "COUNTER"), ("ProgA", "Other", "REAL"),
        ])
        # The controller (0x50, a root comp) owns tag collection 0x51; program
        # ProgB (0x60, under program collection 0x5F) owns tag collection 0x61.
        insert_comp(cursor, 0x50, "Ctrl", b"")
        insert_comp(cursor, 0x51, "RxTagCollection", b"", parent_id=0x50, record_type=0)
        insert_comp(cursor, 0x5F, "RxProgramCollection", b"", parent_id=0x50, record_type=0)
        insert_comp(cursor, 0x60, "ProgB", b"", parent_id=0x5F)
        insert_comp(cursor, 0x61, "RxTagCollection", b"", parent_id=0x60, record_type=0)
        tags = [
            (0x52, 0x51, "Alarm"), (0x53, 0x51, "Step"),
            (0x62, 0x61, "Step"), (0x63, 0x61, "Shared"), (0x64, 0x61, "Other"),
        ]
        for oid, collection, name in tags:
            # Runtime type 0 resolves no type from the tag's own records.
            record = _legacy_rx(object_id=oid, attributes=[(0x01, _metadata(name, 0))])
            insert_comp(cursor, oid, name, record, parent_id=collection, record_type=0)
        notes = ConversionNotes()
        types = {oid: TagBuilder(cursor, oid, _notes=notes).build().data_type for oid, _, _ in tags}
        # Own scope first; otherwise other scopes, if they agree, with a note.
        assert types == {0x52: "ALARM_DIGITAL", 0x53: "", 0x62: "COUNTER", 0x63: "DINT", 0x64: "REAL"}
        assert notes.current() == [
            "Tag 'Step': no data type; TagInfo.XML has no entry in its scope "
            "and same-named tags in other scopes disagree",
            "Tag 'Shared': data type DINT taken from a same-named tag in another scope of TagInfo.XML",
            "Tag 'Other': data type REAL taken from a same-named tag in another scope of TagInfo.XML",
        ]
    finally:
        database.close()
