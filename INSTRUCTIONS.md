# FX Options Analytics v2 — Setup & User Guide

## Overview

A self-contained FX options analytics dashboard built in Python + JavaScript for gamma richness analysis, portfolio Greek monitoring, and real-time DTCC vol surface construction.

**4 Tabs:**
1. **Market Data** — Mark or upload vol surfaces per currency pair
2. **Surface Analysis** — Interactive gamma richness heatmap with Greeks calculator
3. **Portfolio Analysis** — Analyze positions against your marked surface with drill-down
4. **DTCC Live Surface** — Real-time vol surface from DTCC trade prints via ericlanalytics.com

---

## Files

| File | Purpose |
|------|---------|
| `fx_gamma_richness.py` | Main Python script — generates the HTML dashboard |
| `fx_gamma_trading.html` | Generated dashboard (open in browser or serve) |
| `serve_dashboard.py` | Local server with DTCC API proxy (avoids CORS) |
| `fx_gamma_inputs.xlsx` | Position upload template (Pair/Strike/Expiry/Vol/Notional/Type) |
| `vol_surface_template.xlsx` | Vol surface template (one sheet per pair) |
| `load_bloomberg.py` | Bloomberg BQL vol surface loader (optional) |
| `mkt_data.json` | Vol surface data in JSON format (alternative to Excel) |

---

## Quick Start

### 1. Install Dependencies

```bash
pip install numpy pandas scipy openpyxl
```

### 2. First Run — Generate Templates

```bash
cd "C:\Users\matan\Documents\...\New folder"
python fx_gamma_richness.py
```

This creates:
- `fx_gamma_inputs.xlsx` — Positions template with sample data
- `vol_surface_template.xlsx` — Vol surface template with default EUR/JPY/GBP surfaces

### 3. Add Your Positions

Edit `fx_gamma_inputs.xlsx` → **Positions** sheet:

| Pair | Strike | Expiry | Vol | Notional | Type |
|------|--------|--------|-----|----------|------|
| EURUSD | 1.090 | 2026-06-15 | 8.5 | 10 | C |
| EURUSD | 1.075 | 2026-04-30 | 9.2 | -5 | P |
| USDJPY | 155.0 | 2026-04-18 | 9.0 | 20 | P |

- **Pair**: 6-char code (EURUSD, USDJPY, GBPUSD, etc.)
- **Strike**: Absolute strike price
- **Expiry**: YYYY-MM-DD format
- **Vol**: Implied vol in % (e.g., 8.5 = 8.5%)
- **Notional**: $M face value. Positive = long, negative = short
- **Type**: C or P

### 4. Regenerate & Serve

```bash
python fx_gamma_richness.py       # Generates fx_gamma_trading.html
python serve_dashboard.py         # Serves at http://localhost:8080
```

The browser opens automatically. For custom port: `python serve_dashboard.py --port 9090`

---

## Tab 1: Market Data

### Manual Entry
1. Click a pair tab (EURUSD, USDJPY, etc.) or click **+ Add Pair**
2. Edit Spot, Terms Rate (%), Base Rate (%)
3. Edit the vol surface table: ATM, RR25, RR10, FLY25, FLY10, FwdPts per tenor

All values in vol % (e.g., 8.5 not 0.085). Forward points in pips.

### Upload Vol Surface from Excel

Click **Upload Excel** and select a file in either format:

**Format A (multi-sheet):** Each sheet named after a pair (e.g., "EURUSD"):
```
Row 1: Spot | 1.085 | TermsRate | 4.5 | BaseRate | 2.5
Row 3: Tenor | ATM | RR25 | RR10 | FLY25 | FLY10 | FwdPts
Row 4: O/N   | 7.5  | -0.3 | -0.6 | 0.15  | 0.4   | 0.59
Row 5: 1W    | 7.8  | -0.35| -0.7 | 0.18  | 0.45  | 4.16
...
```

**Format B (single-sheet):** All pairs in one sheet with a Pair column:
```
Pair    | Tenor | Spot  | TermsRate | BaseRate | ATM | RR25 | RR10 | FLY25 | FLY10 | FwdPts
EURUSD  | O/N   | 1.085 | 4.5       | 2.5      | 7.5 | -0.3 | -0.6 | 0.15  | 0.4   | 0.59
```

Use `vol_surface_template.xlsx` as a starting point.

### Upload from JSON

Click **Load JSON** to load a `mkt_data.json` file (from Bloomberg loader or previous export).

### Export

Click **Export JSON** to save your current marks for later.

### Bloomberg Integration (Optional)

If you have Bloomberg Terminal access:

```bash
pip install blpapi
python load_bloomberg.py                    # Default pairs: EURUSD, USDJPY, GBPUSD
python load_bloomberg.py EURUSD AUDUSD      # Specific pairs
python load_bloomberg.py --all-g10          # All G10
python load_bloomberg.py --template         # Generate template only
```

This writes `mkt_data.json` which you load via the **Load JSON** button.

**Bloomberg tickers used:**
- ATM vol: `EURUSDV1M Curncy`, `EURUSDV3M Curncy`, etc.
- 25d RR: `EURUSD25R1M Curncy`
- 10d RR: `EURUSD10R1M Curncy`
- 25d Fly: `EURUSD25B1M Curncy`
- Spot: `EURUSD Curncy`
- Forward: `EUR1M Curncy` (forward points)

---

## Tab 2: Surface Analysis

Shows the gamma richness heatmap computed from your Market Data marks.

### Controls
- **Pair dropdown**: Switch between marked pairs
- **Notional ($M)**: Scale all Greeks by notional
- **Richness/Theta/Vega**: Toggle heatmap display mode

### Gamma Richness Formula
```
Cost = |Theta + Delta × T/N_Roll / Spot| / Gamma
Richness = normalized to 1–5 scale using p5/p95 percentiles
```
- **1 (blue)** = Cheap gamma (low cost to carry)
- **5 (red)** = Rich gamma (high cost to carry)

### Cheapest/Richest Tables
Shows the cheapest and richest gamma point per tenor — use for relative value.

### Greeks Calculator
Select a tenor and delta, click Calculate to see strike, vol, delta, gamma, theta, vega for that point.

---

## Tab 3: Portfolio Analysis

### Loading Positions

**From the original Excel:** Positions are loaded from `fx_gamma_inputs.xlsx` when you run `python fx_gamma_richness.py`.

**Upload dynamically:** Click **Upload Positions** to load a new Excel file at any time. Expected columns: Pair, Strike, Expiry, Vol, Notional, Type. The column headers are flexible (e.g., "strike" or "STRIKE" or "Strike" all work).

### Portfolio Heatmap
- Positions mapped to tenor × strike grid
- **Richness**: Gamma-weighted average richness per bucket
- **Vega**: Net vega per bucket
- **Decay**: Net theta per bucket
- **L/S labels**: Shows net direction per cell
- **Click any cell** to drill down into individual positions

### Inefficient Positions
- **Long (Rich Gamma)**: Your longs sorted by richness (most expensive first)
- **Short (Cheap Gamma)**: Your shorts sorted by richness

### Greeks Over Time
Projection of portfolio gamma, theta, vega, and cumulative decay as positions age and expire.

---

## Tab 4: DTCC Live Surface

### Requirements
Must run `serve_dashboard.py` for the DTCC proxy to work. Direct file opening won't connect to the API.

### How It Works
1. Fetches trade prints from `https://dtcc.ericlanalytics.com/api/optionflow`
2. For each trade: computes IV from raw premium using B76 solver (or falls back to site's IV)
3. Classifies options as OTM using strike vs forward: `strike >= fwd = call, strike < fwd = put`
4. Distributes trades to tenor pillars with weighted interpolation (O/N isolated to ≤2-day trades)
5. Computes ATM/RR/Fly shifts from your base surface
6. Applies smoothing: max-gap constraints between adjacent tenors, trade-weighted anchoring
7. Enforces RR/Fly ratios: 10d RR ≈ 1.925× 25d RR, 10d Fly ≈ 3.6× 25d Fly

### Controls
- **Pair chips**: Click to switch pair. Green underline = you have marks for this pair
- **Lookback**: How far back to pull trades (1h to 1 day)
- **Min size**: Filter by minimum notional ($5M+, $10M+, etc.)
- **Snap/Clear**: Snapshot the current surface for comparison. The heatmap shows ▲/▼ arrows for vol moves since last refresh
- **Auto checkbox**: Auto-refresh every 30 seconds
- **Vol/Chg toggle**: Show absolute vol levels or change from snapshot

### Feed Panel
- Shows individual trade prints for the selected pair
- `*` after IV = computed from premium (vs site's pre-computed IV)
- Color coding: green = call, red = put; bright = high vol or large notional

### O/N Responsiveness
O/N uses an aggressive 80/20 blend (80% observed print, 20% base mark) rather than the standard shift mechanism. A single O/N ATM print at 11% when your base is 9% will immediately show ~10.6%.

---

## Pricing Engine

### Black-76 Model
All Greeks are computed using Black-76 (forward-based Black-Scholes):

- **Forward**: `F = Spot + FwdPts/10000`
- **Delta**: Spot delta with forward/spot discount factor
- **Gamma**: Per-unit, then scaled to $M per 1% spot move
- **Theta**: Per-day, including financing cost `rP` and time decay
- **Vega**: Per 1% vol move in $K

### Smile Construction
From broker-style ATM/RR/FLY inputs:
```
25C vol = ATM + Fly25 + RR25/2
25P vol = ATM + Fly25 - RR25/2
10C vol = ATM + Fly10 + RR10/2
10P vol = ATM + Fly10 - RR10/2
```

Intermediate deltas interpolated linearly between pillars.

---

## Architecture Notes

### No External Dependencies at Runtime
The generated `fx_gamma_trading.html` is fully self-contained. All JavaScript, CSS, and data are inline. Only external resources: Plotly CDN and SheetJS CDN (for Excel upload).

### Data Flow
```
Excel (positions) → Python → HTML (with embedded JS engine)
                              ↓
                    Browser renders all tabs
                              ↓
Market Data tab edits → JS state (mktSurfaces) → Surface/Portfolio recompute
                              ↓
DTCC tab → fetch via proxy → JS processes → shifts applied to mktSurfaces base
```

### Server Proxy
`serve_dashboard.py` proxies `/api/optionflow` requests to `https://dtcc.ericlanalytics.com` to avoid CORS restrictions. The dashboard tries the proxy first, then falls back to direct.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| DTCC tab shows "No connection" | Run `serve_dashboard.py`, not just open the HTML file |
| Portfolio shows "No surface for PAIR" | Add that pair in Market Data tab first |
| Excel upload doesn't work | Check column headers match (Pair, Strike, Expiry, Vol, Notional, Type) |
| Blank heatmap | Ensure vol surface has non-zero ATM values |
| favicon.ico errors | Fixed in latest `serve_dashboard.py` |
| Bloomberg loader fails | Install `blpapi`, ensure Terminal is running |

---

## File Locations (Windows)

Default working directory:
```
C:\Users\matan\Documents\Internships, Resumes, Etc\Finance Python Projects\Python Trading Test\New folder\
```

All files should be in the same directory. Run both Python scripts from this directory.
