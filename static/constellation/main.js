import { loadUniverse, universe, conById, GALAXIES } from './data.js?v=6';
import { view, onViewChange, gotoUniverse, gotoGalaxy, gotoConstellation, gotoStar, goUp } from './state.js?v=6';
import { initRender, resizeRender, drawFrame, hitTest, onDataLoaded, resetCamera, zoomBy, panBy, rebuildLayouts } from './render.js?v=6';
import { bridgesOfCon } from './layout.js?v=6';

const mc = document.getElementById('mc');
const $ = id => document.getElementById(id);
let hovered = null, dragging = false, dragged = false, lastX = 0, lastY = 0;

function showPanel(item, category, conId) {
    $('p-cat').textContent = category;
    $('p-title').textContent = item.title || item.name || item.label || 'Memory';
    $('p-body').textContent = item.content || 'No additional detail.';
    $('p-meta').textContent = item.created_at ? `Importance ${item.importance || '-'} · ${item.created_at}` : '';
    $('p-date').textContent = item.source_session ? `Session: ${item.source_session}` : '';
    renderBridgeLinks(conId);
    $('panel').classList.add('visible');
}
function renderBridgeLinks(conId) {
    const box = $('p-bridges');
    box.replaceChildren();
    if (!conId) return;
    const label = document.createElement('div');
    label.className = 'p-bridges-label';
    label.textContent = '关联';
    box.appendChild(label);
    const rels = bridgesOfCon(conId);
    if (!rels.length) {
        const empty = document.createElement('div');
        empty.className = 'p-bridge-empty';
        empty.textContent = '暂无已发现的实体关系';
        box.appendChild(empty);
        return;
    }
    rels.forEach(br => {
        const other = conById(br.otherId);
        const btn = document.createElement('button');
        btn.className = 'p-bridge-link';
        const name = other ? other.label : br.otherId;
        btn.textContent = br.relation ? `${name} — ${br.relation}` : name;
        btn.onclick = () => gotoConstellation(br.otherId);
        box.appendChild(btn);
    });
}
function hidePanel() { $('panel').classList.remove('visible'); }
function renderBreadcrumb() {
    const items = [{ label: 'Universe', action: gotoUniverse, active: view.level === 'universe' }];
    if (view.galaxyId) items.push({ label: view.galaxyId, action: () => gotoGalaxy(view.galaxyId), active: view.level === 'galaxy' });
    if (view.conId) { const con = conById(view.conId); if (con) items.push({ label: con.label, action: () => gotoConstellation(con.id), active: view.level === 'constellation' || view.level === 'star' }); }
    $('breadcrumb').replaceChildren(...items.flatMap((item, index) => {
        const button = document.createElement('button'); button.className = `bc-item${item.active ? ' active' : ''}`; button.textContent = item.label; if (!item.active) button.onclick = item.action;
        return index ? [Object.assign(document.createElement('span'), { className: 'bc-sep', textContent: '›' }), button] : [button];
    }));
}
function initPills() {
    const nav = $('galaxy-pills'); nav.replaceChildren();
    [{ id: '', label: 'All' }, ...GALAXIES.map(g => ({ id: g.id, label: g.id }))].forEach(g => {
        const button = document.createElement('button'); button.className = 'gpill'; button.dataset.galaxy = g.id; button.textContent = g.label;
        button.onclick = () => g.id ? gotoGalaxy(g.id) : gotoUniverse(); nav.appendChild(button);
    });
}
function updatePills() { document.querySelectorAll('.gpill').forEach(b => b.classList.toggle('active', b.dataset.galaxy === (view.galaxyId || ''))); }
function tooltip(x, y, text) { const node = $('tt'); node.textContent = text; node.style.left = `${x + 14}px`; node.style.top = `${y + 14}px`; node.style.opacity = '1'; }
function hideTooltip() { $('tt').style.opacity = '0'; }

onViewChange(() => {
    resetCamera(); rebuildLayouts(); renderBreadcrumb(); updatePills();
    const con = view.conId ? conById(view.conId) : null;
    if (view.level === 'constellation' && con) {
        showPanel({ title: con.label, content: `${con.stars.length} 段记忆 · 点击星点查看原始内容` }, con.galaxyLabel, con.id);
    } else {
        hidePanel();
    }
});
mc.addEventListener('wheel', event => { event.preventDefault(); zoomBy(event.deltaY < 0 ? 1.11 : .9); }, { passive: false });
mc.addEventListener('mousedown', event => { dragging = true; dragged = false; lastX = event.clientX; lastY = event.clientY; });
window.addEventListener('mousemove', event => {
    if (dragging) { const dx = event.clientX - lastX, dy = event.clientY - lastY; if (Math.abs(dx) + Math.abs(dy) > 3) dragged = true; panBy(dx, dy); lastX = event.clientX; lastY = event.clientY; }
    hovered = hitTest(event.clientX, event.clientY); mc.style.cursor = hovered ? 'pointer' : 'grab';
    if (!hovered) return hideTooltip();
    tooltip(event.clientX, event.clientY, hovered.type === 'star' ? hovered.star.title : hovered.type === 'con' ? hovered.con.label : hovered.name || hovered.id);
});
window.addEventListener('mouseup', () => { dragging = false; });
mc.addEventListener('click', event => {
    if (dragged) { dragged = false; return; }
    const hit = hitTest(event.clientX, event.clientY); if (!hit) return goUp();
    if (hit.type === 'galaxy') gotoGalaxy(hit.id);
    else if (hit.type === 'con') gotoConstellation(hit.id);
    else if (hit.type === 'star') { gotoStar(hit.id, view.conId); showPanel(hit.star, 'Memory fragment', hit.star.conId); }
    else if (hit.type === 'core') showPanel(hit.ent || { name: hit.name }, 'Memory core');
});
window.addEventListener('keydown', event => { if (event.key === 'Escape') view.level === 'universe' ? hidePanel() : goUp(); });
$('panel-close').onclick = hidePanel;

async function refresh() {
    try { await loadUniverse(); onDataLoaded(); $('tb-count').textContent = `${universe.totalFragments} fragments · ${universe.constellations.length} constellations`; renderBreadcrumb(); }
    catch (error) { $('tb-count').textContent = `Load failed: ${error.message}`; }
}
function loop(time = 0) { drawFrame(time / 1000, hovered); requestAnimationFrame(loop); }
initRender($('bg'), mc); initPills(); refresh(); loop();
window.addEventListener('resize', resizeRender);
