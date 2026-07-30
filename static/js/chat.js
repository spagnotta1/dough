/* ═════════════════════════════════════════════════════════════════════════
   Ask Dough — chat client.
   ───────────────────────────────────────────────────────────────────────
   Moved out of an inline <script> block in chat.html (Phase 9, Wave 1b) with
   one necessary edit: the model catalogue used to be interpolated by Jinja
   directly into the source. A static .js file is never rendered, so it now
   reads #chat-config — a JSON data island the template writes.

   Wave 1c then changed the markup this file builds, not what it does: chips,
   buttons, tables, empty states and fields are .ds-* components, and the
   expanded-chart view is a native <dialog> rather than a <div> with a
   hand-written focus trap.

   The page runs under SPA navigation, which swaps <main> and re-executes the
   scripts inside it. Two consequences the code below depends on:

     * boot is guarded by root.dataset.booted, so a second execution against
       an already-live DOM returns immediately;
     * window.__spaBeforeLeave is set at the bottom, so leaving the page
       aborts an in-flight stream instead of writing into a detached DOM.
   ═════════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  /* Server-rendered state. The element is written by chat.html; SPA
     navigation preserves it verbatim because its type is not a script type. */
  function readConfig() {
    var node = document.getElementById('chat-config');
    try {
      return JSON.parse(node.textContent);
    } catch (e) {
      return { models: [], default_model: null };
    }
  }

  var root = document.getElementById('chat-root');
  if (!root || root.dataset.booted) return;
  root.dataset.booted = '1';

  /* Let the chat page use the whole viewport. */
  var mainEl = root.closest('main');
  if (mainEl) mainEl.classList.add('is-chat');

  /* ─────────────────────────────────────────────────────────────
     Elements
     ───────────────────────────────────────────────────────────── */
  var el = {
    side:      document.getElementById('side'),
    scrim:     document.getElementById('scrim'),
    sideOpen:  document.getElementById('side-open'),
    sideClose: document.getElementById('side-close'),
    newChat:   document.getElementById('new-chat'),
    newChatTop:document.getElementById('new-chat-top'),
    search:    document.getElementById('conv-search'),
    convList:  document.getElementById('conv-list'),
    modelBtn:  document.getElementById('model-btn'),
    modelName: document.getElementById('model-name'),
    thread:    document.getElementById('thread'),
    turns:     document.getElementById('turns'),
    heroTitle: document.getElementById('hero-title'),
    suggest:   document.getElementById('suggest'),
    input:     document.getElementById('input'),
    send:      document.getElementById('send'),
    jump:      document.getElementById('jump'),
    live:      document.getElementById('live')
  };

  var ICON = {
    send:  '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
    stop:  '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="3"/></svg>',
    copy:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    check: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m4 12 5.5 5.5L20 7"/></svg>',
    redo:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 1 3 6.7"/><path d="M3 20v-5h5"/></svg>',
    edit:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
    trash: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
    pen:   '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
    dots:  '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg>',
    warn:  '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.2v.1"/></svg>',
    grow:  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6M9 21H3v-6M21 3l-7.5 7.5M3 21l7.5-7.5"/></svg>',
    close: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>'
  };

  /* Served from dough/ai/catalog.py via a context processor. This list used to
     be hardcoded here AND in rules.html AND as an allow-set in app.py, and the
     four copies disagreed about both the labels and the default. A test asserts
     no model id is hardcoded in a template. */
  var CFG = readConfig();
  var MODELS = CFG.models.map(function (m) {
    return { id: m.id, name: m.label, desc: m.description };
  });
  var DEFAULT_MODEL = CFG.default_model;

  /* The opening questions Dough offers. Phrased the way a person actually
     asks about their own money — not as feature names — because these chips
     are most users' first sentence to him. */
  var SUGGESTIONS = [
    'Where did my money go?',
    'Am I on track this month?',
    'Can I save more?',
    'How is my portfolio doing?',
    'Help me build a budget.'
  ];

  var LS = { conv: 'check-active-conv', model: 'check-active-model',
             side: 'check-chat-side', msgs: 'check-mc-', list: 'check-cl' };
  var TTL_MSGS = 864e5;   /* 24 h */
  var TTL_LIST = 36e5;    /* 1 h  */

  /* ─────────────────────────────────────────────────────────────
     State
     ───────────────────────────────────────────────────────────── */
  var convs   = [];        /* {id,title,updated_at} */
  var msgs    = [];        /* {role,content,created_at,persisted,error} */
  var convId  = null;
  var model   = localStorage.getItem(LS.model) || DEFAULT_MODEL;
  if (!MODELS.some(function (m) { return m.id === model; })) model = DEFAULT_MODEL;

  var streaming = false;
  var abortCtrl = null;
  var stick     = true;      /* pinned to the bottom of the thread? */
  var filter    = '';
  var openPop   = null;
  var destroyed = false;

  var isTouch = window.matchMedia('(pointer: coarse)').matches;
  var isPhone = function () { return window.innerWidth <= 860; };

  /* ─────────────────────────────────────────────────────────────
     Small helpers
     ───────────────────────────────────────────────────────────── */
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function announce(t) { if (el.live) el.live.textContent = t; }
  function toast(m, kind) {
    if (typeof window.showToast === 'function') window.showToast(m, kind || 'error');
  }
  function store(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  function load(k, ttl) {
    try {
      var raw = localStorage.getItem(k);
      if (!raw) return null;
      var o = JSON.parse(raw);
      if (!o || (ttl && Date.now() - o.ts > ttl)) { localStorage.removeItem(k); return null; }
      return o.v;
    } catch (e) { return null; }
  }
  function cacheMsgs(id, list) {
    if (!id) return;
    store(LS.msgs + id, { ts: Date.now(), v: list.filter(function (m) { return m.persisted; }) });
  }

  async function api(url, opts) {
    var res = await fetch(url, opts);
    if (!res.ok) {
      var detail = '';
      try { detail = (await res.json()).error || ''; } catch (e) {}
      throw new Error(detail || ('Request failed (' + res.status + ')'));
    }
    return res.json();
  }

  /* ═════════════════════════════════════════════════════════════
     MARKDOWN
     A small, streaming-tolerant renderer. Everything is escaped
     before any markup is produced, so partial model output can
     never inject HTML.
     ═════════════════════════════════════════════════════════════ */

  var KEYWORDS = new RegExp('\\b(' + [
    'const','let','var','function','return','if','else','for','while','class','new','import',
    'from','export','default','async','await','try','catch','finally','throw','typeof',
    'instanceof','this','null','true','false','undefined','def','elif','lambda','pass','with',
    'as','in','not','and','or','is','None','True','False','self','print','select','from',
    'where','group','order','by','join','left','inner','on','insert','update','delete','into',
    'values','sum','count','avg','case','when','then','end','public','private','static','void',
    'int','float','string','bool','struct','type','interface','func','package','end'
  ].join('|') + ')\\b', 'g');

  function highlight(code) {
    /* One pass over the raw source; each captured chunk is escaped as it is
       emitted so the tokenizer never sees (or produces) markup. */
    var re = /(\/\*[\s\S]*?\*\/|\/\/[^\n]*|#[^\n]*|--[^\n]*)|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)|(\b\d[\d_]*(?:\.\d+)?(?:e[+-]?\d+)?\b)/gi;
    var out = '', last = 0, m;
    while ((m = re.exec(code)) !== null) {
      out += words(code.slice(last, m.index));
      if (m[1])      out += '<span class="tk-com">' + esc(m[1]) + '</span>';
      else if (m[2]) out += '<span class="tk-str">' + esc(m[2]) + '</span>';
      else           out += '<span class="tk-num">' + esc(m[3]) + '</span>';
      last = m.index + m[0].length;
    }
    out += words(code.slice(last));
    return out;

    function words(chunk) {
      if (!chunk) return '';
      return esc(chunk)
        .replace(KEYWORDS, '<span class="tk-kw">$1</span>')
        .replace(/\b([A-Za-z_]\w*)(?=\s*\()/g, function (whole, name) {
          return name.indexOf('tk-') === 0 ? whole : '<span class="tk-fn">' + name + '</span>';
        });
    }
  }

  /* Lookbehind is avoided on purpose — older iOS WebViews reject it at parse time. */
  var AMOUNT = /(^|[\s(>])(-?\$\s?\d[\d,]*(?:\.\d+)?)/g;

  /* Private-use sentinels. They never appear in model output and survive esc(). */
  var SLOT_A = '\uE000', SLOT_B = '\uE001';
  var SLOT_RE = new RegExp(SLOT_A + '(\\d+)' + SLOT_B, 'g');

  function inline(src) {
    /* Code spans are lifted out first so their contents survive untouched. */
    var spans = [];
    var s = String(src).replace(/`([^`\n]+)`/g, function (_, code) {
      spans.push('<code>' + esc(code) + '</code>');
      return SLOT_A + (spans.length - 1) + SLOT_B;
    });

    s = esc(s);

    /* [label](url) — http(s)/mailto only */
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
      function (_, label, href) {
        return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
      });

    /* Bare URLs that are not already inside an anchor */
    s = s.replace(/(^|[\s(])((?:https?:\/\/)[^\s<)]+[^\s<).,;:!?])/g,
      function (_, pre, url) {
        return pre + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
      });

    s = s.replace(/\*\*\*([^\n*]+)\*\*\*/g, '<strong><em>$1</em></strong>')
         .replace(/\*\*([^\n*]+)\*\*/g, '<strong>$1</strong>')
         .replace(/(^|[\s(])\*([^\n*]+)\*(?=$|[\s).,;:!?])/g, '$1<em>$2</em>')
         .replace(/(^|[\s(])_([^\n_]+)_(?=$|[\s).,;:!?])/g, '$1<em>$2</em>')
         .replace(/~~([^\n]+?)~~/g, '<del>$1</del>');

    /* Money reads better as a value than as prose */
    s = s.replace(AMOUNT, '$1<span class="amt">$2</span>');

    return s.replace(SLOT_RE, function (_, i) { return spans[+i]; });
  }

  function isNumeric(v) {
    return /^[\s(]*[-+$]?[\d,]+(\.\d+)?\s*%?[)\s]*$/.test(v) && /\d/.test(v);
  }

  function splitRow(line) {
    var t = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    return t.split('|').map(function (c) { return c.trim(); });
  }

  function indentOf(line) {
    var m = /^[\t ]*/.exec(line)[0];
    return m.replace(/\t/g, '  ').length;
  }

  function renderBlocks(lines) {
    var out = [], i = 0;

    while (i < lines.length) {
      var line = lines[i];
      var t = line.trim();

      if (!t) { i++; continue; }

      /* Fenced code — an unterminated fence is normal mid-stream */
      var fence = /^(```|~~~)\s*([A-Za-z0-9_+#.-]*)/.exec(t);
      if (fence) {
        var mark = fence[1], lang = fence[2] || '', buf = [];
        i++;
        while (i < lines.length && lines[i].trim().indexOf(mark) !== 0) { buf.push(lines[i]); i++; }
        var closed = i < lines.length;
        i++;

        /* A chart only becomes a figure once its fence closes — half-arrived
           JSON is never parsed, so the stream shows a placeholder instead of
           flickering through broken charts. */
        if (lang.toLowerCase() === 'chart') {
          out.push(closed
            ? '<div class="chart-slot" data-spec="' + esc(buf.join('\n')) + '"></div>'
            : '<div class="chart-pending"><span class="orb"></span>' +
              '<span class="thinking-label">Building chart…</span></div>');
          continue;
        }

        out.push(
          '<div class="cb"><div class="cb-top"><span class="cb-lang">' + esc(lang || 'code') + '</span>' +
          '<button class="act" type="button" data-act="copy-code">' + ICON.copy + 'Copy</button></div>' +
          '<pre><code>' + highlight(buf.join('\n')) + '</code></pre></div>');
        continue;
      }

      /* Heading */
      var h = /^(#{1,6})\s+(.*)$/.exec(t);
      if (h) {
        var lvl = Math.min(h[1].length, 4);
        out.push('<h' + lvl + '>' + inline(h[2]) + '</h' + lvl + '>');
        i++;
        continue;
      }

      /* Rule */
      if (/^([-*_])\s*(\1\s*){2,}$/.test(t)) { out.push('<hr>'); i++; continue; }

      /* Table: header row followed by a |---|---| separator */
      if (t.indexOf('|') === 0 && i + 1 < lines.length &&
          /^\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1].trim()) &&
          lines[i + 1].indexOf('-') > -1) {
        var head = splitRow(t);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].trim().indexOf('|') === 0) {
          rows.push(splitRow(lines[i])); i++;
        }
        var numeric = head.map(function (_, c) {
          var vals = rows.map(function (r) { return r[c] || ''; }).filter(Boolean);
          return vals.length > 0 && vals.every(isNumeric);
        });
        var html = '<div class="ds-table-wrap ds-scroll"><table class="ds-table"><thead><tr>' +
          head.map(function (c, ci) {
            return '<th' + (numeric[ci] ? ' class="num"' : '') + '>' + inline(c) + '</th>';
          }).join('') + '</tr></thead><tbody>' +
          rows.map(function (r) {
            return '<tr>' + head.map(function (_, ci) {
              return '<td' + (numeric[ci] ? ' class="num"' : '') + '>' + inline(r[ci] || '') + '</td>';
            }).join('') + '</tr>';
          }).join('') + '</tbody></table></div>';
        out.push(html);
        continue;
      }

      /* Blockquote */
      if (/^>\s?/.test(t)) {
        var q = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          q.push(lines[i].replace(/^\s*>\s?/, '')); i++;
        }
        out.push('<blockquote>' + renderBlocks(q) + '</blockquote>');
        continue;
      }

      /* Lists (nested by indentation) */
      var bullet = /^([-*+]|\d+[.)])\s+/.exec(t);
      if (bullet) {
        var ordered = /\d/.test(bullet[1]);
        var base = indentOf(line);
        var items = [];
        while (i < lines.length) {
          var cur = lines[i];
          if (!cur.trim()) {
            /* After a blank line the list only continues for a deeper block or
               another item at this level. Anything else — a paragraph, a quote,
               a table — starts a new block instead of being absorbed. */
            var nxt = lines[i + 1];
            if (!nxt || !nxt.trim()) break;
            var nInd = indentOf(nxt);
            var nIsItem = /^([-*+]|\d+[.)])\s+/.test(nxt.trim());
            if (!(nInd > base || (nInd === base && nIsItem))) break;
            i++;
            continue;
          }
          var ind = indentOf(cur);
          if (ind < base) break;
          var mk = /^([-*+]|\d+[.)])\s+(.*)$/.exec(cur.trim());
          if (ind === base && mk) {
            if (/\d/.test(mk[1]) !== ordered) break;
            items.push([mk[2]]);
            i++;
          } else if (items.length) {
            items[items.length - 1].push(cur.slice(Math.min(ind, base + 2)));
            i++;
          } else break;
        }
        var tag = ordered ? 'ol' : 'ul';
        out.push('<' + tag + '>' + items.map(function (parts) {
          var first = parts[0];
          var task = /^\[([ xX])\]\s+(.*)$/.exec(first);
          var head2 = task
            ? '<input type="checkbox" disabled' + (task[1] === ' ' ? '' : ' checked') + '> ' + inline(task[2])
            : inline(first);
          var rest = parts.slice(1);
          return '<li>' + head2 + (rest.length ? renderBlocks(rest) : '') + '</li>';
        }).join('') + '</' + tag + '>');
        continue;
      }

      /* Paragraph — always consumes at least the current line so the
         tokenizer can never stall on an unrecognised construct. */
      var para = [t];
      i++;
      while (i < lines.length) {
        var p = lines[i].trim();
        if (!p) break;
        if (/^(#{1,6}\s|>\s?|```|~~~)/.test(p)) break;
        if (/^([-*+]|\d+[.)])\s+/.test(p)) break;
        if (p.indexOf('|') === 0) break;
        para.push(p);
        i++;
      }
      out.push('<p>' + inline(para.join(' ')) + '</p>');
    }

    return out.join('');
  }

  function md(text) {
    if (!text) return '';
    return renderBlocks(String(text).replace(/\r\n?/g, '\n').split('\n'));
  }

  /* ═════════════════════════════════════════════════════════════
     CHARTS

     The model supplies only a type and the numbers. Every colour
     decision is made here, so the palette rules hold no matter what
     the model emits.

     Palettes are the validated eight-slot categorical set, stepped
     separately for light and dark surfaces; the mode is chosen from
     the active theme's own background rather than the OS setting,
     because this app themes itself.
     ═════════════════════════════════════════════════════════════ */
  var PALETTE = {
    light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'],
    dark:  ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']
  };
  /* Diverging poles: warm/cool so they read as opposites, neutral grey at zero.
     Which sign gets which pole is semantic, not cosmetic — see `positive`. */
  var DIVERGE = {
    light: { warm: '#e34948', cool: '#2a78d6', mid: '#f0efec' },
    dark:  { warm: '#e66767', cool: '#3987e5', mid: '#383835' }
  };
  var CHART_TYPES = { bar: 1, grouped_bar: 1, line: 1, stacked_bar: 1, donut: 1, diverging_bar: 1 };
  var MAX_SERIES = 6, MAX_POINTS = 24, MAX_DONUT = 6;

  /* Read the live theme rather than the OS preference. */
  function chartEnv() {
    var cs = getComputedStyle(root);
    var surface = cs.backgroundColor || '#ffffff';
    var probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;visibility:hidden;color:var(--fg)';
    root.appendChild(probe);
    var ink = getComputedStyle(probe).color;
    probe.remove();

    var m = /(\d+(?:\.\d+)?)[,\s]+(\d+(?:\.\d+)?)[,\s]+(\d+(?:\.\d+)?)/.exec(surface);
    var lum = 1;
    if (m) {
      var c = [+m[1], +m[2], +m[3]].map(function (v) {
        v /= 255;
        return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
      });
      lum = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    }
    var mode = lum < 0.2 ? 'dark' : 'light';
    return {
      mode: mode,
      series: PALETTE[mode],
      diverge: DIVERGE[mode],
      surface: surface,
      ink: ink,
      muted: mixInk(cs, 0.62),
      grid: mixInk(cs, 0.12)
    };
  }

  function mixInk(cs, alpha) {
    var fg = cs.getPropertyValue('--fg').trim() || '#888';
    var m = /^#?([\da-f]{6})$/i.exec(fg);
    if (!m) return 'rgba(128,128,128,' + alpha + ')';
    var n = parseInt(m[1], 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + alpha + ')';
  }

  function fmtValue(v, unit) {
    if (typeof v !== 'number' || !isFinite(v)) return '';
    if (unit === 'percent') return (Math.round(v * 10) / 10) + '%';
    if (unit === 'usd') {
      /* Cents are all-or-nothing — "$1,284.5" is not a way money is written. */
      var cents = Math.abs(v % 1) > 1e-9 ? 2 : 0;
      return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US',
        { minimumFractionDigits: cents, maximumFractionDigits: cents });
    }
    return v.toLocaleString('en-US', { maximumFractionDigits: 2 });
  }

  /* Compact axis ticks — $1.2k rather than $1,200 */
  function fmtTick(v, unit) {
    if (unit === 'percent') return v + '%';
    var abs = Math.abs(v), s;
    if (abs >= 1e6)      s = (v / 1e6).toFixed(abs % 1e6 ? 1 : 0) + 'm';
    else if (abs >= 1e3) s = (v / 1e3).toFixed(abs % 1e3 ? 1 : 0) + 'k';
    else                 s = String(Math.round(v * 100) / 100);
    return unit === 'usd' ? s.replace('-', '-$').replace(/^(?!-)/, '$') : s;
  }

  /* Strict allowlist: anything unexpected means no chart, not a broken one. */
  function parseSpec(raw) {
    var spec;
    try { spec = JSON.parse(raw); } catch (e) { return null; }
    if (!spec || typeof spec !== 'object' || Array.isArray(spec)) return null;
    if (!CHART_TYPES[spec.type]) return null;

    var labels = Array.isArray(spec.labels) ? spec.labels : null;
    if (!labels || !labels.length || labels.length > MAX_POINTS) return null;
    labels = labels.map(function (l) { return String(l).slice(0, 40); });

    var series = Array.isArray(spec.series) ? spec.series : null;
    if (!series || !series.length || series.length > MAX_SERIES) return null;

    var clean = [];
    for (var i = 0; i < series.length; i++) {
      var s = series[i];
      if (!s || !Array.isArray(s.data) || s.data.length !== labels.length) return null;
      var data = s.data.map(function (v) {
        var n = typeof v === 'number' ? v : parseFloat(v);
        return isFinite(n) ? n : null;
      });
      if (data.every(function (v) { return v === null; })) return null;
      clean.push({ name: String(s.name == null ? 'Series ' + (i + 1) : s.name).slice(0, 40), data: data });
    }

    var single = spec.type === 'donut' || spec.type === 'diverging_bar';
    if (single && clean.length !== 1) clean = clean.slice(0, 1);
    if (spec.type === 'donut' && labels.length > MAX_DONUT) return null;

    return {
      type: spec.type,
      title: String(spec.title || '').slice(0, 120),
      note: String(spec.note || '').slice(0, 200),
      unit: ({ usd: 1, percent: 1, number: 1 })[spec.unit] ? spec.unit : 'number',
      /* Which direction is the unwelcome one. Defaults to "bad" because the
         common diverging case here is variance against a budget or target,
         where positive means overspending. */
      positive: spec.positive === 'good' ? 'good' : 'bad',
      labels: labels,
      series: clean
    };
  }

  /* Long or numerous category names read better down the side than rotated. */
  function wantsHorizontal(spec) {
    if (spec.type !== 'bar' && spec.type !== 'diverging_bar') return false;
    if (spec.series.length > 1) return false;
    return spec.labels.length > 6 ||
           spec.labels.some(function (l) { return l.length > 12; });
  }

  function buildConfig(spec, env, opts) {
    opts = opts || {};
    var horizontal = wantsHorizontal(spec);
    var unit = spec.unit;
    var stacked = spec.type === 'stacked_bar';
    /* Chart.js skips the baseline edge by default, so a plain radius rounds the
       data end only — and stays correct for horizontal and negative bars. */
    var BAR = { borderRadius: 4, maxBarThickness: 44, barPercentage: 0.62, categoryPercentage: 0.8 };

    var datasets;
    if (spec.type === 'donut') {
      datasets = [{
        data: spec.series[0].data,
        backgroundColor: spec.labels.map(function (_, i) { return env.series[i % env.series.length]; }),
        borderColor: env.surface,
        borderWidth: 2,              /* surface gap, not a contrasting outline */
        hoverOffset: 4
      }];
    } else if (spec.type === 'diverging_bar') {
      /* The warm pole marks the unwelcome direction, whichever sign that is. */
      var warmSign = spec.positive === 'good' ? -1 : 1;
      datasets = [Object.assign({
        label: spec.series[0].name,
        data: spec.series[0].data,
        backgroundColor: spec.series[0].data.map(function (v) {
          return (v || 0) * warmSign >= 0 ? env.diverge.warm : env.diverge.cool;
        })
      }, BAR)];
    } else if (spec.type === 'line') {
      datasets = spec.series.map(function (s, i) {
        var c = env.series[i % env.series.length];
        return {
          label: s.name, data: s.data,
          borderColor: c, backgroundColor: c,
          borderWidth: 2, tension: 0.32,
          pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 18,
          pointBackgroundColor: c, pointBorderColor: env.surface, pointBorderWidth: 2,
          /* A lone trend line gets a faint wash; multiples stay unfilled so
             they do not occlude one another. */
          fill: spec.series.length === 1 ? { target: 'origin', above: hexA(c, 0.10) } : false,
          spanGaps: true
        };
      });
    } else {
      /* bar / grouped_bar / stacked_bar — nominal categories with one series all
         take slot 1; bar length already encodes magnitude, so hue is not spent
         on it. Grouped series sit close inside their group and lean on the gap
         between groups to read as a set. */
      var last = spec.series.length - 1;
      if (!stacked && spec.series.length > 1) {
        BAR = Object.assign({}, BAR, { barPercentage: 0.88, categoryPercentage: 0.74 });
      }
      datasets = spec.series.map(function (s, i) {
        return Object.assign({}, BAR, {
          label: s.name, data: s.data,
          backgroundColor: env.series[i % env.series.length],
          /* Only the outermost stacked segment carries the rounded end. */
          borderRadius: stacked ? (i === last ? 4 : 0) : 4,
          /* A surface-coloured seam, never a contrasting outline. */
          borderColor: env.surface,
          borderWidth: stacked ? 2 : 0
        });
      });
    }

    var valueAxis = {
      stacked: stacked,
      beginAtZero: true,
      border: { display: false },
      grid: { color: env.grid, drawTicks: false, lineWidth: 1 },
      ticks: {
        color: env.muted, padding: 8, maxTicksLimit: 5,
        font: { size: 11, family: getComputedStyle(root).getPropertyValue('--ui') },
        callback: function (v) { return fmtTick(v, unit); }
      }
    };
    var catAxis = {
      stacked: stacked,
      border: { display: false },
      grid: { display: false },
      ticks: {
        color: env.muted, padding: 6, autoSkip: true, maxRotation: 0,
        font: { size: 11, family: getComputedStyle(root).getPropertyValue('--ui') }
      }
    };

    var scales = spec.type === 'donut' ? {}
      : horizontal ? { x: valueAxis, y: catAxis } : { x: catAxis, y: valueAxis };

    return {
      type: spec.type === 'donut' ? 'doughnut' : (spec.type === 'line' ? 'line' : 'bar'),
      data: { labels: spec.labels, datasets: datasets },
      plugins: window.ChartValueLabels ? [window.ChartValueLabels.plugin] : [],
      options: {
        indexAxis: horizontal ? 'y' : 'x',
        responsive: true,
        maintainAspectRatio: false,
        animation: prefersReducedMotion() ? false : { duration: 420 },
        layout: { padding: figPadding(spec, horizontal, !!opts.values) },
        cutout: spec.type === 'donut' ? '62%' : undefined,
        interaction: { mode: spec.type === 'line' ? 'index' : 'nearest', intersect: false },
        scales: scales,
        plugins: {
          valueLabels: {
            enabled: !!opts.values,
            format: unit,
            ink: env.ink, size: opts.large ? 12 : 11,
            family: getComputedStyle(root).getPropertyValue('--ui')
          },
          /* One series is named by the title; a legend box would be noise. */
          legend: (spec.series.length > 1 || spec.type === 'donut') ? {
            display: true, position: 'bottom', align: 'start',
            labels: {
              color: env.muted, boxWidth: 10, boxHeight: 10,
              usePointStyle: true, pointStyle: 'circle', padding: 14,
              font: { size: 11.5 }
            }
          } : { display: false },
          tooltip: {
            backgroundColor: env.mode === 'dark' ? 'rgba(20,20,20,.94)' : 'rgba(255,255,255,.97)',
            titleColor: env.ink, bodyColor: env.ink,
            borderColor: env.grid, borderWidth: 1,
            padding: 10, cornerRadius: 10, displayColors: true,
            usePointStyle: true, boxPadding: 4,
            titleFont: { size: 12, weight: '600' }, bodyFont: { size: 12 },
            callbacks: {
              label: function (c) {
                var name = spec.type === 'donut' ? c.label : (c.dataset.label || '');
                var v = fmtValue(spec.type === 'donut' ? c.parsed : c.parsed[horizontal ? 'x' : 'y'], unit);
                return name ? name + ': ' + v : v;
              }
            }
          }
        }
      }
    };
  }

  /* Room for the labels, from the shared module so their geometry has one
     owner. `spec` is read before a chart instance exists to inspect. */
  function figPadding(spec, horizontal, on) {
    if (!window.ChartValueLabels) return { top: 4 };
    return window.ChartValueLabels.pad({
      enabled: on,
      base: { top: 4 },
      donut: spec.type === 'donut',
      horizontal: horizontal,
      negatives: spec.series.some(function (s) {
        return s.data.some(function (v) { return (v || 0) < 0; });
      })
    });
  }

  function hexA(hex, a) {
    var m = /^#?([\da-f]{6})$/i.exec(hex);
    if (!m) return hex;
    var n = parseInt(m[1], 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /* The table is both the accessible alternative and the contrast relief for
     the lighter palette slots — it ships with every figure, not on request. */
  function dataTable(spec) {
    var head = '<tr><th>' + esc(spec.type === 'donut' ? 'Segment' : 'Label') + '</th>' +
      spec.series.map(function (s) { return '<th class="num">' + esc(s.name) + '</th>'; }).join('') + '</tr>';
    var rows = spec.labels.map(function (l, i) {
      return '<tr><td>' + esc(l) + '</td>' + spec.series.map(function (s) {
        return '<td class="num">' + esc(fmtValue(s.data[i], spec.unit)) + '</td>';
      }).join('') + '</tr>';
    }).join('');
    return '<div class="ds-table-wrap ds-scroll"><table class="ds-table"><thead>' + head +
           '</thead><tbody>' + rows + '</tbody></table></div>';
  }

  function describe(spec) {
    var parts = [spec.type.replace('_', ' ') + ' chart'];
    if (spec.title) parts.push('titled ' + spec.title);
    parts.push('with ' + spec.labels.length + ' ' + (spec.labels.length === 1 ? 'category' : 'categories'));
    if (spec.series.length > 1) parts.push('across ' + spec.series.length + ' series');
    return parts.join(', ') + '. The figures are listed in the accompanying data table.';
  }

  function destroyCharts(scope) {
    if (!scope || !window.Chart || !Chart.getChart) return;
    scope.querySelectorAll('canvas.fig-canvas').forEach(function (c) {
      var inst = Chart.getChart(c);
      if (inst) inst.destroy();
    });
  }

  /* The figure element itself — markup only, no chart and no listeners, so the
     inline figure and the expanded one are built from one description.
     `opts.large` drops the expand control (you are already there). */
  function buildFigure(spec, opts) {
    var fig = document.createElement('figure');
    fig.className = 'figure' + (opts.large ? ' fig-lg' : '');
    var id = 'fig-' + Math.random().toString(36).slice(2, 9);
    var on = !!opts.values;
    fig.innerHTML =
      (spec.title ? '<figcaption class="fig-title">' + esc(spec.title) + '</figcaption>' : '') +
      (spec.note ? '<p class="fig-sub">' + esc(spec.note) + '</p>' : '') +
      '<div class="fig-plot"><canvas class="fig-canvas" role="img" aria-label="' +
        esc(describe(spec)) + '"></canvas></div>' +
      '<div class="fig-foot">' +
        '<button class="ds-chip fig-toggle" type="button" data-fig="table" aria-expanded="false" ' +
          'aria-controls="' + id + '">Show data</button>' +
        '<button class="ds-chip fig-toggle" type="button" data-fig="values" aria-pressed="' +
          (on ? 'true' : 'false') + '">' + (on ? 'Hide values' : 'Show values') + '</button>' +
        (opts.large ? '' :
          '<button class="ds-chip fig-toggle fig-expand" type="button" data-fig="expand">' +
          ICON.grow + 'Expand</button>') +
      '</div>' +
      '<div class="fig-data" id="' + id + '" hidden>' + dataTable(spec) + '</div>';
    if (on) fig.dataset.values = '1';
    return fig;
  }

  /* Draw the chart and wire the three controls. The figure must already be in
     the document — the seam colour is read off its own computed background. */
  function mountFigure(fig, spec, env, opts) {
    var table = fig.querySelector('.fig-data');
    var tableBtn = fig.querySelector('[data-fig="table"]');
    var valuesBtn = fig.querySelector('[data-fig="values"]');
    var expandBtn = fig.querySelector('[data-fig="expand"]');

    tableBtn.addEventListener('click', function () {
      var open = table.hasAttribute('hidden');
      if (open) table.removeAttribute('hidden'); else table.setAttribute('hidden', '');
      tableBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      tableBtn.textContent = open ? 'Hide data' : 'Show data';
    });

    function tableOnly() {
      /* No canvas — the table is the whole story, so just open it. */
      var plot = fig.querySelector('.fig-plot');
      if (plot) plot.remove();
      table.removeAttribute('hidden');
      fig.querySelector('.fig-foot').remove();
    }

    if (!window.Chart) { tableOnly(); return; }

    var chart;
    try {
      /* Seams and point rings must match the card the chart sits on, not the
         page behind it, or they read as white hairlines. */
      var figEnv = Object.assign({}, env, {
        surface: getComputedStyle(fig).backgroundColor || env.surface
      });
      chart = new Chart(fig.querySelector('canvas'), buildConfig(spec, figEnv, opts));
    } catch (e) {
      tableOnly();
      return;
    }

    /* No label module (the one script that could 404) — no control for it. */
    if (!window.ChartValueLabels) valuesBtn.remove();
    else valuesBtn.addEventListener('click', function () {
      var on = valuesBtn.getAttribute('aria-pressed') !== 'true';
      chart.options.plugins.valueLabels.enabled = on;
      chart.options.layout.padding = figPadding(spec, wantsHorizontal(spec), on);
      chart.update();
      valuesBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      valuesBtn.textContent = on ? 'Hide values' : 'Show values';
      fig.dataset.values = on ? '1' : '';
    });

    if (expandBtn) {
      expandBtn.addEventListener('click', function () { openFigure(spec, fig, expandBtn); });
    }
  }

  /* Turn every settled .chart-slot inside `scope` into a real figure. */
  function mountCharts(scope) {
    if (!scope) return;
    var slots = scope.querySelectorAll('.chart-slot');
    if (!slots.length) return;
    var env = chartEnv();

    slots.forEach(function (slot) {
      var raw = slot.getAttribute('data-spec') || '';
      var spec = parseSpec(raw);

      if (!spec) {
        /* Malformed spec: show the numbers we can, never a broken canvas. */
        var dead = document.createElement('figure');
        dead.className = 'figure';
        dead.innerHTML = '<p class="fig-fallback">That chart could not be drawn.</p>';
        slot.replaceWith(dead);
        return;
      }

      var opts = { values: slot.dataset.values === '1' };
      var fig = buildFigure(spec, opts);
      fig.dataset.spec = raw;
      slot.replaceWith(fig);
      mountFigure(fig, spec, env, opts);
    });
  }

  /* ── Expanded view ── */
  var figModal = null;

  function openFigure(spec, source, opener) {
    closeFigure();

    /* A native <dialog>. This used to be a fixed <div role="dialog"> with a
       hand-written Tab trap and its own backdrop; showModal() gives the trap,
       Esc, the backdrop and inertness of the conversation behind for free —
       and gets them right for screen readers, which the div never did.
       Appended inside #chat-root so the theme tokens still resolve. */
    var dlg = document.createElement('dialog');
    dlg.id = 'fig-modal';
    dlg.className = 'ds-dialog ds-dialog--wide';
    dlg.setAttribute('aria-label', spec.title || 'Expanded chart');
    dlg.innerHTML = '<div class="ds-dialog__panel ds-dialog__panel--flush">' +
      '<button class="ds-btn ds-btn--icon ds-dialog__close fig-close" type="button" ' +
      'aria-label="Close expanded chart">' + ICON.close + '</button></div>';

    var card = dlg.querySelector('.ds-dialog__panel');
    var opts = { large: true, values: source && source.dataset.values === '1' };
    var fig = buildFigure(spec, opts);
    card.appendChild(fig);

    root.appendChild(dlg);
    figModal = { dlg: dlg, card: card, spec: spec, opener: opener || null };
    dlg.showModal();
    /* After showModal(): the canvas has no layout until the dialog is in the
       top layer, and the chart sizes itself from the box it is drawn into. */
    mountFigure(fig, spec, chartEnv(), opts);

    /* The panel is a child, so a click landing on the dialog itself is a click
       on the backdrop. */
    dlg.addEventListener('mousedown', function (e) { if (e.target === dlg) dlg.close(); });
    dlg.querySelector('.fig-close').addEventListener('click', function () { dlg.close(); });
    /* Esc closes without going through closeFigure(), so teardown hangs off
       the event rather than the button. */
    dlg.addEventListener('close', teardownFigure);

    dlg.querySelector('.fig-close').focus();
  }

  function closeFigure() {
    if (figModal) figModal.dlg.close();   /* fires 'close' → teardownFigure */
  }

  function teardownFigure() {
    if (!figModal) return;
    var dlg = figModal.dlg, opener = figModal.opener;
    figModal = null;
    destroyCharts(dlg);
    dlg.remove();
    /* The thread may have re-rendered underneath us. */
    if (opener && document.contains(opener)) opener.focus();
  }

  /* Chart.js bakes colours in at draw time, so a theme switch needs a rebuild. */
  function repaintCharts() {
    el.turns.querySelectorAll('.figure[data-spec]').forEach(function (fig) {
      var slot = document.createElement('div');
      slot.className = 'chart-slot';
      slot.setAttribute('data-spec', fig.dataset.spec);
      if (fig.dataset.values === '1') slot.dataset.values = '1';
      destroyCharts(fig);
      fig.replaceWith(slot);
    });
    mountCharts(el.turns);

    if (figModal) {
      var old = figModal.card.querySelector('.figure');
      var opts = { large: true, values: old.dataset.values === '1' };
      var next = buildFigure(figModal.spec, opts);
      destroyCharts(old);
      old.replaceWith(next);
      mountFigure(next, figModal.spec, chartEnv(), opts);
    }
  }

  /* ═════════════════════════════════════════════════════════════
     RENDERING
     ═════════════════════════════════════════════════════════════ */

  function timeOfDay() {
    var h = new Date().getHours();
    if (h < 5)  return 'Still up?';
    if (h < 12) return 'Good morning!';
    if (h < 18) return 'Good afternoon!';
    return 'Good evening!';
  }

  /* What Dough says while he works. Varied so a long session does not feel
     like a spinner with a caption, and all describing the same thing he is
     literally doing: reading the user's own data. */
  var THINKING = [
    'Digging through your numbers…',
    'Having a sniff around…',
    'Let me pull that up…',
    'Checking your accounts…',
    'One moment — I’m looking…'
  ];
  function thinkingLine() {
    return THINKING[Math.floor(Math.random() * THINKING.length)];
  }

  function renderSuggestions() {
    el.suggest.innerHTML = SUGGESTIONS.map(function (s) {
      return '<button class="ds-chip" type="button" data-q="' + esc(s) + '">' + esc(s) + '</button>';
    }).join('');
  }

  function syncEmptyState() {
    var empty = msgs.length === 0;
    root.classList.toggle('is-empty', empty);
    if (empty) el.heroTitle.textContent = timeOfDay();
  }

  function actionBtn(act, icon, label) {
    return '<button class="act" type="button" data-act="' + act + '" title="' + label + '">' +
           icon + '<span>' + label + '</span></button>';
  }

  /* Dough's byline. Built from markup rather than Dough.svg() so it does not
     matter whether the deferred mascot script has run yet — the hydrator
     picks the slot up whenever it lands. */
  function doughFrom() {
    var b = document.createElement('div');
    b.className = 'a-from';
    b.innerHTML = '<span class="dough-avatar dough-avatar-xs" aria-hidden="true">' +
                    '<span data-dough="happy" data-dough-size="28" data-dough-tight></span>' +
                  '</span><span>Dough</span>';
    return b;
  }

  function buildTurn(m, index, animate) {
    var wrap = document.createElement('div');
    wrap.className = 'turn ' + m.role + (animate ? ' enter' : '');
    wrap.dataset.i = index;

    var col = document.createElement('div');
    col.className = 'col';
    wrap.appendChild(col);

    if (m.role === 'user') {
      var b = document.createElement('div');
      b.className = 'u-bubble';
      b.textContent = m.content;
      col.appendChild(b);

      var ua = document.createElement('div');
      ua.className = 'acts';
      ua.innerHTML = actionBtn('edit', ICON.edit, 'Edit') + actionBtn('copy', ICON.copy, 'Copy');
      col.appendChild(ua);
      return wrap;
    }

    /* Every answer in this thread is Dough speaking. Saying so once per
       turn is what makes the page feel like a conversation with someone
       rather than a box that emits markdown — and it keeps the attribution
       visible when a user scrolls back into the middle of a long thread. */
    col.appendChild(doughFrom());

    if (m.error) {
      col.innerHTML =
        '<div class="a-from"><span class="dough-avatar dough-avatar-xs" aria-hidden="true">' +
          '<span data-dough="concerned" data-dough-size="28" data-dough-tight></span>' +
        '</span><span>Dough</span></div>' +
        '<div class="ds-card ds-card--danger err" role="alert">' + ICON.warn +
          '<div class="err__msg">' + esc(m.content) + '</div></div>' +
        '<div class="acts pinned">' + actionBtn('retry', ICON.redo, 'Try again') + '</div>';
      return wrap;
    }

    var body = document.createElement('div');
    body.className = 'a-body';
    body.innerHTML = md(m.content);
    mountCharts(body);
    col.appendChild(body);

    var aa = document.createElement('div');
    aa.className = 'acts';
    aa.innerHTML = actionBtn('copy', ICON.copy, 'Copy') +
                   actionBtn('regen', ICON.redo, 'Regenerate');
    col.appendChild(aa);

    clampIfLong(body);
    return wrap;
  }

  /* Only a genuine wall of text gets collapsed — several screens' worth. Below
     that, scrolling is the faster interaction, especially on a phone. */
  function clampIfLong(body) {
    if (body.dataset.clampChecked || body.classList.contains('streaming')) return;
    if (!body.isConnected) return;            /* measure only once it is laid out */
    body.dataset.clampChecked = '1';

    var limit = Math.max(1600, window.innerHeight * 2.4);
    if (body.scrollHeight <= limit) return;

    body.classList.add('clipped');
    body.style.maxHeight = Math.round(window.innerHeight * 1.15) + 'px';

    var btn = document.createElement('button');
    btn.className = 'ds-btn ds-btn--sm ds-btn--secondary expand-btn';
    btn.type = 'button';
    btn.textContent = 'Show the rest';
    btn.addEventListener('click', function () {
      body.classList.remove('clipped');
      body.style.maxHeight = '';
      btn.remove();
    });
    body.after(btn);
  }

  function renderThread(animateLast) {
    destroyCharts(el.turns);          /* Chart.js keeps its own registry */
    el.turns.innerHTML = '';
    var frag = document.createDocumentFragment();
    msgs.forEach(function (m, i) {
      frag.appendChild(buildTurn(m, i, animateLast && i === msgs.length - 1));
    });
    el.turns.appendChild(frag);
    syncEmptyState();
    /* Clamp needs layout, so re-check after paint. */
    requestAnimationFrame(function () {
      el.turns.querySelectorAll('.a-body').forEach(clampIfLong);
    });
  }

  /* ── Conversation list ── */
  function bucket(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return 'Earlier';
    var days = Math.floor((Date.now() - d.getTime()) / 864e5);
    var today = new Date(); today.setHours(0, 0, 0, 0);
    if (d >= today) return 'Today';
    if (d >= new Date(today.getTime() - 864e5)) return 'Yesterday';
    if (days < 7)  return 'Previous 7 days';
    if (days < 30) return 'Previous 30 days';
    return 'Earlier';
  }

  function renderConvs() {
    var list = convs.filter(function (c) {
      return !filter || (c.title || '').toLowerCase().indexOf(filter) > -1;
    });

    if (!list.length) {
      el.convList.innerHTML = '<div class="ds-empty ds-empty--tight"><p class="ds-empty__body">' +
        (filter ? 'No chats match “' + esc(filter) + '”.' : 'Your chats will show up here.') +
        '</p></div>';
      return;
    }

    var html = '', group = null;
    list.forEach(function (c) {
      var g = bucket(c.updated_at);
      if (g !== group) { group = g; html += '<div class="conv-group">' + g + '</div>'; }
      html += '<div class="conv' + (c.id === convId ? ' active' : '') + '" role="listitem" ' +
              'data-id="' + esc(c.id) + '" tabindex="0">' +
              '<span class="conv-title">' + esc(c.title || 'New chat') + '</span>' +
              '<button class="conv-menu-btn" type="button" data-menu="' + esc(c.id) + '" ' +
              'aria-label="Chat options">' + ICON.dots + '</button></div>';
    });
    el.convList.innerHTML = html;
  }

  /* ── Popovers ── */
  function closePop() {
    if (openPop) { openPop.remove(); openPop = null; }
    document.querySelectorAll('.conv.menu-open').forEach(function (n) { n.classList.remove('menu-open'); });
    el.modelBtn.setAttribute('aria-expanded', 'false');
  }

  function showPop(anchor, html, align) {
    closePop();
    var pop = document.createElement('div');
    pop.className = 'pop';
    pop.setAttribute('role', 'menu');
    pop.innerHTML = html;
    /* Mounted inside #chat-root so it inherits the chat's theme tokens. */
    root.appendChild(pop);

    var r = anchor.getBoundingClientRect();
    var w = pop.offsetWidth, h = pop.offsetHeight;
    var left = align === 'right' ? r.right - w : r.left;
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    var top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';

    openPop = pop;
    return pop;
  }

  function openModelPicker() {
    if (openPop && openPop.dataset.kind === 'model') { closePop(); return; }
    var html = MODELS.map(function (m) {
      return '<button class="model-opt' + (m.id === model ? ' active' : '') + '" type="button" ' +
             'role="menuitemradio" aria-checked="' + (m.id === model) + '" data-model="' + m.id + '">' +
             '<span class="mo-check">' + ICON.check + '</span>' +
             '<span class="mo-body"><span class="mo-name">' + m.name + '</span>' +
             '<span class="mo-desc">' + m.desc + '</span></span></button>';
    }).join('');
    var pop = showPop(el.modelBtn, html, 'left');
    pop.dataset.kind = 'model';
    pop.style.maxWidth = '310px';
    el.modelBtn.setAttribute('aria-expanded', 'true');
    pop.addEventListener('click', function (e) {
      var b = e.target.closest('[data-model]');
      if (!b) return;
      setModel(b.dataset.model);
      closePop();
    });
  }

  function setModel(id) {
    model = id;
    localStorage.setItem(LS.model, id);
    var m = MODELS.find(function (x) { return x.id === id; }) || MODELS[1];
    el.modelName.textContent = m.name;
  }

  /* ═════════════════════════════════════════════════════════════
     SIDEBAR
     ═════════════════════════════════════════════════════════════ */
  function setSidebar(open) {
    /* Visibility (and the phone-only scrim) is handled entirely in CSS. */
    root.classList.toggle('side-hidden', !open);
    if (!isPhone()) localStorage.setItem(LS.side, open ? '1' : '0');
    /* Focus belongs in the composer — only the search shortcut moves it. */
  }
  function sidebarOpen() { return !root.classList.contains('side-hidden'); }

  /* ═════════════════════════════════════════════════════════════
     SCROLLING
     ═════════════════════════════════════════════════════════════ */
  function atBottom() {
    return el.thread.scrollHeight - el.thread.scrollTop - el.thread.clientHeight < 80;
  }
  function toBottom(smooth) {
    if (smooth === false) {
      var prev = el.thread.style.scrollBehavior;
      el.thread.style.scrollBehavior = 'auto';
      el.thread.scrollTop = el.thread.scrollHeight;
      el.thread.style.scrollBehavior = prev;
    } else {
      el.thread.scrollTop = el.thread.scrollHeight;
    }
    stick = true;
    el.jump.classList.remove('show');
  }

  el.thread.addEventListener('scroll', function () {
    stick = atBottom();
    el.jump.classList.toggle('show', !stick && msgs.length > 0);
  }, { passive: true });

  el.jump.addEventListener('click', function () { toBottom(true); });

  /* ═════════════════════════════════════════════════════════════
     COMPOSER
     ═════════════════════════════════════════════════════════════ */
  function autoGrow() {
    /* An empty composer owns no height override: clear it instead of
       measuring. Measuring an empty textarea can only ever reproduce its
       CSS height — except when the measurement is taken in a state the
       user never sees (mid-swap, mid-transition, a placeholder quirk), and
       one bad reading here used to be PERMANENT: nothing re-measures until
       the next keystroke, so a composer once pinned at the 42vh cap stayed
       a third of the screen tall with nothing in it. Clearing makes every
       call — boot, resize, submit — heal that state instead of trusting
       whatever the engine reports at that instant. */
    if (!el.input.value) { el.input.style.height = ''; return; }
    el.input.style.height = 'auto';
    el.input.style.height = Math.min(el.input.scrollHeight, window.innerHeight * 0.42) + 'px';
  }

  function refreshSend() {
    if (streaming) {
      el.send.innerHTML = ICON.stop;
      el.send.disabled = false;
      el.send.classList.add('stop');
      el.send.title = 'Stop';
      el.send.setAttribute('aria-label', 'Stop generating');
    } else {
      el.send.innerHTML = ICON.send;
      el.send.disabled = el.input.value.trim().length === 0;
      el.send.classList.remove('stop');
      el.send.title = 'Send';
      el.send.setAttribute('aria-label', 'Send message');
    }
  }

  el.input.addEventListener('input', function () { autoGrow(); refreshSend(); });

  el.input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
      return;
    }
    /* Empty composer + Up = edit the last thing you said (ChatGPT / shell habit) */
    if (e.key === 'ArrowUp' && !el.input.value && !streaming) {
      for (var i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === 'user') { e.preventDefault(); startEdit(i); break; }
      }
    }
  });

  /* Tapping send must not blur the field — the keyboard stays up on iOS */
  el.send.addEventListener('mousedown', function (e) { e.preventDefault(); });
  el.send.addEventListener('click', function () {
    if (streaming) { stop(); return; }
    submit();
  });

  function submit() {
    var text = el.input.value.trim();
    if (!text || streaming) return;
    el.input.value = '';
    autoGrow();
    refreshSend();
    sendMessage(text);
  }

  /* ═════════════════════════════════════════════════════════════
     CONVERSATION DATA
     ═════════════════════════════════════════════════════════════ */
  async function loadConvs(useCache) {
    if (useCache) {
      var cached = load(LS.list, TTL_LIST);
      if (cached && cached.length) { convs = cached; renderConvs(); }
    }
    try {
      var data = await api('/api/conversations');
      convs = data.conversations || [];
      store(LS.list, { ts: Date.now(), v: convs });
      renderConvs();
    } catch (e) { /* cached list stays on screen */ }
    return convs;
  }

  async function loadHistory(id) {
    var cached = load(LS.msgs + id, TTL_MSGS);
    if (cached) {
      msgs = cached.map(function (m) { m.persisted = true; return m; });
      renderThread(false);
      toBottom(false);
    }
    try {
      var data = await api('/api/chat_history?conv=' + encodeURIComponent(id));
      if (id !== convId || destroyed) return;
      var fresh = (data.messages || []).map(function (m) {
        return { role: m.role, content: m.content, created_at: m.created_at, persisted: true };
      });
      if (JSON.stringify(fresh) !== JSON.stringify(cached || [])) {
        msgs = fresh;
        renderThread(false);
        toBottom(false);
      }
      cacheMsgs(id, msgs);
    } catch (e) {
      if (!cached) {
        msgs = [];
        renderThread(false);
        toast('Could not load this conversation.');
      }
    }
  }

  async function selectConv(id, opts) {
    if (id === convId && !(opts && opts.force)) return;
    if (streaming) stop();
    convId = id;
    localStorage.setItem(LS.conv, id);
    msgs = [];
    renderThread(false);
    renderConvs();
    if (isPhone()) setSidebar(false);
    await loadHistory(id);
    if (!isTouch) el.input.focus();
  }

  async function newConv() {
    if (streaming) stop();
    try {
      var c = await api('/api/conversations', { method: 'POST' });
      convs.unshift({ id: c.id, title: c.title || 'New chat', updated_at: new Date().toISOString() });
      store(LS.list, { ts: Date.now(), v: convs });
      convId = c.id;
      localStorage.setItem(LS.conv, c.id);
      msgs = [];
      renderThread(false);
      renderConvs();
      if (isPhone()) setSidebar(false);
      if (!isTouch) el.input.focus();
    } catch (e) {
      toast('Could not start a new chat.');
    }
  }

  async function deleteConv(id) {
    try { await fetch('/api/conversations/' + encodeURIComponent(id), { method: 'DELETE' }); } catch (e) {}
    try { localStorage.removeItem(LS.msgs + id); } catch (e) {}
    convs = convs.filter(function (c) { return c.id !== id; });
    store(LS.list, { ts: Date.now(), v: convs });
    if (id === convId) {
      if (convs.length) { convId = null; await selectConv(convs[0].id); }
      else await newConv();
    } else {
      renderConvs();
    }
  }

  async function renameConv(id, title) {
    var c = convs.find(function (x) { return x.id === id; });
    if (c) { c.title = title || 'New chat'; store(LS.list, { ts: Date.now(), v: convs }); }
    renderConvs();
    try {
      await api('/api/conversations/' + encodeURIComponent(id), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title })
      });
    } catch (e) { toast('Could not rename that chat.'); }
  }

  function beginRename(node, id) {
    var titleEl = node.querySelector('.conv-title');
    if (!titleEl) return;
    var current = titleEl.textContent;
    var input = document.createElement('input');
    input.className = 'ds-input conv-rename';
    input.value = current;
    input.setAttribute('aria-label', 'Chat name');
    titleEl.replaceWith(input);
    input.focus();
    input.select();

    var done = false;
    function commit(save) {
      if (done) return;
      done = true;
      var val = input.value.trim();
      if (save && val && val !== current) renameConv(id, val);
      else renderConvs();
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); commit(true); }
      if (e.key === 'Escape') { e.preventDefault(); commit(false); }
    });
    input.addEventListener('blur', function () { commit(true); });
    input.addEventListener('click', function (e) { e.stopPropagation(); });
  }

  /* ═════════════════════════════════════════════════════════════
     STREAMING
     ═════════════════════════════════════════════════════════════ */
  /* How many messages the server holds ahead of a given local index. */
  function persistedBefore(index) {
    return msgs.slice(0, index).filter(function (m) { return m.persisted; }).length;
  }

  function stop() {
    if (abortCtrl) { try { abortCtrl.abort(); } catch (e) {} }
  }

  async function truncate(keep) {
    await api('/api/chat_truncate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conv_id: convId, keep: keep })
    });
  }

  /* Send a new user message. */
  function sendMessage(text) {
    msgs.push({ role: 'user', content: text, created_at: new Date().toISOString(), persisted: true });
    var turn = buildTurn(msgs[msgs.length - 1], msgs.length - 1, true);
    el.turns.appendChild(turn);
    syncEmptyState();
    toBottom(false);
    run({ message: text });
  }

  /* Re-answer the last user turn (regenerate / retry). */
  function resend() { run({ resend: true }); }

  async function run(payload) {
    if (streaming) return;
    streaming = true;
    stick = true;
    refreshSend();
    announce('Dough is responding');

    /* If the user switches chats (or leaves) mid-stream, whatever arrives
       belongs to a conversation that is no longer on screen. */
    var runConv = convId;
    function stale() { return destroyed || convId !== runConv; }

    /* Placeholder turn for the incoming answer */
    var turn = document.createElement('div');
    turn.className = 'turn assistant enter';
    turn.innerHTML =
      '<div class="col">' +
        '<div class="a-from"><span class="dough-avatar dough-avatar-xs" aria-hidden="true">' +
          '<span data-dough="happy" data-dough-size="28" data-dough-tight></span>' +
        '</span><span>Dough</span></div>' +
        '<div class="thinking">' +
          '<span class="think-dough" data-dough="thinking" data-dough-size="36"></span>' +
          '<span class="thinking-label">' + thinkingLine() + '</span>' +
        '</div>' +
      '</div>';
    el.turns.appendChild(turn);
    syncEmptyState();
    toBottom(false);

    var body = null;          /* becomes the .a-body once the first token lands */
    var acc = '';
    var pending = false;
    var lastPaint = 0;

    function paint(force) {
      if (!body || stale()) return;
      var now = performance.now();
      if (!force && now - lastPaint < 55) {
        if (!pending) {
          pending = true;
          requestAnimationFrame(function () { pending = false; paint(false); });
        }
        return;
      }
      lastPaint = now;
      body.innerHTML = md(acc);
      body.classList.toggle('is-blank', acc.length === 0);
      if (stick) toBottom(false);
    }

    function firstToken() {
      turn.innerHTML = '<div class="col"><div class="a-body streaming"></div></div>';
      body = turn.querySelector('.a-body');
    }

    function fail(message) {
      turn.remove();
      if (stale()) return;
      msgs.push({ role: 'assistant', content: message, created_at: new Date().toISOString(),
                  persisted: false, error: true });
      el.turns.appendChild(buildTurn(msgs[msgs.length - 1], msgs.length - 1, true));
      if (stick) toBottom(false);
      announce('Dough could not respond.');
    }

    /* Swap the live streaming node for a settled turn with its actions. */
    function commit() {
      turn.remove();
      if (stale()) return;
      msgs.push({ role: 'assistant', content: acc, created_at: new Date().toISOString(), persisted: true });
      el.turns.appendChild(buildTurn(msgs[msgs.length - 1], msgs.length - 1, false));
      cacheMsgs(runConv, msgs);
      if (stick) toBottom(false);
    }

    abortCtrl = new AbortController();
    var req = Object.assign({ conv_id: convId, model: model }, payload);

    try {
      var res = await fetch('/api/chat_stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
        signal: abortCtrl.signal
      });

      if (!res.ok) {
        var msg = 'Something went wrong. Please try again.';
        try { msg = (await res.json()).error || msg; } catch (e) {}
        fail(msg);
        return;
      }

      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      var failed = null;
      var done = false;

      while (!done) {
        var chunk;
        try { chunk = await reader.read(); } catch (e) { break; }
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        var lines = buf.split('\n');
        buf = lines.pop() || '';
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i];
          if (line.indexOf('data: ') !== 0) continue;
          var data = line.slice(6);
          if (data === '[DONE]') { done = true; break; }
          var parsed;
          try { parsed = JSON.parse(data); } catch (e) { continue; }
          if (parsed.error) { failed = parsed.error; done = true; break; }
          if (typeof parsed.delta === 'string') {
            if (!body) firstToken();
            acc += parsed.delta;
            paint(false);
          }
        }
      }

      if (failed && !acc) { fail(failed); return; }

      if (acc) {
        commit();
        announce('Response complete.');
        if (failed) toast(failed, 'warning');
        loadConvs(false);   /* the server may have auto-titled this chat */
      } else if (!failed) {
        fail("I didn't manage to get a reply out. Let's try that again.");
      }
    } catch (err) {
      if (err && err.name === 'AbortError') {
        /* Stop pressed: keep what arrived — the server saves the partial too. */
        if (acc) commit(); else turn.remove();
        announce('Stopped.');
      } else {
        fail('Lost connection to the server. Check your network and try again.');
      }
    } finally {
      streaming = false;
      abortCtrl = null;
      if (!destroyed) {
        refreshSend();
        syncEmptyState();
        requestAnimationFrame(function () {
          if (!destroyed) el.turns.querySelectorAll('.a-body').forEach(clampIfLong);
        });
        if (!isTouch) el.input.focus();
      }
    }
  }

  /* ═════════════════════════════════════════════════════════════
     TURN ACTIONS  (delegated)
     ═════════════════════════════════════════════════════════════ */
  function flashDone(btn, label) {
    var original = btn.innerHTML;
    btn.classList.add('done');
    btn.innerHTML = ICON.check + '<span>' + label + '</span>';
    setTimeout(function () {
      btn.classList.remove('done');
      btn.innerHTML = original;
    }, 1600);
  }

  function copyText(text, btn, label) {
    var ok = function () { flashDone(btn, label || 'Copied'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok, function () { legacyCopy(text, ok); });
    } else {
      legacyCopy(text, ok);
    }
  }
  function legacyCopy(text, ok) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); ok(); } catch (e) { toast('Could not copy.'); }
    ta.remove();
  }

  function startEdit(index) {
    if (streaming) return;
    var turn = el.turns.querySelector('.turn[data-i="' + index + '"]');
    if (!turn) return;
    var col = turn.querySelector('.col');
    var original = msgs[index].content;

    col.innerHTML = '';
    var wrap = document.createElement('div');
    wrap.className = 'ds-control edit-wrap';
    var ta = document.createElement('textarea');
    ta.className = 'ds-control__input';
    ta.value = original;
    ta.rows = 1;
    wrap.appendChild(ta);

    var actions = document.createElement('div');
    actions.className = 'edit-actions';
    actions.innerHTML =
      '<button class="ds-btn ds-btn--sm ds-btn--secondary" type="button" data-edit="cancel">Cancel</button>' +
      '<button class="ds-btn ds-btn--sm ds-btn--primary" type="button" data-edit="save">Send</button>';
    wrap.appendChild(actions);
    col.appendChild(wrap);

    function grow() {
      ta.style.height = 'auto';
      ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * 0.4) + 'px';
    }
    grow();
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    ta.addEventListener('input', grow);
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); save(); }
      if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });

    actions.addEventListener('click', function (e) {
      var b = e.target.closest('[data-edit]');
      if (!b) return;
      if (b.dataset.edit === 'save') save(); else cancel();
    });

    function cancel() { renderThread(false); }

    async function save() {
      var text = ta.value.trim();
      if (!text) return;
      if (text === original) { cancel(); return; }
      try {
        await truncate(index);
      } catch (e) {
        toast('Could not update that message.');
        cancel();
        return;
      }
      msgs = msgs.slice(0, index);
      renderThread(false);
      cacheMsgs(convId, msgs);
      sendMessage(text);
    }
  }

  async function regenerate(index) {
    if (streaming) return;
    try {
      await truncate(persistedBefore(index));
    } catch (e) {
      toast('Could not regenerate that reply.');
      return;
    }
    msgs = msgs.slice(0, index);
    renderThread(false);
    cacheMsgs(convId, msgs);
    resend();
  }

  /* Retry after a failure. The request may have died before or after the
     server stored the prompt, so rewind past it and send it fresh — that is
     correct either way, and never leaves a duplicate behind. */
  async function retryLast() {
    if (streaming) return;
    var u = -1;
    for (var i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') { u = i; break; }
    }
    if (u < 0) return;
    var text = msgs[u].content;
    try {
      await truncate(persistedBefore(u));
    } catch (e) {
      toast('Could not resend that message.');
      return;
    }
    msgs = msgs.slice(0, u);
    renderThread(false);
    cacheMsgs(convId, msgs);
    sendMessage(text);
  }

  el.turns.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var act = btn.dataset.act;

    if (act === 'copy-code') {
      var code = btn.closest('.cb').querySelector('code');
      copyText(code.innerText, btn, 'Copied');
      return;
    }

    var turn = btn.closest('.turn');
    if (!turn) return;
    var index = parseInt(turn.dataset.i, 10);
    var m = msgs[index];
    if (!m) return;

    if (act === 'copy')  copyText(m.content, btn, 'Copied');
    if (act === 'edit')  startEdit(index);
    if (act === 'regen') regenerate(index);
    if (act === 'retry') retryLast();
  });

  /* ═════════════════════════════════════════════════════════════
     WIRING
     ═════════════════════════════════════════════════════════════ */
  el.sideOpen.addEventListener('click', function () { setSidebar(!sidebarOpen()); });
  el.sideClose.addEventListener('click', function () { setSidebar(false); });
  el.scrim.addEventListener('click', function () { setSidebar(false); });
  el.newChat.addEventListener('click', newConv);
  el.newChatTop.addEventListener('click', newConv);
  el.modelBtn.addEventListener('click', function (e) { e.stopPropagation(); openModelPicker(); });

  el.search.addEventListener('input', function () {
    filter = el.search.value.trim().toLowerCase();
    renderConvs();
  });
  el.search.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { el.search.value = ''; filter = ''; renderConvs(); el.search.blur(); }
  });

  el.suggest.addEventListener('click', function (e) {
    var chip = e.target.closest('[data-q]');
    if (!chip || streaming) return;
    sendMessage(chip.dataset.q);
  });

  el.convList.addEventListener('click', function (e) {
    var menuBtn = e.target.closest('[data-menu]');
    if (menuBtn) {
      e.stopPropagation();
      var id = menuBtn.dataset.menu;
      var node = menuBtn.closest('.conv');
      if (openPop && openPop.dataset.kind === 'conv' && openPop.dataset.id === id) { closePop(); return; }
      var pop = showPop(menuBtn, '<button class="pop-item" type="button" data-do="rename">' + ICON.pen +
        'Rename</button><button class="pop-item danger" type="button" data-do="delete">' + ICON.trash +
        'Delete</button>', 'right');
      pop.dataset.kind = 'conv';
      pop.dataset.id = id;
      node.classList.add('menu-open');
      pop.addEventListener('click', function (ev) {
        var item = ev.target.closest('[data-do]');
        if (!item) return;
        var action = item.dataset.do;
        closePop();
        if (action === 'rename') beginRename(node, id);
        if (action === 'delete') {
          if (window.confirm('Delete this chat? This cannot be undone.')) deleteConv(id);
        }
      });
      return;
    }
    var row = e.target.closest('.conv');
    if (row && !e.target.closest('.conv-rename')) selectConv(row.dataset.id);
  });

  el.convList.addEventListener('keydown', function (e) {
    /* Only when the row itself holds focus. The rename field and the options
       button are descendants, and they need Space and Enter for themselves. */
    if (e.target !== e.currentTarget && !e.target.classList.contains('conv')) return;
    var row = e.target.closest('.conv');
    if (!row) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectConv(row.dataset.id); }
  });

  el.convList.addEventListener('dblclick', function (e) {
    var row = e.target.closest('.conv');
    if (row) beginRename(row, row.dataset.id);
  });

  /* Listeners on document/window outlive this page's DOM, so track them and
     detach on SPA teardown — otherwise every visit adds another set. */
  var global = [];
  function on(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    global.push([target, type, fn, opts]);
  }

  on(document, 'click', function (e) {
    if (openPop && !openPop.contains(e.target)) closePop();
  });
  on(window, 'resize', function () { closePop(); autoGrow(); });

  /* Theme switch repaints charts — Chart.js resolves colours at draw time. */
  on(document, 'check:theme-changed', function () { repaintCharts(); });

  /* ── Keyboard shortcuts ── */
  function onKeydown(e) {
    var mod = e.metaKey || e.ctrlKey;

    if (e.key === 'Escape') {
      /* <dialog> closes itself on Esc, but this branch still has to run and
         return: without it the same keypress would fall through and stop the
         stream behind the chart. */
      if (figModal) { closeFigure(); return; }
      if (openPop) { closePop(); return; }
      if (streaming) { stop(); return; }
      if (isPhone() && sidebarOpen()) { setSidebar(false); return; }
    }
    if (!mod) return;

    var k = e.key.toLowerCase();
    if (k === 'b') { e.preventDefault(); setSidebar(!sidebarOpen()); }
    else if (k === 'k') {
      e.preventDefault();
      if (!sidebarOpen()) setSidebar(true);
      setTimeout(function () { el.search.focus(); el.search.select(); }, 40);
    } else if (e.shiftKey && k === 'o') { e.preventDefault(); newConv(); }
  }
  on(document, 'keydown', onKeydown);

  /* ── Touch: swipe in from the left edge opens the drawer, swipe left closes ── */
  (function swipe() {
    var x0 = null, y0 = null, tracking = false;

    on(document, 'touchstart', function (e) {
      tracking = false;
      if (!isPhone() || e.touches.length !== 1) return;
      var t = e.touches[0];
      x0 = t.clientX; y0 = t.clientY;
      tracking = sidebarOpen() ? !!e.target.closest('#side, #scrim') : x0 < 26;
    }, { passive: true });

    on(document, 'touchend', function (e) {
      if (!tracking || x0 === null) return;
      tracking = false;
      var t = e.changedTouches[0];
      var dx = t.clientX - x0, dy = t.clientY - y0;
      if (Math.abs(dx) < 55 || Math.abs(dy) > Math.abs(dx)) return;
      if (dx > 0 && !sidebarOpen()) setSidebar(true);
      if (dx < 0 && sidebarOpen()) setSidebar(false);
    }, { passive: true });
  })();

  /* Dragging the transcript dismisses the keyboard, like a native list */
  if (isTouch) {
    el.thread.addEventListener('touchmove', function () {
      if (document.activeElement === el.input) el.input.blur();
    }, { passive: true });
  }

  /* Keep the composer glued above the on-screen keyboard */
  if (window.visualViewport) {
    on(window.visualViewport, 'resize', function () {
      window.scrollTo(0, 0);
      if (stick) requestAnimationFrame(function () { toBottom(false); });
    });
  }

  /* ═════════════════════════════════════════════════════════════
     SPA teardown
     ═════════════════════════════════════════════════════════════ */
  window.__spaBeforeLeave = function () {
    destroyed = true;
    stop();
    closePop();
    closeFigure();
    destroyCharts(el.turns);
    global.forEach(function (g) { g[0].removeEventListener(g[1], g[2], g[3]); });
    global = [];
    if (mainEl) mainEl.classList.remove('is-chat');
  };

  /* ═════════════════════════════════════════════════════════════
     BOOT
     ═════════════════════════════════════════════════════════════ */
  async function boot() {
    setModel(model);
    renderSuggestions();
    refreshSend();
    autoGrow();
    syncEmptyState();

    var wantSide = !isPhone() && localStorage.getItem(LS.side) !== '0';
    setSidebar(wantSide);

    var saved = localStorage.getItem(LS.conv);
    var cached = load(LS.list, TTL_LIST);
    if (cached && cached.length) {
      convs = cached;
      convId = cached.some(function (c) { return c.id === saved; }) ? saved : cached[0].id;
      renderConvs();
      loadHistory(convId);
    }

    var list = await loadConvs(false);
    if (destroyed) return;

    if (!list.length) { await newConv(); return; }

    var target = list.some(function (c) { return c.id === saved; }) ? saved : list[0].id;
    if (target !== convId) {
      convId = target;
      localStorage.setItem(LS.conv, target);
      renderConvs();
      await loadHistory(target);
    } else {
      renderConvs();
    }
    if (!isTouch) el.input.focus();
  }

  boot();
})();
