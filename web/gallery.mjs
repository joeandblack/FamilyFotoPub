const layouts = new WeakMap();
const tooltips = new WeakSet();

function setupFilenameTooltip(grid) {
  if (tooltips.has(grid)) return;
  tooltips.add(grid);
  const doc=grid.ownerDocument,tip=doc.createElement('div');
  tip.className='filename-tooltip';tip.id='galleryFilenameTooltip';tip.role='tooltip';tip.hidden=true;
  doc.body.append(tip);
  let active=null;
  const hide=()=>{tip.hidden=true;active?.removeAttribute('aria-describedby');active=null;};
  const show=tile=>{
    hide();if(!tile?.dataset.filename)return;
    active=tile;tip.textContent=tile.dataset.filename;tip.hidden=false;
    tile.setAttribute('aria-describedby',tip.id);
    const r=tile.getBoundingClientRect(),box=tip.getBoundingClientRect();
    tip.style.left=Math.max(8,Math.min(r.left,doc.documentElement.clientWidth-box.width-8))+'px';
    tip.style.top=Math.max(8,r.bottom+8+box.height>doc.documentElement.clientHeight?r.top-box.height-8:r.bottom+8)+'px';
  };
  grid.addEventListener('pointerover',e=>{if(e.pointerType!=='touch'&&!e.buttons)show(e.target.closest('.tile'));});
  grid.addEventListener('pointerout',e=>{if(!active?.contains(e.relatedTarget))hide();});
  grid.addEventListener('focusin',e=>show(e.target.closest('.tile')));
  grid.addEventListener('focusout',hide);
  grid.addEventListener('pointerdown',hide);
  grid.addEventListener('scroll',hide);
  doc.addEventListener('keydown',e=>{if(e.key==='Escape')hide();});
  doc.defaultView.addEventListener('resize',hide);
}

export function updateGallery(grid, tiles) {
  setupFilenameTooltip(grid);
  const previous = layouts.get(grid) || new Map();
  const next = new Map();
  for (const tile of tiles) {
    let entry = previous.get(tile.id);
    if (!entry || entry.html !== tile.html) {
      const template = grid.ownerDocument.createElement('template');
      template.innerHTML = tile.html;
      const node = template.content.firstElementChild;
      if(node.title){node.dataset.filename=node.title;node.removeAttribute('title');}
      if (entry) entry.node.replaceWith(node);
      entry = { html: tile.html, node };
    }
    next.set(tile.id, entry);
  }
  for (const [id, entry] of previous) {
    if (!next.has(id)) entry.node.remove();
  }
  if (!tiles.length) {
    if (previous.size || !grid.firstElementChild) grid.innerHTML = '<div class="empty">No media in this view.</div>';
  } else {
    if (!previous.size) grid.replaceChildren();
    // Preserve loaded images, focus and scroll during progress polling.
    let cursor = grid.firstElementChild;
    for (const entry of next.values()) {
      if (cursor === entry.node) cursor = cursor.nextElementSibling;
      else grid.insertBefore(entry.node, cursor);
    }
  }
  layouts.set(grid, next);
}
