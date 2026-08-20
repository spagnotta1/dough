/* ══════════════════════════════════════════════════════════════════════════
   Investments behavior
   ──────────────────────────────────────────────────────────────────────────
   Everything is wired inside one init() that the SPA router calls again on
   every navigation, so the page must be safe to set up twice: every listener
   attaches to an element that was just rendered, and every chart destroys its
   predecessor before drawing.

   Charts are built lazily. A collapsed analytics panel has never created a
   Chart.js instance — on a page with seven panels that is the difference
   between seven canvases laid out on first paint and none.

   The Alpine component in the template still owns the manual-holding forms
   and the sync button; everything here is plain DOM.
   ══════════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  var LS_LAYOUT = 'check-inv-layout-v1';
  var LS_VIEW = 'check-inv-view-v1';

  /* Nothing starts open. The hero, the copilot and the story already answer
     the questions most visits are about; analytics is for the visit that
     wants more than that. */
  var DEFAULT_OPEN = [];

  var state = {
    charts: {},        // id -> Chart instance
    builders: {},      // id -> () => config
    showValues: {},    // id -> bool
    valueShapes: {},   // id -> shape hint for the label plugin
    layout: null,
    focusChart: null,
    lastFocused: null,
    data: null,
    alloc: 'asset_class',
    period: 'day',
    view: 'cards',
    sort: { key: 'value', dir: 'desc' },
    thread: [],        // [{role, content}] — client-side only, capped
    abort: null
  };

  /* ── Layout persistence ─────────────────────────────────────────────── */

  function loadLayout() {
    try {
      var raw = JSON.parse(localStorage.getItem(LS_LAYOUT));
      if (raw && typeof raw === 'object') {
        return {
          order: Array.isArray(raw.order) ? raw.order : [],
          open: Array.isArray(raw.open) ? raw.open : DEFAULT_OPEN.slice(),
          pinned: Array.isArray(raw.pinned) ? raw.pinned : [],
          hidden: Array.isArray(raw.hidden) ? raw.hidden : []
        };
      }
    } catch (e) { /* corrupt or unavailable storage falls back to defaults */ }
    return { order: [], open: DEFAULT_OPEN.slice(), pinned: [], hidden: [] };
  }

  function saveLayout() {
    try { localStorage.setItem(LS_LAYOUT, JSON.stringify(state.layout)); } catch (e) {}
  }

  function toggleIn(list, key, on) {
    var i = list.indexOf(key);
    if (on && i === -1) list.push(key);
    if (!on && i !== -1) list.splice(i, 1);
  }

  /* ── Formatting ─────────────────────────────────────────────────────── */

  function money(v) {
    var n = Math.abs(Math.round(v || 0));
    return (v < 0 ? '-$' : '$') + n.toLocaleString('en-US');
  }

  function pct(v, places) {
    return (v >= 0 ? '+' : '') + (v || 0).toFixed(places === undefined ? 2 : places) + '%';
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ── Count-up ────────────────────────────────────────────────────────
     Numbers rise to their value on first paint. Skipped under reduced
     motion — the figure is the point, the animation is decoration. */

  function countUp(el) {
    var target = parseFloat(el.getAttribute('data-countup'));
    if (!isFinite(target)) return;
    var format = el.getAttribute('data-format') || 'usd';
    var render = format === 'pct'
      ? function (v) { return v.toFixed(1) + '%'; }
      : format === 'int'
        ? function (v) { return String(Math.round(v)); }
        : money;

    if (window.CheckCharts && CheckCharts.reducedMotion()) {
      el.textContent = render(target);
      return;
    }

    var duration = 850;
    var start = null;
    function step(ts) {
      if (start === null) start = ts;
      var t = Math.min(1, (ts - start) / duration);
      var eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
      el.textContent = render(target * eased);
      if (t < 1) requestAnimationFrame(step);
      else el.textContent = render(target);
    }
    requestAnimationFrame(step);
  }

  function fillBars(root) {
    (root || document).querySelectorAll('[data-fill]').forEach(function (el) {
      var p = parseFloat(el.getAttribute('data-fill'));
      if (!isFinite(p)) return;
      // Two frames: the first commits width:0, the second animates to target.
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { el.style.width = Math.max(0, Math.min(100, p)) + '%'; });
      });
    });
  }

  function drawRings(root) {
    (root || document).querySelectorAll('[data-ring]').forEach(function (el) {
      var score = parseFloat(el.getAttribute('data-ring')) || 0;
      var offset = 264 * (1 - Math.max(0, Math.min(100, score)) / 100);
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { el.style.strokeDashoffset = offset; });
      });
    });
  }

  /* Allocation swatches read the same palette the donut does, so a row and
     its arc always agree. */
  function paintSwatches(root) {
    if (!window.CheckCharts) return;
    var P = CheckCharts.palette();
    (root || document).querySelectorAll('[data-swatches] [data-slot]').forEach(function (el) {
      el.style.background = P[parseInt(el.getAttribute('data-slot'), 10) % P.length];
    });
  }

  /* A ticker's mark colour is derived from the ticker itself, so the same
     holding keeps the same colour across sessions and sort orders. */
  function markColor(ticker) {
    var P = CheckCharts.palette();
    var h = 0;
    for (var i = 0; i < ticker.length; i++) h = (h * 31 + ticker.charCodeAt(i)) >>> 0;
    return P[h % P.length];
  }

  function paintMarks(root) {
    if (!window.CheckCharts) return;
    (root || document).querySelectorAll('[data-mark]').forEach(function (el) {
      var bg = markColor(el.getAttribute('data-mark') || '?');
      el.style.background = bg;
      // The palette hue is arbitrary, so the ink has to be chosen against it:
      // a fixed white was unreadable on the light hues several themes carry.
      el.style.color = window.CheckScheme ? CheckScheme.onColor(bg) : '#fff';
    });
  }

  /* ══════════════════════════════════════════════════════════════════════
     Charts
     ══════════════════════════════════════════════════════════════════════ */

  function destroyChart(id) {
    var el = document.getElementById(id);
    if (el && typeof Chart !== 'undefined' && Chart.getChart) {
      var existing = Chart.getChart(el);
      if (existing) existing.destroy();
    }
    delete state.charts[id];
  }

  function withValueLabels(id, cfg, shape) {
    var VL = window.ChartValueLabels;
    if (!VL) return cfg;
    shape = shape || {};
    state.valueShapes[id] = shape;
    var on = !!state.showValues[id];
    var t = CheckCharts.tokens();
    cfg.options = cfg.options || {};
    cfg.options.plugins = cfg.options.plugins || {};
    cfg.options.plugins.valueLabels = { enabled: on, format: 'usd0', ink: t.ink, size: 10.5 };
    cfg.options.layout = Object.assign({}, cfg.options.layout, {
      padding: VL.pad({
        enabled: on, base: shape.base,
        donut: !!shape.donut, horizontal: !!shape.horizontal, negatives: !!shape.negatives
      })
    });
    return cfg;
  }

  function ensureChart(id) {
    if (state.charts[id]) return state.charts[id];
    var build = state.builders[id];
    var el = document.getElementById(id);
    if (!build || !el) return null;
    destroyChart(id);
    state.charts[id] = new Chart(el, build());
    return state.charts[id];
  }

  function buildChartBuilders(data) {
    var B = state.builders;

    /* ── Portfolio value over time ──────────────────────────────────────
       The only chart on this page drawn purely from measurement. Cash and
       investments are stacked so the reader can see which half moved. */
    B.invPerfChart = function () {
      var t = CheckCharts.tokens();
      var P = CheckCharts.palette();
      var hist = data.history || [];
      var labels = hist.map(function (h) { return h.date; });
      return withValueLabels('invPerfChart', {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Investments',
              data: hist.map(function (h) { return h.total_investments; }),
              borderColor: P[2],
              backgroundColor: function (ctx) { return CheckCharts.areaFill(ctx, P[2], 0.2); },
              fill: true,
              pointRadius: 0
            },
            {
              label: 'Cash',
              data: hist.map(function (h) { return h.total_cash; }),
              borderColor: P[0],
              backgroundColor: function (ctx) { return CheckCharts.areaFill(ctx, P[0], 0.16); },
              fill: true,
              pointRadius: 0
            }
          ]
        },
        options: CheckCharts.base({
          scales: CheckCharts.moneyScales(t, {
            beginAtZero: false,
            xTicks: { callback: function (v, i) { return CheckCharts.dayLabel(labels[i]); }, maxTicksLimit: 8 }
          }),
          plugins: {
            legend: CheckCharts.legendFor(t),
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: {
                title: function (items) { return CheckCharts.dayLabel(items[0].label); },
                label: function (c) { return c.dataset.label + ': ' + CheckCharts.money(c.parsed.y); }
              }
            })
          }
        })
      }, { base: 12 });
    };

    /* ── Allocation donut ───────────────────────────────────────────── */
    B.invAllocChart = function () {
      var t = CheckCharts.tokens();
      var P = CheckCharts.palette();
      var buckets = ((data.allocation || {})[state.alloc] || {}).buckets || [];
      return withValueLabels('invAllocChart', {
        type: 'doughnut',
        data: {
          labels: buckets.map(function (b) { return b.label; }),
          datasets: [{
            data: buckets.map(function (b) { return b.value; }),
            backgroundColor: buckets.map(function (_, i) { return P[i % P.length]; }),
            borderWidth: 2,
            borderColor: t.surface
          }]
        },
        options: CheckCharts.base({
          cutout: '62%',
          plugins: {
            legend: { display: false },
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: {
                label: function (c) {
                  var total = c.dataset.data.reduce(function (s, v) { return s + v; }, 0);
                  return c.label + ': ' + CheckCharts.money(c.raw) +
                    ' (' + (total ? (c.raw / total * 100).toFixed(1) : '0') + '%)';
                }
              }
            })
          }
        })
      }, { donut: true, base: 6 });
    };

    /* ── Net-worth donut (the original chart, preserved) ─────────────── */
    B.portfolioDonut = function () {
      var t = CheckCharts.tokens();
      var P = CheckCharts.palette();
      // [label, value] pairs rather than an object: arc colours are assigned by
      // position and have to line up with the server-rendered legend rows, so
      // the order has to survive JSON serialisation.
      var mix = data.netWorthMix || [];
      return withValueLabels('portfolioDonut', {
        type: 'doughnut',
        data: {
          labels: mix.map(function (e) { return e[0]; }),
          datasets: [{
            data: mix.map(function (e) { return e[1]; }),
            backgroundColor: mix.map(function (_, i) { return P[i % P.length]; }),
            borderWidth: 2,
            borderColor: t.surface
          }]
        },
        options: CheckCharts.base({
          cutout: '62%',
          plugins: {
            legend: { display: false },
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: { label: function (c) { return c.label + ': ' + CheckCharts.money(c.raw); } }
            })
          }
        })
      }, { donut: true, base: 6 });
    };

    /* ── Benchmark ──────────────────────────────────────────────────────
       Indexed to 100 rather than plotted in dollars: the question is
       "which line rose faster", and dollars answer a different one. The
       reference is dashed because it is modelled, not measured — the same
       cue the basis note states in words. */
    B.invBenchChart = function () {
      var t = CheckCharts.tokens();
      var P = CheckCharts.palette();
      var bench = data.benchmark || {};
      if (!bench.available) return { type: 'line', data: { labels: [], datasets: [] } };
      var labels = bench.portfolio.map(function (p) { return p.date; });
      return withValueLabels('invBenchChart', {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Your portfolio',
              data: bench.portfolio.map(function (p) { return p.index; }),
              borderColor: P[0],
              backgroundColor: function (ctx) { return CheckCharts.areaFill(ctx, P[0], 0.18); },
              fill: true,
              pointRadius: 0
            },
            {
              label: bench.benchmark + ' (modelled)',
              data: bench.reference.map(function (p) { return p.index; }),
              borderColor: P[1],
              borderDash: [5, 4],
              fill: false,
              pointRadius: 0
            }
          ]
        },
        options: CheckCharts.base({
          scales: {
            x: {
              grid: { display: false }, border: { display: false },
              ticks: CheckCharts.ticksFor(t, {
                callback: function (v, i) { return CheckCharts.dayLabel(labels[i]); },
                maxTicksLimit: 8
              })
            },
            y: {
              grid: CheckCharts.gridFor(t), border: { display: false },
              beginAtZero: false,
              ticks: CheckCharts.ticksFor(t, { callback: function (v) { return v.toFixed(0); } })
            }
          },
          plugins: {
            legend: CheckCharts.legendFor(t),
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: {
                title: function (items) { return CheckCharts.dayLabel(items[0].label); },
                label: function (c) { return c.dataset.label + ': ' + c.parsed.y.toFixed(1); },
                footer: function () { return 'Indexed to 100 at the start of the period'; }
              }
            })
          }
        })
      }, { base: 12 });
    };

    /* ── Dividend contributors ─────────────────────────────────────────── */
    B.invDivChart = function () {
      var t = CheckCharts.tokens();
      var P = CheckCharts.palette();
      var rows = (data.dividends || {}).contributors || [];
      return withValueLabels('invDivChart', {
        type: 'bar',
        data: {
          labels: rows.map(function (r) { return r.ticker; }),
          datasets: [{
            label: 'Estimated annual income',
            data: rows.map(function (r) { return r.income; }),
            backgroundColor: rows.map(function (r, i) {
              // A holding whose yield came from a class default rather than
              // the table is drawn muted, so the estimate is visible as one.
              return r.estimated ? CheckCharts.hexAlpha(P[i % P.length], 0.45) : P[i % P.length];
            })
          }]
        },
        options: CheckCharts.base({
          indexAxis: 'y',
          scales: {
            x: {
              grid: CheckCharts.gridFor(t), border: { display: false }, beginAtZero: true,
              ticks: CheckCharts.ticksFor(t, { callback: function (v) { return CheckCharts.moneyShort(v); } })
            },
            y: { grid: { display: false }, border: { display: false }, ticks: CheckCharts.ticksFor(t) }
          },
          plugins: {
            legend: { display: false },
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: {
                label: function (c) {
                  var row = rows[c.dataIndex];
                  return CheckCharts.money(c.parsed.x) + ' a year at ' + row.yield_pct + '%';
                },
                footer: function (items) {
                  return rows[items[0].dataIndex].estimated
                    ? 'Yield from an asset-class default' : 'Yield from the reference table';
                }
              }
            })
          }
        })
      }, { horizontal: true, base: 10 });
    };

    /* ── Projection with confidence band ─────────────────────────────────
       The band is drawn first and filled between its bounds, so the eye
       reads a range rather than three competing lines. Contributions are a
       dashed floor: the part of the outcome that is not a market bet. */
    B.invProjChart = function () {
      var t = CheckCharts.tokens();
      var P = CheckCharts.palette();
      var pts = (data.projection || {}).points || [];
      var labels = pts.map(function (p) { return 'Yr ' + Math.round(p.year); });
      return withValueLabels('invProjChart', {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            // The band is one idea, so it gets one legend entry on its upper
            // bound and the lower bound is filtered out below. Labelling both
            // "Upper"/"Lower" put a lone grey "Lower (80%)" chip in the
            // legend, which reads as a fourth series rather than a range.
            {
              label: '80% range',
              data: pts.map(function (p) { return p.high; }),
              borderColor: CheckCharts.hexAlpha(P[2], 0.4),
              borderWidth: 1,
              backgroundColor: CheckCharts.hexAlpha(P[2], 0.16),
              fill: '+1',
              pointRadius: 0
            },
            {
              label: 'Lower bound',
              data: pts.map(function (p) { return p.low; }),
              borderColor: CheckCharts.hexAlpha(P[2], 0.4),
              borderWidth: 1,
              fill: false,
              pointRadius: 0
            },
            {
              label: 'Expected',
              data: pts.map(function (p) { return p.expected; }),
              borderColor: P[2],
              fill: false,
              pointRadius: 0,
              borderWidth: 2.5
            },
            {
              label: 'Contributed',
              data: pts.map(function (p) { return p.contributed; }),
              borderColor: P[3],
              borderDash: [5, 4],
              fill: false,
              pointRadius: 0
            }
          ]
        },
        options: CheckCharts.base({
          scales: CheckCharts.moneyScales(t, { beginAtZero: false }),
          plugins: {
            legend: CheckCharts.legendFor(t, {
              labels: Object.assign({}, CheckCharts.legendFor(t).labels, {
                filter: function (item) { return item.text !== 'Lower bound'; }
              })
            }),
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: {
                label: function (c) { return c.dataset.label + ': ' + CheckCharts.money(c.parsed.y); },
                footer: function () { return 'Modelled, not a prediction'; }
              }
            })
          }
        })
      }, { base: 12 });
    };
  }

  /* ── Chart tools: value labels, expand, export ──────────────────────── */

  function toggleValues(id) {
    state.showValues[id] = !state.showValues[id];
    document.querySelectorAll('[data-values="' + id + '"]').forEach(function (b) {
      b.setAttribute('aria-pressed', state.showValues[id] ? 'true' : 'false');
    });
    if (state.charts[id]) { destroyChart(id); ensureChart(id); }
    if (state.focusChart && document.getElementById('invFocus').getAttribute('data-chart') === id) {
      state.focusChart.destroy();
      state.focusChart = new Chart(document.getElementById('invFocusCanvas'), state.builders[id]());
    }
  }

  function initChartTools(root) {
    root.querySelectorAll('[data-values]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        toggleValues(btn.getAttribute('data-values'));
      });
    });
    root.querySelectorAll('[data-expand]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        openFocus(btn.getAttribute('data-expand'), btn.getAttribute('data-title') || '');
      });
    });
  }

  function openFocus(chartId, title) {
    var modal = document.getElementById('invFocus');
    if (!modal || !state.builders[chartId]) return;
    state.lastFocused = document.activeElement;
    modal.setAttribute('data-chart', chartId);
    document.getElementById('invFocusTitle').textContent = title;
    document.getElementById('invFocusValues')
      .setAttribute('aria-pressed', state.showValues[chartId] ? 'true' : 'false');
    modal.hidden = false;
    if (state.focusChart) { state.focusChart.destroy(); state.focusChart = null; }
    state.focusChart = new Chart(document.getElementById('invFocusCanvas'), state.builders[chartId]());
    var close = modal.querySelector('[data-close-focus]');
    if (close) close.focus();
  }

  function closeFocus() {
    var modal = document.getElementById('invFocus');
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    if (state.focusChart) { state.focusChart.destroy(); state.focusChart = null; }
    if (state.lastFocused && state.lastFocused.focus) state.lastFocused.focus();
  }

  function initFocus() {
    var modal = document.getElementById('invFocus');
    if (!modal) return;
    modal.querySelectorAll('[data-close-focus]').forEach(function (el) {
      el.addEventListener('click', closeFocus);
    });
    var values = document.getElementById('invFocusValues');
    if (values) {
      values.addEventListener('click', function () {
        toggleValues(modal.getAttribute('data-chart'));
      });
    }
    var exp = document.getElementById('invFocusExport');
    if (exp) {
      exp.addEventListener('click', function () {
        if (!state.focusChart) return;
        var a = document.createElement('a');
        a.href = state.focusChart.toBase64Image('image/png', 1);
        a.download = (modal.getAttribute('data-chart') || 'chart') + '.png';
        a.click();
      });
    }
  }

  /* ══════════════════════════════════════════════════════════════════════
     Analytics panels
     ══════════════════════════════════════════════════════════════════════ */

  function panelEl(key) { return document.querySelector('[data-panel="' + key + '"]'); }

  function chartIdIn(key) {
    var el = panelEl(key);
    if (!el) return null;
    var canvas = el.querySelector('canvas');
    return canvas ? canvas.id : null;
  }

  function setOpen(key, open) {
    var el = panelEl(key);
    if (!el) return;
    var body = el.querySelector('.inv-panel__body');
    var toggle = el.querySelector('[data-toggle]');
    if (!body || !toggle) return;
    body.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggleIn(state.layout.open, key, open);
    if (open) {
      // First reveal is the first time this panel costs anything. A chart
      // that throws must not take the rest of the layout with it — "Expand
      // all" used to abort on the first bad canvas and leave every panel
      // after it shut, with no clue why.
      var id = chartIdIn(key);
      if (id) {
        try { ensureChart(id); }
        catch (e) { console.error('[investments] chart "' + id + '" failed to build:', e); }
      }
      fillBars(body);
      paintSwatches(body);
    }
  }

  function setPinned(key, pinned) {
    var el = panelEl(key);
    if (!el) return;
    var btn = el.querySelector('[data-pin]');
    if (btn) btn.setAttribute('aria-pressed', pinned ? 'true' : 'false');
    toggleIn(state.layout.pinned, key, pinned);
    if (pinned) setOpen(key, true);
  }

  function setHidden(key, hidden) {
    var el = panelEl(key);
    if (!el) return;
    el.hidden = hidden;
    toggleIn(state.layout.hidden, key, hidden);
    renderHiddenTray();
  }

  function renderHiddenTray() {
    var tray = document.getElementById('invHiddenTray');
    var items = document.getElementById('invHiddenItems');
    if (!tray || !items) return;
    items.innerHTML = '';
    state.layout.hidden.forEach(function (key) {
      var el = panelEl(key);
      if (!el) return;
      var title = el.querySelector('.inv-panel__title');
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ds-chip';
      btn.textContent = 'Restore ' + (title ? title.textContent : key);
      btn.addEventListener('click', function () { setHidden(key, false); saveLayout(); });
      items.appendChild(btn);
    });
    tray.hidden = state.layout.hidden.length === 0;
  }

  function applyOrder() {
    var wrap = document.getElementById('invPanels');
    if (!wrap || !state.layout.order.length) return;
    state.layout.order.forEach(function (key) {
      var el = panelEl(key);
      if (el) wrap.appendChild(el);
    });
  }

  function captureOrder() {
    var wrap = document.getElementById('invPanels');
    if (!wrap) return;
    state.layout.order = Array.prototype.map.call(
      wrap.querySelectorAll('[data-panel]'),
      function (el) { return el.getAttribute('data-panel'); });
  }

  function initPanels() {
    var wrap = document.getElementById('invPanels');
    if (!wrap) return;

    applyOrder();

    wrap.querySelectorAll('[data-panel]').forEach(function (el) {
      var key = el.getAttribute('data-panel');
      var toggle = el.querySelector('[data-toggle]');
      var pin = el.querySelector('[data-pin]');
      var hide = el.querySelector('[data-hide]');

      if (toggle) {
        toggle.addEventListener('click', function () {
          var open = toggle.getAttribute('aria-expanded') === 'true';
          // A pinned panel is pinned *open*; collapsing it would silently
          // contradict the pin, so the pin is released first.
          if (open && state.layout.pinned.indexOf(key) !== -1) setPinned(key, false);
          setOpen(key, !open);
          saveLayout();
        });
      }
      if (pin) {
        pin.addEventListener('click', function (e) {
          e.stopPropagation();
          setPinned(key, pin.getAttribute('aria-pressed') !== 'true');
          saveLayout();
        });
      }
      if (hide) {
        hide.addEventListener('click', function (e) {
          e.stopPropagation();
          setHidden(key, true);
          saveLayout();
        });
      }

      // Drag to reorder, from the grip only — dragging from anywhere would
      // make selecting text inside a panel impossible.
      var grip = el.querySelector('.inv-panel__grip');
      if (grip) {
        grip.addEventListener('mousedown', function () { el.draggable = true; });
        grip.addEventListener('mouseup', function () { el.draggable = false; });
      }
      el.addEventListener('dragstart', function (e) {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', key);
        el.classList.add('is-dragging');
      });
      el.addEventListener('dragend', function () {
        el.classList.remove('is-dragging');
        el.draggable = false;
        wrap.querySelectorAll('.is-drop-target').forEach(function (n) {
          n.classList.remove('is-drop-target');
        });
        captureOrder();
        saveLayout();
      });
      el.addEventListener('dragover', function (e) {
        e.preventDefault();
        el.classList.add('is-drop-target');
      });
      el.addEventListener('dragleave', function () { el.classList.remove('is-drop-target'); });
      el.addEventListener('drop', function (e) {
        e.preventDefault();
        el.classList.remove('is-drop-target');
        var dragged = panelEl(e.dataTransfer.getData('text/plain'));
        if (!dragged || dragged === el) return;
        var after = dragged.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING;
        wrap.insertBefore(dragged, after ? el.nextSibling : el);
        captureOrder();
        saveLayout();
      });
    });

    // Restore saved state.
    state.layout.hidden.slice().forEach(function (key) { setHidden(key, true); });
    state.layout.pinned.slice().forEach(function (key) { setPinned(key, true); });
    state.layout.open.slice().forEach(function (key) { setOpen(key, true); });
    renderHiddenTray();

    var expandAll = document.getElementById('invExpandAll');
    if (expandAll) {
      expandAll.addEventListener('click', function () {
        var keys = Array.prototype.map.call(wrap.querySelectorAll('[data-panel]'),
          function (el) { return el.getAttribute('data-panel'); });
        var allOpen = keys.every(function (k) { return state.layout.open.indexOf(k) !== -1; });
        keys.forEach(function (k) { setOpen(k, !allOpen); });
        expandAll.textContent = allOpen ? 'Expand all' : 'Collapse all';
        saveLayout();
      });
    }

    var reset = document.getElementById('invResetLayout');
    if (reset) {
      reset.addEventListener('click', function () {
        try { localStorage.removeItem(LS_LAYOUT); } catch (e) {}
        location.reload();
      });
    }

    // Opening a panel from elsewhere on the page (the hero rings).
    document.querySelectorAll('[data-open-panel]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        revealPanel(btn.getAttribute('data-open-panel'));
      });
    });

    // Arriving from another page — /investments#cash comes off the
    // dashboard's Cash available tile. A collapsed or hidden panel would
    // otherwise leave the visitor staring at the spot where their answer
    // should be, so the hash opens it rather than merely scrolling to it.
    revealPanel(location.hash.slice(1));
    window.addEventListener('hashchange', function () {
      revealPanel(location.hash.slice(1));
    });
  }

  /* Unhide, open and scroll to one panel. Silently ignores keys that are
     not panels, so any other hash on the page still behaves natively. */
  function revealPanel(key) {
    if (!key) return;
    var el = panelEl(key);
    if (!el) return;
    setHidden(key, false);
    setOpen(key, true);
    saveLayout();
    el.scrollIntoView({ behavior: CheckCharts.reducedMotion() ? 'auto' : 'smooth', block: 'center' });
  }

  /* ══════════════════════════════════════════════════════════════════════
     Hero period toggle
     Every window is already computed server-side, so switching is a text
     swap rather than a request. Windows the history cannot support are
     disabled in the markup rather than shown as zero.
     ══════════════════════════════════════════════════════════════════════ */

  var PERIOD_LABEL = {
    day: 'portfolio, since', week: 'portfolio, since', month: 'portfolio, since',
    quarter: 'portfolio, since', ytd: 'portfolio, year to date from',
    year: 'portfolio, since', all: 'portfolio, since'
  };

  function renderPeriod(key) {
    var windows = (state.data.performance || {}).windows || {};
    var w = windows[key];
    var deltaEl = document.getElementById('invHeroDelta');
    var windowEl = document.getElementById('invHeroWindow');
    if (!deltaEl || !w || !w.available) return;

    var good = w.change >= 0;
    deltaEl.innerHTML =
      '<span class="ds-delta ' + (good ? 'ds-delta--up' : 'ds-delta--down') + '">' +
        '<span aria-hidden="true">' + (good ? '▲' : '▼') + '</span> ' +
        '<span class="ds-num">' + escapeHtml(money(Math.abs(w.change))) +
        (w.change_pct === null ? '' : ' (' + pct(w.change_pct) + ')') + '</span>' +
      '</span>';
    if (windowEl) {
      windowEl.textContent = (PERIOD_LABEL[key] || 'since') + ' ' + w.from_date;
    }
    state.period = key;
  }

  function initPeriods() {
    document.querySelectorAll('[data-period]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (btn.disabled) return;
        document.querySelectorAll('[data-period]').forEach(function (b) {
          b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
        });
        renderPeriod(btn.getAttribute('data-period'));
      });
    });
  }

  /* ══════════════════════════════════════════════════════════════════════
     Allocation dimension switch
     ══════════════════════════════════════════════════════════════════════ */

  var ALLOC_BASIS = {
    asset_class: 'Asset classes come from the sync feed itself.',
    sector: 'Sector labels come from a built-in reference table, not a market data feed. ' +
            'Coverage: {c}% of portfolio value could be classified.',
    region: 'Region labels come from a built-in reference table. Coverage: {c}% of value.',
    market_cap: 'Market-cap bands come from a built-in reference table. Coverage: {c}% of value.',
    account: 'Accounts come from the sync feed and your manual entries.'
  };

  function renderAlloc(key) {
    var alloc = (state.data.allocation || {})[key];
    if (!alloc) return;
    state.alloc = key;

    var P = CheckCharts.palette();
    var list = document.getElementById('invAllocList');
    if (list) {
      var html = '<ul class="inv-alloc__list" data-swatches="invAllocChart">';
      alloc.buckets.forEach(function (b, i) {
        html += '<li class="inv-alloc__row">' +
          '<span class="inv-alloc__swatch" style="background:' + P[i % P.length] + '"></span>' +
          '<span class="inv-alloc__label">' + escapeHtml(b.label) + '</span>' +
          '<span class="inv-alloc__value ds-num">' + escapeHtml(money(b.value)) + '</span>' +
          '<span class="inv-alloc__pct ds-num">' + b.pct.toFixed(1) + '%</span>' +
        '</li>';
      });
      list.innerHTML = html + '</ul>';
    }

    var basis = document.getElementById('invAllocBasis');
    if (basis) {
      var span = basis.querySelector('span');
      if (span) {
        span.textContent = (ALLOC_BASIS[key] || '')
          .replace('{c}', alloc.coverage.toFixed(0));
      }
    }

    destroyChart('invAllocChart');
    if (!document.getElementById('inv-panel-allocation').hidden) ensureChart('invAllocChart');
  }

  function initAlloc() {
    document.querySelectorAll('[data-alloc]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        document.querySelectorAll('[data-alloc]').forEach(function (b) {
          b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
        });
        renderAlloc(btn.getAttribute('data-alloc'));
      });
    });
  }

  /* ══════════════════════════════════════════════════════════════════════
     Holdings — filter, view, sort, detail
     ══════════════════════════════════════════════════════════════════════ */

  function applyFilter() {
    var input = document.getElementById('invSearch');
    var q = (input ? input.value : '').trim().toLowerCase();
    var shown = 0, total = 0;

    ['#invCards [data-position]', '#invTableBody [data-position]'].forEach(function (sel) {
      document.querySelectorAll(sel).forEach(function (el) {
        var match = !q || (el.getAttribute('data-search') || '').indexOf(q) !== -1;
        el.hidden = !match;
      });
    });

    document.querySelectorAll('#invCards [data-position]').forEach(function (el) {
      total++;
      if (!el.hidden) shown++;
    });

    var count = document.getElementById('invCount');
    if (count) {
      count.textContent = q
        ? shown + ' of ' + total + ' holdings'
        : total + ' holding' + (total === 1 ? '' : 's');
    }
  }

  function setView(view) {
    state.view = view;
    var cards = document.getElementById('invCards');
    var table = document.getElementById('invTableWrap');
    if (cards) cards.hidden = view !== 'cards';
    if (table) table.hidden = view !== 'table';
    document.querySelectorAll('[data-view]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-view') === view ? 'true' : 'false');
    });
    try { localStorage.setItem(LS_VIEW, view); } catch (e) {}
  }

  function sortTable(key) {
    var body = document.getElementById('invTableBody');
    if (!body) return;
    var dir = (state.sort.key === key && state.sort.dir === 'desc') ? 'asc' : 'desc';
    state.sort = { key: key, dir: dir };

    var positions = state.data.positions || [];
    var rows = Array.prototype.slice.call(body.querySelectorAll('[data-position]'));
    rows.sort(function (a, b) {
      var pa = positions[parseInt(a.getAttribute('data-position'), 10)] || {};
      var pb = positions[parseInt(b.getAttribute('data-position'), 10)] || {};
      var va = pa[key], vb = pb[key];
      // Positions without a gain sort last in either direction — a missing
      // cost basis is not the same claim as a zero gain.
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      if (typeof va === 'string') return dir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
      return dir === 'asc' ? va - vb : vb - va;
    });
    rows.forEach(function (r) { body.appendChild(r); });

    document.querySelectorAll('[data-sort]').forEach(function (b) {
      b.setAttribute('aria-sort',
        b.getAttribute('data-sort') === key ? (dir === 'asc' ? 'ascending' : 'descending') : 'none');
    });
  }

  function detailRow(label, value, tone) {
    return '<div class="inv-detail">' +
      '<p class="ds-eyebrow">' + escapeHtml(label) + '</p>' +
      '<p class="inv-detail__value ds-num' + (tone ? ' inv-detail__value--' + tone : '') + '">' +
        escapeHtml(value) + '</p></div>';
  }

  function openSheet(index) {
    var p = (state.data.positions || [])[index];
    var sheet = document.getElementById('invSheet');
    if (!p || !sheet) return;

    state.lastFocused = document.activeElement;
    document.getElementById('invSheetTitle').textContent = p.ticker;
    document.getElementById('invSheetSub').textContent =
      p.name + ' · ' + p.account + (p.source === 'sync' ? ' · synced' : ' · entered manually');

    var gainTone = p.gain === null ? '' : (p.gain >= 0 ? 'up' : 'down');
    var html =
      '<div class="inv-detail-grid">' +
        detailRow('Market value', money(p.value)) +
        detailRow('Portfolio weight', p.weight.toFixed(2) + '%') +
        detailRow('Shares', p.shares ? p.shares.toFixed(4) : '—') +
        detailRow('Price', p.price === null ? '—' : money(p.price)) +
        detailRow('Average cost', p.avg_cost === null ? '—' : money(p.avg_cost)) +
        detailRow('Cost basis', p.cost_basis === null ? '—' : money(p.cost_basis)) +
        detailRow('Unrealized gain', p.gain === null ? 'No basis reported' : money(p.gain), gainTone) +
        detailRow('Return', p.gain_pct === null ? '—' : pct(p.gain_pct, 1), gainTone) +
      '</div>';

    html += '<div>' +
      '<p class="ds-eyebrow" style="margin-bottom: var(--sp-2)">Classification</p>' +
      '<div class="inv-detail-grid">' +
        detailRow('Asset class', p.asset_class) +
        detailRow('Sector', p.sector_known ? p.sector : 'Unclassified') +
        detailRow('Region', p.region_known ? p.region : 'Unclassified') +
        detailRow('Market cap', p.market_cap_known ? p.market_cap : 'Unclassified') +
      '</div>' +
      '<p class="inv-basis"><span>Sector, region and market cap come from a built-in ' +
        'reference table, not a market data feed.</span></p>' +
    '</div>';

    html += '<div>' +
      '<p class="ds-eyebrow" style="margin-bottom: var(--sp-2)">Estimated income</p>' +
      '<div class="inv-detail-grid">' +
        detailRow('Yield', p.yield_pct.toFixed(2) + '%') +
        detailRow('Annual income', money(p.income)) +
      '</div>' +
      '<p class="inv-basis"><span>' +
        (p.yield_known
          ? 'Yield from the reference table for ' + escapeHtml(p.ticker) + '.'
          : 'No yield on file for ' + escapeHtml(p.ticker) + ' — using the ' +
            escapeHtml(p.asset_class) + ' default, so treat this as a rough figure.') +
      '</span></p>' +
    '</div>';

    if (p.last_synced_at) {
      html += '<p class="ds-meta">Last synced ' + escapeHtml(p.last_synced_at) + '</p>';
    }

    document.getElementById('invSheetBody').innerHTML = html;

    var foot = document.getElementById('invSheetFoot');
    foot.innerHTML = '';
    if (p.source === 'sync') {
      var note = document.createElement('span');
      note.className = 'ds-meta';
      note.textContent = 'Synced automatically — manage it from Connections.';
      foot.appendChild(note);
    } else {
      var edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'ds-btn ds-btn--primary ds-btn--sm';
      edit.textContent = 'Edit holding';
      edit.addEventListener('click', function () { closeSheet(); window.editHolding(p.id); });
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'ds-btn ds-btn--danger ds-btn--sm';
      del.textContent = 'Delete';
      del.addEventListener('click', function () { window.deleteHolding(p.id); });
      foot.appendChild(edit);
      foot.appendChild(del);
    }
    var ask = document.createElement('button');
    ask.type = 'button';
    ask.className = 'ds-btn ds-btn--ghost ds-btn--sm';
    ask.textContent = 'Ask the copilot';
    ask.addEventListener('click', function () {
      closeSheet();
      ask_('Tell me about my ' + p.ticker + ' position — is it sized right?');
    });
    foot.appendChild(ask);

    sheet.hidden = false;
    var close = sheet.querySelector('[data-close-sheet]');
    if (close) close.focus();
  }

  function closeSheet() {
    var sheet = document.getElementById('invSheet');
    if (!sheet || sheet.hidden) return;
    sheet.hidden = true;
    if (state.lastFocused && state.lastFocused.focus) state.lastFocused.focus();
  }

  function initHoldings() {
    var search = document.getElementById('invSearch');
    if (search) search.addEventListener('input', applyFilter);

    document.querySelectorAll('[data-view]').forEach(function (btn) {
      btn.addEventListener('click', function () { setView(btn.getAttribute('data-view')); });
    });
    var saved = 'cards';
    try { saved = localStorage.getItem(LS_VIEW) || 'cards'; } catch (e) {}
    setView(saved === 'table' ? 'table' : 'cards');

    document.querySelectorAll('[data-sort]').forEach(function (btn) {
      btn.addEventListener('click', function () { sortTable(btn.getAttribute('data-sort')); });
    });

    ['#invCards', '#invTableBody'].forEach(function (sel) {
      var root = document.querySelector(sel);
      if (!root) return;
      root.addEventListener('click', function (e) {
        if (e.target.closest('button.ds-btn')) return;   // row actions win
        var el = e.target.closest('[data-position]');
        if (el) openSheet(parseInt(el.getAttribute('data-position'), 10));
      });
    });

    var sheet = document.getElementById('invSheet');
    if (sheet) {
      sheet.querySelectorAll('[data-close-sheet]').forEach(function (el) {
        el.addEventListener('click', closeSheet);
      });
    }

    applyFilter();
  }

  /* ══════════════════════════════════════════════════════════════════════
     Insights — "why" disclosure
     ══════════════════════════════════════════════════════════════════════ */

  function initInsights() {
    document.querySelectorAll('[data-why]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var target = document.getElementById(btn.getAttribute('data-why'));
        if (!target) return;
        var open = !target.hidden;
        target.hidden = open;
        btn.setAttribute('aria-expanded', open ? 'false' : 'true');
        btn.textContent = open ? 'Why?' : 'Hide';
      });
    });
  }

  /* ══════════════════════════════════════════════════════════════════════
     Wealth copilot
     ══════════════════════════════════════════════════════════════════════ */

  /* A deliberately small Markdown subset — bold, inline code, bullets,
     paragraphs. Everything is escaped first, so model output can never
     inject markup. */
  function renderLite(md) {
    var esc = escapeHtml(md);
    var lines = esc.split('\n');
    var html = '';
    var inList = false;
    lines.forEach(function (line) {
      var bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
      if (bullet) {
        if (!inList) { html += '<ul>'; inList = true; }
        html += '<li>' + inline(bullet[1]) + '</li>';
        return;
      }
      if (inList) { html += '</ul>'; inList = false; }
      if (line.trim()) html += '<p>' + inline(line) + '</p>';
    });
    if (inList) html += '</ul>';
    return html;

    function inline(s) {
      return s
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/`([^`]+)`/g, '<code>$1</code>');
    }
  }

  function loadBrief() {
    var narrative = document.getElementById('invNarrative');
    var loading = document.getElementById('invNarrativeLoading');
    if (!narrative) return;

    fetch('/api/investments/brief')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (loading) loading.remove();
        if (!data || !data.available) {
          // No key configured, or the model could not be reached. Every hard
          // number on this page was rendered server-side and stands alone.
          narrative.remove();
          return;
        }
        if (data.narrative) narrative.innerHTML = renderLite(data.narrative);
        else narrative.remove();

        var opps = data.opportunities || [];
        if (opps.length) {
          var wrap = document.getElementById('invOpps');
          wrap.hidden = false;
          wrap.innerHTML = '';
          opps.forEach(function (o, i) {
            var el = document.createElement('div');
            el.className = 'inv-opp';
            el.style.setProperty('--ds-delay', (i * 70) + 'ms');
            el.innerHTML =
              '<span class="inv-opp__num">' + (i + 1) + '</span>' +
              '<div style="flex:1;min-width:0">' +
                '<p class="inv-opp__title"></p>' +
                '<p class="inv-opp__detail"></p>' +
              '</div>' +
              (o.impact ? '<span class="ds-badge ds-badge--ai inv-opp__impact"></span>' : '');
            el.querySelector('.inv-opp__title').textContent = o.title || '';
            el.querySelector('.inv-opp__detail').textContent = o.detail || '';
            if (o.impact) el.querySelector('.inv-opp__impact').textContent = o.impact;
            wrap.appendChild(el);
          });
        }

        var questions = data.questions || [];
        if (questions.length) {
          var suggest = document.getElementById('invSuggest');
          suggest.innerHTML = '';
          questions.slice(0, 5).forEach(function (q) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'ds-chip ds-chip--ai';
            b.setAttribute('data-ask', q);
            b.textContent = q;
            suggest.appendChild(b);
          });
        }
      })
      .catch(function () {
        if (loading) loading.remove();
        narrative.remove();
      });
  }

  /* Named with a trailing underscore because `ask` is also a local in
     openSheet, and shadowing it there silently broke the detail button. */
  function ask_(question) {
    question = (question || '').trim();
    if (!question) return;

    var thread = document.getElementById('invThread');
    var foot = document.getElementById('invThreadFoot');
    var input = document.getElementById('invAskInput');
    if (!thread) return;

    if (state.abort) state.abort.abort();
    state.abort = new AbortController();

    thread.hidden = false;
    if (foot) foot.hidden = false;
    if (input) input.value = '';

    var turn = document.createElement('div');
    turn.className = 'inv-turn';
    var q = document.createElement('div');
    q.className = 'inv-turn__q';
    q.textContent = question;
    var a = document.createElement('div');
    a.className = 'inv-turn__a is-streaming';
    turn.appendChild(q);
    turn.appendChild(a);
    thread.appendChild(turn);
    turn.scrollIntoView({ behavior: CheckCharts.reducedMotion() ? 'auto' : 'smooth', block: 'nearest' });

    var buffer = '';
    // The history sent is the conversation so far, not including this turn —
    // the server appends the question itself.
    var history = state.thread.slice(-6);

    fetch('/api/investments/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, history: history }),
      signal: state.abort.signal
    })
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (j) { throw new Error(j.error || 'Request failed'); });
        }
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var pending = '';

        function pump() {
          return reader.read().then(function (result) {
            if (result.done) { finish(); return; }
            pending += decoder.decode(result.value, { stream: true });
            var chunks = pending.split('\n\n');
            pending = chunks.pop();
            chunks.forEach(function (chunk) {
              var line = chunk.trim();
              if (line.indexOf('data: ') !== 0) return;
              var payload = line.slice(6);
              if (payload === '[DONE]') { finish(); return; }
              try {
                var obj = JSON.parse(payload);
                if (obj.error) { a.textContent = obj.error; finish(); return; }
                if (obj.delta) {
                  buffer += obj.delta;
                  a.innerHTML = renderLite(buffer);
                }
              } catch (e) { /* a partial frame; the next read completes it */ }
            });
            return pump();
          });
        }
        return pump();
      })
      .catch(function (err) {
        if (err.name === 'AbortError') return;
        a.textContent = err.message || 'Something went wrong. Try again.';
        finish();
      });

    function finish() {
      a.classList.remove('is-streaming');
      if (buffer) {
        a.innerHTML = renderLite(buffer);
        state.thread.push({ role: 'user', content: question });
        state.thread.push({ role: 'assistant', content: buffer });
        state.thread = state.thread.slice(-8);
      }
    }
  }

  function initCopilot() {
    var form = document.getElementById('invAskForm');
    var input = document.getElementById('invAskInput');
    var suggest = document.getElementById('invSuggest');
    var clear = document.getElementById('invThreadClear');

    if (form) {
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        ask_(input ? input.value : '');
      });
    }
    if (suggest) {
      suggest.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-ask]');
        if (btn) ask_(btn.getAttribute('data-ask'));
      });
    }
    if (clear) {
      clear.addEventListener('click', function () {
        if (state.abort) state.abort.abort();
        state.thread = [];
        var thread = document.getElementById('invThread');
        var foot = document.getElementById('invThreadFoot');
        if (thread) { thread.innerHTML = ''; thread.hidden = true; }
        if (foot) foot.hidden = true;
      });
    }

    var fab = document.getElementById('invFab');
    if (fab) {
      fab.addEventListener('click', function () {
        var copilot = document.getElementById('invCopilot');
        if (copilot) copilot.scrollIntoView({ behavior: CheckCharts.reducedMotion() ? 'auto' : 'smooth', block: 'center' });
        if (input) input.focus();
      });
    }

    loadBrief();
  }

  /* ══════════════════════════════════════════════════════════════════════
     Report — printing is what most people mean by "generate a report", and
     it needs every panel open first.
     ══════════════════════════════════════════════════════════════════════ */

  function initReport() {
    var btn = document.getElementById('invReport');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var wrap = document.getElementById('invPanels');
      if (wrap) {
        wrap.querySelectorAll('[data-panel]').forEach(function (el) {
          setOpen(el.getAttribute('data-panel'), true);
        });
      }
      // Give the freshly created charts a frame to lay out before the
      // print dialog snapshots them.
      setTimeout(function () { window.print(); }, 350);
    });
  }

  /* ══════════════════════════════════════════════════════════════════════
     Init
     ══════════════════════════════════════════════════════════════════════ */

  function init() {
    var node = document.getElementById('invData');
    if (!node) return;   // not the investments page
    if (typeof Chart === 'undefined' || !window.CheckCharts) {
      console.error('[investments] Chart.js or CheckCharts did not load.');
      return;
    }
    try {
      state.data = JSON.parse(node.textContent);
    } catch (e) {
      console.error('[investments] could not parse #invData:', e);
      return;
    }

    state.layout = loadLayout();
    state.charts = {};
    state.builders = {};
    state.thread = [];

    buildChartBuilders(state.data);

    document.querySelectorAll('[data-countup]').forEach(countUp);
    fillBars(document);
    drawRings(document);
    paintSwatches(document);
    paintMarks(document);

    initPeriods();
    initAlloc();
    initPanels();
    initChartTools(document);
    initFocus();
    initHoldings();
    initInsights();
    initCopilot();
    initReport();
  }

  function onKeydown(e) {
    if (e.key !== 'Escape') return;
    var focus = document.getElementById('invFocus');
    if (focus && !focus.hidden) { closeFocus(); return; }
    var sheet = document.getElementById('invSheet');
    if (sheet && !sheet.hidden) { closeSheet(); }
  }

  /* Chart.js bakes colors in at draw time, so a theme switch needs a
     rebuild — but only of the charts that actually exist. */
  function onThemeChange() {
    paintSwatches(document);
    paintMarks(document);
    Object.keys(state.charts).forEach(function (id) {
      destroyChart(id);
      ensureChart(id);
    });
    if (state.focusChart) {
      var chartId = document.getElementById('invFocus').getAttribute('data-chart');
      state.focusChart.destroy();
      state.focusChart = new Chart(document.getElementById('invFocusCanvas'), state.builders[chartId]());
    }
  }

  // Listeners on document survive SPA swaps, so they are attached once and
  // guarded rather than re-added on every navigation.
  if (!window._invListenersBound) {
    document.addEventListener('keydown', onKeydown);
    document.addEventListener('check:theme-changed', function () {
      if (document.getElementById('invData')) onThemeChange();
    });
    window._invListenersBound = true;
  }

  // An in-flight answer must not keep streaming into a page that has been
  // torn down by the SPA router.
  window.__spaBeforeLeave = function () {
    if (state.abort) state.abort.abort();
  };

  window._initInvestments = init;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
