/* Energy Planner panel: plan, car and evaluation views. No dependencies; charts are hand-drawn SVG.
   All data-derived text is inserted with textContent (via h()/svg()), never as HTML. */
(() => {
  'use strict';

  // ---------------------------------------------------------------- helpers
  const NS = 'http://www.w3.org/2000/svg';
  const $ = (sel) => document.querySelector(sel);

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    append(el, kids);
    return el;
  }
  function append(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k == null || k === false) continue;
      el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
    }
  }
  function svg(tag, attrs, parent, text) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null) continue;
      if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else el.setAttribute(k, v);
    }
    if (text != null) el.textContent = text;
    if (parent) parent.appendChild(el);
    return el;
  }

  const nfCache = {};
  const nf = (d) => (nfCache[d] ||= new Intl.NumberFormat('da-DK', { minimumFractionDigits: d, maximumFractionDigits: d }));
  const fmt = (v, d = 2) => (v == null || Number.isNaN(+v) ? '–' : nf(d).format(+v));
  const fmtSigned = (v, d = 2) => (v == null ? '–' : (v > 0 ? '+' : v < 0 ? '−' : '') + fmt(Math.abs(v), d));
  const pct = (v, d = 0) => (v == null ? '–' : fmt(v, d) + ' %');
  const hhmm = (t) => t.slice(11, 16);
  const dayKey = (t) => t.slice(0, 10);
  const dayName = (key, opts = { weekday: 'short', day: 'numeric', month: 'short' }) =>
    new Date(key + 'T12:00:00').toLocaleDateString('da-DK', opts);
  const todayKey = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  };
  function relDay(key) {
    const t = new Date(todayKey() + 'T12:00:00');
    const d = new Date(key + 'T12:00:00');
    const diff = Math.round((d - t) / 864e5);
    if (diff === 0) return '';
    if (diff === 1) return 'i morgen ';
    if (diff === -1) return 'i går ';
    return dayName(key, { weekday: 'short' }) + ' ';
  }
  const addHour = (t) => {
    const hh = (parseInt(t.slice(11, 13), 10) + 1) % 24;
    return String(hh).padStart(2, '0') + ':00';
  };
  const isOn = (v) => ['enabled', 'enable', 'on', 'true', '1'].includes(String(v ?? '').trim().toLowerCase());

  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    let body = null;
    try { body = await r.json(); } catch (e) { /* empty */ }
    if (!r.ok) throw new Error((body && body.detail) || `${r.status} ${r.statusText}`);
    return body;
  }
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.hidden = true; }, 4000);
  }
  const store = {
    get(k) { try { return localStorage.getItem('ep.' + k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem('ep.' + k, v); } catch (e) { /* private mode */ } },
  };

  // ---------------------------------------------------------------- vocabulary
  const C = {
    car: 'var(--s-car)', sun: 'var(--s-sun)', gridbat: 'var(--s-gridbat)', batt: 'var(--s-batt)',
    export: 'var(--s-export)', hold: 'var(--s-hold)', buy: 'var(--s-price-buy)', sell: 'var(--s-price-sell)',
    ink2: 'var(--ink-2)', muted: 'var(--muted)',
  };
  const MODES = {
    grid_charge: { label: 'Lader fra nettet', color: C.gridbat },
    solar_charge: { label: 'Lader fra sol', color: C.sun },
    discharge: { label: 'Aflader til huset', color: C.batt },
    hold: { label: 'Holder batteriet', color: C.hold },
  };
  const CATS = {
    grid: { label: 'Lad fra nettet', color: C.gridbat },
    battery_first: { label: 'Battery first (sol / hold)', color: C.sun },
    load_first: { label: 'Load first (aflad)', color: C.batt },
  };
  const CAR_ACTIONS = {
    charge: 'Lader', pause: 'Pause', idle: 'Venter', none: 'Ikke tilsluttet',
  };
  const CHARGER_STATUS = {
    available: 'Ledig', preparing: 'Klar (venter på start)', charging: 'Lader', suspended_ev: 'Pause (bil)',
    suspended_evse: 'Pause (lader)', finishing: 'Afslutter', faulted: 'Fejl', unavailable: 'Utilgængelig',
  };

  const icon = {
    warn: () => { const s = svg('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' }); svg('path', { d: 'M12 3 2 21h20L12 3z', style: { fill: 'var(--warning)' } }, s); svg('path', { d: 'M12 10v5M12 17.5v.5', style: { stroke: '#0b0b0b', strokeWidth: 2, strokeLinecap: 'round' } }, s); return s; },
    error: () => { const s = svg('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' }); svg('circle', { cx: 12, cy: 12, r: 10, style: { fill: 'var(--critical)' } }, s); svg('path', { d: 'M12 7v6M12 16.5v.5', style: { stroke: '#fff', strokeWidth: 2, strokeLinecap: 'round' } }, s); return s; },
  };

  // ---------------------------------------------------------------- tooltip
  const tip = {
    el: null,
    show(content, x, y) {
      const el = this.el || (this.el = $('#tooltip'));
      el.replaceChildren(content);
      el.hidden = false;
      const r = el.getBoundingClientRect();
      const vw = window.innerWidth, vh = window.innerHeight;
      let left = x + 16, top = y + 12;
      if (left + r.width > vw - 8) left = x - r.width - 16;
      if (left < 8) left = 8;
      if (top + r.height > vh - 8) top = Math.max(8, vh - r.height - 8);
      el.style.left = left + 'px';
      el.style.top = top + 'px';
    },
    hide() { if (this.el) this.el.hidden = true; },
  };
  const ttRow = (color, label, value, kind = 'line') =>
    h('div', { class: 'tt-row' }, h('span', { class: 'key ' + kind, style: { background: color } }), h('span', { class: 'lbl' }, label), h('b', {}, value));
  const ttSec = (t) => h('div', { class: 'tt-sec' }, t);

  // ---------------------------------------------------------------- chart primitives
  function niceTicks(min, max, count = 4) {
    if (min === max) { max = min + 1; }
    const span = max - min;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || 10 * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
    return { lo, hi, ticks, step };
  }
  function barPath(x, y, w, hgt, roundTop, roundBottom) {
    const r = Math.min(4, w / 2, hgt / 2);
    const rt = roundTop ? r : 0, rb = roundBottom ? r : 0;
    return `M${x},${y + rt}` +
      (rt ? `a${rt},${rt} 0 0 1 ${rt},${-rt}` : '') + `h${w - 2 * rt}` +
      (rt ? `a${rt},${rt} 0 0 1 ${rt},${rt}` : '') + `v${hgt - rt - rb}` +
      (rb ? `a${rb},${rb} 0 0 1 ${-rb},${rb}` : '') + `h${-(w - 2 * rb)}` +
      (rb ? `a${rb},${rb} 0 0 1 ${-rb},${-rb}` : '') + 'z';
  }
  function hatchDefs(root) {
    const defs = svg('defs', {}, root);
    const p = svg('pattern', { id: 'hatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, defs);
    svg('rect', { width: 6, height: 6, style: { fill: 'var(--wash)' } }, p);
    svg('line', { x1: 0, y1: 0, x2: 0, y2: 6, style: { stroke: 'var(--grid)', strokeWidth: 2 } }, p);
  }
  function observe(container, render) {
    let w = 0;
    const ro = new ResizeObserver(() => {
      const nw = Math.round(container.clientWidth);
      if (nw && nw !== w) { w = nw; render(nw); }
    });
    ro.observe(container);
    return ro;
  }

  /* Time chart: vertically stacked panels sharing one hour axis, one crosshair and one tooltip.
     cfg.rows: plan rows; cfg.panels: [{title, height, kind: 'price'|'energy'|'soc', ...}] */
  function timeChart(container, cfg) {
    const rows = cfg.rows;
    const n = rows.length;
    container.replaceChildren();
    if (!n) { container.appendChild(h('div', { class: 'empty' }, 'Ingen plan endnu.')); return; }
    observe(container, (W) => draw(W));

    function draw(W) {
      const narrow = W < 560;
      const ml = narrow ? 34 : 42, mr = cfg.endLabels ? (narrow ? 50 : 64) : 12, top = 46, gap = 46, axisH = 22;
      const pw = W - ml - mr;
      const bw = pw / n;
      const totalH = top + cfg.panels.reduce((a, p) => a + p.height, 0) + gap * (cfg.panels.length - 1) + axisH;
      const root = svg('svg', { viewBox: `0 0 ${W} ${totalH}`, height: totalH, role: 'img', 'aria-label': cfg.label || 'Plan' });
      hatchDefs(root);
      const xAt = (i) => ml + i * bw;
      const dt0 = rows[0].dt ?? 1;
      const xNow = ml + (1 - dt0) * bw;

      // panels
      let y = top;
      const geo = [];
      for (const p of cfg.panels) {
        const g = svg('g', {}, root);
        const info = drawPanel(g, p, y, p.height);
        geo.push({ p, y, h: p.height, ...info });
        y += p.height + gap;
      }
      const yEnd = y - gap;

      // day separators and labels
      rows.forEach((r, i) => {
        if (i > 0 && hhmm(r.t) === '00:00') {
          svg('line', { x1: xAt(i), x2: xAt(i), y1: 2, y2: yEnd, class: 'baseline' }, root);
        }
        if (i === 0 || hhmm(r.t) === '00:00') {
          const opts = narrow ? { weekday: 'short', day: 'numeric' } : { weekday: 'long', day: 'numeric', month: 'short' };
          const label = (i === 0 && !narrow ? 'I dag, ' : '') + dayName(dayKey(r.t), opts);
          const x0 = i === 0 ? Math.max(xAt(0) + 4, xNow + 18) : xAt(i) + 4;
          if (i === 0 || xAt(i) + 50 < W) svg('text', { x: x0, y: 14, class: 'daylabel' }, root, label);
        }
      });

      // x axis
      const step = [1, 2, 3, 4, 6, 12].find((k) => bw * k >= (narrow ? 30 : 34)) || 12;
      rows.forEach((r, i) => {
        const hh = parseInt(r.t.slice(11, 13), 10);
        if (i > 0 && hh % step === 0) {
          svg('text', { x: xAt(i), y: yEnd + 15, class: 'tick', 'text-anchor': 'middle' }, root, String(hh).padStart(2, '0'));
        }
      });

      // now line
      svg('line', { x1: xNow, x2: xNow, y1: 18, y2: yEnd, class: 'nowline' }, root);
      svg('rect', { x: xNow - 12, y: 3, width: 24, height: 15, rx: 7.5, style: { fill: 'var(--ink)' } }, root);
      svg('text', { x: xNow, y: 14, class: 'nowlabel', 'text-anchor': 'middle' }, root, 'nu');

      // hover layer
      const hoverBand = svg('rect', { x: 0, y: top, width: bw, height: yEnd - top, class: 'hoverband', visibility: 'hidden' }, root);
      const cross = svg('line', { x1: 0, x2: 0, y1: top, y2: yEnd, class: 'crosshair', visibility: 'hidden' }, root);
      const overlay = svg('rect', { x: ml, y: top, width: pw, height: yEnd - top, fill: 'transparent', class: 'plot', tabindex: 0, 'aria-label': 'Flyt med piletasterne for at se hver time' }, root);
      let cur = -1;
      const showAt = (i, cx, cy) => {
        cur = Math.max(0, Math.min(n - 1, i));
        hoverBand.setAttribute('x', xAt(cur));
        hoverBand.setAttribute('visibility', 'visible');
        cross.setAttribute('x1', xAt(cur) + bw / 2);
        cross.setAttribute('x2', xAt(cur) + bw / 2);
        cross.setAttribute('visibility', 'visible');
        tip.show(cfg.tooltip(cur), cx, cy);
      };
      const hide = () => { hoverBand.setAttribute('visibility', 'hidden'); cross.setAttribute('visibility', 'hidden'); tip.hide(); };
      overlay.addEventListener('pointermove', (e) => {
        const box = root.getBoundingClientRect();
        const x = (e.clientX - box.left) * (W / box.width);
        showAt(Math.floor((x - ml) / bw), e.clientX, e.clientY);
      });
      overlay.addEventListener('pointerleave', hide);
      overlay.addEventListener('blur', hide);
      overlay.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        const i = cur < 0 ? 0 : cur + (e.key === 'ArrowRight' ? 1 : -1);
        const box = root.getBoundingClientRect();
        showAt(i, box.left + (xAt(Math.max(0, Math.min(n - 1, i))) + bw) * (box.width / W), box.top + 40);
      });

      container.replaceChildren(root);

      // ---- panel renderers
      function drawPanel(g, p, y0, ph) {
        const vals = [], capVals = [];
        if (p.kind === 'price') rows.forEach((r) => p.lines.forEach((l) => vals.push(l.get(r))));
        if (p.kind === 'energy') {
          rows.forEach((r) => {
            vals.push(p.up.filter((s) => !s.capped).reduce((a, s) => a + Math.max(0, s.get(r)), 0));
            capVals.push(p.up.reduce((a, s) => a + Math.max(0, s.get(r)), 0));
            vals.push(-p.down.reduce((a, s) => a + Math.max(0, s.get(r)), 0));
            (p.lines || []).forEach((l) => vals.push(l.get(r)));
            if (p.area) vals.push(p.area.get(r));
          });
        }
        let lo, hi, ticks;
        if (p.kind === 'soc') { lo = 0; hi = 100; ticks = [0, 50, 100]; }
        else {
          const mn = Math.min(0, ...vals.filter(Number.isFinite));
          let mx = Math.max(...vals.filter(Number.isFinite), p.kind === 'energy' ? 1 : 0.5);
          // A capped series (the car, ~11 kW) may not flatten everything else: if it is far larger, the
          // axis follows the rest and its bars are cut with a break mark and their value printed.
          const capMax = Math.max(0, ...capVals);
          if (capMax > mx * 1.6) mx = Math.max(mx * 1.25, 2); else mx = Math.max(mx, capMax);
          const nt = niceTicks(p.kind === 'price' ? Math.max(0, Math.min(...vals) - 0.2) : mn, mx, ph < 110 ? 2 : 3);
          ({ lo, hi, ticks } = nt);
          if (p.kind === 'price' && Math.min(...vals) < lo) lo = Math.min(...vals);
        }
        const yv = (v) => y0 + ph - ((v - lo) / (hi - lo)) * ph;

        svg('text', { x: 0, y: y0 - (p.modeStrip ? 16 : 12), class: 'panel-title' }, g, p.title);
        for (const t of ticks) {
          svg('line', { x1: ml, x2: ml + pw, y1: yv(t), y2: yv(t), class: t === 0 && p.kind === 'energy' ? 'baseline' : 'gridline' }, g);
          svg('text', { x: ml - 6, y: yv(t) + 4, class: 'tick', 'text-anchor': 'end' }, g, p.tickFmt ? p.tickFmt(t) : fmt(t, t % 1 ? 1 : 0));
        }

        if (p.kind === 'price') {
          const fi = rows.findIndex((r) => r.forecast_price);
          if (fi >= 0) {
            svg('rect', { x: xAt(fi), y: y0, width: ml + pw - xAt(fi), height: ph, class: 'forecast-zone' }, g);
            svg('text', { x: xAt(fi) + 6, y: y0 + 12, class: 'tick' }, g, 'Estimerede priser');
          }
          for (const l of p.lines) {
            let d = '';
            rows.forEach((r, i) => { const v = yv(l.get(r)); d += (i ? 'L' : 'M') + xAt(i) + ',' + v + 'H' + xAt(i + 1); });
            svg('path', { d, style: { fill: 'none', stroke: l.color, strokeWidth: 2, strokeLinejoin: 'round', strokeLinecap: 'round' } }, g);
            if (cfg.endLabels) {
              const last = l.get(rows[n - 1]);
              svg('text', { x: ml + pw + 6, y: yv(last) + 4 + (l.labelDy || 0), class: 'endlabel' }, g, (l.short || l.label) + ' ' + fmt(last, 2));
            }
          }
        }

        if (p.kind === 'energy') {
          const base = yv(0);
          const barW = Math.max(2, Math.min(24, bw * 0.7));
          if (p.area) {
            let d = `M${xAt(0)},${base}`;
            rows.forEach((r, i) => { const v = yv(p.area.get(r)); d += `L${xAt(i)},${v}H${xAt(i + 1)}`; });
            d += `L${xAt(n)},${base}Z`;
            svg('path', { d, style: { fill: p.area.color, opacity: 0.16, stroke: 'none' } }, g);
          }
          if (p.modeStrip) {
            rows.forEach((r, i) => {
              const m = MODES[r.mode] || MODES.hold;
              svg('rect', { x: xAt(i) + 1, y: y0 - 9, width: Math.max(1, bw - 2), height: 5, rx: 2, style: { fill: m.color } }, g);
            });
          }
          rows.forEach((r, i) => {
            const cx = xAt(i) + bw / 2 - barW / 2;
            const stack = (series, dir) => {
              const segs = series.map((s) => ({ s, v: Math.max(0, s.get(r)) })).filter((x) => x.v > 0.005);
              let acc = 0;
              segs.forEach((x, k) => {
                const yA = Math.max(y0, yv(dir * acc)), yB = yv(dir * (acc + x.v));
                acc += x.v;
                let top = Math.min(yA, yB), bot = Math.max(yA, yB);
                const clipped = dir > 0 && top < y0;
                if (clipped) top = y0;
                const outer = k === segs.length - 1;
                if (dir > 0) { if (k > 0) bot -= 1; if (!outer) top += 1; } else { if (k > 0) top += 1; if (!outer) bot -= 1; }
                const hgt = bot - top;
                if (hgt < 0.5) return;
                svg('path', { d: barPath(cx, top, barW, hgt, dir > 0 && outer, dir < 0 && outer), style: { fill: x.s.color } }, g);
                if (clipped) {
                  svg('path', { d: `M${cx - 1},${y0 + 9} l${barW / 2 + 1},-4 l${barW / 2 + 1},4`, style: { fill: 'none', stroke: 'var(--surface)', strokeWidth: 2.5 } }, g);
                  svg('text', { x: cx + barW + 3, y: y0 + 10, class: 'tick' }, g, fmt(acc, 1));
                }
              });
            };
            stack(p.up, 1);
            stack(p.down, -1);
          });
          for (const l of p.lines || []) {
            let d = '';
            rows.forEach((r, i) => { const v = yv(l.get(r)); d += (i ? 'L' : 'M') + xAt(i) + ',' + v + 'H' + xAt(i + 1); });
            svg('path', { d, style: { fill: 'none', stroke: l.color, strokeWidth: 2, strokeLinejoin: 'round', strokeLinecap: 'round' } }, g);
          }
        }

        if (p.kind === 'soc') {
          for (const ref of p.refs || []) {
            svg('line', { x1: ml, x2: ml + pw, y1: yv(ref.v), y2: yv(ref.v), style: { stroke: ref.color, strokeWidth: 1, opacity: 0.55 } }, g);
            svg('text', { x: ml + pw - 4, y: yv(ref.v) - 4, class: 'tick', 'text-anchor': 'end' }, g, ref.label);
          }
          const ends = [];
          for (const l of p.lines) {
            const pts = [[xNow, l.start]];
            rows.forEach((r, i) => { const v = l.get(r); if (v != null) pts.push([xAt(i + 1), v]); });
            if (pts.length < 2 || pts[0][1] == null) continue;
            const d = pts.map(([x, v], k) => (k ? 'L' : 'M') + x + ',' + yv(v)).join('');
            svg('path', { d, style: { fill: 'none', stroke: l.color, strokeWidth: 2, strokeLinejoin: 'round', strokeLinecap: 'round' } }, g);
            const [ex, ev] = pts[pts.length - 1];
            svg('circle', { cx: ex, cy: yv(ev), r: 4, style: { fill: l.color, stroke: 'var(--surface)', strokeWidth: 2 } }, g);
            ends.push({ y: yv(ev), text: (l.short || l.label) + ' ' + fmt(ev, 0) + ' %' });
          }
          // End labels only when they do not collide; the legend carries identity otherwise.
          if (cfg.endLabels && (ends.length < 2 || Math.abs(ends[0].y - ends[1].y) >= 13)) {
            ends.forEach((e) => svg('text', { x: ml + pw + 8, y: e.y + 4, class: 'endlabel' }, g, e.text));
          }
        }
        return { yv };
      }
    }
  }

  /* Column chart (one or more series per category), with zero baseline and hover per group. */
  function columnChart(container, cfg) {
    container.replaceChildren();
    const items = cfg.items;
    if (!items.length) { container.appendChild(h('div', { class: 'empty' }, cfg.empty || 'Ingen data endnu.')); return; }
    observe(container, (W) => {
      const H = cfg.height || 200, ml = 46, mr = 8, top = 14, axisH = 22;
      const pw = W - ml - mr, ph = H - top - axisH;
      const n = items.length, k = cfg.series.length;
      const band = pw / n;
      const vals = cfg.stacked
        ? items.flatMap((it) => [it.values.reduce((a, v) => a + Math.max(0, v || 0), 0), it.values.reduce((a, v) => a + Math.min(0, v || 0), 0)])
        : items.flatMap((it) => it.values).filter((v) => v != null);
      const nt = niceTicks(Math.min(0, ...vals), Math.max(0, ...vals, 0.1), 4);
      const yv = (v) => top + ph - ((v - nt.lo) / (nt.hi - nt.lo)) * ph;
      const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', 'aria-label': cfg.label || '' });
      for (const t of nt.ticks) {
        svg('line', { x1: ml, x2: ml + pw, y1: yv(t), y2: yv(t), class: t === 0 ? 'baseline' : 'gridline' }, root);
        svg('text', { x: ml - 6, y: yv(t) + 4, class: 'tick', 'text-anchor': 'end' }, root, fmt(t, Number.isInteger(nt.step) ? 0 : 1));
      }
      const kk = cfg.stacked ? 1 : k;
      const barW = Math.max(cfg.stacked ? 1.5 : 3, Math.min(24, (band * 0.72 - (kk - 1) * 2) / kk));
      const groupW = barW * kk + (kk - 1) * 2;
      const labelEvery = cfg.labelEvery || Math.ceil(36 / band);
      items.forEach((it, i) => {
        const gx = ml + i * band + band / 2 - groupW / 2;
        const g = svg('g', { tabindex: 0, class: 'plot' }, root);
        svg('rect', { x: ml + i * band, y: top, width: band, height: ph, fill: 'transparent' }, g);
        if (cfg.stacked) {
          // positives stack up, negatives down, in series order; 2 px gap between segments,
          // rounded only at the outer end of each side
          for (const dir of [1, -1]) {
            const segs = it.values.map((v, j) => ({ v: v || 0, j })).filter((x) => x.v * dir > 0);
            let acc = 0;
            segs.forEach((x, idx) => {
              const a = yv(acc), b = yv(acc + x.v);
              acc += x.v;
              let top = Math.min(a, b), bot = Math.max(a, b);
              const outer = idx === segs.length - 1;
              if (dir > 0) { if (idx > 0) bot -= 1; if (!outer) top += 1; } else { if (idx > 0) top += 1; if (!outer) bot -= 1; }
              if (bot - top < 0.5) return;
              svg('path', { d: barPath(gx, top, barW, bot - top, dir > 0 && outer, dir < 0 && outer), style: { fill: cfg.series[x.j].color } }, g);
            });
          }
        } else {
          it.values.forEach((v, j) => {
            if (v == null) return;
            const y0 = yv(0), y1 = yv(v);
            const hgt = Math.abs(y1 - y0);
            if (hgt < 0.5) return;
            svg('path', { d: barPath(gx + j * (barW + 2), Math.min(y0, y1), barW, hgt, v > 0, v < 0), style: { fill: cfg.series[j].color } }, g);
          });
        }
        if (cfg.valueLabels && k === 1 && it.values[0] != null && band >= 34) {
          const v = it.values[0];
          svg('text', { x: ml + i * band + band / 2, y: v >= 0 ? yv(v) - 5 : yv(v) + 13, class: 'tick', 'text-anchor': 'middle' }, root, fmt(v, cfg.valueDigits ?? 0));
        }
        if (i % labelEvery === 0 && it.label) svg('text', { x: ml + i * band + band / 2, y: H - 6, class: 'tick', 'text-anchor': 'middle' }, root, it.label);
        const show = (cx, cy) => { g.style.opacity = 0.85; tip.show(cfg.tooltip(it, i), cx, cy); };
        g.addEventListener('pointermove', (e) => show(e.clientX, e.clientY));
        g.addEventListener('pointerleave', () => { g.style.opacity = ''; tip.hide(); });
        g.addEventListener('focus', () => { const b = g.getBoundingClientRect(); show(b.right, b.top); });
        g.addEventListener('blur', () => { g.style.opacity = ''; tip.hide(); });
      });
      container.replaceChildren(root);
    });
  }

  /* Line over categories (one series): area wash, end dot with label, crosshair + tooltip. */
  function lineChart(container, cfg) {
    container.replaceChildren();
    const items = cfg.items;
    if (items.length < 2) { container.appendChild(h('div', { class: 'empty' }, cfg.empty || 'For lidt data til en kurve.')); return; }
    observe(container, (W) => {
      const H = cfg.height || 180, ml = 46, mr = 64, top = 14, axisH = 22;
      const pw = W - ml - mr, ph = H - top - axisH;
      const n = items.length;
      const xs = (i) => ml + (i / (n - 1)) * pw;
      const vals = items.map((it) => it.value);
      const nt = niceTicks(Math.min(0, ...vals), Math.max(0, ...vals, 0.1), 4);
      const yv = (v) => top + ph - ((v - nt.lo) / (nt.hi - nt.lo)) * ph;
      const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', 'aria-label': cfg.label || '' });
      for (const t of nt.ticks) {
        svg('line', { x1: ml, x2: ml + pw, y1: yv(t), y2: yv(t), class: t === 0 ? 'baseline' : 'gridline' }, root);
        svg('text', { x: ml - 6, y: yv(t) + 4, class: 'tick', 'text-anchor': 'end' }, root, fmt(t, Number.isInteger(nt.step) ? 0 : 1));
      }
      const pts = items.map((it, i) => [xs(i), yv(it.value)]);
      const line = pts.map(([x, y], i) => (i ? 'L' : 'M') + x + ',' + y).join('');
      svg('path', { d: line + `L${xs(n - 1)},${yv(0)}L${xs(0)},${yv(0)}Z`, style: { fill: cfg.color, opacity: 0.1 } }, root);
      svg('path', { d: line, style: { fill: 'none', stroke: cfg.color, strokeWidth: 2, strokeLinejoin: 'round', strokeLinecap: 'round' } }, root);
      const [ex, ey] = pts[n - 1];
      svg('circle', { cx: ex, cy: ey, r: 4, style: { fill: cfg.color, stroke: 'var(--surface)', strokeWidth: 2 } }, root);
      svg('text', { x: ex + 8, y: ey + 4, class: 'endlabel' }, root, fmt(vals[n - 1], 0) + (cfg.unit ? ' ' + cfg.unit : ''));
      const labelEvery = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(pw / 56))));
      items.forEach((it, i) => {
        if (i % labelEvery === 0 && it.label) svg('text', { x: xs(i), y: H - 6, class: 'tick', 'text-anchor': 'middle' }, root, it.label);
      });
      const cross = svg('line', { x1: 0, x2: 0, y1: top, y2: top + ph, class: 'crosshair', visibility: 'hidden' }, root);
      const dot = svg('circle', { r: 4, style: { fill: cfg.color, stroke: 'var(--surface)', strokeWidth: 2 }, visibility: 'hidden' }, root);
      const overlay = svg('rect', { x: ml, y: top, width: pw, height: ph, fill: 'transparent', class: 'plot', tabindex: 0 }, root);
      let cur = -1;
      const showAt = (i, cx, cy) => {
        cur = Math.max(0, Math.min(n - 1, i));
        const [x, y] = pts[cur];
        cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
        dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.setAttribute('visibility', 'visible');
        tip.show(cfg.tooltip(items[cur], cur), cx, cy);
      };
      const hide = () => { cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); tip.hide(); };
      overlay.addEventListener('pointermove', (e) => {
        const box = root.getBoundingClientRect();
        const x = (e.clientX - box.left) * (W / box.width);
        showAt(Math.round(((x - ml) / pw) * (n - 1)), e.clientX, e.clientY);
      });
      overlay.addEventListener('pointerleave', hide);
      overlay.addEventListener('blur', hide);
      overlay.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        const box = root.getBoundingClientRect();
        const i = cur < 0 ? 0 : cur + (e.key === 'ArrowRight' ? 1 : -1);
        showAt(i, box.left + pts[Math.max(0, Math.min(n - 1, i))][0] * (box.width / W), box.top + 30);
      });
      container.replaceChildren(root);
    });
  }

  /* Two-row strip: what the planner wanted vs what Node-RED had set, over time. */
  function stripChart(container, runs) {
    container.replaceChildren();
    if (!runs.length) { container.appendChild(h('div', { class: 'empty' }, 'Ingen sammenligning endnu.')); return; }
    const t0 = new Date(runs[0].start).getTime();
    const t1 = new Date(runs[runs.length - 1].end).getTime();
    observe(container, (W) => {
      const ml = W < 560 ? 66 : 78, mr = 8, rowH = 16, gap = 8, top = 4;
      const H = top + rowH * 3 + gap * 2 + 22;
      const pw = W - ml - mr;
      const x = (t) => ml + ((t - t0) / Math.max(1, t1 - t0)) * pw;
      const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', 'aria-label': 'Planner mod Node-RED over tid' });
      hatchDefs(root);
      const labels = ['Planner', 'Node-RED', 'Uenige'];
      labels.forEach((l, r) => svg('text', { x: 0, y: top + r * (rowH + gap) + 12, class: 'tick' }, root, l));
      // day ticks
      const d0 = new Date(t0); d0.setHours(24, 0, 0, 0);
      const dayW = 864e5 * pw / Math.max(1, t1 - t0);
      const short = dayW < 72;
      const every = Math.max(1, Math.ceil((short ? 34 : 72) / dayW));
      let k = 0;
      for (let d = d0.getTime(); d < t1; d += 864e5, k++) {
        svg('line', { x1: x(d), x2: x(d), y1: top, y2: H - 18, class: 'gridline' }, root);
        const dd = new Date(d);
        if (k % every === 0 && x(d) + 30 < W) {
          svg('text', { x: x(d) + 3, y: H - 4, class: 'tick' }, root,
            dd.toLocaleDateString('da-DK', short ? { day: 'numeric', month: 'numeric' } : { weekday: 'short', day: 'numeric' }));
        }
      }
      for (const run of runs) {
        const a = new Date(run.start).getTime(), b = new Date(run.end).getTime();
        const xa = x(a), w = Math.max(1, x(b) - xa);
        const g = svg('g', {}, root);
        [run.planned, run.actual].forEach((c, r) => {
          const cat = CATS[c];
          svg('rect', { x: xa, y: top + r * (rowH + gap), width: w, height: rowH,
            style: cat ? { fill: cat.color } : {}, fill: cat ? null : 'url(#hatch)' }, g);
        });
        if (run.planned && run.actual && run.planned !== run.actual) {
          svg('rect', { x: xa, y: top + 2 * (rowH + gap) + 4, width: w, height: rowH - 8, rx: 2, style: { fill: 'var(--ink-2)' } }, g);
        }
        svg('rect', { x: xa, y: top, width: w, height: rowH * 3 + gap * 2, fill: 'transparent' }, g);
        g.addEventListener('pointermove', (e) => {
          const fmtT = (t) => new Date(t).toLocaleString('da-DK', { weekday: 'short', hour: '2-digit', minute: '2-digit' });
          const agree = run.planned && run.actual ? (run.planned === run.actual ? 'Enige' : 'Uenige') : 'Ukendt';
          tip.show(h('div', {},
            h('div', { class: 'tt-head' }, fmtT(a) + ' – ' + fmtT(b)),
            ttRow(CATS[run.planned]?.color || C.muted, 'Planner', CATS[run.planned]?.label || 'ukendt', ''),
            ttRow(CATS[run.actual]?.color || C.muted, 'Node-RED', CATS[run.actual]?.label || 'ukendt', ''),
            h('div', { class: 'tt-sec' }, agree)), e.clientX, e.clientY);
        });
        g.addEventListener('pointerleave', () => tip.hide());
      }
      container.replaceChildren(root);
    });
  }

  const legend = (items) => h('div', { class: 'legend' },
    items.map((it) => h('span', {}, h('span', { class: 'key' + (it.line ? ' line' : '') + (it.wash ? ' wash' : ''), style: { background: it.color } }), it.label)));

  // ---------------------------------------------------------------- data access
  const state = { plan: null, planError: null, car: null, evalDays: parseInt(store.get('evalDays') || '14', 10), evalData: null, tab: 'plan',
    econDays: parseInt(store.get('econDays') || '30', 10), econData: null, econError: null };
  const perH = (r, v) => (v || 0) / (r.dt || 1);   // energy per step -> per hour (step 0 is partial)
  const carIn = (r) => (r.car_grid || 0) + (r.car_pv || 0) + (r.car_bat || 0);

  // ---------------------------------------------------------------- Plan tab
  function tile(label, value, detail, extra) {
    return h('div', { class: 'tile' }, h('div', { class: 'label' }, label), h('div', { class: 'value' }, value), extra || null, detail ? h('div', { class: 'detail' }, detail) : null);
  }
  function meter(pctVal, marks = [], cls = '') {
    const m = h('div', { class: 'meter ' + cls, role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': Math.round(pctVal || 0) },
      h('div', { class: 'fill', style: { width: Math.max(0, Math.min(100, pctVal || 0)) + '%' } }));
    marks.forEach((mk) => mk.v != null && m.appendChild(h('div', { class: 'mark' + (mk.ghost ? ' ghost' : ''), title: mk.title, style: { left: `calc(${mk.v}% - 1px)` } })));
    return m;
  }

  // "Kommende" is computed by the add-on (same text as the sensor attributes for dashboards).
  function upcoming(p) {
    return (p.upcoming || []).map((it) => ({
      when: it.when, what: it.label, why: it.text,
      color: it.kind === 'car' ? C.car : (MODES[it.mode] || MODES.hold).color,
    }));
  }

  function registerRows(p) {
    const sp = p.setpoints_live || p.setpoints, a = p.actual_registers || {};
    const writes = {};
    for (const w of ((p.production || {}).writes || [])) writes[w.key] = w;
    const rows = [
      ['Battery first', sp.battery_first ? 'Til' : 'Fra', a.reg_battery_first, (x) => isOn(x) === sp.battery_first, 'reg_battery_first'],
      ['Lad fra nettet', sp.grid_charge ? 'Til' : 'Fra', a.reg_grid_charge, (x) => isOn(x) === sp.grid_charge, 'reg_grid_charge'],
      ['Max SoC (mål)', sp.max_soc_pct == null ? '–' : sp.max_soc_pct + ' %', a.reg_max_soc, (x) => sp.max_soc_pct == null || Math.abs(+x - sp.max_soc_pct) <= 1, 'reg_max_soc'],
      ['Min SoC (gulv)', sp.min_soc_pct + ' %', a.reg_min_soc, (x) => Math.abs(+x - sp.min_soc_pct) <= 1, 'reg_min_soc'],
      ['Ladehastighed', sp.charge_rate_pct + ' %', a.reg_charge_rate, (x) => !sp.battery_first || Math.abs(+x - sp.charge_rate_pct) <= 5, 'reg_charge_rate'],
      ['Eksport', sp.export_hint === 'export' ? 'Sælg' : 'Ingen', a.reg_export_limit, null, null],
    ];
    return rows.map(([name, planned, actual, same, key]) => {
      const known = actual != null && actual !== 'unknown' && actual !== 'unavailable';
      const ok = same && known ? same(actual) : null;
      const w = key && writes[key];
      const status = w
        ? h('span', { class: 'flag ' + (w.status === 'error' ? 'warn' : 'ok'), title: w.error || '' },
          w.status === 'error' ? 'Fejl' : (w.status === 'confirmed' ? 'Skrevet ' : 'Sendt ') + w.at.slice(11, 16))
        : (ok == null ? '' : h('span', { class: 'flag ' + (ok ? 'ok' : 'warn') }, ok ? 'Ens' : 'Afviger'));
      return h('tr', { class: ok === false && !w ? 'diff' : '' },
        h('td', {}, name), h('td', {}, planned), h('td', {}, known ? String(actual) : '–'), h('td', {}, status));
    });
  }

  function planTooltip(p, i) {
    const r = p.plan[i];
    const m = MODES[r.mode] || MODES.hold;
    const kids = [h('div', { class: 'tt-head' }, `${dayName(dayKey(r.t))} · ${hhmm(r.t)}–${addHour(r.t)}`),
      h('div', { class: 'tt-row' }, h('span', { class: 'mode-dot', style: { background: m.color } }), h('span', { class: 'lbl' }, m.label))];
    kids.push(ttSec('Pris, kr/kWh' + (r.forecast_price ? ' (estimeret)' : '')));
    kids.push(ttRow(C.buy, 'Køb', fmt(r.buy, 2)), ttRow(C.sell, 'Salg', fmt(r.sell, 2)));
    kids.push(ttSec('Energi, kWh' + (r.dt < 1 ? ` (resten af timen)` : '')));
    const flows = [[C.gridbat, 'Net → batteri', r.grid_bat], [C.sun, 'Sol → batteri', r.pv_bat], [C.car, 'Til bil', carIn(r)],
      [C.batt, 'Batteri → hus', r.bat_load + (r.car_bat || 0)], [C.export, 'Eksport', r.pv_exp]];
    flows.forEach(([c, l, v]) => { if (v > 0.005) kids.push(ttRow(c, l, fmt(v, 2), '')); });
    kids.push(ttRow(C.ink2, 'Net → hus', fmt(r.grid_load, 2)), ttRow(C.sun, 'Sol (prognose)', fmt(r.pv, 2) + (r.pv_p90 > r.pv_p10 ? `  (${fmt(r.pv_p10, 1)}–${fmt(r.pv_p90, 1)})` : '')),
      r.pv_factor != null && r.pv_factor < 0.995 ? ttRow(C.ink2, 'Skyggekorrektion', '× ' + fmt(r.pv_factor, 2)) : null, ttRow(C.ink2, 'Husforbrug', fmt(r.load, 2)));
    kids.push(ttSec('Batteri ved timens slut'));
    kids.push(ttRow(C.batt, 'Hus', pct(r.soc_pct)));
    if (r.car_soc != null) kids.push(ttRow(C.car, 'Bil', pct(r.car_soc)));
    return h('div', {}, kids);
  }

  function renderPlan() {
    const root = $('#tab-plan');
    const p = state.plan;
    if (!p) {
      root.replaceChildren(h('div', { class: 'card' }, h('div', { class: 'empty' }, state.planError ? 'Ingen plan: ' + state.planError : 'Henter plan…')));
      return;
    }
    const sp = p.setpoints_live || p.setpoints;
    const m = MODES[sp.mode] || MODES.hold;
    const rows = p.plan;
    const r0 = rows[0];
    const buys = rows.map((r) => r.buy);
    const rank = buys.filter((b) => b < r0.buy).length / Math.max(1, buys.length - 1);

    const banners = [];
    if (p.last_error) banners.push(h('div', { class: 'banner error' }, icon.error(), h('div', {}, 'Seneste planlægning fejlede: ', p.last_error)));
    const cw = state.car && state.car.latest && state.car.latest.warning;
    if (cw) banners.push(h('div', { class: 'banner error' }, icon.error(), h('div', {}, 'Bil: ', cw)));
    const bad = Object.entries(p.entity_check || {}).filter(([, v]) => v !== 'ok');
    const warns = [...(p.warnings || [])];
    if (bad.length) warns.push('Entiteter mangler eller er utilgængelige: ' + bad.map(([e, v]) => `${e} (${v === 'missing' ? 'findes ikke' : 'utilgængelig'})`).join(', '));
    if (warns.length) banners.push(h('div', { class: 'banner' }, icon.warn(), h('ul', {}, warns.map((w) => h('li', {}, translateWarning(w))))));

    let modeDetail = '';
    if (sp.mode === 'grid_charge') modeDetail = `til ${sp.max_soc_pct} % med ${fmt(sp.charge_kw, 1)} kW (ladehastighed ${sp.charge_rate_pct} %)`
      + (sp.run_target_pct > sp.max_soc_pct ? `, derefter sol til ${sp.run_target_pct} %` : '');
    else if (sp.handed_over) modeDetail = `nettet har ladet sin del, solen lader videre til ${sp.max_soc_pct} %`;
    else if (sp.mode === 'solar_charge') modeDetail = `til ${sp.max_soc_pct} %` + (sp.charge_rate_pct < 100 ? ` · ladehastighed ${sp.charge_rate_pct} %` : '');
    else if (sp.mode === 'discharge') modeDetail = `${fmt(sp.discharge_kw, 1)} kW · gulv ${sp.min_soc_pct} %`;
    else modeDetail = sp.export_hint === 'export' ? 'solen sælges, batteriet gemmes' : 'batteriet gemmes til senere';

    const tiles = h('div', { class: 'tiles' },
      tile('Nu', h('span', { class: 'mode-badge', style: { fontSize: '20px' } }, h('span', { class: 'mode-dot', style: { background: m.color } }), m.label), modeDetail),
      tile('Husbatteri', h('span', {}, fmt(p.soc_pct, 0), h('small', {}, ' %')),
        `mål ${sp.max_soc_pct == null ? '–' : sp.max_soc_pct + ' %'} · gulv ${sp.min_soc_pct} %`,
        meter(p.soc_pct, [{ v: sp.max_soc_pct, title: 'Mål' }, { v: sp.min_soc_pct, ghost: true, title: 'Gulv' }])),
      tile('Pris nu', h('span', {}, fmt(r0.buy, 2), h('small', {}, ' kr/kWh')),
        `salg ${fmt(r0.sell, 2)} kr · ${rank <= 0.25 ? 'blandt de billigste' : rank >= 0.75 ? 'blandt de dyreste' : 'middel'} timer i planen`),
      tile('Plan mod kun sol', h('span', {}, fmtSigned(p.savings_vs_solar_only_dkk, 2), h('small', {}, ' kr')),
        `sparet over ${p.horizon_hours} timer ved at lade fra nettet når det betaler sig`),
      p.car && p.car.enabled ? tile('Bil', p.car.connected ? h('span', {}, fmt(p.car.soc_pct, 0), h('small', {}, ' %')) : h('span', { class: 'ink2', style: { fontSize: '18px' } }, 'Ikke tilsluttet'),
        p.car.connected ? `planlagt ${fmt(p.car.planned_kwh, 1)} kWh frem til kl. ${p.car.deadline.slice(11, 16)}` : 'planen tager bilen med, når den sættes til',
        p.car.connected ? meter(p.car.soc_pct, [{ v: 42, title: 'Minimum' }, { v: 90, ghost: true, title: 'Maks' }], 'car') : null) : null);

    const hasCar = rows.some((r) => carIn(r) > 0.005);
    const chartBox = h('div', { class: 'chart' });
    const chartCard = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'De næste ' + rows.length + ' timer'), h('span', { class: 'hint' }, 'Hold musen over grafen for detaljer · piletaster virker også')),
      chartBox,
      h('div', { class: 'stack', style: { gap: '6px', marginTop: '10px' } },
        legend([{ color: C.buy, label: 'Købspris', line: true }, { color: C.sell, label: 'Salgspris', line: true }]),
        legend([{ color: C.gridbat, label: 'Net → batteri' }, { color: C.sun, label: 'Sol → batteri' }, hasCar ? { color: C.car, label: 'Til bil' } : null,
          { color: C.batt, label: 'Batteri → hus' }, { color: C.export, label: 'Eksport' }, { color: C.sun, label: 'Solprognose', wash: true }, { color: C.ink2, label: 'Husforbrug', line: true }].filter(Boolean)),
        legend([{ color: C.batt, label: 'Husbatteri SoC', line: true }, p.car && p.car.connected ? { color: C.car, label: 'Bil SoC', line: true } : null].filter(Boolean)),
        legend(Object.values(MODES).map((mm) => ({ color: mm.color, label: mm.label })))));

    const up = upcoming(p);
    const upCard = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Kommende')),
      up.length ? h('ul', { class: 'upcoming' }, up.map((it) => h('li', {},
        h('span', { class: 'mode-dot', style: { background: it.color } }),
        h('div', {}, h('div', { class: 'line1' }, h('span', { class: 'what' }, it.what), h('span', { class: 'when' }, it.when)),
          h('div', { class: 'why' }, it.why))))) : h('div', { class: 'empty' }, 'Intet planlagt.'));

    const prodOn = p.production && p.production.battery;
    const regCard = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Planner mod inverteren nu'),
        h('span', { class: 'hint' }, prodOn ? 'Produktion: add-on’en skriver registrene (manuelle ændringer overskrives ved næste cyklus)' : 'Dry-run: hvad planneren ville sætte, og hvad Node-RED har sat')),
      h('div', { class: 'table-wrap' }, h('table', { class: 'cmp' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Register'), h('th', {}, 'Planner'), h('th', {}, 'Nu'), h('th', {}, ''))),
        h('tbody', {}, registerRows(p)))));

    const table = h('table', {},
      h('thead', {}, h('tr', {}, ['Time', 'Tilstand', 'Køb', 'Salg', 'Sol', 'Forbrug', 'Net→bat', 'Sol→bat', 'Bat→hus', 'Eksport', 'SoC', ...(p.car && p.car.connected ? ['Bil kWh', 'Bil %'] : [])].map((t) => h('th', {}, t)))),
      h('tbody', {}, rows.map((r, i) => {
        const z = (v, d = 2) => h('td', { class: Math.abs(v || 0) < 0.005 ? 'zero' : '' }, fmt(v, d));
        const mm = MODES[r.mode] || MODES.hold;
        return h('tr', { class: i === 0 ? 'now' : '' },
          h('td', {}, (hhmm(r.t) === '00:00' || i === 0 ? dayName(dayKey(r.t), { weekday: 'short' }) + ' ' : '') + hhmm(r.t)),
          h('td', {}, h('span', { class: 'mode-dot', style: { background: mm.color } }), mm.label),
          h('td', { class: r.forecast_price ? 'est' : '' }, fmt(r.buy, 2)), h('td', { class: r.forecast_price ? 'est' : '' }, fmt(r.sell, 2)),
          z(r.pv), z(r.load), z(r.grid_bat), z(r.pv_bat), z(r.bat_load + (r.car_bat || 0)), z(r.pv_exp), h('td', {}, fmt(r.soc_pct, 0) + ' %'),
          ...(p.car && p.car.connected ? [z(carIn(r)), h('td', {}, fmt(r.car_soc, 0) + ' %')] : []));
      })));
    const tableCard = h('div', { class: 'card' },
      h('details', { open: store.get('tableOpen') === '1' ? true : null, ontoggle: (e) => store.set('tableOpen', e.target.open ? '1' : '0') },
        h('summary', {}, 'Plan time for time (tabel)'),
        h('div', { class: 'hint', style: { marginBottom: '8px' } }, 'Energi i kWh pr. time. Kursiv pris = estimeret ud fra samme time sidste kendte dag.'),
        h('div', { class: 'table-wrap' }, table)));

    const prof = p.profile;
    const profBox = h('div', { class: 'chart' });
    const profCard = prof ? h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Forbrugsprofil (basisforbrug pr. time, kWh)'),
        h('span', { class: 'hint' }, `Det planen regner med · ${fmt(prof.hours_of_data, 0)} timers statistik`)),
      profBox,
      h('div', { style: { marginTop: '10px' } }, legend([{ color: C.batt, label: 'Hverdag' }, { color: C.export, label: 'Weekend' }])),
      h('ul', { class: 'diag' }, profileDiagnostics(prof).map((d) => h('li', { class: d.warn ? 'warn' : '' }, d.text)))) : null;

    const dmp = p.pv_dampening;
    const dmpBox = h('div', { class: 'chart' });
    const dmpCard = dmp ? dampeningCard(dmp, dmpBox) : null;

    root.replaceChildren(h('div', { class: 'stack' }, banners, tiles, chartCard, h('div', { class: 'grid-2' }, upCard, regCard), profCard, dmpCard, tableCard));
    if (dmp && !dmp.solcast_auto) {
      const hrs = [...Array(24).keys()].filter((hh) => (dmp.hours || [])[hh] > 0);
      columnChart(dmpBox, {
        label: 'Sol pr. time: Solcast, faktisk og korrigeret', height: 180,
        empty: 'Ingen soltimer at sammenligne endnu – historikken bygges op time for time.',
        items: hrs.map((hh) => ({ label: String(hh).padStart(2, '0'), values: [dmp.forecast[hh], dmp.actual[hh], dmp.corrected[hh]], hh })),
        series: [{ label: 'Solcast', color: C.sun }, { label: 'Faktisk', color: C.batt }, { label: 'Med korrektion', color: C.gridbat }],
        tooltip: (it) => h('div', {}, h('div', { class: 'tt-head' }, `Kl. ${it.label} · gns. over ${dmp.hours[it.hh]} dage`),
          ttRow(C.sun, 'Solcast (uden dæmpning)', fmt(it.values[0], 2) + ' kWh', ''), ttRow(C.batt, 'Faktisk', fmt(it.values[1], 2) + ' kWh', ''),
          ttRow(C.gridbat, 'Med korrektion', fmt(it.values[2], 2) + ' kWh', ''),
          ttRow(C.ink2, 'Faktor ved klar himmel', fmt(dmp.clear_factors[it.hh], 2)), ttRow(C.ink2, 'Faktor i dag', fmt(dmp.today_factors[it.hh], 2))),
      });
    }
    if (prof) {
      columnChart(profBox, {
        label: 'Basisforbrug pr. time', height: 170,
        items: prof.weekday.map((v, hh) => ({ label: String(hh).padStart(2, '0'), values: [v, prof.weekend[hh]], hh })),
        series: [{ label: 'Hverdag', color: C.batt }, { label: 'Weekend', color: C.export }],
        tooltip: (it) => h('div', {}, h('div', { class: 'tt-head' }, `Kl. ${it.label}–${String((it.hh + 1) % 24).padStart(2, '0')}`),
          ttRow(C.batt, 'Hverdag', fmt(it.values[0], 2) + ' kWh', ''), ttRow(C.export, 'Weekend', fmt(it.values[1], 2) + ' kWh', '')),
      });
    }

    const carConnected = p.car && p.car.connected;
    timeChart(chartBox, {
      rows, endLabels: true, label: 'Priser, energi og batteriets SoC time for time',
      tooltip: (i) => planTooltip(p, i),
      panels: [
        { kind: 'price', title: 'Pris (kr/kWh)', height: 96, lines: [
          { label: 'Køb', color: C.buy, get: (r) => r.buy },
          { label: 'Salg', color: C.sell, get: (r) => r.sell }] },
        { kind: 'energy', title: 'Energi pr. time (kWh) · tilstand', height: 150, modeStrip: true,
          up: [{ label: 'Net → batteri', color: C.gridbat, get: (r) => perH(r, r.grid_bat) },
            { label: 'Sol → batteri', color: C.sun, get: (r) => perH(r, r.pv_bat) },
            { label: 'Til bil', color: C.car, capped: true, get: (r) => perH(r, carIn(r)) }],
          down: [{ label: 'Batteri → hus', color: C.batt, get: (r) => perH(r, r.bat_load + (r.car_bat || 0)) },
            { label: 'Eksport', color: C.export, get: (r) => perH(r, r.pv_exp) }],
          area: { label: 'Solprognose', color: C.sun, get: (r) => perH(r, r.pv) },
          lines: [{ label: 'Husforbrug', color: C.ink2, get: (r) => perH(r, r.load) }] },
        { kind: 'soc', title: 'Batteri (%)', height: 100, tickFmt: (t) => t + '',
          lines: [{ label: 'Husbatteri', short: 'Hus', color: C.batt, start: p.soc_pct, get: (r) => r.soc_pct },
            ...(carConnected ? [{ label: 'Bil', short: 'Bil', color: C.car, start: p.car.soc_pct, get: (r) => r.car_soc }] : [])] },
      ],
    });
  }

  function profileDiagnostics(prof) {
    const d = prof.diagnostics || {}, out = [];
    if (!prof.hours_of_data) {
      out.push({ warn: true, text: 'Ingen statistik endnu (eller forespørgslen fejlede) – planen bruger default_load_kw for alle timer.' });
      return out;
    }
    if (!d.car_rows) out.push({ warn: true, text: 'Billaderens effekt har ingen langtidsstatistik endnu (giv den state_class: measurement).' });
    else out.push({ text: `Bilen er trukket fra i ${fmt(d.car_hours, 0)} timer med opladning (${d.car_kind === 'energy' ? 'energitæller, kWh pr. time' : 'effekt'}, statistik for ${fmt(d.car_rows, 0)} timer).` });
    if (d.car_excluded_hours) out.push({ text: `${fmt(d.car_excluded_hours, 0)} timer uden data fra laderen og med over ${fmt(d.car_detect_kw, 1)} kW husforbrug er regnet som bilopladning og taget ud af profilen.` });
    const b = d.battery_detection || {};
    const mode = d.battery_mode === 'auto' ? '' : ` (indstillet til "${d.battery_mode}")`;
    if (d.battery_mode === 'no') out.push({ text: 'Batteriets opladning regnes ikke med i husforbruget (indstilling "no", korrekt for Growatt SPH).' });
    else if (!d.battery_rows) out.push({ warn: true, text: 'Batteriets ladeeffekt har ingen langtidsstatistik – kan ikke afgøre, om husforbruget indeholder batteriets opladning.' });
    else if (b.reason === 'no_car_statistics') out.push({ warn: true, text: 'Kan ikke afgøre, om husforbruget indeholder batteriets opladning, før bilens statistik findes (bilens natopladning ville ligne batteriopladning).' + (d.battery_subtracted ? ' Trækkes fra' + mode + '.' : '') });
    else if (b.included === true) out.push({ warn: !d.battery_subtracted, text: `Husforbruget indeholder batteriets opladning: om natten ${fmt(b.base_while_charging_kw, 2)} kW mens batteriet lader (${fmt(b.charge_kw, 1)} kW) mod ${fmt(b.base_while_idle_kw, 2)} kW uden. ` + (d.battery_subtracted ? `Opladning fra nettet er trukket fra i ${fmt(d.battery_corrected_hours, 0)} timer.` : 'Den trækkes ikke fra') + mode });
    else if (b.included === false) out.push({ text: `Husforbruget indeholder ikke batteriets opladning (om natten ${fmt(b.base_while_charging_kw, 2)} kW under opladning mod ${fmt(b.base_while_idle_kw, 2)} kW uden).` + (d.battery_subtracted ? ' Trækkes alligevel fra' + mode + '.' : '') });
    else out.push({ text: `For få nattetimer med batteriopladning (${b.night_charging_hours ?? 0}) til at afgøre, om husforbruget indeholder den.` + (d.battery_subtracted ? ' Trækkes fra' + mode + '.' : '') });
    return out;
  }

  function dampeningCard(d, box) {
    const head = h('div', { class: 'card-head' }, h('h2', {}, 'Solkorrektion (skygge pr. time)'),
      h('span', { class: 'hint' }, d.solcast_auto ? 'Solcast dæmper selv' : `Lært over ${d.days} dage · faktor = 1 − skygge × klarhed`));
    if (d.solcast_auto) {
      return h('div', { class: 'card' }, head, h('div', { class: 'empty' },
        'Solcasts egen automatiske dæmpning er slået til. Planneren bruger Solcasts dæmpede prognose, og add-on’ens korrektion er sat på pause (de to kan ikke køre samtidig).'));
    }
    const status = [];
    const modeTxt = { auto: 'Lærer automatisk', manual: 'Kun manuelle timer', off: 'Slået fra' }[d.mode] || d.mode;
    status.push(modeTxt + (d.manual_hours && d.manual_hours.length ? ` · manuelt låst: kl. ${d.manual_hours.map((x) => String(x).padStart(2, '0')).join(', ')}` : ''));
    if (d.push_error) status.push({ warn: true, text: 'Kunne ikke sende til Solcast: ' + d.push_error });
    else if (d.push) status.push(d.last_pushed ? `Sendt til Solcast ${dayName(d.last_pushed.slice(0, 10), { weekday: 'short' })} kl. ${d.last_pushed.slice(11, 16)}` : 'Sendes til Solcast ved næste cyklus');
    else status.push('Virker kun i planneren (solcast_push er slået fra)');
    const bf = d.backfill;
    if (bf && bf.error) status.push({ warn: true, text: 'Kunne ikke hente prognose-historik fra Solcast (prøver igen om 6 timer): ' + bf.error });
    else if (bf) status.push(`Hentede ${bf.received} timers prognose-historik fra Solcast (seneste 14 dage), ${bf.hours} nye`);
    const day = [...Array(24).keys()].filter((hh) => (d.hours || [])[hh] > 0 || d.clear_factors[hh] < 1);
    const cell = (v, manual) => h('td', { class: v < 0.995 ? '' : 'zero' }, fmt(v, 2) + (manual ? '*' : ''));
    const table = day.length ? h('div', { class: 'table-wrap', style: { marginTop: '12px' } }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'Kl.'), day.map((hh) => h('th', {}, String(hh).padStart(2, '0'))))),
      h('tbody', {},
        h('tr', {}, h('td', {}, 'Klar himmel'), day.map((hh) => cell(d.clear_factors[hh], (d.manual_hours || []).includes(hh)))),
        h('tr', {}, h('td', {}, 'I dag'), day.map((hh) => cell(d.today_factors[hh]))),
        h('tr', {}, h('td', {}, 'I Solcast nu'), day.map((hh) => cell(d.applied_in_solcast[hh]))),
        h('tr', {}, h('td', {}, 'Dage med data'), day.map((hh) => h('td', { class: 'zero' }, String(d.hours[hh] || 0))))))) : null;
    return h('div', { class: 'card' }, head, box,
      h('div', { style: { marginTop: '10px' } }, legend([{ color: C.sun, label: 'Solcast (uden dæmpning)' }, { color: C.batt, label: 'Faktisk' }, { color: C.gridbat, label: 'Med korrektion' }])),
      table,
      h('ul', { class: 'diag' }, status.map((x) => typeof x === 'string' ? h('li', {}, x) : h('li', { class: 'warn' }, x.text))),
      h('div', { class: 'chips', style: { marginTop: '8px' } }, h('span', { class: 'chip wrap', title: 'Dagens faktorer til solcast_solar.set_dampening' }, d.today_factors.map((f) => f.toFixed(2)).join(','))));
  }

  const WARN_DA = [
    [/^(\d+) of (\d+) hours use estimated prices.*/, (m) => `${m[1]} af ${m[2]} timer bruger estimerede priser (samme time som sidste kendte dag).`],
    [/^no solar forecast available.*/, () => 'Ingen solprognose – planlægger uden sol.'],
    [/^car minimum cannot be reached by the deadline \(([\d.]+) kWh short\)/, (m) => `Bilens minimum kan ikke nås inden deadline (mangler ${fmt(+m[1], 1)} kWh).`],
    [/^the car charger power has no long-term statistics.*/, () => 'Billaderens effekt har ingen langtidsstatistik. Indtil den har, regnes timer med over car_detect_kw husforbrug som bilopladning og tages ud af forbrugsprofilen. Se kortet "Forbrugsprofil".'],
    [/^could not write (\S+): (.*)/, (m) => `Kunne ikke skrive ${m[1]}: ${m[2]}`],
    [/^the car only reaches (\d+) % by the departure/, (m) => `Bilen når kun ${m[1]} % til afgang.`],
    [/^car is connected but its SoC is unavailable.*/, () => 'Bilen er tilsluttet, men dens SoC er utilgængelig – bilen er udeladt af planen.'],
  ];
  function translateWarning(w) {
    for (const [re, f] of WARN_DA) { const m = w.match(re); if (m) return f(m); }
    return w;
  }

  const REASON_DA = [
    [/^not connected \((.*)\)$/, () => 'Ikke tilsluttet'],
    [/^car SoC unknown$/, () => 'Bilens SoC er ukendt'],
    [/^car at max SoC \((\d+) %\)$/, (m) => `Bilen er på maks (${m[1]} %)`],
    [/^critical SoC$/, () => 'Kritisk lav SoC – lader fuldt'],
    [/^plan: ([\d.]+) kW this hour$/, (m) => `Planen: ${fmt(+m[1], 1)} kW denne time`],
    [/^solar surplus (-?\d+) W$/, (m) => `Soloverskud ${fmt(+m[1] / 1000, 1)} kW`],
    [/^no plan, below minimum SoC$/, () => 'Ingen plan – under minimum, lader fuldt'],
    [/^plan: no car charging this hour$/, () => 'Planen lader ikke bilen denne time'],
    [/^no plan$/, () => 'Ingen plan'],
  ];
  function translateReason(r) {
    if (!r) return '';
    let suffix = '';
    const sm = r.match(/ \((cooldown|minimum charge time), (-?\d+) s left\)$/);
    if (sm) {
      r = r.slice(0, sm.index);
      suffix = ` (${sm[1] === 'cooldown' ? 'cooldown' : 'minimum ladetid'}, ${Math.max(1, Math.round(+sm[2] / 60))} min tilbage)`;
    }
    for (const [re, f] of REASON_DA) { const m = r.match(re); if (m) return f(m) + suffix; }
    return r + suffix;
  }

  // Calls in the car log: what was executed with its result (production), else what it would call.
  function callCell(e) {
    const short = (svc) => svc.replace('kia_uvo.', 'kia ').replace('number.set_value', 'strøm').replace('button.press', 'knap');
    if (e.executed && e.executed.length) {
      return e.executed.map((x) => {
        const mark = x.status === 'ok' ? '✓' : x.status === 'skipped' ? '–' : '✗';
        return h('div', { title: x.error || '' }, `${mark} ${short(x.service)}${x.value != null ? ' ' + x.value : ''}${x.error ? ': ' + x.error : ''}`);
      });
    }
    return (e.calls || []).length ? (e.dry_run === false ? '' : 'ville: ') + (e.calls || []).map((x) => short(x.service)).join(', ') : '';
  }

  // Until when the car is assumed connected: the next deadline, or a later planned trip.
  function carUntil(ci) {
    const d = ci.departure || {};
    if ((d.status === 'planned' || d.status === 'beyond_horizon') && d.at > ci.deadline) {
      return { at: d.at, label: dayName(d.at.slice(0, 10), { weekday: 'short' }) + ' kl. ' + d.at.slice(11, 16) };
    }
    return { at: ci.deadline, label: 'kl. ' + ci.deadline.slice(11, 16) };
  }

  function fullTile(ci) {
    const f = (ci && ci.full_charge) || {};
    const last = f.last_full ? dayName(f.last_full.slice(0, 10), { day: 'numeric', month: 'short' }) : 'ukendt';
    const since = f.days_since != null ? ` (${f.days_since} dage siden)` : '';
    const status = {
      off: 'slået fra (car_full_charge)',
      full: 'bilen er fuld nu',
      too_soon: `tidligst ${f.possible_from ? dayName(f.possible_from, { day: 'numeric', month: 'short' }) : ''}`,
      planned: f.why === 'negative_sell' ? 'planlagt: negativ salgspris' : `planlagt: nattens pris ${fmt(f.night_price, 2)} kr er i bund`,
      waiting: f.threshold != null ? `venter på bundpris: nat ${fmt(f.night_price, 2)} kr, grænse ${fmt(f.threshold, 2)} kr` : 'venter på negativ salgspris (for lidt prishistorik)',
    }[f.status] || (ci && ci.connected ? '–' : 'bilen er ikke tilsluttet');
    return tile('100 %-ladning', h('span', { style: { fontSize: '20px' } }, f.active ? 'I nat' : 'Ikke nu'), `sidst fuld ${last}${since} · ${status}`);
  }

  function departureTile(ci) {
    const d = (ci && ci.departure) || {};
    if (!d.set) return tile('Afgang', h('span', { style: { fontSize: '20px' } }, 'Ingen'), 'sæt input_datetime.ioniq9_afgang for en lang tur');
    const when = dayName(d.at.slice(0, 10)) + ' kl. ' + d.at.slice(11, 16);
    const status = {
      passed: 'afgangen er passeret',
      beyond_horizon: 'planlægges, når priserne er kendt',
      planned: ci.departure_ok ? `planen når ${fmt(ci.departure_reached_pct, 0)} %` : `⚠ når kun ${fmt(ci.departure_reached_pct, 0)} %`,
    }[d.status] || '';
    return tile('Afgang', h('span', { style: { fontSize: '20px' } }, when), `mål ${fmt(d.soc, 0)} % · ${status}`);
  }

  // ---------------------------------------------------------------- Car tab
  function renderCar() {
    const root = $('#tab-bil');
    const p = state.plan;
    const c = state.car && state.car.latest;
    const carInfo = p && p.car;
    if (carInfo && carInfo.enabled === false) {
      root.replaceChildren(h('div', { class: 'card' }, h('div', { class: 'empty' }, 'Bilen er slået fra i add-on’ens konfiguration (car_enabled).')));
      return;
    }
    const live = (c && c.live) || {};
    const status = live.status;
    const action = c ? (CAR_ACTIONS[c.action] || c.action) : '–';

    const tiles = h('div', { class: 'tiles' },
      tile('Bilens batteri', live.car_soc != null ? h('span', {}, fmt(live.car_soc, 0), h('small', {}, ' %')) : '–',
        `minimum 42 % kl. 06 · maks ${fmt((carInfo && carInfo.max_soc_pct) || 90, 0)} %`,
        meter(live.car_soc, [{ v: 42, title: 'Minimum' }, { v: (carInfo && carInfo.max_soc_pct) || 90, ghost: true, title: 'Maks' }], 'car')),
      tile('Lader', h('span', { style: { fontSize: '20px' } }, CHARGER_STATUS[status] || status || '–'),
        live.charger_w != null ? `${fmt((live.charger_w || 0) / 1000, 1)} kW · ${live.amps != null ? fmt(live.amps, 0) + ' A sat' : ''}` : ''),
      tile('Styringen ville', h('span', { style: { fontSize: '20px' } }, action + (c && c.target_amps ? ` · ${c.target_amps} A` : '')), c ? (c.reason_da || translateReason(c.reason)) : 'venter på første cyklus'),
      tile('Planlagt', carInfo && carInfo.connected ? h('span', {}, fmt(carInfo.planned_kwh, 1), h('small', {}, ' kWh')) : '–',
        carInfo && carInfo.connected ? `frem til ${carUntil(carInfo).label} · net-del ca. ${fmt(carInfo.planned_cost_dkk, 0)} kr` + (carInfo.shortfall_kwh > 0.05 ? ` · mangler ${fmt(carInfo.shortfall_kwh, 1)} kWh` : '') : 'bilen er ikke tilsluttet'),
      fullTile(carInfo), departureTile(carInfo));

    const live_ = c && c.dry_run === false;
    const executed = (c && c.executed) || [];
    const calls = live_ && executed.length
      ? h('div', { class: 'chips' }, executed.map((x) => h('span', { class: 'chip', title: x.error || '' },
        x.service + (x.value != null ? ` → ${x.value}` : '') + (x.status === 'ok' ? ' ✓' : ` (${x.status === 'skipped' ? 'sprunget over' : 'fejl'})`))))
      : c && c.calls && c.calls.length
        ? h('div', { class: 'chips' }, c.calls.map((x) => h('span', { class: 'chip' }, x.service + (x.value != null ? ` → ${x.value}` : ''))))
        : h('div', { class: 'hint' }, 'Ingen kald lige nu.');
    const callsCard = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, live_ ? 'Kaldte nu' : 'Ville kalde nu'),
        h('span', { class: 'hint' }, live_ ? 'Produktion: add-on’en styrer laderen og bilen' : 'Dry-run: intet sendes til laderen eller bilen')),
      calls,
      c ? h('dl', { class: 'kv', style: { marginTop: '14px' } },
        h('dt', {}, 'Soloverskud'), h('dd', {}, c.surplus_w != null ? fmt(c.surplus_w / 1000, 2) + ' kW' : '–'),
        h('dt', {}, 'Sol nu'), h('dd', {}, fmt((live.pv_w || 0) / 1000, 2) + ' kW'),
        h('dt', {}, 'Husforbrug (uden bil)'), h('dd', {}, fmt(Math.max(0, (live.load_w || 0) - (live.charger_w || 0)) / 1000, 2) + ' kW'),
        h('dt', {}, 'Husbatteri'), h('dd', {}, pct(live.house_soc)),
        h('dt', {}, 'Salgspris nu'), h('dd', {}, live.sell != null ? fmt(live.sell, 2) + ' kr' : '–'),
        h('dt', {}, 'Siden statusskift'), h('dd', {}, live.secs_since_status != null && live.secs_since_status < 9999 ? Math.round(live.secs_since_status / 60) + ' min' : '–'),
        h('dt', {}, 'Opdateret'), h('dd', {}, c.updated ? c.updated.slice(11, 16) : '–')) : null);

    const chartBox = h('div', { class: 'chart' });
    const rows = p ? p.plan : [];
    const showChart = carInfo && carInfo.connected && rows.length;
    const chartCard = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Bilens plan'), h('span', { class: 'hint' }, showChart ? `Tilsluttet frem til ${carUntil(carInfo).label} (antaget)` : '')),
      showChart ? [chartBox, h('div', { class: 'stack', style: { gap: '6px', marginTop: '10px' } },
        legend([{ color: C.buy, label: 'Købspris', line: true }, { color: C.sell, label: 'Salgspris', line: true }]),
        legend([{ color: C.car, label: 'Net → bil' }, { color: C.sun, label: 'Sol → bil' }, { color: C.batt, label: 'Husbatteri → bil' }]))]
        : h('div', { class: 'empty' }, 'Bilen er ikke tilsluttet. Planen tager den med, så snart laderen melder den tilsluttet.'));

    const log = (state.car && state.car.log) || [];
    const logCard = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Styringens log'), h('span', { class: 'hint' }, 'Kun ændringer · nyeste først')),
      log.length ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Tid', 'Ville', 'A', 'Begrundelse', 'Lader', 'Sat A', 'Bil %', 'Kald'].map((t) => h('th', {}, t)))),
        h('tbody', {}, log.slice().reverse().slice(0, 150).map((e) => h('tr', {},
          h('td', {}, e.updated ? dayName(e.updated.slice(0, 10), { weekday: 'short' }) + ' ' + e.updated.slice(11, 16) : ''),
          h('td', {}, CAR_ACTIONS[e.action] || e.action), h('td', {}, e.target_amps ?? ''),
          h('td', { style: { textAlign: 'left', whiteSpace: 'normal', minWidth: '200px' } }, e.reason_da || translateReason(e.reason)),
          h('td', {}, CHARGER_STATUS[e.live?.status] || e.live?.status || ''), h('td', {}, e.live?.amps ?? ''),
          h('td', {}, e.live?.car_soc != null ? fmt(e.live.car_soc, 0) : ''),
          h('td', { style: { textAlign: 'left', whiteSpace: 'normal' } }, callCell(e)))))))
        : h('div', { class: 'empty' }, 'Ingen hændelser endnu.'));

    const carWarn = c && c.warning ? h('div', { class: 'banner error' }, icon.error(), h('div', {}, c.warning)) : null;
    root.replaceChildren(h('div', { class: 'stack' }, carWarn, tiles, h('div', { class: 'grid-2' }, chartCard, callsCard), logCard));

    if (showChart) {
      const end = rows.findIndex((r) => r.car_grid === undefined || new Date(r.t) >= new Date(carUntil(carInfo).at));
      const crow = rows.slice(0, Math.max(2, end < 0 ? rows.length : Math.min(rows.length, end + 2)));
      timeChart(chartBox, {
        rows: crow, endLabels: true, label: 'Bilens planlagte opladning',
        tooltip: (i) => {
          const r = crow[i];
          return h('div', {}, h('div', { class: 'tt-head' }, `${dayName(dayKey(r.t))} · ${hhmm(r.t)}–${addHour(r.t)}`),
            ttSec('Pris, kr/kWh' + (r.forecast_price ? ' (estimeret)' : '')), ttRow(C.buy, 'Køb', fmt(r.buy, 2)), ttRow(C.sell, 'Salg', fmt(r.sell, 2)),
            ttSec('Til bilen, kWh'), ttRow(C.car, 'Fra nettet', fmt(r.car_grid, 2), ''), ttRow(C.sun, 'Fra sol', fmt(r.car_pv, 2), ''),
            r.car_bat > 0.005 ? ttRow(C.batt, 'Fra husbatteri', fmt(r.car_bat, 2), '') : null,
            ttSec('Ved timens slut'), ttRow(C.car, 'Bil', pct(r.car_soc)));
        },
        panels: [
          { kind: 'price', title: 'Pris (kr/kWh)', height: 80, lines: [{ label: 'Køb', color: C.buy, get: (r) => r.buy }, { label: 'Salg', color: C.sell, get: (r) => r.sell }] },
          { kind: 'energy', title: 'Til bilen pr. time (kWh)', height: 120,
            up: [{ label: 'Husbatteri → bil', color: C.batt, get: (r) => perH(r, r.car_bat) }, { label: 'Sol → bil', color: C.sun, get: (r) => perH(r, r.car_pv) }, { label: 'Net → bil', color: C.car, get: (r) => perH(r, r.car_grid) }],
            down: [] },
          { kind: 'soc', title: 'Bilens batteri (%)', height: 100,
            refs: [{ v: 42, label: 'minimum 42 %', color: C.muted }, { v: 90, label: 'maks 90 %', color: C.muted }],
            lines: [{ label: 'Bil', short: 'Bil', color: C.car, start: carInfo.soc_pct, get: (r) => r.car_soc }] },
        ],
      });
    }
  }

  // ---------------------------------------------------------------- Evaluation tab
  function renderEval() {
    const root = $('#tab-evaluering');
    const ev = state.evalData;
    const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Periode' },
      [7, 14, 30].map((d) => h('button', { type: 'button', 'aria-pressed': String(state.evalDays === d), onclick: () => { state.evalDays = d; store.set('evalDays', d); loadEval(true); } }, `${d} dage`)));
    const filters = h('div', { class: 'filters' }, seg, h('span', { class: 'hint' }, 'Gælder alt herunder'));
    if (!ev) { root.replaceChildren(h('div', { class: 'stack' }, filters, h('div', { class: 'card' }, h('div', { class: 'empty' }, 'Henter evaluering…')))); return; }
    const days = ev.days || [];
    const s = ev.summary || {};
    if (!days.length) {
      root.replaceChildren(h('div', { class: 'stack' }, filters, h('div', { class: 'card' }, h('div', { class: 'empty' },
        'Ingen data endnu. Evalueringen bygger på planlægningsloggen og kræver mindst to cyklusser (15 min) med tællere.'))));
      return;
    }
    const paid = days.filter((d) => d.avg_buy_paid != null);
    const avgPaid = paid.length ? paid.reduce((a, d) => a + d.avg_buy_paid * d.import_kwh, 0) / Math.max(1e-9, paid.reduce((a, d) => a + d.import_kwh, 0)) : null;
    const mk = days.filter((d) => d.avg_buy_market != null);
    const avgMarket = mk.length ? mk.reduce((a, d) => a + d.avg_buy_market * d.hours, 0) / mk.reduce((a, d) => a + d.hours, 0) : null;

    const tiles = h('div', { class: 'tiles' },
      tile('Netudgift', h('span', {}, fmt(s.cost_dkk, 0), h('small', {}, ' kr')), `${fmt(s.import_kwh, 0)} kWh købt · ${fmt(s.export_kwh, 0)} kWh solgt over ${s.days} dage`),
      tile('Betalt pr. kWh', h('span', {}, fmt(avgPaid, 2), h('small', {}, ' kr')),
        avgPaid != null && avgMarket != null ? `gns. pris i perioden ${fmt(avgMarket, 2)} kr · ${avgPaid <= avgMarket ? 'køber i billigere timer end gennemsnittet' : 'køber i dyrere timer end gennemsnittet'}` : ''),
      tile('Enighed med Node-RED', h('span', {}, s.agreement_pct != null ? fmt(s.agreement_pct, 0) : '–', h('small', {}, ' %')), 'andel af tiden hvor planneren ville samme tilstand'),
      tile('Solprognose', h('span', {}, s.pv_bias_pct != null ? fmtSigned(s.pv_bias_pct, 0) : '–', h('small', {}, ' %')),
        s.pv_bias_pct == null ? 'for lidt sol i perioden til at vurdere'
          : `${s.pv_bias_pct > 0 ? 'over' : 'under'}vurderet · foreslået pv_p10_weight ${s.suggested_p10_weight ?? '–'} (nu ${s.current_p10_weight ?? '–'})`));

    const costBox = h('div', { class: 'chart' });
    const costCard = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, 'Netudgift pr. dag (kr)'), h('span', { class: 'hint' }, 'Købt × købspris − solgt × salgspris, det der faktisk skete')), costBox);

    const stripBox = h('div', { class: 'chart' });
    const stripCard = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Planner mod Node-RED'), h('span', { class: 'hint' }, 'Hvad planneren ville, og hvad registrene faktisk stod på')),
      stripBox, h('div', { style: { marginTop: '10px' } }, legend([...Object.values(CATS).map((c) => ({ color: c.color, label: c.label })), { color: 'var(--ink-2)', label: 'Uenige' }])));

    const pvBox = h('div', { class: 'chart' });
    const pvCard = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Sol: faktisk mod prognose (kWh)'), h('span', { class: 'hint' }, 'Prognosen er den planen brugte (p50 blandet med p10)')),
      pvBox, h('div', { style: { marginTop: '10px' } }, legend([{ color: C.batt, label: 'Faktisk' }, { color: C.sun, label: 'Prognose' }])));

    const table = h('table', {},
      h('thead', {}, h('tr', {}, ['Dag', 'Købt', 'Solgt', 'Netudgift', 'Betalt/kWh', 'Gns. pris', 'Batteri ind/ud', 'Sol faktisk', 'Sol prognose', 'Forbrug*', 'Forbrug prognose', 'Enighed'].map((t) => h('th', {}, t)))),
      h('tbody', {}, days.slice().reverse().map((d) => h('tr', {},
        h('td', {}, dayName(d.day)), h('td', {}, fmt(d.import_kwh, 1)), h('td', {}, fmt(d.export_kwh, 1)), h('td', {}, fmt(d.cost_dkk, 2) + ' kr'),
        h('td', {}, fmt(d.avg_buy_paid, 2)), h('td', {}, fmt(d.avg_buy_market, 2)),
        h('td', {}, `${fmt(d.battery_in_kwh, 1)} / ${fmt(d.battery_out_kwh, 1)}`),
        h('td', {}, fmt(d.pv_actual_kwh, 1)), h('td', {}, fmt(d.pv_plan_kwh, 1)),
        h('td', {}, fmt(d.load_actual_kwh, 1)), h('td', {}, fmt(d.load_pred_kwh, 1)),
        h('td', {}, d.agreement_pct != null ? fmt(d.agreement_pct, 0) + ' %' : '–')))));
    const tableCard = h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Pr. dag'), h('span', { class: 'hint' }, 'kWh og kr · *forbrug uden bil, anslået ud fra tællerne og billaderens effekt')),
      h('div', { class: 'table-wrap' }, table));

    const dec = (ev.decisions || []).slice().reverse();
    const decCard = h('div', { class: 'card' }, h('details', {},
      h('summary', {}, `Seneste beslutninger (${dec.length})`),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Tid', 'SoC', 'Planner', 'Mål', 'Gulv', 'Rate', 'Battery first', 'Net-lad', 'Max', 'Min', 'Køb'].map((t) => h('th', {}, t)))),
        h('tbody', {}, dec.map((e) => {
          const mm = MODES[e.mode] || MODES.hold, sp = e.setpoints || {}, a = e.actual_registers || {};
          return h('tr', {},
            h('td', {}, dayName(e.updated.slice(0, 10), { weekday: 'short' }) + ' ' + e.updated.slice(11, 16)), h('td', {}, pct(e.soc_pct)),
            h('td', {}, h('span', { class: 'mode-dot', style: { background: mm.color } }), mm.label),
            h('td', {}, sp.max_soc_pct ?? '–'), h('td', {}, sp.min_soc_pct ?? '–'), h('td', {}, sp.charge_rate_pct ?? '–'),
            h('td', {}, a.reg_battery_first ?? '–'), h('td', {}, a.reg_grid_charge ?? '–'), h('td', {}, a.reg_max_soc ?? '–'), h('td', {}, a.reg_min_soc ?? '–'),
            h('td', {}, e.price_now ? fmt(e.price_now.buy, 2) : '–'));
        }))))));

    root.replaceChildren(h('div', { class: 'stack' }, filters, tiles, costCard, stripCard, pvCard, tableCard, decCard));

    const shortDay = (k) => dayName(k, { day: 'numeric', month: 'numeric' });
    columnChart(costBox, {
      label: 'Netudgift pr. dag', valueLabels: true, valueDigits: 0, height: 210,
      items: days.map((d) => ({ label: shortDay(d.day), values: [d.cost_dkk], d })),
      series: [{ label: 'Netudgift', color: C.batt }],
      tooltip: (it) => h('div', {}, h('div', { class: 'tt-head' }, dayName(it.d.day, { weekday: 'long', day: 'numeric', month: 'long' })),
        ttRow(C.batt, 'Netudgift', fmt(it.d.cost_dkk, 2) + ' kr', ''), ttRow(C.ink2, 'Købt', fmt(it.d.import_kwh, 1) + ' kWh'), ttRow(C.ink2, 'Solgt', fmt(it.d.export_kwh, 1) + ' kWh'),
        ttRow(C.ink2, 'Betalt pr. kWh', fmt(it.d.avg_buy_paid, 2) + ' kr'), ttRow(C.ink2, 'Gns. købspris', fmt(it.d.avg_buy_market, 2) + ' kr'),
        it.d.hours < 23 ? h('div', { class: 'tt-sec' }, `Kun ${fmt(it.d.hours, 1)} timers data`) : null),
    });
    stripChart(stripBox, ev.timeline || []);
    columnChart(pvBox, {
      label: 'Sol faktisk mod prognose', height: 190, empty: 'Ingen soldata endnu.',
      items: days.filter((d) => d.pv_actual_kwh != null).map((d) => ({ label: shortDay(d.day), values: [d.pv_actual_kwh, d.pv_plan_kwh], d })),
      series: [{ label: 'Faktisk', color: C.batt }, { label: 'Prognose', color: C.sun }],
      tooltip: (it) => h('div', {}, h('div', { class: 'tt-head' }, dayName(it.d.day, { weekday: 'long', day: 'numeric', month: 'long' })),
        ttRow(C.batt, 'Faktisk', fmt(it.d.pv_actual_kwh, 1) + ' kWh', ''), ttRow(C.sun, 'Prognose (brugt)', fmt(it.d.pv_plan_kwh, 1) + ' kWh', ''),
        ttRow(C.ink2, 'p50', fmt(it.d.pv_p50_kwh, 1) + ' kWh'), ttRow(C.ink2, 'p10', fmt(it.d.pv_p10_kwh, 1) + ' kWh')),
    });
  }

  // ---------------------------------------------------------------- Economy tab
  const ECON_PERIODS = [[1, 'I dag'], [7, '7 dage'], [30, '30 dage'], [90, '90 dage'], [365, '1 år']];

  function econLabel(t, days, i) {
    const hh = t.slice(11, 13);
    if (days <= 1) return parseInt(hh, 10) % 3 === 0 ? hh : '';
    if (!(hh === '00' || i === 0)) return '';
    const d = new Date(t.slice(0, 10) + 'T12:00:00');
    return ['søn', 'man', 'tir', 'ons', 'tor', 'fre', 'lør'][d.getDay()] + ' ' + d.getDate() + '.';
  }

  function renderEconomy() {
    const root = $('#tab-okonomi');
    const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Periode' },
      ECON_PERIODS.map(([d, l]) => h('button', { type: 'button', 'aria-pressed': String(state.econDays === d),
        onclick: () => { state.econDays = d; store.set('econDays', d); loadEcon(true); } }, l)));
    const filters = h('div', { class: 'filters' }, seg, h('span', { class: 'hint' }, 'Fra HA’s statistik · gælder alt herunder'));
    const ev = state.econData;
    if (!ev) {
      root.replaceChildren(h('div', { class: 'stack' }, filters, h('div', { class: 'card' },
        h('div', { class: 'empty' }, state.econError ? 'Kunne ikke hente statistik: ' + state.econError : 'Henter statistik…'))));
      return;
    }
    const t = ev.totals || {};
    const banners = [];
    const warns = [];
    if ((ev.missing || []).length) warns.push('Ingen statistik for: ' + ev.missing.join(', ') + '. Tallene er ufuldstændige.');
    if (ev.skipped_hours) warns.push(`${ev.skipped_hours} timer mangler tællere eller pris og er ikke talt med.`);
    if (t.fallback_price_hours) warns.push(`${t.fallback_price_hours} timer bruger priser fra add-on’ens log (prisstatistik manglede).`);
    if (warns.length) banners.push(h('div', { class: 'banner' }, icon.warn(), h('ul', {}, warns.map((w) => h('li', {}, w)))));
    if (!t.hours) {
      root.replaceChildren(h('div', { class: 'stack' }, filters, banners, h('div', { class: 'card' }, h('div', { class: 'empty' }, 'Ingen timer med data i perioden.'))));
      return;
    }
    const kr = (v) => h('span', {}, fmt(v, Math.abs(v) < 10 ? 2 : 0), h('small', {}, ' kr'));
    const tiles = h('div', { class: 'tiles four' },
      tile('Anlægsværdi', kr(t.value_dkk), `sparet: uden anlæg ${fmt(t.without_dkk, 0)} kr, betalt ${fmt(t.net_dkk, 0)} kr`),
      tile('Netudgift', kr(t.net_dkk), `købt for ${fmt(t.bought_dkk, 0)} kr · solgt for ${fmt(t.sold_dkk, 0)} kr`),
      tile('Solens bidrag', kr(t.solar_value_dkk), `${fmt(t.solar_kwh, 0)} kWh produceret · ${t.solar_self_use_pct != null ? fmt(t.solar_self_use_pct, 0) + ' % brugt selv' : ''}`),
      tile('Batteriets bidrag', kr(t.battery_value_dkk), `${fmt(t.battery_out_kwh, 0)} kWh brugt · ${fmt(t.battery_in_kwh, 0)} kWh ladet`),
      tile('Solgt for', kr(t.sold_dkk), `${fmt(t.export_kwh, 0)} kWh${t.export_kwh > 0.05 ? ' à ' + fmt(t.sold_dkk / t.export_kwh, 2) + ' kr' : ''}`),
      tile('Købt for', kr(t.bought_dkk), `${fmt(t.import_kwh, 0)} kWh · betalt ${fmt(t.avg_buy_paid, 2)} kr/kWh mod gns. ${fmt(t.avg_buy_market, 2)}`),
      tile('Bilen', kr(t.car_dkk), `${fmt(t.car_kwh, 0)} kWh, regnet ved købsprisen`),
      tile('Selvforsyning', h('span', {}, fmt(t.self_sufficiency_pct, 0), h('small', {}, ' %')), `af ${fmt(t.consumption_kwh, 0)} kWh forbrug dækket af sol og batteri`));

    const hourly = !!ev.hours;
    const series = hourly ? ev.hours : ev.days;
    const unit = hourly ? 'time' : 'dag';
    const when = (r, i) => hourly ? econLabel(r.t, ev.period_days, i) : dayName(r.day, ev.period_days > 31 ? { day: 'numeric', month: 'numeric' } : { day: 'numeric', month: 'numeric' });
    const head = (r) => hourly ? `${dayName(r.t.slice(0, 10))} · ${r.t.slice(11, 16)}–${addHour(r.t)}` : dayName(r.day, { weekday: 'long', day: 'numeric', month: 'long' });

    const valueBox = h('div', { class: 'chart' }), moneyBox = h('div', { class: 'chart' });
    const cumBox = h('div', { class: 'chart' }), energyBox = h('div', { class: 'chart' });
    const card = (title, hint, box, leg) => h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, title), hint ? h('span', { class: 'hint' }, hint) : null), box,
      leg ? h('div', { style: { marginTop: '10px' } }, legend(leg)) : null);

    const days = ev.days || [];
    const table = h('table', {},
      h('thead', {}, h('tr', {}, ['Dag', 'Anlægsværdi', 'Sol', 'Batteri', 'Netudgift', 'Købt for', 'Solgt for', 'Købt kWh', 'Solgt kWh', 'Sol kWh', 'Bil kr', 'Selvforsyning'].map((x) => h('th', {}, x)))),
      h('tbody', {}, days.slice().reverse().map((d) => h('tr', {},
        h('td', {}, dayName(d.day)), h('td', {}, fmt(d.value_dkk, 2)), h('td', {}, fmt(d.solar_value_dkk, 2)), h('td', {}, fmt(d.battery_value_dkk, 2)),
        h('td', {}, fmt(d.net_dkk, 2)), h('td', {}, fmt(d.bought_dkk, 2)), h('td', {}, fmt(d.sold_dkk, 2)),
        h('td', {}, fmt(d.import_kwh, 1)), h('td', {}, fmt(d.export_kwh, 1)), h('td', {}, fmt(d.solar_kwh, 1)), h('td', {}, fmt(d.car_dkk, 2)),
        h('td', {}, d.self_sufficiency_pct != null ? fmt(d.self_sufficiency_pct, 0) + ' %' : '–')))));

    root.replaceChildren(h('div', { class: 'stack' }, filters, banners, tiles,
      card(`Anlægsværdi pr. ${unit} (kr)`, 'Hvad sol og batteri sparede i forhold til at købe alt fra nettet', valueBox,
        [{ color: C.sun, label: 'Sol' }, { color: C.batt, label: 'Batteri (negativ når det lader fra nettet)' }]),
      h('div', { class: 'grid-2' },
        card(`Penge ind og ud pr. ${unit} (kr)`, 'Solgt op, købt ned', moneyBox, [{ color: C.export, label: 'Solgt for' }, { color: C.gridbat, label: 'Købt for' }]),
        card('Anlægsværdi akkumuleret (kr)', '', cumBox, null)),
      card(`Energi pr. ${unit} (kWh)`, '', energyBox, [{ color: C.sun, label: 'Sol produceret' }, { color: C.gridbat, label: 'Købt' }, { color: C.export, label: 'Solgt' }]),
      h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, 'Pr. dag'), h('span', { class: 'hint' }, 'kr og kWh · bilen regnet ved købspris')),
        h('div', { class: 'table-wrap' }, table))));

    const valueTip = (r) => h('div', {}, h('div', { class: 'tt-head' }, head(r)),
      ttRow(C.sun, 'Sol', fmt(r.solar_value_dkk, 2) + ' kr', ''), ttRow(C.batt, 'Batteri', fmt(r.battery_value_dkk, 2) + ' kr', ''),
      ttSec('I alt'), ttRow(C.ink2, 'Anlægsværdi', fmt(r.value_dkk, 2) + ' kr'), ttRow(C.ink2, 'Uden anlæg', fmt(r.without_dkk, 2) + ' kr'),
      ttRow(C.ink2, 'Betalt (netto)', fmt(r.net_dkk, 2) + ' kr'),
      hourly ? ttRow(C.ink2, 'Pris køb / salg', `${fmt(r.buy, 2)} / ${fmt(r.sell, 2)}`) : null);
    const opts = { labelEvery: hourly ? 1 : undefined };
    columnChart(valueBox, { ...opts, label: 'Anlægsværdi', height: 220, stacked: true,
      items: series.map((r, i) => ({ label: when(r, i), values: [r.solar_value_dkk, r.battery_value_dkk], r })),
      series: [{ label: 'Sol', color: C.sun }, { label: 'Batteri', color: C.batt }], tooltip: (it) => valueTip(it.r) });
    columnChart(moneyBox, { ...opts, label: 'Penge ind og ud', height: 190, stacked: true,
      items: series.map((r, i) => ({ label: when(r, i), values: [r.sold_dkk, -r.bought_dkk], r })),
      series: [{ label: 'Solgt for', color: C.export }, { label: 'Købt for', color: C.gridbat }],
      tooltip: (it) => h('div', {}, h('div', { class: 'tt-head' }, head(it.r)),
        ttRow(C.export, 'Solgt for', fmt(it.r.sold_dkk, 2) + ' kr', ''), ttRow(C.gridbat, 'Købt for', fmt(it.r.bought_dkk, 2) + ' kr', ''),
        ttRow(C.ink2, 'Netto', fmt(it.r.net_dkk, 2) + ' kr')) });
    let acc = 0;
    lineChart(cumBox, { label: 'Anlægsværdi akkumuleret', height: 190, color: C.batt, unit: 'kr',
      items: series.map((r, i) => { acc += r.value_dkk; return { label: when(r, i), value: acc, r }; }),
      tooltip: (it) => h('div', {}, h('div', { class: 'tt-head' }, head(it.r)), ttRow(C.batt, 'Akkumuleret', fmt(it.value, 2) + ' kr'),
        ttRow(C.ink2, `Denne ${unit}`, fmt(it.r.value_dkk, 2) + ' kr')) });
    columnChart(energyBox, { ...opts, label: 'Energi', height: 190,
      items: series.map((r, i) => ({ label: when(r, i), values: [r.solar_kwh, r.import_kwh, r.export_kwh], r })),
      series: [{ label: 'Sol', color: C.sun }, { label: 'Købt', color: C.gridbat }, { label: 'Solgt', color: C.export }],
      tooltip: (it) => h('div', {}, h('div', { class: 'tt-head' }, head(it.r)),
        ttRow(C.sun, 'Sol produceret', fmt(it.r.solar_kwh, 2) + ' kWh', ''), ttRow(C.gridbat, 'Købt', fmt(it.r.import_kwh, 2) + ' kWh', ''),
        ttRow(C.export, 'Solgt', fmt(it.r.export_kwh, 2) + ' kWh', ''), ttRow(C.ink2, 'Forbrug', fmt(it.r.consumption_kwh, 2) + ' kWh'),
        it.r.car_kwh > 0.05 ? ttRow(C.ink2, 'Heraf bil', fmt(it.r.car_kwh, 2) + ' kWh') : null) });
  }

  async function loadEcon(dim) {
    const root = $('#tab-okonomi');
    if (dim) root.classList.add('loading');
    try { state.econData = await getJSON('api/economy?days=' + state.econDays); state.econError = null; }
    catch (e) { state.econError = e.message; if (!state.econData) state.econData = null; }
    root.classList.remove('loading');
    if (state.tab === 'okonomi') renderEconomy();
  }

  // ---------------------------------------------------------------- loading & tabs
  function setUpdated() {
    const p = state.plan;
    const pill = $('#mode-pill');
    const prod = (p && p.production) || {};
    const parts = [prod.battery ? 'batteri' : null, prod.car ? 'bil' : null].filter(Boolean);
    pill.textContent = parts.length ? 'Produktion: ' + parts.join(' + ') : 'Dry-run';
    pill.classList.toggle('live', parts.length > 0);
    pill.title = parts.length
      ? 'Add-on’en styrer ' + parts.join(' og ') + '. Slå hjælperen fra for at gå tilbage til dry-run.'
      : 'Planen beregnes og vises, men intet skrives til inverteren eller laderen';
    $('#updated').textContent = p ? `Opdateret ${p.updated.slice(11, 16)} · ${p.horizon_hours} timers horisont` : (state.planError || '');
  }
  async function loadPlan() {
    try { state.plan = await getJSON('api/plan'); state.planError = null; } catch (e) { state.planError = e.message; }
    try { state.car = await getJSON('api/car'); } catch (e) { /* keep the last */ }
    setUpdated();
    if (state.tab === 'plan') renderPlan();
    if (state.tab === 'bil') renderCar();
  }
  async function loadCar() {
    try { state.car = await getJSON('api/car'); } catch (e) { /* keep the last */ }
    if (state.tab === 'bil') renderCar();
  }
  async function loadEval(dim) {
    const root = $('#tab-evaluering');
    if (dim) root.classList.add('loading');
    try { state.evalData = await getJSON('api/evaluation?days=' + state.evalDays); } catch (e) { toast('Evaluering: ' + e.message); }
    root.classList.remove('loading');
    if (state.tab === 'evaluering') renderEval();
  }
  function selectTab(name, push) {
    if (!['plan', 'bil', 'evaluering', 'okonomi'].includes(name)) name = 'plan';
    state.tab = name;
    document.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    document.querySelectorAll('.tab').forEach((s) => { s.hidden = s.id !== 'tab-' + name; });
    tip.hide();
    if (push) { try { history.replaceState(null, '', '#' + name); } catch (e) { /* sandboxed */ } }
    if (name === 'plan') renderPlan();
    if (name === 'bil') { renderCar(); loadCar(); }
    if (name === 'evaluering') { renderEval(); loadEval(!!state.evalData); }
    if (name === 'okonomi') { renderEconomy(); loadEcon(!!state.econData); }
  }
  document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => selectTab(b.dataset.tab, true)));
  $('#run').addEventListener('click', async () => {
    const btn = $('#run');
    btn.classList.add('busy'); btn.disabled = true;
    try { state.plan = await getJSON('api/run', { method: 'POST' }); state.planError = null; setUpdated(); selectTab(state.tab); toast('Ny plan beregnet'); } catch (e) { toast('Planlægning fejlede: ' + e.message); }
    btn.classList.remove('busy'); btn.disabled = false;
  });

  window.addEventListener('hashchange', () => selectTab((location.hash || '').slice(1)));
  selectTab((location.hash || '').slice(1) || 'plan');
  loadPlan();
  setInterval(loadPlan, 60000);
  setInterval(() => { if (state.tab === 'bil') loadCar(); }, 30000);
  setInterval(() => { if (state.tab === 'evaluering') loadEval(false); }, 300000);
})();
