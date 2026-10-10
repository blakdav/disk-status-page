const $ = id => document.getElementById(id);
const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = value => value ? new Date(value * 1000).toLocaleString('en-US', {timeZone:'America/Los_Angeles', month:'short', day:'numeric', hour:'numeric', minute:'2-digit', timeZoneName:'short'}) : 'Not observed yet';
function render(data) {
  $('demo').hidden = !data.demo;
  const disks = data.disks.map(d => ({...d, stale: data.server_time - d.observed_at > data.poll_seconds * 2.5}));
  const count = state => disks.filter(d => !d.stale && d.state === state).length;
  $('summary').innerHTML = [['Disks', disks.length], ['Awake', count('awake')], ['Sleeping', count('standby')], ['Need attention', disks.filter(d => d.stale || d.error || !d.smart || d.smart.passed !== true || d.smart.warnings?.length).length]].map(([label,n]) => `<div><strong>${n}</strong><span>${label}</span></div>`).join('');
  $('disks').innerHTML = disks.map(d => {
    const s = d.smart, state = d.stale ? 'stale' : d.state;
    const health = !s || s.passed == null ? 'Not available' : !s.passed ? 'Failed' : s.warnings?.length ? 'Warnings' : 'Passed';
    return `<article><div class="card-top"><div><h3>${esc(d.label)}</h3><p>${esc(d.role)}</p></div><span class="badge ${esc(state)}">${esc(state === 'standby' ? 'Sleeping' : state)}</span></div><p class="serial">${esc(d.serial)}</p><dl><div><dt>Last observed spin-up</dt><dd>${date(d.last_up)}</dd></div><div><dt>Last observed spin-down</dt><dd>${date(d.last_down)}</dd></div><div><dt>SMART health</dt><dd>${health}</dd></div><div><dt>Temperature at last read</dt><dd>${s?.temperature != null ? esc(s.temperature) + ' °C' : '—'}</dd></div></dl><p class="read-time">SMART read: ${d.smart_at ? date(d.smart_at) : 'Not collected yet'}</p>${d.error ? `<p class="warning">${esc(d.error)}</p>` : ''}${(s?.warnings || []).map(w => `<p class="warning">${esc(w)}</p>`).join('')}<details><summary>Counters &amp; diagnostic logs</summary><pre>${esc(JSON.stringify({counters:s?.counters,io:d.io,error_log:s?.error_log,self_test_log:s?.self_test_log}, null, 2))}</pre></details></article>`;
  }).join('') || '<p>Waiting for the first collection…</p>';
  const labels = Object.fromEntries(disks.map(d => [d.serial, d.label]));
  $('events').innerHTML = data.events.map(e => `<li><time>${date(e.observed_at)}</time><strong>${esc(labels[e.serial] || e.serial)}</strong> observed ${e.state === 'awake' ? 'awake' : 'sleeping'}</li>`).join('') || '<li>No transitions observed yet.</li>';
  $('connection').textContent = disks.some(d => d.stale) ? 'Collector data is stale' : `Updated · ${new Date().toLocaleTimeString()}`;
}
async function refresh() {
  try { const response = await fetch('/api/status'); if (!response.ok) throw Error(); render(await response.json()); }
  catch { $('connection').textContent = 'Disconnected · displayed values may be stale'; }
  finally { setTimeout(refresh, 15000); }
}
refresh();
