// filter_dialog.js — Event filter dialog initialization
// Called from init() in viewer.js

function initFilterDialog(){
    // Filter dialog
    const fltBackdrop=document.getElementById('filter-backdrop');
    const fltDialog=document.getElementById('filter-dialog');
    function openFilterDialog(){
        fltBackdrop.classList.add('active'); fltDialog.classList.add('active');
        document.getElementById('flt-status-msg').textContent='';
        // populate from current filter state
        fetch('/api/filter').then(r=>r.json()).then(f=>{
            const w=f.waveform||{}, c=f.clustering||{};
            document.getElementById('flt-wf-enable').checked=w.enable||false;
            document.getElementById('flt-wf-modules').value=(w.modules||[]).join(', ');
            document.getElementById('flt-wf-nmin').value=w.n_peaks_min||1;
            document.getElementById('flt-wf-nmax').value=w.n_peaks_max||999999;
            document.getElementById('flt-wf-tmin').value=w.time_min!=null?w.time_min:'';
            document.getElementById('flt-wf-tmax').value=w.time_max!=null?w.time_max:'';
            document.getElementById('flt-wf-imin').value=w.integral_min!=null?w.integral_min:'';
            document.getElementById('flt-wf-imax').value=w.integral_max!=null?w.integral_max:'';
            document.getElementById('flt-wf-hmin').value=w.height_min!=null?w.height_min:'';
            document.getElementById('flt-wf-hmax').value=w.height_max!=null?w.height_max:'';
            document.getElementById('flt-cl-enable').checked=c.enable||false;
            document.getElementById('flt-cl-nmin').value=c.n_min||0;
            document.getElementById('flt-cl-nmax').value=c.n_max||999999;
            document.getElementById('flt-cl-emin').value=c.energy_min!=null?c.energy_min:'';
            document.getElementById('flt-cl-emax').value=c.energy_max!=null?c.energy_max:'';
            document.getElementById('flt-cl-smin').value=c.size_min||1;
            document.getElementById('flt-cl-smax').value=c.size_max||999999;
            document.getElementById('flt-cl-includes').value=(c.includes_modules||[]).join(', ');
            document.getElementById('flt-cl-incmin').value=c.includes_min||1;
            document.getElementById('flt-cl-centers').value=(c.center_modules||[]).join(', ');
            toggleFilterFields();
        }).catch(()=>{});
    }
    function closeFilterDialog(){ fltBackdrop.classList.remove('active'); fltDialog.classList.remove('active'); }
    function toggleFilterFields(){
        const wfOn=document.getElementById('flt-wf-enable').checked;
        document.getElementById('flt-wf-fields').style.opacity=wfOn?'1':'0.4';
        document.querySelectorAll('#flt-wf-fields input').forEach(i=>{if(i.type!=='checkbox')i.disabled=!wfOn;});
        const clOn=document.getElementById('flt-cl-enable').checked;
        document.getElementById('flt-cl-fields').style.opacity=clOn?'1':'0.4';
        document.querySelectorAll('#flt-cl-fields input').forEach(i=>{if(i.type!=='checkbox')i.disabled=!clOn;});
        const ttOn=document.getElementById('flt-tt-enable').checked;
        document.getElementById('flt-tt-fields').style.opacity=ttOn?'1':'0.4';
        document.querySelectorAll('#flt-tt-checks input').forEach(i=>{i.disabled=!ttOn;});
    }
    function parseModuleList(s){ return s.split(/[,\s]+/).map(x=>x.trim()).filter(x=>x); }
    function optFloat(id){ const v=document.getElementById(id).value; return v===''?undefined:parseFloat(v); }

    // build trigger type checkboxes in filter dialog
    const ttContainer=document.getElementById('flt-tt-checks');
    if(ttContainer && triggerTypeDef.length){
        for(const d of triggerTypeDef){
            const lbl=document.createElement('label');
            lbl.style.cssText='display:flex;align-items:center;gap:3px;cursor:pointer';
            const cb=document.createElement('input');
            cb.type='checkbox'; cb.checked=true;
            cb.dataset.trigtype=parseInt(d.type,16);
            lbl.appendChild(cb);
            lbl.appendChild(document.createTextNode(d.label||d.name));
            ttContainer.appendChild(lbl);
        }
    }

    document.getElementById('btn-filter').onclick=()=>openFilterDialog();
    document.getElementById('filter-dialog-close').onclick=()=>closeFilterDialog();
    document.getElementById('flt-cancel').onclick=()=>closeFilterDialog();
    fltBackdrop.onclick=()=>closeFilterDialog();
    document.getElementById('flt-wf-enable').onchange=toggleFilterFields;
    document.getElementById('flt-cl-enable').onchange=toggleFilterFields;
    document.getElementById('flt-tt-enable').onchange=toggleFilterFields;
    document.getElementById('flt-apply').onclick=()=>{
        const fj={};
        // trigger type
        if(document.getElementById('flt-tt-enable').checked){
            const accept=[];
            document.querySelectorAll('#flt-tt-checks input[type="checkbox"]').forEach(cb=>{
                if(cb.checked) accept.push(parseInt(cb.dataset.trigtype));
            });
            fj.trigger_type={enable:true, accept};
        }
        // waveform
        const wf={enable:document.getElementById('flt-wf-enable').checked};
        const wfMods=parseModuleList(document.getElementById('flt-wf-modules').value);
        if(wfMods.length) wf.modules=wfMods;
        wf.n_peaks_min=parseInt(document.getElementById('flt-wf-nmin').value)||1;
        wf.n_peaks_max=parseInt(document.getElementById('flt-wf-nmax').value)||999999;
        const tmin=optFloat('flt-wf-tmin'); if(tmin!=null) wf.time_min=tmin;
        const tmax=optFloat('flt-wf-tmax'); if(tmax!=null) wf.time_max=tmax;
        const imin=optFloat('flt-wf-imin'); if(imin!=null) wf.integral_min=imin;
        const imax=optFloat('flt-wf-imax'); if(imax!=null) wf.integral_max=imax;
        const hmin=optFloat('flt-wf-hmin'); if(hmin!=null) wf.height_min=hmin;
        const hmax=optFloat('flt-wf-hmax'); if(hmax!=null) wf.height_max=hmax;
        fj.waveform=wf;
        // clustering
        const cl={enable:document.getElementById('flt-cl-enable').checked};
        cl.n_min=parseInt(document.getElementById('flt-cl-nmin').value)||0;
        cl.n_max=parseInt(document.getElementById('flt-cl-nmax').value)||999999;
        const emin=optFloat('flt-cl-emin'); if(emin!=null) cl.energy_min=emin;
        const emax=optFloat('flt-cl-emax'); if(emax!=null) cl.energy_max=emax;
        cl.size_min=parseInt(document.getElementById('flt-cl-smin').value)||1;
        cl.size_max=parseInt(document.getElementById('flt-cl-smax').value)||999999;
        const incMods=parseModuleList(document.getElementById('flt-cl-includes').value);
        if(incMods.length){ cl.includes_modules=incMods; cl.includes_min=parseInt(document.getElementById('flt-cl-incmin').value)||1; }
        const ctrMods=parseModuleList(document.getElementById('flt-cl-centers').value);
        if(ctrMods.length) cl.center_modules=ctrMods;
        fj.clustering=cl;

        document.getElementById('flt-status-msg').textContent='Applying filter...';
        fetch('/api/filter/load',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(fj)})
            .then(r=>r.json()).then(d=>{
                if(d.error){ document.getElementById('flt-status-msg').textContent='Error: '+d.error; return; }
                closeFilterDialog();
                fetchConfigAndApply();
            }).catch(()=>{ document.getElementById('flt-status-msg').textContent='Request failed'; });
    };
    document.getElementById('flt-clear').onclick=()=>{
        document.getElementById('flt-status-msg').textContent='Clearing filter...';
        fetch('/api/filter/unload',{method:'POST'}).then(()=>{
            closeFilterDialog();
            fetchConfigAndApply();
        }).catch(()=>{ document.getElementById('flt-status-msg').textContent='Request failed'; });
    };
    toggleFilterFields();
}
