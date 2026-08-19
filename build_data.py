#!/usr/bin/env python3
"""
build_data.py -- fetch a date range and merge it into the JSON the dashboard reads.

Everything analytical lives in de_trade_balance.py; this only reshapes its output
into one record per day and upserts it into data/<YYYY-MM>.json, one file per
calendar month, then rewrites data/index.json. A daily cron and a one-off
backfill are the same code path.

Monthly files rather than one store: the page reads the index to learn what exists,
then fetches only the months its selected range touches. A single file would make
someone download every day ever recorded to look at the last week.

    ./build_data.py                          # yesterday, into data/
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

SMARD is both: Bundesnetzagentur grants CC BY 4.0 itself, under its own publication
mandate in § 111d EnWG rather than as an ENTSO-E data user, and it carries each
direction of each border separately.
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
import re
import sys

import pandas as pd

import de_trade_balance as dtb

HERE = os.path.dirname(os.path.abspath(__file__))
# Repo root: index.html sits beside data/, and the deploy assembles the published
# site from just those two rather than the layout having to match what is served.
OUT = os.path.join(HERE, "data")
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


def market_values(backend, start, end, price: pd.Series) -> pd.DataFrame | None:
    """Per day: how much each technology's output was worth at the hourly price.

    The generation-weighted price is the official Marktwert, and the gap to the
    plain average is the cannibalisation that drives the EEG top-up: solar all
    arrives in the same hours and depresses the price in exactly those hours.
    Stored with the volumes so the page can re-weight across any window.
    """
    if not hasattr(backend, "generation"):
        return None
    frames = []
    for ws, we in dtb.windows(start, end, "month"):
        try:
            frames.append(backend.generation(ws, we))
        except Exception as exc:
            print(f"  ! generation {ws.date()}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if not frames:
        return None
    g = pd.concat(frames).sort_index()
    g = g[(g.index >= start) & (g.index < end)]
    j = g.join(price.rename("_px"), how="inner").dropna(subset=["_px"])
    if j.empty:
        return None
    key = dtb.period_of(j.index, "day")
    out = {}
    for tech in [c for c in g.columns]:
        vol = j[tech].groupby(key).sum()
        val = (j[tech] * j["_px"]).groupby(key).sum()
        out[f"{tech}_gwh"] = vol / 1000
        out[f"{tech}_mv"] = (val / vol).where(vol > 0)
    return pd.DataFrame(out)


def day_records(d: pd.DataFrame, source: str, flows: str,
                demand: pd.DataFrame | None = None,
                mv: pd.DataFrame | None = None) -> list[dict]:
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

    # The German price weighted by Germany's own share of the flows. Note what this
    # is and is not: it is the price the German zone cleared at during importing or
    # exporting hours, not a payment for imported energy -- nobody buys a labelled
    # imported MWh, everyone in the zone pays that hour's German price. Transit is
    # excluded because it is bought and sold in the same hour at the same price, so
    # leaving it in drags both averages together. Weighted per MTU, never an average
    # of daily averages.
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
        mrow = mv.loc[date] if mv is not None and date in mv.index else None
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
            **({k: _n(v) for k, v in mrow.items()} if mrow is not None else {}),
        })
    return out


def merge_months(outdir: str, fresh: list[dict], borders_asked: list) -> list[str]:
    """Upsert the fetched days into one file per calendar month, then reindex.

    Monthly files rather than one growing store: the page only needs the months its
    selected range touches, and a single file would have the reader download every
    day ever recorded to look at the last week. Upsert still happens per date, so a
    re-fetch corrects days in place.
    """
    os.makedirs(outdir, exist_ok=True)
    by_month: dict[str, list[dict]] = {}
    for r in fresh:
        by_month.setdefault(r["date"][:7], []).append(r)

    touched = []
    for month, rows in sorted(by_month.items()):
        path = os.path.join(outdir, f"{month}.json")
        try:
            with open(path) as fh:
                existing = {d["date"]: d for d in json.load(fh)["days"]}
        except (OSError, ValueError, KeyError):
            existing = {}
        added = sum(1 for r in rows if r["date"] not in existing)
        existing.update({r["date"]: r for r in rows})
        _write(path, {"month": month, "days": [existing[k] for k in sorted(existing)]})
        print(f"  {month}: {added} new, {len(rows) - added} updated, "
              f"{len(existing)} in file")
        touched.append(month)

    reindex(outdir, borders_asked)
    return touched


def reindex(outdir: str, borders_asked: list) -> None:
    """Rebuild the manifest from whatever month files are on disk.

    The page reads this first to learn what exists, so it can work out which months
    a range needs without downloading any of them.
    """
    months = []
    for name in sorted(os.listdir(outdir)):
        m = re.fullmatch(r"(\d{4}-\d{2})\.json", name)
        if not m:
            continue
        try:
            with open(os.path.join(outdir, name)) as fh:
                days = json.load(fh)["days"]
        except (OSError, ValueError, KeyError):
            continue
        if not days:
            continue
        months.append({"month": m.group(1), "days": len(days),
                       "first": days[0]["date"], "last": days[-1]["date"],
                       "sources": sorted({d.get("source") for d in days if d.get("source")})})
    total = sum(x["days"] for x in months)
    _write(os.path.join(outdir, "index.json"), {
        "schema": SCHEMA,
        "generated_at": pd.Timestamp.now(tz=dtb.TZ).isoformat(timespec="seconds"),
        "timezone": dtb.TZ,
        "borders_requested": list(borders_asked),
        "units": {"gwh": "GWh", "keur": "thousand EUR", "px": "EUR/MWh", "gw": "GW"},
        "months": months,
        "total_days": total,
        "first_date": months[0]["first"] if months else None,
        "last_date": months[-1]["last"] if months else None,
    })
    print(f"  index: {len(months)} month file(s), {total} days")


def _write(path: str, obj: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=False)
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
    ap.add_argument("--out", default=OUT,
                    help=f"directory for the monthly files (default: {OUT})")
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
    mv = market_values(backend, start, end, dtb._across_borders(d)["price_de"])
    if cache:
        cache.report()

    merge_months(a.out, day_records(d, a.source, a.flows, demand, mv), borders)
    print(f"  -> {a.out}/")


if __name__ == "__main__":
    main()
