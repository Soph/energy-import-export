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


def system_costs(backend, outdir: str) -> None:
    """Write the TSO's monthly system-security costs, with monthly congestion rent.

    Both sides in one file, on purpose. The rent could be summed from the daily
    files, but then the panel would only work for months the reader happened to have
    loaded -- and since the cost series lags about three months, the default recent
    window has no cost data at all and the panel would be permanently empty. As a
    monthly pair it is self-contained and always populated.

    Its own file rather than columns on the daily records: it is monthly,
    Germany-wide rather than per border, and each series publishes on a different lag.
    """
    if not hasattr(backend, "costs"):
        return
    try:
        c = backend.costs(pd.Timestamp("2024-01-01", tz=dtb.TZ),
                          pd.Timestamp.now(tz=dtb.TZ).normalize() + pd.DateOffset(months=1))
    except Exception as exc:
        print(f"  ! system costs: {type(exc).__name__}: {exc}", file=sys.stderr)
        return

    # congestion rent per month, from whatever month files exist, complete ones only
    rent, complete = {}, {}
    for name in sorted(os.listdir(outdir)):
        m = re.fullmatch(r"(\d{4}-\d{2})\.json", name)
        if not m:
            continue
        try:
            with open(os.path.join(outdir, name)) as fh:
                days = json.load(fh)["days"]
        except (OSError, ValueError, KeyError):
            continue
        month = m.group(1)
        in_month = pd.Period(month).days_in_month
        rent[month] = sum((d.get("rent_keur") or 0) for d in days) * 1000
        complete[month] = len(days) == in_month

    months = []
    for ts, r in c.iterrows():
        key = ts.strftime("%Y-%m")
        row = {"month": key}
        row.update({k: _n(v, 0) for k, v in r.items()})
        if key in rent and complete.get(key):
            row["congestion_rent"] = _n(rent[key], 0)
        if any(v is not None for k, v in row.items() if k != "month"):
            months.append(row)

    paired = sum(1 for m in months if m.get("congestion_rent") is not None
                 and (m.get("grid_security") or 0) > 0)
    _write(os.path.join(outdir, "system_costs.json"), {
        "note": ("Monthly costs to the transmission operators of keeping the system "
                 "secure, in EUR, paired with the congestion rent earned on the DE-LU "
                 "borders in the same month. grid_security is the redispatch family "
                 "reported together -- redispatch, grid reserve, contracted load "
                 "reduction -- "
                 "and cannot be split further here; measures instructed by distribution "
                 "operators are not included. Cost series publish on different lags, so "
                 "recent months are often absent. Rent appears only for months whose "
                 "daily data is complete."),
        "source": "Bundesnetzagentur | SMARD.de, Kosten der ÜNB (region DE)",
        "licence": "CC BY 4.0",
        "generated_at": pd.Timestamp.now(tz=dtb.TZ).isoformat(timespec="seconds"),
        "months": months,
    })
    print(f"  system_costs: {len(months)} month(s), {paired} with both sides")


# Grouped into five, cheap-to-dear, because a merit order is an *ordered* category
# and eight-plus technologies in one stack stops being readable. Nuclear is omitted
# rather than bucketed: Germany's last reactors closed in 2023 and the series is zero.
# Renewables first, then pumped storage, then the conventional plant by ascending marginal
# cost -- so the renewable/conventional line is horizontal in the stack. Not a strict merit
# order: storage discharges into peak prices and in a true merit order would sit near the
# top, but keeping it between the two families is what makes the boundary legible.
# Note what this split is and is not: the first three are SMARD's own "Erneuerbare
# Energietraeger" list, which includes Biomasse -- a combustion plant that buys fuel, and
# about 11% of the bucket. So this is a renewable/conventional boundary, NOT a
# zero-marginal-cost one, and anything labelled "no fuel cost" would be wrong.
# Pumped storage is deliberately its own bucket rather than filed under hydro -- it is not
# renewable generation, it re-emits energy something else made earlier, and calling it
# renewable would undercut the one distinction this panel is drawing. SMARD's Pumpspeicher
# module reports discharge only (charging is a separate consumption series), so it never
# arrives negative.
STACK = {
    "solar":     [1004068],
    "wind":      [1004067, 1001225],
    "hydro_bio": [1001226, 1004066, 1001228],            # hydro, biomass, other RES
    "storage":   [1004070],                              # pumped storage, discharging
    "nuclear":   [1001224],                              # zero after 15 April 2023
    "coal":      [1001223, 1004069],                     # lignite, hard coal
    "gas_other": [1004071, 1001227],                     # gas, other conventional
}
# Nuclear is here even though it is identically zero for every day after 15 April 2023,
# when the last three reactors shut. Leaving it out was safe while the record started in
# 2026 -- and it was checked, the module summed to zero. It stops being safe the moment any
# pre-2023 day is fetched: nuclear ran at 2.6-7.6 GW, and because the renewable share is
# renewables over *total* generation, dropping it from the denominator inflated the share.
# Measured on 2021-06-05 the page would have shown 50.2% renewable where the truth is
# 41.8%, an 8.4-point overstatement that is invisible in 2026 data.
# The renewable buckets, matching SMARD's own category list. Used for the renewable
# share the pages quote. Deliberately not called "fuel-free": Biomasse burns.
RENEWABLE = ("solar", "wind", "hydro_bio")


def example_day(backend, outdir: str, start, end) -> None:
    """Write one real day, hour by hour, as a worked example of how a price happens.

    Picks the day with the widest intraday price spread, because that is the one where
    the mechanism is visible: the stack fills bottom-up with whatever is cheapest, and
    the price follows whatever had to run last. Records which day and why, so the choice
    is not mistaken for a hand-picked illustration.

    Only replaces the day on file if this range beats it. The candidate is the widest day
    in the *refreshed* range, and the daily job refreshes three days -- so without this
    the cron would overwrite a 672 EUR/MWh day with the best of last Tuesday to Thursday
    while the page went on saying the choice was automatic. The claim is "the widest day
    we have seen", so the file has to be allowed to keep it.
    """
    if not hasattr(backend, "generation"):
        return
    ids = [i for v in STACK.values() for i in v]
    try:
        frames, prices = [], []
        for ws, we in dtb.windows(start, end, "month"):
            frames.append(pd.DataFrame(backend._fetch(ids, ws, we)))
            prices.append(backend._prices_for(ws, we)["DE_LU"])
        gen = pd.concat(frames).sort_index()
        px = pd.concat(prices).sort_index()
        load = pd.concat([backend.demand(ws, we)["load_mw"]
                          for ws, we in dtb.windows(start, end, "month")]).sort_index()
    except Exception as exc:
        print(f"  ! example day: {type(exc).__name__}: {exc}", file=sys.stderr)
        return

    j = gen.join(px.rename("price"), how="inner").join(load.rename("load"), how="inner")
    j = j[(j.index >= start) & (j.index < end)].dropna(subset=["price"])
    if j.empty:
        return
    step_minutes = (j.index.to_series().diff().dropna().median().total_seconds() / 60
                    if len(j) > 1 else 60.0)
    spread = j.groupby(dtb.period_of(j.index, "day"))["price"].agg(lambda s: s.max() - s.min())
    pick = spread.idxmax()
    path = os.path.join(outdir, "example_day.json")
    try:
        with open(path) as fh:
            held = json.load(fh)
    except (OSError, ValueError):
        held = None
    if held and held.get("spread_eur_mwh", 0) > spread[pick] and held.get("date") != pick:
        print(f"  example_day: kept {held['date']} "
              f"({held['spread_eur_mwh']:.0f} EUR/MWh beats this range's {spread[pick]:.0f})")
        return
    day = j[dtb.period_of(j.index, "day") == pick]

    # every module in STACK needs an entry here, or the bucket raises on lookup
    LABEL = {1004068: "Photovoltaik", 1004067: "Wind Onshore", 1001225: "Wind Offshore",
             1001226: "Wasserkraft", 1004070: "Pumpspeicher", 1004066: "Biomasse",
             1001228: "Sonstige Erneuerbare", 1001224: "Kernenergie",
             1001223: "Braunkohle", 1004069: "Steinkohle", 1004071: "Erdgas",
             1001227: "Sonstige Konventionelle"}
    missing = [i for v in STACK.values() for i in v if i not in LABEL]
    assert not missing, f"STACK modules with no LABEL: {missing}"
    hours = []
    for ts, r in day.iterrows():
        row = {"hour": ts.strftime("%H:%M"), "price": _n(r["price"]), "load_gw": _n(r["load"] / 1000)}
        for bucket, mods in STACK.items():
            cols = [LABEL[i] for i in mods if LABEL[i] in day.columns]
            row[bucket] = _n(sum(r[c] for c in cols) / 1000)
        hours.append(row)

    _write(path, {
        "date": pick,
        "spread_eur_mwh": _n(spread[pick]),
        # The record now spans both market eras, and the widest-spread day can fall in
        # either. The page states the resolution rather than promising one.
        "step_minutes": _n(step_minutes, 0),
        "chosen_because": (f"widest intraday price spread on record: "
                           f"{spread[pick]:.0f} EUR/MWh"),
        "note": ("Generation is what actually ran, grouped cheap-to-dear. The ordering is "
                 "the conventional merit order, not observed bids -- what each unit offered "
                 "is not public, so the stack shows volumes, not costs."),
        "source": "Bundesnetzagentur | SMARD.de",
        "buckets": list(STACK),
        "renewable_buckets": list(RENEWABLE),
        "hours": hours,
    })
    print(f"  example_day: {pick} ({spread[pick]:.0f} EUR/MWh spread), {len(hours)} hours")


def wind_shortfall(backend, start, end, price: pd.Series
                   ) -> tuple[pd.DataFrame, dict] | tuple[None, None]:
    """Per day: how far wind ran below its own day-ahead forecast, split by price sign.

    Signed, never clipped. An earlier version summed max(0, forecast - actual) over
    negative-price hours, which is not a measure of anything: forecast error is roughly
    symmetric, so discarding the negative side accumulates ordinary noise into a large
    positive number. Over Jan-Aug it reported 781 GWh where the signed deficit was
    563 GWh -- a 39% inflation -- and on nine days it showed a positive "missing" bar
    for days when wind ran *above* forecast in exactly the hours being counted. Under a
    sign-flip null the clipped statistic returns ~500 GWh from pure noise, so most of
    what it reported was its own one-sidedness.

    Both hour sets are carried so the page can state a like-for-like control: the same
    signed measure, as a share of forecast, in negative-price hours against every other
    hour. Comparing a clipped subset sum against a signed all-hours sum -- which the
    page used to do -- is what let a 32 GWh claim be "supported" by a -142 GWh control.

    Returns the daily frame and the adjacency profile (see `adjacency`), which is the
    evidence that actually identifies the effect.
    """
    if not (hasattr(backend, "wind_forecast") and hasattr(backend, "generation")):
        return None, None
    fc, act = [], []
    for ws, we in dtb.windows(start, end, "month"):
        try:
            fc.append(backend.wind_forecast(ws, we))
            g = backend.generation(ws, we)
            act.append(g[[c for c in ("wind_onshore", "wind_offshore") if c in g]].sum(axis=1))
        except Exception as exc:
            print(f"  ! wind shortfall {ws.date()}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if not fc or not act:
        return None, None
    j = pd.DataFrame({"fc": pd.concat(fc).sort_index(),
                      "act": pd.concat(act).sort_index()}).join(price.rename("px"), how="inner")
    j = j[(j.index >= start) & (j.index < end)].dropna(subset=["fc", "act"])
    if j.empty:
        return None, None

    j["deficit"] = j.fc - j.act                 # signed: positive means wind ran short
    below = j.px < 0
    # fc and act are MW, so a sum over intervals is only MWh at hourly resolution --
    # at 15min each reading covers a quarter of an hour. Getting this wrong inflates
    # every volume by 4 while leaving the percentages right, which is exactly the shape
    # of bug that hides.
    step = (j.index.to_series().diff().dropna().median().total_seconds() / 60
            if len(j) > 1 else 60.0)
    hrs = step / 60.0
    key = dtb.period_of(j.index, "day")
    daily = pd.DataFrame({
        "wind_forecast_gwh": j.fc.groupby(key).sum() * hrs / 1000,
        "wind_actual_gwh": j.act.groupby(key).sum() * hrs / 1000,
        # Only the raw daily observations. The negative/positive split used to ship here
        # too, as a per-window control -- but the panel establishes that per-window is
        # meaningless for this effect, so the split is only computed over the full record,
        # in wind_adjacency.json. Nothing read these six fields.
        "negative_price_hours": below.groupby(key).sum() * hrs,
    })
    return daily, adjacency(j, below, step)


def adjacency(j: pd.DataFrame, below: pd.Series, step_minutes: float) -> dict:
    """The deficit as a share of forecast, by distance in time from a negative price.

    This is the test that identifies the effect. A badly-forecast windy episode is smooth
    in time: it cannot produce a shortfall one market interval wide. A price-triggered
    shutdown can, and does.

    Lags are expressed in *minutes*, not index steps, so the chart means the same thing
    whichever resolution the run used -- at quarter-hour resolution an offset of one step
    is 15 minutes, and labelling that "1 h" would quietly overstate how sharp the
    discontinuity is. The window either side is held at one hour and filled with whatever
    steps that takes.

    Neighbouring intervals that are themselves below zero are excluded from the lags, so
    the control is uncontaminated rather than quietly counting the same intervals again.
    """
    hrs = step_minutes / 60.0            # MW -> MWh for one interval
    per_hour = max(1, int(round(60.0 / step_minutes)))
    lags = list(range(-per_hour, per_hour + 1))
    idx = j.index
    pos = pd.Series(range(len(idx)), index=idx)
    neg_at = pos[below].to_numpy()
    prof = {}
    for k in lags:
        want = neg_at + k
        want = want[(want >= 0) & (want < len(idx))]
        rows = j.iloc[want]
        if k != 0:                                    # keep the control clean
            rows = rows[rows.px >= 0]
        if rows.empty or rows.fc.sum() <= 0:
            continue
        prof[str(k)] = {
            "minutes": int(round(k * step_minutes)),
            "intervals": int(len(rows)),
            "share_of_forecast_pct": _n(rows.deficit.sum() / rows.fc.sum() * 100, 2),
            "deficit_gwh": _n(rows.deficit.sum() * hrs / 1000, 1),
        }

    # Per month over the same full record, so the panel's seasonal caveat is on the same
    # basis as its headline rather than on whatever months a reader happened to load.
    neg = j[below]
    by_month = []
    for m, g in neg.groupby(neg.index.strftime("%Y-%m")):
        if g.fc.sum() <= 0:
            continue
        by_month.append({
            "month": m,
            "intervals": int(len(g)),
            "deficit_gwh": _n(g.deficit.sum() * hrs / 1000, 1),
            "forecast_gwh": _n(g.fc.sum() * hrs / 1000, 1),
            "share_of_forecast_pct": _n(g.deficit.sum() / g.fc.sum() * 100, 2),
        })

    other = j[~below]
    return {
        "control_other_intervals": {
            "intervals": int(len(other)),
            "hours_equiv": _n(len(other) * step_minutes / 60, 0),
            "deficit_gwh": _n(other.deficit.sum() * hrs / 1000, 1),
            "share_of_forecast_pct": _n(
                other.deficit.sum() / other.fc.sum() * 100, 2) if other.fc.sum() > 0 else None,
        },
        "note": ("Wind deficit against day-ahead forecast, as a share of that forecast, by "
                 "distance in minutes from a market interval whose day-ahead price was "
                 "below zero. Lag 0 is the negative interval itself; other lags exclude "
                 "intervals that were themselves below zero, so they are a clean control. "
                 "A shortfall one interval wide cannot be forecast error. by_month covers "
                 "the negative-price intervals only, over the same record."),
        "profile": prof,
        "by_month": by_month,
        "step_minutes": _n(step_minutes, 0),
        "negative_intervals": int(below.sum()),
        "negative_hours_equiv": _n(below.sum() * step_minutes / 60, 1),
        "intervals_total": int(len(j)),
        "covers": [str(idx[0].date()), str(idx[-1].date())],
    }


def seasons(outdir: str) -> None:
    """Monthly aggregates, for the panel that shows January and July are different systems.

    Read back from the month files rather than from this run's range, so it always covers
    the whole record -- the same reason `system_costs` reads the rent from disk. Complete
    months only: a half-finished month would plot as a low bar rather than a missing one,
    which is the more misleading of the two.

    This is the project's strongest finding and it was invisible on the page: the price gap
    is largely a summer effect, Germany flips from net exporter to net importer, and solar's
    cannibalisation discount disappears entirely in winter.
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
        key = m.group(1)
        if len(days) != pd.Period(key).days_in_month:
            continue                                    # incomplete month
        tot = lambda f: sum((d.get(f) or 0) for d in days)
        wavg = lambda pf, vf: (sum((d.get(pf) or 0) * (d.get(vf) or 0) for d in days)
                               / tot(vf)) if tot(vf) else None
        # Raw numerator/denominator pairs as well as the ratios. The claim page needs to
        # aggregate arbitrary periods, and a mean of monthly means is not the mean -- with
        # these it can sum months and divide, which is exact. It also means that page loads
        # one small file instead of every month file; at 92 months that was 7.5 MB.
        wsum = lambda pf, vf: sum((d.get(pf) or 0) * (d.get(vf) or 0) for d in days)
        imp_px, exp_px = wavg("imp_px_own", "stayed_gwh"), wavg("exp_px_own", "from_de_plants_gwh")
        solar_mv = wavg("solar_mv", "solar_gwh")
        # mean of daily means: every day carries equal weight, which is what "an average
        # day that month" means. Not volume-weighted, and it is labelled as such.
        da = sum(d["price_de_mean"] for d in days if d.get("price_de_mean") is not None) / len(days)
        months.append({
            "month": key,
            "days": len(days),
            "price_gap": _n(imp_px - exp_px, 1) if None not in (imp_px, exp_px) else None,
            "px_importing": _n(imp_px, 1),
            "px_exporting": _n(exp_px, 1),
            "net_import_gwh": _n(tot("net_import_gwh"), 0),
            "day_ahead_mean": _n(da, 1),
            "solar_mv": _n(solar_mv, 1),
            "solar_vs_market": _n(solar_mv - da, 1) if solar_mv is not None else None,
            "rent_meur": _n(tot("rent_keur") / 1000, 1),
            # everything below is for exact re-aggregation, not for reading
            "bal_keur": _n(tot("bal_de_keur"), 1),
            "rent_keur": _n(tot("rent_keur"), 1),
            "import_gwh": _n(tot("import_gwh"), 1),
            "export_gwh": _n(tot("export_gwh"), 1),
            "transit_gwh": _n(tot("transit_gwh"), 1),
            "stayed_gwh": _n(tot("stayed_gwh"), 1),
            "from_de_plants_gwh": _n(tot("from_de_plants_gwh"), 1),
            "load_gwh": _n(tot("load_gw") * 24, 1),
            "w_imp_de": _n(wsum("imp_px_de", "import_gwh"), 1),
            "w_exp_de": _n(wsum("exp_px_de", "export_gwh"), 1),
            "w_imp_own": _n(wsum("imp_px_own", "stayed_gwh"), 1),
            "w_exp_own": _n(wsum("exp_px_own", "from_de_plants_gwh"), 1),
            "step_minutes": _n(max((d.get("step_minutes") or 60) for d in days), 0),
        })
    if not months:
        return
    _write(os.path.join(outdir, "seasons.json"), {
        "note": ("One row per complete calendar month. price_gap is the volume-weighted "
                 "day-ahead price in importing hours minus exporting hours, on the "
                 "transit-free basis. day_ahead_mean is the mean of daily means, so every "
                 "day counts equally. solar_vs_market is solar's generation-weighted "
                 "capture price against that mean -- negative is the cannibalisation "
                 "discount. Positive net_import_gwh means Germany was a net importer."),
        "source": "Bundesnetzagentur | SMARD.de",
        "licence": "CC BY 4.0",
        "generated_at": pd.Timestamp.now(tz=dtb.TZ).isoformat(timespec="seconds"),
        "months": months,
    })
    flips = sum(1 for a, b in zip(months, months[1:])
                if (a["net_import_gwh"] or 0) * (b["net_import_gwh"] or 0) < 0)
    print(f"  seasons: {len(months)} complete month(s), {flips} net-position flip(s)")


def write_adjacency(outdir: str, adj: dict | None) -> None:
    """Write the adjacency profile, but never let a narrow run overwrite a wide one.

    This statistic is only meaningful over a long record, and the default range is
    yesterday. Without this guard the daily cron would recompute it from a single day and
    publish that. `example_day` carries the same guard for the same reason.
    """
    if not adj or not adj.get("profile"):
        return
    path = os.path.join(outdir, "wind_adjacency.json")
    try:
        with open(path) as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = None
    # A coarser run must never replace a finer one. The lag axis is in minutes, so an
    # hourly run would relabel the discontinuity as an hour wide when the finer data shows
    # it is fifteen minutes -- a weaker claim presented as the same one. Backfilling 2019
    # or 2021, which are necessarily hourly, would otherwise silently do this.
    if old and old.get("step_minutes", 60) < adj["step_minutes"]:
        print(f"  wind_adjacency: kept ({old['step_minutes']}min on file is finer than this "
              f"run's {adj['step_minutes']}min)")
        return
    if old and old.get("step_minutes", 60) == adj["step_minutes"] \
           and old.get("negative_hours_equiv", 0) > adj["negative_hours_equiv"]:
        print(f"  wind_adjacency: kept ({old['negative_hours_equiv']}h of negative prices "
              f"on file beats this run's {adj['negative_hours_equiv']}h)")
        return
    adj["generated_at"] = pd.Timestamp.now(tz=dtb.TZ).isoformat(timespec="seconds")
    _write(path, adj)
    lag0 = adj["profile"].get("0", {}).get("share_of_forecast_pct")
    print(f"  wind_adjacency: {adj['negative_intervals']} negative intervals "
          f"({adj['negative_hours_equiv']}h) at {adj['step_minutes']}min, "
          f"lag 0 = {lag0}% of forecast")


def day_records(d: pd.DataFrame, source: str, flows: str,
                demand: pd.DataFrame | None = None,
                mv: pd.DataFrame | None = None,
                wind: pd.DataFrame | None = None) -> list[dict]:
    """One record per calendar day, with its per-border breakdown nested."""
    # Recorded per day because the record now spans two market eras: hourly before the
    # 15-minute MTU went live on 1 October 2025, quarter-hourly after. Anything that counts
    # intervals is only comparable if you know which grid produced it.
    # the frame is indexed (border, mtu); the timestamps are level "mtu"
    idx = d.index.get_level_values("mtu") if isinstance(d.index, pd.MultiIndex) else d.index
    uniq = pd.DatetimeIndex(pd.Index(idx).unique()).sort_values()
    step_minutes = (uniq.to_series().diff().dropna().median().total_seconds() / 60
                    if len(uniq) > 1 else 60.0)
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
        wrow = wind.loc[date] if wind is not None and date in wind.index else None
        out.append({
            "date": date,
            "source": source,
            "flows": flows,
            "step_minutes": _n(step_minutes, 0),
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
            **({k: _n(v, 1 if k == "negative_price_hours" else 2)
                for k, v in wrow.items()} if wrow is not None else {}),
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
    ap.add_argument("--freq", default="15min",
                    help="analysis grid (default 15min, the market's own MTU since "
                         "Oct 2025; 60min averages four real prices into one)")
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
        # Ask SMARD for the resolution we are actually going to analyse at. Requesting
        # hourly and then resampling to 15min would forward-fill one number into four
        # slots -- the shape of quarter-hour data with none of the content.
        sub_hourly = pd.Timedelta(a.freq) < pd.Timedelta("1h")
        backend = dtb.SmardBackend(cache=cache,
                                   res="quarterhour" if sub_hourly else "hour")
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
    px_hourly = dtb._across_borders(d)["price_de"]
    mv = market_values(backend, start, end, px_hourly)
    wind, adj = wind_shortfall(backend, start, end, px_hourly)
    if cache:
        cache.report()

    merge_months(a.out, day_records(d, a.source, a.flows, demand, mv, wind), borders)
    system_costs(backend, a.out)
    write_adjacency(a.out, adj)
    seasons(a.out)
    example_day(backend, a.out, start, end)
    print(f"  -> {a.out}/")


if __name__ == "__main__":
    main()
