// The full Synology log, a page at a time, newest first. Filtering happens
// on the server, across both log files.
const $ = id => document.getElementById(id);
const PAGE = 500;
let query = '';
let offset = 0;
async function load(append) {
  const params = new URLSearchParams({q: query, offset, limit: PAGE});
  try {
    const response = await fetch('/api/synology-log?' + params);
    if (!response.ok) throw Error(response.status === 404 ? 'The Synology log is not set up.' : 'Log unavailable');
    const page = await response.json();
    const text = page.lines.join('\n');
    $('log').textContent = append && $('log').textContent ? $('log').textContent + '\n' + text : text;
    offset = page.offset + page.lines.length;
    $('count').textContent = (query ? `${page.matched.toLocaleString()} of ${page.total.toLocaleString()} lines match` : `${page.total.toLocaleString()} lines`) + (page.matched ? ` · showing ${offset.toLocaleString()}` : '');
    $('more').hidden = offset >= page.matched;
  } catch (error) {
    $('count').textContent = error.message;
  }
}
$('search').addEventListener('submit', event => {
  event.preventDefault();
  query = $('query').value.trim();
  offset = 0;
  load(false);
});
$('more').addEventListener('click', () => load(true));
load(false);
