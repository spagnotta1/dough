/* ═══════════════════════════════════════════════════════════════════════════
   Filter bar — the behaviour behind `.ds-filters`
   ─────────────────────────────────────────────────────────────────────────
   Three pages carry a filter bar: the dashboard, the ledger and the recurring
   view. This is the one copy of what makes it work.

   ── How little there is to do ──
   Each control is a `<details>`, so open and close, the accessible name, the
   expanded state and Enter/Space are the element's. The enclosing
   `<form method="GET">` is the filter state — its own fields are the only
   place that state lives, and the page that comes back renders every control
   from the query it just answered. So nothing here holds state, and nothing
   here reads the DOM to find out what is filtered.

   What is left is the four things a disclosure does not do on its own:
   one open at a time, Escape, click-away, and applying a preset without a
   trip through Apply.

   ── Why the listeners are on the document ──
   SPA navigation replaces `<main>` wholesale, so any listener bound to an
   element inside it dies with the page that added it — and a page-local
   script would have to re-bind on every navigation. Delegated listeners
   survive the swap because the document does. The only per-element work is
   `sync()`, which reads the date fields to decide which preset is the one on
   screen; that runs on load and again on `check:navigated`.

   ── The markup contract ──
     [data-filter-bar]          the wrapper; everything below is scoped to one
     form                       the filter state, and the thing that submits
     [data-preset="this_month"] a button that writes a range and applies it
     [data-filter-start/-end]   the two date inputs a preset writes into
     [data-date-label]          anything that should read as the preset's name
     [data-filter-custom]       the custom-range disclosure, opened when no
                                preset matches the window on screen
     [data-apply-now]           a control that applies on change rather than
                                waiting for Apply
     [data-filter-clear]        clears the controls in its own popover
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var BAR = '[data-filter-bar]';

  function bars() {
    return Array.prototype.slice.call(document.querySelectorAll(BAR));
  }

  function menus(bar, openOnly) {
    return Array.prototype.slice.call(
      bar.querySelectorAll(openOnly ? '.ds-filter-menu[open]' : '.ds-filter-menu'));
  }

  function closeMenus(bar, except) {
    menus(bar, true).forEach(function (menu) {
      if (menu !== except) menu.open = false;
    });
  }

  /* Applying a filter is a navigation, so it takes the same router every link
     on the page takes: the panels transition instead of the window blinking
     white, and the address bar ends up carrying the filter, which is what
     makes a filtered view something you can send to somebody.

     FormData is the whole payload deliberately — it is the same set of
     successful controls a native submit would send, including the date inputs
     inside a collapsed custom-range disclosure, which still submit because
     `hidden` hides a field rather than disabling it.

     Empty values are dropped, with one exception. A blank field usually means
     "no filter" and does not belong in the URL — but the ledger's filters are
     *sticky in the session*, and there `?account=` is how a page says the
     reader cleared it, as against saying nothing and inheriting last time's.
     A bar that filters from the session marks itself, and then sends the
     blanks. See sticky_filter in dough/services/transactions.py. */
  function applyFilters(form) {
    var bar = form.closest(BAR);
    var sticky = bar && bar.hasAttribute('data-filter-sticky');
    var params = new URLSearchParams();
    new FormData(form).forEach(function (value, key) {
      if (sticky || String(value).trim() !== '') params.append(key, value);
    });
    var query = params.toString();
    var url = form.getAttribute('action') || window.location.pathname;
    var target = query ? url + '?' + query : url;
    if (typeof window.spaNavigate === 'function') window.spaNavigate(target);
    else window.location.href = target;
  }

  /* A preset's date range. Pure — it computes, it does not navigate, which is
     what lets the same function both fill the inputs and decide which option
     should read as active.

     These six definitions are the product's, not this file's: they match what
     the server does with the same names, and changing one here silently moves
     what "Last 3 Months" means on three pages. */
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

  function dateFields(bar) {
    return {
      start: bar.querySelector('[data-filter-start]'),
      end: bar.querySelector('[data-filter-end]')
    };
  }

  /* Which preset the window on screen corresponds to, applied to every place
     in this bar that names it. The pill and the chip say the same thing
     because they are written by the same pass — they used to be able to
     disagree, and a filter bar whose two halves disagree is worse than one
     that says nothing.

     Only ever an override: with no preset matching, the server's rendering of
     the window stands, because "Aug 1 – Aug 14, 2026" is the true answer and
     this function has no better one. */
  function syncDateLabels(bar) {
    var f = dateFields(bar);
    if (!f.start || !f.end) return;

    var matched = null;
    bar.querySelectorAll('[data-preset]').forEach(function (btn) {
      var range = presetRange(btn.getAttribute('data-preset'));
      var hit = !!range && range.start === f.start.value && range.end === f.end.value;
      btn.setAttribute('aria-pressed', hit ? 'true' : 'false');
      if (hit && matched === null) matched = btn.textContent.trim();
    });

    if (matched) {
      bar.querySelectorAll('[data-date-label]').forEach(function (el) {
        el.textContent = matched;
      });
    }
    // A window nobody has a name for is one somebody typed, so the fields that
    // produced it open with the menu rather than behind another click. Only
    // when something is set: an empty range is the default, not a custom one.
    var custom = bar.querySelector('[data-filter-custom]');
    if (custom) custom.open = !matched && !!(f.start.value || f.end.value);
  }

  function sync() { bars().forEach(syncDateLabels); }

  /* ── Delegated listeners ─────────────────────────────────────────────── */

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.closest || !form.closest(BAR)) return;
    e.preventDefault();
    applyFilters(form);
  });

  document.addEventListener('click', function (e) {
    var target = e.target;
    if (!target.closest) return;

    var bar = target.closest(BAR);
    if (!bar) {
      // Click-away. A <details> stays open until something closes it, and a
      // click anywhere else on the page is the ordinary way to dismiss a menu.
      bars().forEach(function (b) { closeMenus(b); });
      return;
    }

    var preset = target.closest('[data-preset]');
    if (preset) {
      var range = presetRange(preset.getAttribute('data-preset'));
      var f = dateFields(bar);
      if (!range || !f.start || !f.end) return;
      f.start.value = range.start;
      f.end.value = range.end;
      // A preset is a complete answer, so it applies on the spot. Composing
      // several criteria before one refresh is what "+ Filters" is for.
      applyFilters(bar.querySelector('form'));
      return;
    }

    // "Clear" empties the controls in its own popover and leaves the rest of
    // the bar alone — it is the counterpart of the Apply beside it, not a
    // "clear all", which is a link to the server's own reset.
    var clear = target.closest('[data-filter-clear]');
    if (clear) {
      var pop = clear.closest('.ds-filter-menu__pop') || bar;
      pop.querySelectorAll('input[type="checkbox"], input[type="radio"]').forEach(
        function (box) { box.checked = box.hasAttribute('data-filter-default'); });
      pop.querySelectorAll('input[type="text"], input[type="search"]').forEach(
        function (field) { field.value = ''; });
    }
  });

  document.addEventListener('change', function (e) {
    var field = e.target;
    if (!field.closest || !field.closest(BAR)) return;
    // Everything else waits for Apply: that panel exists so three changes cost
    // one request rather than three.
    if (!field.hasAttribute('data-apply-now')) return;
    applyFilters(field.closest('form'));
  });

  // `toggle` does not bubble, so it is caught on the way down instead.
  document.addEventListener('toggle', function (e) {
    var menu = e.target;
    if (!menu.classList || !menu.classList.contains('ds-filter-menu')) return;
    var bar = menu.closest(BAR);
    if (bar && menu.open) closeMenus(bar, menu);
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    bars().forEach(function (bar) {
      var open = menus(bar, true);
      if (!open.length) return;

      // Innermost first: the custom-range disclosure lives inside the date
      // menu, and Escape closing both at once loses the reader's place.
      var custom = bar.querySelector('[data-filter-custom]');
      if (custom && custom.open && custom.contains(document.activeElement)) {
        custom.open = false;
        var summary = custom.querySelector('summary');
        if (summary) summary.focus();
        return;
      }

      var trigger = open[0].querySelector('.ds-filter-trigger');
      var inside = bar.contains(document.activeElement);
      closeMenus(bar);
      // Focus goes back to the pill that opened it, or Escape would drop the
      // keyboard user at the top of the document.
      if (trigger && inside) trigger.focus();
    });
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', sync);
  } else {
    sync();
  }
  // <main> was just replaced, so any bar in it is new markup that has never
  // been read. See spaNavigate() in base.html.
  document.addEventListener('check:navigated', sync);

  window.DoughFilters = { sync: sync, presetRange: presetRange };
})();
