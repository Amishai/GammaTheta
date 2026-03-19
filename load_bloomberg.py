#!/usr/bin/env python3
"""
Bloomberg BQL Vol Surface Loader for FX Gamma Dashboard
========================================================
Pulls FX option vol surfaces from Bloomberg and writes mkt_data.json
which the dashboard can load via the "Load from File" button.

Requirements:
    pip install blpapi   (Bloomberg Python API)
    Bloomberg Terminal must be running with active session.

Usage:
    python load_bloomberg.py                     # Pull defaults (EURUSD, USDJPY, GBPUSD)
    python load_bloomberg.py EURUSD USDJPY       # Specific pairs
    python load_bloomberg.py --all-g10           # All G10 pairs
    python load_bloomberg.py --output my_vols.json

The output JSON can be loaded into the dashboard's Market Data tab.
"""
import json, sys, os
from datetime import datetime

# Tenor definitions matching the dashboard
TENORS = [
    {'label': 'O/N', 'bbg': 'ON',  'T': 1/365},
    {'label': '1W',  'bbg': '1W',  'T': 7/365},
    {'label': '2W',  'bbg': '2W',  'T': 14/365},
    {'label': '1M',  'bbg': '1M',  'T': 1/12},
    {'label': '2M',  'bbg': '2M',  'T': 2/12},
    {'label': '3M',  'bbg': '3M',  'T': 3/12},
    {'label': '6M',  'bbg': '6M',  'T': 6/12},
    {'label': '9M',  'bbg': '9M',  'T': 9/12},
    {'label': '1Y',  'bbg': '1Y',  'T': 1.0},
    {'label': '2Y',  'bbg': '2Y',  'T': 2.0},
]

# Bloomberg ticker patterns for FX options
# ATM:    EURUSDV1M Curncy   (ATM vol)
# RR25:   EURUSD25R1M Curncy (25-delta risk reversal)
# RR10:   EURUSD10R1M Curncy (10-delta risk reversal)
# FLY25:  EURUSD25B1M Curncy (25-delta butterfly)
# FLY10:  EURUSD10B1M Curncy (10-delta butterfly - may not exist for all)
# FWD:    EUR1M Curncy        (forward points)
# SPOT:   EURUSD Curncy

BBG_PAIRS = {
    'EURUSD': {'spot': 'EURUSD Curncy', 'vol': 'EURUSDV', 'rr25': 'EURUSD25R', 'rr10': 'EURUSD10R', 'fly25': 'EURUSD25B', 'fly10': 'EURUSD10B', 'fwd': 'EUR'},
    'USDJPY': {'spot': 'USDJPY Curncy', 'vol': 'USDJPYV', 'rr25': 'USDJPY25R', 'rr10': 'USDJPY10R', 'fly25': 'USDJPY25B', 'fly10': 'USDJPY10B', 'fwd': 'JPY'},
    'GBPUSD': {'spot': 'GBPUSD Curncy', 'vol': 'GBPUSDV', 'rr25': 'GBPUSD25R', 'rr10': 'GBPUSD10R', 'fly25': 'GBPUSD25B', 'fly10': 'GBPUSD10B', 'fwd': 'GBP'},
    'USDCHF': {'spot': 'USDCHF Curncy', 'vol': 'USDCHFV', 'rr25': 'USDCHF25R', 'rr10': 'USDCHF10R', 'fly25': 'USDCHF25B', 'fly10': 'USDCHF10B', 'fwd': 'CHF'},
    'AUDUSD': {'spot': 'AUDUSD Curncy', 'vol': 'AUDUSDV', 'rr25': 'AUDUSD25R', 'rr10': 'AUDUSD10R', 'fly25': 'AUDUSD25B', 'fly10': 'AUDUSD10B', 'fwd': 'AUD'},
    'NZDUSD': {'spot': 'NZDUSD Curncy', 'vol': 'NZDUSDV', 'rr25': 'NZDUSD25R', 'rr10': 'NZDUSD10R', 'fly25': 'NZDUSD25B', 'fly10': 'NZDUSD10B', 'fwd': 'NZD'},
    'USDCAD': {'spot': 'USDCAD Curncy', 'vol': 'USDCADV', 'rr25': 'USDCAD25R', 'rr10': 'USDCAD10R', 'fly25': 'USDCAD25B', 'fly10': 'USDCAD10B', 'fwd': 'CAD'},
    'USDSEK': {'spot': 'USDSEK Curncy', 'vol': 'USDSEKV', 'rr25': 'USDSEK25R', 'rr10': 'USDSEK10R', 'fly25': 'USDSEK25B', 'fly10': 'USDSEK10B', 'fwd': 'SEK'},
    'USDNOK': {'spot': 'USDNOK Curncy', 'vol': 'USDNOKV', 'rr25': 'USDNOK25R', 'rr10': 'USDNOK10R', 'fly25': 'USDNOK25B', 'fly10': 'USDNOK10B', 'fwd': 'NOK'},
    'EURGBP': {'spot': 'EURGBP Curncy', 'vol': 'EURGBPV', 'rr25': 'EURGBP25R', 'rr10': 'EURGBP10R', 'fly25': 'EURGBP25B', 'fly10': 'EURGBP10B', 'fwd': 'EURGBP'},
    'EURJPY': {'spot': 'EURJPY Curncy', 'vol': 'EURJPYV', 'rr25': 'EURJPY25R', 'rr10': 'EURJPY10R', 'fly25': 'EURJPY25B', 'fly10': 'EURJPY10B', 'fwd': 'EURJPY'},
    'GBPJPY': {'spot': 'GBPJPY Curncy', 'vol': 'GBPJPYV', 'rr25': 'GBPJPY25R', 'rr10': 'GBPJPY10R', 'fly25': 'GBPJPY25B', 'fly10': 'GBPJPY10B', 'fwd': 'GBPJPY'},
}

# Default deposit rates (override if you have live feeds)
DEFAULT_RATES = {
    'USD': 0.045, 'EUR': 0.025, 'GBP': 0.044, 'JPY': 0.005, 'CHF': 0.015,
    'AUD': 0.035, 'NZD': 0.04, 'CAD': 0.035, 'SEK': 0.03, 'NOK': 0.035,
}


def pull_bbg_surfaces(pairs, output_file='mkt_data.json'):
    """Pull vol surfaces from Bloomberg and write JSON."""
    try:
        import blpapi
    except ImportError:
        print("ERROR: blpapi not installed. Install with: pip install blpapi")
        print("       Bloomberg Terminal must also be running.\n")
        print("Generating TEMPLATE file instead (edit manually or use BQL in Excel)...\n")
        return generate_template(pairs, output_file)

    # Connect to Bloomberg
    sessionOptions = blpapi.SessionOptions()
    sessionOptions.setServerHost("localhost")
    sessionOptions.setServerPort(8194)
    session = blpapi.Session(sessionOptions)

    if not session.start():
        print("ERROR: Failed to connect to Bloomberg. Is the Terminal running?")
        return generate_template(pairs, output_file)

    if not session.openService("//blp/refdata"):
        print("ERROR: Failed to open Bloomberg reference data service.")
        session.stop()
        return generate_template(pairs, output_file)

    refDataService = session.getService("//blp/refdata")
    surfaces = {}

    for pair in pairs:
        if pair not in BBG_PAIRS:
            print(f"  SKIP: {pair} — no Bloomberg ticker mapping defined")
            continue

        cfg = BBG_PAIRS[pair]
        base_ccy = pair[:3]
        terms_ccy = pair[3:]
        print(f"  Pulling {pair}...")

        # Build ticker list
        tickers = [cfg['spot']]  # Spot
        for tn in TENORS:
            bbg_tn = tn['bbg']
            tickers.append(f"{cfg['vol']}{bbg_tn} Curncy")      # ATM vol
            tickers.append(f"{cfg['rr25']}{bbg_tn} Curncy")     # 25d RR
            tickers.append(f"{cfg['rr10']}{bbg_tn} Curncy")     # 10d RR
            tickers.append(f"{cfg['fly25']}{bbg_tn} Curncy")    # 25d Fly
            tickers.append(f"{cfg['fly10']}{bbg_tn} Curncy")    # 10d Fly (may not exist)
            # Forward points
            if len(cfg['fwd']) == 3:
                tickers.append(f"{cfg['fwd']}{bbg_tn} Curncy")
            else:
                tickers.append(f"{cfg['fwd']}{bbg_tn} Curncy")

        # Request
        request = refDataService.createRequest("ReferenceDataRequest")
        for t in tickers:
            request.getElement("securities").appendValue(t)
        request.getElement("fields").appendValue("PX_LAST")
        session.sendRequest(request)

        # Collect results
        data = {}
        while True:
            ev = session.nextEvent(5000)
            for msg in ev:
                if msg.hasElement("securityData"):
                    secArr = msg.getElement("securityData")
                    for i in range(secArr.numValues()):
                        sec = secArr.getValueAsElement(i)
                        ticker = sec.getElementAsString("security")
                        fields = sec.getElement("fieldData")
                        if fields.hasElement("PX_LAST"):
                            data[ticker] = fields.getElementAsFloat("PX_LAST")
            if ev.eventType() == blpapi.Event.RESPONSE:
                break

        # Parse into surface structure
        spot = data.get(cfg['spot'], 0)
        if spot <= 0:
            print(f"    WARNING: No spot for {pair}, skipping")
            continue

        r_d = DEFAULT_RATES.get(terms_ccy, 0.04)
        r_f = DEFAULT_RATES.get(base_ccy, 0.03)

        tenor_data = []
        for tn in TENORS:
            bbg_tn = tn['bbg']
            atm = data.get(f"{cfg['vol']}{bbg_tn} Curncy", 0)
            rr25 = data.get(f"{cfg['rr25']}{bbg_tn} Curncy", 0)
            rr10 = data.get(f"{cfg['rr10']}{bbg_tn} Curncy", 0)
            fly25 = data.get(f"{cfg['fly25']}{bbg_tn} Curncy", 0)
            fly10 = data.get(f"{cfg['fly10']}{bbg_tn} Curncy", 0)

            # Forward points
            fwd_key = f"{cfg['fwd']}{bbg_tn} Curncy"
            fwdPts = data.get(fwd_key, 0)

            # If 10d fly is missing, estimate from 25d
            if fly10 == 0 and fly25 != 0:
                fly10 = fly25 * 3.6
            # If 10d RR is missing, estimate from 25d
            if rr10 == 0 and rr25 != 0:
                rr10 = rr25 * 1.925

            if atm > 0:
                tenor_data.append({
                    'tenor': tn['label'], 'T': round(tn['T'], 6),
                    'atm': round(atm, 4), 'rr25': round(rr25, 4),
                    'rr10': round(rr10, 4), 'fly25': round(fly25, 4),
                    'fly10': round(fly10, 4), 'fwdPts': round(fwdPts, 4)
                })

        if tenor_data:
            surfaces[pair] = {
                'spot': round(spot, 6),
                'r_d': r_d,
                'r_f': r_f,
                'tenors': tenor_data
            }
            print(f"    OK: {len(tenor_data)} tenors")
        else:
            print(f"    WARNING: No vol data for {pair}")

    session.stop()

    # Write output
    output = {
        'source': 'Bloomberg BQL',
        'timestamp': datetime.now().isoformat(),
        'surfaces': surfaces
    }
    with open(output_file, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\n  -> {output_file} ({len(surfaces)} pairs)")
    return output_file


def generate_template(pairs, output_file='mkt_data.json'):
    """Generate a template JSON file that can be filled manually or via Excel BQL."""
    surfaces = {}
    for pair in pairs:
        base_ccy = pair[:3]
        terms_ccy = pair[3:]
        surfaces[pair] = {
            'spot': 0,
            'r_d': DEFAULT_RATES.get(terms_ccy, 0.04),
            'r_f': DEFAULT_RATES.get(base_ccy, 0.03),
            'tenors': [
                {'tenor': tn['label'], 'T': round(tn['T'], 6),
                 'atm': 0, 'rr25': 0, 'rr10': 0, 'fly25': 0, 'fly10': 0, 'fwdPts': 0}
                for tn in TENORS
            ]
        }

    output = {
        'source': 'template',
        'timestamp': datetime.now().isoformat(),
        'note': 'Fill in values manually or via Bloomberg Excel BQL formulas',
        'bql_example': {
            'ATM_vol': '=BQL("EURUSDV1M Curncy","PX_LAST")',
            'RR25': '=BQL("EURUSD25R1M Curncy","PX_LAST")',
            'FLY25': '=BQL("EURUSD25B1M Curncy","PX_LAST")',
            'spot': '=BQL("EURUSD Curncy","PX_LAST")',
            'fwd_pts': '=BQL("EUR1M Curncy","PX_LAST")',
        },
        'surfaces': surfaces
    }
    with open(output_file, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"  -> {output_file} (template — fill in values)")
    return output_file


if __name__ == '__main__':
    args = sys.argv[1:]
    output = 'mkt_data.json'

    # Parse args
    pairs = ['EURUSD', 'USDJPY', 'GBPUSD']
    all_g10 = False
    i = 0
    custom_pairs = []
    while i < len(args):
        if args[i] == '--output' and i+1 < len(args):
            output = args[i+1]; i += 2
        elif args[i] == '--all-g10':
            all_g10 = True; i += 1
        elif args[i] == '--template':
            generate_template(pairs if not custom_pairs else custom_pairs, output)
            sys.exit(0)
        else:
            custom_pairs.append(args[i].upper().replace('/', '')); i += 1

    if custom_pairs:
        pairs = custom_pairs
    if all_g10:
        pairs = list(BBG_PAIRS.keys())

    print(f"\n{'='*50}")
    print(f"  Bloomberg Vol Surface Loader")
    print(f"  Pairs: {', '.join(pairs)}")
    print(f"{'='*50}\n")

    pull_bbg_surfaces(pairs, output)
