import sqlite3

import pytest

from acd.l5x.elements import _get_aoi_records, _get_program_records, _required_program_collection


def _comps_cursor():
    connection = sqlite3.connect(":memory:")
    cursor = connection.cursor()
    cursor.execute(
        "CREATE TABLE comps("
        "object_id int, parent_id int, comp_name text, seq_number int, "
        "record_type int, record BLOB NOT NULL)"
    )
    return connection, cursor


def test_program_records_exclude_non_program_metadata():
    connection, cursor = _comps_cursor()
    try:
        cursor.executemany(
            "INSERT INTO comps VALUES (?, ?, ?, ?, ?, ?)",
            [
                (10, 1, "Program", 0, 256, b"program"),
                (20, 1, "Metadata", 1, 512, b"metadata"),
            ],
        )

        assert _get_program_records(cursor, 1) == [("Program", 10, 256, b"program")]
    finally:
        connection.close()


@pytest.mark.parametrize("collection_name", ["RxRoutineCollection", "RxTagCollection"])
def test_required_program_collection_reports_program_context(collection_name):
    connection, cursor = _comps_cursor()
    try:
        with pytest.raises(
            ValueError,
            match=(
                r"program 'Program' \(object_id=10, record_type=256\) "
                rf"is missing required {collection_name}"
            ),
        ):
            _required_program_collection(cursor, "Program", 10, 256, collection_name)
    finally:
        connection.close()


def test_aoi_records_empty_without_aoi_collection():
    connection, cursor = _comps_cursor()
    try:
        cursor.execute(
            "INSERT INTO comps VALUES (?, ?, ?, ?, ?, ?)",
            (2, 1, "RxProgramCollection", 0, 0, b"programs"),
        )

        assert _get_aoi_records(cursor, 1) == []
    finally:
        connection.close()


def test_aoi_records_returns_aois_in_collection():
    connection, cursor = _comps_cursor()
    try:
        cursor.executemany(
            "INSERT INTO comps VALUES (?, ?, ?, ?, ?, ?)",
            [
                (3, 1, "RxUDIDefinitionCollection", 0, 0, b"aois"),
                (30, 3, "MyAOI", 0, 256, b"aoi"),
                (31, 3, "Metadata", 1, 512, b"metadata"),
            ],
        )

        assert _get_aoi_records(cursor, 1) == [("MyAOI", 30, 3, 256)]
    finally:
        connection.close()
