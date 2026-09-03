/* =====================================================================
   Декоративные иллюстрации для тёмных блоков главной.

   Рисуем в SVG прямо в разметке, а не картинками: политика безопасности
   (CSP) разрешает изображения только со своего домена и Wikimedia, а
   заводить ради двух рисунков файловое хранилище незачем. Заодно SVG
   не мылится на любом экране и весит пару килобайт.

   Стиль — тонкая штриховая графика в один цвет: рисунок должен
   «прорастать» из градиента, а не лежать поверх него картинкой.
   Поэтому линии полупрозрачные, а слева блок гасится маской (см. .art).

   Экспортируется в window.Art — используется в public/app.js.
   ===================================================================== */
(function (root) {
  'use strict';

  /* Общая палитра линий. Светло-зелёный поверх тёмного фона: чем дальше
     деталь «вглубь», тем ниже прозрачность. */
  const INK = '#8FCB7A';

  /** Лавка с навесом, ящиками и деревом — для промо-блока. */
  function storefront() {
    return `
<svg viewBox="0 0 560 380" fill="none" xmlns="http://www.w3.org/2000/svg"
     role="img" aria-label="Иллюстрация: продуктовая лавка с навесом и ящиками">
  <g stroke="${INK}" stroke-linecap="round" stroke-linejoin="round" fill="none">

    <!-- дальний фон: холмы и облака -->
    <g opacity=".18" stroke-width="1.6">
      <path d="M0 250c60-26 96-8 150-30 46-19 78 6 120-14"/>
      <path d="M330 232c40-18 66 4 100-12 30-14 60 2 90-8"/>
      <path d="M395 58c8-12 26-12 32 2 12-6 24 2 24 12 0 8-6 12-14 12h-48c-9 0-15-6-15-13 0-8 8-14 21-13Z"/>
      <path d="M120 88c6-9 20-9 24 2 9-5 18 2 18 9s-4 9-10 9h-36c-7 0-11-5-11-10 0-6 6-11 15-10Z"/>
    </g>

    <!-- дерево -->
    <g opacity=".42" stroke-width="1.8">
      <path d="M96 300v-62"/>
      <path d="M96 262c-10-6-14-14-12-22M96 250c9-5 13-12 12-20"/>
      <path d="M60 214c0-26 18-44 40-44s38 17 38 42c0 24-16 40-38 40s-40-15-40-38Z"/>
      <path d="M74 196c10-8 22-10 34-6M104 226c10-3 18-10 22-19"/>
    </g>

    <!-- здание лавки -->
    <g stroke-width="2" opacity=".85">
      <path d="M196 300V150h250v150"/>
      <path d="M186 150l135-46 135 46"/>
      <path d="M196 150h250"/>
    </g>

    <!-- вывеска -->
    <g stroke-width="1.8" opacity=".7">
      <rect x="288" y="112" width="120" height="34" rx="4"/>
      <path d="M302 124h56M302 134h84" stroke-width="2.4" opacity=".85"/>
    </g>

    <!-- полосатый навес -->
    <g stroke-width="1.8" opacity=".8">
      <path d="M186 186h274l-16 40H202l-16-40Z"/>
      <path d="M214 186l-8 40M242 186l-6 40M270 186l-4 40M298 186l-2 40M326 186v40M354 186l2 40M382 186l4 40M410 186l6 40M438 186l8 40" opacity=".5"/>
    </g>

    <!-- витрина и дверь -->
    <g stroke-width="1.8" opacity=".75">
      <rect x="212" y="238" width="86" height="46" rx="3"/>
      <path d="M212 262h86M255 238v46" opacity=".45"/>
      <rect x="386" y="234" width="46" height="66" rx="3"/>
      <circle cx="396" cy="268" r="2.6" fill="${INK}" stroke="none" opacity=".8"/>
    </g>

    <!-- ящики с овощами -->
    <g stroke-width="1.8" opacity=".78">
      <path d="M310 300v-30h64v30"/>
      <path d="M310 280h64" opacity=".5"/>
      <circle cx="324" cy="264" r="7"/><circle cx="342" cy="261" r="8"/><circle cx="360" cy="265" r="6.5"/>
      <path d="M324 257v-4M342 253v-5M360 259v-4" opacity=".6"/>

      <path d="M228 300v-22h58v22"/>
      <path d="M228 286h58" opacity=".5"/>
      <circle cx="242" cy="272" r="6"/><circle cx="258" cy="270" r="6.5"/><circle cx="274" cy="273" r="5.5"/>
    </g>

    <!-- грифельная доска с меню -->
    <g stroke-width="1.7" opacity=".55">
      <rect x="132" y="242" width="52" height="42" rx="3"/>
      <path d="M142 300l6-16M174 300l-6-16"/>
      <path d="M142 254h30M142 262h34M142 270h24" opacity=".7"/>
    </g>

    <!-- растение в горшке -->
    <g stroke-width="1.7" opacity=".5">
      <path d="M470 300v-18h30v18"/>
      <path d="M485 282v-24"/>
      <path d="M485 268c-10-2-16-10-15-19 9-1 16 5 18 13M485 262c9-3 14-11 12-19-9 0-15 6-16 14"/>
    </g>

    <!-- земля -->
    <path d="M0 300h560" stroke-width="2" opacity=".55"/>
    <path d="M24 312h84M150 312h48M420 312h72" stroke-width="1.6" opacity=".22"/>
  </g>
</svg>`;
  }

  /** Разделочный стол, тесак и весы — для блока предзаказа мяса. */
  function butcher() {
    return `
<svg viewBox="0 0 460 300" fill="none" xmlns="http://www.w3.org/2000/svg"
     role="img" aria-label="Иллюстрация: разделочная доска, нож и весы">
  <g stroke="${INK}" stroke-linecap="round" stroke-linejoin="round" fill="none">

    <!-- подвесные весы -->
    <g stroke-width="1.8" opacity=".55">
      <path d="M330 26v34"/>
      <circle cx="330" cy="86" r="26"/>
      <path d="M330 70v16l11 7" opacity=".8"/>
      <path d="M312 112l-10 22h56l-10-22" opacity=".7"/>
      <path d="M300 140h60" stroke-width="2"/>
    </g>

    <!-- связка зелени на крюке -->
    <g stroke-width="1.6" opacity=".38">
      <path d="M96 26v26"/>
      <path d="M96 52c-8 14-12 30-10 46M96 52c8 12 13 28 12 44M96 52c-2 16-2 32 0 46"/>
      <path d="M84 98c8 6 16 6 24 0" opacity=".7"/>
    </g>

    <!-- разделочная доска -->
    <g stroke-width="2" opacity=".8">
      <path d="M120 214h250c6 0 10 4 10 10v10c0 6-4 10-10 10H120c-6 0-10-4-10-10v-10c0-6 4-10 10-10Z"/>
      <circle cx="360" cy="228" r="4" opacity=".6"/>
      <path d="M140 224h180" opacity=".3"/>
    </g>

    <!-- куски мяса на доске -->
    <g stroke-width="1.8" opacity=".78">
      <path d="M156 214c-14-4-22-16-18-28 4-13 20-19 34-14 8 3 13 9 15 16 12-3 24 3 27 14 2 8-2 15-9 19"/>
      <path d="M168 186c6-4 14-4 20 1" opacity=".55"/>
      <path d="M186 196c5 2 8 6 9 11" opacity=".55"/>

      <path d="M246 214c-10-2-16-10-14-19 3-10 15-14 24-9 5 3 8 8 9 13" opacity=".8"/>
      <path d="M254 194c4-2 9-1 12 2" opacity=".5"/>
    </g>

    <!-- тесак -->
    <g stroke-width="1.9" opacity=".72">
      <path d="M286 150h74c5 0 8 4 8 9v26c0 5-3 9-8 9h-74v-44Z"/>
      <path d="M286 158h-46c-6 0-10 4-10 9s4 9 10 9h46" opacity=".9"/>
      <path d="M300 162v20M312 162v20" opacity=".35"/>
      <circle cx="356" cy="163" r="3" opacity=".6"/>
    </g>

    <!-- разделочная нить и специи -->
    <g stroke-width="1.6" opacity=".32">
      <circle cx="404" cy="216" r="12"/>
      <path d="M404 204v24M392 216h24"/>
      <path d="M62 232c10-8 22-8 32 0" />
      <circle cx="70" cy="222" r="4"/><circle cx="86" cy="220" r="4.5"/>
    </g>

    <!-- стол -->
    <path d="M0 244h460" stroke-width="2" opacity=".5"/>
    <path d="M150 256v22M330 256v22" stroke-width="1.8" opacity=".28"/>
    <path d="M40 262h70M370 262h60" stroke-width="1.5" opacity=".18"/>
  </g>
</svg>`;
  }

  root.Art = { storefront, butcher };
})(typeof window !== 'undefined' ? window : globalThis);
