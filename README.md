# dax-ko-sim

An offline intraday backtest simulator for DAX knock-out (KO) products on
5-minute bars. One Python file, standard library only, no network, no broker,
no accounts.

> **Not financial advice. Not a trading system.** This is an educational
> simulation with deliberately simplified, artificial KO products. Results —
> including any simulated profits — prove nothing about real returns.

## What it does

- Reads 5-minute OHLC bars for the DAX index (or generates synthetic demo data).
- Generates signals from a simple rule-based SMA-5 / SMA-20 trend strategy
  (no machine learning). Only completed previous bars feed the signal; entries
  happen at the next bar's open.
- Every trade passes a separate `RiskGate` that checks funding, fees, trading
  hours, per-trade risk and a daily loss budget.
- Simulates artificial long/short KO products with a fixed barrier
  (ratio 0.01, leverage ≈ 5), including spread and slippage.
- Books all money in whole euro cents (`Decimal`, `ROUND_HALF_UP` per fill).
- Writes a SQLite event journal, `Trades.csv`, `Tagesberichte.csv` (daily
  reports), `Bericht.json` and `Bericht.txt` for every run.

## Quick start

Requires Python 3.9+ with time zone data. No extra packages.

```sh
python3 dax_ko_sim.py --self-test        # built-in test suite
python3 dax_ko_sim.py                    # synthetic demo run, 100 EUR
python3 dax_ko_sim.py --csv examples/synthetic_dax_5min.csv
python3 dax_ko_sim.py --fee 0.50 --spread 0.04 --capital 500
```

Each run starts fresh and writes a timestamped folder under
`dax_ko_sim_results/`.

**Expected result with default costs: zero trades.** The default fee of
1 EUR per fill already uses up the 2 EUR per-trade risk budget, so the
`RiskGate` correctly rejects every entry. Lower `--fee` to see trades.

## Input data format

UTF-8 CSV with exactly these columns:

```csv
timestamp,open,high,low,close
2026-03-27T09:00:00+01:00,20000.0,20001.0,19999.0,20000.25
```

- ISO 8601 timestamps **with UTC offset**; timestamp = start of the 5-minute bar.
- Only complete sessions 09:00–17:30 Europe/Berlin, Monday–Friday
  (102 bars per day, last bar starts 17:25).
- The whole file is validated before any simulation starts: columns, offsets,
  DST transitions, order, duplicates, missing bars and OHLC consistency.
  Invalid files are rejected — nothing is silently sorted, filled or guessed.
- Exchange holidays are **not** detected; clean them beforehand.

`examples/synthetic_dax_5min.csv` contains two complete **synthetic** days
across the March DST change. These are not real DAX prices.

## Risk model (defaults)

| Rule | Default |
| --- | --- |
| Starting capital | 100 EUR |
| Fee per fill | 1.00 EUR |
| Spread / slippage per unit | 0.04 / 0.01 EUR |
| Max risk per trade (incl. both fees) | 2 EUR |
| Daily loss budget | 5 EUR |
| Max entries per day | 3 |
| Time limit per position | 60 minutes, flat by 17:30 |

Same-bar ambiguity is resolved conservatively: knock-out first, then stop,
then target. Gaps can exceed the planned risk; the daily limit is not a
guaranteed loss cap.

**Kill switch:** a file named `STOP` in the working directory (or next to the
script) blocks new entries and closes an open position at the next simulated
open. Ctrl+C marks the run as incomplete without pretending an open position
was closed.

## Known limitations

The KO product model is intentionally simple. It ignores financing costs,
issuer risk, variable spreads, the order book, partial fills and latency.
Historical index prices do **not** turn the artificial KO prices into
realistic derivative prices — this warning is written into every report.

## Historical diagnosis mode (optional)

`historische_diagnose.py` runs a shortened 09:05–17:30 diagnosis on a local
FirstRate Data sample. That data is licensed and **not included**; without it
the script will not run. It is kept here because the self-test covers its
day-selection logic.

## Language

Code comments, CLI output and report fields are currently in German.
Translations and pull requests are welcome.

## License

MIT — see [LICENSE](LICENSE).
