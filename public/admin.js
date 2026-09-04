/* =====================================================================
   FructCity — панель управления (ТЗ 10).

   Права проверяет сервер. Здесь мы лишь не показываем недоступные
   разделы, чтобы не путать сотрудника: скрытие пункта меню само по себе
   защитой не является.
   ===================================================================== */
'use strict';

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = s => (s === null || s === undefined) ? '' : String(s).replace(/[&<>"']/g, c => ESC[c]);
const RAW = Symbol('raw');

/* Готовая разметка. Помечена символом RAW, поэтому повторно не
   экранируется. toString() позволяет присваивать её в innerHTML,
   склеивать через + и собирать через .join('') как обычную строку. */
function markup(html) {
  return { [RAW]: String(html), toString() { return this[RAW]; } };
}
const raw = markup;

/** Разворачивает значение подстановки в безопасный HTML. */
function part(v) {
  if (v === null || v === undefined || v === false) return '';
  if (v[RAW] !== undefined) return v[RAW];          // уже разметка
  if (Array.isArray(v)) return v.map(part).join(''); // список — поэлементно
  return esc(v);                                     // данные — экранируем
}

/**
 * Безопасный шаблон разметки.
 *
 * Возвращает не строку, а объект-разметку. Это принципиально: массив
 * результатов h`` — например items.map(x => h`<tr>…`) — вставляется
 * как разметка, а не экранируется поэлементно. Раньше h возвращала
 * строку, и такие массивы выводились на экран текстом вида «<tr>…».
 */
function h(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i++) out += part(vals[i]) + strings[i + 1];
  return markup(out);
}
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const rub = n => (Math.round(Number(n) || 0)).toLocaleString('ru-RU') + ' ₽';

/* ---------------------------------------------------------------------
   Транспорт
   --------------------------------------------------------------------- */
function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)fc_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: Object.assign(opts.body ? { 'Content-Type': 'application/json' } : {},
      { 'X-CSRF-Token': csrf() }),
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: 'same-origin'
  });
  const ctype = res.headers.get('content-type') || '';
  const data = ctype.includes('json') ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) {
    const e = new Error(data && data.error || 'request_failed');
    e.status = res.status; e.data = data;
    throw e;
  }
  return data;
}

const ERRORS = {
  sku_exists: 'Такой артикул уже есть',
  code_exists: 'Такой промокод уже есть',
  login_exists: 'Такой логин уже занят',
  category_exists: 'Такая категория уже есть',
  category_not_empty: 'В категории есть товары — сначала перенесите их',
  category_is_system: 'Системную категорию удалить нельзя',
  last_admin: 'Нельзя убрать последнего администратора',
  cannot_demote_self: 'Нельзя понизить самого себя',
  cannot_delete_self: 'Нельзя удалить собственную учётную запись',
  weak_password: 'Пароль слишком простой',
  status_cannot_go_back: 'Статус нельзя вернуть назад',
  status_skip_not_allowed: 'Нельзя перескакивать через статус',
  order_cancelled: 'Заказ отменён — изменения недоступны',
  zone_manual_quote: 'Для этой зоны стоимость считается вручную',
  forbidden: 'Недостаточно прав',
  unauthorized: 'Сессия истекла — войдите заново',
  validation_failed: 'Проверьте заполнение полей',
  unknown_sku: 'В подборке есть несуществующие артикулы'
};
function errText(e) {
  if (e && e.data && e.data.fields) return Object.values(e.data.fields)[0];
  return ERRORS[e && e.message] || 'Что-то пошло не так';
}

let toastTimer;
function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, 2800);
}

/* ---------------------------------------------------------------------
   Модалка
   --------------------------------------------------------------------- */
let lastFocus = null;
const isModalOpen = () => $('#ov').classList.contains('show');

function modal(html) {
  // запоминаем точку возврата только при ПЕРВОМ открытии: карточка заказа
  // перерисовывает себя через modal(), и иначе lastFocus указал бы на
  // элемент внутри самой модалки, который уже удалён
  if (!isModalOpen()) lastFocus = document.activeElement;
  $('#sheet').innerHTML = html;
  $('#ov').classList.add('show');
  document.body.style.overflow = 'hidden';
  const f = $('#sheet').querySelector('input,select,textarea,button');
  if (f) f.focus();
}
function closeModal() {
  $('#ov').classList.remove('show');
  document.body.style.overflow = '';
  $('#sheet').innerHTML = '';
  if (lastFocus && lastFocus.focus && document.contains(lastFocus)) lastFocus.focus();
  lastFocus = null;
}
$('#ov').onclick = e => { if (e.target.id === 'ov') closeModal(); };

/* Кнопки «Отмена» и «✕» внутри модалок. Инлайновый onclick здесь тоже
   запрещён политикой безопасности, поэтому один делегированный
   обработчик на документе. */
document.addEventListener('click', e => {
  const btn = e.target.closest && e.target.closest('[data-close]');
  if (btn) { e.preventDefault(); closeModal(); }
});

const FOCUSABLE = 'input:not([disabled]),select:not([disabled]),textarea:not([disabled]),' +
  'button:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])';

document.addEventListener('keydown', e => {
  if (!isModalOpen()) return;
  if (e.key === 'Escape') return closeModal();
  // ловушка фокуса: без неё Tab уводит на элементы под оверлеем (ТЗ 15.4)
  if (e.key !== 'Tab') return;
  const f = Array.from($('#sheet').querySelectorAll(FOCUSABLE));
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

/* ---------------------------------------------------------------------
   Фото
   --------------------------------------------------------------------- */
let PHOTOS = {};
function photo(p, w) {
  const file = PHOTOS[p.image_key];
  const emoji = p.emoji || '📦';
  if (!file) return h`<span class="e" aria-hidden="true">${emoji}</span>`;
  const url = 'https://commons.wikimedia.org/wiki/Special:FilePath/' +
    encodeURIComponent(file) + (w ? '?width=' + w : '');
  return h`<img src="${url}" alt="" loading="lazy" data-emoji="${emoji}">`;
}

/* Подмена битой картинки на эмодзи.

   Атрибут onerror прямо в разметке не работает: CSP запрещает
   инлайновые обработчики (script-src 'self'). Событие error не
   всплывает, но проходит фазу перехвата — одного слушателя на
   документе хватает на все картинки, включая отрисованные позже. */
function emojiFallback(img) {
  const emoji = img && img.dataset && img.dataset.emoji;
  if (!emoji) return;
  const span = document.createElement('span');
  span.className = 'e';
  span.setAttribute('aria-hidden', 'true');
  span.textContent = emoji;
  img.replaceWith(span);
}

document.addEventListener('error', e => {
  if (e.target && e.target.tagName === 'IMG') emojiFallback(e.target);
}, true);

/* Если картинка взялась из кеша уже сломанной, события error не будет —
   такие ловим обходом после перерисовки. */
function sweepPhotos() {
  document.querySelectorAll('img[data-emoji]').forEach(img => {
    if (img.complete && img.naturalWidth === 0) emojiFallback(img);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  if (typeof MutationObserver !== 'function') return;
  const obs = new MutationObserver(() => sweepPhotos());
  ['#content', '#sheet'].forEach(sel => {
    const el = document.querySelector(sel);
    if (el) obs.observe(el, { childList: true, subtree: true });
  });
});

/* =====================================================================
   Навигация и права
   ===================================================================== */
const NAV = [
  { id: 'dash', name: 'Дашборд', perm: 'dashboard', icon: '<rect x="3" y="12" width="4" height="8"/><rect x="10" y="7" width="4" height="13"/><rect x="17" y="3" width="4" height="17"/>' },
  { id: 'home', name: 'Главная страница', perm: 'home', icon: '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>' },
  { id: 'products', name: 'Товары', perm: 'products', icon: '<path d="M21 8l-9-5-9 5 9 5 9-5Z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>' },
  { id: 'categories', name: 'Категории', perm: 'categories', icon: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>' },
  { id: 'orders', name: 'Заказы', perm: 'orders', icon: '<path d="M6 2h9l3 3v17H6z"/><path d="M9 8h6M9 12h6M9 16h4"/>' },
  { id: 'preorders', name: 'Предзаказы мяса', perm: 'preorders', icon: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>' },
  { id: 'promos', name: 'Промокоды', perm: 'promos', icon: '<path d="M20 10 12 2 4 6v8l8 8 8-8v-4Z"/><circle cx="8.5" cy="8.5" r="1.2"/>' },
  { id: 'delivery', name: 'Доставка', perm: 'delivery', icon: '<rect x="1" y="7" width="13" height="9"/><path d="M14 10h4l3 3v3h-7z"/><circle cx="6" cy="18" r="1.6"/><circle cx="17.5" cy="18" r="1.6"/>' },
  { id: 'staff', name: 'Сотрудники', perm: 'staff', icon: '<circle cx="9" cy="8" r="3.2"/><path d="M2 20c0-4 3-6.5 7-6.5S16 16 16 20"/><circle cx="18" cy="9" r="2.4"/><path d="M22 20c0-3-2-5-4.5-5.3"/>' }
];
const icon = it => h`<svg class="i" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${raw(it.icon)}</svg>`;

const S = { user: null, perms: new Set(), page: 'dash', data: {} };
const can = p => S.perms.has(p);

function renderNav() {
  $('#nav').innerHTML = NAV.filter(n => can(n.perm)).map(n =>
    h`<a class="${S.page === n.id ? 'on' : ''}" data-page="${n.id}" role="button" tabindex="0">
      ${raw(icon(n))}<span>${n.name}</span></a>`).join('');
  $$('#nav a').forEach(a => {
    a.onclick = () => go(a.dataset.page);
    a.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(a.dataset.page); } };
  });
}

function go(id) {
  const item = NAV.find(n => n.id === id);
  if (!item || !can(item.perm)) return;
  S.page = id;
  if (location.hash.slice(1) !== id) location.hash = '#' + id;
  $('#ptitle').textContent = item.name;
  renderNav();
  const fn = { dash: pageDash, home: pageHome, products: pageProducts, categories: pageCategories,
    orders: pageOrders, preorders: pagePreorders, promos: pagePromos, delivery: pageDelivery, staff: pageStaff }[id];
  $('#content').innerHTML = '<div class="panel skel" style="height:200px"></div>';
  fn().catch(e => {
    if (e.status === 401) return showLogin();
    $('#content').innerHTML = h`<div class="panel"><h3>Не удалось загрузить</h3>
      <p class="muted">${errText(e)}</p></div>`;
  });
}

/* =====================================================================
   Графики.

   Рисуем сами, в SVG. Готовая библиотека сюда не годится: политика
   безопасности отдаётся со `script-src 'self'`, то есть загрузить
   Chart.js с CDN браузер не даст, а класть трёхсоткилобайтную
   зависимость в репозиторий ради четырёх картинок несоразмерно.

   Три правила для всех графиков ниже:
     1. Размеры в единицах viewBox, масштаб — на CSS. Так график
        одинаково выглядит на любой ширине панели и не требует
        пересчёта при изменении размера окна.
     2. Числа приходят из API и считаются недоверенными: любая
        подстановка идёт через h``, включая координаты.
     3. График — это `role="img"` с осмысленным aria-label, а рядом
        обязательно остаётся таблица с теми же числами. Диаграмма,
        которую нельзя прочитать голосом, — не данные, а украшение.
   ===================================================================== */
const CH = {
  ink: '#2C2C2A', grid: '#E7E5DF', muted: '#8A877E',
  green: '#3D7C36', greenSoft: 'rgba(61,124,54,.14)', gold: '#C9A24A',
  /* палитра для долей: различима и в оттенках серого — проверял
     переводом в яркость, соседние значения расходятся минимум на 12% */
  slices: ['#3D7C36', '#C9A24A', '#6E9E4A', '#A8763C', '#4F6F8F', '#8C5A78', '#B94A48']
};

/** Округление до «красивого» верха шкалы: 1–2–5 × 10ⁿ. */
function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}

const shortRub = v => v >= 1e6 ? (v / 1e6).toFixed(1) + ' млн'
  : v >= 1e3 ? Math.round(v / 1e3) + ' тыс' : String(Math.round(v));

/**
 * Линия с заливкой: выручка по дням.
 * `points` — [{ymd, sum, count}], ряд без пропусков (см. dailyRevenue).
 */
function chartLine(points, label) {
  const W = 720, H = 200, L = 52, R = 8, T = 12, B = 26;
  const iw = W - L - R, ih = H - T - B;
  if (points.length < 2) return h`<p class="muted">Слишком мало данных для графика.</p>`;

  const max = niceMax(Math.max(...points.map(p => p.sum)));
  const x = i => L + iw * i / (points.length - 1);
  const y = v => T + ih - ih * (v / max);

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.sum).toFixed(1)}`).join(' ');
  const area = `${line} L${x(points.length - 1).toFixed(1)} ${(T + ih).toFixed(1)} L${x(0).toFixed(1)} ${(T + ih).toFixed(1)} Z`;

  const ticks = [0, .25, .5, .75, 1].map(f => max * f);
  /* подписи дат: только начало, середина и конец — иначе 30 подписей
     сливаются в серую кашу */
  const marks = [0, Math.floor((points.length - 1) / 2), points.length - 1];

  return h`<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}">
    ${ticks.map(t => h`<g>
      <line x1="${L}" y1="${y(t).toFixed(1)}" x2="${W - R}" y2="${y(t).toFixed(1)}"
        stroke="${CH.grid}" stroke-width="1"/>
      <text x="${L - 8}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end"
        font-size="11" fill="${CH.muted}">${shortRub(t)}</text>
    </g>`)}
    <path d="${area}" fill="${CH.greenSoft}"/>
    <path d="${line}" fill="none" stroke="${CH.green}" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>
    ${points.map((p, i) => p.count
      ? h`<circle cx="${x(i).toFixed(1)}" cy="${y(p.sum).toFixed(1)}" r="2.5" fill="${CH.green}"/>`
      : '')}
    ${marks.map(i => h`<text x="${x(i).toFixed(1)}" y="${H - 8}" font-size="11"
      text-anchor="${i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle'}"
      fill="${CH.muted}">${dmy(points[i].ymd)}</text>`)}
  </svg>`;
}

/** Горизонтальные полосы: топ товаров, выручка по категориям. */
function chartBars(items, fmt) {
  if (!items.length) return h`<p class="muted">Данных пока нет.</p>`;
  const W = 720, rowH = 30, T = 6;
  const H = T * 2 + items.length * rowH;
  const L = 176, R = 92, iw = W - L - R;
  const max = Math.max(...items.map(i => i.value)) || 1;

  return h`<svg class="chart" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="Столбчатая диаграмма: ${items.length} значений">
    ${items.map((it, n) => {
      const y = T + n * rowH, w = Math.max(2, iw * it.value / max);
      return h`<g>
        <text x="${L - 10}" y="${y + 19}" text-anchor="end" font-size="12"
          fill="${CH.ink}">${clip(it.name, 26)}</text>
        <rect x="${L}" y="${y + 6}" width="${w.toFixed(1)}" height="17" rx="3"
          fill="${CH.slices[n % CH.slices.length]}"/>
        <text x="${(L + w + 8).toFixed(1)}" y="${y + 19}" font-size="12"
          font-weight="700" fill="${CH.ink}">${fmt(it.value)}</text>
      </g>`;
    })}
  </svg>`;
}

/** Вертикальные столбцы: загрузка интервалов доставки. */
function chartColumns(items) {
  if (!items.length) return h`<p class="muted">Заказов с доставкой пока нет.</p>`;
  const W = 720, H = 190, L = 40, R = 8, T = 12, B = 30;
  const iw = W - L - R, ih = H - T - B;
  const max = niceMax(Math.max(...items.map(i => i.count)));
  const step = iw / items.length, bw = Math.min(46, step * .62);

  return h`<svg class="chart" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="Загрузка интервалов доставки">
    ${[0, .5, 1].map(f => h`<g>
      <line x1="${L}" y1="${(T + ih - ih * f).toFixed(1)}" x2="${W - R}"
        y2="${(T + ih - ih * f).toFixed(1)}" stroke="${CH.grid}" stroke-width="1"/>
      <text x="${L - 8}" y="${(T + ih - ih * f + 4).toFixed(1)}" text-anchor="end"
        font-size="11" fill="${CH.muted}">${Math.round(max * f)}</text>
    </g>`)}
    ${items.map((it, n) => {
      const bh = Math.max(2, ih * it.count / max);
      const cx = L + step * n + step / 2;
      return h`<g>
        <rect x="${(cx - bw / 2).toFixed(1)}" y="${(T + ih - bh).toFixed(1)}"
          width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="3" fill="${CH.green}"/>
        <text x="${cx.toFixed(1)}" y="${(T + ih - bh - 5).toFixed(1)}" text-anchor="middle"
          font-size="11" font-weight="700" fill="${CH.ink}">${it.count}</text>
        <text x="${cx.toFixed(1)}" y="${H - 10}" text-anchor="middle" font-size="11"
          fill="${CH.muted}">${it.label}</text>
      </g>`;
    })}
  </svg>`;
}

/** Кольцо: распределение заказов по статусам. */
function chartDonut(items) {
  const total = items.reduce((s, i) => s + i.count, 0);
  if (!total) return h`<p class="muted">Заказов пока нет.</p>`;
  const S = 190, c = S / 2, rOut = 84, rIn = 52;

  let angle = -Math.PI / 2;
  const arcs = items.map((it, n) => {
    const frac = it.count / total;
    const a0 = angle, a1 = angle + frac * Math.PI * 2;
    angle = a1;
    const color = CH.slices[n % CH.slices.length];
    /* Полный круг дугой не нарисовать: начальная и конечная точки
       совпадают, и браузер рисует пустоту. Единственный статус —
       это кольцо целиком, поэтому подменяем его двумя окружностями. */
    if (frac > 0.999) {
      return h`<circle cx="${c}" cy="${c}" r="${(rOut + rIn) / 2}" fill="none"
        stroke="${color}" stroke-width="${rOut - rIn}"/>`;
    }
    const pt = (r, a) => `${(c + r * Math.cos(a)).toFixed(2)} ${(c + r * Math.sin(a)).toFixed(2)}`;
    const big = a1 - a0 > Math.PI ? 1 : 0;
    const d = `M${pt(rOut, a0)} A${rOut} ${rOut} 0 ${big} 1 ${pt(rOut, a1)}` +
      ` L${pt(rIn, a1)} A${rIn} ${rIn} 0 ${big} 0 ${pt(rIn, a0)} Z`;
    return h`<path d="${d}" fill="${color}"/>`;
  });

  return h`<div class="chart-row">
    <svg class="donut" viewBox="0 0 ${S} ${S}" role="img"
        aria-label="Кольцевая диаграмма: заказы по статусам">
      ${arcs}
      <text x="${c}" y="${c - 2}" text-anchor="middle" font-size="26"
        font-weight="700" fill="${CH.ink}">${total}</text>
      <text x="${c}" y="${c + 18}" text-anchor="middle" font-size="11"
        fill="${CH.muted}">заказов</text>
    </svg>
    <ul class="legend">
      ${items.map((it, n) => h`<li>
        <i style="background:${CH.slices[n % CH.slices.length]}"></i>
        <span>${it.label}</span>
        <b>${it.count}</b>
        <em>${Math.round(it.count / total * 100)}%</em>
      </li>`)}
    </ul>
  </div>`;
}

/** «2026-08-25» → «25.08». Для подписей осей. */
function dmy(ymd) {
  const p = String(ymd).split('-');
  return p.length === 3 ? p[2] + '.' + p[1] : String(ymd);
}
const clip = (s, n) => String(s).length > n ? String(s).slice(0, n - 1) + '…' : String(s);

/* =====================================================================
   Дашборд (ТЗ 10.9)
   ===================================================================== */
async function pageDash() {
  const d = await api('/api/admin/dashboard');
  const k = d.kpi;
  $('#content').innerHTML = h`
    <div class="kpis">
      <div class="kpi"><b>${k.orders_total}</b><span>заказов всего</span></div>
      <div class="kpi"><b>${k.orders_active}</b><span>в работе</span></div>
      <div class="kpi"><b>${rub(k.revenue)}</b><span>выручка</span></div>
      <div class="kpi"><b>${rub(k.avg_check)}</b><span>средний чек</span></div>
    </div>
    <div class="kpis">
      <div class="kpi"><b>${k.preorders_new}</b><span>новых предзаказов</span></div>
      <div class="kpi"><b>${k.low_stock}</b><span>товаров заканчивается</span></div>
      <div class="kpi"><b>${k.cancelled}</b><span>отменено</span></div>
      <div class="kpi"><b>${d.top_products.length}</b><span>позиций в продажах</span></div>
    </div>
    <div class="panel"><h3>Выручка по периодам</h3>
      <div class="kpis" style="margin:0">
        ${[['day', 'Сегодня'], ['week', 'За 7 дней'], ['month', 'За 30 дней']].map(([k, title]) => {
          const p = d.periods && d.periods[k];
          if (!p) return raw('');
          return h`<div class="kpi">
            <b>${rub(p.sum)}</b>
            <span>${title} · ${p.count} заказ.${p.count ? ' · средний ' + rub(p.avg) : ''}</span>
            ${p.delta_pct === null
              ? raw(h`<div class="hint" style="margin-top:6px">не с чем сравнить</div>`)
              : raw(h`<div class="hint" style="margin-top:6px;color:${p.delta_pct >= 0 ? 'var(--green-tx)' : 'var(--red)'}">
                  ${p.delta_pct >= 0 ? '+' : ''}${p.delta_pct}% к предыдущему периоду</div>`)}
          </div>`;
        })}
      </div>
    </div>
    <div class="panel"><h3>Выручка по дням, 30 суток</h3>
      ${chartLine(d.daily || [], 'График выручки по дням за последние 30 суток')}
      <p class="hint">Дни без заказов показаны нулями — иначе линия сгладила бы паузу
        и её было бы не видно.</p>
    </div>
    <div class="charts-2">
      <div class="panel"><h3>Заказы по статусам</h3>
        ${chartDonut(d.by_status || [])}
      </div>
      <div class="panel"><h3>Загрузка интервалов</h3>
        ${chartColumns(d.by_slot || [])}
        <p class="hint">Сколько заказов приходится на каждый интервал доставки.</p>
      </div>
    </div>
    <div class="panel"><h3>Топ товаров</h3>
      ${d.top_products.length
        ? chartBars(d.top_products.map(p => ({ name: p.name, value: p.sum })), rub)
        : raw('<p class="muted">Продаж пока нет.</p>')}
      ${d.top_products.length ? raw('<table class="dt sr-table"><caption class="sr-only">Топ товаров по выручке</caption><tbody>' + d.top_products.map(p =>
        h`<tr><td>${p.name}</td><td style="text-align:right;font-weight:700">${rub(p.sum)}</td></tr>`).join('') + '</tbody></table>') : ''}
    </div>
    <div class="panel"><h3>Выручка по категориям</h3>
      ${d.top_categories.length
        ? chartBars(d.top_categories.map(c => ({ name: c.name, value: c.sum })), rub)
        : raw('<p class="muted">Данных пока нет.</p>')}
      ${d.top_categories.length ? raw('<table class="dt sr-table"><caption class="sr-only">Выручка по категориям</caption><tbody>' + d.top_categories.map(c =>
        h`<tr><td>${c.name}</td><td class="muted">${c.pct}%</td>
          <td style="text-align:right;font-weight:700">${rub(c.sum)}</td></tr>`).join('') + '</tbody></table>') : ''}
    </div>
    <div class="panel"><h3>Последние заказы</h3>
      ${d.recent.length ? raw('<div class="tbl-wrap"><table class="dt"><thead><tr><th>№</th><th>Клиент</th><th>Статус</th><th>Оплата</th><th>Сумма</th></tr></thead><tbody>' +
        d.recent.map(o => h`<tr class="row" data-order="${o.id}"><td><b>${o.number}</b></td>
          <td>${o.name}</td><td><span class="badge ${statusColor(o.status)}">${o.status_label}</span></td>
          <td><span class="badge ${payColor(o.payment_status)}">${o.payment_label}</span></td>
          <td style="font-weight:700">${rub(o.total)}</td></tr>`).join('') + '</tbody></table></div>')
        : raw('<p class="muted">Заказов пока нет.</p>')}
    </div>`;
  $$('[data-order]').forEach(r => r.onclick = () => openOrder(+r.dataset.order));
}

const STATUS_COLOR = { new: 'gray', awaiting_payment: 'amber', assembling: 'amber', partially_assembled: 'amber', ready: 'green', in_delivery: 'green', delivered: 'green', cancelled: 'red' };
const statusColor = s => STATUS_COLOR[s] || 'gray';
const payColor = s => s === 'paid' ? 'green' : s === 'refunded' ? 'red' : 'amber';

/* =====================================================================
   Товары (ТЗ 10.1)
   ===================================================================== */
const prodState = { view: 'table', q: '', cat: 'all', delConfirm: null };

async function pageProducts() {
  const [{ products }, { categories }] = await Promise.all([
    api('/api/admin/products'), api('/api/admin/categories')
  ]);
  S.data.products = products; S.data.categories = categories;
  paintProducts();
}

function filteredProducts() {
  const q = prodState.q.toLowerCase().trim();
  return S.data.products.filter(p => {
    if (prodState.cat !== 'all' && p.category_id !== prodState.cat) return false;
    if (!q) return true;
    return p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q);
  });
}

function paintProducts() {
  const list = filteredProducts();
  $('#content').innerHTML = h`
    <div class="panel">
      <div class="ptools">
        <input class="t grow" id="pq" aria-label="Поиск товаров по названию или артикулу"
          placeholder="Поиск по названию или артикулу" value="${prodState.q}">
        <select class="t" id="pcat" style="width:auto">
          <option value="all">Все категории</option>
          ${S.data.categories.filter(c => !c.is_system).map(c =>
            raw(h`<option value="${c.id}" ${prodState.cat === c.id ? 'selected' : ''}>${c.name}</option>`))}
        </select>
        <div class="rowbtns" role="group" aria-label="Вид списка">
          <button class="btn gh sm ${prodState.view === 'table' ? 'on' : ''}" id="vTable"
            aria-pressed="${prodState.view === 'table'}">Таблица</button>
          <button class="btn gh sm ${prodState.view === 'cards' ? 'on' : ''}" id="vCards"
            aria-pressed="${prodState.view === 'cards'}">Карточки</button>
        </div>
        <button class="btn gh sm" id="expBtn">Экспорт CSV</button>
        <button class="btn gh sm" id="impBtn">Импорт CSV</button>
        <button class="btn sm" id="addProd">Добавить товар</button>
      </div>
      <p class="muted" style="font-size:12px">Показано ${list.length} из ${S.data.products.length}</p>
      <div id="plist"></div>
    </div>`;

  $('#plist').innerHTML = prodState.view === 'cards' ? cardsHtml(list) : tableHtml(list);

  $('#pq').oninput = e => { prodState.q = e.target.value; refreshList(); };
  $('#pcat').onchange = e => { prodState.cat = e.target.value; refreshList(); };
  $('#vTable').onclick = () => { prodState.view = 'table'; paintProducts(); };
  $('#vCards').onclick = () => { prodState.view = 'cards'; paintProducts(); };
  $('#addProd').onclick = () => productModal(null);
  $('#expBtn').onclick = () => { location.href = '/api/admin/products.csv'; };
  $('#impBtn').onclick = importModal;
  bindProductActions();
}

function refreshList() {
  const list = filteredProducts();
  $('#plist').innerHTML = prodState.view === 'cards' ? cardsHtml(list) : tableHtml(list);
  bindProductActions();
}

function tableHtml(list) {
  if (!list.length) return h`<p class="muted">Ничего не найдено.</p>`;
  return h`<div class="tbl-wrap"><table class="dt">
    <thead><tr><th></th><th>Артикул</th><th>Название</th><th>Категория</th><th>Тип</th>
      <th>Цена</th><th>Остаток</th><th>НДС</th><th>Активен</th><th></th></tr></thead>
    <tbody>${list.map(p => {
      const cat = (S.data.categories.find(c => c.id === p.category_id) || {}).name || p.category_id;
      const typeN = { unit: 'Штучный', weighted: 'Весовой', preorder: 'Предзаказ' }[p.type];
      return h`<tr class="row" data-edit="${p.id}">
        <td><span class="ph">${photo(p, 80)}</span></td>
        <td class="sku">${p.sku}</td>
        <td><b>${p.name}</b>${!p.in_stock && p.type !== 'preorder' ? raw('<br><span class="badge red">нет в наличии</span>') : ''}</td>
        <td>${cat}</td><td>${typeN}</td>
        <td>${p.is_sale ? raw(h`<span class="strike">${rub(p.base_price)}</span>
          <b style="color:var(--green-tx)">${rub(p.sale_price)}</b>`) : rub(p.unit_price)}${p.type === 'unit' ? '' : '/кг'}</td>
        <td>${p.stock}</td><td>${p.vat_rate}%</td>
        <td data-stop><label class="sw"><input type="checkbox" ${p.is_active ? 'checked' : ''} data-toggle="${p.id}"><span class="tr"></span></label></td>
        <td data-stop><div class="rowbtns"><button class="iconbtn" data-del="${p.id}" title="Удалить">✕</button></div></td>
      </tr>`;
    })}</tbody></table></div>`;
}

function cardsHtml(list) {
  if (!list.length) return h`<p class="muted">Ничего не найдено.</p>`;
  return h`<div class="pgrid">${list.map(p => {
    const tags = [];
    if (p.is_sale) tags.push(h`<span class="badge gold">Акция</span>`);
    if (p.type === 'preorder') tags.push(h`<span class="badge gray">Предзаказ</span>`);
    if (!p.in_stock && p.type !== 'preorder') tags.push(h`<span class="badge red">Нет</span>`);
    return h`<div class="pcard ${p.is_active ? '' : 'off'}">
      <div class="pthumb">${photo(p, 240)}<div class="ptags">${tags.map(raw)}</div></div>
      <div class="pname">${p.name}</div>
      <div class="pmeta">${p.sku} · остаток ${p.stock} · НДС ${p.vat_rate}%</div>
      <div class="pprice">${p.is_sale ? raw(h`<span class="strike">${rub(p.base_price)}</span>
        <span style="color:var(--green-tx)">${rub(p.sale_price)}</span>`) : rub(p.unit_price)}</div>
      <div class="pactions">
        <button class="btn gh sm" data-edit="${p.id}">Изменить</button>
        <button class="iconbtn" data-del="${p.id}" title="Удалить">✕</button>
      </div></div>`;
  })}</div>`;
}

function bindProductActions() {
  $$('[data-edit]').forEach(el => el.onclick = e => {
    if (e.target.closest('[data-stop]')) return;
    productModal(+el.dataset.edit);
  });
  $$('[data-toggle]').forEach(el => el.onchange = async e => {
    e.stopPropagation();
    const p = S.data.products.find(x => x.id === +el.dataset.toggle);
    try {
      await api('/api/admin/products/' + p.id, { method: 'PUT', body: toPayload(p, { is_active: el.checked }) });
      p.is_active = el.checked;
      toast(el.checked ? 'Товар включён' : 'Товар скрыт');
    } catch (err) { el.checked = !el.checked; toast(errText(err), true); }
  });
  $$('[data-del]').forEach(el => el.onclick = async e => {
    e.stopPropagation();
    const id = +el.dataset.del;
    if (prodState.delConfirm !== id) {
      prodState.delConfirm = id;
      el.textContent = '?'; el.style.borderColor = 'var(--red)'; el.style.color = 'var(--red)';
      setTimeout(() => { if (prodState.delConfirm === id) { prodState.delConfirm = null; refreshList(); } }, 3000);
      return;
    }
    try {
      const r = await api('/api/admin/products/' + id, { method: 'DELETE' });
      toast(r.deactivated ? 'Товар есть в заказах — он скрыт, а не удалён' : 'Товар удалён');
      prodState.delConfirm = null;
      await pageProducts();
    } catch (err) { toast(errText(err), true); }
  });
}

/** Полное тело для PUT: сервер валидирует всю запись целиком. */
function toPayload(p, over = {}) {
  return Object.assign({
    sku: p.sku, name: p.name, slug: p.slug || '', category_id: p.category_id, type: p.type,
    price: p.price, price_per_kg: p.price_per_kg,
    sale_price: p.sale_price || '', sale_until: p.sale_until || '',
    vat_rate: String(p.vat_rate), stock: p.stock,
    min_weight: p.min_weight, weight_step: p.weight_step,
    is_active: p.is_active, image_key: p.image_key || '', emoji: p.emoji || '',
    description: p.description || ''
  }, over);
}

function productModal(id) {
  // Товар нельзя завести, пока нет ни одной обычной категории: раньше
  // здесь падало обращение к .id у несуществующей записи, и кнопка
  // «Добавить товар» просто переставала работать без объяснения.
  const firstCat = S.data.categories.find(c => !c.is_system);
  if (!id && !firstCat) {
    return toast('Сначала создайте категорию — товару нужно её указать', true);
  }

  const p = id ? S.data.products.find(x => x.id === id)
    : { sku: '', name: '', category_id: firstCat.id, type: 'unit',
        price: 0, price_per_kg: 0, sale_price: '', vat_rate: 10, stock: 0, is_active: true,
        emoji: '📦', image_key: '', description: '', min_weight: 0.5, weight_step: 0.5 };

  if (!p) return toast('Товар не найден — обновите список', true);

  modal(h`<div class="shead"><h2>${id ? 'Товар' : 'Новый товар'}</h2>
      <button class="x" data-close="1" aria-label="Закрыть">✕</button></div>
    <div class="sbody">
      <div class="frow">
        <div><label class="f" for="f_sku">Артикул <span class="req">*</span></label>
          <input class="t" id="f_sku" value="${p.sku}" maxlength="32"></div>
        <div><label class="f" for="f_type">Тип <span class="req">*</span></label>
          <select class="t" id="f_type">
            <option value="unit" ${p.type === 'unit' ? 'selected' : ''}>Штучный</option>
            <option value="weighted" ${p.type === 'weighted' ? 'selected' : ''}>Весовой</option>
            <option value="preorder" ${p.type === 'preorder' ? 'selected' : ''}>Предзаказ</option>
          </select></div>
      </div>
      <label class="f" for="f_name">Название <span class="req">*</span></label>
      <input class="t" id="f_name" value="${p.name}" maxlength="160">
      <div class="frow">
        <div><label class="f" for="f_cat">Категория <span class="req">*</span></label>
          <select class="t" id="f_cat">${S.data.categories.filter(c => !c.is_system).map(c =>
            raw(h`<option value="${c.id}" ${p.category_id === c.id ? 'selected' : ''}>${c.name}</option>`))}</select></div>
        <div><label class="f" for="f_vat">НДС <span class="req">*</span></label>
          <select class="t" id="f_vat">${[0, 10, 20].map(v =>
            raw(h`<option value="${v}" ${Number(p.vat_rate) === v ? 'selected' : ''}>${v}%</option>`))}</select></div>
      </div>
      <div class="frow">
        <div><label class="f" for="f_price">Цена <span class="req">*</span></label>
          <input class="t" id="f_price" type="number" min="0" step="0.01"
            value="${p.type === 'unit' ? p.price : p.price_per_kg}">
          <div class="hint" id="priceHint"></div></div>
        <div><label class="f" for="f_sale">Акционная цена</label>
          <input class="t" id="f_sale" type="number" min="0" step="0.01" value="${p.sale_price || ''}"></div>
      </div>
      <label class="f" for="f_until">Акция действует до</label>
      <input class="t" id="f_until" type="date" value="${p.sale_until || ''}">
      <div class="hint">Включительно. После этой даты акция снимается сама —
        цена возвращается к базовой. Пусто — акция бессрочная.</div>
      <div class="frow">
        <div><label class="f" for="f_stock">Остаток <span class="req">*</span></label>
          <input class="t" id="f_stock" type="number" min="0" step="0.01" value="${p.stock}"></div>
        <div><label class="f" for="f_img">Ключ фото</label>
          <input class="t" id="f_img" value="${p.image_key || ''}" maxlength="40" placeholder="avocado"></div>
      </div>
      <div class="frow">
        <div><label class="f" for="f_emoji">Эмодзи-заглушка</label>
          <input class="t" id="f_emoji" value="${p.emoji || ''}" maxlength="8"></div>
        <div><label class="f">Активен</label>
          <label class="sw" style="margin-top:8px"><input type="checkbox" id="f_active" ${p.is_active ? 'checked' : ''}><span class="tr"></span></label></div>
      </div>
      <label class="f" for="f_desc">Описание</label>
      <textarea class="t" id="f_desc" maxlength="2000">${p.description || ''}</textarea>
      <div class="err-msg" id="pErr" role="alert"></div>
      <div class="mfoot">
        <button class="btn gh" data-close="1">Отмена</button>
        <button class="btn" id="saveProd">Сохранить</button>
      </div>
    </div>`);

  const hint = () => {
    $('#priceHint').textContent = $('#f_type').value === 'unit' ? 'за штуку' : 'за килограмм';
  };
  $('#f_type').onchange = hint; hint();

  $('#saveProd').onclick = async () => {
    const type = $('#f_type').value;
    const price = Number($('#f_price').value);
    const body = {
      sku: $('#f_sku').value.trim(), name: $('#f_name').value.trim(),
      slug: p.slug || '', category_id: $('#f_cat').value, type,
      price: type === 'unit' ? price : 0,
      price_per_kg: type === 'unit' ? 0 : price,
      sale_price: $('#f_sale').value || '', sale_until: $('#f_until').value || '',
      vat_rate: $('#f_vat').value, stock: Number($('#f_stock').value),
      min_weight: p.min_weight || 0.5, weight_step: p.weight_step || 0.5,
      is_active: $('#f_active').checked,
      image_key: $('#f_img').value.trim(), emoji: $('#f_emoji').value.trim(),
      description: $('#f_desc').value
    };
    try {
      await api(id ? '/api/admin/products/' + id : '/api/admin/products',
        { method: id ? 'PUT' : 'POST', body });
      closeModal(); toast(id ? 'Товар обновлён' : 'Товар добавлен');
      await pageProducts();
    } catch (e) { $('#pErr').textContent = errText(e); }
  };
}

function importModal() {
  modal(h`<div class="shead"><h2>Импорт товаров из CSV</h2>
      <button class="x" data-close="1" aria-label="Закрыть">✕</button></div>
    <div class="sbody">
      <div class="note">Колонки через точку с запятой:
        <code>sku;name;category;type;price;sale_price;stock;vat;active</code>.
        Сопоставление идёт по артикулу (SKU). Битые строки пропускаются,
        остальные импортируются.</div>
      <label class="f">Режим (ТЗ 10.7)</label>
      <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:10px">
        <label><input type="radio" name="impMode" value="full" checked>
          Полная карточка — есть SKU: обновить всё; нет — создать товар</label>
        <label><input type="radio" name="impMode" value="prices_stock">
          Только цены и остатки — не трогает название/категорию/тип,
          новые SKU не создаёт</label>
        <label><input type="radio" name="impMode" value="new_only">
          Только новые — существующие SKU не трогает вовсе</label>
      </div>
      <label class="f" for="csvFile">Файл CSV</label>
      <input class="t" type="file" id="csvFile" accept=".csv,text/csv">
      <label class="f" for="csvText">…или вставьте текстом</label>
      <textarea class="t" id="csvText" rows="7" placeholder="sku;name;category;type;price;sale_price;stock;vat;active"></textarea>
      <div id="impResult"></div>
      <div class="mfoot">
        <button class="btn gh" data-close="1">Отмена</button>
        <button class="btn" id="doImport">Импортировать</button>
      </div>
    </div>`);

  $('#csvFile').onchange = e => {
    const f = e.target.files[0];
    if (!f) return;
    if (f.size > 4 * 1024 * 1024) return toast('Файл больше 4 МБ', true);
    const r = new FileReader();
    r.onload = () => { $('#csvText').value = r.result; };
    r.readAsText(f, 'utf-8');
  };
  $('#doImport').onclick = async () => {
    const csv = $('#csvText').value.trim();
    if (!csv) return toast('Выберите файл или вставьте текст', true);
    const btn = $('#doImport'); btn.disabled = true; btn.textContent = 'Импортируем…';
    const mode = $('input[name="impMode"]:checked').value;
    try {
      const r = await api('/api/admin/products/import', { method: 'POST', body: { csv, mode } });
      $('#impResult').innerHTML = h`<div class="note">Создано: ${r.created}, обновлено: ${r.updated},
        пропущено: ${r.skipped.length}</div>` +
        (r.skipped.length ? h`<div class="note err"><b>Пропущенные строки:</b><br>
          ${r.skipped.slice(0, 20).map(s => raw(h`${s.sku} — ${s.reason}<br>`))}</div>` : '');
      toast(`Импорт завершён: +${r.created}, обновлено ${r.updated}`);
      await pageProducts();
    } catch (e) { $('#impResult').innerHTML = h`<div class="note err">${errText(e)}</div>`; }
    finally { btn.disabled = false; btn.textContent = 'Импортировать'; }
  };
}

/* =====================================================================
   Категории (ТЗ 10.2)
   ===================================================================== */
async function pageCategories() {
  const { categories } = await api('/api/admin/categories');
  S.data.categories = categories;
  $('#content').innerHTML = h`<div class="panel">
    <div class="ptools"><h3 style="flex:1;margin:0">Категории</h3>
      <button class="btn sm" id="addCat">Добавить</button></div>
    <div class="tbl-wrap"><table class="dt">
      <thead><tr><th>Порядок</th><th></th><th>Название</th><th>Товаров</th><th>Активна</th><th></th></tr></thead>
      <tbody>${categories.map((c, i) => h`<tr>
        <td><div class="rowbtns">
          <button class="iconbtn" data-up="${i}" ${i === 0 ? 'disabled' : ''} aria-label="Выше">↑</button>
          <button class="iconbtn" data-down="${i}" ${i === categories.length - 1 ? 'disabled' : ''} aria-label="Ниже">↓</button>
        </div></td>
        <td style="font-size:19px">${c.emoji}</td>
        <td><b>${c.name}</b>${c.is_system ? raw(' <span class="badge gray">системная</span>') : ''}</td>
        <td>${c.product_count}</td>
        <td><label class="sw"><input type="checkbox" ${c.is_active !== false ? 'checked' : ''} data-cattoggle="${c.id}"><span class="tr"></span></label></td>
        <td><div class="rowbtns">
          <button class="iconbtn" data-catedit="${c.id}" aria-label="Изменить">✎</button>
          ${c.is_system ? '' : raw(h`<button class="iconbtn" data-catdel="${c.id}" aria-label="Удалить">✕</button>`)}
        </div></td></tr>`)}</tbody></table></div>
    </div>`;

  $('#addCat').onclick = () => catModal(null);
  $$('[data-catedit]').forEach(b => b.onclick = () => catModal(b.dataset.catedit));
  $$('[data-up],[data-down]').forEach(b => b.onclick = async () => {
    const i = +(b.dataset.up !== undefined ? b.dataset.up : b.dataset.down);
    const j = b.dataset.up !== undefined ? i - 1 : i + 1;
    const order = categories.map(c => c.id);
    [order[i], order[j]] = [order[j], order[i]];
    try { await api('/api/admin/categories/reorder', { method: 'POST', body: { order } }); await pageCategories(); }
    catch (e) { toast(errText(e), true); }
  });
  $$('[data-cattoggle]').forEach(el => el.onchange = async () => {
    const c = categories.find(x => x.id === el.dataset.cattoggle);
    try {
      await api('/api/admin/categories/' + c.id, { method: 'PUT',
        body: { id: c.id, name: c.name, emoji: c.emoji, is_active: el.checked } });
      toast('Сохранено');
    } catch (e) { el.checked = !el.checked; toast(errText(e), true); }
  });
  $$('[data-catdel]').forEach(b => b.onclick = async () => {
    if (!confirm('Удалить категорию?')) return;
    try { await api('/api/admin/categories/' + b.dataset.catdel, { method: 'DELETE' }); toast('Категория удалена'); await pageCategories(); }
    catch (e) { toast(errText(e), true); }
  });
}

function catModal(id) {
  const c = id ? S.data.categories.find(x => x.id === id) : { id: '', name: '', emoji: '📦', is_active: true };
  modal(h`<div class="shead"><h2>${id ? 'Категория' : 'Новая категория'}</h2>
      <button class="x" data-close="1">✕</button></div>
    <div class="sbody">
      <label class="f" for="c_id">Идентификатор <span class="req">*</span></label>
      <input class="t" id="c_id" value="${c.id}" ${id ? 'disabled' : ''} maxlength="40" placeholder="latin_id">
      ${id ? raw('<div class="hint">Идентификатор менять нельзя — на него ссылаются товары.</div>') : ''}
      <label class="f" for="c_name">Название <span class="req">*</span></label>
      <input class="t" id="c_name" value="${c.name}" maxlength="80">
      <label class="f" for="c_emoji">Эмодзи</label>
      <input class="t" id="c_emoji" value="${c.emoji}" maxlength="8">
      <div class="err-msg" id="cErr" role="alert"></div>
      <div class="mfoot"><button class="btn gh" data-close="1">Отмена</button>
        <button class="btn" id="saveCat">Сохранить</button></div>
    </div>`);
  $('#saveCat').onclick = async () => {
    const body = { id: id || $('#c_id').value.trim(), name: $('#c_name').value.trim(),
      emoji: $('#c_emoji').value.trim(), is_active: c.is_active !== false };
    try {
      await api(id ? '/api/admin/categories/' + id : '/api/admin/categories', { method: id ? 'PUT' : 'POST', body });
      closeModal(); toast('Сохранено'); await pageCategories();
    } catch (e) { $('#cErr').textContent = errText(e); }
  };
}

/* =====================================================================
   Заказы (ТЗ 10.3)
   ===================================================================== */
const ordState = { status: 'all', q: '' };

async function pageOrders() {
  const qs = new URLSearchParams({ status: ordState.status });
  if (ordState.q) qs.set('q', ordState.q);
  const { orders } = await api('/api/admin/orders?' + qs);
  S.data.orders = orders;

  $('#content').innerHTML = h`<div class="panel">
    <div class="ptools">
      <input class="t grow" id="oq" aria-label="Поиск заказов по номеру, имени или телефону"
        placeholder="Номер, имя или телефон" value="${ordState.q}">
      <select class="t" id="ostatus" style="width:auto">
        <option value="all">Все статусы</option>
        ${Object.entries(Calc.STATUS_LABEL).map(([k, v]) =>
          raw(h`<option value="${k}" ${ordState.status === k ? 'selected' : ''}>${v}</option>`))}
      </select>
      <button class="btn gh sm" id="oexp">Экспорт CSV</button>
    </div>
    <div class="tbl-wrap"><table class="dt">
      <thead><tr><th>№</th><th>Дата</th><th>Клиент</th><th>Получение</th><th>Статус</th><th>Оплата</th><th>Сумма</th></tr></thead>
      <tbody>${orders.length ? orders.map(o => h`<tr class="row" data-order="${o.id}">
        <td><b>${o.number}</b></td>
        <td class="muted">${new Date(o.created_at).toLocaleDateString('ru-RU')}</td>
        <td>${o.name}<br><span class="muted sku">${o.phone}</span></td>
        <td>${o.method === 'delivery' ? 'Доставка' : 'Самовывоз'}<br>
          <span class="muted">${o.slot_ymd} ${o.slot_from}:00</span></td>
        <td><span class="badge ${statusColor(o.status)}">${o.status_label}</span></td>
        <td><span class="badge ${payColor(o.payment_status)}">${o.payment_label}</span></td>
        <td style="font-weight:700;white-space:nowrap">${rub(o.total)}</td></tr>`)
        : raw('<tr><td colspan="7" class="muted">Заказов нет.</td></tr>')}</tbody></table></div>
    </div>`;

  let t;
  $('#oq').oninput = e => { clearTimeout(t); ordState.q = e.target.value; t = setTimeout(pageOrders, 300); };
  $('#ostatus').onchange = e => { ordState.status = e.target.value; pageOrders(); };
  $('#oexp').onclick = () => { location.href = '/api/admin/orders.csv'; };
  $$('[data-order]').forEach(r => r.onclick = () => openOrder(+r.dataset.order));
}

async function openOrder(id) {
  let o;
  try { o = (await api('/api/admin/orders/' + id)).order; }
  catch (e) { return toast(errText(e), true); }
  paintOrder(o);
}

function paintOrder(o) {
  const weighted = o.items.filter(i => i.type === 'weighted');
  const diff = o.total - o.planned_total;

  modal(h`<div class="shead"><h2>Заказ №${o.number}</h2>
      <button class="x" data-close="1" aria-label="Закрыть">✕</button></div>
    <div class="sbody">
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
        <span class="badge ${statusColor(o.status)}">${o.status_label}</span>
        <span class="badge ${payColor(o.payment_status)}">${o.payment_label}</span>
        ${o.is_guest ? raw('<span class="badge gray">гость</span>') : ''}
      </div>
      <div class="rev">
        <div class="l"><span>Клиент</span><b>${o.name}, ${o.phone}</b></div>
        <div class="l"><span>Email для чека</span><b>${o.email}</b></div>
        <div class="l"><span>Получение</span><b>${o.method === 'delivery' ? 'Доставка · ' + (o.zone || '') : 'Самовывоз'}</b></div>
        ${o.address ? raw(h`<div class="l"><span>Адрес</span><b>${o.address}</b></div>`) : ''}
        <div class="l"><span>Интервал</span><b>${o.slot_ymd}, ${o.slot_from}:00–${o.slot_to}:00</b></div>
        <div class="l"><span>Оплата</span><b>${Calc.paymentLabel(o.payment_method)}</b></div>
        ${o.comment ? raw(h`<div class="l"><span>Комментарий</span><b>${o.comment}</b></div>`) : ''}
        ${o.cancel_reason ? raw(h`<div class="l"><span>Причина отмены</span><b>${o.cancel_reason}</b></div>`) : ''}
      </div>

      <h3 style="font-size:14px;margin:16px 0 6px">Состав</h3>
      ${o.items.map(i => h`<div class="oi" style="${i.is_removed ? 'opacity:.5' : ''}">
        <div class="in">
          <b style="${i.is_removed ? 'text-decoration:line-through' : ''}">${i.name}</b>
          <small>${i.type === 'unit' ? i.requested_quantity + ' шт' : '~' + i.requested_weight + ' кг'}
            · ${rub(i.price_at_purchase)}/${i.type === 'unit' ? 'шт' : 'кг'} · НДС ${i.vat_rate}%</small>
          ${i.type === 'weighted' && !i.is_removed ? raw(h`<div style="margin-top:6px;display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            <label class="f" style="margin:0;font-size:11px" for="w_${i.id}">Факт, кг</label>
            <input class="t" id="w_${i.id}" type="number" step="0.01" min="0" style="width:96px"
              value="${i.actual_weight != null ? i.actual_weight : ''}" placeholder="${i.requested_weight}">
            <button class="btn gh sm" data-setw="${i.id}">Записать</button>
            ${i.actual_weight != null ? raw(weightBadge(i)) : ''}
            ${i.actual_weight != null && i.weight_confirmed === false
              ? raw(h`<button class="btn sm" data-confirmw="${i.id}"
                  style="width:auto;background:var(--amber);border-color:var(--amber);color:#fff">
                  Клиент согласовал</button>`) : ''}
          </div>`) : ''}
        </div>
        <div class="rt">${rub(i.line_total)}<br>
          <button class="btn gh sm" style="margin-top:4px" data-rm="${i.id}" data-val="${i.is_removed ? '0' : '1'}">
            ${i.is_removed ? 'Вернуть' : 'Нет в наличии'}</button></div>
      </div>`)}

      <div class="sum">
        <div class="row"><span>Товары</span><span>${rub(o.items_total)}</span></div>
        ${o.discount_amount ? raw(h`<div class="row disc"><span>Скидка ${o.promocode || ''}</span><span>−${rub(o.discount_amount)}</span></div>`) : ''}
        <div class="row"><span>Доставка</span><span>${o.delivery_cost ? rub(o.delivery_cost) : 'бесплатно'}</span></div>
        <div class="row tot"><span>Итого</span><span>${rub(o.total)}</span></div>
        ${diff !== 0 ? raw(h`<div class="row" style="color:${diff > 0 ? 'var(--red)' : 'var(--green-tx)'}">
          <span>Изменение после сборки</span><span>${diff > 0 ? '+' : '−'}${rub(Math.abs(diff))}</span></div>`) : ''}
        ${o.hold_amount ? raw(h`<div class="row"><span>Холд на карте</span><span>${rub(o.hold_amount)}</span></div>`) : ''}
      </div>

      <h3 style="font-size:14px;margin:16px 0 6px">История</h3>
      <div class="hist">${(o.history || []).map(x =>
        h`<div>${new Date(x.at).toLocaleString('ru-RU')} — ${x.label}
          <span class="muted">(${x.actor})</span>${x.comment ? ' · ' + esc(x.comment) : ''}</div>`)}</div>

      <div class="mfoot">
        <a class="btn gh sm" href="/api/admin/orders/${o.id}/packing-list" target="_blank"
           rel="noopener" style="text-decoration:none;line-height:1.7">Лист сборки</a>
        ${o.status !== 'cancelled' && o.status !== 'delivered'
          ? raw(h`<button class="btn danger sm" id="cancelOrd">Отменить заказ</button>`) : ''}
        ${o.payment_status === 'pending' && o.status !== 'cancelled' && can('payments')
          ? raw(h`<button class="btn gh sm" id="markPaid">Отметить оплаченным</button>`) : ''}
        ${o.next_status && o.status !== 'cancelled'
          ? raw(h`<button class="btn sm" id="nextStatus">→ ${Calc.STATUS_LABEL[o.next_status]}</button>`) : ''}
        ${!o.next_status && o.blocked_reason === 'payment_not_confirmed'
          ? raw(h`<span class="hint" style="align-self:center">Ждём подтверждения платежа${can('payments') ? '' : ' — обратитесь к администратору'}</span>`) : ''}
      </div>
    </div>`);

  $$('[data-setw]').forEach(b => b.onclick = async () => {
    const id = +b.dataset.setw;
    const val = Number($('#w_' + id).value);
    if (!(val > 0)) return toast('Введите фактический вес', true);
    try {
      const r = await api(`/api/admin/orders/${o.id}/item-weight`,
        { method: 'POST', body: { item_id: id, actual_weight: val } });
      if (r.awaiting_confirmation) {
        toast(`Отклонение ${r.check.deviation}% — сумма не изменена, нужно согласие клиента`, true);
      } else {
        toast(`Записано, отклонение ${r.check.deviation}%`);
      }
      paintOrder(r.order);
    } catch (e) { toast(errText(e), true); }
  });

  // подтверждение перевеса после звонка клиенту (ТЗ 3.4)
  $$('[data-confirmw]').forEach(b => b.onclick = async () => {
    const id = +b.dataset.confirmw;
    const item = o.items.find(i => i.id === id);
    if (!item) return;
    if (!confirm(`Клиент согласовал ${item.actual_weight} кг вместо ${item.requested_weight} кг?\n\nСумма заказа будет пересчитана.`)) return;
    try {
      const r = await api(`/api/admin/orders/${o.id}/item-weight`,
        { method: 'POST', body: { item_id: id, actual_weight: item.actual_weight, confirm: true } });
      toast('Вес согласован, сумма пересчитана');
      paintOrder(r.order);
    } catch (e) { toast(errText(e), true); }
  });
  $$('[data-rm]').forEach(b => b.onclick = async () => {
    try {
      const r = await api(`/api/admin/orders/${o.id}/item-remove`,
        { method: 'POST', body: { item_id: +b.dataset.rm, is_removed: b.dataset.val === '1' } });
      paintOrder(r.order);
    } catch (e) { toast(errText(e), true); }
  });
  const ns = $('#nextStatus');
  if (ns) ns.onclick = async () => {
    try {
      const r = await api(`/api/admin/orders/${o.id}/status`, { method: 'POST', body: { status: o.next_status, comment: '' } });
      toast('Статус обновлён'); paintOrder(r.order); if (S.page === 'orders') pageOrders();
    } catch (e) { toast(errText(e), true); }
  };
  const mp = $('#markPaid');
  if (mp) mp.onclick = async () => {
    try {
      const r = await api(`/api/admin/orders/${o.id}/payment`, { method: 'POST', body: { payment_status: 'paid' } });
      toast('Отмечено как оплачено'); paintOrder(r.order);
    } catch (e) { toast(errText(e), true); }
  };
  const co = $('#cancelOrd');
  if (co) co.onclick = async () => {
    const reason = prompt('Причина отмены:');
    if (reason === null) return;
    try {
      const r = await api(`/api/admin/orders/${o.id}/status`, { method: 'POST', body: { status: 'cancelled', comment: reason } });
      toast('Заказ отменён, остатки возвращены'); paintOrder(r.order); if (S.page === 'orders') pageOrders();
    } catch (e) { toast(errText(e), true); }
  };
}

function weightBadge(i) {
  const c = Calc.checkActualWeight(i.requested_weight, i.actual_weight);
  const pending = i.weight_confirmed === false;
  const label = c.ok ? '' : (pending ? ' — сумма не изменена, нужен звонок' : ' — согласовано');
  return h`<span class="badge ${c.ok ? 'green' : pending ? 'red' : 'amber'}">${c.deviation > 0 ? '+' : ''}${c.deviation}%${label}</span>`;
}

/* =====================================================================
   Предзаказы (ТЗ 10.4)
   ===================================================================== */
async function pagePreorders() {
  const d = await api('/api/admin/preorders');
  $('#content').innerHTML = h`
    <div class="panel"><h3>Настройки поставок мяса</h3>
      <p class="muted" style="font-size:12.5px">Дни поставки: <b>${d.meat.days.join(', ')}</b> ·
        дневной объём: <b>${d.meat.limit_kg} кг</b> · приём закрывается за <b>${d.meat.cutoff_days} сут.</b>
        Изменить можно в разделе «Доставка».</p>
      ${Object.keys(d.meat.bookings || {}).length ? raw(h`<div class="tbl-wrap"><table class="dt">
        <thead><tr><th>Дата</th><th>Забронировано</th><th>Свободно</th></tr></thead><tbody>
        ${Object.entries(d.meat.bookings).filter(([, v]) => v > 0).map(([ymd, kg]) =>
          h`<tr><td>${ymd}</td><td>${kg} кг</td><td>${Math.max(0, d.meat.limit_kg - kg)} кг</td></tr>`)}
        </tbody></table></div>`) : raw('<p class="muted">Броней пока нет.</p>')}
    </div>
    <div class="panel"><h3>Заявки</h3>
      <div class="tbl-wrap"><table class="dt">
        <thead><tr><th>№</th><th>Клиент</th><th>Товар</th><th>Вес</th><th>Дата выдачи</th><th>Ориентир</th><th>Статус</th><th></th></tr></thead>
        <tbody>${d.preorders.length ? d.preorders.map(p => h`<tr>
          <td><b>${p.number}</b></td>
          <td>${p.name}<br><span class="muted sku">${p.phone}</span></td>
          <td>${p.product_name}${p.comment ? raw(h`<br><span class="muted">${p.comment}</span>`) : ''}</td>
          <td>~${p.requested_weight} кг</td>
          <td>${p.pickup_date}</td>
          <td>${rub(p.estimate)}</td>
          <td><span class="badge ${p.status === 'done' ? 'green' : p.status === 'cancelled' ? 'red' : 'amber'}">${preLabel(p.status)}</span></td>
          <td><select class="t" style="width:auto;font-size:12px" data-prestatus="${p.id}">
            ${Object.entries(PRE_LABELS).map(([k, v]) =>
              raw(h`<option value="${k}" ${p.status === k ? 'selected' : ''}>${v}</option>`))}
          </select></td></tr>`) : raw('<tr><td colspan="8" class="muted">Заявок нет.</td></tr>')}
        </tbody></table></div>
    </div>`;

  $$('[data-prestatus]').forEach(sel => sel.onchange = async () => {
    try {
      await api('/api/admin/preorders/' + sel.dataset.prestatus + '/status',
        { method: 'POST', body: { status: sel.value } });
      toast('Статус обновлён'); await pagePreorders();
    } catch (e) { toast(errText(e), true); }
  });
}
const PRE_LABELS = { new: 'Новый', confirmed: 'Подтверждён', ready: 'Готов', done: 'Выдан', cancelled: 'Отменён' };
const preLabel = s => PRE_LABELS[s] || s;

/* =====================================================================
   Промокоды (ТЗ 10.5)
   ===================================================================== */
async function pagePromos() {
  const { promocodes } = await api('/api/admin/promocodes');
  S.data.promos = promocodes;
  $('#content').innerHTML = h`<div class="panel">
    <div class="ptools"><h3 style="flex:1;margin:0">Промокоды</h3>
      <button class="btn sm" id="addPromo">Добавить</button></div>
    <div class="tbl-wrap"><table class="dt">
      <thead><tr><th>Код</th><th>Тип</th><th>Значение</th><th>От суммы</th><th>Использовано</th><th>Действует до</th><th>Активен</th><th></th></tr></thead>
      <tbody>${promocodes.map(p => h`<tr>
        <td><b class="sku" style="font-size:13px;color:var(--ink)">${p.code}</b></td>
        <td>${{ percent: 'Процент', fixed: 'Фикс. сумма', delivery: 'Доставка' }[p.type]}</td>
        <td>${p.type === 'percent' || p.type === 'delivery' ? p.value + '%' : rub(p.value)}</td>
        <td>${p.min_order ? rub(p.min_order) : '—'}</td>
        <td>${p.uses_count}${p.uses_limit ? ' / ' + p.uses_limit : ''}</td>
        <td>${p.valid_until || '—'}</td>
        <td><label class="sw"><input type="checkbox" ${p.is_active ? 'checked' : ''} data-promotoggle="${p.id}"><span class="tr"></span></label></td>
        <td><div class="rowbtns">
          <button class="iconbtn" data-promoedit="${p.id}" aria-label="Изменить">✎</button>
          <button class="iconbtn" data-promodel="${p.id}" aria-label="Удалить">✕</button></div></td>
      </tr>`)}</tbody></table></div>
    </div>`;

  $('#addPromo').onclick = () => promoModal(null);
  $$('[data-promoedit]').forEach(b => b.onclick = () => promoModal(+b.dataset.promoedit));
  $$('[data-promotoggle]').forEach(el => el.onchange = async () => {
    const p = promocodes.find(x => x.id === +el.dataset.promotoggle);
    try {
      await api('/api/admin/promocodes/' + p.id, { method: 'PUT', body: promoPayload(p, { is_active: el.checked }) });
      toast('Сохранено');
    } catch (e) { el.checked = !el.checked; toast(errText(e), true); }
  });
  $$('[data-promodel]').forEach(b => b.onclick = async () => {
    if (!confirm('Удалить промокод?')) return;
    try { await api('/api/admin/promocodes/' + b.dataset.promodel, { method: 'DELETE' }); toast('Удалён'); await pagePromos(); }
    catch (e) { toast(errText(e), true); }
  });
}

const promoPayload = (p, over = {}) => Object.assign({
  code: p.code, type: p.type, value: p.value, min_order: p.min_order,
  uses_limit: p.uses_limit, per_user_limit: p.per_user_limit,
  valid_until: p.valid_until || '', is_active: p.is_active
}, over);

function promoModal(id) {
  const p = id ? S.data.promos.find(x => x.id === id)
    : { code: '', type: 'percent', value: 10, min_order: 0, uses_limit: 0, per_user_limit: 1, valid_until: '', is_active: true };
  modal(h`<div class="shead"><h2>${id ? 'Промокод' : 'Новый промокод'}</h2>
      <button class="x" data-close="1">✕</button></div>
    <div class="sbody">
      <label class="f" for="pr_code">Код <span class="req">*</span></label>
      <input class="t" id="pr_code" value="${p.code}" maxlength="24" style="text-transform:uppercase">
      <div class="frow">
        <div><label class="f" for="pr_type">Тип <span class="req">*</span></label>
          <select class="t" id="pr_type">
            <option value="percent" ${p.type === 'percent' ? 'selected' : ''}>Процент от суммы</option>
            <option value="fixed" ${p.type === 'fixed' ? 'selected' : ''}>Фиксированная сумма</option>
            <option value="delivery" ${p.type === 'delivery' ? 'selected' : ''}>Скидка на доставку, %</option>
          </select></div>
        <div><label class="f" for="pr_val">Значение <span class="req">*</span></label>
          <input class="t" id="pr_val" type="number" min="1" value="${p.value}"></div>
      </div>
      <div class="frow">
        <div><label class="f" for="pr_min">Минимальная сумма</label>
          <input class="t" id="pr_min" type="number" min="0" value="${p.min_order}"></div>
        <div><label class="f" for="pr_lim">Лимит использований</label>
          <input class="t" id="pr_lim" type="number" min="0" value="${p.uses_limit}">
          <div class="hint">0 — без ограничения</div></div>
      </div>
      <div class="frow">
        <div><label class="f" for="pr_user">На одного клиента</label>
          <input class="t" id="pr_user" type="number" min="0" value="${p.per_user_limit}"></div>
        <div><label class="f" for="pr_until">Действует до</label>
          <input class="t" id="pr_until" type="date" value="${p.valid_until || ''}"></div>
      </div>
      <div class="note">Процент не суммируется с акционной ценой — скидка считается только
        от неакционных позиций (ТЗ 6.1).</div>
      <div class="err-msg" id="prErr" role="alert"></div>
      <div class="mfoot"><button class="btn gh" data-close="1">Отмена</button>
        <button class="btn" id="savePromo">Сохранить</button></div>
    </div>`);
  $('#savePromo').onclick = async () => {
    const body = {
      code: $('#pr_code').value.trim().toUpperCase(), type: $('#pr_type').value,
      value: Number($('#pr_val').value), min_order: Number($('#pr_min').value),
      uses_limit: Number($('#pr_lim').value), per_user_limit: Number($('#pr_user').value),
      valid_until: $('#pr_until').value || '', is_active: p.is_active
    };
    try {
      await api(id ? '/api/admin/promocodes/' + id : '/api/admin/promocodes', { method: id ? 'PUT' : 'POST', body });
      closeModal(); toast('Сохранено'); await pagePromos();
    } catch (e) { $('#prErr').textContent = errText(e); }
  };
}

/* =====================================================================
   Доставка и настройки (ТЗ 10.6)
   ===================================================================== */
async function pageDelivery() {
  const { settings: st, zones } = await api('/api/admin/settings');
  $('#content').innerHTML = h`
    <div class="panel"><h3>Зоны доставки</h3>
      <div class="tbl-wrap"><table class="dt">
        <thead><tr><th>Зона</th><th>Стоимость, ₽</th><th>Бесплатно от, ₽</th><th></th></tr></thead>
        <tbody>${zones.map(z => z.manual_quote
          ? h`<tr><td>${z.name}</td><td colspan="3" class="muted">Расчёт вручную — доставка сторонней
              службой за счёт покупателя (ТЗ 5.2, Фаза 2)</td></tr>`
          : h`<tr><td>${z.name}</td>
              <td><input class="t" style="width:100px" type="number" min="0" id="zc_${z.id}"
                aria-label="Стоимость доставки в зону ${z.name}, ₽" value="${z.cost}"></td>
              <td><input class="t" style="width:110px" type="number" min="0" id="zf_${z.id}"
                aria-label="Бесплатная доставка в зону ${z.name} от суммы, ₽" value="${z.free_from}"></td>
              <td><button class="btn gh sm" data-zone="${z.id}">Сохранить</button></td></tr>`)}
        </tbody></table></div>
    </div>

    <div class="panel"><h3>Слоты и режим работы</h3>
      <div class="frow">
        <div><label class="f" for="s_from">Начало работы</label>
          <input class="t" id="s_from" type="number" min="0" max="23" value="${st.work_from}"></div>
        <div><label class="f" for="s_to">Конец работы</label>
          <input class="t" id="s_to" type="number" min="1" max="24" value="${st.work_to}"></div>
      </div>
      <div class="frow">
        <div><label class="f" for="s_cut">Cut-off, часов до слота</label>
          <input class="t" id="s_cut" type="number" min="0" max="24" value="${st.cutoff_h}"></div>
        <div><label class="f" for="s_hor">Горизонт записи, дней</label>
          <input class="t" id="s_hor" type="number" min="1" max="30" value="${st.horizon_d}"></div>
      </div>
      <div class="frow">
        <div><label class="f" for="s_capd">Лимит на слот — доставка</label>
          <input class="t" id="s_capd" type="number" min="1" value="${st.slot_capacity_delivery}"></div>
        <div><label class="f" for="s_capp">Лимит на слот — самовывоз</label>
          <input class="t" id="s_capp" type="number" min="1" value="${st.slot_capacity_pickup}"></div>
      </div>
      <div class="hint">Интервал слота фиксирован в ТЗ 4.4 — 2 часа. Часовой пояс Europe/Moscow.</div>
    </div>

    <div class="panel"><h3>Праздники и выходные</h3>
      <div class="hint">Даты из списка закрыты для доставки и самовывоза целиком —
        слот недоступен независимо от часа (ТЗ 4.4).</div>
      <div id="holidaysList"></div>
      <div class="frow">
        <div><label class="f" for="s_holiday_new">Добавить дату</label>
          <input class="t" id="s_holiday_new" type="date"></div>
        <div style="align-self:flex-end"><button class="btn gh sm" id="addHoliday">Добавить</button></div>
      </div>
    </div>

    <div class="panel"><h3>Предзаказ мяса</h3>
      <label class="f">Дни поставки</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap">${['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].map(d =>
        raw(h`<label class="day ${st.meat_days.includes(d) ? 'on' : ''}" style="cursor:pointer">
          <input type="checkbox" class="sr-only" data-meatday="${d}" ${st.meat_days.includes(d) ? 'checked' : ''}>${d}</label>`))}</div>
      <div class="frow">
        <div><label class="f" for="s_kg">Дневной объём, кг</label>
          <input class="t" id="s_kg" type="number" min="0" value="${st.meat_limit_kg}"></div>
        <div><label class="f" for="s_cd">Приём закрывается за, суток</label>
          <input class="t" id="s_cd" type="number" min="0" max="14" value="${st.meat_cutoff_days}"></div>
      </div>
    </div>

    <div class="panel"><h3>Контакты и реквизиты</h3>
      <label class="f" for="s_addr">Адрес самовывоза</label>
      <input class="t" id="s_addr" value="${st.pickup_address}" maxlength="300">
      <div class="frow">
        <div><label class="f" for="s_phone">Телефон</label>
          <input class="t" id="s_phone" value="${st.phone}" maxlength="40"></div>
        <div><label class="f" for="s_email">Email</label>
          <input class="t" id="s_email" value="${st.email || ''}" maxlength="254"></div>
      </div>
      <label class="f" for="s_req">Реквизиты (ТЗ 14.3)</label>
      <input class="t" id="s_req" value="${st.requisites}" maxlength="300">
      <div class="mfoot"><button class="btn" id="saveSettings">Сохранить настройки</button></div>
    </div>`;

  // Список дат-исключений редактируется локально и уходит на сервер
  // вместе с остальными настройками по кнопке «Сохранить настройки» —
  // как и meat_days рядом, а не отдельным маршрутом.
  let holidays = [...(st.holidays || [])].sort();
  function renderHolidays() {
    $('#holidaysList').innerHTML = holidays.length
      ? holidays.map(d => h`<div class="chiprow">
          <span>${d}</span>
          <button class="iconbtn" data-holiday-del="${d}" title="Убрать дату">✕</button>
        </div>`)
      : h`<div class="muted">Список пуст</div>`;
    $$('[data-holiday-del]').forEach(b => b.onclick = () => {
      holidays = holidays.filter(d => d !== b.dataset.holidayDel);
      renderHolidays();
    });
  }
  renderHolidays();
  $('#addHoliday').onclick = () => {
    const v = $('#s_holiday_new').value;
    if (!v) return;
    if (!holidays.includes(v)) { holidays.push(v); holidays.sort(); renderHolidays(); }
    $('#s_holiday_new').value = '';
  };

  $$('[data-zone]').forEach(b => b.onclick = async () => {
    const id = b.dataset.zone;
    try {
      await api('/api/admin/zones/' + id, { method: 'PUT',
        body: { cost: Number($('#zc_' + id).value), free_from: Number($('#zf_' + id).value) } });
      toast('Зона сохранена');
    } catch (e) { toast(errText(e), true); }
  });
  $$('[data-meatday]').forEach(cb => cb.onchange = () => {
    cb.closest('.day').classList.toggle('on', cb.checked);
  });
  $('#saveSettings').onclick = async () => {
    const body = {
      work_from: Number($('#s_from').value), work_to: Number($('#s_to').value),
      cutoff_h: Number($('#s_cut').value), horizon_d: Number($('#s_hor').value),
      slot_capacity_delivery: Number($('#s_capd').value),
      slot_capacity_pickup: Number($('#s_capp').value),
      pickup_address: $('#s_addr').value.trim(), phone: $('#s_phone').value.trim(),
      email: $('#s_email').value.trim(),
      meat_days: $$('[data-meatday]').filter(c => c.checked).map(c => c.dataset.meatday),
      meat_limit_kg: Number($('#s_kg').value), meat_cutoff_days: Number($('#s_cd').value),
      requisites: $('#s_req').value.trim(), holidays
    };
    try { await api('/api/admin/settings', { method: 'PUT', body }); toast('Настройки сохранены'); }
    catch (e) { toast(errText(e), true); }
  };
}

/* =====================================================================
   Сотрудники (ТЗ 10.8)
   ===================================================================== */
async function pageStaff() {
  const [{ staff }, { audit }] = await Promise.all([
    api('/api/admin/staff'), api('/api/admin/audit').catch(() => ({ audit: [] }))
  ]);
  S.data.staff = staff;
  $('#content').innerHTML = h`
    <div class="panel">
      <div class="ptools"><h3 style="flex:1;margin:0">Сотрудники</h3>
        <button class="btn sm" id="addStaff">Добавить</button></div>
      <div class="tbl-wrap"><table class="dt">
        <thead><tr><th>Имя</th><th>Логин</th><th>Телефон</th><th>Роль</th><th></th></tr></thead>
        <tbody>${staff.map(u => h`<tr>
          <td><b>${u.name}</b>${u.id === S.user.id ? raw(' <span class="badge gray">это вы</span>') : ''}</td>
          <td class="sku">${u.login}</td><td>${u.phone || '—'}</td>
          <td><span class="badge ${u.role === 'admin' ? 'gold' : 'gray'}">${u.role === 'admin' ? 'Администратор' : 'Менеджер'}</span></td>
          <td><div class="rowbtns">
            <button class="iconbtn" data-staffedit="${u.id}" aria-label="Изменить">✎</button>
            ${u.id === S.user.id ? '' : raw(h`<button class="iconbtn" data-staffdel="${u.id}" aria-label="Удалить">✕</button>`)}
          </div></td></tr>`)}</tbody></table></div>
      <div class="note">Менеджер видит только заказы и предзаказы. Товары, промокоды,
        настройки и сотрудники доступны администратору (ТЗ 10.8).</div>
    </div>
    <div class="panel"><h3>Журнал действий</h3>
      ${audit.length ? raw('<div class="hist">' + audit.slice(0, 40).map(a =>
        h`<div>${new Date(a.at).toLocaleString('ru-RU')} — <b>${a.actor}</b> ${a.action}
          ${a.details ? raw(h`<span class="muted">${a.details}</span>`) : ''}</div>`).join('') + '</div>')
        : raw('<p class="muted">Записей нет.</p>')}
    </div>`;

  $('#addStaff').onclick = () => staffModal(null);
  $$('[data-staffedit]').forEach(b => b.onclick = () => staffModal(+b.dataset.staffedit));
  $$('[data-staffdel]').forEach(b => b.onclick = async () => {
    if (!confirm('Удалить сотрудника? Его сессии будут завершены.')) return;
    try { await api('/api/admin/staff/' + b.dataset.staffdel, { method: 'DELETE' }); toast('Удалён'); await pageStaff(); }
    catch (e) { toast(errText(e), true); }
  });
}

function staffModal(id) {
  const u = id ? S.data.staff.find(x => x.id === id) : { name: '', login: '', phone: '', role: 'manager' };
  modal(h`<div class="shead"><h2>${id ? 'Сотрудник' : 'Новый сотрудник'}</h2>
      <button class="x" data-close="1">✕</button></div>
    <div class="sbody">
      <label class="f" for="st_name">Имя <span class="req">*</span></label>
      <input class="t" id="st_name" value="${u.name}" maxlength="120">
      <div class="frow">
        <div><label class="f" for="st_login">Логин <span class="req">*</span></label>
          <input class="t" id="st_login" value="${u.login || ''}" maxlength="64" autocomplete="off"></div>
        <div><label class="f" for="st_role">Роль <span class="req">*</span></label>
          <select class="t" id="st_role">
            <option value="manager" ${u.role === 'manager' ? 'selected' : ''}>Менеджер</option>
            <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Администратор</option>
          </select></div>
      </div>
      <label class="f" for="st_phone">Телефон</label>
      <input class="t" id="st_phone" value="${u.phone || ''}" maxlength="24">
      <label class="f" for="st_pw">Пароль ${id ? '' : raw('<span class="req">*</span>')}</label>
      <input class="t" id="st_pw" type="password" autocomplete="new-password" maxlength="200"
        placeholder="${id ? 'оставьте пустым, чтобы не менять' : ''}">
      <div class="hint">Минимум 10 символов, заглавная и строчная буквы, цифра.</div>
      <div class="err-msg" id="stErr" role="alert"></div>
      <div class="mfoot"><button class="btn gh" data-close="1">Отмена</button>
        <button class="btn" id="saveStaff">Сохранить</button></div>
    </div>`);
  $('#saveStaff').onclick = async () => {
    const body = {
      name: $('#st_name').value.trim(), login: $('#st_login').value.trim().toLowerCase(),
      phone: $('#st_phone').value.trim(), role: $('#st_role').value,
      password: $('#st_pw').value
    };
    try {
      await api(id ? '/api/admin/staff/' + id : '/api/admin/staff', { method: id ? 'PUT' : 'POST', body });
      closeModal(); toast('Сохранено'); await pageStaff();
    } catch (e) { $('#stErr').textContent = errText(e); }
  };
}

/* =====================================================================
   Конструктор главной страницы
   ===================================================================== */
async function pageHome() {
  const d = await api('/api/admin/home');
  S.data.home = d.home; S.data.skus = d.skus;
  paintHome();
}

function paintHome() {
  const c = S.data.home;
  const known = new Set(S.data.skus.map(s => s.sku));
  const badSale = c.sale_skus.filter(s => !known.has(s));
  const badMeat = c.meat_skus.filter(s => !known.has(s));

  $('#content').innerHTML = h`
    <div class="panel"><h3>Блоки главной страницы</h3>
      <div class="hint" style="margin-bottom:10px">Порядок и видимость блоков на витрине.</div>
      <div class="hb-list">${c.sections.map((s, i) => h`<div class="hb-row">
        <div class="hb-arrows">
          <button class="iconbtn" data-hup="${i}" ${i === 0 ? 'disabled' : ''} aria-label="Выше">↑</button>
          <button class="iconbtn" data-hdown="${i}" ${i === c.sections.length - 1 ? 'disabled' : ''} aria-label="Ниже">↓</button>
        </div>
        <div class="hb-name">${s.name}</div>
        <label class="sw"><input type="checkbox" ${s.is_visible ? 'checked' : ''} data-hvis="${i}"><span class="tr"></span></label>
      </div>`)}</div>
    </div>

    <div class="panel"><h3>Промо-блок</h3>
      <label class="f" for="hb_tag">Бейдж</label>
      <input class="t" id="hb_tag" value="${c.hero_tag}" maxlength="120">
      <label class="f" for="hb_title">Заголовок <span class="req">*</span></label>
      <input class="t" id="hb_title" value="${c.hero_title}" maxlength="120">
      <label class="f" for="hb_text">Текст</label>
      <textarea class="t" id="hb_text" maxlength="1000">${c.hero_text}</textarea>
    </div>

    <div class="panel"><h3>Товары по акции на главной</h3>
      <label class="f" for="hb_sale">Артикулы через запятую, в порядке показа</label>
      <input class="t" id="hb_sale" value="${c.sale_skus.join(', ')}">
      <div class="hint" style="${badSale.length ? 'color:var(--red)' : ''}">
        ${badSale.length ? 'Нет в каталоге: ' + badSale.join(', ') : 'Найдено в каталоге: ' + c.sale_skus.length}</div>
    </div>

    <div class="panel"><h3>Мясо по предзаказу на главной</h3>
      <label class="f" for="hb_meat">Артикулы через запятую</label>
      <input class="t" id="hb_meat" value="${c.meat_skus.join(', ')}">
      <div class="hint" style="${badMeat.length ? 'color:var(--red)' : ''}">
        ${badMeat.length ? 'Нет в каталоге: ' + badMeat.join(', ') : 'Найдено в каталоге: ' + c.meat_skus.length}</div>
    </div>

    <div class="mfoot" style="border:0">
      <a class="btn gh sm" href="/" target="_blank" rel="noopener" style="text-decoration:none;line-height:1.6">Посмотреть витрину</a>
      <button class="btn sm" id="saveHome">Сохранить главную</button>
    </div>`;

  $$('[data-hup],[data-hdown]').forEach(b => b.onclick = () => {
    const i = +(b.dataset.hup !== undefined ? b.dataset.hup : b.dataset.hdown);
    const j = b.dataset.hup !== undefined ? i - 1 : i + 1;
    [c.sections[i], c.sections[j]] = [c.sections[j], c.sections[i]];
    paintHome();
  });
  $$('[data-hvis]').forEach(el => el.onchange = () => { c.sections[+el.dataset.hvis].is_visible = el.checked; });

  $('#saveHome').onclick = async () => {
    const parse = v => v.split(',').map(s => s.trim()).filter(Boolean);
    const body = {
      hero_tag: $('#hb_tag').value.trim(), hero_title: $('#hb_title').value.trim(),
      hero_text: $('#hb_text').value.trim(),
      sale_skus: parse($('#hb_sale').value), meat_skus: parse($('#hb_meat').value),
      sections: c.sections.map(s => ({ id: s.id, is_visible: s.is_visible }))
    };
    try {
      const r = await api('/api/admin/home', { method: 'PUT', body });
      S.data.home = r.home; toast('Главная страница сохранена'); paintHome();
    } catch (e) {
      toast(e.data && e.data.skus ? 'Нет в каталоге: ' + e.data.skus.join(', ') : errText(e), true);
    }
  };
}

/* =====================================================================
   Вход и запуск
   ===================================================================== */
function showLogin(msg) {
  $('#shell').hidden = true;
  $('#login').hidden = false;
  if (msg) $('#loginErr').textContent = msg;
  const f = $('#loginInput'); if (f) f.focus();
}

$('#loginForm').onsubmit = async e => {
  e.preventDefault();
  const btn = $('#loginBtn');
  btn.disabled = true; $('#loginErr').textContent = '';
  try {
    const r = await api('/api/admin/login', { method: 'POST',
      body: { login: $('#loginInput').value.trim(), password: $('#password').value } });
    S.user = r.user; S.perms = new Set(r.permissions);
    $('#password').value = '';
    await start();
  } catch (err) {
    $('#loginErr').textContent = err.status === 429
      ? 'Слишком много попыток. Подождите 15 минут.'
      : 'Неверный логин или пароль';
  } finally { btn.disabled = false; }
};

$('#logoutBtn').onclick = async () => {
  await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
  S.user = null; S.perms = new Set();
  showLogin('Вы вышли из панели');
};

/* Кнопки «Назад» и «Вперёд» меняют hash — без этого слушателя адрес
   расходился бы с показанным разделом (ТЗ 15.1). */
window.addEventListener('hashchange', () => {
  if (!S.user) return;
  const id = location.hash.slice(1);
  if (!id || id === S.page) return;
  const item = NAV.find(n => n.id === id && can(n.perm));
  if (item) go(item.id);
});

async function start() {
  $('#login').hidden = true;
  $('#shell').hidden = false;
  $('#whoName').textContent = S.user.name;
  $('#whoRole').textContent = S.user.role === 'admin' ? 'Администратор' : 'Менеджер';
  PHOTOS = await fetch('/photos.json').then(r => r.json()).catch(() => ({}));

  const hash = location.hash.slice(1);
  const first = NAV.find(n => n.id === hash && can(n.perm)) || NAV.find(n => can(n.perm));
  go(first ? first.id : 'orders');
}

(async function boot() {
  try {
    const me = await api('/api/admin/me');
    S.user = me.user; S.perms = new Set(me.permissions);
    await start();
  } catch { showLogin(); }
})();

window.closeModal = closeModal;
