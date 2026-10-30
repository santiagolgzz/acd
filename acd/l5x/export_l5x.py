import argparse
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Cursor
from typing import Dict, List, Tuple, Union

from acd.database.dbextract import DbExtract
from acd.zip.unzip import Unzip
from loguru import logger as log

from acd.l5x.catalog_numbers import load_external_catalog
from acd.l5x.elements import (
    Controller,
    ControllerBuilder,
    ProjectBuilder,
    RSLogix5000Content,
    taginfo_layouts,
)
from acd.record.comments import CommentsRecord
from acd.record.comps import CompsRecord
from acd.record.nameless import NamelessRecord
from acd.record.sbregion import SbRegionRecord


@dataclass
class ExportL5x:
    input_filename: os.PathLike
    # Directory for the extracted files and SQLite database. None (default)
    # creates a unique temp dir per instance, removed when the instance is
    # garbage-collected or the interpreter exits, even after an error. Pass a
    # specific dir (e.g. "build") to keep the files for inspection.
    _temp_dir: Union[str, None] = None
    _controller: Union[Controller, None] = None
    _project: Union[RSLogix5000Content, None] = None
    # Optional richer catalog (CIP identity -> catalog number) used to resolve
    # module CatalogNumbers and the controller ProcessorType. None (the default)
    # means use the built-in CATALOG_NUMBERS only. A merged table (built-in with
    # an external catalog on top, via catalog_numbers.merge_catalog) lets an
    # out-of-table CPU resolve to a real part number so the L5X imports cleanly
    # in Studio 5000 (the A3 finding). See acd/l5x/catalog_numbers.py.
    _catalog_table: Union[Dict[Tuple[int, int, int], str], None] = None

    def __post_init__(self):
        if self._temp_dir is None:
            self._temp_dir = tempfile.mkdtemp(prefix="acd-build-")
            weakref.finalize(self, shutil.rmtree, self._temp_dir, True)
        log.info(
            "Creating temporary directory (if it doesn't exist to store ACD database files - "
            + self._temp_dir
        )
        _DEFAULT_SQL_DATABASE_NAME = "acd.db"
        if os.path.exists(os.path.join(self._temp_dir, _DEFAULT_SQL_DATABASE_NAME)):
            os.remove(os.path.join(self._temp_dir, _DEFAULT_SQL_DATABASE_NAME))
        if not os.path.exists(os.path.join(self._temp_dir)):
            os.makedirs(self._temp_dir)
        log.info("Creating sqllite database to store ACD database records")
        self._db = sqlite3.connect(
            os.path.join(self._temp_dir, _DEFAULT_SQL_DATABASE_NAME)
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=OFF")
        self._cur: Cursor = self._db.cursor()

        log.debug("Create Comps table in sqllite db")
        self._cur.execute(
            "CREATE TABLE comps(object_id int, parent_id int, comp_name text, seq_number int, record_type int, record BLOB NOT NULL)"
        )
        log.debug("Create pointers table in sqllite db")
        self._cur.execute(
            "CREATE TABLE pointers(object_id int, parent_id int, comp_name text, seq_number int, record_type int, record BLOB NOT NULL)"
        )
        log.debug("Create Rungs table in sqllite db")
        self._cur.execute(
            "CREATE TABLE rungs(object_id int, rung text, seq_number int)"
        )
        log.debug("Create Region_map table in sqllite db")
        self._cur.execute(
            "CREATE TABLE region_map(object_id int, parent_id int, unknown int, seq_no int, record BLOB NOT NULL)"
        )
        log.debug("Create Comment_keys table in sqllite db")
        self._cur.execute("CREATE TABLE comment_keys(object_id int, scope_key int, member_key int)")
        log.debug("Create Tag_data table in sqllite db")
        self._cur.execute("CREATE TABLE tag_data(object_id int, data BLOB NOT NULL)")
        log.debug("Create Rung_uids table in sqllite db")
        self._cur.execute(
            "CREATE TABLE rung_uids(object_id int, parent_id int, uid int)"
        )
        log.debug("Create Comments table in sqllite db")
        self._cur.execute(
            "CREATE TABLE comments(seq_number int, sub_record_length int, object_id int, record_string text, record_type int, parent int, tag_reference text, rung_content int, member_ref int)"
        )

        log.debug("Create Nameless table in sqllite db")
        self._cur.execute(
            "CREATE TABLE nameless(object_id int, parent_id int, record BLOB NOT NULL)"
        )

        log.info("Extracting ACD database file")
        unzip = Unzip(self.input_filename)
        unzip.write_files(self._temp_dir)

        # Preserve all embedded files in original order for round-trip writing.
        # Read directly from the ACD archive (pre-decompression) so that
        # compressed files are carried as-is and write-back is byte-identical.
        self._file_order: List[str] = [r.filename for r in unzip.records]
        self._footer_unknown: int = unzip.header._unknown_two
        self._raw_files: Dict[str, bytes] = {}
        with open(self.input_filename, "rb") as acd_fh:
            for record in unzip.records:
                acd_fh.seek(record.file_offset)
                self._raw_files[record.filename] = acd_fh.read(record.file_length)

        log.info("Getting records from ACD Comps file and storing in sqllite database")
        comps_db = DbExtract(os.path.join(self._temp_dir, "Comps.Dat")).read()
        # Deduplicate by object_id. When duplicate object_ids exist (e.g. a routine that
        # appears twice in Comps.Dat with different record_type values), keep the entry
        # with the largest record because the smaller/later entry is typically a truncated
        # or partial record (e.g. record_type=271 vs 259 for routines) that fails to parse
        # correctly with RxGeneric. The full record is always the largest one.
        comps_by_id = {}
        data_by_id = {}
        keys_by_id = {}
        # Projects saved by v20 and earlier use the legacy Comps layout (see
        # CompsRecord.parse); their Comments.Dat uses a legacy layout too.
        legacy_records = sum(
            1
            for record in comps_db.records.record
            if record.identifier == 64250
            and len(bytes(record.record.record_buffer)) >= 0x10
            and struct.unpack_from("<I", bytes(record.record.record_buffer), 0x0C)[0] != 0
        )
        self._legacy_layout = legacy_records * 2 > len(comps_db.records.record)
        # Every Region Map record seen; a file can hold stale copies that fail
        # validation, so populate_region_map tries them all.
        self._region_map_candidates: List[bytes] = []
        for record in comps_db.records.record:
            t = CompsRecord.parse(record)
            if t is not None:
                if t[1] == 0 and t[2] == "Region Map":
                    self._region_map_candidates.append(bytes(t[5]))
                oid = t[0]
                if oid not in comps_by_id or len(t[5]) > len(comps_by_id[oid][5]):
                    comps_by_id[oid] = t
                    data_by_id[oid] = CompsRecord.data_value(record)
                    keys_by_id[oid] = CompsRecord.legacy_comment_keys(record)
        self._cur.executemany("INSERT INTO comps VALUES (?,?,?,?,?,?)", comps_by_id.values())
        self._cur.executemany(
            "INSERT INTO comment_keys VALUES (?,?,?)",
            [(oid, *keys) for oid, keys in keys_by_id.items() if keys is not None],
        )
        self._cur.executemany(
            "INSERT INTO tag_data VALUES (?,?)",
            [(oid, data) for oid, data in data_by_id.items() if data is not None],
        )
        self._db.commit()

        # Build name lookup for SbRegion tag reference resolution (object_id → comp_name).
        # Store on self for use during write-back (patch_sbregion_dat needs id_to_name).
        name_lookup = {oid: t[2] for oid, t in comps_by_id.items()}
        self._id_to_name: Dict[int, str] = name_lookup

        log.info(
            "Getting records from ACD Region Map file and storing in sqllite database"
        )
        self.populate_region_map()
        self.populate_rung_uids()

        log.info(
            "Getting records from ACD SbRegion file and storing in sqllite database"
        )
        sb_region_db = DbExtract(os.path.join(self._temp_dir, "SbRegion.Dat")).read()
        rung_tuples = [t for record in sb_region_db.records.record if (t := SbRegionRecord.parse(record, name_lookup)) is not None]
        self._cur.executemany("INSERT INTO rungs VALUES (?,?,?)", rung_tuples)
        self._db.commit()

        log.info(
            "Getting records from ACD Comments file and storing in sqllite database"
        )
        comments_db = DbExtract(os.path.join(self._temp_dir, "Comments.Dat")).read()
        parse_comment = CommentsRecord.parse_legacy if self._legacy_layout else CommentsRecord.parse
        comment_tuples = [t for record in comments_db.records.record if (t := parse_comment(record)) is not None]
        self._cur.executemany("INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?)", comment_tuples)
        self._db.commit()

        log.info(
            "Getting records from ACD Nameless file and storing in sqllite database"
        )
        nameless_db = DbExtract(os.path.join(self._temp_dir, "Nameless.Dat")).read()
        nameless_tuples = [t for record in nameless_db.records.record if (t := NamelessRecord.parse(record)) is not None]
        self._cur.executemany("INSERT INTO nameless VALUES (?,?,?)", nameless_tuples)
        self._db.commit()

        log.info("Creating indexes for fast object graph queries")
        self._cur.execute("CREATE INDEX idx_comps_object_id ON comps(object_id)")
        self._cur.execute("CREATE INDEX idx_comps_parent_id ON comps(parent_id)")
        self._cur.execute("CREATE INDEX idx_comps_parent_name ON comps(parent_id, comp_name)")
        self._cur.execute("CREATE INDEX idx_rungs_object_id ON rungs(object_id)")
        self._cur.execute("CREATE INDEX idx_region_map_parent_id ON region_map(parent_id)")
        self._cur.execute("CREATE INDEX idx_rung_uids_parent_id ON rung_uids(parent_id)")
        self._cur.execute("CREATE INDEX idx_tag_data_object_id ON tag_data(object_id)")
        self._cur.execute("CREATE INDEX idx_comment_keys_object_id ON comment_keys(object_id)")
        self._cur.execute("CREATE INDEX idx_comments_parent ON comments(parent)")
        self._db.commit()

    def _taginfo_layouts(self):
        path = os.path.join(self._temp_dir, "TagInfo.XML")
        if not os.path.exists(path):
            return {}
        with open(path, "rb") as f:
            raw = f.read()
        encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        text = raw.decode(encoding, errors="replace")
        # ElementTree rejects an encoding declaration on an already-decoded str.
        text = re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip())
        return taginfo_layouts(text)

    @property
    def controller(self):
        if self._controller is None:
            self._controller = ControllerBuilder(
                self._cur, _catalog_table=self._catalog_table,
                _taginfo_layouts=self._taginfo_layouts(),
            ).build()
        return self._controller

    @property
    def project(self):
        if self._project is None:
            self._project = ProjectBuilder(
                Path(os.path.join(self._temp_dir, "QuickInfo.XML"))
            ).build()
            self._project.controller = self.controller
            self._project._raw_files = self._raw_files
            self._project._file_order = self._file_order
            self._project._footer_unknown = self._footer_unknown
            self._project._id_to_name = self._id_to_name
        return self._project

    def populate_region_map(self):
        # Use the largest Region Map record that parses; fall back to the
        # largest one's error if none does.
        candidates = sorted(self._region_map_candidates, key=len, reverse=True)
        if not candidates:
            return
        entries, first_error = None, None
        for record in candidates:
            try:
                entries = self._parse_region_map(record)
                break
            except ValueError as error:
                first_error = first_error or error
        if entries is None:
            raise first_error

        self._cur.executemany(
            "INSERT INTO region_map VALUES (?, ?, ?, ?, ?)",
            entries,
        )
        self._db.commit()

    @staticmethod
    def _parse_region_map(record):
        # Current empty projects use a header-only Region Map without a length
        # field or entries.
        if len(record) == 0x4A:
            return []

        # Legacy records have a 28-byte prefix and count bytes after that
        # prefix. Modern records have a 78-byte prefix and include four bytes
        # preceding the entries in their stored length.
        if (
            len(record) >= 78
            and struct.unpack_from("<I", record, 0x4A)[0]
            == len(record) - 0x4A
        ):
            identifier_offset = 0x4E
            entries_length = struct.unpack_from("<I", record, 0x4A)[0] - 4
            # Current records can carry a 12-byte non-entry trailer.
            allowed_trailer_lengths = (0, 12)
        else:
            legacy_length = (
                struct.unpack_from("<I", record, 0x18)[0]
                if len(record) >= 28
                else 0
            )
            if legacy_length not in (
                len(record) - 0x1C,
                len(record) - 0x18,
            ):
                raise ValueError("Invalid Region Map length")
            identifier_offset = 0x1C
            entries_length = len(record) - identifier_offset
            allowed_trailer_lengths = (0,)

        trailer_length = entries_length % 16
        if trailer_length not in allowed_trailer_lengths:
            raise ValueError("Invalid Region Map entry length")
        record_end = identifier_offset + entries_length - trailer_length
        entries = []
        while identifier_offset + 16 <= record_end:
            (
                parent_id_identifier,
                unknown_identifier,
                seq_identifier,
                object_id_identifier,
            ) = struct.unpack_from(
                "<IIII", record, identifier_offset
            )
            entries.append(
                (
                    object_id_identifier,
                    parent_id_identifier,
                    unknown_identifier,
                    seq_identifier,
                    record[identifier_offset : identifier_offset + 16],
                )
            )
            identifier_offset += 16
        return entries

    def populate_rung_uids(self):
        path = os.path.join(self._temp_dir, "RegnLink.Dat")
        if not os.path.exists(path):
            return
        with open(path, "rb") as f:
            data = f.read()
        self._cur.execute("SELECT object_id, parent_id FROM region_map")
        self._cur.executemany(
            "INSERT INTO rung_uids VALUES (?, ?, ?)",
            self._parse_rung_uids(data, self._cur.fetchall()),
        )
        self._db.commit()

    @staticmethod
    def _parse_rung_uids(data: bytes, rungs):
        # RegnLink.Dat holds one 22-byte link record per rung:
        #   u8[6] flags, u16 rung uid, u16 unknown, u32 routine object_id,
        #   u32 rung object_id, u32 next object_id
        # Rung comments in Comments.Dat are keyed by that uid (the high half of
        # the comment's rung_content), not by rung position. The file's page
        # layout is not decoded, so records are located by the
        # (routine, rung) object_id pair, which the region map already gives.
        # The routine's own head record (flags 00 00 00 ..., uid 0xFFFF) also
        # contains a (routine, first rung) pair, so require the third flag
        # byte to be 0x02 as it is on every rung link record.
        wanted = {struct.pack("<II", parent, rung): (rung, parent) for rung, parent in rungs}
        found = {}
        for i in range(10, len(data) - 7):
            key = data[i : i + 8]
            if key in wanted and data[i - 8] == 0x02 and wanted[key] not in found:
                found[wanted[key]] = struct.unpack_from("<H", data, i - 4)[0]
        return [(rung, parent, uid) for (rung, parent), uid in found.items()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read an ACD file and export the database as an L5X file"
    )
    parser.add_argument(
        "input", metavar="input", type=str, nargs="+", help="The file to be converted"
    )
    parser.add_argument(
        "output",
        metavar="output",
        type=str,
        nargs="+",
        help="Filename of the exported file",
    )
    parser.add_argument(
        "--catalog",
        metavar="JSON",
        type=str,
        default=None,
        help="External catalog JSON path (merges over built-in CATALOG_NUMBERS)",
    )

    args = parser.parse_args()
    # Build the in-memory project and write the L5X XML. (Previously this
    # passed the output path as ExportL5x's SECOND POSITIONAL arg, which landed
    # in the _temp_dir field and wrote nothing -- the CLI built the SQLite DB but
    # never emitted the .L5X. This mirrors ConvertAcdToL5x.extract() in
    # acd.api: import the project, serialise via to_xml(), write the file.)
    export = ExportL5x(args.input[0])
    # Thread external catalog through so out-of-table CPUs resolve to real
    # part numbers (A3 fix). None -> built-in only.
    if args.catalog:
        export._catalog_table = load_external_catalog(args.catalog)
    project = export.project
    raw_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + project.to_xml()
    out_dir = os.path.dirname(args.output[0])
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output[0], "w", encoding="utf-8") as f:
        f.write(raw_xml)
    log.info("Wrote L5X: " + args.output[0])
