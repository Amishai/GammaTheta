#!/usr/bin/env python3
"""
FX Gamma Trading Dashboard

Core formula: (Theta + Delta * T/N_roll / Spot) / Gamma
Output: Normalized to 1-5 scale (lower = cheaper gamma)
"""

import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq
import json
import os

# === BLACK-SCHOLES ===

def d1(S, K, T, sigma, r_d, r_f):
    if T <= 0 or sigma <= 0: return 0.0
    return (np.log(S / K) + (r_d - r_f + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))

def d2(S, K, T, sigma, r_d, r_f):
    return d1(S, K, T, sigma, r_d, r_f) - sigma * np.sqrt(T)

def bs_delta(S, K, T, sigma, r_d, r_f, is_call):
    if T <= 0: return 0.0
    d1_val = d1(S, K, T, sigma, r_d, r_f)
    df_f = np.exp(-r_f * T)
    return df_f * norm.cdf(d1_val) if is_call else df_f * (norm.cdf(d1_val) - 1)

def bs_gamma(S, K, T, sigma, r_d, r_f):
    if T <= 0 or sigma <= 0: return 0.0
    d1_val = d1(S, K, T, sigma, r_d, r_f)
    df_f = np.exp(-r_f * T)
    return df_f * norm.pdf(d1_val) / (S * sigma * np.sqrt(T))

def bs_theta(S, K, T, sigma, r_d, r_f, is_call):
    if T <= 0: return 0.0
    d1_val, d2_val = d1(S, K, T, sigma, r_d, r_f), d2(S, K, T, sigma, r_d, r_f)
    df_d, df_f = np.exp(-r_d * T), np.exp(-r_f * T)
    term1 = -S * df_f * norm.pdf(d1_val) * sigma / (2 * np.sqrt(T))
    if is_call:
        term2 = r_f * S * df_f * norm.cdf(d1_val) - r_d * K * df_d * norm.cdf(d2_val)
    else:
        term2 = -r_f * S * df_f * norm.cdf(-d1_val) + r_d * K * df_d * norm.cdf(-d2_val)
    return (term1 + term2) / 365

def bs_vega(S, K, T, sigma, r_d, r_f):
    if T <= 0: return 0.0
    d1_val = d1(S, K, T, sigma, r_d, r_f)
    df_f = np.exp(-r_f * T)
    return S * df_f * norm.pdf(d1_val) * np.sqrt(T) * 0.01

# === STRIKE FROM DELTA ===

def strike_from_delta_call(F, T, sigma, target_delta, df_f):
    def obj(K):
        d1_val = (np.log(F / K) + 0.5 * sigma**2 * T) / (sigma * np.sqrt(T))
        return df_f * norm.cdf(d1_val) - target_delta
    try: return brentq(obj, F * 0.5, F * 2.0)
    except: return F

def strike_from_delta_put(F, T, sigma, target_delta, df_f):
    def obj(K):
        d1_val = (np.log(F / K) + 0.5 * sigma**2 * T) / (sigma * np.sqrt(T))
        return df_f * (norm.cdf(d1_val) - 1) - target_delta
    try: return brentq(obj, F * 0.5, F * 2.0)
    except: return F

def atm_dns_strike(F, T, sigma):
    return F * np.exp(0.5 * sigma**2 * T)

# === SMILE ===

def solve_smile(atm_vol, rr25, fly25, rr10, fly10):
    atm = atm_vol / 100
    return {
        'ATM': atm,
        '25C': atm + fly25/100 + 0.5 * rr25/100,
        '25P': atm + fly25/100 - 0.5 * rr25/100,
        '10C': atm + fly10/100 + 0.5 * rr10/100,
        '10P': atm + fly10/100 - 0.5 * rr10/100
    }

def interp_vol(smile, delta, is_call):
    if delta == 0: return smile['ATM']
    ad = abs(delta)
    if is_call:
        if ad <= 0.10: return smile['10C']
        elif ad <= 0.25: return smile['10C'] + (ad - 0.10) / 0.15 * (smile['25C'] - smile['10C'])
        else: return smile['25C'] + (ad - 0.25) / 0.25 * (smile['ATM'] - smile['25C'])
    else:
        if ad <= 0.10: return smile['10P']
        elif ad <= 0.25: return smile['10P'] + (ad - 0.10) / 0.15 * (smile['25P'] - smile['10P'])
        else: return smile['25P'] + (ad - 0.25) / 0.25 * (smile['ATM'] - smile['25P'])

# === EXCEL I/O ===

def create_template(fp):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    
    wb = Workbook()
    hf, hfill = Font(bold=True, color='FFFFFF'), PatternFill('solid', fgColor='1F4E79')
    inf, infill = Font(color='0000FF'), PatternFill('solid', fgColor='D6EAF8')
    bord = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
    
    ws = wb.active
    ws.title = 'Parameters'
    for i, h in enumerate(['Parameter', 'Value', 'Description'], 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font, c.fill, c.border = hf, hfill, bord
    
    for i, (n, v, d) in enumerate([
        ('Spot', 1.0850, 'Spot rate'),
        ('TermsRate', 0.045, 'Terms/quote currency rate'),
        ('BaseRate', 0.025, 'Base currency rate'),
        ('TN_Roll_Pips', -0.55, 'T/N roll in pips')
    ], 2):
        ws.cell(row=i, column=1, value=n).border = bord
        c = ws.cell(row=i, column=2, value=v)
        c.font, c.fill, c.border = inf, infill, bord
        ws.cell(row=i, column=3, value=d).border = bord
    ws.column_dimensions['A'].width, ws.column_dimensions['B'].width, ws.column_dimensions['C'].width = 15, 12, 35
    
    ws2 = wb.create_sheet('VolSurface')
    for i, h in enumerate(['Tenor', 'T_Years', 'ATM', 'RR25', 'RR10', 'FLY25', 'FLY10'], 1):
        c = ws2.cell(row=1, column=i, value=h)
        c.font, c.fill, c.border = hf, hfill, bord
    
    data = [
        ('O/N', 1/365, 7.50, -0.30, -0.60, 0.15, 0.40),
        ('1W', 7/365, 7.80, -0.35, -0.70, 0.18, 0.45),
        ('2W', 14/365, 8.00, -0.40, -0.80, 0.20, 0.50),
        ('1M', 1/12, 8.20, -0.50, -1.00, 0.25, 0.60),
        ('2M', 2/12, 8.50, -0.60, -1.20, 0.30, 0.70),
        ('3M', 3/12, 8.80, -0.70, -1.40, 0.35, 0.80),
        ('6M', 6/12, 9.20, -0.80, -1.60, 0.40, 0.95),
        ('9M', 9/12, 9.50, -0.85, -1.70, 0.42, 1.05),
        ('1Y', 1.0, 9.80, -0.90, -1.80, 0.45, 1.15),
        ('2Y', 2.0, 10.20, -1.00, -2.00, 0.50, 1.30),
    ]
    for ri, row in enumerate(data, 2):
        for ci, v in enumerate(row, 1):
            c = ws2.cell(row=ri, column=ci, value=v)
            c.border = bord
            if ci >= 3: c.font, c.fill = inf, infill
    for i, w in enumerate([8, 10, 10, 10, 10, 10, 10], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    
    wb.save(fp)

def load_data(fp):
    p = pd.read_excel(fp, sheet_name='Parameters')
    params = dict(zip(p['Parameter'], p['Value']))
    v = pd.read_excel(fp, sheet_name='VolSurface')
    v.columns = ['tenor', 'T_years', 'atm', 'rr25', 'rr10', 'fly25', 'fly10']
    return {'spot': params['Spot'], 'r_terms': params['TermsRate'], 'r_base': params['BaseRate'], 
            'tn_roll': params['TN_Roll_Pips'], 'vol_surface': v}

# === BUILD SURFACE ===

def build_surface(mkt):
    S, r_d, r_f, tn = mkt['spot'], mkt['r_terms'], mkt['r_base'], mkt['tn_roll']
    notional = 1_000_000
    
    deltas = [('10P', -0.10, False), ('15P', -0.15, False), ('20P', -0.20, False), ('25P', -0.25, False),
              ('30P', -0.30, False), ('35P', -0.35, False), ('40P', -0.40, False), ('45P', -0.45, False),
              ('ATM', 0.0, None),
              ('45C', 0.45, True), ('40C', 0.40, True), ('35C', 0.35, True), ('30C', 0.30, True),
              ('25C', 0.25, True), ('20C', 0.20, True), ('15C', 0.15, True), ('10C', 0.10, True)]
    
    rows = []
    fwd_points_list = []
    
    for _, r in mkt['vol_surface'].iterrows():
        T = r['T_years']
        F = S * np.exp((r_d - r_f) * T)
        df_f = np.exp(-r_f * T)
        smile = solve_smile(r['atm'], r['rr25'], r['fly25'], r['rr10'], r['fly10'])
        
        # Calculate forward points for this tenor
        fwd_pts = (F - S) * 10000  # in pips
        days = max(1, round(T * 365))  # At least 1 day for O/N
        fwd_points_list.append({'tenor': r['tenor'], 'T_years': T, 'days': days, 'forward': F, 'fwd_points': fwd_pts})
        
        for dname, dval, is_call in deltas:
            if dval == 0:
                sigma = smile['ATM']
                K = atm_dns_strike(F, T, sigma)
                theta = (bs_theta(S, K, T, sigma, r_d, r_f, True) + bs_theta(S, K, T, sigma, r_d, r_f, False)) / 2
                delta = 0.5
                is_call_flag = True
            else:
                sigma = interp_vol(smile, dval, is_call)
                K = strike_from_delta_call(F, T, sigma, abs(dval), df_f) if is_call else strike_from_delta_put(F, T, sigma, dval, df_f)
                theta = bs_theta(S, K, T, sigma, r_d, r_f, is_call)
                delta = bs_delta(S, K, T, sigma, r_d, r_f, is_call)
                is_call_flag = is_call
            
            gamma = bs_gamma(S, K, T, sigma, r_d, r_f)
            vega = bs_vega(S, K, T, sigma, r_d, r_f)
            
            # Gamma cost: (Theta + Delta * TN_roll / Spot) / Gamma
            roll = delta * (tn / 10000) / S
            cost = abs(theta + roll) / gamma if gamma > 1e-12 else 0
            
            # Calculate total decay over life of trade
            theta_daily = theta * notional
            total_decay = theta_daily * days
            
            rows.append({
                'tenor': r['tenor'], 'T_years': T, 'days': days, 'delta_label': dname, 'delta_val': dval,
                'strike': K, 'vol': sigma * 100, 'delta': delta,
                'gamma': gamma * S * 0.01 * notional / 1_000_000,  # in millions
                'theta': theta * notional / 1000,  # in thousands (daily)
                'total_decay': total_decay / 1000,  # in thousands (total over life)
                'vega': vega * notional / 1000,  # in thousands
                'gamma_cost_raw': cost
            })
    
    df = pd.DataFrame(rows)
    raw = df['gamma_cost_raw'].values
    p5, p95 = np.percentile(raw[raw > 0], 5), np.percentile(raw[raw > 0], 95)
    df['richness'] = df['gamma_cost_raw'].apply(lambda x: max(1, min(5, 1 + (x - p5) / (p95 - p5) * 4)) if x > 0 else 1)
    
    fwd_df = pd.DataFrame(fwd_points_list)
    return df, fwd_df

# === HTML DASHBOARD ===

def create_dashboard(df, fwd_df, mkt, output='fx_gamma_trading.html'):
    tenors = df['tenor'].unique().tolist()
    deltas = ['10P', '15P', '20P', '25P', '30P', '35P', '40P', '45P', 'ATM', '45C', '40C', '35C', '30C', '25C', '20C', '15C', '10C']
    dlabels = ['10Δ P', '15Δ P', '20Δ P', '25Δ P', '30Δ P', '35Δ P', '40Δ P', '45Δ P', 'ATM', '45Δ C', '40Δ C', '35Δ C', '30Δ C', '25Δ C', '20Δ C', '15Δ C', '10Δ C']
    
    # Generate matrices for each Greek
    matrix_gamma = [[float(df[(df['tenor']==t) & (df['delta_label']==d)]['richness'].values[0]) 
               for d in deltas] for t in tenors]
    matrix_theta = [[float(df[(df['tenor']==t) & (df['delta_label']==d)]['theta'].values[0]) 
               for d in deltas] for t in tenors]
    matrix_vega = [[float(df[(df['tenor']==t) & (df['delta_label']==d)]['vega'].values[0]) 
               for d in deltas] for t in tenors]
    
    sdata = df.to_dict('records')
    fwd_data = fwd_df.to_dict('records')
    
    # Get cheapest and richest per tenor for each Greek
    tenor_order = fwd_df.sort_values('T_years')['tenor'].tolist()
    
    # For Gamma (use richness - lower = cheaper gamma)
    cheapest_gamma = []
    richest_gamma = []
    for t in tenor_order:
        t_df = df[df['tenor'] == t]
        cheapest = t_df.loc[t_df['richness'].idxmin()]
        richest = t_df.loc[t_df['richness'].idxmax()]
        cheapest_gamma.append({
            'tenor': t, 'delta': cheapest['delta_label'], 'score': cheapest['richness'],
            'theta': cheapest['theta'], 'gamma': cheapest['gamma'], 'vega': cheapest['vega'],
            'total_decay': cheapest['total_decay']
        })
        richest_gamma.append({
            'tenor': t, 'delta': richest['delta_label'], 'score': richest['richness'],
            'theta': richest['theta'], 'gamma': richest['gamma'], 'vega': richest['vega'],
            'total_decay': richest['total_decay']
        })
    
    # Forward points table HTML
    fwd_table = '<table><tr><th>Tenor</th><th>Days</th><th>Forward</th><th>Fwd Pts</th></tr>'
    for f in fwd_data:
        fwd_table += f'<tr><td>{f["tenor"]}</td><td>{f["days"]}</td><td>{f["forward"]:.5f}</td><td>{f["fwd_points"]:.2f}</td></tr>'
    fwd_table += '</table>'
    
    html = f'''<!DOCTYPE html>
<html><head><title>FX Gamma Trading</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
*{{box-sizing:border-box}}body{{font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:15px;background:#2d2d2d;color:#e0e0e0}}
h1{{text-align:center;color:#90caf9;margin:10px 0 20px}}h2{{color:#90caf9;margin:0 0 15px;font-size:18px}}h3{{color:#90caf9;margin:15px 0 10px;font-size:14px}}
.grid{{display:grid;grid-template-columns:1fr 420px;gap:15px;max-width:1600px;margin:0 auto}}
.card{{background:#3d3d3d;padding:15px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.3)}}
.market-bar{{display:flex;gap:20px;justify-content:center;margin-bottom:15px;padding:10px;background:#3d3d3d;border-radius:8px}}
.market-item{{text-align:center}}.market-item .label{{font-size:11px;color:#aaa}}.market-item .value{{font-size:16px;font-weight:bold;color:#90caf9}}
.legend{{display:flex;justify-content:center;gap:20px;margin:10px 0;font-size:12px}}
.legend-item{{display:flex;align-items:center;gap:5px}}.legend-color{{width:16px;height:16px;border-radius:3px}}
table{{width:100%;border-collapse:collapse;font-size:11px;margin-bottom:10px}}th,td{{padding:5px 6px;text-align:right;border:1px solid #555}}
th{{background:#1F4E79;color:white}}td:first-child{{text-align:left;font-weight:600}}.cheap{{background:#1a3a4a}}.rich{{background:#4a2a2a}}
.btn{{padding:8px 16px;border:none;border-radius:4px;cursor:pointer;font-weight:600;margin-right:8px}}
.btn-primary{{background:#1F4E79;color:white}}
.btn-toggle{{padding:6px 12px;border:1px solid #555;border-radius:4px;cursor:pointer;font-weight:600;margin-right:4px;background:#2d2d2d;color:#e0e0e0}}
.btn-toggle.active{{background:#1F4E79;color:white;border-color:#1F4E79}}
.greeks-result{{background:#2d2d2d;padding:12px;border-radius:6px;margin-top:10px}}
.greeks-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;text-align:center}}
.greek-box{{padding:8px;background:#3d3d3d;border-radius:4px}}.greek-label{{font-size:10px;color:#aaa}}
.greek-value{{font-size:14px;font-weight:bold;color:#90caf9}}.greek-value.positive{{color:#4caf50}}.greek-value.negative{{color:#f44336}}
.help{{font-size:11px;color:#aaa;margin-top:8px}}
.unit{{font-size:9px;color:#888}}
select{{padding:6px;background:#3d3d3d;color:#e0e0e0;border:1px solid #555;border-radius:4px}}
.toggle-bar{{margin-bottom:15px;display:flex;align-items:center;gap:10px}}
.toggle-label{{font-size:12px;color:#aaa}}
.notional-input{{width:80px;padding:6px;background:#2d2d2d;color:#90caf9;border:1px solid #555;border-radius:4px;font-size:14px;font-weight:bold;text-align:center}}
</style></head><body>
<h1>FX Gamma Trading Dashboard</h1>
<div class="market-bar">
<div class="market-item"><div class="label">Spot</div><div class="value">{mkt['spot']:.4f}</div></div>
<div class="market-item"><div class="label">Terms Rate</div><div class="value">{mkt['r_terms']*100:.2f}%</div></div>
<div class="market-item"><div class="label">Base Rate</div><div class="value">{mkt['r_base']*100:.2f}%</div></div>
<div class="market-item"><div class="label">T/N Roll</div><div class="value">{mkt['tn_roll']:.2f} pips</div></div>
<div class="market-item"><div class="label">Notional ($M)</div><input type="number" id="notional" class="notional-input" value="1" min="0.1" step="0.1" onchange="updateNotional()"></div>
</div>
<div class="grid"><div>
<div class="card">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
<h2 style="margin:0" id="heatmap-title">Gamma Richness Surface</h2>
<div>
<button class="btn-toggle active" onclick="setHeatmapView('gamma')" id="hm-btn-gamma">Gamma</button>
<button class="btn-toggle" onclick="setHeatmapView('theta')" id="hm-btn-theta">Theta</button>
<button class="btn-toggle" onclick="setHeatmapView('vega')" id="hm-btn-vega">Vega</button>
</div>
</div>
<div id="legend-gamma" class="legend">
<div class="legend-item"><div class="legend-color" style="background:#0d47a1"></div><span>1 = Cheap</span></div>
<div class="legend-item"><div class="legend-color" style="background:#fff59d"></div><span>3 = Fair</span></div>
<div class="legend-item"><div class="legend-color" style="background:#b71c1c"></div><span>5 = Rich</span></div>
</div>
<div id="legend-theta" class="legend" style="display:none">
<div class="legend-item"><div class="legend-color" style="background:#4caf50"></div><span>Low Decay</span></div>
<div class="legend-item"><div class="legend-color" style="background:#f44336"></div><span>High Decay</span></div>
</div>
<div id="legend-vega" class="legend" style="display:none">
<div class="legend-item"><div class="legend-color" style="background:#0d47a1"></div><span>Low Vega</span></div>
<div class="legend-item"><div class="legend-color" style="background:#b71c1c"></div><span>High Vega</span></div>
</div>
<div id="heatmap"></div>
<div class="help">Units: Gamma/Delta in $M, Theta/Vega in $K</div></div>

<div class="card" style="margin-top:15px"><h2>Richness by Tenor</h2>
<h3>Cheapest Gamma per Tenor (Buy)</h3>
<div id="cheap-table"></div>
<h3>Richest Gamma per Tenor (Sell)</h3>
<div id="rich-table"></div>
<div class="help">Total Decay = Daily Theta x Days to Expiry</div>
</div></div>

<div>
<div class="card"><h2>Forward Points</h2>
{fwd_table}
<div class="help">Fwd Pts in pips (terms currency)</div></div>

<div class="card" style="margin-top:15px"><h2>Greeks Calculator</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
<div><label style="font-size:12px">Tenor</label><select id="calc-t" style="width:100%"></select></div>
<div><label style="font-size:12px">Delta</label><select id="calc-d" style="width:100%"></select></div>
</div><button class="btn btn-primary" onclick="calcGreeks()" style="margin-top:10px">Calculate</button>
<div id="calc-result"></div></div>
</div></div>

<script>
var S={json.dumps(sdata)},T={json.dumps(tenors)},D={json.dumps(deltas)},DL={json.dumps(dlabels)};
var M_gamma={json.dumps(matrix_gamma)};
var M_theta={json.dumps(matrix_theta)};
var M_vega={json.dumps(matrix_vega)};

var cs_gamma=[[0,'#0d47a1'],[.125,'#1976d2'],[.25,'#42a5f5'],[.375,'#90caf9'],[.5,'#fff59d'],[.625,'#ffcc80'],[.75,'#ff9800'],[.875,'#e65100'],[1,'#b71c1c']];
var cs_theta=[[0,'#4caf50'],[.5,'#fff59d'],[1,'#f44336']];
var cs_vega=[[0,'#0d47a1'],[.5,'#fff59d'],[1,'#b71c1c']];

var greekData = {{
    gamma: {{
        cheap: {json.dumps(cheapest_gamma)},
        rich: {json.dumps(richest_gamma)}
    }}
}};

var currentHeatmap = 'gamma';

function getNotional() {{
    return parseFloat(document.getElementById('notional').value) || 1;
}}

function updateNotional() {{
    renderTables();
    renderHeatmap();
    // Re-calculate Greeks if result is showing
    if(document.getElementById('calc-result').innerHTML) calcGreeks();
}}

function setHeatmapView(greek) {{
    currentHeatmap = greek;
    ['gamma','theta','vega'].forEach(g => {{
        document.getElementById('hm-btn-'+g).className = 'btn-toggle' + (g===greek?' active':'');
        document.getElementById('legend-'+g).style.display = (g===greek?'flex':'none');
    }});
    var titles = {{gamma:'Gamma Richness Surface',theta:'Theta Surface ($K/day)',vega:'Vega Surface ($K)'}};
    document.getElementById('heatmap-title').textContent = titles[greek];
    renderHeatmap();
}}

function renderHeatmap() {{
    var n = getNotional();
    var M, cs, zmin, zmax;
    if(currentHeatmap==='gamma') {{
        M=M_gamma; cs=cs_gamma; zmin=1; zmax=5;
    }} else if(currentHeatmap==='theta') {{
        M=M_theta.map(r=>r.map(v=>v*n)); cs=cs_theta;
        var flat=M.flat();
        zmin=Math.min(...flat); zmax=Math.max(...flat);
    }} else {{
        M=M_vega.map(r=>r.map(v=>v*n)); cs=cs_vega;
        var flat=M.flat();
        zmin=Math.min(...flat); zmax=Math.max(...flat);
    }}
    var txt=M.map(r=>r.map(v=>v.toFixed(2)));
    Plotly.react('heatmap',[{{z:M,x:DL,y:T,type:'heatmap',colorscale:cs,text:txt,texttemplate:'%{{text}}',textfont:{{size:10,color:'black'}},
    colorbar:{{title:{{text:currentHeatmap==='gamma'?'Score':'$K',font:{{color:'#e0e0e0'}}}},tickfont:{{color:'#e0e0e0'}},len:.9}},zmin:zmin,zmax:zmax}}],
    {{margin:{{t:20,b:80,l:60,r:50}},xaxis:{{title:'Delta',tickangle:45,color:'#e0e0e0'}},yaxis:{{title:'Tenor',color:'#e0e0e0'}},paper_bgcolor:'#3d3d3d',plot_bgcolor:'#3d3d3d'}});
}}

function renderTables() {{
    var data = greekData.gamma;
    var n = getNotional();
    
    var cheapHtml = '<table><tr><th>Tenor</th><th>Delta</th><th>Score</th><th>Theta ($K)</th><th>Gamma ($M)</th><th>Total Decay ($K)</th></tr>';
    data.cheap.forEach(r => {{
        cheapHtml += '<tr class="cheap"><td>'+r.tenor+'</td><td>'+r.delta+'</td><td>'+r.score.toFixed(2)+'</td><td>'+(r.theta*n).toFixed(2)+'</td><td>'+(r.gamma*n).toFixed(4)+'</td><td>'+(r.total_decay*n).toFixed(1)+'</td></tr>';
    }});
    cheapHtml += '</table>';
    document.getElementById('cheap-table').innerHTML = cheapHtml;
    
    var richHtml = '<table><tr><th>Tenor</th><th>Delta</th><th>Score</th><th>Theta ($K)</th><th>Gamma ($M)</th><th>Total Decay ($K)</th></tr>';
    data.rich.forEach(r => {{
        richHtml += '<tr class="rich"><td>'+r.tenor+'</td><td>'+r.delta+'</td><td>'+r.score.toFixed(2)+'</td><td>'+(r.theta*n).toFixed(2)+'</td><td>'+(r.gamma*n).toFixed(4)+'</td><td>'+(r.total_decay*n).toFixed(1)+'</td></tr>';
    }});
    richHtml += '</table>';
    document.getElementById('rich-table').innerHTML = richHtml;
}}

function init(){{
    ['calc-t'].forEach(id=>{{var s=document.getElementById(id);T.forEach(t=>s.innerHTML+='<option>'+t+'</option>')}});
    ['calc-d'].forEach(id=>{{var s=document.getElementById(id);D.forEach((d,i)=>s.innerHTML+='<option value="'+d+'">'+DL[i]+'</option>')}});
    renderTables();
    renderHeatmap();
}};

function getP(t,d){{return S.find(x=>x.tenor===t&&x.delta_label===d)}};

function calcGreeks(){{
    var p=getP(document.getElementById('calc-t').value,document.getElementById('calc-d').value);
    if(!p)return;
    var n = getNotional();
    var days=p.days||1;
    var totalDecay=(p.total_decay||0)*n;
    var gamma=p.gamma*n;
    var theta=p.theta*n;
    var vega=p.vega*n;
    document.getElementById('calc-result').innerHTML='<div class="greeks-result">'+
    '<div style="margin-bottom:10px"><strong>'+p.tenor+' '+p.delta_label+'</strong> | K: '+p.strike.toFixed(4)+' | Vol: '+p.vol.toFixed(2)+'% | Days: '+days+' | Notional: $'+n.toFixed(1)+'M</div>'+
    '<div class="greeks-grid">'+
    '<div class="greek-box"><div class="greek-label">Delta</div><div class="greek-value">'+p.delta.toFixed(3)+'</div></div>'+
    '<div class="greek-box"><div class="greek-label">Gamma <span class="unit">($M)</span></div><div class="greek-value">'+gamma.toFixed(4)+'</div></div>'+
    '<div class="greek-box"><div class="greek-label">Theta/Day <span class="unit">($K)</span></div><div class="greek-value '+(theta>=0?'positive':'negative')+'">'+theta.toFixed(2)+'</div></div>'+
    '<div class="greek-box"><div class="greek-label">Vega <span class="unit">($K)</span></div><div class="greek-value">'+vega.toFixed(2)+'</div></div>'+
    '</div>'+
    '<div style="margin-top:12px;padding:10px;background:#2a3f4f;border-radius:6px;border-left:4px solid #90caf9">'+
    '<div class="greeks-grid" style="grid-template-columns:1fr 1fr">'+
    '<div class="greek-box"><div class="greek-label">Total Decay Over Life <span class="unit">($K)</span></div><div class="greek-value '+(totalDecay>=0?'positive':'negative')+'">'+totalDecay.toFixed(2)+'</div></div>'+
    '<div class="greek-box"><div class="greek-label">Gamma Richness</div><div class="greek-value" style="color:#90caf9">'+p.richness.toFixed(2)+'</div></div>'+
    '</div></div></div>';
}};

init();
</script></body></html>'''
    
    with open(output, 'w') as f: f.write(html)
    return output

# === MAIN ===

def main():
    fp = 'fx_gamma_inputs.xlsx'
    print("\n" + "="*50 + "\n   FX GAMMA TRADING DASHBOARD\n" + "="*50 + "\n")
    
    if not os.path.exists(fp):
        print(f"Creating template: {fp}")
        create_template(fp)
        print("\n  -> Edit Excel file, then run again.\n")
        return
    
    print(f"Loading: {fp}")
    mkt = load_data(fp)
    print(f"  Spot: {mkt['spot']:.4f} | Terms: {mkt['r_terms']*100:.2f}% | Base: {mkt['r_base']*100:.2f}% | Roll: {mkt['tn_roll']:.2f} pips")
    
    print("\nBuilding surface...")
    df, fwd_df = build_surface(mkt)
    
    print("\nForward Points:")
    print(fwd_df[['tenor', 'days', 'forward', 'fwd_points']].to_string(index=False))
    
    print("\nGamma Richness (1=Cheap, 5=Rich):")
    piv = df.pivot(index='tenor', columns='delta_label', values='richness')[['10P','25P','ATM','25C','10C']]
    print(piv.round(2).to_string())
    
    out = create_dashboard(df, fwd_df, mkt)
    print(f"\n  -> Dashboard: {out}")
    df.to_csv('fx_gamma_surface.csv', index=False)
    print(f"  -> CSV: fx_gamma_surface.csv\n\nComplete!\n")

if __name__ == '__main__':
    main()
