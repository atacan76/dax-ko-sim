#!/usr/bin/env python3
"""Shortened historical diagnostic test using local FirstRate files, no network."""
import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from dax_ko_sim import Config, CSV_FIELDS, PRODUCT_WARNING, read_csv, simulate

LABEL = 'REAL HISTORICAL INDEX DATA; artificial KO product prices; SHORTENED HISTORICAL DIAGNOSTIC TEST 09:05-17:30'
SESSION = 'diagnose_0905'
EASTERN = ZoneInfo('America/New_York')
BERLIN = ZoneInfo('Europe/Berlin')
DEFAULT_PROBE = Path(__file__).resolve().parent/'historische_daten'/'firstrate_dax_probe_20261020'


def load_provider(path):
    rows = []
    previous = None
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader = csv.reader(f,strict=True)
        if next(reader,None) != CSV_FIELDS:
            raise ValueError('Unerwartete Anbieter-Kopfzeile: '+str(path))
        for number,row in enumerate(reader,2):
            if len(row)!=5:
                raise ValueError(f'{path}:{number}: fünf Felder erforderlich.')
            naive = datetime.strptime(row[0],'%Y-%m-%d %H:%M:%S')
            candidates = set()
            for fold in (0,1):
                instant = naive.replace(tzinfo=EASTERN,fold=fold).astimezone(timezone.utc)
                if instant.astimezone(EASTERN).replace(tzinfo=None)==naive:
                    candidates.add(instant)
            if len(candidates)!=1:
                raise ValueError('Mehrdeutige/nicht existente Quellzeit: '+row[0])
            stamp = candidates.pop().astimezone(BERLIN)
            if previous is not None and stamp<=previous:
                raise ValueError(f'{path}:{number}: Duplikat/Reihenfolgefehler.')
            previous = stamp
            try:
                values = tuple(Decimal(v) for v in row[1:])
            except InvalidOperation:
                raise ValueError(f'{path}:{number}: ungültiger Preis.') from None
            if any(not v.is_finite() or v<=0 for v in values):
                raise ValueError(f'{path}:{number}: ungültiger Preis.')
            op,hi,lo,cl = values
            if not lo<=min(op,cl)<=max(op,cl)<=hi:
                raise ValueError(f'{path}:{number}: OHLC verletzt.')
            rows.append((stamp,row[1:],values,number))
    if not rows:raise ValueError('Leere Anbieterdatei: '+str(path))
    return rows


def select_days(five, one):
    groups = defaultdict(list)
    for row in five:groups[row[0].date()].append(row)
    for row in one:groups[row[0].date()]  # auch einseitig vorhandene Tage prüfen
    minute_map = {r[0]:r for r in one}
    records=[];selected=[]
    for day,rows in sorted(groups.items()):
        start = datetime.combine(day,time(9,5),BERLIN)
        expected_minutes = [start+timedelta(minutes=i) for i in range(505)]
        expected_five = [start+timedelta(minutes=5*i) for i in range(101)]
        end = start+timedelta(minutes=505)
        included = [r for r in rows if start<=r[0]<end]
        missing_one = [t for t in expected_minutes if t not in minute_map]
        actual = [r[0] for r in included]
        mismatches=[]
        for stamp,text,values,line in included:
            children = [minute_map[t][2] for t in
                        (stamp+timedelta(minutes=i) for i in range(5)) if t in minute_map]
            if children and (children[0][0],max(p[1] for p in children),
                             min(p[2] for p in children),children[-1][3])!=values:
                mismatches.append(stamp.isoformat())
        reasons=[]
        if day.weekday()>=5:reasons.append('WEEKEND')
        if actual!=expected_five:reasons.append('5MIN_RASTER_UNVOLLSTAENDIG_ODER_ABWEICHEND')
        if missing_one:reasons.append('MINUTE_COVERAGE_INCOMPLETE')
        if mismatches:reasons.append('AGGREGATION_MISMATCH')
        use = not reasons
        if use:selected.extend(included)
        records.append(dict(day=str(day),used=use,reason='VERWENDET: Raster und Minutenaggregation bestätigt' if use else '; '.join(reasons),
                            raw_5min_rows=len(rows),selected_5min_rows=len(included) if use else 0,
                            candidate_5min_rows=len(included),missing_minutes=len(missing_one),
                            missing_minute_labels=[t.isoformat() for t in missing_one],
                            aggregation_mismatches=mismatches,
                            excluded_source_rows=[dict(line=r[3],timestamp=r[0].isoformat(),
                                reason='OUTSIDE_DIAGNOSTIC_WINDOW' if not start<=r[0]<end else 'DAY_NOT_APPROVED')
                                for r in rows if not use or not start<=r[0]<end]))
    return selected,records


def prepare_probe(probe, out):
    manifest = json.loads((probe/'herkunft_und_pruefsummen.json').read_text())
    for item in manifest['files']:
        path = probe/'original'/item['relative_path']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Original checksum changed: '+str(path))
    docs = (probe/'original'/'_readme_documentation.txt').read_text()
    if not all(word in docs for word in ('DAX Index','US Eastern Time','Timestamps run from the start')):
        raise ValueError('Instrument/time zone/bar start not confirmed by the available documentation.')
    five = load_provider(probe/'original'/'DAX_5min_sample.csv')
    one = load_provider(probe/'original'/'DAX_1min_sample.csv')
    selected,records = select_days(five,one)
    if not selected:raise ValueError('No day with confirmed diagnostic coverage.')
    out.mkdir(parents=True,exist_ok=False)
    path = out/'REAL_INDEX_DATA_SHORTENED_DIAGNOSIS_0905_1730.csv'
    with path.open('x',encoding='utf-8',newline='') as f:
        writer=csv.writer(f);writer.writerow(CSV_FIELDS)
        for stamp,original_prices,values,line in selected:
            writer.writerow([stamp.isoformat()]+original_prices)
    # Echter Import mit separater Sitzungsvalidierung, kein Monkeypatch.
    bars = read_csv(path,session=SESSION)
    selection = dict(run_label=LABEL,used_days=[r['day'] for r in records if r['used']],
                     excluded_days=[dict(day=r['day'],reason=r['reason']) for r in records if not r['used']],
                     days=records,selected_rows=len(selected),source_5min_rows=len(five),
                     excluded_source_rows=sum(len(r['excluded_source_rows']) for r in records),
                     scope='Nur Tage in der Anbieterprobe; kein vollständiger Börsenkalender.',
                     limitation='Interne Minutenabdeckung/OHLC bestätigt; keine unabhängige Sekundentickreferenz, Indexvariante/ISIN nicht bestätigt.',
                     transformations=['US Eastern (America/New_York) nach Europe/Berlin mit ISO-Offset',
                                      'Nur bestätigte Tage und Balkenanfänge 09:05–17:25',
                                      'Originale OHLC-Textfelder byteinhaltlich übernommen; keine Rundung/Aggregation/Füllung'],
                     original_file_hashes=manifest['files'],
                     converted_csv_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                     product_warning=PRODUCT_WARNING)
    (out/'day_selection.json').write_text(json.dumps(selection,ensure_ascii=False,indent=2)+'\n')
    with (out/'day_selection.csv').open('x',encoding='utf-8',newline='') as f:
        fields=['day','used','reason','raw_5min_rows','selected_5min_rows','missing_minutes','run_label','model_warning']
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for r in records:
            writer.writerow(dict({k:r[k] for k in fields if k in r},run_label=LABEL,model_warning=PRODUCT_WARNING))
    return bars,selection,path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe',type=Path,default=DEFAULT_PROBE,help='Folder with the local FirstRate sample')
    parser.add_argument('--out',type=Path,default=Path('historical_diagnosis_results'),help='Base folder for results')
    args=parser.parse_args()
    try:
        run=args.out/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        bars,selection,path=prepare_probe(args.probe,run)
        report=simulate(bars,Config(),run/'simulation',
                        'REAL HISTORICAL INDEX DATA — FirstRate DAX user sample; exact index variant unconfirmed',
                        Path('STOP'),session=SESSION,selection=selection)
        print('Day selection and converted original prices:',run.resolve())
        return 0 if report['status']=='COMPLETED' else 130
    except (ValueError,OSError,KeyError,csv.Error) as exc:
        print('Diagnosis aborted:',exc)
        return 1


if __name__=='__main__':raise SystemExit(main())
