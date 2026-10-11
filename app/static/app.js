// small helpers shared by every page

async function api(path, opts = {}) {
  const o = { method: opts.method || 'GET', headers: {}, credentials: 'same-origin' };
  if (opts.body instanceof FormData) {
    o.body = opts.body;
  } else if (opts.body !== undefined) {
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(path, o);
  } catch (e) {
    throw new Error("Couldn't reach the server");
  }
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    location.href = '/login';
    throw new Error('Not logged in');
  }
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text.slice(0, 200) }; }
  if (!res.ok) {
    throw new Error((data && data.detail) || ('Error ' + res.status));
  }
  return data;
}

function num(n) {
  if (n === null || n === undefined || n === '') return '';
  return String(Math.round(Number(n) * 100) / 100);
}

function qty(amount, unit, plural) {
  const n = Number(amount);
  return num(n) + ' ' + (n === 1 ? unit : (plural || unit));
}

function shelfLabel(kind) {
  return kind === 'use_by' ? 'Use by' : 'Best by';
}

function today() {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 10);
}

function addDays(iso, n) {
  const d = new Date(iso + 'T12:00:00');
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}

function daysUntil(iso) {
  if (!iso) return null;
  const a = new Date(today() + 'T12:00:00'), b = new Date(iso + 'T12:00:00');
  return Math.round((b - a) / 86400000);
}

// returns {text, cls} for showing a best by / use by date
function expiry(iso, kind) {
  if (!iso) return { text: '', cls: '' };
  const d = daysUntil(iso);
  const label = shelfLabel(kind);
  let when;
  if (d < 0) when = (d === -1 ? 'yesterday' : (-d) + ' days ago');
  else if (d === 0) when = 'today';
  else if (d === 1) when = 'tomorrow';
  else if (d < 14) when = 'in ' + d + ' days';
  else when = iso;
  let cls = 'ok';
  if (d < 0) cls = (kind === 'use_by' ? 'bad' : 'warn');
  else if (d <= 2) cls = 'warn';
  return { text: label + ' ' + when, cls };
}

function pic(name) {
  return name ? '/uploads/' + encodeURIComponent(name) : '';
}

document.addEventListener('alpine:init', () => {
  Alpine.store('toast', {
    msg: '', bad: false, t: null,
    show(msg, bad = false) {
      this.msg = msg; this.bad = bad;
      clearTimeout(this.t);
      this.t = setTimeout(() => { this.msg = ''; }, bad ? 6000 : 2500);
    },
  });
});

function toast(msg, bad = false) { Alpine.store('toast').show(msg, bad); }
function oops(e) { toast(e.message || String(e), true); }

async function logout() {
  await api('/api/auth/logout', { method: 'POST' });
  location.href = '/login';
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
