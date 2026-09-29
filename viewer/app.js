// work-tracker viewer: keeps the page in step with the tracker files.
// Every 3 s it asks the server for a version "<data>.<code>.<synced>". A data change swaps <main> in place, keeping
// open tickets and the active filter; a code change (this file, style.css, page.html, the Python) reloads the page;
// synced is when the server last pulled PR state from GitHub.

const slug = document.body.dataset.slug;
const live = document.getElementById('live');
let version = document.body.dataset.version;
let filter = 'all';

const GATES = ['ready', 'blocked']; // matched on data-b, not the status

function applyFilter() {
  document.querySelectorAll('.filters button').forEach(b => b.setAttribute('aria-pressed', b.dataset.f === filter));
  document.querySelectorAll('details.t').forEach(d => {
    const { s, b, c } = d.dataset; // c: "1" when the server counts the ticket closed
    d.hidden = !(filter === 'all' ||
      (filter === 'active' ? c !== '1' : GATES.includes(filter) ? b === filter : s === filter));
  });
  // A step's number shows on its first shown row only.
  let step = null;
  document.querySelectorAll('details.t:not([hidden])').forEach(d => {
    d.classList.toggle('rep', d.dataset.step === step);
    step = d.dataset.step;
  });
}

function detailsFor(id) {
  return document.querySelector(`details[data-id="${CSS.escape(id)}"]`);
}

function openRecord(id) {
  const d = detailsFor(id);
  if (d) {
    if (d.hidden) { filter = 'all'; applyFilter(); }
    for (let p = d; p; p = p.parentElement.closest('details')) p.open = true; // also the sections around it
    d.scrollIntoView({ block: 'start' });
  }
}

// `tracker open <id>` puts the id in the hash. It opens once: the hash is then cleared, so a reload starts collapsed.
function openHash() {
  const id = decodeURIComponent(location.hash.slice(1));
  if (!id) return;
  openRecord(id);
  history.replaceState(null, '', location.pathname + location.search);
}

async function poll() {
  try {
    const next = await (await fetch(`/t/${slug}/version`, { cache: 'no-store' })).text();
    const [data, code, synced] = next.split('.');
    const [oldData, oldCode] = version.split('.');
    if (code !== oldCode) return location.reload();
    if (data !== oldData) {
      // Keep each part open or closed as it was, also a section that starts open; new parts take their default.
      const was = new Map([...document.querySelectorAll('details[data-id]')].map(d => [d.dataset.id, d.open]));
      document.querySelector('main').innerHTML =
        await (await fetch(`/t/${slug}/main`, { cache: 'no-store' })).text();
      was.forEach((open, id) => { const d = detailsFor(id); if (d) d.open = open; });
      applyFilter();
    }
    version = next;
    const at = t => t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    live.textContent = 'live · ' + at(new Date()) +
      (Number(synced) ? ' · PRs from GitHub ' + at(new Date(synced * 1000)) : '');
    live.className = 'on';
  } catch {
    live.textContent = 'viewer stopped — run `tracker open`';
    live.className = 'off';
  }
}

if (slug) {
  document.addEventListener('click', e => {
    const b = e.target.closest('.filters button');
    if (b) { filter = b.dataset.f; applyFilter(); }
    const a = e.target.closest('a[href^="#"]');
    if (a) { e.preventDefault(); openRecord(decodeURIComponent(a.getAttribute('href').slice(1))); } // no hash, no history
  });
  window.addEventListener('hashchange', openHash);
  applyFilter();
  openHash();
  poll();
  setInterval(poll, 3000);
}
