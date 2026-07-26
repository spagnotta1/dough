/* ══════════════════════════════════════════════════════════════════════════
   CheckCharts — one chart system for the whole app.
   ──────────────────────────────────────────────────────────────────────────
   Every chart in Check draws its palette, type scale, grid weight, and
   tooltip from here, so the dashboard, the investments page, and the figures
   the chat assistant renders all read as one product.

   ── On color ─────────────────────────────────────────────────────────────
   Two palettes, used for two different jobs, and the distinction matters:

   *Values* — a number, a delta, a KPI, a budget bar — keep the money
   convention: green is money in, red is money out. That is a STATUS color,
   and it is always accompanied by a glyph (▲ ▼) and a word, so the meaning
   survives without the hue.

   *Series* — two or more lines/bars a reader has to tell apart — use the
   validated categorical palette below. Green-vs-red as a SERIES pair is a
   hard accessibility failure: at OKLab ΔE 1.1–3.7 for deuteranopia, an
   income line and an outgo line in those colors are the same color to
   roughly one man in twelve. Slots 1 and 2 here (blue/orange) sit at ΔE 25+
   for every CVD type, which is why the income/outgo trend uses them.

   The categorical order is fixed, never cycled by index, and validated in
   both light and dark against the six checks (lightness band, chroma floor,
   CVD separation, normal-vision floor, contrast). Light mode leaves three
   slots under 3:1 on white; the relief for that is the direct-value toggle
   every chart card carries plus the category table beneath, both of which
   state the figures in text.
   ══════════════════════════════════════════════════════════════════════════ */

(function (global) {
  'use strict';

  /* ── Categorical palette — fixed slot order, validated in both modes ──── */
  var CATEGORICAL = {
    light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'],
    dark:  ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']
  };

  /* Sequential single hue (blue), light → dark. For magnitude, not identity. */
  var SEQUENTIAL = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#2a78d6', '#256abf', '#184f95', '#0d366b'];

  /* Anything past the last slot folds into one neutral rather than inventing
     a hue — a generated 9th color is never distinguishable from the 8. */
  var OVERFLOW = { light: '#8b8b86', dark: '#9a9a94' };

  function mode() {
    return document.documentElement.getAttribute('data-scheme') === 'dark' ? 'dark' : 'light';
  }

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function reducedMotion() {
    return global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /* ── Semantic colors, read live from the design tokens ────────────────── */
  function tokens() {
    var m = mode();
    return {
      mode:    m,
      ink:     cssVar('--fg', m === 'dark' ? '#e6e6e6' : '#111827'),
      surface: cssVar('--panel', m === 'dark' ? '#161b22' : '#ffffff'),
      border:  cssVar('--border', m === 'dark' ? '#30363d' : '#e5e7eb'),
      accent:  cssVar('--red', '#7c3aed'),
      ok:      cssVar('--ok-mark', m === 'dark' ? '#22c55e' : '#16a34a'),
      warn:    cssVar('--warn-mark', '#f59e0b'),
      danger:  cssVar('--danger-mark', '#ef4444'),
      info:    cssVar('--info-mark', '#3b82f6'),
      ai:      cssVar('--ai-mark', m === 'dark' ? '#a78bfa' : '#8b5cf6')
    };
  }

  function palette() { return CATEGORICAL[mode()]; }

  /* ── Stable category → color ──────────────────────────────────────────
     Color follows the entity, not its rank within the current view. The
     server sends every category the account has, ordered by lifetime volume,
     and slot N goes to the Nth entry. Two properties come out of that:

     * A filter never repaints a survivor. The ordering is computed from the
       whole history, so narrowing the dashboard to three categories leaves
       each of them exactly the color it already was. Assigning by loop index
       — the usual shortcut — repaints everything on every filter change and
       quietly destroys the reader's memory of what each color meant.

     * The eight hues land on the categories that actually reach a chart.
       Charts show the top few categories by amount, which is the head of
       this same list, so they get eight distinct hues instead of whatever
       alphabetical order happened to hand out. */
  var _catSlots = {};

  function registerCategories(names) {
    _catSlots = {};
    (names || []).forEach(function (name, i) { _catSlots[name] = i; });
  }

  /* Does this category hold a real hue, or has it fallen into the neutral?
     The UI uses this to decide whether showing a color chip beside it says
     anything — a row of identical grays is worse than no chips at all. */
  function hasOwnColor(name) {
    var slot = _catSlots[name];
    return slot !== undefined && slot < CATEGORICAL.light.length;
  }

  function slotOf(name) {
    var slot = _catSlots[name];
    return slot === undefined ? Number.MAX_SAFE_INTEGER : slot;
  }

  /* Order series so that neighbours hold CONSECUTIVE palette slots.
     ────────────────────────────────────────────────────────────────────
     The palette's slot order is the colorblind-safety mechanism: it was
     validated pair-by-pair on the *adjacent* pairlist, because in a stack
     or a grouped bar only neighbouring series actually touch. That
     guarantee holds only if neighbours really are consecutive slots.

     Feeding datasets in dictionary order breaks it — two categories holding
     slots 2 and 5 end up stacked against each other, and that pair was
     never checked. Measured on this palette, the worst such accidental
     pair sits at OKLab ΔE 1.6 for deuteranopia (indistinguishable), versus
     8.4 for the worst genuinely-adjacent pair. Sorting by slot before
     building the datasets is what puts the chart back inside the
     configuration the validator signed off on. */
  function orderBySlot(names) {
    return (names || []).slice().sort(function (a, b) { return slotOf(a) - slotOf(b); });
  }

  function forCategory(name) {
    var p = palette();
    var slot = _catSlots[name];
    if (slot === undefined) {
      // Not in the registry (e.g. the synthetic "Other" bucket).
      return OVERFLOW[mode()];
    }
    return slot < p.length ? p[slot] : OVERFLOW[mode()];
  }

  /* ── Formatters ───────────────────────────────────────────────────────── */
  var _usd0 = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', maximumFractionDigits: 0
  });
  var _usd2 = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2
  });

  function money(v) { return _usd0.format(v || 0); }
  function moneyExact(v) { return _usd2.format(v || 0); }

  /* Axis labels: $1.2k / $34k / $1.1M — a y-axis full of $12,000-wide
     labels squeezes the plot area for no added precision. */
  function moneyShort(v) {
    var n = Math.abs(v);
    var sign = v < 0 ? '-' : '';
    if (n >= 1e6) return sign + '$' + (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
    if (n >= 1e3) return sign + '$' + (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k';
    return sign + '$' + Math.round(n);
  }

  var MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  /* "2026-01-02" → "Jan 2". */
  function dayLabel(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
    if (!m) return iso || '';
    return (MONTHS[parseInt(m[2], 10) - 1] || m[2]) + ' ' + parseInt(m[3], 10);
  }

  /* "2026-03" → "Mar", "2026-01" → "Jan '26" so a year boundary is visible. */
  function monthLabel(key) {
    var m = /^(\d{4})-(\d{2})$/.exec(String(key || ''));
    if (!m) return key;
    var name = MONTHS[parseInt(m[2], 10) - 1] || key;
    return m[2] === '01' ? name + " '" + m[1].slice(2) : name;
  }

  /* ── Shared Chart.js options ──────────────────────────────────────────── */

  function fonts() {
    return {
      family: 'ui-sans-serif, -apple-system, "Segoe UI", Inter, Roboto, sans-serif',
      size: 11
    };
  }

  /* Grid and axes are deliberately recessive: the data is the ink, the
     scaffolding is a hint. One axis only — never a second y-scale. */
  function gridFor(t) {
    return {
      color: 'color-mix' in (global.CSS || {}) && CSS.supports('color', 'color-mix(in srgb, red 10%, blue)')
        ? 'color-mix(in srgb, ' + t.ink + ' 11%, transparent)'
        : t.border,
      drawTicks: false,
      lineWidth: 1
    };
  }

  function ticksFor(t, extra) {
    return Object.assign({
      color: 'color-mix(in srgb, ' + t.ink + ' 58%, transparent)',
      font: fonts(),
      padding: 8,
      maxRotation: 0,
      autoSkipPadding: 12
    }, extra || {});
  }

  function tooltipFor(t, opts) {
    return Object.assign({
      backgroundColor: t.mode === 'dark'
        ? 'color-mix(in srgb, ' + t.surface + ' 88%, white)'
        : 'color-mix(in srgb, ' + t.surface + ' 96%, black)',
      titleColor: t.ink,
      bodyColor: 'color-mix(in srgb, ' + t.ink + ' 82%, transparent)',
      /* Chart.js defaults the footer to white, which vanishes on the light
         surface. It carries context for the body above it (a range, a share),
         so it sits a step back from the body rather than matching it. */
      footerColor: 'color-mix(in srgb, ' + t.ink + ' 66%, transparent)',
      footerFont: Object.assign({ weight: '500' }, fonts()),
      footerMarginTop: 7,
      borderColor: 'color-mix(in srgb, ' + t.ink + ' 16%, transparent)',
      borderWidth: 1,
      cornerRadius: 10,
      padding: { top: 9, right: 12, bottom: 9, left: 11 },
      boxPadding: 5,
      boxWidth: 9,
      boxHeight: 9,
      usePointStyle: true,
      titleFont: Object.assign({ weight: '600' }, fonts()),
      bodyFont: fonts(),
      titleMarginBottom: 6,
      caretSize: 5,
      displayColors: true
    }, opts || {});
  }

  function legendFor(t, opts) {
    return Object.assign({
      position: 'top',
      align: 'end',
      labels: {
        color: 'color-mix(in srgb, ' + t.ink + ' 72%, transparent)',
        font: fonts(),
        boxWidth: 8,
        boxHeight: 8,
        usePointStyle: true,
        pointStyle: 'circle',
        padding: 14
      }
    }, opts || {});
  }

  /* The base every chart in the app starts from. Callers merge their own
     data-specific bits on top rather than restating layout and typography. */
  function base(overrides) {
    var t = tokens();
    var cfg = {
      responsive: true,
      maintainAspectRatio: false,
      animation: reducedMotion() ? false : { duration: 480, easing: 'easeOutQuart' },
      // Hovering anywhere in the column reads the whole column — the reader
      // should not have to hit a 2px line to see its value.
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: legendFor(t),
        tooltip: tooltipFor(t)
      },
      elements: {
        line: { borderWidth: 2, tension: 0.35 },
        point: { radius: 0, hoverRadius: 5, hitRadius: 14, borderWidth: 2 },
        bar: { borderRadius: 4, borderSkipped: false }
      }
    };
    return deepMerge(cfg, overrides || {});
  }

  /* Money-on-y, categories-on-x — the shape almost every chart here uses. */
  function moneyScales(t, opts) {
    opts = opts || {};
    return {
      x: {
        grid: { display: false },
        border: { display: false },
        ticks: ticksFor(t, opts.xTicks),
        stacked: !!opts.stacked
      },
      y: {
        grid: gridFor(t),
        border: { display: false },
        beginAtZero: opts.beginAtZero !== false,
        ticks: ticksFor(t, Object.assign({
          callback: function (v) { return moneyShort(v); },
          maxTicksLimit: 6
        }, opts.yTicks)),
        stacked: !!opts.stacked
      }
    };
  }

  function deepMerge(target, source) {
    Object.keys(source).forEach(function (key) {
      var sv = source[key], tv = target[key];
      if (sv && typeof sv === 'object' && !Array.isArray(sv) &&
          tv && typeof tv === 'object' && !Array.isArray(tv)) {
        deepMerge(tv, sv);
      } else {
        target[key] = sv;
      }
    });
    return target;
  }

  /* ── Gradient fill under a line ───────────────────────────────────────── */
  function areaFill(ctx, color, strength) {
    var chart = ctx.chart;
    var area = chart.chartArea;
    if (!area) return 'transparent';
    var g = chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
    var s = strength === undefined ? 0.22 : strength;
    g.addColorStop(0, hexAlpha(color, s));
    g.addColorStop(1, hexAlpha(color, 0));
    return g;
  }

  function hexAlpha(hex, alpha) {
    var h = String(hex).replace('#', '');
    if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
    if (h.length !== 6) return hex;
    return 'rgba(' + parseInt(h.slice(0,2),16) + ',' + parseInt(h.slice(2,4),16) + ','
                   + parseInt(h.slice(4,6),16) + ',' + alpha + ')';
  }

  /* ── Top-N + Other ────────────────────────────────────────────────────
     Past a handful of categories a chart stops being readable, and no
     palette fixes it. Fold the tail into one bucket that still reconciles
     to the true total. */
  function topN(pairs, limit) {
    var sorted = pairs.filter(function (p) { return p.value > 0; })
                      .sort(function (a, b) { return b.value - a.value; });
    if (sorted.length <= limit) return sorted;
    var head = sorted.slice(0, limit);
    var rest = sorted.slice(limit).reduce(function (s, p) { return s + p.value; }, 0);
    head.push({ label: 'Other', value: rest, other: true });
    return head;
  }

  global.CheckCharts = {
    CATEGORICAL: CATEGORICAL,
    SEQUENTIAL: SEQUENTIAL,
    mode: mode,
    tokens: tokens,
    palette: palette,
    registerCategories: registerCategories,
    forCategory: forCategory,
    hasOwnColor: hasOwnColor,
    slotOf: slotOf,
    orderBySlot: orderBySlot,
    money: money,
    moneyExact: moneyExact,
    moneyShort: moneyShort,
    monthLabel: monthLabel,
    dayLabel: dayLabel,
    fonts: fonts,
    base: base,
    moneyScales: moneyScales,
    gridFor: gridFor,
    ticksFor: ticksFor,
    tooltipFor: tooltipFor,
    legendFor: legendFor,
    areaFill: areaFill,
    hexAlpha: hexAlpha,
    topN: topN,
    reducedMotion: reducedMotion
  };
})(window);
