// =========================================================================
// File browser (lazy-loading)
// =========================================================================

function openFileDialog() {
    const hdr = document.querySelector('.file-dialog-header span');
    const list = document.getElementById('file-list');
    const filter = document.getElementById('file-filter');
    const opts = document.querySelector('.file-dialog-options');

    document.getElementById('file-dialog').classList.add('open');
    document.getElementById('file-backdrop').classList.add('open');

    if (!g_dataDirEnabled) {
        hdr.textContent = 'Open EVIO File';
        filter.style.display = 'none';
        if (opts) opts.style.display = 'none';
        list.innerHTML = '<div style="padding:20px;color:var(--dim);text-align:center">'
            + 'No data folder configured.<br>Start with <code>--data-dir /path</code></div>';
        return;
    }

    hdr.textContent = `Open EVIO File — ${g_dataDir}`;
    filter.style.display = '';
    if (opts) opts.style.display = '';
    filter.value = '';
    list.innerHTML = '';

    fetchDirEntries('', list);
    filter.focus();
}

function closeFileDialog() {
    document.getElementById('file-dialog').classList.remove('open');
    document.getElementById('file-backdrop').classList.remove('open');
}

function fetchDirEntries(dir, container) {
    const url = dir ? `/api/files?dir=${encodeURIComponent(dir)}` : '/api/files';
    fetch(url).then(r => r.json()).then(data => {
        const entries = data.entries || [];
        if (!entries.length) {
            container.innerHTML = '<div style="padding:12px;color:var(--dim);text-align:center">Empty</div>';
            return;
        }
        renderEntries(entries, container);
    });
}

function renderEntries(entries, container) {
    const currentFile = g_currentFile || '';
    for (const e of entries) {
        if (e.type === 'dir') {
            const dirName = e.name.split('/').pop() || e.name;
            const row = document.createElement('div');
            row.className = 'file-folder';
            row.innerHTML = `<span class="folder-arrow">▸</span><span>${dirName}/</span>`
                + `<span class="folder-count">(${e.count})</span>`;
            const contents = document.createElement('div');
            contents.className = 'file-folder-contents';
            let loaded = false;
            row.onclick = () => {
                const open = contents.classList.toggle('open');
                row.querySelector('.folder-arrow').textContent = open ? '▾' : '▸';
                if (open && !loaded) {
                    loaded = true;
                    fetchDirEntries(e.name, contents);
                }
            };
            container.appendChild(row);
            container.appendChild(contents);
        } else {
            const fname = e.name.split('/').pop() || e.name;
            const isCurrent = e.name === currentFile;
            const row = document.createElement('div');
            row.className = 'file-item' + (isCurrent ? ' current' : '');
            row.innerHTML = `<span>${fname}</span><span class="fsize">${e.size_mb} MB</span>`;
            row.onclick = () => { closeFileDialog(); loadNewFile(e.name); };
            container.appendChild(row);
        }
    }
}

function filterFileList(text) {
    const filt = text.toLowerCase();
    const list = document.getElementById('file-list');
    // Filter visible folder/file rows by name
    for (const el of list.querySelectorAll('.file-folder')) {
        const name = el.textContent.toLowerCase();
        const contents = el.nextElementSibling;
        const match = !filt || name.includes(filt);
        el.style.display = match ? '' : 'none';
        if (contents && contents.classList.contains('file-folder-contents'))
            contents.style.display = match && contents.classList.contains('open') ? '' : 'none';
    }
    for (const el of list.querySelectorAll('.file-item')) {
        // only filter root-level items (not inside folders)
        if (el.parentElement === list) {
            const name = el.textContent.toLowerCase();
            el.style.display = (!filt || name.includes(filt)) ? '' : 'none';
        }
    }
}

let g_currentFile = '';
let g_histCheckbox = false;
let g_dataDirEnabled = false;
let g_dataDir = '';

function loadNewFile(relpath) {
    g_histCheckbox = document.getElementById('hist-checkbox').checked;
    document.getElementById('status-bar').textContent = `Loading ${relpath}...`;
    const histParam = g_histCheckbox ? '1' : '0';
    fetch(`/api/load?file=${encodeURIComponent(relpath)}&hist=${histParam}`).then(r => r.json()).then(data => {
        if (data.error) {
            document.getElementById('status-bar').textContent = data.error;
            return;
        }
        // show progress overlay and start polling
        showProgress(relpath);
    });
}

function showProgress(filename) {
    document.getElementById('progress-overlay').classList.add('active');
    document.getElementById('progress-title').textContent = `Loading ${filename.replace(/.*\//, '')}`;
    document.getElementById('progress-bar').style.width = '0%';
    document.getElementById('progress-text').textContent = 'Starting...';
    pollProgress();
}

function pollProgress() {
    fetch('/api/progress').then(r => r.json()).then(data => {
        if (!data.loading) {
            // done — hide overlay, reload config + first event
            document.getElementById('progress-overlay').classList.remove('active');
            fetchConfigAndApply();
            return;
        }
        const pct = data.total > 0 ? Math.round(100 * data.current / data.total) : 0;
        const phaseText = data.phase === 'indexing' ? 'Indexing events' : 'Building histograms';
        document.getElementById('progress-bar').style.width = data.total > 0 ? `${Math.min(pct, 100)}%` : '100%';
        document.getElementById('progress-text').textContent = data.total > 0
            ? `${phaseText}... ${data.current} / ${data.total}`
            : `${phaseText}...`;
        setTimeout(pollProgress, 300);
    }).catch(() => setTimeout(pollProgress, 1000));
}

function fetchOccupancy() {
    fetch('/api/occupancy').then(r => r.json()).then(data => {
        occData = data.occ || {};
        occTcutData = data.occ_tcut || {};
        occTotal = data.total || 0;
        // redraw if currently showing occupancy on DQ tab
        if (activeTab === 'dq' && document.getElementById('color-metric').value === 'occupancy') {
            syncDqRange();
            geoDq();
        }
        updateGeoTooltip();
    }).catch(() => {});
}


let sampleCount=0;
function updateHeaderStats(){
    const el=document.getElementById('header-stats');
    if(mode==='online'){
        el.textContent=`${sampleCount} samples`;
    } else {
        el.textContent=`${totalEvents} events`;
    }
}

