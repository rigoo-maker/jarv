"""Live dashboard renderer.

Builds a self-contained HTML page with TradingView lightweight-charts (CDN) for
candlesticks + EMA/Bollinger overlays, the signal-scoring panel, order-book
imbalance, risk/P&L status, and alerts. Auto-refreshes by polling /api/state
(served by server.py) every `refresh_secs`. If opened as a static file it shows
the snapshot baked in at render time.
"""

from __future__ import annotations

import json


def render(state: dict, refresh_secs: float = 5.0) -> str:
    payload = json.dumps(state)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KRYPT Trader</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 :root{{color-scheme:dark}}
 *{{box-sizing:border-box}}
 body{{margin:0;font:14px/1.5 system-ui,sans-serif;background:#0a0e14;color:#e6edf3}}
 header{{display:flex;justify-content:space-between;align-items:center;padding:14px 22px;border-bottom:1px solid #1c2530;background:#0d1320}}
 header h1{{margin:0;font-size:18px;letter-spacing:.5px}}
 .pill{{font-size:11px;padding:3px 10px;border-radius:20px;border:1px solid #30363d}}
 .pill.live{{background:#f8514922;border-color:#f85149;color:#ff7b72}}
 .pill.testnet{{background:#1f6feb22;border-color:#1f6feb;color:#79c0ff}}
 .pill.analyze{{background:#23863622;border-color:#238636;color:#3fb950}}
 .wrap{{display:grid;grid-template-columns:1.6fr 1fr;gap:16px;padding:18px}}
 .card{{background:#0f1620;border:1px solid #1c2530;border-radius:12px;padding:16px}}
 #chart{{height:360px}}
 h2{{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e;margin:0 0 10px}}
 .score{{font-size:40px;font-weight:700;margin:4px 0}}
 .bull{{color:#3fb950}} .bear{{color:#f85149}} .flat{{color:#8b949e}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 td,th{{padding:5px 8px;border-bottom:1px solid #161d27;text-align:left}}
 .sig-pos{{color:#3fb950}} .sig-neg{{color:#f85149}}
 .bar{{height:8px;border-radius:4px;background:#161d27;overflow:hidden;margin-top:6px}}
 .bar>span{{display:block;height:100%}}
 .alert{{background:#f0883e22;border:1px solid #f0883e;color:#f0c674;padding:8px 12px;border-radius:8px;margin:6px 0;font-size:13px}}
 .muted{{color:#8b949e;font-size:12px}}
 .kv{{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #161d27}}
 footer{{padding:14px 22px;color:#8b949e;font-size:12px;border-top:1px solid #1c2530}}
</style></head>
<body>
<header>
  <h1>⚡ KRYPT Trader</h1>
  <div id="badges"></div>
</header>
<div class="wrap">
  <div class="card"><h2 id="sym">—</h2><div id="chart"></div></div>
  <div>
    <div class="card"><h2>Signal score</h2>
      <div class="score" id="score">—</div>
      <div id="label" class="muted"></div>
      <div class="bar"><span id="scorebar"></span></div>
      <table id="components"></table>
    </div>
    <div class="card" style="margin-top:16px"><h2>Order book / risk</h2>
      <div id="book"></div><div id="risk"></div>
    </div>
    <div class="card" style="margin-top:16px"><h2>Alerts</h2><div id="alerts"></div></div>
  </div>
</div>
<footer>Auto-refresh every {refresh_secs}s · advanced TA + scoring + scalper/hedge ·
 <b>not financial advice</b>. Crypto trading can lose all your capital.</footer>
<script>
const REFRESH = {refresh_secs} * 1000;
let chart, candleSeries, emaSeries, initial = {payload};

function fmt(n,d=2){{return n==null?'—':Number(n).toLocaleString(undefined,{{maximumFractionDigits:d}})}}

function setupChart(){{
  chart = LightweightCharts.createChart(document.getElementById('chart'), {{
    layout:{{background:{{color:'transparent'}},textColor:'#8b949e'}},
    grid:{{vertLines:{{color:'#161d27'}},horzLines:{{color:'#161d27'}}}},
    timeScale:{{timeVisible:true}}, height:360,
  }});
  candleSeries = chart.addCandlestickSeries({{upColor:'#3fb950',downColor:'#f85149',
    wickUpColor:'#3fb950',wickDownColor:'#f85149',borderVisible:false}});
  emaSeries = chart.addLineSeries({{color:'#f0c674',lineWidth:2}});
}}

function render(s){{
  if(!s||!s.symbol) return;
  document.getElementById('sym').textContent = s.symbol + '  $' + fmt(s.price);
  // badges
  const mode=s.mode||'analyze', net=s.testnet?'testnet':'mainnet';
  document.getElementById('badges').innerHTML =
    `<span class="pill ${{mode}}">${{mode.toUpperCase()}}</span> `+
    `<span class="pill ${{s.testnet?'testnet':'live'}}">${{net.toUpperCase()}}</span>`;
  // chart
  if(s.candles){{
    candleSeries.setData(s.candles.map(c=>({{time:Math.floor(c.time/1000),open:c.open,high:c.high,low:c.low,close:c.close}})));
    if(s.ema21) emaSeries.setData(s.candles.map((c,i)=>({{time:Math.floor(c.time/1000),value:s.ema21[i]}})).filter(p=>p.value!=null));
  }}
  // score
  const sc=s.scoring||{{}};
  const el=document.getElementById('score');
  el.textContent=(sc.score>0?'+':'')+fmt(sc.score,1);
  el.className='score '+(sc.bias||'flat');
  document.getElementById('label').textContent=sc.label||'';
  const bar=document.getElementById('scorebar');
  bar.style.width=Math.min(100,Math.abs(sc.score||0))+'%';
  bar.style.background=(sc.score||0)>=0?'#3fb950':'#f85149';
  document.getElementById('components').innerHTML=(sc.components||[]).map(c=>
    `<tr><td>${{c.name}}</td><td class="${{c.signal>=0?'sig-pos':'sig-neg'}}">${{c.signal>=0?'+':''}}${{c.signal}}</td><td class="muted">${{c.detail}}</td></tr>`).join('');
  // book + risk
  const b=s.book||{{}};
  document.getElementById('book').innerHTML=
    `<div class="kv"><span>Imbalance</span><b class="${{(b.imbalance||0)>=0?'bull':'bear'}}">${{fmt(b.imbalance,3)}}</b></div>`+
    `<div class="kv"><span>Spread</span><span>${{fmt(b.spread,2)}}</span></div>`+
    `<div class="kv"><span>Best bid / ask</span><span>${{fmt(b.best_bid)}} / ${{fmt(b.best_ask)}}</span></div>`;
  const r=s.risk||{{}};
  document.getElementById('risk').innerHTML=
    `<div class="kv"><span>Realized P&L (today)</span><b class="${{(r.realized_pnl_today||0)>=0?'bull':'bear'}}">$${{fmt(r.realized_pnl_today)}}</b></div>`+
    `<div class="kv"><span>Consec. losses</span><span>${{r.consecutive_losses||0}}</span></div>`+
    `<div class="kv"><span>Halted</span><b class="${{r.halted?'bear':'bull'}}">${{r.halted?('YES — '+r.halt_reason):'no'}}</b></div>`;
  // alerts
  document.getElementById('alerts').innerHTML=(s.alerts&&s.alerts.length)?
    s.alerts.map(a=>`<div class="alert">🔔 ${{a.msg}} (${{fmt(a.value)}})</div>`).join('')
    :'<div class="muted">no active alerts</div>';
}}

setupChart(); render(initial);
async function tick(){{
  try{{const r=await fetch('/api/state');if(r.ok) render(await r.json());}}catch(e){{}}
}}
setInterval(tick, REFRESH);
</script>
</body></html>"""
