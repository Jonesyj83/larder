// small helpers shared by every page

const BASE = window.BASE_PATH || '';

async function api(path, opts = {}) {
  // the server refuses cookie-authenticated writes without this header (see SameOriginWrites)
  const o = { method: opts.method || 'GET', headers: {'X-Requested-With': 'fetch'}, credentials: 'same-origin' };
  if (opts.body instanceof FormData) {
    o.body = opts.body;
  } else if (opts.body !== undefined) {
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(BASE + path, o);
  } catch (e) {
    throw new Error("Couldn't reach the server");
  }
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    location.href = BASE + '/login';
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
  const display = displayQuantity(amount, unit);
  const n = Number(display.amount);
  const label = n !== 0 && Math.abs(n) < .01 ? String(Number(n.toPrecision(3))) : num(n);
  return label + ' ' + (display.unit !== unit ? display.unit : (n === 1 ? unit : (plural || unit)));
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
  else when = formatDate(iso);
  let cls = 'ok';
  if (d < 0) cls = 'bad';
  else if (d <= 3) cls = 'warn';
  return { text: label + ' ' + formatDate(iso) + (when === formatDate(iso) ? '' : ' (' + when + ')'), cls };
}

function pic(name) {
  return name ? BASE + '/uploads/' + encodeURIComponent(name) : '';
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
  location.href = BASE + '/login';
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register(BASE + '/sw.js', { scope: BASE + '/' }).catch(() => {});
}

function preferences() {
  return window.USER_PREFS || {date_format: 'DD/MM/YYYY', measurement_system: 'metric', show_prices: false, portion_size: 'regular'};
}

const PORTION_FACTOR = {small: 0.5, regular: 1.0, large: 1.5};

function portionFactor() {
  return PORTION_FACTOR[preferences().portion_size] || 1.0;
}

function formatDate(iso, format = preferences().date_format) {
  if (!iso) return '';
  const [y, m, d] = iso.slice(0, 10).split('-');
  return format === 'YYYY-MM-DD' ? `${y}-${m}-${d}` :
    format === 'MM/DD/YYYY' ? `${m}/${d}/${y}` : `${d}/${m}/${y}`;
}

function parseDate(text, format = preferences().date_format) {
  if (text === '') return '';
  const slash = /^(\d{2})\/(\d{2})\/(\d{4})$/;
  const match = text.match(format === 'YYYY-MM-DD' ? /^(\d{4})-(\d{2})-(\d{2})$/ : slash);
  if (!match) throw Error('Enter a date as ' + format);
  const [y, m, d] = format === 'YYYY-MM-DD' ? match.slice(1) :
    format === 'MM/DD/YYYY' ? [match[3], match[1], match[2]] : [match[3], match[2], match[1]];
  const iso = `${y}-${m}-${d}`;
  const date = new Date(iso + 'T12:00:00Z');
  if (+y < 1 || isNaN(date) || date.toISOString().slice(0, 10) !== iso)
    throw Error('Enter a valid date as ' + format);
  return iso;
}

// x-modelable keeps the parent's value canonical; invalid text stays visible
// and blocks native form submission without replacing the last valid value.
function dateField(value) {
  return {
    iso: value || '', text: '', error: '',
    init() { this.sync(this.iso); this.$watch('iso', value => this.sync(value)); },
    sync(value) { this.iso = value || ''; this.text = formatDate(this.iso); this.error = '';
      if (this.$refs?.input) this.$refs.input.setCustomValidity(''); },
    accept() {
      try { const value = parseDate(this.text); this.error = ''; this.$refs.input.setCustomValidity(''); this.iso = value; }
      catch (e) { this.error = e.message; this.$refs.input.setCustomValidity(this.error); }
    },
  };
}

const MASS = {g: 1, kg: 1000, oz: 28.349523125, lb: 453.59237};
const VOLUME = {ml: 1, l: 1000, 'fl oz (US)': 29.5735295625};
function preferredUnits(units, selected) {
  const imperial = preferences().measurement_system === 'imperial';
  const preferred = imperial ? ['oz', 'lb', 'fl oz (US)'] : ['g', 'kg', 'ml', 'l'];
  // Keep all existing/custom units selectable, and merely foreground the preference.
  return [...units].sort((a, b) => Number(preferred.includes(b.name)) - Number(preferred.includes(a.name)) ||
    Number(b.id == selected) - Number(a.id == selected));
}

function displayQuantity(amount, unit) {
  const normalized = String(unit || '').toLowerCase();
  const group = MASS[normalized] ? MASS : VOLUME[normalized === 'fl oz (us)' ? 'fl oz (US)' : normalized] ? VOLUME : null;
  if (!group) return {amount, unit};
  const source = normalized === 'fl oz (us)' ? 'fl oz (US)' : normalized;
  const base = Number(amount) * group[source];
  const imperial = preferences().measurement_system === 'imperial';
  const target = group === MASS ? (imperial ? (Math.abs(base) >= MASS.lb ? 'lb' : 'oz') : (Math.abs(base) >= 1000 ? 'kg' : 'g')) :
    (imperial ? 'fl oz (US)' : (Math.abs(base) >= 1000 ? 'l' : 'ml'));
  const factor = unitFactor(source, target);
  return factor === null ? {amount, unit} : {amount: Number(amount) * factor, unit: target};
}

function productUnitChoices(units, product) {
  const stock = units.find(u => u.id == product.unit_id);
  const stockName = stock?.name.toLowerCase();
  const group = MASS[stockName] ? MASS : VOLUME[stockName === 'fl oz (us)' ? 'fl oz (US)' : stockName] ? VOLUME : {};
  return preferredUnits(units.filter(u => u.id == product.unit_id || u.id == product.purchase_unit_id ||
    (Object.keys(group).some(name => name.toLowerCase() === u.name.toLowerCase()) && unitFactor(u.name, stock.name) !== null)), product.unit_id);
}

// Use the persisted rules for presentation too, including household overrides.
function unitFactor(source, target) {
  const rules = window.UNIT_RULES;
  if (!rules) {
    const group = MASS[source] ? MASS : VOLUME;
    return group[source] && group[target] ? group[source] / group[target] : null;
  }
  const direct = rules.find(r => r.source.toLowerCase() === source.toLowerCase() && r.target.toLowerCase() === target.toLowerCase());
  if (direct) return direct.factor;
  const reverse = rules.find(r => r.source.toLowerCase() === target.toLowerCase() && r.target.toLowerCase() === source.toLowerCase());
  if (reverse?.factor > 0) return 1 / reverse.factor;
  const graph = new Map();
  const add = (a, b, f) => { a = a.toLowerCase(); b = b.toLowerCase();
    if (!graph.has(a)) graph.set(a, []); graph.get(a).push([b, f]); };
  for (const r of rules) if (r.factor > 0) { add(r.source, r.target, r.factor); add(r.target, r.source, 1 / r.factor); }
  source = source.toLowerCase(); target = target.toLowerCase();
  const queue = [[source, 1]], seen = new Set([source]);
  for (const [u, f] of queue) {
    if (u === target) return f;
    for (const [v, step] of graph.get(u) || []) if (!seen.has(v)) { seen.add(v); queue.push([v, f * step]); }
  }
  return null;
}

function selectIngredient(i, products) {
  const p = products.find(p => p.id == i.product_id);
  if (p) { i.unit_id = p.unit_id; i.name = ''; }
}

function ingredientBody(i) {
  const body = {product_id: i.product_id ? Number(i.product_id) : null,
    amount: Number(i.amount), unit_id: i.unit_id ? Number(i.unit_id) : null};
  if (!i.product_id) body.name = (i.name || '').trim();
  if (i.note) body.note = i.note;
  return body;
}

function saveRequestId() {
  return window.crypto?.randomUUID?.() || Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
}

// OpenFoodFacts gives a free-text pack size like "500 g", "1 L", "6 x 170g (1.02 kg)".
// Pull out one usable number+unit so a scanned product starts in a real unit instead of
// a generic, un-comparable "piece" - this is what makes it work for any product anywhere,
// not just the handful someone bothers to size by hand.
function parseOffQuantity(text, units) {
  if (!text) return null;
  // prefer a bracketed total if there is one ("6 x 170g (1.02 kg)" -> the 1.02 kg)
  const bracket = text.match(/\(([^)]+)\)/);
  const hay = bracket ? bracket[1] : text;
  const m = hay.match(/([\d]+(?:[.,]\d+)?)\s*(kg|g|l|ml|L|litre|liter)\b/i);
  if (!m) return null;
  const amount = parseFloat(m[1].replace(',', '.'));
  if (!Number.isFinite(amount) || amount <= 0) return null;
  const unitName = { kg: 'kg', g: 'g', l: 'l', ml: 'ml', litre: 'l', liter: 'l' }[m[2].toLowerCase()];
  const unit = units.find(u => u.name.toLowerCase() === unitName);
  if (!unit) return null; // this install doesn't have that unit - leave it to the piece default
  return { unit_id: unit.id, amount };
}

function compactNote(content) {
  const line = content.replace(/\s+/g, ' ').trim();
  return line.length > 140 ? line.slice(0, 140) + '…' : line;
}

function drawHistory(svg, chart) {
  svg.replaceChildren();
  if (!chart) return;
  const add = (tag, attrs, text, parent = svg) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    if (text !== undefined) element.textContent = text;
    parent.appendChild(element);
    return element;
  };
  const {width, height} = svg.viewBox?.baseVal || {width: 560, height: 210};
  const right = width - 16, bottom = height - 36;
  const x = value => 52 + (value - 52) / 492 * (right - 52);
  const y = value => 16 + (value - 16) / 158 * (bottom - 16);
  add('path', {d:`M52 16V${bottom}H${right}`, fill:'none', stroke:'var(--line)'});
  add('text', {x:48, y:24, 'text-anchor':'end'}, chart.high);
  add('text', {x:48, y:bottom, 'text-anchor':'end'}, chart.low);
  add('text', {x:52, y:height - 10}, chart.start);
  add('text', {x:right, y:height - 10, 'text-anchor':'end'}, chart.end);
  const dot = (p, colour, purchase = false) => {
    const circle = add('circle', {cx:x(p.x), cy:y(p.y), r:purchase || p.fake_special ? 4 : 2,
      fill:p.fake_special ? 'var(--card)' : colour,
      stroke:purchase ? 'var(--card)' : colour, tabindex:0});
    add('title', {}, p.label, circle);
  };
  for (const series of chart.stores) {
    add('polyline', {points:series.points.map(p => x(p.x) + ',' + y(p.y)).join(' '),
      fill:'none', stroke:series.colour, 'stroke-width':2});
    series.points.forEach(p => dot(p, series.colour));
  }
  chart.purchases.forEach(p => dot(p, 'var(--text)', true));
}
function buildHistoryChart(history) {
  const timestamp = p => {
    const value = (p.checked_at || p.at || '').replace(' ', 'T');
    return Date.parse(value.length === 19 ? value + 'Z' : value);
  };
  const valid = p => p.price !== null && Number.isFinite(p.price) && Number.isFinite(timestamp(p));
  const colours = ['var(--accent)', 'var(--ok)', 'var(--muted)', 'var(--warn)'];
  const names = {woolworths:'Woolworths', coles:'Coles', aldi:'Aldi', iga:'IGA'};
  const stores = history.stores.filter(s => s.enabled).map((s, i) => ({
    store:s.store, name:names[s.store] || s.store, colour:colours[i % colours.length], points:s.history.filter(valid)
  })).filter(s => s.points.length);
  const purchases = history.purchases.filter(valid);
  const all = [...stores.flatMap(s => s.points), ...purchases];
  if (!all.length) return null;
  const times = all.map(timestamp);
  const prices = all.map(p => p.price);
  const start = Math.min(...times), end = Math.max(...times);
  const low = Math.min(...prices), high = Math.max(...prices);
  const point = (p, name) => ({...p,
    x:end === start ? 298 : 52 + (timestamp(p) - start) / (end - start) * 492,
    y:high === low ? 95 : 174 - (p.price - low) / (high - low) * 158,
    label:name + ' · ' + (p.checked_at || p.at).slice(0, 10) + ' · $' + p.price.toFixed(2)
      + (p.fake_special ? ' · not really cheaper than usual' : '')
  });
  return {stores:stores.map(s => ({...s, points:s.points.map(p => point(p, s.name))})),
    purchases:purchases.map(p => point(p, 'Your purchase (amount paid)')),
    low:'$' + low.toFixed(2), high:'$' + high.toFixed(2),
    start:new Date(start).toISOString().slice(0, 10), end:new Date(end).toISOString().slice(0, 10)};
}

function storePricing(page) {
  return Object.defineProperties(page, Object.getOwnPropertyDescriptors({
    historyItem: null, historyTrigger: null,
    openHistory(i, trigger) {
      this.historyTrigger = trigger; this.historyItem = i;
      this.$nextTick(() => this.$refs.historyClose.focus());
    },
    closeHistory() { this.historyItem = null; this.historyTrigger?.focus(); },
    checkedText(at) {
      if (!at) return 'Not checked yet';
      const days = daysUntil(at.slice(0, 10));
      return 'Checked ' + (days === 0 ? 'today' : days === -1 ? 'yesterday' : days < 0 ? -days + ' days ago' : formatDate(at.slice(0, 10)));
    },
    storeName(store) { return {woolworths: 'Woolworths', coles: 'Coles', aldi: 'Aldi', iga: 'IGA'}[store] || store; },
    get enabledStores() { return this.stores.filter(s => s.enabled); },
    priceLinks(i) { return (i.links || []).filter(link => this.enabledStores.some(s => s.store === link.store)); },
    priceText(price) { return price == null ? 'price not checked yet' : '$' + Number(price).toFixed(2); },
    money(n) { return '$' + (Number(n) || 0).toFixed(2); },
    cheapest(i, link) {
      const prices = this.priceLinks(i).map(l => l.latest_check?.price).filter(p => p != null).map(Number);
      return link.latest_check?.price != null && Number(link.latest_check.price) === Math.min(...prices);
    },
    async loadPrices(items) {
      const lookups = items.filter(i => i.product_id).map(i => this.loadPrice(i));
      try { this.stores = await api('/api/pricing/stores'); }
      catch (e) { items.filter(i => i.product_id).forEach(i => { i.pricingError = 'Store settings unavailable.'; }); }
      await Promise.all(lookups);
    },
    async loadPrice(i) {
      i.pricingError = '';
      try { i.links = await api('/api/pricing/products/' + i.product_id); i.pricingLoaded = true; }
      catch (e) { i.pricingError = 'Prices unavailable.'; }
      try { i.historyChart = buildHistoryChart(await api('/api/pricing/products/' + i.product_id + '/history')); }
      catch (e) { i.historyChart = null; i.pricingError = i.pricingError || 'History unavailable.'; }
    },
    storeLink(i, store) { return this.priceLinks(i).find(link => link.store === store); },
    badgePrice(i, store) {
      const link = this.storeLink(i, store);
      if (!link) return '—';
      const size = link.size || (link.store_name || '').match(/\b\d+(?:\.\d+)?\s*(?:kg|ml|mg|g|l|pack|pk)\b/i)?.[0];
      return this.priceText(link.latest_check?.price) + (size ? ' · ' + size : '');
    },
    openPicker(i, store) {
      if (i.picker && i.picker.store === store) { i.picker = null; return; }
      i.picker = { store, mode: this.storeLink(i, store) ? 'info' : 'search', query: i.label, candidates: [], busy: false, message: '' };
    },
    async searchMatches(i) {
      if (!i.picker) return;
      i.picker.busy = true; i.picker.message = ''; i.picker.candidates = [];
      try {
        i.picker.candidates = await api('/api/pricing/search?store=' + i.picker.store + '&q=' + encodeURIComponent(i.picker.query));
        if (!i.picker.candidates.length) i.picker.message = 'No matches found.';
      } catch (e) { i.picker.message = 'Search failed.'; }
      i.picker.busy = false;
    },
    matchBody(store, candidate) {
      return {
        store, store_item_id: String(candidate.store_item_id), store_name: candidate.name,
        size: candidate.size || '', price: candidate.price, was_price: candidate.was_price,
        unit_price: candidate.unit_price, unit_measure: candidate.unit_measure, on_special: candidate.on_special,
      };
    },
    async confirmMatch(i, store) {
      const link = this.storeLink(i, store);
      if (!link) return;
      await this.pickMatch(i, store, {...link.latest_check, store_item_id: link.store_item_id,
        name: link.store_name, size: link.size});
    },
    async pickMatch(i, store, candidate) {
      if (!i.picker) return;
      i.picker.busy = true;
      try {
        await api('/api/pricing/products/' + i.product_id + '/link', { method: 'POST', body: this.matchBody(store, candidate)});
        i.picker = null;
        await this.loadPrice(i);
      } catch (e) { oops(e); i.picker.busy = false; }
    },
    async unlink(i, store) {
      try {
        await api('/api/pricing/products/' + i.product_id + '/link/' + store, { method: 'DELETE' });
        i.picker = null;
        await this.loadPrice(i);
      } catch (e) { oops(e); }
    },
  }));
}
