#!/usr/bin/env python3
"""
build_data.py -- fetch a date range and merge it into the JSON the dashboard reads.

Everything analytical lives in de_trade_balance.py; this only reshapes its output
into one record per day and upserts it into docs/data/daily.json, so a daily cron
and a one-off backfill are the same code path.

    ./build_data.py                          # yesterday, into docs/data/daily.json
    ./build_data.py --date 2026-07           # all of July
    ./build_data.py --date 2026-07-17 --days 31
    ./build_data.py --source entsoe          # gross per-direction flows, needs a token

Upsert is by date, so re-running a day overwrites it in place -- which is what you
want, because both sources revise the last few days after first publication.

Each record carries the source it came from. That matters: balance_at_de_price,
congestion_rent and the whole daily series are net-only and agree across sources
to well under a percent, but the gross columns (import_gwh, export_gwh, the
volume-weighted prices) are ~26% low from energy-charts, which reports flows
already netted. The dashboard says which source it is showing for that reason.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

import de_trade_balance as dtb

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "docs", "data", "daily.json")
SCHEMA = 1


def _n(v, digits=2):
    """JSON-safe number: NaN/inf become null, everything else rounds."""
    if v is None or not pd.notna(v):
        return None
    return round(float(v), digits)


def day_records(d: pd.DataFrame, source: str, flows: str) -> list[dict]:
    """One record per calendar day, with its per-border breakdown nested."""
    day = dtb.per_period(d, "day")
    per_bd = dtb.per_border_period(d, "day")

    # the day's mean day-ahead price, deduplicated across borders first
    px = dtb._across_borders(d)["price_de"]
    px = px.groupby(dtb.period_of(px.index, "day")).mean()

    out = []
    for date, r in day.iterrows():
        borders = {}
        if date in per_bd.index.get_level_values("day"):
            for border, b in per_bd.xs(date, level="day").iterrows():
                borders[border] = {
                    "import_gwh": _n(b["import_GWh"]),
                    "export_gwh": _n(b["export_GWh"]),
                    "net_import_gwh": _n(b["net_imp_GWh"]),
                    "bal_de_keur": _n(b["bal_de_kEUR"], 1),
                    "rent_keur": _n(b["rent_kEUR"], 1),
                    "mean_spread": _n(b["mean_spread"]),
                }
        out.append({
            "date": date,
            "source": source,
            "flows": flows,
            "price_de_mean": _n(px.get(date)),
            "import_gwh": _n(r["import_GWh"]),
            "export_gwh": _n(r["export_GWh"]),
            "net_import_gwh": _n(r["net_imp_GWh"]),
            "imp_px_de": _n(r["imp_px_de"]),
            "exp_px_de": _n(r["exp_px_de"]),
            "bal_de_keur": _n(r["bal_de_kEUR"], 1),
            "bal_zonal_keur": _n(r["bal_zonal_kEUR"], 1),
            "rent_keur": _n(r["rent_kEUR"], 1),
            "borders": borders,
        })
    return out


def merge(path: str, fresh: list[dict], borders_asked: list) -> dict:
    """Upsert by date into whatever is already on disk."""
    try:
        with open(path) as fh:
            store = json.load(fh)
    except (OSError, ValueError):
        store = {}

    by_date = {r["date"]: r for r in store.get("days", [])}
    added = sum(1 for r in fresh if r["date"] not in by_date)
    by_date.update({r["date"]: r for r in fresh})

    store["schema"] = SCHEMA
    store["generated_at"] = pd.Timestamp.now(tz=dtb.TZ).isoformat(timespec="seconds")
    store["timezone"] = dtb.TZ
    store["borders_requested"] = list(borders_asked)
    store["units"] = {"gwh": "GWh", "keur": "thousand EUR", "px": "EUR/MWh"}
    store["days"] = [by_date[k] for k in sorted(by_date)]
    print(f"  {added} new day(s), {len(fresh) - added} updated, "
          f"{len(store['days'])} total")
    return store


def write(path: str, store: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(store, fh, indent=1, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=(pd.Timestamp.now(tz=dtb.TZ) - pd.Timedelta(days=1))
                    .strftime("%Y-%m-%d"),
                    help="start: YYYY-MM-DD, YYYY-MM or YYYY (default: yesterday)")
    ap.add_argument("--end", help="last day, inclusive; same forms as --date")
    ap.add_argument("--days", type=int, help="length in days instead of --end")
    ap.add_argument("--flows", choices=["scheduled", "physical"], default="scheduled")
    ap.add_argument("--freq", default="60min")
    ap.add_argument("--borders", help="comma-separated subset")
    ap.add_argument("--source", choices=["energy-charts", "entsoe", "demo"],
                    default=os.environ.get("DE_TRADE_SOURCE", "energy-charts"))
    ap.add_argument("--token", default=os.environ.get("ENTSOE_API_TOKEN"))
    ap.add_argument("--out", default=OUT, help=f"JSON store (default: {OUT})")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    start = pd.Timestamp(a.date, tz=dtb.TZ).normalize()
    end = (pd.Timestamp(a.end, tz=dtb.TZ).normalize() + dtb.span_of(a.end) if a.end
           else start + pd.DateOffset(days=a.days) if a.days
           else start + dtb.span_of(a.date))
    if end <= start:
        sys.exit("Empty range.")
    borders = [b.strip() for b in a.borders.split(",")] if a.borders else dtb.NEIGHBOURS

    cache = None if (a.no_cache or a.source != "energy-charts") else dtb.Cache(
        dtb.default_cache_dir(), refresh=a.refresh)
    if a.source == "demo":
        backend = dtb.DemoBackend()
    elif a.source == "entsoe":
        if not a.token:
            sys.exit("Set ENTSOE_API_TOKEN or pass --token.")
        backend = dtb.EntsoeBackend(a.token)
    else:
        backend = dtb.EnergyChartsBackend(cache=cache)

    print(f"{start.date()} .. {(end - pd.Timedelta(days=1)).date()} "
          f"via {a.source} ({a.flows})", file=sys.stderr)
    raw = dtb.collect_range(backend, start, end, a.freq, a.flows == "physical",
                            borders, "month")
    d = dtb.value(raw, pd.Timedelta(a.freq) / pd.Timedelta("1h"))
    if cache:
        cache.report()

    write(a.out, merge(a.out, day_records(d, a.source, a.flows), borders))
    print(f"  -> {a.out}")


if __name__ == "__main__":
    main()
