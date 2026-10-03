/* musicdl — new window. Plain JS, no dependencies.
 *
 * The Python side sends a snapshot and then small patches (see session.py).
 * Everything that moves on screen is done here: the track list is virtual
 * (only the visible rows exist in the DOM), spinners are CSS, and the clock
 * runs locally, so a list of thousands stays smooth. */
'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const ROW_H = 54;
const BUFFER = 4;

/* ------------------------------------------------------------------ token + api */
const token = (() => {
  let value = new URLSearchParams(location.search).get('t') || '';
  try {
    if (value) sessionStorage.setItem('musicdl-token', value);
    else value = sessionStorage.getItem('musicdl-token') || '';
  } catch (_) { /* storage unavailable: the token only lives in this page then */ }
  history.replaceState(null, '', location.pathname);
  return value;
})();

async function api(name, body) {
  const response = await fetch(`/api/${name}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Musicdl-Token': token },
    body: JSON.stringify(body || {}),
  });
  return response.json();
}

/* ------------------------------------------------------------------------ state */
const S = {
  rows: [],
  meta: null,
  config: null,
  form: { mode: 'csv', csv_path: '', link: '', out_dir: '', bitrate: '320k', threads: 4, only_verified: false },
  filter: 'all',
  query: '',
  cats: { all: 0, ok: 0, bad: 0, wait: 0 },
  view: [],
  viewDirty: true,
  follow: true,
  frontier: 0,
  phase: '',
  runId: -1,
  skew: 0,
  setupOpen: true,
  setupTouched: false,
  noticeDismissed: -1,
  clientBusy: false,
};

const STATES = {
  queued:      { cat: 'wait',   cls: 'st-muted',  icon: 'dot',      text: 'В очереди' },
  searching:   { cat: 'active', cls: 'st-active', icon: 'spin',     text: r => `Ищу · ${r.x}` },
  found:       { cat: 'ok',     cls: 'st-ok',     icon: 'i-check',  text: 'Найдено' },
  ready:       { cat: 'ok',     cls: 'st-info',   icon: 'i-clock',  text: 'Ждёт загрузки' },
  missing:     { cat: 'bad',    cls: 'st-bad',    icon: 'i-x',      text: 'Не найдено' },
  searcherr:   { cat: 'bad',    cls: 'st-bad',    icon: 'i-alert',  text: 'Ошибка поиска' },
  cancelled:   { cat: 'wait',   cls: 'st-muted',  icon: 'i-pause',  text: 'Остановлено' },
  preparing:   { cat: 'active', cls: 'st-active', icon: 'spin',     text: 'Подготовка…' },
  downloading: { cat: 'active', cls: 'st-active', icon: 'spin',     text: r => (r.p > 0 && r.p < 100 ? `Загрузка ${r.p}%` : 'Загрузка…') },
  converting:  { cat: 'active', cls: 'st-active', icon: 'spin',     text: 'Конвертация в mp3…' },
  tagging:     { cat: 'active', cls: 'st-active', icon: 'spin',     text: 'Запись тегов…' },
  retrying:    { cat: 'active', cls: 'st-active', icon: 'i-retry',  text: 'Повторная загрузка…' },
  done:        { cat: 'ok',     cls: 'st-ok',     icon: 'i-check',  text: 'Скачано' },
  exists:      { cat: 'ok',     cls: 'st-muted',  icon: 'i-check',  text: 'Уже есть' },
  dlerror:     { cat: 'bad',    cls: 'st-bad',    icon: 'i-x',      text: 'Ошибка загрузки' },
};
// "active" rows count as waiting in the filter chips (they are not finished yet)
const catOf = state => { const c = (STATES[state] || STATES.queued).cat; return c === 'active' ? 'wait' : c; };
const DOWNLOAD_DONE = new Set(['done', 'exists', 'missing', 'searcherr', 'cancelled', 'dlerror']);

/* ----------------------------------------------------------------------- helpers */
function fmtTime(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}
const baseName = path => path.split(/[\\/]/).filter(Boolean).pop() || path;
const plural = (n, one, few, many) => {
  const m = n % 100, d = n % 10;
  return m > 10 && m < 20 ? many : d === 1 ? one : d >= 2 && d <= 4 ? few : many;
};
function debounce(fn, ms) {
  let timer = 0;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}
function icon(id) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'ic');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#${id}`);
  svg.appendChild(use);
  return svg;
}
function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

function toast(level, text) {
  const box = document.createElement('div');
  box.className = `toast ${level}`;
  box.append(icon(level === 'ok' ? 'i-check' : level === 'info' ? 'i-info' : 'i-alert'));
  const span = document.createElement('span');
  span.textContent = text;
  box.append(span);
  $('#toasts').append(box);
  const life = level === 'bad' ? 9000 : 5000;
  setTimeout(() => { box.classList.add('out'); setTimeout(() => box.remove(), 250); }, life);
  while ($('#toasts').children.length > 4) $('#toasts').firstChild.remove();
}

/* ------------------------------------------------------------------ rows / list */
function prepareRow(raw) {
  raw.ver = 0;
  raw.cat = catOf(raw.s);
  raw.q = `${raw.a} ${raw.n} ${raw.f} ${raw.fa}`.toLowerCase();
  return raw;
}

function recount() {
  S.cats = { all: S.rows.length, ok: 0, bad: 0, wait: 0 };
  for (const row of S.rows) S.cats[row.cat]++;
}

function replaceAll(message) {
  S.rows = message.rows.map(prepareRow);
  S.frontier = 0;
  recount();
  S.viewDirty = true;
  applyMeta(message.meta);
  S.follow = true;
  scheduleRender();
}

function applyPatch(message) {
  for (const [index, next] of message.rows) {
    const row = S.rows[index];
    if (!row) continue;
    const oldCat = row.cat;
    const oldF = row.f;
    Object.assign(row, next);
    row.cat = catOf(row.s);
    if (row.f !== oldF) row.q = `${row.a} ${row.n} ${row.f} ${row.fa}`.toLowerCase();
    row.ver++;
    S.cats[oldCat]--;
    S.cats[row.cat]++;
    if ((S.filter !== 'all' && oldCat !== row.cat) || (S.query && row.f !== oldF)) S.viewDirty = true;
  }
  applyMeta(message.meta);
  scheduleRender();
}

function rebuildView() {
  const query = S.query.trim().toLowerCase();
  const view = [];
  for (let i = 0; i < S.rows.length; i++) {
    const row = S.rows[i];
    if (S.filter !== 'all' && row.cat !== S.filter) continue;
    if (query && !row.q.includes(query)) continue;
    view.push(i);
  }
  S.view = view;
  S.viewDirty = false;
  $('#spacer').style.height = `${view.length * ROW_H}px`;
}

const list = { slots: [], el: $('#tbody') };

function ensureSlots() {
  const need = Math.ceil(list.el.clientHeight / ROW_H) + BUFFER * 2 + 2;
  while (list.slots.length < need) {
    const el = document.createElement('div');
    el.className = 'row';
    el.setAttribute('role', 'row');
    el.innerHTML =
      '<div class="c-n"></div>' +
      '<div class="c-track"><div class="t1"></div><div class="t2"></div></div>' +
      '<div class="c-found"><div class="t1"><span></span></div><div class="t2"></div></div>' +
      '<div class="c-src"><span class="badge"></span></div>' +
      '<div class="c-dur"></div>' +
      '<div class="c-status"><span class="st"></span><div class="minibar"><i></i></div></div>';
    el.querySelector('.c-found .t1').append(icon('i-external'));
    el.querySelector('.c-found .t1 .ic').classList.add('ext');
    list.el.append(el);
    list.slots.push({
      el, ri: -1, vi: -1, ver: -1, hidden: false,
      n: el.querySelector('.c-n'),
      t1: el.querySelector('.c-track .t1'), t2: el.querySelector('.c-track .t2'),
      f1: el.querySelector('.c-found .t1 span'), f2: el.querySelector('.c-found .t2'),
      src: el.querySelector('.badge'), dur: el.querySelector('.c-dur'),
      st: el.querySelector('.st'), bar: el.querySelector('.minibar'), fill: el.querySelector('.minibar i'),
      stKey: '',
    });
  }
}

function bindSlot(slot, ri, vi) {
  const row = S.rows[ri];
  if (slot.vi !== vi) {
    slot.el.style.transform = `translateY(${vi * ROW_H}px)`;
    slot.vi = vi;
  }
  if (slot.hidden) { slot.el.hidden = false; slot.hidden = false; }
  if (slot.ri === ri && slot.ver === row.ver) return;
  slot.ri = ri;
  slot.ver = row.ver;

  setText(slot.n, String(ri + 1));
  setText(slot.t1, row.n);
  setText(slot.t2, row.a);
  slot.el.classList.remove('skeleton');
  setText(slot.f1, row.f);
  setText(slot.f2, row.fa);
  slot.t1.title = `${row.a} — ${row.n}`;
  slot.f1.title = row.f;

  const sourceText = row.src ? row.src.replace('YouTube Music, видео', 'YT Music · видео') : '';
  setText(slot.src, sourceText);
  slot.src.hidden = !sourceText;
  slot.src.className = 'badge' + (row.src.startsWith('YouTube Music') ? (row.v ? ' ytm' : ' ytm') : row.src === 'YouTube' ? ' yt' : row.src === 'SoundCloud' ? ' sc' : '');
  slot.src.title = row.src === 'YouTube Music' ? 'Официальный трек YouTube Music' : row.src;
  setText(slot.dur, row.d === '?' ? '' : row.d);

  const state = STATES[row.s] || STATES.queued;
  const label = typeof state.text === 'function' ? state.text(row) : state.text;
  const key = `${row.s}|${state.icon}`;
  if (slot.stKey !== key) {
    slot.stKey = key;
    slot.st.className = `st ${state.cls}`;
    slot.st.textContent = '';
    if (state.icon === 'spin') {
      const spin = document.createElement('span');
      spin.className = 'spin';
      slot.st.append(spin);
    } else if (state.icon === 'dot') {
      const dot = document.createElement('span');
      dot.className = 'dotmark';
      slot.st.append(dot);
    } else {
      slot.st.append(icon(state.icon));
    }
    const text = document.createElement('span');
    text.className = 'lbl';
    slot.st.append(text);
  }
  setText(slot.st.lastChild, label);
  slot.st.title = row.s === 'searcherr' && row.x ? row.x : label;

  const showBar = row.s === 'downloading' && row.p > 0 && row.p < 100;
  slot.bar.style.visibility = showBar ? 'visible' : 'hidden';
  slot.fill.style.width = `${row.p}%`;

  slot.el.classList.toggle('link', !!row.l);
  slot.el.classList.toggle('r-active', state.cat === 'active');
  slot.el.classList.toggle('r-bad', state.cat === 'bad');
  slot.el.classList.toggle('r-muted', row.s === 'exists');
  slot.el.dataset.row = String(ri);
}

function bindSkeleton(slot, vi) {
  const el = slot.el;
  if (slot.ri !== -2) {
    slot.ri = -2;
    slot.ver = -1;
    slot.stKey = '';
    slot.n.textContent = '';
    slot.t1.textContent = 'Загружаю список треков…';
    slot.t2.textContent = '··········';
    slot.f1.textContent = '';
    slot.f2.textContent = '';
    slot.src.hidden = true;
    slot.dur.textContent = '';
    slot.st.textContent = '';
    slot.bar.style.visibility = 'hidden';
    el.className = 'row skeleton';
  }
  slot.el.style.transform = `translateY(${vi * ROW_H}px)`;
  slot.vi = vi;
  if (slot.hidden) { el.hidden = false; slot.hidden = false; }
}

function renderList() {
  if (S.viewDirty) rebuildView();
  ensureSlots();
  const loading = !!(S.meta && S.meta.running && S.meta.phase === 'load' && !S.rows.length);
  const empty = $('#empty');
  const showEmpty = !S.view.length && !loading;
  empty.hidden = !showEmpty;
  if (showEmpty) {
    const filtered = S.rows.length > 0;
    setText($('#emptyTitle'), filtered ? 'Ничего не найдено' : 'Здесь появятся треки');
    setText($('#emptyText'), filtered
      ? 'Измените фильтр или поисковый запрос.'
      : 'Выберите CSV-файл или вставьте ссылку Spotify. «Проверить» покажет, что нашлось, а «Скачать» сохранит mp3 в вашу папку.');
  }
  if (loading) {
    list.slots.forEach((slot, k) => { if (k < 7) bindSkeleton(slot, k); else if (!slot.hidden) { slot.el.hidden = true; slot.hidden = true; } });
    $('#spacer').style.height = '0px';
    return;
  }
  const first = Math.max(0, Math.floor(list.el.scrollTop / ROW_H) - BUFFER);
  list.slots.forEach((slot, k) => {
    const vi = first + k;
    if (vi >= S.view.length) {
      if (!slot.hidden) { slot.el.hidden = true; slot.hidden = true; }
      return;
    }
    if (slot.ri === -2) { slot.el.className = 'row'; slot.ri = -1; slot.ver = -1; slot.stKey = ''; }
    bindSlot(slot, S.view[vi], vi);
  });
}

/* ---------------------------------------------------------------- follow the run */
function advanceFrontier() {
  const meta = S.meta;
  if (!meta) return;
  if (meta.phase !== S.phase) { S.phase = meta.phase; S.frontier = 0; }
  const finished = meta.phase === 'search'
    ? row => row.s !== 'queued' && row.s !== 'searching'
    : row => DOWNLOAD_DONE.has(row.s);
  while (S.frontier < S.rows.length && finished(S.rows[S.frontier])) S.frontier++;
}

let programmaticTop = -1;
function followFrontier() {
  if (!S.follow || !S.meta || !S.meta.running || S.frontier >= S.rows.length) return;
  const vi = S.filter === 'all' && !S.query ? S.frontier : S.view.indexOf(S.frontier);
  if (vi < 0) return;
  const top = vi * ROW_H, height = list.el.clientHeight;
  const current = list.el.scrollTop;
  if (top < current + ROW_H || top > current + height - ROW_H * 3) {
    programmaticTop = Math.max(0, Math.round(top - height * 0.3));
    list.el.scrollTop = programmaticTop;
  }
}

function stopFollowing() {
  if (S.follow) { S.follow = false; updateFollowButton(); }
}
function updateFollowButton() {
  $('#followBtn').hidden = S.follow || !(S.meta && S.meta.running);
}

/* -------------------------------------------------------------------- meta / chrome */
const PHASES = {
  '': ['Готов к работе', ''],
  load: ['Загружаю список', ''],
  search: ['Поиск треков', 'search'],
  download: ['Скачивание', 'download'],
};

function applyMeta(meta) {
  const previous = S.meta;
  S.meta = meta;
  S.skew = meta.now - Date.now() / 1000;
  if (meta.run !== S.runId) {
    S.runId = meta.run;
    S.setupTouched = false;
    S.follow = true;
  }
  advanceFrontier();
  if (!previous || previous.running !== meta.running) S.setupTouched = false;
}

function renderMeta() {
  const meta = S.meta;
  if (!meta) return;
  const running = meta.running;

  document.body.classList.toggle('running', running);
  $('#idleActions').hidden = running;
  $('#busyActions').hidden = !running;
  $('#stopBtn').disabled = !meta.canStop;
  setText($('#busyLabel'), meta.dryRun ? 'Идёт проверка…' : meta.phase === 'download' ? 'Идёт скачивание…' : 'Идёт поиск…');
  $('#offline').hidden = !meta.offline;

  // setup card: open when idle and nothing to show yet, one line while working
  const autoOpen = !running && S.rows.length === 0;
  const open = S.setupTouched ? S.setupOpen : autoOpen;
  S.setupOpen = open;
  $('#setup').dataset.open = String(open);
  $('#setup').inert = !open;
  $('#setupGrid').inert = running;
  $('#setupStrip').hidden = open;
  $('#stripEdit').hidden = running;
  renderStrip();

  // progress
  let [name, kind] = PHASES[meta.phase] || ['', ''];
  let dot = running ? 'on' : '';
  if (meta.phase === 'done') {
    name = meta.error ? 'Ошибка' : meta.summary && meta.summary.cancelled ? 'Остановлено' : meta.dryRun ? 'Проверка завершена' : 'Готово';
    dot = meta.error ? 'bad' : meta.tone === 'warn' ? 'warn' : 'ok';
  }
  setText($('#phaseName'), name);
  $('#phaseDot').className = `dot ${dot}`;
  const total = meta.total, done = Math.min(meta.done, meta.total);
  setText($('#phaseCount'), kind && total ? `${done} из ${total}` : '');
  const pct = total ? Math.min(100, Math.round((100 * done) / total)) : meta.phase === 'done' && !meta.error ? 100 : 0;
  setText($('#phasePct'), total || meta.phase === 'done' ? `${pct}%` : '');
  const bar = $('#bar');
  bar.className = 'bar' + (running && meta.phase === 'load' ? ' indet' : running ? ' live' : meta.phase === 'done' && !meta.error ? ' ok' : '');
  bar.setAttribute('aria-valuenow', String(pct));
  $('#barFill').style.width = `${pct}%`;
  document.title = running ? `musicdl · ${pct}%` : 'musicdl';

  for (const key of ['total', 'existing', 'found', 'missing', 'done']) {
    const el = $(`#c-${key}`);
    const value = meta.counters[key];
    const text = value < 0 ? '…' : meta.phase === '' && !S.rows.length ? '—' : String(value);
    if (el.textContent !== text) {
      el.textContent = text;
      el.classList.remove('bump');
      void el.offsetWidth;
      if (previousHad(el)) el.classList.add('bump');
    }
  }

  const status = $('#status');
  status.dataset.tone = meta.tone;
  setText($('#statusText'), meta.status || $('#statusText').textContent);
  $('#statusText').title = meta.status || '';
  const statusIcon = status.querySelector('use');
  statusIcon.setAttribute('href', meta.tone === 'ok' ? '#i-check' : meta.tone === 'info' ? '#i-info' : '#i-alert');

  $('#openReport').disabled = !meta.report;
  $('#notice').hidden = !(meta.summary && meta.summary.allFailed && S.noticeDismissed !== meta.run);
  updateFollowButton();
  renderTime();
}
const bumped = new WeakSet();
function previousHad(el) {
  if (!bumped.has(el)) { bumped.add(el); return false; }
  return true;
}

function renderTime() {
  const meta = S.meta;
  const box = $('#timing');
  if (!meta || !meta.start) { setText(box, ''); return; }
  const now = Date.now() / 1000 + S.skew;
  if (!meta.running) {
    setText(box, meta.end ? `готово за ${fmtTime(meta.end - meta.start)}` : '');
    return;
  }
  let text = `прошло ${fmtTime(now - meta.start)}`;
  if (meta.total && meta.done > 0 && meta.phaseStart) {
    const perItem = (now - meta.phaseStart) / meta.done;
    text += ` · осталось ≈ ${fmtTime(perItem * (meta.total - meta.done))}`;
  }
  setText(box, text);
}

function renderStrip() {
  const form = S.form;
  const source = form.mode === 'csv' ? baseName(form.csv_path || '') || 'CSV-файл' : (form.link || 'Ссылка Spotify').replace(/^https?:\/\/open\.spotify\.com\//, 'spotify/');
  setText($('#stripSource'), source);
  setText($('#stripDest'), form.out_dir || '');
  const parts = [form.bitrate, `${form.threads} ${plural(form.threads, 'поток', 'потока', 'потоков')}`];
  if (form.only_verified) parts.push('официальные');
  setText($('#stripMeta'), parts.join(' · '));
}

function renderFilters() {
  for (const key of ['all', 'ok', 'bad', 'wait']) setText($(`#f-${key}`), String(S.cats[key]));
  $$('#filters button').forEach(button => button.classList.toggle('on', button.dataset.f === S.filter));
}

/* ------------------------------------------------------------------ render loop */
let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    if (S.viewDirty) rebuildView();
    followFrontier();
    renderMeta();
    renderFilters();
    renderList();
  });
}

list.el.addEventListener('scroll', () => {
  if (programmaticTop >= 0 && Math.abs(list.el.scrollTop - programmaticTop) < 3) programmaticTop = -1;
  scheduleRender();
}, { passive: true });
['wheel', 'touchstart', 'pointerdown'].forEach(type => list.el.addEventListener(type, stopFollowing, { passive: true }));
list.el.addEventListener('keydown', event => {
  if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'].includes(event.key)) stopFollowing();
});
new ResizeObserver(scheduleRender).observe(list.el);

$('#followBtn').addEventListener('click', () => { S.follow = true; scheduleRender(); });
$('#noticeClose').addEventListener('click', () => { S.noticeDismissed = S.meta ? S.meta.run : -1; $('#notice').hidden = true; });

list.el.addEventListener('click', event => {
  const row = event.target.closest('.row.link');
  if (!row) return;
  api('open', { what: 'row', row: Number(row.dataset.row) }).then(result => { if (!result.ok) toast('bad', result.message); });
});

$$('#filters button').forEach(button => button.addEventListener('click', () => {
  S.filter = button.dataset.f;
  S.viewDirty = true;
  list.el.scrollTop = 0;
  scheduleRender();
}));
$('#query').addEventListener('input', debounce(event => {
  S.query = event.target.value;
  S.viewDirty = true;
  list.el.scrollTop = 0;
  scheduleRender();
}, 120));

setInterval(renderTime, 500);

/* -------------------------------------------------------------------------- form */
const saveSettings = debounce(() => {
  const { mode, csv_path, link, out_dir, bitrate, threads, only_verified } = S.form;
  api('settings', { mode, csv_path, link, out_dir, bitrate, threads, only_verified, theme: document.documentElement.dataset.theme });
}, 400);

function syncForm() {
  const form = S.form;
  $$('[data-mode]').forEach(button => button.classList.toggle('on', button.dataset.mode === form.mode));
  $('#paneCsv').hidden = form.mode !== 'csv';
  $('#paneUrl').hidden = form.mode !== 'url';
  const hasFile = !!form.csv_path;
  $('#dropZone').hidden = hasFile;
  $('#fileCard').hidden = !hasFile;
  setText($('#fileName'), baseName(form.csv_path));
  setText($('#filePath'), form.csv_path ? '‎' + form.csv_path : '');
  if (document.activeElement !== $('#csvPath')) $('#csvPath').value = form.csv_path;
  if (document.activeElement !== $('#linkInput')) $('#linkInput').value = form.link;
  if (document.activeElement !== $('#outDir')) $('#outDir').value = form.out_dir;
  $$('#bitrateSeg button').forEach(button => button.classList.toggle('on', button.dataset.b === form.bitrate));
  setText($('#thrValue'), String(form.threads));
  $('#onlyVerified').checked = !!form.only_verified;
  renderStrip();
}

function setForm(changes, save = true) {
  Object.assign(S.form, changes);
  syncForm();
  if (save) saveSettings();
}

function buildBitrates(values) {
  const box = $('#bitrateSeg');
  box.textContent = '';
  for (const value of values) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.b = value;
    button.textContent = value.replace('k', '');
    button.title = `${value}bit/s`.replace('kbit/s', ' кбит/с');
    button.addEventListener('click', () => setForm({ bitrate: value }));
    box.append(button);
  }
}

$$('[data-mode]').forEach(button => button.addEventListener('click', () => setForm({ mode: button.dataset.mode })));
$('#csvPath').addEventListener('input', event => setForm({ csv_path: event.target.value.trim() }));
$('#linkInput').addEventListener('input', event => { setForm({ link: event.target.value.trim() }); checkLink(); });
$('#outDir').addEventListener('input', event => setForm({ out_dir: event.target.value.trim() }));
$('#onlyVerified').addEventListener('change', event => setForm({ only_verified: event.target.checked }));
$('#thrMinus').addEventListener('click', () => setForm({ threads: Math.max(1, S.form.threads - 1) }));
$('#thrPlus').addEventListener('click', () => setForm({ threads: Math.min(8, S.form.threads + 1) }));
$('#manualToggle').addEventListener('click', () => {
  const input = $('#csvPath');
  input.hidden = !input.hidden;
  if (!input.hidden) input.focus();
});

async function pick(kind) {
  const initial = kind === 'csv' ? S.form.csv_path : S.form.out_dir;
  const result = await api('pick', { kind, initial });
  if (!result.ok) {
    toast('warn', result.message);
    if (kind === 'csv') { $('#csvPath').hidden = false; $('#csvPath').focus(); }
    return;
  }
  if (!result.path) return;
  if (kind === 'csv') setForm({ csv_path: result.path, mode: 'csv' });
  else setForm({ out_dir: result.path });
}
$('#dropZone').addEventListener('click', () => pick('csv'));
$('#fileChange').addEventListener('click', () => pick('csv'));
$('#outPick').addEventListener('click', () => pick('folder'));

$('#pasteBtn').addEventListener('click', async () => {
  try {
    const text = (await navigator.clipboard.readText()).trim();
    if (!text) throw new Error('empty');
    setForm({ link: text });
    checkLink();
  } catch (_) {
    $('#linkInput').focus();
    toast('info', 'Нажмите Ctrl+V, чтобы вставить ссылку в поле.');
  }
});

const checkLink = debounce(async () => {
  const hint = $('#linkHint'), input = $('#linkInput');
  const link = S.form.link;
  if (!link) {
    hint.className = 'hint';
    hint.textContent = 'В Spotify: «Поделиться → Копировать ссылку». Подходят трек, альбом и публичный плейлист.';
    input.classList.remove('invalid');
    return;
  }
  const result = await api('validate_link', { link });
  if (S.form.link !== link) return;
  input.classList.toggle('invalid', !result.ok);
  hint.className = `hint ${result.ok ? 'ok' : 'bad'}`;
  hint.textContent = result.ok
    ? { track: 'Трек — готово к загрузке', album: 'Альбом — готово к загрузке', playlist: 'Плейлист — готово к загрузке' }[result.kind] || 'Ссылка подходит'
    : 'Это не похоже на ссылку Spotify на трек, альбом или плейлист.';
}, 250);

/* ----------------------------------------------------------------------- actions */
async function startRun(dryRun) {
  if (S.clientBusy || (S.meta && S.meta.running)) return;
  S.clientBusy = true;
  $('#downloadBtn').disabled = $('#checkBtn').disabled = true;
  try {
    const result = await api('start', { ...S.form, dry_run: dryRun, theme: document.documentElement.dataset.theme });
    if (!result.ok) toast('bad', result.message);
  } catch (_) {
    toast('bad', 'Не удалось связаться с программой.');
  } finally {
    S.clientBusy = false;
    $('#downloadBtn').disabled = $('#checkBtn').disabled = false;
  }
}
$('#downloadBtn').addEventListener('click', () => startRun(false));
$('#checkBtn').addEventListener('click', () => startRun(true));
$('#stopBtn').addEventListener('click', () => { $('#stopBtn').disabled = true; api('stop'); });
$('#stripEdit').addEventListener('click', () => { S.setupTouched = true; S.setupOpen = true; scheduleRender(); });

const openThing = (what, extra) => api('open', { what, ...extra }).then(result => { if (!result.ok) toast('warn', result.message); });
$('#openOut').addEventListener('click', () => openThing('out', { out_dir: S.form.out_dir }));
$('#openReport').addEventListener('click', () => openThing('report'));
$('#openLog').addEventListener('click', () => openThing('log'));

/* theme */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('#themeIcon').firstElementChild.setAttribute('href', theme === 'dark' ? '#i-sun' : '#i-moon');
}
$('#themeBtn').addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  saveSettings();
});

/* ------------------------------------------------------------- drag & drop, keys */
let dragDepth = 0;
const hasFiles = event => event.dataTransfer && Array.from(event.dataTransfer.types || []).some(type => type === 'Files' || type === 'text/plain');
window.addEventListener('dragenter', event => { if (hasFiles(event)) { dragDepth++; $('#dropVeil').hidden = false; } });
window.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('#dropVeil').hidden = true; });
window.addEventListener('dragover', event => { if (hasFiles(event)) event.preventDefault(); });
window.addEventListener('drop', async event => {
  event.preventDefault();
  dragDepth = 0;
  $('#dropVeil').hidden = true;
  if (S.meta && S.meta.running) { toast('info', 'Сейчас идёт работа. Дождитесь окончания или нажмите «Стоп».'); return; }
  const file = event.dataTransfer.files && event.dataTransfer.files[0];
  if (file) {
    if (!/\.csv$/i.test(file.name)) { toast('warn', 'Нужен CSV-файл из TuneMyMusic.'); return; }
    const response = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
      method: 'POST', headers: { 'X-Musicdl-Token': token }, body: file,
    });
    const result = await response.json();
    if (result.ok) { setForm({ mode: 'csv', csv_path: result.path }); S.setupTouched = true; S.setupOpen = true; scheduleRender(); }
    else toast('bad', result.message || 'Не удалось прочитать файл.');
    return;
  }
  const text = (event.dataTransfer.getData('text/plain') || '').trim();
  if (/open\.spotify\.com\//.test(text)) { setForm({ mode: 'url', link: text }); checkLink(); S.setupTouched = true; S.setupOpen = true; scheduleRender(); }
});

window.addEventListener('keydown', event => {
  const ctrl = event.ctrlKey || event.metaKey;
  if (ctrl && event.key.toLowerCase() === 'o') { event.preventDefault(); if (!(S.meta && S.meta.running)) pick('csv'); }
  else if (ctrl && event.key === 'Enter') { event.preventDefault(); startRun(false); }
  else if (ctrl && event.key.toLowerCase() === 'f') { event.preventDefault(); $('#query').focus(); $('#query').select(); }
  else if (event.key === 'Escape' && document.activeElement === $('#query')) { $('#query').value = ''; $('#query').dispatchEvent(new Event('input')); $('#query').blur(); }
});
window.addEventListener('beforeunload', event => {
  if (S.meta && S.meta.running) { event.preventDefault(); event.returnValue = ''; }
});

/* --------------------------------------------------------------- live connection */
let lostTimer = 0;
function connect() {
  const source = new EventSource(`/api/events?t=${encodeURIComponent(token)}`);
  source.onopen = () => { clearTimeout(lostTimer); $('#lost').hidden = true; };
  source.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.t === 'snapshot') replaceAll(message);
    else if (message.t === 'patch') applyPatch(message);
    else if (message.t === 'toast') toast(message.level, message.text);
  };
  source.onerror = () => {
    clearTimeout(lostTimer);
    lostTimer = setTimeout(() => { $('#lost').hidden = false; }, 4000);
  };
}

async function boot() {
  let theme = '';
  try {
    const response = await fetch(`/api/config?t=${encodeURIComponent(token)}`, { headers: { 'X-Musicdl-Token': token } });
    if (!response.ok) throw new Error(response.status);
    S.config = await response.json();
    Object.assign(S.form, S.config.form);
    theme = S.config.form.theme;
    setText($('#version'), S.config.version ? `· ${S.config.version}` : '');
    $('#demoTag').hidden = S.config.engine !== 'demo';
    buildBitrates(S.config.bitrates);
  } catch (_) {
    $('#lost').hidden = false;
    return;
  }
  applyTheme(theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
  syncForm();
  checkLink();
  connect();
}
boot();
