import html
import math
import os
import re
import shutil
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from os import PathLike
from pathlib import Path
from sqlite3 import Cursor
from typing import Any, List, NamedTuple, Tuple, Dict, Optional, Union

from loguru import logger as log

from acd.l5x.conversion_notes import XML_ILLEGAL_RE, ConversionNotes, xml_comment
from acd.l5x.catalog_numbers import CATALOG_NUMBERS, catalog_number_for_identity
from acd.l5x.port_structures import PORT_STRUCTURES
from acd.record.comps import comment_keys
from acd.record.rx import LegacyRxGeneric, RxGeneric, RxRecord


def _escape_xml_attr(value: object) -> str:
    """Escape a value for use inside a double-quoted XML attribute.

    Beyond ``html.escape``, this strips characters that are illegal in XML 1.0
    and encodes the legal-but-delimiting whitespace (TAB/CR/LF) as numeric
    character references. Some binary records decode to garbage when an offset
    drifts (e.g. an AOI ``Vendor`` field read with ``errors="replace"``); without
    this, those raw control characters and newlines land verbatim in an
    attribute value, producing non-well-formed L5X that breaks downstream XML
    parsers.
    """
    text = XML_ILLEGAL_RE.sub("", str(value))
    text = html.escape(text, quote=True)
    return text.replace("\t", "&#x9;").replace("\r", "&#xD;").replace("\n", "&#xA;")


def _cdata_section(text: str) -> str:
    """Wrap text in a CDATA section, stripping XML-illegal characters.

    A CDATA section cannot contain ``]]>``, so that sequence is split across
    two sections (``]]]]><![CDATA[>``), which is how Rockwell escapes it.
    """
    text = XML_ILLEGAL_RE.sub("", text).replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{text}]]>"


# Characters that are illegal in Windows (and some other) filesystem names.
# comp_name values are Logix tag/channel names and can legally contain
# ":" (e.g. "CHANNEL_DI_TIMESTAMP:O:0"), "/" and "\" (member access), and
# other characters that Windows refuses in a filename or directory name.
_PATH_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _sanitize_path_component(name: str) -> str:
    """Make a Logix comp_name safe to use as a single path component.

    ``DumpCompsRecords`` writes one file (and one sub-directory) per record,
    naming them after ``comp_name``. Those names come straight from the ACD
    binary and may contain characters that are illegal in filesystem paths on
    some platforms -- most commonly ``:`` on Windows (a channel name such as
    ``CHANNEL_DI_TIMESTAMP:O:0``). Without this, ``os.makedirs``/``open`` raise
    OSError [WinError 123] on Windows.

    Illegal characters are replaced with ``_`` (deterministic and lossless for
    round-tripping to the directory layout, since the original name is still
    recorded in output.log). Names that need no change pass through untouched,
    so existing dumps on Linux/macOS are unaffected.
    """
    cleaned = _PATH_ILLEGAL_RE.sub("_", name)
    # Windows also reserves device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9);
    # a trailing space/dot can break open() there. Prefix such names defensively.
    stem = cleaned.strip().split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)[1-9]", stem):
        cleaned = "_" + cleaned
    cleaned = cleaned.rstrip(" .")
    return cleaned or "_"


@dataclass
class L5xElementBuilder:
    _cur: Cursor
    _object_id: int = -1
    # Where fallback notes for this conversion go; pass the caller's along by
    # keyword. Subclass fields come after it, so they all need defaults.
    _notes: ConversionNotes = field(default_factory=ConversionNotes)


# Comment rows that hold user text. A type-1 row's object_id says what it
# holds: 1 is a user comment; other values are internal strings (member
# names, localization metadata) laid out differently, which would otherwise
# be read as a truncated description. Only the older rung-comment layout,
# which numbers rungs in object_id, reads type-1 rows without this filter.
_USER_COMMENT = "(record_type != 1 OR object_id = 1)"


def _get_program_records(cur: Cursor, collection_id: int) -> List[Tuple[str, int, int, bytes]]:
    cur.execute(
        "SELECT comp_name, object_id, record_type, record FROM comps "
        "WHERE parent_id=? AND record_type=256",
        (collection_id,),
    )
    return cur.fetchall()


def _get_aoi_records(cur: Cursor, controller_id: int) -> List[Tuple[str, int, int, int]]:
    cur.execute(
        "SELECT comp_name, object_id, parent_id, record_type FROM comps "
        "WHERE parent_id=? AND comp_name='RxUDIDefinitionCollection'",
        (controller_id,),
    )
    results = cur.fetchall()
    if len(results) > 1:
        raise Exception("Contains more than one AOI collection")
    if not results:
        # Add-On Instructions were introduced in v16, so older projects have no AOI collection.
        return []
    cur.execute(
        "SELECT comp_name, object_id, parent_id, record_type FROM comps "
        "WHERE parent_id=? AND record_type=256",
        (results[0][1],),
    )
    return cur.fetchall()


def _required_program_collection(
    cur: Cursor,
    program_name: str,
    program_id: int,
    record_type: int,
    collection_name: str,
) -> int:
    cur.execute(
        "SELECT object_id FROM comps WHERE parent_id=? AND comp_name=?",
        (program_id, collection_name),
    )
    rows = cur.fetchall()
    context = (
        f"program {program_name!r} (object_id={program_id}, record_type={record_type})"
    )
    if not rows:
        raise ValueError(f"{context} is missing required {collection_name}")
    if len(rows) > 1:
        raise ValueError(f"{context} has multiple {collection_name} children")
    return rows[0][0]


# Maps Python attribute names to L5X XML section wrapper tag names.
# Entries here also control which list attributes are serialized as child sections.
_LIST_SECTION_NAMES = {
    "tags": "Tags",
    "local_tags": "LocalTags",
    "parameters": "Parameters",
    "data_types": "DataTypes",
    "members": "Members",
    "modules": "Modules",
    "programs": "Programs",
    "routines": "Routines",
    "aois": "AddOnInstructionDefinitions",
    "tasks": "Tasks",
    "scheduled_programs": "ScheduledPrograms",
}


@dataclass
class L5xElement:
    _name: str

    def __post_init__(self):
        self._export_name = ""

    def to_xml(self) -> str:
        attribute_list: List[str] = []
        child_list: List[str] = []
        # Attributes named in _ATTRIBUTE_ORDER come first, then the rest in field order.
        order = getattr(self, "_ATTRIBUTE_ORDER", ())
        ordered = [n for n in order if n in self.__dict__]
        ordered += [n for n in self.__dict__ if n not in order]
        for attribute in ordered:
            if attribute[0] != "_":
                attribute_value = self.__getattribute__(attribute)
                if attribute_value is None:
                    continue
                if isinstance(attribute_value, L5xElement):
                    child_list.append(attribute_value.to_xml())
                elif isinstance(attribute_value, list):
                    if attribute in _LIST_SECTION_NAMES:
                        section_name = _LIST_SECTION_NAMES[attribute]
                        new_child_list: List[str] = []
                        for element in attribute_value:
                            if isinstance(element, L5xElement):
                                if getattr(element, "_l5x_exclude", False):
                                    continue
                                new_child_list.append(element.to_xml())
                            else:
                                new_child_list.append(f"<{element}/>")
                        child_list.append(
                            f'<{section_name}>{"".join(new_child_list)}</{section_name}>'
                        )
                else:
                    if attribute == "cls":
                        attribute = "class"
                    if isinstance(attribute_value, bool):
                        attribute_value = str(attribute_value).lower()
                    _overrides = getattr(self, "_xml_attr_overrides", {})
                    xml_attr_name = _overrides.get(attribute, attribute.title().replace("_", ""))
                    attribute_list.append(
                        f'{xml_attr_name}="{_escape_xml_attr(attribute_value)}"'
                    )

        _export_name = (
            getattr(self, "_export_name", "") or self.__class__.__name__.title().replace("_", "")
        )
        return f'<{_export_name} {" ".join(attribute_list)}>{"".join(child_list)}</{_export_name}>'


@dataclass
class Member(L5xElement):
    name: str
    data_type: str
    dimension: int
    radix: str
    hidden: bool
    target: Union[str, None]      # BIT members only; None omits the attribute
    bit_number: Union[int, None]  # BIT members only; None omits the attribute
    external_access: str
    _description: Union[str, None] = field(default=None)
    # Byte offset of the member's data in the structure (for BIT members, of
    # the host field) and bit number within it, from the member record.
    _offset: int = 0
    _bit: int = 0

    def to_xml(self) -> str:
        base = super().to_xml()
        if not self._description:
            return base
        desc_xml = f'<Description>\n<![CDATA[{self._description}]]>\n</Description>'
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + base[idx + 1:]


@dataclass
class DataType(L5xElement):
    name: str
    family: str
    cls: str
    members: List[Member]
    _description: Union[str, None] = field(default=None)
    # Size of the structure in bytes (Rx attribute 0x65); 0 when unknown.
    _size: int = 0

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "DataType"

    @property
    def _l5x_exclude(self) -> bool:
        return self.cls == "ProductDefined" or ":" in self.name

    def to_xml(self) -> str:
        base = super().to_xml()
        if not self._description:
            return base
        desc_xml = f'<Description>\n<![CDATA[{self._description}]]>\n</Description>'
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + base[idx + 1:]


# Maps primitive DataType names to their L5K zero-default value string.
# UDT, STRING, ALARM_DIGITAL, MESSAGE, and array types are intentionally omitted —
# they require complex structured L5K encoding that is not yet implemented.
_PRIMITIVE_L5K_ZERO: Dict[str, str] = {
    "BOOL":  "0",
    "SINT":  "0",
    "INT":   "0",
    "DINT":  "0",
    "LINT":  "0",
    "USINT": "0",
    "UINT":  "0",
    "UDINT": "0",
    "ULINT": "0",
    "REAL":  "0.00000000e+000",
    "LREAL": "0.00000000e+000",
    "DT":    "0",
    "LDT":   "0",
    "TIME":  "0",
    "LTIME": "0",
    "TIME32": "0",
}

# Radix string used in Decorated DataValueMember for each numeric primitive.
# BOOL and BIT use no Radix attribute; REAL/LREAL use "Float"; all integers use "Decimal".
_PRIMITIVE_RADIX: Dict[str, str] = {
    "SINT":  "Decimal",
    "INT":   "Decimal",
    "DINT":  "Decimal",
    "LINT":  "Decimal",
    "USINT": "Decimal",
    "UINT":  "Decimal",
    "UDINT": "Decimal",
    "ULINT": "Decimal",
    "REAL":  "Float",
    "LREAL": "Float",
    "DT":    "Date/Time",
    "LDT":   "Date/Time (ns)",
    "TIME":  "Time (us)",
    "LTIME": "LTime (ns)",
    "TIME32": "Time32 (us)",
}

# Default zero value string for each primitive in Decorated output.
_PRIMITIVE_DECORATED_ZERO: Dict[str, str] = {
    "BOOL":  "0",
    "BIT":   "0",
    "SINT":  "0",
    "INT":   "0",
    "DINT":  "0",
    "LINT":  "0",
    "USINT": "0",
    "UINT":  "0",
    "UDINT": "0",
    "ULINT": "0",
    "REAL":  "0.0",
    "LREAL": "0.0",
    "DT":    "DT#1970-01-01-00:00:00.000_000Z",
    "LDT":   "LDT#1970-01-01-00:00:00.000_000_000Z",
    "TIME":  "T#0us",
    "LTIME": "LT#0ns",
    "TIME32": "T32#0us",
}

# Built-in Logix struct types that are not in the user DataType list.
# Each entry is a list of (member_name, member_data_type) tuples.
# Only non-hidden, visible members are listed (as they appear in Decorated output).
_BUILTIN_STRUCT_MEMBERS: Dict[str, List[Tuple[str, str]]] = {
    "TIMER": [
        ("PRE", "DINT"), ("ACC", "DINT"),
        ("EN", "BOOL"), ("TT", "BOOL"), ("DN", "BOOL"),
    ],
    "COUNTER": [
        ("PRE", "DINT"), ("ACC", "DINT"),
        ("CU", "BOOL"), ("CD", "BOOL"), ("DN", "BOOL"), ("OV", "BOOL"), ("UN", "BOOL"),
    ],
    "CONTROL": [
        ("LEN", "DINT"), ("POS", "DINT"),
        ("EN", "BOOL"), ("EU", "BOOL"), ("DN", "BOOL"), ("EM", "BOOL"),
        ("ER", "BOOL"), ("UL", "BOOL"), ("IN", "BOOL"), ("FD", "BOOL"),
    ],
}

# Stored layout of the built-in structs above: a control word, then two DINTs.
# These tables, not the display list, set the 12-byte element size and the
# value decoding, so they are declared on their own.
# Control-word bit names, from bit 31 down.
_BUILTIN_STRUCT_BITS: Dict[str, List[str]] = {
    "TIMER": ["EN", "TT", "DN"],
    "COUNTER": ["CU", "CD", "DN", "OV", "UN"],
    "CONTROL": ["EN", "EU", "DN", "EM", "ER", "UL", "IN", "FD"],
}
# The two DINTs after the control word, in stored order.
_BUILTIN_STRUCT_WORDS: Dict[str, List[str]] = {
    "TIMER": ["PRE", "ACC"],
    "COUNTER": ["PRE", "ACC"],
    "CONTROL": ["LEN", "POS"],
}

# Types for which we emit no Decorated element at all (they use other formats).
_SKIP_DECORATED: set = {"ALARM_DIGITAL", "MESSAGE", "AXIS_SERVO", "PID_ENHANCED"}


_BOOL_TYPES = ("BOOL", "BIT")


def _datavalue_member_xml(name: str, dt: str, value: str, radix: Union[str, None] = None) -> str:
    radix_attr = f' Radix="{radix}"' if radix is not None else ""
    return f'<DataValueMember Name="{name}" DataType="{dt}"{radix_attr} Value="{value}"/>'


def _structure_xml(type_name: str, inner: str) -> str:
    return f'<Structure DataType="{type_name}">{inner}</Structure>'


def _structure_member_xml(name: str, type_name: str, inner: str) -> str:
    return f'<StructureMember Name="{name}" DataType="{type_name}">{inner}</StructureMember>'


def _data_xml(body: str) -> str:
    return f'<Data Format="Decorated">\n{body}\n</Data>'


def _index_labels(dims: List[int]) -> List[str]:
    """Element index labels such as "[1][0]", in row-major order."""
    labels = [""]
    for d in dims:
        labels = [f"{label}[{i}]" for label in labels for i in range(d)]
    return labels


def _elements_xml(dims: List[int], values: List[Union[str, None]],
                  struct_type: Union[str, None] = None) -> str:
    """One <Element> per index, in row-major order.

    values holds each element's Value or, when struct_type is given, each
    element's structure members XML (wrapped in a <Structure> of that type).
    """
    labels = _index_labels(dims)
    if struct_type is None:
        return "".join(
            f'<Element Index="{label}" Value="{value}"/>' for label, value in zip(labels, values)
        )
    return "".join(
        f'<Element Index="{label}">{_structure_xml(struct_type, value or "")}</Element>'
        for label, value in zip(labels, values)
    )


def _array_xml(type_name: str, dims: List[int], radix: Union[str, None], elements: str,
               name: Union[str, None] = None) -> str:
    """An <Array> of a tag, or an <ArrayMember> of a structure when name is given."""
    dim_str = ",".join(str(d) for d in dims)
    radix_attr = f' Radix="{radix}"' if radix is not None else ""
    if name is None:
        return f'<Array DataType="{type_name}" Dimensions="{dim_str}"{radix_attr}>{elements}</Array>'
    return (
        f'<ArrayMember Name="{name}" DataType="{type_name}" Dimensions="{dim_str}"{radix_attr}>'
        f'{elements}</ArrayMember>'
    )


def _member_decorated_xml(member_name: str, member_dt: str, member_dim: int,
                           data_types_map: Dict[str, "DataType"]) -> str:
    """Return the Decorated XML fragment for a single UDT member.

    member_dt:  the DataType name of the member (already upper-cased by caller)
    member_dim: array dimension (0 = scalar)
    """
    if member_dim > 0:
        return _array_member_xml(member_name, member_dt, member_dim, data_types_map)

    if member_dt in _BOOL_TYPES:
        return _datavalue_member_xml(member_name, "BOOL", "0")

    radix = _PRIMITIVE_RADIX.get(member_dt)
    zero = _PRIMITIVE_DECORATED_ZERO.get(member_dt)
    if radix is not None and zero is not None:
        return _datavalue_member_xml(member_name, member_dt, zero, radix)

    # Struct member (nested UDT, TIMER, COUNTER, etc.)
    inner = _struct_members_xml(member_dt, data_types_map)
    if inner is None:
        return ""  # unknown / skip
    return _structure_member_xml(member_name, member_dt, inner)


def _array_member_xml(member_name: str, member_dt: str, dim: int,
                      data_types_map: Dict[str, "DataType"]) -> str:
    """Generate an <ArrayMember> element for a member that is an array."""
    if member_dt in _BOOL_TYPES:
        return _array_xml("BOOL", [dim], "Decimal", _elements_xml([dim], ["0"] * dim), name=member_name)

    radix = _PRIMITIVE_RADIX.get(member_dt)
    zero = _PRIMITIVE_DECORATED_ZERO.get(member_dt)
    if radix is not None and zero is not None:
        return _array_xml(member_dt, [dim], radix, _elements_xml([dim], [zero] * dim), name=member_name)

    # Array of structs
    inner = _struct_members_xml(member_dt, data_types_map)
    if inner is None:
        return ""
    return _array_xml(member_dt, [dim], None,
                      _elements_xml([dim], [inner] * dim, struct_type=member_dt), name=member_name)


def _struct_members_xml(dt_name: str, data_types_map: Dict[str, "DataType"]) -> Union[str, None]:
    """Return the inner XML for a Structure/StructureMember of the given DataType.

    Returns None if the type is unknown or should be skipped.
    The returned string does NOT include the outer <Structure> wrapper.
    """
    if dt_name in _SKIP_DECORATED:
        return None

    # Handle STRING as a special built-in: LEN (DINT) + DATA (STRING/ASCII)
    if dt_name == "STRING":
        return (
            _datavalue_member_xml("LEN", "DINT", "0", "Decimal")
            + '<DataValueMember Name="DATA" DataType="STRING" Radix="ASCII">\n\n</DataValueMember>'
        )

    # Built-in struct types (TIMER, COUNTER, CONTROL)
    builtin_members = _BUILTIN_STRUCT_MEMBERS.get(dt_name)
    if builtin_members is not None:
        return "".join(
            _datavalue_member_xml(mname, mdt, _PRIMITIVE_DECORATED_ZERO[mdt], _PRIMITIVE_RADIX.get(mdt))
            for mname, mdt in builtin_members
        )

    # User-defined type: look up in data_types_map. Hidden members are skipped.
    dt_obj = data_types_map.get(dt_name)
    if dt_obj is None:
        return None
    return "".join(
        _member_decorated_xml(member.name, member.data_type.upper(), member.dimension, data_types_map)
        for member in dt_obj.members
        if not member.hidden
    )


def _generate_decorated(dt_base: str, dimensions: Union[str, None],
                        data_types_map: Dict[str, "DataType"]) -> str:
    """Generate a complete <Data Format="Decorated"> XML string for a tag.

    dt_base:    the base DataType name (uppercase, array brackets already stripped)
    dimensions: comma-separated dimension string (e.g. "100" or "4,8") or None for scalar
    Returns "" if this type should not have a Decorated element.
    """
    if dt_base in _SKIP_DECORATED:
        return ""

    if dimensions is None:
        # Scalar struct
        inner = _struct_members_xml(dt_base, data_types_map)
        if inner is None:
            return ""
        return _data_xml(_structure_xml(dt_base, inner))

    # Array tag: parse dimensions (up to 3D, comma-separated). Logix displays
    # multi-dimensional elements as [i][j], one flat element per index.
    dim_parts = [int(d) for d in dimensions.split(",") if d.strip().isdigit()]
    if not dim_parts:
        return ""
    total = math.prod(dim_parts)

    radix = _PRIMITIVE_RADIX.get(dt_base)
    zero = _PRIMITIVE_DECORATED_ZERO.get(dt_base)
    if dt_base in _BOOL_TYPES:
        body = _array_xml("BOOL", dim_parts, "Decimal", _elements_xml(dim_parts, ["0"] * total))
    elif radix is not None and zero is not None:
        body = _array_xml(dt_base, dim_parts, radix, _elements_xml(dim_parts, [zero] * total))
    else:
        # Struct array (UDT, TIMER, COUNTER, STRING, ...)
        inner = _struct_members_xml(dt_base, data_types_map)
        if inner is None:
            return ""
        body = _array_xml(dt_base, dim_parts, None,
                          _elements_xml(dim_parts, [inner] * total, struct_type=dt_base))
    return _data_xml(body)


# Stored tag values (a tag data object's attribute 0x66) are the controller's
# little-endian memory image of the tag. Only the layouts below are decoded;
# everything else (AOI instances, types without a known size, and the types in
# _SKIP_DECORATED) keeps the zero defaults above.
_PRIMITIVE_FORMAT: Dict[str, str] = {
    "SINT": "<b", "INT": "<h", "DINT": "<i", "LINT": "<q",
    "USINT": "<B", "UINT": "<H", "UDINT": "<I", "ULINT": "<Q",
    "REAL": "<f", "LREAL": "<d",
    # Date and duration types are signed integer counts: DT and TIME of
    # microseconds, LDT and LTIME of nanoseconds, TIME32 of microseconds.
    "DT": "<q", "LDT": "<q", "TIME": "<q", "LTIME": "<q", "TIME32": "<i",
}

# Decorated spelling of the date and duration types: (prefix, ticks per
# microsecond). DT/LDT count from 1970-01-01 UTC.
_TIME_TYPES: Dict[str, Tuple[str, int]] = {
    "DT": ("DT#", 1), "LDT": ("LDT#", 1000),
    "TIME": ("T#", 1), "LTIME": ("LT#", 1000), "TIME32": ("T32#", 1),
}

# STRING: LEN (DINT) + DATA (SINT[82]) + 2 pad bytes.
_STRING_SIZE = 88
_STRING_DATA_LEN = 82


def _element_size(dt_base: str,
                  data_types_map: Union[Dict[str, "DataType"], None] = None) -> Union[int, None]:
    fmt = _PRIMITIVE_FORMAT.get(dt_base)
    if fmt is not None:
        return struct.calcsize(fmt)
    if dt_base in _BUILTIN_STRUCT_BITS:
        return 12
    if dt_base == "STRING":
        return _STRING_SIZE
    dt_obj = (data_types_map or {}).get(dt_base)
    if dt_obj is not None and dt_obj._size and dt_base not in _SKIP_DECORATED:
        return dt_obj._size
    return None


def _type_name(dt_base: str, data_types_map: Union[Dict[str, "DataType"], None]) -> str:
    """The declared spelling of a type name (map keys are upper-cased)."""
    dt_obj = (data_types_map or {}).get(dt_base)
    return dt_obj.name if dt_obj is not None else dt_base


def _value_inner_xml(dt_base: str, raw: bytes,
                     data_types_map: Dict[str, "DataType"]) -> Union[str, None]:
    """Members XML of one structure value (built-in or user type), or None."""
    if dt_base in _BUILTIN_STRUCT_BITS or dt_base == "STRING":
        return _struct_value_members_xml(dt_base, raw)
    dt_obj = data_types_map.get(dt_base)
    if dt_obj is None or dt_base in _SKIP_DECORATED or not dt_obj._size:
        return None
    parts = []
    for member in dt_obj.members:
        if member.hidden:
            continue
        fragment = _member_value_xml(member, raw, data_types_map)
        if fragment is None:
            return None
        parts.append(fragment)
    return "".join(parts)


def _member_value_xml(member: "Member", raw: bytes,
                      data_types_map: Dict[str, "DataType"]) -> Union[str, None]:
    mdt = member.data_type.upper()
    name, offset, dim = member.name, member._offset, member.dimension

    def bit_value(bit_index: int) -> Union[str, None]:
        byte = offset + bit_index // 8
        return str((raw[byte] >> (bit_index % 8)) & 1) if byte < len(raw) else None

    if mdt in _BOOL_TYPES and dim == 0:
        value = bit_value(member._bit)
        if value is None:
            return None
        return _datavalue_member_xml(name, "BOOL", value)

    if dim > 0 and mdt == "BOOL":
        # BOOL array members are packed 32 to a DINT.
        values = [bit_value(i) for i in range(dim)]
        if None in values:
            return None
        return _array_xml("BOOL", [dim], "Decimal", _elements_xml([dim], values), name=name)

    size = _element_size(mdt, data_types_map)
    if size is None or offset + size * max(dim, 1) > len(raw):
        return None

    def element(i: int) -> Union[str, None]:
        chunk = raw[offset + i * size : offset + (i + 1) * size]
        if mdt in _PRIMITIVE_FORMAT:
            return _format_number(mdt, chunk, l5k=False)
        return _value_inner_xml(mdt, chunk, data_types_map)

    if dim == 0:
        value = element(0)
        if value is None:
            return None
        if mdt in _PRIMITIVE_FORMAT:
            return _datavalue_member_xml(name, mdt, value, _PRIMITIVE_RADIX[mdt])
        return _structure_member_xml(name, _type_name(mdt, data_types_map), value)

    values = [element(i) for i in range(dim)]
    if None in values:
        return None
    if mdt in _PRIMITIVE_FORMAT:
        return _array_xml(mdt, [dim], _PRIMITIVE_RADIX[mdt], _elements_xml([dim], values), name=name)
    type_name = _type_name(mdt, data_types_map)
    return _array_xml(type_name, [dim], None,
                      _elements_xml([dim], values, struct_type=type_name), name=name)


def _format_time(dt_base: str, value: int) -> str:
    """Decorated text of a DT, LDT, TIME, LTIME or TIME32 value."""
    prefix, per_us = _TIME_TYPES[dt_base]
    if dt_base in ("DT", "LDT"):
        seconds, sub = divmod(value, 1_000_000 * per_us)
        try:
            stamp = datetime(1970, 1, 1) + timedelta(seconds=seconds)
        except OverflowError:
            return str(value)
        digits = f"{sub:0{6 if per_us == 1 else 9}d}"
        fraction = "_".join(digits[i : i + 3] for i in range(0, len(digits), 3))
        return f"{prefix}{stamp:%Y-%m-%d-%H:%M:%S}.{fraction}Z"
    # Durations: largest unit first, zero units left out (T#1h_30m).
    sign, rest = ("-", -value) if value < 0 else ("", value)
    units = [("d", 86_400_000_000), ("h", 3_600_000_000), ("m", 60_000_000),
             ("s", 1_000_000), ("ms", 1_000), ("us", 1)]
    if per_us == 1000:
        units = [(u, n * 1000) for u, n in units] + [("ns", 1)]
    parts = []
    for unit, size in units:
        count, rest = divmod(rest, size)
        if count:
            parts.append(f"{count}{unit}")
    return prefix + sign + ("_".join(parts) or "0" + units[-1][0])


def _group_digits(digits: str, size: int) -> str:
    """Split digits into groups of size from the right, joined by '_'."""
    head = len(digits) % size
    groups = ([digits[:head]] if head else []) + [
        digits[i : i + size] for i in range(head, len(digits), size)
    ]
    return "_".join(groups)


def _radix_value(dt_base: str, radix: Union[str, None], raw: bytes) -> str:
    """Decorated text of one atomic value in a tag's display radix.

    Integers follow Logix's spellings: 16#0000_0000, 2#0000_..., 8#00_000_...
    and ASCII as a quoted byte string, most significant byte first. Radixes
    that do not apply to the type fall back to the type's default text.
    """
    if dt_base == "BOOL":
        return str(raw[0] & 1)
    if dt_base in _TIME_TYPES:
        return _format_number(dt_base, raw, l5k=False)
    if dt_base in ("REAL", "LREAL"):
        return _format_number(dt_base, raw, l5k=radix == "Exponential")
    value = struct.unpack(_PRIMITIVE_FORMAT[dt_base], raw)[0]
    bits = len(raw) * 8
    unsigned = value & ((1 << bits) - 1)
    if radix == "Hex":
        return "16#" + _group_digits(f"{unsigned:0{bits // 4}x}", 4)
    if radix == "Binary":
        return "2#" + _group_digits(f"{unsigned:0{bits}b}", 4)
    if radix == "Octal":
        return "8#" + _group_digits(f"{unsigned:0{(bits + 2) // 3}o}", 3)
    if radix == "ASCII":
        return _logix_string(raw[::-1])
    return str(value)


def _format_number(dt_base: str, raw: bytes, l5k: bool) -> str:
    value = struct.unpack(_PRIMITIVE_FORMAT[dt_base], raw)[0]
    if dt_base in _TIME_TYPES:
        return str(value) if l5k else _format_time(dt_base, value)
    if dt_base not in ("REAL", "LREAL"):
        return str(value)
    if math.isnan(value) or math.isinf(value):
        # Logix spells non-finite floats the way the MSVC runtime prints them.
        text = "1.#QNAN" if math.isnan(value) else ("-1.#INF" if value < 0 else "1.#INF")
        return text + ("000e+000" if text.endswith("NAN") else "0000e+000") if l5k else text
    if l5k:
        # L5K writes floats as 0.00000000e+000 (three exponent digits).
        mantissa, exponent = f"{value:.8e}".split("e")
        return f"{mantissa}e{exponent[0]}{int(exponent[1:]):03d}"
    if dt_base == "REAL":
        # Shortest text that reads back as the same 32-bit float.
        for precision in range(1, 10):
            text = f"{value:.{precision}g}"
            try:
                packed = struct.pack("<f", float(text))
            except OverflowError:
                # Rounded up past the largest finite REAL; try more digits.
                continue
            if packed == raw:
                break
    else:
        text = repr(value)
    if text.replace("-", "").isdigit():
        text += ".0"
    return text


def _logix_string(raw: bytes) -> str:
    """Quote bytes as a Logix ASCII string literal ($ escapes, single quotes)."""
    out = []
    for b in raw:
        ch = chr(b)
        if ch == "$":
            out.append("$$")
        elif ch == "'":
            out.append("$'")
        elif 0x20 <= b < 0x7F:
            out.append(ch)
        else:
            out.append(f"${b:02X}")
    return "'" + "".join(out) + "'"


def _struct_value_members_xml(dt_base: str, raw: bytes) -> Union[str, None]:
    if dt_base == "STRING":
        length = struct.unpack_from("<i", raw, 0)[0]
        if not 0 <= length <= _STRING_DATA_LEN:
            return None
        text = _logix_string(raw[4 : 4 + length])
        return (
            _datavalue_member_xml("LEN", "DINT", str(length), "Decimal")
            + f'<DataValueMember Name="DATA" DataType="STRING" Radix="ASCII">\n'
            + f'{_cdata_section(text)}\n</DataValueMember>'
        )
    control, first, second = struct.unpack_from("<Iii", raw, 0)
    names = _BUILTIN_STRUCT_WORDS[dt_base]
    parts = [
        _datavalue_member_xml(names[0], "DINT", str(first), "Decimal"),
        _datavalue_member_xml(names[1], "DINT", str(second), "Decimal"),
    ]
    for bit, name in enumerate(_BUILTIN_STRUCT_BITS[dt_base]):
        parts.append(_datavalue_member_xml(name, "BOOL", str((control >> (31 - bit)) & 1)))
    return "".join(parts)


def _l5k_from_data(dt_base: str, data: bytes) -> Union[str, None]:
    """L5K text for a scalar numeric or BOOL tag's stored value, or None."""
    if dt_base == "BOOL":
        # Scalar BOOLs store one byte, 0 or 1.
        return str(data[0]) if len(data) == 1 and data[0] in (0, 1) else None
    if dt_base not in _PRIMITIVE_FORMAT:
        return None
    size = struct.calcsize(_PRIMITIVE_FORMAT[dt_base])
    if not size <= len(data) < size + 8:
        return None
    return _format_number(dt_base, data[:size], l5k=True)


def _decorated_from_data(dt_base: str, dimensions: Union[str, None], data: bytes,
                         data_types_map: Union[Dict[str, "DataType"], None] = None) -> Union[str, None]:
    """<Data Format="Decorated"> holding a tag's stored values, or None.

    None means the type is not decoded or the data size does not match the
    tag's type and dimensions; the caller then falls back to zero defaults.
    """
    dim_parts = (
        [int(d) for d in dimensions.split(",") if d.strip().isdigit()] if dimensions else []
    )
    if dimensions and not dim_parts:
        return None
    total = math.prod(dim_parts)

    if dt_base == "BOOL":
        # BOOL arrays are packed 32 to a DINT.
        if not dim_parts or len(data) < ((total + 31) // 32) * 4:
            return None
        size = 0
    else:
        size = _element_size(dt_base, data_types_map)
        if size is None or not size * total <= len(data) < size * total + 8:
            return None

    if not dim_parts:
        inner = _value_inner_xml(dt_base, data[:size], data_types_map or {})
        if inner is None:
            return None
        return _data_xml(_structure_xml(_type_name(dt_base, data_types_map), inner))

    if dt_base == "BOOL":
        values: List[Union[str, None]] = [str((data[i // 8] >> (i % 8)) & 1) for i in range(total)]
        return _data_xml(_array_xml("BOOL", dim_parts, "Decimal", _elements_xml(dim_parts, values)))
    if dt_base in _PRIMITIVE_FORMAT:
        values: List[Union[str, None]] = [
            _format_number(dt_base, data[i * size : (i + 1) * size], l5k=False) for i in range(total)
        ]
        return _data_xml(_array_xml(
            dt_base, dim_parts, _PRIMITIVE_RADIX[dt_base], _elements_xml(dim_parts, values)
        ))
    inners = [
        _value_inner_xml(dt_base, data[i * size : (i + 1) * size], data_types_map or {})
        for i in range(total)
    ]
    if None in inners:
        return None
    type_name = _type_name(dt_base, data_types_map)
    return _data_xml(_array_xml(
        type_name, dim_parts, None, _elements_xml(dim_parts, inners, struct_type=type_name)
    ))


def taginfo_tags(root: ET.Element) -> List[Tuple[str, str, str]]:
    """(program, tag name, data type) for every tag listed in TagInfo.XML.

    Program is "" for controller-scope tags. Used only as a fallback when a
    tag's own records don't resolve its data type.
    """
    rows: List[Tuple[str, str, str]] = []
    for tags in root.findall("Tags"):
        rows.extend(("", t.get("Name", ""), t.get("DataType", "")) for t in tags.findall("Tag"))
    for program in root.iter("Program"):
        tags = program.find("Tags")
        for t in tags.findall("Tag") if tags is not None else []:
            rows.append((program.get("Name", ""), t.get("Name", ""), t.get("DataType", "")))
    return [row for row in rows if row[1] and row[2]]


def taginfo_layouts(root: ET.Element) -> Dict[str, "DataType"]:
    """Type layouts (size, member offsets and bits) from a project's TagInfo.XML.

    TagInfo.XML lists every type the project uses, including Add-On
    Instructions and module-defined types, with byte offsets. Only the
    layout is kept; it is used to decode stored tag values, not exported.
    """
    layouts: Dict[str, DataType] = {}
    for dt in root.iter("DataType"):
        members = []
        for m in dt.findall("Members/Member"):
            dims = [int(d.get("Size", "0")) for d in m.findall("Dimensions/Dim")]
            dim = 1
            for d in dims:
                dim *= d
            bit = m.get("Bit")
            members.append(Member(
                m.get("Name"), m.get("Name"), m.get("DataType", ""), dim if dims else 0,
                "Decimal", m.get("Hidden") == "true", None,
                int(bit) if bit is not None else None, "Read/Write",
                _offset=int(m.get("Offset", "0")), _bit=int(bit) if bit is not None else 0,
            ))
        name = dt.get("Name", "")
        layouts[name.upper()] = DataType(
            name, name, "NoFamily", "User", members, _size=int(dt.get("Size", "0"))
        )
    return layouts


def merge_value_types(data_types_map: Dict[str, "DataType"],
                      taginfo: Dict[str, "DataType"]) -> Dict[str, "DataType"]:
    """Types for value decoding: the Comps-derived type where it has a size
    and members, otherwise the TagInfo.XML layout."""
    merged = dict(taginfo)
    for key, dt in data_types_map.items():
        if dt._size and dt.members:
            merged[key] = dt
    return merged


@dataclass
class ProduceInfo(L5xElement):
    produce_count: str
    programmatically_send_event_trigger: str
    unicast_permitted: str
    minimum_rpi: str
    maximum_rpi: str
    default_rpi: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "ProduceInfo"
        self._xml_attr_overrides = {
            "minimum_rpi": "MinimumRPI",
            "maximum_rpi": "MaximumRPI",
            "default_rpi": "DefaultRPI",
        }


@dataclass
class ConsumeInfo(L5xElement):
    producer: str
    remote_tag: str
    remote_instance: str
    rpi: str
    unicast: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "ConsumeInfo"
        self._xml_attr_overrides = {"rpi": "RPI"}


@dataclass
class Tag(L5xElement):
    name: str
    tag_type: str
    data_type: Union[str, None]
    radix: Union[str, None]
    external_access: str
    constant: Union[str, None]  # "true" for constants; None omits the attribute
    dimensions: Union[str, None]
    _data_table_instance: int
    _comments: List[Tuple[str, str]]
    _data_types_map: Dict[str, "DataType"] = field(default_factory=dict)
    alias_for: Union[str, None] = None
    produce_info: Union[ProduceInfo, None] = None
    consume_info: Union[ConsumeInfo, None] = None
    # Raw stored value from the tag's data object; None when not found.
    _data: Union[bytes, None] = None
    # Type layouts used to decode _data (see value_types_map); falls back to
    # _data_types_map when empty.
    _value_types_map: Dict[str, "DataType"] = field(default_factory=dict)
    # Program parameter usage (Input, Output, InOut, Public); None for others.
    usage: Union[str, None] = None

    # Attribute order of Rockwell exports; other fields follow in field order.
    _ATTRIBUTE_ORDER = (
        "name", "tag_type", "data_type", "dimensions", "radix", "alias_for",
        "usage", "constant", "external_access",
    )

    def __post_init__(self):
        super().__post_init__()
        # Structure, block and module types carry no radix (raw value 0).
        if self.radix == "NullType":
            self.radix = None
        # Rockwell writes Constant on every Base tag.
        if self.tag_type == "Base" and self.constant is None:
            self.constant = "false"

    @property
    def _l5x_exclude(self) -> bool:
        """Exclude tags with empty or non-identifier names (hex-address placeholders, etc.)."""
        return (
            not self.name
            or not (self.name[0].isalpha() or self.name[0] == "_")
            or ":" in self.name
            or self.name.startswith("__l0")
            or self.name.startswith("__CLONE")
            or self.name.startswith("__SL")
        )

    @staticmethod
    def _sanitize_xml_text(text: str) -> str:
        """Encode characters illegal in XML 1.0 as XML character references (&#xNN;).

        XML 1.0 allows only #x9, #xA, #xD, and #x20–#xD7FF, #xE000–#xFFFD.
        Control characters outside that range (e.g. #x02 STX) are emitted as
        character references rather than stripped, matching Logix Designer output.
        """
        parts = []
        for ch in text:
            cp = ord(ch)
            if ch in ("\t", "\n", "\r") or (0x20 <= cp <= 0xD7FF) or (0xE000 <= cp <= 0xFFFD):
                parts.append(ch)
            else:
                parts.append(f"&#x{cp:04X};")
        return "".join(parts)

    def _data_value_xml(self, dt_base: str, stored: Union[str, None]) -> str:
        """<Data Format="Decorated"> with the scalar atomic value in the tag's radix.

        stored is the L5K text of the stored value, or None when it does not decode;
        zero is used then (the L5K default).
        """
        size = 1 if dt_base == "BOOL" else struct.calcsize(_PRIMITIVE_FORMAT[dt_base])
        data = self._data or b""
        raw = data[:size] if stored is not None else bytes(size)
        radix = self.radix or _PRIMITIVE_RADIX.get(dt_base, "Decimal")
        value = _radix_value(dt_base, radix, raw)
        type_name = self.data_type.split("[")[0] if self.data_type else dt_base
        return (
            f'<Data Format="Decorated">\n<DataValue DataType="{_escape_xml_attr(type_name)}" '
            f'Radix="{_escape_xml_attr(radix)}" Value="{_escape_xml_attr(value)}"/>\n</Data>'
        )

    def to_xml(self) -> str:
        base = super().to_xml()

        # --- Description child element ---
        # Find tag-level description: empty tag_reference means the tag itself.
        # Tags with multiple empty-ref entries (e.g. array-element bit descriptions
        # alongside the tag description) store the real tag description as the
        # longest entry — short entries like "Spare" or "End CIP" are element labels.
        candidates = [text for ref, text in self._comments if ref in ("", ".") and text]
        desc_raw = max(candidates, key=len) if candidates else None
        desc = self._sanitize_xml_text(desc_raw) if desc_raw else None
        desc_xml = f'<Description>\n{_cdata_section(desc)}\n</Description>' if desc else ""

        # --- Comments child element: per-bit / per-member descriptions ---
        # Stored tag_references omit the leading "." in current files (".2"
        # is stored as "2"); references starting with "!" are not operands.
        operand_comments: Dict[str, str] = {}
        for ref, text in self._comments:
            if not ref or ref == "." or ref.startswith("!") or not text:
                continue
            operand = ref if ref[0] in ".[" else "." + ref
            operand_comments.setdefault(operand.upper(), text)
        if operand_comments:
            desc_xml += "<Comments>" + "".join(
                f'<Comment Operand="{_escape_xml_attr(operand)}">\n'
                f'{_cdata_section(self._sanitize_xml_text(text))}\n</Comment>'
                for operand, text in operand_comments.items()
            ) + "</Comments>"

        # --- Data child element(s) ---
        # Scalar primitives and BOOL get an L5K value: the stored value when it
        # decodes, else zero. Unless the tag is an alias, they also get a Decorated
        # DataValue in the tag's radix. Other tags get Decorated data from the stored
        # bytes when they decode, else zero defaults. STRING and _SKIP_DECORATED types
        # get no zero-default element.
        dt_base = self.data_type.split("[")[0].upper() if self.data_type else ""
        l5k_zero = _PRIMITIVE_L5K_ZERO.get(dt_base) if not self.dimensions else None
        stored = None
        if l5k_zero is not None and self._data and not self.alias_for:
            stored = _l5k_from_data(dt_base, self._data)
            l5k_zero = stored or l5k_zero
        data_xml = f'<Data Format="L5K">\n{l5k_zero}\n</Data>' if l5k_zero is not None else ""
        if l5k_zero is not None and not self.alias_for:
            data_xml += self._data_value_xml(dt_base, stored)

        if not data_xml and self._data and not self.alias_for:
            data_xml = (
                _decorated_from_data(
                    dt_base, self.dimensions, self._data, self._value_types_map or self._data_types_map
                ) or ""
            )

        if not data_xml and dt_base not in _SKIP_DECORATED and dt_base != "STRING":
            # Generate Decorated data for non-primitive / array types
            decorated = _generate_decorated(dt_base, self.dimensions, self._data_types_map)
            if decorated:
                data_xml = decorated

        if not desc_xml and not data_xml:
            return base

        # Insert Description (if any) then Data (if any) immediately after the opening tag.
        opening_end = base.index(">") + 1
        if desc_xml:
            base = base[:opening_end] + desc_xml + base[opening_end:]

        insert_at = opening_end + len(desc_xml)
        for child_name in ("ProduceInfo", "ConsumeInfo"):
            child_end = base.find(f"</{child_name}>", insert_at)
            if child_end >= 0:
                insert_at = child_end + len(child_name) + 3
        return base[:insert_at] + data_xml + base[insert_at:]


@dataclass
class LocalTag(L5xElement):
    """Represents a local (non-public) tag inside an AOI (<LocalTag> in L5X)."""
    name: str
    data_type: str
    dimensions: Union[str, None]  # array size; None for scalars (omitted from XML)
    radix: Union[str, None]   # None for complex/UDT types (omitted from XML)
    external_access: str
    _description: Union[str, None] = field(default=None)

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "LocalTag"

    @property
    def _l5x_exclude(self) -> bool:
        """Exclude hex-address placeholders, empty names, and ACD-internal runtime tags."""
        return (
            not self.name
            or not (self.name[0].isalpha() or self.name[0] == "_")
            or ":" in self.name
            or self.name.startswith("__l0")
            or self.name.startswith("__CLONE")
        )

    def to_xml(self) -> str:
        base = super().to_xml()
        if not self._description:
            return base
        desc_xml = f'<Description>\n<![CDATA[{self._description}]]>\n</Description>'
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + base[idx + 1:]


@dataclass
class Parameter(L5xElement):
    """Represents a public parameter of an AOI (<Parameter> in L5X)."""
    name: str
    tag_type: str       # always "Base"
    data_type: str
    usage: str          # "Input", "Output", or "InOut"
    radix: Union[str, None]   # None for complex types (omitted from XML)
    required: str       # "true" or "false"
    visible: str        # "true" or "false"
    external_access: Union[str, None]  # None for InOut (omitted, replaced by Constant)
    constant: Union[str, None]  # "false" for non-MESSAGE InOut, None otherwise (omitted)
    dimensions: Union[str, None]  # array size; None for scalars (omitted from XML)
    _description: Union[str, None] = field(default=None)

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "Parameter"

    @property
    def _l5x_exclude(self) -> bool:
        return (
            not self.name
            or not (self.name[0].isalpha() or self.name[0] == "_")
        )

    def to_xml(self) -> str:
        base = super().to_xml()
        if not self._description:
            return base
        desc_xml = f'<Description>\n<![CDATA[{self._description}]]>\n</Description>'
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + base[idx + 1:]


@dataclass
class Module(L5xElement):
    """Represents a Logix hardware module (<Module> in L5X)."""
    name: str
    catalog_number: str
    vendor: int
    product_type: int
    product_code: int
    major: int
    minor: int
    parent_module: str
    parent_mod_port_id: int
    inhibited: str
    major_fault: str
    # Private fields (not serialised as XML attributes)
    _ekey_state: str = field(default="CompatibleModule")
    _slot: int = field(default=0)
    _ip_address: str = field(default="")
    _backplane_slot: Union[int, None] = field(default=None)
    _chassis_size: Union[int, None] = field(default=None)
    _port_child_counts: Dict[int, int] = field(default_factory=dict)
    # Communications / ExtendedProperties / Description (optional)
    _description: str = field(default="")
    _comm_method: Union[str, None] = field(default=None)
    # Each entry: (name, rpi_str, conn_type_str)
    _connections: List[Tuple[str, str, str]] = field(default_factory=list)
    _extended_properties: str = field(default="")

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "Module"

    def to_xml(self) -> str:
        # Hash-named drive peripherals have no Name attribute in Logix-exported L5X.
        name_attr = "" if self.name == "?" else f'Name="{self.name}" '
        attrs = (
            f'{name_attr}'
            f'CatalogNumber="{self.catalog_number}" '
            f'Vendor="{self.vendor}" '
            f'ProductType="{self.product_type}" '
            f'ProductCode="{self.product_code}" '
            f'Major="{self.major}" '
            f'Minor="{self.minor}" '
            f'ParentModule="{self.parent_module}" '
            f'ParentModPortId="{self.parent_mod_port_id}" '
            f'Inhibited="{self.inhibited}" '
            f'MajorFault="{self.major_fault}"'
        )

        # Optional <Description>
        desc_xml = ""
        if self._description:
            desc_xml = f'<Description>\n<![CDATA[{self._description}]]>\n</Description>'

        ekey = f'<EKey State="{self._ekey_state}"/>'
        ports = self._build_ports_xml()

        # <Communications> section — only emitted when a CommMethod is known.
        comm_xml = ""
        if self._comm_method is not None:
            conn_parts: List[str] = []
            for (conn_name, rpi_str, conn_type) in self._connections:
                safe_name = _escape_xml_attr(conn_name)
                # Derive InputTag / OutputTag stubs based on connection type.
                if conn_type == "Output":
                    tag_stubs = (
                        '<OutputTag ExternalAccess="Read/Write">'
                        '<Comments/>'
                        '</OutputTag>'
                    )
                else:
                    # Input or InputOutput: include both stubs.
                    tag_stubs = (
                        '<InputTag ExternalAccess="Read Only">'
                        '<Comments/>'
                        '</InputTag>'
                        '<OutputTag ExternalAccess="Read/Write">'
                        '<Comments/>'
                        '</OutputTag>'
                    )
                conn_parts.append(
                    f'<Connection Name="{safe_name}" RPI="{rpi_str}" Type="{conn_type}"'
                    f' EventID="0" ProgrammaticallySendEventTrigger="false" Unicast="false">'
                    f'{tag_stubs}'
                    f'</Connection>'
                )
            joined = "".join(conn_parts)
            connections_xml = f'<Connections>{joined}</Connections>' if joined else '<Connections/>'
            comm_xml = (
                f'<Communications CommMethod="{self._comm_method}">'
                f'{connections_xml}'
                f'</Communications>'
            )

        # <ExtendedProperties> section — only emitted when public data is known.
        ext_xml = ""
        if self._extended_properties:
            ext_xml = f'<ExtendedProperties><public>{self._extended_properties}</public></ExtendedProperties>'

        return f'<Module {attrs}>{desc_xml}{ekey}{ports}{comm_xml}{ext_xml}</Module>'

    def _build_ports_xml(self) -> str:
        """Build the <Ports>...</Ports> XML section for this module.

        Looks up the port structure from PORT_STRUCTURES by (vendor, product_type,
        product_code). Falls back to <Ports/> if the catalog number is not in the table.
        """
        key = (self.vendor, self.product_type, self.product_code)
        port_defs = PORT_STRUCTURES.get(key)
        if port_defs is None:
            return '<Ports/>'

        is_root = (self.major_fault == "true")
        port_parts: List[str] = []

        for pd in port_defs:
            # --- Upstream direction ---
            # Root modules (self-parenting CPU) have all ports downstream.
            # For other modules: if upstream_fixed=True, use the static upstream_port value.
            # If upstream_fixed=False, determine from parent_mod_port_id (the port on the
            # parent module that this module connects through — when that matches port_id,
            # this port faces upstream).
            if is_root:
                upstream_str = "false"
            elif pd.upstream_fixed:
                upstream_str = "true" if pd.upstream_port else "false"
            else:
                upstream_str = "true" if pd.port_id == self.parent_mod_port_id else "false"

            # --- Address attribute ---
            if pd.address_mode == "omit":
                addr_attr = ""
            elif pd.address_mode == "slot":
                # Non-upstream ICP ports (remote chassis owner): use _backplane_slot if known.
                if upstream_str == "false" and self._backplane_slot is not None:
                    addr_attr = f' Address="{self._backplane_slot}"'
                else:
                    addr_attr = f' Address="{self._slot if self._slot != 0xFFFFFFFF else 0}"'
            elif pd.address_mode == "zero":
                addr_attr = ' Address="0"'
            else:  # "empty" — use IP from binary if present, else omit value
                addr_attr = f' Address="{self._ip_address}"'

            # --- Bus element ---
            # Bus is only emitted on downstream (Upstream="false") ports.
            # upstream ports never carry a Bus element.
            is_upstream = (upstream_str == "true")
            bus_xml = self._bus_xml(pd, is_upstream)

            if bus_xml:
                port_parts.append(
                    f'<Port Id="{pd.port_id}"{addr_attr} Type="{pd.port_type}" Upstream="{upstream_str}">\n'
                    f'{bus_xml}\n'
                    f'</Port>\n'
                )
            else:
                port_parts.append(
                    f'<Port Id="{pd.port_id}"{addr_attr} Type="{pd.port_type}" Upstream="{upstream_str}"/>\n'
                )

        return f'<Ports>\n{"".join(port_parts)}</Ports>\n'

    def _bus_xml(self, pd, is_upstream: bool) -> str:
        """Return the Bus XML string for a port, or '' if no Bus element should be emitted.

        Bus elements are only present on downstream (Upstream=false) ports.
        """
        if is_upstream:
            return ""
        mode = pd.bus_mode
        if mode == "none":
            return ""
        if mode == "always":
            return "<Bus/>"
        if mode.startswith("fixed:"):
            # Use binary chassis size when available (read from RxDataCollection);
            # fall back to the hardcoded port_structures value.
            if self._chassis_size is not None:
                return f'<Bus Size="{self._chassis_size}"/>'
            size = mode.split(":")[1]
            return f'<Bus Size="{size}"/>'
        if mode == "children_or_none":
            child_count = self._port_child_counts.get(pd.port_id, 0)
            if self._chassis_size is not None:
                child_count = max(child_count, self._chassis_size)
            if child_count == 0:
                return ""
            return f'<Bus Size="{child_count}"/>'
        # "children" mode: child count, but never less than _chassis_size when known
        # (handles remote chassis with empty slots not represented as child modules).
        child_count = self._port_child_counts.get(pd.port_id, 0)
        if self._chassis_size is not None:
            child_count = max(child_count, self._chassis_size)
        return f'<Bus Size="{child_count}"/>'


@dataclass
class Routine(L5xElement):
    name: str
    type: str
    rungs: List[str]
    _rung_ids: List[int] = field(default_factory=list)
    _rung_comments: Dict[int, str] = field(default_factory=dict)
    _st_lines: List[str] = field(default_factory=list)
    _description: Union[str, None] = None

    def to_xml(self) -> str:
        rll_content = ""
        st_content = ""
        if self.type == "RLL" and self.rungs:
            rung_xmls = []
            for i, rung_text in enumerate(self.rungs):
                text = (rung_text or "").strip()
                if not text:
                    continue
                comment_xml = ""
                if i in self._rung_comments:
                    comment_text = self._rung_comments[i]
                    comment_xml = f'<Comment><![CDATA[{comment_text}]]></Comment>'
                rung_xmls.append(
                    f'<Rung Number="{i}" Type="N">'
                    f'{comment_xml}'
                    f'<Text><![CDATA[{text}]]></Text>'
                    f'</Rung>'
                )
            if rung_xmls:
                rll_content = f'<RLLContent>{"".join(rung_xmls)}</RLLContent>'
        if self.type == "ST" and self._st_lines:
            # Blank lines are kept: the line number is the position in the source.
            line_xmls = [
                f'<Line Number="{i}">{_cdata_section(text)}</Line>'
                for i, text in enumerate(self._st_lines)
            ]
            st_content = f'<STContent>{"".join(line_xmls)}</STContent>'
        return (
            f'<Routine Name="{_escape_xml_attr(self.name)}" Type="{self.type}">'
            f'{_description_xml(self._description)}{rll_content}{st_content}</Routine>'
        )


@dataclass
class AOI(L5xElement):
    # Attributes and sections set to None are omitted from the XML. A
    # source-protected AOI uses None for everything that is stored encrypted.
    name: str
    revision: Union[str, None]
    revision_extension: Union[str, None]
    vendor: Union[str, None]
    execute_prescan: Union[str, None]
    execute_postscan: Union[str, None]
    execute_enable_in_false: Union[str, None]
    created_date: Union[str, None]
    created_by: Union[str, None]
    edited_date: Union[str, None]
    edited_by: Union[str, None]
    software_revision: Union[str, None]
    parameters: Union[List[Parameter], None]
    local_tags: Union[List[LocalTag], None]
    routines: List[Routine]
    _description: Union[str, None] = field(default=None)
    _revision_note: str = field(default="")

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "AddOnInstructionDefinition"

    def to_xml(self) -> str:
        base = super().to_xml()
        idx = base.index(">")
        inject = ""
        if self._description:
            inject += f'<Description>\n<![CDATA[{self._description}]]>\n</Description>'
        if self._revision_note:
            inject += f'<RevisionNote>\n<![CDATA[{self._revision_note}]]>\n</RevisionNote>'
        return base[:idx + 1] + inject + base[idx + 1:]


@dataclass
class Program(L5xElement):
    name: str
    test_edits: str
    main_routine_name: Union[str, None]  # None if absent (omitted from XML)
    fault_routine_name: Union[str, None]  # None if absent (omitted from XML)
    disabled: str
    synchronize_redundancy_data_after_execution: Union[str, None]  # None → omit attr
    use_as_folder: str
    tags: List[Tag]        # Tags section before Routines (matches L5X export order)
    routines: List[Routine]


@dataclass
class ScheduledProgram(L5xElement):
    name: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "ScheduledProgram"


@dataclass
class EventInfo(L5xElement):
    event_trigger: str
    enable_timeout: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "EventInfo"


@dataclass
class Task(L5xElement):
    name: str
    type: str
    rate: Union[str, None]  # None for CONTINUOUS tasks (omitted from XML)
    priority: str
    watchdog: str
    disable_update_outputs: str
    inhibit_task: str
    event_info: Union[EventInfo, None]  # None for non-EVENT tasks
    scheduled_programs: List[ScheduledProgram]


@dataclass
class Controller(L5xElement):
    use: str
    name: str
    processor_type: Union[str, None]  # None if unknown (omitted from XML)
    major_rev: str
    minor_rev: str
    major_fault_program: Union[str, None]  # None if not set (omitted from XML)
    project_creation_date: str
    last_modified_date: str
    sfc_execution_control: str
    sfc_restart_position: str
    sfc_last_scan: str
    comm_path: Union[str, None]  # None if not set (omitted from XML)
    project_sn: str
    match_project_to_controller: str
    can_use_rpi_from_producer: str
    inhibit_automatic_firmware_update: str
    pass_through_configuration: str
    download_project_documentation_and_extended_properties: str
    download_project_custom_properties: str
    report_minor_overflow: str
    auto_diags_enabled: str
    web_server_enabled: str
    data_types: List[DataType]
    modules: List[Module]
    tags: List[Tag]
    programs: List[Program]
    tasks: List[Task]
    aois: List[AOI]
    # _redundancy_enabled is NOT serialised as a regular XML attribute (underscore prefix
    # skips it in the base to_xml()); it is used only to build the <RedundancyInfo> element.
    _redundancy_enabled: bool = field(default=False)
    _description: Union[str, None] = field(default=None)
    # Controller-scoped tags left out because they could not be read.
    _skipped_tags: List[Dict[str, object]] = field(default_factory=list)

    def __post_init__(self):
        super().__post_init__()
        self._xml_attr_overrides = {
            "sfc_execution_control": "SFCExecutionControl",
            "sfc_restart_position": "SFCRestartPosition",
            "sfc_last_scan": "SFCLastScan",
            "project_sn": "ProjectSN",
            "can_use_rpi_from_producer": "CanUseRPIFromProducer",
        }

    def to_xml(self) -> str:
        # The <Controller> child element ORDER is fixed by the Studio 5000 schema
        # (validated against the known-good reference files in resources/, e.g.
        # ACDTestsWithAOI.L5X). It is:
        #   Description, RedundancyInfo, Security, SafetyInfo, DataTypes, Modules,
        #   AddOnInstructionDefinitions, Tags, Programs, Tasks, CST, WallClockTime,
        #   Trends, DataLogs, TimeSynchronize, EthernetPorts
        # NOTE: AddOnInstructionDefinitions comes right AFTER Modules (not after
        # Tasks as the field-declaration order would suggest), and the structural
        # stubs (RedundancyInfo/Security/SafetyInfo) come BEFORE the data sections.
        # The base to_xml() emits children in dataclass field order, which is the
        # WRONG order for Studio import, so we build the inner content explicitly.
        base = super().to_xml()
        idx = base.index(">")
        open_tag = base[: idx + 1]

        redundancy_enabled_str = "true" if self._redundancy_enabled else "false"
        # Reference files carry a <Description> (empty when there is none) as the first child.
        parts = [
            open_tag,
            _description_xml(self._description) or "<Description></Description>",
            f'<RedundancyInfo Enabled="{redundancy_enabled_str}" KeepTestEditsOnSwitchOver="false"/>',
            '<Security Code="0" ChangesToDetect="16#ffff_ffff_ffff_ffff"/>',
            '<SafetyInfo/>',
            self._section_xml("data_types", "DataTypes"),
            self._section_xml("modules", "Modules"),
            self._section_xml("aois", "AddOnInstructionDefinitions"),  # AFTER Modules
            self._section_xml("tags", "Tags"),
            self._section_xml("programs", "Programs"),
            self._section_xml("tasks", "Tasks"),
            '<CST MasterID="0"/>',
            '<WallClockTime LocalTimeAdjustment="0" TimeZone="0"/>',
            '<Trends/>',
            '<DataLogs/>',
            '<TimeSynchronize Priority1="128" Priority2="128" PTPEnable="true"/>',
            self._ethernet_ports_xml(),
            '</Controller>',
        ]
        return "".join(parts)

    def _ethernet_ports_xml(self) -> str:
        """Build the <EthernetPorts> section.

        A root ControlLogix CPU with an integrated Ethernet port (L83E/L84E/L85E/
        L82E/L86E etc.) requires an <EthernetPort> descriptor. Omitting it (an empty
        <Ports/>) makes Studio 5000 rename+delete the CPU module on import ("Collision
        ... renamed to Local1 / Deleting module"). The known-good reference files
        (e.g. ACDTestsWithAOI.L5X) carry:
            <EthernetPorts><EthernetPort Port="1" Label="1" PortEnabled="true"/></EthernetPorts>
        Emit that descriptor when the root CPU has an Ethernet port; otherwise an
        empty <EthernetPorts/> (a CPU without integrated Ethernet, e.g. L74, has none).
        """
        for mod in self.modules:
            if mod.major_fault != "true":
                continue  # not the root CPU
            # The CPU's integrated Ethernet port is the one with an Ethernet port
            # definition. Detect via the port structure table.
            from acd.l5x.port_structures import PORT_STRUCTURES

            defs = PORT_STRUCTURES.get((mod.vendor, mod.product_type, mod.product_code)) or []
            eth_ports = [d for d in defs if d.port_type == "Ethernet"]
            if not eth_ports:
                return "<EthernetPorts/>"
            # One descriptor per integrated Ethernet port (these CPUs have exactly one).
            # Port="1" is the CPU's own (first) Ethernet port in the EthernetPorts
            # context; Label mirrors it; PortEnabled=true (active by default).
            inner = "".join(
                f'<EthernetPort Port="{n}" Label="{n}" PortEnabled="true"/>'
                for n in range(1, len(eth_ports) + 1)
            )
            return f"<EthernetPorts>{inner}</EthernetPorts>"
        return "<EthernetPorts/>"

    def _section_xml(self, field_name: str, tag_name: str) -> str:
        """Serialize a list-of-L5xElement field as <Tag>...</Tag> (or <Tag/>)."""
        items = getattr(self, field_name, None) or []
        if not items:
            return f"<{tag_name}/>"
        inner = "".join(item.to_xml() for item in items)
        return f"<{tag_name}>{inner}</{tag_name}>"


@dataclass
class RSLogix5000Content(L5xElement):
    """Controller Project"""

    controller: Union[Controller, None]
    schema_revision: str
    software_revision: str
    target_name: str
    target_type: str
    contains_context: str
    export_date: str
    export_options: str
    # Fallbacks taken during the conversion, written as an XML comment at the
    # top of the file (see conversion_notes.py). Empty means no comment.
    conversion_notes: List[str] = field(default_factory=list)

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "RSLogix5000Content"

    def to_xml(self) -> str:
        # The comment comes before the root element, so it lands right after
        # the XML declaration that the caller writes first.
        return xml_comment(self.conversion_notes) + super().to_xml()


def radix_enum(i: int) -> str:
    if i == 0:
        return "NullType"
    if i == 1:
        return "General"
    if i == 2:
        return "Binary"
    if i == 3:
        return "Octal"
    if i == 4:
        return "Decimal"
    if i == 5:
        return "Hex"
    if i == 6:
        return "Exponential"
    if i == 7:
        return "Float"
    if i == 8:
        return "ASCII"
    if i == 9:
        return "Unicode"
    if i == 10:
        return "Date/Time"
    if i == 11:
        return "Date/Time (ns)"
    # 12-14 seen on TIME32, TIME and LTIME tags (ACDTestsWithAOI reference).
    if i == 12:
        return "Time32 (us)"
    if i == 13:
        return "Time (us)"
    if i == 14:
        return "LTime (ns)"
    return "General"


def external_access_enum(i: int) -> str:
    default = "Read/Write"
    if i == 0:
        return default
    if i == 2:
        return "Read Only"
    if i == 3:
        return "None"
    return default


class _MemberFields(NamedTuple):
    radix: int
    type_id: int
    dimension: int
    offset: int
    bit: int
    host: int
    target_key: int
    hidden: int
    access: int


def _member_fields(rec: bytes) -> _MemberFields:
    """The u32 fields of a data type member record, from 0x54: radix, type
    object, dimension, byte offset (0x60), bit, host (0x68), target key
    (0x6C), hidden (0x70) and external access (0x74)."""
    return _MemberFields(*struct.unpack_from("<9I", rec, 0x54))


@dataclass
class MemberBuilder(L5xElementBuilder):
    record: bytes = field(default_factory=bytes)
    # Map from offset-0x60 value to backing member name, used to resolve BIT Target.
    # Built by DataTypeBuilder before iterating children and passed in here.
    _offset60_to_name: Dict[int, str] = field(default_factory=dict)
    # Fallback target name for Pattern-2 BIT members (0x68==0, 0x6c==0xFFFFFFFF).
    # Set by DataTypeBuilder to the most recent preceding hidden SINT/INT in member order.
    _fallback_target: Union[str, None] = field(default=None)

    def build(self) -> Member:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        name = results[0][0]
        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return Member(name, name, "", 0, "Decimal", False, None, None, "Read/Write")

        fields = _member_fields(self.record)
        dimension = fields.dimension
        radix = radix_enum(fields.radix)
        hidden = bool(fields.hidden)
        external_access = external_access_enum(fields.access)

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(fields.type_id)
        )
        data_type_results = self._cur.fetchall()
        data_type = data_type_results[0][0]

        # BIT members: data_type resolves to BOOL but the encoding varies by sub-type.
        #
        # Pattern 1 (0x6c != 0xFFFFFFFF): 0x6c holds the 0x60 byte-offset of the backing
        #   field.  Resolve target via offset60_to_name.
        #
        # Pattern 2 (0x6c == 0xFFFFFFFF and 0x68 == 0): BIT member where the backing field
        #   pointer is absent.  Use _fallback_target (the most-recent preceding hidden SINT).
        #
        # Pattern 3 (0x6c == 0xFFFFFFFF and 0x68 == 1): BIT member where 0x60 directly
        #   matches the backing field's 0x60 value.  Resolve target via offset60_to_name
        #   using the member's own 0x60 value as the lookup key.
        #
        # Plain BOOL (0x6c == 0xFFFFFFFF and 0x68 == 0x800): not a BIT member; leave as BOOL.
        target: Union[str, None] = None
        bit_number: Union[int, None] = None
        if data_type == "BOOL":
            target_key = fields.target_key
            val_68 = fields.host
            if target_key != 0xFFFFFFFF:
                # Pattern 1: direct backing-field reference via offset-60 map
                data_type = "BIT"
                # offset 0x5C holds a bit-offset into the host register, not an array
                # size — force dimension to 0 so _member_decorated_xml treats this as
                # a scalar rather than emitting thousands of <Element> entries.
                dimension = 0
                bit_number = fields.bit
                target = self._offset60_to_name.get(target_key)
            elif val_68 == 0:
                # Pattern 2: BIT member without explicit backing-field pointer
                data_type = "BIT"
                dimension = 0  # same bit-offset field; not an array size
                bit_number = fields.bit
                target = self._fallback_target
            elif val_68 == 1:
                # Pattern 3: BIT member — 0x60 of this member equals 0x60 of the backing
                # hidden field, allowing direct lookup in offset60_to_name.
                data_type = "BIT"
                dimension = 0
                bit_number = fields.bit
                target = self._offset60_to_name.get(fields.offset)

        # --- Description ---
        # The member's description is identified in the comments table by a
        # member_ref value extracted from bytes [14:18] of the comps record.
        # This value is non-zero for sub-elements (members) and zero for the
        # owning object's own description.
        description: Union[str, None] = None
        raw_comps = bytes(results[0][3])
        if len(raw_comps) >= 18:
            member_ref = struct.unpack_from("<I", raw_comps, 14)[0]
            if member_ref:
                self._cur.execute(
                    f"SELECT record_string FROM comments WHERE parent=? AND member_ref=? AND {_USER_COMMENT} LIMIT 1",
                    ((r.comment_id * 0x10000) + r.cip_type, member_ref),
                )
                desc_row = self._cur.fetchone()
                if desc_row and desc_row[0]:
                    description = desc_row[0]

        return Member(
            name, name, data_type, dimension, radix, hidden, target, bit_number, external_access, description,
            _offset=fields.offset,
            _bit=fields.bit,
        )


def _u32(attributes: Dict[int, bytes], key: int, default: int = 0) -> int:
    """Attribute `key` read as a u32, or `default` when absent or not 4 bytes."""
    value = attributes.get(key, b"")
    return struct.unpack("<I", value)[0] if len(value) == 4 else default


@dataclass
class DataTypeBuilder(L5xElementBuilder):
    def build(self) -> DataType:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        name = results[0][0]

        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return DataType(name, name, "NoFamily", "User", [])

        extended_records = r.attributes
        # Plain user-defined types can lack any of these attributes.
        string_family = "StringFamily" if _u32(extended_records, 0x6C) == 1 else "NoFamily"
        built_in = _u32(extended_records, 0x67)
        module_defined = _u32(extended_records, 0x69)

        class_type = "User"
        if module_defined > 0:
            class_type = "IO"
        if built_in & 0x03:
            class_type = "ProductDefined"
        member_count = _u32(extended_records, 0x64)
        size = _u32(extended_records, 0x65)

        if isinstance(r, LegacyRxGeneric):
            return DataType(
                name, name, string_family, class_type,
                self._legacy_members(extended_records, member_count),
                self._legacy_description(), _size=size,
            )

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(self._object_id)
        )
        member_results = self._cur.fetchall()
        children: List[Member] = []
        if len(member_results) == 1:
            member_collection_id = member_results[0][1]

            self._cur.execute(
                f"SELECT comp_name, object_id, parent_id, seq_number, record FROM comps WHERE parent_id={member_collection_id} ORDER BY seq_number"
            )
            children_results = self._cur.fetchall()

            # Build offset60→name map so BIT members can resolve their Target name.
            # Each member's extended record stores the byte offset of that member's data
            # within the UDT at [0x60]; BIT members reference their backing field's
            # offset via [0x6c].  Non-BIT members have [0x6c]=0xFFFFFFFF and 0x68=0x800.
            # Only include non-BIT members (0x68==0x800) so that BIT members sharing the
            # same 0x60 value as their backing field do not overwrite the backing entry.
            offset60_to_name: Dict[int, str] = {}
            for idx2, child2 in enumerate(children_results):
                key2 = 0x6E + idx2
                if key2 not in extended_records:
                    break
                rec2 = bytes(extended_records[key2])
                if len(rec2) >= 0x70:
                    target_key2 = struct.unpack_from("<I", rec2, 0x6C)[0]
                    val_68_2 = struct.unpack_from("<I", rec2, 0x68)[0]
                    if target_key2 == 0xFFFFFFFF and val_68_2 == 0x800:
                        val_60 = struct.unpack_from("<I", rec2, 0x60)[0]
                        offset60_to_name[val_60] = child2[0]

            # Some ACD files have mismatched member_count vs children list — iterate what we have.
            # Track the most recent preceding hidden SINT (fallback target for Pattern-2 BIT members).
            last_hidden_backing: Union[str, None] = None
            for idx, child in enumerate(children_results):
                key = 0x6E + idx
                if key not in extended_records:
                    break
                rec = bytes(extended_records[key])
                # Update last_hidden_backing when we see a hidden member
                if len(rec) >= 0x74:
                    is_hidden = bool(struct.unpack_from("<I", rec, 0x70)[0])
                    if is_hidden:
                        last_hidden_backing = child[0]
                try:
                    children.append(
                        MemberBuilder(
                            self._cur, child[1], record=bytes(extended_records[key]),
                            _offset60_to_name=offset60_to_name,
                            _fallback_target=last_hidden_backing,
                        ).build()
                    )
                except Exception:
                    pass

        # --- Description ---
        description: Union[str, None] = None
        self._cur.execute(
            f"SELECT record_string FROM comments WHERE parent=? AND member_ref=0 AND {_USER_COMMENT} LIMIT 1",
            ((r.comment_id * 0x10000) + r.cip_type,),
        )
        desc_row = self._cur.fetchone()
        if desc_row and desc_row[0]:
            description = desc_row[0]

        return DataType(name, name, string_family, class_type, children, description, _size=size)

    def _legacy_members(self, extended_records: Dict[int, bytes], member_count: int) -> List[Member]:
        """Members of a legacy-layout (v20 and earlier) type.

        These types have no member objects in Comps; each member is a record
        inside the type's own record, at attribute 0x6E + index. The record
        starts with the member name (UTF-16LE, NUL-terminated, 0x54 bytes)
        and then has the same fields as a current-layout member record (see
        _member_fields). For a BIT member the host field is the index of its
        host member (0x800 for any other member).
        """
        records = []
        for idx in range(member_count):
            rec = extended_records.get(0x6E + idx, b"")
            if len(rec) < 0x78:
                break
            records.append(rec)
        names = [rec[:0x54].decode("utf-16-le", errors="replace").split("\0")[0] for rec in records]

        members: List[Member] = []
        for rec, name in zip(records, names):
            fields = _member_fields(rec)
            self._cur.execute("SELECT comp_name FROM comps WHERE object_id=?", (fields.type_id,))
            row = self._cur.fetchone()
            data_type = row[0] if row else ""
            dimension = fields.dimension
            target: Union[str, None] = None
            bit_number: Union[int, None] = None
            if data_type == "BOOL" and fields.host != 0x800 and fields.host < len(names):
                data_type, dimension = "BIT", 0
                target, bit_number = names[fields.host], fields.bit
            members.append(Member(
                name, name, data_type, dimension, radix_enum(fields.radix), bool(fields.hidden),
                target, bit_number, external_access_enum(fields.access),
                _offset=fields.offset, _bit=fields.bit,
            ))
        return members

    def _legacy_description(self) -> Union[str, None]:
        """A legacy-layout type's description, filed under its own comment key."""
        scope_key, _ = _legacy_comment_keys(self._cur, self._object_id)
        if not scope_key:
            return None
        self._cur.execute(
            f"SELECT record_string FROM comments WHERE parent=? AND member_ref=0 AND {_USER_COMMENT} LIMIT 1",
            (scope_key,),
        )
        row = self._cur.fetchone()
        return row[0] if row and row[0] else None


@dataclass
class ModuleBuilder(L5xElementBuilder):
    # Map from modid (u32) → module name, built by ControllerBuilder and passed in.
    _modid_to_name: Dict[int, str] = field(default_factory=dict)
    # Optional richer CIP-identity -> catalog-number table (None -> built-in only).
    _catalog_table: Union[Dict[Tuple[int, int, int], str], None] = None

    def _ip_from_data_collection(self, icp_slot: int) -> str:
        """Look up the Ethernet IP for a local backplane module via RxDataCollection.

        Local bridge modules (e.g. EN2T in the main chassis) store their IP as XML
        in hash-named children of RxDataCollection. The record for a given module
        contains its ICP slot as Type="ICP" Addr="{slot}", which uniquely identifies it.
        """
        import re as _re
        needle = f'Type="ICP" Addr="{icp_slot}"'.encode()
        # Find RxDataCollection — it is a direct child of the controller object.
        self._cur.execute(
            "SELECT object_id FROM comps WHERE comp_name='RxDataCollection' LIMIT 1"
        )
        row = self._cur.fetchone()
        if not row:
            return ""
        coll_oid = row[0]
        # Fetch all children in batches and filter in Python (SQLite LIKE on BLOBs is unreliable).
        self._cur.execute(
            "SELECT record FROM comps WHERE parent_id=?", (coll_oid,)
        )
        for (raw,) in self._cur.fetchall():
            raw = bytes(raw)
            if needle not in raw:
                continue
            m = _re.search(rb'Type="EN" Addr="([^"]+)"', raw)
            return m.group(1).decode("ascii", errors="replace") if m else ""
        return ""

    def _comms_from_data_collection(
        self, icp_slot: int, ip_address: str = ""
    ) -> "Tuple[Union[str, None], str]":
        """Extract CommMethod and ExtendedProperties public data for a module.

        Searches RxDataCollection for the hash-named child whose <in> block contains
        a Port with Type="ICP" Addr="{icp_slot}".  For Ethernet-connected modules
        (icp_slot == 0 or not found by ICP slot) a second pass matches by the
        module's IP address instead.

        Returns a 2-tuple:
          (comm_method_str_or_None, public_content_str)

        comm_method_str: the numeric string from <CF>...</CF>, or None if absent.
        public_content_str: inner content of <public>...</public>, or "" if absent.

        The record XML is often stored without the closing </public> tag (it is
        truncated in the ACD binary). We reconstruct the content by extracting
        everything after <public>.
        """
        import re as _re

        def _extract(raw: bytes) -> "Tuple[Union[str, None], str]":
            xml_start = raw.find(b'<')
            if xml_start < 0:
                return (None, "")
            xml_text = raw[xml_start:].decode("latin-1", errors="replace")
            comm_method: Union[str, None] = None
            cf_m = _re.search(r'<CF>(\d+)</CF>', xml_text)
            if cf_m:
                comm_method = cf_m.group(1)
            pub_content = ""
            pub_start = xml_text.find("<public>")
            if pub_start >= 0:
                after_pub = xml_text[pub_start + len("<public>"):]
                end_tag_m = _re.search(r'</pub', after_pub)
                if end_tag_m:
                    pub_content = after_pub[:end_tag_m.start()]
                else:
                    pub_content = after_pub.rstrip("\x00 \r\n")
            return (comm_method, pub_content)

        self._cur.execute(
            "SELECT object_id FROM comps WHERE comp_name='RxDataCollection' LIMIT 1"
        )
        row = self._cur.fetchone()
        if not row:
            return (None, "")
        coll_oid = row[0]
        self._cur.execute(
            "SELECT record FROM comps WHERE parent_id=?", (coll_oid,)
        )
        all_recs = [(bytes(raw),) for (raw,) in self._cur.fetchall()]

        # First pass: match by ICP slot.
        if icp_slot:
            needle = f'Type="ICP" Addr="{icp_slot}"'.encode()
            for (raw,) in all_recs:
                if needle in raw:
                    result = _extract(raw)
                    if result[0] is not None or result[1]:
                        return result

        # Second pass: match by IP address (for EN-connected modules).
        if ip_address:
            ip_needle = f'Addr="{ip_address}"'.encode()
            for (raw,) in all_recs:
                if ip_needle in raw:
                    result = _extract(raw)
                    if result[0] is not None or result[1]:
                        return result

        return (None, "")

    def _chassis_size_from_data_collection(self) -> "Union[int, None]":
        """Read the local backplane Bus Size from the RxDataCollection record for the CPU.

        The root controller (Local) module stores its backplane configuration as XML in a
        hash-named child of RxDataCollection.  The record contains:
          <Port Id="1" Type="ICP" Addr="0" Ups="False"><Bus Max="17" Size="7"/></Port>
        We extract the Size attribute from the Bus element on the ICP port at Addr="0".
        """
        import re as _re
        self._cur.execute(
            "SELECT object_id FROM comps WHERE comp_name='RxDataCollection' LIMIT 1"
        )
        row = self._cur.fetchone()
        if not row:
            return None
        coll_oid = row[0]
        self._cur.execute(
            "SELECT record FROM comps WHERE parent_id=?", (coll_oid,)
        )
        needle = b'Type="ICP" Addr="0"'
        for (raw,) in self._cur.fetchall():
            raw = bytes(raw)
            if needle not in raw:
                continue
            text_start = raw.find(b"<")
            if text_start < 0:
                continue
            text = raw[text_start:].decode("latin-1", errors="replace")
            m = _re.search(r'<Bus\b[^>]*\bSize="(\d+)"', text)
            if m:
                return int(m.group(1))
        return None

    def build(self) -> Module:
        self._cur.execute(
            "SELECT comp_name, object_id, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        db_name = row[0]
        raw_rec = bytes(row[2])

        # Hex-encoded names like $02cc5e9d$ are unnamed peripheral modules (drive expansion
        # cards, etc.).  Logix Designer exports these with Name="?".
        name = "?" if (db_name.startswith("$") and db_name.endswith("$")) else db_name

        try:
            r = RxGeneric.from_bytes(raw_rec)
        except Exception:
            return Module(name, name, "", 0, 0, 0, 0, 0, "Local", 1, "false", "false")

        if r.cip_type != 0x69:
            return Module(name, name, "", 0, 0, 0, 0, 0, "Local", 1, "false", "false")

        exts = r.attributes
        e1 = exts.get(0x001, b"")
        if len(e1) < 0x30:
            major_fault = "true" if name == "Local" else "false"
            return Module(name, name, "", 0, 0, 0, 0, 0, "Local", 1, "false", major_fault)

        vendor        = struct.unpack("<H", e1[0x02:0x04])[0]
        product_type  = struct.unpack("<H", e1[0x04:0x06])[0]
        product_code  = struct.unpack("<H", e1[0x06:0x08])[0]
        # bit 7 of the major byte is a flag; strip it to get the firmware revision.
        major         = e1[0x08] & 0x7F
        minor         = e1[0x09]
        parent_modid  = struct.unpack("<I", e1[0x16:0x1A])[0]
        parent_port   = struct.unpack("<H", e1[0x1A:0x1C])[0]
        slot          = struct.unpack("<I", e1[0x1C:0x20])[0]

        # Hash-named modules are drive peripheral expansion cards. The ACD binary stores
        # them with ProductType=123 and site-specific ProductCodes, but Logix exports them
        # all as PT=0 PC=28 (RHINOBP-DRIVE-PERIPHERAL-MODULE) without a Name attribute.
        if name == "?":
            product_type = 0
            product_code = 28

        # Resolve parent module name from the modid→name map built by ControllerBuilder.
        parent_name = self._modid_to_name.get(parent_modid, "Local")

        # MajorFault=true for the root controller module: its parent resolves to itself.
        major_fault = "true" if parent_name == name else "false"
        # bit 2 (0x04) of e1[0] → EKey Disabled (Local=0x06→Disabled, EN2T=0x11→CompatibleModule).
        ekey_state  = "Disabled" if (e1[0] & 0x04) else "CompatibleModule"

        # IP address: stored at e1[0x30] as a u16 length-prefixed ASCII string for modules
        # that connect via Ethernet upstream (parent_port == 2). Local backplane bridge
        # modules (parent_port == 1, e.g. local EN2T) leave e1[0x32] zero — their IP is
        # stored as XML in a child of RxDataCollection, keyed by ICP slot number.
        ip_address = ""
        if len(e1) > 0x32:
            ip_len = struct.unpack("<H", e1[0x30:0x32])[0]
            if ip_len:
                ip_address = e1[0x32:0x32 + ip_len].rstrip(b"\x00").decode("ascii", errors="replace")
        if not ip_address and slot:
            ip_address = self._ip_from_data_collection(slot)

        # For modules that own a remote backplane (e.g. remote chassis EN2T), the Output
        # connection record under RxMapConnectionCollection stores the chassis size at [0x4e]
        # and the module's own slot in that chassis at [0x6e].
        backplane_slot = None
        chassis_size = None
        self._cur.execute(
            "SELECT o.record FROM comps coll "
            "JOIN comps o ON o.parent_id = coll.object_id AND o.comp_name = 'Output' "
            "WHERE coll.parent_id = ? AND coll.comp_name = 'RxMapConnectionCollection'",
            (self._object_id,),
        )
        out_row = self._cur.fetchone()
        if out_row:
            out_rec = bytes(out_row[0])
            if len(out_rec) > 0x70:
                backplane_slot = struct.unpack("<H", out_rec[0x6e:0x70])[0]
                chassis_size   = struct.unpack("<H", out_rec[0x4e:0x50])[0]

        # For the root (Local) CPU module (slot=0xFFFFFFFF, self-parenting), the Output
        # connection record is absent.  The backplane chassis size is stored in the
        # RxDataCollection hash child that carries the Local module's ICP port at Addr="0".
        # Example: <Port Id="1" Type="ICP" Addr="0" Ups="False"><Bus Max="17" Size="7"/></Port>
        if chassis_size is None and slot == 0xFFFFFFFF:
            chassis_size = self._chassis_size_from_data_collection()

        # --- Description ---
        # Module descriptions are stored in the comments table keyed by
        # (comment_id * 0x10000 + cip_type), same as for tags.
        description = ""
        self._cur.execute(
            f"SELECT record_string FROM comments WHERE parent=? AND member_ref=0 AND {_USER_COMMENT} LIMIT 1",
            ((r.comment_id * 0x10000) + r.cip_type,),
        )
        desc_row = self._cur.fetchone()
        if desc_row:
            description = desc_row[0] or ""

        # --- Communications and ExtendedProperties ---
        # Both are extracted from the hash-named child of RxDataCollection that
        # corresponds to this module's ICP backplane slot (primary) or its IP
        # address (secondary, for EN-connected modules).
        comm_method: Union[str, None] = None
        connections: List[Tuple[str, str, str]] = []
        extended_properties = ""
        if slot or ip_address:
            comm_method, extended_properties = self._comms_from_data_collection(
                slot, ip_address
            )

        # Read individual connection records from RxMapConnectionCollection children.
        # Each child's comp_name is the connection Name in the L5X output.
        # Connection Type is inferred from the name (heuristic):
        #   names containing "output" or equal to "config" -> "Output"
        #   all others -> "Input"
        # RPI: we do not have a reliable binary decoder for the short connection
        # records seen in the test data, so we default to "0.0" (acceptable for import).
        self._cur.execute(
            "SELECT c2.comp_name FROM comps c1 "
            "JOIN comps c2 ON c2.parent_id = c1.object_id "
            "WHERE c1.parent_id = ? AND c1.comp_name = 'RxMapConnectionCollection' "
            "AND c2.comp_name NOT IN ('Output') "
            "ORDER BY c2.seq_number",
            (self._object_id,),
        )
        for (conn_name,) in self._cur.fetchall():
            name_lower = conn_name.lower()
            if "output" in name_lower or name_lower == "config":
                conn_type = "Output"
            else:
                conn_type = "Input"
            connections.append((conn_name, "0.0", conn_type))

        return Module(
            name,           # L5xElement._name (private)
            name,           # Module.name
            catalog_number_for_identity(
                (vendor, product_type, product_code), table=self._catalog_table
            ),
            vendor,
            product_type,
            product_code,
            major,
            minor,
            parent_name,
            parent_port,
            "false",        # Inhibited: always false in practice; no known bit
            major_fault,
            _ekey_state=ekey_state,
            _slot=slot,
            _ip_address=ip_address,
            _backplane_slot=backplane_slot,
            _chassis_size=chassis_size,
            _description=description,
            _comm_method=comm_method,
            _connections=connections,
            _extended_properties=extended_properties,
        )


@dataclass
class TagBuilder(L5xElementBuilder):
    # The parsed tag record, set by _build() for build(); None when the
    # record isn't a tag.
    _record: Any = field(default=None)

    def _taginfo_data_type(self, name: str) -> str:
        # Fallback for tags whose records don't resolve a type (e.g. alarm
        # tags in legacy files): TagInfo.XML lists every tag with its type.
        # An entry in the tag's own scope comes first: a tag collection's
        # parent is the controller (the root comp) or the owning program.
        self._cur.execute(
            "SELECT o.comp_name, o.parent_id FROM comps t "
            "JOIN comps c ON c.object_id = t.parent_id "
            "JOIN comps o ON o.object_id = c.parent_id WHERE t.object_id=?",
            (self._object_id,),
        )
        owner = self._cur.fetchone()
        if owner is not None:
            program = "" if owner[1] == 0 else owner[0]
            self._cur.execute(
                "SELECT DISTINCT data_type FROM taginfo_tags WHERE name=? AND program=?",
                (name, program),
            )
            rows = self._cur.fetchall()
            if rows:
                return rows[0][0] if len(rows) == 1 else ""
        # No entry in its own scope: fall back to same-named tags elsewhere
        # when they agree on one type.
        self._cur.execute("SELECT DISTINCT data_type FROM taginfo_tags WHERE name=?", (name,))
        rows = self._cur.fetchall()
        if len(rows) == 1:
            self._notes.warn(
                f"Tag '{name}': data type {rows[0][0]} taken from a same-named "
                "tag in another scope of TagInfo.XML"
            )
            return rows[0][0]
        if rows:
            self._notes.warn(
                f"Tag '{name}': no data type; TagInfo.XML has no entry in its "
                "scope and same-named tags in other scopes disagree"
            )
        return ""

    @staticmethod
    def _format_rpi(microseconds: int) -> str:
        if microseconds % 1000 == 0:
            return str(microseconds // 1000)
        return f"{microseconds / 1000:.3f}"

    def _legacy_connection_descriptor(
        self, attribute_id: int, descriptor_kind: int
    ) -> bytes:
        self._cur.execute(
            "SELECT record FROM comps WHERE length(record) >= 10 "
            "AND hex(substr(record, 9, 2)) = '7E00'"
        )
        matches = []
        for (raw_record,) in self._cur.fetchall():
            try:
                parsed = RxGeneric.from_bytes(raw_record)
            except Exception:
                continue
            if parsed.cip_type != 0x7E:
                continue
            attributes = parsed.attributes
            reference = attributes.get(attribute_id, b"")
            data = attributes.get(0x01, b"")
            if (
                len(reference) >= 4
                and struct.unpack_from("<I", reference)[0] == self._object_id
                and len(data) >= 2
                and struct.unpack_from("<H", data)[0] == descriptor_kind
            ):
                matches.append(data)
        if len(matches) != 1:
            raise ValueError("Tag does not have exactly one connection descriptor")
        return matches[0]

    def _legacy_linked_data(self, extended_records: Dict[int, bytes]):
        link = extended_records.get(0x6B, b"")
        if len(link) < 4:
            raise ValueError("Tag connection does not reference linked data")
        linked_id = struct.unpack_from("<I", link)[0]
        self._cur.execute(
            "SELECT record FROM comps WHERE object_id=?",
            (linked_id,),
        )
        row = self._cur.fetchone()
        if not row:
            raise ValueError("Tag connection linked data does not exist")
        linked = RxGeneric.from_bytes(row[0])
        attributes = linked.attributes
        metadata = attributes.get(0x64, b"")
        if len(metadata) < 4:
            raise ValueError("Tag connection linked data is incomplete")
        return linked, struct.unpack_from("<I", metadata)[0]

    def _legacy_processor_minimum_rpi(self) -> int:
        self._cur.execute("SELECT record FROM comps WHERE comp_name='Local'")
        for (raw_record,) in self._cur.fetchall():
            try:
                parsed = RxGeneric.from_bytes(raw_record)
            except Exception:
                continue
            if parsed.cip_type != 0x69:
                continue
            attributes = parsed.attributes
            metadata = attributes.get(0x01, b"")
            if len(metadata) >= 8:
                device = struct.unpack_from("<HHH", metadata, 2)
                if device == (1, 14, 72):
                    return 1000
        return 0

    def _legacy_produce_info(
        self,
        extended_records: Dict[int, bytes],
    ) -> ProduceInfo:
        data = self._legacy_connection_descriptor(0x191, 0x0A)
        linked, element_size = self._legacy_linked_data(extended_records)
        if len(data) < 0x156 or struct.unpack_from("<H", data)[0] != 0x0A:
            raise ValueError(
                f"Invalid produced tag descriptor for object {self._object_id}"
            )
        if struct.unpack_from("<I", data, 0x1A)[0] != element_size:
            raise ValueError("Produced tag descriptor has the wrong element size")
        if struct.unpack_from("<I", data, 0x1E)[0] != linked.unique_tag_identifier:
            raise ValueError("Produced tag descriptor has the wrong linked data")

        minimum_rpi = max(
            struct.unpack_from("<I", data, len(data) - 0x10)[0],
            self._legacy_processor_minimum_rpi(),
        )
        maximum_rpi = struct.unpack_from("<I", data, len(data) - 0x0C)[0]
        default_rpi = struct.unpack_from("<I", data, len(data) - 0x08)[0]
        return ProduceInfo(
            "ProduceInfo",
            str(struct.unpack_from("<H", data, 0x141)[0]),
            "true" if struct.unpack_from("<I", data, 0x134)[0] else "false",
            "true" if data[0x144] else "false",
            self._format_rpi(minimum_rpi),
            self._format_rpi(maximum_rpi),
            self._format_rpi(default_rpi),
        )

    def _legacy_consume_info(
        self,
        extended_records: Dict[int, bytes],
    ) -> ConsumeInfo:
        data = self._legacy_connection_descriptor(0x190, 0x09)
        linked, element_size = self._legacy_linked_data(extended_records)
        if len(data) <= 0x143 or struct.unpack_from("<H", data)[0] != 0x09:
            raise ValueError(
                f"Invalid consumed tag descriptor for object {self._object_id}"
            )
        if struct.unpack_from("<I", data, 0x0C)[0] != element_size:
            raise ValueError("Consumed tag descriptor has the wrong element size")
        if struct.unpack_from("<I", data, 0x10)[0] != linked.unique_tag_identifier:
            raise ValueError("Consumed tag descriptor has the wrong linked data")

        remote_name_length = struct.unpack_from("<H", data, 0x22)[0]
        remote_name_end = 0x24 + remote_name_length
        if remote_name_end > len(data):
            raise ValueError("Consumed tag descriptor has an invalid remote tag")
        remote_tag = data[0x24:remote_name_end].decode("utf-8")
        unicast_value = data[0x143]
        if unicast_value not in (1, 2):
            raise ValueError("Consumed tag descriptor has an invalid unicast value")

        producer_reference = extended_records.get(0x66, b"")
        if len(producer_reference) < 4:
            raise ValueError("Consumed tag does not reference a producer")
        producer_id = struct.unpack_from("<I", producer_reference)[0]
        self._cur.execute(
            "SELECT comp_name FROM comps WHERE object_id=?",
            (producer_id,),
        )
        producer_row = self._cur.fetchone()
        if not producer_row:
            raise ValueError("Consumed tag producer does not exist")

        return ConsumeInfo(
            "ConsumeInfo",
            producer_row[0],
            remote_tag,
            str(struct.unpack_from("<H", data, 0x06)[0]),
            self._format_rpi(struct.unpack_from("<I", data, 0x02)[0]),
            "true" if unicast_value == 2 else "false",
        )

    def _legacy_data_type(self, record: LegacyRxGeneric) -> str:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=?",
            (record.data_instance_id,),
        )
        data_row = self._cur.fetchone()
        if data_row:
            raw_data_record = bytes(data_row[1])
            legacy_cip_type = (
                struct.unpack_from("<H", raw_data_record, 8)[0]
                if len(raw_data_record) >= 10
                else 0
            )
            if legacy_cip_type == 0x8D:
                return "MESSAGE"
            if legacy_cip_type == 0xB0:
                return "MOTION_GROUP"
            if legacy_cip_type == 0xB1:
                if record.main_record.runtime_type & 0xFF == 0xC7:
                    return "AXIS_SERVO_DRIVE"
                return "AXIS_SERVO"
            try:
                data_record = RxGeneric.from_bytes(raw_data_record)
                if data_record.cip_type == 0x6A:
                    self._cur.execute(
                        "SELECT comp_name FROM comps WHERE object_id=?",
                        (data_record.data_instance_id,),
                    )
                    type_row = self._cur.fetchone()
                    if type_row:
                        return type_row[0]
            except Exception:
                pass

        atomic_types = {
            0xC1: "BOOL",
            0xC2: "SINT",
            0xC3: "INT",
            0xC4: "DINT",
            0xC5: "LINT",
            0xC6: "USINT",
            0xC7: "UINT",
            0xC8: "UDINT",
            0xC9: "ULINT",
            0xCA: "REAL",
            0xCB: "LREAL",
        }
        return atomic_types.get(record.main_record.runtime_type & 0xFF, "")

    def _resolve_alias_path(self, value: bytes) -> str:
        # UTF-16 path, usually null-terminated; a path that fills the field
        # has no terminator.
        if len(value) % 2:
            raise ValueError("Invalid alias path")
        path = value.decode("utf-16-le").rstrip("\x00")

        def replace_object_id(match):
            object_id = int(match.group(1), 16)
            self._cur.execute(
                "SELECT comp_name, record FROM comps WHERE object_id=?",
                (object_id,),
            )
            row = self._cur.fetchone()
            if not row:
                raise ValueError("Alias target does not exist")
            try:
                target = RxGeneric.from_bytes(row[1])
                logical_name = getattr(target, "logical_name", "")
                if logical_name:
                    return logical_name
            except Exception:
                pass
            return self._module_tag_name(row[0])

        resolved = re.sub(r"@([0-9a-fA-F]{8})@", replace_object_id, path)
        if re.search(r"@[0-9a-fA-F]{8}@", resolved):
            raise ValueError("Alias target could not be resolved")
        return resolved

    def _module_tag_name(self, comp_name: str) -> str:
        # Current-layout module tags are named "&<module object id>:2:O";
        # Rockwell writes the module's name there ("Local:2:O").
        match = re.match(r"&([0-9a-fA-F]{8}):", comp_name)
        if not match:
            return comp_name
        self._cur.execute(
            "SELECT comp_name FROM comps WHERE object_id=?",
            (int(match.group(1), 16),),
        )
        row = self._cur.fetchone()
        if not row:
            raise ValueError("Alias target module does not exist")
        return row[0] + comp_name[match.end() - 1 :]

    def build(self) -> Tag:
        tag = self._build()
        if not tag.data_type and tag.tag_type != "Alias":
            tag.data_type = self._taginfo_data_type(tag.name) or tag.data_type
        r = self._record
        if r is not None:
            data = self._data_object_value(r.data_object_id)
            if data is not None:
                tag._data = data
            tag.usage = _program_tag_usage(r)
        return tag

    def _data_object_value(self, object_id: int) -> Union[bytes, None]:
        """The stored value bytes (attribute 0x66) of a tag's data object."""
        self._cur.execute("SELECT record FROM comps WHERE object_id=?", (object_id,))
        row = self._cur.fetchone()
        if not row:
            return None
        try:
            return RxGeneric.from_bytes(row[0]).attribute(0x66)
        except Exception:
            return None

    def _build(self) -> Tag:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        # Extract ExternalAccess and Constant from the raw record at fixed offsets:
        #   raw[0x278]: ExternalAccess enum (0=Read/Write, 2=Read Only, 3=None)
        #   raw[0x279]: Constant flag (0=false, 1=true)
        raw_rec = bytes(results[0][3])
        if len(raw_rec) > 0x279:
            external_access = external_access_enum(raw_rec[0x278])
            constant = "true" if raw_rec[0x279] else None
        else:
            external_access = "Read/Write"
            constant = None

        self._record = None
        try:
            r = RxGeneric.from_bytes(raw_rec)
        except Exception:
            return Tag(
                results[0][0], results[0][0], "Base", "", None, external_access, constant, None, 0, []
            )

        if r.cip_type != 0x6B and r.cip_type != 0x68:
            return Tag(
                results[0][0], results[0][0], "Base", "", None, external_access, constant, None, 0, []
            )
        self._record = r

        # Program tags: (program, tag); controller tags: (tag, 0).
        scope_key, member_key = _comment_keys(self._cur, self._object_id, r)
        if member_key or isinstance(r, LegacyRxGeneric):
            # Only the comments filed under this tag's own key, not every
            # comment in the program's scope.
            self._cur.execute(
                "SELECT tag_reference, record_string FROM comments WHERE parent=? AND member_ref=? "
                f"AND {_USER_COMMENT} ORDER BY rowid",
                (scope_key, member_key),
            )
        else:
            self._cur.execute(
                "SELECT tag_reference, record_string FROM comments WHERE parent="
                + str((r.comment_id * 0x10000) + r.cip_type)
                + f" AND {_USER_COMMENT} ORDER BY rowid"
            )
        comment_results = self._cur.fetchall()

        extended_records = r.attributes

        if isinstance(r, LegacyRxGeneric):
            metadata_name = r.logical_name
            name = metadata_name or results[0][0]
            radix = (
                radix_enum(r.main_record.radix)
                if r.main_record.radix
                else None
            )
            external_access = external_access_enum(r.main_record.external_access)

            if 0x65 in extended_records:
                return Tag(
                    name,
                    name,
                    "Alias",
                    None,
                    radix,
                    external_access,
                    None,
                    None,
                    r.main_record.data_table_instance,
                    comment_results,
                    alias_for=self._resolve_alias_path(extended_records[0x65]),
                )

            data_type = self._legacy_data_type(r)
            dimensions = [
                r.main_record.dimension_1,
                r.main_record.dimension_2,
                r.main_record.dimension_3,
            ]
            if (
                data_type == "BOOL"
                and r.main_record.runtime_type == 0x20D3
                and dimensions[0] == 1
            ):
                dimensions[0] = 32
            dimension_text = ",".join(
                str(dimension) for dimension in dimensions if dimension
            ) or None
            tag_type = "Base"
            if 0x6B in extended_records and ":" not in name:
                tag_type = "Consumed" if 0x66 in extended_records else "Produced"
            try:
                produce_info = (
                    self._legacy_produce_info(extended_records)
                    if tag_type == "Produced"
                    else None
                )
                consume_info = (
                    self._legacy_consume_info(extended_records)
                    if tag_type == "Consumed"
                    else None
                )
            except ValueError as e:
                # An empty produce/consume link (ext 0x6B) leaves nothing to
                # build the connection from; export a Base tag instead.
                self._notes.warn(
                    f"Tag '{name}': {tag_type.lower()} connection info could not "
                    "be read; exported as a Base tag",
                    str(e),
                )
                tag_type = "Base"
                produce_info = None
                consume_info = None
            return Tag(
                name,
                name,
                tag_type,
                data_type,
                radix,
                external_access,
                "false" if tag_type == "Produced" else None,
                dimension_text,
                r.main_record.data_table_instance,
                comment_results,
                produce_info=produce_info,
                consume_info=consume_info,
            )

        if r.main_record.data_type == 0xFFFFFFFF:
            data_type = ""
        else:
            self._cur.execute(
                "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
                + str(r.main_record.data_type)
            )
            data_type_results = self._cur.fetchall()
            data_type = data_type_results[0][0] if data_type_results else ""

        # The name is in ext[0x01] (u16 length, then utf-8), else comp_name.
        name = results[0][0]
        if len(extended_records.get(0x01, b"")) >= 2:
            name_length = struct.unpack("<H", extended_records[0x01][0:2])[0]
            name = extended_records[0x01][2 : name_length + 2].decode("utf-8", errors="replace")
        alias_for = None
        if 0x65 in extended_records:
            # Current-layout alias tags keep the same @<object id>@ path as
            # legacy ones, in attribute 0x65.
            try:
                alias_for = self._resolve_alias_path(extended_records[0x65])
            except ValueError as e:
                log.warning(
                    f"Tag '{name}': couldn't resolve its alias target ({e}) -- "
                    "exporting it as a Base tag instead"
                )
        if alias_for:
            return Tag(
                name,
                name,
                "Alias",
                None,
                radix_enum(r.main_record.radix),
                external_access,
                None,
                None,
                r.main_record.data_table_instance,
                comment_results,
                alias_for=alias_for,
            )

        dimensions = ",".join(
            str(dimension)
            for dimension in (
                r.main_record.dimension_1,
                r.main_record.dimension_2,
                r.main_record.dimension_3,
            )
            if dimension
        ) or None
        return Tag(
            name,
            name,
            "Base",
            data_type,
            radix_enum(r.main_record.radix),
            external_access,
            constant,
            dimensions,
            r.main_record.data_table_instance,
            comment_results,
        )


def _program_tag_usage(r: RxRecord) -> Union[str, None]:
    """Usage of a program parameter tag, or None for other tags.

    Current-layout program tags (cip_type 0x68) keep usage flags at ext[0x01]
    offset 0x20E, as Add-On Instruction tags do: 0x04 Input, 0x08 Output
    (both: InOut), and 0x10 Public (seen with 0x04 also set). Local tags carry
    only 0x02.
    """
    if isinstance(r, LegacyRxGeneric) or r.cip_type != 0x68:
        return None
    flags = _aoi_tag_usage_flags(r.attribute(0x01) or b"")
    if flags & 0x10:
        return "Public"
    if flags & 0x0C == 0x0C:
        return "InOut"
    if flags & 0x04:
        return "Input"
    if flags & 0x08:
        return "Output"
    return None


def _aoi_tag_usage_flags(ext01: bytes) -> int:
    """Return the usage-flags byte from an AOI tag record's ext[0x01] blob.

    Returns 0 if the record is too short.  The caller is responsible for
    interpreting the bits (0x04=Input, 0x08=Output; both set = InOut;
    neither set = local tag).
    """
    return ext01[0x20E] if len(ext01) > 0x20E else 0


def _aoi_tag_data_type(cur, raw_rec: bytes) -> str:
    """Look up the DataType name for an AOI tag record.

    The DataType OID is stored at offset 0x2A in the raw parameter record
    as a little-endian u32.  We look it up in the comps table by object_id.
    """
    if len(raw_rec) < 0x2E:
        return ""
    dt_oid = struct.unpack_from("<I", raw_rec, 0x2A)[0]
    cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(dt_oid))
    row = cur.fetchone()
    return row[0] if row else ""


@dataclass
class ParameterBuilder(L5xElementBuilder):
    """Build a Parameter from an AOI RxTagCollection child record."""

    def build(self) -> Parameter:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name = row[0]
        raw_rec = bytes(row[1])

        data_type = _aoi_tag_data_type(self._cur, raw_rec)

        # Dimensions (array size) at raw record offset 0x1A as u32; 0 means scalar.
        dimensions: Union[str, None] = None
        if len(raw_rec) >= 0x1E:
            dim_val = struct.unpack_from("<I", raw_rec, 0x1A)[0]
            if dim_val:
                dimensions = str(dim_val)

        try:
            r = RxGeneric.from_bytes(raw_rec)
            exts = r.attributes
        except Exception:
            return Parameter(name, name, "Base", data_type, "Input", None, "false", "false", "Read/Write", None, dimensions)

        ext01 = exts.get(0x01, b"")
        flags = _aoi_tag_usage_flags(ext01)

        usage_bits = flags & 0x0C
        if usage_bits == 0x04:
            usage = "Input"
        elif usage_bits == 0x08:
            usage = "Output"
        else:
            usage = "InOut"

        required = "true" if (flags & 0x20) else "false"
        visible = "true" if (flags & 0x40) else "false"

        # ExternalAccess (u16 at ext01[0x21E])
        # MESSAGE-type InOut parameters don't carry Constant in L5X; all others do.
        if usage == "InOut":
            external_access = None
            constant: Union[str, None] = None if data_type == "MESSAGE" else "false"
        elif len(ext01) > 0x21F:
            ea_val = struct.unpack_from("<H", ext01, 0x21E)[0]
            external_access = external_access_enum(ea_val)
            constant = None
        else:
            external_access = "Read/Write"
            constant = None

        # Radix (high nibble of ext01[0x20F]).  InOut params for complex/UDT types
        # omit Radix; InOut params for scalar/array types (BOOL, INT, DINT, REAL, etc.)
        # still carry Radix in the golden L5X, so include it whenever the radix index
        # is non-zero regardless of Usage.
        if not data_type or len(ext01) <= 0x20F:
            radix: Union[str, None] = None
        else:
            radix_idx = ext01[0x20F] >> 4
            radix = radix_enum(radix_idx) if radix_idx != 0 else None

        # --- Description ---
        # Use bytes [14:18] of the comps record as member_ref to identify the
        # specific parameter description in the comments table.
        description: Union[str, None] = None
        if len(raw_rec) >= 18:
            member_ref = struct.unpack_from("<I", raw_rec, 14)[0]
            if member_ref:
                self._cur.execute(
                    f"SELECT record_string FROM comments WHERE parent=? AND member_ref=? AND {_USER_COMMENT} LIMIT 1",
                    ((r.comment_id * 0x10000) + r.cip_type, member_ref),
                )
                desc_row = self._cur.fetchone()
                if desc_row and desc_row[0]:
                    description = desc_row[0]

        return Parameter(
            name,
            name,
            "Base",
            data_type,
            usage,
            radix,
            required,
            visible,
            external_access,
            constant,
            dimensions,
            description,
        )


@dataclass
class LocalTagBuilder(L5xElementBuilder):
    """Build a LocalTag from an AOI RxTagCollection child record."""

    def build(self) -> LocalTag:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name = row[0]
        raw_rec = bytes(row[1])

        data_type = _aoi_tag_data_type(self._cur, raw_rec)

        # Dimensions at raw record offset 0x1A.
        dimensions: Union[str, None] = None
        if len(raw_rec) >= 0x1E:
            dim_val = struct.unpack_from("<I", raw_rec, 0x1A)[0]
            if dim_val:
                dimensions = str(dim_val)

        try:
            r = RxGeneric.from_bytes(raw_rec)
            exts = r.attributes
        except Exception:
            return LocalTag(name, name, data_type, dimensions, None, "Read/Write")

        ext01 = exts.get(0x01, b"")
        if len(ext01) > 0x21F:
            ea_val = struct.unpack_from("<H", ext01, 0x21E)[0]
            external_access = external_access_enum(ea_val)
        else:
            external_access = "Read/Write"

        if len(ext01) > 0x20F:
            radix_idx = ext01[0x20F] >> 4
            radix: Union[str, None] = radix_enum(radix_idx) if radix_idx != 0 else None
        else:
            radix = None

        # --- Description ---
        # Use bytes [14:18] of the comps record as member_ref to identify the
        # specific local tag description in the comments table.
        description: Union[str, None] = None
        if len(raw_rec) >= 18:
            member_ref = struct.unpack_from("<I", raw_rec, 14)[0]
            if member_ref:
                self._cur.execute(
                    f"SELECT record_string FROM comments WHERE parent=? AND member_ref=? AND {_USER_COMMENT} LIMIT 1",
                    ((r.comment_id * 0x10000) + r.cip_type, member_ref),
                )
                desc_row = self._cur.fetchone()
                if desc_row and desc_row[0]:
                    description = desc_row[0]

        return LocalTag(name, name, data_type, dimensions, radix, external_access, description)


def routine_type_enum(idx: int) -> str:
    if idx == 0:
        return "TypeLess"
    if idx == 1:
        return "RLL"
    if idx == 2:
        return "FBD"
    if idx == 3:
        return "SFC"
    if idx == 4:
        return "ST"
    if idx == 5:
        return "External"
    if idx == 6:
        return "Encrypted"
    return "Typeless"


def _description_xml(text: Union[str, None]) -> str:
    """<Description> element for text, or "" when there is none."""
    return f"<Description>\n{_cdata_section(text)}\n</Description>" if text else ""


def _own_description(cur: Cursor, parent: int, alt_parent: Union[int, None],
                     member_ref: int) -> Union[str, None]:
    """An object's own description: the comment filed under its key with no
    operand and no rung (parent = comment scope, member_ref = own key or 0)."""
    parents = [p for p in (parent, alt_parent) if p]
    if not parents:
        return None
    try:
        cur.execute(
            "SELECT record_string FROM comments WHERE parent IN ("
            + ",".join("?" * len(parents))
            + ") AND member_ref=? AND rung_content=0 AND tag_reference='' "
            f"AND record_string!='' AND {_USER_COMMENT} ORDER BY rowid",
            (*parents, member_ref),
        )
        row = cur.fetchone()
    except Exception:
        return None
    return row[0] if row else None


def scoped_comment_keys(raw: bytes) -> Tuple[int, int]:
    """Return (scope key, member key) for a Comps record.

    Objects owned by a program (routines, program tags) carry the program's
    comment key (cip_type, comment_id) at 0x0A and their own at 0x0E.
    RxGeneric reads only the first, so every routine and tag in a program
    resolves to the same key. Their comments are stored with
    parent = scope key and member_ref = own key. The member key is 0 for
    objects that have no owning scope (e.g. controller tags).
    """
    if len(raw) < 0x12:
        return 0, 0
    return comment_keys(raw, 0x0A)


def _legacy_comment_keys(cur: Cursor, object_id: int) -> Tuple[int, int]:
    """(scope key, member key) of a legacy-layout object, from the comment_keys
    table ExportL5x fills from the record header; (0, 0) if unknown."""
    try:
        cur.execute(
            "SELECT scope_key, member_key FROM comment_keys WHERE object_id=?", (object_id,)
        )
        row = cur.fetchone()
    except Exception:
        row = None
    return (row[0], row[1]) if row else (0, 0)


def _comment_keys(cur: Cursor, object_id: int, r: RxRecord) -> Tuple[int, int]:
    """(scope key, member key) of a parsed Rx record of either layout."""
    if isinstance(r, LegacyRxGeneric):
        return _legacy_comment_keys(cur, object_id)
    # The header's cip_type and comment_id are the scope's pair; the
    # object's own pair opens the main record (see scoped_comment_keys).
    return comment_keys(struct.pack("<HH", r.cip_type, r.comment_id) + bytes(r.record_buffer[:4]), 0)


def _routine_owner_name(cur: Cursor, collection_id: int) -> str:
    # A routine's parent is its routine collection, whose parent is the
    # owning program or Add-On Instruction.
    cur.execute(
        "SELECT o.comp_name FROM comps c JOIN comps o ON o.object_id = c.parent_id "
        "WHERE c.object_id=?",
        (collection_id,),
    )
    row = cur.fetchone()
    return row[0] if row else ""


@dataclass
class RoutineBuilder(L5xElementBuilder):
    def build(self) -> Routine:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        try:
            r = RxGeneric.from_bytes(results[0][3], allow_protected=True)
        except Exception:
            r = None
        if r is None or r.protected:
            # A source-protected routine's attributes are encrypted, but its
            # 60-byte main record is intact, so the routine type can still be
            # read from it (0x30 of the main record, which starts at 14). An
            # unreadable record gets the same fallback.
            raw = bytes(results[0][3])
            routine_type = (
                routine_type_enum(struct.unpack_from("<H", raw, 14 + 0x30)[0])
                if len(raw) >= 74
                else ""
            )
            owner = _routine_owner_name(self._cur, results[0][2])
            self._notes.add(
                f"Routine '{owner + '/' if owner else ''}{results[0][0]}': content "
                "could not be read (likely source-protected); exported with no rungs"
            )
            return Routine(results[0][0], results[0][0], routine_type, [])

        name = results[0][0]
        routine_type = routine_type_enum(r.routine_type)

        self._cur.execute(
            "SELECT rm.object_id, r.rung FROM region_map rm "
            "LEFT JOIN rungs r ON r.object_id = rm.object_id "
            "WHERE rm.parent_id=" + str(self._object_id) + " ORDER BY rm.unknown"
        )
        rows = [(row[0], row[1]) for row in self._cur.fetchall() if row[1] is not None]
        rung_ids = [row[0] for row in rows]
        rungs = [row[1] for row in rows]

        # Resolve &hexid: placeholders (object ID references) to comp names.
        # The ACD binary stores tag references as &XXXXXXXX: where XXXXXXXX is the
        # object_id in hex. Batch-resolve all unique IDs to avoid per-rung queries.
        import re as _re
        all_hex = set(_re.findall(r'&([0-9a-f]{8}):', " ".join(r for r in rungs if r)))
        if all_hex:
            id_to_name: Dict[int, str] = {}
            for hex_id in all_hex:
                oid = int(hex_id, 16)
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=?", (oid,))
                row2 = self._cur.fetchone()
                if row2:
                    id_to_name[hex_id] = row2[0]
            if id_to_name:
                def _resolve(rung: str) -> str:
                    return _re.sub(
                        r'&([0-9a-f]{8}):',
                        lambda m: (id_to_name[m.group(1)] + ":") if m.group(1) in id_to_name else m.group(0),
                        rung,
                    )
                rungs = [_resolve(r) if r else r for r in rungs]

        # Fetch rung-level comments from the comments table, as AsciiRecord
        # (record_type=1) entries where rung_content != 0 (distinguishes user rung
        # comments from internal metadata strings like FBDRoutineDescription which
        # have rung_content=0).
        rung_comments: Dict[int, str] = {}
        scope_key, member_key = _comment_keys(self._cur, self._object_id, r)
        try:
            if member_key:
                # Rung comments sit in the program's comment scope with
                # member_ref = the routine's own key, and the high half of
                # rung_content is the rung uid from RegnLink.Dat.
                position = {oid: i for i, oid in enumerate(rung_ids)}
                self._cur.execute(
                    "SELECT object_id, uid FROM rung_uids WHERE parent_id=?",
                    (self._object_id,),
                )
                index_by_uid = {
                    uid: position[oid] for oid, uid in self._cur.fetchall() if oid in position
                }
                self._cur.execute(
                    "SELECT rung_content, record_string FROM comments "
                    "WHERE parent=? AND member_ref=? AND record_type=1 AND rung_content!=0 "
                    f"AND {_USER_COMMENT} ORDER BY rowid",
                    (scope_key, member_key),
                )
                for rung_content, rec_str in self._cur.fetchall():
                    rung_index = index_by_uid.get(rung_content >> 16)
                    if rung_index is not None and rec_str and rung_index not in rung_comments:
                        rung_comments[rung_index] = rec_str
            else:
                # Older layout: comments under the routine's own key, with
                # object_id as the 1-based rung index (rung 0 -> object_id=1).
                # These are the only rows read without the user-comment filter.
                self._cur.execute(
                    "SELECT object_id, record_string FROM comments "
                    "WHERE parent=? AND record_type=1 AND rung_content!=0 ORDER BY rowid",
                    ((r.comment_id * 0x10000) + r.cip_type,),
                )
                for obj_id, rec_str in self._cur.fetchall():
                    rung_index = obj_id - 1  # convert 1-based to 0-based
                    if rung_index >= 0 and rec_str and rung_index not in rung_comments:
                        rung_comments[rung_index] = rec_str
        except Exception:
            pass

        # Some firmware revisions store the routine type in a record layout
        # where the usual type field reads back 0 (TypeLess) even though the
        # routine carries ladder logic. The rung text here comes from "Rung NT"
        # SbRegion regions, which are RLL ladder rungs, so a TypeLess routine
        # that actually has rungs is an RLL routine whose type byte could not be
        # read from this layout. Classify it RLL so its logic is emitted instead
        # of silently dropped (Routine.to_xml only serialises rungs for RLL).
        if routine_type == "TypeLess" and rungs:
            routine_type = "RLL"
            self._notes.warn(
                f"Routine '{name}' has no readable type but carries ladder "
                "rungs; exported as RLL"
            )

        # The routine's description: same key as its rung comments, no rung.
        if member_key:
            description = _own_description(self._cur, scope_key, None, member_key)
        else:
            description = _own_description(
                self._cur, (r.comment_id * 0x10000) + r.cip_type, None, 0
            )

        st_lines: List[str] = []
        if routine_type == "ST":
            self._cur.execute(
                "SELECT text FROM st_lines WHERE routine_id=? ORDER BY line_number",
                (self._object_id,),
            )
            st_lines = [row[0] for row in self._cur.fetchall()]
            if not st_lines:
                self._notes.warn(
                    f"Structured Text routine '{name}' has no decoded lines; "
                    "exported with an empty body"
                )

        return Routine(
            name, name, routine_type, rungs, rung_ids, rung_comments,
            _st_lines=st_lines, _description=description,
        )


def _parse_fffeff(data: bytes, offset: int):
    """Parse one fffeff-encoded string at offset. Returns (str, new_offset)."""
    if offset + 3 > len(data) or not (data[offset] == 0xFF and data[offset+1] == 0xFE and data[offset+2] == 0xFF):
        return "", offset
    length = data[offset+3]
    s = data[offset+4:offset+4+length*2].decode("utf-16-le", errors="replace")
    return s, offset + 4 + length * 2


def _filetime_to_iso(ft: int) -> str:
    """Convert a Windows FILETIME (100-ns units since 1601-01-01) to ISO8601.

    Returns "" for an empty or out-of-range value. Some AOI nameless records
    carry a corrupt FILETIME (the variable-length field walk can drift and read
    8 garbage bytes), which would otherwise overflow ``datetime`` for years
    beyond 9999 and abort the whole export with ``OverflowError``.
    """
    if not ft:
        return ""
    try:
        dt = datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)
    except (OverflowError, OSError):
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_aoi_nameless(data: bytes) -> dict:
    """Extract AOI metadata from its large nameless record."""
    result: dict = {}

    offset = 0x1A
    # Three empty fffeff strings
    for _ in range(3):
        _, offset = _parse_fffeff(data, offset)

    # 8 bytes (unknown - some kind of date, skip)
    offset += 8

    # 2-byte constant (0x0002 observed)
    offset += 2

    # CreatedBy
    result["created_by"], offset = _parse_fffeff(data, offset)

    # Software revision at creation time (skip - we want the current one later)
    _, offset = _parse_fffeff(data, offset)

    # 4 zero bytes
    offset += 4

    # Empty fffeff placeholder
    _, offset = _parse_fffeff(data, offset)

    # CreatedDate FILETIME (8 bytes, Windows FILETIME in 100-ns units)
    result["created_date"] = _filetime_to_iso(struct.unpack_from("<Q", data, offset)[0])
    offset += 8

    # EditedBy
    result["edited_by"], offset = _parse_fffeff(data, offset)

    # SoftwareRevision (current)
    result["software_revision"], offset = _parse_fffeff(data, offset)

    # 4 bytes (01 00 00 00)
    offset += 4

    # RevisionExtension
    rev_ext, offset = _parse_fffeff(data, offset)
    result["revision_extension"] = rev_ext or None

    # EditedDate FILETIME (always last 8 bytes)
    result["edited_date"] = _filetime_to_iso(struct.unpack_from("<Q", data, len(data) - 8)[0])

    return result


def _aoi_data_type_description(cur, name: str) -> Union[str, None]:
    """Description of an AOI read from its backing data type's comments.

    A source-protected AOI keeps its own comments encrypted, but the data
    type that Logix creates for it (same name, in RxDataTypeCollection) holds
    the same description in plain text.
    """
    cur.execute(
        "SELECT c.record FROM comps c JOIN comps p ON c.parent_id = p.object_id "
        "WHERE p.comp_name='RxDataTypeCollection' AND c.comp_name=?",
        (name,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    try:
        r = RxGeneric.from_bytes(bytes(row[0]))
    except Exception:
        return None
    cur.execute(
        "SELECT record_string FROM comments WHERE parent=? AND member_ref=0 "
        f"AND record_type=1 AND tag_reference='' AND {_USER_COMMENT} LIMIT 1",
        ((r.comment_id * 0x10000) + r.cip_type,),
    )
    desc_row = cur.fetchone()
    return desc_row[0] if desc_row and desc_row[0] else None


@dataclass
class AoiBuilder(L5xElementBuilder):
    def _routines(self) -> List[Routine]:
        routines: List[Routine] = []
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id=? AND comp_name='RxRoutineCollection'",
            (self._object_id,),
        )
        routine_coll_row = self._cur.fetchone()
        if routine_coll_row:
            self._cur.execute(
                "SELECT object_id FROM comps WHERE parent_id=?", (routine_coll_row[0],)
            )
            for (child_oid,) in self._cur.fetchall():
                try:
                    routines.append(RoutineBuilder(self._cur, child_oid, _notes=self._notes).build())
                except Exception:
                    pass
        return routines

    def _build_protected(self, name: str) -> AOI:
        # Only the name, the description (via the backing data type) and the
        # routine names and types are stored readably. Revision, vendor,
        # dates, help text and the parameter/local-tag split are encrypted,
        # so they are left out rather than guessed.
        self._notes.add(
            f"Add-On Instruction '{name}' is source-protected: its revision, vendor, "
            "dates, help text, parameters and local tags are encrypted and were left "
            "out; only its name, description and routine names were exported"
        )
        return AOI(
            name, name, None, None, None,
            None, None, None,
            None, None, None, None, None,
            None, None, self._routines(),
            _aoi_data_type_description(self._cur, name),
            "",
        )

    def build(self) -> AOI:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        aoi_record = bytes(results[0][3])
        name = results[0][0]
        try:
            _r_aoi = RxGeneric.from_bytes(aoi_record, allow_protected=True)
        except Exception:
            _r_aoi = None
        if _r_aoi is not None and _r_aoi.protected:
            return self._build_protected(name)

        # --- Revision (major.minor) from ext[0x01] ---
        e01 = (_r_aoi.attribute(0x01) or b"") if _r_aoi is not None else b""
        rev_major = struct.unpack_from("<H", e01, 0x1A)[0] if len(e01) > 0x1B else 1
        rev_minor = struct.unpack_from("<H", e01, 0x1C)[0] if len(e01) > 0x1D else 0
        revision = f"{rev_major}.{rev_minor}"

        # --- Vendor from comps record ---
        vlen = struct.unpack_from("<H", aoi_record, 0xA6)[0] if len(aoi_record) > 0xA8 else 0
        vendor: Union[str, None] = aoi_record[0xA8:0xA8+vlen].decode("utf-8", errors="replace") if vlen > 0 else None

        # --- Metadata from large nameless record ---
        self._cur.execute(
            "SELECT record FROM nameless WHERE parent_id=" + str(self._object_id)
            + " ORDER BY LENGTH(record) DESC LIMIT 1"
        )
        nameless_row = self._cur.fetchone()
        if nameless_row and len(bytes(nameless_row[0])) > 50:
            meta = _parse_aoi_nameless(bytes(nameless_row[0]))
        else:
            meta = {"created_by": "", "created_date": "", "edited_by": "", "edited_date": "",
                    "software_revision": "", "revision_extension": None}

        parameters: List[Parameter] = []
        local_tags: List[LocalTag] = []

        # --- Extract Parameters and LocalTags from RxTagCollection ---
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        tag_coll_row = self._cur.fetchone()
        if tag_coll_row:
            tag_coll_oid = tag_coll_row[0]
            self._cur.execute(
                "SELECT object_id, record FROM comps WHERE parent_id="
                + str(tag_coll_oid)
                + " AND record_type != 512"
                + " ORDER BY seq_number"
            )
            for child_oid, child_rec in self._cur.fetchall():
                child_rec = bytes(child_rec)
                # Determine whether this is a parameter or a local tag by inspecting
                # ext01[0x20E]: bits 0x04 (Input) or 0x08 (Output) indicate a parameter.
                is_param = False
                try:
                    r_child = RxGeneric.from_bytes(child_rec)
                    exts_child = r_child.attributes
                    ext01 = exts_child.get(0x01, b"")
                    flags = _aoi_tag_usage_flags(ext01)
                    is_param = bool(flags & 0x0C)
                except Exception:
                    pass

                if is_param:
                    try:
                        parameters.append(ParameterBuilder(self._cur, child_oid).build())
                    except Exception:
                        pass
                else:
                    try:
                        local_tags.append(LocalTagBuilder(self._cur, child_oid).build())
                    except Exception:
                        pass

        routines = self._routines()

        # --- Description + RevisionNote ---
        aoi_description: Union[str, None] = None
        revision_note = ""
        if _r_aoi is not None:
            aoi_comment_parent = (_r_aoi.comment_id * 0x10000) + _r_aoi.cip_type
            self._cur.execute(
                f"SELECT record_string FROM comments WHERE parent=? AND member_ref=0 AND {_USER_COMMENT} LIMIT 1",
                (aoi_comment_parent,),
            )
            desc_row = self._cur.fetchone()
            if desc_row and desc_row[0]:
                aoi_description = desc_row[0]
            try:
                self._cur.execute(
                    "SELECT record_string FROM comments WHERE parent=? AND tag_reference='__REVISION_NOTE__' "
                    "ORDER BY rowid LIMIT 1",
                    (aoi_comment_parent,),
                )
                rn_row = self._cur.fetchone()
                if rn_row:
                    revision_note = rn_row[0] or ""
            except Exception:
                pass

        return AOI(
            name, name, revision,
            meta["revision_extension"],
            vendor,
            "false", "false", "false",
            meta["created_date"], meta["created_by"],
            meta["edited_date"], meta["edited_by"],
            meta["software_revision"],
            parameters, local_tags, routines,
            aoi_description,
            revision_note,
        )


# Comps record type of tags Logix creates itself (__l<hash>, __SHADOW_<id>,
# __DEFVAL_<id>, __CONTAINER); user tags are record type 256.
_SYSTEM_TAG_RECORD_TYPE = 264


@dataclass
class ProgramBuilder(L5xElementBuilder):
    _data_types_map: Dict[str, "DataType"] = field(default_factory=dict)
    _redundancy_enabled: bool = field(default=False)
    _value_types_map: Dict[str, "DataType"] = field(default_factory=dict)
    # The program's parsed record, when the caller already has it.
    _record: Any = field(default=None)

    def build(self) -> Program:
        self._cur.execute(
            "SELECT comp_name, record_type, record FROM comps WHERE object_id=?",
            (self._object_id,),
        )
        row = self._cur.fetchone()
        if row is None:
            raise ValueError(f"program object_id={self._object_id} was not found")

        name, record_type, record = row
        r = self._record if self._record is not None else RxGeneric.from_bytes(record)

        # --- MainRoutineName and FaultRoutineName from extended records ---
        # ext[0x12D] = MainRoutine object_id, ext[0x066] = FaultRoutine object_id
        exts = r.attributes
        main_routine_name: Union[str, None] = None
        fault_routine_name: Union[str, None] = None
        if 0x12D in exts and len(exts[0x12D]) >= 4:
            main_oid = struct.unpack_from("<I", exts[0x12D], 0)[0]
            if main_oid:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(main_oid))
                row = self._cur.fetchone()
                main_routine_name = row[0] if row else None
        if 0x066 in exts and len(exts[0x066]) >= 4:
            fault_oid = struct.unpack_from("<I", exts[0x066], 0)[0]
            if fault_oid:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(fault_oid))
                row = self._cur.fetchone()
                fault_routine_name = row[0] if row else None

        # --- Disabled flag from ext[0x01] at offset 0x24 ---
        # A u32 of 0xFFFFFFFF means the program is disabled; 0x00000000 means enabled.
        ext01 = exts.get(0x01, b"")
        disabled_flag = (
            struct.unpack_from("<I", ext01, 0x24)[0] != 0
            if len(ext01) >= 0x28
            else False
        )
        disabled = "true" if disabled_flag else "false"

        collection_id = _required_program_collection(
            self._cur,
            name,
            self._object_id,
            record_type,
            "RxRoutineCollection",
        )

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(collection_id)
        )
        routine_results = self._cur.fetchall()

        routines = []
        for child in routine_results:
            routines.append(RoutineBuilder(self._cur, child[1], _notes=self._notes).build())

        # Get the Program Scoped Tags
        tag_collection_id = _required_program_collection(
            self._cur,
            name,
            self._object_id,
            record_type,
            "RxTagCollection",
        )

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(tag_collection_id)
        )
        results = self._cur.fetchall()
        # A program output parameter is stored twice: the tag itself, renamed
        # __INTERNAL_<name>, and a "__SHADOW_<its object id>" record that
        # carries the public name. Rockwell exports only the shadow.
        shadowed = {
            int(row[0][len("__SHADOW_"):], 16)
            for row in results
            if re.fullmatch(r"__SHADOW_[0-9A-Fa-f]{8}", row[0])
        }
        tags: List[Tag] = []
        for result in results:
            if result[1] in shadowed:
                continue
            tag = TagBuilder(self._cur, result[1], _notes=self._notes).build()
            if result[3] == _SYSTEM_TAG_RECORD_TYPE and tag.name == result[0]:
                # A system-created tag (e.g. __l<hash>) under its own system
                # name; only shadows (which carry a user name) are exported.
                continue
            tag._data_types_map = self._data_types_map
            tag._value_types_map = self._value_types_map
            tags.append(tag)

        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent="
            + str((r.comment_id * 0x10000) + r.cip_type)
            + f" AND {_USER_COMMENT} ORDER BY rowid"
        )
        comment_results = self._cur.fetchall()

        # SynchronizeRedundancyDataAfterExecution: present only for redundant controllers.
        # The binary does not expose a per-program flag for this attribute — it is implicit
        # for all programs in a redundant controller project.
        sync_redundancy = "true" if self._redundancy_enabled else None

        return Program(name, name, "false", main_routine_name, fault_routine_name,
                       disabled, sync_redundancy, "false", tags, routines)


_TASK_TYPE_MAP = {1: "EVENT", 2: "PERIODIC", 4: "CONTINUOUS"}


def _program_schedule_id(parsed: RxRecord) -> int:
    """A program's task-schedule id: attribute 0x01 offset 267 in legacy
    records, the Rx comment id in current ones."""
    if isinstance(parsed, LegacyRxGeneric):
        metadata = parsed.attribute(0x01) or b""
        if len(metadata) < 271:
            raise ValueError("Program record does not contain a schedule ID")
        return struct.unpack_from("<I", metadata, 267)[0]
    return parsed.comment_id


@dataclass
class TaskBuilder(L5xElementBuilder):
    def build(self, comment_id_to_program: Dict[int, str]) -> Optional[Task]:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name, record = row[0], row[1]

        # All task config fields live within ext[0x01]; offsets below are
        # relative to that attribute's value (reverse-engineered from
        # CIPDemo_RevEng.ACD, where it starts at record offset 0x5A). The
        # current layout's attribute is larger than 0x10D4 bytes; the legacy
        # layout's is at least 806.
        try:
            parsed = RxGeneric.from_bytes(record)
        except Exception:
            parsed = None
        task_data = (parsed.attribute(0x01) or b"") if parsed is not None else b""
        if not isinstance(parsed, LegacyRxGeneric) and len(task_data) > 0x10D4:
            current_offsets = True
        elif len(record) > 0x112E:
            # Any other record this large (unparseable, legacy, or with a short
            # attribute 0x01) is read as laid out in the reference file; a
            # mismatch shows up as an implausible scheduled-program count below.
            task_data = bytes(record)[0x5A:]
            current_offsets = True
        else:
            current_offsets = False
        if current_offsets:
            rate_offset = 0x1012
            type_offset = 0x109C
            priority_offset = 0x109E
            watchdog_offset = 0x10B0
            disable_update = task_data[0x10D4]
        elif parsed is None:
            self._notes.warn(
                f"Task '{name}': record could not be read; the task is left "
                "out and its programs are unscheduled",
                f"object_id={self._object_id}",
            )
            return None
        elif len(task_data) < 806:
            raise ValueError("Task record does not contain configuration data")
        else:
            rate_offset = 514
            type_offset = 652
            priority_offset = 654
            watchdog_offset = 802
            disable_update = 0

        rate_us = struct.unpack_from("<I", task_data, rate_offset)[0]
        type_val = struct.unpack_from("<H", task_data, type_offset)[0]
        priority = struct.unpack_from("<H", task_data, priority_offset)[0]
        watchdog_us = struct.unpack_from("<I", task_data, watchdog_offset)[0]

        task_type = _TASK_TYPE_MAP.get(type_val, "PERIODIC")
        rate_str = str(rate_us // 1000) if task_type != "CONTINUOUS" else None

        # Scheduled programs open attribute 0x01: a u16 count followed by
        # that many u32 program schedule ids.
        prog_count = struct.unpack_from("<H", task_data, 0)[0]
        max_program_count = (rate_offset - 2) // 4
        scheduled_programs = []
        if prog_count > max_program_count:
            # The record does not match a known task layout; keep the task
            # but drop the unreadable schedule list.
            self._notes.warn(
                f"Task '{name}': scheduled-program list could not be read; "
                "exported with an empty list",
                f"scheduled-program count {prog_count} > max {max_program_count}",
            )
        else:
            for i in range(prog_count):
                cid = struct.unpack_from("<I", task_data, 2 + i * 4)[0]
                prog_name = comment_id_to_program.get(cid)
                if prog_name:
                    scheduled_programs.append(ScheduledProgram(prog_name, prog_name))

        event_info = None
        if task_type == "EVENT":
            event_info = EventInfo("EventInfo", "EVENT Instruction Only", "false")

        return Task(
            name,
            name,
            task_type,
            rate_str,
            str(priority),
            str(watchdog_us // 1000),
            "true" if disable_update else "false",
            "false",
            event_info,
            scheduled_programs,
        )


@dataclass
class ControllerBuilder(L5xElementBuilder):
    # Optional richer CIP-identity -> catalog-number table for resolving module
    # CatalogNumbers and the controller ProcessorType. None -> built-in only.
    _catalog_table: Union[Dict[Tuple[int, int, int], str], None] = None
    # Type layouts from the project's TagInfo.XML (see taginfo_layouts).
    _taginfo_layouts: Dict[str, "DataType"] = field(default_factory=dict)

    def _programs(
        self,
        collection_id: int,
        data_types_map: Dict[str, "DataType"],
        value_types_map: Dict[str, "DataType"],
        redundancy_enabled: bool,
    ) -> List[Program]:
        """The collection's programs. A program record can have a corrupt
        attribute length partway through; it becomes an empty placeholder
        rather than failing the project."""
        programs: List[Program] = []
        for pname, program_oid, _, rec in _get_program_records(self._cur, collection_id):
            try:
                programs.append(
                    ProgramBuilder(
                        self._cur, program_oid, _data_types_map=data_types_map,
                        _redundancy_enabled=redundancy_enabled,
                        _value_types_map=value_types_map,
                        _record=RxGeneric.from_bytes(rec), _notes=self._notes,
                    ).build()
                )
            except Exception as e:
                self._notes.warn(
                    f"Program '{pname}': could not be read; exported as an "
                    "empty placeholder",
                    f"object_id={program_oid}, {type(e).__name__}: {e}",
                )
                programs.append(
                    Program(pname, pname, "false", None, None,
                             "false", None, "false", [], [])
                )
        return programs

    def _program_schedule_ids(self, collection_id: int) -> Dict[int, str]:
        """Task schedule ID -> program name, over every child of the program
        collection. A program whose ID can't be read is still exported; it
        just isn't listed in any task's schedule."""
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE parent_id=?", (collection_id,)
        )
        schedule_ids: Dict[int, str] = {}
        for pname, rec in self._cur.fetchall():
            try:
                schedule_ids[_program_schedule_id(RxGeneric.from_bytes(rec))] = pname
            except Exception as e:
                self._notes.warn(
                    f"Program '{pname}': task-schedule ID could not be read; it "
                    "won't appear in any task's scheduled-program list",
                    f"{type(e).__name__}: {e}",
                )
        return schedule_ids

    def build(self) -> Controller:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type, record FROM comps WHERE parent_id=0 AND record_type=256"
        )
        results = self._cur.fetchall()
        if len(results) != 1:
            raise Exception("Does not contain exactly one root controller node")

        r = RxGeneric.from_bytes(results[0][4])
        description = _own_description(
            self._cur,
            (r.comment_id * 0x10000) + r.cip_type,
            _legacy_comment_keys(self._cur, results[0][1])[0]
            if isinstance(r, LegacyRxGeneric) else None,
            0,
        )
        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent="
            + str((r.comment_id * 0x10000) + r.cip_type)
            + f" AND {_USER_COMMENT} ORDER BY rowid"
        )
        comment_results = self._cur.fetchall()

        extended_records = r.attributes

        def _decode_utf16(key):
            raw = extended_records.get(key)
            if raw is None or len(raw) < 2:
                return ""
            return bytes(raw[:-2]).decode("utf-16", errors="replace")

        sfc_execution_control = _decode_utf16(0x6F)
        sfc_restart_position = _decode_utf16(0x70)
        sfc_last_scan = _decode_utf16(0x71)

        # CommPath (key 0x06A), utf-16; usually the record's last attribute.
        _comm_path_prefix: Union[str, None] = None
        if 0x06A in extended_records:
            _cp_str = extended_records[0x06A].decode("utf-16-le", errors="replace").rstrip("\x00")
            if _cp_str:
                _comm_path_prefix = _cp_str

        if 0x75 in extended_records:
            sn_raw = hex(struct.unpack("<I", extended_records[0x75])[0])[2:].zfill(8)
            project_sn = f"16#{sn_raw[:4]}_{sn_raw[4:]}"
        else:
            project_sn = "Unknown"

        raw_modified_date = struct.unpack("<Q", extended_records[0x66])[0] / 10000000
        last_modified_date = (
            datetime(1601, 1, 1) + timedelta(seconds=raw_modified_date)
        ).strftime("%a %b %d %H:%M:%S %Y")

        raw_created_date = struct.unpack("<Q", extended_records[0x65])[0] / 10000000
        project_creation_date = (
            datetime(1601, 1, 1) + timedelta(seconds=raw_created_date)
        ).strftime("%a %b %d %H:%M:%S %Y")

        # MajorRev and MinorRev: derived later from the root controller module (see below)

        # MajorFaultProgram from ext[0x068] OID → comp_name lookup
        major_fault_program: Union[str, None] = None
        if 0x068 in extended_records and len(extended_records[0x068]) >= 4:
            mfp_oid = struct.unpack_from("<I", extended_records[0x068])[0]
            if mfp_oid and mfp_oid != 0xFFFFFFFF:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(mfp_oid))
                mfp_row = self._cur.fetchone()
                major_fault_program = mfp_row[0] if mfp_row else None

        # RedundancyEnabled: controller extended record 0x001, byte offset 0x0E.
        # This byte is 0x01 for redundant controllers (e.g. 1756-L85E in redundancy mode)
        # and 0x00 for non-redundant controllers.
        _ctrl_ext001 = extended_records.get(0x001, b"")
        redundancy_enabled: bool = bool(_ctrl_ext001[0x0E]) if len(_ctrl_ext001) > 0x0E else False

        self._object_id = results[0][1]
        controller_name = results[0][0]

        # Get the data types
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxDataTypeCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller data type collection")

        _data_type_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_data_type_id)
        )
        results = self._cur.fetchall()

        data_types: List[DataType] = []
        # all_data_types_map includes ProductDefined types (excluded from L5X output but
        # needed for generating Decorated XML for tags that reference those types).
        all_data_types_map: Dict[str, DataType] = {}
        for result in results:
            _data_type_object_id = result[1]
            dt = DataTypeBuilder(self._cur, _data_type_object_id).build()
            all_data_types_map[dt.name.upper()] = dt
            if dt.cls == "User":
                data_types.append(dt)

        # data_types_map: case-insensitive name → DataType for all types (User + ProductDefined).
        # Used by Tag.to_xml() when generating Decorated XML.
        data_types_map: Dict[str, DataType] = all_data_types_map
        value_types_map = merge_value_types(data_types_map, self._taginfo_layouts)

        # Get the Controller Scoped Tags
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller tag collection")
        _tag_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_tag_collection_object_id)
        )
        results = self._cur.fetchall()
        tags: List[Tag] = []
        # Tags that parse are exported; a tag this builder cannot faithfully
        # reconstruct (an unexpected descriptor layout for this firmware, a
        # dangling reference) is recorded and skipped rather than aborting the
        # whole controller export. This keeps "emit L5X for the readable
        # objects" honest: everything readable comes out, and what could not be
        # rendered becomes a conversion note and is listed in _skipped_tags.
        skipped_tags: List[Dict[str, object]] = []
        for result in results:
            _tag_object_id = result[1]
            tag_name = result[0]
            try:
                tag = TagBuilder(self._cur, _tag_object_id, _notes=self._notes).build()
            except ValueError as exc:
                self._notes.warn(
                    f"Tag '{tag_name}' could not be read and was left out",
                    f"object {_tag_object_id}: {exc}",
                )
                skipped_tags.append(
                    {
                        "name": tag_name,
                        "object_id": _tag_object_id,
                        "reason": str(exc),
                    }
                )
                continue
            tag._data_types_map = data_types_map
            tag._value_types_map = value_types_map
            if (
                (tag.data_type or tag.tag_type == "Alias")
                and not tag.name.startswith("$")
                and ":" not in tag.name
                and not tag.name.startswith("__")
            ):
                tags.append(tag)

        # Get the Program Collection and get the programs
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxProgramCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller program collection")

        _program_collection_object_id = results[0][1]
        programs = self._programs(
            _program_collection_object_id, data_types_map, value_types_map, redundancy_enabled
        )
        comment_id_to_program = self._program_schedule_ids(_program_collection_object_id)

        # Get the Task Collection and build Tasks
        self._cur.execute(
            "SELECT comp_name, object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTaskCollection'"
        )
        task_coll_results = self._cur.fetchall()
        tasks: List[Task] = []
        if task_coll_results:
            _task_collection_object_id = task_coll_results[0][1]
            self._cur.execute(
                "SELECT comp_name, object_id FROM comps WHERE parent_id="
                + str(_task_collection_object_id)
                + " AND record_type=256"
            )
            for task_result in self._cur.fetchall():
                task = TaskBuilder(self._cur, task_result[1], _notes=self._notes).build(comment_id_to_program)
                if task is not None:
                    tasks.append(task)

        # Get the AOI Collection and get the AOIs
        results = _get_aoi_records(self._cur, self._object_id)
        aois: List[AOI] = []
        for result in results:
            _aoi_object_id = result[1]
            aois.append(AoiBuilder(self._cur, _aoi_object_id, _notes=self._notes).build())

        # Get the Module (IO) Collection and build all Module elements.
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxMapDeviceCollection'"
        )
        row = self._cur.fetchone()
        if row is None:
            modules: List[Module] = []
        else:
            coll_oid = row[0]
            self._cur.execute(
                "SELECT comp_name, object_id, record FROM comps WHERE parent_id="
                + str(coll_oid)
                + " ORDER BY seq_number"
            )
            mod_rows = self._cur.fetchall()

            # First pass: build modid→name map so child modules can resolve their parent name.
            from acd.record.rx import RxGeneric as _RxG
            modid_to_name: Dict[int, str] = {}
            for db_name, mod_oid, mod_rec in mod_rows:
                display_name = "?" if (db_name.startswith("$") and db_name.endswith("$")) else db_name
                try:
                    r = _RxG.from_bytes(bytes(mod_rec))
                    if r.cip_type == 0x69:
                        exts = r.attributes
                        e1 = exts.get(0x001, b"")
                        if len(e1) >= 0x30:
                            modid = struct.unpack("<I", e1[0x2C:0x30])[0]
                            modid_to_name[modid] = display_name
                except Exception:
                    pass

            # Second pass: build Module objects.
            modules = []
            for _, mod_oid, _ in mod_rows:
                modules.append(
                    ModuleBuilder(
                        self._cur, mod_oid, _modid_to_name=modid_to_name,
                        _catalog_table=self._catalog_table,
                    ).build()
                )

            # Third pass: compute (parent_name, parent_port_id) → child count,
            # then inject the relevant sub-dict into each Module as _port_child_counts.
            child_counts: Dict[tuple, int] = {}
            for m in modules:
                k = (m.parent_module, m.parent_mod_port_id)
                child_counts[k] = child_counts.get(k, 0) + 1
            for m in modules:
                m._port_child_counts = {
                    port_id: child_counts.get((m.name, port_id), 0)
                    for port_id in range(1, 20)
                    if (m.name, port_id) in child_counts
                }

        # ProcessorType is the CatalogNumber of the root controller module (the one
        # whose parent is itself, i.e. MajorFault="true").
        #
        # A3 finding (Studio 5000 v35 import): Studio reads ProcessorType to identify
        # the CPU. A real part number (e.g. "1756-L85E") imports cleanly. For an
        # out-of-table CPU the value is the CIP-triple placeholder ("CIP-1-14-216"),
        # which Studio does NOT recognise as a catalog, so it shows a "Change
        # Controller Type" dialog (observed: defaulted to 1756-L1/5550) -- the user
        # then picks the real CPU (e.g. 1756-L82E) and the import completes.
        #
        # We DELIBERATELY keep emitting the placeholder here (NOT omitting it):
        # omitting ProcessorType makes Studio unable to create the project file at
        # all ("Failed to create project file ... Couldn't be found", Error
        # 716-80042001) -- a worse outcome than the prompt. The clean fix for an
        # out-of-table CPU is to cover its identity in the external catalog (so
        # catalog_number_for_identity resolves to a real number like "1756-L82");
        # until then the placeholder degrades to a manual "Change Controller Type"
        # step, which works.
        # NOTE: the ProcessorType is derived from the CPU module's catalog_number
        # (resolved above via ModuleBuilder with self._catalog_table), so a richer
        # catalog (e.g. an external catalog merged over the built-in) flows through
        # here automatically: an out-of-table CPU that IS in the external catalog
        # gets a real ProcessorType (clean Studio import); one that is NOT still
        # gets the CIP-... placeholder (the manual "Change Controller Type" case).
        processor_type = next(
            (m.catalog_number for m in modules if m.major_fault == "true" and m.catalog_number),
            None,
        )

        # MajorRev and MinorRev come from the firmware version of the Local (backplane
        # controller) module, stored in its ext[0x01] bytes [0x08] and [0x09].
        local_module = next(
            (m for m in modules if m.name == "Local"),
            next((m for m in modules if m.major_fault == "true"), None),
        )
        if local_module is not None:
            major_rev = str(local_module.major)
            minor_rev = str(local_module.minor)
        else:
            major_rev = "0"
            minor_rev = "0"

        # CommPath: combine the stored path prefix (ends with "\") with the controller
        # module's backplane slot number (e.g. "EthernetModule\192.168.1.10\Backplane\4").
        comm_path: Union[str, None] = None
        if _comm_path_prefix is not None and not _comm_path_prefix.endswith("\\"):
            # The stored path already ends with the controller's slot.
            comm_path = _comm_path_prefix
        elif _comm_path_prefix is not None:
            _ctrl_slot = next(
                (m._slot for m in modules if m.major_fault == "true"), None
            )
            if _ctrl_slot is not None:
                comm_path = _comm_path_prefix + str(_ctrl_slot)

        controller = Controller(
            controller_name,
            "Target",
            controller_name,
            processor_type,
            major_rev,
            minor_rev,
            major_fault_program,
            project_creation_date,
            last_modified_date,
            sfc_execution_control,
            sfc_restart_position,
            sfc_last_scan,
            comm_path,
            project_sn,
            "false",        # MatchProjectToController
            "false",        # CanUseRPIFromProducer
            "0",            # InhibitAutomaticFirmwareUpdate
            "EnabledWithAppend",  # PassThroughConfiguration
            "true",         # DownloadProjectDocumentationAndExtendedProperties
            "true",         # DownloadProjectCustomProperties
            "false",        # ReportMinorOverflow
            "false",        # AutoDiagsEnabled
            "false",        # WebServerEnabled
            data_types,
            modules,
            tags,
            programs,
            tasks,
            aois,
            redundancy_enabled,
            description,
        )
        # Diagnostics: controller-scoped tags that could not be faithfully
        # reconstructed (see the tag loop above). Empty when everything parsed.
        controller._skipped_tags = skipped_tags
        return controller


@dataclass
class ProjectBuilder:
    quick_info_filename: PathLike

    def build(self) -> RSLogix5000Content:
        element = ET.parse(self.quick_info_filename)
        rslogix_content_element = element.find(".")
        if rslogix_content_element is not None:
            target_name = rslogix_content_element.attrib["Name"]

        schema_version_element = element.find("SchemaVersion")
        if schema_version_element is not None:
            schema_version_major = schema_version_element.attrib["Major"]
            schema_version_minor = schema_version_element.attrib["Minor"]
            schema_revision = f"{schema_version_major}.{schema_version_minor}"
        else:
            schema_revision = "1.0"

        # SWVersion reflects the Studio 5000 application version (e.g. "RSLogix 5000 v35.04"),
        # which is what RSLogix5000Content SoftwareRevision represents.  DeviceIdentity
        # MajorRevision/MinorRevision is the controller firmware version — a different value.
        sw_version_element = element.find("SWVersion")
        if sw_version_element is not None:
            sw_version_string = sw_version_element.attrib.get("String", "")
            # Extract the version number from the trailing "vXX.YY" portion.
            match = re.search(r"v(\d+\.\d+)$", sw_version_string.strip())
            if match:
                software_revision = match.group(1)
            else:
                # Unexpected format — fall back to DeviceIdentity firmware version.
                device_identity = element.find("DeviceIdentity")
                if device_identity is not None:
                    software_revision = (
                        f"{device_identity.attrib['MajorRevision']}"
                        f".{device_identity.attrib['MinorRevision']}"
                    )
                else:
                    software_revision = "33.01"
        else:
            # No SWVersion element — fall back to DeviceIdentity firmware version.
            device_identity = element.find("DeviceIdentity")
            if device_identity is not None:
                software_revision = (
                    f"{device_identity.attrib['MajorRevision']}"
                    f".{device_identity.attrib['MinorRevision']}"
                )
            else:
                software_revision = "33.01"

        target_type = "Controller"
        contains_context = "false"
        now = datetime.now()
        export_date = now.strftime("%a %b %d %H:%M:%S %Y")
        export_options = (
            "NoRawData L5KData DecoratedData ForceProtectedEncoding AllProjDocTrans"
        )
        return RSLogix5000Content(
            target_name,
            None,
            schema_revision,
            software_revision,
            target_name,
            target_type,
            contains_context,
            export_date,
            export_options,
        )


@dataclass
class DumpCompsRecords(L5xElementBuilder):
    base_directory: PathLike = Path("dump")

    def dump(self, parent_id: int = 0, log_file=None):
        self._cur.execute(
            f"SELECT comp_name, object_id, parent_id, record_type, record FROM comps WHERE parent_id={parent_id}"
        )
        results = self._cur.fetchall()

        for result in results:
            object_id = result[1]
            name = result[0]
            record = result[4]
            # comp_name may contain path-unsafe characters (e.g. ":" in channel
            # names on Windows). Use a sanitized component for the on-disk path;
            # keep the original name in the log so nothing is lost.
            safe_name = _sanitize_path_component(name)
            new_path = Path(os.path.join(self.base_directory, safe_name))
            if os.path.exists(os.path.join(new_path)):
                shutil.rmtree(os.path.join(new_path))
            if not os.path.exists(os.path.join(new_path)):
                os.makedirs(new_path)
            with open(Path(os.path.join(new_path, safe_name + ".dat")), "wb") as file:
                log_file.write(
                    f"Class - {struct.unpack_from('<H', result[4], 0xA)[0]} Instance {struct.unpack_from('<H', result[4], 0xC)[0]}- {name}\n"
                )
                file.write(record)

            DumpCompsRecords(self._cur, object_id, new_path).dump(object_id, log_file)
