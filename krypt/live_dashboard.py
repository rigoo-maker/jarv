"""Standalone LIVE tick dashboard generator.

Renders a self-contained HTML page that connects DIRECTLY from the browser to an
exchange's public WebSocket and streams live Bitcoin tick data — individual
trades, a live-updating candlestick chart, best bid/ask spread, and a trade tape.

No backend, no API key. Works opened as a local file (file://) because Binance
public endpoints send permissive CORS headers and WebSockets aren't CORS-gated.
This sidesteps any server-side network block: the browser does the connecting.

    python3 -m krypt.app live --symbols BTCUSDT        # writes krypt_live.html

Exchanges supported: binance (default), coinbase.
"""

from __future__ import annotations


def render(symbol: str = "BTCUSDT", exchange: str = "binance") -> str:
    sym_lower = symbol.lower()
    return _TEMPLATE.replace("__SYMBOL__", symbol).replace("__SYMLOWER__", sym_lower) \
                    .replace("__EXCHANGE__", exchange)


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KRYPT — Live __SYMBOL__ Ticks</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;font:14px/1.5 system-ui,sans-serif;background:#0a0e14;color:#e6edf3}
 header{display:flex;justify-content:space-between;align-items:center;padding:14px 22px;border-bottom:1px solid #1c2530;background:#0d1320}
 h1{margin:0;font-size:18px;letter-spacing:.5px}
 #status{font-size:12px;padding:3px 10px;border-radius:20px;border:1px solid #30363d}
 .on{background:#23863622;border-color:#238636;color:#3fb950}
 .off{background:#f8514922;border-color:#f85149;color:#ff7b72}
 .wrap{display:grid;grid-template-columns:1.7fr 1fr;gap:16px;padding:18px}
 .card{background:#0f1620;border:1px solid #1c2530;border-radius:12px;padding:16px}
 #chart{height:420px}
 .price{font-size:52px;font-weight:800;letter-spacing:-1px;transition:color .15s}
 .up{color:#3fb950}.down{color:#f85149}
 .row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #161d27}
 .muted{color:#8b949e;font-size:12px}
 h2{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:#8b949e;margin:18px 0 8px}
 #tape{font-family:ui-monospace,monospace;font-size:12px;max-height:240px;overflow:auto}
 #tape div{display:flex;justify-content:space-between;padding:2px 4px;border-bottom:1px solid #11161e}
 .buy{color:#3fb950}.sell{color:#f85149}
 select{background:#0d1320;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 8px}
 footer{padding:14px 22px;color:#8b949e;font-size:12px;border-top:1px solid #1c2530}
</style></head>
<body>
<header>
  <h1>⚡ KRYPT — Live <span id="symlabel">__SYMBOL__</span> Ticks</h1>
  <div>
    <select id="exchange">
      <option value="binance">Binance</option>
      <option value="coinbase">Coinbase</option>
    </select>
    <span id="status" class="off">connecting…</span>
  </div>
</header>
<div class="wrap">
  <div class="card"><div id="chart"></div></div>
  <div>
    <div class="card">
      <div class="muted">last trade</div>
      <div class="price" id="price">—</div>
      <div class="row"><span>best bid</span><b id="bid" class="buy">—</b></div>
      <div class="row"><span>best ask</span><b id="ask" class="sell">—</b></div>
      <div class="row"><span>spread</span><span id="spread">—</span></div>
      <div class="row"><span>24h trades seen</span><span id="count">0</span></div>
      <h2>Trade tape (live)</h2>
      <div id="tape"></div>
    </div>
  </div>
</div>
<footer>Live tick stream straight from the exchange WebSocket in your browser — no
 backend, no API key. Not financial advice.</footer>
<script>
const SYMBOL = "__SYMBOL__";
let chart, candle, ws, lastPrice = null, count = 0, curBar = null;

function fmt(n,d=2){return n==null?'—':Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});}
function setStatus(ok,txt){const s=document.getElementById('status');s.className=ok?'on':'off';s.textContent=txt;}

function setupChart(){
  chart = LightweightCharts.createChart(document.getElementById('chart'), {
    layout:{background:{color:'transparent'},textColor:'#8b949e'},
    grid:{vertLines:{color:'#161d27'},horzLines:{color:'#161d27'}},
    timeScale:{timeVisible:true,secondsVisible:true}, height:420,
  });
  candle = chart.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',
    wickUpColor:'#3fb950',wickDownColor:'#f85149',borderVisible:false});
}

// ---- bootstrap historical candles so the chart isn't empty ----
async function bootstrap(){
  try{
    const r = await fetch(`https://api.binance.com/api/v3/klines?symbol=${SYMBOL}&interval=1m&limit=120`);
    const k = await r.json();
    candle.setData(k.map(c=>({time:Math.floor(c[0]/1000),open:+c[1],high:+c[2],low:+c[3],close:+c[4]})));
  }catch(e){ /* offline / blocked: chart fills from live ticks */ }
}

function showTick(price, qty, isBuy, tms){
  const el=document.getElementById('price');
  el.textContent='$'+fmt(price);
  el.className='price '+(lastPrice!=null && price<lastPrice?'down':'up');
  lastPrice=price;
  count++; document.getElementById('count').textContent=count.toLocaleString();
  // trade tape
  const tape=document.getElementById('tape');
  const d=document.createElement('div');
  d.innerHTML=`<span class="${isBuy?'buy':'sell'}">${isBuy?'▲ BUY ':'▼ SELL'}</span>`+
              `<span>$${fmt(price)}</span><span class="muted">${fmt(qty,4)}</span>`+
              `<span class="muted">${new Date(tms).toLocaleTimeString()}</span>`;
  tape.prepend(d);
  while(tape.childNodes.length>60) tape.removeChild(tape.lastChild);
  // live candle (1m buckets, built from ticks)
  const t=Math.floor(tms/1000/60)*60;
  if(!curBar || curBar.time!==t){
    curBar={time:t,open:price,high:price,low:price,close:price};
  }else{
    curBar.high=Math.max(curBar.high,price);
    curBar.low=Math.min(curBar.low,price);
    curBar.close=price;
  }
  candle.update(curBar);
}

// ---- Binance combined stream: trades + bookTicker ----
function connectBinance(){
  const url=`wss://stream.binance.com:9443/stream?streams=${SYMBOL.toLowerCase()}@trade/${SYMBOL.toLowerCase()}@bookTicker`;
  ws=new WebSocket(url);
  ws.onopen=()=>setStatus(true,'● live · Binance');
  ws.onclose=()=>{setStatus(false,'disconnected — retrying');setTimeout(connectBinance,2000);};
  ws.onerror=()=>setStatus(false,'error');
  ws.onmessage=(m)=>{
    const {stream,data}=JSON.parse(m.data);
    if(stream.endsWith('@trade')){
      // isBuyerMaker true => sell-side aggressor
      showTick(+data.p, +data.q, !data.m, data.T);
    }else if(stream.endsWith('@bookticker')||stream.endsWith('@bookTicker')){
      const bid=+data.b, ask=+data.a;
      document.getElementById('bid').textContent='$'+fmt(bid);
      document.getElementById('ask').textContent='$'+fmt(ask);
      document.getElementById('spread').textContent='$'+fmt(ask-bid)+' ('+fmt((ask-bid)/bid*1e4,1)+' bps)';
    }
  };
}

// ---- Coinbase matches channel ----
function connectCoinbase(){
  const product=SYMBOL.replace('USDT','-USD').replace('USD','-USD').replace('--','-');
  ws=new WebSocket('wss://ws-feed.exchange.coinbase.com');
  ws.onopen=()=>{setStatus(true,'● live · Coinbase');
    ws.send(JSON.stringify({type:'subscribe',product_ids:[product],channels:['matches','ticker']}));};
  ws.onclose=()=>{setStatus(false,'disconnected — retrying');setTimeout(connectCoinbase,2000);};
  ws.onerror=()=>setStatus(false,'error');
  ws.onmessage=(m)=>{
    const d=JSON.parse(m.data);
    if((d.type==='match'||d.type==='last_match')&&d.price){
      showTick(+d.price, +d.size, d.side==='buy', Date.parse(d.time));
    }else if(d.type==='ticker'){
      if(d.best_bid) document.getElementById('bid').textContent='$'+fmt(+d.best_bid);
      if(d.best_ask){document.getElementById('ask').textContent='$'+fmt(+d.best_ask);
        document.getElementById('spread').textContent='$'+fmt(+d.best_ask-+d.best_bid);}
    }
  };
}

function connect(){
  if(ws){try{ws.close();}catch(e){}}
  count=0;curBar=null;lastPrice=null;
  const ex=document.getElementById('exchange').value;
  if(ex==='coinbase') connectCoinbase(); else connectBinance();
}
document.getElementById('exchange').addEventListener('change',connect);

setupChart(); bootstrap(); connect();
</script>
</body></html>"""
