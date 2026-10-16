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
const tb = n => Number.isFinite(n) ? (n / 10 ** 12).toFixed(1) + ' TB' : '—';
const pct = n => Number.isFinite(n) ? n + '%' : '—';
const stateLabel = (d, state) => state === 'standby' ? 'Sleeping' : state === 'awake' ? (d.type === 'nvme' ? 'Active' : 'Awake') : state;
const smartHealth = s => !s || s.passed == null ? 'Not available' : !s.passed ? 'Failed' : s.active_warnings?.length ? 'Warnings' : s.known_warnings?.length ? 'Known issue' : 'Passed';
const temp = s => s?.temperature != null ? esc(s.temperature) + ' °C' : '—';
const usedPct = c => c?.status === 'available' && c.total_bytes ? Math.round(c.used_bytes / c.total_bytes * 100) + '%' : '—';
// Each card shows what is needed at a glance: state, health, temperature and
// anything wrong. Everything else folds away under Details & controls.
function details(key, summary, body) {
  return `<details data-key="${esc(key)}"><summary>${summary}</summary>${body}</details>`;
}
// NVMe drives: always active, so no power history or controls; health, wear
// and capacity instead. Written is in decimal TB, as drive makers rate it.
function nvmeCard(d, s, state, health, enabled) {
  const w = s?.wear || {};
  const spare = Number.isFinite(w.available_spare) ? `${w.available_spare}% (threshold ${pct(w.available_spare_threshold)})` : '—';
  const more = `<p class="serial">${esc(d.serial)}</p><dl><div><dt>Spare capacity</dt><dd>${esc(spare)}</dd></div><div><dt>Data written</dt><dd>${tb(w.written_bytes)}</dd></div><div><dt>Power-on hours</dt><dd>${esc(w.power_on_hours?.toLocaleString())}</dd></div><div><dt>Media errors</dt><dd>${esc(w.media_errors)}</dd></div><div><dt>Unsafe shutdowns</dt><dd>${esc(w.unsafe_shutdowns?.toLocaleString())}</dd></div><div><dt>Time above warning temperature</dt><dd>${w.warning_temperature_minutes != null ? esc(w.warning_temperature_minutes) + ' min' : '—'}</dd></div></dl><p class="read-time">Health data retrieved: ${d.smart_at ? date(d.smart_at) : 'Not collected yet'}</p>${capacityHtml(d.capacity)}<pre>${esc(JSON.stringify({wear:w,io:d.io}, null, 2))}</pre>`;
  return `<article class="nvme" id="disk-${esc(d.serial)}"><div class="card-top"><div><h3>${esc(d.label)}</h3><p>${esc(d.role)} · NVMe</p></div><span class="badge ${esc(state)}">${esc(stateLabel(d, state))}</span></div><div class="wear"><div class="wear-label"><span>Endurance used</span><strong>${pct(w.percentage_used)}</strong></div><div class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${esc(w.percentage_used ?? 0)}" aria-label="Endurance used"><span style="width:${Math.min(100, Math.max(0, w.percentage_used || 0))}%"></span></div></div><dl><div><dt>SMART health</dt><dd>${health}</dd></div><div><dt>Temperature</dt><dd>${temp(s)}</dd></div><div><dt>Used</dt><dd>${usedPct(d.capacity)}</dd></div></dl>${d.error ? `<p class="warning">${esc(d.error)}</p>` : ''}${warningHtml(d, enabled)}${details('d-' + d.serial, 'Details', more)}</article>`;
}
function ataCard(d, s, state, health, enabled) {
  const more = `<p class="serial">${esc(d.serial)}</p><p class="read-time">Health data retrieved: ${d.smart_at ? date(d.smart_at) : 'Not collected yet'}</p><p class="test-status">Self-test: ${esc(s?.test_status?.string || 'No status available')}${s?.test_status?.remaining_percent != null ? ' · ' + esc(s.test_status.remaining_percent) + '% remaining' : ''}</p>${extendedTestHtml(d)}${capacityHtml(d.capacity, d.state !== 'awake')}<div class="disk-actions"><button data-action="spin-up" data-serial="${esc(d.serial)}" ${!enabled || d.state === 'awake' ? 'disabled' : ''}>Spin up</button><button data-action="spin-down" data-serial="${esc(d.serial)}" ${!enabled || !d.allow_spindown || d.state !== 'awake' ? 'disabled' : ''} title="Only unmounted configured spares can be spun down">Spin down</button><button data-action="health" data-serial="${esc(d.serial)}" ${!enabled ? 'disabled' : ''}>Refresh health</button><button data-action="test" data-serial="${esc(d.serial)}" ${!enabled ? 'disabled' : ''}>Extended self-test</button></div><pre>${esc(JSON.stringify({counters:s?.counters,io:d.io,error_log:s?.error_log,self_test_log:s?.self_test_log}, null, 2))}</pre>`;
  return `<article id="disk-${esc(d.serial)}"><div class="card-top"><div><h3>${esc(d.label)}</h3><p>${esc(d.role)}${d.expected_state ? ' · should be ' + esc(d.expected_state) : ''}</p></div><span class="badge ${esc(state)}">${esc(stateLabel(d, state))}</span></div>${d.unexpected_state ? `<p class="unexpected">${esc(state === 'standby' ? 'Sleeping' : 'Awake')}, but should be ${esc(d.expected_state)}</p>` : ''}<dl><div><dt>SMART health</dt><dd>${health}</dd></div><div><dt>Temperature</dt><dd>${temp(s)}</dd></div><div><dt>Last spin-up</dt><dd>${date(d.last_up)}</dd></div><div><dt>Last spin-down</dt><dd>${date(d.last_down)}</dd></div></dl>${d.error ? `<p class="warning">${esc(d.error)}</p>` : ''}${warningHtml(d, enabled)}${details('d-' + d.serial, 'Details &amp; controls', more)}</article>`;
}
// Disk groups, in this order. Roles come from the NAS's disk list.
const GROUPS = [['NVMe drives', d => d.type === 'nvme'], ['Pool', d => d.expected_state !== 'asleep'], ['Cold spares', () => true]];
function groupDisks(disks) {
  const groups = GROUPS.map(([name]) => [name, []]);
  for (const d of disks) groups[GROUPS.findIndex(([, test]) => test(d))][1].push(d);
  return groups.filter(([, members]) => members.length);
}
// One card per backup job. The NAS's systemd hooks record each run; this
// only shows what they wrote, plus whether the last success is too old.
const BACKUP_BADGES = {ok: 'OK', running: 'Running', waiting: 'Waiting', problem: 'Problem', unknown: 'Unknown'};
function backupsHtml(backups) {
  if (!backups) return '<p>Backup status unavailable.</p>';
  if (backups.error) return `<p class="warning">Backup list unreadable: ${esc(backups.error)}</p>`;
  if (!backups.jobs.length) return '<p>No backup jobs configured.</p>';
  return backups.jobs.map(j => {
    const r = j.restic;
    const restic = r ? `<div><dt>Last snapshot</dt><dd>${esc(r.snapshot_id)}</dd></div><div><dt>Data added</dt><dd>${esc(r.data_added_label)}</dd></div><div><dt>New / changed files</dt><dd>${esc(r.files_new?.toLocaleString())} / ${esc(r.files_changed?.toLocaleString())}</dd></div>` : '';
    return `<article class="backup"><div class="card-top"><div><h3>${esc(j.name)}</h3><p>${esc(j.method || '')}${j.method ? ' · ' : ''}${esc(j.schedule_label)}</p></div><span class="badge ${esc(j.state)}">${esc(BACKUP_BADGES[j.state] || j.state)}</span></div>${j.problem ? `<p class="warning">${esc(j.problem)}</p>` : ''}<dl><div><dt>Status</dt><dd>${esc(j.state_label)}</dd></div><div><dt>Last success</dt><dd>${esc(j.last_success_label)}</dd></div><div><dt>Last run ended</dt><dd>${j.finished_at ? date(j.finished_at) : 'Not recorded'}</dd></div><div><dt>Last result</dt><dd>${esc(j.result_label)}</dd></div>${restic}</dl><p class="read-time">Overdue after ${esc(j.max_age_label)} without a success</p></article>`;
  }).join('');
}
// The Synology's Log Center events, forwarded by its Log Sending. DSM logs
// each wake but not when the disks went to sleep.
function synologyHtml(s) {
  const rows = s.wakes.map(w => `<li><time>${esc(w.woke_label)}</time>Disks woke up from hibernation</li>`).join('') || '<li>No wakes recorded yet.</li>';
  const log = `<p><a href="/synology-log">View the full log</a></p>` + (s.log.length ? `<details data-key="synology-log"><summary>Recent log lines (newest first)</summary><pre>${esc(s.log.join('\n'))}</pre></details>` : '<p>No log lines received yet.</p>');
  return `<h2>Synology disk sleep</h2><dl><div><dt>Last wake</dt><dd>${esc(s.last_wake_label)}</dd></div><div><dt>Wakes, last 24 hours</dt><dd>${esc(s.wakes_24h)}</dd></div><div><dt>Wakes, last 7 days</dt><dd>${esc(s.wakes_7d)}</dd></div><div><dt>Last log line received</dt><dd>${s.last_line_at ? date(s.last_line_at) : 'None yet'}</dd></div></dl><p>From the Synology's Log Center, sent by its Log Sending as each event happens. It records when the disks wake, not when they went to sleep.</p><ol>${rows}</ol>${log}`;
}
// The overview: one tile per area (green, red, or grey for information
// only), then every current problem in one list.
function tile(tab, title, value, detail, tone) {
  return `<a class="tile ${tone}" href="#${tab}"><span class="tile-title"><i class="dot" aria-hidden="true"></i>${esc(title)}</span><strong>${esc(value)}</strong><span>${esc(detail)}</span></a>`;
}
function overviewHtml(data, disks) {
  const h = data.health || {};
  const sync = data.snapraid || {};
  const jobs = data.backups?.jobs || [];
  const spinning = disks.filter(d => d.type !== 'nvme' && !d.stale);
  const awake = spinning.filter(d => d.state === 'awake').length;
  const tone = result => !result ? 'info' : result.ok ? 'ok' : 'bad';
  const pool = data.pool || {};
  const tiles = [
    tile('disks', 'Disks', h.disks?.ok ? 'Healthy' : `${h.disks?.problems?.length || 0} problem${h.disks?.problems?.length === 1 ? '' : 's'}`, `${awake} awake · ${spinning.length - awake} asleep`, tone(h.disks)),
    tile('storage', 'Parity sync', sync.state_label || 'Unavailable', sync.finished_at ? 'Last finished ' + date(sync.finished_at) : '', tone(h.sync)),
    tile('backups', 'Backups', jobs.length ? `${jobs.filter(j => j.state === 'ok').length} of ${jobs.length} OK` : 'None configured', jobs.filter(j => j.state === 'running').length ? 'A job is running' : jobs.filter(j => j.state === 'waiting').length ? `${jobs.filter(j => j.state === 'waiting').length} awaiting first run` : '', tone(h.backups)),
    tile('storage', 'Pool', pool.total_bytes ? Math.round(pool.used_bytes / pool.total_bytes * 100) + '% used' : 'Unavailable', pool.available_label && pool.available_label !== 'Unavailable' ? pool.available_label + ' available' : '', 'info'),
  ];
  if (data.synology?.configured) tiles.push(tile('synology', 'Synology', data.synology.last_wake_label === 'None recorded' ? 'No wakes yet' : 'Woke ' + data.synology.last_wake_label, `${data.synology.wakes_24h} wake${data.synology.wakes_24h === 1 ? '' : 's'} in 24 hours`, 'info'));
  const problems = [...(h.disks?.problems || []).map(p => ['disks', p]), ...(h.sync?.problems || []).map(p => ['storage', 'Parity sync: ' + p]), ...(h.backups?.problems || []).map(p => ['backups', p])];
  return [tiles.join(''), problems.length ? `<section class="problems"><h2>Needs a look</h2><ul>${problems.map(([tab, p]) => `<li><a href="#${tab}">${esc(p)}</a></li>`).join('')}</ul></section>` : '<p class="all-good">Everything looks fine.</p>'];
}
function diskTableHtml(disks) {
  return `<table><thead><tr><th>Disk</th><th>Role</th><th>State</th><th>Health</th><th>Temp</th><th>Used</th></tr></thead><tbody>${disks.map(d => {
    const state = d.stale ? 'stale' : d.state;
    const flag = d.needs_attention || d.unexpected_state;
    return `<tr class="${flag ? 'flag' : ''}"><td><a href="#disks/${esc(d.serial)}">${esc(d.label)}</a></td><td>${esc(d.role)}</td><td><span class="badge ${esc(state)}">${esc(stateLabel(d, state))}</span>${d.unexpected_state ? ` <span class="unexpected-inline">should be ${esc(d.expected_state)}</span>` : ''}</td><td>${smartHealth(d.smart)}</td><td>${temp(d.smart)}</td><td>${usedPct(d.capacity)}</td></tr>`;
  }).join('')}</tbody></table>`;
}
// Tabs live in the URL (#disks, or #disks/<serial> to jump to one disk), so
// a refresh or a shared link opens the same view.
function showTab() {
  const [tab, target] = (location.hash.slice(1) || 'overview').split('/');
  const panel = document.querySelector(`[data-panel="${CSS.escape(tab)}"]`) ? tab : 'overview';
  document.querySelectorAll('[data-panel]').forEach(p => p.hidden = p.dataset.panel !== panel);
  document.querySelectorAll('.tabs a').forEach(a => a.toggleAttribute('aria-current', a.getAttribute('href') === '#' + panel));
  const card = target && document.getElementById('disk-' + target);
  if (card) card.scrollIntoView({block: 'start'});
  return Boolean(card) || !target;
}
// A #disks/<serial> link opened directly waits for the first data to scroll.
let pendingScroll = true;
window.addEventListener('hashchange', showTab);
function render(data) {
  latest = data;
  // Re-rendering replaces the markup; keep open sections open.
  const open = new Set([...document.querySelectorAll('details[open][data-key]')].map(d => d.dataset.key));
  const sync = data.snapraid || {};
  $('snapraid').innerHTML = `<h2>SnapRAID parity sync</h2><dl><div><dt>Current status</dt><dd>${esc(sync.state_label || 'Unavailable')}</dd></div><div><dt>Last finished</dt><dd>${esc(sync.finished_label || 'Not recorded')}</dd></div><div><dt>Last finished result</dt><dd>${esc(sync.result_label || 'Not recorded')}</dd></div><div><dt>Current / last start</dt><dd>${date(sync.started_at)}</dd></div></dl>${sync.detail ? `<p class="read-time">${esc(sync.detail)}</p>` : ''}<p>Parity sync: ${esc(data.schedules?.sync?.label || 'Unavailable')}</p><p>Full parity scrub: ${esc(data.schedules?.scrub?.label || 'Unavailable')}</p><p>Tracks scheduled syncs and manual runs through snapraid-sync.service.</p>`;
  $('backups').innerHTML = backupsHtml(data.backups);
  $('capacity').innerHTML = `<h2>Pool capacity</h2>${capacityHtml(data.pool)}<p>Available is mergerfs filesystem availability. Individual files must fit on a member disk; the pool creation policy also applies.</p>`;
  $('synology-tab').hidden = !data.synology?.configured;
  if (data.synology?.configured) $('synology').innerHTML = synologyHtml(data.synology);
  const busy = submitting || ['queued', 'running'].includes(data.job?.state);
  const enabled = data.controls_enabled && token && !busy;
  $('refresh').disabled = !enabled;
  $('unlock').disabled = !data.controls_enabled || busy;
  $('unlock').textContent = token ? 'Lock controls' : 'Unlock controls';
  if (data.job) $('action-status').textContent = `${data.job.action}: ${data.job.message}`;
  $('demo').hidden = !data.demo;
  const disks = data.disks.map(d => ({...d, stale: data.server_time - d.observed_at > data.poll_seconds * 2.5}));
  const [tiles, problems] = overviewHtml(data, disks);
  $('status-tiles').innerHTML = tiles;
  $('problems').innerHTML = problems;
  $('disk-table').innerHTML = diskTableHtml(disks);
  const count = state => disks.filter(d => !d.stale && d.state === state && d.type !== 'nvme').length;
  $('summary').innerHTML = [['Disks', disks.length], ['Awake', count('awake')], ['Sleeping', count('standby')], ['Need attention', disks.filter(d => d.needs_attention).length], ['Known issues', disks.filter(d => d.smart?.known_warnings?.length).length], ['Health unknown', disks.filter(d => d.health_unknown).length], ['Unexpected state', disks.filter(d => d.unexpected_state).length]].map(([label,n]) => `<div><strong>${n}</strong><span>${label}</span></div>`).join('');
  $('disks').innerHTML = groupDisks(disks).map(([name, members]) => `<div class="section-heading"><h2>${esc(name)}</h2><p>${members.length} disk${members.length === 1 ? '' : 's'}</p></div><div class="grid">${members.map(d => {
    const s = d.smart, state = d.stale ? 'stale' : d.state;
    return (d.type === 'nvme' ? nvmeCard : ataCard)(d, s, state, smartHealth(s), enabled);
  }).join('')}</div>`).join('') || '<p>Waiting for the first collection…</p>';
  const labels = Object.fromEntries(disks.map(d => [d.serial, d.label]));
  $('events').innerHTML = data.events.map(e => `<li><time>${date(e.observed_at)}</time><strong>${esc(labels[e.serial] || e.serial)}</strong> observed ${e.state === 'awake' ? 'awake' : 'sleeping'}</li>`).join('') || '<li>No transitions observed yet.</li>';
  document.querySelectorAll('details[data-key]').forEach(d => { if (open.has(d.dataset.key)) d.open = true; });
  if (pendingScroll) pendingScroll = !showTab();
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
showTab();
tick();
