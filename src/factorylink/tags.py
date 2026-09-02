"""Tag database: the single source of truth for what lives at which address.

A tag database is the thing that turns "holding register 42, two words, little
word order, multiply by 0.1" into "fill_temperature, 63.4 degC". Getting it
into one validated file -- instead of scattered across driver calls -- is what
makes the rest of the system testable.

Supported sources: YAML (needs PyYAML), CSV (stdlib) and plain dicts. The
validator is deliberately noisy: it collects *every* problem and reports them
together, because fixing address maps one error per run is miserable.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .datatypes import (
    MAX_READ_COUNT,
    REGISTER_COUNT,
    ByteOrder,
    DataType,
    RegisterArea,
    WordOrder,
    is_bit_area,
)
from .protocols.modbus_codec import apply_scaling, remove_scaling

__all__ = [
    "TagError",
    "TagValidationError",
    "AlarmLimits",
    "TagDef",
    "TagDatabase",
]

_TRUE_STRINGS = {"1", "true", "yes", "y", "on"}
_FALSE_STRINGS = {"0", "false", "no", "n", "off", ""}


class TagError(Exception):
    """Base class for tag database problems."""


class TagValidationError(TagError):
    """Raised when a tag database fails validation.

    Carries every individual problem in :attr:`problems` so an operator can fix
    the whole file in one pass.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        joined = "\n  - ".join(self.problems)
        super().__init__(f"{len(self.problems)} problem(s) in tag database:\n  - {joined}")


def _as_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    raise TagError(f"{field_name}: cannot read {value!r} as a boolean")


def _as_float(value: Any, field_name: str, default: float | None = None) -> float | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise TagError(f"{field_name}: cannot read {value!r} as a number") from None


def _req_float(value: Any, field_name: str, default: float) -> float:
    """Parse a float that always has a value.

    Kept separate from :func:`_as_float` because ``x or default`` silently
    turns a legitimate 0 into the default, which is how a scale of 0 slips
    through validation.
    """
    parsed = _as_float(value, field_name, default)
    return default if parsed is None else float(parsed)


def _as_int(value: Any, field_name: str, default: int | None = None) -> int | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        raise TagError(f"{field_name}: cannot read {value!r} as an integer") from None


@dataclass(frozen=True)
class AlarmLimits:
    """Static alarm limits carried on the tag, in engineering units."""

    hi_hi: float | None = None
    hi: float | None = None
    lo: float | None = None
    lo_lo: float | None = None
    deadband: float = 0.0

    def is_empty(self) -> bool:
        """True when no limit is configured."""
        return all(v is None for v in (self.hi_hi, self.hi, self.lo, self.lo_lo))

    def ordered_problems(self, tag_name: str) -> list[str]:
        """Return every ordering problem in these limits."""
        problems: list[str] = []
        ladder = [
            ("lo_lo", self.lo_lo),
            ("lo", self.lo),
            ("hi", self.hi),
            ("hi_hi", self.hi_hi),
        ]
        present = [(name, value) for name, value in ladder if value is not None]
        for (a_name, a_val), (b_name, b_val) in zip(present, present[1:]):
            if a_val > b_val:
                problems.append(
                    f"{tag_name}: alarm limits out of order, {a_name}={a_val} "
                    f"is above {b_name}={b_val}"
                )
        if self.deadband < 0:
            problems.append(f"{tag_name}: alarm deadband {self.deadband} is negative")
        return problems


@dataclass(frozen=True)
class TagDef:
    """One addressable point on one device.

    Attributes map directly onto the config file fields. ``address`` is the
    zero-based protocol address, not the 4xxxx-style documentation address --
    see docs/FIELD_NOTES.md for why that off-by-one eats an afternoon.
    """

    name: str
    address: int
    data_type: DataType = DataType.UINT16
    device: str = "plc1"
    area: RegisterArea = RegisterArea.HOLDING
    word_order: WordOrder = WordOrder.BIG
    byte_order: ByteOrder = ByteOrder.BIG
    bit: int | None = None
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    deadband: float = 0.0
    poll_group: str = "default"
    writable: bool = False
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""
    alarm: AlarmLimits = field(default_factory=AlarmLimits)

    @property
    def register_count(self) -> int:
        """Registers (or bits) this tag occupies."""
        if is_bit_area(self.area):
            return 1
        return REGISTER_COUNT[self.data_type]

    @property
    def end_address(self) -> int:
        """Last address inclusive."""
        return self.address + self.register_count - 1

    @property
    def key(self) -> tuple[str, RegisterArea]:
        """Identity of the address space this tag lives in."""
        return (self.device, self.area)

    @property
    def is_bit(self) -> bool:
        """True when this tag resolves to a single boolean."""
        return is_bit_area(self.area) or self.data_type is DataType.BOOL

    def to_engineering(self, raw: float | int | bool) -> float | bool:
        """Convert a decoded raw value into engineering units."""
        if isinstance(raw, bool):
            return raw
        return apply_scaling(float(raw), self.scale, self.offset)

    def to_raw(self, engineering: float | bool) -> float | bool:
        """Convert an engineering value back into a raw register value."""
        if isinstance(engineering, bool):
            return engineering
        return remove_scaling(float(engineering), self.scale, self.offset)

    def in_range(self, engineering: float) -> bool:
        """True when ``engineering`` sits inside the configured tag range."""
        if self.min_value is not None and engineering < self.min_value:
            return False
        if self.max_value is not None and engineering > self.max_value:
            return False
        return True

    def clamp(self, engineering: float) -> float:
        """Clamp ``engineering`` into the configured range."""
        value = float(engineering)
        if self.min_value is not None:
            value = max(self.min_value, value)
        if self.max_value is not None:
            value = min(self.max_value, value)
        return value

    def format_value(self, value: float | bool | None) -> str:
        """Human-readable rendering used by the CLI table and dashboard."""
        if value is None:
            return "--"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        if text in ("", "-"):
            text = "0"
        return f"{text} {self.unit}".strip()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TagDef":
        """Build a tag from a config mapping, raising :class:`TagError`."""
        data = {str(k).strip().lower(): v for k, v in raw.items() if str(k).strip() != ""}
        name = str(data.get("name", "")).strip()
        if not name:
            raise TagError("tag is missing a name")
        try:
            address = _as_int(data.get("address"), f"{name}.address")
        except TagError as exc:
            raise TagError(str(exc)) from None
        if address is None:
            raise TagError(f"{name}: missing address")

        area = RegisterArea.parse(data.get("area", "holding"))
        if is_bit_area(area):
            data_type = DataType.BOOL
        else:
            data_type = DataType.parse(data.get("data_type", data.get("type", "uint16")))

        alarm_raw = data.get("alarm") or {}
        if not isinstance(alarm_raw, Mapping):
            raise TagError(f"{name}: alarm section must be a mapping")
        alarm = AlarmLimits(
            hi_hi=_as_float(alarm_raw.get("hi_hi", data.get("alarm_hi_hi")), f"{name}.hi_hi"),
            hi=_as_float(alarm_raw.get("hi", data.get("alarm_hi")), f"{name}.hi"),
            lo=_as_float(alarm_raw.get("lo", data.get("alarm_lo")), f"{name}.lo"),
            lo_lo=_as_float(alarm_raw.get("lo_lo", data.get("alarm_lo_lo")), f"{name}.lo_lo"),
            deadband=_req_float(
                alarm_raw.get("deadband", data.get("alarm_deadband")),
                f"{name}.alarm_deadband",
                0.0,
            ),
        )

        return cls(
            name=name,
            address=address,
            data_type=data_type,
            device=str(data.get("device", "plc1")).strip() or "plc1",
            area=area,
            word_order=WordOrder.parse(data.get("word_order", "big")),
            byte_order=ByteOrder.parse(data.get("byte_order", "big")),
            bit=_as_int(data.get("bit"), f"{name}.bit"),
            scale=_req_float(data.get("scale"), f"{name}.scale", 1.0),
            offset=_req_float(data.get("offset"), f"{name}.offset", 0.0),
            unit=str(data.get("unit", "") or "").strip(),
            deadband=_req_float(data.get("deadband"), f"{name}.deadband", 0.0),
            poll_group=str(data.get("poll_group", "default") or "default").strip(),
            writable=_as_bool(data.get("writable"), f"{name}.writable"),
            min_value=_as_float(data.get("min_value"), f"{name}.min_value"),
            max_value=_as_float(data.get("max_value"), f"{name}.max_value"),
            description=str(data.get("description", "") or "").strip(),
            alarm=alarm,
        )

    def to_mapping(self) -> dict[str, Any]:
        """Round-trippable dict form, used by ``dump-tags``."""
        out: dict[str, Any] = {
            "name": self.name,
            "device": self.device,
            "area": self.area.value,
            "address": self.address,
            "data_type": self.data_type.value,
            "word_order": self.word_order.value,
            "byte_order": self.byte_order.value,
            "scale": self.scale,
            "offset": self.offset,
            "unit": self.unit,
            "deadband": self.deadband,
            "poll_group": self.poll_group,
            "writable": self.writable,
            "description": self.description,
        }
        if self.bit is not None:
            out["bit"] = self.bit
        if self.min_value is not None:
            out["min_value"] = self.min_value
        if self.max_value is not None:
            out["max_value"] = self.max_value
        if not self.alarm.is_empty() or self.alarm.deadband:
            out["alarm"] = {
                k: v
                for k, v in {
                    "hi_hi": self.alarm.hi_hi,
                    "hi": self.alarm.hi,
                    "lo": self.alarm.lo,
                    "lo_lo": self.alarm.lo_lo,
                    "deadband": self.alarm.deadband or None,
                }.items()
                if v is not None
            }
        return out


class TagDatabase:
    """An ordered, validated collection of :class:`TagDef`."""

    def __init__(self, tags: Iterable[TagDef], *, validate: bool = True) -> None:
        self._tags: dict[str, TagDef] = {}
        for tag in tags:
            self._tags[tag.name] = tag
        if validate:
            self.validate()

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dicts(cls, rows: Iterable[Mapping[str, Any]], *, validate: bool = True) -> "TagDatabase":
        """Build from a list of mappings, collecting per-row parse errors."""
        tags: list[TagDef] = []
        problems: list[str] = []
        for index, row in enumerate(rows):
            try:
                tags.append(TagDef.from_mapping(row))
            except (TagError, ValueError) as exc:
                problems.append(f"row {index + 1}: {exc}")
        if problems:
            raise TagValidationError(problems)
        return cls(tags, validate=validate)

    @classmethod
    def from_yaml(cls, path: str | os.PathLike[str], *, validate: bool = True) -> "TagDatabase":
        """Load from a YAML file with a top-level ``tags:`` list."""
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - exercised only without PyYAML
            raise TagError(
                "PyYAML is not installed; use a CSV tag file or `pip install pyyaml`"
            ) from exc
        text = Path(path).read_text(encoding="utf-8")
        doc = yaml.safe_load(text) or {}
        if isinstance(doc, list):
            rows = doc
        elif isinstance(doc, Mapping):
            rows = doc.get("tags", [])
        else:
            raise TagError(f"{path}: expected a mapping with a 'tags' list")
        if not isinstance(rows, list):
            raise TagError(f"{path}: 'tags' must be a list")
        return cls.from_dicts(rows, validate=validate)

    @classmethod
    def from_csv(cls, path: str | os.PathLike[str], *, validate: bool = True) -> "TagDatabase":
        """Load from a CSV file whose header row names the tag fields."""
        with open(path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [dict(row) for row in reader]
        return cls.from_dicts(rows, validate=validate)

    @classmethod
    def load(cls, path: str | os.PathLike[str], *, validate: bool = True) -> "TagDatabase":
        """Load a tag file, choosing the parser from the file extension."""
        suffix = Path(path).suffix.lower()
        if suffix in (".yaml", ".yml"):
            return cls.from_yaml(path, validate=validate)
        if suffix == ".csv":
            return cls.from_csv(path, validate=validate)
        raise TagError(f"{path}: unsupported tag file type {suffix!r} (use .yaml, .yml or .csv)")

    # -- container protocol ----------------------------------------------

    def __len__(self) -> int:
        return len(self._tags)

    def __iter__(self) -> Iterator[TagDef]:
        return iter(self._tags.values())

    def __contains__(self, name: object) -> bool:
        return name in self._tags

    def __getitem__(self, name: str) -> TagDef:
        try:
            return self._tags[name]
        except KeyError:
            raise TagError(f"unknown tag {name!r}") from None

    def get(self, name: str, default: TagDef | None = None) -> TagDef | None:
        """Dict-style lookup that does not raise."""
        return self._tags.get(name, default)

    @property
    def names(self) -> list[str]:
        """Tag names in file order."""
        return list(self._tags)

    def tags(self) -> list[TagDef]:
        """All tags in file order."""
        return list(self._tags.values())

    # -- selection --------------------------------------------------------

    def by_group(self, group: str) -> list[TagDef]:
        """All tags in one poll group."""
        return [t for t in self._tags.values() if t.poll_group == group]

    def by_device(self, device: str) -> list[TagDef]:
        """All tags on one device."""
        return [t for t in self._tags.values() if t.device == device]

    @property
    def groups(self) -> list[str]:
        """Poll group names, in first-appearance order."""
        seen: list[str] = []
        for tag in self._tags.values():
            if tag.poll_group not in seen:
                seen.append(tag.poll_group)
        return seen

    @property
    def devices(self) -> list[str]:
        """Device names, in first-appearance order."""
        seen: list[str] = []
        for tag in self._tags.values():
            if tag.device not in seen:
                seen.append(tag.device)
        return seen

    def with_overrides(self, name: str, **changes: Any) -> "TagDatabase":
        """Return a copy of the database with one tag modified."""
        tags = [replace(t, **changes) if t.name == name else t for t in self._tags.values()]
        return TagDatabase(tags)

    # -- validation -------------------------------------------------------

    def validate(self) -> None:
        """Raise :class:`TagValidationError` listing every problem found."""
        problems: list[str] = []
        problems.extend(self._check_fields())
        problems.extend(self._check_overlaps())
        if problems:
            raise TagValidationError(problems)

    def _check_fields(self) -> list[str]:
        problems: list[str] = []
        for tag in self._tags.values():
            if tag.address < 0:
                problems.append(f"{tag.name}: address {tag.address} is negative")
            if tag.scale == 0:
                problems.append(f"{tag.name}: scale is 0, which is not invertible")
            if tag.deadband < 0:
                problems.append(f"{tag.name}: deadband {tag.deadband} is negative")
            if tag.bit is not None and not 0 <= tag.bit <= 15:
                problems.append(f"{tag.name}: bit {tag.bit} is outside 0..15")
            if tag.bit is not None and tag.data_type is not DataType.BOOL:
                problems.append(
                    f"{tag.name}: bit extraction only makes sense on a bool tag, "
                    f"got {tag.data_type.value}"
                )
            if tag.min_value is not None and tag.max_value is not None:
                if tag.min_value > tag.max_value:
                    problems.append(
                        f"{tag.name}: impossible range, min_value={tag.min_value} "
                        f"is above max_value={tag.max_value}"
                    )
            limit = MAX_READ_COUNT[tag.area]
            if tag.register_count > limit:
                problems.append(
                    f"{tag.name}: occupies {tag.register_count} units, above the "
                    f"{limit} limit for area {tag.area.value}"
                )
            problems.extend(tag.alarm.ordered_problems(tag.name))
            for label, value in (
                ("hi_hi", tag.alarm.hi_hi),
                ("hi", tag.alarm.hi),
                ("lo", tag.alarm.lo),
                ("lo_lo", tag.alarm.lo_lo),
            ):
                if value is None:
                    continue
                if not tag.in_range(value):
                    problems.append(
                        f"{tag.name}: alarm {label}={value} is outside the tag range "
                        f"[{tag.min_value}, {tag.max_value}] and can never trigger"
                    )
        return problems

    def _check_overlaps(self) -> list[str]:
        """Report tags that claim the same registers in the same address space.

        Two bit tags may legitimately share a status word, provided they name
        different bits. Everything else overlapping is a map error.
        """
        problems: list[str] = []
        spaces: dict[tuple[str, RegisterArea], list[TagDef]] = {}
        for tag in self._tags.values():
            spaces.setdefault(tag.key, []).append(tag)
        for (device, area), tags in spaces.items():
            ordered = sorted(tags, key=lambda t: (t.address, t.name))
            for i, tag in enumerate(ordered):
                for other in ordered[i + 1 :]:
                    if other.address > tag.end_address:
                        break
                    if self._bits_are_disjoint(tag, other):
                        continue
                    problems.append(
                        f"{device}/{area.value}: {tag.name} "
                        f"({tag.address}..{tag.end_address}) overlaps {other.name} "
                        f"({other.address}..{other.end_address})"
                    )
        return problems

    @staticmethod
    def _bits_are_disjoint(a: TagDef, b: TagDef) -> bool:
        if a.bit is None or b.bit is None:
            return False
        if a.address != b.address:
            return False
        return a.bit != b.bit

    # -- export -----------------------------------------------------------

    def to_csv(self) -> str:
        """Render the database as CSV text."""
        columns = [
            "name",
            "device",
            "area",
            "address",
            "bit",
            "data_type",
            "word_order",
            "byte_order",
            "scale",
            "offset",
            "unit",
            "deadband",
            "poll_group",
            "writable",
            "min_value",
            "max_value",
            "alarm_lo_lo",
            "alarm_lo",
            "alarm_hi",
            "alarm_hi_hi",
            "alarm_deadband",
            "description",
        ]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for tag in self._tags.values():
            writer.writerow(
                {
                    "name": tag.name,
                    "device": tag.device,
                    "area": tag.area.value,
                    "address": tag.address,
                    "bit": "" if tag.bit is None else tag.bit,
                    "data_type": tag.data_type.value,
                    "word_order": tag.word_order.value,
                    "byte_order": tag.byte_order.value,
                    "scale": tag.scale,
                    "offset": tag.offset,
                    "unit": tag.unit,
                    "deadband": tag.deadband,
                    "poll_group": tag.poll_group,
                    "writable": int(tag.writable),
                    "min_value": "" if tag.min_value is None else tag.min_value,
                    "max_value": "" if tag.max_value is None else tag.max_value,
                    "alarm_lo_lo": "" if tag.alarm.lo_lo is None else tag.alarm.lo_lo,
                    "alarm_lo": "" if tag.alarm.lo is None else tag.alarm.lo,
                    "alarm_hi": "" if tag.alarm.hi is None else tag.alarm.hi,
                    "alarm_hi_hi": "" if tag.alarm.hi_hi is None else tag.alarm.hi_hi,
                    "alarm_deadband": tag.alarm.deadband,
                    "description": tag.description,
                }
            )
        return buffer.getvalue()

    def to_yaml(self) -> str:
        """Render the database as YAML text (hand-written, no PyYAML needed)."""
        lines = ["tags:"]
        for tag in self._tags.values():
            mapping = tag.to_mapping()
            first = True
            for key, value in mapping.items():
                prefix = "  - " if first else "    "
                first = False
                if isinstance(value, dict):
                    lines.append(f"{prefix}{key}:")
                    for sub_key, sub_value in value.items():
                        lines.append(f"      {sub_key}: {_yaml_scalar(sub_value)}")
                else:
                    lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")
        return "\n".join(lines) + "\n"


#: Strings matching this are safe to emit unquoted in YAML.
_PLAIN_SCALAR = re.compile(r"^[A-Za-z][A-Za-z0-9 _./+-]*$")
_RESERVED_WORDS = {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "none"}


def _yaml_scalar(value: Any) -> str:
    """Render a Python value as a YAML scalar, quoting whenever in doubt."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if _PLAIN_SCALAR.match(text) and text.lower() not in _RESERVED_WORDS:
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
