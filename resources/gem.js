// gem.js — GEM detector visualization tab
//
// Left column:  per-event 2D cluster scatter (two planes stacked)
// Right column: accumulated cluster occupancy heatmaps
//
// Hit coordinates from the backend are centered: (0,0) = beam center.

'use strict';

// --- configuration ----------------------------------------------------------
const GEM_COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728'];
const GEM_PLANES = [
    { name: 'Plane 1 (upstream)',   dets: [0, 1], hitId: 'gem-plane-0' },
    { name: 'Plane 2 (downstream)', dets: [2, 3], hitId: 'gem-plane-1' },
];

// Theme-aware layout factories (read from the active THEME at call time).
function PL_GEM() {
    return {
        ...plotlyLayout(),
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor:  THEME.canvas,
        font: { color: THEME.text, size: 11 },
        margin: { l: 50, r: 20, t: 30, b: 40 },
        hovermode: 'closest',
    };
}

function PL_GEM_OCC() {
    return {
        ...plotlyLayout(),
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor:  THEME.canvas,
        font: { color: THEME.text, size: 10 },
        margin: { l: 45, r: 10, t: 28, b: 32 },
        hovermode: 'closest',
        showlegend: false,
    };
}

let gemConfig = null;

// --- helpers ----------------------------------------------------------------

function gemDetInfo(detId) {
    const def = { xSize: 614.4, ySize: 512.0, xOff: 0, yOff: 0 };
    if (!gemConfig || !gemConfig.layers) return def;
    const layer = gemConfig.layers.find(l => l.id === detId);
    if (!layer) return def;
    const pos = layer.position || [0, 0, 0];
    return {
        xSize: layer.x_size || layer.x_apvs * 128 * layer.x_pitch,
        ySize: layer.y_size || layer.y_apvs * 128 * layer.y_pitch,
        xOff:  pos[0] || 0,
        yOff:  pos[1] || 0,
    };
}

// --- fetch + render ---------------------------------------------------------

function fetchGemData() {
    const configReady = gemConfig
        ? Promise.resolve(gemConfig)
        : fetch('/api/gem/config').then(r => r.json()).then(cfg => { gemConfig = cfg; return cfg; });

    configReady.then(() => {
        fetch('/api/gem/hits').then(r => r.json()).then(plotGemHits).catch(() => {});
    });
}

function fetchGemAccum() {
    fetch('/api/gem/occupancy').then(r => r.json()).then(plotGemOccupancy).catch(() => {});
    fetch('/api/gem/hist').then(r => r.json()).then(plotGemHist).catch(() => {});
}

// --- event cluster scatter (left) -------------------------------------------

function plotGemHits(data) {
    if (!data || !data.enabled) {
        GEM_PLANES.forEach(plane => {
            const div = document.getElementById(plane.hitId);
            if (div) div.innerHTML = '<div style="color:var(--dim);padding:40px;text-align:center">GEM not enabled</div>';
        });
        return;
    }

    const detectors = data.detectors || [];

    GEM_PLANES.forEach((plane) => {
        const traces = [];
        const shapes = [];
        // Compute the plane's geometric bounds from the static detector
        // outlines (NOT from this event's hits) so the axes don't snap
        // around as hits move from event to event.
        let xMin = +Infinity, xMax = -Infinity;
        let yMin = +Infinity, yMax = -Infinity;

        plane.dets.forEach((detId) => {
            const det = detectors.find(d => d.id === detId);
            if (!det) return;

            const hits = det.hits_2d || [];
            const color = GEM_COLORS[detId] || THEME.textDim;
            const detName = det.name || ('GEM' + detId);

            traces.push({
                x: hits.map(h => h.x),
                y: hits.map(h => h.y),
                mode: 'markers',
                type: 'scatter',
                name: detName,
                marker: {
                    color: color, size: 6, opacity: 0.8,
                    line: { width: 0.5, color: THEME.selectBorder },
                },
                hovertemplate: detName + '<br>x=%{x:.1f} mm<br>y=%{y:.1f} mm<extra></extra>',
            });

            // detector outline — offset to lab frame position
            const info = gemDetInfo(detId);
            const x0 = info.xOff - info.xSize / 2;
            const x1 = info.xOff + info.xSize / 2;
            const y0 = info.yOff - info.ySize / 2;
            const y1 = info.yOff + info.ySize / 2;
            if (x0 < xMin) xMin = x0;
            if (x1 > xMax) xMax = x1;
            if (y0 < yMin) yMin = y0;
            if (y1 > yMax) yMax = y1;
            shapes.push({
                type: 'rect',
                x0, y0, x1, y1,
                line: { color: color, width: 1.5, dash: 'dot' },
                fillcolor: 'rgba(0,0,0,0)',
            });
        });

        if (traces.length === 0) {
            traces.push({ x: [], y: [], mode: 'markers', type: 'scatter',
                          name: 'No data', marker: { size: 0 } });
        }

        // Pad the bounds slightly so outline strokes aren't clipped, and
        // fall back to a sane default if no detector is present on the
        // plane (gemConfig late-arriving).
        let xRange = null, yRange = null;
        if (isFinite(xMin) && isFinite(xMax)) {
            const xPad = (xMax - xMin) * 0.04;
            const yPad = (yMax - yMin) * 0.04;
            xRange = [xMin - xPad, xMax + xPad];
            yRange = [yMin - yPad, yMax + yPad];
        }

        const layout = Object.assign({}, PL_GEM(), {
            title: { text: plane.name, font: { size: 13, color: THEME.text } },
            xaxis: { title: 'X (mm)', scaleanchor: 'y', scaleratio: 1,
                     range: xRange, autorange: xRange ? false : true,
                     gridcolor: THEME.grid, zerolinecolor: THEME.border },
            yaxis: { title: 'Y (mm)',
                     range: yRange, autorange: yRange ? false : true,
                     gridcolor: THEME.grid, zerolinecolor: THEME.border },
            shapes: shapes,
            showlegend: true,
            legend: { x: 0.01, y: 0.99, bgcolor: 'rgba(0,0,0,0.3)', font: { size: 10 } },
        });

        Plotly.react(plane.hitId, traces, layout, { responsive: true, displayModeBar: false });
    });
}

// --- occupancy heatmap (right, 2x2 per-detector) ---------------------------

const GEM_OCC_IDS = ['gem-occ-0', 'gem-occ-1', 'gem-occ-2', 'gem-occ-3'];

function plotGemOccupancy(data) {
    if (!data || !data.enabled) {
        GEM_OCC_IDS.forEach(id => {
            const div = document.getElementById(id);
            if (div) div.innerHTML = '<div style="color:var(--dim);padding:20px;text-align:center">GEM not enabled</div>';
        });
        return;
    }

    const detectors = data.detectors || [];
    const total = data.total || 0;
    const scale = total > 0 ? 1.0 / total : 0;

    // Pre-compute per-detector z matrices and the global zmax so all four
    // heatmaps share one colour axis.  Sharing the scale lets the eye
    // compare absolute occupancy across GEMs, not just shape per GEM.
    const dets = GEM_OCC_IDS.map((_, detId) => detectors.find(d => d.id === detId));
    const grids = dets.map(det => {
        if (!det) return null;
        const nx = det.nx, ny = det.ny;
        const z = [];
        let local_max = 0;
        for (let iy = 0; iy < ny; iy++) {
            const row = [];
            for (let ix = 0; ix < nx; ix++) {
                const v = (det.bins[iy * nx + ix] || 0) * scale;
                row.push(v);
                if (v > local_max) local_max = v;
            }
            z.push(row);
        }
        return { det, z, local_max };
    });
    let zmax = 0;
    for (const g of grids) if (g && g.local_max > zmax) zmax = g.local_max;
    if (zmax <= 0) zmax = 1e-6;   // avoid Plotly auto-scaling to a flat plot

    // Compact per-heatmap layout: thin colourbar only on the right column
    // (cells 1 and 3), no axis titles, small title font.  Margins tightened
    // so the 2x2 grid uses its real estate for the heatmaps, not whitespace.
    const compactMargin  = { l: 28, r: 8,  t: 18, b: 20 };
    const compactMarginR = { l: 28, r: 42, t: 18, b: 20 };  // reserve for colourbar

    GEM_OCC_IDS.forEach((divId, idx) => {
        const g = grids[idx];
        const onRightCol = (idx % 2) === 1;   // 0,2 left | 1,3 right
        const showBar = onRightCol;
        const titleText = g && g.det
            ? g.det.name + (total > 0 ? ` (${total})` : '')
            : 'GEM' + idx;

        const layout = Object.assign({}, PL_GEM_OCC(), {
            title: { text: titleText, font: { size: 11, color: THEME.text } },
            xaxis: { gridcolor: THEME.grid, zerolinecolor: THEME.border, ticks: 'outside', ticklen: 3 },
            yaxis: { gridcolor: THEME.grid, zerolinecolor: THEME.border, ticks: 'outside', ticklen: 3 },
            margin: showBar ? compactMarginR : compactMargin,
        });

        if (!g) {
            Plotly.react(divId,
                [{ x: [], y: [], z: [[]], type: 'heatmap' }],
                layout, { responsive: true, displayModeBar: false });
            return;
        }

        const det = g.det;
        const nx = det.nx, ny = det.ny;
        const xStep = det.x_size / nx, yStep = det.y_size / ny;
        const x0 = -det.x_size / 2 + xStep / 2;
        const y0 = -det.y_size / 2 + yStep / 2;
        const xArr = Array.from({length: nx}, (_, i) => x0 + i * xStep);
        const yArr = Array.from({length: ny}, (_, i) => y0 + i * yStep);

        const trace = {
            x: xArr, y: yArr, z: g.z,
            type: 'heatmap',
            colorscale: 'Hot',
            zmin: 0, zmax: zmax,
            zauto: false,
            hovertemplate: det.name + '<br>x=%{x:.0f}<br>y=%{y:.0f}<br>rate=%{z:.4f}<extra></extra>',
            showscale: showBar,
        };
        if (showBar) {
            trace.colorbar = { thickness: 6, tickfont: { size: 8 }, tickformat: '.2f', len: 0.92 };
        }

        Plotly.react(divId, [trace], layout, { responsive: true, displayModeBar: false });
    });
}

// --- GEM histograms (bottom right) ------------------------------------------

const GEM_HIST_IDS = ['gem-ncl-hist', 'gem-theta-hist'];
let currentGemNclHist = null, currentGemThetaHist = null;

function plotGemHist(data) {
    if (!data) { currentGemNclHist = currentGemThetaHist = null; return; }

    function plotOne(divId, hdata, title, xlabel, color, refKey) {
        if (!hdata || !hdata.bins || hdata.bins.length === 0) {
            Plotly.react(divId, [], Object.assign({}, PL_GEM_OCC(), {
                title: { text: title, font: { size: 12, color: THEME.text } },
            }), { responsive: true, displayModeBar: false });
            return null;
        }
        const n = hdata.bins.length;
        const x = Array.from({length: n}, (_, i) => hdata.min + (i + 0.5) * hdata.step);
        const entries = hdata.bins.reduce((a, b) => a + b, 0);
        // store non-zero bins for copy
        const cx = [], cy = [];
        for (let i = 0; i < n; i++) { if (hdata.bins[i] > 0) { cx.push(x[i]); cy.push(hdata.bins[i]); } }
        Plotly.react(divId, [{
            x: x, y: hdata.bins, type: 'bar',
            marker: { color: color, line: { width: 0 } },
            hovertemplate: xlabel + '=%{x:.1f}<br>count=%{y}<extra></extra>',
        }], Object.assign({}, PL_GEM_OCC(), {
            title: { text: title + '<br><span style="font-size:9px;color:var(--theme-text-dim)">' + entries + ' entries</span>',
                     font: { size: 12, color: THEME.text } },
            xaxis: { title: xlabel, gridcolor: THEME.grid, zerolinecolor: THEME.border },
            yaxis: { title: 'Counts', gridcolor: THEME.grid, zerolinecolor: THEME.border },
            bargap: 0.05,
            shapes: refKey ? refShapes(refKey) : [],
        }), { responsive: true, displayModeBar: false });
        return { x: cx, y: cy };
    }

    currentGemNclHist = plotOne('gem-ncl-hist', data.nclusters, 'GEM Clusters / Event', 'N clusters', '#51cf66', 'gem_clusters');
    currentGemThetaHist = plotOne('gem-theta-hist', data.theta, 'GEM Hit Angle', 'θ (deg)', '#00b4d8', 'gem_theta');
}

// --- resize -----------------------------------------------------------------

function resizeGem() {
    GEM_PLANES.forEach(plane => {
        try { Plotly.Plots.resize(plane.hitId); } catch (e) {}
    });
    GEM_OCC_IDS.forEach(id => {
        try { Plotly.Plots.resize(id); } catch (e) {}
    });
    GEM_HIST_IDS.forEach(id => {
        try { Plotly.Plots.resize(id); } catch (e) {}
    });
}
