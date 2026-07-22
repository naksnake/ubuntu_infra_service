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

// Poll a job until it is no longer running, streaming output into `el`.
function pollJob(jobId, el, statusEl, onDone) {
  let stop = false;
  async function tick() {
    if (stop) return;
    try {
      const j = await api('GET', '/api/jobs/' + jobId);
      el.textContent = j.output || '(waiting for output…)';
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

async function del(url, msg, cb) {
  if (!confirm(msg || 'Delete this item?')) return;
  try { await api('DELETE', url); toast('Deleted'); cb ? cb() : location.reload(); }
  catch (e) { toast(e.message, true); }
}
