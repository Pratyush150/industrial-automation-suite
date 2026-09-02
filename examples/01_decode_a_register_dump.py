#!/usr/bin/env python3
"""Work out a device's word and byte order from a raw register dump.

This is the first thing to do with an unfamiliar Modbus device, and it takes
about two minutes if you do it systematically instead of guessing.

The method: read the registers while the process value is something you know
-- a tank you can see, a temperature next to a handheld meter, a motor you
just started -- then decode the same bytes in all four orders and see which
one gives a believable number.

Run:  python3 examples/01_decode_a_register_dump.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factorylink.datatypes import ByteOrder, DataType, WordOrder  # noqa: E402
from factorylink.protocols.modbus_codec import decode_registers, encode_value  # noqa: E402

# A two-register dump straight off the wire. The operator says the tank is at
# roughly 63 percent full.
DUMP = [0x0000, 0x427D]
OBSERVED = 63.0


def main() -> int:
    """Decode one register pair every way and rank the candidates."""
    # Build a realistic dump instead of a made-up one: a device that stores
    # 63.4 as a CDAB float32.
    dump = encode_value(63.4, DataType.FLOAT32, WordOrder.LITTLE, ByteOrder.BIG)
    print(f"registers read from the device : {[hex(r) for r in dump]}")
    print(f"value the operator can see     : about {OBSERVED} %")
    print()
    print(f"{'word order':<12}{'byte order':<12}{'as float32':>18}{'as uint32':>14}  verdict")
    print("-" * 76)

    for word_order in (WordOrder.BIG, WordOrder.LITTLE):
        for byte_order in (ByteOrder.BIG, ByteOrder.LITTLE):
            as_float = decode_registers(dump, DataType.FLOAT32, word_order, byte_order)
            as_uint = decode_registers(dump, DataType.UINT32, word_order, byte_order)
            plausible = abs(as_float - OBSERVED) < 5.0
            verdict = "<-- this one" if plausible else ""
            print(
                f"{word_order.value:<12}{byte_order.value:<12}"
                f"{as_float:>18.6g}{as_uint:>14d}  {verdict}"
            )

    print()
    print("Notes:")
    print("  * A wrong word order does not raise. It returns a number, usually")
    print("    absurdly large or absurdly small, and occasionally one that looks")
    print("    almost right. That is why this has to be config, not a guess.")
    print("  * If none of the four float32 columns is plausible, the value is")
    print("    probably a scaled integer. Look at the uint32 column: 634 with a")
    print("    scale of 0.1, or 6340 with a scale of 0.01, is a very common map.")
    print("  * Record the answer in the tag database once. Do not rediscover it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
