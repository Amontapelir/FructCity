/* =====================================================================
   FructCity — ЕДИНОЕ расчётное ядро.
   Один и тот же файл исполняется на сервере (require) и в браузере
   (<script src="/lib/calc.js">). Это гарантирует, что витрина, админка
   и сервер считают деньги ОДИНАКОВО — расхождение расчётов физически
   невозможно, потому что код один.

   Здесь НЕТ данных каталога и НЕТ обращений к БД/DOM — только чистые
   функции. Данные всегда передаются аргументами.
   ===================================================================== */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Calc = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /* ---------- константы предметной области (ТЗ 3.4, 4.4, 5.1) ---------- */
  const WEIGHT_TOLERANCE_PCT = 10;   // ТЗ 3.4 — допустимое отклонение факт. веса
  const HOLD_MULTIPLIER = 1.1;       // ТЗ 3.4 — холд = расчёт + 10%
  const SLOT_INTERVAL_H = 2;         // ТЗ 4.4 — интервалы строго по 2 часа
  const MIN_WEIGHT_KG = 0.5;
  const WEIGHT_STEP_KG = 0.5;

  /* ---------- деньги ----------
     Все суммы — целые рубли. Округление — единая точка входа, чтобы
     сервер и клиент не разошлись на копейку из-за разных правил. */
  function money(n) {
    if (!Number.isFinite(n)) return 0;
    return Math.round(n);
  }

  /* ---------- цены товара ---------- */
  function basePrice(p) {
    return p.type === 'unit' ? num(p.price) : num(p.price_per_kg);
  }
  /**
   * Действует ли акция (ТЗ 3.2).
   * Срок sale_until — включительно: акция «до 20 августа» работает весь
   * день 20-го и снимается сама 21-го, без вмешательства администратора.
   * Дата сравнивается по московскому календарю, а не по часовому поясу
   * сервера, иначе акция гасла бы на несколько часов раньше или позже.
   */
  /**
   * Истёк ли срок, заданный датой вида YYYY-MM-DD.
   * Срок ВКЛЮЧИТЕЛЬНЫЙ: «до 20 августа» действует весь день 20-го.
   * Сравниваем календарные даты по Москве, а не через new Date(): строка
   * «2026-08-20» разбирается как полночь UTC, и промокод считался бы
   * просроченным весь последний день.
   */
  function dateExpired(untilYmd, now) {
    if (!untilYmd) return false;
    return mskParts(now || new Date()).ymd > String(untilYmd).slice(0, 10);
  }

  function isSale(p, now) {
    const sp = num(p.sale_price);
    if (!(sp > 0 && sp < basePrice(p))) return false;
    return !dateExpired(p.sale_until, now);
  }
  function unitPrice(p, now) {
    return isSale(p, now) ? num(p.sale_price) : basePrice(p);
  }
  function inStock(p) {
    return num(p.stock) > 0;
  }
  function num(v) {
    const n = typeof v === 'string' ? parseFloat(v) : v;
    return Number.isFinite(n) ? n : 0;
  }

  /* ---------- позиция корзины ----------
     weighted считается по весу; unit и preorder — по количеству.
     preorder оплачивается по факту при выдаче, но в корзине показываем
     ориентир по заявленному весу (ТЗ 7.1). */
  function lineTotal(p, item, now) {
    if (p.type === 'weighted') return money(unitPrice(p, now) * num(item.weight));
    if (p.type === 'preorder') return money(unitPrice(p, now) * num(item.weight || 1));
    return money(unitPrice(p, now) * Math.max(0, Math.trunc(num(item.qty))));
  }

  /* ---------- нормализация строки корзины ----------
     Единое место, где решается «сколько единиц/кг» — используется и при
     расчёте, и при валидации входящего запроса. */
  function normalizeItem(p, raw) {
    if (p.type === 'weighted' || p.type === 'preorder') {
      let w = num(raw.weight);
      w = Math.round(w / WEIGHT_STEP_KG) * WEIGHT_STEP_KG;
      w = Math.max(MIN_WEIGHT_KG, Math.round(w * 100) / 100);
      return { product_id: p.id, weight: w, qty: null };
    }
    let q = Math.trunc(num(raw.qty));
    q = Math.max(1, q);
    return { product_id: p.id, qty: q, weight: null };
  }

  /* =====================================================================
     ПОЛНЫЙ РАСЧЁТ ЗАКАЗА (ТЗ 6.1)
     Порядок операций зафиксирован:
       1) сумма товаров
       2) скидка промокода (процент — НЕ на акционные позиции)
       3) порог бесплатной доставки считается от суммы ПОСЛЕ скидки
       4) промокод на доставку применяется последним
     ===================================================================== */
  function calcOrder(input) {
    const items = input.items || [];
    const products = input.products || [];
    const promo = input.promo || null;
    const zone = input.zone || null;
    const method = input.method || 'delivery';
    const usePreorderWeights = !!input.usePreorderWeights;

    const byId = new Map(products.map(p => [String(p.id), p]));

    let itemsTotal = 0, saleTotal = 0;
    const lines = [];
    for (const it of items) {
      const p = byId.get(String(it.product_id));
      if (!p) continue;
      if (it.is_removed) { lines.push({ product_id: p.id, total: 0, removed: true }); continue; }
      // При сборке считаем по фактическому весу, если он проставлен (ТЗ 3.4)
      const eff = (it.actual_weight != null && it.actual_weight !== '')
        ? { qty: it.qty, weight: num(it.actual_weight) }
        : it;
      // момент расчёта общий для всего заказа: иначе позиция, посчитанная
      // на границе суток, могла бы получить акцию, а соседняя — уже нет
      const t = lineTotal(p, eff, input.now);
      const onSale = isSale(p, input.now);
      itemsTotal += t;
      if (onSale) saleTotal += t;
      lines.push({ product_id: p.id, total: t, removed: false, sale: onSale });
    }
    itemsTotal = money(itemsTotal);
    saleTotal = money(saleTotal);

    /* ---- промокод ---- */
    let discount = 0, deliveryDiscount = 0, promoError = null;
    if (promo) {
      const min = num(promo.min_order);
      if (!promo.is_active) {
        promoError = 'Промокод не активен';
      } else if (dateExpired(promo.valid_until, input.now)) {
        promoError = 'Срок действия промокода истёк';
      } else if (num(promo.uses_limit) > 0 && num(promo.uses_count) >= num(promo.uses_limit)) {
        promoError = 'Промокод исчерпан';
      } else if (itemsTotal < min) {
        promoError = `Промокод действует от ${min} ₽`;
      } else if (promo.type === 'percent') {
        // ТЗ 6.1 — процент не суммируется с акционной ценой
        const base = itemsTotal - saleTotal;
        discount = Math.floor(base * num(promo.value) / 100);
        if (discount === 0) promoError = 'В корзине только акционные товары — промокод не применяется';
      } else if (promo.type === 'fixed') {
        discount = Math.min(num(promo.value), itemsTotal);
      }
      // type === 'delivery' обрабатывается ниже, после расчёта доставки
    }

    const afterDiscount = itemsTotal - discount;

    /* ---- доставка (ТЗ 5.1) ---- */
    let delivery = 0, freeDelivery = false, needsQuote = false;
    if (method === 'delivery' && zone) {
      if (zone.cost == null) {
        needsQuote = true;                      // ТЗ 5.2 — «другие районы», расчёт вручную
      } else if (num(zone.free_from) > 0 && afterDiscount >= num(zone.free_from)) {
        delivery = 0; freeDelivery = true;
      } else {
        delivery = num(zone.cost);
      }
    }
    if (promo && promo.type === 'delivery' && !promoError && delivery > 0) {
      // скидка не может превысить саму доставку: иначе процент больше 100
      // уводил бы стоимость доставки, а с ней и весь заказ, в минус
      deliveryDiscount = Math.min(delivery, Math.floor(delivery * num(promo.value) / 100));
      delivery = delivery - deliveryDiscount;
    }

    // страховка: к оплате не может быть отрицательной суммы ни при каких
    // настройках промокодов — деньги клиенту мы не доплачиваем
    const total = Math.max(0, money(afterDiscount + delivery));

    return {
      lines,
      itemsTotal,
      saleTotal,
      discount,
      deliveryDiscount,
      delivery,
      freeDelivery,
      needsQuote,
      promoError,
      total,
      hold: holdAmount(total)
    };
  }

  /* ---------- весовые товары (ТЗ 3.4) ---------- */
  function checkActualWeight(requested, actual) {
    const req = num(requested), act = num(actual);
    if (req <= 0) return { deviation: 0, ok: false, needsCall: true };
    // округляем до 0.1% — иначе float даёт 10.000000000000002 на ровно 10%
    const dev = Math.round(((act - req) / req) * 1000) / 10;
    return {
      deviation: dev,
      ok: Math.abs(dev) <= WEIGHT_TOLERANCE_PCT,
      needsCall: Math.abs(dev) > WEIGHT_TOLERANCE_PCT
    };
  }
  function holdAmount(total) {
    return Math.ceil(num(total) * HOLD_MULTIPLIER);
  }

  /* =====================================================================
     СЛОТЫ ДОСТАВКИ (ТЗ 4.4)
     Часовой пояс Europe/Moscow фиксирован. Считаем в «московских» полях,
     полученных через Intl — так расчёт не зависит от TZ сервера.
     ===================================================================== */
  const MSK_TZ = 'Europe/Moscow';
  function mskParts(date) {
    const fmt = new Intl.DateTimeFormat('en-CA', {
      timeZone: MSK_TZ, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false
    });
    const p = {};
    for (const { type, value } of fmt.formatToParts(date)) p[type] = value;
    return {
      ymd: `${p.year}-${p.month}-${p.day}`,
      hour: parseInt(p.hour, 10),
      minute: parseInt(p.minute, 10)
    };
  }
  function addDaysYmd(ymd, days) {
    const [y, m, d] = ymd.split('-').map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d));
    dt.setUTCDate(dt.getUTCDate() + days);
    return dt.toISOString().slice(0, 10);
  }

  /**
   * Слоты на дату. Возвращает список с причиной недоступности —
   * клиент показывает закрытые слоты серыми, а не прячет (понятнее).
   */
  function slotsForDate(opts) {
    const { ymd, now, workFrom, workTo, cutoffH, capacity, booked, holidays } = opts;
    const nowP = mskParts(now || new Date());
    const nowMin = nowP.hour * 60 + nowP.minute;
    const isHoliday = (holidays || []).includes(ymd);
    const out = [];
    for (let h = workFrom; h + SLOT_INTERVAL_H <= workTo; h += SLOT_INTERVAL_H) {
      const key = `${ymd}|${h}`;
      const used = num((booked || {})[key]);
      let ok = true, reason = null;
      if (ymd < nowP.ymd) { ok = false; reason = 'прошло'; }
      // Праздник/выходной — весь день, независимо от времени (ТЗ 4.4).
      else if (isHoliday) { ok = false; reason = 'выходной'; }
      else if (ymd === nowP.ymd && (h * 60 - nowMin) < num(cutoffH) * 60) { ok = false; reason = 'поздно'; }
      if (ok && capacity > 0 && used >= capacity) { ok = false; reason = 'занято'; }
      out.push({ ymd, from: h, to: h + SLOT_INTERVAL_H, ok, reason, used, capacity });
    }
    return out;
  }

  /** Ближайшая дата, где есть свободный слот (ТЗ 4.4 — «предложить ближайшую»). */
  function firstAvailableDate(opts) {
    const nowP = mskParts(opts.now || new Date());
    for (let d = 0; d < num(opts.horizonD); d++) {
      const ymd = addDaysYmd(nowP.ymd, d);
      if (slotsForDate(Object.assign({}, opts, { ymd })).some(s => s.ok)) return ymd;
    }
    return null;
  }

  /* ---------- предзаказ мяса (ТЗ 7.1) ---------- */
  const WEEKDAYS = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
  function weekdayOf(ymd) {
    const [y, m, d] = ymd.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d)).getUTCDay();
  }
  /**
   * Даты поставки мяса: дни недели из настроек, приём заявок закрывается
   * за cutoffDays суток, плюс дневной лимит по килограммам.
   */
  function meatDates(opts) {
    const nowP = mskParts(opts.now || new Date());
    const out = [];
    for (let d = 0; d <= num(opts.horizonD || 21); d++) {
      const ymd = addDaysYmd(nowP.ymd, d);
      const wd = WEEKDAYS[weekdayOf(ymd)];
      if (!(opts.days || []).includes(wd)) continue;
      const booked = num((opts.bookedKg || {})[ymd]);
      const limit = num(opts.limitKg);
      const daysLeft = d;
      const closed = daysLeft < num(opts.cutoffDays || 1);
      const full = limit > 0 && booked >= limit;
      out.push({
        ymd, weekday: wd, booked, limit,
        ok: !closed && !full,
        reason: closed ? 'приём закрыт' : (full ? 'дневной объём выбран' : null)
      });
    }
    return out;
  }

  /* ---------- поиск (ТЗ С-6): синонимы + грубый стемминг ---------- */
  const SYNONYMS = {
    'томат': 'помидор', 'помидорчик': 'помидор', 'кориандр': 'кинза',
    'картошка': 'картофель', 'морковка': 'морковь', 'бульба': 'картофель',
    'авокадо': 'авокадо', 'булка': 'хлеб'
  };
  function normalizeQuery(s) {
    s = String(s || '').toLowerCase().trim();
    for (const k in SYNONYMS) if (s.includes(k)) s = s.split(k).join(SYNONYMS[k]);
    return s;
  }
  function stem(w) {
    return w.replace(/(ами|ями|ов|ей|ах|ях|ы|и|а|я|у|ю|е|о)$/, '');
  }
  function matchesQuery(p, q) {
    if (!q) return true;
    const hay = normalizeQuery(`${p.name} ${p.description || ''} ${p.sku}`);
    return normalizeQuery(q).split(/\s+/).filter(Boolean).every(w => {
      const s = stem(w);
      return hay.includes(w) || (s.length > 2 && hay.includes(s));
    });
  }

  /* ---------- slug для ЧПУ (ТЗ 15.3) ---------- */
  const TRANSLIT = {
    а:'a',б:'b',в:'v',г:'g',д:'d',е:'e',ё:'e',ж:'zh',з:'z',и:'i',й:'y',к:'k',л:'l',м:'m',
    н:'n',о:'o',п:'p',р:'r',с:'s',т:'t',у:'u',ф:'f',х:'h',ц:'c',ч:'ch',ш:'sh',щ:'sch',
    ъ:'',ы:'y',ь:'',э:'e',ю:'yu',я:'ya'
  };
  function slugify(s) {
    return String(s || '').toLowerCase().split('').map(ch => TRANSLIT[ch] !== undefined ? TRANSLIT[ch] : ch)
      .join('').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80) || 'item';
  }

  /* ---------- статусная модель заказа (ТЗ 13) ---------- */
  const STATUS_FLOW = ['new', 'awaiting_payment', 'assembling', 'partially_assembled', 'ready', 'in_delivery', 'delivered'];
  const STATUS_LABEL = {
    new: 'Новый', awaiting_payment: 'Ожидает оплаты', assembling: 'Сборка',
    partially_assembled: 'Частично собран', ready: 'Готов', in_delivery: 'В доставке',
    delivered: 'Доставлен', cancelled: 'Отменён',
    // Зона без тарифа (ТЗ 5.2) — вне STATUS_FLOW, как и cancelled: заказ
    // сюда попадает при оформлении вместо отказа, а выводит его только
    // персонал, назвавший стоимость (POST .../delivery-quote), не обычная
    // кнопка «дальше по статусам».
    awaiting_delivery_quote: 'Ждёт расчёта доставки'
  };
  const PAYMENT_LABEL = { pending: 'Ожидает оплаты', paid: 'Оплачен', refunded: 'Возвращён' };

  /* ---------- способы оплаты (ТЗ 2.1.8, 4.3) ----------
     Список и подписи держим здесь: они нужны и серверу (сообщение сборщикам),
     и витрине, и админке. Раньше строки были продублированы в четырёх местах
     и незнакомый способ печатался как «undefined». */
  const PAYMENT_METHODS = ['cash', 'card_courier', 'sbp', 'online'];
  const PAYMENT_METHOD_LABEL = {
    cash: 'Наличными при получении',
    card_courier: 'Картой курьеру',
    sbp: 'СБП',
    online: 'Онлайн картой'
  };
  const PAYMENT_METHOD_SHORT = {
    cash: 'наличные', card_courier: 'карта курьеру', sbp: 'СБП', online: 'онлайн'
  };
  function paymentLabel(m, short) {
    const map = short ? PAYMENT_METHOD_SHORT : PAYMENT_METHOD_LABEL;
    return map[m] || String(m || '—');
  }
  /** Оплачено до получения — заказ ждёт подтверждения платежа. */
  function isPrepaid(m) { return m === 'online' || m === 'sbp'; }
  /**
   * Блокировка суммы с запасом возможна только на банковской карте (ТЗ 3.4).
   * СБП — перевод, а не авторизация: заблокировать «с запасом» нечего,
   * поэтому по весовым позициям списывается расчётная сумма, а разница
   * возвращается отдельным платежом после взвешивания.
   */
  function supportsHold(m) { return m === 'online'; }
  function nextStatus(s) {
    const i = STATUS_FLOW.indexOf(s);
    return (i < 0 || i === STATUS_FLOW.length - 1) ? null : STATUS_FLOW[i + 1];
  }

  /**
   * Куда заказ может перейти прямо сейчас (ТЗ 13).
   *
   * STATUS_FLOW — линейный список, но `awaiting_payment` относится только
   * к предоплате. Для наличных и карты курьеру этот шаг пропускается:
   * иначе сборщик не мог бы взять заказ в работу, не проставив заказу
   * бессмысленный статус «ожидает оплаты».
   *
   * Обратное правило важнее: предоплаченный заказ не двигается дальше,
   * пока платёж не подтверждён, — иначе товар уезжает бесплатно.
   *
   * @returns {{allowed: string[], blockedReason: string|null}}
   */
  function allowedTransitions(order) {
    if (!order || order.status === 'cancelled') return { allowed: [], blockedReason: 'order_cancelled' };
    if (order.status === 'delivered') return { allowed: [], blockedReason: 'order_delivered' };
    if (order.status === 'awaiting_delivery_quote') return { allowed: [], blockedReason: 'awaiting_delivery_quote' };

    const prepaid = isPrepaid(order.payment_method);
    const paid = order.payment_status === 'paid';
    const i = STATUS_FLOW.indexOf(order.status);
    if (i < 0) return { allowed: [], blockedReason: 'bad_status' };

    let next = STATUS_FLOW[i + 1] || null;
    // «ожидает оплаты» не для оплаты при получении — перешагиваем
    if (next === 'awaiting_payment' && !prepaid) next = STATUS_FLOW[i + 2] || null;

    if (!next) return { allowed: [], blockedReason: null };

    if (order.status === 'awaiting_payment' && prepaid && !paid) {
      return { allowed: [], blockedReason: 'payment_not_confirmed' };
    }
    return { allowed: [next], blockedReason: null };
  }

  /** Следующий шаг с учётом способа оплаты — для кнопки в админке. */
  function nextStatusFor(order) {
    return allowedTransitions(order).allowed[0] || null;
  }

  /**
   * Допустимые переходы статуса ОПЛАТЫ (ТЗ 13 — независимая ось).
   * Возврат — конечное состояние: деньги ушли обратно, «переоплатить»
   * тот же заказ нельзя, нужен новый.
   */
  const PAYMENT_FLOW = { pending: ['paid', 'refunded'], paid: ['refunded'], refunded: [] };
  function canChangePayment(order, to) {
    if (!order) return false;
    if (order.status === 'cancelled' && to !== 'refunded') return false;
    return (PAYMENT_FLOW[order.payment_status] || []).includes(to);
  }
  /** Клиент может отменить сам только до сборки (ТЗ 4.6). */
  function customerCanCancel(status) {
    return status === 'new' || status === 'awaiting_payment' || status === 'awaiting_delivery_quote';
  }

  return {
    WEIGHT_TOLERANCE_PCT, HOLD_MULTIPLIER, SLOT_INTERVAL_H, MIN_WEIGHT_KG, WEIGHT_STEP_KG,
    money, num, basePrice, unitPrice, isSale, inStock, lineTotal, normalizeItem, dateExpired,
    calcOrder, checkActualWeight, holdAmount,
    mskParts, addDaysYmd, slotsForDate, firstAvailableDate,
    meatDates, WEEKDAYS, weekdayOf,
    normalizeQuery, matchesQuery, slugify,
    STATUS_FLOW, STATUS_LABEL, PAYMENT_LABEL, nextStatus, customerCanCancel,
    allowedTransitions, nextStatusFor, canChangePayment, PAYMENT_FLOW,
    PAYMENT_METHODS, PAYMENT_METHOD_LABEL, PAYMENT_METHOD_SHORT,
    paymentLabel, isPrepaid, supportsHold
  };
});
