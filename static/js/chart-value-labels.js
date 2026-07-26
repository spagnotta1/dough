/* Value labels for Chart.js — shared by the dashboard cards and the chat figures.
 *
 * Off by default everywhere: a number on every mark is noise, and the axis,
 * tooltip and data table already carry the rest. When a reader does turn them
 * on, the rules are:
 *
 *   - outside the data end when there is room, inside on contrast-picked ink
 *     when there is not;
 *   - nothing is ever clipped or drawn over another label — a value that
 *     cannot be placed is skipped, and the tooltip still has it;
 *   - text wears the ink token, never the series colour. The single exception
 *     is a label set inside a filled mark, which picks white or near-black off
 *     that fill's luminance.
 *
 * Chart.js routes everything under `options` through its context resolver
 * Proxy, which chokes on a raw function stored there ("Cannot convert object
 * to primitive value"). So the plugin options carry only primitives, and the
 * number formatter is named by string and looked up in `formats` below.
 */
(function (global) {
  'use strict';

  function relLum(hex) {
    var m = /^#?([\da-f]{6})$/i.exec(String(hex).trim());
    if (!m) return 1;                       /* unknown fill → assume light → dark ink */
    var n = parseInt(m[1], 16);
    var c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(function (v) {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }

  var formats = {
    number: function (v) {
      return v.toLocaleString('en-US', { maximumFractionDigits: 2 });
    },
    percent: function (v) {
      return (Math.round(v * 10) / 10) + '%';
    },
    /* Cents are all-or-nothing — "$1,284.5" is not a way money is written. */
    usd: function (v) {
      var cents = Math.abs(v % 1) > 1e-9 ? 2 : 0;
      return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US',
        { minimumFractionDigits: cents, maximumFractionDigits: cents });
    },
    /* Whole dollars — for charts whose axis is already rounded. */
    usd0: function (v) {
      return (v < 0 ? '-$' : '$') + Math.round(Math.abs(v)).toLocaleString('en-US');
    }
  };

  function isDonut(chart) {
    var t = chart.config.type;
    return t === 'doughnut' || t === 'pie';
  }

  function isHorizontal(chart) {
    return chart.options.indexAxis === 'y';
  }

  function isStacked(chart) {
    var s = chart.options.scales || {};
    return !!((s.x && s.x.stacked) || (s.y && s.y.stacked));
  }

  function hasNegative(chart) {
    return (chart.data.datasets || []).some(function (ds) {
      return (ds.data || []).some(function (v) {
        return typeof v === 'number' && v < 0;
      });
    });
  }

  /* Values need somewhere to sit that is not the plot: a strip above the marks,
     and beside the tips on a horizontal bar. Only claimed when they are on. */
  function pad(o) {
    var base = o.base || {};
    if (!o.enabled) return Object.assign({}, base);
    /* A donut labels outside its ring, so the room it needs is sideways — and
       the ring is height-bound anyway, so this costs nothing. */
    if (o.donut) return Object.assign({}, base, { top: 12, right: 62, bottom: 12, left: 62 });
    return Object.assign({}, base, {
      top: 20,
      right: o.horizontal ? 46 : Math.max(base.right || 0, 4),
      left: o.horizontal && o.negatives ? 46 : (base.left || 0),
      bottom: !o.horizontal && o.negatives ? 14 : (base.bottom || 0)
    });
  }

  function padFor(chart, enabled, base) {
    return pad({
      enabled: enabled,
      base: base,
      donut: isDonut(chart),
      horizontal: isHorizontal(chart),
      negatives: hasNegative(chart)
    });
  }

  var plugin = {
    id: 'valueLabels',

    afterDatasetsDraw: function (chart, args, o) {
      if (!o || !o.enabled) return;

      var fmt = formats[o.format] || formats.number;
      var size = o.size || 11;
      var ink = o.ink || chart.options.color || '#111827';
      var family = o.family ||
        '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';

      var donut = isDonut(chart);
      var horizontal = isHorizontal(chart);
      var stacked = isStacked(chart);

      var ctx = chart.ctx, taken = [], GAP = 6;
      var area = chart.chartArea ||
        { left: 0, right: chart.width, top: 0, bottom: chart.height };

      /* A mark inside the plot keeps its label inside the plot — drift left of
         the value axis and it lands on the tick labels. Marks that label into
         the layout padding on purpose (a bar tip, a donut arc) get the canvas. */
      var PLOT = { l: area.left, r: area.right };
      var CANVAS = { l: 1, r: chart.width - 1 };

      ctx.save();
      ctx.font = '600 ' + size + 'px ' + family;
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'left';

      /* `x` is the anchor; `align` says which side of it the text hangs on. */
      function draw(text, x, y, align, color, bound) {
        bound = bound || CANVAS;
        var w = ctx.measureText(text).width;
        var l = align === 'center' ? x - w / 2 : align === 'right' ? x - w : x;
        /* Fitting is judged on the ink itself; the padding is breathing room
           between neighbours, and charging it against the edge would drop
           labels that sit exactly on the axis. */
        if (l < bound.l || l + w > bound.r ||
            y - size / 2 < 1 || y + size / 2 > chart.height - 1) return;
        var box = { l: l - 3, r: l + w + 3, t: y - size / 2 - 2, b: y + size / 2 + 2 };
        for (var i = 0; i < taken.length; i++) {
          var p = taken[i];
          if (box.l < p.r && box.r > p.l && box.t < p.b && box.b > p.t) return;
        }
        taken.push(box);
        ctx.fillStyle = color;
        ctx.fillText(text, l, y);
      }

      chart.data.datasets.forEach(function (ds, di) {
        var meta = chart.getDatasetMeta(di);
        if (meta.hidden) return;
        /* A leading underscore marks a scaffolding series — a confidence band's
           edges, a spacer — that the reader is not meant to read values off.
           Tooltips already honour this; labels must too, or the scaffolding
           wins the collision race against the series it was drawn to support. */
        if (String(ds.label || '').charAt(0) === '_') return;

        meta.data.forEach(function (mark, i) {
          var v = ds.data[i];
          if (typeof v !== 'number' || !isFinite(v)) return;
          var text = fmt(v);
          if (!text) return;
          var w = ctx.measureText(text).width;

          /* Set inside a fill, the label picks white or ink off that fill's
             luminance. 0.18 is where the two contrast ratios cross, so this
             always takes the better of the pair rather than defaulting to
             white on mid-tones (the greens and ambers, where white fails). */
          var fill = Array.isArray(ds.backgroundColor) ? ds.backgroundColor[i] : ds.backgroundColor;
          var inInk = relLum(fill) > 0.18 ? '#141414' : '#ffffff';

          /* Horizontal text inside a ring only fits where the band happens to
             run sideways, so the label sits just outside the arc instead, on
             the radius that already points at it. */
          if (donut) {
            if (Math.abs(mark.circumference) < 0.14) return;   /* ~8° — a sliver */
            var a = mark.startAngle + mark.circumference / 2;
            var r = mark.outerRadius + 8;
            draw(text, mark.x + Math.cos(a) * r, mark.y + Math.sin(a) * r,
                 Math.cos(a) >= 0 ? 'left' : 'right', ink);
            return;
          }

          if (mark.base === undefined) {          /* line / scatter point */
            var ly = mark.y - size - 2;
            if (ly - size / 2 < 1) ly = mark.y + size + 2;
            /* At the ends, the label leans in rather than being dropped. */
            var la = mark.x + w / 2 > PLOT.r ? 'right'
                   : mark.x - w / 2 < PLOT.l ? 'left' : 'center';
            draw(text, mark.x, ly, la, ink, PLOT);
            return;
          }

          /* A stacked segment has no free end, so it labels inside or not at all. */
          if (stacked) {
            var span = horizontal ? Math.abs(mark.x - mark.base) : Math.abs(mark.y - mark.base);
            if (span < (horizontal ? w + 12 : size + 10)) return;
            var p = mark.getCenterPoint();
            draw(text, p.x, p.y, 'center', inInk, PLOT);
            return;
          }

          if (horizontal) {
            var dx = mark.x >= mark.base ? 1 : -1;
            var outX = mark.x + dx * GAP;
            if (dx > 0 ? outX + w < chart.width - 2 : outX - w > 2) {
              draw(text, outX, mark.y, dx > 0 ? 'left' : 'right', ink);
            } else if (Math.abs(mark.x - mark.base) > w + 14) {
              draw(text, mark.x - dx * GAP, mark.y, dx > 0 ? 'right' : 'left', inInk);
            }
            return;
          }

          var dy = mark.y <= mark.base ? -1 : 1;
          var outY = mark.y + dy * (GAP + size / 2);
          if (dy < 0 ? outY - size / 2 > 2 : outY + size / 2 < chart.height - 2) {
            draw(text, mark.x, outY, 'center', ink, PLOT);
          } else if (Math.abs(mark.y - mark.base) > size + 14) {
            draw(text, mark.x, mark.y - dy * (GAP + size / 2), 'center', inInk, PLOT);
          }
        });
      });

      ctx.restore();
    }
  };

  /* Registered globally so any chart in the app honours
     `options.plugins.valueLabels` without also having to list the plugin in
     its own `plugins` array. It draws nothing until `enabled` is set, so this
     costs charts that never opt in one early return per frame. Charts that do
     pass it inline (the chat figures) are unaffected — Chart.js dedupes the
     descriptor by object identity. */
  if (global.Chart && global.Chart.register) global.Chart.register(plugin);

  global.ChartValueLabels = {
    plugin: plugin,
    formats: formats,
    pad: pad,
    padFor: padFor,
    relLum: relLum
  };
})(window);
