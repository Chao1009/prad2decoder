// report.js — Report generation, PDF download, and elog posting
//
// Depends on globals from viewer.js: modules, mode, sampleCount, totalEvents,
// currentEventNumber, activeTab, geoCanvas, canvasW, canvasH, scale, offsetX,
// offsetY, fitView, redrawGeo, occData, occTcutData, occTotal, clHistBins,
// clHistMin, clHistMax, clHistStep, clHistEvents, nclustBins, nclustMin,
// nclustStep, nblocksBins, nblocksMin, nblocksStep, clusterData,
// lmsSummaryData, g_lmsRefIndex

// =========================================================================
// Registry
// =========================================================================
const reportRegistry=[];
let elogConfig={url:'',logbook:'',author:'',tags:[]};
let reportAttachments=[];

function registerReportSection(section){
    reportRegistry.push(section);
    reportRegistry.sort((a,b)=>a.order-b.order);
}

// =========================================================================
// Capture helpers
// =========================================================================

// Capture the geo canvas for a given tab at fixed high resolution with light theme.
async function captureGeoForTab(tab){
    const prev={tab:activeTab,w:geoCanvas.width,h:geoCanvas.height,
                s:scale,ox:offsetX,oy:offsetY};
    geoCanvas.width=1200; geoCanvas.height=900;
    canvasW=1200; canvasH=900;
    activeTab=tab;
    geoLightTheme=true;
    fitView(); redrawGeo();
    const url=geoCanvas.toDataURL('image/png');
    geoLightTheme=false;
    geoCanvas.width=prev.w; geoCanvas.height=prev.h;
    canvasW=prev.w; canvasH=prev.h;
    scale=prev.s; offsetX=prev.ox; offsetY=prev.oy;
    activeTab=prev.tab;
    redrawGeo();
    return url;
}

// Capture a geo view and return an HTML img tag + register as elog attachment.
async function captureGeoSection(tab,title,filename){
    try{
        const img=await captureGeoForTab(tab);
        addReportAttachment(img,filename,title);
        return `<h3>${title}</h3><img class="geo-img" src="${img}">`;
    }catch(e){ return ''; }
}

// Render a Plotly chart off-screen, capture as image.
async function plotToImage(plotFn,w,h){
    const div=document.createElement('div');
    div.style.cssText='position:fixed;left:-9999px;width:'+w+'px;height:'+h+'px';
    document.body.appendChild(div);
    await plotFn(div);
    const img=await Plotly.toImage(div,{format:'png',width:w,height:h});
    Plotly.purge(div);
    div.remove();
    return img;
}

// Light-theme Plotly layout for printable reports.
const RPL={paper_bgcolor:'#fff',plot_bgcolor:'#fff',
    font:{family:'Helvetica,Arial,sans-serif',size:11,color:'#222'},
    margin:{l:50,r:15,t:28,b:36},
    xaxis:{gridcolor:'#ddd',zerolinecolor:'#bbb',linecolor:'#999',mirror:true},
    yaxis:{gridcolor:'#ddd',zerolinecolor:'#bbb',linecolor:'#999',mirror:true}};

// Capture a bar histogram from raw bins into a PNG image.
// Returns the data-URL string, or null on failure.
async function histToImage(bins,binMin,binStep,title,xTitle,color,w,h){
    if(!bins||!bins.some(b=>b>0)) return null;
    try{
        return await plotToImage(async div=>{
            const x=bins.map((_,i)=>binMin+(i+0.5)*binStep);
            const entries=bins.reduce((a,b)=>a+b,0);
            const titleText=entries?`${title} (${entries} entries)`:title;
            await Plotly.newPlot(div,[{x,y:bins,type:'bar',
                marker:{color,line:{width:0}}}],
                {...RPL,title:{text:titleText,font:{size:12,color:'#222'}},
                 xaxis:{...RPL.xaxis,title:xTitle},
                 yaxis:{...RPL.yaxis,title:'Counts'},bargap:0.05});
        },w,h);
    }catch(e){ return null; }
}

function addReportAttachment(dataUrl,filename,caption){
    const b64=dataUrl.split(',')[1];
    if(b64) reportAttachments.push({data:b64,filename,caption,type:'image/png'});
}

// =========================================================================
// HTML table helper
// =========================================================================
function htmlTable(headers,rows){
    let h='<table><thead><tr>';
    for(const col of headers)
        h+=`<th${col.left?' style="text-align:left"':''}>${col.label}</th>`;
    h+='</tr></thead><tbody>';
    for(const row of rows){
        h+='<tr>';
        for(let i=0;i<headers.length;i++)
            h+=`<td${headers[i].left?' style="text-align:left"':''}>${row[i]}</td>`;
        h+='</tr>';
    }
    h+='</tbody></table>';
    return h;
}

// =========================================================================
// Report sections
// =========================================================================

// --- DQ ---
registerReportSection({id:'dq',title:'Waveform Data (DQ)',order:10,
    generate:async()=>{
        let html='<div class="section"><h2>Waveform Data (DQ)</h2>';
        const metric=document.getElementById('color-metric').value;
        html+=await captureGeoSection('dq',`Detector Map (${metric})`,'dq_geo.png');
        if(occTotal>0){
            html+=`<h3>Occupancy Summary</h3><p>Total events: ${occTotal}</p>`;
            const entries=modules.map(m=>{
                const key=`${m.roc}_${m.sl}_${m.ch}`;
                return{name:m.n,occ:occData[key]||0,occTcut:occTcutData[key]||0};
            }).filter(e=>e.occ>0).sort((a,b)=>b.occ-a.occ);
            const show=entries.slice(0,50);
            html+=htmlTable(
                [{label:'Module',left:true},{label:'Hits'},{label:'Occupancy %'},{label:'Hits (tcut)'}],
                show.map(e=>[e.name,e.occ,(100*e.occ/occTotal).toFixed(2)+'%',e.occTcut])
            );
            if(entries.length>50)
                html+=`<p class="no-data">Showing top 50 of ${entries.length} active modules</p>`;
        }else{
            html+='<p class="no-data">No occupancy data available</p>';
        }
        return html+'</div>';
    }
});

// --- Clustering ---
registerReportSection({id:'cluster',title:'Clustering',order:20,
    generate:async()=>{
        const hasHist=clHistBins&&clHistBins.some(b=>b>0);
        const hasClusters=clusterData&&clusterData.clusters&&clusterData.clusters.length;
        if(!hasHist&&!hasClusters) return null;

        let html='<div class="section"><h2>Clustering</h2>';
        // energy histogram
        const eImg=await histToImage(clHistBins,clHistMin,clHistStep,
            `Cluster Energy (${clHistEvents} evts)`,'Energy (MeV)','#ff922b',800,300);
        if(eImg){
            html+=`<h3>Cluster Energy Distribution</h3><img class="chart-img" src="${eImg}">`;
            addReportAttachment(eImg,'cluster_energy.png','Cluster Energy Histogram');
        }
        // stat histograms
        const statImgs=[];
        const ncImg=await histToImage(nclustBins,nclustMin,nclustStep,
            'Clusters per Event','# Clusters','#00b4d8',400,260);
        if(ncImg){statImgs.push(ncImg);addReportAttachment(ncImg,'clusters_per_event.png','Clusters per Event');}
        const nbImg=await histToImage(nblocksBins,nblocksMin,nblocksStep,
            'Blocks per Cluster','# Blocks','#51cf66',400,260);
        if(nbImg){statImgs.push(nbImg);addReportAttachment(nbImg,'blocks_per_cluster.png','Blocks per Cluster');}
        if(statImgs.length){
            html+='<h3>Cluster Statistics</h3><div class="chart-row">';
            for(const img of statImgs) html+=`<img class="chart-img" src="${img}">`;
            html+='</div>';
        }
        // cluster table
        if(hasClusters){
            html+=`<h3>Clusters (Event #${currentEventNumber})</h3>`;
            html+=htmlTable(
                [{label:'#'},{label:'Center',left:true},{label:'Energy [MeV]'},
                 {label:'X [mm]'},{label:'Y [mm]'},{label:'Blocks'}],
                clusterData.clusters.map((cl,i)=>[
                    i,cl.center,cl.energy.toFixed(1),cl.x.toFixed(1),cl.y.toFixed(1),cl.nblocks
                ])
            );
        }
        return html+'</div>';
    }
});

// --- LMS ---
registerReportSection({id:'lms',title:'Gain Monitoring (LMS)',order:30,
    generate:async()=>{
        if(!lmsSummaryData||!lmsSummaryData.modules) return null;
        const entries=Object.entries(lmsSummaryData.modules)
            .map(([idx,m])=>({idx:parseInt(idx),...m}));
        if(!entries.length) return null;

        let html='<div class="section"><h2>Gain Monitoring (LMS)</h2>';
        html+=await captureGeoSection('lms','LMS Status Map','lms_geo.png');
        entries.sort((a,b)=>{
            if(a.warn!==b.warn) return a.warn?-1:1;
            const ra=a.mean>0?a.rms/a.mean:0, rb=b.mean>0?b.rms/b.mean:0;
            return rb-ra;
        });
        const warnCount=entries.filter(e=>e.warn).length;
        html+=`<h3>Module Summary</h3>`;
        html+=`<p>LMS events: ${lmsSummaryData.events||0} | Modules: ${entries.length} | `;
        html+=`Warnings: <span class="${warnCount?'warn':'ok'}">${warnCount}</span></p>`;
        html+=htmlTable(
            [{label:'Module',left:true},{label:'Mean'},{label:'RMS'},
             {label:'RMS/Mean %'},{label:'Count'},{label:'Status'}],
            entries.map(e=>[
                e.name, e.mean.toFixed(1), e.rms.toFixed(2),
                (e.mean>0?(e.rms/e.mean*100).toFixed(1):'--')+'%',
                e.count, e.warn?'<span class="warn">WARN</span>':'<span class="ok">OK</span>'
            ])
        );
        return html+'</div>';
    }
});

// =========================================================================
// Report generation core
// =========================================================================

async function refreshDataForReport(){
    const fetches=[];
    fetches.push(fetch('/api/occupancy').then(r=>r.json()).then(d=>{
        occData=d.occ||{}; occTcutData=d.occ_tcut||{}; occTotal=d.total||0;
    }).catch(()=>{}));
    fetches.push(fetch('/api/cluster_hist').then(r=>r.json()).then(d=>{
        if(d.bins&&d.bins.length){
            if(d.min!==undefined) clHistMin=d.min;
            if(d.max!==undefined) clHistMax=d.max;
            if(d.step!==undefined) clHistStep=d.step;
            clHistBins=d.bins; clHistEvents=d.events||0;
        }
    }).catch(()=>{}));
    const refQ=g_lmsRefIndex>=0?`?ref=${g_lmsRefIndex}`:'';
    fetches.push(fetch(`/api/lms/summary${refQ}`).then(r=>r.json()).then(d=>{
        lmsSummaryData=d;
    }).catch(()=>{}));
    await Promise.all(fetches);
}

function buildReportHtml(sections){
    const ts=new Date().toLocaleString();
    const evInfo=mode==='online'?`${sampleCount} samples`:`${totalEvents} events`;
    return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>PRad2 Monitor Report - ${ts}</title>
<style>
body{font-family:'Helvetica Neue',Arial,sans-serif;max-width:1100px;margin:0 auto;padding:20px;color:#222;background:#fff}
h1{font-size:20px;border-bottom:2px solid #0074d9;padding-bottom:6px}
h2{font-size:16px;color:#0074d9;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px;page-break-after:avoid}
h3{font-size:13px;color:#333;margin:14px 0 6px}
.meta{color:#666;font-size:12px;margin-bottom:16px}
.meta span{margin-right:20px}
img{max-width:100%;height:auto;display:block;margin:8px 0}
table{border-collapse:collapse;width:100%;font-size:11px;margin:8px 0}
th,td{border:1px solid #ccc;padding:3px 6px;text-align:right}
th{background:#f0f0f0;font-weight:600}
tr:nth-child(even){background:#fafafa}
.warn{color:#c00;font-weight:600}
.ok{color:#080}
.section{page-break-inside:avoid}
.geo-img{max-width:700px;border:1px solid #ddd}
.chart-img{max-width:800px}
.chart-row{display:flex;gap:10px}
.chart-row img{max-width:48%}
.no-data{color:#999;font-style:italic}
@media print{body{padding:0}.section{page-break-inside:avoid}}
</style></head><body>
<h1>PRad2 HyCal Monitor Report</h1>
<div class="meta">
<span>Generated: ${ts}</span>
<span>Mode: ${mode}</span>
<span>${evInfo}</span>
<span>Event #${currentEventNumber}</span>
</div>
${sections.join('\n')}
<div class="meta" style="margin-top:30px;border-top:1px solid #ddd;padding-top:6px">
PRad2 HyCal Online Monitor &mdash; Report generated ${ts}
</div></body></html>`;
}

async function generateReport(){
    if(!modules.length){
        alert('No data loaded. Please load data before generating a report.');
        return null;
    }
    const statusBar=document.getElementById('status-bar');
    const prevStatus=statusBar.textContent;
    statusBar.textContent='Generating report...';
    try{
        await refreshDataForReport();
        reportAttachments=[];
        const sections=[];
        for(const entry of reportRegistry){
            try{
                const html=await entry.generate();
                if(html) sections.push(html);
            }catch(err){
                sections.push(`<div class="section"><h2>${entry.title}</h2>
                    <p class="no-data">Error: ${err.message}</p></div>`);
            }
        }
        statusBar.textContent=prevStatus;
        return buildReportHtml(sections);
    }catch(err){
        statusBar.textContent=`Report error: ${err.message}`;
        return null;
    }
}

// =========================================================================
// PDF download
// =========================================================================

async function downloadReportPdf(){
    const html=await generateReport();
    if(!html) return;
    const statusBar=document.getElementById('status-bar');
    statusBar.textContent='Rendering PDF...';
    try{
        // Render in a hidden iframe for CSS isolation from the monitor's styles
        const iframe=document.createElement('iframe');
        iframe.style.cssText='position:fixed;left:-9999px;top:0;width:1100px;height:900px;border:none';
        document.body.appendChild(iframe);
        const idoc=iframe.contentDocument||iframe.contentWindow.document;
        idoc.open(); idoc.write(html); idoc.close();
        // Wait for images to load
        await new Promise(r=>setTimeout(r,200));

        const ts=new Date().toISOString().slice(0,19).replace(/[T:]/g,'-');
        await html2pdf().set({
            margin:[10,10,10,10],
            filename:`prad2_report_${ts}.pdf`,
            image:{type:'png',quality:0.95},
            html2canvas:{scale:2,useCORS:true,logging:false},
            jsPDF:{unit:'mm',format:'a4',orientation:'portrait'},
            pagebreak:{mode:['css','legacy'],avoid:'.section'}
        }).from(idoc.body).save();
        iframe.remove();
        statusBar.textContent='PDF saved';
        setTimeout(()=>{statusBar.textContent='Ready';},3000);
    }catch(err){
        statusBar.textContent=`PDF error: ${err.message}`;
    }
}

// =========================================================================
// Elog posting
// =========================================================================

function escXml(s){
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
        .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function buildElogXml(title,logbook,author,tags,bodyHtml,attachments){
    const parts=['<?xml version="1.0" encoding="UTF-8"?>','<Logentry>',
        `  <created>${new Date().toISOString()}</created>`,
        `  <Author><username>${escXml(author)}</username></Author>`,
        `  <title>${escXml(title)}</title>`,
        `  <body type="html"><![CDATA[${bodyHtml}]]></body>`,
        '  <Logbooks>'];
    for(const lb of logbook.split(','))
        parts.push(`    <logbook>${escXml(lb.trim())}</logbook>`);
    parts.push('  </Logbooks>');
    if(tags&&tags.length){
        parts.push('  <Tags>');
        for(const t of tags) parts.push(`    <tag>${escXml(t.trim())}</tag>`);
        parts.push('  </Tags>');
    }
    if(attachments&&attachments.length){
        parts.push('  <Attachments>');
        for(const a of attachments)
            parts.push('    <Attachment>',
                `      <caption>${escXml(a.caption)}</caption>`,
                `      <filename>${escXml(a.filename)}</filename>`,
                `      <type>${escXml(a.type)}</type>`,
                `      <data encoding="base64">${a.data}</data>`,
                '    </Attachment>');
        parts.push('  </Attachments>');
    }
    parts.push('</Logentry>');
    return parts.join('\n');
}

function showElogDialog(){
    document.getElementById('elog-backdrop').classList.add('open');
    document.getElementById('elog-dialog').classList.add('open');
    document.getElementById('elog-status').textContent='';
}
function hideElogDialog(){
    document.getElementById('elog-backdrop').classList.remove('open');
    document.getElementById('elog-dialog').classList.remove('open');
}

async function postToElog(){
    const title=document.getElementById('elog-title').value.trim();
    const logbook=document.getElementById('elog-logbook').value.trim();
    const author=document.getElementById('elog-author').value.trim();
    const tagsStr=document.getElementById('elog-tags').value.trim();
    const tags=tagsStr?tagsStr.split(',').map(s=>s.trim()).filter(s=>s):[];
    const statusEl=document.getElementById('elog-status');

    if(!title||!logbook||!author){
        statusEl.textContent='Title, logbook, and author are required.';
        statusEl.style.color='#c00';
        return;
    }
    statusEl.textContent='Generating report...';
    statusEl.style.color='var(--dim)';

    const html=await generateReport();
    if(!html){statusEl.textContent='Failed to generate report.';statusEl.style.color='#c00';return;}

    // strip base64 images from body (they go as attachments)
    const bodyMatch=html.match(/<body[^>]*>([\s\S]*)<\/body>/i);
    const body=(bodyMatch?bodyMatch[1]:'').replace(/<img[^>]*src="data:[^"]*"[^>]*>/gi,'');

    statusEl.textContent='Posting to elog...';
    const xml=buildElogXml(title,logbook,author,tags,body,reportAttachments);

    try{
        const resp=await fetch('/api/elog/post',{
            method:'POST',headers:{'Content-Type':'application/xml'},body:xml});
        const result=await resp.json();
        if(result.ok){
            statusEl.textContent='Posted successfully! (HTTP '+result.status+')';
            statusEl.style.color='#080';
            setTimeout(hideElogDialog,2000);
        }else{
            statusEl.textContent='Post failed: HTTP '+result.status+(result.error?' - '+result.error:'');
            statusEl.style.color='#c00';
        }
    }catch(err){
        statusEl.textContent='Network error: '+err.message;
        statusEl.style.color='#c00';
    }
}

// =========================================================================
// Init — called from viewer.js init() with config data
// =========================================================================
function initReport(data){
    // report dropdown toggle
    const reportBtn=document.getElementById('btn-report');
    const reportMenu=document.getElementById('report-menu');
    reportBtn.onclick=(e)=>{e.stopPropagation();reportMenu.classList.toggle('open');};
    document.addEventListener('click',()=>reportMenu.classList.remove('open'));
    reportMenu.onclick=(e)=>e.stopPropagation();
    document.getElementById('btn-report-pdf').onclick=()=>{
        reportMenu.classList.remove('open'); downloadReportPdf();};
    document.getElementById('btn-report-elog').onclick=()=>{
        reportMenu.classList.remove('open'); showElogDialog();};

    // elog dialog
    document.getElementById('elog-dialog-close').onclick=hideElogDialog;
    document.getElementById('elog-backdrop').onclick=hideElogDialog;
    document.getElementById('elog-cancel').onclick=hideElogDialog;
    document.getElementById('elog-submit').onclick=postToElog;

    // elog config defaults from server
    if(data&&data.elog){
        elogConfig=data.elog;
        document.getElementById('elog-logbook').value=data.elog.logbook||'';
        document.getElementById('elog-author').value=data.elog.author||'';
        document.getElementById('elog-tags').value=(data.elog.tags||[]).join(', ');
        if(!data.elog.url){
            const eb=document.getElementById('btn-report-elog');
            if(eb) eb.style.display='none';
        }
    }
}
