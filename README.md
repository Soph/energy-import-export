# German cross-border electricity balance

Did Germany pay more for imported electricity than it earned from exports? This
values every cross-border flow at day-ahead prices, per border and per market
time unit, and reports it over a day or a range.

```
de_trade_balance.py    the analysis and CLI report
build_data.py          reshapes a range into docs/data/daily.json
docs/                  the dashboard GitHub Pages serves
```

## Quick start

```bash
pip install pandas requests           # entsoe-py too, for --source entsoe
./de_trade_balance.py --date 2026-08-16          # one day, per MTU
./de_trade_balance.py --date 2026-07             # all of July, per day
./de_trade_balance.py --date 2026 --by week      # the year, per ISO week
./de_trade_balance.py --demo                     # synthetic, no network
```

No account needed: it defaults to Fraunhofer ISE's energy-charts API. Every raw
response is cached under `~/.cache/de_trade_balance`, which matters because that
API is rate limited per IP and endpoint and has no bulk export — a cold month of
all 11 borders costs ~90s and about a dozen requests, then it is free forever.

## The dashboard

```bash
python3 build_data.py --date 2026-07-18 --days 30   # fill the data file
cd docs && python3 -m http.server 8731              # then open localhost:8731
```

`build_data.py` upserts by date, so re-running a day corrects it in place. Both
sources revise the last few days after first publication, which is why the daily
job re-fetches a short trailing window rather than only yesterday.

### Publishing it

Settings → Pages → Source: **Deploy from a branch**, branch `site-daily-dashboard`,
folder **`/docs`**. The data file is committed, so each data commit is a deploy;
there is no build step and no CDN — the page is one HTML file with inline SVG
charts and no dependencies.

`.github/workflows/daily.yml` refreshes the data every morning and commits it.
**GitHub only fires `schedule` on the default branch**, so while this sits on a
side branch the cron will not run — trigger it by hand from the Actions tab
(`workflow_dispatch`, optionally with a date range to backfill), or merge this
branch to make the cron live.

## Which source to use

The dashboard is built from **ENTSO-E** (`build_data.py` defaults to it), because
it publishes each direction of a border separately and carries every bidding zone.
It needs a free token in `ENTSOE_API_TOKEN` and `pip install entsoe-py`; for the
daily job the token goes in Settings → Secrets and variables → Actions.

`de_trade_balance.py` still defaults to energy-charts, so the CLI and `--demo`
keep working with no account at all. Both scripts take `--source`.

Measured over July 2026, the two agree on `balance_at_de_price` to **0.0002%** and
on `congestion_rent` to 0.6% — not luck, but because both reduce to net-only
expressions per MTU:

```
balance_at_de_price = sum(net_MWh * price_de)
congestion_rent     = sum(net_MWh * spread)
```

so a source that reports flows already netted cannot shift either. What it does
shift is everything gross: energy-charts nets opposite flows within a border-hour,
so `import_gwh`/`export_gwh` come out ~26% low, and the volume-weighted prices are
biased apart. It also does not carry DK_2 — though the Kontek energy is still
there, filed under DK_1, so only the zonal pricing suffers.

## Reading the numbers

There is no single true figure, so three are reported. Cross-border trade in the
coupled day-ahead market is *implicit*: nobody buys a labelled MWh from France.
German buyers pay the German price, the seller abroad receives theirs, and the
difference is congestion rent collected by the transmission operators on both
sides — regulated back into grid fees and interconnector investment, not a loss.

Day-ahead only: intraday and balancing trades settle at other prices and are not
included. The long-form version of all of this is the docstring at the top of
`de_trade_balance.py`.

## Attribution

Day-ahead prices for Germany and its neighbours are
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) from
[Bundesnetzagentur | SMARD.de](https://www.smard.de), redistributed unchanged by
[Energy-Charts.info](https://www.energy-charts.info) (Fraunhofer ISE), which is
also the source of the cross-border exchange series. Other bidding zones on that
endpoint are marked private/internal use only — don't widen the border list and
then publish the numbers.
