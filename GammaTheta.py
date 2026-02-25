#!/usr/bin/env python3
"""
FX Volatility Surface Heatmap Generator
========================================
Builds a full vol surface from market quotes (ATM, RR, BF) across pillar tenors,
then generates an interactive heatmap visualization.

Pillar Tenors: O/N, 1W, 2W, 1M, 2M, 3M, 6M, 9M, 1Y, 2Y, 3Y, 5Y, 7Y, 10Y
Delta Strikes: 10Δ Put, 25Δ Put, ATM, 25Δ Call, 10Δ Call (plus interpolated points)
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from scipy.interpolate import CubicSpline
from scipy.optimize import brentq
from scipy.stats import norm
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# MARKET DATA — READ FROM EXCEL OR CREATE TEMPLATE
# =============================================================================

INPUT_FILE = 'fx_vol_inputs.xlsx'

def create_input_template(filepath):
    """
    Create a formatted Excel template for market data inputs.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    
    wb = Workbook()
    
    # --- Sheet 1: Market Parameters ---
    ws_params = wb.active
    ws_params.title = 'Parameters'
    
    # Styles
    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='1F4E79')
    input_fill = PatternFill('solid', fgColor='D6EAF8')
    input_font = Font(color='0000FF')
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    
    # Headers
    ws_params['A1'] = 'Parameter'
    ws_params['B1'] = 'Value'
    ws_params['C1'] = 'Description'
    for cell in ['A1', 'B1', 'C1']:
        ws_params[cell].font = header_font
        ws_params[cell].fill = header_fill
        ws_params[cell].alignment = Alignment(horizontal='center')
    
    # Parameters (blue text = user inputs)
    # For EUR/USD: EUR is base (foreign), USD is quote (domestic)
    params = [
        ('Spot', 1.0850, 'Spot rate (EUR/USD)'),
        ('DOM_RATE', 0.045, 'USD rate — domestic/quote currency (decimal, e.g. 0.045 = 4.5%)'),
        ('FOR_RATE', 0.025, 'EUR rate — foreign/base currency (decimal, e.g. 0.025 = 2.5%)'),
        ('REF_TENOR', '3M', 'Reference tenor for weighting (weights = 1.0 at this tenor)'),
    ]
    
    for i, (name, value, desc) in enumerate(params, start=2):
        ws_params[f'A{i}'] = name
        ws_params[f'B{i}'] = value
        ws_params[f'B{i}'].font = input_font
        ws_params[f'B{i}'].fill = input_fill
        ws_params[f'C{i}'] = desc
        for col in ['A', 'B', 'C']:
            ws_params[f'{col}{i}'].border = thin_border
    
    ws_params.column_dimensions['A'].width = 15
    ws_params.column_dimensions['B'].width = 15
    ws_params.column_dimensions['C'].width = 65
    
    # --- Sheet 2: Volatility Surface ---
    ws_vol = wb.create_sheet('VolSurface')
    
    # Headers - now includes Weight column
    headers = ['Tenor', 'T_Years', 'Weight', 'ATM', 'RR25', 'FLY25', 'RR10', 'FLY10']
    for col, header in enumerate(headers, start=1):
        cell = ws_vol.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')
        cell.border = thin_border
    
    # Sample data with weights (3M = 1.0, scale by sqrt(T_ref / T))
    # Reference tenor is 3M = 0.25 years
    T_ref = 0.25
    pillar_data = [
        # (tenor, T_years, ATM, RR25, FLY25, RR10, FLY10)
        ('O/N',  1/365,   7.50, -0.30, 0.15, -0.60, 0.40),
        ('1W',   7/365,   7.80, -0.35, 0.18, -0.70, 0.45),
        ('2W',   14/365,  8.00, -0.40, 0.20, -0.80, 0.50),
        ('1M',   1/12,    8.20, -0.50, 0.25, -1.00, 0.60),
        ('2M',   2/12,    8.50, -0.60, 0.30, -1.20, 0.70),
        ('3M',   3/12,    8.80, -0.70, 0.35, -1.40, 0.80),
        ('6M',   6/12,    9.20, -0.80, 0.40, -1.60, 0.95),
        ('9M',   9/12,    9.50, -0.85, 0.42, -1.70, 1.05),
        ('1Y',   1.0,     9.80, -0.90, 0.45, -1.80, 1.15),
        ('2Y',   2.0,    10.20, -1.00, 0.50, -2.00, 1.30),
        ('3Y',   3.0,    10.50, -1.10, 0.55, -2.20, 1.45),
        ('5Y',   5.0,    10.80, -1.20, 0.60, -2.40, 1.60),
        ('7Y',   7.0,    11.00, -1.25, 0.65, -2.50, 1.75),
        ('10Y', 10.0,    11.20, -1.30, 0.70, -2.60, 1.90),
    ]
    
    for row_idx, (tenor, T, atm, rr25, fly25, rr10, fly10) in enumerate(pillar_data, start=2):
        # Calculate weight: sqrt(T_ref / T) so 3M = 1.0
        weight = (T_ref / T) ** 0.5
        
        ws_vol.cell(row=row_idx, column=1, value=tenor).border = thin_border
        ws_vol.cell(row=row_idx, column=2, value=T).border = thin_border
        
        # Weight column (editable)
        weight_cell = ws_vol.cell(row=row_idx, column=3, value=round(weight, 4))
        weight_cell.font = input_font
        weight_cell.fill = input_fill
        weight_cell.border = thin_border
        
        # Vol inputs
        for col_idx, value in enumerate([atm, rr25, fly25, rr10, fly10], start=4):
            cell = ws_vol.cell(row=row_idx, column=col_idx, value=value)
            cell.font = input_font
            cell.fill = input_fill
            cell.border = thin_border
    
    # Column widths
    widths = [8, 10, 10, 10, 10, 10, 10, 10]
    for i, w in enumerate(widths, start=1):
        ws_vol.column_dimensions[get_column_letter(i)].width = w
    
    # --- Sheet 3: Instructions ---
    ws_inst = wb.create_sheet('Instructions')
    instructions = [
        ('FX Volatility Surface Input Template', ''),
        ('', ''),
        ('CURRENCY CONVENTION (EUR/USD example):', ''),
        ('  Base/Foreign currency = EUR', 'The currency you are buying/selling'),
        ('  Quote/Domestic currency = USD', 'The currency used to express the price'),
        ('  Spot = 1.0850 means 1 EUR = 1.0850 USD', ''),
        ('', ''),
        ('PARAMETERS SHEET:', ''),
        ('  Spot', 'Current spot rate'),
        ('  DOM_RATE', 'Domestic (USD) interest rate as decimal (e.g., 0.045 = 4.5%)'),
        ('  FOR_RATE', 'Foreign (EUR) interest rate as decimal'),
        ('  REF_TENOR', 'Reference tenor for weights (default 3M)'),
        ('', ''),
        ('VOL SURFACE SHEET:', ''),
        ('  Tenor', 'Tenor label (O/N, 1W, 2W, 1M, etc.)'),
        ('  T_Years', 'Time to expiry in years (e.g., 0.25 for 3M)'),
        ('  Weight', 'Multiplier for Vega and Gamma/Theta (3M = 1.0 by default)'),
        ('  ATM', 'ATM volatility in % (e.g., 8.5 = 8.5%)'),
        ('  RR25', '25-delta risk reversal in % (call vol - put vol)'),
        ('  FLY25', '25-delta butterfly in % (wing avg - ATM)'),
        ('  RR10', '10-delta risk reversal in %'),
        ('  FLY10', '10-delta butterfly in %'),
        ('', ''),
        ('WEIGHTING:', ''),
        ('  Default weights use sqrt(T_ref / T) formula:', ''),
        ('  - Shorter tenors get weight > 1 (more importance)', ''),
        ('  - Longer tenors get weight < 1 (less importance)', ''),
        ('  - Reference tenor (3M default) gets weight = 1.0', ''),
        ('  - You can override any weight manually', ''),
        ('', ''),
        ('NOTES:', ''),
        ('  - Blue cells are inputs you can modify', ''),
        ('  - Vols are in %, not decimals (8.5 = 8.5%, not 0.085)', ''),
        ('  - RR is typically negative for EUR/USD (puts > calls)', ''),
        ('  - FLY is typically positive (wings > ATM)', ''),
    ]
    
    for row_idx, (col1, col2) in enumerate(instructions, start=1):
        ws_inst.cell(row=row_idx, column=1, value=col1)
        ws_inst.cell(row=row_idx, column=2, value=col2)
        if row_idx == 1:
            ws_inst.cell(row=row_idx, column=1).font = Font(bold=True, size=14)
    
    ws_inst.column_dimensions['A'].width = 45
    ws_inst.column_dimensions['B'].width = 50
    
    wb.save(filepath)
    return filepath


def load_market_data(filepath):
    """
    Load market data from Excel file.
    Returns: spot, dom_rate, for_rate, pillar_df (with weights)
    """
    # Read parameters
    params_df = pd.read_excel(filepath, sheet_name='Parameters')
    params = dict(zip(params_df['Parameter'], params_df['Value']))
    
    spot = params['Spot']
    dom_rate = params['DOM_RATE']
    for_rate = params['FOR_RATE']
    
    # Read vol surface (now includes Weight column)
    vol_df = pd.read_excel(filepath, sheet_name='VolSurface')
    vol_df.columns = ['tenor', 'T_years', 'weight', 'atm', 'rr25', 'fly25', 'rr10', 'fly10']
    
    return spot, dom_rate, for_rate, vol_df

# =============================================================================
# BLACK-SCHOLES HELPERS
# =============================================================================

def bs_d1(F, K, T, sigma):
    """Black-Scholes d1."""
    if T <= 0 or sigma <= 0:
        return 0.0
    return (np.log(F / K) + 0.5 * sigma**2 * T) / (sigma * np.sqrt(T))

def bs_d2(F, K, T, sigma):
    """Black-Scholes d2."""
    return bs_d1(F, K, T, sigma) - sigma * np.sqrt(T)

def bs_call(F, K, T, sigma, df_d):
    """Black call price."""
    if T <= 0:
        return max(F - K, 0) * df_d
    d1 = bs_d1(F, K, T, sigma)
    d2 = bs_d2(F, K, T, sigma)
    return df_d * (F * norm.cdf(d1) - K * norm.cdf(d2))

def bs_put(F, K, T, sigma, df_d):
    """Black put price."""
    if T <= 0:
        return max(K - F, 0) * df_d
    d1 = bs_d1(F, K, T, sigma)
    d2 = bs_d2(F, K, T, sigma)
    return df_d * (K * norm.cdf(-d2) - F * norm.cdf(-d1))

def spot_delta_call(F, K, T, sigma, df_f):
    """Spot delta for a call (non-premium-adjusted)."""
    if T <= 0:
        return 1.0 if F > K else 0.0
    d1 = bs_d1(F, K, T, sigma)
    return df_f * norm.cdf(d1)

def spot_delta_put(F, K, T, sigma, df_f):
    """Spot delta for a put (non-premium-adjusted)."""
    if T <= 0:
        return -1.0 if F < K else 0.0
    d1 = bs_d1(F, K, T, sigma)
    return df_f * (norm.cdf(d1) - 1)

def atm_dns_strike(F, T, sigma):
    """Delta-neutral straddle ATM strike."""
    return F * np.exp(0.5 * sigma**2 * T)

# =============================================================================
# GREEKS
# =============================================================================

def bs_vega(S, K, T, sigma, r_d, r_f):
    """
    Vega: sensitivity to 1% (absolute) move in vol.
    Returns vega per unit notional of foreign currency.
    """
    if T <= 1e-8 or sigma <= 0:
        return 0.0
    df_f = np.exp(-r_f * T)
    F = S * np.exp((r_d - r_f) * T)
    d1 = bs_d1(F, K, T, sigma)
    # Vega for 1% vol move (0.01 in decimal)
    return S * df_f * np.sqrt(T) * norm.pdf(d1) * 0.01


def bs_gamma(S, K, T, sigma, r_d, r_f):
    """
    Gamma: second derivative of price w.r.t. spot.
    Returns gamma per unit notional of foreign currency.
    """
    if T <= 1e-8 or sigma <= 0:
        return 0.0
    df_f = np.exp(-r_f * T)
    F = S * np.exp((r_d - r_f) * T)
    d1 = bs_d1(F, K, T, sigma)
    return df_f * norm.pdf(d1) / (S * sigma * np.sqrt(T))


def bs_theta_call(S, K, T, sigma, r_d, r_f):
    """
    Theta for a call: time decay per calendar day.
    """
    if T <= 1e-8:
        return 0.0
    df_d = np.exp(-r_d * T)
    df_f = np.exp(-r_f * T)
    F = S * np.exp((r_d - r_f) * T)
    d1 = bs_d1(F, K, T, sigma)
    d2 = bs_d2(F, K, T, sigma)
    
    # Theta in annual terms
    theta_annual = (
        -S * df_f * norm.pdf(d1) * sigma / (2 * np.sqrt(T))
        - r_d * K * df_d * norm.cdf(d2)
        + r_f * S * df_f * norm.cdf(d1)
    )
    # Convert to per calendar day
    return theta_annual / 365.0


def bs_theta_put(S, K, T, sigma, r_d, r_f):
    """
    Theta for a put: time decay per calendar day.
    """
    if T <= 1e-8:
        return 0.0
    df_d = np.exp(-r_d * T)
    df_f = np.exp(-r_f * T)
    F = S * np.exp((r_d - r_f) * T)
    d1 = bs_d1(F, K, T, sigma)
    d2 = bs_d2(F, K, T, sigma)
    
    # Theta in annual terms
    theta_annual = (
        -S * df_f * norm.pdf(d1) * sigma / (2 * np.sqrt(T))
        + r_d * K * df_d * norm.cdf(-d2)
        - r_f * S * df_f * norm.cdf(-d1)
    )
    # Convert to per calendar day
    return theta_annual / 365.0


def bs_delta_call(S, K, T, sigma, r_f):
    """Spot delta for a call."""
    if T <= 1e-8:
        return 1.0 if S > K else 0.0
    df_f = np.exp(-r_f * T)
    F = S * np.exp((0) * T)  # We need forward for d1
    F = S * df_f / np.exp(-0 * T)  # Simplified
    d1 = bs_d1(S * np.exp(r_f * T) * np.exp(-r_f * T), K, T, sigma)
    # Recalculate properly
    F_calc = S * np.exp((DOM_RATE - r_f) * T)
    d1 = bs_d1(F_calc, K, T, sigma)
    return df_f * norm.cdf(d1)


def bs_delta_put(S, K, T, sigma, r_f):
    """Spot delta for a put."""
    if T <= 1e-8:
        return -1.0 if S < K else 0.0
    df_f = np.exp(-r_f * T)
    F_calc = S * np.exp((DOM_RATE - r_f) * T)
    d1 = bs_d1(F_calc, K, T, sigma)
    return df_f * (norm.cdf(d1) - 1)


def theta_with_roll_cost(S, K, T, sigma, r_d, r_f, is_call=True):
    """
    Theta adjusted for forward point roll cost.
    
    When delta-hedging an FX option, you hold a spot position that must be 
    rolled daily. The roll cost is the 1-day forward points on your hedge.
    
    1-day forward points = S * (r_d - r_f) / 365
    
    If you're hedging a long call (positive delta):
      - You're SHORT foreign currency spot
      - Rolling short spot: you do a buy/sell swap (buy spot, sell T/N forward)
      - If fwd points > 0 (r_d > r_f): you earn the points (buy low, sell high)
      - Roll P&L = +|delta| * fwd_points (positive, you earn)
    
    If you're hedging a long put (negative delta):
      - You're LONG foreign currency spot  
      - Rolling long spot: you do a sell/buy swap (sell spot, buy T/N forward)
      - If fwd points > 0: you pay the points (sell low, buy high)
      - Roll P&L = -|delta| * fwd_points (negative, you pay)
    
    Adjusted Theta = Option Theta + Roll P&L on delta hedge
    
    This gives the daily P&L of a delta-hedged option position.
    """
    if T <= 1e-8:
        return 0.0
    
    # Forward and discount factors
    F = S * np.exp((r_d - r_f) * T)
    df_f = np.exp(-r_f * T)
    
    # Get raw theta (per calendar day)
    if is_call:
        theta = bs_theta_call(S, K, T, sigma, r_d, r_f)
        delta = spot_delta_call(F, K, T, sigma, df_f)
    else:
        theta = bs_theta_put(S, K, T, sigma, r_d, r_f)
        delta = spot_delta_put(F, K, T, sigma, df_f)
    
    # 1-day forward points (T/N points)
    fwd_points_1d = S * (r_d - r_f) / 365.0
    
    # Roll cost on delta hedge
    # Hedge position = -delta (opposite of option delta)
    # Roll P&L for hedge = hedge_position * fwd_points = -delta * fwd_points
    # 
    # But from perspective of total position (long option + hedge):
    # - Long call (delta > 0), short spot hedge earns points if r_d > r_f
    # - Long put (delta < 0), long spot hedge pays points if r_d > r_f
    #
    # Roll P&L = -delta * (-fwd_points) = delta * fwd_points ... wait, let me be careful
    #
    # Actually, if you're short spot and points > 0:
    #   You buy spot at S, sell forward at S + points → you EARN points
    # If you're long spot and points > 0:
    #   You sell spot at S, buy forward at S + points → you PAY points
    #
    # Hedge for long call: SHORT delta units → earns delta * fwd_points
    # Hedge for long put: LONG |delta| units → pays |delta| * fwd_points = delta * fwd_points (delta negative)
    #
    # So roll_pnl = -delta * fwd_points is WRONG
    # It should be: roll_pnl = -hedge_delta * fwd_points = -(-option_delta) * fwd_points = delta * fwd_points
    # 
    # Hmm, but for a put with negative delta, this would be negative, meaning you pay — that's correct!
    
    roll_pnl = delta * fwd_points_1d
    
    # Total daily P&L = option theta + roll P&L on hedge
    return theta + roll_pnl

def strike_from_delta_call(F, T, sigma, delta_target, df_f, tol=1e-10):
    """Invert delta to find strike for a call."""
    if T <= 1e-8:
        return F
    K_lo = F * 0.5
    K_hi = F * 2.0
    try:
        K = brentq(lambda k: spot_delta_call(F, k, T, sigma, df_f) - delta_target, K_lo, K_hi, xtol=tol)
    except:
        K = F
    return K

def strike_from_delta_put(F, T, sigma, delta_target, df_f, tol=1e-10):
    """Invert delta to find strike for a put (delta_target is negative)."""
    if T <= 1e-8:
        return F
    K_lo = F * 0.5
    K_hi = F * 2.0
    try:
        K = brentq(lambda k: spot_delta_put(F, k, T, sigma, df_f) - delta_target, K_lo, K_hi, xtol=tol)
    except:
        K = F
    return K

# =============================================================================
# SMILE CONSTRUCTION (BROKER FLY SOLVE)
# =============================================================================

def solve_smile_single_tenor(T, F, df_d, df_f, atm, rr25, fly25, rr10, fly10, tol=1e-8, max_iter=50):
    """
    Solve for the 5 smile vols from market quotes.
    Uses smile strangle convention (simpler, more robust).
    Returns dict with vols for 10P, 25P, ATM, 25C, 10C.
    """
    atm_vol = atm / 100.0
    rr25_vol = rr25 / 100.0
    fly25_vol = fly25 / 100.0
    rr10_vol = rr10 / 100.0
    fly10_vol = fly10 / 100.0

    # Smile strangle interpretation:
    # σ_call = ATM + BF + RR/2
    # σ_put  = ATM + BF - RR/2
    sigma_25c = atm_vol + fly25_vol + 0.5 * rr25_vol
    sigma_25p = atm_vol + fly25_vol - 0.5 * rr25_vol
    sigma_10c = atm_vol + fly10_vol + 0.5 * rr10_vol
    sigma_10p = atm_vol + fly10_vol - 0.5 * rr10_vol

    return {
        '10P': sigma_10p,
        '25P': sigma_25p,
        'ATM': atm_vol,
        '25C': sigma_25c,
        '10C': sigma_10c,
    }

# =============================================================================
# FULL SURFACE CONSTRUCTION
# =============================================================================

def build_vol_surface(pillar_data, spot, dom_rate, for_rate):
    """
    Build the full volatility surface across all tenors and delta pillars.
    Returns DataFrames for vol, gamma, theta (adjusted), vega, and gamma/theta ratio.
    """
    # Delta pillars for the surface (put deltas negative, call deltas positive)
    delta_pillars = {
        '10Δ Put': (-0.10, False),
        '15Δ Put': (-0.15, False),
        '25Δ Put': (-0.25, False),
        '35Δ Put': (-0.35, False),
        'ATM': (0.0, None),  # Straddle - we'll average
        '35Δ Call': (0.35, True),
        '25Δ Call': (0.25, True),
        '15Δ Call': (0.15, True),
        '10Δ Call': (0.10, True),
    }

    vol_data = []
    gamma_data = []
    theta_data = []
    vega_data = []
    gamma_theta_data = []

    for _, row in pillar_data.iterrows():
        tenor = row['tenor']
        T = row['T_years']
        weight = row['weight'] if 'weight' in row else 1.0

        # Discount factors
        df_d = np.exp(-dom_rate * T)
        df_f = np.exp(-for_rate * T)

        # Forward
        F = spot * np.exp((dom_rate - for_rate) * T)

        # Solve the 5-point smile
        smile = solve_smile_single_tenor(
            T, F, df_d, df_f,
            row['atm'], row['rr25'], row['fly25'], row['rr10'], row['fly10']
        )

        # Initialize row dicts
        tenor_vols = {'tenor': tenor, 'T_years': T}
        tenor_gamma = {'tenor': tenor, 'T_years': T}
        tenor_theta = {'tenor': tenor, 'T_years': T}
        tenor_vega = {'tenor': tenor, 'T_years': T}
        tenor_gamma_theta = {'tenor': tenor, 'T_years': T}
        
        for name, (delta, is_call) in delta_pillars.items():
            # Get vol for this delta point
            if delta == 0:
                sigma = smile['ATM']
            elif delta < 0:
                # Put wing interpolation
                abs_delta = abs(delta)
                if abs_delta <= 0.10:
                    sigma = smile['10P']
                elif abs_delta <= 0.25:
                    t = (abs_delta - 0.10) / (0.25 - 0.10)
                    sigma = smile['10P'] + t * (smile['25P'] - smile['10P'])
                else:
                    t = (abs_delta - 0.25) / (0.50 - 0.25)
                    sigma = smile['25P'] + t * (smile['ATM'] - smile['25P'])
            else:
                # Call wing interpolation
                if delta <= 0.10:
                    sigma = smile['10C']
                elif delta <= 0.25:
                    t = (delta - 0.10) / (0.25 - 0.10)
                    sigma = smile['10C'] + t * (smile['25C'] - smile['10C'])
                else:
                    t = (delta - 0.25) / (0.50 - 0.25)
                    sigma = smile['25C'] + t * (smile['ATM'] - smile['25C'])
            
            tenor_vols[name] = sigma * 100
            
            # Calculate strike from delta
            if delta == 0:
                # ATM DNS strike
                K = atm_dns_strike(F, T, sigma)
                # For ATM, average call and put Greeks
                gamma = bs_gamma(spot, K, T, sigma, dom_rate, for_rate)
                theta_call = theta_with_roll_cost(spot, K, T, sigma, dom_rate, for_rate, is_call=True)
                theta_put = theta_with_roll_cost(spot, K, T, sigma, dom_rate, for_rate, is_call=False)
                theta = (theta_call + theta_put) / 2  # Straddle theta
                vega = bs_vega(spot, K, T, sigma, dom_rate, for_rate)
            elif is_call:
                K = strike_from_delta_call(F, T, sigma, delta, df_f)
                gamma = bs_gamma(spot, K, T, sigma, dom_rate, for_rate)
                theta = theta_with_roll_cost(spot, K, T, sigma, dom_rate, for_rate, is_call=True)
                vega = bs_vega(spot, K, T, sigma, dom_rate, for_rate)
            else:
                K = strike_from_delta_put(F, T, sigma, delta, df_f)
                gamma = bs_gamma(spot, K, T, sigma, dom_rate, for_rate)
                theta = theta_with_roll_cost(spot, K, T, sigma, dom_rate, for_rate, is_call=False)
                vega = bs_vega(spot, K, T, sigma, dom_rate, for_rate)
            
            # Scale Greeks to useful units:
            # Gamma: per 1% spot move (multiply by S * 0.01)
            # Theta: per day, per 1M notional
            # Vega: per 1% vol move, per 1M notional
            notional = 1_000_000
            
            gamma_1pct = gamma * spot * 0.01 * notional  # P&L for 1% spot move
            theta_daily = theta * notional  # Daily P&L
            vega_1pct = vega * notional  # P&L for 1% vol move
            
            # Theta/Gamma ratio (daily bleed per unit of gamma)
            # Lower = cheaper gamma, higher = more expensive
            # Expressed as: |theta + roll| / gamma (per 1% move)
            # Scaled to give intuitive 0-5 range
            if gamma_1pct > 1e-6:
                # Ratio: daily theta per $1000 of gamma
                theta_gamma_ratio = (abs(theta_daily) / gamma_1pct) * 1000
            else:
                theta_gamma_ratio = 0.0
            
            # Apply tenor weight to vega and gamma/theta ratio
            vega_weighted = vega_1pct * weight
            theta_gamma_weighted = theta_gamma_ratio * weight
            
            tenor_gamma[name] = gamma_1pct
            tenor_theta[name] = theta_daily
            tenor_vega[name] = vega_weighted
            tenor_gamma_theta[name] = theta_gamma_weighted

        vol_data.append(tenor_vols)
        gamma_data.append(tenor_gamma)
        theta_data.append(tenor_theta)
        vega_data.append(tenor_vega)
        gamma_theta_data.append(tenor_gamma_theta)

    return (pd.DataFrame(vol_data), 
            pd.DataFrame(gamma_data), 
            pd.DataFrame(theta_data), 
            pd.DataFrame(vega_data),
            pd.DataFrame(gamma_theta_data))


def create_heatmap(surface_df, output_path='fx_vol_surface_heatmap.png', 
                   title='FX Implied Volatility Surface', label='Implied Volatility (%)',
                   fmt='.2f', cmap_style='vol', ax=None, show_colorbar=True):
    """
    Create a professional heatmap for vol or Greeks.
    If ax is provided, draw on that axis (for subplots).
    """
    # Prepare data for heatmap
    delta_cols = ['10Δ Put', '15Δ Put', '25Δ Put', '35Δ Put', 'ATM', '35Δ Call', '25Δ Call', '15Δ Call', '10Δ Call']
    tenors = surface_df['tenor'].tolist()

    # Create the heatmap matrix
    heatmap_data = surface_df[delta_cols].values

    # Create figure if no axis provided
    if ax is None:
        fig, ax = plt.subplots(figsize=(14, 10))
        standalone = True
    else:
        fig = ax.figure
        standalone = False

    # Red (high) to Blue (low) colormap
    colors = ['#0d47a1', '#1565c0', '#1976d2', '#1e88e5', '#42a5f5', 
              '#64b5f6', '#90caf9', '#bbdefb',  # Blues
              '#e3f2fd', '#fffde7', '#fff9c4', '#fff59d',  # Light transition
              '#ffcc80', '#ffb74d', '#ffa726', '#ff9800',  # Oranges
              '#f57c00', '#ef6c00', '#e65100',  # Deep oranges
              '#d84315', '#c62828', '#b71c1c']  # Reds
    cmap = mcolors.LinearSegmentedColormap.from_list('blue_red', colors, N=256)

    # Create heatmap
    im = ax.imshow(heatmap_data, cmap=cmap, aspect='auto', interpolation='bicubic')

    # Set ticks
    ax.set_xticks(np.arange(len(delta_cols)))
    ax.set_yticks(np.arange(len(tenors)))
    ax.set_xticklabels(delta_cols, fontsize=10, fontweight='medium', color='black')
    ax.set_yticklabels(tenors, fontsize=10, fontweight='medium', color='black')

    # Rotate x labels
    plt.setp(ax.get_xticklabels(), rotation=45, ha='right', rotation_mode='anchor')

    # Add colorbar
    if show_colorbar:
        cbar = fig.colorbar(im, ax=ax, shrink=0.8, pad=0.02)
        cbar.set_label(label, fontsize=11, fontweight='bold', color='black')
        cbar.ax.tick_params(colors='black')

    # Add value annotations - all black text
    for i in range(len(tenors)):
        for j in range(len(delta_cols)):
            val = heatmap_data[i, j]
            # Format based on magnitude
            if abs(val) >= 1000:
                display_val = f'{val/1000:.1f}k'
            elif abs(val) >= 100:
                display_val = f'{val:.0f}'
            else:
                display_val = f'{val:{fmt}}'
            ax.text(j, i, display_val, ha='center', va='center',
                   color='black', fontsize=8, fontweight='medium')

    # Labels and title
    ax.set_xlabel('Delta', fontsize=12, fontweight='bold', labelpad=10, color='black')
    ax.set_ylabel('Tenor', fontsize=12, fontweight='bold', labelpad=10, color='black')
    ax.set_title(f'{title}\n', fontsize=14, fontweight='bold', pad=10, color='black')

    # Add subtle grid
    ax.set_xticks(np.arange(len(delta_cols)+1)-.5, minor=True)
    ax.set_yticks(np.arange(len(tenors)+1)-.5, minor=True)
    ax.grid(which='minor', color='#cccccc', linestyle='-', linewidth=0.5)
    ax.tick_params(which='minor', size=0)

    # If standalone, save
    if standalone:
        plt.tight_layout()
        fig.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white', edgecolor='none')
        plt.close()

    return output_path


def create_combined_heatmaps(gamma_theta_df, vega_df, output_path='fx_greeks_surface.png'):
    """
    Create a single image with both heatmaps side by side.
    """
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(22, 10))
    
    # Set white background
    fig.patch.set_facecolor('white')
    
    # Theta/Gamma heatmap on left
    create_heatmap(
        gamma_theta_df, ax=ax1,
        title='Theta/Gamma Ratio (Lower = Cheaper Gamma)',
        label='|Theta+Roll|/Gamma (x1000)', fmt='.2f'
    )
    
    # Vega heatmap on right
    create_heatmap(
        vega_df, ax=ax2,
        title='Vega (P&L per 1% Vol Move, $1M notional)',
        label='Vega (USD)', fmt='.0f'
    )
    
    plt.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white', edgecolor='none')
    plt.close()
    
    return output_path


def create_3d_surface(surface_df, output_path='fx_vol_surface_3d.png'):
    """
    Create a 3D volatility surface visualization.
    """
    from mpl_toolkits.mplot3d import Axes3D

    delta_cols = ['10Δ Put', '15Δ Put', '25Δ Put', '35Δ Put', 'ATM', '35Δ Call', '25Δ Call', '15Δ Call', '10Δ Call']
    tenors = surface_df['tenor'].tolist()
    T_years = surface_df['T_years'].values

    # Create meshgrid
    delta_numeric = np.array([-0.10, -0.15, -0.25, -0.35, 0.0, 0.35, 0.25, 0.15, 0.10])
    X, Y = np.meshgrid(delta_numeric, T_years)
    Z = surface_df[delta_cols].values

    # Set up figure
    plt.style.use('dark_background')
    fig = plt.figure(figsize=(14, 10))
    ax = fig.add_subplot(111, projection='3d')

    # Custom colormap
    colors = ['#0d47a1', '#1976d2', '#42a5f5', '#fff176', '#ff7043', '#c62828']
    cmap = mcolors.LinearSegmentedColormap.from_list('vol_3d', colors, N=256)

    # Plot surface
    surf = ax.plot_surface(X, np.log(Y + 0.01), Z, cmap=cmap, edgecolor='none',
                           alpha=0.9, antialiased=True, linewidth=0)

    # Labels
    ax.set_xlabel('\nDelta', fontsize=11, fontweight='bold', labelpad=10)
    ax.set_ylabel('\nLog(Time to Expiry)', fontsize=11, fontweight='bold', labelpad=10)
    ax.set_zlabel('\nImplied Vol (%)', fontsize=11, fontweight='bold', labelpad=10)
    ax.set_title('FX Volatility Surface (3D View)\n', fontsize=14, fontweight='bold')

    # Colorbar
    cbar = fig.colorbar(surf, ax=ax, shrink=0.6, pad=0.1)
    cbar.set_label('Implied Volatility (%)', fontsize=10, fontweight='bold')

    # Viewing angle
    ax.view_init(elev=25, azim=45)

    plt.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='#1a1a1a', edgecolor='none')
    plt.close()

    return output_path


def print_surface_table(surface_df, title="IMPLIED VOLATILITY", unit="%", fmt=".2f"):
    """Pretty print a surface as a table."""
    delta_cols = ['10Δ Put', '15Δ Put', '25Δ Put', '35Δ Put', 'ATM', '35Δ Call', '25Δ Call', '15Δ Call', '10Δ Call']

    print("\n" + "="*110)
    print(f"FX {title} SURFACE ({unit}) — per $1M notional")
    print("="*110)

    # Header
    header = f"{'Tenor':>6}"
    for col in delta_cols:
        header += f" {col:>11}"
    print(header)
    print("-"*110)

    # Data rows
    for _, row in surface_df.iterrows():
        line = f"{row['tenor']:>6}"
        for col in delta_cols:
            val = row[col]
            if abs(val) >= 10000:
                line += f" {val/1000:>10.1f}k"
            elif abs(val) >= 1000:
                line += f" {val:>11,.0f}"
            else:
                line += f" {val:>11{fmt}}"
        print(line)

    print("="*110 + "\n")


# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    import os
    
    print("\n╔══════════════════════════════════════════════════════════════════╗")
    print("║         FX GREEKS SURFACE HEATMAP GENERATOR                      ║")
    print("╚══════════════════════════════════════════════════════════════════╝\n")

    # Check for input file, create template if not exists
    if not os.path.exists(INPUT_FILE):
        print(f"Input file '{INPUT_FILE}' not found.")
        print(f"Creating template with sample data...")
        create_input_template(INPUT_FILE)
        print(f"  → Template created: {INPUT_FILE}")
        print(f"\n  Edit the Excel file with your market data, then run again.\n")
        print("  Sheets in template:")
        print("    • Parameters — Spot, DOM_RATE, FOR_RATE")
        print("    • VolSurface — Tenor, T_Years, ATM, RR25, FLY25, RR10, FLY10")
        print("    • Instructions — How to fill in the data")
        print()
    else:
        print(f"Loading market data from: {INPUT_FILE}")
        SPOT, DOM_RATE, FOR_RATE, PILLAR_DATA = load_market_data(INPUT_FILE)
        
        print(f"Spot: {SPOT:.4f}  |  DOM Rate: {DOM_RATE*100:.2f}%  |  FOR Rate: {FOR_RATE*100:.2f}%")
        fwd_pts_1d = SPOT * (DOM_RATE - FOR_RATE) / 365
        print(f"1-Day Fwd Points: {fwd_pts_1d*10000:.4f} pips  |  Notional: $1,000,000")
        print(f"Tenors loaded: {len(PILLAR_DATA)}\n")

        # Build surfaces
        print("Building volatility and Greeks surfaces...")
        vol_surface, gamma_surface, theta_surface, vega_surface, gamma_theta_surface = build_vol_surface(
            PILLAR_DATA, SPOT, DOM_RATE, FOR_RATE
        )

        # Print summary tables
        print_surface_table(gamma_theta_surface, "|THETA + ROLL| / GAMMA", "ratio", ".2f")
        print_surface_table(vega_surface, "VEGA (P&L per 1% vol move)", "$", ".0f")

        # Generate combined heatmap
        print("Generating combined heatmap visualization...")
        
        combined_path = create_combined_heatmaps(
            gamma_theta_surface, vega_surface, 'fx_greeks_surface.png'
        )
        print(f"  → Combined heatmap: {combined_path}")

        # Save CSVs
        gamma_theta_surface.to_csv('fx_gamma_theta_surface.csv', index=False)
        vega_surface.to_csv('fx_vega_surface.csv', index=False)
        print(f"  → CSV files saved")

        print("\n✓ Complete!\n")