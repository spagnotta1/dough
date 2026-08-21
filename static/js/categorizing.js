/* ══════════════════════════════════════════════════════════════════════════
   Automatic categorization, as the user sees it.
   ──────────────────────────────────────────────────────────────────────────
   The one poller of /api/sync/status in the application, and the controller
   for the dialog in templates/_categorizing.html.

   ## Why this is app-wide rather than part of the Connections page

   The pass it reports on runs on a scheduler thread, not in a request. It
   starts when a sync imports something — which happens when somebody links a
   bank, when they press Refresh, and twice a day on its own — and on a first
   connection it reads a household's whole history on the deep model, which is
   minutes. None of that is tied to the page the user happens to be looking
   at, so neither is this. It lives outside <main> (so the SPA swap cannot
   destroy it mid-pass) and in <head> with `defer` (so it is bound once for
   the life of the document, like filter-bar.js).

   ## Why the page does not poll as well

   Connections used to run its own 1.5s loop, and with the dialog added there
   would have been two loops asking the same endpoint the same question and
   two components deciding independently what "finished" means. Instead this
   is the only caller and it broadcasts:

       check:sync-state   every poll, with the raw status payload
       check:sync-idle    once, when the sync AND the categorization are done

   `check:sync-idle` is deliberately late: if the dialog is showing a finished
   report, the event is held until the user dismisses it. Connections reloads
   on that event, and reloading out from under somebody who is still reading
   what Dough did would destroy the only place it was ever said.

   ## What it never does

   It does not start work. Every fetch here is a GET of the status endpoint;
   the sync and the categorization are started by the page that asked for
   them, or by the scheduler. A failed poll is ignored and retried on the next
   tick — the pass is running on the server either way, and an error toast per
   1.5 seconds is worse than a bar that pauses.
   ══════════════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';

  var POLL_MS = 1500;
  var STATUS_URL = '/api/sync/status';

  var timer = null;
  /* The status from the last successful poll, so a dismissal that happens
     after polling stopped still has something to report and to hand to the
     idle listeners. */
  var latest = null;
  /* True once this run has put the dialog on screen. It is what separates
     "the pass finished" from "the pass finished and somebody was watching",
     which is the difference between closing quietly and holding the idle
     event until they have read the result. */
  var shown = false;
  /* An idle status waiting on the user to dismiss the finished dialog. */
  var held = null;
  /* The user closed it. "Keep going in the background" has to mean that, so
     nothing reopens for the rest of this pass — not the next progress frame,
     and not the finished report, which downgrades to a toast. A modal that
     comes back after being dismissed is the behaviour that teaches people to
     dismiss things without reading them. */
  var dismissed = false;

  function dialog() { return document.getElementById('categorizing-dialog'); }

  function part(root, hook) { return root.querySelector('[data-cat-' + hook + ']'); }

  function count(n) { return Number(n || 0).toLocaleString(); }

  function plural(n, one, many) { return Number(n) === 1 ? one : many; }

  /* ── Copy ────────────────────────────────────────────────────────────────
     Kept together rather than inlined at each write, because these six
     strings are the entire product voice of the feature and they have to
     agree with each other. Two rules they follow: the subject is Dough and
     the object is the user's money ("your transactions", not "the batch"),
     and no sentence claims more than the server reported — a pass that could
     not place everything says so rather than rounding up to "all sorted".  */

  function titleFor(progress) {
    if (progress && progress.first_run) return 'Sorting out your transactions';
    return 'Catching up on what just came in';
  }

  function subFor(progress) {
    if (!progress) return 'Getting started.';
    if (progress.phase === 'applying') return 'Filing everything into its category.';
    if (progress.first_run) return 'Reading every transaction your bank sent over.';
    return 'Reading the transactions that just arrived.';
  }

  function countFor(progress) {
    if (!progress || !progress.transactions_total) return '';
    if (progress.phase === 'applying') {
      return 'Read all ' + count(progress.transactions_total) + ' — filing them now.';
    }
    return count(progress.transactions_done) + ' of ' +
           count(progress.transactions_total) + ' transactions read';
  }

  function noteFor(progress) {
    if (progress && progress.first_run) {
      return 'This is a one-time read of your history, so it can take a few ' +
             'minutes. You can close this and keep using Dough — it carries ' +
             'on in the background.';
    }
    return 'You can close this and keep using Dough — it carries on in the ' +
           'background.';
  }

  function doneNote(done) {
    var n = Number(done.transactions_categorized || 0);
    var rules = Number(done.rules_added || 0);
    var left = Number(done.remaining_uncategorized || 0);
    var text = 'I sorted ' + count(n) + ' ' + plural(n, 'transaction', 'transactions') +
               ' into ' + count(rules) + ' ' + plural(rules, 'category rule', 'category rules') +
               '. You can change any of them on the Rules page.';
    /* Whatever is still bare, and which kind of bare it is. From the ledger
       the two look identical — a row with no category — and the fix is
       different for each: `partial` means Dough never got to it, `left` means
       Dough read it and could not place it. Saying neither is what made a
       half-categorized ledger read as a bug. */
    if (done.partial) {
      text += ' Some of your history I could not get through this time — ' +
              'open Rules and press Analyze to finish it.';
    } else if (left) {
      text += ' ' + count(left) + ' ' +
              plural(left, 'transaction is', 'transactions are') +
              ' still uncategorized — I read those and could not place them, ' +
              'so they are yours to name on the Rules page.';
    }
    return text;
  }

  /* ── Rendering ─────────────────────────────────────────────────────────── */

  function render(status) {
    var root = dialog();
    if (!root) return;
    var progress = status.categorization_progress;

    part(root, 'title').textContent = titleFor(progress);
    part(root, 'sub').textContent = subFor(progress);
    part(root, 'count').textContent = countFor(progress);
    part(root, 'note').textContent = noteFor(progress);

    var percent = progress ? Number(progress.percent || 0) : 0;
    var bar = part(root, 'bar');
    part(root, 'fill').style.width = percent + '%';
    bar.setAttribute('aria-valuenow', String(percent));
    /* The percentage is the drawing; the sentence is the information. A
       screen reader gets the sentence. */
    bar.setAttribute('aria-valuetext', countFor(progress) || 'Getting started');
  }

  function renderDone(status) {
    var root = dialog();
    if (!root) return;
    var done = status.last_categorization || {};

    root.classList.add('cat-dialog--done');
    part(root, 'title').textContent = 'All sorted';
    part(root, 'sub').textContent = 'Your transactions are categorized.';
    part(root, 'count').textContent = '';
    part(root, 'note').textContent = doneNote(done);

    var bar = part(root, 'bar');
    part(root, 'fill').style.width = '100%';
    bar.setAttribute('aria-valuenow', '100');
    bar.setAttribute('aria-valuetext', 'Finished');

    part(root, 'hide').hidden = true;
    var ok = part(root, 'done');
    ok.hidden = false;

    var mascot = root.querySelector('.cat-head__mascot [data-dough]');
    if (mascot && global.Dough) global.Dough.set(mascot, 'success');

    /* Moving focus off the button that is now hidden. Without this the focus
       ring sits on a `hidden` element and the next Tab starts from the top of
       the page rather than from the one action left. */
    try { ok.focus(); } catch (e) {}
  }

  function open(status) {
    var root = dialog();
    if (!root || root.open || dismissed) return;
    root.classList.remove('cat-dialog--done');
    part(root, 'hide').hidden = false;
    part(root, 'done').hidden = true;
    var mascot = root.querySelector('.cat-head__mascot [data-dough]');
    if (mascot && global.Dough) global.Dough.set(mascot, 'thinking');
    render(status);
    try { root.showModal(); } catch (e) { return; }
    shown = true;
  }

  function close() {
    var root = dialog();
    if (root && root.open) root.close();
  }

  /* ── The loop ────────────────────────────────────────────────────────────
     `read` is the fetch and the broadcast, with no opinion about what the
     answer means. `tick` is one beat of an active watch, which is the only
     place allowed to decide that everything is over. Keeping those apart is
     what lets `detect` ask the same question on page load without its answer
     being mistaken for a sync that just finished.                          */

  function read() {
    return fetch(STATUS_URL, { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (status) {
        if (!status) return null;
        latest = status;
        document.dispatchEvent(new CustomEvent('check:sync-state',
                                               { detail: status }));
        return status;
      })
      .catch(function () { return null; /* keep polling; see the header */ });
  }

  function tick() {
    return read().then(function (status) {
      if (!status) return;
      if (status.categorizing) { open(status); render(status); return; }
      if (status.running) return;
      stop();
      finish(status);
    });
  }

  /* Everything is done. How loudly to say so depends on two things.

     Whether it did anything: a sync that imported four transactions Dough
     already had rules for is a non-event, and popping a modal to report it
     would train people to dismiss the one that matters.

     And whether anyone is still watching: somebody who pressed "keep going in
     the background" has told us they do not want this panel, so the result
     reaches them as a toast instead. Either way it is *reported* — that is the
     flag Connections reads to decide whether its own message would be saying
     the same thing twice. */
  function finish(status) {
    var done = status.last_categorization || {};
    var worked = !done.skipped && Number(done.transactions_categorized || 0) > 0;

    if (worked && shown && !dismissed) {
      renderDone(status);
      held = status;          /* released by the `close` handler below */
      return;
    }
    if (worked && dismissed && global.showToast) {
      global.showToast(doneNote(done), 'success');
    }
    close();
    idle(status);
  }

  function idle(status) {
    shown = false;
    held = null;
    dismissed = false;
    document.dispatchEvent(new CustomEvent('check:sync-idle', {
      /* `reported` tells a listener whether the dialog already told the user
         what was categorized. Connections uses it to decide whether its own
         toast would be saying the same thing twice. */
      detail: { status: status, reported: !!(status.last_categorization &&
                                             !status.last_categorization.skipped &&
                                             status.last_categorization.transactions_categorized) }
    }));
  }

  /* No immediate tick. `run_sync` returns 202 the instant the worker thread is
     spawned, and the thread sets `running` a moment later — a poll fired in
     the same turn as the POST reads the state from *before* the sync, sees
     nothing in flight and calls it finished. Waiting out the first interval
     is what the page did before this file existed, and it is load-bearing. */
  function start() {
    if (timer !== null) return;
    dismissed = false;
    timer = setInterval(tick, POLL_MS);
  }

  function stop() {
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  /* ── Wiring ──────────────────────────────────────────────────────────── */

  function bind() {
    var root = dialog();
    if (!root || root.dataset.catBound) return;
    root.dataset.catBound = '1';

    part(root, 'hide').addEventListener('click', function () {
      close();
      if (global.showToast) {
        global.showToast('I will keep sorting in the background.', 'info');
      }
    });
    part(root, 'done').addEventListener('click', function () { close(); });

    /* One handler for every way a <dialog> can close — the two buttons, Esc,
       and a backdrop dismissal. A held idle event is released here rather
       than in the click handlers so that Esc on the finished panel does not
       strand the page waiting for an event that never fires. */
    root.addEventListener('close', function () {
      if (held) { idle(held); return; }
      /* Closed while the pass is still going: they asked to be left alone.
         `shown` stays true — they did see it, so the result is still theirs to
         be told about — and `dismissed` decides that the telling is a toast. */
      dismissed = true;
    });
  }

  /* On load, ask once whether something is already running. This is what
     catches the two cases nothing else can: a scheduled pass that started
     while nobody was looking, and a user who navigated away mid-pass and came
     back. `check:navigated` repeats it for a soft navigation, which has no
     DOMContentLoaded of its own. */
  function detect() {
    if (timer !== null) return;
    read().then(function (status) {
      if (!status || !(status.running || status.categorizing)) return;
      if (status.categorizing) open(status);
      start();
    });
  }

  function boot() {
    bind();
    detect();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
  document.addEventListener('check:navigated', detect);

  global.SyncWatch = { start: start, stop: stop, status: function () { return latest; } };
})(window);
