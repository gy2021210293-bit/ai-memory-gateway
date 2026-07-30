// Gateway adapter for the original Memory Constellations renderer.
// It deliberately reads the existing gateway API only; it does not create a second memory store.
const gatewayKey = new URLSearchParams(window.location.search).get('gateway_key') || '';
const AUTH = () => gatewayKey ? { headers: { 'X-Gateway-Key': gatewayKey } } : {};

export const GALAXIES = [
    { id: 'People', hue: 8, azimuth: -90, desc: 'People and pets' },
    { id: 'Places', hue: 215, azimuth: -18, desc: 'Places and locations' },
    { id: 'Projects', hue: 275, azimuth: 54, desc: 'Projects, organizations, and activities' },
    { id: 'Events', hue: 152, azimuth: 126, desc: 'Named events and experiences' },
    { id: 'Life', hue: 45, azimuth: 198, desc: 'Objects and other named subjects' },
];
export const GALAXY_BY_ID = Object.fromEntries(GALAXIES.map(g => [g.id, g]));

export function strHash(str) {
    let h = 2166136261;
    for (let i = 0; i < String(str).length; i++) { h ^= String(str).charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
}
export function mulberry32(seed) {
    let a = seed >>> 0;
    return () => { a |= 0; a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
export function hslToRgbStr(hue, sat, lit) {
    const s = sat / 100, l = lit / 100, c = (1 - Math.abs(2 * l - 1)) * s;
    const x = c * (1 - Math.abs(((hue / 60) % 2) - 1)), m = l - c / 2;
    let r = 0, g = 0, b = 0;
    if (hue < 60) [r, g, b] = [c, x, 0]; else if (hue < 120) [r, g, b] = [x, c, 0];
    else if (hue < 180) [r, g, b] = [0, c, x]; else if (hue < 240) [r, g, b] = [0, x, c];
    else if (hue < 300) [r, g, b] = [x, 0, c]; else [r, g, b] = [c, 0, x];
    return `${Math.round((r + m) * 255)},${Math.round((g + m) * 255)},${Math.round((b + m) * 255)}`;
}
function colorFor(galaxyId, name) {
    const g = GALAXY_BY_ID[galaxyId] || GALAXIES[0], h = strHash(name);
    const hue = (g.hue + (h % 41) - 20 + 360) % 360, sat = 64 + ((h >>> 8) % 14), lit = 62 + ((h >>> 16) % 12);
    return { css: `hsl(${hue},${sat}%,${lit}%)`, rgb: hslToRgbStr(hue, sat, lit) };
}

export const universe = { constellations: [], core: [], bridges: [], galaxyBridges: [], totalFragments: 0, loaded: false };

function titleOf(memory) {
    return memory.title || (memory.content || `Memory #${memory.id}`).replace(/\s+/g, ' ').slice(0, 36);
}
function starOf(memory, conId, conLabel) {
    return { ...memory, id: `m${memory.id}`, memoryId: memory.id, title: titleOf(memory), conId, conLabel, conf: Math.max(0.15, Math.min(1, Number(memory.importance || 5) / 10)), lifecycle: memory.is_active === false ? 'frozen' : 'active' };
}
function makeConstellation(id, label, galaxyLabel, memories) {
    const color = colorFor(galaxyLabel, label);
    return { id, label, galaxyLabel, color: color.css, rgb: color.rgb, stars: memories.map(m => starOf(m, id, label)) };
}
function galaxyForType(type) {
    if (type === 'person' || type === 'pet') return 'People';
    if (type === 'place') return 'Places';
    if (type === 'project' || type === 'organization' || type === 'activity') return 'Projects';
    if (type === 'event') return 'Events';
    return 'Life';
}

export async function loadUniverse() {
    const response = await fetch('/api/memories?active_only=true', AUTH());
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    const memories = payload.memories || [];
    const fragments = memories.filter(m => Number(m.layer) === 1);
    const events = memories.filter(m => Number(m.layer) === 2);
    const cores = memories.filter(m => Number(m.layer) === 3);
    const entityGroups = new Map();
    memories.forEach(memory => (memory.entities || [])
        .filter(entity => entity.retrieval_status === 'active')
        .forEach(entity => {
        const key = Number(entity.id);
        if (!entityGroups.has(key)) entityGroups.set(key, { entity, memories: [] });
        entityGroups.get(key).memories.push(memory);
    }));
    const cons = [...entityGroups.values()].map(({ entity, memories: linked }) =>
        makeConstellation(`n${entity.id}`, entity.name, galaxyForType(entity.type), linked));

    const unlinked = memories.filter(memory =>
        !(memory.entities || []).some(entity => entity.retrieval_status === 'active')
        && Number(memory.layer) < 3);
    if (unlinked.length) cons.push(makeConstellation('unlinked', 'Unlinked memories', 'Life', unlinked));

    universe.constellations = cons;
    const ui = window.MEMORY_UI_CONFIG || { user: { name: '晏晏' }, ai: { name: '栖' } };
    universe.core = [
        { name: ui.user.name, content: events.length ? `${events.length} 段共同事件记忆` : '共同经历仍在形成' },
        { name: ui.ai.name, content: cores.length ? cores.map(titleOf).join('\n') : '核心记忆仍在形成' },
    ];
    universe.bridges = [];
    universe.galaxyBridges = [];
    universe.totalFragments = fragments.length;
    universe.loaded = true;
    return universe;
}
export function consOfGalaxy(galaxyId) { return universe.constellations.filter(c => c.galaxyLabel === galaxyId); }
export function conById(id) { return universe.constellations.find(c => c.id === id) || null; }
