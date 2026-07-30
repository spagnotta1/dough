/* ══════════════════════════════════════════════════════════════════════════
   Dough — the mascot system.
   ──────────────────────────────────────────────────────────────────────────
   Dough is the product's face, not a decoration bolted onto the AI page, so
   the mascot has to be cheap to place anywhere: a dashboard card header, an
   empty state, a chat avatar, a toast. That rules out a folder of PNGs — one
   file per expression per theme per density is unmaintainable, and a raster
   can't inherit the accent color of 17 user themes.

   So Dough is composed at runtime from one set of vector parts. The ears,
   fur crown, muzzle and paws are shared by every expression; only the eyes,
   brows, mouth and any prop swap out. Adding a thirteenth mood means adding
   one row to EXPRESSIONS, not redrawing a dog.

   The drawing follows the reference art: a cream-golden doodle puppy lying
   down with its chin between its front paws. The details that make it *that*
   dog rather than a generic cartoon dog, and which therefore must survive
   every expression:

     · a scalloped fur crown — the head silhouette is bumpy, never a smooth dome
     · big fur-edged ears that hang wider than the head itself
     · large almond eyes carrying a white catchlight AND a four-point sparkle
     · a two-lobe muzzle that dips in the middle, under the nose
     · a warm brown outline, not black

   Usage — markup, hydrated automatically (SPA navigations included):

       <span data-dough="thinking" data-dough-size="56"></span>
       <span data-dough="happy" data-dough-label="Dough"></span>
       <span data-dough="proud" data-dough-tight></span>   <!-- avatar crop -->

   Usage — imperative:

       Dough.svg('searching', { size: 72 })   → SVG markup string
       Dough.set(el, 'celebrating')           → re-render an existing slot

   Accessibility: a mascot never carries meaning on its own — every place
   Dough appears, the same information is in the adjacent text. So the SVG is
   aria-hidden unless the caller passes data-dough-label, and all motion is
   defined in dough.css behind a prefers-reduced-motion guard.
   ══════════════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';

  var uid = 0;

  /* ── Palette ───────────────────────────────────────────────────────────
     Fixed, not themed. Dough is a brand asset: a dog whose fur turned
     cyberpunk magenta with the theme picker would read as a bug, and the
     surrounding card already carries the theme. Only the accessory accent
     (magnifier rim, sparkles) follows --dough-accent so props sit with the
     UI they annotate.                                                     */
  var LINE  = '#5c3a22';   // outline — warm brown, never black
  var EYE   = '#3b2317';
  var IRIS  = '#7c4b2c';   // the lit lower crescent inside the eye
  var SNOUT = '#f9dfba';
  var PAW   = '#f6d3a4';
  var NOSEC = '#7a5138';

  /* ══════════════════════════════════════════════════════════════════════
     Shared geometry
     ══════════════════════════════════════════════════════════════════════ */

  function defs(id) {
    return '' +
      '<defs>' +
        '<linearGradient id="' + id + 'f" x1="0.25" y1="0" x2="0.6" y2="1">' +
          '<stop offset="0" stop-color="#f4cb96"/>' +
          '<stop offset="0.55" stop-color="#e6ab6c"/>' +
          '<stop offset="1" stop-color="#d69350"/>' +
        '</linearGradient>' +
        '<linearGradient id="' + id + 'e" x1="0" y1="0" x2="0.45" y2="1">' +
          '<stop offset="0" stop-color="#eab179"/>' +
          '<stop offset="1" stop-color="#cd8b4b"/>' +
        '</linearGradient>' +
      '</defs>';
  }

  /* Ears. Drawn before the head so the head overlaps their inner edge.
     Long, soft curves — an earlier pass gave the outer contour many small
     scallops to read as fur, and at any size below ~80px the bumps stacked
     into a cauliflower texture. The fur cue belongs on the crown, where
     there is one row of it; the ears just need to hang heavy and wide. */
  var EAR_L =
    'M176 216 C132 196, 82 216, 58 262 C30 282, 26 340, 48 378 ' +
    'C58 420, 100 452, 142 440 C170 430, 182 406, 177 380 ' +
    'C186 326, 186 250, 176 216 Z';
  var EAR_R =
    'M336 216 C380 196, 430 216, 454 262 C482 282, 486 340, 464 378 ' +
    'C454 420, 412 452, 370 440 C342 430, 330 406, 335 380 ' +
    'C326 326, 326 250, 336 216 Z';

  /* The fur crown. Nine short outward-bulging arcs ride an ellipse across the
     top of the skull; the bottom half is a plain wide curve because the ears
     and muzzle cover it anyway. This scalloped edge is the single strongest
     cue that Dough is a doodle and not a beagle, so it is never simplified —
     not even in the tight avatar crop. */
  var HEAD =
    'M131.9 278.5 Q125.1 255.7 144.8 241.8 Q145.4 217.9 168.5 210.8 ' +
    'Q176.5 188.3 200.8 188.6 Q215.5 169.5 238.5 177.2 Q258.5 163.6 277.9 177.9 ' +
    'Q301.2 171.0 315.2 190.5 Q339.4 191.1 346.6 213.9 Q369.4 221.9 369.2 245.7 ' +
    'Q388.4 260.2 380.8 282.7 C388 340, 380 396, 330 428 C302 445, 210 445, 182 428 ' +
    'C132 396, 124 340, 131.9 278.5 Z';

  /* The two-lobe muzzle: a cheek puff either side of a dip that sits directly
     under the nose, closing again into a soft notch at the chin. */
  var MUZZLE =
    'M256 374 C250 356, 226 350, 206 352 C168 356, 146 380, 146 404 ' +
    'C146 432, 174 450, 206 450 C232 450, 250 440, 256 424 ' +
    'C262 440, 280 450, 306 450 C338 450, 366 432, 366 404 ' +
    'C366 380, 344 356, 306 352 C286 350, 262 356, 256 374 Z';

  var NOSE =
    '<path d="M224 366 C224 352, 288 352, 288 366 C288 386, 272 398, 256 398 ' +
             'C240 398, 224 386, 224 366 Z" fill="' + NOSEC + '"/>' +
    '<ellipse cx="246" cy="364" rx="14" ry="6" fill="#b08160" opacity="0.5"/>';

  /* Fur texture — a few strokes on the ears and cheeks. Thin and low-contrast
     on purpose: at avatar sizes they should blur into shading, not stripes. */
  var TEXTURE =
    '<g fill="none" stroke="' + LINE + '" stroke-width="7" stroke-linecap="round" opacity="0.5">' +
      '<path d="M84 300 C76 326, 78 356, 92 380"/>' +
      '<path d="M132 332 C128 352, 132 372, 141 386"/>' +
      '<path d="M428 300 C436 326, 434 356, 420 380"/>' +
      '<path d="M380 332 C384 352, 380 372, 371 386"/>' +
    '</g>';

  /* The forehead tuft — the small cowlick over the brow. */
  var TUFT =
    '<path d="M240 246 C244 224, 256 224, 258 242 C262 224, 274 226, 274 246" ' +
          'fill="none" stroke="' + LINE + '" stroke-width="8" stroke-linecap="round"/>';

  function paws(wave) {
    /* The left paw lifts for the greeting wave; everything else keeps both
       paws down, chin resting between them, which is the reference pose.

       The lift is baked into the markup rather than left to the animation,
       because a reduced-motion user gets no animation at all — and a greeting
       whose only difference from `happy` is motion is no greeting for them.
       The wave then plays on an inner group, on top of the pose, so the two
       compose instead of overwriting each other. */
    var toes = '<g fill="none" stroke="' + LINE + '" stroke-width="8" stroke-linecap="round">';
    var l = (wave ? '<g transform="rotate(-16 190 470)">' : '') +
            '<g class="dough-paw-l' + (wave ? ' dough-wave' : '') + '">' +
              '<path d="M150 430 C112 430, 92 448, 92 468 C92 486, 116 494, 152 494 ' +
                       'C190 494, 214 486, 214 468 C214 448, 190 430, 150 430 Z" fill="' + PAW + '"/>' +
              toes + '<path d="M130 462 L128 492"/><path d="M172 462 L172 494"/></g>' +
            '</g>' + (wave ? '</g>' : '');
    var r = '<g class="dough-paw-r">' +
              '<path d="M362 430 C400 430, 420 448, 420 468 C420 486, 396 494, 360 494 ' +
                       'C322 494, 298 486, 298 468 C298 448, 322 430, 362 430 Z" fill="' + PAW + '"/>' +
              toes + '<path d="M382 462 L384 492"/><path d="M340 462 L340 494"/></g>' +
            '</g>';
    return l + r;
  }

  /* ══════════════════════════════════════════════════════════════════════
     Eyes
     ──────────────────────────────────────────────────────────────────────
     Two catchlights, not one: a round white dot high on the inner side and a
     four-point sparkle beside it. That pairing is what reads as "optimistic"
     instead of "sleepy", and it is the detail the reference art leans on
     hardest, so it survives every open-eyed expression and every size.

     A glance is expressed by moving the catchlights and the lit iris
     crescent, never the eye outline — the eye is almost entirely pupil, so
     shifting the outline would look like the eyeball left the socket.
     ══════════════════════════════════════════════════════════════════════ */

  function sparkle(x, y, r) {
    var i = r * 0.24;
    return '<path fill="#ffffff" d="M' + x + ' ' + (y - r) +
           ' Q' + (x + i) + ' ' + (y - i) + ' ' + (x + r) + ' ' + y +
           ' Q' + (x + i) + ' ' + (y + i) + ' ' + x + ' ' + (y + r) +
           ' Q' + (x - i) + ' ' + (y + i) + ' ' + (x - r) + ' ' + y +
           ' Q' + (x - i) + ' ' + (y - i) + ' ' + x + ' ' + (y - r) + ' Z"/>';
  }

  function eye(cx, rot, o) {
    var cy = o.cy, rx = o.rx, ry = o.ry, dx = o.dx, dy = o.dy, hi = o.hi;
    return '<g transform="rotate(' + rot + ' ' + cx + ' ' + cy + ')">' +
             '<ellipse cx="' + cx + '" cy="' + cy + '" rx="' + rx + '" ry="' + ry + '" fill="' + EYE + '"/>' +
             '<ellipse cx="' + (cx + dx * 0.4) + '" cy="' + (cy + ry * 0.40 + dy * 0.4) + '" ' +
                      'rx="' + (rx * 0.66) + '" ry="' + (ry * 0.30) + '" fill="' + IRIS + '" opacity="0.92"/>' +
             '<circle cx="' + (cx - 13 + dx) + '" cy="' + (cy - 17 + dy) + '" r="' + (12 * hi) + '" fill="#ffffff"/>' +
             sparkle(cx + 11 + dx, cy + 3 + dy, 10.5 * hi) +
           '</g>';
  }

  function openEyes(o) {
    o = o || {};
    var p = {
      cy: 320 + (o.dyEye || 0),
      rx: o.rx || 36,
      ry: o.ry || 43,
      dx: o.dx || 0,
      dy: o.dy || 0,
      hi: o.hi === undefined ? 1 : o.hi
    };
    return '<g class="dough-eyes">' + eye(202, -7, p) + eye(310, 7, p) + '</g>';
  }

  /* Closed eyes. `down` is the sleeping curve; up is the happy squint that
     pleasure and pride use, so celebration never reads as surprise. */
  function archEyes(down) {
    var d = down
      ? 'M170 312 Q202 346 234 312 M278 312 Q310 346 342 312'
      : 'M170 336 Q202 294 234 336 M278 336 Q310 294 342 336';
    return '<g class="dough-eyes" fill="none" stroke="' + EYE + '" stroke-width="14" ' +
           'stroke-linecap="round"><path d="' + d + '"/></g>';
  }

  /* Brows. The reference dog has none at rest, so they appear only where the
     mood needs them — and concern lifts the inner ends (worry) rather than
     dropping them (disapproval), because Dough never judges. */
  function brows(kind) {
    var g = '<g class="dough-brow" fill="none" stroke="' + LINE + '" stroke-width="10" ' +
            'stroke-linecap="round" opacity="0.85">';
    if (kind === 'worry') {
      return g + '<path d="M168 268 C188 254, 214 258, 230 270"/>' +
                 '<path d="M344 268 C324 254, 298 258, 282 270"/></g>';
    }
    if (kind === 'raise') {
      return g + '<path d="M170 274 C190 264, 212 264, 228 272"/>' +
                 '<path d="M282 256 C300 242, 326 242, 344 254"/></g>';
    }
    return '';
  }

  /* ══════════════════════════════════════════════════════════════════════
     Mouths — all sit in the dip between the muzzle lobes
     ══════════════════════════════════════════════════════════════════════ */
  function mouth(kind) {
    var philtrum = '<path d="M256 398 L256 424" fill="none" stroke="' + LINE +
                   '" stroke-width="9" stroke-linecap="round"/>';
    var line = '<g class="dough-mouth" fill="none" stroke="' + LINE +
               '" stroke-width="9" stroke-linecap="round">';

    if (kind === 'open') {
      return philtrum +
        '<path d="M224 428 C232 464, 280 464, 288 428 Z" fill="#7a3b34"/>' +
        '<path d="M238 450 C246 466, 266 466, 274 450 Z" fill="#e8807f"/>';
    }
    if (kind === 'grin') {
      return philtrum +
        '<path d="M212 424 C222 472, 290 472, 300 424 Z" fill="#7a3b34"/>' +
        '<path d="M230 452 C242 476, 270 476, 282 452 Z" fill="#e8807f"/>';
    }
    if (kind === 'o') {
      return philtrum + '<ellipse cx="256" cy="440" rx="16" ry="14" fill="#7a3b34"/>';
    }
    if (kind === 'small') {   // gentle and closed — concern, thinking, review
      return philtrum + line + '<path d="M240 436 C247 444, 265 444, 272 436"/></g>';
    }
    /* default: the resting closed smile */
    return philtrum + line + '<path d="M230 434 C242 450, 270 450, 282 434"/></g>';
  }

  /* ══════════════════════════════════════════════════════════════════════
     Props — drawn outside the head group so they are not tilted with it, and
     only in the full frame; an avatar crop has no room for them.
     ══════════════════════════════════════════════════════════════════════ */
  var PROPS = {
    magnifier:
      '<g class="dough-prop dough-prop-search">' +
        '<circle cx="424" cy="228" r="52" fill="#ffffff" opacity="0.18"/>' +
        '<circle cx="424" cy="228" r="52" fill="none" stroke="var(--dough-accent, #7c3aed)" stroke-width="16"/>' +
        '<path d="M461 265 L498 302" stroke="var(--dough-accent, #7c3aed)" stroke-width="20" stroke-linecap="round"/>' +
      '</g>',
    zzz:
      '<g class="dough-prop dough-prop-zzz" fill="var(--dough-accent, #7c3aed)" ' +
         'font-family="ui-sans-serif, system-ui, sans-serif" font-weight="700">' +
        '<text class="dough-z1" x="398" y="236" font-size="52">z</text>' +
        '<text class="dough-z2" x="440" y="190" font-size="38">z</text>' +
        '<text class="dough-z3" x="470" y="154" font-size="26">z</text>' +
      '</g>',
    dots:
      '<g class="dough-prop dough-prop-dots" fill="var(--dough-accent, #7c3aed)">' +
        '<circle class="dough-d1" cx="404" cy="228" r="15"/>' +
        '<circle class="dough-d2" cx="448" cy="192" r="11"/>' +
        '<circle class="dough-d3" cx="480" cy="164" r="7.5"/>' +
      '</g>',
    sparkles:
      '<g class="dough-prop dough-prop-spark" fill="var(--dough-accent, #7c3aed)">' +
        '<path class="dough-s1" d="M92 194 l10 26 26 10 -26 10 -10 26 -10 -26 -26 -10 26 -10 Z"/>' +
        '<path class="dough-s2" d="M424 172 l8 21 21 8 -21 8 -8 21 -8 -21 -21 -8 21 -8 Z"/>' +
        '<path class="dough-s3" d="M372 232 l6 15 15 6 -15 6 -6 15 -6 -15 -15 -6 15 -6 Z"/>' +
      '</g>',
    confetti:
      '<g class="dough-prop dough-prop-confetti">' +
        '<rect class="dough-c1" x="88" y="188" width="20" height="28" rx="5" fill="var(--dough-accent, #7c3aed)" transform="rotate(-18 98 202)"/>' +
        '<rect class="dough-c2" x="410" y="168" width="18" height="26" rx="5" fill="#14b8a6" transform="rotate(22 419 181)"/>' +
        '<circle class="dough-c3" cx="152" cy="168" r="11" fill="#f5b301"/>' +
        '<circle class="dough-c4" cx="370" cy="206" r="9" fill="var(--dough-accent, #7c3aed)"/>' +
        '<rect class="dough-c5" x="334" y="154" width="15" height="22" rx="4" fill="#14b8a6" transform="rotate(-30 341 165)"/>' +
      '</g>'
  };

  /* ══════════════════════════════════════════════════════════════════════
     Expression table
     ──────────────────────────────────────────────────────────────────────
     One row per mood. `anim` names a class dough.css animates; every one of
     those animations is disabled under prefers-reduced-motion, so each row
     must also read correctly as a still frame.
     ══════════════════════════════════════════════════════════════════════ */
  var EXPRESSIONS = {
    happy:       { eyes: openEyes(),                                   mouth: 'smile', anim: 'blink' },
    greeting:    { eyes: openEyes(),                                   mouth: 'open',  anim: 'blink', wave: true },
    curious:     { eyes: openEyes({ dx: 9, dy: -10 }), brow: 'raise',  mouth: 'o',     anim: 'tilt', tilt: -5 },
    thinking:    { eyes: openEyes({ dx: -10, dy: -9, ry: 30 }),        mouth: 'small', anim: 'tilt', tilt: -5, prop: 'dots' },
    searching:   { eyes: openEyes({ dx: 12, dy: -6 }),                 mouth: 'small', anim: 'sweep', prop: 'magnifier' },
    watching:    { eyes: openEyes({ dx: 11, rx: 35, ry: 43 }),         mouth: 'small', anim: 'blink' },
    reviewing:   { eyes: openEyes({ dy: 8, ry: 32, dyEye: 4 }),        mouth: 'small', anim: 'blink' },
    excited:     { eyes: openEyes({ rx: 37, ry: 45, hi: 1.15 }),       mouth: 'grin',  anim: 'bounce', prop: 'sparkles' },
    proud:       { eyes: archEyes(false),                              mouth: 'open',  anim: 'breathe' },
    celebrating: { eyes: archEyes(false),                              mouth: 'grin',  anim: 'bounce', prop: 'confetti' },
    concerned:   { eyes: openEyes({ dy: 7, ry: 37 }), brow: 'worry',   mouth: 'small', anim: 'breathe' },
    sleeping:    { eyes: archEyes(true),                               mouth: 'o',     anim: 'breathe', prop: 'zzz' }
  };

  var DEFAULT = 'happy';

  var FULL  = '0 130 512 400';   // room beside the head for props

  /* The avatar crop.
     ──────────────────────────────────────────────────────────────────────
     .dough-avatar clips to a *disc*, so the constraint here is not "does the
     drawing fit the square" — it is "does the drawing fit the circle the
     square inscribes". Two earlier boxes got this wrong: one framed on the
     eyes and muzzle alone, and the replacement (46 120 420 420) still ran the
     ear tips 17 units past each edge, which the disc then cut into a flat
     vertical shave. Dough's silhouette is ear-to-ear, so cropping the ears is
     the one thing that stops reading as a dog at 25–30px.

     Measured from the paths themselves — ears, crown, muzzle, paws, every
     stroke width, the baked -5° tilt poses, and the idle animations at their
     extremes (bounce lifts 5%, the ears twitch ±5°) — the whole mascot is
     contained by a circle of radius 244 centred on (247.8, 330.7). This box
     is that circle with ~5% air, so nothing ever touches the rim:

         centre (248, 330), inscribed radius 256

     Square on purpose: .dough-avatar sizes both axes, so a non-square viewBox
     would squash him. Re-run tools/dough_bbox.py if the artwork changes. */
  var TIGHT = '-8 74 512 512';

  function svg(name, opts) {
    opts = opts || {};
    var ex = EXPRESSIONS[name] || EXPRESSIONS[DEFAULT];
    var id = 'dg' + (++uid);
    var tight = !!opts.tight;
    var size = opts.size || 64;
    var label = opts.label || '';

    /* A resting tilt is baked in for the poses that have one, for the same
       reason the wave is: without motion, `thinking` must still not look
       identical to `happy`. The animation runs on the inner .dough-head, so
       the two transforms compose. */
    var tilt = ex.tilt || 0;

    var body =
      (tilt ? '<g transform="rotate(' + tilt + ' 256 470)">' : '') +
      '<g class="dough-head">' +
        '<g stroke="' + LINE + '" stroke-width="10" stroke-linejoin="round">' +
          '<g class="dough-ear dough-ear-l"><path d="' + EAR_L + '" fill="url(#' + id + 'e)"/></g>' +
          '<g class="dough-ear dough-ear-r"><path d="' + EAR_R + '" fill="url(#' + id + 'e)"/></g>' +
          '<path d="' + HEAD + '" fill="url(#' + id + 'f)"/>' +
          /* The cheeks carry the reference's whole lower face, so they are
             scaled up around their own centre rather than redrawn. */
          '<path d="' + MUZZLE + '" fill="' + SNOUT + '" ' +
                'transform="translate(256,404) scale(1.1) translate(-256,-404)"/>' +
          paws(ex.wave && !tight) +
        '</g>' +
        TEXTURE + TUFT +
        (ex.brow ? brows(ex.brow) : '') +
        ex.eyes + NOSE + mouth(ex.mouth) +
      '</g>' +
      (tilt ? '</g>' : '');

    var prop = (!tight && ex.prop && PROPS[ex.prop]) ? PROPS[ex.prop] : '';

    /* Width follows the viewBox aspect so a caller only ever sets height. */
    var vb = tight ? TIGHT : FULL;
    var parts = vb.split(' ');
    var w = Math.round(size * (parseFloat(parts[2]) / parseFloat(parts[3])));

    return '<svg class="dough dough-' + name + (tight ? ' dough-tight' : '') +
             (opts.anim === false ? '' : ' dough-anim-' + ex.anim) + '" ' +
           'viewBox="' + vb + '" width="' + w + '" height="' + size + '" ' +
           (label ? 'role="img" aria-label="' + esc(label) + '"' : 'aria-hidden="true" focusable="false"') + '>' +
           (label ? '<title>' + esc(label) + '</title>' : '') +
           defs(id) + prop + body +
           '</svg>';
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* ── Hydration ─────────────────────────────────────────────────────────
     Slots are filled in place. The `doughDone` marker keeps a re-hydration
     (SPA swap, MutationObserver echo) from rebuilding a slot that is already
     drawn, which would restart its animation on every DOM mutation.       */
  function fill(el) {
    var name = el.getAttribute('data-dough') || DEFAULT;
    if (el.dataset.doughDone === name) return;
    el.innerHTML = svg(name, {
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
    svg: svg,
    set: set,
    hydrate: hydrate,
    expressions: Object.keys(EXPRESSIONS)
  };
})(window);
