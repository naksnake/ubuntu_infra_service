// Shared helpers: CSRF-aware fetch, toast, node selection, job polling.
const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';

function toast(msg, isErr) {
  const t = document.getElementById('toast');
  if (!t) { if (isErr) alert(msg); return; }
  t.textContent = msg;
  t.classList.toggle('err', !!isErr);
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), 3200);
}

async function api(method, url, body) {
  const opt = { method, headers: { 'X-CSRF-Token': CSRF } };
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const res = await fetch(url, opt);
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtSize(n) {
  if (n == null) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + u[i];
}

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

// Collect checked node ids + group + cluster from a standard selector block
function selectedNodePayload(root) {
  const scope = root || document;
  const ids = [...scope.querySelectorAll('.node-cb:checked')].map(c => c.value);
  const group = (scope.querySelector('.group-input')?.value || '').trim();
  const cluster_id = scope.querySelector('.cluster-input')?.value || '';
  return { node_ids: ids, group, cluster_id };
}

// ── job output rendering ─────────────────────────────────────────────────────
// Multi-node output is grouped into nested collapsibles: infrastructure group
// <details> containing one host <details> each, summarised as
//   [group] 🌐 host (ip) | STATUS: SUCCESS
// Host blocks are delimited by '===== host (addr) … =====' headers (emitted by
// ClusterShell, the Slurm stages and file deploys); '##GROUP## name' lines mark
// which infrastructure group the following hosts belong to. Logs without any
// host header (onboarding, hardware scans) fall back to line colouring.

function _statusOf(header, body) {
  const m = /\|\s*STATUS:\s*([A-Z]+)/.exec(header);
  if (m) return m[1];
  const exit = /^\[.*\bexit\s+(\d+)\]\s*$/m.exec(body);
  if (exit) return exit[1] === '0' ? 'SUCCESS' : 'FAILED';
  if (/\b(fatal|error):|FAILED\b|not a valid controller|Unable to (contact|determine)/i.test(body))
    return 'FAILED';
  if (/\bCHANGED\b/.test(body)) return 'CHANGED';
  return body.trim() ? 'SUCCESS' : 'UNKNOWN';
}

function _statCls(s) {
  return s === 'SUCCESS' ? 'ok' : (s === 'CHANGED' ? 'chg'
    : (s === 'UNKNOWN' ? '' : 'err'));
}

// '<host> (<addr>, ssh root@:22)' / '<host> (<addr>) : label' → {host, addr, label}
function _parseHostHeader(inner) {
  const m = /^([^\s(]+)\s*(?:\(([^)]*)\))?\s*(?::\s*(.*?))?\s*$/.exec(
    inner.replace(/\|\s*STATUS:\s*[A-Z]+\s*$/, '').trim());
  if (!m) return { host: inner.trim(), addr: '', label: '' };
  const addr = (m[2] || '').split(',')[0].trim();
  return { host: m[1], addr, label: (m[3] || '').trim() };
}

function renderConsole(el, text) {
  const raw = String(text == null ? '' : text);
  const lines = raw.split('\n');
  const hasHosts = lines.some(l => /^=====.*=====\s*$/.test(l));
  if (!hasHosts) { el.innerHTML = _colorLines(lines); return; }

  const preamble = [];
  const groups = [];          // [{name, hosts:[{host,addr,label,body[]}]}]
  let curGroup = null, curHost = null;

  const groupFor = name => {
    let g = groups.find(x => x.name === name);
    if (!g) { g = { name, hosts: [] }; groups.push(g); }
    return g;
  };

  for (const line of lines) {
    const gm = /^##GROUP##\s*(.*)$/.exec(line);
    if (gm) { curGroup = groupFor(gm[1].trim() || 'ungrouped'); curHost = null; continue; }
    const hm = /^=====\s*(.*?)\s*=====\s*$/.exec(line);
    if (hm) {
      const meta = _parseHostHeader(hm[1]);
      curHost = { ...meta, header: hm[1], body: [] };
      (curGroup || (curGroup = groupFor('nodes'))).hosts.push(curHost);
      continue;
    }
    if (curHost) curHost.body.push(line);
    else preamble.push(line);
  }

  let html = preamble.join('\n').trim() ? `<div>${_colorLines(preamble)}</div>` : '';
  for (const g of groups) {
    const stats = g.hosts.map(h => _statusOf(h.header, h.body.join('\n')));
    const tally = {};
    stats.forEach(s => { tally[s] = (tally[s] || 0) + 1; });
    const bad = stats.some(s => s !== 'SUCCESS' && s !== 'UNKNOWN');
    const summary = Object.keys(tally).map(k =>
      `<span class="rstat ${_statCls(k)}" style="margin:0">${tally[k]} ${k}</span>`).join(' · ');
    html += `<details class="rgroup" ${bad || g.hosts.length === 1 ? 'open' : ''}>` +
      `<summary>[${esc(g.name)}] <span class="rcount">${g.hosts.length} host` +
      `${g.hosts.length === 1 ? '' : 's'}</span> ${summary}</summary><div class="rbody">`;
    g.hosts.forEach((h, i) => {
      const st = stats[i];
      html += `<details class="rhost" ${st !== 'SUCCESS' || g.hosts.length === 1 ? 'open' : ''}>` +
        `<summary>[${esc(g.name)}] 🌐 ${esc(h.host)}` +
        (h.addr ? ` (${esc(h.addr)})` : '') +
        (h.label ? ` <span class="rcount">· ${esc(h.label)}</span>` : '') +
        ` | STATUS: <span class="rstat ${_statCls(st)}">${st}</span></summary>` +
        `<pre>${_colorLines(h.body)}</pre></details>`;
    });
    html += '</div></details>';
  }
  el.innerHTML = html;
}

function _colorLines(lines) {
  return lines.map(line => {
    if (/^=====.*=====\s*$/.test(line)) return '<span class="c-host">' + esc(line) + '</span>';
    if (/^\[.*\bexit\s+0\]\s*$/.test(line) || /^(VALIDATE|BENCHMARK) PASSED\b/.test(line) || /^MANAGED\b/.test(line))
      return '<span class="c-ok">' + esc(line) + '</span>';
    if (/^\[.*\bexit\s+([1-9]\d*)\]\s*$/.test(line) || /^(VALIDATE|BENCHMARK) FAILED\b/.test(line) || /^FAILED[:\s]/.test(line) || /\b(fatal|error):/i.test(line) || /not a valid controller|Unable to (contact|determine)/i.test(line))
      return '<span class="c-err">' + esc(line) + '</span>';
    if (/^\[ccp\]/.test(line) || /^\[\d+\/\d+\]/.test(line) || /^\[verify\]/.test(line) || /^\s+(credentials OK|key installed|command execution OK|node renamed)/.test(line))
      return '<span class="c-info">' + esc(line) + '</span>';
    return esc(line);
  }).join('\n');
}

// Poll a job until it is no longer running, streaming output into `el`.
function pollJob(jobId, el, statusEl, onDone) {
  let stop = false;
  async function tick() {
    if (stop) return;
    try {
      const j = await api('GET', '/api/jobs/' + jobId);
      renderConsole(el, j.output || '(waiting for output…)');
      el.scrollTop = el.scrollHeight;
      if (statusEl) {
        statusEl.textContent = j.status + (j.exit_code != null ? ' · exit ' + j.exit_code : '');
        statusEl.className = 'badge st-' + j.status;
      }
      if (j.status === 'running') { setTimeout(tick, 1000); }
      else { stop = true; if (onDone) onDone(j); }
    } catch (e) { el.textContent += '\n[poll error] ' + e.message; }
  }
  tick();
  return () => { stop = true; };
}

// Node detail dialog (markup lives in base.html so every page can open it)
async function showDetail(id) {
  const dlg = document.getElementById('detail-dlg'), body = document.getElementById('dt-body');
  if (!dlg) return;
  body.textContent = 'Loading…'; dlg.showModal();
  try {
    const d = await api('GET', '/api/nodes/' + id);
    document.getElementById('dt-name').textContent = d.node.name;
    const rows = [['State', d.node.state + (d.node.state_detail ? ' — ' + d.node.state_detail : '')],
      ['Address', d.node.address], ['MAC', d.node.mac || '—'],
      ['SSH', d.node.conn === 'ssh' ? d.node.ssh_user + '@:' + d.node.ssh_port : 'local'],
      ['Topology', d.node.rack != null ? 'rack ' + d.node.rack + ' · sled ' + d.node.sled +
        (d.node.role ? ' · ' + d.node.role : '') : '—'],
      ['Onboarded', fmtTime(d.node.onboarded_at)]];
    const h = d.hardware;
    if (h) {
      rows.push(['OS', h.os_name || '—'], ['Kernel', h.kernel || '—'],
        ['CPU', (h.cpu_model || '—') + (h.cpu_cores ? ' — ' + h.cpu_cores + ' CPUs' : '') +
          (h.cpu_sockets ? ', ' + h.cpu_sockets + ' socket(s)' : '') +
          (h.threads_per_core ? ', ' + h.threads_per_core + ' thread(s)/core' : '')],
        ['Memory', h.mem_mb ? Math.round(h.mem_mb / 1024) + ' GB' : '—'],
        ['Disks', h.disks || '—'], ['Network', h.nics || '—'],
        ['GPU', h.gpu_count ? h.gpu_count + '× ' + (h.gpu_model || 'GPU') : 'none detected'],
        ['InfiniBand', h.infiniband || '—'],
        ['Facts updated', fmtTime(h.updated_at)]);
    } else rows.push(['Hardware', 'not scanned yet']);
    body.innerHTML = '<table>' + rows.map(r => '<tr><th style="width:130px;text-align:left">' +
      esc(r[0]) + '</th><td>' + esc(r[1]) + '</td></tr>').join('') + '</table>';
  } catch (e) { body.textContent = e.message; }
}

async function del(url, msg, cb) {
  if (!confirm(msg || 'Delete this item?')) return;
  try { await api('DELETE', url); toast('Deleted'); cb ? cb() : location.reload(); }
  catch (e) { toast(e.message, true); }
}
