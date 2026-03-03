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
    
    deltas = [('10P', -0.10, False), ('15P', -0.15, False), ('25P', -0.25, False), ('35P', -0.35, False),
              ('ATM', 0.0, None), ('35C', 0.35, True), ('25C', 0.25, True), ('15C', 0.15, True), ('10C', 0.10, True)]
    
    rows = []
    for _, r in mkt['vol_surface'].iterrows():
        T = r['T_years']
        F = S * np.exp((r_d - r_f) * T)
        df_f = np.exp(-r_f * T)
        smile = solve_smile(r['atm'], r['rr25'], r['fly25'], r['rr10'], r['fly10'])
        
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
            
            rows.append({
                'tenor': r['tenor'], 'T_years': T, 'delta_label': dname, 'delta_val': dval,
                'strike': K, 'vol': sigma * 100, 'delta': delta,
                'gamma': gamma * S * 0.01 * notional, 'theta': theta * notional,
                'vega': vega * notional, 'gamma_cost_raw': cost
            })
    
    df = pd.DataFrame(rows)
    raw = df['gamma_cost_raw'].values
    p5, p95 = np.percentile(raw[raw > 0], 5), np.percentile(raw[raw > 0], 95)
    df['richness'] = df['gamma_cost_raw'].apply(lambda x: max(1, min(5, 1 + (x - p5) / (p95 - p5) * 4)) if x > 0 else 1)
    return df

# === HTML DASHBOARD ===

def create_dashboard(df, mkt, output='fx_gamma_trading.html'):
    tenors = df['tenor'].unique().tolist()
    deltas = ['10P', '15P', '25P', '35P', 'ATM', '35C', '25C', '15C', '10C']
    dlabels = ['10Δ Put', '15Δ Put', '25Δ Put', '35Δ Put', 'ATM', '35Δ Call', '25Δ Call', '15Δ Call', '10Δ Call']
    
    matrix = [[float(df[(df['tenor']==t) & (df['delta_label']==d)]['richness'].values[0]) 
               for d in deltas] for t in tenors]
    
    sdata = df.to_dict('records')
    cheap = df.nsmallest(5, 'richness')[['tenor','delta_label','richness','theta','gamma','vega']].to_dict('records')
    rich = df.nlargest(5, 'richness')[['tenor','delta_label','richness','theta','gamma','vega']].to_dict('records')
    
    html = f'''<!DOCTYPE html>
<html><head><title>FX Gamma Trading</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
*{{box-sizing:border-box}}body{{font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:15px;background:#f0f2f5}}
h1{{text-align:center;color:#1F4E79;margin:10px 0 20px}}h2{{color:#1F4E79;margin:0 0 15px;font-size:18px}}
.grid{{display:grid;grid-template-columns:1fr 380px;gap:15px;max-width:1600px;margin:0 auto}}
.card{{background:white;padding:15px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}
.market-bar{{display:flex;gap:20px;justify-content:center;margin-bottom:15px;padding:10px;background:white;border-radius:8px}}
.market-item{{text-align:center}}.market-item .label{{font-size:11px;color:#666}}.market-item .value{{font-size:16px;font-weight:bold;color:#1F4E79}}
.legend{{display:flex;justify-content:center;gap:20px;margin:10px 0;font-size:12px}}
.legend-item{{display:flex;align-items:center;gap:5px}}.legend-color{{width:16px;height:16px;border-radius:3px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:6px 8px;text-align:right;border:1px solid #ddd}}
th{{background:#1F4E79;color:white}}td:first-child{{text-align:left;font-weight:600}}.cheap{{background:#e3f2fd}}.rich{{background:#ffebee}}
.leg{{display:grid;grid-template-columns:80px 100px 100px 60px 40px;gap:8px;align-items:center;margin-bottom:8px}}
.leg select,.leg input{{padding:6px;border:1px solid #ddd;border-radius:4px;font-size:12px}}
.leg button{{padding:6px;background:#dc3545;color:white;border:none;border-radius:4px;cursor:pointer}}
.btn{{padding:8px 16px;border:none;border-radius:4px;cursor:pointer;font-weight:600;margin-right:8px}}
.btn-primary{{background:#1F4E79;color:white}}.btn-success{{background:#28a745;color:white}}.btn-secondary{{background:#6c757d;color:white}}
.greeks-result{{background:#f8f9fa;padding:12px;border-radius:6px;margin-top:10px}}
.greeks-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;text-align:center}}
.greek-box{{padding:8px;background:white;border-radius:4px}}.greek-label{{font-size:11px;color:#666}}
.greek-value{{font-size:16px;font-weight:bold;color:#1F4E79}}.greek-value.positive{{color:#28a745}}.greek-value.negative{{color:#dc3545}}
.compare-select{{display:grid;grid-template-columns:1fr 1fr;gap:15px}}
.compare-box{{padding:10px;background:#f8f9fa;border-radius:6px}}.compare-box h4{{margin:0 0 8px;font-size:13px}}
.compare-box select{{width:100%;padding:6px;margin-bottom:5px}}
.spread-result{{margin-top:15px;padding:12px;background:#e8f4fd;border-radius:6px;border-left:4px solid #1F4E79}}
.help{{font-size:11px;color:#666;margin-top:8px}}
</style></head><body>
<h1>🎯 FX Gamma Trading Dashboard</h1>
<div class="market-bar">
<div class="market-item"><div class="label">Spot</div><div class="value">{mkt['spot']:.4f}</div></div>
<div class="market-item"><div class="label">Terms Rate</div><div class="value">{mkt['r_terms']*100:.2f}%</div></div>
<div class="market-item"><div class="label">Base Rate</div><div class="value">{mkt['r_base']*100:.2f}%</div></div>
<div class="market-item"><div class="label">T/N Roll</div><div class="value">{mkt['tn_roll']:.2f} pips</div></div>
</div>
<div class="grid"><div>
<div class="card"><h2>Gamma Richness Surface</h2>
<div class="legend">
<div class="legend-item"><div class="legend-color" style="background:#0d47a1"></div><span>1 = Cheap</span></div>
<div class="legend-item"><div class="legend-color" style="background:#fff59d"></div><span>3 = Fair</span></div>
<div class="legend-item"><div class="legend-color" style="background:#b71c1c"></div><span>5 = Rich</span></div>
</div><div id="heatmap"></div>
<div class="help">Click cell to add to trade builder. Formula: (Θ + Δ×Roll/S) / Γ</div></div>
<div class="card" style="margin-top:15px"><h2>📊 Spread Analyzer</h2>
<div class="compare-select">
<div class="compare-box"><h4>Leg 1 (Buy)</h4><select id="c-t1"></select><select id="c-d1"></select></div>
<div class="compare-box"><h4>Leg 2 (Sell)</h4><select id="c-t2"></select><select id="c-d2"></select></div>
</div><button class="btn btn-primary" onclick="compare()" style="margin-top:10px">Compare</button>
<div id="compare-result"></div></div></div>
<div>
<div class="card"><h2>🏆 Trade Ideas</h2>
<h3 style="color:#2c5282;margin:15px 0 10px;font-size:14px">Cheapest Gamma (Buy)</h3>
<table><tr><th>Tenor</th><th>Delta</th><th>Score</th><th>Theta</th><th>Gamma</th></tr>
{"".join(f'<tr class="cheap"><td>{r["tenor"]}</td><td>{r["delta_label"]}</td><td>{r["richness"]:.2f}</td><td>${r["theta"]:,.0f}</td><td>${r["gamma"]:,.0f}</td></tr>' for r in cheap)}</table>
<h3 style="color:#2c5282;margin:15px 0 10px;font-size:14px">Richest Gamma (Sell)</h3>
<table><tr><th>Tenor</th><th>Delta</th><th>Score</th><th>Theta</th><th>Gamma</th></tr>
{"".join(f'<tr class="rich"><td>{r["tenor"]}</td><td>{r["delta_label"]}</td><td>{r["richness"]:.2f}</td><td>${r["theta"]:,.0f}</td><td>${r["gamma"]:,.0f}</td></tr>' for r in rich)}</table></div>
<div class="card" style="margin-top:15px"><h2>🔧 Trade Builder</h2>
<div id="legs"></div>
<button class="btn btn-success" onclick="addLeg()">+ Add Leg</button>
<button class="btn btn-primary" onclick="calcTrade()">Calculate</button>
<button class="btn btn-secondary" onclick="clearLegs()">Clear</button>
<div id="trade-result"></div></div>
<div class="card" style="margin-top:15px"><h2>🧮 Greeks Calculator</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
<div><label style="font-size:12px">Tenor</label><select id="calc-t" style="width:100%;padding:6px"></select></div>
<div><label style="font-size:12px">Delta</label><select id="calc-d" style="width:100%;padding:6px"></select></div>
</div><button class="btn btn-primary" onclick="calcGreeks()" style="margin-top:10px">Calculate</button>
<div id="calc-result"></div></div></div></div>
<script>
var S={json.dumps(sdata)},T={json.dumps(tenors)},D={json.dumps(deltas)},DL={json.dumps(dlabels)},M={json.dumps(matrix)},legs=[];
var cs=[[0,'#0d47a1'],[.125,'#1976d2'],[.25,'#42a5f5'],[.375,'#90caf9'],[.5,'#fff59d'],[.625,'#ffcc80'],[.75,'#ff9800'],[.875,'#e65100'],[1,'#b71c1c']];
function init(){{['c-t1','c-t2','calc-t'].forEach(id=>{{var s=document.getElementById(id);T.forEach(t=>s.innerHTML+='<option>'+t+'</option>')}});
['c-d1','c-d2','calc-d'].forEach(id=>{{var s=document.getElementById(id);D.forEach((d,i)=>s.innerHTML+='<option value="'+d+'">'+DL[i]+'</option>')}})}};
function heatmap(){{var txt=M.map(r=>r.map(v=>v.toFixed(2)));
Plotly.newPlot('heatmap',[{{z:M,x:DL,y:T,type:'heatmap',colorscale:cs,text:txt,texttemplate:'%{{text}}',textfont:{{size:11,color:'black'}},
colorbar:{{title:'Score',tickvals:[1,2,3,4,5],len:.9}},zmin:1,zmax:5}}],{{margin:{{t:20,b:70,l:60,r:40}},xaxis:{{title:'Delta',tickangle:45}},yaxis:{{title:'Tenor'}}}});
document.getElementById('heatmap').on('plotly_click',d=>{{addLegClick(d.points[0].y,D[d.points[0].x])}})}};
function getP(t,d){{return S.find(x=>x.tenor===t&&x.delta_label===d)}};
function compare(){{var p1=getP(document.getElementById('c-t1').value,document.getElementById('c-d1').value),
p2=getP(document.getElementById('c-t2').value,document.getElementById('c-d2').value);if(!p1||!p2)return;
var diff=p1.richness-p2.richness,nT=-p1.theta+p2.theta,nG=p1.gamma-p2.gamma,nV=p1.vega-p2.vega,
typ=p1.tenor===p2.tenor?'Fly/RR':(p1.delta_label===p2.delta_label?'Calendar':'Diagonal'),
sig=diff<-0.5?'✅ BUY spread':diff>0.5?'🔴 SELL spread':'⚪ Neutral';
document.getElementById('compare-result').innerHTML='<div class="spread-result"><strong>'+typ+'</strong><br>Richness Diff: <strong>'+diff.toFixed(2)+'</strong> '+sig+
'<br><br><div class="greeks-grid"><div class="greek-box"><div class="greek-label">Net Theta</div><div class="greek-value '+(nT>=0?'positive':'negative')+'">$'+nT.toFixed(0)+'</div></div>'+
'<div class="greek-box"><div class="greek-label">Net Gamma</div><div class="greek-value">$'+nG.toFixed(0)+'</div></div>'+
'<div class="greek-box"><div class="greek-label">Net Vega</div><div class="greek-value">$'+nV.toFixed(0)+'</div></div>'+
'<div class="greek-box"><div class="greek-label">Θ/Γ</div><div class="greek-value">'+(nG?Math.abs(nT/nG).toFixed(3):'N/A')+'</div></div></div></div>'}};
function addLeg(){{legs.push({{tenor:T[0],delta:D[4],dir:1,not:1}});renderLegs()}};
function addLegClick(t,d){{legs.push({{tenor:t,delta:d,dir:1,not:1}});renderLegs()}};
function removeLeg(i){{legs.splice(i,1);renderLegs()}};
function renderLegs(){{var h='';legs.forEach((l,i)=>{{h+='<div class="leg"><select onchange="legs['+i+'].dir=+this.value"><option value="1"'+(l.dir==1?' selected':'')+'>Buy</option><option value="-1"'+(l.dir==-1?' selected':'')+'>Sell</option></select>'+
'<select onchange="legs['+i+'].tenor=this.value">'+T.map(t=>'<option'+(l.tenor==t?' selected':'')+'>'+t+'</option>').join('')+'</select>'+
'<select onchange="legs['+i+'].delta=this.value">'+D.map((d,j)=>'<option value="'+d+'"'+(l.delta==d?' selected':'')+'>'+DL[j]+'</option>').join('')+'</select>'+
'<input type="number" value="'+l.not+'" min=".1" step=".1" onchange="legs['+i+'].not=+this.value" style="width:60px">'+
'<button onclick="removeLeg('+i+')">×</button></div>'}});document.getElementById('legs').innerHTML=h}};
function clearLegs(){{legs=[];renderLegs();document.getElementById('trade-result').innerHTML=''}};
function calcTrade(){{if(!legs.length)return;var tot={{t:0,g:0,v:0,d:0}},det='';
legs.forEach(l=>{{var p=getP(l.tenor,l.delta);if(!p)return;var m=l.dir*l.not;tot.t+=p.theta*m;tot.g+=p.gamma*m;tot.v+=p.vega*m;tot.d+=p.delta*m;
det+=(l.dir>0?'BUY ':'SELL ')+l.not+'x '+l.tenor+' '+l.delta+'<br>'}});
document.getElementById('trade-result').innerHTML='<div class="greeks-result"><div style="font-size:12px;margin-bottom:10px">'+det+'</div>'+
'<div class="greeks-grid"><div class="greek-box"><div class="greek-label">Net Delta</div><div class="greek-value">'+tot.d.toFixed(3)+'</div></div>'+
'<div class="greek-box"><div class="greek-label">Net Gamma</div><div class="greek-value">$'+tot.g.toFixed(0)+'</div></div>'+
'<div class="greek-box"><div class="greek-label">Net Theta</div><div class="greek-value '+(tot.t>=0?'positive':'negative')+'">$'+tot.t.toFixed(0)+'</div></div>'+
'<div class="greek-box"><div class="greek-label">Net Vega</div><div class="greek-value">$'+tot.v.toFixed(0)+'</div></div></div></div>'}};
function calcGreeks(){{var p=getP(document.getElementById('calc-t').value,document.getElementById('calc-d').value);if(!p)return;
document.getElementById('calc-result').innerHTML='<div class="greeks-result"><div style="margin-bottom:10px"><strong>'+p.tenor+' '+p.delta_label+'</strong> | K: '+p.strike.toFixed(4)+' | Vol: '+p.vol.toFixed(2)+'%</div>'+
'<div class="greeks-grid"><div class="greek-box"><div class="greek-label">Delta</div><div class="greek-value">'+p.delta.toFixed(3)+'</div></div>'+
'<div class="greek-box"><div class="greek-label">Gamma</div><div class="greek-value">$'+p.gamma.toFixed(0)+'</div></div>'+
'<div class="greek-box"><div class="greek-label">Theta</div><div class="greek-value '+(p.theta>=0?'positive':'negative')+'">$'+p.theta.toFixed(0)+'</div></div>'+
'<div class="greek-box"><div class="greek-label">Vega</div><div class="greek-value">$'+p.vega.toFixed(0)+'</div></div></div>'+
'<div style="margin-top:10px;text-align:center"><span style="font-size:13px">Gamma Richness: <strong style="color:#1F4E79">'+p.richness.toFixed(2)+'</strong></span></div></div>'}};
init();heatmap();
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
        print("\n  → Edit Excel file, then run again.\n")
        return
    
    print(f"Loading: {fp}")
    mkt = load_data(fp)
    print(f"  Spot: {mkt['spot']:.4f} | Terms: {mkt['r_terms']*100:.2f}% | Base: {mkt['r_base']*100:.2f}% | Roll: {mkt['tn_roll']:.2f} pips")
    
    print("\nBuilding surface...")
    df = build_surface(mkt)
    
    print("\nGamma Richness (1=Cheap, 5=Rich):")
    piv = df.pivot(index='tenor', columns='delta_label', values='richness')[['10P','25P','ATM','25C','10C']]
    print(piv.round(2).to_string())
    
    out = create_dashboard(df, mkt)
    print(f"\n  → Dashboard: {out}")
    df.to_csv('fx_gamma_surface.csv', index=False)
    print(f"  → CSV: fx_gamma_surface.csv\n\n✓ Complete!\n")

if __name__ == '__main__':
    main()
