/* fungi_plate: a numbered mycological plate of the fungi recorded round the
   panel this month.

   The server hands over the species already ranked, each with its picture as
   a data URI (a photograph converted for the panel, or a painting once it has
   landed), so this file only lays the page out: the title, a ruled frame, a
   grid of numbered specimens and a key at the foot; or, in the other layout,
   one ruled box per species with a caption bar. The page is the cell, paper
   edge to edge. */

const esc = (s) =>
  String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

const PAPER = { cream: '#f3ead6', white: '#ffffff', aged: '#e9dcc0' };

/* How the specimens sit: by count and by the shape of the cell. A portrait
   cell stacks more rows than columns; the usual counts have a fixed shape so
   a plate of six is always three by two (or two by three), never a ragged
   last row. */
function gridOf(n, w, h) {
  const portrait = h > w * 1.15;
  const fixed = {
    1: [1, 1],
    2: portrait ? [1, 2] : [2, 1],
    3: portrait ? [1, 3] : [3, 1],
    4: [2, 2],
    6: portrait ? [2, 3] : [3, 2],
    8: portrait ? [2, 4] : [4, 2],
    9: [3, 3],
  }[n];
  if (fixed) return { cols: fixed[0], rows: fixed[1] };
  const cols = Math.max(1, Math.min(n, Math.round(Math.sqrt((n * w) / h))));
  return { cols, rows: Math.ceil(n / cols) };
}

/* The name a species goes by on the plate: the common name, else the Latin
   (in italics, so it reads as what it is). */
function nameOf(s) {
  return s.name ? esc(s.name) : `<i class="fp-latin">${esc(s.sci)}</i>`;
}

function picture(s) {
  const p = s.pic || {};
  if (!p.src) {
    return '<div class="fp-pic fp-none"><i class="ph-bold ph-flower-lotus" aria-hidden="true"></i></div>';
  }
  const kind = p.kind === 'painting' ? 'fp-paint' : 'fp-photo';
  return `<div class="fp-pic ${kind}"><div class="fp-img"><img src="${p.src}" alt="" decoding="sync"></div></div>`;
}

function subLine(d, size) {
  const bits = [esc(d.plate).toUpperCase(), esc(d.window_words).toUpperCase(), esc(d.within).toUpperCase()];
  if (size === 'lg' && d.species_total > 0) {
    bits.push(`${d.species_total} SPECIES RECORDED`);
  }
  return bits.join(' · ');
}

function plateBody(d, species, size) {
  const slots = species.map((s) => `
    <figure class="fp-slot">
      ${picture(s)}
      <figcaption class="fp-cap">
        <b class="fp-num">${s.n}</b>
        <span class="fp-name">${nameOf(s)}</span>
      </figcaption>
    </figure>`).join('');
  const key = species.map((s) => `${s.n}. ${nameOf(s)}`).join(', ');
  return `
    <div class="fp-frame">
      <div class="fp-sub">${subLine(d, size)}</div>
      <div class="fp-grid">${slots}</div>
      <div class="fp-key">${key}</div>
    </div>`;
}

function boxesBody(d, species, size) {
  const boxes = species.map((s) => {
    const records = `${s.count} record${s.count === 1 ? '' : 's'}`;
    const latin = s.name && s.sci ? `<i class="fp-latin">${esc(s.sci)}</i> · ` : '';
    return `
    <figure class="fp-box">
      ${picture(s)}
      <figcaption class="fp-bar">
        <div class="fp-bar-name">${nameOf(s)}</div>
        <div class="fp-bar-sub">${latin}<span class="fp-bar-n">${records}</span></div>
      </figcaption>
    </figure>`;
  }).join('');
  return `
    <div class="fp-sub fp-sub-boxes">${subLine(d, size)}</div>
    <div class="fp-grid fp-grid-boxes">${boxes}</div>`;
}

function foot(d) {
  const bits = [...(d.notices || []), ...(d.status || [])];
  if (d.credit_line) bits.push(d.credit_line);
  return bits.length ? `<div class="fp-foot">${bits.map(esc).join(' · ')}</div>` : '';
}

function emptyBody(d) {
  return `
    <div class="fp-frame fp-frame-empty">
      <div class="fp-sub">${esc(d.plate).toUpperCase()} · ${esc(d.window_words).toUpperCase()}</div>
      <div class="fp-empty">
        <i class="ph-bold ph-flower-lotus" aria-hidden="true"></i>
        <div class="fp-empty-t">${esc(d.empty)}</div>
      </div>
    </div>`;
}

function errorBody(msg) {
  return `
    <div class="fp-err">
      <i class="ph-bold ph-warning-circle" aria-hidden="true"></i>
      <div>
        <div class="fp-err-t">Fungi Plate</div>
        <div class="fp-err-m">${esc(msg)}</div>
      </div>
    </div>`;
}

export default async function render(shadow, ctx) {
  const d = (ctx && ctx.data) || {};
  const cell = (ctx && ctx.cell) || {};
  const size = cell.size || 'md';
  const w = Number(cell.w) || 640;
  const h = Number(cell.h) || 400;
  const paper = PAPER[d.paper] || PAPER.cream;
  const panel = d.panel || 'colour';
  const layout = d.layout === 'boxes' ? 'boxes' : 'plate';

  /* A small cell shows one or two specimens and the title only; a half panel
     at most six, so nothing on it falls under about 11 px. */
  const cap = size === 'sm' ? (h > w ? 1 : 2) : size === 'md' ? 6 : 99;
  const species = (d.species || []).slice(0, cap);
  const grid = gridOf(species.length, w, h);

  let body;
  if (d.error) body = errorBody(d.error);
  else if (!species.length) body = emptyBody(d);
  else if (layout === 'boxes') body = boxesBody(d, species, size);
  else body = plateBody(d, species, size);

  const title = d.error ? '' : `<div class="fp-title">${esc(d.title)}</div>`;

  shadow.innerHTML = `
    <link rel="stylesheet" href="/static/style/spectra-widgets.css">
    <link rel="stylesheet" href="/static/icons/phosphor/bold/style.css">
    <link rel="stylesheet" href="/plugins/fungi_plate/client.css">
    <div class="fp size-${esc(size)} lv-${esc(panel)} lay-${layout}${d.error ? ' is-error' : ''}"
         style="--paper:${paper};--cols:${grid.cols};--rows:${grid.rows}">
      ${title}
      ${body}
      ${d.error || !species.length ? '' : foot(d)}
    </div>`;
}
