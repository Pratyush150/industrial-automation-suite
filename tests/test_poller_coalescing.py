"""Register-range coalescing: correctness, minimality and the protocol limits."""

from __future__ import annotations

import random
from typing import Sequence

import pytest

from factorylink.datatypes import DataType, RegisterArea
from factorylink.poller import ReadBlock, coalesce_blocks
from factorylink.tags import TagDef

INFINITE_GAP = 10**9


def tag(name: str, address: int, data_type: DataType = DataType.UINT16, **kwargs) -> TagDef:
    """Build a tag for coalescing tests."""
    return TagDef(name=name, address=address, data_type=data_type, **kwargs)


def minimal_block_count(tags: Sequence[TagDef], limit: int) -> int:
    """Independent reference implementation of the minimum block count.

    Classic interval point cover: repeatedly take the lowest uncovered tag,
    open a window at its start address, and swallow every tag that fits inside
    ``limit`` registers from there. This is provably optimal, and it is written
    here from scratch so it does not share a bug with the implementation.
    """
    remaining = sorted(tags, key=lambda t: (t.address, t.end_address))
    blocks = 0
    index = 0
    while index < len(remaining):
        start = remaining[index].address
        blocks += 1
        while index < len(remaining) and remaining[index].end_address - start + 1 <= limit:
            index += 1
    return blocks


def test_contiguous_tags_become_one_block():
    """Twenty adjacent registers are one request, not twenty."""
    tags = [tag(f"t{i}", i) for i in range(20)]
    blocks = coalesce_blocks(tags)
    assert len(blocks) == 1
    assert blocks[0].start == 0
    assert blocks[0].count == 20
    assert len(blocks[0].tags) == 20
    assert blocks[0].wasted == 0


def test_every_tag_appears_exactly_once():
    """Coalescing must not drop or duplicate a tag."""
    tags = [tag(f"t{i}", a) for i, a in enumerate([0, 1, 2, 40, 41, 300, 301, 302])]
    blocks = coalesce_blocks(tags, max_gap=4)
    covered = [t.name for b in blocks for t in b.tags]
    assert sorted(covered) == sorted(t.name for t in tags)
    assert len(covered) == len(set(covered))


def test_a_wide_gap_starts_a_new_block():
    """Reading 200 registers of padding costs more than a second request."""
    tags = [tag("a", 0), tag("b", 1), tag("c", 250), tag("d", 251)]
    blocks = coalesce_blocks(tags, max_gap=8)
    assert len(blocks) == 2
    assert [b.count for b in blocks] == [2, 2]


def test_a_narrow_gap_is_bridged():
    """A four-register hole is cheaper to read than to skip."""
    tags = [tag("a", 0), tag("b", 5)]
    blocks = coalesce_blocks(tags, max_gap=8)
    assert len(blocks) == 1
    assert blocks[0].count == 6
    assert blocks[0].wasted == 4


def test_max_gap_zero_means_contiguous_only():
    """With no bridging allowed, any hole splits the block."""
    tags = [tag("a", 0), tag("b", 2)]
    assert len(coalesce_blocks(tags, max_gap=0)) == 2
    assert len(coalesce_blocks([tag("a", 0), tag("b", 1)], max_gap=0)) == 1


def test_no_block_exceeds_the_125_register_limit():
    """A Modbus register read is capped at 125 registers by the PDU size."""
    tags = [tag(f"t{i}", i) for i in range(400)]
    blocks = coalesce_blocks(tags, max_gap=INFINITE_GAP)
    assert all(b.count <= 125 for b in blocks)
    assert sum(len(b.tags) for b in blocks) == 400
    assert len(blocks) == 4


def test_bit_areas_use_the_2000_bit_limit():
    """Coils and discrete inputs are capped at 2000 bits, not 125."""
    tags = [
        tag(f"c{i}", i, DataType.BOOL, area=RegisterArea.COIL) for i in range(2500)
    ]
    blocks = coalesce_blocks(tags, max_gap=INFINITE_GAP)
    assert all(b.count <= 2000 for b in blocks)
    assert len(blocks) == 2


def test_blocks_never_merge_across_devices():
    """Two PLCs are two sockets; the same address means different data."""
    tags = [tag("a", 0, device="plc1"), tag("b", 1, device="plc2")]
    blocks = coalesce_blocks(tags)
    assert len(blocks) == 2
    assert {b.device for b in blocks} == {"plc1", "plc2"}


def test_blocks_never_merge_across_areas():
    """Holding register 0 and coil 0 are different address spaces."""
    tags = [
        tag("a", 0, area=RegisterArea.HOLDING),
        tag("b", 1, area=RegisterArea.HOLDING),
        tag("c", 0, DataType.BOOL, area=RegisterArea.COIL),
        tag("d", 0, DataType.BOOL, area=RegisterArea.DISCRETE),
        tag("e", 0, area=RegisterArea.INPUT),
    ]
    blocks = coalesce_blocks(tags)
    assert len(blocks) == 4
    assert {(b.device, b.area) for b in blocks} == {
        ("plc1", RegisterArea.HOLDING),
        ("plc1", RegisterArea.COIL),
        ("plc1", RegisterArea.DISCRETE),
        ("plc1", RegisterArea.INPUT),
    }


def test_multi_register_tags_are_not_split():
    """A float32 spans two registers; a block boundary must not cut it."""
    tags = [tag(f"f{i}", i * 2, DataType.FLOAT32) for i in range(80)]
    blocks = coalesce_blocks(tags, max_gap=INFINITE_GAP)
    for block in blocks:
        for member in block.tags:
            assert member.address >= block.start
            assert member.end_address <= block.end
    assert all(b.count <= 125 for b in blocks)


def test_result_matches_the_minimum_for_random_maps():
    """Greedy left-to-right merging is optimal; check it against a reference."""
    rng = random.Random(20260101)
    for _ in range(60):
        addresses = sorted(rng.sample(range(0, 900), rng.randint(2, 60)))
        tags = [tag(f"t{i}", a) for i, a in enumerate(addresses)]
        blocks = coalesce_blocks(tags, max_gap=INFINITE_GAP)
        assert len(blocks) == minimal_block_count(tags, 125)


def test_mixed_widths_still_match_the_minimum():
    """Optimality must hold when tags have different register widths."""
    rng = random.Random(7)
    for _ in range(30):
        tags: list[TagDef] = []
        address = 0
        for i in range(rng.randint(3, 40)):
            data_type = rng.choice([DataType.UINT16, DataType.INT32, DataType.FLOAT32])
            tags.append(tag(f"t{i}", address, data_type))
            address += (1 if data_type is DataType.UINT16 else 2) + rng.randint(0, 30)
        blocks = coalesce_blocks(tags, max_gap=INFINITE_GAP)
        assert len(blocks) == minimal_block_count(tags, 125)


def test_bottling_line_fast_group_collapses_to_two_requests(db):
    """The shipped map: 21 fast tags become 2 requests, one per address space."""
    fast = db.by_group("fast")
    blocks = coalesce_blocks(fast, max_gap=8)
    assert len(fast) == 21
    assert len(blocks) == 2
    assert {b.area for b in blocks} == {RegisterArea.HOLDING, RegisterArea.DISCRETE}
    assert all(b.count <= 125 for b in blocks)


def test_read_block_reports_padding_without_double_counting_bits(db):
    """Five status bits in one word count as one used register, not five."""
    fast = db.by_group("fast")
    holding = [b for b in coalesce_blocks(fast) if b.area is RegisterArea.HOLDING][0]
    assert holding.wasted >= 0
    assert holding.wasted < holding.count
    assert "holding" in str(holding)


def test_a_tag_wider_than_the_limit_is_rejected():
    """Nothing sane produces this, but the error must be explicit."""
    wide = TagDef(name="x", address=0, data_type=DataType.FLOAT64)
    with pytest.raises(ValueError, match="read limit"):
        coalesce_blocks([wide], max_count={RegisterArea.HOLDING: 2})


def test_negative_max_gap_is_rejected():
    """A negative gap has no meaning."""
    with pytest.raises(ValueError, match="max_gap"):
        coalesce_blocks([tag("a", 0)], max_gap=-1)


def test_empty_input_produces_no_blocks():
    """No tags, no requests."""
    assert coalesce_blocks([]) == []


def test_read_block_end_and_string_form():
    """ReadBlock is part of the CLI output, so its formatting is pinned."""
    block = ReadBlock("plc1", RegisterArea.HOLDING, 10, 5, (tag("a", 10),))
    assert block.end == 14
    assert str(block) == "plc1/holding[10..14] 5 regs, 1 tags"
