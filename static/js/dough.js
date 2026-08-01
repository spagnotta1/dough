/* Dough — the mascot, placed.
   ===========================================================================

   Dough's artwork is `static/img/dough_V2.jpg`. It is a finished brand asset
   and it is the only Dough this product has. This file does not draw him,
   approximate him, or hold a single coordinate of him: it decides which crop
   of that artwork a slot gets, at what size, and with what label. See
   AGENTS.md.

   This file used to compose him at runtime from ~13 hand-authored vector
   paths "traced by measurement" from the reference. It was a redraw, and it
   shipped a different dog — narrow strap ears instead of plush flared ones, a
   smooth dome where the reference has fur scalloping on the cheeks, a small
   nose and small tongue against the reference's wide open smile. A trace is a
   redraw however carefully it is measured, which is why the rule is "use the
   image" and not "match the image".

   The two crops, both produced by `python tools/build_dough_assets.py`:

     FULL   the whole seated puppy — dough.png. Heroes, empty states, the
            404, onboarding: anywhere he is big enough to read.
     HEAD   a square around head + ears — dough-head.png. `.dough-avatar`
            clips to a *disc* 28-40px across, and the seated body inside one
            of those leaves a head about ten pixels tall, which is not a face.

   Usage — markup, hydrated automatically (SPA navigations included):

       <span data-dough="thinking" data-dough-size="56"></span>
       <span data-dough="happy" data-dough-label="Dough"></span>
       <span data-dough="proud" data-dough-tight></span>   <!-- avatar crop -->

   Accessibility: a mascot never carries meaning on its own — everywhere Dough
   appears the same information is in the adjacent text. So a slot with no
   data-dough-label is decorative and gets alt="", and every animation in
   dough.css sits behind a prefers-reduced-motion guard.

   ── About the mood names ──────────────────────────────────────────────────
   The artwork has one pose, so a mood no longer changes what Dough looks
   like. The names stay because ~20 templates use them, and because each still
   selects a *motion* the stylesheet animates. Motion is the one thing allowed
   to vary: it moves the image, it never modifies it.

   Adding a mood is one row in MOODS. Adding a *pose* is not possible, and is
   not supposed to be — that would mean new artwork.
*/
(function (global) {
  'use strict';

  /* Where the artwork lives. Derived from this file's own URL so a static
     prefix or a CDN needs no configuration; the literal is only a fallback
     for a page that inlines this script. */
  var BASE = (function () {
    var s = document.currentScript;
    var m = s && s.src && s.src.match(/^(.*)\/js\/dough\.js(\?.*)?$/);
    return m ? m[1] + '/img/' : '/static/img/';
  })();

  /* Intrinsic pixel sizes of the shipped crops, so a slot can reserve its
     space before the image arrives — an unsized <img> collapses to nothing
     and then shoves the page down on load, which on the dashboard is a
     visible jolt. tests/test_dough_mascot.py reads the real PNGs and fails if
     these drift, so they cannot quietly stop matching the files. */
  var ART = {
    full: { src: 'dough.png',      w: 465, h: 512 },
    head: { src: 'dough-head.png', w: 256, h: 256 }
  };

  /* ── States ───────────────────────────────────────────────────────────────
     A state is *semantic*: it says what the product is doing, and the
     presentation layer decides what that looks like. It never selects a
     different Dough — there is one pose, and there will be one pose until
     somebody commissions layered artwork.

     Each row controls three things, none of which touch the artwork:

       anim      the idle motion class dough.css animates
       dots      show the animated thinking dots *beside* him
       confetti  fire a page-level confetti burst near him

     `dots` and `confetti` are the props that used to be drawn onto the
     mascot — a magnifier over his shoulder, z's above his head, confetti
     falling across the frame. They were SVG artwork, so they went with the
     redraw, and bringing them back as artwork would be drawing on Dough
     again. As UI elements they are also simply better: a real "Thinking…"
     line is readable, translatable, and available to a screen reader, which
     three dots inside an aria-hidden drawing never were.

     There is no tail wag. It rotated the tail path, and a raster has no parts
     — the tail cannot move without the whole dog moving with it, and a fake
     wag reads worse than none. If layered artwork ever arrives, that is the
     first thing to animate.                                                */
  var STATES = {
    idle:      { anim: 'blink' },
    loading:   { anim: 'tilt',    dots: true },
    thinking:  { anim: 'tilt',    dots: true },
    searching: { anim: 'sweep',   dots: true },
    celebrate: { anim: 'bounce',  confetti: true },
    success:   { anim: 'bounce' },
    sleep:     { anim: 'breathe' },
    wave:      { anim: 'bounce' }
  };

  /* The names ~20 templates already use, mapped onto the states above. Kept
     rather than rewritten so the vocabulary can move without a flag day, and
     because several of them read better at a call site than the state does:
     `concerned` beside an over-budget figure says more than `sleep`. */
  var ALIASES = {
    happy:       'idle',
    greeting:    'wave',
    curious:     'thinking',
    watching:    'idle',
    reviewing:   'idle',
    excited:     'success',
    proud:       'success',
    celebrating: 'celebrate',
    concerned:   'sleep',
    sleeping:    'sleep'
  };

  var DEFAULT = 'idle';

  function resolve(name) {
    if (STATES[name]) return name;
    if (ALIASES[name]) return ALIASES[name];
    return DEFAULT;
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* The markup for one slot: the <img>, plus whatever UI the state asks to sit
     beside him. `size` is the rendered height in px; the width follows the
     artwork's own aspect, so a caller only ever sets one. */
  function markup(name, opts) {
    opts = opts || {};
    var state = resolve(name);
    var s = STATES[state];
    var tight = !!opts.tight;
    var art = tight ? ART.head : ART.full;
    var size = opts.size || 64;
    var w = Math.round(size * art.w / art.h);
    var label = opts.label || '';
    var animate = opts.anim !== false;

    var cls = 'dough dough-' + state + (tight ? ' dough-tight' : '');
    if (animate) cls += ' dough-anim-' + s.anim;

    /* srcset hands the 2x file to retina without spending the bytes on a 1x
       screen. decoding=async keeps a 900px hero off the critical path. */
    var img =
      '<img class="' + cls + '" ' +
      'src="' + BASE + art.src + '" ' +
      'srcset="' + BASE + art.src + ' 1x, ' +
                  BASE + art.src.replace('.png', '@2x.png') + ' 2x" ' +
      'width="' + w + '" height="' + size + '" ' +
      'decoding="async" draggable="false" ' +
      (label ? 'alt="' + esc(label) + '"' : 'alt="" aria-hidden="true"') +
      '>';

    /* The effects are siblings of the image, never children, and never
       overlaid on it. An avatar disc has no room for either and clips them
       anyway, so a tight slot gets neither. */
    var extra = '';
    if (!tight && animate && s.dots) extra += DOTS;
    if (!tight && animate && s.confetti) extra += confetti();
    return img + extra;
  }

  /* Three dots beside Dough while something is generating. aria-hidden: the
     component that uses this carries a real text label ("Thinking…"), and
     that is what a screen reader should get. */
  var DOTS =
    '<span class="dough-dots" aria-hidden="true">' +
      '<i></i><i></i><i></i>' +
    '</span>';

  /* A page-level confetti burst — positioned against the slot, not drawn on
     the dog. Twelve pieces is enough to read as celebration and few enough
     that it stays one compositor layer. */
  function confetti(pieces) {
    var out = '<span class="dough-confetti" aria-hidden="true">';
    for (var i = 0; i < (pieces || 12); i++) {
      out += '<i style="--i:' + i + '"></i>';
    }
    return out + '</span>';
  }

  /* ── Hydration ────────────────────────────────────────────────────────────
     Slots are filled in place. The `doughDone` marker keeps a re-hydration
     (SPA swap, MutationObserver echo) from rebuilding a slot that is already
     drawn, which would restart its animation on every DOM mutation.        */
  function fill(el) {
    var name = el.getAttribute('data-dough') || DEFAULT;
    if (el.dataset.doughDone === name) return;
    el.innerHTML = markup(name, {
      size:  parseFloat(el.getAttribute('data-dough-size')) || 64,
      tight: el.hasAttribute('data-dough-tight'),
      label: el.getAttribute('data-dough-label') || '',
      anim:  el.getAttribute('data-dough-anim') === 'off' ? false : true
    });
    el.dataset.doughDone = name;
  }

  function hydrate(root) {
    var scope = root || document;
    var slots = scope.querySelectorAll ? scope.querySelectorAll('[data-dough]') : [];
    for (var i = 0; i < slots.length; i++) fill(slots[i]);
  }

  function set(el, name) {
    if (!el) return;
    el.setAttribute('data-dough', name);
    fill(el);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hydrate(); });
  } else {
    hydrate();
  }

  /* The SPA layer replaces <main> wholesale and knows nothing about Dough.
     Watching the tree is simpler and more durable than teaching every
     navigation path to call hydrate(). */
  if (global.MutationObserver) {
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var added = records[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var n = added[j];
          if (n.nodeType !== 1) continue;
          if (n.hasAttribute && n.hasAttribute('data-dough')) fill(n);
          hydrate(n);
        }
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
  }

  global.Dough = {
    markup: markup,
    set: set,
    hydrate: hydrate,
    resolve: resolve,
    states: Object.keys(STATES),
    aliases: ALIASES,
    art: ART
  };
})(window);
