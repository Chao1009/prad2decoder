// physics.js — Physics tab: energy vs angle + XY position 2D histograms
//
// Depends on globals from viewer.js: PL, PC_EPICS, activeTab

let physicsData=null, posXYData=null;

function fetchEnergyAngle(){
    fetch('/api/physics/energy_angle').then(r=>r.json()).then(data=>{
        physicsData=data;
        plotEnergyAngle();
    }).catch(()=>{});
}

function fetchPositionXY(){
    fetch('/api/physics/position_xy').then(r=>r.json()).then(data=>{
        posXYData=data;
        plotPositionXY();
    }).catch(()=>{});
}

function fetchPhysics(){
    fetchEnergyAngle();
    fetchPositionXY();
}

// ep elastic scattering: E' = E / (1 + (E/Mp)*(1 - cos(theta)))
function elasticEp(beamE, thetaDeg){
    const Mp=938.272;
    const th=thetaDeg*Math.PI/180;
    return beamE/(1+(beamE/Mp)*(1-Math.cos(th)));
}

function plotEnergyAngle(){
    const div='physics-plot';
    if(!physicsData||!physicsData.bins||!physicsData.bins.length||!physicsData.nx){
        Plotly.react(div,[],{...PL,title:{text:'Energy vs Angle — No data',font:{size:12,color:'#888'}}},PC_EPICS);
        document.getElementById('physics-stats').textContent='';
        return;
    }
    const d=physicsData;
    const logZ=document.getElementById('physics-logz').checked;
    const showElastic=document.getElementById('physics-elastic').checked;

    // reshape flat bins to 2D [ny][nx]
    const z=[];
    for(let iy=0;iy<d.ny;iy++){
        const row=d.bins.slice(iy*d.nx,(iy+1)*d.nx);
        z.push(logZ?row.map(v=>v>0?Math.log10(v):null):row);
    }

    // axis tick values at bin centers
    const x=[];for(let i=0;i<d.nx;i++) x.push(d.angle_min+(i+0.5)*d.angle_step);
    const y=[];for(let i=0;i<d.ny;i++) y.push(d.energy_min+(i+0.5)*d.energy_step);

    const traces=[{
        z:z, x:x, y:y,
        type:'heatmap',
        colorscale:'Hot',
        reversescale:false,
        hovertemplate:'θ=%{x:.2f}° E=%{y:.0f} MeV: %{text}<extra></extra>',
        text:z.map((row,iy)=>row.map((v,ix)=>{
            const raw=d.bins[iy*d.nx+ix];
            return String(raw);
        })),
        colorbar:{title:logZ?'log₁₀(counts)':'counts',titleside:'right',
            titlefont:{size:10,color:'#aaa'},tickfont:{size:9,color:'#aaa'}},
    }];

    // elastic ep line overlay
    if(showElastic && d.beam_energy>0){
        const ex=[],ey=[];
        for(let th=d.angle_min+0.1;th<=d.angle_max;th+=0.05){
            const e=elasticEp(d.beam_energy,th);
            if(e>=d.energy_min&&e<=d.energy_max){ex.push(th);ey.push(e);}
        }
        traces.push({x:ex,y:ey,mode:'lines',
            line:{color:'#00ff88',width:2,dash:'dot'},
            name:`ep elastic (${d.beam_energy} MeV)`,
            hovertemplate:'θ=%{x:.2f}° E=%{y:.0f} MeV<extra>ep elastic</extra>'});
    }

    Plotly.react(div,traces,{...PL,
        title:{text:`Energy vs Angle (${d.events} evts)`,font:{size:12,color:'#ccc'}},
        xaxis:{...PL.xaxis,title:'Scattering Angle (deg)'},
        yaxis:{...PL.yaxis,title:'Energy (MeV)'},
        margin:{l:55,r:80,t:30,b:40},
        showlegend:showElastic,
        legend:{x:0.7,y:0.95,font:{size:10,color:'#aaa'},bgcolor:'rgba(0,0,0,0)'},
    },PC_EPICS);

    document.getElementById('physics-stats').textContent=
        `${d.events} events | beam: ${d.beam_energy||'?'} MeV | HyCal z: ${(d.hycal_z||5800)/1000}m`;
}

function plotPositionXY(){
    const div='physics-xy-plot';
    if(!posXYData||!posXYData.bins||!posXYData.bins.length||!posXYData.nx){
        Plotly.react(div,[],{...PL,title:{text:'XY Position — No data',font:{size:12,color:'#888'}}},PC_EPICS);
        return;
    }
    const d=posXYData;
    const logZ=document.getElementById('physics-logz').checked;

    const z=[];
    for(let iy=0;iy<d.ny;iy++){
        const row=d.bins.slice(iy*d.nx,(iy+1)*d.nx);
        z.push(logZ?row.map(v=>v>0?Math.log10(v):null):row);
    }
    const x=[];for(let i=0;i<d.nx;i++) x.push(d.x_min+(i+0.5)*d.x_step);
    const y=[];for(let i=0;i<d.ny;i++) y.push(d.y_min+(i+0.5)*d.y_step);

    Plotly.react(div,[{
        z:z, x:x, y:y,
        type:'heatmap',
        colorscale:'Hot',
        reversescale:false,
        hovertemplate:'x=%{x:.1f} y=%{y:.1f} mm: %{text}<extra></extra>',
        text:z.map((row,iy)=>row.map((v,ix)=>{
            return String(d.bins[iy*d.nx+ix]);
        })),
        colorbar:{title:logZ?'log₁₀(counts)':'counts',titleside:'right',
            titlefont:{size:10,color:'#aaa'},tickfont:{size:9,color:'#aaa'}},
    }],{...PL,
        title:{text:`Cluster Position (${d.events} evts)`,font:{size:12,color:'#ccc'}},
        xaxis:{...PL.xaxis,title:'X (mm)',scaleanchor:'y',scaleratio:1},
        yaxis:{...PL.yaxis,title:'Y (mm)'},
        margin:{l:55,r:80,t:30,b:40},
    },PC_EPICS);
}

function clearPhysicsFrontend(){
    physicsData=null; posXYData=null;
    Plotly.react('physics-plot',[],{...PL},PC_EPICS);
    Plotly.react('physics-xy-plot',[],{...PL},PC_EPICS);
    document.getElementById('physics-stats').textContent='';
}

function resizePhysics(){
    try{Plotly.Plots.resize('physics-plot');}catch(e){}
    try{Plotly.Plots.resize('physics-xy-plot');}catch(e){}
}

function initPhysics(data){
    document.getElementById('physics-logz').onchange=()=>{plotEnergyAngle();plotPositionXY();};
    document.getElementById('physics-elastic').onchange=plotEnergyAngle;
}
