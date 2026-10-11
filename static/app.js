let token = '';
let latest = null;
let submitting = false;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = value => value ? new Date(value * 1000).toLocaleString('en-US', {timeZone:'America/Los_Angeles', month:'short', day:'numeric', hour:'numeric', minute:'2-digit', timeZoneName:'short'}) : 'Not observed yet';
const bytes = n => Number.isFinite(n) ? (n / 2 ** 40).toFixed(2) + ' TiB' : '—';
function capacityHtml(c, sleeping = false) {
  if (!c || c.status !== 'available') return `<p>Capacity: ${esc(c?.status === 'unmounted' ? 'Unmounted' : 'Not available yet')}</p>${c?.error ? `<p class="warning">${esc(c.error)}</p>` : ''}`;
  return `<dl><div><dt>Used</dt><dd>${bytes(c.used_bytes)}</dd></div><div><dt>Free</dt><dd>${bytes(c.free_bytes)}</dd></div><div><dt>Available</dt><dd>${bytes(c.available_bytes)}</dd></div></dl><p class="read-time">${sleeping || c.cached ? 'Cached · ' : ''}Capacity measured: ${date(c.observed_at)}</p>`;
}
function warningHtml(disk, enabled) {
  const smart = disk.smart || {};
  return (smart.issues || []).map(issue => {
    const known = issue.acknowledged;
    const delta = issue.increased_by ? ` · +${issue.increased_by} since acknowledgement` : '';
    const note = known ? 'Known issue · unchanged: ' : '';
    const control = issue.acknowledgeable ? `<button data-action="${known ? 'unacknowledge' : 'acknowledge'}" data-serial="${esc(disk.serial)}" data-attribute="${esc(issue.attribute_id)}" data-raw="${esc(issue.raw)}" ${!enabled ? 'disabled' : ''}>${known ? 'Undo acknowledgement' : 'Acknowledge current value'}</button>` : '';
    return `<div class="${known ? 'known-issue' : 'warning'}"><p>${esc(note + issue.message + delta)}</p>${control}</div>`;
  }).join('');
}
function extendedTestHtml(disk) {
  const test = disk.extended_test || {};
  const dateWithYear = value => value ? new Date(value * 1000).toLocaleString('en-US', {
    timeZone: 'America/Los_Angeles', year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', timeZoneName: 'short'
  }) : 'Not recorded';
  const startedLabel = test.last_started_source === 'observed' ? 'First observed running' : 'Last recorded extended start';
  return `<dl class="extended-test"><div><dt>${startedLabel}</dt><dd>${dateWithYear(test.last_started_at)}</dd></div><div><dt>Last observed test ended</dt><dd>${dateWithYear(test.last_observed_completed_at)}</dd></div></dl><p class="test-status">Last extended result: ${esc(test.last_result || 'Not available')}${test.result_source === 'historical' ? ' · Historical result; calendar date unknown' : ''}${test.last_result_power_on_hours != null ? ' · ' + esc(test.last_result_power_on_hours) + ' power-on hours' : ''}</p>${test.pending ? '<p class="test-status">Awaiting a new extended-test result; the last result above is from an earlier test.</p>' : ''}`;
}
function render(data) {
  latest = data;
  const sync = data.snapraid || {};
  $('snapraid').innerHTML = `<h2>SnapRAID parity sync</h2><dl><div><dt>Current status</dt><dd>${esc(sync.state_label || 'Unavailable')}</dd></div><div><dt>Last finished</dt><dd>${esc(sync.finished_label || 'Not recorded')}</dd></div><div><dt>Last finished result</dt><dd>${esc(sync.result_label || 'Not recorded')}</dd></div><div><dt>Current / last start</dt><dd>${date(sync.started_at)}</dd></div></dl>${sync.detail ? `<p class="read-time">${esc(sync.detail)}</p>` : ''}<p>Parity sync: ${esc(data.schedules?.sync?.label || 'Unavailable')}</p><p>Full parity scrub: ${esc(data.schedules?.scrub?.label || 'Unavailable')}</p><p>Tracks scheduled syncs and manual runs through snapraid-sync.service.</p>`;
  $('capacity').innerHTML = `<h2>Pool capacity</h2>${capacityHtml(data.pool)}<p>Available is mergerfs filesystem availability. Individual files must fit on a member disk; the pool creation policy also applies.</p>`;
  const busy = submitting || ['queued', 'running'].includes(data.job?.state);
  const enabled = data.controls_enabled && token && !busy;
  $('refresh').disabled = !enabled;
  $('unlock').disabled = !data.controls_enabled || busy;
  $('unlock').textContent = token ? 'Lock controls' : 'Unlock controls';
  if (data.job) $('action-status').textContent = `${data.job.action}: ${data.job.message}`;
  $('demo').hidden = !data.demo;
  const disks = data.disks.map(d => ({...d, stale: data.server_time - d.observed_at > data.poll_seconds * 2.5}));
  const count = state => disks.filter(d => !d.stale && d.state === state).length;
  $('summary').innerHTML = [['Disks', disks.length], ['Awake', count('awake')], ['Sleeping', count('standby')], ['Need attention', disks.filter(d => d.needs_attention).length], ['Known issues', disks.filter(d => d.smart?.known_warnings?.length).length], ['Health unknown', disks.filter(d => d.health_unknown).length]].map(([label,n]) => `<div><strong>${n}</strong><span>${label}</span></div>`).join('');
  $('disks').innerHTML = disks.map(d => {
    const s = d.smart, state = d.stale ? 'stale' : d.state;
    const health = !s || s.passed == null ? 'Not available' : !s.passed ? 'Failed' : s.active_warnings?.length ? 'Warnings' : s.known_warnings?.length ? 'Known issue' : 'Passed';
    return `<article><div class="card-top"><div><h3>${esc(d.label)}</h3><p>${esc(d.role)}</p></div><span class="badge ${esc(state)}">${esc(state === 'standby' ? 'Sleeping' : state)}</span></div><p class="serial">${esc(d.serial)}</p><dl><div><dt>Last observed spin-up</dt><dd>${date(d.last_up)}</dd></div><div><dt>Last observed spin-down</dt><dd>${date(d.last_down)}</dd></div><div><dt>SMART health</dt><dd>${health}</dd></div><div><dt>Temperature at last read</dt><dd>${s?.temperature != null ? esc(s.temperature) + ' °C' : '—'}</dd></div></dl><p class="read-time">Health data retrieved: ${d.smart_at ? date(d.smart_at) : 'Not collected yet'}</p>${d.error ? `<p class="warning">${esc(d.error)}</p>` : ''}${warningHtml(d, enabled)}<p class="test-status">Self-test: ${esc(s?.test_status?.string || 'No status available')}${s?.test_status?.remaining_percent != null ? ' · ' + esc(s.test_status.remaining_percent) + '% remaining' : ''}</p>${extendedTestHtml(d)}${capacityHtml(d.capacity, d.state !== 'awake')}<div class="disk-actions"><button data-action="spin-up" data-serial="${esc(d.serial)}" ${!enabled || d.state === 'awake' ? 'disabled' : ''}>Spin up</button><button data-action="spin-down" data-serial="${esc(d.serial)}" ${!enabled || !d.allow_spindown || d.state !== 'awake' ? 'disabled' : ''} title="Only unmounted configured spares can be spun down">Spin down</button><button data-action="health" data-serial="${esc(d.serial)}" ${!enabled ? 'disabled' : ''}>Refresh health</button><button data-action="test" data-serial="${esc(d.serial)}" ${!enabled ? 'disabled' : ''}>Extended self-test</button></div><details><summary>Counters &amp; diagnostic logs</summary><pre>${esc(JSON.stringify({counters:s?.counters,io:d.io,error_log:s?.error_log,self_test_log:s?.self_test_log}, null, 2))}</pre></details></article>`;
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
$('unlock').addEventListener('click', async () => {
  if (token) {
    token = '';
  } else {
    $('unlock').disabled = true;
    try {
      const response = await fetch('/api/unlock', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Requested-With': 'DiskStatusPage'},
        body: '{}'
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || 'Could not unlock controls');
      token = result.token;
    } catch (error) {
      $('action-status').textContent = error.message;
    }
  }
  if (latest) render(latest);
});
async function action(name, serial = null, attributeId = null, expectedRaw = null) {
  if (submitting || !token) return;
  const disk = latest?.disks.find(d => d.serial === serial);
  let wake = false;
  if (name === 'acknowledge' && !confirm(`Acknowledge the current raw count (${expectedRaw}) for ${disk.label}? It will stay visible as a known issue. Any increase will need attention again.`)) return;
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
      body: JSON.stringify({action:name, serial, wake, attribute_id:attributeId, expected_raw:expectedRaw})
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
  if (button && !button.disabled) action(button.dataset.action, button.dataset.serial, button.dataset.attribute ? Number(button.dataset.attribute) : null, button.dataset.raw ? Number(button.dataset.raw) : null);
});
async function tick() {
  await refresh();
  setTimeout(tick, ['queued', 'running'].includes(latest?.job?.state) ? 1000 : 5000);
}
tick();
