let token = '';
let latest = null;
let submitting = false;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = value => value ? new Date(value * 1000).toLocaleString('en-US', {timeZone:'America/Los_Angeles', month:'short', day:'numeric', hour:'numeric', minute:'2-digit', timeZoneName:'short'}) : 'Not observed yet';
function render(data) {
  latest = data;
  const busy = submitting || ['queued', 'running'].includes(data.job?.state);
  const enabled = data.controls_enabled && token && !busy;
  $('refresh').disabled = !enabled;
  $('unlock').disabled = !data.controls_enabled || busy;
  if (data.job) $('action-status').textContent = `${data.job.action}: ${data.job.message}`;
  $('demo').hidden = !data.demo;
  const disks = data.disks.map(d => ({...d, stale: data.server_time - d.observed_at > data.poll_seconds * 2.5}));
  const count = state => disks.filter(d => !d.stale && d.state === state).length;
  $('summary').innerHTML = [['Disks', disks.length], ['Awake', count('awake')], ['Sleeping', count('standby')], ['Need attention', disks.filter(d => d.stale || d.error || !d.smart || d.smart.passed !== true || d.smart.warnings?.length).length]].map(([label,n]) => `<div><strong>${n}</strong><span>${label}</span></div>`).join('');
  $('disks').innerHTML = disks.map(d => {
    const s = d.smart, state = d.stale ? 'stale' : d.state;
    const health = !s || s.passed == null ? 'Not available' : !s.passed ? 'Failed' : s.warnings?.length ? 'Warnings' : 'Passed';
    return `<article><div class="card-top"><div><h3>${esc(d.label)}</h3><p>${esc(d.role)}</p></div><span class="badge ${esc(state)}">${esc(state === 'standby' ? 'Sleeping' : state)}</span></div><p class="serial">${esc(d.serial)}</p><dl><div><dt>Last observed spin-up</dt><dd>${date(d.last_up)}</dd></div><div><dt>Last observed spin-down</dt><dd>${date(d.last_down)}</dd></div><div><dt>SMART health</dt><dd>${health}</dd></div><div><dt>Temperature at last read</dt><dd>${s?.temperature != null ? esc(s.temperature) + ' °C' : '—'}</dd></div></dl><p class="read-time">Health data retrieved: ${d.smart_at ? date(d.smart_at) : 'Not collected yet'}</p>${d.error ? `<p class="warning">${esc(d.error)}</p>` : ''}${(s?.warnings || []).map(w => `<p class="warning">${esc(w)}</p>`).join('')}<p class="test-status">Self-test: ${esc(s?.test_status?.string || 'No status available')}${s?.test_status?.remaining_percent != null ? ' · ' + esc(s.test_status.remaining_percent) + '% remaining' : ''}</p><div class="disk-actions"><button data-action="spin-up" data-serial="${esc(d.serial)}" ${!enabled || d.state === 'awake' ? 'disabled' : ''}>Spin up</button><button data-action="spin-down" data-serial="${esc(d.serial)}" ${!enabled || !d.allow_spindown || d.state !== 'awake' ? 'disabled' : ''} title="Only unmounted configured spares can be spun down">Spin down</button><button data-action="health" data-serial="${esc(d.serial)}" ${!enabled ? 'disabled' : ''}>Refresh health</button><button data-action="test" data-serial="${esc(d.serial)}" ${!enabled ? 'disabled' : ''}>Extended self-test</button></div><details><summary>Counters &amp; diagnostic logs</summary><pre>${esc(JSON.stringify({counters:s?.counters,io:d.io,error_log:s?.error_log,self_test_log:s?.self_test_log}, null, 2))}</pre></details></article>`;
  }).join('') || '<p>Waiting for the first collection…</p>';
  const labels = Object.fromEntries(disks.map(d => [d.serial, d.label]));
  $('events').innerHTML = data.events.map(e => `<li><time>${date(e.observed_at)}</time><strong>${esc(labels[e.serial] || e.serial)}</strong> observed ${e.state === 'awake' ? 'awake' : 'sleeping'}</li>`).join('') || '<li>No transitions observed yet.</li>';
  $('connection').textContent = disks.some(d => d.stale) ? 'Collector data is stale' : `Updated · ${new Date().toLocaleTimeString()}`;
}
async function refresh() {
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw Error('Status unavailable');
    render(await response.json());
  } catch {
    $('connection').textContent = 'Disconnected · displayed values may be stale';
  }
}
$('unlock').addEventListener('click', () => {
  if (token) {
    token = '';
    $('unlock').textContent = 'Unlock controls';
  } else {
    token = prompt('Enter the NAS action token. It stays only in this page’s memory.')?.trim() || '';
    $('unlock').textContent = token ? 'Lock controls' : 'Unlock controls';
  }
  if (latest) render(latest);
});
async function action(name, serial = null) {
  if (submitting || !token) return;
  const disk = latest?.disks.find(d => d.serial === serial);
  let wake = false;
  if (name === 'spin-down' && !confirm(`Put ${disk.label} into standby? The NAS will check that it is unmounted.`)) return;
  if (name === 'test' && !confirm(`Start an extended self-test on ${disk.label}? This can take hours and may wake the disk. It is separate from retrieving health data.`)) return;
  if (['health', 'test'].includes(name) && disk?.state !== 'awake') {
    wake = confirm(`Allow ${disk.label} to wake for this request?`);
    if (!wake) return;
  }
  submitting = true;
  render(latest);
  $('action-status').textContent = 'Submitting…';
  try {
    const response = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type':'application/json', 'X-Action-Token':token},
      body: JSON.stringify({action:name, serial, wake})
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Request failed');
    latest.job = result;
  } catch (error) {
    alert(error.message);
  } finally {
    submitting = false;
    await refresh();
  }
}
$('refresh').addEventListener('click', () => action('refresh'));
$('disks').addEventListener('click', event => {
  const button = event.target.closest('button[data-action]');
  if (button && !button.disabled) action(button.dataset.action, button.dataset.serial);
});
async function tick() {
  await refresh();
  setTimeout(tick, ['queued', 'running'].includes(latest?.job?.state) ? 1000 : 5000);
}
tick();
