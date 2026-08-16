"""NinjaScript (NinjaTrader 8) exporter.

The maps say which strategies still have an edge; this turns those into C#
strategy files you can drop into NinjaTrader and run on a chart.

Two things make the generated code more than a template dump:

1. **Same logic, same convention.** Every rule mirrors `krypt/strats.py`
   one-for-one, on bar close, with the same thresholds — so the NinjaTrader
   chart is trading what the backtest measured, not a lookalike. Where
   NinjaTrader's indicator differs in scale (its StochRSI is 0..1, KRYPT's is
   0..100) the generated code converts rather than silently changing the rule.

2. **The evidence rides along.** Each file's header carries what the analysis
   actually measured for that strategy on that sample: edge score and verdict,
   out-of-sample Sharpe, the regime where its money came from, and the cost
   level at which it stopped working. When you open the file in six months, the
   reason it exists is still in it.

Every strategy ships a regime filter derived from the regime map (reversion
gated to low ADX / chop, trend gated to high ADX), a percent stop and target,
and a long-only switch — all editable in NinjaTrader's strategy dialog.

Generated code is a STARTING POINT for NinjaTrader's own Strategy Analyzer.
Different venue, different fees, different fills, different data. Re-verify
there before it touches an account.
"""

from __future__ import annotations

import os
import time

NT_HEADER = """#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion
"""


# --------------------------------------------------------------- param helper

def P(name, ctype, default, lo, hi, display, order, group="1. Strategy"):
    return {"name": name, "type": ctype, "default": default, "lo": lo, "hi": hi,
            "display": display, "order": order, "group": group}


PROP_PARAMS = [
    P("UsePropGuard", "bool", True, 0, 1, "Enforce prop-firm rules", 60,
      "5. Prop firm"),
    P("PropStartBalance", "double", 50000, 0, 10_000_000, "Account starting balance",
      61, "5. Prop firm"),
    P("TrailingDrawdown", "double", 2500, 0, 1_000_000, "Trailing drawdown ($)",
      62, "5. Prop firm"),
    P("TrailOnUnrealized", "bool", True, 0, 1,
      "Trail on unrealized equity (Apex) vs end-of-day (Topstep)", 63, "5. Prop firm"),
    P("LockThresholdAt", "double", 50100, 0, 10_000_000,
      "Threshold locks at ($; 0 = never)", 64, "5. Prop firm"),
    P("DailyLossLimit", "double", 0, 0, 1_000_000,
      "Daily loss limit ($; 0 = none)", 65, "5. Prop firm"),
    P("PropBuffer", "double", 100, 0, 100_000, "Stop this far short ($)", 66,
      "5. Prop firm"),
    P("FlattenTime", "int", 155500, 0, 235959,
      "Flatten at (HHmmss, chart time; 0 = off)", 67, "5. Prop firm"),
]

COMMON = [
    P("Quantity", "int", 1, 1, 1000000, "Quantity", 90, "3. Execution"),
    P("StopLossPercent", "double", 1.5, 0.05, 50, "Stop loss (%)", 91, "3. Execution"),
    P("ProfitTargetPercent", "double", 2.0, 0.05, 100, "Profit target (%)", 92, "3. Execution"),
]


def _prop(p):
    rng = (f'\t\t[Range({p["lo"]}, {p["hi"]})]\n' if p["type"] != "bool" else "")
    return (f'\t\t[NinjaScriptProperty]\n{rng}'
            f'\t\t[Display(Name = "{p["display"]}", GroupName = "{p["group"]}", '
            f'Order = {p["order"]})]\n'
            f'\t\tpublic {p["type"]} {p["name"]} {{ get; set; }}\n')


def _default_assign(p):
    v = p["default"]
    if p["type"] == "bool":
        v = "true" if v else "false"
    elif p["type"] == "double":
        v = f"{float(v)}"
    return f"\t\t\t\t{p['name']} = {v};"


# --------------------------------------------------------- per-strategy specs
# Each spec: params, indicator field declarations, DataLoaded init, and the
# signal block that must set `signal` to 1 / 0 / -1 exactly like strats.py.

def _spec(name):
    if name == "ema_cross":
        return {
            "cls": "KryptEmaCross", "family": "trend",
            "desc": "EMA(fast) vs EMA(slow) trend following — KRYPT ema_cross.",
            "params": [P("FastPeriod", "int", 9, 1, 500, "Fast EMA", 1),
                       P("SlowPeriod", "int", 21, 2, 1000, "Slow EMA", 2)],
            "fields": "\t\tprivate EMA emaFast;\n\t\tprivate EMA emaSlow;\n",
            "init": "\t\t\t\temaFast = EMA(Close, FastPeriod);\n"
                    "\t\t\t\temaSlow = EMA(Close, SlowPeriod);\n",
            "signal": "\t\t\tsignal = emaFast[0] > emaSlow[0] ? 1 : -1;\n",
            "warmup": "SlowPeriod + 5",
        }
    if name == "macd_cross":
        return {
            "cls": "KryptMacdCross", "family": "trend",
            "desc": "MACD line vs signal line — KRYPT macd_cross.",
            "params": [P("Fast", "int", 12, 1, 500, "MACD fast", 1),
                       P("Slow", "int", 26, 2, 1000, "MACD slow", 2),
                       P("Smooth", "int", 9, 1, 500, "MACD signal", 3)],
            "fields": "\t\tprivate MACD macdInd;\n",
            "init": "\t\t\t\tmacdInd = MACD(Close, Fast, Slow, Smooth);\n",
            "signal": "\t\t\tsignal = macdInd.Default[0] > macdInd.Avg[0] ? 1 : -1;\n",
            "warmup": "Slow + Smooth + 5",
        }
    if name == "rsi_reversion":
        return {
            "cls": "KryptRsiReversion", "family": "reversion",
            "desc": "RSI mean reversion — buy oversold, fade overbought. KRYPT rsi_reversion.",
            "params": [P("RsiPeriod", "int", 14, 2, 500, "RSI period", 1),
                       P("Oversold", "double", 30, 1, 49, "Oversold (long below)", 2),
                       P("Overbought", "double", 70, 51, 99, "Overbought (short above)", 3)],
            "fields": "\t\tprivate RSI rsiInd;\n",
            "init": "\t\t\t\trsiInd = RSI(Close, RsiPeriod, 3);\n",
            "signal": ("\t\t\tdouble r = rsiInd[0];\n"
                       "\t\t\tif (r <= Oversold) signal = 1;\n"
                       "\t\t\telse if (r >= Overbought) signal = -1;\n"
                       "\t\t\telse signal = 0;\n"),
            "warmup": "RsiPeriod + 10",
        }
    if name == "bb_breakout":
        return {
            "cls": "KryptBollingerBreakout", "family": "trend",
            "desc": "Bollinger band breakout (volatility expansion) — KRYPT bb_breakout.",
            "params": [P("BbPeriod", "int", 20, 2, 500, "Bollinger period", 1),
                       P("BbStdDev", "double", 2.0, 0.1, 6, "Std deviations", 2)],
            "fields": "\t\tprivate Bollinger bb;\n",
            "init": "\t\t\t\tbb = Bollinger(Close, BbStdDev, BbPeriod);\n",
            "signal": ("\t\t\tif (Close[0] > bb.Upper[0]) signal = 1;\n"
                       "\t\t\telse if (Close[0] < bb.Lower[0]) signal = -1;\n"
                       "\t\t\telse signal = 0;\n"),
            "warmup": "BbPeriod + 5",
        }
    if name == "bb_reversion":
        return {
            "cls": "KryptBollingerReversion", "family": "reversion",
            "desc": "Bollinger band mean reversion — fade the bands. KRYPT bb_reversion.",
            "params": [P("BbPeriod", "int", 20, 2, 500, "Bollinger period", 1),
                       P("BbStdDev", "double", 2.0, 0.1, 6, "Std deviations", 2)],
            "fields": "\t\tprivate Bollinger bb;\n",
            "init": "\t\t\t\tbb = Bollinger(Close, BbStdDev, BbPeriod);\n",
            "signal": ("\t\t\tif (Close[0] <= bb.Lower[0]) signal = 1;\n"
                       "\t\t\telse if (Close[0] >= bb.Upper[0]) signal = -1;\n"
                       "\t\t\telse signal = 0;\n"),
            "warmup": "BbPeriod + 5",
        }
    if name == "donchian_breakout":
        return {
            "cls": "KryptDonchianBreakout", "family": "trend",
            "desc": "Donchian channel breakout of the PRIOR N bars — KRYPT donchian_breakout.",
            "params": [P("Channel", "int", 20, 2, 500, "Channel length", 1)],
            "fields": "\t\tprivate MAX donHigh;\n\t\tprivate MIN donLow;\n",
            "init": "\t\t\t\tdonHigh = MAX(High, Channel);\n"
                    "\t\t\t\tdonLow  = MIN(Low, Channel);\n",
            # [1] excludes the current bar, matching strats.py's prior-N window
            "signal": ("\t\t\tdouble hi = donHigh[1];\n"
                       "\t\t\tdouble lo = donLow[1];\n"
                       "\t\t\tif (Close[0] >= hi) signal = 1;\n"
                       "\t\t\telse if (Close[0] <= lo) signal = -1;\n"
                       "\t\t\telse signal = 0;\n"),
            "warmup": "Channel + 5",
        }
    if name == "vwap_reversion":
        return {
            "cls": "KryptVwapReversion", "family": "reversion",
            "desc": ("Session-VWAP mean reversion — fade price stretched from VWAP. "
                     "KRYPT vwap_reversion."),
            "params": [P("BandPercent", "double", 0.4, 0.01, 20,
                         "Band from VWAP (%)", 1)],
            # NinjaTrader's built-in VWAP is not available in every edition, so the
            # session VWAP is accumulated here — no licensing surprises at compile.
            "fields": ("\t\tprivate double cumPv;\n\t\tprivate double cumVol;\n"
                       "\t\tprivate double vwap;\n"),
            "init": "\t\t\t\tcumPv = 0; cumVol = 0; vwap = 0;\n",
            "signal": ("\t\t\tif (Bars.IsFirstBarOfSession) { cumPv = 0; cumVol = 0; }\n"
                       "\t\t\tdouble typical = (High[0] + Low[0] + Close[0]) / 3.0;\n"
                       "\t\t\tcumPv  += typical * Volume[0];\n"
                       "\t\t\tcumVol += Volume[0];\n"
                       "\t\t\tvwap = cumVol > 0 ? cumPv / cumVol : Close[0];\n"
                       "\t\t\tdouble k = BandPercent / 100.0;\n"
                       "\t\t\tif (vwap <= 0) signal = 0;\n"
                       "\t\t\telse if (Close[0] < vwap * (1 - k)) signal = 1;\n"
                       "\t\t\telse if (Close[0] > vwap * (1 + k)) signal = -1;\n"
                       "\t\t\telse signal = 0;\n"),
            "warmup": "20",
        }
    if name == "stochrsi_cross":
        return {
            "cls": "KryptStochRsiCross", "family": "momentum",
            "desc": "Stochastic-RSI K/D cross with extreme guards — KRYPT stochrsi_cross.",
            "params": [P("StochPeriod", "int", 14, 2, 500, "StochRSI period", 1),
                       P("DSmooth", "int", 3, 1, 100, "D smoothing", 2),
                       P("UpperGuard", "double", 80, 51, 100, "No longs above K", 3),
                       P("LowerGuard", "double", 20, 0, 49, "No shorts below K", 4)],
            "fields": "\t\tprivate StochRSI srsi;\n\t\tprivate SMA srsiAvg;\n",
            "init": "\t\t\t\tsrsi = StochRSI(Close, StochPeriod);\n"
                    "\t\t\t\tsrsiAvg = SMA(srsi, DSmooth);\n",
            # NinjaTrader's StochRSI is 0..1; KRYPT's is 0..100 — convert, don't
            # quietly redefine the thresholds.
            "signal": ("\t\t\tdouble k = srsi[0] * 100.0;\n"
                       "\t\t\tdouble d = srsiAvg[0] * 100.0;\n"
                       "\t\t\tif (k > d && k < UpperGuard) signal = 1;\n"
                       "\t\t\telse if (k < d && k > LowerGuard) signal = -1;\n"
                       "\t\t\telse signal = 0;\n"),
            "warmup": "StochPeriod * 2 + DSmooth + 5",
        }
    if name == "adx_di":
        return {
            "cls": "KryptAdxDi", "family": "trend",
            "desc": "ADX-gated DI cross — trade only when the trend is strong. KRYPT adx_di.",
            "params": [P("AdxPeriod", "int", 14, 2, 500, "ADX / DM period", 1),
                       P("AdxEntry", "double", 25, 1, 100, "Min ADX to trade", 2)],
            "fields": "\t\tprivate ADX adxSig;\n\t\tprivate DM dmInd;\n",
            "init": "\t\t\t\tadxSig = ADX(Close, AdxPeriod);\n"
                    "\t\t\t\tdmInd = DM(AdxPeriod);\n",
            "signal": ("\t\t\tif (adxSig[0] < AdxEntry) signal = 0;\n"
                       "\t\t\telse signal = dmInd.DiPlus[0] > dmInd.DiMinus[0] ? 1 : -1;\n"),
            "warmup": "AdxPeriod * 3 + 5",
        }
    if name == "confluence":
        return {
            "cls": "KryptConfluence", "family": "ensemble",
            "desc": ("Weighted confluence score of EMA stack, MACD, RSI, StochRSI, "
                     "Bollinger, VWAP and ADX/DI — a port of KRYPT scoring.py."),
            "params": [P("EnterScore", "double", 35, 1, 100,
                         "Score to enter (+long / -short)", 1)],
            "fields": ("\t\tprivate EMA e9, e21, e50;\n\t\tprivate MACD macdInd;\n"
                       "\t\tprivate RSI rsiInd;\n\t\tprivate StochRSI srsi;\n"
                       "\t\tprivate SMA srsiAvg;\n\t\tprivate Bollinger bb;\n"
                       "\t\tprivate ADX adxSig;\n\t\tprivate DM dmInd;\n"
                       "\t\tprivate double cumPv, cumVol;\n"),
            "init": ("\t\t\t\te9 = EMA(Close, 9); e21 = EMA(Close, 21); e50 = EMA(Close, 50);\n"
                     "\t\t\t\tmacdInd = MACD(Close, 12, 26, 9);\n"
                     "\t\t\t\trsiInd = RSI(Close, 14, 3);\n"
                     "\t\t\t\tsrsi = StochRSI(Close, 14);\n"
                     "\t\t\t\tsrsiAvg = SMA(srsi, 3);\n"
                     "\t\t\t\tbb = Bollinger(Close, 2, 20);\n"
                     "\t\t\t\tadxSig = ADX(Close, 14);\n\t\t\t\tdmInd = DM(14);\n"
                     "\t\t\t\tcumPv = 0; cumVol = 0;\n"),
            "signal": ("\t\t\tdouble score = ConfluenceScore();\n"
                       "\t\t\tif (score >= EnterScore) signal = 1;\n"
                       "\t\t\telse if (score <= -EnterScore) signal = -1;\n"
                       "\t\t\telse signal = 0;\n"),
            "warmup": "80",
        }
    return _equity_spec(name)


def _equity_spec(name):
    """US-equity daily rules. These gate themselves on the 200-day line, so they
    do not get the ADX regime filter the crypto templates carry."""
    if name == "sma200_trend":
        return {
            "cls": "KryptSma200Trend", "family": "trend", "regime": False,
            "desc": "Long while price is above its 200-day average. KRYPT sma200_trend.",
            "params": [P("TrendLen", "int", 200, 5, 1000, "Trend SMA length", 1)],
            "fields": "\t\tprivate SMA trendMa;\n",
            "init": "\t\t\t\ttrendMa = SMA(Close, TrendLen);\n",
            "signal": "\t\t\tsignal = Close[0] > trendMa[0] ? 1 : 0;\n",
            "warmup": "TrendLen + 5",
        }
    if name == "golden_cross":
        return {
            "cls": "KryptGoldenCross", "family": "trend", "regime": False,
            "desc": "Long while the 50-day is above the 200-day. KRYPT golden_cross.",
            "params": [P("FastLen", "int", 50, 2, 500, "Fast SMA", 1),
                       P("SlowLen", "int", 200, 5, 1000, "Slow SMA", 2)],
            "fields": "\t\tprivate SMA fastMa;\n\t\tprivate SMA slowMa;\n",
            "init": "\t\t\t\tfastMa = SMA(Close, FastLen);\n"
                    "\t\t\t\tslowMa = SMA(Close, SlowLen);\n",
            "signal": "\t\t\tsignal = fastMa[0] > slowMa[0] ? 1 : 0;\n",
            "warmup": "SlowLen + 5",
        }
    if name == "connors_rsi2":
        return {
            "cls": "KryptConnorsRsi2", "family": "reversion", "regime": False,
            "desc": ("RSI(2) oversold inside a 200-day uptrend, exit on the 5-day "
                     "mean. KRYPT connors_rsi2."),
            "params": [P("RsiLen", "int", 2, 1, 50, "RSI length", 1),
                       P("Oversold", "double", 10, 1, 50, "Entry below RSI", 2),
                       P("TrendLen", "int", 200, 5, 1000, "Trend SMA", 3),
                       P("ExitLen", "int", 5, 2, 100, "Exit SMA", 4)],
            "fields": ("\t\tprivate RSI rsiFast;\n\t\tprivate SMA trendMa, exitMa;\n"
                       "\t\tprivate int connorsPos;\n"),
            "init": ("\t\t\t\trsiFast = RSI(Close, RsiLen, 1);\n"
                     "\t\t\t\ttrendMa = SMA(Close, TrendLen);\n"
                     "\t\t\t\texitMa = SMA(Close, ExitLen);\n"
                     "\t\t\t\tconnorsPos = 0;\n"),
            # the state machine lives in a field: entry and exit are different
            # conditions, so a stateless "is oversold now" rule is a different
            # strategy from the one that was backtested
            "signal": ("\t\t\tif (connorsPos == 0)\n"
                       "\t\t\t{\n"
                       "\t\t\t\tif (Close[0] > trendMa[0] && rsiFast[0] < Oversold)\n"
                       "\t\t\t\t\tconnorsPos = 1;\n"
                       "\t\t\t}\n"
                       "\t\t\telse if (Close[0] > exitMa[0] || Close[0] < trendMa[0])\n"
                       "\t\t\t\tconnorsPos = 0;\n"
                       "\t\t\tsignal = connorsPos;\n"),
            "warmup": "TrendLen + 10",
        }
    if name == "ibs_reversion":
        return {
            "cls": "KryptIbsReversion", "family": "reversion", "regime": False,
            "desc": ("Internal Bar Strength: close in the bottom of the day's range "
                     "inside an uptrend, one-bar hold. KRYPT ibs_reversion."),
            "params": [P("IbsLevel", "double", 0.2, 0.01, 0.9, "IBS below", 1),
                       P("TrendLen", "int", 200, 5, 1000, "Trend SMA", 2)],
            "fields": "\t\tprivate SMA trendMa;\n",
            "init": "\t\t\t\ttrendMa = SMA(Close, TrendLen);\n",
            "signal": ("\t\t\tdouble rng = High[0] - Low[0];\n"
                       "\t\t\tdouble ibs = rng > 0 ? (Close[0] - Low[0]) / rng : 0.5;\n"
                       "\t\t\tsignal = (ibs < IbsLevel && Close[0] > trendMa[0]) ? 1 : 0;\n"),
            "warmup": "TrendLen + 5",
        }
    if name == "gap_fade":
        return {
            "cls": "KryptGapFade", "family": "reversion", "regime": False,
            "desc": "Fade a down-gap while the 200-day trend holds. KRYPT gap_fade.",
            "params": [P("GapPercent", "double", 1.0, 0.05, 20, "Down-gap size (%)", 1),
                       P("TrendLen", "int", 200, 5, 1000, "Trend SMA", 2)],
            "fields": "\t\tprivate SMA trendMa;\n",
            "init": "\t\t\t\ttrendMa = SMA(Close, TrendLen);\n",
            "signal": ("\t\t\tdouble gap = Close[1] > 0 ? (Open[0] / Close[1] - 1) * 100 : 0;\n"
                       "\t\t\tsignal = (gap < -GapPercent && Close[0] > trendMa[0]) ? 1 : 0;\n"),
            "warmup": "TrendLen + 5",
        }
    if name == "turn_of_month":
        return {
            "cls": "KryptTurnOfMonth", "family": "calendar", "regime": False,
            "desc": ("Hold across the turn of the month. KRYPT turn_of_month.\n"
                     "//  NOTE: the Python version counts TRADING days inside the "
                     "month; a live\n//  strategy cannot know which day is the last "
                     "one until it has passed, so\n//  this uses calendar days near "
                     "the boundary instead. Same idea, not the\n//  same rule - "
                     "expect small differences from the backtest."),
            "params": [P("DaysBefore", "int", 3, 1, 10, "Calendar days before month end", 1),
                       P("DaysAfter", "int", 3, 1, 10, "Trading days into the month", 2)],
            "fields": "",
            "init": "",
            "signal": ("\t\t\tint dom = Time[0].Day;\n"
                       "\t\t\tint dim = DateTime.DaysInMonth(Time[0].Year, Time[0].Month);\n"
                       "\t\t\tsignal = (dom > dim - DaysBefore || dom <= DaysAfter) ? 1 : 0;\n"),
            "warmup": "5",
        }
    if name == "momentum_12_1":
        return {
            "cls": "KryptMomentum121", "family": "trend", "regime": False,
            "desc": ("12-month momentum skipping the last month. KRYPT momentum_12_1."),
            "params": [P("LookbackBars", "int", 252, 20, 2000, "Lookback (bars)", 1),
                       P("SkipBars", "int", 21, 0, 200, "Skip recent (bars)", 2)],
            "fields": "",
            "init": "",
            "signal": ("\t\t\tdouble past = Close[LookbackBars + SkipBars];\n"
                       "\t\t\tsignal = (past > 0 && Close[SkipBars] / past - 1 > 0) ? 1 : 0;\n"),
            "warmup": "LookbackBars + SkipBars + 5",
        }
    if name == "high52_breakout":
        return {
            "cls": "KryptHigh52Breakout", "family": "trend", "regime": False,
            "desc": ("New 52-week high, held until the 200-day line breaks. "
                     "KRYPT high52_breakout."),
            "params": [P("Lookback", "int", 252, 20, 2000, "Highest-high lookback", 1),
                       P("TrendLen", "int", 200, 5, 1000, "Exit SMA", 2)],
            "fields": ("\t\tprivate MAX hiN;\n\t\tprivate SMA trendMa;\n"
                       "\t\tprivate int breakoutPos;\n"),
            "init": ("\t\t\t\thiN = MAX(High, Lookback);\n"
                     "\t\t\t\ttrendMa = SMA(Close, TrendLen);\n"
                     "\t\t\t\tbreakoutPos = 0;\n"),
            "signal": ("\t\t\tif (breakoutPos == 0)\n"
                       "\t\t\t{\n"
                       "\t\t\t\tif (Close[0] >= hiN[1]) breakoutPos = 1;\n"
                       "\t\t\t}\n"
                       "\t\t\telse if (Close[0] < trendMa[0]) breakoutPos = 0;\n"
                       "\t\t\tsignal = breakoutPos;\n"),
            "warmup": "Math.Max(Lookback, TrendLen) + 5",
        }
    if name in ("vix_calm", "vix_spike_reversal"):
        raise KeyError(
            f"'{name}' needs a VIX data series. NinjaTrader can add one with "
            "AddDataSeries(\"^VIX\"), but availability depends on your data "
            "provider, so it is not generated blind - wire it up by hand if your "
            "feed carries VIX.")
    raise KeyError(f"no NinjaScript template for strategy '{name}'")


PROP_FIELDS = """		private double propPeak, propDayStart;
		private bool propHalted, propInit;
		private DateTime propDay;
"""

PROP_METHOD = r"""
		/// <summary>
		/// Prop-firm guardrails (Apex / Topstep style), enforced bar by bar.
		/// Returns false when trading must stop.
		///
		/// The trailing drawdown is the rule that ends most evaluations, and it is
		/// the one people model wrong: at Apex it follows your UNREALIZED equity
		/// high, so an open winner raises the kill line and giving that winner back
		/// breaches it even though the day is green. Topstep instead trails the
		/// END-OF-DAY balance and adds an intraday daily loss limit. Both are
		/// selectable below; set them to the account you actually have.
		///
		/// The buffer exists because being flat one tick before the line is the same
		/// as being flat one tick after it, except the account still exists.
		/// </summary>
		private bool PropGuardOk()
		{
			if (!UsePropGuard) return true;

			double realized = SystemPerformance.AllTrades.TradesPerformance.Currency.CumProfit;
			double open = Position.MarketPosition == MarketPosition.Flat ? 0
				: Position.GetUnrealizedProfitLoss(PerformanceUnit.Currency, Close[0]);
			double equity = PropStartBalance + realized + open;

			if (!propInit)
			{
				propPeak = PropStartBalance;
				propDayStart = equity;
				propDay = Time[0].Date;
				propInit = true;
			}

			// day roll: Topstep-style trailing updates here, and the daily loss
			// limit resets from the new session's opening equity
			if (Time[0].Date != propDay)
			{
				if (!TrailOnUnrealized) propPeak = Math.Max(propPeak, equity);
				propDay = Time[0].Date;
				propDayStart = equity;
			}

			if (TrailOnUnrealized) propPeak = Math.Max(propPeak, equity);

			// the threshold stops trailing once it reaches the lock level
			double threshold = propPeak - TrailingDrawdown;
			if (LockThresholdAt > 0) threshold = Math.Min(threshold, LockThresholdAt);

			if (equity <= threshold + PropBuffer)
			{
				PropStop(string.Format("trailing drawdown: equity {0:C0} vs threshold {1:C0}",
					equity, threshold));
				return false;
			}
			if (DailyLossLimit > 0 && (equity - propDayStart) <= -(DailyLossLimit - PropBuffer))
			{
				PropStop(string.Format("daily loss limit: {0:C0} today",
					equity - propDayStart));
				return false;
			}

			// flat before the session close - no prop account allows overnight risk
			if (FlattenTime > 0 && ToTime(Time[0]) >= FlattenTime)
			{
				FlattenNow("session close");
				return false;
			}
			return true;
		}

		private void PropStop(string why)
		{
			if (!propHalted)
			{
				propHalted = true;
				Print(Time[0] + "  PROP GUARD STOP - " + why);
			}
			FlattenNow(why);
		}

		private void FlattenNow(string why)
		{
			if (Position.MarketPosition == MarketPosition.Long) ExitLong("PropFlat", "");
			else if (Position.MarketPosition == MarketPosition.Short) ExitShort("PropFlat", "");
		}
"""

CONFLUENCE_METHOD = r"""
		/// <summary>
		/// Port of KRYPT scoring.py: each component votes in [-1, +1], the votes are
		/// combined with the same weights, and the result is scaled to [-100, +100].
		/// Weights and thresholds match the Python engine exactly — change them in
		/// both places or the backtest stops describing this strategy.
		/// </summary>
		private double ConfluenceScore()
		{
			double num = 0, den = 0;

			// EMA trend stack (weight 1.5)
			double s;
			if (e9[0] > e21[0] && e21[0] > e50[0]) s = 1.0;
			else if (e9[0] < e21[0] && e21[0] < e50[0]) s = -1.0;
			else s = e9[0] > e21[0] ? 0.4 : -0.4;
			num += 1.5 * s; den += 1.5;

			// MACD (weight 1.2)
			double hist = macdInd.Diff[0];
			s = (macdInd.Default[0] > macdInd.Avg[0] ? 0.6 : -0.6) + (hist > 0 ? 0.4 : -0.4);
			s = Math.Max(-1, Math.Min(1, s));
			num += 1.2 * s; den += 1.2;

			// RSI (weight 1.0)
			double r = rsiInd[0];
			if (r >= 70) s = -0.6;
			else if (r <= 30) s = 0.6;
			else s = Math.Max(-1, Math.Min(1, (r - 50) / 25.0));
			num += 1.0 * s; den += 1.0;

			// Stochastic RSI (weight 0.8) — NinjaTrader's StochRSI is 0..1, KRYPT's 0..100
			double k = srsi[0] * 100.0, d = srsiAvg[0] * 100.0;
			if (k > d && k < 80) s = 0.6;
			else if (k < d && k > 20) s = -0.6;
			else s = Math.Max(-1, Math.Min(1, (k - 50) / 50.0));
			num += 0.8 * s; den += 0.8;

			// Bollinger band position (weight 0.9)
			double halfWidth = (bb.Upper[0] - bb.Lower[0]) / 2.0;
			if (halfWidth > 0)
			{
				double pos = (Close[0] - bb.Middle[0]) / halfWidth;
				if (pos > 0.9) s = -0.5;
				else if (pos < -0.9) s = 0.5;
				else s = Math.Max(-1, Math.Min(1, pos * 0.5));
				num += 0.9 * s; den += 0.9;
			}

			// VWAP deviation (weight 1.0) — session VWAP accumulated locally
			if (Bars.IsFirstBarOfSession) { cumPv = 0; cumVol = 0; }
			cumPv += ((High[0] + Low[0] + Close[0]) / 3.0) * Volume[0];
			cumVol += Volume[0];
			if (cumVol > 0)
			{
				double vw = cumPv / cumVol;
				if (vw > 0)
				{
					double dev = (Close[0] - vw) / vw * 100.0;
					s = Math.Max(-1, Math.Min(1, dev));
					num += 1.0 * s; den += 1.0;
				}
			}

			// ADX trend-strength gate (weight 1.1)
			double dir = dmInd.DiPlus[0] > dmInd.DiMinus[0] ? 1.0 : -1.0;
			s = dir * (adxSig[0] >= 25 ? 0.8 : 0.2);
			num += 1.1 * s; den += 1.1;

			return den > 0 ? num / den * 100.0 : 0.0;
		}
"""


# ----------------------------------------------------------------- the writer

def _evidence_block(name, report):
    """The measured case for (or against) this strategy, as a header comment."""
    if not report:
        return ("//   No measured stats embedded — generated with --no-analysis.\n"
                "//   Run `python3 -m krypt.app heatmap` and regenerate to bake the\n"
                "//   evidence into this header.\n")
    meta = report["meta"]
    row = next((r for r in report["edge"]["rows"] if r["strategy"] == name), None)
    lines = [f"//   Sample      : {meta['symbol']} {meta['interval']}, "
             f"{meta['candles']:,} candles, "
             f"{time.strftime('%Y-%m-%d', time.gmtime(meta['t0']))} -> "
             f"{time.strftime('%Y-%m-%d', time.gmtime(meta['t1']))} UTC",
             f"//   Costs       : {meta['fee_bps']} bps fee + {meta['slippage_bps']} "
             f"bps slippage per side"]
    if row:
        lines += [
            f"//   Edge score  : {row['score']}/100  ->  {row['verdict']}",
            f"//   Sharpe(ann) : full {row['full_sharpe']} | in-sample {row['is_sharpe']} "
            f"| OUT-OF-SAMPLE {row['oos_sharpe']}",
            f"//   Out-of-sample return {row['oos_return_pct']}% over the held-out tail",
            f"//   Consistency : {row['hit_rate']} of windows profitable, "
            f"decay {row['decay']} (negative = fading)",
            f"//   Full sample : {row['full_return_pct']}% return, "
            f"max drawdown {row['max_dd_pct']}%, {row['trades']} trades",
        ]
    # best / worst regime for this strategy
    reg = report.get("regimes") or {}
    if reg.get("cols"):
        rrow = next((x for x in reg["rows"] if x["strategy"] == name), None)
        if rrow:
            pairs = sorted(zip(reg["cols"], rrow["cells"]),
                           key=lambda p: p[1]["mean_bps"], reverse=True)
            best, worst = pairs[0], pairs[-1]
            lines.append(f"//   Earns most in {best[0]['label']} "
                         f"({best[1]['mean_bps']} bps/bar), bleeds most in "
                         f"{worst[0]['label']} ({worst[1]['mean_bps']} bps/bar)")
    # cost breakeven
    costs = report.get("costs") or {}
    if costs.get("cols"):
        crow = next((x for x in costs["rows"] if x["strategy"] == name), None)
        if crow:
            ok = [c["bps"] for c, cell in zip(costs["cols"], crow["cells"])
                  if cell["total_return_pct"] > 0]
            lines.append("//   Cost tolerance: " +
                         (f"still profitable up to {max(ok):g} bps per side"
                          if ok else "unprofitable even at ZERO cost on this sample"))
    return "\n".join(lines) + "\n"


_ASCII_MAP = {
    "\u2014": "--", "\u2013": "-", "\u2192": "->", "\u00b7": "*", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u2265": ">=", "\u2264": "<=", "\u00d7": "x",
    "\u26a1": "", "\u2705": "", "\u274c": "",
}


def _ascii(text):
    """NinjaScript files are read by an editor with no guaranteed encoding, and a
    stray em-dash there turns into mojibake or a compile error. Fold to ASCII."""
    for k, v in _ASCII_MAP.items():
        text = text.replace(k, v)
    return text.encode("ascii", "replace").decode("ascii")


def _regime_choice(name, report, family):
    """Which way to gate the regime filter — measured, not assumed.

    The textbook says reversion pays in chop and breakouts pay in trends, and the
    regime map usually agrees. When it does not, saying so and shipping the filter
    OFF is more useful than shipping a gate that fights the data.
    """
    fallback = (("<=", "mean reversion is assumed to earn in chop, so entries are "
                       "gated to LOW ADX (textbook assumption -- nothing was "
                       "measured for this file)")
                if family == "reversion" else
                (">=", "trend/momentum rules are assumed to need a trend, so entries "
                       "are gated to HIGH ADX (textbook assumption -- nothing was "
                       "measured for this file)"))
    reg = (report or {}).get("regimes") or {}
    if not reg.get("cols"):
        return fallback[0], True, fallback[1]
    row = next((x for x in reg["rows"] if x["strategy"] == name), None)
    if not row:
        return fallback[0], True, fallback[1]
    agg = {}
    for col, cell in zip(reg["cols"], row["cells"]):
        b = agg.setdefault(col["trend"], [0.0, 0])
        b[0] += cell["mean_bps"] * cell["bars"]
        b[1] += cell["bars"]
    means = {k: (v[0] / v[1] if v[1] else 0.0) for k, v in agg.items()}
    chop, strong = means.get("chop", 0.0), means.get("strong", 0.0)
    expected = "<=" if family == "reversion" else ">="
    measured = "<=" if chop > strong else ">="
    detail = (f"measured {chop:.2f} bps/bar in chop vs {strong:.2f} in strong "
              f"trends on the analyzed sample")
    if measured != expected:
        return expected, False, (
            f"the regime map DISAGREES with the textbook here: {detail}. "
            f"The filter is shipped OFF -- turn it on only if a second date range "
            f"says otherwise")
    if measured == "<=":
        return "<=", True, (f"{detail}, so entries are gated to LOW ADX (chop)")
    return ">=", True, (f"{detail}, so entries are gated to HIGH ADX (trend)")


def _wrap_comment(text, prefix="//   ", width=78):
    out, line = [], prefix
    for word in text.split():
        if len(line) + len(word) + 1 > width and line.strip() != prefix.strip():
            out.append(line)
            line = prefix + word
        else:
            line = (line + " " + word) if line.strip() != prefix.strip() else line + word
    out.append(line)
    return "\n".join(out)


def generate(name, report=None, *, quantity=1, allow_short=True, rules=None):
    """Return (filename, C# source) for one strategy.

    `rules` is a propfirm.PropRules: when given, the generated file ships
    pre-configured for that account (balance, trailing drawdown, trail mode,
    lock level, daily loss limit, contract cap) instead of generic defaults.
    """
    spec = _spec(name)
    uses_regime = spec.get("regime", True)
    regime_cmp, regime_on, regime_why = (
        _regime_choice(name, report, spec["family"]) if uses_regime
        else ("<=", False, ""))
    regime_params = [
        P("UseRegimeFilter", "bool", None, 0, 1,
          "Use ADX regime filter", 80, "2. Regime filter"),
        P("RegimeAdxPeriod", "int", 14, 2, 500, "Regime ADX period", 81, "2. Regime filter"),
        P("RegimeAdxLevel", "double", 25, 1, 100,
          ("Max ADX to enter (chop)" if regime_cmp == "<="
           else "Min ADX to enter (trend)"), 82, "2. Regime filter"),
    ] if uses_regime else []
    params = list(spec["params"]) + COMMON + PROP_PARAMS + regime_params + [
        P("AllowShorts", "bool", allow_short, 0, 1, "Allow shorts", 93, "3. Execution"),
        P("MaxPropContracts", "int", 10, 1, 1000, "Max contracts allowed", 68,
          "5. Prop firm"),
        P("UseTimeFilter", "bool", False, 0, 1, "Restrict trading hours", 84, "4. Session"),
        P("StartHour", "int", 0, 0, 23, "Start hour (chart time)", 85, "4. Session"),
        P("EndHour", "int", 23, 0, 23, "End hour (chart time)", 86, "4. Session"),
    ]
    prop_defaults = {}
    if rules is not None:
        lock = {"start_plus_100": rules.starting_balance + 100,
                "start": rules.starting_balance}.get(rules.trail_lock, 0)
        prop_defaults = {
            "PropStartBalance": rules.starting_balance,
            "TrailingDrawdown": rules.max_drawdown,
            "TrailOnUnrealized": rules.trail_mode == "intraday",
            "LockThresholdAt": lock,
            "DailyLossLimit": rules.daily_loss_limit or 0,
            "MaxPropContracts": rules.max_contracts,
        }
    for p in params:
        if p["name"] == "Quantity":
            p["default"] = quantity
        elif p["name"] == "UseRegimeFilter":
            p["default"] = regime_on
        elif p["name"] in prop_defaults:
            p["default"] = prop_defaults[p["name"]]
    cls = spec["cls"]
    ev = _evidence_block(name, report)
    if rules is not None:
        ev += (f"//\n//  PROP ACCOUNT: {rules.label} — target "
               f"${rules.profit_target:,.0f}, trailing drawdown "
               f"${rules.max_drawdown:,.0f} ({rules.trail_mode}"
               f"{', locks at start' if rules.trail_lock != 'none' else ''})"
               + (f", daily loss limit ${rules.daily_loss_limit:,.0f}"
                  if rules.daily_loss_limit else ", no daily loss limit") + "\n"
               + _wrap_comment(f"Rules snapshot: {rules.as_of}. {rules.notes}") + "\n")
    extra = CONFLUENCE_METHOD if name == "confluence" else ""
    if uses_regime:
        regime_fields = "\t\tprivate ADX regimeAdx;\n"
        regime_init = "\t\t\t\tregimeAdx = ADX(Close, RegimeAdxPeriod);\n"
        regime_gate = (
            "\t\t\t// --- regime gate (see the REGIME FILTER note in the header)\n"
            f"\t\t\tif (UseRegimeFilter && signal != 0 && !(regimeAdx[0] {regime_cmp} "
            "RegimeAdxLevel))\n\t\t\t\tsignal = 0;\n\n")
        regime_header = (f"//  REGIME FILTER  (ADX {regime_cmp} RegimeAdxLevel, "
                         f"default {'ON' if regime_on else 'OFF'})\n"
                         f"{_wrap_comment(regime_why)}\n//\n")
    else:
        regime_fields = regime_init = regime_gate = ""
        regime_header = _wrap_comment(
            "REGIME FILTER: none. This rule gates itself on its own trend filter "
            "(the 200-day line), so bolting an ADX gate on top would be two "
            "regime filters fighting each other.", prefix="//  ") + "\n//\n"
    meta = (report or {}).get("meta") or {}
    src = (f"{meta.get('symbol', 'crypto')} {meta.get('interval', '')} bars".strip()
           if meta else "crypto spot bars")
    sample_note = (f"//   * Measured on {src}, with bar-close fills, no funding,\n")
    props = "\n".join(_prop(p) for p in params)
    defaults = "\n".join(_default_assign(p) for p in params)

    src = f"""{NT_HEADER}
// ---------------------------------------------------------------------------
//  {cls}  —  generated by KRYPT (krypt/ninjascript.py) from `krypt.strats.{name}`
//
//  {spec['desc']}
//
//  WHAT THE ANALYSIS MEASURED
{ev}//
{regime_header}//
//  BEFORE YOU RUN THIS ON MONEY
{sample_note}//     no latency, no partial fills — a different market, feed and broker than the
//     one NinjaTrader is pointed at. Re-run it in Strategy Analyzer on YOUR
//     instrument and YOUR costs; expect the result to be worse.
//   * Hour-of-day findings in the KRYPT report are UTC. The session filter below
//     uses CHART time. Convert before you use it.
//   * Backtest fills here are optimistic on stops: use Strategy Analyzer's
//     tick-by-tick / Order Fill Resolution settings before believing them.
//   * Not financial advice. Trading can lose more than your capital.
// ---------------------------------------------------------------------------

namespace NinjaTrader.NinjaScript.Strategies
{{
	public class {cls} : Strategy
	{{
{spec['fields']}{PROP_FIELDS}{regime_fields}

		protected override void OnStateChange()
		{{
			if (State == State.SetDefaults)
			{{
				Description = @"{spec['desc']}";
				Name = "{cls}";
				Calculate = Calculate.OnBarClose;
				EntriesPerDirection = 1;
				EntryHandling = EntryHandling.AllEntries;
				IsExitOnSessionCloseStrategy = true;
				ExitOnSessionCloseSeconds = 30;
				IsFillLimitOnTouch = false;
				MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
				OrderFillResolution = OrderFillResolution.Standard;
				Slippage = 0;
				StartBehavior = StartBehavior.WaitUntilFlat;
				TimeInForce = TimeInForce.Gtc;
				TraceOrders = false;
				RealtimeErrorHandling = RealtimeErrorHandling.StopCancelClose;
				StopTargetHandling = StopTargetHandling.PerEntryExecution;
				BarsRequiredToTrade = 60;
				IsInstantiatedOnEachOptimizationIteration = true;

{defaults}
			}}
			else if (State == State.Configure)
			{{
			}}
			else if (State == State.DataLoaded)
			{{
{spec['init']}{regime_init}				SetStopLoss(CalculationMode.Percent, StopLossPercent / 100.0);
				SetProfitTarget(CalculationMode.Percent, ProfitTargetPercent / 100.0);
			}}
		}}

		protected override void OnBarUpdate()
		{{
			if (BarsInProgress != 0) return;
			if (CurrentBar < Math.Max(BarsRequiredToTrade, {spec['warmup']})) return;

			// prop-firm rules come FIRST: a breached account cannot trade a signal
			if (propHalted) return;
			if (!PropGuardOk()) return;

			int signal = 0;
{spec['signal']}
{regime_gate}			// --- session gate (CHART time, not UTC)
			if (UseTimeFilter && signal != 0)
			{{
				int h = Time[0].Hour;
				bool inWindow = StartHour <= EndHour
					? (h >= StartHour && h <= EndHour)
					: (h >= StartHour || h <= EndHour);
				if (!inWindow) signal = 0;
			}}

			if (!AllowShorts && signal < 0) signal = 0;
			if (UsePropGuard && Quantity > MaxPropContracts) signal = 0;

			// --- position management: the signal IS the desired position, so a flip
			// closes and reverses on the same bar, exactly like the KRYPT backtest.
			if (signal > 0 && Position.MarketPosition != MarketPosition.Long)
				EnterLong(Quantity, "{cls}Long");
			else if (signal < 0 && Position.MarketPosition != MarketPosition.Short)
				EnterShort(Quantity, "{cls}Short");
			else if (signal == 0 && Position.MarketPosition != MarketPosition.Flat)
			{{
				if (Position.MarketPosition == MarketPosition.Long) ExitLong();
				else ExitShort();
			}}
		}}
{PROP_METHOD}{extra}
		#region Properties
{props}		#endregion
	}}
}}
"""
    return f"{cls}.cs", _ascii(src)


def recommend(report, top=3, min_score=45.0):
    """Which strategies are worth exporting, in the analysis's own order."""
    rows = report["edge"]["rows"]
    # BETA ONLY and the benchmark are deliberately excluded: exporting a rule that
    # loses to buy-and-hold, into a platform where it costs commissions to run, is
    # worse than exporting nothing.
    dead = {"NO EDGE", "BETA ONLY", "BENCHMARK"}
    keep = [r["strategy"] for r in rows
            if r["score"] >= min_score and r["verdict"] not in dead]
    return keep[:top]


README = """# KRYPT -> NinjaTrader 8

Generated by `python3 -m krypt.app ninja`. Each `.cs` file is a NinjaScript
strategy mirroring the KRYPT strategy of the same name, with the measured
evidence for it in the file header.

## Install

1. Copy the `.cs` files into
   `Documents\\NinjaTrader 8\\bin\\Custom\\Strategies\\`
2. In NinjaTrader: **New > NinjaScript Editor**, press **F5** to compile.
3. Chart > **Strategies** tab > pick the strategy > set parameters > Enabled.

## Before you enable one on a live account

* Run it in **Strategy Analyzer** on your instrument, your data, your
  commissions, with Order Fill Resolution set to **High** (or tick-by-tick).
  The KRYPT numbers came from crypto spot bars with bar-close fills — a
  different market with different microstructure.
* Start on **Sim101** (NinjaTrader's simulated account) and leave it there until
  the live sim curve matches the backtest shape, not just its sign.
* The regime filter (ADX) and session filter are ON/OFF switches in the strategy
  dialog. The KRYPT hour-of-day map is **UTC**; NinjaTrader's session filter uses
  **chart time**.
* Position sizing here is a fixed `Quantity`. Size it so that the max drawdown in
  the header — measured, not hoped for — is one you can actually sit through.

**Not financial advice. Trading can lose more than your capital.**
"""


def export(names, report=None, outdir="ninja", *, quantity=1, allow_short=True,
           rules=None):
    """Write strategy files (+ a README) and return the paths written."""
    os.makedirs(outdir, exist_ok=True)
    written = []
    for name in names:
        fname, code = generate(name, report, quantity=quantity,
                               allow_short=allow_short, rules=rules)
        path = os.path.join(outdir, fname)
        with open(path, "w") as f:
            f.write(code)
        written.append(path)
    rpath = os.path.join(outdir, "README.md")
    with open(rpath, "w") as f:
        f.write(README)
    written.append(rpath)
    return written
