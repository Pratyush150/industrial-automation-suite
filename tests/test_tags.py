"""Tag database loading, validation and engineering-unit conversion."""

from __future__ import annotations

import pytest

from factorylink.datatypes import ByteOrder, DataType, RegisterArea, WordOrder
from factorylink.tags import TagDatabase, TagDef, TagError, TagValidationError


def rows(*extra):
    """A minimal valid tag list plus whatever the test adds."""
    base = [{"name": "a", "address": 0, "data_type": "float32", "unit": "degC"}]
    return base + list(extra)


def test_overlapping_addresses_are_reported():
    """A float32 at 0 occupies 0 and 1, so a uint16 at 1 is a map error."""
    with pytest.raises(TagValidationError) as excinfo:
        TagDatabase.from_dicts(rows({"name": "b", "address": 1, "data_type": "uint16"}))
    assert "overlaps" in str(excinfo.value)
    assert len(excinfo.value.problems) == 1


def test_non_overlapping_addresses_are_accepted():
    """Adjacent tags that do not collide must load."""
    db = TagDatabase.from_dicts(rows({"name": "b", "address": 2, "data_type": "uint16"}))
    assert len(db) == 2
    assert db["a"].end_address == 1
    assert db["b"].end_address == 2


def test_same_address_in_a_different_area_is_not_an_overlap():
    """Holding 0 and coil 0 are unrelated storage."""
    db = TagDatabase.from_dicts(
        rows({"name": "c", "address": 0, "area": "coil", "data_type": "bool"})
    )
    assert len(db) == 2


def test_same_address_on_a_different_device_is_not_an_overlap():
    """Two PLCs both have a register 0."""
    db = TagDatabase.from_dicts(
        rows({"name": "d", "address": 0, "device": "plc2", "data_type": "float32"})
    )
    assert len(db) == 2


def test_bit_tags_may_share_a_status_word():
    """Different bits of one register is the normal case, not a collision."""
    db = TagDatabase.from_dicts(
        [
            {"name": "run", "address": 10, "data_type": "bool", "bit": 0},
            {"name": "fault", "address": 10, "data_type": "bool", "bit": 1},
        ]
    )
    assert len(db) == 2


def test_two_tags_on_the_same_bit_are_an_overlap():
    """The same bit twice is a copy-paste error in the map."""
    with pytest.raises(TagValidationError, match="overlaps"):
        TagDatabase.from_dicts(
            [
                {"name": "run", "address": 10, "data_type": "bool", "bit": 0},
                {"name": "running", "address": 10, "data_type": "bool", "bit": 0},
            ]
        )


def test_unknown_data_type_names_the_valid_options():
    """An error message that lists the alternatives saves a doc lookup."""
    with pytest.raises(TagValidationError) as excinfo:
        TagDatabase.from_dicts([{"name": "x", "address": 0, "data_type": "float24"}])
    message = str(excinfo.value)
    assert "float24" in message
    assert "float32" in message


def test_impossible_range_is_rejected():
    """min above max can never be satisfied."""
    with pytest.raises(TagValidationError, match="impossible range"):
        TagDatabase.from_dicts(
            [{"name": "x", "address": 0, "min_value": 100, "max_value": 10}]
        )


def test_alarm_limit_outside_the_tag_range_is_rejected():
    """A high limit above full scale is an alarm that can never fire."""
    with pytest.raises(TagValidationError, match="can never trigger"):
        TagDatabase.from_dicts(
            [
                {
                    "name": "x",
                    "address": 0,
                    "min_value": 0,
                    "max_value": 100,
                    "alarm": {"hi": 150},
                }
            ]
        )


def test_alarm_limits_out_of_order_are_rejected():
    """hi below lo is a transposed pair of numbers."""
    with pytest.raises(TagValidationError, match="out of order"):
        TagDatabase.from_dicts([{"name": "x", "address": 0, "alarm": {"lo": 90, "hi": 10}}])


def test_every_problem_is_reported_at_once():
    """Fixing an address map one error per run is miserable; report them all."""
    with pytest.raises(TagValidationError) as excinfo:
        TagDatabase.from_dicts(
            [
                {"name": "x", "address": 0, "scale": 0},
                {"name": "y", "address": 1, "deadband": -1},
                {"name": "z", "address": 2, "min_value": 10, "max_value": 1},
            ]
        )
    assert len(excinfo.value.problems) >= 3


def test_zero_scale_is_rejected():
    """A scale of zero cannot be inverted back to a raw value."""
    with pytest.raises(TagValidationError, match="not invertible"):
        TagDatabase.from_dicts([{"name": "x", "address": 0, "scale": 0}])


def test_bit_index_out_of_range_is_rejected():
    """Bit 16 does not exist in a 16-bit register."""
    with pytest.raises(TagValidationError, match="outside 0..15"):
        TagDatabase.from_dicts([{"name": "x", "address": 0, "data_type": "bool", "bit": 16}])


@pytest.mark.parametrize(
    ("scale", "offset", "raw"),
    [(1.0, 0.0, 1234), (0.1, 0.0, 4550), (0.5, -40.0, 300), (2.0, 100.0, 17), (0.01, 0.0, 65535)],
)
def test_engineering_conversion_round_trips(scale, offset, raw):
    """to_engineering and to_raw must be exact inverses."""
    tag = TagDef(name="t", address=0, scale=scale, offset=offset)
    engineering = tag.to_engineering(raw)
    assert tag.to_raw(engineering) == pytest.approx(raw)


def test_engineering_conversion_uses_scale_then_offset():
    """The convention is engineering = raw * scale + offset."""
    tag = TagDef(name="t", address=0, scale=0.5, offset=-40.0)
    assert tag.to_engineering(200) == pytest.approx(60.0)
    assert tag.to_raw(60.0) == pytest.approx(200.0)


def test_clamp_and_in_range():
    """Range checks drive both alarm validation and the safety layer."""
    tag = TagDef(name="t", address=0, min_value=0.0, max_value=100.0)
    assert tag.in_range(50.0) is True
    assert tag.in_range(-1.0) is False
    assert tag.clamp(-5.0) == 0.0
    assert tag.clamp(500.0) == 100.0
    assert tag.clamp(50.0) == 50.0


def test_word_and_byte_order_aliases_are_accepted():
    """Vendors write CDAB, not 'little word order'."""
    db = TagDatabase.from_dicts(
        [{"name": "x", "address": 0, "data_type": "real", "word_order": "cdab",
          "byte_order": "swapped"}]
    )
    tag = db["x"]
    assert tag.data_type is DataType.FLOAT32
    assert tag.word_order is WordOrder.LITTLE
    assert tag.byte_order is ByteOrder.LITTLE


def test_area_aliases_and_bit_area_forces_bool():
    """A coil is a boolean whatever the file says about data_type."""
    db = TagDatabase.from_dicts(
        [{"name": "x", "address": 3, "area": "co", "data_type": "uint16"}]
    )
    assert db["x"].area is RegisterArea.COIL
    assert db["x"].data_type is DataType.BOOL
    assert db["x"].register_count == 1


def test_unknown_tag_lookup_raises_a_useful_error():
    """A typo in a tag name should say so."""
    db = TagDatabase.from_dicts(rows())
    with pytest.raises(TagError, match="unknown tag"):
        _ = db["nope"]
    assert db.get("nope") is None


def test_groups_and_devices_preserve_file_order():
    """Deterministic ordering keeps CLI output stable between runs."""
    db = TagDatabase.from_dicts(
        [
            {"name": "a", "address": 0, "poll_group": "fast", "device": "p1"},
            {"name": "b", "address": 1, "poll_group": "slow", "device": "p2"},
            {"name": "c", "address": 2, "poll_group": "fast", "device": "p1"},
        ]
    )
    assert db.groups == ["fast", "slow"]
    assert db.devices == ["p1", "p2"]
    assert [t.name for t in db.by_group("fast")] == ["a", "c"]


def test_csv_round_trip(tmp_path, db):
    """The whole bottling-line map must survive a CSV export and reload."""
    path = tmp_path / "tags.csv"
    path.write_text(db.to_csv(), encoding="utf-8")
    reloaded = TagDatabase.load(path)
    assert reloaded.names == db.names
    for original, copy in zip(db.tags(), reloaded.tags()):
        assert original == copy


def test_yaml_round_trip(tmp_path, db):
    """Same for YAML, including the alarm sub-mapping."""
    path = tmp_path / "tags.yaml"
    path.write_text(db.to_yaml(), encoding="utf-8")
    reloaded = TagDatabase.load(path)
    assert reloaded.names == db.names
    assert reloaded["fill_temperature"].alarm == db["fill_temperature"].alarm
    assert reloaded["motor_current"].word_order is WordOrder.LITTLE
    assert reloaded["vibration_rms"].byte_order is ByteOrder.LITTLE


def test_shipped_config_matches_the_builtin_map(repo_root, db):
    """config/tags_bottling_line.yaml must not drift from the code."""
    config = repo_root / "config" / "tags_bottling_line.yaml"
    assert config.exists()
    shipped = TagDatabase.load(config)
    assert shipped.names == db.names
    for name in db.names:
        assert shipped[name] == db[name]


def test_unsupported_file_type_is_refused(tmp_path):
    """A .txt tag file is almost certainly a mistake."""
    path = tmp_path / "tags.txt"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(TagError, match="unsupported"):
        TagDatabase.load(path)
