"""Heatmap report renderer — the maps, as one self-contained HTML page.

No CDN, no build step, no dependencies: open `krypt_heatmaps.html` in a browser
(or mail it to yourself) and everything works offline.

Color rules (the part people usually eyeball and get wrong, so it is computed):
  * every matrix here encodes POLARITY (made money / lost money, correlated /
    anticorrelated), so every scale is DIVERGING: two hues, a neutral gray
    midpoint, equal steps per arm.
  * the two arms are generated from the same lightness/chroma steps with only
    the hue rotated, in OKLab — so "-3%" and "+3%" are equally loud, which is
    what makes the map honest to skim.
  * light and dark are separate, deliberate step sets against their own
    surface; the dark map is not an inverted light map.
  * scales are normalized to a robust (90th-percentile) magnitude per matrix,
    so one freak cell cannot flatten the rest of the map into gray. The
    saturation point is printed on every legend.
"""

from __future__ import annotations

import html
import math
import time

# ---------------------------------------------------------------- OKLab utils

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _rgb_to_hex(r, g, b):
    def ch(x):
        return max(0, min(255, int(round(x * 255))))
    return "#%02x%02x%02x" % (ch(r), ch(g), ch(b))


def _srgb_to_lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lin_to_srgb(c):
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def hex_to_oklab(hx):
    r, g, b = (_srgb_to_lin(c) for c in _hex_to_rgb(hx))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (x ** (1 / 3) if x > 0 else -((-x) ** (1 / 3)) for x in (l, m, s))
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def oklab_to_rgb(L, a, b):
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return tuple(_lin_to_srgb(x) for x in (r, g, bb))


def _in_gamut(rgb, eps=0.002):
    return all(-eps <= c <= 1 + eps for c in rgb)


def oklab_to_hex(L, a, b):
    """Convert, pulling chroma in until the color is representable in sRGB."""
    C = math.hypot(a, b)
    h = math.atan2(b, a)
    for _ in range(40):
        rgb = oklab_to_rgb(L, C * math.cos(h), C * math.sin(h))
        if _in_gamut(rgb):
            return _rgb_to_hex(*rgb)
        C *= 0.95
    return _rgb_to_hex(*oklab_to_rgb(L, 0, 0))


def _rotate_hue(hx, target_hue_hex):
    """Same lightness + chroma, different hue — how the second diverging arm is
    built, so both arms are equally loud at equal magnitude."""
    L, a, b = hex_to_oklab(hx)
    C = math.hypot(a, b)
    _, ta, tb = hex_to_oklab(target_hue_hex)
    h = math.atan2(tb, ta)
    return oklab_to_hex(L, C * math.cos(h), C * math.sin(h))


# ------------------------------------------------------------- diverging scale
# Positive arm anchors per mode (neutral -> mid -> extreme), from the reference
# palette's blue ramp. Light: darker = stronger. Dark: brighter = stronger.
_ARMS = {
    "light": ["#f0efec", "#9ec5f4", "#2a78d6", "#104281"],
    "dark":  ["#383835", "#1c5cab", "#3987e5", "#9ec5f4"],
}
_NEG_HUE = {"light": "#d03b3b", "dark": "#e66767"}   # red pole per mode


def _arm_color(t, mode, negative):
    """t in [0,1] -> hex on the requested arm."""
    anchors = _ARMS[mode]
    t = max(0.0, min(1.0, t))
    seg = t * (len(anchors) - 1)
    i = min(int(seg), len(anchors) - 2)
    f = seg - i
    c0, c1 = hex_to_oklab(anchors[i]), hex_to_oklab(anchors[i + 1])
    lab = tuple(c0[k] + (c1[k] - c0[k]) * f for k in range(3))
    hx = oklab_to_hex(*lab)
    return _rotate_hue(hx, _NEG_HUE[mode]) if negative and t > 0.001 else hx


def _rel_lum(hx):
    r, g, b = (_srgb_to_lin(c) for c in _hex_to_rgb(hx))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _ink_for(hx):
    """Cell text is chosen per cell AND per mode, by measured contrast rather
    than a lightness rule of thumb: the diverging arms run dark at high magnitude
    on light surfaces and bright at high magnitude on dark ones, so one fixed ink
    would fail at one end of every scale (and a threshold guess dips right at the
    crossover). Measured worst case across both ramps is 4.45:1, at the one step
    whose lightness sits equidistant from black and white — the ceiling for any
    continuous ramp, not a slip. In-cell numbers are off by default and every
    cell's full stats live in the tooltip, which renders on the surface color."""
    black, white = "#0b0b0b", "#ffffff"
    return white if contrast(hx, white) >= contrast(hx, black) else black


def cell_colors(value, scale):
    """(light hex, dark hex, light ink, dark ink) for a signed value."""
    if value is None or scale <= 0:
        return ("#f0efec", "#383835", "#0b0b0b", "#ffffff")
    t = min(1.0, abs(value) / scale)
    neg = value < 0
    lt = _arm_color(t, "light", neg)
    dk = _arm_color(t, "dark", neg)
    return (lt, dk, _ink_for(lt), _ink_for(dk))


def robust_scale(values, pct=0.90, floor=1e-9):
    """Saturation point = the `pct` quantile of |value| (min 1 cell)."""
    vals = sorted(abs(v) for v in values if v is not None)
    if not vals:
        return floor
    idx = min(len(vals) - 1, int(len(vals) * pct))
    return max(vals[idx], floor)


# ------------------------------------------------------------------ HTML parts

COL_WINDOW = "strategy \\ window"
COL_RSI = "oversold \\ overbought"


def _esc(x):
    return html.escape(str(x), quote=True)


def _fmt(v, digits=2, suffix=""):
    if v is None:
        return "—"
    return f"{v:,.{digits}f}{suffix}"


def _legend(scale, unit, note=""):
    grad_l = ",".join(cell_colors(((i / 20) * 2 - 1) * scale, scale)[0] for i in range(21))
    grad_d = ",".join(cell_colors(((i / 20) * 2 - 1) * scale, scale)[1] for i in range(21))
    return (f'<div class="legend"><span class="lg-lab">−{_fmt(scale)}{unit}</span>'
            f'<span class="ramp" style="--gl:linear-gradient(90deg,{grad_l});'
            f'--gd:linear-gradient(90deg,{grad_d})"></span>'
            f'<span class="lg-lab">0</span>'
            f'<span class="ramp2" style="--gl:linear-gradient(90deg,{grad_l});'
            f'--gd:linear-gradient(90deg,{grad_d})"></span>'
            f'<span class="lg-lab">+{_fmt(scale)}{unit}</span>'
            f'<span class="lg-note">{_esc(note)}</span></div>')


def _heat_table(*, rows, cols, value_of, tip_of, label_of, row_label,
                col_title="", unit="", scale=None, cell_fmt=1, compact=False):
    """Generic heatmap: a real <table> (so it is also the table view), with
    every cell carrying its number in the DOM for screen readers and for the
    'numbers' toggle."""
    vals = [value_of(r, c) for r in rows for c in cols]
    scale = scale or robust_scale(vals)
    head = "".join(f'<th scope="col" title="{_esc(label_of(c))}">{_esc(label_of(c))}</th>'
                   for c in cols)
    body = []
    for r in rows:
        tds = []
        for c in cols:
            v = value_of(r, c)
            lt, dk, il, idk = cell_colors(v, scale)
            tip = tip_of(r, c)
            tds.append(
                f'<td class="cell" style="--cl:{lt};--cd:{dk};--il:{il};--id:{idk}" '
                f'tabindex="0" data-tip="{_esc(tip)}"><span class="v">'
                f'{_fmt(v, cell_fmt)}</span></td>')
        tds = "".join(tds)
        tds_label = _esc(row_label(r))
        body.append(f'<tr><th scope="row">{tds_label}</th>{tds}</tr>')
    cls = "heat compact" if compact else "heat"
    return (f'{_legend(scale, unit)}'
            f'<div class="scroll"><table class="{cls}">'
            f'<thead><tr><th class="corner">{_esc(col_title)}</th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _verdict_badge(v):
    cls = {"ACTIVE EDGE": "good", "WEAK / WATCH": "warning",
           "FADING": "serious", "NO EDGE": "critical",
           "THIN SAMPLE": "muted"}.get(v, "muted")
    icon = {"good": "●", "warning": "▲", "serious": "▼", "critical": "✕",
            "muted": "○"}[cls]
    return f'<span class="badge {cls}"><span class="ic">{icon}</span>{_esc(v)}</span>'


def _edge_section(report):
    edge = report["edge"]
    meta = report["meta"]
    rows = edge["rows"]
    best = rows[0] if rows else None
    hero = ("—" if not best else
            f'{_esc(best["strategy"])} <span class="hero-sub">'
            f'score {best["score"]} · {_esc(best["verdict"])}</span>')
    trs = []
    for r in rows:
        w = max(0, min(100, r["score"]))
        trs.append(
            f'<tr><td class="name">{_esc(r["strategy"])}</td>'
            f'<td class="scorecell"><span class="scorebar"><span style="width:{w}%"></span></span>'
            f'<b>{r["score"]}</b></td>'
            f'<td>{_verdict_badge(r["verdict"])}</td>'
            f'<td class="num">{_fmt(r["recent_sharpe"])}</td>'
            f'<td class="num">{_fmt(r["is_sharpe"])}</td>'
            f'<td class="num">{_fmt(r["oos_sharpe"])}</td>'
            f'<td class="num">{_fmt(r["oos_return_pct"], 2, "%")}</td>'
            f'<td class="num">{_fmt(r["hit_rate"], 2)}</td>'
            f'<td class="num">{_fmt(r["decay"])}</td>'
            f'<td class="num">{_fmt(r["max_dd_pct"], 1, "%")}</td>'
            f'<td class="num">{r["trades"]}</td></tr>')
    return f"""
<section class="card">
  <h2>Which edge is still active</h2>
  <p class="lede">Ranked by a recency-weighted, out-of-sample-checked score — not by
  total return. A strategy that made all its money in week one and nothing since
  ranks below one that is still working today.</p>
  <div class="hero">{hero}</div>
  <div class="scroll"><table class="lb">
    <thead><tr><th>strategy</th><th>edge score</th><th>verdict</th>
      <th class="num">recent Sh</th><th class="num">in-samp Sh</th>
      <th class="num">OOS Sh</th><th class="num">OOS ret</th>
      <th class="num">hit rate</th><th class="num">decay</th>
      <th class="num">max DD</th><th class="num">trades</th></tr></thead>
    <tbody>{''.join(trs)}</tbody></table></div>
  <p class="note"><b>How the score is built.</b> 30% recency-weighted window Sharpe
  (half-life ¼ of the sample) · 25% out-of-sample Sharpe · 20% hit rate across
  windows · 15% improving-vs-decaying · 10% full-sample Sharpe, scaled down when
  there are too few trades to trust, and hard-capped at 45 if the held-out slice
  lost money. Out-of-sample = the last {int(meta['oos_frac']*100)}% of bars
  ({edge['oos_bars']} candles), excluded from nothing else because nothing else
  needs to be honest — this column does.
  All Sharpe figures are annualized and net of {meta['fee_bps']}+{meta['slippage_bps']} bps costs.</p>
</section>"""


# ------------------------------------------------------------------- sections

def _windows_section(rep):
    wm = rep["windows"]
    if not wm["cols"]:
        return ""
    cols = list(range(len(wm["cols"])))
    rows = wm["rows"]

    def lab(c):
        t0 = wm["cols"][c]["t0"]
        return time.strftime("%m-%d %H:%M", time.gmtime(t0))

    def val(r, c):
        return r["cells"][c]["total_return_pct"]

    def tip(r, c):
        cell = r["cells"][c]
        col = wm["cols"][c]
        span = (time.strftime("%Y-%m-%d %H:%M", time.gmtime(col["t0"])) + " → " +
                time.strftime("%m-%d %H:%M", time.gmtime(col["t1"])))
        return (f'{r["strategy"]} · {span}\n'
                f'return {cell["total_return_pct"]}%  ·  Sharpe(ann) {cell["ann_sharpe"]}\n'
                f'trades {cell["trades"]}  ·  win {cell["win_rate_pct"]}%  ·  '
                f'maxDD {cell["max_drawdown_pct"]}%')
    return f"""
<section class="card">
  <h2>Edge over time — is it alive or is it a memory?</h2>
  <p class="lede">Each column is an independent slice of the sample, backtested on
  its own. Read left to right: a live edge stays blue as you move right. Blue on
  the left and red on the right is a <i>decayed</i> edge — the exact thing a
  single whole-sample return number hides.</p>
  {_heat_table(rows=rows, cols=cols, value_of=val, tip_of=tip, label_of=lab,
               row_label=lambda r: r["strategy"], col_title=COL_WINDOW,
               unit="%", cell_fmt=1)}
</section>"""


def _matrix_section(rep, key, title, lede, unit, metric, fmt=2):
    m = rep[key]
    if not m["cols"]:
        return ""
    cols = list(range(len(m["cols"])))

    def val(r, c):
        return r["cells"][c].get(metric)

    def tip(r, c):
        cell = r["cells"][c]
        extra = "\n".join(f"{k}: {v}" for k, v in cell.items())
        return f'{r["strategy"]} · {m["cols"][c]["label"]}\n{extra}'
    return f"""
<section class="card">
  <h2>{_esc(title)}</h2>
  <p class="lede">{lede}</p>
  {_heat_table(rows=m["rows"], cols=cols, value_of=val, tip_of=tip,
               label_of=lambda c: m["cols"][c]["label"],
               row_label=lambda r: r["strategy"],
               col_title="strategy", unit=unit, cell_fmt=fmt)}
</section>"""


def _abbrev(name):
    """Short column code for square matrices — the row header keeps the full name
    and the tooltip repeats both, so the columns can be narrow enough to fit."""
    parts = name.split("_")
    if len(parts) == 1:
        return parts[0][:5]
    return f"{parts[0][:3]}_{parts[1][0]}"


def _corr_section(rep, key, title, lede):
    m = rep[key]
    names = m["names"]
    idx = list(range(len(names)))

    def val(r, c):
        return m["matrix"][r][c]

    def tip(r, c):
        v = m["matrix"][r][c]
        return (f'{names[r]} vs {names[c]}\n'
                f'{"—" if v is None else round(v, 3)}')
    return f"""
<section class="card half">
  <h2>{_esc(title)}</h2>
  <p class="lede">{lede}</p>
  {_heat_table(rows=idx, cols=idx, value_of=val, tip_of=tip,
               label_of=lambda c: _abbrev(names[c]),
               row_label=lambda r: names[r], col_title="", unit="",
               scale=1.0, cell_fmt=2, compact=True)}
</section>"""


def _sweep_section(rep):
    sw = rep["sweep_rsi"]
    vw = rep["sweep_vwap"]
    cols = list(range(len(sw["cols"])))

    def val(r, c):
        return r["cells"][c]["total_return_pct"]

    def tip(r, c):
        cell = r["cells"][c]
        return (f'{r["strategy"]} / overbought {sw["cols"][c]["label"]}\n'
                f'return {cell["total_return_pct"]}%  ·  Sharpe(ann) {cell["ann_sharpe"]}\n'
                f'trades {cell["trades"]}  ·  maxDD {cell["max_drawdown_pct"]}%')
    vcols = list(range(len(vw["cols"])))

    def vval(r, c):
        return r["cells"][c]["total_return_pct"]

    def vtip(r, c):
        cell = r["cells"][c]
        return (f'band {vw["cols"][c]["label"]} from VWAP\n'
                f'return {cell["total_return_pct"]}%  ·  Sharpe(ann) {cell["ann_sharpe"]}\n'
                f'trades {cell["trades"]}')
    return f"""
<section class="card">
  <h2>Mean-reversion robustness — plateau or spike?</h2>
  <p class="lede">The same strategy re-run across its own parameter grid. A real
  edge is a broad <b>plateau</b>: neighbours of the winning cell are also blue, so
  being slightly wrong about the threshold still works. One hot cell in a cold
  field is a curve fit, and it will not survive next month.</p>
  <h3>RSI reversion · oversold × overbought</h3>
  {_heat_table(rows=sw["rows"], cols=cols, value_of=val, tip_of=tip,
               label_of=lambda c: sw["cols"][c]["label"],
               row_label=lambda r: r["strategy"],
               col_title=COL_RSI, unit="%", cell_fmt=1)}
  <h3>VWAP reversion · band width</h3>
  {_heat_table(rows=vw["rows"], cols=vcols, value_of=vval, tip_of=vtip,
               label_of=lambda c: vw["cols"][c]["label"],
               row_label=lambda r: r["strategy"],
               col_title="band", unit="%", cell_fmt=1)}
</section>"""


CSS = """
:root{
 --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
 --line:#e1e0d9; --ring:rgba(11,11,11,.10);
 --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
 --series:#2a78d6;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
 --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
 --line:#2c2c2a; --ring:rgba(255,255,255,.10); --series:#3987e5;
}
 :root:not([data-theme="light"]) .cell{background:var(--cd)}
 :root:not([data-theme="light"]) .ramp,:root:not([data-theme="light"]) .ramp2{
  background-image:var(--gd)}
 :root:not([data-theme="light"]) .cell .v{color:var(--id)}
}
:root[data-theme="dark"]{
 --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
 --line:#2c2c2a; --ring:rgba(255,255,255,.10); --series:#3987e5;
}
:root[data-theme="dark"] .cell{background:var(--cd)}
:root[data-theme="dark"] .ramp,:root[data-theme="dark"] .ramp2{background-image:var(--gd)}
:root[data-theme="dark"] .cell .v{color:var(--id)}
:root[data-theme="light"] .cell{background:var(--cl)}
:root[data-theme="light"] .ramp,:root[data-theme="light"] .ramp2{background-image:var(--gl)}
:root[data-theme="light"] .cell .v{color:var(--il)}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
 font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:26px 24px 10px;max-width:1400px;margin:0 auto}
h1{margin:0 0 4px;font-size:22px;letter-spacing:.2px}
.sub{color:var(--ink2);font-size:13px}
.wrap{max-width:1400px;margin:0 auto;padding:12px 24px 60px;
 display:grid;grid-template-columns:1fr 1fr;gap:18px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;
 padding:18px 18px 20px;grid-column:1/-1;min-width:0}
.card.half{grid-column:span 1}
@media (max-width:980px){.card.half{grid-column:1/-1}.wrap{grid-template-columns:1fr}}
h2{margin:0 0 6px;font-size:15px;letter-spacing:.2px}
h3{margin:18px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;
 color:var(--ink2);font-weight:600}
.lede{margin:0 0 14px;color:var(--ink2);font-size:13px;max-width:88ch}
.note{margin:14px 0 0;color:var(--muted);font-size:12px;max-width:100ch}
.hero{font-size:26px;font-weight:700;margin:6px 0 14px}
.hero-sub{font-size:13px;font-weight:400;color:var(--ink2)}
.scroll{overflow-x:auto;border-radius:8px}
table{border-collapse:separate;border-spacing:2px;font-size:12px}
.heat th{font-weight:500;color:var(--ink2);white-space:nowrap;
 font-variant-numeric:tabular-nums}
.heat thead th{padding:2px 4px;text-align:center;font-size:11px}
.heat tbody th{text-align:right;padding:0 8px 0 2px;position:sticky;left:0;
 background:var(--surface);z-index:1}
.corner{text-align:right!important;color:var(--muted)!important;font-size:10px!important}
.cell{background:var(--cl);width:52px;height:26px;border-radius:4px;text-align:center;
 box-shadow:inset 0 0 0 1px var(--ring);cursor:default;
 font-variant-numeric:tabular-nums}
.compact .cell{width:40px;height:24px}
.compact thead th{font-size:10px}
.cell:focus-visible{outline:2px solid var(--ink);outline-offset:1px}
.cell .v{opacity:0;font-size:10px;color:var(--il)}
body.nums .cell .v{opacity:1}
.legend{display:flex;align-items:center;gap:8px;margin:0 0 10px;font-size:11px;
 color:var(--muted);flex-wrap:wrap}
.ramp,.ramp2{height:10px;width:140px;border-radius:5px;background-image:var(--gl);
 background-size:200% 100%;box-shadow:inset 0 0 0 1px var(--ring)}
.ramp{background-position:0 0}
.ramp2{background-position:100% 0}
.lg-lab{font-variant-numeric:tabular-nums}
.lg-note{color:var(--muted)}
.lb{border-spacing:0;width:100%;font-size:13px}
.lb th,.lb td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;
 white-space:nowrap}
.lb th{color:var(--ink2);font-weight:600;font-size:11px;text-transform:uppercase;
 letter-spacing:.5px}
.lb .num,.lb th.num{text-align:right;font-variant-numeric:tabular-nums}
.lb .name{font-weight:600}
.scorecell{display:flex;align-items:center;gap:8px}
.scorebar{display:inline-block;width:90px;height:8px;border-radius:4px;
 background:var(--line);overflow:hidden}
.scorebar>span{display:block;height:100%;background:var(--series);border-radius:4px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;
 padding:2px 9px;border-radius:20px;box-shadow:inset 0 0 0 1px var(--ring);color:var(--ink)}
.badge .ic{font-size:10px}
.badge.good .ic{color:var(--good)} .badge.warning .ic{color:var(--warning)}
.badge.serious .ic{color:var(--serious)} .badge.critical .ic{color:var(--critical)}
.badge.muted .ic{color:var(--muted)}
.toolbar{max-width:1400px;margin:0 auto;padding:6px 24px 0;display:flex;gap:10px;
 align-items:center;flex-wrap:wrap}
button{font:inherit;font-size:12px;padding:5px 12px;border-radius:8px;
 border:1px solid var(--line);background:var(--surface);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--muted)}
#tip{position:fixed;z-index:99;pointer-events:none;opacity:0;transition:opacity .08s;
 background:var(--surface);color:var(--ink);border:1px solid var(--line);
 border-radius:8px;padding:8px 10px;font-size:12px;white-space:pre-line;
 box-shadow:0 8px 24px rgba(0,0,0,.28);max-width:340px;font-variant-numeric:tabular-nums}
footer{max-width:1400px;margin:0 auto;padding:0 24px 50px;color:var(--muted);font-size:12px}
footer b{color:var(--ink2)}
"""

JS = """
const tip=document.getElementById('tip');
function show(e){const t=e.currentTarget;tip.textContent=t.dataset.tip;tip.style.opacity=1;move(e);}
function move(e){const r=tip.getBoundingClientRect();
 let x=(e.clientX||0)+14,y=(e.clientY||0)+14;
 if(x+r.width>innerWidth-8)x=innerWidth-r.width-8;
 if(y+r.height>innerHeight-8)y=(e.clientY||0)-r.height-12;
 tip.style.left=x+'px';tip.style.top=y+'px';}
function hide(){tip.style.opacity=0;}
for(const c of document.querySelectorAll('.cell')){
 c.addEventListener('mouseenter',show);c.addEventListener('mousemove',move);
 c.addEventListener('mouseleave',hide);
 c.addEventListener('focus',e=>{const r=e.currentTarget.getBoundingClientRect();
  show({currentTarget:e.currentTarget,clientX:r.left+r.width/2,clientY:r.bottom});});
 c.addEventListener('blur',hide);
}
document.getElementById('numbers').onclick=()=>document.body.classList.toggle('nums');
document.getElementById('theme').onclick=()=>{
 const d=document.documentElement;
 const dark=d.getAttribute('data-theme')==='dark'||
  (!d.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme: dark)').matches);
 d.setAttribute('data-theme',dark?'light':'dark');};
"""


def render(report) -> str:
    meta = report["meta"]
    t0 = time.strftime("%Y-%m-%d %H:%M", time.gmtime(meta["t0"]))
    t1 = time.strftime("%Y-%m-%d %H:%M", time.gmtime(meta["t1"]))
    sub = (f'{meta["symbol"]} · {meta["interval"]} · {meta["candles"]:,} candles · '
           f'{t0} → {t1} UTC · costs {meta["fee_bps"]}+{meta["slippage_bps"]} bps '
           f'per side · {"long/short" if meta["allow_short"] else "long only"} · '
           f'{meta["leverage"]:g}× leverage')
    body = "".join([
        _edge_section(report),
        _windows_section(report),
        _matrix_section(report, "regimes",
                        "Where the edge lives — trend strength × volatility",
                        "Mean net return per bar in each market regime. This is the map "
                        "that separates a mean-reversion edge from a trend one: reversion "
                        "should pay in <b>chop</b> and bleed in <b>strong</b> trends, "
                        "breakout the reverse. A strategy that looks equally good "
                        "everywhere is usually just long the drift.",
                        " bps", "mean_bps", fmt=2),
        _matrix_section(report, "costs",
                        "Edge or cost illusion? — return vs trading cost",
                        "The same strategy re-run at rising fee+slippage. Columns run from "
                        "a free market (0 bps, which does not exist) to an expensive one. "
                        "A row that is blue at 0 and red by 10 bps never had an edge — it "
                        "had a fee subsidy. High-frequency strategies die leftmost.",
                        "%", "total_return_pct", fmt=1),
        _matrix_section(report, "hours",
                        "Session map — mean return by hour (UTC)",
                        "Mean net return per bar by hour of day. Useful for a trading-window "
                        "filter, but treat with suspicion: 24 columns on a few weeks of data "
                        "is a lot of chances to find a pattern that is not there.",
                        " bps", "mean_bps", fmt=2),
        _corr_section(report, "correlation", "Correlation of net returns",
                      "Pearson correlation of per-bar returns after costs. Two strategies "
                      "above ~0.8 are one trade wearing two names — running both doubles "
                      "position size and risk without adding edge. Negative pairs are the "
                      "ones worth combining."),
        _corr_section(report, "overlap", "Position overlap",
                      "Cosine similarity of the raw position vectors: +1 = in the same "
                      "trade at the same time, −1 = systematically opposite sides, 0 = "
                      "independent exposure. Correlation says results agree; this says "
                      "the positions themselves do."),
        _sweep_section(report),
    ])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KRYPT edge maps · {_esc(meta["symbol"])}</title>
<style>{CSS}</style></head>
<body>
<header>
  <h1>⚡ KRYPT edge maps</h1>
  <div class="sub">{_esc(sub)}</div>
</header>
<div class="toolbar">
  <button id="numbers" type="button">Show numbers</button>
  <button id="theme" type="button">Toggle light / dark</button>
  <span class="sub">hover or tab to any cell for the full stats</span>
</div>
<div class="wrap">{body}</div>
<div id="tip" role="tooltip"></div>
<footer>
  <p><b>Read this before you trade any of it.</b> Every number here is measured on
  ONE symbol over ONE date range with bar-close fills, no funding, no latency and
  no partial fills. Running ten strategies across dozens of windows, regimes and
  parameter cells means hundreds of comparisons — some cells are blue by luck
  alone. The out-of-sample column and the parameter plateaus exist to fight that,
  and they only reduce the problem.</p>
  <p>The honest workflow: pick a candidate here → re-run these maps on a
  <i>different</i> date range and a <i>different</i> symbol → if the plateau and the
  regime story survive both, paper-trade it → only then, small size.
  <b>Not financial advice. Crypto trading can lose all your capital.</b></p>
</footer>
<script>{JS}</script>
</body></html>"""
