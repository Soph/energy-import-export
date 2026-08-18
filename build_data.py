#!/usr/bin/env python3
"""
build_data.py -- fetch a date range and merge it into the JSON the dashboard reads.

Everything analytical lives in de_trade_balance.py; this only reshapes its output
into one record per day and upserts it into docs/data/daily.json, so a daily cron
and a one-off backfill are the same code path.

    ./build_data.py                          # yesterday, into docs/data/daily.json
    ./build_data.py --date 2026-07           # all of July
    ./build_data.py --date 2026-07-17 --days 31
    ./build_data.py --source entsoe          # better data, but see the warning below

Upsert is by date, so re-running a day overwrites it in place -- which is what you
want, because both sources revise the last few days after first publication. It
also means switching source rewrites those days, so the dashboard's attribution
follows along on its own.


WHY THE DEFAULT IS SMARD
-------------------------------------------------------------------------------
It is the only source that is both publishable and complete.

The trade-off looked unavoidable for a while. ENTSO-E has the better shape --
gross per-direction flows, DK_2 as its own border -- but its list of data
available for free re-use (Article 2.5 of its terms) covers physical flows at
12.1.g and does *not* cover day-ahead prices at 12.1.d or scheduled commercial
exchanges at 12.1.f. The whole 12.1.d/e/f market-results block is absent, because
it belongs to the power exchanges rather than the TSOs, and ENTSO-E cannot
sub-license rights it does not hold. energy-charts is cleanly licensed but nets
opposite flows within each border-hour, so its gross volumes run ~27% low and
Denmark arrives unsplit.

SMARD is both: Bundesnetzagentur is the primary publisher and a public authority,
its data is CC BY 4.0, and it carries each direction of each border separately.
Cross-checked against ENTSO-E for 2026-07-18 -- all 11 borders, both directions,
agreeing to under 0.02 GWh.

So the ordering is: SMARD to publish, ENTSO-E to cross-check, energy-charts as the
no-fuss fallback. (The re-use list checked was the 18 Oct 2023 revision; it gets
amended, so re-read it before leaning on the ENTSO-E half of this.)
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


def demand_daily(backend, start, end) -> pd.DataFrame | None:
    """Daily mean load and residual load in GW, when the backend has them.

    Residual load is load minus wind and solar -- how far into the dispatchable
    stack the day had to reach. It is the closest thing to an answer for "what set
    this price", which the balance itself cannot say.
    """
    if not hasattr(backend, "demand"):
        return None
    frames = []
    for ws, we in dtb.windows(start, end, "month"):
        try:
            frames.append(backend.demand(ws, we))
        except Exception as exc:
            print(f"  ! demand {ws.date()}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if not frames:
        return None
    df = pd.concat(frames).sort_index()
    df = df[(df.index >= start) & (df.index < end)]
    g = df.groupby(dtb.period_of(df.index, "day"))
    return pd.DataFrame({"load_gw": g["load_mw"].mean() / 1000,
                         "residual_gw": g["residual_mw"].mean() / 1000})


def day_records(d: pd.DataFrame, source: str, flows: str,
                demand: pd.DataFrame | None = None) -> list[dict]:
    """One record per calendar day, with its per-border breakdown nested."""
    day = dtb.per_period(d, "day")
    per_bd = dtb.per_border_period(d, "day")

    # the day's mean day-ahead price, deduplicated across borders first
    across = dtb._across_borders(d)
    px = across["price_de"]
    px = px.groupby(dtb.period_of(px.index, "day")).mean()

    # Transit: in every hour Germany both imports and exports, and the overlap is
    # power that entered by one border and left by another. min() is the tight
    # bound on it and has to be taken per MTU -- min of daily totals would hide
    # every hour whose direction differed from the day's net.
    ov = across[["import_mwh", "export_mwh"]].min(axis=1)
    key = dtb.period_of(ov.index, "day")
    transit = ov.groupby(key).sum() / 1000

    # Prices for Germany's own share only. Transit is bought and sold in the same
    # hour at the same German price, so leaving it in drags both averages toward
    # each other and understates the very gap the page is about. Weighted per MTU,
    # never an average of daily averages.
    stayed = (across["import_mwh"] - ov)
    german = (across["export_mwh"] - ov)
    px_h = across["price_de"]              # hourly; `px` above is the daily mean
    def vw(vol):
        num = (vol * px_h).groupby(key).sum()
        den = vol.groupby(key).sum()
        return (num / den).where(den > 0)
    imp_px_own, exp_px_own = vw(stayed), vw(german)
    stayed_d = stayed.groupby(key).sum() / 1000
    german_d = german.groupby(key).sum() / 1000

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
        dm = demand.loc[date] if demand is not None and date in demand.index else None
        out.append({
            "date": date,
            "source": source,
            "flows": flows,
            "price_de_mean": _n(px.get(date)),
            "load_gw": _n(dm["load_gw"]) if dm is not None else None,
            "residual_gw": _n(dm["residual_gw"]) if dm is not None else None,
            "import_gwh": _n(r["import_GWh"]),
            "export_gwh": _n(r["export_GWh"]),
            "net_import_gwh": _n(r["net_imp_GWh"]),
            "transit_gwh": _n(transit.get(date)),
            "stayed_gwh": _n(stayed_d.get(date)),
            "from_de_plants_gwh": _n(german_d.get(date)),
            "imp_px_own": _n(imp_px_own.get(date)),
            "exp_px_own": _n(exp_px_own.get(date)),
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
    ap.add_argument("--source", choices=["smard", "energy-charts", "entsoe", "demo"],
                    default=os.environ.get("DE_TRADE_SOURCE", "smard"),
                    help="default smard: CC BY 4.0 and gross per-direction flows, "
                         "the only source that is both. See the note at the top of "
                         "this file before changing it")
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

    cache = None if (a.no_cache or a.source not in ("energy-charts", "smard")) else dtb.Cache(
        dtb.default_cache_dir(), refresh=a.refresh)
    if a.source == "demo":
        backend = dtb.DemoBackend()
    elif a.source == "smard":
        backend = dtb.SmardBackend(cache=cache)
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
    demand = demand_daily(backend, start, end)
    if cache:
        cache.report()

    write(a.out, merge(a.out, day_records(d, a.source, a.flows, demand), borders))
    print(f"  -> {a.out}")


if __name__ == "__main__":
    main()
