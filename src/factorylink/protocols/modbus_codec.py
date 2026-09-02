"""Modbus register encoding and decoding, implemented from first principles.

Everything here is a pure function over lists of 16-bit integers. No sockets,
no library, no state. That is deliberate: word-order and byte-order confusion
is the single most common Modbus integration bug, and the only way to be sure
about it is to unit-test the transform against known byte patterns.

The model
---------
A Modbus register is an unsigned 16-bit integer. On the wire it is transmitted
high byte first. A value wider than 16 bits is spread over consecutive
registers, and the standard says nothing useful about which register holds the
most significant word. So every device makes its own choice, and there are four
combinations in the wild for a 32-bit value:

    value 1.0 as float32 = 0x3F800000

    word=big    byte=big     -> [0x3F80, 0x0000]   "ABCD"  (most common)
    word=little byte=big     -> [0x0000, 0x3F80]   "CDAB"  (very common)
    word=big    byte=little  -> [0x803F, 0x0000]   "BADC"  (gateways)
    word=little byte=little  -> [0x0000, 0x803F]   "DCBA"  (rare, but real)

Pick the wrong one and the value does not merely shift -- it becomes noise, or
a plausible-looking number that is wrong by orders of magnitude. See
docs/FIELD_NOTES.md for how to identify which one a device is using in about
two minutes.

Conventions used here
---------------------
* ``registers`` is always ordered lowest address first, exactly as returned by
  a Modbus read.
* ``WordOrder.BIG`` means the lowest-addressed register is the most
  significant word.
* ``ByteOrder.BIG`` means the normal Modbus wire order inside each register.
* Scaling is ``engineering = raw * scale + offset``.
"""

from __future__ import annotations

import struct
from typing import Sequence

from ..datatypes import REGISTER_COUNT, STRUCT_FORMAT, ByteOrder, DataType, WordOrder

__all__ = [
    "CodecError",
    "registers_to_bytes",
    "bytes_to_registers",
    "decode_registers",
    "encode_value",
    "swap_words",
    "swap_bytes",
    "bit_from_word",
    "bits_from_word",
    "word_from_bits",
    "coils_to_word",
    "word_to_coils",
    "apply_scaling",
    "remove_scaling",
    "clamp_to_type",
    "register_count",
]

_UINT16_MASK = 0xFFFF


class CodecError(ValueError):
    """Raised when registers cannot be decoded into the requested type."""


def register_count(data_type: DataType) -> int:
    """Return how many 16-bit registers ``data_type`` occupies."""
    return REGISTER_COUNT[data_type]


def _validate_registers(registers: Sequence[int]) -> None:
    for index, value in enumerate(registers):
        if not isinstance(value, int) or isinstance(value, bool):
            raise CodecError(f"register {index} is {value!r}, expected an int")
        if value < 0 or value > _UINT16_MASK:
            raise CodecError(
                f"register {index} value {value} is outside the 16-bit range 0..65535"
            )


def swap_words(registers: Sequence[int]) -> list[int]:
    """Reverse the register order of a multi-register value."""
    return list(registers)[::-1]


def swap_bytes(registers: Sequence[int]) -> list[int]:
    """Swap the high and low byte inside every register."""
    return [((r & 0x00FF) << 8) | ((r >> 8) & 0x00FF) for r in registers]


def registers_to_bytes(
    registers: Sequence[int],
    word_order: WordOrder = WordOrder.BIG,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> bytes:
    """Flatten registers into canonical big-endian bytes.

    The result is ready for ``struct.unpack`` with a ``>`` format. This is the
    one place where word order and byte order are applied; every decode path
    goes through it, so there is exactly one transform to get right.
    """
    _validate_registers(registers)
    ordered = swap_words(registers) if word_order is WordOrder.LITTLE else list(registers)
    out = bytearray()
    for reg in ordered:
        hi = (reg >> 8) & 0xFF
        lo = reg & 0xFF
        if byte_order is ByteOrder.LITTLE:
            out.append(lo)
            out.append(hi)
        else:
            out.append(hi)
            out.append(lo)
    return bytes(out)


def bytes_to_registers(
    data: bytes,
    word_order: WordOrder = WordOrder.BIG,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> list[int]:
    """Inverse of :func:`registers_to_bytes`.

    ``data`` is the canonical big-endian representation of the value; the
    result is the register list to put on the wire, lowest address first.
    """
    if len(data) % 2 != 0:
        raise CodecError(f"need an even number of bytes, got {len(data)}")
    regs: list[int] = []
    for i in range(0, len(data), 2):
        hi, lo = data[i], data[i + 1]
        if byte_order is ByteOrder.LITTLE:
            regs.append((lo << 8) | hi)
        else:
            regs.append((hi << 8) | lo)
    if word_order is WordOrder.LITTLE:
        regs = swap_words(regs)
    return regs


def decode_registers(
    registers: Sequence[int],
    data_type: DataType,
    word_order: WordOrder = WordOrder.BIG,
    byte_order: ByteOrder = ByteOrder.BIG,
    bit: int | None = None,
) -> float | int | bool:
    """Decode a register list into a Python scalar.

    ``bit`` extracts a single bit from a packed status word; it is only valid
    with ``DataType.BOOL`` on a single register.
    """
    expected = REGISTER_COUNT[data_type]
    if len(registers) != expected:
        raise CodecError(
            f"{data_type.value} needs exactly {expected} register(s), got {len(registers)}"
        )
    _validate_registers(registers)

    if data_type is DataType.BOOL:
        word = registers[0]
        if byte_order is ByteOrder.LITTLE:
            word = swap_bytes([word])[0]
        if bit is None:
            return word != 0
        return bit_from_word(word, bit)

    raw = registers_to_bytes(registers, word_order, byte_order)
    (value,) = struct.unpack(STRUCT_FORMAT[data_type], raw)
    return value


def encode_value(
    value: float | int | bool,
    data_type: DataType,
    word_order: WordOrder = WordOrder.BIG,
    byte_order: ByteOrder = ByteOrder.BIG,
    bit: int | None = None,
    base_word: int = 0,
) -> list[int]:
    """Encode a Python scalar into the register list to write.

    For a bit tag, ``base_word`` is the current contents of the status word so
    that the surrounding bits are preserved -- writing a whole word to set one
    bit is how you accidentally stop a line.
    """
    if data_type is DataType.BOOL:
        if bit is None:
            word = 1 if value else 0
        else:
            word = word_from_bits({bit: bool(value)}, base_word=base_word)
        if byte_order is ByteOrder.LITTLE:
            word = swap_bytes([word])[0]
        return [word & _UINT16_MASK]

    if data_type in (DataType.FLOAT32, DataType.FLOAT64):
        payload = struct.pack(STRUCT_FORMAT[data_type], float(value))
    else:
        ivalue = clamp_to_type(int(round(float(value))), data_type)
        payload = struct.pack(STRUCT_FORMAT[data_type], ivalue)
    return bytes_to_registers(payload, word_order, byte_order)


def clamp_to_type(value: int, data_type: DataType) -> int:
    """Clamp an integer into the representable range of ``data_type``.

    Silently wrapping a value into a signed register is how a setpoint of
    40000 becomes -25536 and a pump runs backwards.
    """
    ranges = {
        DataType.INT16: (-(2**15), 2**15 - 1),
        DataType.UINT16: (0, 2**16 - 1),
        DataType.INT32: (-(2**31), 2**31 - 1),
        DataType.UINT32: (0, 2**32 - 1),
    }
    if data_type not in ranges:
        raise CodecError(f"{data_type.value} is not an integer type")
    lo, hi = ranges[data_type]
    return max(lo, min(hi, value))


def bit_from_word(word: int, bit: int) -> bool:
    """Extract one bit (0 = least significant) from a 16-bit word."""
    if not 0 <= bit <= 15:
        raise CodecError(f"bit index {bit} outside 0..15")
    if not 0 <= word <= _UINT16_MASK:
        raise CodecError(f"word {word} outside the 16-bit range")
    return bool((word >> bit) & 1)


def bits_from_word(word: int, count: int = 16) -> list[bool]:
    """Unpack a status word into ``count`` booleans, least significant first."""
    if not 1 <= count <= 16:
        raise CodecError(f"bit count {count} outside 1..16")
    return [bit_from_word(word, i) for i in range(count)]


def word_from_bits(bits: dict[int, bool], base_word: int = 0) -> int:
    """Set/clear individual bits in ``base_word`` and return the new word."""
    word = base_word & _UINT16_MASK
    for index, state in bits.items():
        if not 0 <= index <= 15:
            raise CodecError(f"bit index {index} outside 0..15")
        if state:
            word |= 1 << index
        else:
            word &= ~(1 << index) & _UINT16_MASK
    return word


def coils_to_word(coils: Sequence[bool]) -> int:
    """Pack up to 16 coil booleans into a word, least significant bit first."""
    if len(coils) > 16:
        raise CodecError(f"cannot pack {len(coils)} coils into one 16-bit word")
    return word_from_bits({i: bool(state) for i, state in enumerate(coils)})


def word_to_coils(word: int, count: int = 16) -> list[bool]:
    """Unpack a word into coil booleans, least significant bit first."""
    return bits_from_word(word, count)


def apply_scaling(raw: float, scale: float = 1.0, offset: float = 0.0) -> float:
    """Convert a raw register value to engineering units."""
    if scale == 0:
        raise CodecError("scale of 0 makes the conversion irreversible")
    return raw * scale + offset


def remove_scaling(engineering: float, scale: float = 1.0, offset: float = 0.0) -> float:
    """Convert an engineering value back to a raw register value."""
    if scale == 0:
        raise CodecError("scale of 0 makes the conversion irreversible")
    return (engineering - offset) / scale
