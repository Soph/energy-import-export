#!/usr/bin/env python3
"""
de_trade_balance.py -- Did Germany pay more for imported electricity than it
earned from exports, over a day or over a longer range?

Pulls, per border and per market time unit (MTU):
  * cross-border exchange DE-LU <-> neighbour, both directions
  * day-ahead price of DE-LU and of the neighbouring bidding zone

...then values the flows and prints a per-border summary plus a time series at
whatever resolution the range warrants (MTU, day, ISO week, month, year).

Data source: ENTSO-E Transparency Platform (https://transparency.entsoe.eu).
Requires a free API token: register, then Account Settings -> "Generate a new
token" (older accounts: email transparency@entsoe.eu asking for API access).

    export ENTSOE_API_TOKEN=...             # --source entsoe only
    pip install entsoe-py pandas requests
    ./de_trade_balance.py --date 2026-08-16                    # one day, per MTU
    ./de_trade_balance.py --date 2026-07                       # all of July, per day
    ./de_trade_balance.py --date 2026 --by week                # the year, ISO weeks
    ./de_trade_balance.py --date 2026-01-01 --end 2026-06-30 --by month
    ./de_trade_balance.py --date 2026-08-16 --flows physical --csv out/
    ./de_trade_balance.py --demo                               # synthetic data


A NOTE ON WHAT "COST" MEANS HERE
-------------------------------------------------------------------------------
There is no single true number, so this script reports three.

In the coupled European day-ahead market, cross-border trade is *implicit*:
nobody buys a labelled MWh "from France". Buyers in a zone pay their own zonal
price, sellers in a zone receive their own zonal price, and the algorithm moves
power across a border until either the prices converge or the interconnector
saturates. So for one MWh flowing X -> DE:

    German buyers pay          P_DE
    the seller in X receives   P_X
    the difference (P_DE-P_X)  is congestion rent, collected by the two TSOs
                               (normally split 50/50, and regulated back into
                               grid fees / interconnector investment)

Hence:

  balance_at_de_price   Everything valued at the German price. This is the
                        "what did this volume trade for at home" view and the
                        one journalism usually means. Under this metric, if DE
                        is a net importer in expensive hours and a net exporter
                        in cheap hours, the balance is negative.

  balance_at_zonal      Imports valued at the exporting zone's price, exports
                        valued at the German price -- i.e. money actually
                        leaving/entering the German market area, ignoring
                        congestion rent. Almost always the friendlier number.

  congestion_rent       The gap between the two. Not a loss; it is revenue to
                        the TSOs on both sides.

Also worth keeping in mind:
  * Day-ahead is the bulk of it, not all of it. Intraday and balancing trades
    are settled at other prices and are not included here.
  * "physical" flows (metered, includes loop and transit flows) differ from
    "scheduled" commercial exchanges. Only the latter has a price attached in
    any meaningful sense, so it is the default. Physical is available for
    comparison via --flows physical.
  * Luxembourg is inside the DE-LU bidding zone, so it is not a "border" here.
  * Austria was in the same zone as DE until 1 Oct 2018; dates before that
    need Area.DE_AT_LU and are not handled.


RANGES AND PERIOD AGGREGATION
-------------------------------------------------------------------------------
--date takes a day (2026-08-16), a month (2026-07) or a year (2026) and covers
exactly that span, unless --end (inclusive, same three forms) or --days says
otherwise. --by sets the resolution of the time-series table: mtu, day, week,
month, year, or auto -- the default, which picks one from the length of the
range. Per-border totals always cover the whole range regardless of --by.

Weeks are ISO weeks (%G-W%V), so "2026-W01" is Mon 29 Dec 2025 to Sun 4 Jan
2026 and the label carries the ISO year, not the calendar one.

Ranges are fetched one calendar month per request, and every raw response is
cached on disk (~/.cache/de_trade_balance, or --cache DIR / DE_TRADE_CACHE).
The energy-charts API is rate limited per IP and endpoint and has no bulk
export, so a re-run of a long range would otherwise spend the whole budget
refetching what it already had. Months that ended more than five days ago are
kept indefinitely; anything nearer to now expires after an hour, because
day-ahead prices arrive during the day and exchange volumes are still being
revised. --refresh overrides that, --no-cache skips it, and only the
energy-charts backend is cached at all.

Two things to watch when reading period rows:
  * Weeks and months at the edges of a range are usually partial. The "days"
    column says how many days actually landed in each row, so a small balance
    there may just be a short row.
  * Missing source data (a dead border series, a chunk the API refuses) is
    dropped, never zero-filled, so a period can be quietly short on volume.
    Warnings go to stderr -- read them before comparing rows to each other.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

import pandas as pd

TZ = "Europe/Berlin"
HOME = "DE_LU"

# Bidding zones sharing a border with DE-LU. NO_2 is the NordLink cable,
# SE_4 is Hansa PowerBridge/Baltic Cable, DK_1 and DK_2 are separate zones.
NEIGHBOURS = ["AT", "BE", "CH", "CZ", "DK_1", "DK_2", "FR", "NL", "NO_2", "PL", "SE_4"]


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

# Canonical zone ids follow entsoe-py's Area names. Each backend maps to its own
# spelling; keeping one internal vocabulary stops either dialect from leaking
# into the valuation code.
EC_BZN = {"DE_LU": "DE-LU", "DK_1": "DK1", "DK_2": "DK2", "NO_2": "NO2", "SE_4": "SE4"}

# /cbet and /cbpf label series by country name, not bidding zone. Both sides are
# normalised (lowercased, non-alphanumerics stripped) before lookup.
EC_SERIES = {
    "austria": "AT", "belgium": "BE", "switzerland": "CH",
    "czechrepublic": "CZ", "czechia": "CZ", "france": "FR",
    "netherlands": "NL", "poland": "PL",
    "denmark": "DK_1", "denmark1": "DK_1", "dk1": "DK_1",
    "denmark2": "DK_2", "dk2": "DK_2",
    "norway": "NO_2", "norway2": "NO_2", "no2": "NO_2",
    "sweden": "SE_4", "sweden4": "SE_4", "se4": "SE_4",
}
# Aggregates and same-zone entries that must never be treated as a border.
EC_IGNORE = {"nettotal", "net", "total", "sum", "crossborderelectricitytrading",
             "crossborderphysicalflows", "luxembourg", "netexport", "netimport"}


def _norm(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def default_cache_dir() -> str:
    base = (os.environ.get("XDG_CACHE_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(base, "de_trade_balance")


class Cache:
    """Raw payloads on disk, so a re-run of a long range costs no requests.

    Keyed on the request itself rather than on anything derived from it, so
    changing --freq, --by or --borders still hits: those all happen downstream
    of the fetch.

    Expiry follows how the data settles rather than a fixed TTL. A window that
    ended more than SETTLED_AFTER days ago is kept forever -- day-ahead prices
    are final once published, and flows stop being revised. Anything touching
    the last few days gets a short TTL instead, because day-ahead prices for
    today and tomorrow appear during the day and exchange volumes are still
    being corrected.

    404s are cached too (as a tombstone), which is what stops a border the
    source simply does not carry -- DK_2 here -- from costing a request on
    every single run.
    """

    SETTLED_AFTER = 5      # days before a window counts as final
    FRESH_TTL = 3600       # seconds to trust an unsettled window

    def __init__(self, root: str, refresh: bool = False):
        self.root = root
        self.refresh = refresh
        self.hits = self.fetched = 0

    def _path(self, key: str) -> str:
        return os.path.join(self.root,
                            hashlib.sha1(key.encode()).hexdigest()[:20] + ".json")

    @staticmethod
    def key(path: str, params: dict) -> str:
        return path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

    @classmethod
    def settled(cls, params: dict) -> bool:
        """Is every timestamp in this window old enough to be final?"""
        end = params.get("end")
        if not end:
            return False
        try:
            e = pd.Timestamp(end)
        except ValueError:
            return False
        e = e.tz_localize(TZ) if e.tz is None else e.tz_convert(TZ)
        return e <= pd.Timestamp.now(tz=TZ) - pd.Timedelta(days=cls.SETTLED_AFTER)

    def get(self, key: str):
        """The payload, the MISSING sentinel, or None for a miss."""
        if self.refresh:
            return None
        try:
            with open(self._path(key)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            return None
        if not rec.get("settled") and time.time() - rec.get("at", 0) > self.FRESH_TTL:
            return None
        self.hits += 1
        return rec["payload"]

    def put(self, key: str, payload, params: dict) -> None:
        self.fetched += 1
        rec = {"key": key, "at": time.time(), "settled": self.settled(params),
               "payload": payload}
        try:
            os.makedirs(self.root, exist_ok=True)
            tmp = self._path(key) + f".{os.getpid()}.tmp"
            with open(tmp, "w") as fh:
                json.dump(rec, fh)
            os.replace(tmp, self._path(key))   # never leave a half-written file
        except OSError as exc:
            print(f"  ! cache write failed ({exc}); continuing uncached",
                  file=sys.stderr)

    def report(self) -> None:
        if self.hits or self.fetched:
            print(f"[cache: {self.hits} hit(s), {self.fetched} fetched, "
                  f"{self.root}]", file=sys.stderr)


MISSING = {"__missing__": True}   # tombstone for a 404


class Backend:
    """Two methods, so the valuation code never learns which source it got.

    prices()    -> Series of EUR/MWh indexed by MTU start
    exchanges() -> {zone: DataFrame[import_mw, export_mw]} in MW, DE-LU's view
    """

    def prices(self, zone, start, end) -> pd.Series:
        raise NotImplementedError

    def exchanges(self, start, end, physical, neighbours) -> dict:
        raise NotImplementedError


class EntsoeBackend(Backend):
    """One request per zone per direction, so directions stay separate."""

    def __init__(self, token: str):
        from entsoe import EntsoePandasClient
        self.c = EntsoePandasClient(api_key=token, retry_count=3, timeout=60)

    def prices(self, zone, start, end):
        return self.c.query_day_ahead_prices(zone, start=start, end=end)

    def exchanges(self, start, end, physical, neighbours):
        q = (self.c.query_crossborder_flows if physical
             else self.c.query_scheduled_exchanges)
        out = {}
        for z in neighbours:
            try:
                imp = q(z, HOME, start=start, end=end)
                exp = q(HOME, z, start=start, end=end)
            except Exception as exc:
                print(f"  ! {z}: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
                continue
            out[z] = pd.DataFrame({"import_mw": imp, "export_mw": exp})
        return out


class EnergyChartsBackend(Backend):
    """Fraunhofer ISE's open API (api.energy-charts.info). No auth, no signup.

    Three differences from ENTSO-E worth knowing before trusting the output:

    1. Flows arrive already netted: one signed series per border (positive =
       into Germany), in GW, one request for all borders at once. ENTSO-E
       publishes each direction separately, and a border can genuinely carry
       flow both ways inside one MTU. Here that cancels, so gross import and
       export volumes come out somewhat lower than via ENTSO-E. The *net*
       balance per border is unaffected, but balance_at_de_price is not
       strictly net-only (it prices gross volumes at the same price, so it
       survives; balance_at_zonal does shift slightly).
    2. Series are labelled by country, and Denmark is not guaranteed to arrive
       split into DK1/DK2. If it comes through as plain "Denmark" the whole
       flow gets priced at DK1 -- right for the Jutland interconnectors, wrong
       for the Kontek cable to DK2. The script warns when this happens.
    3. Prices for DE-LU and every zone bordering it are redistributed from
       SMARD under CC BY 4.0, so this use is fine. Other bidding zones on the
       same endpoint are marked private/internal use only -- don't widen the
       border list and then publish the numbers.
    """

    BASE = "https://api.energy-charts.info"
    GAP = 0.5        # starting seconds between requests
    MAX_GAP = 8.0    # ceiling once the limiter has pushed back
    TRIES = 5

    def __init__(self, timeout: int = 60, cache: Cache | None = None):
        import requests
        self.s = requests.Session()
        self.timeout = timeout
        self.cache = cache
        self._pcache: dict = {}
        self._last = 0.0
        self.gap = self.GAP

    def _get(self, path: str, **params) -> dict:
        """GET, from the disk cache if it is there, with backoff if it is not.

        A long range means a dozen price requests per window, and the API
        starts answering 429 well before that finishes. Failing here drops a
        whole border from the totals without making them look wrong, so it is
        worth both caching and waiting.
        """
        key = Cache.key(path, params)
        if self.cache:
            hit = self.cache.get(key)
            if hit == MISSING:
                raise LookupError(f"no data for {path} {params} (cached)")
            if hit is not None:
                return hit

        for attempt in range(self.TRIES):
            time.sleep(max(0.0, self._last + self.gap - time.monotonic()))
            r = self.s.get(f"{self.BASE}{path}", params=params, timeout=self.timeout)
            self._last = time.monotonic()
            if r.status_code == 404:
                if self.cache:
                    self.cache.put(key, MISSING, params)
                raise LookupError(f"no data for {path} {params}")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.TRIES - 1:
                    r.raise_for_status()
                # Retry-After comes back at 25-30s, so the limiter is counting
                # requests per window rather than policing a rate. Widening the
                # gap for the rest of the run is what actually stops the next
                # one: waiting out a single 429 costs more than spacing every
                # remaining request would have.
                self.gap = min(self.gap * 2, self.MAX_GAP)
                wait = float(r.headers.get("Retry-After") or 2 ** attempt)
                print(f"  . {r.status_code} on {path}, waiting {wait:.0f}s "
                      f"(spacing now {self.gap:.1f}s)", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            payload = r.json()
            if self.cache:
                self.cache.put(key, payload, params)
            return payload
        raise LookupError(f"gave up on {path} {params}")  # unreachable

    @staticmethod
    def _index(payload: dict) -> pd.DatetimeIndex:
        if not payload.get("unix_seconds"):
            raise LookupError("empty payload")
        return pd.to_datetime(payload["unix_seconds"], unit="s", utc=True).tz_convert(TZ)

    def prices(self, zone, start, end):
        zone = str(zone)
        key = (zone, start.isoformat(), end.isoformat())
        if key not in self._pcache:
            bzn = EC_BZN.get(zone, zone)
            d = self._get("/price", bzn=bzn,
                          start=start.isoformat(), end=end.isoformat())
            if d.get("deprecated"):
                print(f"  ! {bzn}: endpoint reports deprecated data", file=sys.stderr)
            self._pcache[key] = pd.Series(d["price"], index=self._index(d),
                                          dtype="float64")
        return self._pcache[key]

    def exchanges(self, start, end, physical, neighbours):
        d = self._get("/cbpf" if physical else "/cbet", country="de",
                      start=start.isoformat(), end=end.isoformat())
        idx = self._index(d)

        out, unknown = {}, []
        for series in d["countries"]:
            n = _norm(series["name"])
            if n in EC_IGNORE:
                continue
            zone = EC_SERIES.get(n)
            if zone is None:
                unknown.append(series["name"])
                continue
            if zone not in neighbours:
                continue
            if zone == "DK_1" and n == "denmark":
                print("  ! Denmark arrives unsplit; pricing all of it at DK1 "
                      "(Kontek/DK2 volume is misattributed).", file=sys.stderr)
            mw = pd.Series(series["data"], index=idx, dtype="float64") * 1000.0
            if mw.isna().all():
                # a reporting gap, not a genuinely idle interconnector -- keeping
                # it would silently pad the totals with zeros
                print(f"  ! {zone}: series is entirely null, dropped", file=sys.stderr)
                continue
            out[zone] = pd.DataFrame({"import_mw": mw.clip(lower=0),
                                      "export_mw": (-mw).clip(lower=0)})
        if unknown:
            print(f"  ! unmapped series ignored: {unknown} (add to EC_SERIES)",
                  file=sys.stderr)
        return out


class DemoBackend(Backend):
    """Synthetic but arbitrage-consistent data, so the pipeline runs offline.

    Stateless, because a long range is fetched chunk by chunk and each chunk
    asks for its own window.
    """

    def prices(self, zone, start, end):
        import math
        base = {"DE_LU": 78, "FR": 62, "NL": 80, "CH": 92, "AT": 84, "CZ": 71,
                "PL": 96, "BE": 79, "DK_1": 55, "DK_2": 58, "NO_2": 41, "SE_4": 49}
        b = base.get(str(zone), 70)
        idx = pd.date_range(start, end, freq="h", inclusive="left", tz=TZ)
        # a daily shape plus a slow seasonal drift, otherwise a year of demo
        # data is 365 identical days and every period row comes out the same
        return pd.Series(
            [b + 34 * math.sin((h.hour - 6) / 24 * 2 * math.pi + 3.6)
               + 22 * math.sin((h.dayofyear - 20) / 365 * 2 * math.pi)
             for h in idx],
            index=idx)

    def exchanges(self, start, end, physical, neighbours):
        p_home = self.prices(HOME, start, end)
        out = {}
        for z in neighbours:
            spread = p_home - self.prices(z, start, end)
            # ord-sum rather than hash(), which is salted per process and would
            # make two runs of the same range disagree
            flow = (spread / 40).clip(-1, 1) * (400 + 60 * (sum(map(ord, z)) % 9))
            out[z] = pd.DataFrame({"import_mw": flow.clip(lower=0),
                                   "export_mw": (-flow).clip(lower=0)})
        return out


def align(s: pd.Series, freq: str) -> pd.Series:
    """Put a series on a common time grid.

    Both backends hand back mixed resolutions -- day-ahead moved to 15-minute
    MTUs in late 2025 while plenty of flow series are still hourly, and some
    borders report in 30-minute steps. Downsampling averages (prices and MW
    flows are both interval averages, so the mean is the correct aggregate);
    upsampling forward-fills.
    """
    if s is None or s.empty:
        return pd.Series(dtype="float64")
    s = s.sort_index()
    s.index = (s.index.tz_localize(TZ) if s.index.tz is None
               else s.index.tz_convert(TZ))
    return s.resample(freq).mean().ffill()


def collect(backend: Backend, start, end, freq: str, physical: bool,
            neighbours: list) -> pd.DataFrame:
    """One row per MTU per border, flows in MW, prices in EUR/MWh.

    Raises LookupError when the window comes back unusable; over a range that
    is one chunk to warn about, not a reason to abandon the run.
    """
    p_home = align(backend.prices(HOME, start, end), freq)
    if p_home.empty:
        raise LookupError(f"no day-ahead price for {HOME}")
    p_home = p_home[(p_home.index >= start) & (p_home.index < end)]

    rows = []
    for zone, flows in backend.exchanges(start, end, physical, neighbours).items():
        try:
            p_far = align(backend.prices(zone, start, end), freq)
        except Exception as exc:
            print(f"  ! {zone}: no price ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        df = pd.DataFrame({
            "import_mw": align(flows["import_mw"], freq),
            "export_mw": align(flows["export_mw"], freq),
            "price_de": p_home,
            "price_far": p_far,
        }).reindex(p_home.index)
        df[["import_mw", "export_mw"]] = df[["import_mw", "export_mw"]].fillna(0.0)
        df["border"] = zone
        rows.append(df.dropna(subset=["price_de", "price_far"]))

    if not rows:
        raise LookupError("no border data")
    out = pd.concat(rows)
    out.index.name = "mtu"
    return out.reset_index().set_index(["border", "mtu"]).sort_index()


def windows(start, end, unit: str):
    """The windows a range is actually fetched in.

    Whole calendar months, even when the range starts or ends mid-month, and
    the surplus is trimmed off afterwards. Splitting at all is necessary
    because a year of 15-minute data in one request earns a timeout from
    energy-charts and a 400 from ENTSO-E (which caps most endpoints at a year).
    Splitting on *fixed* boundaries is what makes the disk cache worth having:
    windows counted off from an arbitrary start date would give every range its
    own keys, so asking a slightly different question about an overlapping
    period would refetch all of it.

    unit="all" does the whole range in one request instead -- still cached, but
    only reusable by an identical range.
    """
    if unit == "all":
        yield start, end
        return
    m = start.replace(day=1)
    while m < end:
        nxt = m + pd.DateOffset(months=1)
        yield m, nxt
        m = nxt


def collect_range(backend: Backend, start, end, freq: str, physical: bool,
                  neighbours: list, unit: str) -> pd.DataFrame:
    """collect() over a whole range, window by window. Bad windows are dropped."""
    wins = list(windows(start, end, unit))
    frames = []
    for i, (ws, we) in enumerate(wins, 1):
        if len(wins) > 1:
            print(f"[{i}/{len(wins)}] {ws.date()} .. "
                  f"{(we - pd.Timedelta(days=1)).date()}", file=sys.stderr)
        try:
            frames.append(collect(backend, ws, we, freq, physical, neighbours))
        except Exception as exc:
            print(f"  ! {ws.date()}..{(we - pd.Timedelta(days=1)).date()}: dropped "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)

    if not frames:
        sys.exit("No usable data in the whole range -- check the dates and the "
                 "warnings above.")
    out = pd.concat(frames).sort_index()
    mtu = out.index.get_level_values("mtu")      # trim the whole-month surplus
    return out[(mtu >= start) & (mtu < end)]



# --------------------------------------------------------------------------- #
# valuation
# --------------------------------------------------------------------------- #

def value(df: pd.DataFrame, hours: float) -> pd.DataFrame:
    d = df.copy()
    d["import_mwh"] = d["import_mw"] * hours
    d["export_mwh"] = d["export_mw"] * hours
    d["net_import_mwh"] = d["import_mwh"] - d["export_mwh"]

    # everything at the German price
    d["import_cost_de"] = d["import_mwh"] * d["price_de"]
    d["export_rev_de"] = d["export_mwh"] * d["price_de"]

    # imports at the exporting zone's price: money actually leaving the market area
    d["import_cost_zonal"] = d["import_mwh"] * d["price_far"]

    # rent on this border, both directions
    d["congestion_rent"] = (
        d["import_mwh"] * (d["price_de"] - d["price_far"])
        + d["export_mwh"] * (d["price_far"] - d["price_de"])
    )
    d["spread"] = d["price_de"] - d["price_far"]
    return d


def per_border(d: pd.DataFrame) -> pd.DataFrame:
    g = d.groupby("border")
    t = pd.DataFrame({
        "import_GWh": g["import_mwh"].sum() / 1000,
        "export_GWh": g["export_mwh"].sum() / 1000,
        "net_imp_GWh": g["net_import_mwh"].sum() / 1000,
        "imp_cost_de_kEUR": g["import_cost_de"].sum() / 1000,
        "exp_rev_de_kEUR": g["export_rev_de"].sum() / 1000,
        "imp_cost_zonal_kEUR": g["import_cost_zonal"].sum() / 1000,
        "rent_kEUR": g["congestion_rent"].sum() / 1000,
        "mean_spread": g["spread"].mean(),
        "pct_coupled": g["spread"].apply(lambda s: (s.abs() < 0.01).mean() * 100),
    })
    t["bal_de_kEUR"] = t["exp_rev_de_kEUR"] - t["imp_cost_de_kEUR"]
    t["bal_zonal_kEUR"] = t["exp_rev_de_kEUR"] - t["imp_cost_zonal_kEUR"]
    return t.round(2)


BY = ("mtu", "day", "week", "month", "year")

# %G-W%V, not %Y-W%U: ISO weeks belong to the ISO year, so 2026-W01 starts on
# 29 Dec 2025 and labelling it "2025-W01" would put it in the wrong bucket.
FMT = {"day": "%Y-%m-%d", "week": "%G-W%V", "month": "%Y-%m", "year": "%Y"}


def period_of(idx: pd.DatetimeIndex, by: str):
    """The bucket label each MTU falls into. Sorts chronologically as a string."""
    return idx if by == "mtu" else idx.strftime(FMT[by])


def _across_borders(d: pd.DataFrame) -> pd.DataFrame:
    """Sum the borders away, one row per MTU, unrounded and unscaled.

    price_de is repeated once per border in d, so it is taken rather than
    summed -- everything else is additive.
    """
    g = d.groupby(level="mtu")
    cols = ["import_mwh", "export_mwh", "net_import_mwh", "import_cost_de",
            "export_rev_de", "import_cost_zonal", "congestion_rent"]
    out = g[cols].sum()
    out.insert(0, "price_de", g["price_de"].first())
    return out


def per_mtu(d: pd.DataFrame) -> pd.DataFrame:
    m = _across_borders(d)
    t = pd.DataFrame({
        "price_de": m["price_de"],
        "import_MWh": m["import_mwh"],
        "export_MWh": m["export_mwh"],
        "net_imp_MWh": m["net_import_mwh"],
        "bal_de_EUR": m["export_rev_de"] - m["import_cost_de"],
        "bal_zonal_EUR": m["export_rev_de"] - m["import_cost_zonal"],
        "rent_EUR": m["congestion_rent"],
    })
    return t.round(2)


def per_period(d: pd.DataFrame, by: str) -> pd.DataFrame:
    """One row per day / ISO week / month / year, all borders aggregated.

    Import and export prices are volume-weighted, which is the pair that
    answers the question: expensive imports against cheap exports is exactly
    how a negative balance happens.
    """
    if by == "mtu":
        return per_mtu(d)

    m = _across_borders(d)
    key = period_of(m.index, by)
    s = m.groupby(key).sum()

    t = pd.DataFrame(index=s.index)
    if by != "day":
        # partial weeks/months at the edges of a range are the rule, not the
        # exception, and a short row looks like a quiet one
        t["days"] = (pd.Series(m.index.normalize(), index=m.index)
                     .groupby(key).nunique())
    t["import_GWh"] = s["import_mwh"] / 1e3
    t["export_GWh"] = s["export_mwh"] / 1e3
    t["net_imp_GWh"] = s["net_import_mwh"] / 1e3
    t["imp_px_de"] = (s["import_cost_de"] / s["import_mwh"]).where(s["import_mwh"] != 0)
    t["exp_px_de"] = (s["export_rev_de"] / s["export_mwh"]).where(s["export_mwh"] != 0)
    t["bal_de_kEUR"] = (s["export_rev_de"] - s["import_cost_de"]) / 1e3
    t["bal_zonal_kEUR"] = (s["export_rev_de"] - s["import_cost_zonal"]) / 1e3
    t["rent_kEUR"] = s["congestion_rent"] / 1e3
    t.index.name = by
    return t.round(2)


def per_border_period(d: pd.DataFrame, by: str) -> pd.DataFrame:
    """Border x period. Too wide to print; written to CSV when --csv is given."""
    k = d.reset_index()
    k[by] = period_of(pd.DatetimeIndex(k["mtu"]), by)
    g = k.groupby(["border", by])
    t = pd.DataFrame({
        "import_GWh": g["import_mwh"].sum() / 1000,
        "export_GWh": g["export_mwh"].sum() / 1000,
        "net_imp_GWh": g["net_import_mwh"].sum() / 1000,
        "imp_cost_de_kEUR": g["import_cost_de"].sum() / 1000,
        "exp_rev_de_kEUR": g["export_rev_de"].sum() / 1000,
        "imp_cost_zonal_kEUR": g["import_cost_zonal"].sum() / 1000,
        "rent_kEUR": g["congestion_rent"].sum() / 1000,
        "mean_spread": g["spread"].mean(),
    })
    t["bal_de_kEUR"] = t["exp_rev_de_kEUR"] - t["imp_cost_de_kEUR"]
    t["bal_zonal_kEUR"] = t["exp_rev_de_kEUR"] - t["imp_cost_zonal_kEUR"]
    return t.round(2)


def show(t: pd.DataFrame, limit: int = 80) -> None:
    """Print a table, eliding the middle if it is longer than a screenful.

    --by mtu over a year is 8760 rows; nobody reads those, but the ends are
    still worth seeing.
    """
    if len(t) <= limit:
        print(t.to_string())
        return
    half = limit // 2
    print(t.head(half).to_string())
    print(f"  [... {len(t) - 2 * half:,} rows omitted -- use --csv, "
          f"or a coarser --by ...]")
    print(t.tail(half).to_string(header=False))


def report(d: pd.DataFrame, label: str, physical: bool, source: str,
           by: str, requested: list) -> None:
    b, t = per_border(d), per_period(d, by)
    imp_gwh, exp_gwh = b["import_GWh"].sum(), b["export_GWh"].sum()
    cost_de, rev_de = b["imp_cost_de_kEUR"].sum(), b["exp_rev_de_kEUR"].sum()
    cost_zonal = b["imp_cost_zonal_kEUR"].sum()
    rent = b["rent_kEUR"].sum()
    n_days = d.index.get_level_values("mtu").normalize().nunique()

    kind = "physical flows" if physical else "scheduled commercial exchanges"
    print(f"\n{'=' * 78}\nGermany (DE-LU) cross-border balance -- {label}\n"
          f"{kind} via {source}\n{'=' * 78}")

    print("\nPer border (whole range):")
    show(b)

    unit = "MTU" if by == "mtu" else by
    print(f"\nPer {unit} (all borders aggregated):")
    show(t)

    print(f"\n{'-' * 78}\nTotals -- {label}\n{'-' * 78}")
    print(f"  days covered                {n_days:>12,d}")
    print(f"  borders included            {len(b):>12,d} of {len(requested)}")
    print(f"  imported                    {imp_gwh:>12,.1f} GWh")
    print(f"  exported                    {exp_gwh:>12,.1f} GWh")
    print(f"  net import                  {imp_gwh - exp_gwh:>12,.1f} GWh")
    print(f"  volume-weighted import px   {cost_de / imp_gwh if imp_gwh else 0:>12,.2f} EUR/MWh (at DE price)")
    print(f"  volume-weighted export px   {rev_de / exp_gwh if exp_gwh else 0:>12,.2f} EUR/MWh (at DE price)")
    print()
    print(f"  import cost @ DE price      {cost_de:>12,.1f} kEUR")
    print(f"  export revenue @ DE price   {rev_de:>12,.1f} kEUR")
    print(f"  -> balance_at_de_price      {rev_de - cost_de:>12,.1f} kEUR")
    if n_days > 1:
        print(f"     per day (avg)            {(rev_de - cost_de) / n_days:>12,.1f} kEUR")
    print()
    print(f"  import cost @ zonal price   {cost_zonal:>12,.1f} kEUR")
    print(f"  -> balance_at_zonal         {rev_de - cost_zonal:>12,.1f} kEUR")
    print(f"  congestion rent to TSOs     {rent:>12,.1f} kEUR")

    if by != "mtu" and len(t) > 1:
        neg = t["bal_de_kEUR"] < 0
        worst, best = t["bal_de_kEUR"].idxmin(), t["bal_de_kEUR"].idxmax()
        print()
        print(f"  {by + 's in deficit':<28}{neg.sum():>12,d} of {len(t)} (at DE price)")
        print(f"  {'worst ' + by:<28}{t.at[worst, 'bal_de_kEUR']:>12,.1f} kEUR  ({worst})")
        print(f"  {'best ' + by:<28}{t.at[best, 'bal_de_kEUR']:>12,.1f} kEUR  ({best})")

    bal = rev_de - cost_de
    verb = "cost more than exports earned" if bal < 0 else "earned more than imports cost"
    print(f"\n  Verdict (at DE price): imports {verb}, "
          f"net {abs(bal):,.1f} kEUR {'out' if bal < 0 else 'in'}.")
    missing = [z for z in requested if z not in b.index]
    if missing:
        print(f"  Incomplete: no data for {', '.join(missing)} -- every total above "
              "is missing those borders.")
    if (rev_de - cost_zonal) * bal < 0:
        print("  Note: the two metrics disagree in sign -- congestion rent flips it. "
              "Read the section at the top of this file before quoting a number.")


# --------------------------------------------------------------------------- #

def span_of(spec: str) -> pd.DateOffset:
    """How much calendar a partial --date/--end covers: a day, month or year."""
    return {2: pd.DateOffset(days=1),
            1: pd.DateOffset(months=1)}.get(spec.count("-"), pd.DateOffset(years=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=(pd.Timestamp.now(tz=TZ) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    help="start of the range: YYYY-MM-DD, YYYY-MM or YYYY. Without "
                         "--end/--days it covers exactly that day, month or year "
                         "(default: yesterday)")
    ap.add_argument("--end", help="last period of the range, inclusive; same three "
                                  "forms as --date")
    ap.add_argument("--days", type=int,
                    help="length in days from --date instead of --end")
    ap.add_argument("--by", choices=("auto",) + BY, default="auto",
                    help="resolution of the time-series table (default: auto, "
                         "scaled to the length of the range)")
    ap.add_argument("--flows", choices=["scheduled", "physical"], default="scheduled",
                    help="scheduled commercial exchanges (default) or metered physical flows")
    ap.add_argument("--freq", default="60min", help="common time grid (60min, 15min, ...)")
    ap.add_argument("--chunk", choices=["month", "all"], default="month",
                    help="fetch a calendar month per request (default, and what "
                         "makes the cache reusable) or the whole range at once")
    ap.add_argument("--cache", metavar="DIR", default=os.environ.get("DE_TRADE_CACHE"),
                    help=f"raw-response cache (default: {default_cache_dir()})")
    ap.add_argument("--no-cache", action="store_true", help="never read or write the cache")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore what is cached, refetch and overwrite it")
    ap.add_argument("--borders", help="comma-separated subset, e.g. FR,NL,PL")
    ap.add_argument("--csv", metavar="DIR", help="also write raw + summary CSVs here")
    ap.add_argument("--source", choices=["energy-charts", "entsoe", "demo"],
                    default=os.environ.get("DE_TRADE_SOURCE", "energy-charts"),
                    help="data backend (default: energy-charts, no auth needed)")
    ap.add_argument("--token", default=os.environ.get("ENTSOE_API_TOKEN"),
                    help="ENTSO-E security token (--source entsoe only)")
    ap.add_argument("--demo", action="store_true", help="alias for --source demo")
    a = ap.parse_args()

    try:
        start = pd.Timestamp(a.date, tz=TZ).normalize()
        # all offsets are calendar-based, so DST days stay 23 or 25 hours long
        end = (pd.Timestamp(a.end, tz=TZ).normalize() + span_of(a.end) if a.end
               else start + pd.DateOffset(days=a.days) if a.days
               else start + span_of(a.date))
    except ValueError as exc:
        sys.exit(f"Bad date: {exc}")
    if end <= start:
        sys.exit(f"Empty range: it ends on {(end - pd.Timedelta(days=1)).date()}, "
                 f"before --date {start.date()}.")

    n_days = (end - start).days
    by = a.by if a.by != "auto" else (
        "mtu" if n_days <= 2 else "day" if n_days <= 70 else
        "week" if n_days <= 400 else "month")
    last = (end - pd.Timedelta(days=1)).date()
    label = (str(start.date()) if n_days == 1
             else f"{start.date()} .. {last} ({n_days} days)")
    slug = str(start.date()) if n_days == 1 else f"{start.date()}_{last}"

    hours = pd.Timedelta(a.freq) / pd.Timedelta("1h")
    borders = [b.strip() for b in a.borders.split(",")] if a.borders else NEIGHBOURS

    if a.demo:
        a.source = "demo"
    if start < pd.Timestamp("2018-10-01", tz=TZ) and a.source != "demo":
        sys.exit("Dates before 2018-10-01 predate the DE/AT bidding-zone split; "
                 "those need the DE-AT-LU zone and are not handled.")
    horizon = pd.Timestamp.now(tz=TZ).normalize() + pd.DateOffset(days=2)
    if end > horizon and a.source != "demo":
        print(f"  ! range runs past published day-ahead data ({horizon.date()}); "
              "the tail will come back empty", file=sys.stderr)

    # only the energy-charts JSON layer is cached: entsoe-py hands back parsed
    # pandas objects rather than payloads, and demo data costs nothing to make
    cache = None if (a.no_cache or a.source != "energy-charts") else Cache(
        a.cache or default_cache_dir(), refresh=a.refresh)

    if a.source == "demo":
        backend = DemoBackend()
        print("[demo: synthetic data, the numbers mean nothing]", file=sys.stderr)
    elif a.source == "entsoe":
        if not a.token:
            sys.exit("Set ENTSOE_API_TOKEN or pass --token "
                     "(or drop --source entsoe to use energy-charts, which needs neither).")
        backend = EntsoeBackend(a.token)
    else:
        backend = EnergyChartsBackend(cache=cache)

    physical = a.flows == "physical"
    raw = collect_range(backend, start, end, a.freq, physical, borders, a.chunk)
    if cache:
        cache.report()
    d = value(raw, hours)
    report(d, label, physical, a.source, by, borders)

    if a.csv:
        os.makedirs(a.csv, exist_ok=True)
        d.to_csv(f"{a.csv}/raw_{slug}_{a.source}.csv")
        per_border(d).to_csv(f"{a.csv}/by_border_{slug}_{a.source}.csv")
        per_period(d, by).to_csv(f"{a.csv}/by_{by}_{slug}_{a.source}.csv")
        if by != "mtu":  # border x mtu is what raw_ already is
            per_border_period(d, by).to_csv(
                f"{a.csv}/by_border_{by}_{slug}_{a.source}.csv")
        print(f"\nCSVs written to {a.csv}/")


if __name__ == "__main__":
    main()
