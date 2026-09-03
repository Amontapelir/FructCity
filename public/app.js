/* =====================================================================
   FructCity — витрина.

   Правило номер один: никакие данные с сервера не попадают в разметку
   без экранирования. Названия товаров, адреса и комментарии редактируются
   людьми, поэтому считаются недоверенными — для них есть esc() и h``.
   ===================================================================== */
'use strict';

/* ---------------------------------------------------------------------
   Экранирование и безопасный шаблон.
   h`<b>${name}</b>` экранирует ВСЕ подстановки автоматически. Если
   значение нужно вставить как готовую разметку — оборачиваем в raw().
   --------------------------------------------------------------------- */
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ESC[c]);
}
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
  if (v[RAW] !== undefined) return v[RAW];           // уже разметка
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

const $ = sel => document.querySelector(sel);
const $$ = sel => Array.from(document.querySelectorAll(sel));
const rub = n => (Math.round(Number(n) || 0)).toLocaleString('ru-RU') + ' ₽';

/* ---------------------------------------------------------------------
   Транспорт. CSRF-токен подставляется автоматически.
   --------------------------------------------------------------------- */
function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)fc_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: Object.assign(
      opts.body ? { 'Content-Type': 'application/json' } : {},
      { 'X-CSRF-Token': csrf() },
      opts.headers || {}),
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: 'same-origin'
  });
  let data = null;
  try { data = await res.json(); } catch { data = {}; }
  if (!res.ok) {
    const err = new Error(data.error || 'request_failed');
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

/* Человеческие формулировки для кодов ошибок сервера. */
const ERRORS = {
  out_of_stock: 'Товара нет в наличии',
  items_unavailable: 'Часть товаров разобрали, пока вы оформляли заказ',
  cart_empty: 'Корзина пуста',
  slot_unavailable: 'Этот интервал только что заняли — выберите другой',
  slot_not_found: 'Интервал недоступен',
  zone_manual_quote: 'В этот район доставку рассчитывает менеджер — позвоните нам',
  promo_not_found: 'Промокод не найден',
  promo_inactive: 'Промокод неактивен',
  promo_expired: 'Срок действия промокода истёк',
  promo_exhausted: 'Промокод исчерпан',
  promo_user_limit: 'Вы уже использовали этот промокод',
  consent_required: 'Нужно согласие на обработку данных',
  code_invalid: 'Неверный код',
  code_expired: 'Срок действия кода истёк — запросите новый',
  code_not_requested: 'Сначала запросите код',
  too_many_attempts: 'Слишком много попыток. Подождите немного',
  too_many_codes: 'Слишком часто. Подождите 15 минут',
  rate_limited: 'Слишком много запросов. Подождите немного',
  daily_limit_exceeded: 'На эту дату объём мяса уже разобран',
  date_closed: 'Приём заявок на эту дату закрыт',
  cancel_not_allowed: 'Заказ уже в сборке — отмена через менеджера',
  forbidden: 'Нет доступа',
  validation_failed: 'Проверьте заполнение полей',
  csrf_failed: 'Сессия устарела, обновите страницу',
  link_expired: 'Ссылка привязки устарела — создайте новую',
  link_already_used: 'Эта ссылка уже использована',
  chat_already_linked: 'Этот Telegram уже привязан к другому профилю',
  telegram_not_configured: 'Привязка Telegram пока не настроена'
};
const msgOf = e => ERRORS[e && (e.message || e)] || 'Что-то пошло не так';

/* ---------------------------------------------------------------------
   Состояние
   --------------------------------------------------------------------- */
const S = {
  boot: null, products: [], total: 0, cart: null,
  category: 'all', search: '', sort: 'pop', inStock: false, onSale: false,
  priceMin: '', priceMax: '',
  route: 'home', slug: null, user: null, loading: false
};

/* ---------------------------------------------------------------------
   Фото товара. Ключ приходит с сервера, URL собираем на клиенте.
   При обрыве ссылки подставляется эмодзи — без «битой картинки».
   --------------------------------------------------------------------- */
/**
 * Декоративная иллюстрация тёмного блока: гравюра в один цвет.
 *
 * Лежит в public/img, отдаётся со своего домена — политика безопасности
 * (img-src 'self') это разрешает. Фон у файлов прозрачный: цвет несёт
 * только штрих, поэтому рисунок ложится на градиент без видимого края
 * прямоугольника. Если файл не загрузится, останется пустой блок с
 * градиентом — он выглядит нормально и сам по себе.
 */
const ART = {
  storefront: { src: '/img/shop.png', w: 1000, h: 529 },
  butcher: { src: '/img/meat.png', w: 815, h: 614 }
};
function art(name, eager) {
  const a = ART[name];
  if (!a) return '';
  return h`<img src="${a.src}" alt="" width="${a.w}" height="${a.h}"
    decoding="async"${eager ? raw('') : raw(' loading="lazy"')}>`;
}

const PHOTO_BASE = 'https://commons.wikimedia.org/wiki/Special:FilePath/';
let PHOTOS = {};
function photo(p, width) {
  const file = PHOTOS[p.image_key];
  const emoji = p.emoji || '📦';
  if (!file) return h`<span class="e" aria-hidden="true">${emoji}</span>`;
  const url = PHOTO_BASE + encodeURIComponent(file) + (width ? '?width=' + width : '');
  return h`<img src="${url}" alt="${p.name}" loading="lazy" decoding="async" data-emoji="${emoji}">`;
}

/* Подмена битой картинки на эмодзи.

   Раньше это делал атрибут onerror прямо в разметке — и не работал:
   CSP запрещает инлайновые обработчики (script-src 'self' без
   'unsafe-inline'). Браузер молча блокировал обработчик, и вместо
   эмодзи в карточке оставался значок битого файла и alt-текст во всю
   плитку. Поэтому слушаем событие здесь, в файле скрипта.

   Событие error не всплывает, но проходит фазу перехвата — один
   слушатель на документе покрывает все картинки, включая отрисованные
   позже. */
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

/**
 * Картинка могла не загрузиться до того, как разметку вставили в
 * документ (например, взялась из кеша с ошибкой) — тогда события error
 * уже не будет. Проверяем такие явно после каждой отрисовки.
 */
function sweepPhotos(root) {
  const scope = root && root.querySelectorAll ? root : document;
  scope.querySelectorAll('img[data-emoji]').forEach(img => {
    if (img.complete && img.naturalWidth === 0) emojiFallback(img);
  });
}

/* Разметку перерисовывают в нескольких местах; вместо вызова обхода
   после каждого — один наблюдатель за контейнерами страницы. */
document.addEventListener('DOMContentLoaded', () => {
  if (typeof MutationObserver !== 'function') return;
  const obs = new MutationObserver(() => sweepPhotos(document));
  ['#view', '#sheet'].forEach(sel => {
    const el = document.querySelector(sel);
    if (el) obs.observe(el, { childList: true, subtree: true });
  });
});

/* ---------------------------------------------------------------------
   Тост
   --------------------------------------------------------------------- */
let toastTimer;
function toast(msg, isError) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, 2600);
}

/* ---------------------------------------------------------------------
   Модальные окна с ловушкой фокуса (ТЗ 15.4)
   --------------------------------------------------------------------- */
let lastFocused = null;
function showSheet(html) {
  lastFocused = document.activeElement;
  $('#sheet').innerHTML = html;
  $('#ov').classList.add('show');
  document.body.style.overflow = 'hidden';
  const first = $('#sheet').querySelector('input,button,select,textarea,[tabindex]');
  if (first) first.focus();
}
function closeSheet() {
  $('#ov').classList.remove('show');
  document.body.style.overflow = '';
  $('#sheet').innerHTML = '';
  if (lastFocused && lastFocused.focus) lastFocused.focus();
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#ov').classList.contains('show')) closeSheet();
  if (e.key === 'Tab' && $('#ov').classList.contains('show')) {
    const f = $$('#sheet input:not([disabled]),#sheet button:not([disabled]),#sheet select,#sheet textarea,#sheet a[href]');
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});

/* =====================================================================
   Роутер
   ===================================================================== */
function navigate(path, replace) {
  if (replace) history.replaceState({}, '', path);
  else history.pushState({}, '', path);
  render();
}
window.addEventListener('popstate', () => render());

/* Кнопка «Обновить» на экране обрыва связи. Инлайновый onclick
   заблокировала бы политика безопасности. */
document.addEventListener('click', e => {
  if (e.target.closest && e.target.closest('[data-reload]')) location.reload();
});

document.addEventListener('click', e => {
  const nav = e.target.closest('[data-nav]');
  if (nav) { e.preventDefault(); navigate(nav.dataset.nav); return; }
  const link = e.target.closest('a[href^="/"]');
  if (link && !link.target && !link.hasAttribute('download')) {
    const url = new URL(link.href);
    if (url.pathname.startsWith('/api/') || url.pathname === '/admin' ||
        url.pathname.endsWith('.xml') || url.pathname.endsWith('.txt')) return;
    e.preventDefault();
    navigate(url.pathname);
  }
});

function parseRoute() {
  const p = location.pathname;
  if (p === '/') return { route: 'home' };
  if (p === '/cart') return { route: 'cart' };
  if (p === '/checkout') return { route: 'checkout' };
  if (p === '/profile') return { route: 'profile' };
  if (p === '/login') return { route: 'login' };
  if (p === '/policy') return { route: 'policy' };
  if (p === '/offer') return { route: 'offer' };
  // сервер отдаёт SPA-оболочку и на /preorder — без этой строки прямой
  // заход по ссылке показывал «страница не найдена»
  if (p === '/preorder') return { route: 'preorder' };
  let m = p.match(/^\/product\/(.+?)\/?$/);
  if (m) return { route: 'product', slug: decodeURIComponent(m[1]) };
  m = p.match(/^\/catalog(?:\/([^/]+))?\/?$/);
  if (m) return { route: 'catalog', category: m[1] ? decodeURIComponent(m[1]) : 'all' };
  return { route: 'notfound' };
}

async function render() {
  const r = parseRoute();
  S.route = r.route;
  if (r.category) S.category = r.category;
  if (r.slug) S.slug = r.slug;
  window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });

  const view = $('#view');
  switch (r.route) {
    case 'home': return renderHome(view);
    case 'catalog': return renderCatalog(view);
    case 'product': return renderProduct(view, r.slug);
    case 'cart': return renderCartPage(view);
    case 'checkout': return renderCheckout(view);
    case 'profile': return renderProfile(view);
    case 'login': return renderLogin(view);
    case 'policy': return renderLegal(view, 'policy');
    case 'offer': return renderLegal(view, 'offer');
    case 'preorder': return renderPreorderPage(view);
    default:
      view.innerHTML = h`<div class="wrap legal"><h1>Страница не найдена</h1>
        <p><a href="/catalog">Перейти в каталог</a></p></div>`;
  }
}

/* =====================================================================
   Главная (ТЗ — блоки из брифа)
   ===================================================================== */
async function renderHome(view) {
  const b = S.boot;
  const cfg = b.home;
  const visible = id => (cfg.sections || []).some(s => s.id === id && s.is_visible);

  view.innerHTML = h`<div id="home" class="wrap">
    ${visible('hero') ? raw(h`
    <section class="hero">
      <div class="hero-card">
        <span class="art" aria-hidden="true">${art('storefront', true)}</span>
        <div class="hero-tx">
        ${cfg.hero_tag ? raw(h`<span class="tag sale" style="position:static;display:inline-flex;margin-bottom:10px">${cfg.hero_tag}</span>`) : ''}
        <h1>${cfg.hero_title}</h1>
        <p>${cfg.hero_text}</p>
        <div class="hero-btns">
          <button class="btn gold sm" id="heroMeat">Оформить предзаказ</button>
          <button class="btn ghost sm" style="color:#fff;border-color:rgba(255,255,255,.3)" data-nav="/catalog">Смотреть каталог</button>
        </div>
      </div></div>
    </section>`) : ''}

    ${visible('feat') ? raw(h`
    <section><div class="feat-grid">
      <div class="feat"><b>2 000+</b><span>товаров в каталоге</span></div>
      <div class="feat"><b>2 часа</b><span>доставка по району</span></div>
      <div class="feat"><b>54-ФЗ</b><span>электронный чек</span></div>
      <div class="feat"><b>Честный вес</b><span>весовые товары — по фактическому весу</span></div>
    </div></section>`) : ''}

    ${visible('cats') ? raw(h`
    <section><h2 class="h-ttl">Категории товаров</h2>
      <nav class="h-cats" aria-label="Категории">
        <a class="h-cat" href="/catalog"><span class="ico" aria-hidden="true">🗂️</span>Все товары</a>
        ${b.categories.map(c => raw(h`<a class="h-cat" href="/catalog/${c.id}"><span class="ico" aria-hidden="true">${c.emoji}</span>${c.name}</a>`))}
      </nav></section>`) : ''}

    ${visible('sale') ? raw(h`
    <section><h2 class="h-ttl">Товары по акции</h2><div class="grid" id="homeSale"></div></section>`) : ''}

    ${visible('meat') ? raw(h`
    <section class="h-meat" id="meatBlock">
      <span class="art" aria-hidden="true">${art('butcher')}</span>
      <h2 class="h-ttl">Мясо, которое режут под вас</h2>
      <p class="h-meat-p">Поставки два раза в неделю, объём на день ограничен. Можно оформить
        предзаказ заранее — оплата по фактическому весу при выдаче.</p>
      <div class="grid" id="homeMeat"></div>
    </section>`) : ''}

    ${visible('info') ? raw(h`
    <section><h2 class="h-ttl">Доставка, самовывоз и оплата</h2>
      <div class="info-grid">
        <div class="info-card"><h4>Доставка</h4><p>Южное Бутово и Коммунарка — 200 ₽,
          бесплатно от 2 000 ₽. Доставим в выбранный двухчасовой интервал.</p></div>
        <div class="info-card"><h4>Самовывоз</h4><p>${b.shop.pickup_address}. Ежедневно
          ${String(b.shop.work_from).padStart(2, '0')}:00–${String(b.shop.work_to).padStart(2, '0')}:00.</p></div>
        <div class="info-card"><h4>Оплата</h4><p>Наличными или картой курьеру. Электронный
          чек по 54-ФЗ приходит на email или SMS.</p></div>
      </div></section>`) : ''}
  </div>`;

  const heroBtn = $('#heroMeat');
  if (heroBtn) heroBtn.onclick = () => {
    const m = $('#meatBlock');
    if (m) m.scrollIntoView({ behavior: 'smooth' });
    else navigate('/catalog/meat');
  };

  // подборки берём с сервера по SKU из конструктора главной
  if (visible('sale')) fillBySku('#homeSale', cfg.sale_skus);
  if (visible('meat')) fillBySku('#homeMeat', cfg.meat_skus);
}

async function fillBySku(sel, skus) {
  const el = $(sel);
  if (!el || !skus || !skus.length) return;
  el.innerHTML = skus.map(() => '<div class="card skel skel-card"></div>').join('');
  const found = await Promise.all(skus.map(s =>
    api('/api/products/' + encodeURIComponent(s)).then(r => r.product).catch(() => null)));
  const list = found.filter(Boolean);
  el.innerHTML = list.length ? list.map(cardHtml).join('')
    : h`<div class="empty">Подборка пуста</div>`;
}

/* =====================================================================
   Каталог
   ===================================================================== */
async function renderCatalog(view) {
  const b = S.boot;
  view.innerHTML = h`<div class="wrap"><div class="layout">
    <aside class="catnav">
      <div class="catnav-h">Категории</div>
      <nav class="cats" id="cats" aria-label="Категории товаров"></nav>
    </aside>
    <div class="content">
      <div class="bar">
        <h1 id="catTitle">Каталог <span class="n" id="catCount"></span></h1>
        <label class="sr-only" for="sortSel">Сортировка</label>
        <select class="sel" id="sortSel">
          <option value="pop">По популярности</option>
          <option value="asc">Сначала дешевле</option>
          <option value="desc">Сначала дороже</option>
        </select>
        <label class="tog"><input type="checkbox" id="fIn">В наличии</label>
        <label class="tog"><input type="checkbox" id="fSale">Со скидкой</label>
      </div>
      <div class="pricebar" role="group" aria-label="Фильтр по цене">
        <span class="pricebar-l">Цена, ₽</span>
        <label class="sr-only" for="pMin">Цена от</label>
        <input class="t pnum" id="pMin" type="number" min="0" inputmode="numeric" placeholder="от">
        <span aria-hidden="true">—</span>
        <label class="sr-only" for="pMax">Цена до</label>
        <input class="t pnum" id="pMax" type="number" min="0" inputmode="numeric" placeholder="до">
        <button class="btn ghost sm" id="pReset" hidden>Сбросить</button>
      </div>
      <div class="grid" id="grid"></div>
    </div>
  </div></div>`;

  $('#cats').innerHTML = h`<a class="chip ${S.category === 'all' ? 'on' : ''}" href="/catalog">
      <span class="ico" aria-hidden="true">🗂️</span>Все товары</a>` +
    b.categories.map(c => h`<a class="chip ${c.id === 'sale' ? 'sale' : ''} ${S.category === c.id ? 'on' : ''}"
      href="/catalog/${c.id}"><span class="ico" aria-hidden="true">${c.emoji}</span>${c.name}</a>`).join('');

  $('#sortSel').value = S.sort;
  $('#fIn').checked = S.inStock;
  $('#fSale').checked = S.onSale;
  $('#sortSel').onchange = e => { S.sort = e.target.value; loadProducts(); };
  $('#fIn').onchange = e => { S.inStock = e.target.checked; loadProducts(); };
  $('#fSale').onchange = e => { S.onSale = e.target.checked; loadProducts(); };

  /* Фильтр по цене (ТЗ 2.1.3). Ввод с задержкой, чтобы не слать запрос
     на каждую цифру; порядок границ поправляем молча — так предсказуемее,
     чем показывать ошибку. */
  const pMin = $('#pMin'), pMax = $('#pMax'), pReset = $('#pReset');
  pMin.value = S.priceMin || '';
  pMax.value = S.priceMax || '';
  let priceTimer;
  const applyPrice = () => {
    let lo = pMin.value === '' ? '' : Math.max(0, Number(pMin.value));
    let hi = pMax.value === '' ? '' : Math.max(0, Number(pMax.value));
    if (lo !== '' && hi !== '' && lo > hi) { [lo, hi] = [hi, lo]; pMin.value = lo; pMax.value = hi; }
    S.priceMin = lo; S.priceMax = hi;
    pReset.hidden = lo === '' && hi === '';
    clearTimeout(priceTimer);
    priceTimer = setTimeout(loadProducts, 350);
  };
  pMin.oninput = applyPrice;
  pMax.oninput = applyPrice;
  pReset.onclick = () => {
    pMin.value = ''; pMax.value = '';
    S.priceMin = ''; S.priceMax = '';
    pReset.hidden = true;
    loadProducts();
  };
  pReset.hidden = !(S.priceMin || S.priceMax);

  const cat = b.categories.find(c => c.id === S.category);
  $('#catTitle').firstChild.textContent = (cat ? cat.name : 'Каталог') + ' ';
  await loadProducts();
}

/* Счётчик поколений: при быстром вводе в поиске ответы возвращаются не в том
   порядке, в каком уходили. Показываем только результат последнего запроса. */
let productsGen = 0;

async function loadProducts() {
  const grid = $('#grid');
  if (!grid) return;
  const gen = ++productsGen;
  grid.innerHTML = Array.from({ length: 8 }, () => '<div class="card skel skel-card"></div>').join('');

  const qs = new URLSearchParams({ category: S.category, sort: S.sort, limit: '200' });
  if (S.search) qs.set('q', S.search);
  if (S.inStock) qs.set('in_stock', '1');
  if (S.onSale) qs.set('on_sale', '1');
  if (S.priceMin) qs.set('price_min', S.priceMin);
  if (S.priceMax) qs.set('price_max', S.priceMax);

  try {
    const data = await api('/api/products?' + qs);
    if (gen !== productsGen) return;   // пришёл устаревший ответ
    S.products = data.items; S.total = data.total;
    const cnt = $('#catCount');
    if (cnt) cnt.textContent = data.total ? '· ' + data.total : '';
    grid.innerHTML = data.items.length
      ? data.items.map(cardHtml).join('')
      : h`<div class="empty"><div class="em" aria-hidden="true">🔍</div>Ничего не нашлось<br>
          <small>Попробуйте изменить запрос или фильтры</small></div>`;
  } catch (e) {
    if (gen !== productsGen) return;
    grid.innerHTML = h`<div class="empty">Не удалось загрузить каталог.
      <button class="btn sm" id="retryCatalog">Повторить</button></div>`;
    const rb = $('#retryCatalog'); if (rb) rb.onclick = () => loadProducts();
  }
}

function cardHtml(p) {
  const tags = [];
  if (p.is_sale) tags.push(h`<span class="tag sale">Акция</span>`);
  if (p.type === 'preorder') tags.push(h`<span class="tag pre">Предзаказ</span>`);
  if (!p.in_stock) tags.push(h`<span class="tag out">Нет в наличии</span>`);

  const price = p.is_sale
    ? h`<b class="sale">${rub(p.sale_price)}</b><span class="old">${rub(p.base_price)}</span>`
    : h`<b>${rub(p.unit_price)}</b>`;

  let btn;
  if (p.type === 'preorder') btn = h`<button class="add green" data-pre="${p.id}">Предзаказать</button>`;
  else if (!p.in_stock) btn = h`<button class="add" disabled>Нет в наличии</button>`;
  else btn = h`<button class="add" data-add="${p.id}">В корзину</button>`;

  return h`<article class="card ${p.in_stock ? '' : 'out'}">
    <a href="/product/${p.slug}" style="text-decoration:none;color:inherit;display:flex;flex-direction:column;flex:1">
      <span class="thumb">${photo(p, 300)}<span class="tags">${tags.map(raw)}</span></span>
      <span class="nm">${p.name}</span>
      <span class="unit">${p.type === 'unit' ? 'за штуку' : 'за кг'}${p.in_stock ? h` · остаток ${p.stock}` : ''}</span>
      <span class="pr">${raw(price)}</span>
    </a>
    ${raw(btn)}</article>`;
}

/* Делегирование кликов по кнопкам карточек. */
document.addEventListener('click', async e => {
  const add = e.target.closest('[data-add]');
  if (add) {
    e.preventDefault(); e.stopPropagation();
    add.disabled = true;
    try {
      const cart = await api('/api/cart', { method: 'POST', body: { product_id: +add.dataset.add, qty: 1, weight: 1 } });
      updateCartCount(cart.count);
      toast('Добавлено в корзину');
    } catch (err) { toast(msgOf(err), true); }
    finally { add.disabled = false; }
    return;
  }
  const pre = e.target.closest('[data-pre]');
  if (pre) { e.preventDefault(); e.stopPropagation(); openPreorder(+pre.dataset.pre); }
});

function updateCartCount(n) {
  $('#cartCount').textContent = n;
  if (S.cart) S.cart.count = n;
}

/* =====================================================================
   Карточка товара
   ===================================================================== */
async function renderProduct(view, slug) {
  view.innerHTML = h`<div class="wrap" style="padding:24px 0"><div class="card skel" style="height:320px"></div></div>`;
  let p;
  try { p = (await api('/api/products/' + encodeURIComponent(slug))).product; }
  catch { view.innerHTML = h`<div class="wrap legal"><h1>Товар не найден</h1>
    <p><a href="/catalog">Перейти в каталог</a></p></div>`; return; }

  const price = p.is_sale
    ? h`<b class="sale">${rub(p.sale_price)}</b><span class="old">${rub(p.base_price)}</span>`
    : h`<b>${rub(p.unit_price)}</b>`;

  view.innerHTML = h`<div class="wrap" style="padding:22px 0 50px;max-width:860px">
    <p style="font-size:12.5px;color:var(--ink-3)"><a href="/catalog">Каталог</a> ›
      <a href="/catalog/${p.category_id}">${(S.boot.categories.find(c => c.id === p.category_id) || {}).name || ''}</a></p>
    <div class="big" style="margin:16px 0">
      <span class="ph" style="width:180px;height:180px;font-size:64px">${photo(p, 400)}</span>
      <div style="flex:1">
        <h1 style="font-size:24px;margin:0 0 8px">${p.name}</h1>
        <p class="pr" style="margin:0 0 6px">${raw(price)}
          <span style="font-size:12px;color:var(--ink-3)">/ ${p.type === 'unit' ? 'шт' : 'кг'}</span></p>
        <p class="unit">Артикул ${p.sku} · ${p.in_stock ? 'остаток ' + p.stock : 'нет в наличии'} · НДС ${p.vat_rate}%</p>
        <div id="prodAction"></div>
      </div>
    </div>
    <p class="dsc">${p.description}</p>
    ${p.type === 'weighted' ? raw(h`<div class="note">Весовой товар. Вы заказываете примерный вес —
      сборщик взвесит и уточнит цену. Допустимое отклонение ±10%, итоговая сумма может немного отличаться.</div>`) : ''}
    ${p.type === 'preorder' ? raw(h`<div class="note warn">Мясо доступно только по предзаказу.
      Поставка два раза в неделю — выберите дату получения в форме предзаказа.</div>`) : ''}
    ${!p.in_stock && p.type !== 'preorder' ? raw(h`<div class="note err">Товара сейчас нет.
      Карточка остаётся доступной, чтобы не терялась ссылка. Поступление ожидается в ближайшие дни.</div>`) : ''}
  </div>`;

  const act = $('#prodAction');
  if (p.type === 'preorder') {
    act.innerHTML = h`<button class="btn" style="max-width:280px" data-pre="${p.id}">Оформить предзаказ</button>`;
  } else if (!p.in_stock) {
    act.innerHTML = h`<button class="btn" style="max-width:280px" disabled>Нет в наличии</button>`;
  } else if (p.type === 'weighted') {
    // группа кнопок, а не одно поле — label бы указывал в пустоту (ТЗ 15.4)
    act.innerHTML = h`<div style="max-width:320px">
      <span class="f" id="wLabel">Примерный вес</span>
      <div class="wq" id="wq" role="group" aria-labelledby="wLabel">${[0.5, 1, 1.5, 2].map(w =>
        raw(h`<button type="button" data-w="${w}" class="${w === 1 ? 'on' : ''}"
          aria-pressed="${w === 1}">${w} кг</button>`))}</div>
      <button class="btn" id="addW">Добавить · <span id="wPrice">${rub(p.unit_price)}</span></button></div>`;
    let picked = 1;
    $$('#wq button').forEach(b => b.onclick = () => {
      picked = +b.dataset.w;
      $$('#wq button').forEach(x => {
        x.classList.toggle('on', x === b);
        x.setAttribute('aria-pressed', String(x === b));
      });
      $('#wPrice').textContent = rub(p.unit_price * picked);
    });
    $('#addW').onclick = async () => {
      try {
        const c = await api('/api/cart', { method: 'POST', body: { product_id: p.id, weight: picked } });
        updateCartCount(c.count); toast('Добавлено в корзину');
      } catch (e) { toast(msgOf(e), true); }
    };
  } else {
    act.innerHTML = h`<button class="btn" style="max-width:280px" data-add="${p.id}">Добавить в корзину</button>`;
  }
}

/* =====================================================================
   Корзина (ТЗ 4.1)
   ===================================================================== */
async function loadCart() { S.cart = await api('/api/cart'); updateCartCount(S.cart.count); return S.cart; }

async function renderCartPage(view) {
  view.innerHTML = h`<div class="wrap" style="padding:24px 0"><div class="card skel" style="height:240px"></div></div>`;
  const c = await loadCart();
  view.innerHTML = h`<div class="wrap" style="padding:22px 0 50px;max-width:720px">
    <h1 style="font-size:22px;margin:0 0 14px">Корзина ${c.count ? raw(h`<span class="n" style="color:var(--ink-3);font-size:14px;font-weight:400">· ${c.count}</span>`) : ''}</h1>
    <div id="cartBody"></div></div>`;
  paintCart(c, $('#cartBody'), true);
}

function paintCart(c, el, isPage) {
  if (!c.count) {
    el.innerHTML = h`<div class="empty"><div class="em" aria-hidden="true">🛒</div>Корзина пуста<br>
      <small>Добавьте товары из каталога</small><br>
      <button class="btn sm" style="margin-top:14px" data-nav="/catalog">За покупками</button></div>`;
    return;
  }
  const rows = c.items.map(l => {
    const p = l.product;
    const ctl = p.type === 'unit'
      ? h`<div class="mini"><button data-q="${p.id}" data-d="-1" aria-label="Уменьшить">−</button>
          <span>${l.qty} шт</span><button data-q="${p.id}" data-d="1" aria-label="Увеличить">+</button></div>`
      : h`<div class="mini"><button data-w2="${p.id}" data-d="-0.5" aria-label="Уменьшить">−</button>
          <span>~${l.weight} кг</span><button data-w2="${p.id}" data-d="0.5" aria-label="Увеличить">+</button></div>`;
    const warn = l.unavailable ? h`<small style="color:var(--red)">товара нет — удалите позицию</small>`
      : l.insufficient ? h`<small style="color:var(--red)">осталось только ${l.available}</small>` : '';
    return h`<div class="ci">
      <span class="ph">${photo(p, 100)}</span>
      <div class="in"><b>${p.name}</b>
        <small>${rub(p.unit_price)} / ${p.type === 'unit' ? 'шт' : 'кг'}${p.is_sale ? ' · акция' : ''}</small>
        ${raw(warn)}</div>
      <div class="rt"><b>${p.type === 'weighted' ? '≈' : ''}${rub(l.total)}</b>${raw(ctl)}</div>
    </div>`;
  }).join('');

  el.innerHTML = rows + h`
    <div class="promo">
      <label class="sr-only" for="promoInput">Промокод</label>
      <input class="t" id="promoInput" placeholder="Промокод" maxlength="24" value="${c.promo_code || ''}">
      <button id="promoBtn">Применить</button>
    </div>
    ${c.promo_error ? raw(h`<div class="note err">${c.promo_error}</div>`) : ''}
    ${c.promo_code && !c.promo_error ? raw(h`<div class="note">Промокод ${c.promo_code} применён${c.promo_note ? ' — ' + esc(c.promo_note) : ''}.
      <button class="btn ghost sm" id="promoDel" style="margin-left:8px">Убрать</button></div>`) : ''}
    ${c.has_weighted ? raw(h`<div class="note">В корзине есть весовые товары. Итог уточнится после
      взвешивания при сборке — допустимое отклонение ±10%.</div>`) : ''}
    ${c.blocking.length ? raw(h`<div class="note err">Нельзя оформить: ${c.blocking.join(', ')}.</div>`) : ''}
    <div class="sum">
      <div class="row"><span>Товары</span><span>${rub(c.items_total)}</span></div>
      ${c.discount ? raw(h`<div class="row disc"><span>Скидка</span><span>−${rub(c.discount)}</span></div>`) : ''}
      <div class="row tot"><span>Итого</span><span>${rub(c.total)}</span></div>
    </div>
    <button class="btn" id="toCheckout" ${c.blocking.length ? 'disabled' : ''}>Оформить заказ</button>
    ${isPage ? raw(h`<button class="btn ghost" data-nav="/catalog">Продолжить покупки</button>`) : ''}`;

  el.querySelectorAll('[data-q]').forEach(b => b.onclick = async () => {
    const line = c.items.find(l => l.product.id === +b.dataset.q);
    await changeCart({ product_id: +b.dataset.q, qty: line.qty + (+b.dataset.d) }, el, isPage);
  });
  el.querySelectorAll('[data-w2]').forEach(b => b.onclick = async () => {
    const line = c.items.find(l => l.product.id === +b.dataset.w2);
    await changeCart({ product_id: +b.dataset.w2, weight: Math.round((line.weight + (+b.dataset.d)) * 10) / 10 }, el, isPage);
  });
  const pb = el.querySelector('#promoBtn');
  if (pb) pb.onclick = async () => {
    const code = el.querySelector('#promoInput').value.trim();
    if (!code) return;
    try {
      const nc = await api('/api/cart/promo', { method: 'POST', body: { code } });
      S.cart = nc; paintCart(nc, el, isPage); toast('Промокод применён');
    } catch (e) { toast(msgOf(e), true); }
  };
  const pd = el.querySelector('#promoDel');
  if (pd) pd.onclick = async () => {
    const nc = await api('/api/cart/promo', { method: 'DELETE' });
    S.cart = nc; paintCart(nc, el, isPage);
  };
  el.querySelector('#toCheckout').onclick = () => { closeSheet(); navigate('/checkout'); };
}

async function changeCart(body, el, isPage) {
  try {
    const c = await api('/api/cart', { method: 'PUT', body });
    S.cart = c; updateCartCount(c.count); paintCart(c, el, isPage);
  } catch (e) { toast(msgOf(e), true); }
}

async function openCartSheet() {
  const c = await loadCart();
  showSheet(h`<div class="shead"><h2 id="sheetTitle">Корзина</h2>
    <button class="x" id="closeSheet" aria-label="Закрыть">✕</button></div>
    <div class="sbody" id="cartSheetBody"></div>`);
  paintCart(c, $('#cartSheetBody'), false);
  $('#closeSheet').onclick = closeSheet;
}

/* =====================================================================
   Чекаут (ТЗ 4.3)
   ===================================================================== */
const CO = {
  step: 1, method: null, zone_id: null, address: '', slot_ymd: null, slot_from: null,
  name: '', phone: '', email: '', comment: '', payment: null,
  consent: false, marketing_consent: false, telegram_optin: false
};

async function renderCheckout(view) {
  const c = await loadCart();
  if (!c.count) { navigate('/cart'); return; }
  if (S.user) { CO.phone = CO.phone || S.user.phone || ''; CO.name = CO.name || S.user.name || ''; }

  view.innerHTML = h`<div class="wrap" style="padding:22px 0 50px;max-width:640px">
    <h1 style="font-size:22px;margin:0 0 4px">Оформление заказа</h1>
    <div class="steps" style="padding:12px 0">${[1, 2, 3, 4].map(i =>
      raw(h`<div class="${CO.step >= i ? 'on' : ''}"></div>`))}</div>
    <div id="coBody"></div></div>`;
  paintCheckout(c);
}

function paintCheckout(c) {
  const el = $('#coBody');
  const b = S.boot;
  $$('.steps div').forEach((d, i) => d.classList.toggle('on', CO.step >= i + 1));

  if (CO.step === 1) {
    el.innerHTML = h`<h2 style="font-size:16px">Способ получения</h2>
      <div class="opts">
        <label class="opt ${CO.method === 'delivery' ? 'on' : ''}">
          <input type="radio" name="m" value="delivery" ${CO.method === 'delivery' ? 'checked' : ''}>
          <span class="txt">Доставка<small>Южное Бутово, Коммунарка · 200 ₽, бесплатно от 2 000 ₽</small></span></label>
        <label class="opt ${CO.method === 'pickup' ? 'on' : ''}">
          <input type="radio" name="m" value="pickup" ${CO.method === 'pickup' ? 'checked' : ''}>
          <span class="txt">Самовывоз<small>${b.shop.pickup_address}</small></span></label>
      </div>
      <div id="zoneBlock"></div>
      <button class="btn" id="next1" ${CO.method ? '' : 'disabled'}>Далее</button>`;

    el.querySelectorAll('input[name=m]').forEach(r => r.onchange = () => {
      CO.method = r.value; CO.slot_ymd = null; CO.slot_from = null; paintCheckout(c);
    });
    if (CO.method === 'delivery') {
      $('#zoneBlock').innerHTML = h`<label class="f" for="zoneSel">Район <span class="req">*</span></label>
        <select class="t" id="zoneSel">
          <option value="">— выберите —</option>
          ${b.zones.map(z => raw(h`<option value="${z.id}" ${CO.zone_id === z.id ? 'selected' : ''}>${z.name}${z.cost != null ? ' · ' + z.cost + ' ₽' : ' · расчёт вручную'}</option>`))}
        </select>
        <div id="zoneNote"></div>
        <label class="f" for="addrInput">Адрес доставки <span class="req">*</span></label>
        <input class="t" id="addrInput" value="${CO.address}" placeholder="Улица, дом, квартира" maxlength="300">`;
      const zs = $('#zoneSel');
      zs.onchange = () => { CO.zone_id = zs.value ? +zs.value : null; paintCheckout(c); };
      $('#addrInput').oninput = e => { CO.address = e.target.value; toggleNext1(); };
      const zone = b.zones.find(z => z.id === CO.zone_id);
      if (zone && zone.cost == null) {
        $('#zoneNote').innerHTML = h`<div class="note warn">Для этого района стоимость доставки
          рассчитывает менеджер. Оформите самовывоз или позвоните ${b.shop.phone}.</div>`;
      }
    }
    const n1 = $('#next1');
    function toggleNext1() {
      const zone = b.zones.find(z => z.id === CO.zone_id);
      const ok = CO.method === 'pickup' ||
        (CO.zone_id && zone && zone.cost != null && CO.address.trim().length >= 5);
      n1.disabled = !ok;
    }
    toggleNext1();
    n1.onclick = () => { CO.step = 2; paintCheckout(c); };
    return;
  }

  if (CO.step === 2) {
    el.innerHTML = h`<h2 style="font-size:16px">Время и контакты</h2>
      <div id="slotBlock">Загружаем интервалы…</div>
      <label class="f" for="nameInput">Имя <span class="req">*</span></label>
      <input class="t" id="nameInput" value="${CO.name}" maxlength="100" autocomplete="name">
      <label class="f" for="phoneInput">Телефон <span class="req">*</span></label>
      <input class="t" id="phoneInput" value="${CO.phone}" placeholder="+7 999 123-45-67" maxlength="24" autocomplete="tel" inputmode="tel">
      <label class="f" for="emailInput">Email для чека <span class="req">*</span></label>
      <input class="t" id="emailInput" value="${CO.email}" placeholder="you@mail.ru" maxlength="254" autocomplete="email" inputmode="email">
      <div class="note">Электронный чек по 54-ФЗ придёт на этот адрес.</div>
      <label class="f" for="commentInput">Комментарий</label>
      <textarea class="t" id="commentInput" maxlength="500" placeholder="Домофон, этаж, пожелания к сборке">${CO.comment}</textarea>
      <button class="btn" id="next2">Далее</button>
      <button class="btn ghost" id="back2">Назад</button>`;

    ['name', 'phone', 'email', 'comment'].forEach(f => {
      const inp = $('#' + f + 'Input');
      inp.oninput = e => { CO[f] = e.target.value; validate2(); };
    });
    $('#back2').onclick = () => { CO.step = 1; paintCheckout(c); };
    $('#next2').onclick = () => { CO.step = 3; paintCheckout(c); };
    loadSlots();
    validate2();
    return;
  }

  if (CO.step === 3) {
    const hasWeighted = c.has_weighted;
    // подписи берём из общего ядра — они одни и те же на витрине,
    // в админке и в сообщении сборщикам
    const PAY_HINT = {
      cash: 'Курьер привезёт сдачу',
      card_courier: 'Терминал у курьера',
      sbp: hasWeighted
        ? 'Оплата сразу. По весовым позициям разницу вернём после взвешивания'
        : 'Перевод по QR-коду, без комиссии',
      online: hasWeighted
        ? 'Зарезервируем сумму с запасом 10%, спишем фактическую'
        : 'Оплата сразу'
    };
    el.innerHTML = h`<h2 style="font-size:16px">Оплата</h2>
      <div class="opts">
        ${Calc.PAYMENT_METHODS.map(m => raw(h`<label class="opt ${CO.payment === m ? 'on' : ''}">
          <input type="radio" name="p" value="${m}" ${CO.payment === m ? 'checked' : ''}>
          <span class="txt">${Calc.PAYMENT_METHOD_LABEL[m]}<small>${PAY_HINT[m]}</small></span></label>`))}
      </div>
      ${hasWeighted ? raw(h`<div class="note warn" id="holdNote" hidden></div>`) : ''}
      <label class="chk"><input type="checkbox" id="consent" ${CO.consent ? 'checked' : ''}>
        <span>Согласен на обработку персональных данных в соответствии с
        <a href="/policy" target="_blank">политикой конфиденциальности</a> <span class="req">*</span></span></label>
      <label class="chk"><input type="checkbox" id="marketing" ${CO.marketing_consent ? 'checked' : ''}>
        <span>Хочу получать информацию об акциях и скидках</span></label>
      <label class="chk"><input type="checkbox" id="tgoptin" ${CO.telegram_optin ? 'checked' : ''}>
        <span>Присылать статус заказа в Telegram</span></label>
      <button class="btn" id="next3" disabled>Далее</button>
      <button class="btn ghost" id="back3">Назад</button>`;

    el.querySelectorAll('input[name=p]').forEach(r => r.onchange = () => {
      CO.payment = r.value;
      const hn = $('#holdNote');
      if (hn) {
        // предупреждение зависит от способа: карта блокирует сумму,
        // СБП списывает сразу и возвращает разницу отдельным платежом
        if (Calc.supportsHold(r.value)) {
          hn.hidden = false;
          hn.textContent = 'На карте заблокируется сумма с запасом 10% — спишется фактическая после взвешивания, разница вернётся автоматически.';
        } else if (r.value === 'sbp') {
          hn.hidden = false;
          hn.textContent = 'СБП списывает сумму сразу. После взвешивания разницу вернём отдельным переводом в течение рабочего дня.';
        } else {
          hn.hidden = true;
        }
      }
      el.querySelectorAll('.opt').forEach(o => o.classList.toggle('on', o.contains(r) && r.checked));
      validate3();
    });
    $('#consent').onchange = e => { CO.consent = e.target.checked; validate3(); };
    $('#marketing').onchange = e => { CO.marketing_consent = e.target.checked; };
    $('#tgoptin').onchange = e => { CO.telegram_optin = e.target.checked; };
    $('#back3').onclick = () => { CO.step = 2; paintCheckout(c); };
    $('#next3').onclick = () => { CO.step = 4; paintCheckout(c); };
    function validate3() { $('#next3').disabled = !(CO.payment && CO.consent); }
    validate3();
    return;
  }

  /* шаг 4 — подтверждение.
     Доставку считаем тем же ядром, что и сервер (/lib/calc.js): своя
     формула на клиенте разъезжалась с серверной — например, при зоне
     без порога бесплатной доставки показывала «бесплатно», хотя сервер
     выставлял счёт. */
  const zone = b.zones.find(z => z.id === CO.zone_id) || null;
  const preview = Calc.calcOrder({
    items: c.items.map(l => ({
      product_id: l.product.id,
      qty: l.qty, weight: l.weight
    })),
    products: c.items.map(l => l.product).map(p => ({
      id: p.id, type: p.type,
      price: p.price, price_per_kg: p.price_per_kg, sale_price: p.sale_price
    })),
    zone, method: CO.method
  });
  const delivery = preview.delivery;
  el.innerHTML = h`<h2 style="font-size:16px">Проверьте заказ</h2>
    <div class="rev">
      <div class="l"><span>Получение</span><b>${CO.method === 'delivery' ? 'Доставка · ' + (zone ? zone.name : '') : 'Самовывоз'}</b></div>
      ${CO.method === 'delivery' ? raw(h`<div class="l"><span>Адрес</span><b>${CO.address}</b></div>`) : ''}
      <div class="l"><span>Когда</span><b>${CO.slot_ymd}, ${CO.slot_from}:00–${CO.slot_from + 2}:00</b></div>
      <div class="l"><span>Получатель</span><b>${CO.name}, ${CO.phone}</b></div>
      <div class="l"><span>Чек на</span><b>${CO.email}</b></div>
      <div class="l"><span>Оплата</span><b>${Calc.paymentLabel(CO.payment)}</b></div>
    </div>
    <div class="sum">
      <div class="row"><span>Товары</span><span>${rub(c.items_total)}</span></div>
      ${c.discount ? raw(h`<div class="row disc"><span>Скидка</span><span>−${rub(c.discount)}</span></div>`) : ''}
      <div class="row"><span>Доставка</span><span>${delivery ? rub(delivery) : 'бесплатно'}</span></div>
      <div class="row tot"><span>К оплате</span><span>${rub(c.total + delivery)}</span></div>
    </div>
    ${c.has_weighted ? raw(h`<div class="note">Итог уточнится после взвешивания весовых позиций.</div>`) : ''}
    <button class="btn" id="placeBtn">Подтвердить заказ</button>
    <button class="btn ghost" id="back4">Назад</button>`;

  $('#back4').onclick = () => { CO.step = 3; paintCheckout(c); };
  $('#placeBtn').onclick = placeOrder;
}

async function loadSlots() {
  const box = $('#slotBlock');
  if (!box) return;
  try {
    const data = await api('/api/slots?method=' + CO.method);
    if (!CO.slot_ymd && data.first_available) CO.slot_ymd = data.first_available;
    const day = data.days.find(d => d.ymd === CO.slot_ymd) || data.days[0];
    if (day) CO.slot_ymd = day.ymd;

    box.innerHTML = h`<label class="f">Дата и время <span class="req">*</span></label>
      <div class="days">${data.days.map(d => raw(h`<button type="button" class="day ${d.ymd === CO.slot_ymd ? 'on' : ''}"
        data-day="${d.ymd}">${dayLabel(d.ymd)}</button>`))}</div>
      <div class="slots">${(day ? day.slots : []).map(s => raw(h`<button type="button"
        class="slot ${CO.slot_from === s.from && CO.slot_ymd === day.ymd ? 'on' : ''}"
        data-slot="${s.from}" ${s.ok ? '' : 'disabled'}>${s.from}:00–${s.to}:00
        ${s.ok ? '' : raw(h`<small>${s.reason}</small>`)}</button>`))}</div>
      ${!data.days.some(d => d.slots.some(s => s.ok))
        ? raw(h`<div class="note err">Свободных интервалов нет. Позвоните ${S.boot.shop.phone}.</div>`) : ''}`;

    box.querySelectorAll('[data-day]').forEach(bn => bn.onclick = () => {
      CO.slot_ymd = bn.dataset.day; CO.slot_from = null; loadSlots(); validate2();
    });
    box.querySelectorAll('[data-slot]').forEach(bn => bn.onclick = () => {
      CO.slot_from = +bn.dataset.slot;
      box.querySelectorAll('[data-slot]').forEach(x => x.classList.toggle('on', x === bn));
      validate2();
    });
  } catch { box.innerHTML = h`<div class="note err">Не удалось загрузить интервалы.</div>`; }
}

function dayLabel(ymd) {
  const [y, m, d] = ymd.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  const today = new Date();
  const tYmd = today.toISOString().slice(0, 10);
  if (ymd === tYmd) return 'Сегодня';
  const names = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
  return names[dt.getUTCDay()] + ', ' + d + '.' + String(m).padStart(2, '0');
}

function validEmail(v) { return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(String(v || '').trim()); }
function validPhone(v) { return String(v || '').replace(/\D/g, '').length >= 10; }
function validate2() {
  const btn = $('#next2');
  if (!btn) return;
  const ok = CO.slot_ymd && CO.slot_from !== null &&
    CO.name.trim().length >= 2 && validPhone(CO.phone) && validEmail(CO.email);
  btn.disabled = !ok;
  const ei = $('#emailInput');
  if (ei) ei.setAttribute('aria-invalid', CO.email && !validEmail(CO.email) ? 'true' : 'false');
  const pi = $('#phoneInput');
  if (pi) pi.setAttribute('aria-invalid', CO.phone && !validPhone(CO.phone) ? 'true' : 'false');
}

async function placeOrder() {
  const btn = $('#placeBtn');
  btn.disabled = true; btn.textContent = 'Оформляем…';
  try {
    const r = await api('/api/orders', { method: 'POST', body: CO });
    updateCartCount(0);
    Object.assign(CO, { step: 1, method: null, zone_id: null, address: '', slot_ymd: null, slot_from: null, comment: '', payment: null, consent: false });
    showOrderDone(r);
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Подтвердить заказ';
    if (e.data && e.data.problems) {
      const names = e.data.problems.map(p => p.name || ('#' + p.product_id)).join(', ');
      toast('Разобрали, пока вы оформляли: ' + names, true);
      navigate('/cart');
    } else if (e.data && e.data.fields) {
      toast(Object.values(e.data.fields)[0], true);
    } else toast(msgOf(e), true);
  }
}

function showOrderDone(r) {
  const o = r.order;
  $('#view').innerHTML = h`<div class="wrap" style="padding:34px 0 60px;max-width:600px">
    <div class="ok-box"><div class="em" aria-hidden="true">✅</div>
      <h3>Заказ №${o.number} принят</h3>
      <p>${o.method === 'delivery' ? 'Привезём' : 'Ждём вас'} ${o.slot_ymd}, ${o.slot_from}:00–${o.slot_to}:00</p></div>
    <div class="sum"><div class="row tot"><span>Сумма</span><span>${rub(o.total)}</span></div></div>
    ${o.hold_amount ? raw(h`<div class="note warn">На карте заблокировано ${rub(o.hold_amount)} —
      спишется фактическая сумма после взвешивания, разница вернётся.</div>`) : ''}
    <div class="note">Электронный чек по 54-ФЗ придёт на ${o.email}.</div>
    <details style="margin-top:16px"><summary style="cursor:pointer;font-size:13px;color:var(--ink-2)">
      Что ушло в чат сборщиков</summary><div class="tg">${r.picker_message}</div></details>
    <button class="btn" data-nav="/catalog">За покупками</button>
    <button class="btn ghost" data-nav="/profile">Мои заказы</button></div>`;
}

/* =====================================================================
   Предзаказ мяса (ТЗ 7.2)
   ===================================================================== */
async function openPreorder(productId) {
  let p, meat;
  try {
    [p, meat] = await Promise.all([
      api('/api/products/' + productId).then(r => r.product),
      api('/api/meat-dates')
    ]);
  } catch (e) { return toast(msgOf(e), true); }

  const st = { weight: 1, date: (meat.dates.find(d => d.ok) || {}).ymd || null, name: S.user ? S.user.name : '', phone: S.user ? S.user.phone : '', comment: '', consent: false };

  function paint() {
    showSheet(h`<div class="shead"><h2 id="sheetTitle">Предзаказ · ${p.name}</h2>
        <button class="x" id="closeSheet" aria-label="Закрыть">✕</button></div>
      <div class="sbody">
        <div class="big"><span class="ph">${photo(p, 200)}</span>
          <div><p class="pr" style="margin:0 0 4px"><b>${rub(p.unit_price)}</b>
            <span style="font-size:12px;color:var(--ink-3)">/ кг</span></p>
            <p class="unit" style="margin:0">Артикул ${p.sku}</p></div></div>
        <div class="note">Мясо привозим два раза в неделю (${meat.days.join(', ')}) и режем под заказ.
          Заявка закрывается за сутки до дня поставки. Итоговая цена — по фактическому весу при выдаче.</div>
        <label class="f">Примерный вес</label>
        <div class="wq" id="pw">${[0.5, 1, 1.5, 2, 3, 4, 5, 6].slice(0, 4).map(w =>
          raw(h`<button type="button" data-pw="${w}" class="${st.weight === w ? 'on' : ''}">${w} кг</button>`))}</div>
        <p style="font-size:12px;color:var(--ink-3);margin:6px 0 0">Ориентир: ≈${rub(p.unit_price * st.weight)}</p>
        <label class="f">Дата получения <span class="req">*</span></label>
        <div class="opts">${meat.dates.slice(0, 6).map(d => raw(h`<label class="opt ${st.date === d.ymd ? 'on' : ''}">
          <input type="radio" name="pd" value="${d.ymd}" ${st.date === d.ymd ? 'checked' : ''} ${d.ok ? '' : 'disabled'}>
          <span class="txt">${d.weekday}, ${d.ymd}<small>${d.ok ? 'свободно ' + Math.max(0, d.limit - d.booked) + ' кг' : d.reason}</small></span></label>`))}</div>
        <label class="f" for="pn">Имя <span class="req">*</span></label>
        <input class="t" id="pn" value="${st.name}" maxlength="100">
        <label class="f" for="pp">Телефон <span class="req">*</span></label>
        <input class="t" id="pp" value="${st.phone}" placeholder="+7 999 123-45-67" maxlength="24" inputmode="tel">
        <label class="f" for="pc">Пожелания</label>
        <textarea class="t" id="pc" maxlength="500" placeholder="Толщина стейков, без костей…">${st.comment}</textarea>
        <label class="chk"><input type="checkbox" id="pcons" ${st.consent ? 'checked' : ''}>
          <span>Согласен на обработку персональных данных <span class="req">*</span></span></label>
        <button class="btn" id="sendPre" disabled>Оставить предзаказ</button>
      </div>`);

    $('#closeSheet').onclick = closeSheet;
    $$('#pw button').forEach(b => b.onclick = () => { st.weight = +b.dataset.pw; paint(); });
    $$('input[name=pd]').forEach(r => r.onchange = () => { st.date = r.value; paint(); });
    $('#pn').oninput = e => { st.name = e.target.value; check(); };
    $('#pp').oninput = e => { st.phone = e.target.value; check(); };
    $('#pc').oninput = e => { st.comment = e.target.value; };
    $('#pcons').onchange = e => { st.consent = e.target.checked; check(); };
    $('#sendPre').onclick = send;
    function check() {
      $('#sendPre').disabled = !(st.date && st.name.trim().length >= 2 && validPhone(st.phone) && st.consent);
    }
    check();
  }

  async function send() {
    const btn = $('#sendPre'); btn.disabled = true; btn.textContent = 'Отправляем…';
    try {
      const r = await api('/api/preorders', {
        method: 'POST',
        body: { product_id: p.id, weight: st.weight, pickup_date: st.date, name: st.name, phone: st.phone, comment: st.comment, consent: st.consent }
      });
      showSheet(h`<div class="shead"><h2 id="sheetTitle">Предзаказ принят</h2>
          <button class="x" id="closeSheet" aria-label="Закрыть">✕</button></div>
        <div class="sbody"><div class="ok-box"><div class="em" aria-hidden="true">🥩</div>
          <h3>Предзаказ №${r.preorder.number}</h3><p>Ждём вас ${r.preorder.pickup_date}</p></div>
          <div class="note">Когда мясо будет готово, мы сообщим по телефону.</div>
          <details style="margin-top:14px"><summary style="cursor:pointer;font-size:13px;color:var(--ink-2)">Что ушло в чат сборщиков</summary>
            <div class="tg">${r.picker_message}</div></details>
          <button class="btn" id="doneBtn">Готово</button></div>`);
      $('#closeSheet').onclick = closeSheet; $('#doneBtn').onclick = closeSheet;
    } catch (e) {
      btn.disabled = false; btn.textContent = 'Оставить предзаказ';
      toast(e.data && e.data.fields ? Object.values(e.data.fields)[0] : msgOf(e), true);
    }
  }
  paint();
}

/* =====================================================================
   Вход и профиль (ТЗ 9)
   ===================================================================== */
function renderLogin(view) {
  const st = { phone: '', code: '', sent: false, mask: '' };
  paint();
  function paint() {
    view.innerHTML = h`<div class="wrap" style="padding:34px 0 60px;max-width:420px">
      <h1 style="font-size:22px">Вход в личный кабинет</h1>
      <p style="font-size:13px;color:var(--ink-2)">Пароль не нужен — пришлём код в SMS.</p>
      ${!st.sent ? raw(h`
        <label class="f" for="lp">Телефон</label>
        <input class="t" id="lp" value="${st.phone}" placeholder="+7 999 123-45-67" inputmode="tel" maxlength="24" autocomplete="tel">
        <button class="btn" id="sendCode">Получить код</button>`) : raw(h`
        <div class="note">Код отправлен на ${st.mask}. Действует 5 минут.</div>
        <label class="f" for="lc">Код из SMS</label>
        <input class="t" id="lc" value="${st.code}" inputmode="numeric" maxlength="6" placeholder="000000" autocomplete="one-time-code">
        <button class="btn" id="checkCode">Войти</button>
        <button class="btn ghost" id="againBtn">Изменить номер</button>`)}
      <p style="font-size:11.5px;color:var(--ink-3);margin-top:16px">Нажимая «Получить код», вы соглашаетесь
        с <a href="/policy">политикой конфиденциальности</a>.</p></div>`;

    if (!st.sent) {
      $('#lp').oninput = e => { st.phone = e.target.value; };
      $('#sendCode').onclick = async () => {
        if (!validPhone(st.phone)) return toast('Проверьте телефон', true);
        const btn = $('#sendCode'); btn.disabled = true;
        try {
          const r = await api('/api/auth/request-code', { method: 'POST', body: { phone: st.phone, consent: true } });
          st.sent = true; st.mask = r.phone_mask;
          if (r.dev_code) { st.code = r.dev_code; toast('Код разработчика: ' + r.dev_code); }
          paint();
        } catch (e) { btn.disabled = false; toast(msgOf(e), true); }
      };
    } else {
      $('#lc').oninput = e => { st.code = e.target.value.replace(/\D/g, ''); };
      $('#againBtn').onclick = () => { st.sent = false; st.code = ''; paint(); };
      $('#checkCode').onclick = async () => {
        const btn = $('#checkCode'); btn.disabled = true;
        try {
          const r = await api('/api/auth/verify-code', { method: 'POST', body: { phone: st.phone, code: st.code } });
          S.user = r.user;
          toast(r.linked_orders ? `Вход выполнен, найдено заказов: ${r.linked_orders}` : 'Вход выполнен');
          navigate('/profile');
        } catch (e) {
          btn.disabled = false;
          const left = e.data && e.data.attempts_left;
          toast(msgOf(e) + (left ? ` (осталось попыток: ${left})` : ''), true);
        }
      };
    }
  }
}

async function renderProfile(view) {
  view.innerHTML = h`<div class="wrap" style="padding:24px 0"><div class="card skel" style="height:200px"></div></div>`;
  let me;
  try { me = await api('/api/me'); }
  catch { navigate('/login'); return; }
  S.user = me.user;

  view.innerHTML = h`<div class="wrap" style="padding:22px 0 50px;max-width:760px">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <h1 style="font-size:22px;margin:0;flex:1">Личный кабинет</h1>
      <button class="btn ghost sm" id="logoutBtn">Выйти</button></div>
    <p style="color:var(--ink-2);font-size:13px">${me.user.phone}</p>

    <div class="info-card" id="tgCard" style="margin-top:14px"></div>

    <h2 style="font-size:17px;margin-top:24px">Заказы</h2>
    ${me.orders.length ? raw(me.orders.map(orderCardHtml).join('')) :
      raw(h`<p style="color:var(--ink-3);font-size:13px">Заказов пока нет.</p>`)}

    ${me.preorders.length ? raw(h`<h2 style="font-size:17px;margin-top:24px">Предзаказы мяса</h2>` +
      me.preorders.map(p => h`<div class="ci"><div class="in"><b>${p.product_name} · ~${p.requested_weight} кг</b>
        <small>№${p.number} · выдача ${p.pickup_date} · ≈${rub(p.estimate)}</small></div>
        <div class="rt"><span class="badge ${p.status === 'done' ? 'green' : p.status === 'cancelled' ? 'red' : 'amber'}">${preLabel(p.status)}</span></div></div>`).join('')) : ''}
  </div>`;

  paintTelegramCard();

  $('#logoutBtn').onclick = async () => {
    await api('/api/auth/logout', { method: 'POST' });
    S.user = null; updateCartCount(0); toast('Вы вышли'); navigate('/');
  };
  view.querySelectorAll('[data-cancel]').forEach(b => b.onclick = async () => {
    if (!confirm('Отменить заказ? Действие необратимо.')) return;
    try {
      await api('/api/orders/' + b.dataset.cancel + '/cancel', { method: 'POST', body: { reason: 'отменён клиентом' } });
      toast('Заказ отменён'); renderProfile(view);
    } catch (e) { toast(msgOf(e), true); }
  });
}

/* ---------------------------------------------------------------------
   Привязка Telegram (ТЗ 2.1.12)
   --------------------------------------------------------------------- */
async function paintTelegramCard() {
  const card = $('#tgCard');
  if (!card) return;
  let linked = false;
  try { linked = (await api('/api/telegram/status')).linked; } catch { return; }
  if (!$('#tgCard')) return;   // ушли со страницы, пока запрашивали

  card.innerHTML = linked
    ? h`<h4>Telegram подключён</h4>
        <p>Присылаем статус заказа и уведомления о готовности.</p>
        <button class="btn ghost sm" id="tgOff" style="margin-top:8px">Отключить</button>`
    : h`<h4>Уведомления в Telegram</h4>
        <p>Статус заказа и сообщение о готовности будут приходить в мессенджер.</p>
        <button class="btn sm" id="tgOn" style="margin-top:8px;width:auto">Привязать Telegram</button>`;

  const on = $('#tgOn');
  if (on) on.onclick = async () => {
    on.disabled = true;
    try {
      const r = await api('/api/telegram/link', { method: 'POST' });
      card.innerHTML = h`<h4>Откройте бота и нажмите «Старт»</h4>
        <p>Ссылка действует ${Math.round(r.expires_in / 60)} минут и срабатывает один раз.</p>
        <p style="margin-top:8px"><a class="btn sm" style="display:inline-block;text-decoration:none;width:auto"
          href="${r.deeplink}" target="_blank" rel="noopener">Открыть @${r.bot}</a></p>
        <p style="font-size:12px;color:var(--ink-3);margin-top:8px">После подтверждения обновите страницу.</p>`;
    } catch (e) { on.disabled = false; toast(msgOf(e), true); }
  };
  const off = $('#tgOff');
  if (off) off.onclick = async () => {
    off.disabled = true;
    try { await api('/api/telegram/link', { method: 'DELETE' }); toast('Telegram отключён'); paintTelegramCard(); }
    catch (e) { off.disabled = false; toast(msgOf(e), true); }
  };
}

const PRE_LABELS = { new: 'Новый', confirmed: 'Подтверждён', ready: 'Готов', done: 'Выдан', cancelled: 'Отменён' };
const preLabel = s => PRE_LABELS[s] || s;
const STATUS_COLOR = { new: 'gray', awaiting_payment: 'amber', assembling: 'amber', partially_assembled: 'amber', ready: 'green', in_delivery: 'green', delivered: 'green', cancelled: 'red' };

function orderCardHtml(o) {
  return h`<div class="info-card" style="margin-bottom:10px">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <b style="font-size:14px">Заказ №${o.number}</b>
      <span class="badge ${STATUS_COLOR[o.status] || 'gray'}">${o.status_label}</span>
      <span class="badge ${o.payment_status === 'paid' ? 'green' : o.payment_status === 'refunded' ? 'red' : 'amber'}">${o.payment_label}</span>
      <span style="margin-left:auto;font-weight:700">${rub(o.total)}</span></div>
    <p style="margin:6px 0 0;font-size:12.5px;color:var(--ink-2)">
      ${o.method === 'delivery' ? 'Доставка' : 'Самовывоз'} · ${o.slot_ymd}, ${o.slot_from}:00–${o.slot_to}:00 ·
      ${o.items.length} позиц.</p>
    ${o.can_cancel ? raw(h`<button class="btn ghost sm" style="margin-top:8px" data-cancel="${o.id}">Отменить заказ</button>`) : ''}
  </div>`;
}

/* =====================================================================
   Правовые страницы (ТЗ 14.3) — текст берём из серверного noscript,
   чтобы он был единым для робота и для человека.
   ===================================================================== */
/* Путь, для которого в документе лежит серверный <noscript>. */
let noscriptFor = location.pathname;

async function renderLegal(view, kind) {
  const path = kind === 'policy' ? '/policy' : '/offer';
  view.innerHTML = h`<div class="wrap legal" id="legalBody"></div>`;
  const body = $('#legalBody');

  // <noscript> в документе относится к странице, с которой начиналась
  // загрузка. При переходе внутри приложения он содержит чужой текст,
  // поэтому берём его только если он и правда от нужного пути.
  if (noscriptFor === path) {
    const src = document.querySelector('noscript');
    if (src && src.textContent.trim()) {
      body.innerHTML = src.textContent;   // собственная разметка сервера, не пользовательский ввод
      return;
    }
  }

  body.innerHTML = h`<p class="muted">Загружаем…</p>`;
  try {
    const t = await fetch(path, { credentials: 'same-origin' }).then(r => r.text());
    const ns = new DOMParser().parseFromString(t, 'text/html').querySelector('noscript');
    // маршрут мог смениться, пока грузили
    if (S.route !== kind) return;
    body.innerHTML = ns && ns.textContent.trim()
      ? ns.textContent
      : h`<h1>Документ недоступен</h1><p>Попробуйте обновить страницу.</p>`;
  } catch {
    if (S.route === kind) body.innerHTML = h`<h1>Документ недоступен</h1><p>Проверьте соединение.</p>`;
  }
}

/* =====================================================================
   Страница предзаказа мяса: список товаров категории meat
   ===================================================================== */
async function renderPreorderPage(view) {
  view.innerHTML = h`<div class="wrap" style="padding:22px 0 50px">
    <h1 style="font-size:22px;margin:0 0 6px">Мясо по предзаказу</h1>
    <p class="dsc" style="margin:0 0 18px;max-width:620px">Поставки два раза в неделю,
      объём на день ограничен. Оформите предзаказ заранее — оплата по фактическому весу при выдаче.</p>
    <div class="grid" id="preGrid"></div></div>`;
  const grid = $('#preGrid');
  grid.innerHTML = Array.from({ length: 4 }, () => '<div class="card skel skel-card"></div>').join('');
  try {
    const data = await api('/api/products?category=meat&limit=50');
    if (S.route !== 'preorder') return;
    grid.innerHTML = data.items.length
      ? data.items.map(cardHtml).join('')
      : h`<div class="empty">Предзаказ временно недоступен</div>`;
  } catch {
    if (S.route === 'preorder') grid.innerHTML = h`<div class="empty">Не удалось загрузить</div>`;
  }
}

/* =====================================================================
   Старт
   ===================================================================== */
$('#btnCart').onclick = () => { if (S.route === 'cart') return; openCartSheet(); };
$('#btnAccount').onclick = () => navigate(S.user ? '/profile' : '/login');
$('#ov').onclick = e => { if (e.target.id === 'ov') closeSheet(); };

let searchTimer;
$('#q').addEventListener('input', e => {
  S.search = e.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (S.route !== 'catalog') { S.category = 'all'; navigate('/catalog'); }
    else loadProducts();
  }, 250);
});

(async function boot() {
  try {
    S.boot = await api('/api/bootstrap');
    S.user = S.boot.user;
    updateCartCount(S.boot.cart_count);

    const s = S.boot.shop;
    $('#topInfo').textContent = `Ежедневно ${String(s.work_from).padStart(2, '0')}:00–${String(s.work_to).padStart(2, '0')}:00 · Доставка: Южное Бутово, Коммунарка — 200 ₽, бесплатно от 2 000 ₽`;
    $('#topPhone').textContent = s.phone;
    $('#topPhone').href = 'tel:' + s.phone.replace(/\D/g, '');
    $('#footPhone').textContent = s.phone;
    $('#footEmail').textContent = s.email;
    $('#footEmail').href = 'mailto:' + s.email;
    $('#footAddr').textContent = s.pickup_address;
    $('#footHours').textContent = `Ежедневно ${String(s.work_from).padStart(2, '0')}:00–${String(s.work_to).padStart(2, '0')}:00`;
    $('#footReq').textContent = s.requisites;

    PHOTOS = await fetch('/photos.json').then(r => r.json()).catch(() => ({}));
    await render();
  } catch (e) {
    $('#view').innerHTML = h`<div class="wrap legal"><h1>Магазин временно недоступен</h1>
      <p>Не удалось связаться с сервером. <button class="btn sm" data-reload="1">Обновить</button></p></div>`;
  }
})();
