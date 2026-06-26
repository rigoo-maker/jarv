"""Signal-scoring engine.

Aggregates the advanced-TA snapshot into a single weighted score in [-100, +100]
(strong bear .. strong bull) plus a per-indicator breakdown, so you can see WHY
the engine leans a direction. Weights are tunable.

This is a confluence model, not a crystal ball. A high score means many
indicators agree right now — it is not a prediction and not financial advice.
"""

from __future__ import annotations

DEFAULT_WEIGHTS = {
    "trend_ema": 1.5,     # ema9 vs ema21 vs ema50 stack
    "macd": 1.2,          # macd vs signal + histogram sign
    "rsi": 1.0,           # momentum / overbought-oversold
    "stoch_rsi": 0.8,     # faster momentum
    "bollinger": 0.9,     # mean-reversion / band position
    "vwap": 1.0,          # price vs fair value
    "adx": 1.1,           # trend strength gate (+DI/-DI)
}


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def score_snapshot(latest: dict, weights: dict = None) -> dict:
    """Return {score, label, bias, components:[{name,signal,detail}]}."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    comps = []

    price = latest.get("price")
    e9, e21, e50 = latest.get("ema9"), latest.get("ema21"), latest.get("ema50")
    rsi_v = latest.get("rsi")
    macd_v, macd_sig, macd_h = latest.get("macd"), latest.get("macd_signal"), latest.get("macd_hist")
    bb_u, bb_m, bb_l = latest.get("bb_upper"), latest.get("bb_mid"), latest.get("bb_lower")
    vwap_v = latest.get("vwap")
    sk, sd = latest.get("stoch_k"), latest.get("stoch_d")
    adx_v, pdi, mdi = latest.get("adx"), latest.get("plus_di"), latest.get("minus_di")

    # --- EMA trend stack ---
    if None not in (e9, e21, e50):
        if e9 > e21 > e50:
            s, d = 1.0, "EMA9>EMA21>EMA50 (clean uptrend)"
        elif e9 < e21 < e50:
            s, d = -1.0, "EMA9<EMA21<EMA50 (clean downtrend)"
        elif e9 > e21:
            s, d = 0.4, "EMA9>EMA21 (short-term up)"
        else:
            s, d = -0.4, "EMA9<EMA21 (short-term down)"
        comps.append(("trend_ema", s, d))

    # --- MACD ---
    if None not in (macd_v, macd_sig, macd_h):
        s = _clamp((1 if macd_v > macd_sig else -1) * 0.6 + (0.4 if macd_h > 0 else -0.4))
        comps.append(("macd", s, f"MACD {'>' if macd_v > macd_sig else '<'} signal, hist {macd_h:+.4f}"))

    # --- RSI ---
    if rsi_v is not None:
        if rsi_v >= 70:
            s, d = -0.6, f"RSI {rsi_v:.0f} overbought (fade)"
        elif rsi_v <= 30:
            s, d = 0.6, f"RSI {rsi_v:.0f} oversold (bounce)"
        else:
            s, d = _clamp((rsi_v - 50) / 25), f"RSI {rsi_v:.0f}"
        comps.append(("rsi", s, d))

    # --- Stochastic RSI ---
    if None not in (sk, sd):
        if sk > sd and sk < 80:
            s, d = 0.6, f"StochRSI K>D ({sk:.0f}/{sd:.0f}) rising"
        elif sk < sd and sk > 20:
            s, d = -0.6, f"StochRSI K<D ({sk:.0f}/{sd:.0f}) falling"
        else:
            s, d = _clamp((sk - 50) / 50), f"StochRSI {sk:.0f}/{sd:.0f}"
        comps.append(("stoch_rsi", s, d))

    # --- Bollinger position (mean reversion) ---
    if None not in (price, bb_u, bb_m, bb_l) and bb_u > bb_l:
        pos = (price - bb_m) / ((bb_u - bb_l) / 2)  # -1 lower band .. +1 upper band
        if pos > 0.9:
            s, d = -0.5, "At/above upper band (stretched)"
        elif pos < -0.9:
            s, d = 0.5, "At/below lower band (stretched)"
        else:
            s, d = _clamp(pos * 0.5), f"Band pos {pos:+.2f}"
        comps.append(("bollinger", s, d))

    # --- VWAP ---
    if None not in (price, vwap_v) and vwap_v:
        dev = (price - vwap_v) / vwap_v * 100
        s = _clamp(dev / 1.0)  # +/-1% = full tilt
        comps.append(("vwap", s, f"Price {dev:+.2f}% vs VWAP"))

    # --- ADX trend-strength gate ---
    if None not in (adx_v, pdi, mdi):
        strong = adx_v >= 25
        direction = 1.0 if pdi > mdi else -1.0
        s = direction * (0.8 if strong else 0.2)
        comps.append(("adx", s, f"ADX {adx_v:.0f} {'(strong)' if strong else '(weak)'}, "
                                f"{'+DI' if pdi > mdi else '-DI'} leads"))

    # --- weighted aggregate ---
    num = sum(w.get(name, 1.0) * sig for name, sig, _ in comps)
    den = sum(abs(w.get(name, 1.0)) for name, _, _ in comps) or 1.0
    score = round(num / den * 100, 1)

    if score >= 50:
        label, bias = "STRONG BUY", "bull"
    elif score >= 20:
        label, bias = "BUY", "bull"
    elif score <= -50:
        label, bias = "STRONG SELL", "bear"
    elif score <= -20:
        label, bias = "SELL", "bear"
    else:
        label, bias = "NEUTRAL", "flat"

    return {
        "score": score,
        "label": label,
        "bias": bias,
        "components": [{"name": n, "signal": round(s, 2), "detail": d} for n, s, d in comps],
    }
