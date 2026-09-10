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

// Collect checked node ids + group from a standard selector block
function selectedNodePayload(root) {
  const scope = root || document;
  const ids = [...scope.querySelectorAll('.node-cb:checked')].map(c => c.value);
  const group = (scope.querySelector('.group-input')?.value || '').trim();
  return { node_ids: ids, group };
}

// ── job output rendering ─────────────────────────────────────────────────────
// Multi-node output is grouped into nested collapsibles: infrastructure group
// <details> containing one host <details> each, summarised as
//   [group] 🌐 host (ip) | STATUS: SUCCESS
// Host blocks are delimited by '===== host (addr) … =====' headers (emitted by
// ClusterShell and file deploys); '##GROUP## name' lines mark
// which infrastructure group the following hosts belong to. Logs without any
// host header (onboarding, hardware scans) fall back to line colouring.

function _statusOf(header, body) {
  const m = /\|\s*STATUS:\s*([A-Z]+)/.exec(header);
  if (m) return m[1];
  const exit = /^\[.*\bexit\s+(\d+)\]\s*$/m.exec(body);
  if (exit) return exit[1] === '0' ? 'SUCCESS' : 'FAILED';
  if (/\b(fatal|error):|FAILED\b/i.test(body))
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

// SSH connection chatter that is never useful output. Suppressed at the
// source with -o LogLevel=ERROR; stripped here too so logs captured before
// that (or by tools we don't control) read cleanly.
const SSH_NOISE = [
  /^Warning: Permanently added .* to the list of known hosts\.?\s*$/,
  /^Warning: the \S+ host key for .* differs from the key for the IP address/,
  /^Warning: Permanently added the \S+ host key for IP address/,
];

// The stream is tokenized into an ORDERED list of sections (one per
// '##STAGE## k/N name' marker a multi-step job may emit, plus the untitled
// lead-in), each holding text blocks and host groups in the order they were
// written — so playbook output, per-host frames and summaries never get
// shuffled. `running` marks a stage without an end marker as still running.
function renderConsole(el, text, running) {
  const raw = String(text == null ? '' : text);
  const lines = raw.split('\n').filter(l => !SSH_NOISE.some(re => re.test(l)));
  const hasHosts = lines.some(l => /^=====.*=====\s*$/.test(l));
  const hasStages = lines.some(l => /^##STAGE##/.test(l));
  if (!hasHosts && !hasStages) { el.innerHTML = _colorLines(lines); return; }

  // ── 1. tokenize ──
  const sections = [];          // [{title, num, status, items:[text|group]}]
  const byName = new Map();     // host name -> latest host object (late exit lines)
  let sec = null, curGroup = null, curHost = null;
  const newSection = (title, num) => {
    sec = { title, num, status: null, items: [] };
    sections.push(sec); curGroup = null; curHost = null;
  };
  newSection(null, '');
  const lastItem = () => sec.items[sec.items.length - 1];
  const groupFor = name => {
    const li = lastItem();
    if (li && li.type === 'group' && li.name === name) return li;
    const g = { type: 'group', name, hosts: [] };
    sec.items.push(g);
    return g;
  };
  const textItem = () => {
    const li = lastItem();
    if (li && li.type === 'text') return li;
    const t = { type: 'text', lines: [] };
    sec.items.push(t);
    return t;
  };

  for (const line of lines) {
    const sm = /^##STAGE##\s*(?:(\d+\/\d+)\s+)?(.*)$/.exec(line);
    if (sm) { newSection(sm[2].trim(), sm[1] || ''); continue; }
    const em = /^##STAGE-END##\s*\S+\s+(PASSED|FAILED)/.exec(line);
    if (em) { sec.status = em[1]; curHost = null; continue; }

    const gm = /^##GROUP##\s*(.*)$/.exec(line);
    if (gm) { curGroup = groupFor(gm[1].trim() || 'ungrouped'); curHost = null; continue; }

    const hm = /^=====\s*(.*?)\s*=====\s*$/.exec(line);
    if (hm) {
      const meta = _parseHostHeader(hm[1]);
      curHost = { ...meta, header: hm[1], logs: [], exit_code: null };
      // keep filling the open group; a text block in between starts a new one
      if (!curGroup || lastItem() !== curGroup) curGroup = groupFor(curGroup ? curGroup.name : 'nodes');
      curGroup.hosts.push(curHost);
      byName.set(meta.host, curHost);
      continue;
    }

    // A trailing '[<host> exit N]' / '[<host> timed out …]' belongs to the host
    // it NAMES, wherever it appears in the stream — never to whichever block
    // happens to be open (ClusterShell flushes these after all the output).
    const fm = /^\[(\S+)\s+(?:exit\s+(\d+)|(timed out[^\]]*))\]\s*$/.exec(line);
    if (fm) {
      const owner = byName.get(fm[1]);
      if (owner) {
        owner.exit_code = fm[2] !== undefined ? parseInt(fm[2], 10) : 124;
        if (fm[3]) owner.logs.push(line);
        continue;
      }
    }
    if (curHost) curHost.logs.push(line);
    else textItem().lines.push(line);
  }

  // ── 2. render in order: sections → items; 1 host = 1 <details> ──
  const renderItems = items => items.map(it => it.type === 'text'
    ? (it.lines.join('\n').trim() ? `<div class="rtext">${_colorLines(it.lines)}</div>` : '')
    : _renderGroup(it)).join('');
  let html = '';
  sections.forEach((s, i) => {
    if (s.title === null) { html += renderItems(s.items); return; }
    const st = s.status || (running ? 'RUNNING' : 'STOPPED');
    const last = i === sections.length - 1;
    const cls = st === 'PASSED' ? 'ok' : (st === 'FAILED' ? 'err' : (st === 'RUNNING' ? 'chg' : ''));
    html += `<details class="rstage" ${st !== 'PASSED' || last ? 'open' : ''}>` +
      `<summary><span class="rnum">${esc(s.num)}</span> ${esc(s.title)}` +
      ` <span class="rstat ${cls}">${st}</span></summary>` +
      `<div class="rbody">${renderItems(s.items)}</div></details>`;
  });
  el.innerHTML = html;
}

// one infrastructure group → static header + one <details> per host
function _renderGroup(g) {
  const stats = g.hosts.map(h => h.exit_code === null
    ? _statusOf(h.header, h.logs.join('\n'))
    : (h.exit_code === 0 ? (_statusOf(h.header, '') === 'CHANGED' ? 'CHANGED' : 'SUCCESS')
                         : 'FAILED'));
  const tally = {};
  stats.forEach(s => { tally[s] = (tally[s] || 0) + 1; });
  const summary = Object.keys(tally).map(k =>
    `<span class="rstat ${_statCls(k)}">${tally[k]} ${k}</span>`).join(' · ');
  // static header, not a toggle: it must never wrap/mush the host blocks
  let html = `<div class="rgroup"><div class="rghead">[${esc(g.name)}] ` +
    `<span class="rcount">${g.hosts.length} host${g.hosts.length === 1 ? '' : 's'}` +
    `</span> ${summary}</div><div class="rbody">`;
  g.hosts.forEach((h, i) => {
    const st = stats[i];
    const exitLine = h.exit_code === null ? ''
      : `\n<span class="${h.exit_code === 0 ? 'c-ok' : 'c-err'}">[${esc(h.host)} exit ${h.exit_code}]</span>`;
    html += `<details class="rhost" ${st !== 'SUCCESS' || g.hosts.length === 1 ? 'open' : ''}>` +
      `<summary>[${esc(g.name)}] 🌐 ${esc(h.host)}` +
      (h.addr ? ` (${esc(h.addr)})` : '') +
      (h.label ? ` <span class="rcount">· ${esc(h.label)}</span>` : '') +
      ` | STATUS: <span class="rstat ${_statCls(st)}">${st}</span></summary>` +
      `<pre>${_colorLines(h.logs)}${exitLine}</pre></details>`;
  });
  return html + '</div></div>';
}

function _colorLines(lines) {
  return lines.map(line => {
    if (/^=====.*=====\s*$/.test(line)) return '<span class="c-host">' + esc(line) + '</span>';
    if (/^\[.*\bexit\s+0\]\s*$/.test(line) || /^[A-Z ]+ PASSED\b/.test(line) || /^MANAGED\b/.test(line))
      return '<span class="c-ok">' + esc(line) + '</span>';
    if (/^\[.*\bexit\s+([1-9]\d*)\]\s*$/.test(line) || /^[A-Z ]+ FAILED\b/.test(line) || /^FAILED[:\s]/.test(line) || /\b(fatal|error):/i.test(line))
      return '<span class="c-err">' + esc(line) + '</span>';
    if (/^\s*WARNING\b/.test(line) || /\bwarning:/i.test(line))
      return '<span class="c-warn">' + esc(line) + '</span>';
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
      renderConsole(el, j.output || '(waiting for output…)', j.status === 'running');
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
