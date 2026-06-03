/* bedrock-lens UI — frontend */

let chart     = null;
let histChart = null;
let allocPie  = null;
let evtSource = null;
let allocNs   = 'type';

const PIE_COLORS = [
  '#58a6ff','#3fb950','#d29922','#f85149','#bc8cff',
  '#79c0ff','#56d364','#e3b341','#ff7b72','#d2a8ff',
];

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(name) {
  ['live','history','allocation'].forEach(t => {
    document.getElementById('tab-' + t).style.display = t === name ? '' : 'none';
  });
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelector(`.tab[onclick="switchTab('${name}')"]`).classList.add('active');
  if (name === 'history')    loadHistory();
  if (name === 'allocation') loadAllocation();
}

// ── Chart helpers ─────────────────────────────────────────────────────────────
function makeLineChart(id, label, color) {
  return new Chart(document.getElementById(id).getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [{ label, data: [],
      borderColor: color,
      backgroundColor: color.replace('rgb(','rgba(').replace(')',',0.08)'),
      borderWidth: 2, pointRadius: 2, tension: 0.3, fill: true }] },
    options: {
      responsive: true, animation: false,
      plugins: { legend: { labels: { color: '#e6edf3' } },
        tooltip: { callbacks: { label: c => `$${c.parsed.y.toFixed(4)}` } } },
      scales: {
        x: { ticks: { color:'#8b949e', maxTicksLimit:8 }, grid:{ color:'#21262d' } },
        y: { ticks: { color:'#8b949e', callback: v=>`$${v.toFixed(4)}` }, grid:{ color:'#21262d' } }
      }
    }
  });
}

function makePieChart(id) {
  return new Chart(document.getElementById(id).getContext('2d'), {
    type: 'doughnut',
    data: { labels: [], datasets: [{ data: [], backgroundColor: PIE_COLORS, borderWidth: 1, borderColor: '#21262d' }] },
    options: {
      responsive: true, animation: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#e6edf3', padding: 12, font: { size: 12 } } },
        tooltip: { callbacks: { label: c => ` $${c.parsed.toFixed(4)} (${c.label})` } }
      }
    }
  });
}

function updateLineChart(c, series, labelFn) {
  if (!c) return;
  c.data.labels = series.map(labelFn);
  c.data.datasets[0].data = series.map(s => s.cost);
  c.update();
}

// ── Table render ──────────────────────────────────────────────────────────────
function renderRows(tbodyId, cols, rows, newIds=[]) {
  const body = document.getElementById(tbodyId);
  if (!rows?.length) {
    body.innerHTML = `<tr><td colspan="${cols}" class="empty">No data for this period.</td></tr>`;
    return;
  }
  body.innerHTML = rows.map(r => `
    <tr class="${newIds.length?'new-row':''}">
      <td>${r.model ?? r.tag ?? r.label}</td>
      <td>${(r.calls??0).toLocaleString()}</td>
      ${cols === 6
        ? `<td>${(r.input??0).toLocaleString()}</td><td>${(r.output??0).toLocaleString()}</td><td>${(r.total??0).toLocaleString()}</td>`
        : `<td>$${(r.cost??0).toFixed(4)}</td>`}
      <td>${cols===6 ? r.cost : r.share}</td>
    </tr>`).join('');
}

// ── Live tab ──────────────────────────────────────────────────────────────────
function liveParams() {
  return new URLSearchParams({ since:'today', region: document.getElementById('region').value });
}

function applyLiveData(data, newIds=[]) {
  renderRows('usage-body', 6, data.rows, newIds);
  document.getElementById('total-cost').textContent  = data.total_cost;
  document.getElementById('total-calls').textContent = data.total_calls.toLocaleString();
  document.getElementById('updated').textContent     = `Updated ${data.updated_at}`;
  if (!chart) chart = makeLineChart('cost-chart','Cumulative cost ($)','rgb(88,166,255)');
  updateLineChart(chart, data.series, s => new Date(s.ts).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}));
  checkThreshold();
}

function checkThreshold() {
  const thresh  = parseFloat(document.getElementById('threshold').value);
  const current = parseFloat(document.getElementById('total-cost').textContent.replace('$',''));
  document.getElementById('alert-banner').classList.toggle('hidden', isNaN(thresh)||isNaN(current)||current<thresh);
}

function stopLive() { if (evtSource) { evtSource.close(); evtSource = null; } }

function startLive() {
  stopLive();
  evtSource = new EventSource(`/api/usage/live?${liveParams()}`);
  evtSource.onmessage = e => { const d=JSON.parse(e.data); applyLiveData(d, d.new_ids||[]); };
  evtSource.onerror   = () => { console.warn('SSE error — retry in 5s'); stopLive(); setTimeout(startLive,5000); };
}

function restartLive() { startLive(); }

// ── History tab ───────────────────────────────────────────────────────────────
async function loadHistory() {
  const days   = document.getElementById('hist-days').value;
  const region = document.getElementById('hist-region').value;
  const tag    = document.getElementById('hist-tag').value;
  const rp     = region ? `&region=${region}` : '';
  const tp     = tag    ? `&tags=${tag}`      : '';

  const [agg, daily] = await Promise.all([
    fetch(`/api/history?days=${days}${rp}${tp}`).then(r=>r.json()),
    fetch(`/api/history/daily${region?'?region='+region:''}`).then(r=>r.json()),
  ]);

  renderRows('hist-body', 6, agg.rows);
  document.getElementById('hist-total-cost').textContent  = agg.total_cost;
  document.getElementById('hist-total-calls').textContent = agg.total_calls.toLocaleString();

  if (!histChart) histChart = makeLineChart('hist-chart','Daily cumulative cost ($)','rgb(63,185,80)');
  updateLineChart(histChart, agg.series, s => new Date(s.ts).toLocaleDateString([],{month:'short',day:'numeric'}));

  const db = document.getElementById('daily-body');
  db.innerHTML = daily.length
    ? daily.slice(0,parseInt(days)).map(r=>`<tr><td>${r.day}</td><td>${r.calls.toLocaleString()}</td><td>$${r.cost.toFixed(4)}</td></tr>`).join('')
    : '<tr><td colspan="3" class="empty">No history yet.</td></tr>';
}

// Populate the tag filter dropdown from known tags
async function loadTagOptions() {
  const tags = await fetch('/api/tags').then(r=>r.json());
  const sel  = document.getElementById('hist-tag');
  // keep the "All sources" option, add the rest
  const existing = new Set([...sel.options].map(o=>o.value));
  tags.forEach(t => {
    if (!existing.has(t.tag)) {
      const o = document.createElement('option');
      o.value = t.tag;
      o.textContent = `${t.tag} ($${t.cost.toFixed(2)})`;
      sel.appendChild(o);
    }
  });
}

// ── Allocation tab ────────────────────────────────────────────────────────────
function setNs(ns) {
  allocNs = ns;
  document.querySelectorAll('.tag-ns').forEach(b => b.classList.toggle('active', b.dataset.ns===ns));
  loadAllocation();
}

async function loadAllocation() {
  const days = document.getElementById('alloc-days').value;
  const data = await fetch(`/api/allocation?days=${days}&namespace=${allocNs}`).then(r=>r.json());

  const total = data.reduce((s,r)=>s+r.cost,0);

  if (!allocPie) allocPie = makePieChart('alloc-pie');
  allocPie.data.labels = data.map(r=>r.label);
  allocPie.data.datasets[0].data = data.map(r=>r.cost);
  allocPie.update();

  const body = document.getElementById('alloc-body');
  if (!data.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty">No tagged data yet — data is tagged as events are ingested.</td></tr>';
    return;
  }
  body.innerHTML = data.map(r => {
    const share = total > 0 ? ((r.cost/total)*100).toFixed(1)+'%' : '–';
    return `<tr>
      <td>${r.tag}</td>
      <td>${r.calls.toLocaleString()}</td>
      <td>$${r.cost.toFixed(4)}</td>
      <td>${share}</td>
    </tr>`;
  }).join('');
}

// ── Init ──────────────────────────────────────────────────────────────────────
startLive();
loadTagOptions();
