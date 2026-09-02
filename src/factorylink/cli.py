"""Command line interface for factorylink.

Every subcommand runs against the built-in simulated PLC by default, so the
whole tool is exercisable with no hardware, no broker and no network::

    factorylink --demo
    factorylink scan --duration 20
    factorylink dump-tags --plan
    factorylink oee --duration 900 --fault jam@120:90
    factorylink serve --port 8377

The same entry point is available without installing the package, as
``tools/factorylink``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from factorylink.clock import ManualClock, SystemClock
from factorylink.dashboard import DashboardApp, serve
from factorylink.poller import coalesce_blocks
from factorylink.protocols.simulator import Fault
from factorylink.runtime import (
    DEFAULT_PERIODS,
    build_simulated_runtime,
    config_dir,
    format_alarm_list,
    format_scan_table,
    load_tag_database,
)
from factorylink.safety import SAFETY_NOTICE, SafetyError, WritePolicy
from factorylink.tags import TagValidationError

BANNER = "factorylink -- PLC acquisition, alarms, historian, OEE and a live dashboard"


def parse_fault(text: str) -> tuple[float, Fault, float | None]:
    """Parse ``name@start[:duration]`` into a scheduled fault.

    >>> parse_fault("jam@120:90")[0]
    120.0
    """
    if "@" not in text:
        raise argparse.ArgumentTypeError(
            f"fault {text!r} must look like name@start or name@start:duration"
        )
    name, _, timing = text.partition("@")
    start_text, _, duration_text = timing.partition(":")
    try:
        start = float(start_text)
        duration = float(duration_text) if duration_text else None
    except ValueError:
        raise argparse.ArgumentTypeError(f"fault {text!r} has a non-numeric time") from None
    try:
        fault = Fault.parse(name)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return start, fault, duration


def _periods(args: argparse.Namespace) -> dict[str, float]:
    periods = dict(DEFAULT_PERIODS)
    for spec in args.period or []:
        name, _, value = spec.partition("=")
        periods[name.strip()] = float(value)
    return periods


def _build(args: argparse.Namespace, real_time: bool = False):
    clock = SystemClock() if real_time else ManualClock(0.0)
    db = load_tag_database(getattr(args, "config", None))
    alarm_config = getattr(args, "alarms", None) or (config_dir() / "alarms.yaml")
    runtime, plc = build_simulated_runtime(
        clock=clock,
        seed=getattr(args, "seed", 1234),
        db=db,
        periods=_periods(args),
        alarm_config=alarm_config,
        historian_path=getattr(args, "database", ":memory:"),
    )
    for start, fault, duration in getattr(args, "fault", None) or []:
        plc.schedule_fault(start, fault, duration)
    return runtime, plc


# -- subcommands ----------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    """Poll the line and print a table of live values."""
    runtime, _ = _build(args, real_time=args.real_time)
    if args.real_time:
        deadline = runtime.clock.now() + args.duration
        while runtime.clock.now() < deadline:
            runtime.poll_once()
            print(format_scan_table(runtime, args.group))
            runtime.clock.sleep(args.refresh)
    else:
        runtime.run(args.duration)
        print(format_scan_table(runtime, args.group))
    runtime.close()
    return 0


def cmd_dump_tags(args: argparse.Namespace) -> int:
    """Print the tag database, optionally with the coalesced read plan."""
    try:
        db = load_tag_database(args.config)
    except TagValidationError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.format == "yaml":
        print(db.to_yaml(), end="")
    elif args.format == "csv":
        print(db.to_csv(), end="")
    elif args.format == "json":
        print(json.dumps([t.to_mapping() for t in db], indent=2))
    else:
        width = 112
        print("=" * width)
        print(f"TAG DATABASE  {len(db)} tags, {len(db.devices)} device(s), "
              f"{len(db.groups)} poll group(s)")
        print("=" * width)
        header = (f"{'name':<22}{'device':<8}{'area':<10}{'addr':>6}{'type':>9}"
                  f"{'word':>7}{'byte':>7}{'scale':>9}{'group':>8}{'unit':>13}"
                  f"{'w':>3}")
        print(header)
        print("-" * width)
        for tag in db:
            address = str(tag.address) + (f".{tag.bit}" if tag.bit is not None else "")
            print(
                f"{tag.name:<22}{tag.device:<8}{tag.area.value:<10}{address:>6}"
                f"{tag.data_type.value:>9}{tag.word_order.value:>7}{tag.byte_order.value:>7}"
                f"{tag.scale:>9g}{tag.poll_group:>8}{tag.unit:>13}"
                f"{('rw' if tag.writable else 'r'):>3}"
            )
        print("-" * width)
    if args.plan:
        print()
        print("=" * 78)
        print("COALESCED READ PLAN")
        print("=" * 78)
        for group in db.groups:
            tags = db.by_group(group)
            blocks = coalesce_blocks(tags, max_gap=args.max_gap)
            print(f"group {group:<10} {len(tags):>3} tags -> {len(blocks)} request(s)")
            for block in blocks:
                print(f"    {block}  (padding {block.wasted} regs)")
        print("=" * 78)
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """Run the process model and optionally write the signals to CSV."""
    runtime, plc = _build(args)
    runtime.run(args.duration)
    print(format_scan_table(runtime))
    print()
    print(format_alarm_list(runtime))
    if args.csv:
        rows = runtime.historian.export_csv(args.csv)
        print(f"exported {rows} archived samples to {args.csv}")
    runtime.close()
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Run, then print aggregated history for one tag."""
    runtime, _ = _build(args)
    runtime.run(args.duration)
    runtime.flush()
    tag = args.tag
    if tag not in runtime.db:
        print(f"unknown tag {tag!r}; try one of: {', '.join(runtime.db.names[:8])} ...",
              file=sys.stderr)
        runtime.close()
        return 2
    buckets = runtime.historian.aggregate(tag, 0.0, runtime.clock.now(), args.interval)
    stats = runtime.historian.stats()
    per_tag = stats["tags"].get(tag, {})
    width = 72
    print("=" * width)
    print(f"HISTORY  {tag}  interval {args.interval:g}s")
    print("=" * width)
    print(f"received {per_tag.get('received', 0)} samples, stored "
          f"{per_tag.get('stored', 0)} "
          f"(compression ratio {per_tag.get('ratio', 1.0):.3f}, "
          f"deadband {per_tag.get('deadband', 0)}, tolerance {per_tag.get('tolerance', 0)})")
    print("-" * width)
    print(f"{'t_start':>10}{'n':>6}{'min':>13}{'max':>13}{'avg':>13}")
    print("-" * width)
    for bucket in buckets:
        print(f"{bucket.start:>10.1f}{bucket.count:>6}"
              f"{bucket.minimum:>13.4g}{bucket.maximum:>13.4g}{bucket.average:>13.4g}")
    print("=" * width)
    if args.export:
        rows = runtime.historian.export_csv(args.export, [tag])
        print(f"exported {rows} rows to {args.export}")
    runtime.close()
    return 0


def cmd_oee(args: argparse.Namespace) -> int:
    """Run a shift and print the OEE report and downtime Pareto."""
    runtime, _ = _build(args)
    runtime.run(args.duration)
    result = runtime.oee.result(runtime.clock.now())
    print(result.format_report())
    print()
    print(runtime.oee.tracker.format_pareto())
    runtime.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Pre-fill some history, then serve the dashboard over HTTP."""
    runtime, plc = _build(args, real_time=True)
    warm = ManualClock(0.0)
    print(f"{BANNER}\nwarming up {args.warmup:g}s of history ...")
    warm_runtime, warm_plc = build_simulated_runtime(
        clock=warm, seed=args.seed, db=runtime.db, periods=_periods(args)
    )
    for start, fault, duration in args.fault or []:
        warm_plc.schedule_fault(start, fault, duration)
    warm_runtime.run(args.warmup)
    app = DashboardApp(
        warm_runtime.db,
        poller=warm_runtime.poller,
        alarms=warm_runtime.alarms,
        historian=warm_runtime.historian,
        oee=warm_runtime.oee,
        guard=warm_runtime.guard,
    )
    server = serve(app, args.host, args.port)
    print(f"dashboard on http://{args.host}:{args.port}/  (ctrl-c to stop)")
    print(SAFETY_NOTICE)

    import threading

    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            warm.advance(args.step)
            warm_runtime.poll_once()
            stop.wait(args.step)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop.set()
        server.shutting_down = True
        server.shutdown()
        server.server_close()
        warm_runtime.close()
        runtime.close()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """End-to-end demonstration: acquisition, alarms, history, OEE, safety."""
    clock = ManualClock(0.0)
    db = load_tag_database(getattr(args, "config", None))
    runtime, plc = build_simulated_runtime(
        clock=clock,
        seed=args.seed,
        db=db,
        alarm_config=config_dir() / "alarms.yaml",
    )
    plc.schedule_fault(180.0, Fault.JAM, 95.0)
    plc.schedule_fault(600.0, Fault.CHILLER_FAILURE, 420.0)
    plc.schedule_fault(1500.0, Fault.AIR_LEAK, 160.0)
    plc.schedule_fault(2400.0, Fault.MOTOR_OVERLOAD, 200.0)

    print("=" * 96)
    print(BANNER)
    print("=" * 96)
    print(f"tag database        : {len(db)} tags, {len(db.devices)} device(s), "
          f"{len(db.groups)} poll groups")
    for group in db.groups:
        blocks = coalesce_blocks(db.by_group(group))
        print(f"  group {group:<8} {len(db.by_group(group)):>3} tags "
              f"-> {len(blocks)} coalesced read(s)")
    print(f"simulated shift     : {args.duration:g} s of virtual time, "
          f"faults injected at 180 s, 600 s, 1500 s, 2400 s")
    print()

    runtime.run(args.duration)
    runtime.flush()

    print(format_scan_table(runtime))
    print()
    print(format_alarm_list(runtime))
    print()
    acked = runtime.alarms.acknowledge_all(by="demo-operator")
    print(f"operator acknowledges everything ({len(acked)} alarm(s)) ...")
    print("latched alarms whose condition has cleared now leave the list; "
          "alarms still active stay:")
    print(format_alarm_list(runtime))
    print()

    result = runtime.oee.result(runtime.clock.now())
    print(result.format_report())
    print()
    print(runtime.oee.tracker.format_pareto())
    print()

    stats = runtime.historian.stats()
    width = 96
    print("=" * width)
    print("HISTORIAN")
    print("=" * width)
    print(f"samples received {stats['received']}, archived {stats['stored']} "
          f"(overall ratio {stats['ratio']:.3f})")
    print(f"{'tag':<22}{'received':>10}{'stored':>8}{'ratio':>8}"
          f"{'deadband':>10}{'tolerance':>11}")
    print("-" * width)
    for name in ("conveyor_speed", "tank_level", "fill_temperature", "product_count",
                 "motor_current", "energy_kwh"):
        entry = stats["tags"].get(name)
        if entry:
            print(f"{name:<22}{entry['received']:>10}{entry['stored']:>8}"
                  f"{entry['ratio']:>8.3f}{entry['deadband']:>10g}{entry['tolerance']:>11g}")
    print("=" * width)
    print()

    print("=" * width)
    print("SAFETY / WRITE PATH")
    print("=" * width)
    print(SAFETY_NOTICE)
    print("-" * width)
    def attempt(label: str, tag: str, value: float) -> None:
        try:
            approved = runtime.guard.check(tag, value, operator="demo")
        except SafetyError as exc:
            print(f"{label:<24} REFUSED  {type(exc).__name__}: {exc}")
            return
        print(f"{label:<24} ACCEPTED requested {approved.requested:g} -> applied "
              f"{approved.value:g} {approved.tag.unit} (clamped={approved.clamped})")

    attempt("default read-only mode", "conveyor_speed_sp", 28.0)
    runtime.guard.policy = WritePolicy().allowing({"conveyor_speed_sp"})
    print(f"{'':<24} ... writes enabled for conveyor_speed_sp only")
    attempt("tag not on allow-list", "target_rate", 100)
    attempt("value out of range", "conveyor_speed_sp", 999.0)
    attempt("value in range", "conveyor_speed_sp", 28.0)
    for _ in range(5):
        try:
            runtime.guard.check("conveyor_speed_sp", 28.0, operator="demo")
        except SafetyError:
            break
    attempt("rate limit", "conveyor_speed_sp", 28.0)
    print("=" * width)
    runtime.close()
    return 0


# -- argument parsing ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(prog="factorylink", description=BANNER)
    parser.add_argument("--demo", action="store_true", help="run the full end-to-end demo")
    parser.add_argument("--duration", type=float, default=3600.0,
                        help="demo run length in simulated seconds (default 3600)")
    parser.add_argument("--seed", type=int, default=1234, help="simulator PRNG seed")
    parser.add_argument("--config", help="tag database file (.yaml or .csv)")
    sub = parser.add_subparsers(dest="command")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="tag database file (.yaml or .csv)")
        p.add_argument("--alarms", help="alarm config file (.yaml)")
        p.add_argument("--seed", type=int, default=1234, help="simulator PRNG seed")
        p.add_argument("--period", action="append", metavar="GROUP=SECONDS",
                       help="override a poll group period, repeatable")
        p.add_argument("--fault", action="append", type=parse_fault,
                       metavar="NAME@START[:DURATION]",
                       help="inject a fault into the simulated line, repeatable")

    p_scan = sub.add_parser("scan", help="poll the line and print a live table")
    common(p_scan)
    p_scan.add_argument("--duration", type=float, default=30.0, help="seconds to poll")
    p_scan.add_argument("--group", action="append", help="only show these poll groups")
    p_scan.add_argument("--real-time", action="store_true",
                        help="poll in wall-clock time and redraw the table")
    p_scan.add_argument("--refresh", type=float, default=1.0, help="redraw period in real-time mode")
    p_scan.set_defaults(func=cmd_scan)

    p_dump = sub.add_parser("dump-tags", help="print the tag database")
    p_dump.add_argument("--config", help="tag database file (.yaml or .csv)")
    p_dump.add_argument("--format", choices=("table", "yaml", "csv", "json"), default="table")
    p_dump.add_argument("--plan", action="store_true", help="also print the coalesced read plan")
    p_dump.add_argument("--max-gap", type=int, default=8,
                        help="registers of padding worth reading to avoid a second request")
    p_dump.set_defaults(func=cmd_dump_tags)

    p_sim = sub.add_parser("simulate", help="run the simulated line and report")
    common(p_sim)
    p_sim.add_argument("--duration", type=float, default=300.0, help="simulated seconds")
    p_sim.add_argument("--csv", help="export archived samples to this CSV file")
    p_sim.set_defaults(func=cmd_simulate)

    p_hist = sub.add_parser("history", help="aggregate archived history for one tag")
    common(p_hist)
    p_hist.add_argument("--tag", default="tank_level", help="tag to aggregate")
    p_hist.add_argument("--duration", type=float, default=600.0, help="simulated seconds to run")
    p_hist.add_argument("--interval", type=float, default=60.0, help="aggregation interval")
    p_hist.add_argument("--database", default=":memory:", help="SQLite file (default in-memory)")
    p_hist.add_argument("--export", help="export the tag's samples to this CSV file")
    p_hist.set_defaults(func=cmd_history)

    p_oee = sub.add_parser("oee", help="compute OEE for a simulated shift")
    common(p_oee)
    p_oee.add_argument("--duration", type=float, default=1800.0, help="simulated seconds")
    p_oee.set_defaults(func=cmd_oee)

    p_serve = sub.add_parser("serve", help="serve the live dashboard over HTTP")
    common(p_serve)
    p_serve.add_argument("--host", default="127.0.0.1", help="bind address (loopback by default)")
    p_serve.add_argument("--port", type=int, default=8377)
    p_serve.add_argument("--warmup", type=float, default=900.0,
                         help="simulated seconds of history to pre-fill")
    p_serve.add_argument("--step", type=float, default=1.0, help="seconds per background scan")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.demo or args.command is None:
        if not args.demo and args.command is None:
            parser.print_help()
            print("\nNothing to do. Try: factorylink --demo")
            return 0
        return cmd_demo(args)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
