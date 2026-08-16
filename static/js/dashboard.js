/* ══════════════════════════════════════════════════════════════════════════
   Dashboard behavior
   ──────────────────────────────────────────────────────────────────────────
   Everything is wired inside one init function that the SPA router calls
   again on every navigation, so the page must be safe to set up twice: every
   listener is attached to an element that was just rendered, and every chart
   destroys its predecessor before drawing.

   Charts are created lazily — a panel that has never been opened has never
   built a Chart.js instance. On a dashboard with six collapsed panels that
   is the difference between six canvases laid out on first paint and none.
   ══════════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  var LS_LAYOUT = 'check-dash-layout-v1';

  /* Panels that start open for a first-time reader. The category table is the
     one most people want; the rest are there when asked for. */
  var DEFAULT_OPEN = ['categories'];

  var state = {
    charts: {},        // id -> Chart instance
    builders: {},      // id -> () => config
    showValues: {},    // id -> bool
    valueShapes: {},   // id -> shape hint for the label plugin
    layout: null,
    focusChart: null,
    lastFocused: null,
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

  /* ── Count-up ────────────────────────────────────────────────────────
     Numbers rise to their value on first paint. Skipped entirely under
     reduced-motion, and skipped for very large jumps where the animation
     would just look like a slot machine. */

  function countUp(el) {
    var target = parseFloat(el.getAttribute('data-countup'));
    if (!isFinite(target)) return;
    var format = el.getAttribute('data-format') || 'usd';
    var render = format === 'pct'
      ? function (v) { return v.toFixed(1) + '%'; }
      : format === 'int'
        ? function (v) { return String(Math.round(v)); }
        : function (v) { return (v < 0 ? '-$' : '$') + Math.abs(Math.round(v)).toLocaleString('en-US'); };

    if (window.CheckCharts && CheckCharts.reducedMotion()) {
      el.textContent = render(target);
      return;
    }

    var duration = 850;
    var start = null;
    var from = 0;
    function step(ts) {
      if (start === null) start = ts;
      var t = Math.min(1, (ts - start) / duration);
      // easeOutExpo — fast off the mark, settles gently on the real figure.
      var eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
      el.textContent = render(from + (target - from) * eased);
      if (t < 1) requestAnimationFrame(step);
      else el.textContent = render(target);
    }
    requestAnimationFrame(step);
  }

  /* ── Progress bars fill on reveal ───────────────────────────────────── */

  function fillBars(root) {
    (root || document).querySelectorAll('[data-fill]').forEach(function (el) {
      var pct = parseFloat(el.getAttribute('data-fill'));
      if (!isFinite(pct)) return;
      // Two frames: the first commits width:0, the second animates to target.
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { el.style.width = Math.max(0, Math.min(100, pct)) + '%'; });
      });
    });
  }

  /* ── Health ring ────────────────────────────────────────────────────── */

  function drawHealthRing() {
    var wrap = document.querySelector('.dash-health');
    if (!wrap) return;
    var score = parseFloat(wrap.getAttribute('data-health')) || 0;
    var ring = wrap.querySelector('.dash-health__value');
    if (!ring) return;
    var circumference = 264;                        // 2πr for r = 42
    var offset = circumference * (1 - score / 100);
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { ring.style.strokeDashoffset = offset; });
    });
  }

  /* ── Category swatches ──────────────────────────────────────────────
     The table's color chips come from the same stable map the charts use,
     so a row and its series always agree. */

  function paintSwatches() {
    document.querySelectorAll('[data-swatch]').forEach(function (el) {
      var cat = el.getAttribute('data-swatch');
      // A chip only earns its place if it names a color the reader will
      // actually meet in a chart. Painting the whole tail the same neutral
      // makes the column look broken rather than informative.
      if (CheckCharts.hasOwnColor(cat)) {
        el.style.background = CheckCharts.forCategory(cat);
        el.style.visibility = '';
      } else {
        el.style.visibility = 'hidden';
      }
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

  /* Wrap a config with the opt-in value-label plugin options. */
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
    var t = CheckCharts.tokens();
    var P = CheckCharts.palette();
    var money = CheckCharts.money;
    var B = state.builders;

    /* ── Income & spending trend ──────────────────────────────────────
       Slots 1 and 2 (blue / orange), NOT green / red. As a series pair the
       money colors are indistinguishable to deuteranopes — see the note at
       the top of chart-theme.js. The dashed outgo line adds a second,
       non-color cue on top. */
    B.combinedTrendChart = function () {
      var inc = data.monthlyIncome || [];
      var out = data.monthlyOutgo || [];
      var labels = (inc.length >= out.length ? inc : out).map(function (d) {
        return CheckCharts.monthLabel(d.month);
      });
      return withValueLabels('combinedTrendChart', {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Income',
              data: inc.map(function (d) { return d.total; }),
              borderColor: P[0],
              backgroundColor: function (ctx) { return CheckCharts.areaFill(ctx, P[0], 0.18); },
              fill: true, pointBackgroundColor: P[0], pointBorderColor: t.surface
            },
            {
              label: 'Spending',
              data: out.map(function (d) { return d.total; }),
              borderColor: P[1],
              borderDash: [5, 4],
              backgroundColor: function (ctx) { return CheckCharts.areaFill(ctx, P[1], 0.14); },
              fill: true, pointBackgroundColor: P[1], pointBorderColor: t.surface
            }
          ]
        },
        options: CheckCharts.base({
          scales: CheckCharts.moneyScales(t),
          plugins: {
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: {
                label: function (ctx) { return ' ' + ctx.dataset.label + '  ' + money(ctx.raw); },
                /* The gap between the two lines is the whole point of the
                   chart, so state it rather than making the reader subtract. */
                footer: function (items) {
                  if (items.length < 2) return '';
                  var net = (items[0].raw || 0) - (items[1].raw || 0);
                  return (net >= 0 ? 'Net +' : 'Net −') + money(Math.abs(net)).replace('$', '$');
                }
              }
            })
          }
        })
      });
    };

    /* ── Category spending trend (stacked) ────────────────────────────
       Color comes from the stable category map, never the loop index, so
       filtering the dashboard does not repaint the surviving series. */
    B.categoryTrendChart = function () {
      var trend = data.categoryTrend || { months: [], series: {} };
      return withValueLabels('categoryTrendChart', {
        type: 'bar',
        data: {
          labels: (trend.months || []).map(CheckCharts.monthLabel),
          datasets: CheckCharts.foldOverflow(trend.months, trend.series).map(function (s) {
            return {
              label: s.label,
              data: s.data,
              backgroundColor: s.color,
              /* 2px of surface between stacked segments, so adjacent bands
                 read as separate even when their hues are close. */
              borderColor: t.surface,
              borderWidth: { top: 2, right: 0, bottom: 0, left: 0 },
              borderRadius: 3
            };
          })
        },
        options: CheckCharts.base({
          scales: CheckCharts.moneyScales(t, { stacked: true }),
          plugins: {
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: {
                label: function (ctx) { return ' ' + ctx.dataset.label + '  ' + money(ctx.raw); },
                footer: function (items) {
                  var total = items.reduce(function (s, i) { return s + (i.raw || 0); }, 0);
                  return 'Total  ' + money(total);
                }
              }
            })
          }
        })
      });
    };

    /* ── Running balance ─────────────────────────────────────────────── */
    B.balanceChart = function () {
      var hist = data.balanceHistory || [];
      var negative = hist.some(function (d) { return d.balance < 0; });
      return withValueLabels('balanceChart', {
        type: 'line',
        data: {
          labels: hist.map(function (d) { return d.date; }),
          datasets: [{
            label: 'Balance',
            data: hist.map(function (d) { return d.balance; }),
            borderColor: P[0],
            backgroundColor: function (ctx) { return CheckCharts.areaFill(ctx, P[0], 0.2); },
            fill: true,
            pointBackgroundColor: P[0], pointBorderColor: t.surface
          }]
        },
        options: CheckCharts.base({
          // One series — the panel title names it, so a legend box is noise.
          plugins: {
            legend: { display: false },
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: { label: function (ctx) { return ' ' + money(ctx.raw); } }
            })
          },
          scales: CheckCharts.moneyScales(t, {
            beginAtZero: false,
            xTicks: {
              maxTicksLimit: 8,
              // "Jan 2" reads at a glance; "2026-01-02" makes the reader
              // parse eight tick labels to find the shape of the year.
              callback: function (_v, i) { return CheckCharts.dayLabel(hist[i] && hist[i].date); }
            }
          })
        })
      }, { negatives: negative });
    };

    /* ── Drill-down ───────────────────────────────────────────────────
       A bar is a filtered slice of the ledger, so clicking one opens that
       slice in the transaction list. The URL carries the dashboard's own
       period and account alongside the category, because the transaction
       list's filters are sticky in the session: sending only `category`
       would land on that category crossed with whatever range the user last
       looked at over there, showing a different number than the bar did.

       The filter state comes from the server-rendered payload rather than
       from location.search — the SPA router leaves the address bar on the
       last GET form's query string, and a preset period never writes one. */
    function drillUrl(category, direction) {
      var d = data.drill;
      if (!d || !d.url || !category) return null;
      var q = [];
      function add(k, v) {
        if (v) q.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
      }
      add('start_date', d.startDate);
      add('end_date', d.endDate);
      add('account', d.account);
      add('category', category);
      add('type', direction);
      return d.url + (q.length ? '?' + q.join('&') : '');
    }

    /* Same route an ordinary in-app link takes, so a drill-down animates and
       lands in history like the rest of the product. The hard load is the
       fallback for when the router isn't present. */
    function navigate(url) {
      // These builders also back the enlarged chart in the focus modal, and
      // that modal locks body scroll. The lock lives on <body>, which an SPA
      // swap does not replace — leaving it set would strand the transaction
      // list unscrollable.
      var modal = document.getElementById('focusModal');
      if (modal && !modal.hidden) closeFocus();
      if (typeof window.spaNavigate === 'function') window.spaNavigate(url);
      else window.location.href = url;
    }

    /* ── Ranked category bars ─────────────────────────────────────────
       Horizontal bars, not a donut: past about six slices a donut becomes a
       legend-matching exercise, and length is read far more accurately than
       angle. Single hue, because rank is already encoded by position.

       Bars drill down on click. "Other" does not: it is the folded tail of
       several categories and the transaction filter takes one category at a
       time, so any URL built for it would show less than the bar it came
       from. It stays un-clickable rather than quietly under-reporting. */
    function rankedBars(id, field, color, direction) {
      return function () {
        var stats = data.categoryStats || {};
        var pairs = Object.keys(stats).map(function (cat) {
          return { label: cat, value: stats[cat][field] || 0 };
        });
        var top = CheckCharts.topN(pairs, 6);
        return withValueLabels(id, {
          type: 'bar',
          data: {
            labels: top.map(function (p) { return p.label; }),
            datasets: [{
              data: top.map(function (p) { return p.value; }),
              backgroundColor: top.map(function (p) {
                return p.other ? CheckCharts.hexAlpha(color, 0.45) : color;
              }),
              borderRadius: 4,
              maxBarThickness: 20,
              categoryPercentage: 0.78,
              barPercentage: 0.86
            }]
          },
          options: CheckCharts.base({
            indexAxis: 'y',
            interaction: { mode: 'nearest', intersect: true },
            onClick: function (_evt, els) {
              var p = els && els.length ? top[els[0].index] : null;
              if (!p || p.other) return;
              var url = drillUrl(p.label, direction);
              if (url) navigate(url);
            },
            /* The only affordance a canvas can offer — nothing here is a DOM
               element that could carry :hover. Bars that do not drill keep
               the default cursor, which is the honest signal. */
            onHover: function (evt, els) {
              var p = els && els.length ? top[els[0].index] : null;
              var can = evt.native && evt.native.target;
              if (can) can.style.cursor = (p && !p.other && drillUrl(p.label, direction)) ? 'pointer' : 'default';
            },
            plugins: {
              legend: { display: false },
              tooltip: CheckCharts.tooltipFor(t, {
                displayColors: false,
                callbacks: {
                  label: function (ctx) {
                    var total = ctx.dataset.data.reduce(function (s, v) { return s + v; }, 0);
                    var share = total ? Math.round(ctx.raw / total * 100) : 0;
                    return ' ' + money(ctx.raw) + '  ·  ' + share + '% of total';
                  },
                  footer: function (items) {
                    var p = items && items.length ? top[items[0].dataIndex] : null;
                    if (!p || p.other || !drillUrl(p.label, direction)) return '';
                    return 'Click to see these transactions';
                  }
                }
              })
            },
            scales: {
              x: {
                grid: CheckCharts.gridFor(t),
                border: { display: false },
                beginAtZero: true,
                ticks: CheckCharts.ticksFor(t, {
                  callback: function (v) { return CheckCharts.moneyShort(v); },
                  maxTicksLimit: 5
                })
              },
              y: {
                grid: { display: false },
                border: { display: false },
                ticks: CheckCharts.ticksFor(t)
              }
            }
          })
        }, { horizontal: true, base: { right: 10 } });
      };
    }
    B.incomeChart = rankedBars('incomeChart', 'inbound', P[2], 'inbound');
    B.outgoChart = rankedBars('outgoChart', 'outbound', P[1], 'outgo');

    /* ── Budget vs. actual ────────────────────────────────────────────
       A bullet chart: the limit is a wide neutral track and the actual is a
       narrower bar drawn inside it, so "am I over?" is answered by whether
       one bar outruns the other rather than by comparing two bar heights
       across a gap. `grouped: false` is what overlays them — Chart.js
       otherwise sets two datasets side by side.

       Actual is colored by status (under / near / over), which is a reserved
       status use and is backed here by the positional comparison too. */
    B.budgetChart = function () {
      var bmap = data.budgetMap || {};
      var stats = data.categoryStats || {};
      var months = data.periodMonths || 1;
      var cats = Object.keys(bmap);
      var actuals = cats.map(function (c) {
        var s = stats[c] || {};
        return Math.max(0, (s.outbound || 0) - (s.inbound || 0)) / months;
      });
      var limits = cats.map(function (c) { return bmap[c]; });
      return withValueLabels('budgetChart', {
        type: 'bar',
        data: {
          labels: cats,
          datasets: [
            {
              label: 'Budget',
              data: limits,
              backgroundColor: CheckCharts.hexAlpha(t.ink, 0.10),
              borderColor: CheckCharts.hexAlpha(t.ink, 0.20),
              borderWidth: 1,
              borderRadius: 4,
              grouped: false,
              barPercentage: 0.86,
              categoryPercentage: 0.72,
              order: 2
            },
            {
              label: 'Actual',
              data: actuals,
              backgroundColor: actuals.map(function (a, i) {
                return a > limits[i] ? t.danger : a > limits[i] * 0.8 ? t.warn : t.ok;
              }),
              borderRadius: 4,
              grouped: false,
              barPercentage: 0.42,
              categoryPercentage: 0.72,
              order: 1
            }
          ]
        },
        options: CheckCharts.base({
          scales: CheckCharts.moneyScales(t),
          plugins: {
            tooltip: CheckCharts.tooltipFor(t, {
              callbacks: {
                label: function (ctx) { return ' ' + ctx.dataset.label + '  ' + money(ctx.raw); },
                footer: function (items) {
                  var byLabel = {};
                  items.forEach(function (i) { byLabel[i.dataset.label] = i.raw || 0; });
                  if (byLabel.Actual === undefined || byLabel.Budget === undefined) return '';
                  var diff = byLabel.Actual - byLabel.Budget;
                  return diff > 0 ? money(diff) + ' over' : money(-diff) + ' left';
                }
              }
            })
          }
        })
      });
    };

    /* ── Cash-flow forecast ───────────────────────────────────────────
       The band is drawn as two stacked line datasets: a transparent one at
       the low edge and a filled one at the high edge that fills down to it.
       Chart.js has no native band, and this is the version that keeps the
       tooltip readable — the two band datasets stay out of the legend and
       out of the tooltip entirely. */
    B.forecastChart = function () {
      var f = data.forecast || { points: [] };
      var pts = f.points || [];
      var labels = pts.map(function (p) { return p.date; });
      var band = t.mode === 'dark' ? 0.14 : 0.11;
      return withValueLabels('forecastChart', {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: '_low',
              data: pts.map(function (p) { return p.low; }),
              borderWidth: 0, pointRadius: 0, fill: false, tension: 0.3
            },
            {
              label: '_high',
              data: pts.map(function (p) { return p.high; }),
              borderWidth: 0, pointRadius: 0, tension: 0.3,
              fill: '-1',
              backgroundColor: CheckCharts.hexAlpha(P[0], band)
            },
            {
              label: 'Projected cash',
              data: pts.map(function (p) { return p.balance; }),
              borderColor: P[0],
              borderDash: [6, 4],
              backgroundColor: 'transparent',
              fill: false,
              pointBackgroundColor: P[0],
              pointBorderColor: t.surface,
              tension: 0.3
            }
          ]
        },
        options: CheckCharts.base({
          plugins: {
            legend: { display: false },
            tooltip: CheckCharts.tooltipFor(t, {
              filter: function (item) { return item.dataset.label.charAt(0) !== '_'; },
              callbacks: {
                title: function (items) { return items.length ? items[0].label : ''; },
                label: function (ctx) { return ' ' + money(ctx.raw); },
                footer: function (items) {
                  if (!items.length) return '';
                  var p = pts[items[0].dataIndex];
                  if (!p || p.low === p.high) return '';
                  return 'Likely ' + money(p.low) + ' – ' + money(p.high);
                }
              }
            })
          },
          scales: CheckCharts.moneyScales(t, {
            beginAtZero: false,
            xTicks: {
              maxTicksLimit: 6,
              callback: function (v, i) {
                var d = labels[i];
                if (!d) return '';
                var parts = d.split('-');
                return parts[1] + '/' + parts[2];
              }
            }
          })
        })
      }, { negatives: pts.some(function (p) { return p.balance < 0; }) });
    };
  }

  /* ── Value-label toggles ────────────────────────────────────────────── */

  function toggleValues(id) {
    state.showValues[id] = !state.showValues[id];
    var chart = state.charts[id];
    if (chart) { destroyChart(id); ensureChart(id); }
    document.querySelectorAll('[data-values="' + id + '"]').forEach(function (btn) {
      btn.setAttribute('aria-pressed', state.showValues[id] ? 'true' : 'false');
    });
  }

  /* Values and expand are per-chart, not per-panel, and charts live both
     inside the panel grid and on standalone cards above it (the forecast).
     Binding at the dashboard root is what lets both kinds work. */
  function initChartTools() {
    var root = document.getElementById('dash');
    if (!root) return;
    root.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-values],[data-expand]');
      if (!btn) return;
      var key;
      if ((key = btn.getAttribute('data-values'))) {
        toggleValues(key);
      } else if ((key = btn.getAttribute('data-expand'))) {
        openFocus(key, btn.getAttribute('data-title') || '');
      }
    });
  }

  /* ══════════════════════════════════════════════════════════════════════
     Panels — collapse, pin, hide, reorder
     ══════════════════════════════════════════════════════════════════════ */

  function panelEl(key) { return document.querySelector('[data-panel="' + key + '"]'); }

  function chartIdIn(key) {
    var el = panelEl(key);
    if (!el) return null;
    var canvas = el.querySelector('canvas');
    return canvas ? canvas.id : null;
  }

  function setOpen(key, open) {
    var panel = panelEl(key);
    if (!panel) return;
    var body = document.getElementById('panel-body-' + key);
    var toggle = panel.querySelector('[data-toggle="' + key + '"]');
    if (body) body.hidden = !open;
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    panel.classList.toggle('is-open', open);
    toggleIn(state.layout.open, key, open);

    if (open) {
      // Charts are built the first time their panel opens, never before.
      panel.querySelectorAll('canvas').forEach(function (c) { ensureChart(c.id); });
      fillBars(body);
    }
  }

  function setPinned(key, pinned) {
    var panel = panelEl(key);
    if (!panel) return;
    var btn = panel.querySelector('[data-pin="' + key + '"]');
    if (btn) btn.setAttribute('aria-pressed', pinned ? 'true' : 'false');
    toggleIn(state.layout.pinned, key, pinned);
    // Pinning means "keep this open" — so it opens now and stays open.
    if (pinned) setOpen(key, true);
  }

  function setHidden(key, hidden) {
    var panel = panelEl(key);
    if (!panel) return;
    panel.hidden = hidden;
    toggleIn(state.layout.hidden, key, hidden);
    renderHiddenTray();
  }

  function renderHiddenTray() {
    var tray = document.getElementById('hiddenTray');
    var items = document.getElementById('hiddenTrayItems');
    if (!tray || !items) return;
    items.innerHTML = '';
    var hidden = state.layout.hidden.filter(function (k) { return !!panelEl(k); });
    tray.hidden = hidden.length === 0;
    hidden.forEach(function (key) {
      var panel = panelEl(key);
      var title = panel.querySelector('.dash-panel__title');
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ds-chip';
      btn.textContent = 'Show ' + (title ? title.textContent : key);
      btn.addEventListener('click', function () { setHidden(key, false); saveLayout(); });
      items.appendChild(btn);
    });
  }

  function applyOrder() {
    var container = document.getElementById('dashPanels');
    if (!container || !state.layout.order.length) return;
    state.layout.order.forEach(function (key) {
      var panel = panelEl(key);
      if (panel) container.appendChild(panel);
    });
  }

  function captureOrder() {
    var container = document.getElementById('dashPanels');
    if (!container) return;
    state.layout.order = Array.prototype.map.call(
      container.querySelectorAll('[data-panel]'),
      function (el) { return el.getAttribute('data-panel'); });
  }

  function initPanels() {
    var container = document.getElementById('dashPanels');
    if (!container) return;

    applyOrder();

    container.querySelectorAll('[data-panel]').forEach(function (panel) {
      var key = panel.getAttribute('data-panel');
      var pinned = state.layout.pinned.indexOf(key) !== -1;
      var hidden = state.layout.hidden.indexOf(key) !== -1;
      var open = pinned || state.layout.open.indexOf(key) !== -1;

      setOpen(key, open);
      if (pinned) setPinned(key, true);
      if (hidden) setHidden(key, true);

      var head = panel.querySelector('[data-drag-handle]');
      if (head) head.setAttribute('draggable', 'true');
    });

    renderHiddenTray();

    /* One delegated listener for every panel control. The chart tools are not
       here: they also appear on cards outside the panel grid, so they get
       their own dashboard-wide listener (initChartTools). */
    container.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-toggle],[data-pin],[data-hide]');
      if (!btn) return;

      var key;
      if ((key = btn.getAttribute('data-toggle'))) {
        var isOpen = btn.getAttribute('aria-expanded') === 'true';
        // A pinned panel stays open; unpin first if you want it shut.
        if (isOpen && state.layout.pinned.indexOf(key) !== -1) setPinned(key, false);
        setOpen(key, !isOpen);
        saveLayout();
      } else if ((key = btn.getAttribute('data-pin'))) {
        setPinned(key, btn.getAttribute('aria-pressed') !== 'true');
        saveLayout();
      } else if ((key = btn.getAttribute('data-hide'))) {
        setHidden(key, true);
        saveLayout();
      }
    });

    /* ── Drag to reorder ──────────────────────────────────────────────
       Native HTML5 drag on the panel header. Keyboard users get the same
       capability through the reorder shortcuts below, so this is an
       enhancement rather than the only route. */
    var dragging = null;

    container.addEventListener('dragstart', function (e) {
      var head = e.target.closest('[data-drag-handle]');
      if (!head) return;
      dragging = head.closest('[data-panel]');
      if (!dragging) return;
      dragging.classList.add('is-dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', dragging.getAttribute('data-panel')); } catch (err) {}
    });

    container.addEventListener('dragover', function (e) {
      if (!dragging) return;
      e.preventDefault();
      var over = e.target.closest('[data-panel]');
      if (!over || over === dragging) return;
      var rect = over.getBoundingClientRect();
      var after = (e.clientY - rect.top) > rect.height / 2;
      container.insertBefore(dragging, after ? over.nextSibling : over);
    });

    container.addEventListener('dragend', function () {
      if (!dragging) return;
      dragging.classList.remove('is-dragging');
      dragging = null;
      captureOrder();
      saveLayout();
    });

    /* Keyboard reorder: Alt+↑ / Alt+↓ on a focused panel header. */
    container.addEventListener('keydown', function (e) {
      if (!e.altKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
      var panel = e.target.closest('[data-panel]');
      if (!panel) return;
      e.preventDefault();
      if (e.key === 'ArrowUp' && panel.previousElementSibling) {
        container.insertBefore(panel, panel.previousElementSibling);
      } else if (e.key === 'ArrowDown' && panel.nextElementSibling) {
        container.insertBefore(panel.nextElementSibling, panel);
      }
      captureOrder();
      saveLayout();
      e.target.focus();
    });

    var expandAll = document.getElementById('analyticsExpandAll');
    if (expandAll) {
      expandAll.addEventListener('click', function () {
        var allOpen = container.querySelectorAll('[data-panel]:not([hidden])').length ===
                      container.querySelectorAll('[data-panel]:not([hidden]).is-open').length;
        container.querySelectorAll('[data-panel]:not([hidden])').forEach(function (p) {
          setOpen(p.getAttribute('data-panel'), !allOpen);
        });
        expandAll.textContent = allOpen ? 'Expand all' : 'Collapse all';
        saveLayout();
      });
    }

    var reset = document.getElementById('analyticsReset');
    if (reset) {
      reset.addEventListener('click', function () {
        try { localStorage.removeItem(LS_LAYOUT); } catch (e) {}
        state.layout = loadLayout();
        window.location.reload();
      });
    }
  }

  /* ══════════════════════════════════════════════════════════════════════
     Focus modal
     ══════════════════════════════════════════════════════════════════════ */

  function openFocus(chartId, title) {
    var modal = document.getElementById('focusModal');
    var canvas = document.getElementById('focusCanvas');
    var builder = state.builders[chartId];
    if (!modal || !canvas || !builder) return;

    state.lastFocused = document.activeElement;
    document.getElementById('focusTitle').textContent = title;
    modal.hidden = false;
    document.body.style.overflow = 'hidden';

    if (state.focusChart) { state.focusChart.destroy(); state.focusChart = null; }
    state.focusChart = new Chart(canvas, builder());
    modal.setAttribute('data-chart', chartId);

    var valuesBtn = document.getElementById('focusValues');
    valuesBtn.setAttribute('aria-pressed', state.showValues[chartId] ? 'true' : 'false');
    valuesBtn.textContent = state.showValues[chartId] ? 'Hide values' : 'Show values';

    // Focus goes into the dialog so the keyboard is not left behind it.
    var first = modal.querySelector('.ds-btn');
    if (first) first.focus();
  }

  function closeFocus() {
    var modal = document.getElementById('focusModal');
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    document.body.style.overflow = '';
    if (state.focusChart) { state.focusChart.destroy(); state.focusChart = null; }
    if (state.lastFocused && state.lastFocused.focus) state.lastFocused.focus();
  }

  function initFocus() {
    var modal = document.getElementById('focusModal');
    if (!modal) return;

    modal.addEventListener('click', function (e) {
      if (e.target.closest('[data-close-modal]')) closeFocus();
    });

    var valuesBtn = document.getElementById('focusValues');
    if (valuesBtn) {
      valuesBtn.addEventListener('click', function () {
        var chartId = modal.getAttribute('data-chart');
        if (!chartId) return;
        toggleValues(chartId);
        if (state.focusChart) state.focusChart.destroy();
        state.focusChart = new Chart(document.getElementById('focusCanvas'), state.builders[chartId]());
        valuesBtn.setAttribute('aria-pressed', state.showValues[chartId] ? 'true' : 'false');
        valuesBtn.textContent = state.showValues[chartId] ? 'Hide values' : 'Show values';
      });
    }

    var exportBtn = document.getElementById('focusExport');
    if (exportBtn) {
      exportBtn.addEventListener('click', function () {
        if (!state.focusChart) return;
        var a = document.createElement('a');
        var title = document.getElementById('focusTitle').textContent || 'chart';
        a.download = title.toLowerCase().replace(/[^a-z0-9]+/g, '-') + '.png';
        // Chart.js canvases are transparent; paint the panel color underneath
        // so the exported PNG is not black-on-black in a document.
        var src = state.focusChart.canvas;
        var out = document.createElement('canvas');
        out.width = src.width; out.height = src.height;
        var ctx = out.getContext('2d');
        ctx.fillStyle = CheckCharts.tokens().surface;
        ctx.fillRect(0, 0, out.width, out.height);
        ctx.drawImage(src, 0, 0);
        a.href = out.toDataURL('image/png');
        a.click();
      });
    }

    /* Trap focus inside the dialog while it is open. */
    modal.addEventListener('keydown', function (e) {
      if (e.key !== 'Tab') return;
      var focusables = modal.querySelectorAll('button, [href], canvas[tabindex]');
      if (!focusables.length) return;
      var first = focusables[0], last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
  }

  /* ══════════════════════════════════════════════════════════════════════
     Copilot
     ══════════════════════════════════════════════════════════════════════ */

  /* A deliberately small Markdown subset — bold, inline code, bullets,
     paragraphs. Everything is escaped first, so model output can never
     inject markup. */
  function renderLite(md) {
    var esc = String(md)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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

  /* The window the dashboard is currently filtered to. Handed to the copilot
     so its briefing and answers describe the same period the page shows. */
  function currentPeriod() {
    var start = document.getElementById('start_date');
    var end = document.getElementById('end_date');
    return { start: start ? start.value : '', end: end ? end.value : '' };
  }

  function loadBrief() {
    var narrative = document.getElementById('copilotNarrative');
    var loading = document.getElementById('copilotLoading');
    if (!narrative) return;

    var period = currentPeriod();
    fetch('/api/copilot/brief?start=' + encodeURIComponent(period.start) +
          '&end=' + encodeURIComponent(period.end))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (loading) loading.remove();
        if (!data || !data.available) {
          // No key configured, or the model could not be reached. The
          // deterministic brief above is already on screen and stands alone.
          narrative.remove();
          return;
        }
        if (data.narrative) narrative.innerHTML = renderLite(data.narrative);
        else narrative.remove();

        var opps = data.opportunities || [];
        if (opps.length) {
          var wrap = document.getElementById('copilotOpps');
          wrap.hidden = false;
          wrap.innerHTML =
            '<p class="ds-eyebrow">' + opps.length + ' opportunit' +
            (opps.length === 1 ? 'y' : 'ies') + ' found</p>';
          opps.forEach(function (o, i) {
            var el = document.createElement('div');
            el.className = 'dash-opp';
            el.style.setProperty('--ds-delay', (i * 70) + 'ms');
            el.innerHTML =
              '<span class="dash-opp__num">' + (i + 1) + '</span>' +
              '<div style="flex:1;min-width:0">' +
                '<p class="dash-opp__title"></p>' +
                '<p class="dash-opp__detail"></p>' +
              '</div>' +
              (o.impact ? '<span class="ds-badge ds-badge--ok dash-opp__impact"></span>' : '');
            el.querySelector('.dash-opp__title').textContent = o.title || '';
            el.querySelector('.dash-opp__detail').textContent = o.detail || '';
            if (o.impact) el.querySelector('.dash-opp__impact').textContent = o.impact;
            wrap.appendChild(el);
          });
        }

        var questions = data.questions || [];
        if (questions.length) {
          var suggest = document.getElementById('copilotSuggest');
          suggest.innerHTML = '';
          questions.slice(0, 4).forEach(function (q) {
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

  function ask(question) {
    question = (question || '').trim();
    if (!question) return;

    var wrap = document.getElementById('copilotAnswer');
    var qEl = document.getElementById('copilotAnswerQ');
    var aEl = document.getElementById('copilotAnswerA');
    var input = document.getElementById('copilotInput');
    if (!wrap || !aEl) return;

    if (state.abort) state.abort.abort();
    state.abort = new AbortController();

    wrap.hidden = false;
    qEl.textContent = question;
    aEl.textContent = '';
    aEl.classList.add('is-streaming');
    if (input) input.value = '';

    var buffer = '';
    var period = currentPeriod();

    fetch('/api/copilot/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, start: period.start, end: period.end }),
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
                if (obj.error) { aEl.textContent = obj.error; finish(); return; }
                if (obj.delta) {
                  buffer += obj.delta;
                  aEl.innerHTML = renderLite(buffer);
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
        aEl.textContent = err.message ||
          "I couldn't get to that just now. Give it another go in a moment.";
        finish();
      });

    function finish() {
      aEl.classList.remove('is-streaming');
      if (buffer) aEl.innerHTML = renderLite(buffer);
    }
  }

  function initCopilot() {
    var form = document.getElementById('copilotForm');
    var input = document.getElementById('copilotInput');
    var suggest = document.getElementById('copilotSuggest');
    var closeBtn = document.getElementById('copilotAnswerClose');

    if (form) {
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        ask(input ? input.value : '');
      });
    }
    if (suggest) {
      suggest.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-ask]');
        if (btn) ask(btn.getAttribute('data-ask'));
      });
    }
    if (closeBtn) {
      closeBtn.addEventListener('click', function () {
        if (state.abort) state.abort.abort();
        document.getElementById('copilotAnswer').hidden = true;
      });
    }

    loadBrief();
  }

  /* ══════════════════════════════════════════════════════════════════════
     Filter bar

     The three controls are <details> elements, so open/close, the accessible
     name, the expanded state and Enter/Space all come from the element. What
     is added here is only what a disclosure does not have on its own: one
     open at a time, Escape, click-away, and applying a preset or an account
     without a trip through Apply.

     Nothing here holds filter state. The form's own fields are the state —
     the date inputs, the account radio, the category checkboxes — and the
     page that comes back renders every control from the query it answered.
     ══════════════════════════════════════════════════════════════════════ */

  function openMenus() {
    return Array.prototype.slice.call(
      document.querySelectorAll('#dashFilters .dash-menu[open]'));
  }

  function closeMenus(except) {
    openMenus().forEach(function (menu) {
      if (menu !== except) menu.open = false;
    });
  }

  /* Applying a filter is a navigation, so it takes the same router every link
     on the page takes: the panels transition instead of the window blinking
     white, and the address bar ends up carrying the filter, which is what
     makes a filtered dashboard something you can send to somebody.

     FormData is the whole payload deliberately — it is the same set of
     successful controls a native submit would send, including the date inputs
     inside the collapsed custom-range disclosure, which still submit because
     `hidden` hides a field rather than disabling it. */
  function applyFilters(form) {
    var query = new URLSearchParams(new FormData(form)).toString();
    var url = form.getAttribute('action') || window.location.pathname;
    var target = query ? url + '?' + query : url;
    if (typeof window.spaNavigate === 'function') window.spaNavigate(target);
    else window.location.href = target;
  }

  /* Which preset the window on screen corresponds to, applied to every place
     that names it. The pill and the chip say the same thing because they are
     written by the same pass — they used to be able to disagree, and a filter
     bar whose two halves disagree is worse than one that says nothing.

     Only ever an override: with no preset matching, the server's rendering of
     the window stands, because "Aug 1 – Aug 14, 2026" is the true answer and
     this function has no better one. */
  function syncDateLabels() {
    var start = document.getElementById('start_date');
    var end = document.getElementById('end_date');
    if (!start || !end) return;

    var matched = null;
    document.querySelectorAll('#dashFilters [data-preset]').forEach(function (btn) {
      var range = presetRange(btn.getAttribute('data-preset'));
      var hit = !!range && range.start === start.value && range.end === end.value;
      btn.setAttribute('aria-pressed', hit ? 'true' : 'false');
      if (hit && matched === null) matched = btn.textContent.trim();
    });

    if (matched) {
      document.querySelectorAll('#dashFilters [data-date-label]').forEach(function (el) {
        el.textContent = matched;
      });
    }
    // A window nobody has a name for is one somebody typed, so the fields
    // that produced it open with the menu rather than behind another click.
    var custom = document.getElementById('customRange');
    if (custom) custom.open = !matched;
  }

  function initFilters() {
    var form = document.getElementById('dashFilterForm');
    if (!form) return;

    syncDateLabels();

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      applyFilters(form);
    });

    form.addEventListener('click', function (e) {
      var preset = e.target.closest('[data-preset]');
      if (!preset) return;
      var range = presetRange(preset.getAttribute('data-preset'));
      if (!range) return;
      document.getElementById('start_date').value = range.start;
      document.getElementById('end_date').value = range.end;
      // A preset is a complete answer, so it applies on the spot. Composing
      // several criteria before one refresh is what "+ Filters" is for.
      applyFilters(form);
    });

    // Same for the account. A checkbox under "+ Filters" deliberately does
    // not: that panel exists so three changes cost one request, not three.
    form.addEventListener('change', function (e) {
      if (e.target.name === 'account') applyFilters(form);
    });

    var clear = document.getElementById('moreClear');
    if (clear) {
      clear.addEventListener('click', function () {
        form.querySelectorAll('#moreMenu input[type="checkbox"]').forEach(
          function (box) { box.checked = false; });
      });
    }

    document.querySelectorAll('#dashFilters .dash-menu').forEach(function (menu) {
      menu.addEventListener('toggle', function () {
        if (menu.open) closeMenus(menu);
      });
    });
  }

  function initHeader() {
    initFilters();

    var healthToggle = document.getElementById('healthToggle');
    var healthDetail = document.getElementById('healthDetail');
    if (healthToggle && healthDetail) {
      healthToggle.addEventListener('click', function () {
        var open = healthToggle.getAttribute('aria-expanded') === 'true';
        healthToggle.setAttribute('aria-expanded', open ? 'false' : 'true');
        healthDetail.hidden = open;
        if (!open) fillBars(healthDetail);
      });
    }
    document.querySelectorAll('[data-close]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var target = document.getElementById(btn.getAttribute('data-close'));
        if (target) target.hidden = true;
        var opener = document.getElementById('healthToggle');
        if (opener) { opener.setAttribute('aria-expanded', 'false'); opener.focus(); }
      });
    });
  }

  /* A preset's date range. Pure — it computes, it does not navigate, which is
     what lets the same function both fill the inputs and decide which chip
     should read as active. */
  function presetRange(preset) {
    var now = new Date(), y = now.getFullYear(), m = now.getMonth();
    var fmt = function (d) {
      return d.getFullYear() + '-' +
             String(d.getMonth() + 1).padStart(2, '0') + '-' +
             String(d.getDate()).padStart(2, '0');
    };
    var start, end = new Date(now);
    if (preset === 'this_month') start = new Date(y, m, 1);
    else if (preset === 'last_month') { start = new Date(y, m - 1, 1); end = new Date(y, m, 0); }
    else if (preset === 'last_3mo') start = new Date(y, m - 3, now.getDate());
    else if (preset === 'last_6mo') start = new Date(y, m - 6, now.getDate());
    else if (preset === 'ytd') start = new Date(y, 0, 1);
    else if (preset === 'last_year') { start = new Date(y - 1, 0, 1); end = new Date(y - 1, 11, 31); }
    else return null;
    return { start: fmt(start), end: fmt(end) };
  }

  /* ══════════════════════════════════════════════════════════════════════
     Init
     ══════════════════════════════════════════════════════════════════════ */

  function init() {
    // Failures here are loud on purpose. An early `return` used to leave the
    // page looking merely inert — no charts, no health ring, dead buttons —
    // with nothing in the console to say why, which is a miserable thing to
    // debug from a screenshot.
    var node = document.getElementById('dashData');
    if (!node) {
      console.error('[dashboard] #dashData is missing; nothing can be wired up.');
      return;
    }
    if (typeof Chart === 'undefined' || !window.CheckCharts) {
      console.error('[dashboard] Chart.js or CheckCharts did not load.');
      return;
    }

    var data;
    try {
      data = JSON.parse(node.textContent);
    } catch (e) {
      console.error('[dashboard] could not parse #dashData:', e);
      return;
    }

    state.layout = loadLayout();
    state.charts = {};
    state.builders = {};

    CheckCharts.registerCategories(data.allCategories || []);
    paintSwatches();
    buildChartBuilders(data);

    document.querySelectorAll('[data-countup]').forEach(countUp);
    fillBars(document);
    drawHealthRing();

    initHeader();
    initPanels();
    initChartTools();
    initFocus();
    initCopilot();

    // The forecast chart is above the fold, so it is the one chart built
    // eagerly rather than on reveal.
    ensureChart('forecastChart');
  }

  /* Escape closes whatever is open, outermost first. */
  function onKeydown(e) {
    if (e.key !== 'Escape') return;
    var modal = document.getElementById('focusModal');
    if (modal && !modal.hidden) { closeFocus(); return; }
    var detail = document.getElementById('healthDetail');
    if (detail && !detail.hidden) {
      detail.hidden = true;
      var t = document.getElementById('healthToggle');
      if (t) { t.setAttribute('aria-expanded', 'false'); t.focus(); }
      return;
    }
    // Innermost first: the custom-range disclosure lives inside the date
    // menu, and Escape closing both at once loses the reader's place.
    var custom = document.getElementById('customRange');
    if (custom && custom.open && custom.contains(document.activeElement)) {
      custom.open = false;
      custom.querySelector('summary').focus();
      return;
    }
    var menu = openMenus()[0];
    if (menu) {
      menu.open = false;
      var summary = menu.querySelector('summary');
      if (summary) summary.focus();
    }
  }

  /* Chart.js bakes colors in at draw time, so a theme switch needs a
     rebuild — but only of the charts that actually exist. */
  function onThemeChange() {
    paintSwatches();
    Object.keys(state.charts).forEach(function (id) {
      destroyChart(id);
      ensureChart(id);
    });
    if (state.focusChart) {
      var chartId = document.getElementById('focusModal').getAttribute('data-chart');
      state.focusChart.destroy();
      state.focusChart = new Chart(document.getElementById('focusCanvas'), state.builders[chartId]());
    }
  }

  // Listeners live on document and survive SPA swaps, so they are attached
  // once and guarded rather than re-added on every navigation.
  if (!window._dashListenersBound) {
    document.addEventListener('keydown', onKeydown);
    // Click-away. A <details> stays open until something closes it, and a
    // filter popover left hanging over the numbers is the one thing that
    // would make these controls feel heavier than the panel they replaced.
    document.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('#dashFilters .dash-menu')) return;
      closeMenus();
    });
    document.addEventListener('check:theme-changed', function () {
      if (document.getElementById('dashData')) onThemeChange();
    });
    window._dashListenersBound = true;
  }

  window._initDashboard = init;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
