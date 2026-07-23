#!/usr/bin/env python3
"""Unified CLI for the Hochschulsport Münster (Delcom) booking bot.

Command groups:

- **booking**  book (ad-hoc grab / --list slots), run-job (fire a saved job —
               what the per-job systemd timer calls), jobs (list saved jobs)
- **worker**   web (serve the dashboard, optionally on a systemd socket)
- **service**  install / sync / enable / disable / status / logs — the
               systemd --user timers + socket-activated web UI
"""

import argparse
import logging
import os
import subprocess
import sys
from datetime import date, timedelta

WEB_SOCKET = "hsp-web.socket"
WEB_SERVICE = "hsp-web.service"


def _systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", "--user", *args]).returncode


# ── booking ───────────────────────────────────────────────────────────────────

def _list_slots(activity: int, target: date) -> int:
    from config import DELCOM_PASSWORD, DELCOM_USERNAME
    from core.delcom import DelcomClient, slot_is_free, slot_local_end, slot_local_time

    client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
    client.ensure_login()
    slots = client.get_slots(activity, target)
    if not slots:
        print(f"No slots returned for activity {activity} on {target}.")
        return 1
    bookings = client.get_court_bookings({s["bookableProductId"] for s in slots}, target)
    print(f"Slots for activity {activity} on {target}:")
    for s in sorted(slots, key=lambda s: (slot_local_time(s), s.get("bookableProductId", 0))):
        state = "free " if slot_is_free(s, bookings) else "taken"
        print(f"  {slot_local_time(s)}–{slot_local_end(s)}  court {s['bookableProductId']:>4}  {state}")
    return 0


def cmd_book(args: argparse.Namespace) -> int:
    from config import ACTIVITIES

    if args.offset is not None:
        target = date.today() + timedelta(days=args.offset)
    else:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: invalid --date {args.date!r}, expected YYYY-MM-DD", file=sys.stderr)
            return 2

    if args.list:
        return _list_slots(args.activity, target)

    if not args.time:
        print("ERROR: --time is required (unless --list)", file=sys.stderr)
        return 2

    activity_name = ACTIVITIES.get(args.activity, f"activity {args.activity}")
    court = f"court {args.court}" if args.court else "any available court"
    mode = " (dry run)" if args.dry_run else ""
    print(f"Target: {activity_name} (id={args.activity}), {court}")
    print(f"Date: {target}, Time: {args.time}{mode}")

    from core.booking import execute_booking

    status, message = execute_booking(
        activity_product_id=args.activity,
        target=target,
        slot_start_time=args.time,
        preferred_court_id=args.court,
        dry_run=args.dry_run,
    )
    print(f"\n{message}")
    return 0 if status in ("success", "dry_run") else 1


def cmd_run_job(args: argparse.Namespace) -> int:
    """Fire a saved job (pre-warm + spin-wait + grab). Invoked by the timer."""
    from core import db
    from core.worker import run_job

    db.init_db()
    try:
        run_job(args.id)
    finally:
        db.close_pool()
    return 0


def cmd_jobs(_: argparse.Namespace) -> int:
    from core import db

    db.init_db()
    jobs = db.get_all_jobs()
    if not jobs:
        print("No jobs.")
        return 0

    print(f"{'ID':>4}  {'ON':<3} {'ACTIVITY':<16} {'SLOT':<6} {'OFF':<4} {'FIRE':<16} NAME")
    for j in jobs:
        on = "yes" if j.enabled else "-"
        fire = f"{j.run_dow} {j.run_hour:02d}:{j.run_minute:02d}"
        act = (j.activity_option or str(j.activity_product_id))[:16]
        print(f"{j.id:>4}  {on:<3} {act:<16} {j.slot_start_time:<6} {j.date_offset:<4} {fire:<16} {j.name}")
    return 0


# ── worker ────────────────────────────────────────────────────────────────────

def cmd_web(args: argparse.Namespace) -> int:
    """Serve the dashboard. With --fd it accepts a socket passed by systemd."""
    if args.fd is None:
        # Manual run: keep the server up (the socket-activated unit passes --fd).
        os.environ.setdefault("HSP_WEB_IDLE_TIMEOUT", "0")

    import uvicorn

    if args.fd is not None:
        uvicorn.run("web.app:app", fd=args.fd)
    else:
        from config import HOST, PORT
        uvicorn.run("web.app:app", host=HOST, port=PORT)
    return 0


# ── service ───────────────────────────────────────────────────────────────────

def cmd_install(_: argparse.Namespace) -> int:
    from core import db, systemd

    db.init_db()
    systemd.install()
    systemd.sync_all_jobs()
    print("Installed systemd --user units and synced job timers.")
    print("Enable the web UI with:  hsp enable")
    return 0


def cmd_sync(_: argparse.Namespace) -> int:
    from core import db, systemd

    db.init_db()
    systemd.install()
    systemd.sync_all_jobs()
    print("Re-synced units and job timers with the database.")
    return 0


def cmd_enable(_: argparse.Namespace) -> int:
    return _systemctl("enable", "--now", WEB_SOCKET)


def cmd_disable(_: argparse.Namespace) -> int:
    return _systemctl("disable", "--now", WEB_SOCKET)


def cmd_status(_: argparse.Namespace) -> int:
    _systemctl("list-timers", "hsp-book@*", "--all")
    return _systemctl("status", "--no-pager", WEB_SOCKET, WEB_SERVICE)


def cmd_logs(args: argparse.Namespace) -> int:
    cmd = ["journalctl", "--user", "-n", str(args.lines),
           "-u", WEB_SERVICE, "-u", "hsp-book@*"]
    if args.follow:
        cmd.append("-f")
    return subprocess.run(cmd).returncode


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    from config import ACTIVITIES

    parser = argparse.ArgumentParser(
        prog="hsp", description="Hochschulsport Münster (Delcom) booking bot"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # book: ad-hoc grab / list
    p_book = sub.add_parser(
        "book", help="Book a court now, or list slots (--list)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="known activity products:\n" + "\n".join(
            f"  {aid:>3}: {name}" for aid, name in sorted(ACTIVITIES.items())
        ),
    )
    p_book.add_argument("--activity", type=int, default=126,
                        help="Activity product id (default: 126 = Tennis)")
    g = p_book.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="Slot date (YYYY-MM-DD)")
    g.add_argument("--offset", type=int, help="Book for N days from today")
    p_book.add_argument("--time", help='Slot start time (HH:MM). Required unless --list.')
    p_book.add_argument("--court", type=int, help="Preferred court id (default: any)")
    p_book.add_argument("--dry-run", action="store_true",
                        help="Run the flow but stop before booking")
    p_book.add_argument("--list", action="store_true",
                        help="Just list the slots for the activity/date")
    p_book.set_defaults(func=cmd_book)

    # run-job: fire a saved job (used by the timer)
    p_run = sub.add_parser("run-job", help="Fire a saved job by id (pre-warm + grab)")
    p_run.add_argument("id", type=int, help="Job ID (see `hsp jobs`)")
    p_run.set_defaults(func=cmd_run_job)

    sub.add_parser("jobs", help="List saved jobs").set_defaults(func=cmd_jobs)

    # web
    p_web = sub.add_parser("web", help="Serve the dashboard")
    p_web.add_argument("--fd", type=int, default=None,
                       help="Serve on a socket passed by systemd (fd 3)")
    p_web.set_defaults(func=cmd_web)

    # service management
    sub.add_parser("install", help="Write the systemd --user units and job timers").set_defaults(func=cmd_install)
    sub.add_parser("sync", help="Re-sync units and job timers with the DB").set_defaults(func=cmd_sync)
    sub.add_parser("enable", help="Enable + start the web socket").set_defaults(func=cmd_enable)
    sub.add_parser("disable", help="Disable + stop the web socket").set_defaults(func=cmd_disable)
    sub.add_parser("status", help="Show job timers and web-UI status").set_defaults(func=cmd_status)

    p_logs = sub.add_parser("logs", help="Show service logs")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="Lines to show")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow the log")
    p_logs.set_defaults(func=cmd_logs)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
