// gem_apv.js — GEM APV waveform viewer tab
//
// Stacked per-GEM sections (one per detector, ordered by det_id),
// each with a faint background tint and a thin separator above it.
// Inside each section is a responsive grid of small canvas panels
// — one per APV — drawing the 6 time-sample traces (blue → red),
// the zero line, and a hit-row tick under the plot.
//
// Mirrors the simplified controls from gem_event_viewer.py's RawApvTab:
// Process / Signal Only / Shared Y / per-sample (t0…t5) toggles.
// Threshold curve, CM overlay, and clustering knobs are intentionally
// left out — the monitor is for at-a-glance live inspection.

'use strict';

// Per-GEM tints — same palette as gem_event_viewer.py's "All" tab so
// the desktop and web monitor agree on "GEM 1 is green".
const GEM_APV_TINTS = [
    'rgba(0, 180, 216, 0.13)',
    'rgba(81, 207, 102, 0.13)',
    'rgba(255, 146, 43, 0.13)',
    'rgba(204, 93, 232, 0.13)',
];
function gemApvTint(detId) {
    if (detId < 0) return 'transparent';
    return GEM_APV_TINTS[detId % GEM_APV_TINTS.length];
}

// Time-sample trace colours: HSV blue→red, matches ApvPanel._paint.
const GEM_APV_TS_COLORS = (() => {
    const out = [];
    for (let t = 0; t < 6; t++) {
        const frac = t / 5;
        // hue 0.66 (blue) → 0 (red)
        const h = 0.66 * (1 - frac);
        out.push(hsv2rgb(h, 0.85, 0.95));
    }
    return out;
})();
function hsv2rgb(h, s, v) {
    const i = Math.floor(h * 6);
    const f = h * 6 - i;
    const p = v * (1 - s);
    const q = v * (1 - f * s);
    const t = v * (1 - (1 - f) * s);
    let r, g, b;
    switch (i % 6) {
        case 0: r = v; g = t; b = p; break;
        case 1: r = q; g = v; b = p; break;
        case 2: r = p; g = v; b = t; break;
        case 3: r = p; g = q; b = v; break;
        case 4: r = t; g = p; b = v; break;
        case 5: r = v; g = p; b = q; break;
    }
    return `rgb(${Math.round(r*255)},${Math.round(g*255)},${Math.round(b*255)})`;
}

// Tab state.
let gemApvData = null;          // last fetched {detectors:[], apvs:[]}
let gemApvCurrentEvent = -1;
let gemApvShowProcessed = true;
let gemApvShowSignalOnly = false;
let gemApvSharedY = true;
let gemApvSampleMask = [true, true, true, true, true, true];
let gemApvBuiltKey = '';        // signature of section layout currently in DOM
const gemApvCanvases = new Map(); // apv_id → canvas element

// Panel size — driven by CSS minmax.  Canvas pixel size matches its
// CSS box at render time so traces stay crisp on HiDPI screens.
const GEM_APV_TITLE_H  = 16;
const GEM_APV_HIT_ROW_H = 6;

// =====================================================================
// Fetch + section build
// =====================================================================

function fetchGemApvData(evnum) {
    if (typeof evnum !== 'number' || evnum <= 0) return;
    fetch(`/api/gem/apv/${evnum}`)
        .then(r => {
            if (!r.ok) throw new Error('http ' + r.status);
            return r.json();
        })
        .then(data => {
            if (data.error) {
                gemApvSetStatus(data.error);
                return;
            }
            gemApvData = data;
            gemApvCurrentEvent = evnum;
            buildGemApvSections();
            renderGemApvPanels();
        })
        .catch(err => gemApvSetStatus('Fetch error: ' + err));
}

function gemApvSetStatus(text) {
    const el = document.getElementById('gem-apv-stats');
    if (el) el.textContent = text;
}

// Rebuild the section/grid skeleton if the detector layout changed
// (different file, different config).  Cheap when called repeatedly
// with the same data — the skeleton is keyed by det list signature.
function buildGemApvSections() {
    const body = document.getElementById('gem-apv-body');
    if (!body || !gemApvData) return;
    if (!gemApvData.enabled) {
        body.innerHTML = '<div style="padding:40px;text-align:center;color:var(--dim)">GEM not enabled</div>';
        gemApvBuiltKey = '_disabled_';
        return;
    }

    const dets = (gemApvData.detectors || []).slice()
        .sort((a, b) => (a.id - b.id));
    const apvs = gemApvData.apvs || [];

    // Group APVs by det_id, sorted by (plane, crate, mpd, adc).
    const byDet = new Map();
    for (const det of dets) byDet.set(det.id, []);
    for (const apv of apvs) {
        const arr = byDet.get(apv.det_id);
        if (arr) arr.push(apv);
    }
    for (const [, arr] of byDet) {
        arr.sort((a, b) =>
            (a.plane || '').localeCompare(b.plane || '') ||
            (a.crate - b.crate) ||
            (a.mpd   - b.mpd) ||
            (a.adc   - b.adc));
    }

    // Skeleton signature — only rebuild when the per-det APV list changes.
    const key = dets.map(d => `${d.id}:${(byDet.get(d.id) || []).map(a => a.id).join('-')}`).join('|');
    if (key === gemApvBuiltKey) return;
    gemApvBuiltKey = key;

    body.innerHTML = '';
    gemApvCanvases.clear();

    dets.forEach((det, sectionIdx) => {
        if (sectionIdx > 0) {
            const sep = document.createElement('div');
            sep.className = 'gem-apv-sep';
            body.appendChild(sep);
        }
        const section = document.createElement('div');
        section.className = 'gem-apv-section';
        section.dataset.det = det.id;
        section.style.background = gemApvTint(det.id);

        const header = document.createElement('div');
        header.className = 'gem-apv-section-header';
        header.textContent = `GEM ${det.id} — ${det.name}   (${det.n_apvs} APVs)`;
        section.appendChild(header);

        const grid = document.createElement('div');
        grid.className = 'gem-apv-grid';
        section.appendChild(grid);

        const apvsHere = byDet.get(det.id) || [];
        for (const apv of apvsHere) {
            const panel = document.createElement('div');
            panel.className = 'gem-apv-panel';
            panel.dataset.apvId = apv.id;
            const canvas = document.createElement('canvas');
            canvas.className = 'gem-apv-canvas';
            panel.appendChild(canvas);
            grid.appendChild(panel);
            gemApvCanvases.set(apv.id, canvas);
        }
        body.appendChild(section);
    });
}

// =====================================================================
// Render — called whenever data or controls change
// =====================================================================

function renderGemApvPanels() {
    if (!gemApvData || !gemApvData.enabled) return;
    const apvs  = gemApvData.apvs || [];
    const field = gemApvShowProcessed ? 'processed' : 'raw';

    // Compute global Y range across visible (non-filtered) APVs.
    let yLo = Infinity, yHi = -Infinity;
    if (gemApvSharedY) {
        for (const apv of apvs) {
            if (gemApvShowSignalOnly && !apvHasSignal(apv)) continue;
            const f = apv[field];
            if (!f) continue;
            for (let s = 0; s < f.length; s++) {
                for (let t = 0; t < 6; t++) {
                    if (!gemApvSampleMask[t]) continue;
                    const v = f[s][t];
                    if (v < yLo) yLo = v;
                    if (v > yHi) yHi = v;
                }
            }
        }
    }
    if (!isFinite(yLo) || !isFinite(yHi)) { yLo = 0; yHi = 1; }
    if (yHi - yLo < 8) {
        const m = 0.5 * (yLo + yHi);
        yLo = m - 4; yHi = m + 4;
    }
    const pad = 0.08 * (yHi - yLo);
    yLo -= pad; yHi += pad;
    const sharedRange = gemApvSharedY ? [yLo, yHi] : null;

    let total = 0, shown = 0;
    for (const apv of apvs) {
        total++;
        const panel = panelOf(apv.id);
        if (!panel) continue;
        const hasSig = apvHasSignal(apv);
        if (gemApvShowSignalOnly && !hasSig) {
            panel.style.display = 'none';
            continue;
        }
        panel.style.display = '';
        shown++;
        const canvas = gemApvCanvases.get(apv.id);
        if (canvas) drawApvCanvas(canvas, apv, field, sharedRange);
    }

    const mode = gemApvShowProcessed ? 'processed' : 'raw';
    const evlbl = gemApvCurrentEvent > 0 ? `evt ${gemApvCurrentEvent}` : '';
    gemApvSetStatus(`${shown}/${total} APVs  [${mode}]  ${evlbl}`);
}

function apvHasSignal(apv) {
    const h = apv.hits;
    if (!h) return false;
    for (let i = 0; i < h.length; i++) if (h[i]) return true;
    return false;
}

function panelOf(apvId) {
    const c = gemApvCanvases.get(apvId);
    return c ? c.parentElement : null;
}

// Draw a single APV: title (top), 6 trace lines, zero line, hit-tick row.
function drawApvCanvas(canvas, apv, field, sharedRange) {
    // Match the canvas pixel buffer to its CSS box for sharp lines.
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 240;
    const cssH = canvas.clientHeight || 160;
    if (canvas.width  !== Math.round(cssW * dpr) ||
        canvas.height !== Math.round(cssH * dpr)) {
        canvas.width  = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const W = cssW, H = cssH;
    // Canvas background: keep slightly darker than the section tint so
    // the panel reads as a "tile" sitting on the tinted GEM row.
    ctx.fillStyle = THEME && THEME.canvas ? THEME.canvas : '#11112a';
    ctx.fillRect(0, 0, W, H);

    // Border — red if firmware reported full readout but no ZS hits,
    // accent if any ZS survivors, otherwise neutral.
    let borderCol = THEME && THEME.border ? THEME.border : '#333';
    let borderW = 1;
    if (apv.no_hit_fr) {
        borderCol = THEME && THEME.danger ? THEME.danger : '#ff6b6b';
    } else if (apvHasSignal(apv) && !gemApvShowSignalOnly) {
        borderCol = THEME && THEME.accent ? THEME.accent : '#ffd166';
        borderW = 2;
    }
    ctx.strokeStyle = borderCol;
    ctx.lineWidth = borderW;
    ctx.strokeRect(0.5, 0.5, W - 1, H - 1);

    // Title row.
    const titleH = GEM_APV_TITLE_H;
    ctx.fillStyle = THEME && THEME.text ? THEME.text : '#e0e0e0';
    ctx.font = 'bold 10px ui-monospace, monospace';
    ctx.textBaseline = 'middle';
    const title = `c${apv.crate} m${apv.mpd} a${apv.adc}  ${apv.plane} p${apv.det_pos}`;
    ctx.fillText(title, 4, titleH / 2);
    if (apv.no_hit_fr) {
        ctx.fillStyle = THEME && THEME.danger ? THEME.danger : '#ff6b6b';
        ctx.textAlign = 'right';
        ctx.fillText('no hits', W - 4, titleH / 2);
        ctx.textAlign = 'start';
    }

    // Plot region.
    const hitH = GEM_APV_HIT_ROW_H;
    const plotX = 4, plotY = titleH + 2;
    const plotW = W - 8, plotH = H - titleH - hitH - 6;
    if (plotW <= 0 || plotH <= 0) return;

    // Y range.
    const frame = apv[field];
    let yLo, yHi;
    if (sharedRange) {
        yLo = sharedRange[0]; yHi = sharedRange[1];
    } else {
        yLo = Infinity; yHi = -Infinity;
        if (frame) {
            for (let s = 0; s < frame.length; s++) {
                for (let t = 0; t < 6; t++) {
                    if (!gemApvSampleMask[t]) continue;
                    const v = frame[s][t];
                    if (v < yLo) yLo = v;
                    if (v > yHi) yHi = v;
                }
            }
        }
        if (!isFinite(yLo) || !isFinite(yHi)) { yLo = 0; yHi = 1; }
        if (yHi - yLo < 8) { const m = 0.5*(yLo+yHi); yLo = m-4; yHi = m+4; }
        const pad = 0.08 * (yHi - yLo);
        yLo -= pad; yHi += pad;
    }
    const ySpan = (yHi - yLo) || 1;
    const toY = v => plotY + plotH - (v - yLo) / ySpan * plotH;

    // Zero line if it's in range.
    if (yLo < 0 && yHi > 0) {
        ctx.strokeStyle = THEME && THEME.textDim ? THEME.textDim : '#888';
        ctx.lineWidth = 0.5;
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        const zy = toY(0);
        ctx.moveTo(plotX, zy); ctx.lineTo(plotX + plotW, zy);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    // Time-sample traces.
    if (frame && frame.length > 0) {
        const nStrips = Math.min(128, frame.length);
        const stepX = plotW / Math.max(nStrips - 1, 1);
        ctx.lineWidth = 0.9;
        for (let t = 0; t < 6; t++) {
            if (!gemApvSampleMask[t]) continue;
            ctx.strokeStyle = GEM_APV_TS_COLORS[t];
            ctx.beginPath();
            for (let s = 0; s < nStrips; s++) {
                const x = plotX + s * stepX;
                const y = toY(frame[s][t]);
                if (s === 0) ctx.moveTo(x, y);
                else         ctx.lineTo(x, y);
            }
            ctx.stroke();
        }
    }

    // ZS hit tick row.
    if (apv.hits && apv.hits.length > 0) {
        const nStrips = Math.min(128, apv.hits.length);
        const stepX = plotW / Math.max(nStrips - 1, 1);
        const rowY = H - hitH - 2;
        ctx.fillStyle = THEME && THEME.accent ? THEME.accent : '#ffd166';
        for (let s = 0; s < nStrips; s++) {
            if (apv.hits[s]) {
                const x = plotX + s * stepX;
                ctx.fillRect(x - 0.8, rowY, 1.6, hitH);
            }
        }
    }

    // Tiny Y-range labels in the plot corners — useful when shared Y is
    // off so each panel's auto-scale is visible at a glance.
    ctx.fillStyle = THEME && THEME.textDim ? THEME.textDim : '#888';
    ctx.font = '8px ui-monospace, monospace';
    ctx.textBaseline = 'top';
    ctx.fillText(fmtCompact(yHi), plotX + 2, plotY + 1);
    ctx.textBaseline = 'bottom';
    ctx.fillText(fmtCompact(yLo), plotX + 2, plotY + plotH - 1);
    ctx.textBaseline = 'alphabetic';
}

function fmtCompact(v) {
    if (Math.abs(v) < 1000) return v.toFixed(0);
    return (v / 1000).toFixed(1) + 'k';
}

// =====================================================================
// Controls
// =====================================================================

function setupGemApvControls() {
    const cb = (id, on) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.checked = on;
        el.onchange = () => {
            switch (id) {
                case 'gem-apv-process':
                    gemApvShowProcessed = el.checked; break;
                case 'gem-apv-signal-only':
                    gemApvShowSignalOnly = el.checked; break;
                case 'gem-apv-shared-y':
                    gemApvSharedY = el.checked; break;
            }
            renderGemApvPanels();
        };
    };
    cb('gem-apv-process',     gemApvShowProcessed);
    cb('gem-apv-signal-only', gemApvShowSignalOnly);
    cb('gem-apv-shared-y',    gemApvSharedY);
    for (let t = 0; t < 6; t++) {
        const el = document.getElementById('gem-apv-t' + t);
        if (!el) continue;
        el.checked = gemApvSampleMask[t];
        el.onchange = () => {
            gemApvSampleMask[t] = el.checked;
            renderGemApvPanels();
        };
    }
}

// Resize: just redraw — CSS auto-grid handles re-flow, canvas redraw
// adapts to new clientWidth/clientHeight on each render call.
function resizeGemApv() {
    if (!gemApvData || !gemApvData.enabled) return;
    renderGemApvPanels();
}
