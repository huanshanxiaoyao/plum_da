#!/usr/bin/env python3
"""Daily projection/export. No online query server or business API dependency."""

import argparse
import os
from datetime import date
from pathlib import Path

import psycopg

from ingest import db
from migrations import apply_migrations
from warehouse.project import project
from warehouse.report import demo, publish, snapshot

DEFAULT_ATTRIBUTION_SECONDS = 7 * 86400


def main(argv=None):
    parser = argparse.ArgumentParser(description="Plum daily analytics")
    parser.add_argument("--only", choices=["project", "export"])
    parser.add_argument("--attribution-seconds", type=int,
                        default=os.environ.get("PLUM_DA_ATTRIBUTION_SECONDS", DEFAULT_ATTRIBUTION_SECONDS),
                        help="click attribution window; internal-beta default: 7 days")
    parser.add_argument("--start-day", type=date.fromisoformat,
                        help="first activation only; defaults to today UTC; no historical backfill")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--demo", action="store_true", help="synthetic preview; never connects to DB")
    args = parser.parse_args(argv)
    if args.only != "project" and not args.output:
        parser.error("--output is required for export")
    if args.demo:
        if args.only or not args.output:
            parser.error("--demo requires --output and cannot use --only")
        publish(demo(), args.output)
        return 0
    if args.only != "export":
        if not args.attribution_seconds or args.attribution_seconds < 1:
            parser.error("PLUM_DA_ATTRIBUTION_SECONDS must be positive")
        with db.connect() as conn:
            apply_migrations(conn)
            conn.commit()
            project(conn, args.attribution_seconds, start_day=args.start_day)
    if args.only != "project":
        reader_url = os.environ.get("PLUM_DA_REPORT_DATABASE_URL", "")
        if not reader_url:
            parser.error("PLUM_DA_REPORT_DATABASE_URL is required (aggregate-only reader)")
        with psycopg.connect(reader_url) as conn:
            publish(snapshot(conn), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
