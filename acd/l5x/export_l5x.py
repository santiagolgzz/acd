import argparse
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import weakref
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Cursor
from typing import Callable, Dict, List, Tuple, Union

from acd.database.dbextract import DbExtract
from acd.zip.unzip import Unzip
from loguru import logger as log

from acd.l5x.conversion_notes import ConversionNotes
from acd.l5x.catalog_numbers import load_external_catalog
from acd.l5x.elements import (
    Controller,
    ControllerBuilder,
    ProjectBuilder,
    RSLogix5000Content,
    taginfo_layouts,
    taginfo_tags,
)
from acd.l5x.st_decoder import STDecoder
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
        self._notes = ConversionNotes()
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
        self._cur.execute("CREATE TABLE taginfo_tags(program text, name text, data_type text)")
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
        keys_by_id = {}
        # Projects saved by v20 and earlier use the legacy Comps layout (see
        # CompsRecord.parse); their Comments.Dat uses a legacy layout too.
        legacy_records = 0
        # Every Region Map record seen; a file can hold stale copies that fail
        # validation, so populate_region_map tries them all.
        region_map_candidates: List[bytes] = []
        self._pending_region_map_gaps: List[Tuple[int, int, int]] = []
        for record in comps_db.records.record:
            if record.identifier == 64250 and CompsRecord.is_legacy(bytes(record.record.record_buffer)):
                legacy_records += 1
            t = CompsRecord.parse(record)
            if t is not None:
                if t[1] == 0 and t[2] == "Region Map":
                    region_map_candidates.append(bytes(t[5]))
                oid = t[0]
                if oid not in comps_by_id or len(t[5]) > len(comps_by_id[oid][5]):
                    comps_by_id[oid] = t
                    keys_by_id[oid] = CompsRecord.legacy_comment_keys(record)
        self._legacy_layout = legacy_records * 2 > len(comps_db.records.record)
        self._cur.executemany("INSERT INTO comps VALUES (?,?,?,?,?,?)", comps_by_id.values())
        self._cur.executemany(
            "INSERT INTO comment_keys VALUES (?,?,?)",
            [(oid, *keys) for oid, keys in keys_by_id.items() if keys is not None],
        )
        self._db.commit()

        # Build name lookup for SbRegion tag reference resolution (object_id → comp_name).
        # Store on self for use during write-back (patch_sbregion_dat needs id_to_name).
        name_lookup = {oid: t[2] for oid, t in comps_by_id.items()}
        self._id_to_name: Dict[int, str] = name_lookup

        # Rungs are read before the Region Map: its re-alignment fallback
        # matches entries against the known rung object_ids.
        log.info(
            "Getting records from ACD SbRegion file and storing in sqllite database"
        )
        sb_region_db = DbExtract(os.path.join(self._temp_dir, "SbRegion.Dat")).read()
        rung_tuples = [t for record in sb_region_db.records.record if (t := SbRegionRecord.parse(record, name_lookup, self._notes.add)) is not None]
        self._cur.executemany("INSERT INTO rungs VALUES (?,?,?)", rung_tuples)
        self._db.commit()

        log.info(
            "Getting records from ACD Region Map file and storing in sqllite database"
        )
        self.populate_region_map(region_map_candidates)

        # Now that `rungs` (and its on-disk insertion order) and `region_map`
        # both exist, try to recover any Region Map entry that was truncated
        # before its object_id (see populate_region_map/_parse_region_map).
        self._repair_region_map_gaps()
        self.populate_rung_uids()
        taginfo = self._read_taginfo()
        # Type layouts from TagInfo.XML, for decoding tag values.
        self._taginfo_layouts = taginfo_layouts(taginfo) if taginfo is not None else {}
        if taginfo is not None:
            self._cur.executemany("INSERT INTO taginfo_tags VALUES (?,?,?)", taginfo_tags(taginfo))
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

        # Structured Text source lines, keyed by the routine's comps object_id.
        # A decoding failure leaves the table empty; the routines then export
        # with no lines, and the conversion notes say so.
        self._cur.execute("CREATE TABLE st_lines(routine_id int, line_number int, text text)")
        try:
            st_tuples = [
                (comps_id, number, text)
                for _name, comps_id, lines in self.st_decoder().all_routines()
                for number, text in enumerate(lines)
            ]
            self._cur.executemany("INSERT INTO st_lines VALUES (?,?,?)", st_tuples)
            self._db.commit()
        except Exception as exc:
            self._notes.warn(
                "Structured Text routine bodies could not be decoded; "
                "ST routines were exported with no lines",
                str(exc),
            )

        log.info("Creating indexes for fast object graph queries")
        self._cur.execute("CREATE INDEX idx_comps_object_id ON comps(object_id)")
        self._cur.execute("CREATE INDEX idx_comps_parent_id ON comps(parent_id)")
        self._cur.execute("CREATE INDEX idx_comps_parent_name ON comps(parent_id, comp_name)")
        self._cur.execute("CREATE INDEX idx_rungs_object_id ON rungs(object_id)")
        self._cur.execute("CREATE INDEX idx_region_map_parent_id ON region_map(parent_id)")
        self._cur.execute("CREATE INDEX idx_rung_uids_parent_id ON rung_uids(parent_id)")
        self._cur.execute("CREATE INDEX idx_taginfo_tags_name ON taginfo_tags(name)")
        self._cur.execute("CREATE INDEX idx_comment_keys_object_id ON comment_keys(object_id)")
        self._cur.execute("CREATE INDEX idx_comments_parent_member ON comments(parent, member_ref)")
        self._cur.execute("CREATE INDEX idx_st_lines_routine_id ON st_lines(routine_id)")
        self._db.commit()

    def _read_taginfo(self) -> Union[ET.Element, None]:
        """TagInfo.XML's root element, or None when it is missing or unreadable."""
        path = os.path.join(self._temp_dir, "TagInfo.XML")
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            raw = f.read()
        encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        text = raw.decode(encoding, errors="replace")
        # ElementTree rejects an encoding declaration on an already-decoded str.
        try:
            return ET.fromstring(re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip()))
        except ET.ParseError:
            return None

    @property
    def controller(self):
        if self._controller is None:
            self._controller = ControllerBuilder(
                self._cur, _catalog_table=self._catalog_table,
                _taginfo_layouts=self._taginfo_layouts, _notes=self._notes,
            ).build()
        return self._controller

    def st_decoder(self) -> "STDecoder":
        """Return an STDecoder for this ACD file.

        Must be called **before** accessing ``.project`` (which closes the
        sqlite connection).  The returned :class:`STDecoder` holds its own
        in-memory copy of all necessary data and remains usable after
        ``.project`` is called.

        Example::

            exp = ExportL5x("project.acd")
            dec = exp.st_decoder()          # create while DB is open
            project = exp.project           # DB closes here; dec still works

            for name, comps_id, lines in dec.all_routines():
                print(f"{name}: {len(lines)} lines")
        """
        from acd.l5x.st_decoder import STDecoder  # local import avoids circular refs
        return STDecoder(self)

    @property
    def project(self):
        if self._project is None:
            self._project = ProjectBuilder(
                Path(os.path.join(self._temp_dir, "QuickInfo.XML"))
            ).build()
            self._project.controller = self.controller
            # Read after the controller is built: its fallbacks are noted then.
            self._project.conversion_notes = self._notes.current()
            self._project._raw_files = self._raw_files
            self._project._file_order = self._file_order
            self._project._footer_unknown = self._footer_unknown
            self._project._id_to_name = self._id_to_name
            # Nothing queries the database after this; an open connection
            # would lock acd.db on Windows and keep the temp dir from being removed.
            self._db.close()
        return self._project

    def populate_region_map(self, region_map_candidates: List[bytes]):
        # A file can hold several Region Map records (stale copies among
        # them). Use the largest one whose stored length checks out; only if
        # none does, fall back to re-aligning the largest one against the
        # known rung object_ids (see _parse_region_map).
        candidates = sorted(region_map_candidates, key=len, reverse=True)

        # Entries with a resolved object_id are inserted immediately. A
        # trailing 12-byte (parent_id, unknown, seq) triple with no object_id
        # (see _parse_region_map) is stashed for _repair_region_map_gaps(),
        # which can only run once the `rungs` table exists.
        self._pending_region_map_gaps = []

        if not candidates:
            return

        self._cur.execute("SELECT object_id FROM rungs")
        known_rung_ids = {row[0] for row in self._cur.fetchall()}

        parsed = None
        for record in candidates:
            try:
                parsed = self._parse_region_map(record, strict=True)
                break
            except ValueError:
                continue
        if parsed is None:
            parsed = self._parse_region_map(candidates[0], known_rung_ids, warn=self._notes.warn)

        complete_entries = []
        for object_id, parent_id, unknown, seq, raw in parsed:
            if object_id is None:
                self._pending_region_map_gaps.append((parent_id, unknown, seq))
            else:
                complete_entries.append((object_id, parent_id, unknown, seq, raw))

        self._cur.executemany(
            "INSERT INTO region_map VALUES (?, ?, ?, ?, ?)",
            complete_entries,
        )
        self._db.commit()

    def _repair_region_map_gaps(self):
        """Resolve region_map entries whose object_id was truncated from the
        on-disk Region Map record.

        Some ACD files store a final region_map entry as a short 12-byte
        (parent_id, unknown, seq) triple instead of the usual 16 bytes,
        omitting the object_id entirely (observed for a routine's last rung
        after certain edits in Studio 5000; reproduced on a real project
        file). Without recovering it, that one rung silently vanishes from
        the exported routine -- with no error, which is the worst failure
        mode for a ladder-logic export.

        The `rungs` table preserves rungs in their original on-disk order
        (SbRegion.Dat is read and inserted sequentially, and SQLite's
        implicit `rowid` tracks that insertion order). For the routine
        (parent_id) with the gap, every *other* rung of that routine already
        has a resolved object_id and a known seq. If the gap's seq falls
        between two already-resolved neighbours, the missing rung must be
        whichever one sits between those neighbours in on-disk order -- so
        if exactly one unclaimed rung qualifies, it's the answer.
        """
        if not self._pending_region_map_gaps:
            return

        # object_id -> rowid, for every rung, in on-disk insertion order.
        self._cur.execute("SELECT rowid, object_id FROM rungs ORDER BY rowid")
        rung_rowid_by_object_id = {object_id: rowid for rowid, object_id in self._cur.fetchall()}

        # object_ids already claimed by *any* routine's region_map, so a
        # candidate can't be assigned to two places.
        self._cur.execute("SELECT object_id FROM region_map")
        claimed = {row[0] for row in self._cur.fetchall()}

        resolved = []
        unresolved = []
        for parent_id, unknown, seq in self._pending_region_map_gaps:
            self._cur.execute(
                "SELECT object_id, seq_no FROM region_map WHERE parent_id=? ORDER BY seq_no",
                (parent_id,),
            )
            siblings = self._cur.fetchall()
            prev_obj = next((oid for oid, s in siblings if s == seq - 1), None)
            next_obj = next((oid for oid, s in siblings if s == seq + 1), None)
            prev_row = rung_rowid_by_object_id.get(prev_obj)
            next_row = rung_rowid_by_object_id.get(next_obj)

            candidates = [
                object_id
                for object_id, rowid in rung_rowid_by_object_id.items()
                if object_id not in claimed
                and (prev_row is None or rowid > prev_row)
                and (next_row is None or rowid < next_row)
            ]

            if len(candidates) == 1:
                object_id = candidates[0]
                claimed.add(object_id)
                resolved.append((object_id, parent_id, unknown, seq, b""))
                log.info(
                    f"Recovered a truncated Region Map entry: routine {parent_id}, "
                    f"seq {seq} -> rung object_id {object_id} (resolved by on-disk order)"
                )
            else:
                unresolved.append((parent_id, unknown, seq, len(candidates)))

        if resolved:
            self._cur.executemany("INSERT INTO region_map VALUES (?, ?, ?, ?, ?)", resolved)
            self._db.commit()
        for parent_id, unknown, seq, n_candidates in unresolved:
            routine = self._id_to_name.get(parent_id)
            routine_label = f"'{routine}'" if routine else str(parent_id)
            if n_candidates == 0:
                # No unclaimed rung fits this gap, so no rung is known to be missing.
                self._notes.warn(
                    f"Routine {routine_label}: a truncated Region Map entry (seq {seq}) "
                    "could not be matched to any unclaimed rung, so no rung is known "
                    "to be missing"
                )
            else:
                self._notes.warn(
                    f"Routine {routine_label}: a truncated Region Map entry (seq {seq}) "
                    f"could be any of {n_candidates} rungs -- that rung will be missing "
                    "from the export"
                )

    @staticmethod
    def _find_region_map_alignment(record, known_rung_ids):
        """Find where the 16-byte Region Map entries start when no length
        field matches the record's size.

        Tries every byte offset as the start of (parent_id, unknown, seq,
        object_id) entries and scores it by how many object_ids are known
        rungs. Returns the best offset, or None if none scores well enough
        to trust.
        """
        if not known_rung_ids:
            return None
        best_offset = None
        best_ratio = 0.0
        best_hits = 0
        search_limit = min(len(record) - 16, 512)
        for offset in range(0, max(search_limit, 0)):
            hits = 0
            total = 0
            pos = offset
            while pos + 16 <= len(record):
                object_id = struct.unpack_from("<I", record, pos + 12)[0]
                total += 1
                if object_id in known_rung_ids:
                    hits += 1
                pos += 16
            if total < 3:
                continue
            ratio = hits / total
            if ratio > best_ratio or (ratio == best_ratio and hits > best_hits):
                best_offset, best_ratio, best_hits = offset, ratio, hits
        # Require strong, unambiguous support before trusting it: a real
        # alignment matches the overwhelming majority of entries (the file
        # this was reverse-engineered against hit 98%), not a coincidence.
        if best_offset is not None and best_ratio >= 0.8 and best_hits >= 3:
            return best_offset
        return None

    @staticmethod
    def _parse_region_map(
        record, known_rung_ids=None, strict=False, warn: Callable[[str], None] = log.warning
    ):
        # strict: raise on a stored length that doesn't match the record
        # instead of re-aligning (used to pick between Region Map copies).
        # warn: receives a note when the entries had to be re-aligned.
        # Current empty projects use a header-only Region Map without a length
        # field or entries.
        if len(record) == 0x4A:
            return []

        # Legacy records have a 28-byte prefix and count bytes after that
        # prefix. Modern records have a 78-byte prefix and include four bytes
        # preceding the entries in their stored length.
        if (
            len(record) >= 78
            # CompsRecord.parse keeps the four bytes past the stored length.
            and struct.unpack_from("<I", record, 0x4A)[0]
            in (len(record) - 0x4A, len(record) - 0x4E)
        ):
            identifier_offset = 0x4E
            # Everything after the 78-byte prefix is entries: the stored
            # length leaves out the final four bytes that CompsRecord.parse
            # keeps, which hold the last entry's object id.
            entries_length = len(record) - 0x4E
            # Current records can carry a 12-byte non-entry trailer.
            allowed_trailer_lengths = (0, 12)
        else:
            legacy_length = (
                struct.unpack_from("<I", record, 0x18)[0]
                if len(record) >= 28
                else 0
            )
            if legacy_length in (len(record) - 0x1C, len(record) - 0x18):
                identifier_offset = 0x1C
                entries_length = len(record) - identifier_offset
                allowed_trailer_lengths = (0,)
            elif len(record) >= 78 and not strict:
                # No length field matches the record's size: re-align against
                # known rungs, falling back to the usual 0x4E offset.
                found_offset = ExportL5x._find_region_map_alignment(record, known_rung_ids)
                if found_offset is not None:
                    identifier_offset = found_offset
                    warn(
                        "Region Map: its stored length doesn't match its size "
                        f"({len(record)} bytes); entries were re-aligned to offset "
                        f"{found_offset} by matching known rungs"
                    )
                else:
                    identifier_offset = 0x4E
                    warn(
                        "Region Map: its stored length doesn't match its size "
                        f"({len(record)} bytes) and entries could not be matched to "
                        "known rungs; assumed the usual offset 0x4E, which may be wrong"
                    )
                entries_length = len(record) - identifier_offset
                allowed_trailer_lengths = range(16)
            else:
                raise ValueError("Invalid Region Map length")

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

        # A 12-byte trailer is normally an inert footer. But some records
        # (observed on a real project after certain edits) instead store a
        # final entry truncated to 12 bytes: (parent_id, unknown, seq) with
        # the object_id field simply missing. Distinguish the two cases by
        # checking whether the trailer's first field matches a parent_id we
        # already saw as a real entry above -- an inert footer has no reason
        # to coincidentally embed a real routine's object_id there. When it
        # does, surface it as an entry with object_id=None so the caller can
        # attempt to recover the missing id from other evidence (see
        # ExportL5x._repair_region_map_gaps) instead of the rung silently
        # disappearing from the export.
        if trailer_length == 12 and record_end + 12 <= len(record):
            parent_id_identifier, unknown_identifier, seq_identifier = struct.unpack_from(
                "<III", record, record_end
            )
            known_parent_ids = {e[1] for e in entries}
            if parent_id_identifier in known_parent_ids:
                entries.append(
                    (
                        None,
                        parent_id_identifier,
                        unknown_identifier,
                        seq_identifier,
                        record[record_end : record_end + 12],
                    )
                )

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
        j = data.find(b"\x02", 2)
        while j != -1 and j + 16 <= len(data):
            key = data[j + 8 : j + 16]
            if key in wanted and wanted[key] not in found:
                found[wanted[key]] = struct.unpack_from("<H", data, j + 4)[0]
            j = data.find(b"\x02", j + 1)
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
    # Build the project and write the L5X, as ConvertAcdToL5x.extract() does.
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
