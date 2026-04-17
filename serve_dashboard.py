#!/usr/bin/env python3
"""
serve_dashboard.py v3.0 — FX Options Analytics Server
======================================================
Features:
  - Self-patching: injects Bloomberg button + math fixes into any HTML version
  - DTCC SDR reader with background downloading
  - Corrected delta/vol math served via /api/mathfix.js
  - Bloomberg vol surface refresh (built-in)
  - Bloomberg live spot streaming (if blpapi available)

Just run:  python serve_dashboard.py
"""

SCRIPT_VER = "v3.2-20260327"

import argparse, csv, io, json, os, sys, threading, time, traceback, webbrowser, zipfile
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Dict, List, Optional

def log(msg):
    sys.stderr.write(f"  [{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


# ==============================================================
# JAVASCRIPT MATH FIXES — served at /api/mathfix.js
# Overrides buggy functions in the dashboard with corrected versions.
# ==============================================================
MATHFIX_JS = r"""
// === serve_dashboard.py v3.0 math overrides ===
console.log('mathfix.js loaded — fwd pts, spot delta, prints, premium diagnostics');

// Forward points scaling: JPY pairs = /100, others = /10000
function _fwdPtScale(pair) {
    if (!pair) return 10000;
    return /JPY/.test(pair) ? 100 : 10000;
}

// Tag mktSurfaces with pair name
var _origInitApp = typeof initApp === 'function' ? initApp : null;
if (_origInitApp) {
    initApp = function() {
        _origInitApp();
        Object.keys(mktSurfaces).forEach(function(p) { mktSurfaces[p]._pair = p; });
    };
}

// Fix dCD: Garman-Kohlhagen spot delta = exp(-rf*T) * N(d1)
dCD = function(F, K, s, T, ic, rd, S) {
    if (T <= 0 || s <= 0 || F <= 0 || K <= 0) return ic ? 0.5 : -0.5;
    var d1 = (Math.log(F/K) + 0.5*s*s*T) / (s*Math.sqrt(T));
    var dff = 1;
    if (typeof rd === 'number' && S > 0) {
        dff = Math.exp(-rd * T) * F / S;  // = exp(-rf*T)
    }
    return ic ? dff * ncdf(d1) : dff * (ncdf(d1) - 1);
};

// Fix buildSurf: pre-scale fwdPts for JPY pairs
var _origBuildSurf = typeof buildSurf === 'function' ? buildSurf : null;
if (_origBuildSurf) {
    buildSurf = function(pd) {
        var pair = pd._pair || activeAnPair || activeMktPair || '';
        var scale = _fwdPtScale(pair);
        if (scale !== 10000) {
            var adj = JSON.parse(JSON.stringify(pd));
            adj._pair = pair;
            adj.tenors.forEach(function(t) { t.fwdPts = t.fwdPts * 10000 / scale; });
            return _origBuildSurf(adj);
        }
        return _origBuildSurf(pd);
    };
}

// Fix dProcTrades: correct fwd scaling, spot delta, premium handling, better diagnostics
var _origDProcTrades = typeof dProcTrades === 'function' ? dProcTrades : null;
if (_origDProcTrades) {
    dProcTrades = function(trades) {
        dAT = {}; dPM = {};
        DTCC_G10.concat(DTCC_EM).forEach(function(p) { dAT[p] = []; });
        var solved = 0, fallback = 0, skipped = 0, noPrem = 0, noSpot = 0, noPkg = 0;
        var noSpotPairs = {};

        trades.forEach(function(t) { try {
            var pk = DPA[t.pair]; if (!pk || !dAT[pk]) return;
            var days = parseInt(t.days) || 0; if (days <= 0) { skipped++; return; }
            var notl = parseFloat(t.usd_amt) || 0;
            var spot = parseFloat(t.spot) || 0, strike = parseFloat(t.strike) || 0;

            if (spot <= 0 && pk && mktSurfaces[pk] && mktSurfaces[pk].spot > 0) {
                spot = mktSurfaces[pk].spot;
            }
            if (spot <= 0 || strike <= 0) { noSpot++; noSpotPairs[pk] = (noSpotPairs[pk]||0)+1; return; }

            var scale = _fwdPtScale(pk);
            var fwd = spot;
            var rd = 0.045, rf = 0.025;
            if (pk && mktSurfaces[pk]) {
                rd = mktSurfaces[pk].r_d || 0.045;
                rf = mktSurfaces[pk].r_f || 0.025;
                if (mktSurfaces[pk].tenors) {
                    var tnrs = mktSurfaces[pk].tenors, bestTi = 0, bestDist = 9999;
                    for (var ti = 0; ti < tnrs.length; ti++) {
                        var dist = Math.abs(tnrs[ti].T * 365 - days);
                        if (dist < bestDist) { bestDist = dist; bestTi = ti; }
                    }
                    fwd = spot + (tnrs[bestTi].fwdPts || 0) / scale;
                }
            }

            var T = days / 365;
            var apiIsCall = (t.opt_type === 'CALL');
            var ic = (strike >= fwd);

            // IV from premium — CORRECT currency conversion
            // Skip premium-based solving for PACKAGE trades (premium is for whole structure)
            var iv = null;
            var isPackage = t.is_package === true || t.is_package === 'true';
            var premRaw = t.premium ? Number(String(t.premium).replace(/,/g, '')) : 0;
            var premCcy = (t.premium_ccy || '').toUpperCase();
            var baseCcy = (t.base_ccy || pk.slice(0,3)).toUpperCase();
            var termsCcy = (t.terms_ccy || pk.slice(3)).toUpperCase();
            var baseNotl = t.base_notl ? parseFloat(t.base_notl) : 0;
            if (baseNotl <= 0) baseNotl = notl * 1e6 / spot;  // fallback estimate

            if (premRaw > 0 && baseNotl > 0 && !isPackage) {
                // B76P = price in TERMS ccy per unit BASE ccy
                // So mktP = total_prem_in_TERMS / base_notional
                var mktP;
                if (premCcy === termsCcy) {
                    // Premium already in terms currency — just divide by base notional
                    // e.g. USDJPY prem in JPY: mktP = prem_JPY / USD_notl
                    mktP = premRaw / baseNotl;
                } else if (premCcy === baseCcy) {
                    // Premium in base currency — multiply by fwd to get terms ccy
                    // e.g. EURUSD prem in EUR: mktP = prem_EUR * fwd / EUR_notl
                    mktP = premRaw * fwd / baseNotl;
                } else if (premCcy === 'USD' && termsCcy !== 'USD') {
                    // Premium in USD, but terms is not USD (e.g. EURGBP)
                    // Would need cross rate — approximate with spot
                    mktP = premRaw * spot / baseNotl;
                } else {
                    // Premium in USD, terms is USD (e.g. EURUSD, GBPUSD)
                    mktP = premRaw / baseNotl;
                }
                var solvedIV = b76SolveIV(fwd, strike, T, rd, apiIsCall, mktP);
                if (solvedIV !== null) {
                    // Sanity check: reject if IV is more than 2.5x the ATM mark
                    var atmMark = 0;
                    if (mktSurfaces[pk] && mktSurfaces[pk].tenors) {
                        var tnrs2 = mktSurfaces[pk].tenors, bestTi2 = 0, bestDist2 = 9999;
                        for (var ti2 = 0; ti2 < tnrs2.length; ti2++) {
                            var dist2 = Math.abs(tnrs2[ti2].T * 365 - days);
                            if (dist2 < bestDist2) { bestDist2 = dist2; bestTi2 = ti2; }
                        }
                        atmMark = tnrs2[bestTi2].atm || 0;
                    }
                    if (atmMark > 0 && solvedIV > atmMark * 1.5) {
                        // Solved IV way above marks — likely currency mismatch or exotic premium
                        // Skip this solve, fall through to package price
                    } else {
                        iv = solvedIV; solved++;
                    }
                }
            } else {
                if (isPackage) noPkg++;
                else noPrem++;
            }

            // Fallback: packageTransactionPrice with notation=3 (percentage = IV)
            if (iv === null && t.iv != null && t.iv !== '\u2014' && t.iv !== '') {
                var siteIV = parseFloat(String(t.iv).replace('%', ''));
                if (!isNaN(siteIV) && siteIV > 0) { iv = siteIV; fallback++; }
            }
            if (iv === null) { skipped++; return; }

            // Spot delta with GK discount
            var delta = dCD(fwd, strike, iv / 100, T, ic, rd, spot);

            if (Math.abs(delta) < 0.05 || iv > 60 || iv < 0.5) { skipped++; return; }
            var dBucket = dDB(delta);
            if (dBucket === 0 || dBucket === 4) {
                var tw = tenorWeights(days); var bti = tw[0].ti;
                var base = dtccGetBase(pk, DT[bti]);
                if (base && base.vols[dBucket] > 0 && Math.abs(iv - base.vols[dBucket]) > 3.5) { skipped++; return; }
            }
            if (spot > 0 && (!dPM[pk] || t.time > (dPM[pk].lt || ''))) dPM[pk] = {spot: spot, lt: t.time};
            dAT[pk].push({
                time: t.time || '', type: ic ? 'CALL' : 'PUT', strike: strike, spot: spot,
                fwd: fwd, days: days, iv: iv, notl: notl, expiry: t.expiry || '',
                ic: ic, delta: delta, ivSrc: solved > fallback ? 'calc' : 'site'
            });
        } catch(e) { skipped++; } });

        var dot = document.getElementById('dd'), txt = document.getElementById('dt');
        if (dot && dot.className.indexOf('ok') >= 0) {
            var total = solved + fallback;
            var msg = total + ' with IV (' + solved + ' from prem, ' + fallback + ' from site)';
            if (noPrem > 0) msg += ' | ' + noPrem + ' no prem';
            if (noPkg > 0) msg += ' | ' + noPkg + ' pkgs skipped';
            if (noSpot > 0) {
                var pairList = Object.keys(noSpotPairs).sort(function(a,b){return noSpotPairs[b]-noSpotPairs[a];}).slice(0,5);
                msg += ' | ' + noSpot + ' no spot: ' + pairList.map(function(p){return p+'('+noSpotPairs[p]+')';}).join(', ');
            }
            if (skipped > 0) msg += ' | ' + skipped + ' filtered';
            txt.textContent = msg;
        }
    };
}

// Fix dRFeed: show Strike, Time, Date, Notl (.5M), Vol, Delta
var _origDRFeed = typeof dRFeed === 'function' ? dRFeed : null;
dRFeed = function() {
    var tr = (dAT[dAP] || []).slice().sort(function(a,b) {
        return a.time > b.time ? -1 : a.time < b.time ? 1 : 0;
    });
    document.getElementById('dfc').textContent = tr.length + ' prints';
    var list = document.getElementById('dfl');
    if (!tr.length) { list.innerHTML = '<div style="padding:2rem;text-align:center;color:#888">No prints</div>'; return; }

    // Override header
    var hdr = document.querySelector('.dtcc-feed-header');
    if (hdr) hdr.innerHTML = '<span>Strike</span><span>Time</span><span>Expiry</span><span>Notl</span><span>Vol</span><span>\u0394</span>';

    var h = '';
    tr.slice(0, 200).forEach(function(t, i) {
        var tc = t.ic ? 'color:#66bb6a' : 'color:#ef5350';
        var vc = t.iv >= 15 ? 'color:#ffa726;font-weight:700' : t.iv >= 10 ? 'color:#90caf9;font-weight:600' : 'color:#aaa';
        var sc = t.notl >= 50 ? 'color:#ffa726;font-weight:700' : t.notl >= 20 ? 'color:#90caf9;font-weight:600' : 'color:#888';
        var ivTxt = t.iv.toFixed(1) + (t.ivSrc === 'calc' ? '*' : '');
        // Round notional to nearest 0.5M
        var notlR = Math.round(t.notl * 2) / 2;
        var notlTxt = notlR >= 1000 ? (notlR/1000).toFixed(1) + 'B' : notlR.toFixed(1) + 'M';
        // Strike formatting: 3 decimals for <10, 2 for <1000, 0 for >=1000
        var kTxt = t.strike >= 1000 ? t.strike.toFixed(0) : t.strike < 10 ? t.strike.toFixed(4) : t.strike.toFixed(2);
        // Expiry as MMM-DD
        var expTxt = t.expiry ? t.expiry.slice(5) : '';
        var dTxt = Math.round(Math.abs(t.delta) * 100) + '\u0394';
        h += '<div class="dtcc-feed-item' + (i < 3 ? ' fresh' : '') + '">'
            + '<span style="' + tc + ';font-weight:700;font-size:10px">' + kTxt + '</span>'
            + '<span style="color:#888;font-variant-numeric:tabular-nums">' + t.time + '</span>'
            + '<span style="color:#888;font-size:10px">' + expTxt + '</span>'
            + '<span style="' + sc + '">$' + notlTxt + '</span>'
            + '<span style="' + vc + '">' + ivTxt + '</span>'
            + '<span style="color:#888">' + dTxt + '</span>'
            + '</div>';
    });
    list.innerHTML = h;
};

// Bloomberg live data polling — spots + fwd points + derived rates
(function() {
    var DEFAULT_RD = {USD:0.045,EUR:0.025,GBP:0.044,JPY:0.005,CHF:0.015,AUD:0.035,NZD:0.04,CAD:0.035,
                      SEK:0.03,NOK:0.035,MXN:0.1,BRL:0.12,TRY:0.4,ZAR:0.08,CNH:0.025,
                      INR:0.065,KRW:0.03,SGD:0.03,TWD:0.015,IDR:0.06,PHP:0.055,THB:0.02,
                      HKD:0.045,CLP:0.06,COP:0.1,PEN:0.06,ILS:0.045};
    var TN_LABELS = ['O/N','1W','2W','1M','2M','3M','6M','9M','1Y','2Y'];
    function pollBBG() {
        fetch('/api/bbg_spots')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data) return;
            var spots = data.spots || {};
            var fwdPts = data.fwd_pts || {};
            var rates = data.rates || {};

            Object.keys(spots).forEach(function(pair) {
                var s = parseFloat(spots[pair]);
                if (s <= 0) return;

                if (!mktSurfaces[pair]) {
                    var base = pair.slice(0,3), terms = pair.slice(3);
                    var rd = (rates[pair] && rates[pair].r_d) ? rates[pair].r_d : (DEFAULT_RD[terms] || 0.04);
                    var rf = (rates[pair] && rates[pair].r_f) ? rates[pair].r_f : (DEFAULT_RD[base] || 0.03);
                    mktSurfaces[pair] = {
                        spot: s, _pair: pair, r_d: rd, r_f: rf,
                        tenors: TN_LABELS.map(function(tn, ti) {
                            return {tenor: tn, T: TENOR_T[ti], atm: 8, rr25: 0, rr10: 0, fly25: 0.2, fly10: 0.5, fwdPts: 0};
                        })
                    };
                } else {
                    mktSurfaces[pair].spot = s;
                }

                // Apply CIP-derived rates
                if (rates[pair]) {
                    if (rates[pair].r_d) mktSurfaces[pair].r_d = rates[pair].r_d;
                    if (rates[pair].r_f) mktSurfaces[pair].r_f = rates[pair].r_f;
                }

                // Apply fwd points per tenor
                if (fwdPts[pair] && mktSurfaces[pair].tenors) {
                    mktSurfaces[pair].tenors.forEach(function(tn) {
                        var fp = fwdPts[pair][tn.tenor];
                        if (fp !== undefined && fp !== null) tn.fwdPts = fp;
                    });
                }
            });

            var el = document.getElementById('mkt-status');
            if (el) {
                var parts = [];
                if (Object.keys(spots).length > 0) parts.push(Object.keys(spots).length + ' spots');
                if (Object.keys(fwdPts).length > 0) parts.push(Object.keys(fwdPts).length + ' fwd curves');
                if (Object.keys(rates).length > 0) parts.push('rates');
                el.textContent = parts.length ? 'BBG: ' + parts.join(', ') : '';
                el.style.color = '#66bb6a';
            }
        })
        .catch(function() {});
    }
    setInterval(pollBBG, 5000);
    setTimeout(pollBBG, 1500);
})();

// === DAMPER: blend DTCC prints with your marks ===
var DTCC_DAMPER = 0.25;
var dCS_raw = {};  // raw DTCC surface BEFORE damping (for alerts)
var dStratView = 'marks';  // 'marks' or 'adjusted'

// Inject damper slider
(function injectDamper() {
    var dpb = document.getElementById('dpb');
    if (!dpb) { setTimeout(injectDamper, 500); return; }
    var ctrls = dpb.parentElement.querySelector('div[style*="gap:10px"]');
    if (!ctrls) { setTimeout(injectDamper, 500); return; }
    var d = document.createElement('span');
    d.style.cssText = 'display:flex;align-items:center;gap:4px;margin-left:8px';
    d.innerHTML = '<span style="font-size:11px;color:#888">Damper</span>'
        + '<input type="range" min="0" max="100" value="25" id="dtcc-damper" '
        + 'style="width:60px;accent-color:#90caf9" oninput="DTCC_DAMPER=this.value/100;'
        + 'document.getElementById(\'dtcc-damper-val\').textContent=this.value+\'%\';'
        + 'dBuildSurf();dRA();">'
        + '<span id="dtcc-damper-val" style="font-size:11px;color:#90caf9;min-width:30px">25%</span>';
    ctrls.appendChild(d);
})();

// Override dBuildSurf: save raw, then apply damper
var _origDBuildSurf = typeof dBuildSurf === 'function' ? dBuildSurf : null;
if (_origDBuildSurf) {
    dBuildSurf = function() {
        _origDBuildSurf();
        // Save raw DTCC surface before damping
        dCS_raw = {};
        DTCC_G10.concat(DTCC_EM).forEach(function(pair) {
            if (dCS[pair]) dCS_raw[pair] = dCS[pair].map(function(r) { return r.slice(); });
        });
        // Apply damper: displayed = marks*(1-alpha) + dtcc_raw*alpha
        var alpha = DTCC_DAMPER;
        DTCC_G10.concat(DTCC_EM).forEach(function(pair) {
            var raw = dCS[pair];
            if (!raw) return;
            for (var ti = 0; ti < DT.length; ti++) {
                if (!raw[ti]) continue;
                var base = dtccGetBase(pair, DT[ti]);
                if (!base) continue;
                for (var di = 0; di < 5; di++) {
                    if (raw[ti][di] === null) continue;
                    raw[ti][di] = base.vols[di] * (1 - alpha) + raw[ti][di] * alpha;
                }
            }
        });
    };
}

// === HEATMAP: show MY MARKS with alert emojis where DTCC prints deviate ===
dRSurf = function() {
    var pk = dAP;
    var raw = dCS_raw[pk];  // raw DTCC-derived surface
    var pd = mktSurfaces[pk];
    if (!pd || !pd.tenors) {
        document.getElementById('dsp').innerHTML = '<div style="text-align:center;padding:80px 20px;color:#888">No marks for ' + pk + '</div>';
        return;
    }

    // Build marks surface [ti][di] and track which cells have DTCC data
    var marks = [], alerts = [];
    for (var ti = 0; ti < DT.length; ti++) {
        var base = dtccGetBase(pk, DT[ti]);
        marks[ti] = base ? base.vols.slice() : [null, null, null, null, null];
        alerts[ti] = [0, 0, 0, 0, 0];  // 0=none, 1=yellow(>0.5), 2=red(>1.0)
        if (base && raw && raw[ti]) {
            for (var di = 0; di < 5; di++) {
                if (raw[ti][di] !== null && marks[ti][di] !== null) {
                    var diff = Math.abs(raw[ti][di] - marks[ti][di]);
                    if (diff >= 1.0) alerts[ti][di] = 2;
                    else if (diff >= 0.5) alerts[ti][di] = 1;
                }
            }
        }
    }

    // Z values = marks, text = marks + alert emoji
    var z = marks.map(function(r) { return r.map(function(v) { return v !== null ? parseFloat(v.toFixed(2)) : null; }); });
    var txt = marks.map(function(r, ti) {
        return r.map(function(v, di) {
            if (v === null) return '';
            var s = v.toFixed(2);
            if (alerts[ti][di] === 2) s += ' ' + String.fromCodePoint(0x1F534);  // red circle
            else if (alerts[ti][di] === 1) s += ' ' + String.fromCodePoint(0x1F7E1);  // yellow circle
            return s;
        });
    });

    var cs = [[0, '#0d47a1'], [0.5, '#fff59d'], [1, '#b71c1c']];
    Plotly.react('dsp', [{
        z: z, x: DDL, y: DT, type: 'heatmap', colorscale: cs,
        text: txt, texttemplate: '%{text}', textfont: {size: 10, color: 'black'},
        hovertemplate: '%{y} %{x}<br>Mark: %{z:.2f}%<extra></extra>',
        connectgaps: false, colorbar: {thickness: 12, len: 0.85, tickfont: {size: 10, color: '#aaa'}}
    }], {
        margin: {l: 50, r: 60, t: 10, b: 45}, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        xaxis: {color: '#aaa', gridcolor: '#555', tickfont: {size: 11}},
        yaxis: {color: '#aaa', gridcolor: '#555', tickfont: {size: 11}, autorange: 'reversed'},
        font: {color: '#ccc'}
    }, {displayModeBar: false, responsive: true});
};

// === ATM/RR/FLY TABLE: toggle between My Marks and Adjusted ===
// Inject toggle buttons (once DOM is ready)
(function injectStratToggle() {
    var dsb = document.getElementById('dsb');
    if (!dsb) { setTimeout(injectStratToggle, 500); return; }
    var card = dsb.closest('.card');
    if (!card) { setTimeout(injectStratToggle, 500); return; }
    var h2 = card.querySelector('h2');
    if (!h2 || h2.querySelector('#strat-toggle-marks')) return;
    var wrap = document.createElement('div');
    wrap.style.cssText = 'float:right';
    wrap.innerHTML = '<button class="btn-toggle active" id="strat-toggle-marks" onclick="dStratView=\'marks\';document.getElementById(\'strat-toggle-marks\').className=\'btn-toggle active\';document.getElementById(\'strat-toggle-adj\').className=\'btn-toggle\';dRStrat();">My Marks</button>'
        + '<button class="btn-toggle" id="strat-toggle-adj" onclick="dStratView=\'adjusted\';document.getElementById(\'strat-toggle-adj\').className=\'btn-toggle active\';document.getElementById(\'strat-toggle-marks\').className=\'btn-toggle\';dRStrat();">Adjusted</button>';
    h2.appendChild(wrap);
})();

function dCC2(v) {
    if (v == null) return '<td class="dtcc-chg-flat">&mdash;</td>';
    var c = v > 0.005 ? 'dtcc-chg-up' : v < -0.005 ? 'dtcc-chg-dn' : 'dtcc-chg-flat';
    return '<td class="' + c + '">' + (v > 0 ? '+' : '') + v.toFixed(2) + '</td>';
}

dRStrat = function() {
    var pk = dAP;
    var raw = dCS_raw[pk];     // raw DTCC surface
    var damped = dCS[pk];      // damped surface
    var tr = dAT[pk] || [];
    var pd = mktSurfaces[pk];
    if (!pd || !pd.tenors) { document.getElementById('dsb').innerHTML = ''; return; }

    // Count trades per tenor
    var tc = Array(DT.length).fill(0);
    tr.forEach(function(t) { var tw = tenorWeights(t.days); tw.forEach(function(wt) { tc[wt.ti] += wt.w; }); });

    var rows = [];
    for (var t = 0; t < DT.length; t++) {
        var base = dtccGetBase(pk, DT[t]);
        if (!base) continue;

        // Marks values
        var mA = base.atm, mR25 = base.rr25, mR10 = base.rr10, mF25 = base.fly25, mF10 = base.fly10;

        // DTCC raw values (for computing deltas)
        var dA = null, dR25 = null, dR10 = null, dF25 = null, dF10 = null;
        if (raw && raw[t] && raw[t][2] !== null) {
            dA = raw[t][2];
            if (raw[t][3] !== null && raw[t][1] !== null) dR25 = raw[t][3] - raw[t][1];
            if (raw[t][4] !== null && raw[t][0] !== null) dR10 = raw[t][4] - raw[t][0];
            if (raw[t][3] !== null && raw[t][1] !== null) dF25 = (raw[t][3] + raw[t][1]) / 2 - raw[t][2];
            if (raw[t][4] !== null && raw[t][0] !== null) dF10 = (raw[t][4] + raw[t][0]) / 2 - raw[t][2];
        }

        // Deltas = DTCC - Marks (how much to adjust marks to match DTCC)
        var chgA = dA !== null ? dA - mA : null;
        var chgR25 = dR25 !== null ? dR25 - mR25 : null;
        var chgR10 = dR10 !== null ? dR10 - mR10 : null;
        var chgF25 = dF25 !== null ? dF25 - mF25 : null;
        var chgF10 = dF10 !== null ? dF10 - mF10 : null;

        // Display values depend on toggle
        var showA, showR25, showR10, showF25, showF10;
        if (dStratView === 'adjusted' && dA !== null) {
            showA = dA; showR25 = dR25; showR10 = dR10; showF25 = dF25; showF10 = dF10;
        } else {
            showA = mA; showR25 = mR25; showR10 = mR10; showF25 = mF25; showF10 = mF10;
        }

        rows.push('<tr><td>' + DT[t] + '</td>'
            + '<td>' + showA.toFixed(2) + '</td>' + dCC2(chgA)
            + '<td>' + (showR25 !== null ? showR25.toFixed(2) : '&mdash;') + '</td>' + dCC2(chgR25)
            + '<td>' + (showR10 !== null ? showR10.toFixed(2) : '&mdash;') + '</td>' + dCC2(chgR10)
            + '<td>' + (showF25 !== null ? showF25.toFixed(2) : '&mdash;') + '</td>' + dCC2(chgF25)
            + '<td>' + (showF10 !== null ? showF10.toFixed(2) : '&mdash;') + '</td>' + dCC2(chgF10)
            + '<td style="color:#888">' + Math.round(tc[t]) + '</td></tr>');
    }
    document.getElementById('dsb').innerHTML = rows.join('');
};

// Override dRFeed: show Strike, Time, Expiry, Notl, Vol, vs Mark, Delta
dRFeed = function() {
    var tr = (dAT[dAP] || []).slice().sort(function(a,b) {
        return a.time > b.time ? -1 : a.time < b.time ? 1 : 0;
    });
    document.getElementById('dfc').textContent = tr.length + ' prints';
    var list = document.getElementById('dfl');
    if (!tr.length) { list.innerHTML = '<div style="padding:2rem;text-align:center;color:#888">No prints</div>'; return; }

    var hdr = document.querySelector('.dtcc-feed-header');
    if (hdr) hdr.innerHTML = '<span>Strike</span><span>Time</span><span>Expiry</span><span>Notl</span><span>Vol</span><span>vs Mark</span><span>\u0394</span>';
    if (hdr) hdr.style.gridTemplateColumns = '62px 42px 50px 48px 42px 50px 36px';

    var h = '';
    tr.slice(0, 200).forEach(function(t, i) {
        var tc = t.ic ? 'color:#66bb6a' : 'color:#ef5350';
        var vc = t.iv >= 15 ? 'color:#ffa726;font-weight:700' : t.iv >= 10 ? 'color:#90caf9;font-weight:600' : 'color:#aaa';
        var sc = t.notl >= 50 ? 'color:#ffa726;font-weight:700' : t.notl >= 20 ? 'color:#90caf9;font-weight:600' : 'color:#888';
        var ivTxt = t.iv.toFixed(1) + (t.ivSrc === 'calc' ? '*' : '');
        var notlR = Math.round(t.notl * 2) / 2;
        var notlTxt = notlR >= 1000 ? (notlR/1000).toFixed(1) + 'B' : notlR.toFixed(1) + 'M';
        var kTxt = t.strike >= 1000 ? t.strike.toFixed(0) : t.strike < 10 ? t.strike.toFixed(4) : t.strike.toFixed(2);
        var expTxt = t.expiry ? t.expiry.slice(5) : '';
        var dTxt = Math.round(Math.abs(t.delta) * 100) + '\u0394';
        var diffTxt = '', diffStyle = 'color:#555';
        var pk = dAP;
        if (pk && mktSurfaces[pk] && mktSurfaces[pk].tenors) {
            var tnrs = mktSurfaces[pk].tenors, bestTi = 0, bestDist = 9999;
            for (var ti = 0; ti < tnrs.length; ti++) {
                var dist = Math.abs(tnrs[ti].T * 365 - t.days);
                if (dist < bestDist) { bestDist = dist; bestTi = ti; }
            }
            var base = dtccGetBase(pk, tnrs[bestTi].tenor);
            if (base) {
                var di = dDB(t.delta);
                var diff = t.iv - base.vols[di];
                diffTxt = (diff >= 0 ? '+' : '') + diff.toFixed(1);
                if (Math.abs(diff) >= 1.5) diffStyle = diff > 0 ? 'color:#ffa726;font-weight:700' : 'color:#42a5f5;font-weight:700';
                else if (Math.abs(diff) >= 0.5) diffStyle = diff > 0 ? 'color:#ffcc80' : 'color:#90caf9';
                else diffStyle = 'color:#666';
            }
        }
        h += '<div class="dtcc-feed-item' + (i < 3 ? ' fresh' : '') + '" style="grid-template-columns:62px 42px 50px 48px 42px 50px 36px">'
            + '<span style="' + tc + ';font-weight:700;font-size:10px">' + kTxt + '</span>'
            + '<span style="color:#888;font-variant-numeric:tabular-nums">' + t.time + '</span>'
            + '<span style="color:#888;font-size:10px">' + expTxt + '</span>'
            + '<span style="' + sc + '">$' + notlTxt + '</span>'
            + '<span style="' + vc + '">' + ivTxt + '</span>'
            + '<span style="' + diffStyle + ';font-size:10px">' + diffTxt + '</span>'
            + '<span style="color:#888">' + dTxt + '</span></div>';
    });
    list.innerHTML = h;
};

// === PORTFOLIO TAB OVERRIDES ===
// Delta in tables, written Greek names, gamma/notional heatmap toggles

// Inject gamma + notional toggle buttons
(function injectPortToggles() {
    var btn = document.getElementById('ph-btn-decay');
    if (!btn) { setTimeout(injectPortToggles, 500); return; }
    if (document.getElementById('ph-btn-gamma')) return;
    var g = document.createElement('button');
    g.id = 'ph-btn-gamma'; g.className = 'btn-toggle';
    g.textContent = 'Gamma'; g.onclick = function() { setPhView('gamma'); };
    btn.parentNode.insertBefore(g, btn.nextSibling);
    var n = document.createElement('button');
    n.id = 'ph-btn-notional'; n.className = 'btn-toggle';
    n.textContent = 'Notional'; n.onclick = function() { setPhView('notional'); };
    g.parentNode.insertBefore(n, g.nextSibling);
})();

setPhView = function(v) {
    currentPh = v;
    ['richness','vega','decay','gamma','notional'].forEach(function(k) {
        var el = document.getElementById('ph-btn-' + k);
        if (el) el.className = 'btn-toggle' + (k === v ? ' active' : '');
    });
    var titles = {richness:'Portfolio Richness (1-5)', vega:'Portfolio Vega ($K)',
                  decay:'Portfolio Decay ($K/day)', gamma:'Portfolio Gamma ($M)',
                  notional:'Total Notional ($M)'};
    document.getElementById('ph-title').textContent = titles[v] || v;
    renderPortHm();
};

var _origBuildPortHm = typeof buildPortHm === 'function' ? buildPortHm : null;
buildPortHm = function(cs, comp) {
    _origBuildPortHm(cs, comp);
    if (!portHmData) return;
    var nb = portHmData.strikes.length;
    portHmData.gamma = TENORS.map(function() { return portHmData.strikes.map(function() { return 0; }); });
    portHmData.notional = TENORS.map(function() { return portHmData.strikes.map(function() { return 0; }); });
    function getSI(k) {
        if (!portHmData.bw) return portHmData.strikes.indexOf(k);
        return Math.max(0, Math.min(Math.floor((k - portHmData.strikes[0]) / portHmData.bw), nb - 1));
    }
    function getTI(days) {
        var bk = [['O/N',1],['1W',7],['2W',14],['1M',30],['2M',61],['3M',91],['6M',182],['9M',274],['1Y',365],['2Y',730]];
        for (var i = bk.length - 1; i >= 0; i--) { if (days >= bk[i][1] * 0.7) return TENORS.indexOf(bk[i][0]); }
        return 0;
    }
    comp.forEach(function(p) {
        var ti = getTI(p.days), si = getSI(p.strike); if (ti < 0 || si < 0) return;
        portHmData.gamma[ti][si] += p.gamma;
        portHmData.notional[ti][si] += p.notional;
    });
};

var _origRenderPortHm = typeof renderPortHm === 'function' ? renderPortHm : null;
renderPortHm = function() {
    if (!portHmData) return;
    if (currentPh !== 'gamma' && currentPh !== 'notional') { _origRenderPortHm(); return; }
    var M = (currentPh === 'gamma' ? portHmData.gamma : portHmData.notional)
        .map(function(r) { return r.map(function(v) { return v === 0 ? null : v; }); });
    var bt = '$M';
    var cs2 = [[0,'#1565c0'],[0.5,'#e0e0e0'],[1,'#c62828']];
    var fl = M.flat().filter(function(v) { return v !== null; });
    var mx = fl.length > 0 ? Math.max(Math.abs(Math.min.apply(null, fl)), Math.abs(Math.max.apply(null, fl))) : 1;
    var txt = M.map(function(r) { return r.map(function(v) {
        if (v === null) return '';
        return (v >= 0 ? '+' : '') + v.toFixed(currentPh === 'notional' ? 1 : 4);
    }); });
    Plotly.react('port-hm', [{z: M, x: portHmData.labels, y: TENORS, type: 'heatmap', colorscale: cs2,
        text: txt, texttemplate: '%{text}', textfont: {size: 9, color: 'black'},
        colorbar: {title: {text: bt, font: {color:'#e0e0e0'}}, tickfont: {color:'#e0e0e0'}, len: .9},
        zmin: -mx, zmax: mx, hoverongaps: false}],
    {margin: {t:20,b:80,l:60,r:50}, xaxis: {title:'Strike',tickangle:45,color:'#e0e0e0',type:'category'},
     yaxis: {title:'Tenor',color:'#e0e0e0'}, paper_bgcolor:'#3d3d3d', plot_bgcolor:'#3d3d3d'},
    {displayModeBar: false, responsive: true});
    document.getElementById('port-hm').on('plotly_click', function(data) {
        var pt = data.points[0], ti = TENORS.indexOf(pt.y), si = portHmData.labels.indexOf(String(pt.x));
        if (ti >= 0 && si >= 0) showDrill(ti, si);
    });
};

// Override renderPortTab: add delta column, written Greek names
var _origRenderPortTab = typeof renderPortTab === 'function' ? renderPortTab : null;
renderPortTab = function() {
    _origRenderPortTab();
    if (!portComp || !portComp.length) return;
    var h = '<table><tr><th>#</th><th>Strike</th><th>Expiry</th><th>Days</th><th>Vol</th><th>Notl</th><th>Type</th><th>Delta</th><th>Gamma</th><th>Theta</th><th>Vega</th><th>Rich</th></tr>';
    portComp.forEach(function(p) {
        h += '<tr class="' + (p.notional >= 0 ? 'cheap' : 'rich') + '"><td>' + p.id + '</td><td>' + p.strike.toFixed(3) + '</td><td>' + p.expiry + '</td><td>' + p.days + '</td><td>' + p.vol.toFixed(1) + '</td><td>' + (p.notional >= 0 ? '+' : '') + p.notional.toFixed(1) + '</td><td>' + p.type + '</td><td>' + p.delta.toFixed(4) + '</td><td>' + p.gamma.toFixed(4) + '</td><td>' + p.theta.toFixed(2) + '</td><td>' + p.vega.toFixed(1) + '</td><td><span class="richness-badge" style="background:' + richColor(p.rich) + '">' + p.rich.toFixed(2) + '</span></td></tr>';
    });
    h += '</table>'; document.getElementById('pos-tbl').innerHTML = h;
};

showDrill = function(ti, si) {
    if (!portHmData) return;
    var pos = portHmData.byBucket[ti][si];
    var bw = portHmData.bw, sk = portHmData.strikes[si];
    var skL = bw > 0 ? sk.toFixed(3) + '-' + (sk + bw).toFixed(3) : sk.toFixed(3);
    document.getElementById('drill-title').textContent = TENORS[ti] + ' / ' + skL + ' (' + pos.length + ')';
    if (!pos.length) { document.getElementById('drill-content').innerHTML = '<p style="text-align:center;color:#aaa">No positions</p>'; }
    else {
        var h = '<table><tr><th>#</th><th>Strike</th><th>Expiry</th><th>Vol</th><th>Notl</th><th>Type</th><th>Delta</th><th>Gamma</th><th>Theta</th><th>Vega</th><th>Rich</th></tr>';
        pos.forEach(function(p) {
            h += '<tr class="' + (p.notional >= 0 ? 'cheap' : 'rich') + '"><td>' + p.id + '</td><td>' + p.strike.toFixed(3) + '</td><td>' + p.expiry + '</td><td>' + p.vol.toFixed(1) + '</td><td>' + (p.notional >= 0 ? '+' : '') + p.notional.toFixed(1) + '</td><td>' + p.type + '</td><td>' + p.delta.toFixed(4) + '</td><td>' + p.gamma.toFixed(4) + '</td><td>' + p.theta.toFixed(2) + '</td><td>' + p.vega.toFixed(1) + '</td><td><span class="richness-badge" style="background:' + richColor(p.rich) + '">' + p.rich.toFixed(2) + '</span></td></tr>';
        });
        h += '</table>'; document.getElementById('drill-content').innerHTML = h;
    }
    document.getElementById('drill-modal').classList.add('show');
};

renderIneff = function() {
    if (!portComp) return;
    var filtered = currentIneff === 'long' ? portComp.filter(function(p) { return p.notional > 0; }) : portComp.filter(function(p) { return p.notional < 0; });
    var sorted = filtered.sort(function(a, b) { return b.rich - a.rich; });
    if (!sorted.length) { document.getElementById('ineff-tbl').innerHTML = '<p style="text-align:center;color:#aaa">No ' + currentIneff + ' positions</p>'; return; }
    var h = '<table><tr><th>#</th><th>Strike</th><th>Expiry</th><th>Days</th><th>Notl</th><th>Type</th><th>Delta</th><th>Gamma</th><th>Theta</th><th>Decay</th><th>Rich</th></tr>';
    sorted.forEach(function(p) {
        h += '<tr class="' + (p.notional >= 0 ? 'cheap' : 'rich') + '"><td>' + p.id + '</td><td>' + p.strike.toFixed(3) + '</td><td>' + p.expiry + '</td><td>' + p.days + '</td><td>' + (p.notional >= 0 ? '+' : '') + p.notional.toFixed(1) + '</td><td>' + p.type + '</td><td>' + p.delta.toFixed(4) + '</td><td>' + p.gamma.toFixed(4) + '</td><td>' + p.theta.toFixed(2) + '</td><td>' + p.decay.toFixed(1) + '</td><td><span class="richness-badge" style="background:' + richColor(p.rich) + '">' + p.rich.toFixed(2) + '</span></td></tr>';
    });
    h += '</table>'; document.getElementById('ineff-tbl').innerHTML = h;
};

// === DTCC HEATMAP DRILLDOWN ===
// Click any cell on the marks heatmap to see contributing trades
(function setupDtccDrill() {
    var check = function() {
        var el = document.getElementById('dsp');
        if (!el) { setTimeout(check, 1000); return; }
        el.on('plotly_click', function(data) {
            if (!data || !data.points || !data.points.length) return;
            var pt = data.points[0], ti = DT.indexOf(pt.y), di = DDL.indexOf(pt.x);
            if (ti < 0 || di < 0) return;
            var pk = dAP, trades = (dAT[pk] || []).filter(function(t) {
                var tw = tenorWeights(t.days), tdi = dDB(t.delta);
                return tw.some(function(wt) { return wt.ti === ti; }) && tdi === di;
            });
            if (!trades.length) return;
            var base = dtccGetBase(pk, DT[ti]);
            var markVol = base ? base.vols[di] : null;
            var title = pk + ' ' + DT[ti] + ' ' + DDL[di] + ' \u2014 ' + trades.length + ' trades';
            if (markVol) title += ' (mark: ' + markVol.toFixed(2) + ')';
            document.getElementById('drill-title').textContent = title;
            var h = '<table style="width:100%"><tr><th>Time</th><th>Strike</th><th>Expiry</th><th>Type</th><th>Vol</th><th>vs Mark</th><th>Delta</th><th>Notl</th><th>Spot</th></tr>';
            trades.sort(function(a, b) { return a.time > b.time ? -1 : 1; }).forEach(function(t) {
                var diff = markVol ? t.iv - markVol : 0;
                var dc = Math.abs(diff) >= 1.0 ? 'color:#ef5350;font-weight:700' : Math.abs(diff) >= 0.5 ? 'color:#ffa726' : 'color:#aaa';
                var kTxt = t.strike >= 1000 ? t.strike.toFixed(0) : t.strike < 10 ? t.strike.toFixed(4) : t.strike.toFixed(2);
                h += '<tr><td>' + t.time + '</td><td>' + kTxt + '</td><td>' + (t.expiry||'').slice(5) + '</td><td style="' + (t.ic ? 'color:#66bb6a' : 'color:#ef5350') + '">' + t.type + '</td><td>' + t.iv.toFixed(1) + '</td><td style="' + dc + '">' + (diff >= 0 ? '+' : '') + diff.toFixed(1) + '</td><td>' + Math.round(Math.abs(t.delta)*100) + '</td><td>$' + t.notl.toFixed(1) + 'M</td><td>' + (t.spot > 0 ? t.spot.toFixed(4) : '-') + '</td></tr>';
            });
            h += '</table>';
            document.getElementById('drill-content').innerHTML = h;
            document.getElementById('drill-modal').classList.add('show');
        });
    };
    setTimeout(check, 2000);
})();

// === DTCC EDIT MODE ===
var dtccEditMode = false;
(function injectEditToggle() {
    var dpb = document.getElementById('dpb');
    if (!dpb) { setTimeout(injectEditToggle, 500); return; }
    var ctrls = dpb.parentElement.querySelector('div[style*="gap:10px"]');
    if (!ctrls || document.getElementById('dtcc-edit-btn')) return;
    var wrap = document.createElement('span');
    wrap.style.cssText = 'display:flex;align-items:center;gap:4px;margin-left:12px';
    wrap.innerHTML = '<button class="btn-toggle" id="dtcc-edit-btn" onclick="toggleDtccEdit()" style="border-color:#e65100;color:#e65100">Edit Mode</button>'
        + '<button class="btn" id="dtcc-publish-btn" onclick="publishDtccEdits()" style="display:none;background:#2e7d32;color:white;font-size:11px;padding:5px 12px;border-radius:4px">Publish</button>';
    ctrls.appendChild(wrap);
})();

function toggleDtccEdit() {
    dtccEditMode = !dtccEditMode;
    var btn = document.getElementById('dtcc-edit-btn');
    var pub = document.getElementById('dtcc-publish-btn');
    if (dtccEditMode) {
        btn.className = 'btn-toggle active'; btn.style.borderColor = '#e65100'; btn.style.background = '#e65100'; btn.style.color = 'white';
        if (pub) pub.style.display = 'inline-block';
        renderDtccEditTable();
    } else {
        btn.className = 'btn-toggle'; btn.style.borderColor = '#e65100'; btn.style.background = ''; btn.style.color = '#e65100';
        if (pub) pub.style.display = 'none';
        document.getElementById('dsp').innerHTML = '';
        Plotly.purge('dsp');
        dRSurf();
    }
}

function renderDtccEditTable() {
    var pk = dAP, pd = mktSurfaces[pk];
    if (!pd || !pd.tenors) return;
    var h = '<div style="overflow-x:auto"><table class="vol-table"><tr><th>Tenor</th><th>ATM</th><th>25d RR</th><th>10d RR</th><th>25d Fly</th><th>10d Fly</th><th>Fwd Pts</th></tr>';
    pd.tenors.forEach(function(t, i) {
        h += '<tr><td>' + t.tenor + '</td>';
        ['atm','rr25','rr10','fly25','fly10','fwdPts'].forEach(function(f) {
            h += '<td><input type="number" step="0.01" value="' + (t[f] || 0).toFixed(2)
                + '" data-pair="' + pk + '" data-ti="' + i + '" data-f="' + f
                + '" onchange="dtccEditCell(this)" style="width:65px"></td>';
        });
        h += '</tr>';
    });
    h += '</table></div>';
    document.getElementById('dsp').innerHTML = h;
}

function dtccEditCell(el) {
    var pair = el.getAttribute('data-pair');
    var ti = parseInt(el.getAttribute('data-ti'));
    var f = el.getAttribute('data-f');
    var v = parseFloat(el.value);
    if (!isNaN(v) && mktSurfaces[pair] && mktSurfaces[pair].tenors[ti]) {
        mktSurfaces[pair].tenors[ti][f] = v;
    }
}

function publishDtccEdits() {
    saveMarks();
    dtccEditMode = false;
    var btn = document.getElementById('dtcc-edit-btn');
    var pub = document.getElementById('dtcc-publish-btn');
    btn.className = 'btn-toggle'; btn.style.background = ''; btn.style.color = '#e65100';
    if (pub) pub.style.display = 'none';
    // Clear the edit table and restore Plotly div
    document.getElementById('dsp').innerHTML = '';
    Plotly.purge('dsp');
    dBuildSurf(); dRA();
    var st = document.getElementById('mkt-status');
    if (st) st.textContent = 'Surface edits published at ' + new Date().toLocaleTimeString();
}

// === SAVE / LOAD MARKS ===
// Inject Save Marks button next to Bloomberg button
(function injectSaveBtn() {
    var bbgBtn = document.getElementById('bbg-btn');
    if (!bbgBtn) { setTimeout(injectSaveBtn, 500); return; }
    if (document.getElementById('save-marks-btn')) return;
    var btn = document.createElement('button');
    btn.id = 'save-marks-btn';
    btn.className = 'btn';
    btn.style.cssText = 'background:#2e7d32;color:white;font-size:13px;padding:8px 18px;border-radius:6px;white-space:nowrap;margin-left:8px';
    btn.textContent = '\uD83D\uDCBE Save Marks';
    btn.onclick = saveMarks;
    bbgBtn.parentNode.insertBefore(btn, bbgBtn.nextSibling);
})();

function saveMarks() {
    var btn = document.getElementById('save-marks-btn');
    var st = document.getElementById('mkt-status');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; btn.style.opacity = '0.6'; }
    var output = {source: 'dashboard_marks', timestamp: new Date().toISOString(), surfaces: {}};
    Object.keys(mktSurfaces).forEach(function(pair) {
        var pd = mktSurfaces[pair];
        if (!pd || !pd.tenors) return;
        output.surfaces[pair] = {
            spot: pd.spot, r_d: pd.r_d, r_f: pd.r_f,
            tenors: pd.tenors.map(function(t) {
                return {tenor: t.tenor, T: t.T, atm: t.atm, rr25: t.rr25, rr10: t.rr10,
                        fly25: t.fly25, fly10: t.fly10, fwdPts: t.fwdPts};
            })
        };
    });
    fetch('/api/save_marks', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(output)
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (st) st.textContent = 'Marks saved (' + Object.keys(output.surfaces).length + ' pairs) at ' + new Date().toLocaleTimeString();
        if (btn) { btn.disabled = false; btn.textContent = '\uD83D\uDCBE Save Marks'; btn.style.opacity = '1'; }
    })
    .catch(function(err) {
        if (st) st.textContent = 'Save failed: ' + err.message;
        if (btn) { btn.disabled = false; btn.textContent = '\uD83D\uDCBE Save Marks'; btn.style.opacity = '1'; }
    });
}

// Auto-load saved marks on startup
(function loadSavedMarks() {
    fetch('/api/load_marks')
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var surfs = data.surfaces || {};
        var count = 0;
        Object.keys(surfs).forEach(function(pair) {
            var s = surfs[pair];
            if (!s.spot && !s.tenors) return;
            mktSurfaces[pair] = {spot: s.spot || 1.0, r_d: s.r_d || 0.04, r_f: s.r_f || 0.02, _pair: pair, tenors: []};
            var tnList = s.tenors || [];
            TENORS.forEach(function(tn, ti) {
                var src = tnList.find(function(t) { return t.tenor === tn; }) || {};
                mktSurfaces[pair].tenors.push({
                    tenor: tn, T: TENOR_T[ti], atm: src.atm || 0, rr25: src.rr25 || 0,
                    rr10: src.rr10 || 0, fly25: src.fly25 || 0, fly10: src.fly10 || 0, fwdPts: src.fwdPts || 0
                });
            });
            count++;
        });
        if (count > 0) {
            activeMktPair = Object.keys(mktSurfaces)[0];
            renderMktTabs(); renderMktContent();
            var st = document.getElementById('mkt-status');
            if (st) st.textContent = 'Loaded ' + count + ' saved marks';
            console.log('Loaded ' + count + ' saved marks from server');
        }
    })
    .catch(function() { console.log('No saved marks found'); });
})();

// === TOAST ALERTS for trades > 1 vol off marks ===
(function injectToastCSS() {
    var style = document.createElement('style');
    style.textContent = '#toast-container{position:fixed;top:60px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none;max-width:350px}'
        + '.toast{background:#2d2d2d;border-left:4px solid #ef5350;border-radius:6px;padding:10px 14px;color:#e0e0e0;font-size:12px;box-shadow:0 4px 12px rgba(0,0,0,0.5);pointer-events:auto;animation:toastIn 0.3s ease-out;opacity:1;transition:opacity 0.5s}'
        + '.toast.warn{border-left-color:#ffa726}'
        + '.toast .toast-pair{font-weight:700;color:#90caf9;margin-right:6px}'
        + '.toast .toast-vol{font-weight:700}'
        + '.toast .toast-vol.rich{color:#ef5350}.toast .toast-vol.cheap{color:#42a5f5}'
        + '@keyframes toastIn{from{transform:translateX(100px);opacity:0}to{transform:translateX(0);opacity:1}}';
    document.head.appendChild(style);
    var c = document.createElement('div');
    c.id = 'toast-container';
    document.body.appendChild(c);
})();

var _toastSeen = {};  // track dissem IDs to avoid repeat toasts
function showToast(html, ttl) {
    var c = document.getElementById('toast-container');
    if (!c) return;
    var t = document.createElement('div');
    t.className = 'toast';
    t.innerHTML = html;
    c.appendChild(t);
    setTimeout(function() { t.style.opacity = '0'; setTimeout(function() { if (t.parentNode) t.parentNode.removeChild(t); }, 500); }, ttl || 8000);
    // Keep max 5 toasts
    while (c.children.length > 5) c.removeChild(c.firstChild);
}

// Wrap dProcTrades to detect new alerts
var _dProcTradesForToast = typeof dProcTrades === 'function' ? dProcTrades : null;
dProcTrades = function(trades) {
    _dProcTradesForToast(trades);
    // Check for trades with large vol diff from marks
    trades.forEach(function(t) {
        var did = t._did || t.dissem_id || (t.pair + t.strike + t.time);
        if (_toastSeen[did]) return;
        var pk = DPA[t.pair]; if (!pk) return;
        var pd = mktSurfaces[pk]; if (!pd || !pd.tenors) return;
        var days = parseInt(t.days) || 0; if (days <= 0) return;
        // Find matching trade in dAT to get its IV
        var matches = (dAT[pk] || []).filter(function(at) {
            return Math.abs(at.strike - parseFloat(t.strike)) < 0.001 && at.time === t.time;
        });
        if (!matches.length) return;
        var mt = matches[0];
        // Find closest tenor mark
        var tnrs = pd.tenors, bestTi = 0, bestDist = 9999;
        for (var ti = 0; ti < tnrs.length; ti++) {
            var dist = Math.abs(tnrs[ti].T * 365 - days);
            if (dist < bestDist) { bestDist = dist; bestTi = ti; }
        }
        var base = dtccGetBase(pk, tnrs[bestTi].tenor);
        if (!base) return;
        var di = dDB(mt.delta);
        var markVol = base.vols[di];
        var diff = mt.iv - markVol;
        if (Math.abs(diff) >= 1.0) {
            _toastSeen[did] = true;
            var dir = diff > 0 ? 'RICH' : 'CHEAP';
            var cls = diff > 0 ? 'rich' : 'cheap';
            var kTxt = mt.strike >= 1000 ? mt.strike.toFixed(0) : mt.strike < 10 ? mt.strike.toFixed(4) : mt.strike.toFixed(2);
            var notlR = Math.round(mt.notl * 2) / 2;
            showToast(
                '<span class="toast-pair">' + pk + '</span>'
                + mt.type + ' ' + kTxt + ' ' + (mt.expiry || '').slice(5)
                + ' $' + notlR.toFixed(1) + 'M'
                + '<br><span class="toast-vol ' + cls + '">'
                + mt.iv.toFixed(1) + '% (' + (diff > 0 ? '+' : '') + diff.toFixed(1) + ' ' + dir + ')</span>'
                + ' vs mark ' + markVol.toFixed(1) + '%',
                10000
            );
        }
    });
};

// === TOAST ALERTS for trades >1 vol off marks ===
(function initToasts() {
    // Inject toast container CSS
    var style = document.createElement('style');
    style.textContent = '#toast-container{position:fixed;top:60px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:6px;max-height:50vh;overflow-y:auto;pointer-events:none}'
        + '.toast{pointer-events:auto;background:#3d3d3d;border-left:4px solid #ef5350;border-radius:4px;padding:8px 14px;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-size:12px;color:#e0e0e0;min-width:260px;max-width:360px;animation:toastIn 0.3s ease;cursor:pointer}'
        + '.toast:hover{background:#4a4a4a}'
        + '.toast .t-pair{font-weight:700;color:#90caf9}.toast .t-vol{font-weight:700}.toast .t-rich{color:#ef5350}.toast .t-cheap{color:#42a5f5}'
        + '@keyframes toastIn{from{opacity:0;transform:translateX(80px)}to{opacity:1;transform:translateX(0)}}';
    document.head.appendChild(style);
    // Create container
    var c = document.createElement('div');
    c.id = 'toast-container';
    document.body.appendChild(c);
})();

var _lastToastTrades = {};  // track by _did to avoid re-alerting
function showTradeToast(pair, trade, diff, markVol) {
    var did = trade.time + trade.strike;
    if (_lastToastTrades[did]) return;
    _lastToastTrades[did] = true;
    var container = document.getElementById('toast-container');
    if (!container) return;
    var dir = diff > 0 ? 'RICH' : 'CHEAP';
    var cls = diff > 0 ? 't-rich' : 't-cheap';
    var kTxt = trade.strike >= 1000 ? trade.strike.toFixed(0) : trade.strike < 10 ? trade.strike.toFixed(4) : trade.strike.toFixed(2);
    var toast = document.createElement('div');
    toast.className = 'toast';
    toast.innerHTML = '<span class="t-pair">' + pair + '</span> '
        + '<span class="' + cls + '">' + dir + ' ' + (diff > 0 ? '+' : '') + diff.toFixed(1) + 'v</span><br>'
        + '<span style="color:#aaa">K=' + kTxt + ' ' + trade.type + ' '
        + trade.iv.toFixed(1) + '% (mark ' + markVol.toFixed(1) + '%) $' + trade.notl.toFixed(0) + 'M '
        + trade.time + '</span>';
    toast.onclick = function() { toast.remove(); };
    container.appendChild(toast);
    // Auto-remove after 12 seconds
    setTimeout(function() { if (toast.parentNode) toast.remove(); }, 12000);
    // Keep max 8 toasts
    while (container.children.length > 8) container.removeChild(container.firstChild);
}

// Hook into dProcTrades to fire toasts for big deviations
var _origDProcTradesForToast = typeof dProcTrades === 'function' ? dProcTrades : null;
if (_origDProcTradesForToast) {
    var _wrappedDProcTrades = dProcTrades;
    dProcTrades = function(trades) {
        _wrappedDProcTrades(trades);
        // After processing, check all trades for big diffs and toast
        var allPairs = DTCC_G10.concat(DTCC_EM);
        allPairs.forEach(function(pk) {
            var tr = dAT[pk] || [];
            if (!tr.length || !mktSurfaces[pk] || !mktSurfaces[pk].tenors) return;
            tr.forEach(function(t) {
                var tnrs = mktSurfaces[pk].tenors, bestTi = 0, bestDist = 9999;
                for (var ti = 0; ti < tnrs.length; ti++) {
                    var dist = Math.abs(tnrs[ti].T * 365 - t.days);
                    if (dist < bestDist) { bestDist = dist; bestTi = ti; }
                }
                var base = dtccGetBase(pk, tnrs[bestTi].tenor);
                if (!base) return;
                var di = dDB(t.delta);
                var markVol = base.vols[di];
                var diff = t.iv - markVol;
                if (Math.abs(diff) >= 1.0) {
                    showTradeToast(pk, t, diff, markVol);
                }
            });
        });
    };
}

console.log('mathfix.js: all overrides applied');
"""


# ==============================================================
# HTML PATCHER
# ==============================================================
def patch_html(filepath):
    if not os.path.exists(filepath):
        return False
    with open(filepath, 'r', encoding='utf-8') as f:
        html = f.read()
    changed = False

    # 1. Bloomberg button
    if 'bbg-btn' not in html:
        log("Patching: injecting Bloomberg button...")
        marker = "DTCC Live Surface</button>"
        idx = html.find(marker)
        if idx >= 0:
            close_div = html.find("</div>", idx + len(marker))
            if close_div >= 0:
                inject = (
                    '</div>\n'
                    '<button class="btn" onclick="refreshBbg()" id="bbg-btn" '
                    'style="background:#e65100;color:white;font-size:13px;'
                    'padding:8px 18px;border-radius:6px;white-space:nowrap">'
                    '&#128202; Refresh Bloomberg</button>\n'
                    '<span style="font-size:11px;color:#888;white-space:nowrap" '
                    'id="mkt-status"></span>\n'
                )
                html = html[:close_div] + inject + html[close_div + len("</div>"):]
                changed = True

    # 2. refreshBbg JS function
    if 'function refreshBbg' not in html:
        log("Patching: injecting refreshBbg()...")
        bbg_js = """
function refreshBbg(){
    var btn=document.getElementById('bbg-btn'),st=document.getElementById('mkt-status');
    if(!btn)return;
    var pairs=Object.keys(mktSurfaces).join(',');
    btn.disabled=true;btn.textContent='Pulling Bloomberg...';btn.style.opacity='0.6';
    if(st)st.textContent='Connecting...';
    fetch('/api/bbg_refresh?pairs='+encodeURIComponent(pairs))
    .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .then(function(data){
        if(data.error){if(st)st.textContent='BBG: '+data.error;btn.disabled=false;btn.textContent='Refresh Bloomberg';btn.style.opacity='1';return;}
        var surfs=data.surfaces||{},count=0;
        Object.keys(surfs).forEach(function(pair){
            var s=surfs[pair];if(!s.spot&&!s.tenors)return;
            mktSurfaces[pair]={spot:s.spot||1.0,r_d:s.r_d||0.04,r_f:s.r_f||0.02,_pair:pair,tenors:[]};
            var tnList=s.tenors||[];
            TENORS.forEach(function(tn,ti){
                var src=tnList.find(function(t){return t.tenor===tn;})||{};
                mktSurfaces[pair].tenors.push({tenor:tn,T:TENOR_T[ti],atm:src.atm||0,rr25:src.rr25||0,rr10:src.rr10||0,fly25:src.fly25||0,fly10:src.fly10||0,fwdPts:src.fwdPts||0});
            });count++;
        });
        if(count>0){activeMktPair=Object.keys(mktSurfaces)[0];renderMktTabs();renderMktContent();}
        if(st)st.textContent='Bloomberg: '+count+' pairs at '+new Date().toLocaleTimeString();
        btn.disabled=false;btn.textContent='Refresh Bloomberg';btn.style.opacity='1';
    }).catch(function(err){
        if(st)st.textContent='BBG: '+err.message;
        btn.disabled=false;btn.textContent='Refresh Bloomberg';btn.style.opacity='1';
    });
}
"""
        inject_point = html.find("initApp();")
        if inject_point < 0:
            inject_point = html.rfind("</script>")
        if inject_point >= 0:
            html = html[:inject_point] + bbg_js + "\n" + html[inject_point:]
            changed = True

    # 3. Wrap tab-bar in flex container for button layout
    if 'bbg-btn' in html and '<div class="tab-bar">' in html and 'margin-bottom:0;flex:1' not in html:
        log("Patching: wrapping tab bar in flex layout...")
        html = html.replace(
            '<div class="tab-bar">',
            '<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">\n<div class="tab-bar" style="margin-bottom:0;flex:1">',
            1)
        changed = True

    # 4. Inject mathfix.js script tag (the key fix)
    if 'mathfix.js' not in html:
        log("Patching: injecting mathfix.js script tag...")
        idx = html.rfind("</body>")
        if idx >= 0:
            html = html[:idx] + '<script src="/api/mathfix.js"></script>\n' + html[idx:]
            changed = True

    # 5. Build stamp
    if SCRIPT_VER not in html:
        # Remove old stamp if present
        import re
        html = re.sub(r'var PATCHED_BY_SERVER="[^"]*";console\.log\([^)]*\);\n?', '', html)
        stamp = f'{SCRIPT_VER} {time.strftime("%Y-%m-%d %H:%M")}'
        idx = html.find("initApp();")
        if idx >= 0:
            html = html[:idx] + f'\nvar PATCHED_BY_SERVER="{stamp}";console.log("Patched by serve_dashboard.py",PATCHED_BY_SERVER);\n' + html[idx:]
            changed = True

    if changed:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)
        log(f"HTML patched: {SCRIPT_VER}")
    else:
        log("HTML up to date")
    return changed


# ==============================================================
# DTCC ENDPOINTS & PAIR MAP
# ==============================================================
DTCC_TICKER = "https://pddata.dtcc.com/ppd/api/ticker/CFTC/FOREIGNEXCHANGE"
DTCC_SLICES = "https://pddata.dtcc.com/ppd/api/slice/CFTC/FX"
S3_BASE = "https://kgc0418-tdw-data-0.s3.amazonaws.com"
S3_SLICE = S3_BASE + "/cftc/slices/{fileName}"
S3_CUMUL = S3_BASE + "/cftc/eod/CFTC_CUMULATIVE_FOREX_{date}.zip"

PAIR_MAP = {}
for _a, _b, _m in [
    ('EUR','USD','EUR/USD'),('GBP','USD','GBP/USD'),('AUD','USD','AUD/USD'),('NZD','USD','NZD/USD'),
    ('USD','JPY','USD/JPY'),('USD','CHF','USD/CHF'),('USD','CAD','USD/CAD'),
    ('USD','SEK','USD/SEK'),('USD','NOK','USD/NOK'),
    ('EUR','GBP','EUR/GBP'),('EUR','JPY','EUR/JPY'),('GBP','JPY','GBP/JPY'),
    ('EUR','CHF','EUR/CHF'),('AUD','JPY','AUD/JPY'),('AUD','NZD','AUD/NZD'),
    ('USD','MXN','USD/MXN'),('USD','BRL','USD/BRL'),('USD','TRY','USD/TRY'),
    ('USD','ZAR','USD/ZAR'),('USD','CNH','USD/CNH'),('USD','CNY','USD/CNH'),
    ('USD','INR','USD/INR'),('USD','KRW','USD/KRW'),('USD','SGD','USD/SGD'),
    ('USD','TWD','USD/TWD'),('USD','PHP','USD/PHP'),('USD','THB','USD/THB'),
    ('USD','IDR','USD/IDR'),('USD','HKD','USD/HKD'),('USD','CLP','USD/CLP'),
    ('USD','COP','USD/COP'),('USD','PEN','USD/PEN'),
    ('USD','ILS','USD/ILS'),('EUR','ILS','EUR/ILS'),
]:
    PAIR_MAP[(_a, _b)] = _m
    PAIR_MAP[(_b, _a)] = _m


# ==============================================================
# HTTP / ZIP HELPERS
# ==============================================================
def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def http_get_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def extract_csv_from_zip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith('.csv')]
        if not names: return None
        with zf.open(names[0]) as cf:
            return cf.read().decode('utf-8', errors='replace')


# ==============================================================
# TRADE PARSER
# ==============================================================
FIELD_MAP = {
    'action':      ['actionType','Action type','Action Type','Action'],
    'upi_fisn':    ['uniqueProductIdentifierShortName','UPI FISN','Unique Product Identifier Short Name'],
    'upi_under':   ['uniqueProductIdentifierUnderlierName','UPI Underlier Name','Unique Product Identifier Underlier Name'],
    'strike':      ['strikePrice','Strike Price','Strike price'],
    'expiry':      ['expirationDate','Expiration Date','Expiration date'],
    'notl1':       ['notionalAmountLeg1','Notional amount-Leg 1','Notional Amount-Leg 1'],
    'notl2':       ['notionalAmountLeg2','Notional amount-Leg 2','Notional Amount-Leg 2'],
    'ccy1':        ['notionalCurrencyLeg1','Notional currency-Leg 1','Notional Currency-Leg 1'],
    'ccy2':        ['notionalCurrencyLeg2','Notional currency-Leg 2','Notional Currency-Leg 2'],
    'call_ccy':    ['callCurrencyLeg1','Call currency','Call Currency'],
    'premium':     ['optionPremiumAmount','Option Premium Amount','Option premium amount'],
    'premium_ccy': ['optionPremiumCurrency','Option Premium Currency','Option premium currency'],
    'exec_ts':     ['executionTimestamp','Execution Timestamp','Execution timestamp'],
    'dissem_id':   ['disseminationIdentifier','Dissemination Identifier'],
    'pkg_price':   ['packageTransactionPrice','Package transaction price'],
    'pkg_notation': ['packageTransactionPriceNotation','Package transaction price notation'],
    'package_ind':  ['packageIndicator','Package Indicator','Package indicator'],
    'nonstandard':  ['nonstandardizedTermIndicator','Nonstandardized Term Indicator',
                     'Non-standardized Term Indicator','nonstandardTermIndicator'],
}

def get_field(row, name):
    for key in FIELD_MAP.get(name, []):
        val = row.get(key)
        if val is not None:
            s = str(val).strip()
            if s: return s
    return ''

def parse_float(s):
    if not s: return 0.0
    try: return float(s.replace(',','').replace('+','').strip())
    except: return 0.0

def parse_ts(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace('Z','+00:00'))
    except: return None

def parse_trade(row):
    action = get_field(row, 'action')
    if action and action not in ('NEWT','CORR'): return None
    upi = get_field(row, 'upi_fisn')
    if '/O ' not in upi: return None

    # Filter to VANILLA and NDO only — reject barriers, digitals, TARFs, etc.
    # UPI FISN format: "NA/O Van Call EUR USD", "NA/O NDO Put KRW USD",
    #                  "NA/O Bar Call EUR USD" (barrier), "NA/O Dig Put USD JPY" (digital)
    upi_upper = upi.upper()
    after_o = upi[upi.index('/O ') + 3:]  # "Van Call EUR USD"
    opt_subtype = after_o.split()[0].upper() if after_o else ''
    if opt_subtype not in ('VAN', 'NDO'):
        return None  # barrier, digital, TARF, vol/var swap, etc.

    # Reject package trades — premium is for the whole structure, not this leg
    pkg_ind = get_field(row, 'package_ind').upper()
    is_package = pkg_ind in ('TRUE', 'Y', 'YES', '1')

    # Reject non-standardized terms (exotic features)
    ns_ind = get_field(row, 'nonstandard').upper()
    if ns_ind in ('TRUE', 'Y', 'YES', '1'):
        return None

    upi_under = get_field(row, 'upi_under')
    pair = None
    if upi_under:
        parts = upi_under.split()
        if len(parts) >= 2: pair = PAIR_MAP.get((parts[0].upper(), parts[1].upper()))
    if not pair:
        c1, c2 = get_field(row,'ccy1')[:3].upper(), get_field(row,'ccy2')[:3].upper()
        if c1 and c2: pair = PAIR_MAP.get((c1, c2))
    if not pair: return None
    strike = parse_float(get_field(row, 'strike'))
    if strike <= 0: return None
    exp_raw = get_field(row, 'expiry')
    try:
        exp = datetime.strptime(exp_raw[:10], '%Y-%m-%d')
        days = max(1, (exp - datetime.now()).days)
        if days <= 0 or days > 1500: return None
    except: return None
    n1, n2 = parse_float(get_field(row,'notl1')), parse_float(get_field(row,'notl2'))
    c1 = get_field(row,'ccy1')[:3].upper()
    usd = (n1 if c1=='USD' else n2 if n2>0 else n1) / 1e6
    if usd <= 0: return None
    upi_up = upi.upper()
    opt_type = 'CALL' if 'CALL' in upi_up else ('PUT' if 'PUT' in upi_up else ('CALL' if get_field(row,'call_ccy').upper()==pair[:3] else 'PUT'))
    prem = parse_float(get_field(row, 'premium'))
    prem_ccy = get_field(row, 'premium_ccy')[:3].upper()
    # Figure out base notional (for correct B76 price conversion)
    # pair format is "EUR/USD" -> base=EUR, terms=USD
    base_ccy = pair[:3]
    terms_ccy = pair[4:]
    nc1 = get_field(row,'ccy1')[:3].upper()
    nc2 = get_field(row,'ccy2')[:3].upper()
    # base_notl = notional in base currency
    if nc1 == base_ccy:
        base_notl = n1
    elif nc2 == base_ccy:
        base_notl = n2
    else:
        base_notl = n1  # fallback
    iv = ''
    pkg_p, pkg_n = get_field(row,'pkg_price'), get_field(row,'pkg_notation')
    if pkg_n == '3' and pkg_p:
        try:
            v = float(pkg_p)
            if 0.5 < v < 80: iv = v
        except: pass
    ts = parse_ts(get_field(row, 'exec_ts'))
    return {
        'pair':pair,'spot':0,'strike':strike,'fwd_rate':0,'fwd_pts':'','opt_type':opt_type,
        'days':days,'premium':prem,'premium_ccy':prem_ccy,'base_notl':base_notl,
        'base_ccy':base_ccy,'terms_ccy':terms_ccy,'usd_amt':usd,'iv':iv,
        'is_package': is_package,
        'time':ts.strftime('%H:%M') if ts else '','expiry':exp.strftime('%Y-%m-%d'),
        '_ts':ts.isoformat() if ts else '','_did':get_field(row,'dissem_id'),
    }


# ==============================================================
# DTCC SDR READER (background downloading)
# ==============================================================
class DTCCReader:
    def __init__(self):
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.dtcc_cache')
        os.makedirs(self.cache_dir, exist_ok=True)
        self._ticker_trades = []; self._ticker_ts = 0
        self._slice_trades = {}; self._slice_list = []; self._slice_list_ts = 0
        self._cumul_trades = {}
        self._lock = threading.Lock()
        self.stats = {'ticker_raw':0,'ticker_opts':0,'slices_available':0,'slices_downloaded':0,
                      'slices_pending':0,'slices_failed':0,'slices_opts':0,
                      'cumul_days':0,'cumul_opts':0,'total_opts':0,'bg_status':'starting'}
        threading.Thread(target=self._bg_loop, daemon=True).start()

    def _bg_loop(self):
        time.sleep(2)
        while True:
            try: self._bg_tick()
            except Exception as e: log(f"BG ERROR: {e}")
            time.sleep(3)

    def _bg_tick(self):
        if time.time() - self._ticker_ts > 30:
            try:
                data = http_get_json(DTCC_TICKER)
                raw = data.get('tradeList', [])
                opts = list(filter(None, (parse_trade(t) for t in raw)))
                with self._lock: self._ticker_trades = opts
                self._ticker_ts = time.time()
                self.stats.update(ticker_raw=len(raw), ticker_opts=len(opts))
                log(f"Ticker: {len(raw)} -> {len(opts)} opts")
            except Exception as e: log(f"Ticker ERROR: {e}")

        if time.time() - self._slice_list_ts > 45:
            try:
                self._slice_list = http_get_json(DTCC_SLICES)
                self._slice_list_ts = time.time()
                pending = sum(1 for sl in self._slice_list if sl.get('sliceId') and sl['sliceId'] not in self._slice_trades)
                self.stats.update(slices_available=len(self._slice_list), slices_pending=pending)
                log(f"Slices: {len(self._slice_list)} available, {pending} to download")
            except Exception as e: log(f"Slice list ERROR: {e}")

        ok = fail = 0
        for sl in self._slice_list:
            if ok + fail >= 10: break
            sid, fn = sl.get('sliceId'), sl.get('fileName','')
            if not sid or not fn or sid in self._slice_trades: continue
            csv_text = self._dl_slice(fn, sid)
            if csv_text is None: fail += 1; continue
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            opts = list(filter(None, (parse_trade(r) for r in rows)))
            with self._lock: self._slice_trades[sid] = opts
            ok += 1; self.stats['slices_downloaded'] += 1; self.stats['slices_opts'] += len(opts)
        if ok or fail:
            self.stats['slices_failed'] += fail
            pending = sum(1 for sl in self._slice_list if sl.get('sliceId') and sl['sliceId'] not in self._slice_trades)
            self.stats['slices_pending'] = pending
            self.stats['bg_status'] = f"{pending} pending" if pending else "up to date"
            log(f"Slices: +{ok} OK, {fail} fail | {len(self._slice_trades)} cached, {self.stats['slices_opts']} opts, {pending} pending")

    def _dl_slice(self, fn, sid):
        disk = os.path.join(self.cache_dir, f'slice_{sid}.csv')
        if os.path.exists(disk):
            with open(disk, 'r') as f: return f.read()
        try:
            zdata = http_get_bytes(S3_SLICE.format(fileName=fn), timeout=20)
            csv_text = extract_csv_from_zip(zdata)
            if csv_text:
                with open(disk, 'w') as f: f.write(csv_text)
                return csv_text
        except: pass
        return None

    def _load_cumulative(self, cutoff):
        today = datetime.now(timezone.utc).date()
        d = cutoff.date()
        while d < today:
            ds = d.strftime('%Y_%m_%d')
            if ds not in self._cumul_trades: self._dl_cumul(ds)
            d += timedelta(days=1)

    def _dl_cumul(self, ds):
        disk = os.path.join(self.cache_dir, f'cumul_{ds}.json')
        if os.path.exists(disk):
            try:
                with open(disk, 'r') as f: self._cumul_trades[ds] = json.load(f)
                self.stats['cumul_days'] += 1; self.stats['cumul_opts'] += len(self._cumul_trades[ds])
                log(f"Cumul {ds}: {len(self._cumul_trades[ds])} opts (cached)"); return
            except: pass
        log(f"Cumul {ds}: downloading...")
        try:
            zdata = http_get_bytes(S3_CUMUL.format(date=ds), timeout=60)
            csv_text = extract_csv_from_zip(zdata)
            if not csv_text: self._cumul_trades[ds] = []; return
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            opts = list(filter(None, (parse_trade(r) for r in rows)))
            self._cumul_trades[ds] = opts
            self.stats['cumul_days'] += 1; self.stats['cumul_opts'] += len(opts)
            log(f"Cumul {ds}: {len(rows)} rows, {len(opts)} opts")
            try:
                with open(disk, 'w') as f: json.dump(opts, f)
            except: pass
        except Exception as e:
            log(f"Cumul {ds} ERROR: {e}"); self._cumul_trades[ds] = []

    def fetch(self, minutes=240, min_size=0, ccys=None):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=minutes)
        ccy_set = set(c.upper() for c in ccys) if ccys else None
        if minutes > 480: self._load_cumulative(cutoff)
        with self._lock:
            ticker = list(self._ticker_trades)
            slices = {k: list(v) for k, v in self._slice_trades.items()}
        seen, out = set(), []
        for src in [ticker] + list(slices.values()) + list(self._cumul_trades.values()):
            for t in src:
                did = t.get('_did','')
                if did and did in seen: continue
                if did: seen.add(did)
                ts_str = t.get('_ts','')
                if ts_str:
                    ts = parse_ts(ts_str) if isinstance(ts_str, str) else ts_str
                    if ts and ts < cutoff: continue
                if t.get('usd_amt',0) < min_size/1e6: continue
                if ccy_set:
                    p = t.get('pair','')
                    if '/' in p and not {p[:3],p[4:]}.intersection(ccy_set): continue
                out.append({k:v for k,v in t.items() if not k.startswith('_')})
        self.stats['total_opts'] = len(out)
        return {'trades':out,'count':len(out),'source':'DTCC SDR Direct','stats':self.stats.copy()}


# ==============================================================
# BLOOMBERG SURFACES + LIVE SPOTS
# ==============================================================
BBG_TENORS = [
    {'label':'O/N','bbg':'ON','bbg_fwd':'ON','T':1/365},
    {'label':'1W','bbg':'1W','bbg_fwd':'1W','T':7/365},
    {'label':'2W','bbg':'2W','bbg_fwd':'2W','T':14/365},
    {'label':'1M','bbg':'1M','bbg_fwd':'1M','T':1/12},
    {'label':'2M','bbg':'2M','bbg_fwd':'2M','T':2/12},
    {'label':'3M','bbg':'3M','bbg_fwd':'3M','T':3/12},
    {'label':'6M','bbg':'6M','bbg_fwd':'6M','T':6/12},
    {'label':'9M','bbg':'9M','bbg_fwd':'9M','T':9/12},
    {'label':'1Y','bbg':'1Y','bbg_fwd':'12M','T':1.0},
    {'label':'2Y','bbg':'2Y','bbg_fwd':'2Y','T':2.0},
]
BBG_PAIRS = {
    # G10
    'EURUSD':{'spot':'EURUSD Curncy','vol':'EURUSDV','rr25':'EURUSD25R','rr10':'EURUSD10R','fly25':'EURUSD25B','fly10':'EURUSD10B','fwd':'EUR'},
    'USDJPY':{'spot':'USDJPY Curncy','vol':'USDJPYV','rr25':'USDJPY25R','rr10':'USDJPY10R','fly25':'USDJPY25B','fly10':'USDJPY10B','fwd':'JPY'},
    'GBPUSD':{'spot':'GBPUSD Curncy','vol':'GBPUSDV','rr25':'GBPUSD25R','rr10':'GBPUSD10R','fly25':'GBPUSD25B','fly10':'GBPUSD10B','fwd':'GBP'},
    'USDCHF':{'spot':'USDCHF Curncy','vol':'USDCHFV','rr25':'USDCHF25R','rr10':'USDCHF10R','fly25':'USDCHF25B','fly10':'USDCHF10B','fwd':'CHF'},
    'AUDUSD':{'spot':'AUDUSD Curncy','vol':'AUDUSDV','rr25':'AUDUSD25R','rr10':'AUDUSD10R','fly25':'AUDUSD25B','fly10':'AUDUSD10B','fwd':'AUD'},
    'NZDUSD':{'spot':'NZDUSD Curncy','vol':'NZDUSDV','rr25':'NZDUSD25R','rr10':'NZDUSD10R','fly25':'NZDUSD25B','fly10':'NZDUSD10B','fwd':'NZD'},
    'USDCAD':{'spot':'USDCAD Curncy','vol':'USDCADV','rr25':'USDCAD25R','rr10':'USDCAD10R','fly25':'USDCAD25B','fly10':'USDCAD10B','fwd':'CAD'},
    'USDSEK':{'spot':'USDSEK Curncy','vol':'USDSEKV','rr25':'USDSEK25R','rr10':'USDSEK10R','fly25':'USDSEK25B','fly10':'USDSEK10B','fwd':'SEK'},
    'USDNOK':{'spot':'USDNOK Curncy','vol':'USDNOKV','rr25':'USDNOK25R','rr10':'USDNOK10R','fly25':'USDNOK25B','fly10':'USDNOK10B','fwd':'NOK'},
    'EURGBP':{'spot':'EURGBP Curncy','vol':'EURGBPV','rr25':'EURGBP25R','rr10':'EURGBP10R','fly25':'EURGBP25B','fly10':'EURGBP10B','fwd':'EURGBP'},
    'EURJPY':{'spot':'EURJPY Curncy','vol':'EURJPYV','rr25':'EURJPY25R','rr10':'EURJPY10R','fly25':'EURJPY25B','fly10':'EURJPY10B','fwd':'EURJPY'},
    'GBPJPY':{'spot':'GBPJPY Curncy','vol':'GBPJPYV','rr25':'GBPJPY25R','rr10':'GBPJPY10R','fly25':'GBPJPY25B','fly10':'GBPJPY10B','fwd':'GBPJPY'},
    'EURCHF':{'spot':'EURCHF Curncy','vol':'EURCHFV','rr25':'EURCHF25R','rr10':'EURCHF10R','fly25':'EURCHF25B','fly10':'EURCHF10B','fwd':'EURCHF'},
    'AUDJPY':{'spot':'AUDJPY Curncy','vol':'AUDJPYV','rr25':'AUDJPY25R','rr10':'AUDJPY10R','fly25':'AUDJPY25B','fly10':'AUDJPY10B','fwd':'AUDJPY'},
    # EM
    'USDMXN':{'spot':'USDMXN Curncy','vol':'USDMXNV','rr25':'USDMXN25R','rr10':'USDMXN10R','fly25':'USDMXN25B','fly10':'USDMXN10B','fwd':'MXN'},
    'USDBRL':{'spot':'USDBRL Curncy','vol':'USDBRLV','rr25':'USDBRL25R','rr10':'USDBRL10R','fly25':'USDBRL25B','fly10':'USDBRL10B','fwd':'BRL'},
    'USDTRY':{'spot':'USDTRY Curncy','vol':'USDTRYV','rr25':'USDTRY25R','rr10':'USDTRY10R','fly25':'USDTRY25B','fly10':'USDTRY10B','fwd':'TRY'},
    'USDZAR':{'spot':'USDZAR Curncy','vol':'USDZARV','rr25':'USDZAR25R','rr10':'USDZAR10R','fly25':'USDZAR25B','fly10':'USDZAR10B','fwd':'ZAR'},
    'USDCNH':{'spot':'USDCNH Curncy','vol':'USDCNHV','rr25':'USDCNH25R','rr10':'USDCNH10R','fly25':'USDCNH25B','fly10':'USDCNH10B','fwd':'CNH'},
    'USDINR':{'spot':'USDINR Curncy','vol':'USDINRV','rr25':'USDINR25R','rr10':'USDINR10R','fly25':'USDINR25B','fly10':'USDINR10B','fwd':'INR'},
    'USDKRW':{'spot':'USDKRW Curncy','vol':'USDKRWV','rr25':'USDKRW25R','rr10':'USDKRW10R','fly25':'USDKRW25B','fly10':'USDKRW10B','fwd':'KRW'},
    'USDSGD':{'spot':'USDSGD Curncy','vol':'USDSGDV','rr25':'USDSGD25R','rr10':'USDSGD10R','fly25':'USDSGD25B','fly10':'USDSGD10B','fwd':'SGD'},
    'USDTWD':{'spot':'USDTWD Curncy','vol':'USDTWDV','rr25':'USDTWD25R','rr10':'USDTWD10R','fly25':'USDTWD25B','fly10':'USDTWD10B','fwd':'TWD'},
    'USDPHP':{'spot':'USDPHP Curncy','vol':'USDPHPV','rr25':'USDPHP25R','rr10':'USDPHP10R','fly25':'USDPHP25B','fly10':'USDPHP10B','fwd':'PHP'},
    'USDIDR':{'spot':'USDIDR Curncy','vol':'USDIDRV','rr25':'USDIDR25R','rr10':'USDIDR10R','fly25':'USDIDR25B','fly10':'USDIDR10B','fwd':'IDR'},
    'USDTHB':{'spot':'USDTHB Curncy','vol':'USDTHBV','rr25':'USDTHB25R','rr10':'USDTHB10R','fly25':'USDTHB25B','fly10':'USDTHB10B','fwd':'THB'},
    'USDHKD':{'spot':'USDHKD Curncy','vol':'USDHKDV','rr25':'USDHKD25R','rr10':'USDHKD10R','fly25':'USDHKD25B','fly10':'USDHKD10B','fwd':'HKD'},
    'AUDNZD':{'spot':'AUDNZD Curncy','vol':'AUDNZDV','rr25':'AUDNZD25R','rr10':'AUDNZD10R','fly25':'AUDNZD25B','fly10':'AUDNZD10B','fwd':'AUDNZD'},
    'USDCLP':{'spot':'USDCLP Curncy','vol':'USDCLPV','rr25':'USDCLP25R','rr10':'USDCLP10R','fly25':'USDCLP25B','fly10':'USDCLP10B','fwd':'CLP'},
    'USDCOP':{'spot':'USDCOP Curncy','vol':'USDCOPV','rr25':'USDCOP25R','rr10':'USDCOP10R','fly25':'USDCOP25B','fly10':'USDCOP10B','fwd':'COP'},
    'USDPEN':{'spot':'USDPEN Curncy','vol':'USDPENV','rr25':'USDPEN25R','rr10':'USDPEN10R','fly25':'USDPEN25B','fly10':'USDPEN10B','fwd':'PEN'},
    'USDILS':{'spot':'USDILS Curncy','vol':'USDILSV','rr25':'USDILS25R','rr10':'USDILS10R','fly25':'USDILS25B','fly10':'USDILS10B','fwd':'ILS'},
    'EURILS':{'spot':'EURILS Curncy','vol':'EURILSV','rr25':'EURILS25R','rr10':'EURILS10R','fly25':'EURILS25B','fly10':'EURILS10B','fwd':'EURILS'},
}
DEFAULT_RATES = {
    'USD':0.045,'EUR':0.025,'GBP':0.044,'JPY':0.005,'CHF':0.015,'AUD':0.035,'NZD':0.04,'CAD':0.035,
    'SEK':0.03,'NOK':0.035,'MXN':0.1,'BRL':0.12,'TRY':0.4,'ZAR':0.08,'CNH':0.025,
    'INR':0.065,'KRW':0.03,'SGD':0.03,'TWD':0.015,'IDR':0.06,'PHP':0.055,'THB':0.02,
    'ILS':0.045,'HKD':0.045,'CLP':0.06,'COP':0.1,'PEN':0.06,
}

def pull_bbg_surfaces(pairs=None):
    import math
    if not pairs: pairs = ['EURUSD','USDJPY','GBPUSD']
    try: import blpapi
    except ImportError: return {'error':'blpapi not installed','surfaces':{}}
    opts = blpapi.SessionOptions(); opts.setServerHost("localhost"); opts.setServerPort(8194)
    session = blpapi.Session(opts)
    if not session.start(): return {'error':'Cannot connect to Bloomberg','surfaces':{}}
    if not session.openService("//blp/refdata"): session.stop(); return {'error':'Cannot open refdata','surfaces':{}}
    svc = session.getService("//blp/refdata"); surfaces = {}

    # First: pull USD policy rate as anchor
    usd_rate = 0.045  # fallback
    try:
        req0 = svc.createRequest("ReferenceDataRequest")
        req0.getElement("securities").appendValue("FEDL01 Index")  # Fed Funds effective
        req0.getElement("fields").appendValue("PX_LAST")
        session.sendRequest(req0)
        while True:
            ev = session.nextEvent(3000)
            for msg in ev:
                if msg.hasElement("securityData"):
                    arr = msg.getElement("securityData")
                    for i in range(arr.numValues()):
                        flds = arr.getValueAsElement(i).getElement("fieldData")
                        if flds.hasElement("PX_LAST"):
                            usd_rate = flds.getElementAsFloat("PX_LAST") / 100  # BBG quotes as %
            if ev.eventType() == blpapi.Event.RESPONSE: break
        log(f"BBG USD rate (FEDL01): {usd_rate*100:.2f}%")
    except Exception as e:
        log(f"BBG USD rate pull failed ({e}), using {usd_rate*100:.1f}%")

    for pair in pairs:
        pair = pair.upper().replace('/','')
        cfg = BBG_PAIRS.get(pair)
        if not cfg: continue
        base, terms = pair[:3], pair[3:]
        tickers = [cfg['spot']]
        for tn in BBG_TENORS:
            b = tn['bbg']
            bf = tn['bbg_fwd']  # 1Y uses '12M' for fwd points
            tickers += [f"{cfg['vol']}{b} Curncy",f"{cfg['rr25']}{b} Curncy",f"{cfg['rr10']}{b} Curncy",
                        f"{cfg['fly25']}{b} Curncy",f"{cfg['fly10']}{b} Curncy",f"{cfg['fwd']}{bf} Curncy"]
        req = svc.createRequest("ReferenceDataRequest")
        for t in tickers: req.getElement("securities").appendValue(t)
        req.getElement("fields").appendValue("PX_LAST"); session.sendRequest(req)
        data = {}
        while True:
            ev = session.nextEvent(5000)
            for msg in ev:
                if msg.hasElement("securityData"):
                    arr = msg.getElement("securityData")
                    for i in range(arr.numValues()):
                        sec = arr.getValueAsElement(i)
                        flds = sec.getElement("fieldData")
                        if flds.hasElement("PX_LAST"):
                            data[sec.getElementAsString("security")] = flds.getElementAsFloat("PX_LAST")
            if ev.eventType() == blpapi.Event.RESPONSE: break
        spot = data.get(cfg['spot'],0)
        if spot <= 0: continue

        # Derive r_d and r_f from forward points via covered interest parity
        # F = S + fwdPts/scale  and  F = S × exp((r_d - r_f) × T)
        # Therefore: r_d - r_f = ln(F/S) / T
        # USD is always one side — use pulled USD rate as anchor
        scale = 100 if 'JPY' in pair or 'KRW' in pair or 'INR' in pair else 10000
        rate_diffs = []
        td = []
        for tn in BBG_TENORS:
            b = tn['bbg']
            bf = tn['bbg_fwd']
            atm=data.get(f"{cfg['vol']}{b} Curncy",0)
            rr25=data.get(f"{cfg['rr25']}{b} Curncy",0); rr10=data.get(f"{cfg['rr10']}{b} Curncy",0)
            fly25=data.get(f"{cfg['fly25']}{b} Curncy",0); fly10=data.get(f"{cfg['fly10']}{b} Curncy",0)
            fwdPts=data.get(f"{cfg['fwd']}{bf} Curncy",0)
            if fly10==0 and fly25!=0: fly10=fly25*3.6
            if rr10==0 and rr25!=0: rr10=rr25*1.925
            if atm>0:
                td.append({'tenor':tn['label'],'T':round(tn['T'],6),'atm':round(atm,4),
                    'rr25':round(rr25,4),'rr10':round(rr10,4),'fly25':round(fly25,4),
                    'fly10':round(fly10,4),'fwdPts':round(fwdPts,4)})
            # Collect rate diff from tenors >= 1M for stability
            if fwdPts != 0 and tn['T'] >= 1/12:
                fwd = spot + fwdPts / scale
                if fwd > 0:
                    rate_diffs.append(math.log(fwd / spot) / tn['T'])

        # Compute r_d and r_f
        if rate_diffs:
            avg_diff = sum(rate_diffs) / len(rate_diffs)  # r_d - r_f
        else:
            avg_diff = 0

        if terms == 'USD':
            # e.g. EURUSD: terms=USD, base=EUR. r_d = USD rate, r_f = EUR rate
            r_d = usd_rate
            r_f = r_d - avg_diff
        elif base == 'USD':
            # e.g. USDJPY: base=USD, terms=JPY. r_f = USD rate, r_d = JPY rate
            r_f = usd_rate
            r_d = r_f + avg_diff
        else:
            # Cross pair (e.g. EURGBP) — use DEFAULT_RATES as fallback
            r_d = DEFAULT_RATES.get(terms, 0.04)
            r_f = DEFAULT_RATES.get(base, 0.03)

        r_d = max(0, min(0.5, r_d))  # clamp to reasonable range
        r_f = max(0, min(0.5, r_f))

        if td:
            surfaces[pair] = {'spot':round(spot,6),'r_d':round(r_d,5),'r_f':round(r_f,5),'tenors':td}
            log(f"BBG {pair}: spot={spot:.4f}, r_d={r_d*100:.2f}%, r_f={r_f*100:.2f}%, {len(td)} tenors")

    session.stop()
    return {'source':'Bloomberg','timestamp':datetime.now().isoformat(),'surfaces':surfaces}


def get_bbg_hist_spot(pair, date_str, time_str=''):
    """Get historical spot from Bloomberg for a pair at a given date/time."""
    try:
        import blpapi
    except ImportError:
        return {'error': 'blpapi not installed', 'spot': 0}
    
    ticker = f"{pair} Curncy"
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost"); opts.setServerPort(8194)
    session = blpapi.Session(opts)
    if not session.start():
        return {'error': 'Cannot connect to Bloomberg', 'spot': 0}
    if not session.openService("//blp/refdata"):
        session.stop()
        return {'error': 'Cannot open refdata', 'spot': 0}
    
    svc = session.getService("//blp/refdata")
    
    if time_str:
        # Intraday: use IntradayBarRequest for the specific time
        req = svc.createRequest("IntradayBarRequest")
        req.set("security", ticker)
        req.set("eventType", "TRADE")
        req.set("interval", 1)  # 1 minute bars
        # Parse date and time
        dt_start = f"{date_str}T{time_str}:00"
        dt_end = f"{date_str}T{time_str}:59"
        req.set("startDateTime", dt_start)
        req.set("endDateTime", dt_end)
        session.sendRequest(req)
        spot = 0
        while True:
            ev = session.nextEvent(5000)
            for msg in ev:
                if msg.hasElement("barData"):
                    bd = msg.getElement("barData")
                    if bd.hasElement("barTickData"):
                        bars = bd.getElement("barTickData")
                        if bars.numValues() > 0:
                            last_bar = bars.getValueAsElement(bars.numValues() - 1)
                            if last_bar.hasElement("close"):
                                spot = last_bar.getElementAsFloat("close")
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
        session.stop()
        return {'pair': pair, 'date': date_str, 'time': time_str, 'spot': spot}
    else:
        # End of day: use HistoricalDataRequest
        req = svc.createRequest("HistoricalDataRequest")
        req.getElement("securities").appendValue(ticker)
        req.getElement("fields").appendValue("PX_LAST")
        req.set("startDate", date_str.replace('-', ''))
        req.set("endDate", date_str.replace('-', ''))
        session.sendRequest(req)
        spot = 0
        while True:
            ev = session.nextEvent(5000)
            for msg in ev:
                if msg.hasElement("securityData"):
                    sd = msg.getElement("securityData")
                    if sd.hasElement("fieldData"):
                        fd = sd.getElement("fieldData")
                        if fd.numValues() > 0:
                            row = fd.getValueAsElement(0)
                            if row.hasElement("PX_LAST"):
                                spot = row.getElementAsFloat("PX_LAST")
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
        session.stop()
        return {'pair': pair, 'date': date_str, 'spot': spot}


def fetch_bbg_hist_spots(pair, dates_str='', date_str='', time_str=''):
    """Fetch historical spot prices from Bloomberg.
    Can request:
      - Multiple dates: dates=2026-03-26,2026-03-25 (closing prices)
      - Single date+time: date=2026-03-26&time=14:30 (intraday)
    Returns {spots: {datetime_str: price, ...}}
    """
    try:
        import blpapi
    except ImportError:
        return {'error': 'blpapi not installed', 'spots': {}}

    ticker = f"{pair} Curncy"
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost"); opts.setServerPort(8194)
    session = blpapi.Session(opts)
    if not session.start():
        return {'error': 'Cannot connect to Bloomberg', 'spots': {}}
    if not session.openService("//blp/refdata"):
        session.stop()
        return {'error': 'Cannot open refdata', 'spots': {}}

    svc = session.getService("//blp/refdata")
    spots = {}

    if dates_str:
        # Multiple dates — use HistoricalDataRequest
        date_list = [d.strip() for d in dates_str.split(',') if d.strip()]
        if not date_list:
            session.stop()
            return {'error': 'No dates provided', 'spots': {}}

        req = svc.createRequest("HistoricalDataRequest")
        req.getElement("securities").appendValue(ticker)
        req.getElement("fields").appendValue("PX_LAST")
        req.set("startDate", min(date_list).replace('-', ''))
        req.set("endDate", max(date_list).replace('-', ''))
        req.set("periodicitySelection", "DAILY")
        session.sendRequest(req)

        while True:
            ev = session.nextEvent(5000)
            for msg in ev:
                if msg.hasElement("securityData"):
                    sd = msg.getElement("securityData")
                    if sd.hasElement("fieldData"):
                        fd = sd.getElement("fieldData")
                        for i in range(fd.numValues()):
                            row = fd.getValueAsElement(i)
                            if row.hasElement("date") and row.hasElement("PX_LAST"):
                                dt = str(row.getElementAsString("date"))
                                px = row.getElementAsFloat("PX_LAST")
                                spots[dt] = px
            if ev.eventType() == blpapi.Event.RESPONSE:
                break

    elif date_str:
        # Single date — get closing price via HistoricalDataRequest
        req = svc.createRequest("HistoricalDataRequest")
        req.getElement("securities").appendValue(ticker)
        req.getElement("fields").appendValue("PX_LAST")
        req.set("startDate", date_str.replace('-', ''))
        req.set("endDate", date_str.replace('-', ''))
        req.set("periodicitySelection", "DAILY")
        session.sendRequest(req)

        while True:
            ev = session.nextEvent(5000)
            for msg in ev:
                if msg.hasElement("securityData"):
                    sd = msg.getElement("securityData")
                    if sd.hasElement("fieldData"):
                        fd = sd.getElement("fieldData")
                        for i in range(fd.numValues()):
                            row = fd.getValueAsElement(i)
                            if row.hasElement("PX_LAST"):
                                spots[date_str] = row.getElementAsFloat("PX_LAST")
            if ev.eventType() == blpapi.Event.RESPONSE:
                break

    session.stop()
    log(f"BBG hist spot {pair}: {len(spots)} data points")
    return {'pair': pair, 'spots': spots}


class BBGSpotStreamer:
    """Background Bloomberg: live spots + fwd points + derived deposit rates."""

    ALL_FX_PAIRS = [
        'EURUSD','USDJPY','GBPUSD','USDCHF','AUDUSD','NZDUSD','USDCAD',
        'USDSEK','USDNOK','EURGBP','EURJPY','GBPJPY','EURCHF','AUDJPY','AUDNZD',
        'USDMXN','USDBRL','USDTRY','USDZAR','USDCNH','USDINR','USDKRW',
        'USDSGD','USDTWD','USDPHP','USDTHB','USDIDR','USDHKD',
        'USDCLP','USDCOP','USDPEN',
        'USDILS','EURILS',
    ]

    # Forward point ticker prefix: {prefix}{tenor} Curncy
    # USD/XXX: use terms ccy, XXX/USD: use base ccy, crosses: use full pair
    FWD_PREFIX = {
        'EURUSD':'EUR','USDJPY':'JPY','GBPUSD':'GBP','USDCHF':'CHF',
        'AUDUSD':'AUD','NZDUSD':'NZD','USDCAD':'CAD',
        'USDSEK':'SEK','USDNOK':'NOK',
        'EURGBP':'EURGBP','EURJPY':'EURJPY','GBPJPY':'GBPJPY',
        'EURCHF':'EURCHF','AUDJPY':'AUDJPY','AUDNZD':'AUDNZD',
        'USDMXN':'MXN','USDBRL':'BRL','USDTRY':'TRY','USDZAR':'ZAR',
        'USDCNH':'CNH','USDINR':'INR','USDKRW':'KRW',
        'USDSGD':'SGD','USDTWD':'TWD','USDPHP':'PHP','USDTHB':'THB',
        'USDIDR':'IDR','USDHKD':'HKD',
        'USDCLP':'CLP','USDCOP':'COP','USDPEN':'PEN',
        'USDILS':'ILS','EURILS':'EURILS',
    }

    # Tenors to pull fwd points for
    FWD_TENORS = [
        {'label':'O/N','bbg':'ON','T':1/365},
        {'label':'1W','bbg':'1W','T':7/365},
        {'label':'2W','bbg':'2W','T':14/365},
        {'label':'1M','bbg':'1M','T':1/12},
        {'label':'2M','bbg':'2M','T':2/12},
        {'label':'3M','bbg':'3M','T':3/12},
        {'label':'6M','bbg':'6M','T':6/12},
        {'label':'9M','bbg':'9M','T':9/12},
        {'label':'1Y','bbg':'1Y','T':1.0},
        {'label':'2Y','bbg':'2Y','T':2.0},
    ]

    def __init__(self):
        self.spots = {}       # pair -> spot
        self.fwd_pts = {}     # pair -> {tenor_label: pts}
        self.rates = {}       # pair -> {r_d, r_f}
        self._running = False

    def start(self):
        try:
            import blpapi
            self._blpapi = blpapi
        except ImportError:
            log("BBG: blpapi not installed, will use static marks only")
            return False
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        import math
        blpapi = self._blpapi
        opts = blpapi.SessionOptions()
        opts.setServerHost("localhost"); opts.setServerPort(8194)
        session = blpapi.Session(opts)
        if not session.start():
            log("BBG: cannot connect"); self._running = False; return
        if not session.openService("//blp/refdata"):
            log("BBG: cannot open refdata"); session.stop(); self._running = False; return

        svc = session.getService("//blp/refdata")

        # ---- One-shot: pull spots + fwd points for all pairs ----
        log("BBG: pulling spots + fwd points for all pairs...")
        req = svc.createRequest("ReferenceDataRequest")

        # Spot tickers
        for pair in self.ALL_FX_PAIRS:
            req.getElement("securities").appendValue(f"{pair} Curncy")

        # Forward point tickers
        fwd_ticker_map = {}  # "EUR1M Curncy" -> (pair, tenor_label)
        for pair in self.ALL_FX_PAIRS:
            prefix = self.FWD_PREFIX.get(pair)
            if not prefix:
                continue
            for tn in self.FWD_TENORS:
                tk = f"{prefix}{tn['bbg']} Curncy"
                req.getElement("securities").appendValue(tk)
                fwd_ticker_map[tk] = (pair, tn['label'])

        # USD deposit rate (anchor for CIP)
        req.getElement("securities").appendValue("US0003M Index")

        req.getElement("fields").appendValue("PX_LAST")
        session.sendRequest(req)

        data = {}
        while True:
            ev = session.nextEvent(5000)
            for msg in ev:
                if msg.hasElement("securityData"):
                    arr = msg.getElement("securityData")
                    for i in range(arr.numValues()):
                        sec = arr.getValueAsElement(i)
                        tk = sec.getElementAsString("security")
                        flds = sec.getElement("fieldData")
                        if flds.hasElement("PX_LAST"):
                            data[tk] = flds.getElementAsFloat("PX_LAST")
            if ev.eventType() == blpapi.Event.RESPONSE:
                break

        # Parse spots
        for pair in self.ALL_FX_PAIRS:
            px = data.get(f"{pair} Curncy")
            if px and px > 0:
                self.spots[pair] = px

        # Parse fwd points
        for tk, (pair, tenor) in fwd_ticker_map.items():
            pts = data.get(tk)
            if pts is not None:
                if pair not in self.fwd_pts:
                    self.fwd_pts[pair] = {}
                self.fwd_pts[pair][tenor] = pts

        # USD 3M rate (anchor)
        usd_3m = data.get("US0003M Index", 4.5) / 100  # Bloomberg gives in %, convert to decimal

        # Derive rd/rf from 3M fwd points + spot using CIP
        for pair in self.ALL_FX_PAIRS:
            spot = self.spots.get(pair, 0)
            if spot <= 0:
                continue
            fp = self.fwd_pts.get(pair, {}).get('3M', None)
            if fp is None:
                continue
            scale = 100 if 'JPY' in pair else 10000
            fwd = spot + fp / scale
            T = 0.25  # 3M

            base, terms = pair[:3], pair[3:]
            try:
                # CIP: F/S = exp((rd - rf) * T)
                # rd = terms rate, rf = base rate
                rate_diff = math.log(fwd / spot) / T  # rd - rf

                if terms == 'USD':
                    # XXX/USD: terms=USD, rd=USD rate
                    rd = usd_3m
                    rf = rd - rate_diff
                elif base == 'USD':
                    # USD/XXX: base=USD, rf=USD rate
                    rf = usd_3m
                    rd = rf + rate_diff
                else:
                    # Cross: use DEFAULT_RATES as fallback anchor
                    rd = DEFAULT_RATES.get(terms, 0.04)
                    rf = rd - rate_diff

                self.rates[pair] = {'r_d': round(rd, 5), 'r_f': round(rf, 5)}
            except (ValueError, ZeroDivisionError):
                pass

        log(f"BBG: {len(self.spots)} spots, {len(self.fwd_pts)} fwd curves, "
            f"{len(self.rates)} rate pairs, USD 3M={usd_3m*100:.2f}%")

        # ---- Switch to streaming for spot updates ----
        if not session.openService("//blp/mktdata"):
            log("BBG: cannot open mktdata for streaming")
            return
        subs = blpapi.SubscriptionList()
        for pair in self.ALL_FX_PAIRS:
            subs.add(f"{pair} Curncy", "LAST_PRICE", "", blpapi.CorrelationId(pair))
        session.subscribe(subs)
        log(f"BBG: streaming {len(self.ALL_FX_PAIRS)} spots")

        while self._running:
            try:
                ev = session.nextEvent(1000)
                for msg in ev:
                    if msg.hasElement("LAST_PRICE"):
                        cid = msg.correlationIds()[0]
                        pair = cid.value()
                        self.spots[pair] = msg.getElementAsFloat("LAST_PRICE")
            except: pass
        session.stop()

    def stop(self):
        self._running = False


# ==============================================================
# HTTP SERVER
# ==============================================================
_dtcc = None
_bbg_spots = None

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_response(204); self.end_headers(); return
        if self.path.startswith('/api/'):
            self._api()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == '/api/save_marks':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                data = json.loads(body)
                marks_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'saved_marks.json')
                with open(marks_file, 'w') as f:
                    json.dump(data, f, indent=2)
                log(f"Marks saved: {len(data.get('surfaces', {}))} pairs -> saved_marks.json")
                self._json({'ok': True, 'pairs': len(data.get('surfaces', {}))})
            except Exception as e:
                log(f"Save marks error: {e}")
                self._json({'ok': False, 'error': str(e)})
        else:
            self.send_error(404)

    def _api(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)

        if p.path == '/api/mathfix.js':
            body = MATHFIX_JS.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type','application/javascript')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-cache')
            self.end_headers(); self.wfile.write(body)

        elif p.path == '/api/optionflow' and _dtcc:
            try:
                ccys = [c.strip() for c in q.get('ccys',[''])[0].split(',') if c.strip()]
                ms = int(q.get('min_size',[0])[0])
                mn = int(q.get('minutes',[240])[0])
                self._json(_dtcc.fetch(minutes=mn, min_size=ms, ccys=ccys or None))
            except Exception as e:
                log(f"API error: {traceback.format_exc()}")
                self.send_error(500, str(e))

        elif p.path == '/api/bbg_refresh':
            try:
                pp = q.get('pairs',[''])[0]
                pairs = [x.strip().upper().replace('/','') for x in pp.split(',') if x.strip()] if pp else None
                self._json(pull_bbg_surfaces(pairs))
            except Exception as e:
                self._json({'error':str(e),'surfaces':{}})

        elif p.path == '/api/bbg_spots':
            if _bbg_spots:
                self._json({
                    'spots': _bbg_spots.spots,
                    'fwd_pts': _bbg_spots.fwd_pts,
                    'rates': _bbg_spots.rates,
                    'streaming': _bbg_spots._running
                })
            else:
                self._json({'spots': {}, 'fwd_pts': {}, 'rates': {}, 'streaming': False})

        elif p.path == '/api/bbg_hist_spot':
            # Fetch historical spot for a pair at specific datetimes
            # Usage: /api/bbg_hist_spot?pair=EURUSD&dates=2026-03-26,2026-03-25
            # or: /api/bbg_hist_spot?pair=EURUSD&date=2026-03-26&time=14:30
            try:
                pair = q.get('pair', [''])[0].upper().replace('/','')
                dates = q.get('dates', [''])[0]
                date = q.get('date', [''])[0]
                tm = q.get('time', [''])[0]
                result = fetch_bbg_hist_spots(pair, dates, date, tm)
                self._json(result)
            except Exception as e:
                self._json({'error': str(e), 'spots': {}})

        elif p.path == '/api/load_marks':
            marks_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'saved_marks.json')
            if os.path.exists(marks_file):
                try:
                    with open(marks_file, 'r') as f:
                        self._json(json.load(f))
                except Exception as e:
                    self._json({'error': str(e), 'surfaces': {}})
            else:
                self._json({'surfaces': {}})

        elif p.path == '/api/status' and _dtcc:
            self._json(_dtcc.stats)
        else:
            self.send_error(404)

    def _json(self, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers(); self.wfile.write(body)

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/') and '/mathfix' not in self.path:
            log(f"{self.path} -> {args[1] if len(args)>1 else ''}")



# ==============================================================
# EMBEDDED HTML GENERATOR (fx_gamma_richness.py compressed)
# ==============================================================
import base64 as _b64, gzip as _gz

_GENERATOR_B64 = (
    "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"
)

def _extract_generator():
    """Extract and import fx_gamma_richness from embedded data."""
    data = _gz.decompress(_b64.b64decode(_GENERATOR_B64))
    # Write to temp file and import
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_fx_gen_tmp.py')
    with open(tmp, 'wb') as f:
        f.write(data)
    import importlib.util
    spec = importlib.util.spec_from_file_location("fx_gamma_richness", tmp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        os.remove(tmp)
    except:
        pass
    return mod

def generate_html(html_file='fx_gamma_trading.html', excel_file='fx_gamma_inputs.xlsx'):
    """Generate the dashboard HTML from embedded generator."""
    log("Generating dashboard HTML from embedded generator...")
    fxg = _extract_generator()
    # Generate Excel template if it doesn't exist
    if not os.path.exists(excel_file):
        log(f"Creating positions template: {excel_file}")
        fxg.create_template(excel_file)
        log(f"  -> {excel_file} (edit this with your positions)")
    # Generate vol surface template if it doesn't exist
    vol_tpl = 'vol_surface_template.xlsx'
    if not os.path.exists(vol_tpl) and hasattr(fxg, 'create_vol_template'):
        log(f"Creating vol surface template: {vol_tpl}")
        fxg.create_vol_template(vol_tpl)
        log(f"  -> {vol_tpl}")
    positions = []
    if os.path.exists(excel_file):
        try:
            positions = fxg.load_positions(excel_file)
            log(f"Loaded {len(positions)} positions from {excel_file}")
        except Exception as e:
            log(f"Could not load positions: {e}")
    fxg.create_dashboard(positions, output=html_file)
    log(f"Generated {html_file}")


def _ensure_templates():
    """Create Excel templates if they don't exist."""
    excel_file = 'fx_gamma_inputs.xlsx'
    vol_tpl = 'vol_surface_template.xlsx'
    need = not os.path.exists(excel_file) or not os.path.exists(vol_tpl)
    if not need:
        return
    try:
        fxg = _extract_generator()
        if not os.path.exists(excel_file):
            fxg.create_template(excel_file)
            log(f"Created positions template: {excel_file}")
        if not os.path.exists(vol_tpl) and hasattr(fxg, 'create_vol_template'):
            fxg.create_vol_template(vol_tpl)
            log(f"Created vol surface template: {vol_tpl}")
    except Exception as e:
        log(f"Template creation error: {e}")


# ==============================================================
# MAIN
# ==============================================================
def main():
    global _dtcc, _bbg_spots
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--no-bbg-spots', action='store_true', help='Disable Bloomberg spot streaming')
    ap.add_argument('--generate', action='store_true', help='Force regenerate HTML from embedded generator')
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    html_file = 'fx_gamma_trading.html'
    print(f"\n  serve_dashboard.py {SCRIPT_VER}")

    # Auto-delete DTCC cache for clean start
    import shutil
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.dtcc_cache')
    if os.path.exists(cache_dir):
        n = len(os.listdir(cache_dir))
        try:
            shutil.rmtree(cache_dir)
            log(f"Cache cleared: deleted {n} files from .dtcc_cache/")
        except PermissionError:
            # Windows: another process has files locked — delete what we can
            deleted = 0
            for f in os.listdir(cache_dir):
                try:
                    os.remove(os.path.join(cache_dir, f))
                    deleted += 1
                except:
                    pass
            log(f"Cache partially cleared: {deleted}/{n} files (close other serve_dashboard.py instances to fully clear)")
    else:
        log("No cache to clear")

    if not os.path.exists(html_file) or args.generate:
        generate_html(html_file)

    # Always ensure Excel templates exist
    _ensure_templates()

    patch_html(html_file)
    _dtcc = DTCCReader()

    # Start Bloomberg spot streaming (optional)
    if not args.no_bbg_spots:
        _bbg_spots = BBGSpotStreamer()
        _bbg_spots.start()

    url = f'http://localhost:{args.port}/{html_file}'
    print(f"""
{'='*64}
  FX OPTIONS ANALYTICS {SCRIPT_VER}
  DTCC SDR + Bloomberg Live Spots
{'='*64}

  Dashboard:       {url}
  Math fixes:      /api/mathfix.js (fwd pts scaling, spot delta)
  DTCC:            background downloading
  Bloomberg spots: {'enabled (polling every 5s)' if not args.no_bbg_spots else 'disabled'}
  Bloomberg fwds:  {'all tenors on startup, rates derived via CIP' if not args.no_bbg_spots else 'disabled'}
  Bloomberg vols:  orange button in header

  Press Ctrl+C to stop
""")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    srv = HTTPServer(('', args.port), Handler)
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n  Stopped."); srv.server_close()

if __name__ == '__main__':
    main()
