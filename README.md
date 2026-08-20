# German cross-border electricity balance

Did Germany pay more for imported electricity than it earned from exports? This
values every cross-border flow at day-ahead prices, per border and per market
time unit, and reports it over a day or a range.

```
de_trade_balance.py    the analysis and CLI report
build_data.py          reshapes a range into data/<month>.json + data/index.json
data/                  the generated series: one file per calendar month, plus
                       the whole-record files the range-independent panels use
index.html             the claim page: one claim, checked, EN/DE
dashboard.html         the full data view, one file, no dependencies
serve.py               serves it locally, bound to localhost
```

## Quick start

```bash
pip install pandas requests           # entsoe-py too, for --source entsoe
./de_trade_balance.py --date 2026-08-16          # one day, per MTU
./de_trade_balance.py --date 2026-08-16 --source smard   # gross per-direction flows
./de_trade_balance.py --date 2026-07             # all of July, per day
./de_trade_balance.py --date 2026 --by week      # the year, per ISO week
./de_trade_balance.py --demo                     # synthetic, no network
```

No account needed for either no-auth source. Raw responses are cached under
`~/.cache/de_trade_balance`, which matters most for energy-charts: it is rate
limited per IP and endpoint and has no bulk export, so a cold month of all 11
borders costs ~90s, then is free forever. SMARD is far cheaper — it takes many
series per request, so a month of every border plus prices and load is six
requests in a couple of seconds.

## The dashboard

```bash
python3 build_data.py --date 2026-07-18 --days 30   # fill data/ (SMARD)
./serve.py                                          # then open localhost:8731
```

`index.html` and `data/` both sit at the root, so the page's relative fetches work
the same locally as when published and there is no dev-only path. `serve.py` is a
thin wrapper on the stdlib server that binds to localhost — `http.server` otherwise
listens on every interface, which would hand `.git` to anyone on the network — and
refuses dotted paths for the same reason.

`build_data.py` upserts by date, so re-running a day corrects it in place. Both
sources revise the last few days after first publication, which is why the daily
job re-fetches a short trailing window rather than only yesterday.

Data is written one file per calendar month plus an `index.json` manifest. The page
reads the manifest first, then fetches only the months its range needs — a 30-day
view pulls two files (~140 KB) rather than the whole history, and switching to a
longer range fetches just the months it is missing.

Four panels deliberately ignore the range control, and each ships its own file, because
each is only meaningful over the whole record:

| File | Panel | Why it is not windowed |
|---|---|---|
| `seasons.json` | January and July are different systems | it *is* the comparison across months |
| `wind_adjacency.json` | Wind goes missing below zero | the effect is episodic; a short window shows nothing, or the opposite |
| `system_costs.json` | Congestion, both sides | the cost series lags ~3 months, so recent months have no partner |
| `example_day.json` | How one day's price happens | one worked day, chosen as the widest spread on record |
| `reference_prices.json` | Price levels | hand-refreshed retail snapshot |

`example_day.json` and `wind_adjacency.json` are written under a guard: a narrow run
cannot overwrite a wider one. Without it the daily job — whose range is three days —
would replace "the widest spread on record" with the widest of last Tuesday to Thursday.

**Resolution.** The default analysis grid is `--freq 15min`, matching the market: SDAC
has cleared in quarter-hour MTUs since October 2025, and SMARD serves every series used
here at `quarterhour`. Asking for hourly data is not free smoothing — the hourly series
*is* the mean of four real prices. Measured over two days in August, every single hour
had four different quarter-hour prices, averaging €26/MWh apart and reaching €79; one
hour ran from €7.9 to €87.2 and arrives hourly as a flat €44.5. Averaging first also
hides part-negative hours, which is exactly what the curtailment panel conditions on:
at hourly resolution it counted 26 negative hours in a fortnight where quarter-hour
resolution finds 28.75 hours' worth. Volumes are identical either way (a useful check
that the MWh-per-interval scaling is right); congestion rent moves about 1.2%.

### Publishing it

Settings → Pages → Source: **GitHub Actions**. The workflow assembles the publish
directory from `index.html` plus `data/` and uploads it — that is what frees the repo
layout, since branch-based Pages can only serve `/` or `/docs` and would drag
everything the page fetches into one of them. Publishing only those two also keeps
the scripts and git history out of the served site. There is still no build step and
no CDN: the page is one HTML file with inline SVG charts and no dependencies.

`.github/workflows/daily.yml` refreshes the data every morning, commits it, and
deploys.
**GitHub only fires `schedule` on the default branch**, so while this sits on a
side branch the cron will not run — trigger it by hand from the Actions tab
(`workflow_dispatch`, optionally with a date range to backfill), or merge this
branch to make the cron live.

## Which source to use

**SMARD** (`--source smard`, the dashboard default). Bundesnetzagentur publishes it
under a statutory mandate (§ 111d EnWG) and grants
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) in its own right, and it
carries each direction of each border separately. It is the only source that is both
publishable and complete — cross-checked against ENTSO-E for 2026-07-18, all 11
borders and both directions, agreeing to under 0.02 GWh. No account needed.

The other two are worth keeping for what they are: **`--source entsoe`** as an
independent cross-check (free token in `ENTSOE_API_TOKEN`, `pip install entsoe-py`),
and **`--source energy-charts`** as a no-fuss fallback with a finer generation-type
taxonomy.

| Source | licence to republish | flows | Denmark |
|---|---|---|---|
| smard | CC BY 4.0 granted by BNetzA under § 111d EnWG | gross per direction | DK_1 / DK_2 split |
| energy-charts | CC BY 4.0 (prices from SMARD) | netted, ~27% low | often unsplit |
| entsoe | prices + scheduled exchanges **not** on the free-re-use list | gross per direction | split |

That last row is the constraint that shapes everything. The dashboard *republishes*,
and ENTSO-E's
[list of data available for free re-use](https://transparencyplatform.zendesk.com/hc/en-us/articles/40921911218961-Legal-Terms-and-Conditions)
(Article 2.5 of its terms) does not cover the two series this project leans on:

| Series | Article | On the free-re-use list |
|---|---|---|
| Physical flows | 12.1.g | yes, item #18 |
| Day-ahead prices | 12.1.d | **no** |
| Scheduled commercial exchanges | 12.1.f | **no** |

The whole `12.1.d/e/f` market-results block is absent. That is not an oversight:
those series belong to the power exchanges rather than the TSOs, and ENTSO-E cannot
sub-license what it does not hold rights to. The transparency mandate makes the
data *visible*; it does not grant redistribution.

SMARD carries the same series and licenses them openly, which is why the published
page uses it. Worth being precise about why that works, because it is not that SMARD
is upstream of ENTSO-E — it isn't. SMARD states plainly that it takes its data
*from* ENTSO-E. But Bundesnetzagentur publishes under its own statutory mandate
(§ 111d EnWG) and grants CC BY 4.0 as licensor, so ENTSO-E is the delivery channel
rather than the licensor, and Article 2.5 — which binds Transparency Platform data
users — is not the instrument that governs data taken from SMARD. Attribution must
read exactly **"Bundesnetzagentur | SMARD.de"**.

(energy-charts' non-SMARD bidding zones are marked private/internal use only, so
don't widen `NEIGHBOURS` and then publish.) The ENTSO-E list checked was the
18 Oct 2023 revision, which does get amended.

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

Cross-border exchange, day-ahead prices and load come from
[Bundesnetzagentur | SMARD.de](https://www.smard.de) under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Under
`--source energy-charts` the prices are the same SMARD series redistributed
unchanged by [Energy-Charts.info](https://www.energy-charts.info) (Fraunhofer ISE),
which is then also the source of the exchange series; its other bidding zones are
marked private/internal use only, so don't widen the border list and then publish.
The dashboard's footer credits whichever source each day actually came from.

SMARD's download API is undocumented, so the parts worth knowing are recorded in
`SmardBackend`: the module manifest lives at
`/app/chart_configuration/market_data_configuration.json`, cross-border modules
exist only for region `DE-LU`, columns must be matched by header label rather than
request order, and imports arrive negative in German number format.
