"""Modbus codec: every word/byte order combination against known byte patterns.

These are the tests that matter most in the whole suite. Word-order confusion
is the classic Modbus bug, and the only defence is a table of known-good
patterns checked in both directions.
"""

from __future__ import annotations

import math
import struct

import pytest

from factorylink.datatypes import ByteOrder, DataType, WordOrder
from factorylink.protocols.modbus_codec import (
    CodecError,
    apply_scaling,
    bit_from_word,
    bits_from_word,
    bytes_to_registers,
    clamp_to_type,
    coils_to_word,
    decode_registers,
    encode_value,
    register_count,
    registers_to_bytes,
    remove_scaling,
    swap_bytes,
    swap_words,
    word_from_bits,
    word_to_coils,
)

BIG, LITTLE = WordOrder.BIG, WordOrder.LITTLE
B_BIG, B_LITTLE = ByteOrder.BIG, ByteOrder.LITTLE

# float32 1.0 is 0x3F800000. These are the four ways a device can present it.
FLOAT32_ONE = {
    (BIG, B_BIG): [0x3F80, 0x0000],  # ABCD
    (LITTLE, B_BIG): [0x0000, 0x3F80],  # CDAB
    (BIG, B_LITTLE): [0x803F, 0x0000],  # BADC
    (LITTLE, B_LITTLE): [0x0000, 0x803F],  # DCBA
}

# uint32 0x12345678 in the same four orders.
UINT32_PATTERN = {
    (BIG, B_BIG): [0x1234, 0x5678],
    (LITTLE, B_BIG): [0x5678, 0x1234],
    (BIG, B_LITTLE): [0x3412, 0x7856],
    (LITTLE, B_LITTLE): [0x7856, 0x3412],
}


@pytest.mark.parametrize(("orders", "registers"), sorted(FLOAT32_ONE.items(), key=str))
def test_float32_one_decodes_in_every_order(orders, registers):
    """1.0 must decode from the exact register pattern for each order."""
    word_order, byte_order = orders
    assert decode_registers(registers, DataType.FLOAT32, word_order, byte_order) == 1.0


@pytest.mark.parametrize(("orders", "registers"), sorted(FLOAT32_ONE.items(), key=str))
def test_float32_one_encodes_in_every_order(orders, registers):
    """Encoding 1.0 must produce the exact register pattern for each order."""
    word_order, byte_order = orders
    assert encode_value(1.0, DataType.FLOAT32, word_order, byte_order) == registers


@pytest.mark.parametrize(("orders", "registers"), sorted(UINT32_PATTERN.items(), key=str))
def test_uint32_pattern_round_trips(orders, registers):
    """0x12345678 must survive decode/encode in every order."""
    word_order, byte_order = orders
    decoded = decode_registers(registers, DataType.UINT32, word_order, byte_order)
    assert decoded == 0x12345678
    assert encode_value(decoded, DataType.UINT32, word_order, byte_order) == registers


def test_wrong_word_order_gives_a_wrong_but_plausible_number():
    """The failure mode this module exists to prevent, pinned as a test.

    Reading an ABCD device as CDAB does not raise. It returns a number, and
    that number is nonsense -- which is exactly why word order has to be
    configuration rather than a guess.
    """
    registers = encode_value(42.5, DataType.FLOAT32, BIG, B_BIG)
    correct = decode_registers(registers, DataType.FLOAT32, BIG, B_BIG)
    wrong = decode_registers(registers, DataType.FLOAT32, LITTLE, B_BIG)
    assert correct == 42.5
    assert wrong != correct
    assert abs(wrong) < 1e-30 or abs(wrong) > 1e10


@pytest.mark.parametrize("value", [0.0, 1.0, -1.0, 3.14159, 1.0e-8, -2.5e7, 65535.5])
def test_float32_round_trip_across_two_registers(value):
    """float32 must survive a full encode/decode cycle in every order."""
    for word_order in (BIG, LITTLE):
        for byte_order in (B_BIG, B_LITTLE):
            registers = encode_value(value, DataType.FLOAT32, word_order, byte_order)
            assert len(registers) == 2
            decoded = decode_registers(registers, DataType.FLOAT32, word_order, byte_order)
            assert decoded == pytest.approx(struct.unpack(">f", struct.pack(">f", value))[0])


@pytest.mark.parametrize("value", [0.0, 1.0, -1.0, math.pi, 1.234567890123e-9, -9.87e15])
def test_float64_round_trip_across_four_registers(value):
    """float64 spans four registers; word order reverses all four."""
    for word_order in (BIG, LITTLE):
        for byte_order in (B_BIG, B_LITTLE):
            registers = encode_value(value, DataType.FLOAT64, word_order, byte_order)
            assert len(registers) == 4
            assert decode_registers(registers, DataType.FLOAT64, word_order, byte_order) == value


def test_float64_big_order_matches_ieee_bytes():
    """The canonical byte layout must match struct's big-endian packing."""
    registers = encode_value(math.pi, DataType.FLOAT64, BIG, B_BIG)
    assert registers_to_bytes(registers, BIG, B_BIG) == struct.pack(">d", math.pi)


@pytest.mark.parametrize(
    ("value", "data_type", "registers"),
    [
        (-1, DataType.INT16, [0xFFFF]),
        (-32768, DataType.INT16, [0x8000]),
        (32767, DataType.INT16, [0x7FFF]),
        (65535, DataType.UINT16, [0xFFFF]),
        (-2, DataType.INT32, [0xFFFF, 0xFFFE]),
        (-2147483648, DataType.INT32, [0x8000, 0x0000]),
        (4294967295, DataType.UINT32, [0xFFFF, 0xFFFF]),
    ],
)
def test_integer_patterns(value, data_type, registers):
    """Signed and unsigned integers against known two's-complement patterns."""
    assert decode_registers(registers, data_type, BIG, B_BIG) == value
    assert encode_value(value, data_type, BIG, B_BIG) == registers


def test_swap_helpers():
    """Word and byte swapping are independent, composable transforms."""
    assert swap_words([1, 2, 3]) == [3, 2, 1]
    assert swap_bytes([0x1234, 0xABCD]) == [0x3412, 0xCDAB]
    assert swap_bytes(swap_bytes([0x1234])) == [0x1234]


def test_registers_to_bytes_and_back():
    """bytes_to_registers is the exact inverse of registers_to_bytes."""
    registers = [0x1234, 0x5678, 0x9ABC]
    for word_order in (BIG, LITTLE):
        for byte_order in (B_BIG, B_LITTLE):
            raw = registers_to_bytes(registers, word_order, byte_order)
            assert len(raw) == 6
            assert bytes_to_registers(raw, word_order, byte_order) == registers


def test_bit_extraction_from_a_packed_status_word():
    """A status word carries several booleans; each must come out correctly."""
    word = 0b0000_0000_1000_1101  # bits 0, 2, 3 and 7 set
    assert bit_from_word(word, 0) is True
    assert bit_from_word(word, 1) is False
    assert bit_from_word(word, 2) is True
    assert bit_from_word(word, 3) is True
    assert bit_from_word(word, 7) is True
    assert bit_from_word(word, 15) is False
    assert bits_from_word(word, 8) == [True, False, True, True, False, False, False, True]


def test_bool_decode_uses_the_bit_index():
    """A BOOL tag with a bit index reads that bit, not the whole word."""
    assert decode_registers([0b0100], DataType.BOOL, BIG, B_BIG, bit=2) is True
    assert decode_registers([0b0100], DataType.BOOL, BIG, B_BIG, bit=1) is False
    assert decode_registers([0], DataType.BOOL, BIG, B_BIG) is False
    assert decode_registers([7], DataType.BOOL, BIG, B_BIG) is True


def test_bit_write_preserves_the_other_bits():
    """Setting one bit must not clear the rest of the status word."""
    base = 0b1010_1010
    assert word_from_bits({0: True}, base) == 0b1010_1011
    assert word_from_bits({1: False}, base) == 0b1010_1000
    encoded = encode_value(True, DataType.BOOL, BIG, B_BIG, bit=4, base_word=base)
    assert encoded == [0b1011_1010]


def test_coil_packing_round_trip():
    """Coils pack least significant bit first."""
    coils = [True, False, True, True] + [False] * 12
    word = coils_to_word(coils)
    assert word == 0b1101
    assert word_to_coils(word, 4) == [True, False, True, True]


def test_scaling_round_trip():
    """Engineering conversion must be exactly invertible."""
    for scale, offset in ((0.1, 0.0), (0.5, -40.0), (2.0, 100.0), (0.01, 0.0)):
        for raw in (0, 1, 1234, 65535):
            engineering = apply_scaling(raw, scale, offset)
            assert remove_scaling(engineering, scale, offset) == pytest.approx(raw)


def test_scaling_rejects_zero_scale():
    """A scale of zero destroys information and must be refused."""
    with pytest.raises(CodecError):
        apply_scaling(10.0, 0.0, 0.0)
    with pytest.raises(CodecError):
        remove_scaling(10.0, 0.0, 0.0)


def test_clamp_to_type_prevents_silent_wrapping():
    """40000 into an int16 must clamp, not wrap to a negative number."""
    assert clamp_to_type(40000, DataType.INT16) == 32767
    assert clamp_to_type(-40000, DataType.INT16) == -32768
    assert clamp_to_type(-5, DataType.UINT16) == 0
    assert clamp_to_type(70000, DataType.UINT16) == 65535
    with pytest.raises(CodecError):
        clamp_to_type(1, DataType.FLOAT32)


def test_register_count_matches_the_type_width():
    """Register widths must match the data type."""
    assert register_count(DataType.UINT16) == 1
    assert register_count(DataType.FLOAT32) == 2
    assert register_count(DataType.FLOAT64) == 4


def test_wrong_register_count_is_rejected():
    """Decoding a float32 from one register is a configuration error."""
    with pytest.raises(CodecError, match="exactly 2"):
        decode_registers([0x3F80], DataType.FLOAT32)
    with pytest.raises(CodecError, match="exactly 4"):
        decode_registers([0, 0], DataType.FLOAT64)


def test_out_of_range_register_values_are_rejected():
    """A value that cannot fit in 16 bits is not a register."""
    with pytest.raises(CodecError, match="16-bit range"):
        decode_registers([70000], DataType.UINT16)
    with pytest.raises(CodecError, match="16-bit range"):
        decode_registers([-1], DataType.UINT16)


def test_bit_index_bounds_are_checked():
    """Bit indices outside 0..15 are a config error, not a silent no-op."""
    with pytest.raises(CodecError):
        bit_from_word(0, 16)
    with pytest.raises(CodecError):
        bits_from_word(0, 17)
