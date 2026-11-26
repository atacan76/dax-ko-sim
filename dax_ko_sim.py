#!/usr/bin/env python3
"""dax-ko-sim v0.6 — offline simulation only.

Python >= 3.9, no extra packages, no accounts/keys, no network access.
Run:         python3 dax_ko_sim.py
Tests:       python3 dax_ko_sim.py --self-test
Own data:    python3 dax_ko_sim.py --csv dax.csv
Cost inputs: python3 dax_ko_sim.py --fee 0.50 --spread 0.04

CSV (UTF-8): timestamp,open,high,low,close
ISO 8601 timestamps WITH UTC offset, e.g. 2026-10-19T09:00:00+02:00.
Bar time = start of the 5-minute bar; last bar starts 17:25.
Only complete sessions 09:00-17:30 Europe/Berlin, Mon-Fri.
Exchange holidays are NOT detected automatically. Clean the CSV first.
All data is fully validated before a simulation starts.

Model: artificial long/short KO products, barrier fixed at entry,
ratio 0.01, leverage about 5, no issuer, no real ISINs.
Product value = distance to barrier * ratio, bid/ask +/- spread/2.
Not a realistic product valuation: financing, issuer risk, variable
spreads, order book, partial fills and latency are missing.
A knock-out is settled conservatively before a stop, based on index OHLC.
Stop and target in the same bar: stop first. Gaps can exceed the risk.

Signal: rule-based SMA-5/SMA-20 trend strategy, NOT a trained AI.
Only completed previous bars feed the signal; entry at the next open.
Target architecture for AI: signal proposal -> separate RiskGate.
No live feed, no news analysis, no broker, no continuous operation.
Every run starts a fresh attempt with 100 EUR (no resume).
Demo results are NOT proof of profitability.
With default costs of 1 EUR per order, a 2 EUR stop budget including
both fees is not workable: consistently no entries.

Outputs per run: configuration, SQLite event journal, trades.csv,
daily_reports.csv, report.json and report.txt.
Ctrl+C ends the run with status incomplete; an open simulated position
is not claimed to be closed. A file named STOP in the current directory
blocks new entries and closes an open position at the next simulated open.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import random
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Berlin')
VERSION = '0.6'
CSV_FIELDS = ['timestamp', 'open', 'high', 'low', 'close']
PRICE_PATTERN = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
PRODUCT_WARNING = ('Historical index prices do not turn the artificial KO product prices '
                   'into realistic derivative prices.')
STAMP_PATTERN = re.compile(
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?'
    r'(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)')


def cents(value):
    """EUR -> whole cents; half cents are rounded away from zero."""
    return int((Decimal(str(value))*100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def settlement(quantity, price):
    # Round the total fill value once, never the unit price.
    return cents(Decimal(quantity)*Decimal(str(price)))


def euros(value):
    return value / 100


@dataclass(frozen=True)
class Config:
    capital: float = 100.0
    risk: float = 2.0
    daily_loss: float = 5.0
    fee: float = 1.0  # per fill, assumption only, not a broker price
    spread: float = 0.04  # total product spread in EUR
    slippage: float = 0.01  # adverse deviation per unit and fill
    max_spread_pct: float = 0.01
    leverage: float = 5.0
    ratio: float = 0.01
    stop_fraction: float = 0.0015
    max_minutes: int = 60
    max_trades: int = 3

    def validate(self):
        for key, value in asdict(self).items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'Invalid parameter: {key}')
        if min(self.capital, self.risk, self.daily_loss, self.ratio,
               self.stop_fraction, self.max_minutes, self.max_trades) <= 0:
            raise ValueError('Capital, limits and time must be positive.')
        for key in ('capital', 'risk', 'daily_loss', 'fee'):
            value = Decimal(str(getattr(self, key)))
            if value != Decimal(cents(value))/100:
                raise ValueError(f'{key} must be given in whole cents.')
        if not 1 <= self.leverage <= 10 or self.stop_fraction >= 1/self.leverage:
            raise ValueError('Invalid leverage/stop distance.')

@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float

@dataclass
class Position:
    side: int
    barrier: float
    quantity: int
    ask: float
    stop: float
    target: float
    opened: datetime
    planned_risk: float

class RiskGate:
    """The only component that may approve new simulated positions."""
    @staticmethod
    def approve(c, cash, day_pnl, count, bar, side):
        if side not in (-1, 1):
            return None, 'NO_SIGNAL'
        if cents(day_pnl) <= -cents(c.daily_loss):
            return None, 'DAILY_LOSS_LIMIT'
        if count >= c.max_trades:
            return None, 'MAX_TRADES_PER_DAY'
        if not time(9, 20) <= bar.timestamp.time() < time(16, 30):
            return None, 'OUTSIDE_ENTRY_WINDOW'
        barrier = bar.open * (1 - side/c.leverage)
        mid = abs(bar.open - barrier) * c.ratio
        if c.spread / mid > c.max_spread_pct:
            return None, 'SPREAD_TOO_WIDE'
        distance = bar.open * c.stop_fraction
        ask = mid + c.spread/2 + c.slippage
        cash_cent, fee = cents(cash), cents(c.fee)
        budget = min(cents(c.risk), max(0, cents(c.daily_loss)+cents(day_pnl)))
        if budget <= 2*fee:
            return None, 'COSTS_EXCEED_RISK_BUDGET'
        p = Position(side, barrier, 0, ask,
                     bar.open-side*distance, bar.open+side*distance*2,
                     bar.timestamp, 0.0)
        stop_bid = bid_at(p, p.stop, c)
        # Conservative upper bound: every rounding step can cost half a cent.
        loss = Decimal(str(ask))-Decimal(str(stop_bid))
        quantity = min(int(Decimal(max(0, cash_cent-2*fee)+1)/100/Decimal(str(ask)))+1,
                       int(Decimal(max(0, budget-2*fee)+1)/100/loss)+1)
        while quantity > 0:
            purchase = settlement(quantity, ask)
            planned = purchase-settlement(quantity, stop_bid)+2*fee
            if purchase+2*fee <= cash_cent and planned <= budget:
                p.quantity, p.planned_risk = quantity, euros(planned)
                return p, 'APPROVED'
            quantity -= 1
        return None, 'NO_AFFORDABLE_QUANTITY'

def signal(history):
    if len(history) < 20:
        return 0
    fast = sum(history[-5:])/5
    slow = sum(history[-20:])/20
    if abs(fast/slow-1) < 0.0002:
        return 0
    return 1 if fast > slow else -1

def bid_at(position, underlying, c):
    return max(0.0, position.side*(underlying-position.barrier)*c.ratio
               - c.spread/2 - c.slippage)

def exit_price(p, b, c, emergency=False):
    """(product bid, reason, time); None while the position stays open."""
    if p.side*(b.open-p.barrier) <= 0:
        return 0.0, 'KO_GAP', b.timestamp
    if emergency:
        return bid_at(p, b.open, c), 'KILL_SWITCH', b.timestamp
    if p.side*(b.open-p.stop) <= 0:
        return bid_at(p, b.open, c), 'STOP_GAP', b.timestamp
    if p.side*(b.open-p.target) >= 0:
        return bid_at(p, b.open, c), 'TARGET_GAP', b.timestamp
    if (b.timestamp-p.opened).total_seconds()/60 >= c.max_minutes:
        return bid_at(p, b.open, c), 'TIME_LIMIT', b.timestamp
    adverse = b.low if p.side == 1 else b.high
    favorable = b.high if p.side == 1 else b.low
    end = b.timestamp + timedelta(minutes=5)
    if p.side*(adverse-p.barrier) <= 0:
        return 0.0, 'KO_INTRABAR_CONSERVATIVE', end
    if p.side*(adverse-p.stop) <= 0:
        return bid_at(p, p.stop, c), 'STOP', end
    if p.side*(favorable-p.target) >= 0:
        return bid_at(p, p.target, c), 'TARGET', end
    if b.timestamp.time() == time(17, 25):
        return bid_at(p, b.close, c), 'END_OF_DAY', end
    return None

def session_spec(session):
    if session == 'standard':
        return time(9), 102
    if session == 'diagnose_0905':
        return time(9,5), 101
    raise ValueError('Unknown session mode: '+str(session))


def validate_bars(bars, session='standard'):
    """No repairing, sorting, deduplicating or filling in of data."""
    session_start, bar_count = session_spec(session)
    if not bars:
        raise ValueError('No price data.')
    days = {}
    prev = None
    for number, b in enumerate(bars, 1):
        t = b.timestamp
        if t.tzinfo is None or t.utcoffset() is None:
            raise ValueError(f'Bar {number}: timestamp needs a UTC offset.')
        instant = t.astimezone(timezone.utc)
        local = instant.astimezone(TZ)
        # Direct Python callers must not hand RiskGate a UTC wall clock either.
        if (t.replace(tzinfo=None) != local.replace(tzinfo=None)
                or t.utcoffset() != local.utcoffset()):
            raise ValueError(f'Bar {number}: Berlin session time required; '
                             'the CSV import converts explicit offsets automatically.')
        if prev is not None and instant <= prev:
            raise ValueError(f'Bar {number}: timestamps must be unique and ascending.')
        prev = instant
        vals = (b.open, b.high, b.low, b.close)
        if any(not math.isfinite(x) or x <= 0 for x in vals):
            raise ValueError(f'Bar {number}: prices must be finite and positive.')
        if b.low > min(b.open, b.close) or b.high < max(b.open, b.close):
            raise ValueError(f'Bar {number}: invalid OHLC values.')
        if local.weekday() >= 5:
            raise ValueError(f'Bar {number}: weekend data is not allowed.')
        days.setdefault(local.date(), []).append(local)
    for day, times in days.items():
        start = datetime.combine(day, session_start, TZ)
        expected = [start+timedelta(minutes=5*i) for i in range(bar_count)]
        actual = set(times)
        expected_set = set(expected)
        missing = [t.isoformat() for t in expected if t not in actual]
        extra = [t.isoformat() for t in times if t not in expected_set]
        if times != expected:
            raise ValueError(f'{day}: complete 5-minute data {session_start:%H:%M}-17:25 required; '
                             f'missing: {missing[:3]}, outside grid/session: {extra[:3]}.')


def read_csv(path, session='standard'):
    """Strict UTF-8 import; returns fully validated bars only."""
    bars = []
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f, strict=True)
        try:
            header = next(reader, None)
            if header != CSV_FIELDS:
                raise ValueError('CSV header must be exactly timestamp,open,high,low,close.')
            for row in reader:
                line = reader.line_num
                if len(row) != len(CSV_FIELDS) or any(not cell.strip() for cell in row):
                    raise ValueError(f'CSV line {line}: exactly five non-empty fields required.')
                try:
                    if not STAMP_PATTERN.fullmatch(row[0]):
                        raise ValueError('ISO timestamp with T, seconds and UTC offset required.')
                    stamp = datetime.fromisoformat(row[0].replace('Z', '+00:00'))
                    if stamp.tzinfo is None or stamp.utcoffset() is None:
                        raise ValueError('Timestamp without UTC offset.')
                    if any(not PRICE_PATTERN.fullmatch(v.strip()) for v in row[1:]):
                        raise ValueError('Prices must be decimal numbers with a dot.')
                    # The offset defines the instant; never guess a local time zone.
                    bars.append(Bar(stamp.astimezone(TZ), *(float(v) for v in row[1:])))
                except (ValueError, OverflowError) as exc:
                    raise ValueError(f'CSV line {line}: {exc}') from exc
        except csv.Error as exc:
            raise ValueError(f'CSV line {reader.line_num}: invalid CSV: {exc}') from exc
    validate_bars(bars, session)
    return bars

def demo(days=5):
    rng = random.Random(42)
    date = datetime(2026, 1, 5, 9, tzinfo=TZ)
    bars = []
    price = 20000.0
    for d in range(days):
        while date.weekday() >= 5:
            date += timedelta(days=1)
        for i in range(102):
            op = price + rng.gauss(0, 2)
            price = op + 5*math.sin(i/15+d) + rng.gauss(0, 7)
            bars.append(Bar(date+timedelta(minutes=5*i), op,
                            max(op, price)+rng.uniform(1, 8),
                            min(op, price)-rng.uniform(1, 8), price))
        date += timedelta(days=1)
    return bars

def write_csv(path, rows, fields):
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

def simulate(bars, c, out, source, stop_file, session='standard', selection=None):
    c.validate()
    validate_bars(bars, session)
    session_start, bar_count = session_spec(session)
    run_label = (('SHORTENED HISTORICAL DIAGNOSTIC TEST 09:05-17:30' if session == 'diagnose_0905'
                  else 'STANDARD OFFLINE SIMULATION')+'; '+source+'; artificial KO product prices')
    out.mkdir(parents=True, exist_ok=False)
    config_payload = dict(asdict(c), run_label=run_label, session=session)
    (out/'config.json').write_text(json.dumps(config_payload, indent=2, ensure_ascii=False), encoding='utf-8')
    conn = sqlite3.connect(out/'journal.sqlite')
    conn.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, timestamp TEXT, kind TEXT, payload TEXT)')
    def log(t, kind, **payload):
        conn.execute('INSERT INTO events(timestamp,kind,payload) VALUES(?,?,?)',
                     (str(t), kind, json.dumps(payload, ensure_ascii=False, default=str)))
        conn.commit()
    log(bars[0].timestamp, 'MODEL_INFO', warning=PRODUCT_WARNING, source=source, run_label=run_label, session=session)
    cash, peak, drawdown = cents(c.capital), cents(c.capital), 0
    fee = cents(c.fee)
    paid_fees = 0
    position, day, history = None, None, []
    trades, daily, rejections = [], [], {}
    signals = []
    start_cash, count = cash, 0
    interrupted = False
    last_time = ''
    try:
        for b in bars:
            last_time = b.timestamp
            if b.timestamp.date() != day:
                if position:
                    raise RuntimeError('Overnight position: session incomplete.')
                if day is not None:
                    daily.append(dict(day=str(day), start=euros(start_cash), end=euros(cash),
                                      net=euros(cash-start_cash), trades=count))
                day, history, start_cash, count = b.timestamp.date(), [], cash, 0
            emergency = stop_file.exists() or (Path(__file__).resolve().parent/'STOP').exists()
            was_open = position is not None
            side, reason = None, 'KILL_SWITCH' if emergency else 'POSITION_OPEN'
            if not was_open and not emergency:
                side = signal(history)
                position, reason = RiskGate.approve(c, euros(cash), euros(cash-start_cash), count, b, side)
                if position:
                    cash -= settlement(position.quantity, position.ask)+fee
                    paid_fees += fee
                    count += 1
                    log(b.timestamp, 'OPEN', **asdict(position), cash=euros(cash),
                        purchase_value=euros(settlement(position.quantity, position.ask)),
                        fee=euros(fee))
                elif side:
                    rejections[reason] = rejections.get(reason, 0)+1
                    log(b.timestamp, 'REJECT', reason=reason)
            signal_row = dict(timestamp=b.timestamp.isoformat(), history_bars=len(history),
                              side=('LONG' if side == 1 else 'SHORT' if side == -1 else
                                    'NO_SIGNAL' if side == 0 else 'NOT_EVALUATED'),
                              reason=reason)
            signals.append(signal_row)
            log(b.timestamp, 'SIGNAL', **signal_row)
            if position:
                result = exit_price(position, b, c, emergency)
                # Daily loss based on the current liquidation value.
                equity = cash+settlement(position.quantity, bid_at(position, b.close, c))-fee
                if result is None and equity-start_cash <= -cents(c.daily_loss):
                    result = (bid_at(position, b.close, c), 'DAILY_LOSS_LIMIT',
                              b.timestamp+timedelta(minutes=5))
                if result:
                    bid, reason, closed = result
                    p = position
                    cash += settlement(p.quantity, bid)-fee
                    paid_fees += fee
                    net = settlement(p.quantity, bid)-settlement(p.quantity, p.ask)-2*fee
                    trade = dict(opened=p.opened.isoformat(), closed=closed.isoformat(),
                                 side='LONG' if p.side == 1 else 'SHORT', quantity=p.quantity,
                                 ask=p.ask, bid=bid, fees=euros(2*fee), net=euros(net), reason=reason,
                                 planned_risk=p.planned_risk)
                    trades.append(trade)
                    position = None
                    log(closed, 'CLOSE', **trade, cash=euros(cash),
                        sale_value=euros(settlement(p.quantity, bid)))
            equity = cash if position is None else cash+settlement(position.quantity,bid_at(position,b.close,c))-fee
            peak = max(peak, equity)
            drawdown = max(drawdown, peak-equity)
            log(b.timestamp+timedelta(minutes=5), 'EQUITY', equity=euros(equity), cash=euros(cash))
            history.append(b.close)
    except KeyboardInterrupt:
        interrupted = True
        log(last_time, 'INTERRUPTED', cash=euros(cash), fees=euros(paid_fees),
            open_position=asdict(position) if position else None)
    finally:
        conn.close()
    if day is not None:
        daily.append(dict(day=str(day), start=euros(start_cash), end=euros(cash) if not position else None,
                          net=euros(cash-start_cash) if not position else None, trades=count))
    wins = [cents(t['net']) for t in trades if t['net'] > 0]
    losses = [cents(t['net']) for t in trades if t['net'] < 0]
    def average(values):
        return euros(int((Decimal(sum(values))/len(values)).quantize(
            Decimal('1'), rounding=ROUND_HALF_UP))) if values else None
    signal_counts = {side: sum(row['side']==side for row in signals)
                     for side in ('LONG','SHORT','NO_SIGNAL','NOT_EVALUATED')}
    report = dict(version=VERSION, mode='OFFLINE_SIMULATION', source=source,
                  run_label=run_label, session=session, session_start=session_start.isoformat(),
                  session_end='17:30:00', expected_bars_per_day=bar_count,
                  signal_counts=signal_counts, signal_rows=len(signals),
                  warmup_rows=sum(row['history_bars']<20 and row['side']!='NOT_EVALUATED' for row in signals),
                  day_selection=selection, paper_trading_days_counted=0,
                  status='INCOMPLETE' if interrupted else 'COMPLETED',
                  real_orders_possible=False, artificial_products=True,
                  data_validation='Complete bars checked for every included day; '
                  'no check for entirely missing trading days or data origin.',
                  start_capital=c.capital, cash_balance=euros(cash),
                  final_equity=euros(cash) if not position else None,
                  net_pnl=euros(cash-cents(c.capital)) if not position else None,
                  trades=len(trades), win_rate=len(wins)/len(trades) if trades else None,
                  average_win=average(wins),
                  average_loss=average(losses),
                  net_expectancy=average([cents(t['net']) for t in trades]),
                  money_rounding='EUR cents; ROUND_HALF_UP per total fill; prices unrounded',
                  fees=euros(paid_fees),
                  max_drawdown_eur_at_bar_closes=euros(drawdown),
                  rejected_signals=rejections,
                  open_position=asdict(position) if position else None,
                  limitations=PRODUCT_WARNING+' Artificial KO products, fixed costs, no real AI/live prices. '
                  'Drawdown at bar closes only; no statement about real profitability.')
    (out/'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    trade_fields = ['opened','closed','side','quantity','ask','bid','fees','net','reason','planned_risk','model_warning','run_label']
    if not trades and session == 'diagnose_0905':
        # An empty trade table carries the label too, without a fake row.
        trade_fields.append('Report label: '+run_label)
    write_csv(out/'trades.csv', [dict(t, model_warning=PRODUCT_WARNING, run_label=run_label) for t in trades],
              trade_fields)
    write_csv(out/'daily_reports.csv', [dict(d, model_warning=PRODUCT_WARNING, run_label=run_label) for d in daily],
              ['day','start','end','net','trades','model_warning','run_label'])
    write_csv(out/'signals.csv', [dict(row, run_label=run_label, model_warning=PRODUCT_WARNING) for row in signals],
              ['timestamp','history_bars','side','reason','run_label','model_warning'])
    summary = ('#dax-ko-sim — simulation only\n'
               f"Label: {run_label}\nSignals: {signal_counts}\n"
               f"Data: {source}\nStatus: {report['status']}\n"
               f"Start: {c.capital:.2f} EUR | Final equity: {report['final_equity']}\n"
               f"Cash: {report['cash_balance']:.2f} EUR | Fees paid: {report['fees']:.2f} EUR\n"
               f"Open position: {position is not None}\n"
               f"Trades: {len(trades)} | Net: {report['net_pnl']} EUR\n"
               f"Rejections: {rejections}\nData check: {report['data_validation']}\n"
               f"{report['limitations']}\n")
    (out/'report.txt').write_text(summary, encoding='utf-8')
    print(summary)
    print(f'Reports: {out.resolve()}')
    return report

class Tests(unittest.TestCase):
    def setUp(self):
        self.b = Bar(datetime(2026,1,5,11,tzinfo=TZ),20000,20010,19990,20000)
    def test_cost_gate(self):
        p, reason = RiskGate.approve(Config(),100,0,0,self.b,1)
        self.assertIsNone(p)
        self.assertEqual(reason,'COSTS_EXCEED_RISK_BUDGET')
    def test_sizing_and_daily_limit(self):
        c = Config(fee=0.1)
        p,_ = RiskGate.approve(c,100,0,0,self.b,1)
        self.assertIsNotNone(p)
        self.assertLessEqual(p.planned_risk,2)
        self.assertLessEqual(p.quantity*p.ask+c.fee,100)
        self.assertIsNone(RiskGate.approve(c,100,-5,0,self.b,1)[0])
    def test_gap_exceeds_budget(self):
        c = Config(fee=0.1)
        p,_ = RiskGate.approve(c,100,0,0,self.b,1)
        b = Bar(self.b.timestamp+timedelta(minutes=5),19000,19010,18990,19000)
        price,reason,_ = exit_price(p,b,c)
        self.assertEqual(reason,'STOP_GAP')
        self.assertGreater(p.quantity*(p.ask-price)+2*c.fee,p.planned_risk)
    def test_both_touched_stop_first(self):
        c = Config(fee=0.1)
        for side in (1,-1):
            p,_ = RiskGate.approve(c,100,0,0,self.b,side)
            b = Bar(self.b.timestamp,20000,20100,19900,20000)
            self.assertEqual(exit_price(p,b,c)[1],'STOP')
    def test_knockout(self):
        c = Config(fee=0.1)
        p,_ = RiskGate.approve(c,100,0,0,self.b,1)
        b = Bar(self.b.timestamp,20000,20010,15000,19000)
        self.assertEqual(exit_price(p,b,c)[0],0)
    def test_invalid_data(self):
        with self.assertRaises(ValueError):
            validate_bars([self.b])
        bars = demo(1)
        with self.assertRaises(ValueError):
            validate_bars(bars+bars)
    def test_replay_accounting_and_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = simulate(demo(2),Config(fee=0.1),root/'run','TEST',root/'STOP')
            self.assertGreater(r['trades'],0)
            self.assertIsNone(r['open_position'])
            rows = list(csv.DictReader((root/'run'/'trades.csv').open()))
            self.assertAlmostEqual(r['net_pnl'],sum(float(t['net']) for t in rows))
            self.assertTrue(all(t['opened'][:10]==t['closed'][:10] for t in rows))
            self.assertEqual(cents(r['net_pnl']),sum(cents(t['net']) for t in rows))
            self.assertEqual(cents(r['fees']),sum(cents(t['fees']) for t in rows))
            with (root/'run'/'daily_reports.csv').open() as f:
                days = list(csv.DictReader(f))
            self.assertEqual(cents(r['net_pnl']),sum(cents(d['net']) for d in days))
            for d in days:
                self.assertEqual(cents(d['end'])-cents(d['start']),cents(d['net']))
            for stamp,kind,payload in self.read_events(root/'run'):
                if kind in ('OPEN','CLOSE','EQUITY'):
                    self.assertEqual(Decimal(str(payload['cash']))*100,
                                     cents(payload['cash']))
    def test_reserve_exit_fee_for_knockout(self):
        c = Config(fee=0.1)
        # One unit would be affordable if only one fee were reserved.
        p, _ = RiskGate.approve(c, 40.15, 0, 0, self.b, 1)
        self.assertIsNone(p)
        p, _ = RiskGate.approve(c, 40.25, 0, 0, self.b, 1)
        self.assertIsNotNone(p)
        self.assertGreaterEqual(40.25-p.quantity*p.ask-2*c.fee, 0)

    def test_daily_remaining_budget(self):
        c = Config(fee=0.1)
        for side in (1, -1):
            p, _ = RiskGate.approve(c, 100, -4.4, 0, self.b, side)
            self.assertIsNotNone(p)
            self.assertLessEqual(p.planned_risk, 0.6+1e-12)
            self.assertIsNone(RiskGate.approve(c, 100, -5.1, 0, self.b, side)[0])

    def test_exit_time_end_and_emergency(self):
        c = Config(fee=0.1)
        for side in (1, -1):
            p, _ = RiskGate.approve(c, 100, 0, 0, self.b, side)
            b = Bar(self.b.timestamp+timedelta(minutes=60),20000,20001,19999,20000)
            self.assertEqual(exit_price(p,b,c)[1], 'TIME_LIMIT')
            self.assertEqual(exit_price(p,b,c,True)[1], 'KILL_SWITCH')
            p.opened = self.b.timestamp.replace(hour=17,minute=20)
            b = Bar(p.opened+timedelta(minutes=5),20000,20001,19999,20000)
            self.assertEqual(exit_price(p,b,c)[1], 'END_OF_DAY')
            self.assertEqual(exit_price(p,b,c)[2].time(), time(17,30))

    def test_signal_uses_only_previous_closes(self):
        bars = demo(1)
        seen = []
        def inspect(history):
            seen.append(list(history))
            return 0
        with tempfile.TemporaryDirectory() as tmp:
            with patch(__name__+'.signal', side_effect=inspect):
                simulate(bars,Config(),Path(tmp)/'run','TEST',Path(tmp)/'STOP')
        self.assertEqual(len(seen), len(bars))
        for i, history in enumerate(seen):
            self.assertEqual(history, [b.close for b in bars[:i]])

    def test_daily_gap_loss_blocks_further_entries(self):
        bars = demo(1)
        for i, b in enumerate(bars):
            price = 20000 if i <= 20 else 19000
            bars[i] = Bar(b.timestamp,price,price+1,price-1,price)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(__name__+'.signal', return_value=1):
                r = simulate(bars,Config(fee=0.1),root/'run','TEST',root/'STOP')
            self.assertEqual(r['trades'], 2)  # time limit, then gap loss
            self.assertLess(r['net_pnl'], -5)
            self.assertGreater(r['rejected_signals']['DAILY_LOSS_LIMIT'], 0)
            self.assertIsNone(r['open_position'])

    def test_interruption_includes_paid_entry_fee(self):
        bars = demo(1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(__name__+'.signal', return_value=1):
                with patch(__name__+'.exit_price', side_effect=KeyboardInterrupt):
                    r = simulate(bars,Config(fee=0.1),root/'run','TEST',root/'STOP')
            self.assertEqual(r['status'], 'INCOMPLETE')
            self.assertIsNotNone(r['open_position'])
            self.assertIsNone(r['final_equity'])
            self.assertEqual(r['fees'], 0.1)

    @staticmethod
    def read_events(out):
        with sqlite3.connect(out/'journal.sqlite') as db:
            return [(stamp, kind, json.loads(payload)) for stamp, kind, payload in
                    db.execute('SELECT timestamp,kind,payload FROM events ORDER BY id')]

    def test_cent_rounding_and_fractional_product_prices(self):
        self.assertEqual(cents('1.005'), 101)
        self.assertEqual(cents('-1.005'), -101)
        self.assertEqual(settlement(3, '0.335'), 101)
        self.assertNotEqual(settlement(3, '0.335'), 3*cents('0.335'))
        for field in ('capital', 'risk', 'daily_loss', 'fee'):
            with self.assertRaises(ValueError):
                Config(**{field: 1.005}).validate()
        # Actually booked stop losses and funding at cent boundaries.
        c = Config(fee=0.1)
        for price in (19794, 20000.12345, 19999.98765):
            b = Bar(self.b.timestamp,price,price+1,price-1,price)
            for side in (1,-1):
                p, _ = RiskGate.approve(c,100,-4.44,0,b,side)
                if p:
                    actual = settlement(p.quantity,p.ask)-settlement(
                        p.quantity,bid_at(p,p.stop,c))+2*cents(c.fee)
                    self.assertEqual(cents(p.planned_risk),actual)
                    self.assertLessEqual(actual,56)
                    self.assertLessEqual(settlement(p.quantity,p.ask)+20,10000)

    def test_daily_forced_exit_lock_and_next_day_reset(self):
        # Real exit logic: first a -4.44 EUR gap, then the cent boundary at the close.
        bars = demo(2)
        for i,b in enumerate(bars):
            price = 20000 if i < 5 else 19794
            bars[i] = Bar(b.timestamp,price,price+0.01,price-0.01,price)
        b = bars[6]
        close = 19764.31  # just above the stop at 19764.309
        bars[6] = Bar(b.timestamp,19794,19794.01,close,close)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(__name__+'.signal', return_value=1):
                r = simulate(bars,Config(fee=0.1),root/'run','TEST',root/'STOP')
            events = self.read_events(root/'run')
            opens = [(t,p) for t,k,p in events if k=='OPEN']
            closes = [(t,p) for t,k,p in events if k=='CLOSE']
            first_day = str(bars[0].timestamp.date())
            day_closes = [(t,p) for t,p in closes if t.startswith(first_day)]
            self.assertEqual([p['reason'] for t,p in day_closes],['STOP_GAP','DAILY_LOSS_LIMIT'])
            self.assertEqual([cents(p['net']) for t,p in day_closes],[-444,-56])
            self.assertEqual(day_closes[-1][0],str(b.timestamp+timedelta(minutes=5)))
            self.assertEqual(cents(day_closes[-1][1]['cash']),9500)
            self.assertEqual(sum(t.startswith(first_day) for t,p in opens),2)
            rejects = [(t,p) for t,k,p in events if k=='REJECT' and p['reason']=='DAILY_LOSS_LIMIT']
            self.assertTrue(rejects)
            self.assertTrue(all(t.startswith(first_day) for t,p in rejects))
            second_opens = [(t,p) for t,p in opens if not t.startswith(first_day)]
            self.assertEqual(len(second_opens),3)  # trade counter reset as well
            self.assertEqual(second_opens[0][0],str(bars[106].timestamp))
            self.assertEqual(second_opens[0][1]['quantity'],2)  # full risk budget
            with (root/'run'/'daily_reports.csv').open() as f:
                daily = list(csv.DictReader(f))
            self.assertEqual(cents(daily[0]['net']),-500)
            self.assertEqual(cents(daily[1]['start']),9500)
            self.assertEqual(cents(r['net_pnl']),sum(cents(p['net']) for t,p in closes))
            self.assertEqual(r['cash_balance'],r['final_equity'])
            self.assertIsNone(r['open_position'])

    def test_emergency_with_open_position_accounting(self):
        bars = demo(1)
        for i,b in enumerate(bars):
            bars[i] = Bar(b.timestamp,20000,20000.01,19999.99,20000)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stop = root/'STOP'
            calls = 0
            def activate_stop(history):
                nonlocal calls
                calls += 1
                if calls == 5:
                    stop.touch()  # after this bar is checked, before the next open
                return 1
            with patch(__name__+'.signal', side_effect=activate_stop):
                r = simulate(bars,Config(fee=0.1),root/'run','TEST',stop)
            events = self.read_events(root/'run')
            closes = [(t,p) for t,k,p in events if k=='CLOSE']
            self.assertEqual(len(closes),1)
            self.assertEqual(closes[0][0],str(bars[5].timestamp))
            self.assertEqual(closes[0][1]['reason'],'KILL_SWITCH')
            self.assertEqual(closes[0][1]['net'],-0.32)
            self.assertEqual(r['fees'],0.20)
            self.assertEqual(r['cash_balance'],99.68)
            self.assertEqual(r['final_equity'],99.68)
            self.assertIsNone(r['open_position'])
            self.assertEqual(r['status'],'COMPLETED')
            self.assertEqual(sum(k=='OPEN' for t,k,p in events),1)

    def test_interrupted_open_position_cash_and_files(self):
        bars = demo(1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(__name__+'.signal',return_value=1):
                with patch(__name__+'.exit_price',side_effect=KeyboardInterrupt):
                    r = simulate(bars,Config(fee=0.1),root/'run','TEST',root/'STOP')
            p = r['open_position']
            expected = 10000-settlement(p['quantity'],p['ask'])-10
            self.assertEqual(cents(r['cash_balance']),expected)
            self.assertEqual(r['fees'],0.10)
            self.assertEqual(r['trades'],0)
            self.assertIsNone(r['net_pnl'])
            self.assertIsNone(r['final_equity'])
            self.assertEqual(r['status'],'INCOMPLETE')
            self.assertEqual(json.loads((root/'run'/'report.json').read_text()),
                             json.loads(json.dumps(r,default=str)))
            events = self.read_events(root/'run')
            last = events[-1]
            self.assertEqual(last[1],'INTERRUPTED')
            self.assertEqual(cents(last[2]['cash']),expected)
            self.assertEqual(last[2]['fees'],0.10)
            self.assertIsNotNone(last[2]['open_position'])
            self.assertFalse(any(k=='CLOSE' for t,k,p in events))
            with (root/'run'/'daily_reports.csv').open() as f:
                day = next(csv.DictReader(f))
            self.assertEqual(day['end'],'')
            self.assertEqual(day['net'],'')

    @staticmethod
    def csv_session(day='2026-01-05'):
        start = datetime.combine(datetime.fromisoformat(day).date(),time(9),TZ)
        return [[(start+timedelta(minutes=5*i)).isoformat(),
                 '20000.125', '20001.25', '19999.5', '20000.75'] for i in range(102)]

    def import_rows(self, rows, header=None, bom=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'input.csv'
            with path.open('w',newline='',encoding='utf-8-sig' if bom else 'utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(CSV_FIELDS if header is None else header)
                writer.writerows(rows)
            return read_csv(path)

    def test_csv_valid_offsets_and_dst_transitions(self):
        # Fridays and Mondays before/after both 2026 DST changes.
        for day,offset in [('2026-01-05',1),('2026-03-27',1),('2026-03-30',2),
                           ('2026-07-06',2),('2026-10-23',2),('2026-10-26',1)]:
            for utc in (False,True):
                with self.subTest(day=day,utc=utc):
                    rows = self.csv_session(day)
                    if utc:
                        for row in rows:
                            row[0] = datetime.fromisoformat(row[0]).astimezone(
                                timezone.utc).isoformat().replace('+00:00','Z')
                    bars = self.import_rows(rows,bom=True)
                    self.assertEqual(len(bars),102)
                    self.assertEqual(bars[0].timestamp.time(),time(9))
                    self.assertEqual(bars[-1].timestamp.time(),time(17,25))
                    self.assertEqual(bars[0].timestamp.utcoffset(),timedelta(hours=offset))
                    self.assertEqual(bars[0].open,20000.125)
        rows = self.csv_session('2026-03-27')+self.csv_session('2026-03-30')
        self.assertEqual(len(self.import_rows(rows)),204)  # do not fill in the weekend

    def test_csv_invalid_timestamps(self):
        for stamp in ('2026-01-05T09:00:00','2026-01-05','not-a-date',
                      '2026-02-30T09:00:00+01:00','2026-01-05T09:00:00+24:00','2026-01-05T09:00:00+00:60',
                      '2026-01-05T09:00:01+01:00','2026-01-05T09:00:00.000001+01:00',
                      '2026-01-05T09:00:00.0000001+01:00'):
            with self.subTest(stamp=stamp):
                rows = self.csv_session()
                rows[0][0] = stamp
                with self.assertRaises(ValueError):
                    self.import_rows(rows)

    def test_csv_wrong_summer_offset_and_weekends(self):
        rows = self.csv_session('2026-07-06')
        for row in rows:
            row[0] = row[0].replace('+02:00','+01:00')
        with self.assertRaises(ValueError):
            self.import_rows(rows)  # yields 10:00-18:25 Berlin time
        for day in ('2026-03-29','2026-10-25','2026-01-10'):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.import_rows(self.csv_session(day))

    def test_csv_duplicates_order_and_missing_bars(self):
        base = self.csv_session()
        cases = [base[:20]+base[21:],base[1:],base[:-1],
                 base[:20]+[base[20]]+base[20:],list(reversed(base))]
        mixed = [list(row) for row in base]
        mixed[1][0] = datetime.fromisoformat(base[0][0]).astimezone(timezone.utc).isoformat()
        cases.append(mixed)  # same instant, different offset notation
        swapped = [list(row) for row in base]
        swapped[20],swapped[21] = swapped[21],swapped[20]
        cases.append(swapped)
        for i,rows in enumerate(cases):
            with self.subTest(case=i),self.assertRaises(ValueError):
                self.import_rows(rows)

    def test_csv_invalid_prices(self):
        for field in range(1,5):
            for value in ('NaN','inf','-inf','0','-1','abc','1e999','20_000','20,000',''):
                rows = self.csv_session()
                rows[10][field] = value
                with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                    self.import_rows(rows)
        for prices in (['20000','19999','19998','20001'],
                       ['20000','20002','20001','20001'],
                       ['20000','19999','20001','20000']):
            rows = self.csv_session()
            rows[10][1:] = prices
            with self.subTest(prices=prices),self.assertRaises(ValueError):
                self.import_rows(rows)

    def test_csv_schema_empty_rows_and_malformed_quotes(self):
        for header in ([],CSV_FIELDS[:-1],CSV_FIELDS+['volume'],
                       ['timestamp','open','high','low','open'],list(reversed(CSV_FIELDS)),
                       ['timestamp;open;high;low;close']):
            with self.subTest(header=header),self.assertRaises(ValueError):
                self.import_rows(self.csv_session(),header=header)
        for row in ([],[''],['2026-01-05T09:00:00+01:00','20000'],
                    self.csv_session()[0]+['extra']):
            with self.subTest(row=row),self.assertRaises(ValueError):
                self.import_rows([row]+self.csv_session()[1:])
        with self.assertRaises(ValueError):
            self.import_rows([])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'broken.csv'
            for content in ('','timestamp,open,high,low,close\n"unclosed',
                            'timestamp,open,high,low,close\n\n'):
                path.write_text(content)
                with self.subTest(content=content),self.assertRaises(ValueError):
                    read_csv(path)

    def test_direct_bars_reject_naive_or_wrong_local_timezone(self):
        bars = demo(1)
        b = bars[0]
        for stamp in (b.timestamp.replace(tzinfo=None),b.timestamp.astimezone(timezone.utc)):
            with self.subTest(stamp=stamp),self.assertRaises(ValueError):
                validate_bars([Bar(stamp,b.open,b.high,b.low,b.close)]+bars[1:])

    def test_invalid_csv_creates_no_simulation_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid = demo(1)[:-1]
            with self.assertRaises(ValueError):
                simulate(invalid,Config(),root/'run','TEST',root/'STOP')
            self.assertFalse((root/'run').exists())
            path = root/'bad.csv'
            path.write_text('timestamp,open,high,low,close\n')
            with patch('sys.argv',['dax_ko_sim.py','--csv',str(path),'--out',str(root/'cli')]):
                self.assertEqual(main(),1)
            self.assertFalse((root/'cli').exists())

    def test_product_warning_in_all_new_result_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(__name__+'.signal',return_value=1):
                r = simulate(demo(1),Config(fee=0.1),root/'run',
                             'Historical index prices (TEST, artificial data)',root/'STOP')
            self.assertIn(PRODUCT_WARNING,r['limitations'])
            self.assertIn(PRODUCT_WARNING,(root/'run'/'report.txt').read_text())
            self.assertIn(PRODUCT_WARNING,(root/'run'/'report.json').read_text())
            for name in ('trades.csv','daily_reports.csv'):
                with (root/'run'/name).open() as f:
                    rows = list(csv.DictReader(f))
                self.assertTrue(rows)
                self.assertTrue(all(row['model_warning']==PRODUCT_WARNING for row in rows))
            self.assertEqual(self.read_events(root/'run')[0][1],'MODEL_INFO')

    def test_artificial_example_file(self):
        path = Path(__file__).resolve().parent/'examples'/'synthetic_dax_5min.csv'
        bars = read_csv(path)
        self.assertEqual(len(bars),204)
        self.assertEqual(bars[0].timestamp.isoformat(),'2026-03-27T09:00:00+01:00')
        self.assertEqual(bars[102].timestamp.isoformat(),'2026-03-30T09:00:00+02:00')
        self.assertEqual(bars[0].open,20000)
        self.assertEqual(bars[1].open,20000.125)
        self.assertEqual(bars[0].close,20000.25)

    @staticmethod
    def diagnostic_bars(days=1):
        return [b for b in demo(days) if b.timestamp.time()>=time(9,5)]

    def test_diagnostic_session_boundaries_are_separate(self):
        diagnostic = self.diagnostic_bars()
        validate_bars(diagnostic,session='diagnose_0905')
        validate_bars(demo(1))
        for bars,mode in ((diagnostic,'standard'),(demo(1),'diagnose_0905'),
                          (diagnostic[1:],'diagnose_0905'),(diagnostic[:-1],'diagnose_0905'),
                          (diagnostic[:40]+diagnostic[41:],'diagnose_0905')):
            with self.subTest(mode=mode,count=len(bars)),self.assertRaises(ValueError):
                validate_bars(bars,session=mode)
        last=diagnostic[-1]
        extra=Bar(last.timestamp+timedelta(minutes=5),last.open,last.high,last.low,last.close)
        with self.assertRaises(ValueError):
            validate_bars(diagnostic+[extra],session='diagnose_0905')
        with self.assertRaises(ValueError):
            validate_bars(diagnostic,session='unknown')

    def test_diagnostic_history_and_warmup_reset(self):
        bars=self.diagnostic_bars(2)
        seen=[]
        def inspect(history):
            seen.append(list(history))
            return signal_original(history)
        signal_original=signal
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch(__name__+'.signal',side_effect=inspect):
                r=simulate(bars,Config(),root/'run','ARTIFICIAL TEST DATA',root/'STOP',session='diagnose_0905')
            with (root/'run'/'signals.csv').open() as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(len(seen),202)
            for i,history in enumerate(seen):
                day_start=(i//101)*101
                self.assertEqual(history,[b.close for b in bars[day_start:i]])
            for offset in (0,101):
                self.assertEqual(rows[offset+20]['timestamp'][11:16],'10:45')
                self.assertEqual(int(rows[offset+20]['history_bars']),20)
                self.assertTrue(all(row['side']=='NO_SIGNAL' for row in rows[offset:offset+20]))
            self.assertEqual(r['session'],'diagnose_0905')
            self.assertEqual(r['trades'],0)
            with (root/'run'/'trades.csv').open() as f:
                empty=list(csv.reader(f))
            self.assertEqual(len(empty),1)
            self.assertIn('SHORTENED HISTORICAL DIAGNOSTIC TEST',empty[0][-1])
            self.assertEqual(r['warmup_rows'],40)

    def test_diagnostic_position_closes_without_overnight(self):
        bars=[Bar(b.timestamp,20000,20000.01,19999.99,20000) for b in self.diagnostic_bars(2)]
        # Test fees of 0.10 only; the real diagnosis run uses plain Config().
        def one_signal(history):
            return 1 if len(history)==88 else 0  # entry at 16:25
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch(__name__+'.signal',side_effect=one_signal):
                r=simulate(bars,Config(fee=0.1),root/'run','ARTIFICIAL TEST DATA',root/'STOP',session='diagnose_0905')
            with (root/'run'/'trades.csv').open() as f:rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),2)
            self.assertTrue(all(t['reason']=='TIME_LIMIT' and t['closed'][11:16]=='17:25' for t in rows))
            self.assertIsNone(r['open_position'])
            self.assertEqual(r['fees'],0.4)
            self.assertEqual(r['final_equity'],99.36)
        # Extra direct check of the last bar for both directions.
        c=Config(fee=0.1)
        for side in (1,-1):
            p,_=RiskGate.approve(c,100,0,0,self.b,side)
            p.opened=self.b.timestamp.replace(hour=17,minute=20)
            b=Bar(p.opened+timedelta(minutes=5),20000,20000.01,19999.99,20000)
            price,reason,closed=exit_price(p,b,c)
            self.assertEqual(reason,'END_OF_DAY')
            self.assertEqual(closed.time(),time(17,30))

    def test_diagnostic_selection_excludes_incomplete_day(self):
        from historical_diagnosis import select_days
        from decimal import Decimal
        def rows(day,step,count):
            start=datetime(2026,1,day,9,5,tzinfo=TZ)
            return [(start+timedelta(minutes=step*i),['20000','20001','19999','20000'],
                     (Decimal(20000),Decimal(20001),Decimal(19999),Decimal(20000)),i+2) for i in range(count)]
        five=rows(5,5,101)+rows(6,5,101)
        one=rows(5,1,505)+rows(6,1,505)
        del one[505+123]
        selected,days=select_days(five,one)
        self.assertEqual(len(selected),101)
        self.assertTrue(days[0]['used'])
        self.assertFalse(days[1]['used'])
        self.assertIn('MINUTE_COVERAGE',days[1]['reason'])

    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'STOP').touch()
            r = simulate(demo(1),Config(fee=0.1),root/'run','TEST',root/'STOP')
            self.assertEqual(r['trades'],0)
            self.assertEqual(r['final_equity'],100)

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--capital', type=float, default=100.0, help='Starting capital of this new simulation run (EUR)')
    parser.add_argument('--csv', type=Path, help='CSV file with complete 5-minute price data')
    parser.add_argument('--fee', type=float, default=1.0, help='Fee per fill, assumption (EUR)')
    parser.add_argument('--spread', type=float, default=0.04, help='Product spread, assumption (EUR)')
    parser.add_argument('--slippage', type=float, default=0.01, help='Slippage per unit and fill, assumption (EUR)')
    parser.add_argument('--out', type=Path, default=Path('dax_ko_sim_results'), help='Base folder for run results')
    parser.add_argument('--self-test', action='store_true', help='Run the built-in test suite')
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
        return 0 if result.wasSuccessful() else 1
    try:
        c = Config(capital=args.capital,fee=args.fee,spread=args.spread,slippage=args.slippage)
        c.validate()
        bars = read_csv(args.csv) if args.csv else demo()
        source = f'CSV (origin/authenticity not verified): {args.csv.resolve()}' if args.csv else 'SYNTHETIC TEST DATA — NOT REAL DAX PRICES'
        run = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        report = simulate(bars,c,args.out/run,source,Path('STOP'))
        return 0 if report['status']=='COMPLETED' else 130
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print(f'Aborted: {exc}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
