
"""
NIFTY NEXT-DAY BIAS ENGINE v3 (Clean Full Edition)
=====================================================
100% Free Data Sources:
  - Yahoo Finance : Nifty OHLC, India VIX, DXY, Crude, US10Y, USDINR
  - NSE API       : Option Chain, OI Delta, ATM IV, PCR, Max Pain
                    (works only from Indian IP - your local PC)
                    (GitHub Actions uses US IP, so NSE will be estimated)

Computes:
  - CPR, Camarilla, Pivot Levels (from real previous day OHLC)
  - 1-Day Expected Move Range (from ATM IV)
  - Algorithmic Weighted Bias Score (-100 to +100)
  - Max Pain (from all strike OI)
  - Dynamic Strategy Selection (based on bias + IV regime)
  - OI Delta per Strike (smart money flow)

Author  : NiftyBiasEngine
Version : 3.0
"""

import requests
import json
import math
import yfinance as yf
from datetime import datetime

# ─── NSE SESSION HEADERS ────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def get_nse_session():
    """Create an NSE-authenticated session with cookies."""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
        print("  NSE session initialized.")
    except Exception as e:
        print(f"  NSE session warning (expected from non-Indian IP): {e}")
    return session


# ─── SAFE YAHOO FINANCE MACRO FETCH ─────────────────────────────────────────

def fetch_yahoo_macros():
    """
    Fetch DXY, Crude, US 10Y Yield, and USDINR from Yahoo Finance.
    Handles both single-level and multi-level column formats safely.
    Returns a dict with fallback values if any ticker fails.
    """
    print("Fetching global macros from Yahoo Finance...")

    tickers = {
        "DX-Y.NYB": ("dxy",   104.2),
        "BZ=F":     ("crude",  76.5),
        "^TNX":     ("us10y",  4.28),
        "INR=X":    ("usdinr", 83.85),
    }

    result = {}

    for ticker, (key, fallback) in tickers.items():
        try:
            raw = yf.download(ticker, period="3d", progress=False, auto_adjust=True)

            # yfinance can return a MultiIndex or flat DataFrame — handle both
            if raw.empty:
                raise ValueError("Empty DataFrame returned")

            if isinstance(raw.columns, object) and hasattr(raw.columns, "levels"):
                # MultiIndex: ('Close', 'DX-Y.NYB')
                close_col = raw["Close"]
                value = float(close_col.iloc[-1].iloc[0])
            else:
                # Flat columns: just 'Close'
                value = float(raw["Close"].iloc[-1])

            result[key] = round(value, 2)
            print(f"  {key}: {result[key]}")

        except Exception as e:
            print(f"  {ticker} fetch failed ({e}). Using fallback: {fallback}")
            result[key] = fallback

    return result


# ─── YAHOO FINANCE: NIFTY OHLC ───────────────────────────────────────────────

def fetch_nifty_ohlc():
    """
    Fetch last 5 trading days of Nifty 50 OHLC from Yahoo Finance.
    Returns (spot, pdh, pdl, pdc) — all rounded to 2 decimal places.
    Uses the last available day as spot and the day before as previous day.
    """
    print("Fetching Nifty OHLC from Yahoo Finance...")
    try:
        df = yf.download("^NSEI", period="5d", progress=False, auto_adjust=True)

        if df.empty or len(df) < 2:
            raise ValueError("Not enough data rows returned")

        # Handle MultiIndex columns if present
        if isinstance(df.columns, object) and hasattr(df.columns, "levels"):
            close = df["Close"].iloc[:, 0]
            high  = df["High"].iloc[:, 0]
            low   = df["Low"].iloc[:, 0]
        else:
            close = df["Close"]
            high  = df["High"]
            low   = df["Low"]

        spot = round(float(close.iloc[-1]),  2)
        pdh  = round(float(high.iloc[-2]),   2)
        pdl  = round(float(low.iloc[-2]),    2)
        pdc  = round(float(close.iloc[-2]),  2)

        print(f"  Spot (today close) : {spot}")
        print(f"  Prev Day H / L / C : {pdh} / {pdl} / {pdc}")
        return spot, pdh, pdl, pdc

    except Exception as e:
        print(f"  Nifty OHLC fetch failed: {e}. Using fallback values.")
        return 24252.0, 24375.0, 24235.0, 24300.0


# ─── YAHOO FINANCE: INDIA VIX ────────────────────────────────────────────────

def fetch_india_vix_yahoo():
    """Fetch India VIX from Yahoo Finance as a fallback."""
    try:
        df = yf.download("^INDIAVIX", period="3d", progress=False, auto_adjust=True)
        if df.empty:
            raise ValueError("Empty VIX data")

        if isinstance(df.columns, object) and hasattr(df.columns, "levels"):
            vix_val = float(df["Close"].iloc[:, 0].iloc[-1])
        else:
            vix_val = float(df["Close"].iloc[-1])

        return round(vix_val, 2)
    except Exception:
        return 14.0


# ─── NSE: OPTION CHAIN ───────────────────────────────────────────────────────

def fetch_nse_option_chain(session, spot):
    """
    Fetch Nifty option chain from NSE.
    Returns: atm_strike, atm_iv, pcr_oi, pcr_chg, max_pain, oi_delta_strikes
    Works reliably only from an Indian IP address.
    """
    print("Attempting NSE option chain fetch...")

    # Default / fallback values
    atm_strike      = int(round(spot / 50.0) * 50)
    atm_iv          = 14.0
    pcr_oi          = 0.74
    pcr_chg         = 0.53
    max_pain_strike = atm_strike
    oi_delta        = []
    nse_ok          = False

    try:
        url  = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
        resp = session.get(url, timeout=10)

        if resp.status_code != 200:
            raise ValueError(f"NSE returned HTTP {resp.status_code}")

        data    = resp.json()
        records = data["records"]["data"]
        spot    = data["records"]["underlyingValue"]

        strikes_list = [r["strikePrice"] for r in records]
        atm_strike   = min(strikes_list, key=lambda x: abs(x - spot))
        atm_rec      = next(r for r in records if r["strikePrice"] == atm_strike)
        atm_iv       = atm_rec.get("CE", {}).get("impliedVolatility", 14.0) or 14.0

        total_call_oi = total_put_oi = 0
        total_call_chg = total_put_chg = 0

        for r in records:
            ce = r.get("CE", {})
            pe = r.get("PE", {})
            c_oi  = ce.get("openInterest", 0)
            p_oi  = pe.get("openInterest", 0)
            c_chg = ce.get("changeinOpenInterest", 0)
            p_chg = pe.get("changeinOpenInterest", 0)

            total_call_oi  += c_oi
            total_put_oi   += p_oi
            total_call_chg += c_chg
            total_put_chg  += p_chg

            if abs(r["strikePrice"] - spot) <= 500:
                oi_delta.append({
                    "strike":       r["strikePrice"],
                    "call_oi_chg":  c_chg,
                    "put_oi_chg":   p_chg,
                    "call_oi":      c_oi,
                    "put_oi":       p_oi,
                })

        pcr_oi  = round(total_put_oi  / total_call_oi,  2) if total_call_oi  else 0.70
        pcr_chg = round(total_put_chg / total_call_chg, 2) if total_call_chg else 0.50

        # ── Compute Max Pain ──────────────────────────────────────────────
        min_payout = float("inf")
        for test_s in strikes_list:
            payout = 0
            for r in records:
                k     = r["strikePrice"]
                c_oi  = r.get("CE", {}).get("openInterest", 0)
                p_oi  = r.get("PE", {}).get("openInterest", 0)
                payout += max(0, test_s - k) * c_oi
                payout += max(0, k - test_s) * p_oi
            if payout < min_payout:
                min_payout      = payout
                max_pain_strike = test_s

        nse_ok = True
        print(f"  NSE LIVE  ATM {atm_strike} | IV {atm_iv} | "
              f"PCR OI {pcr_oi} | Max Pain {max_pain_strike}")

    except Exception as e:
        print(f"  NSE unavailable: {e}")
        print("  Using Yahoo Finance spot and estimated OI values.")

    return atm_strike, atm_iv, pcr_oi, pcr_chg, max_pain_strike, oi_delta, nse_ok


# ─── NSE: INDIA VIX ──────────────────────────────────────────────────────────

def fetch_india_vix_nse(session):
    """Fetch India VIX live from NSE allIndices endpoint."""
    try:
        resp = session.get(
            "https://www.nseindia.com/api/allIndices", timeout=10
        ).json()
        entry = next(
            (i for i in resp["data"] if "VIX" in i.get("index", "")), None
        )
        if entry:
            return round(entry["last"], 2), round(entry.get("percentChange", 0.0), 2)
    except Exception:
        pass
    return None, 0.0


# ─── TECHNICAL LEVELS ────────────────────────────────────────────────────────

def compute_technical_levels(pdh, pdl, pdc, spot):
    """
    Compute CPR (Central Pivot Range) and Camarilla pivot levels
    from previous day's High, Low, and Close.
    """
    pivot     = round((pdh + pdl + pdc) / 3, 2)
    bc        = round((pdh + pdl) / 2,       2)
    tc        = round((pivot - bc) + pivot,   2)
    cpr_width = round(abs(tc - bc),           2)
    day_range = pdh - pdl

    cam_h4 = round(pdc + day_range * 1.1 / 2, 2)
    cam_h3 = round(pdc + day_range * 1.1 / 4, 2)
    cam_l3 = round(pdc - day_range * 1.1 / 4, 2)
    cam_l4 = round(pdc - day_range * 1.1 / 2, 2)

    if cpr_width < 30:
        cpr_type = "Narrow CPR (Trending Day Alert)"
    elif cpr_width < 60:
        cpr_type = "Medium CPR"
    else:
        cpr_type = "Wide CPR (Rangebound Day)"

    return {
        "pivot": pivot, "tc": tc, "bc": bc,
        "width": cpr_width, "type": cpr_type,
        "pdh": pdh, "pdl": pdl, "pdc": pdc,
        "cam_h4": cam_h4, "cam_h3": cam_h3,
        "cam_l3": cam_l3, "cam_l4": cam_l4,
    }


# ─── BIAS SCORE ──────────────────────────────────────────────────────────────

def compute_bias_score(pcr_chg, basis_pct, vix_change, spot, cpr, max_pain, macros):
    """
    Compute a weighted composite bias score from -100 to +100.

    Factor weights:
        PCR Change Direction  : 25 pts
        Futures Basis         : 20 pts
        VIX Spike Direction   : 15 pts
        Spot vs CPR Position  : 15 pts
        Max Pain Magnet       : 10 pts
        Global Macro (DXY)    : 15 pts
    """

    # A: PCR change direction
    if   pcr_chg < 0.65: s_pcr = -25
    elif pcr_chg > 1.20: s_pcr =  25
    else:                s_pcr = int((pcr_chg - 0.90) * 80)

    # B: Futures basis
    if   basis_pct < -0.05: s_basis = -20
    elif basis_pct >  0.20: s_basis =  20
    else:                   s_basis = int(basis_pct * 100)

    # C: VIX spike
    if   vix_change >  5: s_vix = -15
    elif vix_change < -3: s_vix =  10
    else:                 s_vix =   0

    # D: Spot vs CPR position
    bc = cpr["bc"]
    tc = cpr["tc"]
    if   spot < bc: s_cpr = -15
    elif spot > tc: s_cpr =  15
    else:           s_cpr =   0

    # E: Max pain magnet
    mp_dist = max_pain - spot
    if   mp_dist >  50: s_mp = 10
    elif mp_dist < -50: s_mp = -10
    else:               s_mp =  0

    # F: Global macro headwinds
    dxy   = macros.get("dxy",   104.2)
    crude = macros.get("crude",  76.5)
    if dxy > 105 or crude > 80: s_macro = -10
    elif dxy < 103:             s_macro =   5
    else:                       s_macro =   0

    composite = max(-100, min(100,
        s_pcr + s_basis + s_vix + s_cpr + s_mp + s_macro
    ))

    if   composite <= -40: sentiment = "Strongly Bearish"
    elif composite <= -15: sentiment = "Moderately Bearish"
    elif composite <   15: sentiment = "Neutral / Rangebound"
    elif composite <   40: sentiment = "Moderately Bullish"
    else:                  sentiment = "Strongly Bullish"

    conviction = min(95, 50 + abs(composite) // 2)

    return composite, sentiment, conviction, {
        "pcr_signal":    s_pcr,
        "basis_signal":  s_basis,
        "vix_signal":    s_vix,
        "cpr_signal":    s_cpr,
        "maxpain_signal":s_mp,
        "macro_signal":  s_macro,
    }


# ─── STRATEGY SELECTOR ───────────────────────────────────────────────────────

def select_strategy(composite, iv_rank_est, atm_strike):
    """
    Dynamically select the best options strategy based on
    the computed bias direction and IV regime.
    """
    if composite < -15:          # ── BEARISH BIAS ──
        if iv_rank_est < 40:
            return {
                "name":  "Bear Put Debit Spread",
                "type":  "Buy Premium (IV is Cheap)",
                "legs":  f"Buy {atm_strike} PE / Sell {atm_strike - 200} PE",
                "entry": "09:20 to 09:45 AM on VWAP pullback",
                "target":"Rs 140-160",
                "sl":    "Rs 40-50",
                "rr":    "1:2.5 to 1:3.0",
                "greeks":{"delta": -0.32, "theta": 8.5, "vega": -2.1},
            }
        else:
            return {
                "name":  "Bear Call Credit Spread",
                "type":  "Sell Premium (IV is Rich — prefer Credit)",
                "legs":  f"Sell {atm_strike + 100} CE / Buy {atm_strike + 250} CE",
                "entry": "09:20 to 09:45 AM on resistance rejection",
                "target":"Full premium decay",
                "sl":    "Spread doubles in value",
                "rr":    "1:2.0",
                "greeks":{"delta": -0.20, "theta": 14.0, "vega": -4.5},
            }

    elif composite > 15:         # ── BULLISH BIAS ──
        if iv_rank_est < 40:
            return {
                "name":  "Bull Call Debit Spread",
                "type":  "Buy Premium (IV is Cheap)",
                "legs":  f"Buy {atm_strike} CE / Sell {atm_strike + 200} CE",
                "entry": "09:20 to 09:45 AM on support bounce",
                "target":"Rs 140-160",
                "sl":    "Rs 40-50",
                "rr":    "1:2.5 to 1:3.0",
                "greeks":{"delta": 0.30, "theta": 7.8, "vega": -1.8},
            }
        else:
            return {
                "name":  "Bull Put Credit Spread",
                "type":  "Sell Premium (IV is Rich — prefer Credit)",
                "legs":  f"Sell {atm_strike - 100} PE / Buy {atm_strike - 250} PE",
                "entry": "09:20 to 09:45 AM on support hold",
                "target":"Full premium decay",
                "sl":    "Spread doubles in value",
                "rr":    "1:2.0",
                "greeks":{"delta": 0.18, "theta": 12.5, "vega": -3.9},
            }

    else:                        # ── NEUTRAL BIAS ──
        return {
            "name":  "Iron Condor",
            "type":  "Non-Directional (Neutral Bias + Theta Decay)",
            "legs":  (
                f"Sell {atm_strike+200} CE / Buy {atm_strike+300} CE  +  "
                f"Sell {atm_strike-200} PE / Buy {atm_strike-300} PE"
            ),
            "entry": "09:30 to 10:00 AM after Initial Balance forms",
            "target":"60-70% of max premium collected",
            "sl":    "Any short leg breaches its strike",
            "rr":    "1:1.5",
            "greeks":{"delta": 0.02, "theta": 18.0, "vega": -6.2},
        }


# ─── MAIN ORCHESTRATOR ───────────────────────────────────────────────────────

def fetch_and_compute():
    print("\n" + "=" * 55)
    print("  NIFTY BIAS ENGINE v3  —  Starting Data Collection")
    print("=" * 55 + "\n")

    # 1. Nifty OHLC (Yahoo Finance — always works)
    spot, pdh, pdl, pdc = fetch_nifty_ohlc()

    # 2. Technical levels from real OHLC
    cpr = compute_technical_levels(pdh, pdl, pdc, spot)
    print(f"\nCPR  →  Pivot {cpr['pivot']} | TC {cpr['tc']} | BC {cpr['bc']} "
          f"| Width {cpr['width']} pts  |  {cpr['type']}")

    # 3. NSE Option Chain (works from Indian IP only)
    session = get_nse_session()
    atm_strike, atm_iv, pcr_oi, pcr_chg, max_pain, oi_delta, nse_ok = \
        fetch_nse_option_chain(session, spot)

    # 4. India VIX — try NSE first, fall back to Yahoo
    print("\nFetching India VIX...")
    india_vix, vix_change = fetch_india_vix_nse(session) if nse_ok else (None, 0.0)
    if india_vix is None:
        india_vix  = fetch_india_vix_yahoo()
        vix_change = 0.0
        print(f"  VIX (Yahoo fallback): {india_vix}")
    else:
        print(f"  VIX (NSE live): {india_vix}  Change: {vix_change}%")

    # 5. Global macros (Yahoo Finance — always works)
    macros = fetch_yahoo_macros()

    # 6. Expected 1-Day Move
    daily_sigma = (atm_iv / 100) / math.sqrt(252)
    exp_move    = round(spot * daily_sigma)
    print(f"\nExpected Range  →  "
          f"{round(spot - exp_move)} — {round(spot + exp_move)}  "
          f"(+/- {exp_move} pts)")

    # 7. Futures basis (using approximation; real value needs NSE futures feed)
    fut_price  = round(spot + 15, 2)   # ~15 pt premium estimate
    basis_pct  = round(((fut_price - spot) / spot) * 100, 3)

    # 8. IV Rank estimate (simplified: map VIX into 0-100)
    iv_rank_est = max(0, min(100, int((india_vix - 10) / 25 * 100)))

    # 9. Composite Bias Score
    print("\nComputing weighted bias score...")
    composite, sentiment, conviction, factors = compute_bias_score(
        pcr_chg, basis_pct, vix_change, spot, cpr, max_pain, macros
    )
    print(f"  Score: {composite}  |  {sentiment}  |  Conviction: {conviction}%")

    # 10. Dynamic Strategy
    strategy = select_strategy(composite, iv_rank_est, atm_strike)
    print(f"\nRecommended Strategy  →  {strategy['name']}  ({strategy['type']})")

    # ─── Build output JSON ───────────────────────────────────────────────────

    output = {
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
        "nse_live":   nse_ok,

        "spot":       round(spot, 2),

        "futures": {
            "price":     fut_price,
            "basis_pct": basis_pct,
        },

        "bias": {
            "score":        composite,
            "sentiment":    sentiment,
            "conviction":   conviction,
            "invalidation": round(cpr["tc"] + 40),
            "factors":      factors,
        },

        "expected_range": {
            "low":      round(spot - exp_move),
            "high":     round(spot + exp_move),
            "band_pts": exp_move * 2,
        },

        "volatility": {
            "vix":        india_vix,
            "vix_change": vix_change,
            "atm_iv":     atm_iv,
            "iv_rank":    iv_rank_est,
            "regime": (
                "Cheap IV (Favor Debit Spreads)"      if iv_rank_est < 30 else
                "Fair IV (Directional Spreads)"        if iv_rank_est < 55 else
                "Expensive IV (Favor Credit Spreads)"  if iv_rank_est < 75 else
                "Very Expensive IV (Sell Premium Aggressively)"
            ),
        },

        "derivatives": {
            "pcr_oi":       pcr_oi,
            "pcr_chg":      pcr_chg,
            "atm_strike":   atm_strike,
            "max_pain":     max_pain,
            "max_pain_dist":max_pain - round(spot),
            "gex_flip":     round(cpr["pivot"] + (cpr["tc"] - cpr["pivot"]) * 0.6),
            "top_oi_delta": sorted(
                oi_delta,
                key=lambda x: abs(x["call_oi_chg"]) + abs(x["put_oi_chg"]),
                reverse=True,
            )[:8],
        },

        "cpr": cpr,

        "macros": {
            "dxy":    macros.get("dxy",    104.2),
            "crude":  macros.get("crude",   76.5),
            "us10y":  macros.get("us10y",   4.28),
            "usdinr": macros.get("usdinr", 83.85),
        },

        "strategy": strategy,
    }

    # ─── Write to file ───────────────────────────────────────────────────────
    with open("nifty_bias_data.json", "w") as f:
        json.dump(output, f, indent=2)

    print("\n" + "=" * 55)
    print("  nifty_bias_data.json  written successfully!")
    print(f"  NSE Live Data : {'YES' if nse_ok else 'NO (Yahoo Finance fallback)'}")
    print(f"  Spot          : {output['spot']}")
    print(f"  Bias Score    : {composite}  ({sentiment})")
    print(f"  Strategy      : {strategy['name']}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    fetch_and_compute()
