/* Мост к расчётному ядру браузера.
 *
 * Node здесь — не сервер, а инструмент: способ вызвать `lib/calc.js` тем
 * же движком, каким его исполняет браузер, и сравнить результат с
 * `domain/calc.py`.
 *
 * Читает со стандартного ввода массив вызовов, печатает массив
 * результатов в том же порядке:
 *
 *   [{"op": "money", "args": [10.5]}]  →  [{"value": 11}]
 *
 * Ошибку не глотает: она уходит в поле `error`, и тест падает с текстом,
 * а не с загадочным несовпадением.
 */
'use strict';

const path = require('path');
const Calc = require(path.join(__dirname, '..', '..', 'lib', 'calc.js'));

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  let cases;
  try {
    cases = JSON.parse(input);
  } catch (e) {
    process.stdout.write(JSON.stringify([{ error: 'не разобрался ввод: ' + e.message }]));
    return;
  }

  // {"__date": "2026-08-18T09:00:00.000Z"} → new Date(...).
  // Явная пометка, а не угадывание по виду строки: аргументом бывает и
  // дата-строка «2026-08-18», которая обязана остаться строкой —
  // `dateExpired` сравнивает её как текст.
  const revive = (v) => (v && typeof v === 'object' && v.__date ? new Date(v.__date) : v);

  const out = cases.map((c) => {
    try {
      const args = (c.args || []).map(revive);
      const fn = Calc[c.op];
      if (typeof fn === 'function') return { value: fn.apply(null, args) };
      if (fn !== undefined) return { value: fn };          // константа, не функция
      return { error: 'нет такого имени в calc.js: ' + c.op };
    } catch (e) {
      return { error: String((e && e.message) || e) };
    }
  });

  process.stdout.write(JSON.stringify(out));
});
