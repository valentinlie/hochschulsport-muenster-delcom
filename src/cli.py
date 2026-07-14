#!/usr/bin/env python3
"""hochschulsport-muenster-delcom bot — book courts from the command line."""

import argparse
import logging
import sys
from datetime import date, timedelta

from config import ACTIVITIES, DELCOM_PASSWORD, DELCOM_USERNAME


def _list_slots(activity: int, target: date) -> int:
    from core.delcom import DelcomClient, slot_is_free, slot_local_end, slot_local_time

    client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
    client.login()
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


def main():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    parser = argparse.ArgumentParser(
        prog="hochschulsport-muenster-delcom",
        description="hochschulsport-muenster-delcom bot — book Hochschulsport Münster courts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="known activity products:\n" + "\n".join(
            f"  {aid:>3}: {name}" for aid, name in sorted(ACTIVITIES.items())
        ),
    )
    parser.add_argument(
        "--activity",
        type=int,
        default=126,
        help="Activity product id (default: 126 = Tennisplatz Platzbuchung). See list below.",
    )
    date_group = parser.add_mutually_exclusive_group(required=True)
    date_group.add_argument(
        "--date",
        help="Slot date (YYYY-MM-DD)",
    )
    date_group.add_argument(
        "--offset",
        type=int,
        help="Book for N days from today (e.g. 7 = when the tennis window opens)",
    )
    parser.add_argument(
        "--time",
        help='Slot start time (HH:MM, e.g. "15:00"). Required unless --list.',
    )
    parser.add_argument(
        "--court",
        type=int,
        help="Preferred court id (default: any available court)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the whole flow (login → find slot) but stop before booking",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Just list the slots for the activity/date, then exit",
    )
    args = parser.parse_args()

    if args.offset is not None:
        target = date.today() + timedelta(days=args.offset)
    else:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            parser.error(f"invalid --date {args.date!r}, expected YYYY-MM-DD")

    if args.list:
        sys.exit(_list_slots(args.activity, target))

    if not args.time:
        parser.error("--time is required (unless --list)")

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
    sys.exit(0 if status in ("success", "dry_run") else 1)


if __name__ == "__main__":
    main()
