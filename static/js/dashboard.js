/**
 * AI Memory Gateway - Dashboard JavaScript
 * 整合记忆管理、导入、导出功能
 * 支持三层记忆架构 v2
 */

// ============================================
// 内联 SVG 图标（Lucide 风格，24x24 viewBox）
// ============================================
const ICONS = (() => {
    const s = (inner, size = 16) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
    return {
        brain:      (sz) => s('<path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/>', sz),
        download:   (sz) => s('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>', sz),
        upload:     (sz) => s('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>', sz),
        msgSquare:  (sz) => s('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>', sz),
        link:       (sz) => s('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>', sz),
        github:     (sz) => s('<path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4"/><path d="M9 18c-4.51 2-5-2-7-2"/>', sz),
        search:     (sz) => s('<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>', sz),
        sparkles:   (sz) => s('<path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/><path d="M5 3v4"/><path d="M19 17v4"/><path d="M3 5h4"/><path d="M17 19h4"/>', sz),
        x:          (sz) => s('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>', sz),
        star:       (sz) => s('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>', sz),
        calendar:   (sz) => s('<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/>', sz),
        fileText:   (sz) => s('<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><line x1="16" x2="8" y1="13" y2="13"/><line x1="16" x2="8" y1="17" y2="17"/><line x1="10" x2="8" y1="9" y2="9"/>', sz),
        check:      (sz) => s('<polyline points="20 6 9 17 4 12"/>', sz),
        trash:      (sz) => s('<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>', sz),
        rotateCcw:  (sz) => s('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>', sz),
        undo:       (sz) => s('<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5v0a5.5 5.5 0 0 1-5.5 5.5H11"/>', sz),
        paperclip:  (sz) => s('<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>', sz),
        gitMerge:   (sz) => s('<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/>', sz),
        calculator: (sz) => s('<rect width="16" height="20" x="4" y="2" rx="2"/><line x1="8" x2="16" y1="6" y2="6"/><line x1="16" x2="16" y1="14" y2="18"/><path d="M16 10h.01"/><path d="M12 10h.01"/><path d="M8 10h.01"/><path d="M12 14h.01"/><path d="M8 14h.01"/><path d="M12 18h.01"/><path d="M8 18h.01"/>', sz),
        save:       (sz) => s('<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>', sz),
    };
})();

// ============================================
// 网关鉴权：从URL参数读取gateway_key，自动注入所有请求
// ============================================
const _gatewayKey = new URLSearchParams(window.location.search).get('gateway_key') || '';
if (_gatewayKey) {
    const _origFetch = window.fetch;
    window.fetch = function(url, opts = {}) {
        opts.headers = opts.headers || {};
        if (opts.headers instanceof Headers) {
            opts.headers.set('X-Gateway-Key', _gatewayKey);
        } else {
            opts.headers['X-Gateway-Key'] = _gatewayKey;
        }
        return _origFetch.call(this, url, opts);
    };
}

// ============================================
// 全局状态
// ============================================
let allMemories = [];
let pendingJsonData = null;
let currentLayer = 'all';
let memCurrentPage = 1;
const MEM_PER_PAGE = 50;

const LAYER_NAMES = {
    1: '碎片',
    2: '事件',
    3: '核心'
};

// ============================================
// 初始化
// ============================================
document.addEventListener('DOMContentLoaded', () => {
    // 初始化侧边栏导航
    initNavigation();
    // 初始化Tab切换
    initTabs();
    // 加载记忆数据
    loadMemories();
    // 加载导出统计
    loadExportStats();
});

// ============================================
// 侧边栏导航
// ============================================
function initNavigation() {
    const navItems = document.querySelectorAll('.nav-item[data-section]');
    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const section = item.dataset.section;
            switchSection(section);
        });
    });
}

function switchSection(name) {
    // 更新导航激活状态
    document.querySelectorAll('.nav-item[data-section]').forEach(item => {
        item.classList.toggle('active', item.dataset.section === name);
    });
    
    // 切换内容区域
    document.querySelectorAll('.section').forEach(section => {
        section.classList.toggle('active', section.id === 'section-' + name);
    });
    
    // 切换到导出页面时刷新统计
    if (name === 'export') {
        loadExportStats();
    }
    if (name === 'conversations') {
        loadConversationList(1);
    }
    if (name === 'threads') {
        loadThreads();
    }
    if (name === 'settings') {
        loadSettings();
    }
}

// ============================================
// Tab 切换（导入页面）
// ============================================
function initTabs() {
    const tabs = document.querySelectorAll('.tab[data-tab]');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const tabName = tab.dataset.tab;
            
            // 更新Tab激活状态
            document.querySelectorAll('.tab[data-tab]').forEach(t => {
                t.classList.toggle('active', t.dataset.tab === tabName);
            });
            
            // 切换Tab面板
            document.querySelectorAll('.tab-panel').forEach(panel => {
                panel.classList.toggle('active', panel.id === 'tab-' + tabName);
            });
            
            // 清除消息
            clearImportResult();
        });
    });
}

// ============================================
// 分层 Tab 切换
// ============================================
function switchLayer(layer) {
    currentLayer = layer;
    memCurrentPage = 1;
    document.querySelectorAll('.layer-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.layer == layer);
    });
    filterAndSort();
}

function updateLayerCounts(stats) {
    const el1 = document.getElementById('count-layer-1');
    const el2 = document.getElementById('count-layer-2');
    const el3 = document.getElementById('count-layer-3');
    if (el1) el1.textContent = stats.layer_1?.active || 0;
    if (el2) el2.textContent = stats.layer_2?.active || 0;
    if (el3) el3.textContent = stats.layer_3?.active || 0;
}

// ============================================
// 记忆管理功能
// ============================================
async function loadMemories() {
    try {
        const resp = await fetch('/api/memories');
        const data = await resp.json();
        allMemories = data.memories || [];
        if (data.layer_stats) updateLayerCounts(data.layer_stats);
        document.getElementById('stats').textContent = '共 ' + allMemories.length + ' 条记忆';
        filterAndSort();
    } catch(e) {
        showManageMsg('error', '加载失败：' + e.message);
    }
}

function renderTable(mems, startIndex) {
    startIndex = startIndex || 0;
    const tbody = document.getElementById('tbody');
    tbody.innerHTML = mems.map((m, i) => {
        const layer = m.layer || 1;
        const isInactive = m.is_active === false;
        const rowClass = isInactive ? 'inactive-row' : '';
        const titleDisplay = m.title || '';
        const mergedFrom = m.merged_from || [];
        
        // 层级下拉选择器
        const layerSelect = '<select class="layer-select" id="l_' + m.id + '" onchange="changeLayer(' + m.id + ')">' +
            '<option value="1"' + (layer === 1 ? ' selected' : '') + '>碎片</option>' +
            '<option value="2"' + (layer === 2 ? ' selected' : '') + '>事件</option>' +
            '<option value="3"' + (layer === 3 ? ' selected' : '') + '>核心</option>' +
            '</select>';
        
        // 合并来源提示
        let mergeInfo = '';
        if (mergedFrom.length > 0) {
            mergeInfo = '<div class="merge-info" onclick="showMergeSource(' + m.id + ')">' +
                ICONS.paperclip(12) + ' 由 ' + mergedFrom.length + ' 条合并</div>';
        }
        
        // 撤回按钮（只有事件记忆且有合并来源时显示）
        let revertBtn = '';
        if (layer === 2 && mergedFrom.length > 0) {
            revertBtn = '<button class="btn btn-warning btn-sm" onclick="revertMerge(' + m.id + ')">撤回</button>';
        }
        
        // 恢复按钮（只有已归档的记忆显示）
        let restoreBtn = '';
        let deleteBtn = '<button class="btn btn-danger btn-sm" onclick="delMem(' + m.id + ')">删除</button>';
        if (isInactive) {
            restoreBtn = '<button class="btn btn-success btn-sm" onclick="restoreMem(' + m.id + ')">恢复</button>';
            deleteBtn = '<button class="btn btn-danger btn-sm" onclick="delMem(' + m.id + ', true)">永久删除</button>';
        }
        
        return '<tr data-id="' + m.id + '" class="' + rowClass + '">' +
            '<td class="col-check"><input type="checkbox" class="mem-check" value="' + m.id + '" onchange="updateFloatingBar()"></td>' +
            '<td class="col-id">' + (startIndex + i + 1) + mergeInfo + '</td>' +
            '<td class="col-layer">' + layerSelect + '</td>' +
            '<td class="col-title"><input type="text" class="title-input" id="t_' + m.id + '" value="' + escHtml(titleDisplay) + '" placeholder="无标题"></td>' +
            '<td class="col-content"><textarea class="content-textarea" id="c_' + m.id + '">' + escHtml(m.content) + '</textarea></td>' +
            '<td class="col-importance"><input type="number" class="importance-input" id="i_' + m.id + '" value="' + m.importance + '" min="1" max="10"></td>' +
            '<td class="col-time">' + fmtTime(m.created_at) + '</td>' +
            '<td class="col-actions"><div class="row-actions">' +
                '<button class="btn btn-primary btn-sm" onclick="saveMem(' + m.id + ')">保存</button>' +
                revertBtn +
                restoreBtn +
                deleteBtn +
            '</div></td>' +
            '</tr>';
    }).join('');
}

function escHtml(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtTime(s) { return s || '-'; }

function filterAndSort() {
    const q = document.getElementById('searchBox').value.trim().toLowerCase();
    const sort = document.getElementById('sortSelect').value;
    const dateVal = document.getElementById('dateFilter').value;
    const showInactiveEl = document.getElementById('showInactive');
    const showInactive = showInactiveEl ? showInactiveEl.checked : false;
    
    let mems = allMemories;
    if (currentLayer !== 'all') mems = mems.filter(m => (m.layer || 1) == currentLayer);
    if (!showInactive) mems = mems.filter(m => m.is_active !== false);
    if (q) mems = mems.filter(m => m.content.toLowerCase().includes(q) || (m.title && m.title.toLowerCase().includes(q)));
    if (dateVal) mems = mems.filter(m => m.created_at && fmtTime(m.created_at).slice(0, 10) === dateVal);
    
    mems = [...mems].sort((a, b) => {
        if (sort === 'id-desc') return b.id - a.id;
        if (sort === 'id-asc') return a.id - b.id;
        if (sort === 'imp-desc') return b.importance - a.importance || b.id - a.id;
        if (sort === 'imp-asc') return a.importance - b.importance || a.id - b.id;
        return 0;
    });
    
    // 分页
    const totalItems = mems.length;
    const totalPages = Math.max(1, Math.ceil(totalItems / MEM_PER_PAGE));
    if (memCurrentPage > totalPages) memCurrentPage = totalPages;
    const start = (memCurrentPage - 1) * MEM_PER_PAGE;
    const pageMems = mems.slice(start, start + MEM_PER_PAGE);
    
    renderTable(pageMems, start);
    renderMemPagination(totalItems, totalPages);
    
    const parts = [];
    if (q || dateVal || currentLayer !== 'all') {
        parts.push('筛选到 ' + totalItems + ' 条');
        if (currentLayer !== 'all') parts.push('层级: ' + LAYER_NAMES[currentLayer]);
        if (dateVal) parts.push('日期: ' + dateVal);
    } else {
        parts.push('共 ' + allMemories.filter(m => m.is_active !== false).length + ' 条活跃记忆');
    }
    if (totalPages > 1) {
        parts.push(`第 ${memCurrentPage}/${totalPages} 页`);
    }
    document.getElementById('stats').textContent = parts.join('  ');
}

function renderMemPagination(totalItems, totalPages) {
    // 在表格后面渲染分页控件
    let paginationEl = document.getElementById('mem-pagination');
    if (!paginationEl) {
        const tableCard = document.querySelector('.table-card');
        if (tableCard) {
            paginationEl = document.createElement('div');
            paginationEl.id = 'mem-pagination';
            paginationEl.style.cssText = 'display: flex; justify-content: center; align-items: center; gap: 8px; padding: 16px 0;';
            tableCard.appendChild(paginationEl);
        } else {
            return;
        }
    }
    
    if (totalPages <= 1) {
        paginationEl.innerHTML = '';
        return;
    }
    
    let html = '';
    html += `<button class="btn btn-sm" onclick="goMemPage(1)" ${memCurrentPage === 1 ? 'disabled' : ''}>«</button>`;
    html += `<button class="btn btn-sm" onclick="goMemPage(${memCurrentPage - 1})" ${memCurrentPage === 1 ? 'disabled' : ''}>‹</button>`;
    
    // 显示页码（最多显示5个）
    let startPage = Math.max(1, memCurrentPage - 2);
    let endPage = Math.min(totalPages, startPage + 4);
    startPage = Math.max(1, endPage - 4);
    
    for (let p = startPage; p <= endPage; p++) {
        html += `<button class="btn btn-sm${p === memCurrentPage ? ' btn-primary' : ''}" onclick="goMemPage(${p})">${p}</button>`;
    }
    
    html += `<button class="btn btn-sm" onclick="goMemPage(${memCurrentPage + 1})" ${memCurrentPage === totalPages ? 'disabled' : ''}>›</button>`;
    html += `<button class="btn btn-sm" onclick="goMemPage(${totalPages})" ${memCurrentPage === totalPages ? 'disabled' : ''}>»</button>`;
    html += `<span style="color: var(--text-muted); font-size: 13px; margin-left: 8px;">${totalItems} 条</span>`;
    
    paginationEl.innerHTML = html;
}

function goMemPage(page) {
    memCurrentPage = page;
    filterAndSort();
    // 滚到表格顶部
    document.querySelector('.table-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function clearDateFilter() {
    document.getElementById('dateFilter').value = '';
    filterAndSort();
}

async function semanticSearch() {
    const q = document.getElementById('searchBox').value.trim();
    if (!q) { alert('请先在搜索框输入关键词'); return; }
    
    document.getElementById('stats').textContent = '语义搜索中...';
    
    try {
        const resp = await fetch('/api/memories/search?q=' + encodeURIComponent(q) + '&limit=20');
        const data = await resp.json();
        
        if (data.error) {
            document.getElementById('stats').textContent = '❌ ' + data.error;
            return;
        }
        
        const results = data.results || [];
        renderTable(results);
        
        // 隐藏分页（语义搜索结果不分页）
        const paginationEl = document.getElementById('mem-pagination');
        if (paginationEl) paginationEl.innerHTML = '';
        
        const scoreInfo = results.length > 0 
            ? results.map(r => `#${r.id}(${(r.score || 0).toFixed(3)})`).join(', ')
            : '';
        
        document.getElementById('stats').innerHTML = 
            `🔍 语义搜索 "${q}" → ${results.length} 条结果` +
            (scoreInfo ? ` [${scoreInfo}]` : '') +
            `&nbsp;&nbsp;<a href="#" onclick="exitSemanticSearch(); return false;" style="color: var(--primary);">← 返回全部</a>`;
    } catch(e) {
        document.getElementById('stats').textContent = '❌ 搜索失败: ' + e.message;
    }
}

function exitSemanticSearch() {
    document.getElementById('searchBox').value = '';
    memCurrentPage = 1;
    filterAndSort();
}

async function changeLayer(id) {
    const layerEl = document.getElementById('l_' + id);
    const newLayer = parseInt(layerEl.value);
    
    try {
        const resp = await fetch('/api/memories/' + id, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({layer: newLayer})
        });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
            loadMemories();
        } else {
            showManageMsg('success', '✅ #' + id + ' 层级已改为 ' + LAYER_NAMES[newLayer]);
            const mem = allMemories.find(m => m.id === id);
            if (mem) mem.layer = newLayer;
            loadMemories();
        }
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

async function saveMem(id) {
    const content = document.getElementById('c_' + id).value;
    const importance = parseInt(document.getElementById('i_' + id).value);
    const titleEl = document.getElementById('t_' + id);
    const layerEl = document.getElementById('l_' + id);
    const title = titleEl ? titleEl.value : null;
    const layer = layerEl ? parseInt(layerEl.value) : null;
    
    try {
        const resp = await fetch('/api/memories/' + id, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content, importance, title, layer})
        });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
        } else {
            showManageMsg('success', '✅ 已保存 #' + id);
            loadMemories();
        }
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

async function delMem(id, hard = false) {
    const confirmMsg = hard 
        ? '确定永久删除 #' + id + '？此操作不可撤销！'
        : '确定删除 #' + id + '？（软删除，可恢复）';
    if (!confirm(confirmMsg)) return;
    try {
        const soft = !hard;
        const resp = await fetch('/api/memories/' + id + '?soft=' + soft, { method: 'DELETE' });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
        } else {
            const action = hard ? '永久删除' : '已归档';
            showManageMsg('success', '✅ ' + action + ' #' + id);
            loadMemories();
        }
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

async function restoreMem(id) {
    try {
        const resp = await fetch('/api/memories/' + id + '/restore', { method: 'POST' });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
        } else {
            showManageMsg('success', '✅ 已恢复 #' + id);
            loadMemories();
        }
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

async function batchSave() {
    const checked = [...document.querySelectorAll('.mem-check:checked')].map(c => parseInt(c.value));
    if (checked.length === 0) { showManageMsg('error', '请先勾选要保存的记忆'); return; }
    
    const updates = [];
    checked.forEach(id => {
        const cEl = document.getElementById('c_' + id);
        const iEl = document.getElementById('i_' + id);
        const tEl = document.getElementById('t_' + id);
        const lEl = document.getElementById('l_' + id);
        if (cEl && iEl) {
            updates.push({
                id,
                content: cEl.value,
                importance: parseInt(iEl.value),
                title: tEl ? tEl.value : null,
                layer: lEl ? parseInt(lEl.value) : null
            });
        }
    });
    
    if (!confirm('确定保存选中的 ' + updates.length + ' 条记忆的修改？')) return;
    try {
        const resp = await fetch('/api/memories/batch-update', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({updates: updates})
        });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
        } else {
            showManageMsg('success', '✅ 已保存 ' + data.updated + ' 条');
            clearSelection();
            loadMemories();
        }
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

async function batchDelete() {
    const checked = [...document.querySelectorAll('.mem-check:checked')].map(c => parseInt(c.value));
    if (checked.length === 0) { showManageMsg('error', '请先勾选要删除的记忆'); return; }
    if (!confirm('确定删除选中的 ' + checked.length + ' 条记忆？此操作不可撤销。')) return;
    try {
        const resp = await fetch('/api/memories/batch-delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ids: checked})
        });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
        } else {
            showManageMsg('success', '✅ 已删除 ' + data.deleted + ' 条');
            clearSelection();
            loadMemories();
        }
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

function toggleAll() {
    const val = event.target.checked;
    document.querySelectorAll('.mem-check').forEach(c => c.checked = val);
    document.getElementById('selectAll').checked = val;
    document.getElementById('selectAllHead').checked = val;
    updateFloatingBar();
}

// 监听勾选变化，更新浮动操作栏
function updateFloatingBar() {
    const checked = document.querySelectorAll('.mem-check:checked').length;
    const floatingBar = document.getElementById('floatingBar');
    const countEl = document.getElementById('selectedCount');
    
    if (checked > 0) {
        countEl.textContent = checked;
        floatingBar.style.display = 'flex';
    } else {
        floatingBar.style.display = 'none';
    }
}

function clearSelection() {
    document.querySelectorAll('.mem-check').forEach(c => c.checked = false);
    document.getElementById('selectAll').checked = false;
    document.getElementById('selectAllHead').checked = false;
    updateFloatingBar();
}

function showManageMsg(type, text) {
    const container = document.getElementById('manage-msg');
    container.innerHTML = '<div class="msg msg-' + type + '">' + text + '</div>';
    setTimeout(() => {
        container.innerHTML = '';
    }, 4000);
}

// ============================================
// 查看合并来源
// ============================================
async function showMergeSource(id) {
    const mem = allMemories.find(m => m.id === id);
    if (!mem || !mem.merged_from || mem.merged_from.length === 0) {
        showManageMsg('error', '没有合并来源信息');
        return;
    }
    
    try {
        const resp = await fetch('/api/memories?active_only=false');
        const data = await resp.json();
        const allMems = data.memories || [];
        
        const sources = mem.merged_from.map(srcId => {
            const srcMem = allMems.find(m => m.id === srcId);
            return srcMem ? { id: srcId, content: srcMem.content } : { id: srcId, content: '(已删除)' };
        });
        
        let html = '<h3>合并来源 - 事件 #' + id + '</h3>';
        html += '<p style="color:var(--text-light);margin-bottom:16px;">以下 ' + sources.length + ' 条碎片被合并成了这条事件记忆：</p>';
        sources.forEach((src, i) => {
            html += '<div class="source-item"><b>#' + src.id + '</b><br>' + escHtml(src.content) + '</div>';
        });
        html += '<div class="modal-actions"><button class="btn btn-secondary" onclick="closeMergeSourceModal()">关闭</button></div>';
        
        document.getElementById('mergeSourceContent').innerHTML = html;
        document.getElementById('mergeSourceModal').style.display = 'flex';
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

function closeMergeSourceModal() {
    document.getElementById('mergeSourceModal').style.display = 'none';
}

// ============================================
// 撤回合并
// ============================================
async function revertMerge(id) {
    const mem = allMemories.find(m => m.id === id);
    if (!mem || !mem.merged_from || mem.merged_from.length === 0) {
        showManageMsg('error', '没有合并来源，无法撤回');
        return;
    }
    
    if (!confirm('确定撤回合并？\n\n将恢复 ' + mem.merged_from.length + ' 条原始碎片，并删除当前事件记忆 #' + id)) {
        return;
    }
    
    try {
        const resp = await fetch('/api/memories/' + id + '/revert-merge', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'}
        });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
        } else {
            showManageMsg('success', '✅ 已撤回合并，恢复了 ' + data.restored + ' 条碎片');
            loadMemories();
        }
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

// ============================================
// 合并弹窗
// ============================================
function openMergeModal() {
    const checked = [...document.querySelectorAll('.mem-check:checked')].map(c => parseInt(c.value));
    if (checked.length < 2) {
        showManageMsg('error', '请至少选择 2 条记忆进行合并');
        return;
    }
    
    const selectedContents = checked.map(id => {
        const mem = allMemories.find(m => m.id === id);
        return mem ? mem.content : '';
    }).join('\n\n---\n\n');
    
    document.getElementById('mergeCount').textContent = checked.length;
    document.getElementById('mergeContent').value = selectedContents;
    document.getElementById('mergeContent').placeholder = '请编辑合并后的完整描述...';
    document.getElementById('mergeTitle').value = '';
    document.getElementById('mergeImportance').value = '5';
    document.getElementById('mergeLayer').value = '2';
    document.getElementById('mergeModal').style.display = 'flex';
}

function closeMergeModal() {
    document.getElementById('mergeModal').style.display = 'none';
}

async function doMerge() {
    const checked = [...document.querySelectorAll('.mem-check:checked')].map(c => parseInt(c.value));
    const title = document.getElementById('mergeTitle').value.trim();
    const content = document.getElementById('mergeContent').value.trim();
    const importance = parseInt(document.getElementById('mergeImportance').value);
    const layer = parseInt(document.getElementById('mergeLayer').value);
    
    if (!content) { showManageMsg('error', '请输入合并后的内容'); return; }
    try {
        const resp = await fetch('/api/memories/merge', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ ids: checked, title, content, importance, layer })
        });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
        } else {
            showManageMsg('success', '✅ 已合并 ' + data.merged + ' 条为新记忆 #' + data.new_id);
            closeMergeModal();
            clearSelection();
            loadMemories();
        }
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

// ============================================
// 整理弹窗
// ============================================
function openConsolidateModal() {
    document.getElementById('consolidateModal').style.display = 'flex';
}

function closeConsolidateModal() {
    document.getElementById('consolidateModal').style.display = 'none';
}

async function doConsolidate() {
    const startDate = document.getElementById('consolidateDateStart').value;
    const endDate = document.getElementById('consolidateDateEnd').value;
    
    if (!startDate || !endDate) { 
        showManageMsg('error', '请选择开始和结束日期'); 
        return; 
    }
    if (startDate > endDate) {
        showManageMsg('error', '开始日期不能晚于结束日期');
        return;
    }
    
    showManageMsg('info', '正在提交整理任务...');
    closeConsolidateModal();
    try {
        const resp = await fetch('/api/memories/consolidate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({start_date: startDate, end_date: endDate})
        });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
            return;
        }
        if (data.status === 'already_running') {
            showManageMsg('info', '⏳ 整理任务正在运行中...');
        } else {
            showManageMsg('info', '⏳ 整理任务已启动，后台处理中...');
        }
        // 轮询状态
        const pollInterval = setInterval(async () => {
            try {
                const statusResp = await fetch('/api/memories/consolidate/status');
                const status = await statusResp.json();
                if (status.running) {
                    showManageMsg('info', '⏳ 整理进行中（' + (status.started_at || '') + '）...');
                } else {
                    clearInterval(pollInterval);
                    if (status.error) {
                        showManageMsg('error', '❌ 整理失败: ' + status.error);
                    } else if (status.result) {
                        const r = status.result;
                        if (r.status === 'no_fragments') {
                            showManageMsg('info', '📝 该时间段没有需要整理的碎片记忆');
                        } else if (r.status === 'ok') {
                            showManageMsg('success', '✅ 整理完成！处理了 ' + r.fragments_processed + ' 条碎片，生成了 ' + r.events_created + ' 条事件记忆');
                            loadMemories();
                        } else if (r.status === 'error') {
                            showManageMsg('error', '❌ ' + (r.error || '未知错误'));
                        }
                    }
                }
            } catch(e) {
                clearInterval(pollInterval);
                showManageMsg('error', '❌ 状态查询失败: ' + e.message);
            }
        }, 3000);
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

// ============================================
// 清理归档碎片
// ============================================
async function cleanupOldFragments() {
    if (!confirm('确定清理30天前的归档碎片？此操作不可撤销。')) return;
    
    showManageMsg('info', '正在清理...');
    try {
        const resp = await fetch('/api/memories/cleanup-fragments', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({days: 30})
        });
        const data = await resp.json();
        if (data.error) {
            showManageMsg('error', '❌ ' + data.error);
        } else {
            showManageMsg('success', '✅ 已清理 ' + data.deleted + ' 条归档碎片');
            loadMemories();
        }
    } catch(e) {
        showManageMsg('error', '❌ ' + e.message);
    }
}

// ============================================
// 导入功能
// ============================================
async function doTextImport() {
    const file = document.getElementById('txtFile').files[0];
    const text = document.getElementById('txtInput').value.trim();
    const skip = document.getElementById('skipScore').checked;
    
    let content = '';
    if (file) {
        content = await file.text();
    } else if (text) {
        content = text;
    } else {
        showImportResult('error', '请先上传文件或输入文本');
        return;
    }
    
    const lines = content.split('\n').map(l => l.trim()).filter(l => l.length > 0);
    if (lines.length === 0) {
        showImportResult('error', '没有找到有效的记忆条目');
        return;
    }
    
    showImportResult('info', skip 
        ? '正在导入 ' + lines.length + ' 条记忆...' 
        : '正在为 ' + lines.length + ' 条记忆自动评分，请稍候...');
    
    try {
        const resp = await fetch('/import/text', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({lines: lines, skip_scoring: skip})
        });
        const data = await resp.json();
        if (data.error) {
            showImportResult('error', '❌ ' + data.error);
        } else {
            showImportResult('success', '✅ 导入完成！新增 ' + data.imported + ' 条，跳过 ' + data.skipped + ' 条（已存在），总计 ' + data.total + ' 条');
            // 刷新记忆列表
            loadMemories();
        }
    } catch(e) {
        showImportResult('error', '❌ 请求失败：' + e.message);
    }
}

async function previewJson() {
    const file = document.getElementById('jsonFile').files[0];
    const text = document.getElementById('jsonInput').value.trim();
    const preview = document.getElementById('jsonPreview');
    
    let jsonStr = '';
    if (file) {
        jsonStr = await file.text();
    } else if (text) {
        jsonStr = text;
    } else {
        showImportResult('error', '请先上传文件或粘贴 JSON');
        return;
    }
    
    try {
        const parsed = JSON.parse(jsonStr);
        const mems = parsed.memories || [];
        if (mems.length === 0) {
            showImportResult('error', '❌ 没有找到 memories 字段，请确认这是从导出功能导出的文件');
            preview.innerHTML = '';
            return;
        }
        
        pendingJsonData = parsed;
        let html = '<p><b>预览：共 ' + mems.length + ' 条记忆</b></p>';
        const show = mems.slice(0, 10);
        show.forEach(m => {
            html += '<div class="preview-item">权重 ' + (m.importance || '?') + ' | ' + (m.content || '').substring(0, 80) + '</div>';
        });
        if (mems.length > 10) {
            html += '<div class="preview-item" style="color:#999;">...还有 ' + (mems.length - 10) + ' 条</div>';
        }
        html += '<br><button class="btn btn-primary" onclick="confirmJsonImport()">确认导入</button>';
        preview.innerHTML = html;
        clearImportResult();
    } catch(e) {
        showImportResult('error', '❌ JSON 格式错误：' + e.message);
        preview.innerHTML = '';
    }
}

async function confirmJsonImport() {
    if (!pendingJsonData) {
        showImportResult('error', '请先预览');
        return;
    }
    
    showImportResult('info', '导入中...');
    
    try {
        const resp = await fetch('/import/memories', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(pendingJsonData)
        });
        const data = await resp.json();
        if (data.error) {
            showImportResult('error', '❌ ' + data.error);
        } else {
            showImportResult('success', '✅ 导入完成！新增 ' + data.imported + ' 条，跳过 ' + data.skipped + ' 条（已存在），总计 ' + data.total + ' 条');
            loadMemories();
        }
        document.getElementById('jsonPreview').innerHTML = '';
        pendingJsonData = null;
    } catch(e) {
        showImportResult('error', '❌ 请求失败：' + e.message);
    }
}

function showImportResult(type, text) {
    const container = document.getElementById('import-result');
    container.innerHTML = '<div class="msg msg-' + type + '">' + text + '</div>';
}

function clearImportResult() {
    document.getElementById('import-result').innerHTML = '';
    document.getElementById('jsonPreview').innerHTML = '';
}

// ============================================
// 导出功能
// ============================================
async function loadExportStats() {
    const el = document.getElementById('export-stats');
    try {
        const resp = await fetch('/api/memories');
        const data = await resp.json();
        const count = (data.memories || []).length;
        el.textContent = '当前共有 ' + count + ' 条记忆';
    } catch(e) {
        el.textContent = '无法加载统计';
    }
}

function doExport() {
    // 直接跳转到导出接口，浏览器会下载文件
    window.location.href = '/export/memories';
}


// ============================================
// 对话记录功能
// ============================================
let convCurrentPage = 1;
let convIsSearchMode = false;
let convSearchQuery = '';

async function loadConvStats() {
    const el = document.getElementById('conv-export-stats');
    try {
        const resp = await fetch('/api/conversations?page=1&per_page=1');
        const data = await resp.json();
        el.textContent = '当前共有 ' + (data.total || 0) + ' 个对话';
    } catch(e) {
        el.textContent = '无法加载统计';
    }
}

async function exportConversations() {
    try {
        const resp = await fetch("/api/conversations/export");
        const data = await resp.json();
        if (data.error) { alert("导出失败: " + data.error); return; }
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        const now = new Date();
        const ts = now.getFullYear() +
            String(now.getMonth()+1).padStart(2,"0") +
            String(now.getDate()).padStart(2,"0") + "_" +
            String(now.getHours()).padStart(2,"0") +
            String(now.getMinutes()).padStart(2,"0") +
            String(now.getSeconds()).padStart(2,"0");
        a.href = url;
        a.download = "conversations_backup_" + ts + ".json";
        a.click();
        URL.revokeObjectURL(url);
    } catch(e) { alert("导出失败: " + e.message); }
}

async function doConvExport() { await exportConversations(); }

async function doConvImport() {
    const file = document.getElementById('convJsonFile').files[0];
    const text = document.getElementById('convJsonInput').value.trim();
    const resultEl = document.getElementById('conv-import-result');
    
    let jsonStr = '';
    if (file) { jsonStr = await file.text(); }
    else if (text) { jsonStr = text; }
    else { resultEl.innerHTML = '<div class="msg msg-error">请先上传文件或粘贴 JSON</div>'; return; }
    
    let records;
    try {
        records = JSON.parse(jsonStr);
        if (!Array.isArray(records)) records = records.records || records;
        if (!Array.isArray(records) || records.length === 0) {
            resultEl.innerHTML = '<div class="msg msg-error">❌ 没有找到有效的对话记录</div>';
            return;
        }
    } catch(e) {
        resultEl.innerHTML = '<div class="msg msg-error">❌ JSON 格式错误：' + e.message + '</div>';
        return;
    }
    
    if (!confirm('确定导入 ' + records.length + ' 条对话记录？')) return;
    
    // 分批导入（每批300条，避免超时）
    const BATCH_SIZE = 300;
    const totalBatches = Math.ceil(records.length / BATCH_SIZE);
    let totalImported = 0;
    let totalSkipped = 0;
    let failedBatches = 0;
    
    for (let i = 0; i < totalBatches; i++) {
        const batch = records.slice(i * BATCH_SIZE, (i + 1) * BATCH_SIZE);
        const progress = Math.round(((i + 1) / totalBatches) * 100);
        resultEl.innerHTML = `<div class="msg msg-info">导入中... 第 ${i + 1}/${totalBatches} 批（${progress}%）已导入 ${totalImported} 条</div>`;
        
        try {
            const resp = await fetch('/api/conversations/import', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(batch)
            });
            const data = await resp.json();
            if (data.error) {
                failedBatches++;
                console.error(`批次 ${i + 1} 导入失败:`, data.error);
            } else {
                totalImported += (data.imported || 0);
                totalSkipped += (data.skipped || 0);
            }
        } catch(e) {
            failedBatches++;
            console.error(`批次 ${i + 1} 请求失败:`, e);
        }
    }
    
    let msg = `✅ 导入完成！新增 ${totalImported} 条`;
    if (totalSkipped) msg += `，跳过 ${totalSkipped} 条（已存在）`;
    if (failedBatches) msg += `，${failedBatches} 批失败`;
    resultEl.innerHTML = `<div class="msg msg-success">${msg}</div>`;
    
    loadConvStats();
    loadConversationList(1);
    document.getElementById('convJsonFile').value = '';
    document.getElementById('convJsonInput').value = '';
}

// 加载对话列表（分页）
async function loadConversationList(page = 1) {
    convCurrentPage = page;
    convIsSearchMode = false;
    convSearchQuery = '';
    document.getElementById('conv-search-input').value = '';
    document.getElementById('conv-search-status').textContent = '';
    document.getElementById('conv-list-title').textContent = '对话列表';
    
    const container = document.getElementById('conv-list-container');
    container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px 0;">加载中...</div>';
    
    try {
        const resp = await fetch('/api/conversations?page=' + page + '&per_page=20');
        const data = await resp.json();
        if (data.error) {
            container.innerHTML = '<div style="color: var(--error); padding: 20px 0;">加载失败: ' + data.error + '</div>';
            return;
        }
        renderConvList(data.conversations);
        renderConvPagination(data.page, data.total_pages, data.total);
        document.getElementById('conv-list-count').textContent = `共 ${data.total} 个对话`;
    } catch(e) {
        container.innerHTML = '<div style="color: var(--error); padding: 20px 0;">请求失败: ' + e.message + '</div>';
    }
}

// 搜索对话
async function searchConversations() {
    const query = document.getElementById('conv-search-input').value.trim();
    if (!query) { loadConversationList(1); return; }
    
    convIsSearchMode = true;
    convSearchQuery = query;
    
    const container = document.getElementById('conv-list-container');
    const statusEl = document.getElementById('conv-search-status');
    container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px 0;">搜索中...</div>';
    
    try {
        const resp = await fetch('/api/chat/search?q=' + encodeURIComponent(query) + '&limit=20&offset=0');
        if (resp.status === 404) { statusEl.textContent = '搜索功能暂未启用'; container.innerHTML = ''; return; }
        const data = await resp.json();
        if (data.error) {
            container.innerHTML = '<div style="color: var(--error); padding: 20px 0;">' + data.error + '</div>';
            return;
        }
        statusEl.textContent = `搜索"${query}"找到 ${data.total} 个对话`;
        document.getElementById('conv-list-title').textContent = '搜索结果';
        document.getElementById('conv-list-count').textContent = `${data.total} 个结果`;
        renderConvList(data.results, true);
        // 搜索结果的简易分页
        document.getElementById('conv-pagination').innerHTML = data.total > 20 
            ? `<span style="color: var(--text-muted); font-size: 13px;">显示前 20 条结果，共 ${data.total} 条</span>` 
            : '';
    } catch(e) {
        container.innerHTML = '<div style="color: var(--error); padding: 20px 0;">搜索失败: ' + e.message + '</div>';
    }
}

function clearConvSearch() {
    document.getElementById('conv-search-input').value = '';
    document.getElementById('conv-search-status').textContent = '';
    loadConversationList(1);
}

// 渲染对话列表
function renderConvList(conversations, isSearch = false) {
    const container = document.getElementById('conv-list-container');
    
    if (!conversations || conversations.length === 0) {
        container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 40px 0;">暂无对话记录</div>';
        return;
    }
    
    // 多选控制栏
    let html = `<div id="conv-batch-bar" style="display: flex; gap: 8px; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border); margin-bottom: 4px;">
        <label style="display: flex; align-items: center; gap: 4px; cursor: pointer; font-size: 13px;">
            <input type="checkbox" id="conv-select-all" onchange="toggleConvSelectAll(this.checked)"> 全选
        </label>
        <button class="btn btn-sm" onclick="batchDeleteConversations()" id="conv-batch-delete-btn" style="display: none; font-size: 12px;">${ICONS.trash(13)} 批量删除</button>
        <button class="btn btn-sm" onclick="batchMergeSessions()" id="conv-batch-merge-btn" style="display: none; font-size: 12px;">${ICONS.gitMerge(13)} 合并到...</button>
        <span id="conv-selected-count" style="color: var(--text-muted); font-size: 12px; display: none;"></span>
    </div>`;
    
    for (const conv of conversations) {
        const sid = conv.session_id || conv.id;
        const title = escapeHtml(sid);
        const preview = escapeHtml(conv.title || conv.preview || '');
        const msgCount = conv.message_count || '';
        const totalTokens = conv.total_tokens || 0;
        const tokenStr = totalTokens > 0 ? (totalTokens >= 1000000 ? (totalTokens / 1000000).toFixed(1) + 'M' : totalTokens >= 1000 ? (totalTokens / 1000).toFixed(1) + 'K' : totalTokens) : '';
        const lastTime = conv.last_time || conv.updated_at || '';
        const timeStr = lastTime ? formatConvTime(lastTime) : '';
        
        html += `
        <div class="conv-item" style="display: flex; align-items: flex-start; padding: 12px; border-bottom: 1px solid var(--border); transition: background 0.15s;"
             onmouseover="this.style.background='var(--bg-hover, rgba(0,0,0,0.03))'" 
             onmouseout="this.style.background=''">
            <input type="checkbox" class="conv-checkbox" value="${escapeHtml(sid)}" 
                   onchange="updateConvSelectionCount()" 
                   style="margin-right: 10px; margin-top: 4px; cursor: pointer; flex-shrink: 0;">
            <div style="flex: 1; min-width: 0; cursor: pointer;" onclick="openConvDetail('${escapeHtml(sid)}')">
                <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                    <div style="flex: 1; min-width: 0;">
                        <div style="font-weight: 500; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${title}</div>
                        <div style="color: var(--text-muted); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${preview}</div>
                    </div>
                    <div style="text-align: right; flex-shrink: 0; margin-left: 12px;">
                        <div style="color: var(--text-muted); font-size: 12px;">${timeStr}</div>
                        ${msgCount ? `<div style="color: var(--text-muted); font-size: 12px; margin-top: 2px;">${msgCount} 条</div>` : ''}
                        ${tokenStr ? `<div style="color: var(--text-muted); font-size: 11px; margin-top: 2px;">${tokenStr}</div>` : ''}
                    </div>
                </div>
            </div>
        </div>`;
    }
    
    container.innerHTML = html;
}

// 渲染分页
function renderConvPagination(currentPage, totalPages, total) {
    const container = document.getElementById('conv-pagination');
    if (totalPages <= 1) { container.innerHTML = ''; return; }
    
    let html = '';
    html += `<button class="btn btn-sm" onclick="loadConversationList(${currentPage - 1})" ${currentPage <= 1 ? 'disabled' : ''}>上一页</button>`;
    
    // 页码按钮（最多显示5个）
    let startPage = Math.max(1, currentPage - 2);
    let endPage = Math.min(totalPages, startPage + 4);
    if (endPage - startPage < 4) startPage = Math.max(1, endPage - 4);
    
    for (let i = startPage; i <= endPage; i++) {
        html += `<button class="btn btn-sm${i === currentPage ? ' btn-primary' : ''}" onclick="loadConversationList(${i})">${i}</button>`;
    }
    
    html += `<button class="btn btn-sm" onclick="loadConversationList(${currentPage + 1})" ${currentPage >= totalPages ? 'disabled' : ''}>下一页</button>`;
    html += `<span style="color: var(--text-muted); font-size: 12px; margin-left: 8px;">${currentPage}/${totalPages}</span>`;
    
    container.innerHTML = html;
}

// 打开对话详情
let convDetailSessionId = '';
let convDetailLoadedCount = 0;

async function openConvDetail(sessionId) {
    const panel = document.getElementById('conv-detail-panel');
    const titleEl = document.getElementById('conv-detail-title');
    const messagesEl = document.getElementById('conv-detail-messages');
    
    convDetailSessionId = sessionId;
    convDetailLoadedCount = 0;
    panel.style.display = 'block';
    titleEl.textContent = '加载中...';
    messagesEl.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px 0;">加载中...</div>';
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    
    await loadConvMessages(sessionId, false);
}

async function loadConvMessages(sessionId, append = false) {
    const titleEl = document.getElementById('conv-detail-title');
    const messagesEl = document.getElementById('conv-detail-messages');
    const offset = append ? convDetailLoadedCount : 0;
    
    try {
        const resp = await fetch(`/api/conversations/${encodeURIComponent(sessionId)}/messages?limit=50&offset=${offset}`);
        const data = await resp.json();
        
        if (data.error) {
            messagesEl.innerHTML = '<div style="color: var(--error);">' + data.error + '</div>';
            return;
        }
        
        const messages = data.messages || [];
        const total = data.total || messages.length;
        
        if (!append) {
            convDetailLoadedCount = 0;
        }
        convDetailLoadedCount += messages.length;
        
        titleEl.textContent = `对话详情（${convDetailLoadedCount} / ${total} 条消息）`;
        
        // 渲染消息
        let html = '';
        if (!append) {
            html += `<div style="margin-bottom: 12px; display: flex; gap: 8px; justify-content: flex-end;">
                <button class="btn btn-sm" onclick="deleteConversation('${escapeHtml(sessionId)}')">${ICONS.trash(13)} 删除对话</button>
            </div>`;
        }
        
        for (const msg of messages) {
            const isUser = msg.role === 'user';
            const roleLabel = isUser ? '👤 用户' : '🤖 助手';
            const bgColor = isUser ? 'var(--bg-user, rgba(59,130,246,0.08))' : 'var(--bg-assistant, rgba(0,0,0,0.02))';
            const timeStr = msg.created_at ? formatConvTime(msg.created_at) : '';
            const msgId = msg.id || '';
            const content = escapeHtml(msg.content || '');
            
            html += `
            <div style="padding: 12px; margin-bottom: 8px; border-radius: 8px; background: ${bgColor}; position: relative;" id="msg-${msgId}">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                    <span style="font-weight: 500; font-size: 13px;">${roleLabel}</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span style="color: var(--text-muted); font-size: 12px;">${timeStr}</span>
                        ${msgId ? `<button class="btn btn-sm" onclick="toggleEditMessage(${msgId})" style="font-size: 11px; padding: 2px 8px;">编辑</button><button class="btn btn-sm" onclick="deleteSingleMessage(${msgId})" style="font-size: 11px; padding: 2px 8px; color: var(--error);">删除</button>` : ''}
                    </div>
                </div>
                <div class="msg-content" id="msg-content-${msgId}" style="white-space: pre-wrap; word-break: break-word; font-size: 14px; line-height: 1.6;">${content}</div>
                <div class="msg-edit" id="msg-edit-${msgId}" style="display: none;">
                    <textarea id="msg-textarea-${msgId}" style="width: 100%; min-height: 100px; padding: 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; line-height: 1.6; resize: vertical; font-family: inherit;">${content}</textarea>
                    <div style="margin-top: 8px; display: flex; gap: 8px; justify-content: flex-end;">
                        <button class="btn btn-sm" onclick="toggleEditMessage(${msgId})">取消</button>
                        <button class="btn btn-sm btn-primary" onclick="saveMessageEdit(${msgId})">保存</button>
                    </div>
                </div>
            </div>`;
        }
        
        // 加载更多按钮
        if (convDetailLoadedCount < total) {
            html += `<div style="text-align: center; padding: 16px 0;">
                <button class="btn btn-primary" onclick="loadConvMessages('${escapeHtml(sessionId)}', true)">
                    加载更多（还有 ${total - convDetailLoadedCount} 条）
                </button>
            </div>`;
        }
        
        if (append) {
            // 追加模式：去掉旧的"加载更多"按钮，加上新内容
            const oldLoadMore = messagesEl.querySelector('[onclick*="loadConvMessages"]');
            if (oldLoadMore) oldLoadMore.parentElement.remove();
            messagesEl.insertAdjacentHTML('beforeend', html);
        } else {
            messagesEl.innerHTML = html;
        }
    } catch(e) {
        if (!append) {
            messagesEl.innerHTML = '<div style="color: var(--error);">加载失败: ' + e.message + '</div>';
        }
    }
}

function closeConvDetail() {
    document.getElementById('conv-detail-panel').style.display = 'none';
}

// 编辑消息
function toggleEditMessage(msgId) {
    const contentEl = document.getElementById('msg-content-' + msgId);
    const editEl = document.getElementById('msg-edit-' + msgId);
    
    if (editEl.style.display === 'none') {
        contentEl.style.display = 'none';
        editEl.style.display = 'block';
    } else {
        contentEl.style.display = '';
        editEl.style.display = 'none';
    }
}

async function saveMessageEdit(msgId) {
    const textarea = document.getElementById('msg-textarea-' + msgId);
    const newContent = textarea.value.trim();
    if (!newContent) { alert('内容不能为空'); return; }
    
    try {
        const resp = await fetch(`/api/chat/messages/${msgId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: newContent })
        });
        if (resp.status === 404) { alert('消息编辑功能暂未启用'); return; }
        const data = await resp.json();
        if (data.error) {
            alert('保存失败: ' + data.error);
            return;
        }
        
        // 更新显示
        const contentEl = document.getElementById('msg-content-' + msgId);
        contentEl.textContent = newContent;
        toggleEditMessage(msgId);
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

// 删除单条消息
async function deleteSingleMessage(msgId) {
    if (!confirm('确定删除这条消息？此操作不可撤销。')) return;
    try {
        const resp = await fetch('/api/chat/messages/' + msgId, { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok || data.error) {
            alert('删除失败: ' + (data.error || 'HTTP ' + resp.status));
            return;
        }
        const msgEl = document.getElementById('msg-' + msgId);
        if (msgEl) msgEl.remove();
        const titleEl = document.getElementById('conv-detail-title');
        if (titleEl) {
            const m = titleEl.textContent.match(/(\d+)\s*\/\s*(\d+)/);
            if (m) {
                const loaded = parseInt(m[1]) - 1;
                const total = parseInt(m[2]) - 1;
                titleEl.textContent = `对话详情（${loaded} / ${total} 条消息）`;
            }
        }
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

// 删除对话
async function deleteConversation(sessionId) {
    if (!confirm('确定删除这个对话吗？（可在回收站恢复）')) return;
    
    try {
        const resp = await fetch(`/api/conversations/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
        const data = await resp.json();
        if (data.error) {
            alert('删除失败: ' + data.error);
            return;
        }
        closeConvDetail();
        if (convIsSearchMode) {
            searchConversations();
        } else {
            loadConversationList(convCurrentPage);
        }
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

// 多选功能
function toggleConvSelectAll(checked) {
    document.querySelectorAll('.conv-checkbox').forEach(cb => { cb.checked = checked; });
    updateConvSelectionCount();
}

function updateConvSelectionCount() {
    const checked = document.querySelectorAll('.conv-checkbox:checked');
    const countEl = document.getElementById('conv-selected-count');
    const btnEl = document.getElementById('conv-batch-delete-btn');
    const mergeBtn = document.getElementById('conv-batch-merge-btn');
    const allCb = document.getElementById('conv-select-all');
    const allCheckboxes = document.querySelectorAll('.conv-checkbox');
    
    if (checked.length > 0) {
        countEl.style.display = '';
        countEl.textContent = `已选 ${checked.length} 个`;
        btnEl.style.display = '';
        if (mergeBtn) mergeBtn.style.display = '';
    } else {
        countEl.style.display = 'none';
        btnEl.style.display = 'none';
        if (mergeBtn) mergeBtn.style.display = 'none';
    }
    
    if (allCb) {
        allCb.checked = allCheckboxes.length > 0 && checked.length === allCheckboxes.length;
    }
}

async function batchDeleteConversations() {
    const checked = document.querySelectorAll('.conv-checkbox:checked');
    if (checked.length === 0) return;
    
    if (!confirm(`确定删除选中的 ${checked.length} 个对话吗？（可在回收站恢复）`)) return;
    
    const sessionIds = Array.from(checked).map(cb => cb.value);
    
    try {
        const resp = await fetch('/api/conversations/batch-delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_ids: sessionIds })
        });
        const data = await resp.json();
        if (data.error) {
            alert('批量删除失败: ' + data.error);
            return;
        }
        
        if (convIsSearchMode) {
            searchConversations();
        } else {
            loadConversationList(convCurrentPage);
        }
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

async function batchMergeSessions() {
    const checked = document.querySelectorAll('.conv-checkbox:checked');
    if (checked.length === 0) return;
    
    const targetId = prompt('输入目标 Session ID（所有选中的对话将合并到这个session）:', 'interlocked');
    if (!targetId) return;
    
    const sessionIds = Array.from(checked).map(cb => cb.value);
    
    if (!confirm(`确定将选中的 ${sessionIds.length} 个对话合并到「${targetId}」吗？\n\n此操作不可撤销。`)) return;
    
    try {
        const resp = await fetch('/api/admin/merge-sessions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source_ids: sessionIds, target_id: targetId })
        });
        const data = await resp.json();
        if (data.error) {
            alert('合并失败: ' + data.error);
            return;
        }
        
        alert(`合并完成！\n${data.merged_sessions} 个session → ${targetId}\n${data.merged_messages} 条消息\n${data.merged_token_records} 条token记录`);
        loadConversationList(convCurrentPage);
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

// 工具函数
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function formatConvTime(isoStr) {
    try {
        const d = new Date(isoStr);
        const now = new Date();
        const diffMs = now - d;
        const diffDays = Math.floor(diffMs / 86400000);
        
        if (diffDays === 0) {
            return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        } else if (diffDays === 1) {
            return '昨天';
        } else if (diffDays < 7) {
            return diffDays + '天前';
        } else {
            return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
        }
    } catch(e) {
        return '';
    }
}

// ============================================
// 对话线管理
// ============================================

let _threadData = { threads: [], active_session_id: '' };
let _summaryEditSid = '';

async function loadThreads() {
    try {
        const [statusResp, threadsResp] = await Promise.all([
            fetch('/api/partition/status'),
            fetch('/api/partition/threads')
        ]);
        const status = await statusResp.json();
        const data = await threadsResp.json();
        _threadData = data;
        
        renderThreadStatus(status);
        renderThreadList(data.threads);
        updateCopyFromSelect(data.threads);
    } catch(e) {
        document.getElementById('thread-status').textContent = '加载失败: ' + e.message;
    }
}

function renderThreadStatus(status) {
    const el = document.getElementById('thread-status');
    if (!status.enabled) {
        el.innerHTML = '<span style="color: var(--danger);">⚠️ 分区缓存未启用（CACHE_PARTITION_ENABLED=false）</span>';
        return;
    }
    
    el.innerHTML = `
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px;">
            <div><strong>活跃对话线</strong><br><span style="font-size: 18px; color: var(--primary);">${status.active_session_id || '未设置'}</span></div>
            <div><strong>轮转周期</strong><br>每 ${status.partition_x} 轮</div>
            <div><strong>摘要长度</strong><br>${status.summary_length} 字</div>
            <div><strong>A区起始轮</strong><br>第 ${status.a_start_round} 轮</div>
        </div>
    `;
}

function renderThreadList(threads) {
    const el = document.getElementById('thread-list');
    if (!threads || threads.length === 0) {
        el.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px 0;">暂无对话线</div>';
        return;
    }
    
    let html = '';
    for (const t of threads) {
        const isActive = t.is_active;
        const tokens = t.chat_tokens > 0 ? (t.chat_tokens >= 1000 ? (t.chat_tokens / 1000).toFixed(1) + 'K' : t.chat_tokens) : '0';
        const summaryPreview = t.summary ? (t.summary.substring(0, 80) + (t.summary.length > 80 ? '...' : '')) : '（无摘要）';
        const updatedStr = t.updated_at ? formatConvTime(t.updated_at) : '';
        
        html += `
        <div style="border: 1px solid ${isActive ? 'var(--primary)' : 'var(--border)'}; border-radius: 8px; padding: 14px; margin-bottom: 8px; ${isActive ? 'background: var(--bg-card); box-shadow: 0 0 0 1px var(--primary);' : ''}">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="font-weight: 600; font-size: 15px;">${t.session_id}</span>
                    ${isActive ? '<span style="background: var(--primary); color: white; font-size: 11px; padding: 2px 8px; border-radius: 10px;">活跃</span>' : ''}
                </div>
                <div style="display: flex; gap: 6px;">
                    <button class="btn btn-sm" onclick="renameThread('${t.session_id}')">改名</button>
                    <button class="btn btn-sm" onclick="openSummaryModal('${t.session_id}')">摘要</button>
                    ${!isActive ? `<button class="btn btn-sm btn-primary" onclick="switchThread('${t.session_id}')">切换到此</button>` : ''}
                    ${!isActive ? `<button class="btn btn-sm" onclick="deleteThread('${t.session_id}', ${t.message_count || 0})" style="color: var(--error);">删除</button>` : ''}
                </div>
            </div>
            <div style="color: var(--text-muted); font-size: 13px; line-height: 1.5;">
                <div>${summaryPreview}</div>
                <div style="margin-top: 6px; display: flex; gap: 16px;">
                    <span>${t.message_count} 条消息</span>
                    <span>${tokens}</span>
                    <span>摘要 ${t.summary_length} 字</span>
                    ${updatedStr ? `<span>更新于 ${updatedStr}</span>` : ''}
                </div>
            </div>
        </div>`;
    }
    
    el.innerHTML = html;
}

function updateCopyFromSelect(threads) {
    const sel = document.getElementById('new-thread-copy-from');
    // 保留第一个option
    sel.innerHTML = '<option value="">不继承，从零开始</option>';
    for (const t of threads) {
        if (t.summary_length > 0) {
            sel.innerHTML += `<option value="${t.session_id}">${t.session_id} (${t.summary_length}字)</option>`;
        }
    }
}

async function createThread() {
    const newId = document.getElementById('new-thread-id').value.trim();
    const copyFrom = document.getElementById('new-thread-copy-from').value;
    const msgEl = document.getElementById('thread-create-msg');
    
    if (!newId) {
        msgEl.innerHTML = '<div style="color: var(--danger);">请输入对话线ID</div>';
        return;
    }
    
    try {
        const resp = await fetch('/api/partition/thread', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: newId, copy_summary_from: copyFrom })
        });
        const data = await resp.json();
        if (data.error) {
            msgEl.innerHTML = `<div style="color: var(--danger);">${data.error}</div>`;
            return;
        }
        
        const switchResp = await fetch('/api/partition/switch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: newId })
        });
        const switchData = await switchResp.json();
        if (switchData.error) {
            msgEl.innerHTML = `<div style="color: var(--danger);">创建成功，但切换失败: ${switchData.error}</div>`;
            loadThreads();
            return;
        }

        msgEl.innerHTML = `<div style="color: var(--success);">✅ 创建并切换成功${data.summary_length > 0 ? '（继承了' + data.summary_length + '字摘要）' : ''}</div>`;
        document.getElementById('new-thread-id').value = '';
        loadThreads();
    } catch(e) {
        msgEl.innerHTML = `<div style="color: var(--danger);">请求失败: ${e.message}</div>`;
    }
}

async function renameThread(oldId) {
    const newId = prompt(`请输入新的对话线ID（当前: ${oldId}）:`, oldId);
    if (!newId || newId.trim() === '' || newId.trim() === oldId) return;
    try {
        const resp = await fetch('/api/partition/thread/rename', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_id: oldId, new_id: newId.trim() })
        });
        const data = await resp.json();
        if (data.error) {
            alert('改名失败: ' + data.error);
            return;
        }
        loadThreads();
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

async function switchThread(sessionId) {
    if (!confirm(`确定切换到对话线「${sessionId}」吗？\n\n切换后所有平台的新消息将存入此对话线。`)) return;
    
    try {
        const resp = await fetch('/api/partition/switch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId })
        });
        const data = await resp.json();
        if (data.error) {
            alert('切换失败: ' + data.error);
            return;
        }
        loadThreads();
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

async function deleteThread(sessionId, messageCount) {
    let msg;
    if (messageCount > 0) {
        msg = `⚠️ 对话线「${sessionId}」包含 ${messageCount} 条消息。\n\n删除后对话线配置和摘要将被移除，消息本身不受影响但会失去对话线归属。\n\n确定删除？`;
    } else {
        msg = `确定删除对话线「${sessionId}」吗？\n\n这只会删除对话线配置和摘要。`;
    }
    if (!confirm(msg)) return;
    
    try {
        const resp = await fetch('/api/partition/thread/' + encodeURIComponent(sessionId), { method: 'DELETE' });
        const data = await resp.json();
        if (data.error) {
            alert('删除失败: ' + data.error);
            return;
        }
        loadThreads();
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

async function openSummaryModal(sessionId) {
    _summaryEditSid = sessionId;
    document.getElementById('summary-modal-sid').textContent = sessionId;
    
    // 获取完整摘要
    try {
        const resp = await fetch('/api/partition/status');
        const status = await resp.json();
        
        // 如果是活跃session就直接用status的摘要，否则单独获取
        let summary = '';
        if (sessionId === status.active_session_id) {
            summary = status.summary || '';
        } else {
            // 找对应thread的摘要
            const thread = _threadData.threads.find(t => t.session_id === sessionId);
            if (thread) summary = thread.summary || '';
        }
        
        document.getElementById('summary-editor').value = summary;
        updateSummaryCharCount();
        document.getElementById('summaryModal').style.display = 'flex';
    } catch(e) {
        alert('获取摘要失败: ' + e.message);
    }
}

function closeSummaryModal() {
    document.getElementById('summaryModal').style.display = 'none';
    _summaryEditSid = '';
}

function updateSummaryCharCount() {
    const text = document.getElementById('summary-editor').value;
    document.getElementById('summary-char-count').textContent = `${text.length} 字`;
}

// 绑定输入事件
document.addEventListener('DOMContentLoaded', () => {
    const editor = document.getElementById('summary-editor');
    if (editor) editor.addEventListener('input', updateSummaryCharCount);
});

async function saveSummary() {
    const summary = document.getElementById('summary-editor').value;
    
    try {
        const resp = await fetch('/api/partition/summary', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: _summaryEditSid, summary: summary })
        });
        const data = await resp.json();
        if (data.error) {
            alert('保存失败: ' + data.error);
            return;
        }
        
        closeSummaryModal();
        loadThreads();
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

async function clearSummary() {
    if (!confirm(`确定清空「${_summaryEditSid}」的摘要吗？此操作不可撤销。`)) return;
    
    try {
        const resp = await fetch('/api/partition/summary', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: _summaryEditSid })
        });
        const data = await resp.json();
        if (data.error) {
            alert('清空失败: ' + data.error);
            return;
        }
        
        closeSummaryModal();
        loadThreads();
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

// ============================================
// 记忆向量补算
// ============================================
let _backfillPollTimer = null;

async function startBackfillMemoryEmbeddings() {
    const btn = document.getElementById('backfillMemBtn');
    const progress = document.getElementById('backfill-mem-progress');
    const msgEl = document.getElementById('backfill-mem-msg');
    
    btn.disabled = true;
    btn.textContent = '启动中...';
    msgEl.innerHTML = '';
    
    try {
        const resp = await fetch('/api/admin/backfill-memory-embeddings', { method: 'POST' });
        
        if (!resp.ok) {
            const text = await resp.text();
            msgEl.innerHTML = `<span style="color: var(--danger);">❌ 服务器错误 (${resp.status})：${text.substring(0, 200)}</span>`;
            btn.disabled = false;
            btn.textContent = '开始补算';
            return;
        }
        
        const data = await resp.json();
        
        if (data.error) {
            msgEl.innerHTML = `<span style="color: var(--danger);">❌ ${data.error}</span>`;
            btn.disabled = false;
            btn.textContent = '开始补算';
            return;
        }
        
        if (data.status === 'done') {
            msgEl.innerHTML = `<span style="color: var(--success);">✅ ${data.message}</span>`;
            btn.disabled = false;
            btn.textContent = '开始补算';
            return;
        }
        
        progress.style.display = 'block';
        updateBackfillProgress(0, data.total);
        _backfillPollTimer = setInterval(pollBackfillStatus, 2000);
    } catch (e) {
        msgEl.innerHTML = `<span style="color: var(--danger);">❌ ${e.message}</span>`;
        btn.disabled = false;
        btn.textContent = '开始补算';
    }
}

async function pollBackfillStatus() {
    try {
        const resp = await fetch('/api/admin/backfill-memory-embeddings/status');
        const data = await resp.json();
        
        updateBackfillProgress(data.done, data.total);
        
        if (!data.running) {
            clearInterval(_backfillPollTimer);
            _backfillPollTimer = null;
            
            const btn = document.getElementById('backfillMemBtn');
            const msgEl = document.getElementById('backfill-mem-msg');
            btn.disabled = false;
            btn.textContent = '开始补算';
            
            if (data.error) {
                msgEl.innerHTML = `<span style="color: var(--danger);">❌ 补算出错：${data.error}</span>`;
            } else {
                msgEl.innerHTML = `<span style="color: var(--success);">✅ 补算完成！共处理 ${data.done} 条记忆</span>`;
            }
        }
    } catch (e) {
        console.error('轮询补算状态失败:', e);
    }
}

function updateBackfillProgress(done, total) {
    const bar = document.getElementById('backfill-mem-bar');
    const text = document.getElementById('backfill-mem-text');
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    bar.style.width = pct + '%';
    text.textContent = `${done}/${total} (${pct}%)`;
}


// ============================================
// 设置面板
// ============================================

let _settingsLoaded = false;
let _modelList = [];

// 所有需要读写的字段 key（开源版：EMBEDDING_API_KEY + EMBEDDING_BASE_URL）
const _SETTINGS_FIELDS = {
    str: ['API_BASE_URL', 'API_KEY', 'DEFAULT_MODEL', 'MEMORY_API_KEY', 'MEMORY_MODEL',
          'CACHE_SUMMARY_MODEL', 'CACHE_TTL', 'CACHE_PARTITION_TRIGGER', 'EMBEDDING_API_KEY', 'EMBEDDING_BASE_URL', 'EMBEDDING_MODEL', 'REASONING_EFFORT'],
    int: ['MAX_MEMORIES_INJECT', 'MEMORY_EXTRACT_INTERVAL', 'CACHE_PARTITION_X', 'CACHE_PARTITION_WINDOW', 'EMBEDDING_DIM'],
    float: ['MIN_SCORE_THRESHOLD'],
    bool: ['MEMORY_ENABLED', 'CACHE_PARTITION_ENABLED', 'MEMORY_VECTOR_ENABLED', 'FORCE_STREAM'],
    range: ['MEMORY_HW_KEYWORD', 'MEMORY_HW_SEMANTIC', 'MEMORY_HW_IMPORTANCE',
            'MEMORY_HW_RECENCY', 'MEMORY_SEMANTIC_THRESHOLD'],
    text: ['systemPrompt'],
};

const _MODEL_COMBOS = ['DEFAULT_MODEL', 'MEMORY_MODEL', 'CACHE_SUMMARY_MODEL'];

// 触发模式联动：time模式才显示时间窗口字段
function _togglePartitionWindow(trigger) {
    const el = document.getElementById('field-CACHE_PARTITION_WINDOW');
    if (el) el.style.display = trigger === 'time' ? '' : 'none';
}

async function loadSettings() {
    try {
        const resp = await fetch('/api/settings');
        const data = await resp.json();
        if (data.error) { showSettingsMsg('error', '加载失败: ' + data.error); return; }
        const s = data.settings;

        // 字符串字段
        _SETTINGS_FIELDS.str.forEach(k => {
            const el = document.getElementById('set-' + k);
            if (el) el.value = s[k] || '';
        });
        // 打码字段提示
        ['API_KEY', 'MEMORY_API_KEY', 'EMBEDDING_API_KEY'].forEach(k => {
            const hint = document.getElementById('set-' + k + '-hint');
            if (hint && s[k]) hint.textContent = '当前: ' + s[k];
        });
        // 整数
        _SETTINGS_FIELDS.int.forEach(k => {
            const el = document.getElementById('set-' + k);
            if (el) el.value = s[k];
        });
        // 浮点
        _SETTINGS_FIELDS.float.forEach(k => {
            const el = document.getElementById('set-' + k);
            if (el) el.value = s[k];
        });
        // 布尔（checkbox）
        _SETTINGS_FIELDS.bool.forEach(k => {
            const el = document.getElementById('set-' + k);
            if (el) el.checked = !!s[k];
        });
        // 滑块
        _SETTINGS_FIELDS.range.forEach(k => {
            const el = document.getElementById('set-' + k);
            if (el) { el.value = s[k]; updateSliderVal(k); }
        });
        // 长文本
        const promptEl = document.getElementById('set-systemPrompt');
        if (promptEl) {
            promptEl.value = s.systemPrompt || '';
            updatePromptCount();
        }
        // REASONING_EFFORT 下拉
        const reEl = document.getElementById('set-REASONING_EFFORT');
        if (reEl) reEl.value = s.REASONING_EFFORT || '';

        // CACHE_PARTITION_TRIGGER 下拉 + 联动时间窗口字段
        const triggerEl = document.getElementById('set-CACHE_PARTITION_TRIGGER');
        if (triggerEl) {
            triggerEl.value = s.CACHE_PARTITION_TRIGGER || 'rounds';
            _togglePartitionWindow(triggerEl.value);
            triggerEl.onchange = () => _togglePartitionWindow(triggerEl.value);
        }

        // 加载模型列表（首次）
        if (!_settingsLoaded) loadModelList();
        _settingsLoaded = true;
    } catch (e) {
        showSettingsMsg('error', '加载设置失败: ' + e.message);
    }
}

async function saveSettings() {
    const btn = document.getElementById('save-settings-btn');
    btn.disabled = true;
    btn.textContent = '保存中...';

    const payload = {};

    // 字符串
    _SETTINGS_FIELDS.str.forEach(k => {
        const el = document.getElementById('set-' + k);
        if (el) payload[k] = el.value;
    });
    // 整数
    _SETTINGS_FIELDS.int.forEach(k => {
        const el = document.getElementById('set-' + k);
        if (el) payload[k] = parseInt(el.value) || 0;
    });
    // 浮点
    _SETTINGS_FIELDS.float.forEach(k => {
        const el = document.getElementById('set-' + k);
        if (el) payload[k] = parseFloat(el.value) || 0;
    });
    // 布尔
    _SETTINGS_FIELDS.bool.forEach(k => {
        const el = document.getElementById('set-' + k);
        if (el) payload[k] = el.checked;
    });
    // 滑块
    _SETTINGS_FIELDS.range.forEach(k => {
        const el = document.getElementById('set-' + k);
        if (el) payload[k] = parseFloat(el.value) || 0;
    });
    // 长文本
    const promptEl = document.getElementById('set-systemPrompt');
    if (promptEl) payload.systemPrompt = promptEl.value;

    try {
        const resp = await fetch('/api/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (data.error) {
            showSettingsMsg('error', '保存失败: ' + data.error);
        } else {
            const msg = `已更新 ${data.updated?.length || 0} 项` +
                        (data.skipped?.length ? `，跳过 ${data.skipped.length} 项（未修改）` : '');
            showSettingsMsg('success', msg);
        }
    } catch (e) {
        showSettingsMsg('error', '保存失败: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '保存设置';
    }
}

async function loadModelList() {
    const hint = document.getElementById('model-count-hint');
    if (hint) hint.textContent = '加载模型列表...';
    try {
        const resp = await fetch('/api/models');
        const data = await resp.json();
        _modelList = data.models || [];

        _MODEL_COMBOS.forEach(fieldName => {
            renderComboDropdown(fieldName, _modelList);
        });

        if (hint) {
            hint.textContent = _modelList.length > 0
                ? `共 ${_modelList.length} 个可用模型 (${data.provider || ''})`
                : '无法获取模型列表，请手动输入';
        }
    } catch (e) {
        if (hint) hint.textContent = '模型列表加载失败';
    }
}

function renderComboDropdown(fieldName, models) {
    const dropdown = document.getElementById('dropdown-' + fieldName);
    if (!dropdown) return;
    dropdown.innerHTML = '';
    models.forEach(m => {
        const div = document.createElement('div');
        div.className = 'combo-option';
        div.textContent = m.name || m.id;
        div.dataset.value = m.id;
        div.addEventListener('click', () => {
            document.getElementById('set-' + fieldName).value = m.id;
            dropdown.classList.remove('open');
        });
        dropdown.appendChild(div);
    });
}

function filterCombo(fieldName) {
    const input = document.getElementById('set-' + fieldName);
    const dropdown = document.getElementById('dropdown-' + fieldName);
    if (!input || !dropdown) return;
    const q = input.value.toLowerCase();
    let visible = 0;
    dropdown.querySelectorAll('.combo-option').forEach(opt => {
        const match = !q || opt.textContent.toLowerCase().includes(q) || (opt.dataset.value || '').toLowerCase().includes(q);
        opt.style.display = match ? '' : 'none';
        if (match) visible++;
    });
    if (visible > 0 && q) dropdown.classList.add('open');
}

// 初始化 combo-box 交互
document.addEventListener('DOMContentLoaded', () => {
    _MODEL_COMBOS.forEach(fieldName => {
        const input = document.getElementById('set-' + fieldName);
        const dropdown = document.getElementById('dropdown-' + fieldName);
        if (!input || !dropdown) return;

        input.addEventListener('focus', () => { dropdown.classList.add('open'); });
        input.addEventListener('input', () => { filterCombo(fieldName); });
    });

    // 点击外部关闭所有 combo
    document.addEventListener('click', (e) => {
        _MODEL_COMBOS.forEach(fieldName => {
            const box = document.getElementById('combo-' + fieldName);
            const dropdown = document.getElementById('dropdown-' + fieldName);
            if (box && dropdown && !box.contains(e.target)) {
                dropdown.classList.remove('open');
            }
        });
    });
});

function updateSliderVal(key) {
    const el = document.getElementById('set-' + key);
    const span = document.getElementById('val-' + key);
    if (el && span) span.textContent = parseFloat(el.value).toFixed(2);
}

function updatePromptCount() {
    const el = document.getElementById('set-systemPrompt');
    const hint = document.getElementById('prompt-char-count');
    if (el && hint) hint.textContent = el.value.length + ' 字';
}

// 绑定 prompt 字数实时更新
document.addEventListener('DOMContentLoaded', () => {
    const p = document.getElementById('set-systemPrompt');
    if (p) p.addEventListener('input', updatePromptCount);
});

function showSettingsMsg(type, text) {
    const el = document.getElementById('settings-msg');
    if (!el) return;
    el.style.display = 'block';
    el.className = 'msg-box msg-' + type;
    el.textContent = text;
    setTimeout(() => { el.style.display = 'none'; }, 5000);
}
