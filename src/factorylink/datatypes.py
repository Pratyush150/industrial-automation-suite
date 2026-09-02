"""Primitive data-type, word-order and register-area definitions.

This module deliberately has no dependencies inside the package. Both the
Modbus codec and the tag database import from here, which keeps the codec a
pure, standalone unit that can be unit-tested without a tag database and
without a driver.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "DataType",
    "WordOrder",
    "ByteOrder",
    "RegisterArea",
    "REGISTER_COUNT",
    "MAX_READ_COUNT",
    "STRUCT_FORMAT",
    "is_bit_area",
]


class DataType(str, Enum):
    """Scalar types a tag can be decoded into."""

    BOOL = "bool"
    INT16 = "int16"
    UINT16 = "uint16"
    INT32 = "int32"
    UINT32 = "uint32"
    FLOAT32 = "float32"
    FLOAT64 = "float64"

    @classmethod
    def parse(cls, text: str) -> "DataType":
        """Parse a data type from config text, with a helpful error message."""
        key = str(text).strip().lower()
        aliases = {
            "bit": cls.BOOL,
            "boolean": cls.BOOL,
            "coil": cls.BOOL,
            "short": cls.INT16,
            "word": cls.UINT16,
            "int": cls.INT32,
            "dint": cls.INT32,
            "udint": cls.UINT32,
            "dword": cls.UINT32,
            "real": cls.FLOAT32,
            "float": cls.FLOAT32,
            "lreal": cls.FLOAT64,
            "double": cls.FLOAT64,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError:
            valid = ", ".join(sorted(m.value for m in cls))
            raise ValueError(f"unknown data type {text!r} (expected one of: {valid})") from None


class WordOrder(str, Enum):
    """Order of 16-bit registers inside a multi-register value.

    ``BIG`` means the first (lowest-addressed) register holds the most
    significant word. This is what the Modbus specification implies and what
    most European PLCs do. ``LITTLE`` means the first register holds the least
    significant word, which is what a large fraction of drives, power meters
    and Schneider/AB gateways actually put on the wire.
    """

    BIG = "big"
    LITTLE = "little"

    @classmethod
    def parse(cls, text: str) -> "WordOrder":
        key = str(text).strip().lower()
        aliases = {
            "abcd": cls.BIG,
            "cdab": cls.LITTLE,
            "hi_lo": cls.BIG,
            "lo_hi": cls.LITTLE,
            "msw_first": cls.BIG,
            "lsw_first": cls.LITTLE,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError:
            raise ValueError(
                f"unknown word order {text!r} (expected big, little, abcd or cdab)"
            ) from None


class ByteOrder(str, Enum):
    """Order of the two bytes inside a single 16-bit register.

    ``BIG`` is the normal Modbus wire order (high byte first). ``LITTLE``
    means the device swaps the bytes within each register, which shows up on
    some gateways and on devices that memcpy a little-endian float straight
    into their register image.
    """

    BIG = "big"
    LITTLE = "little"

    @classmethod
    def parse(cls, text: str) -> "ByteOrder":
        key = str(text).strip().lower()
        aliases = {"swapped": cls.LITTLE, "normal": cls.BIG}
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError:
            raise ValueError(f"unknown byte order {text!r} (expected big or little)") from None


class RegisterArea(str, Enum):
    """The four classic Modbus address spaces.

    They are separate address spaces: holding register 40 and coil 40 are
    unrelated storage. Merging a read across two areas is always a bug, so the
    coalescer treats the area as part of the block identity.
    """

    HOLDING = "holding"
    INPUT = "input"
    COIL = "coil"
    DISCRETE = "discrete"

    @classmethod
    def parse(cls, text: str) -> "RegisterArea":
        key = str(text).strip().lower()
        aliases = {
            "hr": cls.HOLDING,
            "holding_register": cls.HOLDING,
            "ir": cls.INPUT,
            "input_register": cls.INPUT,
            "co": cls.COIL,
            "coils": cls.COIL,
            "di": cls.DISCRETE,
            "discrete_input": cls.DISCRETE,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError:
            valid = ", ".join(sorted(m.value for m in cls))
            raise ValueError(f"unknown register area {text!r} (expected one of: {valid})") from None


#: Number of 16-bit registers each data type occupies.
REGISTER_COUNT: dict[DataType, int] = {
    DataType.BOOL: 1,
    DataType.INT16: 1,
    DataType.UINT16: 1,
    DataType.INT32: 2,
    DataType.UINT32: 2,
    DataType.FLOAT32: 2,
    DataType.FLOAT64: 4,
}

#: Big-endian struct format character for each type, applied after the
#: registers have been reordered into canonical big-endian byte order.
STRUCT_FORMAT: dict[DataType, str] = {
    DataType.INT16: ">h",
    DataType.UINT16: ">H",
    DataType.INT32: ">i",
    DataType.UINT32: ">I",
    DataType.FLOAT32: ">f",
    DataType.FLOAT64: ">d",
}

#: Protocol limits per area. A Modbus/TCP response carries at most 253 bytes of
#: PDU, which caps a register read at 125 registers and a bit read at 2000 bits.
#: Exceeding them does not produce a helpful error from most PLCs -- you get an
#: illegal-data-address exception, or a timeout, or a truncated frame.
MAX_READ_COUNT: dict[RegisterArea, int] = {
    RegisterArea.HOLDING: 125,
    RegisterArea.INPUT: 125,
    RegisterArea.COIL: 2000,
    RegisterArea.DISCRETE: 2000,
}


def is_bit_area(area: RegisterArea) -> bool:
    """Return True for the two bit-addressed areas (coils, discrete inputs)."""
    return area in (RegisterArea.COIL, RegisterArea.DISCRETE)
