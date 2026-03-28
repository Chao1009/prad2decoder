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
    { name: 'Plane 1 (upstream)',   dets: [0, 1], hitId: 'gem-plane-0', occId: 'gem-occ-0' },
    { name: 'Plane 2 (downstream)', dets: [2, 3], hitId: 'gem-plane-1', occId: 'gem-occ-1' },
];

const PL_GEM = {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: '#1a1a2e',
    font: { color: '#e0e0e0', size: 11 },
    margin: { l: 50, r: 20, t: 30, b: 40 },
    hovermode: 'closest',
};

let gemConfig = null;

// --- helpers ----------------------------------------------------------------

function gemDetSize(detId) {
    if (!gemConfig || !gemConfig.layers) return { xSize: 614.4, ySize: 512.0 };
    const layer = gemConfig.layers.find(l => l.id === detId);
    if (!layer) return { xSize: 614.4, ySize: 512.0 };
    return {
        xSize: layer.x_size || layer.x_apvs * 128 * layer.x_pitch,
        ySize: layer.y_size || layer.y_apvs * 128 * layer.y_pitch,
    };
}

// --- fetch + render ---------------------------------------------------------

function fetchGemData() {
    const configReady = gemConfig
        ? Promise.resolve(gemConfig)
        : fetch('/api/gem/config').then(r => r.json()).then(cfg => { gemConfig = cfg; return cfg; });

    configReady.then(() => {
        fetch('/api/gem/hits').then(r => r.json()).then(plotGemHits).catch(() => {});
        fetch('/api/gem/occupancy').then(r => r.json()).then(plotGemOccupancy).catch(() => {});
    });
}

function fetchGemOccupancy() {
    fetch('/api/gem/occupancy').then(r => r.json()).then(plotGemOccupancy).catch(() => {});
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

        plane.dets.forEach((detId) => {
            const det = detectors.find(d => d.id === detId);
            if (!det) return;

            const hits = det.hits_2d || [];
            const color = GEM_COLORS[detId] || '#888';
            const detName = det.name || ('GEM' + detId);

            traces.push({
                x: hits.map(h => h.x),
                y: hits.map(h => h.y),
                mode: 'markers',
                type: 'scatter',
                name: detName,
                marker: {
                    color: color, size: 6, opacity: 0.8,
                    line: { width: 0.5, color: '#fff' },
                },
                hovertemplate: detName + '<br>x=%{x:.1f} mm<br>y=%{y:.1f} mm<extra></extra>',
            });

            // detector outline — centered around (0,0)
            const sz = gemDetSize(detId);
            shapes.push({
                type: 'rect',
                x0: -sz.xSize / 2, y0: -sz.ySize / 2,
                x1:  sz.xSize / 2, y1:  sz.ySize / 2,
                line: { color: color, width: 1.5, dash: 'dot' },
                fillcolor: 'rgba(0,0,0,0)',
            });
        });

        if (traces.length === 0) {
            traces.push({ x: [], y: [], mode: 'markers', type: 'scatter',
                          name: 'No data', marker: { size: 0 } });
        }

        const layout = Object.assign({}, PL_GEM, {
            title: { text: plane.name, font: { size: 13, color: '#e0e0e0' } },
            xaxis: {
                title: 'X (mm)', gridcolor: '#333', zerolinecolor: '#555',
                scaleanchor: 'y', scaleratio: 1,
            },
            yaxis: { title: 'Y (mm)', gridcolor: '#333', zerolinecolor: '#555' },
            shapes: shapes,
            showlegend: true,
            legend: { x: 0.01, y: 0.99, bgcolor: 'rgba(0,0,0,0.3)', font: { size: 10 } },
        });

        Plotly.react(plane.hitId, traces, layout, { responsive: true, displayModeBar: false });
    });
}

// --- occupancy heatmap (right) ----------------------------------------------

function plotGemOccupancy(data) {
    if (!data || !data.enabled) {
        GEM_PLANES.forEach(plane => {
            const div = document.getElementById(plane.occId);
            if (div) div.innerHTML = '<div style="color:var(--dim);padding:40px;text-align:center">GEM not enabled</div>';
        });
        return;
    }

    const detectors = data.detectors || [];

    GEM_PLANES.forEach((plane) => {
        // sum occupancy bins across detectors in this plane
        let nx = 0, ny = 0, xSize = 0, ySize = 0;
        let sumBins = null;
        let names = [];

        plane.dets.forEach((detId) => {
            const det = detectors.find(d => d.id === detId);
            if (!det) return;
            names.push(det.name);
            if (!sumBins) {
                nx = det.nx; ny = det.ny;
                xSize = det.x_size; ySize = det.y_size;
                sumBins = det.bins.slice();
            } else {
                for (let i = 0; i < sumBins.length; i++)
                    sumBins[i] += (det.bins[i] || 0);
            }
        });

        if (!sumBins) {
            Plotly.react(plane.occId,
                [{ x: [], y: [], z: [[]], type: 'heatmap', name: 'No data' }],
                Object.assign({}, PL_GEM, {
                    title: { text: plane.name, font: { size: 13, color: '#e0e0e0' } },
                }),
                { responsive: true, displayModeBar: false });
            return;
        }

        const xStep = xSize / nx, yStep = ySize / ny;
        const z = [];
        for (let iy = 0; iy < ny; iy++) {
            const row = [];
            for (let ix = 0; ix < nx; ix++)
                row.push(sumBins[iy * nx + ix] || 0);
            z.push(row);
        }

        const x0 = -xSize / 2 + xStep / 2;
        const y0 = -ySize / 2 + yStep / 2;
        const xArr = Array.from({length: nx}, (_, i) => x0 + i * xStep);
        const yArr = Array.from({length: ny}, (_, i) => y0 + i * yStep);

        const traces = [{
            x: xArr, y: yArr, z: z,
            type: 'heatmap',
            colorscale: 'Hot', reversescale: true,
            name: names.join('+'),
            hovertemplate: names.join('+') + '<br>x=%{x:.0f} mm<br>y=%{y:.0f} mm<br>count=%{z}<extra></extra>',
            colorbar: { thickness: 12, tickfont: { size: 9 } },
        }];

        const layout = Object.assign({}, PL_GEM, {
            title: { text: plane.name + ' (' + names.join('+') + ')', font: { size: 13, color: '#e0e0e0' } },
            xaxis: { title: 'X (mm)', gridcolor: '#333', zerolinecolor: '#555' },
            yaxis: { title: 'Y (mm)', gridcolor: '#333', zerolinecolor: '#555' },
            showlegend: false,
        });

        Plotly.react(plane.occId, traces, layout, { responsive: true, displayModeBar: false });
    });
}

// --- resize -----------------------------------------------------------------

function resizeGem() {
    GEM_PLANES.forEach(plane => {
        try { Plotly.Plots.resize(plane.hitId); } catch (e) {}
        try { Plotly.Plots.resize(plane.occId); } catch (e) {}
    });
}
