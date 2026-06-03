/* bedrock-lens UI — frontend logic */

let chart     = null;
let histChart = null;
let evtSource = null;

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(name) {
  document.getElementById('tab-live').style.display    = name === 'live'    ? '' : 'none';
  document.getElementById('tab-history').style.display = name === 'history' ? '' : 'none';
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelector(`.tab[onclick="switchTab('${name}')"]`).classList.add('active');
  if (name === 'history') loadHistory();
}

// ── Chart helpers ─────────────────────────────────────────────────────────────
function makeChart(canvasId, label, color) {
  const ctx = document.getElementById(canvasId).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label,
        data: [],
        borderColor: color,
        backgroundColor: color.replace(')', ',0.08)').replace('rgb', 'rgba'),
        borderWidth: 2,
        pointRadius: 2,
        tension: 0.3,
        fill: true,
      }]
    },
    options: {
      responsive: true,
      animation: false,
      plugins: {
        legend: { labels: { color: '#e6edf3' } },
        tooltip: { callbacks: { label: ctx => `$${ctx.parsed.y.toFixed(4)}` } }
      },
      scales: {
        x: { ticks: { color: '#8b949e', maxTicksLimit: 8 }, grid: { color: '#21262d' } },
        y: { ticks: { color: '#8b949e', callback: v => `$${v.toFixed(4)}` }, grid: { color: '#21262d' } }
      }
    }
  });
}

function updateChart(c, series, labelFn) {
  if (!c) return;
  c.data.labels = series.map(labelFn);
  c.data.datasets[0].data = series.map(s => s.cost);
  c.update();
}

// ── Table render ──────────────────────────────────────────────────────────────
function renderRows(tbodyId, rows, newIds = []) {
  const body = document.getElementById(tbodyId);
  if (!rows || rows.length === 0) {
    body.innerHTML = '<tr><td colspan="6" class="empty">No data for this period.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => `
    <tr class="${newIds.length ? 'new-row' : ''}">
      <td>${r.model}</td>
      <td>${r.calls.toLocaleString()}</td>
      <td>${r.input.toLocaleString()}</td>
      <td>${r.output.toLocaleString()}</td>
      <td>${r.total.toLocaleString()}</td>
      <td>${r.cost}</td>
    </tr>`).join('');
}

// ── Live tab ──────────────────────────────────────────────────────────────────
function params() {
  return new URLSearchParams({
    since:  'today',
    region: document.getElementById('region').value,
  });
}

function applyLiveData(data, newIds = []) {
  renderRows('usage-body', data.rows, newIds);
  document.getElementById('total-cost').textContent  = data.total_cost;
  document.getElementById('total-calls').textContent = data.total_calls.toLocaleString();
  document.getElementById('updated').textContent     = `Updated ${data.updated_at}`;
  if (!chart) chart = makeChart('cost-chart', 'Cumulative cost ($)', 'rgb(88,166,255)');
  updateChart(chart, data.series, s => {
    const d = new Date(s.ts);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  });
  checkThreshold();
}

function checkThreshold() {
  const thresh  = parseFloat(document.getElementById('threshold').value);
  const current = parseFloat(document.getElementById('total-cost').textContent.replace('$', ''));
  const banner  = document.getElementById('alert-banner');
  if (!isNaN(thresh) && !isNaN(current) && current >= thresh)
    banner.classList.remove('hidden');
  else
    banner.classList.add('hidden');
}

function stopLive() {
  if (evtSource) { evtSource.close(); evtSource = null; }
}

function startLive() {
  stopLive();
  evtSource = new EventSource(`/api/usage/live?${params()}`);
  evtSource.onmessage = e => {
    const data = JSON.parse(e.data);
    applyLiveData(data, data.new_ids || []);
  };
  evtSource.onerror = () => {
    console.warn('SSE error — reconnecting in 5s');
    stopLive();
    setTimeout(startLive, 5000);
  };
}

function restartLive() {
  startLive();
}

// ── History tab ───────────────────────────────────────────────────────────────
async function loadHistory() {
  const days   = document.getElementById('hist-days').value;
  const region = document.getElementById('hist-region').value;
  const rParam = region ? `&region=${region}` : '';

  const [aggResp, dailyResp] = await Promise.all([
    fetch(`/api/history?days=${days}${rParam}`),
    fetch(`/api/history/daily${region ? '?region=' + region : ''}`),
  ]);
  const agg   = await aggResp.json();
  const daily = await dailyResp.json();

  renderRows('hist-body', agg.rows);
  document.getElementById('hist-total-cost').textContent  = agg.total_cost;
  document.getElementById('hist-total-calls').textContent = agg.total_calls.toLocaleString();

  if (!histChart) histChart = makeChart('hist-chart', 'Daily cumulative cost ($)', 'rgb(63,185,80)');
  updateChart(histChart, agg.series, s => {
    const d = new Date(s.ts);
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  });

  const dailyBody = document.getElementById('daily-body');
  if (!daily.length) {
    dailyBody.innerHTML = '<tr><td colspan="3" class="empty">No history yet.</td></tr>';
  } else {
    dailyBody.innerHTML = daily.slice(0, parseInt(days)).map(r => `
      <tr>
        <td>${r.day}</td>
        <td>${r.calls.toLocaleString()}</td>
        <td>$${r.cost.toFixed(4)}</td>
      </tr>`).join('');
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
startLive();
