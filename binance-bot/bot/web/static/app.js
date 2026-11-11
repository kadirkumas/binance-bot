/* Qwen Binance Bot - panel arayuzu (bagimliliksiz vanilla JS) */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html !== undefined) n.innerHTML = html; return n; };

let STATE = {}, CONFIG = {}, STRATS = [], INFO = {};
let CHART = { symbol: 'BTCUSDT', interval: '5m', candles: [], ind: {}, trades: [], grid: null };
let EQUITY = [], LOGSEQ = 0, LOGLINES = [], BT = null, BOOTED = false;
let TIMERS = [];

/* ---------------- yardimcilar ---------------- */
const nf = (n, d = 2) => (isFinite(n) ? Number(n).toLocaleString('tr-TR', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—');
const money = (n) => nf(n, 2);
const px = (n) => { n = Number(n); if (!isFinite(n) || n === 0) return '—'; const d = n >= 1000 ? 2 : n >= 10 ? 3 : n >= 1 ? 4 : 6; return nf(n, d); };
const signCls = (n) => (n > 0.0000001 ? 'pos' : n < -0.0000001 ? 'neg' : 'neutral');
const signed = (n, d = 2) => (n > 0 ? '+' : '') + nf(n, d);
const tsStr = (ms) => new Date(ms).toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const dtStr = (ms) => new Date(ms).toLocaleString('tr-TR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
const esc = (s) => String(s === undefined || s === null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(msg, kind = '') {
  const t = el('div', 'toast ' + kind, esc(msg));
  $('#toasts').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateX(20px)'; setTimeout(() => t.remove(), 300); }, 4200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    ...opts,
  });
  if (res.status === 401) { showLogin(); throw new Error('Oturum gerekli'); }
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('json')) return res.text();
  const data = await res.json();
  if (data && data.ok === false) throw new Error(data.error || 'İstek başarısız');
  return data;
}
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });

function modal(title, bodyHtml, actions) {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = bodyHtml;
  const box = $('#modalActions'); box.innerHTML = '';
  actions.forEach(a => {
    const b = el('button', 'btn ' + (a.cls || ''), a.label);
    b.onclick = () => { if (a.fn) a.fn(); if (!a.keep) closeModal(); };
    box.appendChild(b);
  });
  $('#modal').hidden = false;
}
const closeModal = () => { $('#modal').hidden = true; };

function showLogin() { $('#app').hidden = true; $('#login').style.display = 'grid'; TIMERS.forEach(clearInterval); TIMERS = []; BOOTED = false; }
function showApp() { $('#login').style.display = 'none'; $('#app').hidden = false; }

/* ---------------- login ---------------- */
$('#loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('#loginError').textContent = '';
  try {
    const r = await post('/api/login', { password: $('#loginPassword').value });
    $('#loginPassword').value = '';
    showApp();
    await boot();
    if (r.default_password) toast('Varsayılan parolayı Güvenlik sekmesinden değiştirin!', 'warn');
  } catch (err) { $('#loginError').textContent = err.message; }
});

/* ---------------- boot ---------------- */
async function boot() {
  if (BOOTED) return;
  BOOTED = true;
  try {
    [CONFIG, INFO] = await Promise.all([api('/api/config'), api('/api/info')]);
    STRATS = (await api('/api/strategies')).strategies;
  } catch (e) { BOOTED = false; return; }
  buildIntervalSelects();
  buildStrategySelects();
  buildSettingsForms();
  buildBacktestForm();
  renderSymbolCards();
  renderKeys();
  renderStrategyDocs();
  syncChartSelectors();
  await pollState();
  await loadChart();
  await loadEquity();
  await pollLogs();
  TIMERS.push(setInterval(pollState, 2000));
  TIMERS.push(setInterval(pollLogs, 2500));
  TIMERS.push(setInterval(() => { if ($('#tab-dash').classList.contains('active')) loadChart(); }, 15000));
  TIMERS.push(setInterval(loadEquity, 20000));
}

function buildIntervalSelects() {
  const ivs = INFO.intervals || ['1m', '5m', '15m', '1h', '4h', '1d'];
  ['#chartInterval', '#newInterval'].forEach(sel => {
    const s = $(sel); s.innerHTML = ivs.map(i => `<option value="${i}">${i}</option>`).join('');
    s.value = sel === '#chartInterval' ? CHART.interval : '5m';
  });
}
function buildStrategySelects() {
  const opts = STRATS.map(s => `<option value="${s.key}">${esc(s.label)}</option>`).join('');
  $('#newStrategy').innerHTML = opts;
}
function syncChartSelectors() {
  const syms = (CONFIG.symbols || []).map(s => s.symbol);
  const sel = $('#chartSymbol');
  const list = Array.from(new Set([...syms, CHART.symbol]));
  sel.innerHTML = list.map(s => `<option value="${s}">${s}</option>`).join('');
  sel.value = CHART.symbol;
  $('#chartInterval').value = CHART.interval;
}

/* ---------------- state polling ---------------- */
async function pollState() {
  try {
    STATE = await api('/api/state');
    renderTop(); renderKpis(); renderPositions(); renderWatch(); renderBanner();
    $('#manualMode').textContent = `${STATE.mode} / ${STATE.market}`;
    $('#manualHint').textContent = STATE.market === 'futures'
      ? `Futures · ${CONFIG.futures.leverage}x kaldıraç · short açık`
      : 'Spot piyasada short açılamaz.';
    $('#btnShort').disabled = STATE.market !== 'futures';
  } catch (e) { /* login handled */ }
}

function renderTop() {
  const s = STATE;
  const bm = $('#badgeMode'); bm.textContent = s.mode === 'live' ? 'CANLI' : s.mode.toUpperCase();
  bm.className = 'badge ' + s.mode;
  $('#badgeMarket').textContent = s.market === 'futures' ? 'FUTURES' : 'SPOT';
  const st = $('#badgeStatus');
  st.className = 'badge status ' + (s.running ? 'on' : 'off');
  st.querySelector('span').textContent = s.running ? `Çalışıyor · ${Math.floor(s.uptime_s / 60)}dk` : (s.paused_reason ? 'Duraklatıldı' : 'Durdu');
  $('#tmEquity').textContent = money(s.equity) + ' $';
  const p = $('#tmPnl'); p.textContent = signed(s.total_pnl) + ' $'; p.className = signCls(s.total_pnl);
  const d = $('#tmDaily'); const dp = s.daily && s.daily.pnl_pct || 0;
  d.textContent = signed(dp) + '%'; d.className = signCls(dp);
  $('#btnStart').hidden = !!s.running;
  $('#btnStop').hidden = !s.running;
  $('#btnStart').disabled = !!s.kill_switch;
}

function renderBanner() {
  const b = $('#banner');
  const msgs = [];
  if (STATE.kill_switch) msgs.push('🛑 KILL-SWITCH AKTİF — yeni emir gönderilmiyor. Ayarlar sekmesinden kapatın.');
  if (STATE.paused_reason) msgs.push('⏸ ' + STATE.paused_reason);
  if (STATE.broker_warning) msgs.push('⚠ ' + STATE.broker_warning);
  (STATE.warnings || []).forEach(w => msgs.push('⚠ ' + w));
  if (!msgs.length) { b.hidden = true; return; }
  b.hidden = false;
  b.className = 'banner' + (STATE.kill_switch ? ' err' : '');
  b.innerHTML = msgs.map(esc).join('<br>');
}

function renderKpis() {
  const s = STATE, st = s.stats || {};
  const items = [
    ['Özkaynak', money(s.equity) + ' $', `Nakit: ${money(s.cash)} $`, ''],
    ['Toplam PnL', signed(s.total_pnl) + ' $', signed(s.total_pnl_pct) + '% · başlangıç ' + money(s.start_balance), signCls(s.total_pnl)],
    ['Açık PnL', signed(s.unrealized) + ' $', `${(s.positions || []).length} pozisyon`, signCls(s.unrealized)],
    ['Günlük PnL', signed(s.daily && s.daily.pnl || 0) + ' $', `${signed(s.daily && s.daily.pnl_pct || 0)}% · ${s.daily && s.daily.trades || 0} işlem`, signCls(s.daily && s.daily.pnl || 0)],
    ['Kazanma Oranı', '%' + nf(st.win_rate, 1), `${st.wins || 0}K / ${st.losses || 0}Z · PF ${nf(st.profit_factor, 2)}`, ''],
    ['Toplam İşlem', String(st.trades || 0), `Komisyon: ${money(st.fees || 0)} $`, ''],
  ];
  $('#kpis').innerHTML = items.map(([l, v, sub, cls]) =>
    `<div class="kpi"><label>${l}</label><b class="${cls}">${v}</b><span>${esc(sub)}</span></div>`).join('');
}

function renderPositions() {
  const rows = STATE.positions || [];
  $('#posCount').textContent = `${rows.length} / ${CONFIG.risk.max_open_positions} limit`;
  const t = $('#posTable');
  if (!rows.length) { t.innerHTML = '<tbody><tr><td class="empty">Açık pozisyon yok</td></tr></tbody>'; return; }
  t.innerHTML = `<thead><tr><th>Sembol</th><th>Yön</th><th>Miktar</th><th>Giriş</th><th>Fiyat</th>
    <th>PnL</th><th>%</th><th>Stop</th><th>Hedef</th><th></th></tr></thead><tbody>` +
    rows.map(p => {
      const m = (STATE.position_meta || {})[p.symbol] || {};
      return `<tr>
      <td><b>${p.symbol}</b><div class="tiny muted">${p.market}${p.leverage > 1 ? ' · ' + p.leverage + 'x' : ''}</div></td>
      <td><span class="pill ${p.side === 'long' ? 'buy' : 'sell'}">${p.side === 'long' ? 'LONG' : 'SHORT'}</span></td>
      <td>${nf(p.qty, 6)}</td><td>${px(p.entry)}</td><td>${px(p.price)}</td>
      <td class="${signCls(p.unrealized)}">${signed(p.unrealized)}</td>
      <td class="${signCls(p.pnl_pct)}">${signed(p.pnl_pct)}%</td>
      <td class="tiny">${m.stop ? px(m.stop) : '—'}</td><td class="tiny">${m.target ? px(m.target) : '—'}</td>
      <td><button class="btn tiny danger" data-close="${p.symbol}">Kapat</button></td></tr>`;
    }).join('') + '</tbody>';
  t.querySelectorAll('[data-close]').forEach(b => b.onclick = () => closePosition(b.dataset.close));
}

function renderWatch() {
  const rows = STATE.symbols || [];
  const t = $('#watchTable');
  if (!rows.length) { t.innerHTML = '<tbody><tr><td class="empty">Sembol yok — Stratejiler sekmesinden ekleyin</td></tr></tbody>'; return; }
  t.innerHTML = `<thead><tr><th>Sembol</th><th>Fiyat</th><th>24s</th><th>Strateji</th><th>Periyot</th>
    <th>İndikatörler</th><th>Son Sinyal</th><th>Durum</th><th></th></tr></thead><tbody>` +
    rows.map(s => {
      const inds = Object.entries(s.indicators || {}).filter(([k]) => k !== 'atr')
        .map(([k, v]) => `${k.replace('ema_', 'EMA').replace('sma_', 'SMA')}: ${px(v)}`).join(' · ');
      const sig = s.last_signal ? `${s.last_signal.action} · ${tsStr(s.last_signal.ts)}` : '—';
      return `<tr>
      <td><b>${s.symbol}</b> <span class="tiny muted">${s.market}</span></td>
      <td>${px(s.price)}</td>
      <td class="${signCls(s.change_pct)}">${signed(s.change_pct)}%</td>
      <td>${esc(s.strategy_label || s.strategy)}</td><td>${s.interval}</td>
      <td class="tiny muted">${esc(inds) || '—'}</td>
      <td class="tiny">${esc(sig)}</td>
      <td><span class="pill ${s.enabled ? 'on' : 'off'}">${s.enabled ? 'AKTİF' : 'PASİF'}</span></td>
      <td><button class="btn tiny" data-chart="${s.symbol}" data-iv="${s.interval}">Grafik</button></td></tr>`;
    }).join('') + '</tbody>';
  t.querySelectorAll('[data-chart]').forEach(b => b.onclick = () => {
    CHART.symbol = b.dataset.chart; CHART.interval = b.dataset.iv; syncChartSelectors(); loadChart();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
}

/* ---------------- logs ---------------- */
async function pollLogs() {
  try {
    const r = await api('/api/logs?since=' + LOGSEQ);
    if (r.logs && r.logs.length) {
      LOGSEQ = r.logs[r.logs.length - 1].id;
      LOGLINES = LOGLINES.concat(r.logs).slice(-600);
      renderLogs();
    }
  } catch (e) { }
}
function renderLogs() {
  const filter = $('#logLevel').value;
  const box = $('#logBox');
  const rows = LOGLINES.filter(l => !filter || l.level === filter).slice(-300);
  box.innerHTML = rows.map(l =>
    `<div class="logline ${l.level}"><span class="t">${tsStr(l.ts)}</span><span class="l">${l.level.toUpperCase()}</span><span class="m">${esc(l.msg)}</span></div>`).join('');
  if ($('#logAuto').checked) box.scrollTop = box.scrollHeight;
}
$('#logLevel').onchange = renderLogs;

/* ---------------- grafik ---------------- */
async function loadChart() {
  try {
    const r = await api(`/api/candles?symbol=${CHART.symbol}&interval=${CHART.interval}&limit=260&market=${STATE.market || CONFIG.market}`);
    CHART.candles = r.candles || []; CHART.ind = r.indicators || {}; CHART.trades = r.trades || []; CHART.grid = r.grid_levels;
    drawPriceChart();
  } catch (e) { }
}
async function loadEquity() {
  try {
    const r = await api('/api/equity?limit=600');
    EQUITY = r.curve || [];
    drawEquity();
  } catch (e) { }
}

function setupCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = Math.max(1, w * dpr); cv.height = Math.max(1, h * dpr);
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

const LINE_COLORS = ['#f5a623', '#22d3ee', '#7c5cff', '#16c784', '#ff7ab6'];

function drawPriceChart() {
  const cv = $('#priceChart'); if (!cv.clientWidth) return;
  const { ctx, w, h } = setupCanvas(cv);
  const cs = CHART.candles;
  if (!cs.length) { ctx.fillStyle = '#8b98ad'; ctx.font = '13px sans-serif'; ctx.fillText('Veri bekleniyor…', 16, 28); return; }
  const padR = 62, padL = 8, padT = 10, padB = 22;
  const hasRsi = !!(CHART.ind.rsi && CHART.ind.rsi.some(v => v !== null));
  const volH = 34, rsiH = hasRsi ? 56 : 0;
  const plotH = h - padT - padB - volH - rsiH;
  const plotW = w - padL - padR;
  const n = cs.length;
  const cw = plotW / n;
  let lo = Infinity, hi = -Infinity;
  cs.forEach(c => { lo = Math.min(lo, c.l); hi = Math.max(hi, c.h); });
  Object.entries(CHART.ind).forEach(([k, arr]) => {
    if (!/^(ema|sma)/.test(k)) return;
    arr.forEach(v => { if (v !== null && isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
  });
  (CHART.grid || []).forEach(v => { lo = Math.min(lo, v); hi = Math.max(hi, v); });
  const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
  const Y = (p) => padT + plotH - ((p - lo) / (hi - lo)) * plotH;
  const X = (i) => padL + i * cw + cw / 2;

  // izgara + fiyat etiketleri
  ctx.strokeStyle = 'rgba(35,44,61,.75)'; ctx.lineWidth = 1;
  ctx.fillStyle = '#6b7789'; ctx.font = '10px ui-monospace,monospace'; ctx.textAlign = 'left';
  for (let g = 0; g <= 5; g++) {
    const y = padT + (plotH / 5) * g;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
    ctx.fillText(px(hi - ((hi - lo) / 5) * g), padL + plotW + 6, y + 3);
  }
  // zaman etiketleri
  ctx.textAlign = 'center';
  for (let g = 0; g <= 5; g++) {
    const i = Math.min(n - 1, Math.floor((n - 1) * g / 5));
    ctx.fillText(dtStr(cs[i].t), X(i), h - 6);
  }
  // grid seviyeleri
  (CHART.grid || []).forEach(v => {
    ctx.strokeStyle = 'rgba(124,92,255,.35)'; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padL, Y(v)); ctx.lineTo(padL + plotW, Y(v)); ctx.stroke(); ctx.setLineDash([]);
  });
  // mumlar
  cs.forEach((c, i) => {
    const up = c.c >= c.o;
    const col = up ? '#16c784' : '#ea3943';
    const x = X(i);
    ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, Y(c.h)); ctx.lineTo(x, Y(c.l)); ctx.stroke();
    const bw = Math.max(1, cw * 0.62);
    const yo = Y(c.o), yc = Y(c.c);
    ctx.fillRect(x - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)));
  });
  // indikator cizgileri
  let ci = 0; const legend = [];
  Object.entries(CHART.ind).forEach(([k, arr]) => {
    if (!/^(ema|sma)/.test(k) || !arr) return;
    const color = LINE_COLORS[ci++ % LINE_COLORS.length];
    ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.beginPath();
    let started = false;
    arr.forEach((v, i) => {
      if (v === null || !isFinite(v)) return;
      const x = X(i), y = Y(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
    const lastv = arr.filter(v => v !== null).pop();
    legend.push(`<span><i style="background:${color}"></i>${k.replace('ema_', 'EMA ').replace('sma_', 'SMA ')} ${px(lastv)}</span>`);
  });
  // hacim
  const vmax = Math.max(...cs.map(c => c.v)) || 1;
  cs.forEach((c, i) => {
    const bh = (c.v / vmax) * (volH - 6);
    ctx.fillStyle = c.c >= c.o ? 'rgba(22,199,132,.35)' : 'rgba(234,57,67,.35)';
    ctx.fillRect(X(i) - Math.max(1, cw * 0.62) / 2, padT + plotH + volH - bh, Math.max(1, cw * 0.62), bh);
  });
  // RSI paneli
  if (hasRsi) {
    const top = padT + plotH + volH + 8, hh = rsiH - 12;
    ctx.strokeStyle = 'rgba(35,44,61,.9)';
    [30, 50, 70].forEach(lv => {
      const y = top + hh - (lv / 100) * hh;
      ctx.setLineDash(lv === 50 ? [2, 4] : [4, 4]);
      ctx.strokeStyle = lv === 50 ? 'rgba(107,119,137,.4)' : 'rgba(124,92,255,.35)';
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = '#6b7789'; ctx.textAlign = 'left'; ctx.fillText(String(lv), padL + plotW + 6, y + 3);
    });
    ctx.strokeStyle = '#22d3ee'; ctx.lineWidth = 1.3; ctx.beginPath();
    let st = false;
    CHART.ind.rsi.forEach((v, i) => {
      if (v === null) return;
      const x = X(i), y = top + hh - (v / 100) * hh;
      if (!st) { ctx.moveTo(x, y); st = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
    const rv = CHART.ind.rsi.filter(v => v !== null).pop();
    legend.push(`<span><i style="background:#22d3ee"></i>RSI ${nf(rv, 1)}</span>`);
  }
  // islem isaretleri
  if ($('#chartMarkers').checked) {
    const t0 = cs[0].t, t1 = cs[n - 1].t;
    CHART.trades.forEach(tr => {
      if (tr.ts < t0 || tr.ts > t1 + 3600000) return;
      let idx = Math.round((tr.ts - t0) / Math.max(1, (t1 - t0)) * (n - 1));
      idx = Math.max(0, Math.min(n - 1, idx));
      const buy = tr.side === 'buy';
      const x = X(idx), y = Y(tr.price);
      ctx.fillStyle = buy ? '#16c784' : '#ea3943';
      ctx.beginPath();
      if (buy) { ctx.moveTo(x, y + 12); ctx.lineTo(x - 5, y + 21); ctx.lineTo(x + 5, y + 21); }
      else { ctx.moveTo(x, y - 12); ctx.lineTo(x - 5, y - 21); ctx.lineTo(x + 5, y - 21); }
      ctx.closePath(); ctx.fill();
    });
    legend.push('<span>▲ alım · ▼ satım</span>');
  }
  const last = cs[n - 1];
  legend.unshift(`<span><b>${CHART.symbol} ${CHART.interval}</b> · son ${px(last.c)}</span>`);
  $('#chartLegend').innerHTML = legend.join('');
}

function drawEquity() {
  const cv = $('#equityChart'); if (!cv.clientWidth) return;
  const { ctx, w, h } = setupCanvas(cv);
  const c = EQUITY;
  $('#equityInfo').textContent = c.length ? `${c.length} nokta` : 'kayıt yok';
  if (c.length < 2) {
    ctx.fillStyle = '#8b98ad'; ctx.font = '12px sans-serif';
    ctx.fillText('Bot çalıştıkça özkaynak eğrisi burada birikir.', 14, 26); return;
  }
  const padL = 8, padR = 60, padT = 10, padB = 18;
  const vals = c.map(p => p.equity);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  const X = (i) => padL + (i / (c.length - 1)) * (w - padL - padR);
  const Y = (v) => padT + (h - padT - padB) * (1 - (v - lo) / (hi - lo));
  ctx.strokeStyle = 'rgba(35,44,61,.7)'; ctx.fillStyle = '#6b7789'; ctx.font = '10px ui-monospace,monospace';
  for (let g = 0; g <= 3; g++) {
    const y = padT + ((h - padT - padB) / 3) * g;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillText(money(hi - ((hi - lo) / 3) * g), w - padR + 5, y + 3);
  }
  const up = vals[vals.length - 1] >= vals[0];
  const col = up ? '#16c784' : '#ea3943';
  const grad = ctx.createLinearGradient(0, padT, 0, h - padB);
  grad.addColorStop(0, up ? 'rgba(22,199,132,.35)' : 'rgba(234,57,67,.35)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(X(0), Y(vals[0]));
  vals.forEach((v, i) => ctx.lineTo(X(i), Y(v)));
  ctx.lineTo(X(vals.length - 1), h - padB); ctx.lineTo(X(0), h - padB); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath(); ctx.moveTo(X(0), Y(vals[0]));
  vals.forEach((v, i) => ctx.lineTo(X(i), Y(v)));
  ctx.strokeStyle = col; ctx.lineWidth = 1.8; ctx.stroke();
}

/* ---------------- kontroller ---------------- */
$('#btnStart').onclick = async () => {
  try { const r = await post('/api/engine/start'); toast(r.msg || 'Başlatıldı', r.ok ? 'ok' : 'err'); pollState(); }
  catch (e) { toast(e.message, 'err'); }
};
$('#btnStop').onclick = async () => {
  try { const r = await post('/api/engine/stop'); toast(r.msg || 'Durduruldu', 'warn'); pollState(); }
  catch (e) { toast(e.message, 'err'); }
};
$('#btnKill').onclick = () => modal('🛑 ACİL DURDURMA',
  '<p>Tüm açık pozisyonlar <b>piyasa emriyle kapatılacak</b>, bot durdurulacak ve kill-switch kilitlenecek.</p><p>Bu işlem geri alınamaz.</p>',
  [{ label: 'Vazgeç', cls: 'ghost' }, {
    label: 'EVET, HEPSİNİ KAPAT', cls: 'danger', fn: async () => {
      try { const r = await post('/api/engine/kill', { flatten: true }); toast(`${r.closed} pozisyon kapatıldı`, 'warn'); pollState(); }
      catch (e) { toast(e.message, 'err'); }
    }
  }]);
$('#btnLogout').onclick = async () => { try { await post('/api/logout'); } catch (e) { } showLogin(); };
$('#btnRefreshChart').onclick = loadChart;
$('#chartSymbol').onchange = (e) => { CHART.symbol = e.target.value; loadChart(); };
$('#chartInterval').onchange = (e) => { CHART.interval = e.target.value; loadChart(); };
$('#chartMarkers').onchange = drawPriceChart;
window.addEventListener('resize', () => { drawPriceChart(); drawEquity(); if (BT) drawBtChart(); });

async function manual(side) {
  const symbol = $('#mSymbol').value.trim().toUpperCase();
  const usdt = Number($('#mUsdt').value);
  if (!symbol || !(usdt > 0)) return toast('Sembol ve tutar girin', 'err');
  try {
    const r = await post('/api/order', { symbol, side, usdt });
    toast(`${symbol} ${r.fill.side.toUpperCase()} ${nf(r.fill.qty, 6)} @ ${px(r.fill.price)}`, 'ok');
    pollState(); loadChart();
  } catch (e) { toast(e.message, 'err'); }
}
$('#btnBuy').onclick = () => manual('buy');
$('#btnShort').onclick = () => manual('short');
$('#btnSell').onclick = () => manual('sell');
$('#btnFlatten').onclick = () => modal('Tüm pozisyonları kapat',
  '<p>Açık tüm pozisyonlar piyasa emriyle kapatılacak. Bot çalışmaya devam eder.</p>',
  [{ label: 'Vazgeç', cls: 'ghost' }, {
    label: 'Kapat', cls: 'warn', fn: async () => {
      try { const r = await post('/api/engine/flatten', {}); toast(`${r.closed} pozisyon kapatıldı`, 'ok'); pollState(); }
      catch (e) { toast(e.message, 'err'); }
    }
  }]);

async function closePosition(symbol) {
  try { const r = await post('/api/close', { symbol }); toast(`${symbol} kapatıldı · PnL ${signed(r.fill.realized_pnl)} $`, 'ok'); pollState(); loadChart(); }
  catch (e) { toast(e.message, 'err'); }
}

/* ---------------- sekmeler ---------------- */
$$('.tab-btn').forEach(b => b.onclick = () => {
  $$('.tab-btn').forEach(x => x.classList.remove('active'));
  $$('.tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  $('#tab-' + b.dataset.tab).classList.add('active');
  if (b.dataset.tab === 'trades') loadTrades();
  if (b.dataset.tab === 'backtest') loadBtHistory();
  if (b.dataset.tab === 'dash') { drawPriceChart(); drawEquity(); }
  if (b.dataset.tab === 'settings') buildSettingsForms();
  if (b.dataset.tab === 'security') renderKeys();
});

/* ---------------- strateji kartlari ---------------- */
function paramInput(schema, value, idPrefix) {
  const v = value === undefined ? schema.default : value;
  const id = `${idPrefix}_${schema.name}`;
  let input;
  if (schema.type === 'bool') {
    input = `<label class="chk"><input type="checkbox" id="${id}" ${v ? 'checked' : ''}> ${esc(schema.label)}</label>`;
    return `<div class="pfield" data-type="bool">${input}${schema.help ? `<div class="field-help">${esc(schema.help)}</div>` : ''}</div>`;
  }
  if (schema.type === 'select') {
    input = `<select id="${id}">${(schema.options || []).map(o => `<option value="${o}" ${o === v ? 'selected' : ''}>${o}</option>`).join('')}</select>`;
  } else {
    input = `<input type="number" id="${id}" value="${v}" ${schema.min !== null ? `min="${schema.min}"` : ''} ${schema.max !== null ? `max="${schema.max}"` : ''} step="${schema.step || (schema.type === 'int' ? 1 : 0.1)}">`;
  }
  return `<div class="pfield" data-type="${schema.type}"><label>${esc(schema.label)}</label>${input}${schema.help ? `<div class="field-help">${esc(schema.help)}</div>` : ''}</div>`;
}

function renderSymbolCards() {
  const box = $('#symbolCards');
  const ivs = INFO.intervals || ['1m', '5m', '15m', '1h', '4h', '1d'];
  box.innerHTML = '';
  (CONFIG.symbols || []).forEach((s, idx) => {
    const strat = STRATS.find(x => x.key === s.strategy) || STRATS[0];
    const card = el('div', 'sym-card');
    card.innerHTML = `
      <div class="sym-head">
        <b>${esc(s.symbol)}</b>
        <span class="pill ${s.enabled ? 'on' : 'off'}">${s.enabled ? 'AKTİF' : 'PASİF'}</span>
        <span class="grow"></span>
        <label class="chk"><input type="checkbox" id="sym_en_${idx}" ${s.enabled ? 'checked' : ''}> aktif</label>
        <select id="sym_market_${idx}">
          <option value="spot" ${s.market === 'spot' ? 'selected' : ''}>Spot</option>
          <option value="futures" ${s.market === 'futures' ? 'selected' : ''}>Futures</option>
        </select>
        <select id="sym_iv_${idx}">${ivs.map(i => `<option value="${i}" ${i === s.interval ? 'selected' : ''}>${i}</option>`).join('')}</select>
        <select id="sym_strat_${idx}">${STRATS.map(x => `<option value="${x.key}" ${x.key === s.strategy ? 'selected' : ''}>${esc(x.label)}</option>`).join('')}</select>
        <button class="btn tiny danger" id="sym_del_${idx}">Sil</button>
      </div>
      <div class="tiny muted">${esc(strat.description)}</div>
      <div class="params" id="sym_params_${idx}"></div>`;
    box.appendChild(card);
    const drawParams = () => {
      const key = $(`#sym_strat_${idx}`).value;
      const sc = STRATS.find(x => x.key === key);
      const cur = key === s.strategy ? (s.params || {}) : {};
      $(`#sym_params_${idx}`).innerHTML = sc.schema.map(p => paramInput(p, cur[p.name], `sp_${idx}`)).join('');
    };
    drawParams();
    $(`#sym_strat_${idx}`).onchange = drawParams;
    $(`#sym_del_${idx}`).onclick = () => { CONFIG.symbols.splice(idx, 1); renderSymbolCards(); toast('Kaydetmeyi unutmayın', 'warn'); };
  });
  if (!(CONFIG.symbols || []).length) box.innerHTML = '<div class="empty">Henüz sembol yok. Sağdaki panelden ekleyin.</div>';
}

function collectSymbols() {
  return (CONFIG.symbols || []).map((s, idx) => {
    const key = $(`#sym_strat_${idx}`).value;
    const sc = STRATS.find(x => x.key === key);
    const params = {};
    sc.schema.forEach(p => {
      const node = $(`#sp_${idx}_${p.name}`);
      if (!node) return;
      params[p.name] = p.type === 'bool' ? node.checked : (p.type === 'select' ? node.value : Number(node.value));
    });
    return {
      symbol: s.symbol, enabled: $(`#sym_en_${idx}`).checked,
      market: $(`#sym_market_${idx}`).value, interval: $(`#sym_iv_${idx}`).value,
      strategy: key, params,
    };
  });
}

$('#btnSaveSymbols').onclick = async () => {
  try {
    const r = await post('/api/config', { symbols: collectSymbols() });
    CONFIG = r.config; renderSymbolCards(); syncChartSelectors();
    toast('Sembol ve strateji ayarları kaydedildi', 'ok'); pollState();
  } catch (e) { toast(e.message, 'err'); }
};

$('#btnSymSearch').onclick = async () => {
  try {
    const r = await api('/api/symbols?q=' + encodeURIComponent($('#symSearch').value));
    $('#symSearchResults').innerHTML = (r.symbols || []).map(s => `<option value="${s.symbol}">${s.symbol}</option>`).join('')
      || '<option value="">sonuç yok</option>';
  } catch (e) { toast(e.message, 'err'); }
};
$('#btnAddSymbol').onclick = () => {
  const sym = $('#symSearchResults').value || $('#symSearch').value.trim().toUpperCase();
  if (!sym) return toast('Bir sembol seçin', 'err');
  if ((CONFIG.symbols || []).some(s => s.symbol === sym)) return toast('Bu sembol zaten listede', 'warn');
  CONFIG.symbols = CONFIG.symbols || [];
  CONFIG.symbols.push({ symbol: sym, enabled: true, market: $('#newMarket').value, interval: $('#newInterval').value, strategy: $('#newStrategy').value, params: {} });
  renderSymbolCards(); syncChartSelectors();
  toast(sym + ' eklendi — kaydetmeyi unutmayın', 'ok');
};

function renderStrategyDocs() {
  $('#strategyDocs').innerHTML = STRATS.map(s =>
    `<div class="doc"><b>${esc(s.label)}</b><p>${esc(s.description)}</p></div>`).join('');
}

/* ---------------- ayarlar ---------------- */
const F = {
  mode: [
    { g: 'mode', n: 'mode', l: 'Çalışma modu', t: 'select', opts: [['paper', 'Paper (simülasyon)'], ['testnet', 'Binance Testnet'], ['live', 'CANLI (gerçek para)']] },
    { g: 'mode', n: 'market', l: 'Piyasa', t: 'select', opts: [['spot', 'Spot'], ['futures', 'USDT-M Futures']] },
    { g: 'mode', n: 'kill_switch', l: 'Kill-switch (yeni emirleri kilitle)', t: 'bool' },
  ],
  engine: [
    { g: 'engine', n: 'poll_seconds', l: 'Döngü aralığı (sn)', t: 'int', min: 2, max: 300 },
    { g: 'engine', n: 'candle_limit', l: 'Mum geçmişi (adet)', t: 'int', min: 60, max: 1000 },
    { g: 'engine', n: 'auto_start', l: 'Sunucu açılınca botu başlat', t: 'bool' },
  ],
  risk: [
    { g: 'risk', n: 'sizing', l: 'Pozisyon büyüklüğü', t: 'select', opts: [['fixed', 'Sabit USDT'], ['percent', 'Özkaynak yüzdesi']] },
    { g: 'risk', n: 'order_usdt', l: 'Emir başına USDT', t: 'float', step: 10 },
    { g: 'risk', n: 'max_position_pct', l: 'Özkaynak % (percent modu)', t: 'float', step: 1 },
    { g: 'risk', n: 'max_order_usdt', l: 'Sert üst limit (USDT)', t: 'float', step: 50 },
    { g: 'risk', n: 'min_notional_usdt', l: 'Min. emir (USDT)', t: 'float', step: 1 },
    { g: 'risk', n: 'stop_loss_pct', l: 'Stop-loss %', t: 'float', step: 0.1 },
    { g: 'risk', n: 'take_profit_pct', l: 'Take-profit %', t: 'float', step: 0.1 },
    { g: 'risk', n: 'trailing_pct', l: 'Trailing stop % (0=kapalı)', t: 'float', step: 0.1 },
    { g: 'risk', n: 'use_atr_stops', l: 'ATR bazlı stop', t: 'bool' },
    { g: 'risk', n: 'atr_period', l: 'ATR periyodu', t: 'int' },
    { g: 'risk', n: 'atr_mult', l: 'ATR çarpanı', t: 'float', step: 0.1 },
    { g: 'risk', n: 'max_open_positions', l: 'Maks. açık pozisyon', t: 'int' },
    { g: 'risk', n: 'max_daily_loss_pct', l: 'Günlük zarar limiti % (0=kapalı)', t: 'float', step: 0.5 },
    { g: 'risk', n: 'max_trades_per_day', l: 'Günlük maks. işlem', t: 'int' },
    { g: 'risk', n: 'cooldown_seconds', l: 'İşlem arası bekleme (sn)', t: 'int' },
    { g: 'risk', n: 'close_on_stop', l: 'Limit aşımında pozisyonları kapat', t: 'bool' },
  ],
  paper: [
    { g: 'paper', n: 'start_balance', l: 'Paper başlangıç bakiyesi (USDT)', t: 'float', step: 100 },
    { g: 'paper', n: 'fee_pct', l: 'Spot komisyon %', t: 'float', step: 0.01 },
    { g: 'paper', n: 'slippage_pct', l: 'Kayma (slippage) %', t: 'float', step: 0.01 },
  ],
  futures: [
    { g: 'futures', n: 'leverage', l: 'Kaldıraç (x)', t: 'int', min: 1, max: 50 },
    { g: 'futures', n: 'margin_mode', l: 'Marjin modu', t: 'select', opts: [['ISOLATED', 'Isolated'], ['CROSSED', 'Cross']] },
    { g: 'futures', n: 'fee_pct', l: 'Futures komisyon %', t: 'float', step: 0.01 },
    { g: 'futures', n: 'allow_short', l: 'Short işlemlere izin ver', t: 'bool' },
  ],
  notify: [
    { g: 'notify', n: 'telegram_enabled', l: 'Telegram bildirimleri', t: 'bool' },
    { g: 'notify', n: 'telegram_token', l: 'Bot token', t: 'text' },
    { g: 'notify', n: 'telegram_chat_id', l: 'Chat ID', t: 'text' },
    { g: 'notify', n: 'webhook_url', l: 'Webhook URL (opsiyonel)', t: 'text' },
    { g: 'notify', n: 'on_trade', l: 'İşlem bildirimleri', t: 'bool' },
    { g: 'notify', n: 'on_error', l: 'Hata bildirimleri', t: 'bool' },
    { g: 'notify', n: 'on_kill', l: 'Kill/limit bildirimleri', t: 'bool' },
  ],
};

function fieldHtml(f) {
  const id = `f_${f.g}_${f.n}`;
  const val = (CONFIG[f.g] || {})[f.n];
  if (f.t === 'bool') return `<div class="inline" style="margin-top:10px"><input type="checkbox" id="${id}" ${val ? 'checked' : ''}><label for="${id}" style="margin:0">${esc(f.l)}</label></div>`;
  if (f.t === 'select') return `<div><label>${esc(f.l)}</label><select id="${id}">${f.opts.map(([v, t]) => `<option value="${v}" ${v === val ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></div>`;
  if (f.t === 'text') return `<div><label>${esc(f.l)}</label><input type="text" id="${id}" value="${esc(val === '***' ? '' : val)}" placeholder="${val === '***' ? '(kayıtlı — değiştirmek için yazın)' : ''}"></div>`;
  return `<div><label>${esc(f.l)}</label><input type="number" id="${id}" value="${val}" step="${f.step || 1}" ${f.min !== undefined ? `min="${f.min}"` : ''} ${f.max !== undefined ? `max="${f.max}"` : ''}></div>`;
}

function buildSettingsForms() {
  const map = { modeForm: 'mode', engineForm: 'engine', riskForm: 'risk', paperForm: 'paper', futuresForm: 'futures', notifyForm: 'notify' };
  Object.entries(map).forEach(([box, group]) => {
    const fields = F[group];
    const wrap = (group === 'risk' || group === 'notify');
    $('#' + box).innerHTML = (wrap ? '<div class="grid2">' : '') + fields.map(fieldHtml).join('') + (wrap ? '</div>' : '');
  });
  $('#cfgWarnings').innerHTML = (STATE.warnings || []).map(w => `<div>⚠ ${esc(w)}</div>`).join('') || '<div style="background:#12283d;border-color:#1d4a72;color:#7cc4ff">✓ Belirgin bir yapılandırma uyarısı yok</div>';
}

function collectSettings() {
  const out = {};
  Object.values(F).flat().forEach(f => {
    const node = $(`#f_${f.g}_${f.n}`);
    if (!node) return;
    out[f.g] = out[f.g] || {};
    let v;
    if (f.t === 'bool') v = node.checked;
    else if (f.t === 'select' || f.t === 'text') v = node.value;
    else v = Number(node.value);
    if (f.t === 'text' && f.n === 'telegram_token' && v === '') return;
    out[f.g][v === undefined ? f.n : f.n] = v;
  });
  const patch = { ...out.mode, engine: out.engine, risk: out.risk, paper: out.paper, futures: out.futures, notify: out.notify };
  return patch;
}

$('#btnSaveSettings').onclick = async () => {
  const patch = collectSettings();
  const goingLive = patch.mode === 'live' && CONFIG.mode !== 'live';
  const save = async (extra) => {
    try {
      const r = await post('/api/config', { ...patch, ...extra });
      CONFIG = r.config; buildSettingsForms();
      $('#settingsSaved').textContent = 'Kaydedildi · ' + tsStr(Date.now());
      toast('Ayarlar kaydedildi', 'ok'); pollState();
    } catch (e) { toast(e.message, 'err'); }
  };
  if (patch.mode !== 'live') patch.live_confirmed = false;
  if (goingLive) {
    modal('⚠️ CANLI MOD ONAYI',
      `<p>Gerçek paranızla işlem yapılacak. Devam etmek için:</p>
       <p>1) Güvenlik sekmesinde <b>canlı API anahtarlarını</b> tanımlayın.<br>
       2) Emir üst limitini ve stop-loss'u kontrol edin.<br>
       3) Aşağıya <b>CANLI</b> yazın.</p>
       <input id="liveConfirmInput" placeholder="CANLI">`,
      [{ label: 'Vazgeç', cls: 'ghost' }, {
        label: 'Onayla ve kaydet', cls: 'danger', keep: true, fn: async () => {
          if ($('#liveConfirmInput').value.trim().toUpperCase() !== 'CANLI') return toast('Onay metni hatalı', 'err');
          closeModal(); await save({ live_confirmed: true });
        }
      }]);
    return;
  }
  await save({});
};

$('#btnResetPaper').onclick = () => modal('Paper hesabı sıfırla',
  `<p>Sanal bakiye <b>${money(Number($('#f_paper_start_balance').value))} USDT</b> olarak sıfırlanacak; pozisyonlar, strateji durumları ve özkaynak geçmişi silinecek.</p>`,
  [{ label: 'Vazgeç', cls: 'ghost' }, {
    label: 'Sıfırla', cls: 'warn', fn: async () => {
      try {
        await post('/api/engine/reset_paper', { balance: Number($('#f_paper_start_balance').value) });
        toast('Paper hesap sıfırlandı', 'ok'); pollState(); loadEquity();
      } catch (e) { toast(e.message, 'err'); }
    }
  }]);

$('#btnTestNotify').onclick = async () => {
  try { await post('/api/notify/test', {}); toast('Test bildirimi gönderildi', 'ok'); }
  catch (e) { toast(e.message, 'err'); }
};

/* ---------------- islemler ---------------- */
async function loadTrades() {
  try {
    const mode = $('#tradesMode').value;
    const r = await api('/api/trades?limit=400' + (mode ? '&mode=' + mode : ''));
    const rows = r.trades || [];
    const closed = rows.filter(t => t.action.startsWith('close'));
    const wins = closed.filter(t => t.realized_pnl > 0);
    const realized = closed.reduce((a, t) => a + t.realized_pnl, 0);
    const fees = rows.reduce((a, t) => a + t.fee, 0);
    $('#tradeKpis').innerHTML = [
      ['İşlem', rows.length, ''], ['Kapanan', closed.length, ''],
      ['Kazanma', '%' + nf(closed.length ? wins.length / closed.length * 100 : 0, 1), ''],
      ['Net PnL', signed(realized) + ' $', signCls(realized)],
      ['Komisyon', money(fees) + ' $', ''],
    ].map(([l, v, c]) => `<div class="kpi"><label>${l}</label><b class="${c}">${v}</b></div>`).join('');
    const t = $('#tradesTable');
    if (!rows.length) { t.innerHTML = '<tbody><tr><td class="empty">Henüz işlem yok</td></tr></tbody>'; return; }
    t.innerHTML = `<thead><tr><th>Zaman</th><th>Mod</th><th>Sembol</th><th>Yön</th><th>Miktar</th>
      <th>Fiyat</th><th>Tutar</th><th>Komisyon</th><th>PnL</th><th>Strateji</th><th>Neden</th></tr></thead><tbody>` +
      rows.map(x => `<tr>
        <td>${dtStr(x.ts)}</td><td class="tiny">${x.mode}/${x.market}</td>
        <td><b>${x.symbol}</b></td>
        <td><span class="pill ${x.side === 'buy' ? 'buy' : 'sell'}">${x.side === 'buy' ? 'AL' : 'SAT'}</span></td>
        <td>${nf(x.qty, 6)}</td><td>${px(x.price)}</td><td>${money(x.notional)}</td>
        <td class="tiny muted">${nf(x.fee, 4)}</td>
        <td class="${signCls(x.realized_pnl)}">${x.action.startsWith('close') ? signed(x.realized_pnl) : '—'}</td>
        <td class="tiny">${esc(x.strategy)}</td><td class="wrap tiny">${esc(x.reason)}</td></tr>`).join('') + '</tbody>';
  } catch (e) { }
}
$('#btnRefreshTrades').onclick = loadTrades;
$('#tradesMode').onchange = loadTrades;

/* ---------------- backtest ---------------- */
function buildBacktestForm() {
  const ivs = INFO.intervals || ['5m', '15m', '1h', '4h', '1d'];
  $('#btForm').innerHTML = `
    <label>Sembol</label><input id="bt_symbol" value="BTCUSDT">
    <div class="grid2">
      <div><label>Piyasa</label><select id="bt_market"><option value="spot">Spot</option><option value="futures">Futures</option></select></div>
      <div><label>Periyot</label><select id="bt_interval">${ivs.map(i => `<option value="${i}" ${i === '1h' ? 'selected' : ''}>${i}</option>`).join('')}</select></div>
    </div>
    <div class="grid2">
      <div><label>Gün sayısı</label><input id="bt_days" type="number" value="30" min="1" max="720"></div>
      <div><label>Başlangıç bakiye</label><input id="bt_balance" type="number" value="10000" step="100"></div>
    </div>
    <label>Strateji</label><select id="bt_strategy">${STRATS.map(s => `<option value="${s.key}">${esc(s.label)}</option>`).join('')}</select>
    <div class="params" id="bt_params"></div>
    <div class="divider"></div>
    <div class="grid2">
      <div><label>Emir USDT</label><input id="bt_order" type="number" value="250" step="10"></div>
      <div><label>Kaldıraç</label><input id="bt_lev" type="number" value="3" min="1" max="50"></div>
      <div><label>Stop-loss %</label><input id="bt_sl" type="number" value="2" step="0.1"></div>
      <div><label>Take-profit %</label><input id="bt_tp" type="number" value="4" step="0.1"></div>
      <div><label>Trailing %</label><input id="bt_trail" type="number" value="0" step="0.1"></div>
      <div><label>Komisyon %</label><input id="bt_fee" type="number" value="0.1" step="0.01"></div>
    </div>`;
  const drawParams = () => {
    const sc = STRATS.find(x => x.key === $('#bt_strategy').value);
    $('#bt_params').innerHTML = sc.schema.map(p => paramInput(p, undefined, 'btp')).join('');
  };
  drawParams();
  $('#bt_strategy').onchange = drawParams;
}

$('#btnRunBacktest').onclick = async () => {
  const key = $('#bt_strategy').value;
  const sc = STRATS.find(x => x.key === key);
  const params = {};
  sc.schema.forEach(p => {
    const node = $(`#btp_${p.name}`);
    if (!node) return;
    params[p.name] = p.type === 'bool' ? node.checked : (p.type === 'select' ? node.value : Number(node.value));
  });
  const body = {
    symbol: $('#bt_symbol').value.trim().toUpperCase(), market: $('#bt_market').value,
    interval: $('#bt_interval').value, days: Number($('#bt_days').value),
    strategy: key, params, start_balance: Number($('#bt_balance').value),
    fee_pct: Number($('#bt_fee').value), leverage: Number($('#bt_lev').value),
    risk: {
      order_usdt: Number($('#bt_order').value), sizing: 'fixed',
      stop_loss_pct: Number($('#bt_sl').value), take_profit_pct: Number($('#bt_tp').value),
      trailing_pct: Number($('#bt_trail').value), max_open_positions: 1,
    },
  };
  $('#btnRunBacktest').disabled = true;
  $('#btStatus').textContent = '⏳ Geçmiş veriler indiriliyor ve simülasyon çalışıyor…';
  try {
    BT = await post('/api/backtest', body);
    renderBacktest();
    $('#btStatus').textContent = `✓ ${BT.metrics.runtime_ms} ms · ${BT.metrics.candles} mum`;
    toast('Backtest tamamlandı', 'ok');
    loadBtHistory();
  } catch (e) { $('#btStatus').textContent = '✗ ' + e.message; toast(e.message, 'err'); }
  $('#btnRunBacktest').disabled = false;
};

function renderBacktest() {
  if (!BT) return;
  const m = BT.metrics;
  $('#btTitle').textContent = `${m.symbol} · ${m.interval} · ${dtStr(m.start_ms)} → ${dtStr(m.end_ms)}`;
  const items = [
    ['Net PnL', signed(m.net_pnl) + ' $', signCls(m.net_pnl)],
    ['Getiri', signed(m.net_pnl_pct) + '%', signCls(m.net_pnl_pct)],
    ['Al-Tut', signed(m.buy_hold_pct) + '%', signCls(m.buy_hold_pct)],
    ['Son Bakiye', money(m.end_equity) + ' $', ''],
    ['İşlem', m.closed_trades, ''],
    ['Kazanma', '%' + nf(m.win_rate, 1), ''],
    ['Profit Factor', nf(m.profit_factor, 2), m.profit_factor >= 1 ? 'pos' : 'neg'],
    ['Maks. Düşüş', '%' + nf(m.max_drawdown_pct, 2), 'neg'],
    ['Sharpe', nf(m.sharpe, 2), ''],
    ['Ort. Kazanç', money(m.avg_win), 'pos'],
    ['Ort. Kayıp', money(m.avg_loss), 'neg'],
    ['Komisyon', money(m.fees), ''],
  ];
  $('#btMetrics').innerHTML = items.map(([l, v, c]) => `<div class="metric"><label>${l}</label><b class="${c || ''}">${v}</b></div>`).join('');
  drawBtChart();
  const rows = (BT.trades || []).slice().reverse();
  $('#btTrades').innerHTML = rows.length ? `<thead><tr><th>Zaman</th><th>Yön</th><th>Miktar</th><th>Fiyat</th><th>PnL</th><th>Neden</th></tr></thead><tbody>` +
    rows.map(x => `<tr><td>${dtStr(x.ts)}</td>
      <td><span class="pill ${x.side === 'buy' ? 'buy' : 'sell'}">${x.side === 'buy' ? 'AL' : 'SAT'}</span></td>
      <td>${nf(x.qty, 6)}</td><td>${px(x.price)}</td>
      <td class="${signCls(x.realized_pnl)}">${x.action.startsWith('close') ? signed(x.realized_pnl) : '—'}</td>
      <td class="wrap tiny">${esc(x.reason)}</td></tr>`).join('') + '</tbody>'
    : '<tbody><tr><td class="empty">Bu ayarlarla işlem oluşmadı</td></tr></tbody>';
}

function drawBtChart() {
  const cv = $('#btChart'); if (!cv || !cv.clientWidth || !BT) return;
  const { ctx, w, h } = setupCanvas(cv);
  const c = BT.curve || [];
  if (c.length < 2) { ctx.fillStyle = '#8b98ad'; ctx.font = '12px sans-serif'; ctx.fillText('Veri yok', 14, 24); return; }
  const padL = 8, padR = 66, padT = 12, padB = 22;
  const eq = c.map(p => p.equity), pr = c.map(p => p.price);
  const eLo = Math.min(...eq), eHi = Math.max(...eq), pLo = Math.min(...pr), pHi = Math.max(...pr);
  const ePad = (eHi - eLo) * .12 || 1, pPad = (pHi - pLo) * .12 || 1;
  const X = (i) => padL + (i / (c.length - 1)) * (w - padL - padR);
  const YE = (v) => padT + (h - padT - padB) * (1 - (v - (eLo - ePad)) / ((eHi + ePad) - (eLo - ePad)));
  const YP = (v) => padT + (h - padT - padB) * (1 - (v - (pLo - pPad)) / ((pHi + pPad) - (pLo - pPad)));
  ctx.strokeStyle = 'rgba(35,44,61,.7)'; ctx.fillStyle = '#6b7789'; ctx.font = '10px ui-monospace,monospace';
  for (let g = 0; g <= 4; g++) {
    const y = padT + ((h - padT - padB) / 4) * g;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillText(money((eHi + ePad) - (((eHi + ePad) - (eLo - ePad)) / 4) * g), w - padR + 5, y + 3);
  }
  ctx.textAlign = 'center';
  for (let g = 0; g <= 4; g++) {
    const i = Math.floor((c.length - 1) * g / 4);
    ctx.fillText(dtStr(c[i].ts), X(i), h - 6);
  }
  // fiyat (gri)
  ctx.beginPath(); pr.forEach((v, i) => i ? ctx.lineTo(X(i), YP(v)) : ctx.moveTo(X(i), YP(v)));
  ctx.strokeStyle = 'rgba(139,152,173,.55)'; ctx.lineWidth = 1; ctx.stroke();
  // equity
  const up = eq[eq.length - 1] >= eq[0];
  const grad = ctx.createLinearGradient(0, padT, 0, h - padB);
  grad.addColorStop(0, up ? 'rgba(22,199,132,.3)' : 'rgba(234,57,67,.3)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(X(0), YE(eq[0]));
  eq.forEach((v, i) => ctx.lineTo(X(i), YE(v)));
  ctx.lineTo(X(eq.length - 1), h - padB); ctx.lineTo(X(0), h - padB); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath(); eq.forEach((v, i) => i ? ctx.lineTo(X(i), YE(v)) : ctx.moveTo(X(i), YE(v)));
  ctx.strokeStyle = up ? '#16c784' : '#ea3943'; ctx.lineWidth = 2; ctx.stroke();
  // islemler
  (BT.trades || []).forEach(t => {
    let i = c.findIndex(p => p.ts >= t.ts); if (i < 0) return;
    const x = X(i), y = YE(c[i].equity);
    ctx.fillStyle = t.side === 'buy' ? '#16c784' : '#ea3943';
    ctx.beginPath(); ctx.arc(x, y, 2.6, 0, Math.PI * 2); ctx.fill();
  });
  ctx.textAlign = 'left'; ctx.fillStyle = '#8b98ad';
  ctx.fillText('— özkaynak   — fiyat (gri)', padL + 4, padT + 10);
}

async function loadBtHistory() {
  try {
    const r = await api('/api/backtests');
    $('#btHistory').innerHTML = (r.backtests || []).map(b =>
      `<div class="bt-item" data-bt="${b.id}"><b>${b.params.symbol}</b> ${b.params.interval} · ${esc(b.params.strategy)}
       <div class="${signCls(b.metrics.net_pnl_pct)}">${signed(b.metrics.net_pnl_pct)}% · ${b.metrics.closed_trades} işlem · DD %${nf(b.metrics.max_drawdown_pct, 1)}</div>
       <div class="tiny muted">${dtStr(b.ts)}</div></div>`).join('') || '<div class="tiny muted">Kayıt yok</div>';
    $$('#btHistory [data-bt]').forEach(n => n.onclick = async () => {
      BT = await api('/api/backtest?id=' + n.dataset.bt);
      renderBacktest(); toast('Kayıtlı test yüklendi', 'ok');
    });
  } catch (e) { }
}

/* ---------------- guvenlik ---------------- */
const SCOPES = [
  ['testnet_spot', 'Spot Testnet', 'testnet.binance.vision üzerinden ücretsiz test anahtarı'],
  ['testnet_futures', 'Futures Testnet', 'testnet.binancefuture.com üzerinden test anahtarı'],
  ['spot', 'Spot CANLI', 'Gerçek para — sadece trade izni, çekim izni KAPALI'],
  ['futures', 'Futures CANLI', 'Gerçek para — kaldıraç riski'],
];
function renderKeys() {
  const st = CONFIG.keys_status || {};
  $('#keysList').innerHTML = SCOPES.map(([scope, label, note]) => {
    const s = st[scope] || {};
    return `<div class="key-row">
      <div class="key-head"><b>${label}</b>
        <span class="pill ${s.set ? 'on' : 'off'}">${s.set ? 'TANIMLI' : 'YOK'}</span>
        <span class="tiny muted">${s.masked || ''} ${s.source === 'env' ? '(env)' : ''}</span>
        <span class="grow"></span>
        <button class="btn tiny" data-test="${scope}">Bağlantı testi</button>
        <button class="btn tiny danger" data-del="${scope}">Sil</button>
      </div>
      <div class="tiny muted" style="margin-bottom:8px">${note}</div>
      <div class="grid2">
        <input id="key_${scope}" placeholder="API Key" autocomplete="off">
        <input id="sec_${scope}" placeholder="API Secret" type="password" autocomplete="off">
      </div>
      <button class="btn tiny primary" style="margin-top:8px" data-save="${scope}">Kaydet (şifreli)</button>
    </div>`;
  }).join('');
  $$('#keysList [data-save]').forEach(b => b.onclick = async () => {
    const sc = b.dataset.save;
    const key = $('#key_' + sc).value.trim(), secret = $('#sec_' + sc).value.trim();
    if (!key || !secret) return toast('Anahtar ve secret girin', 'err');
    try {
      const r = await post('/api/keys', { scope: sc, key, secret });
      CONFIG.keys_status = r.keys_status; renderKeys();
      $('#key_' + sc).value = ''; $('#sec_' + sc).value = '';
      toast('Anahtarlar şifreli olarak kaydedildi', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  });
  $$('#keysList [data-del]').forEach(b => b.onclick = async () => {
    try {
      const r = await api('/api/keys?scope=' + b.dataset.del, { method: 'DELETE' });
      CONFIG.keys_status = r.keys_status; renderKeys(); toast('Anahtarlar silindi', 'warn');
    } catch (e) { toast(e.message, 'err'); }
  });
  $$('#keysList [data-test]').forEach(b => b.onclick = async () => {
    try {
      const r = await post('/api/keys/test', { scope: b.dataset.test });
      const bal = Object.entries(r.info.balances || {}).slice(0, 6).map(([a, v]) => `${a}: ${nf(v, 4)}`).join(' · ');
      toast('Bağlantı OK — ' + (bal || 'bakiye yok'), 'ok');
    } catch (e) { toast('Bağlantı hatası: ' + e.message, 'err'); }
  });
}

$('#btnChangePw').onclick = async () => {
  const cur = $('#pwCurrent').value, n1 = $('#pwNew').value, n2 = $('#pwNew2').value;
  if (n1 !== n2) return toast('Yeni parolalar eşleşmiyor', 'err');
  if (n1.length < 6) return toast('Parola en az 6 karakter olmalı', 'err');
  try {
    await post('/api/password', { current: cur, new: n1 });
    toast('Parola değiştirildi — tekrar giriş yapın', 'ok');
    $('#pwCurrent').value = $('#pwNew').value = $('#pwNew2').value = '';
    setTimeout(showLogin, 900);
  } catch (e) { toast(e.message, 'err'); }
};

/* ---------------- otomatik giris kontrolu ---------------- */
(async () => {
  try {
    const s = await fetch('/api/state', { credentials: 'same-origin' });
    if (s.ok) { STATE = await s.json(); showApp(); await boot(); }
    else showLogin();
  } catch (e) { showLogin(); }
})();
