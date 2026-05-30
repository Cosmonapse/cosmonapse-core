"""
cosmo.commands._prism_view
~~~~~~~~~~~~~~~~~~~~~~~~~~~
HTML for the Prism animated visualization.

Single placeholder ``__NAMESPACE__`` is substituted by ``_prism.run_prism``.
The page is a React app loaded from esm.sh that:

  * connects to ``/ws?url=...&namespace=...``
  * renders the Synapse as a central blob and every Neuron+Axon+Dendrite
    as orbiting blobs joined to the Synapse by tendrils
  * pulses the source neuron, the Synapse, and the destination neuron(s)
    for every Signal — and animates a coloured particle along the tendril
  * shows a tooltip on each Neuron blob with id / capabilities / counters
  * has a collapsible right-side sidebar listing every Signal
"""

VIEW_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prism &mdash; __NAMESPACE__</title>
<style>
:root{
  --bg:#07080c;--bg-card:#0f111a;--bg-elev:#0c0e15;
  --border:rgba(255,255,255,0.06);--border-strong:rgba(255,255,255,0.12);
  --text:#e6e7ec;--text-dim:#9097a8;--text-faint:#5b6275;
  --accent:#8b5cf6;--accent-2:#22d3ee;--accent-3:#f472b6;
  --glow:rgba(139,92,246,0.35);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);overflow:hidden;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,sans-serif;-webkit-font-smoothing:antialiased;}
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:
    linear-gradient(rgba(255,255,255,0.025) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,0.025) 1px,transparent 1px);
  background-size:56px 56px;
  -webkit-mask-image:radial-gradient(ellipse at center,black 30%,transparent 80%);
          mask-image:radial-gradient(ellipse at center,black 30%,transparent 80%);
}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#1e2433;border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#2a3146}
#root{height:100vh;width:100vw;position:relative;z-index:1;}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div id="root"></div>
<script type="importmap">
{"imports":{
  "react":"https://esm.sh/react@18.3.1",
  "react-dom/client":"https://esm.sh/react-dom@18.3.1/client",
  "htm/react":"https://esm.sh/htm@3.1.1/react"
}}
</script>
<script type="module">
import React,{useState,useEffect,useRef,useMemo,useCallback}from 'react';
import{createRoot}from 'react-dom/client';
import{html}from 'htm/react';

const PARAMS=new URLSearchParams(location.search);
const BASE_URL=PARAMS.get('url')||'';
const NAMESPACE=PARAMS.get('namespace')||'dev';

const C={
  bg:'#07080c',bgCard:'#0f111a',
  border:'rgba(255,255,255,0.06)',borderStrong:'rgba(255,255,255,0.12)',
  text:'#e6e7ec',textDim:'#9097a8',textFaint:'#5b6275',
  accent:'#8b5cf6',accent2:'#22d3ee',accent3:'#f472b6',
  glow:'rgba(139,92,246,0.35)',
};

const TC={
  TASK:'#22d3ee',AGENT_OUTPUT:'#34d399',FINAL:'#10b981',ERROR:'#f87171',
  CLARIFICATION:'#fbbf24',REGISTER:'#8b5cf6',DEREGISTER:'#7c3aed',
  HEARTBEAT:'#475569',TASK_OFFER:'#c084fc',BID:'#c084fc',
  TASK_AWARDED:'#a855f7',TASK_DECLINED:'#7c3aed',THOUGHT_DELTA:'#64748b',
  PLAN:'#94a3b8',TOOL_CALL:'#e2e8f0',TOOL_RESULT:'#e2e8f0',
  MEMORY_APPEND:'#22d3ee',ESCALATION:'#fb923c',CONSENSUS:'#06b6d4',
  CONTEXT_SYNC:'#22d3ee',CRITIQUE:'#fbbf24',DISCOVER:'#f472b6',
};
const colorFor=t=>TC[t]||C.accent;

// Axon-emitted: signal flows neuron -> synapse
const AXON=new Set(['AGENT_OUTPUT','CLARIFICATION','ERROR','REGISTER','DEREGISTER','HEARTBEAT']);
// Cortex targeting: signal flows synapse -> neuron (consumer)
const TARGET=new Set(['TASK','TASK_OFFER','TASK_AWARDED','TASK_DECLINED']);

const MONO='ui-monospace,Menlo,monospace';

// ─── layout ──────────────────────────────────────────────────────────────
function useLayout(neurons,vp){
  return useMemo(()=>{
    const cx=vp.w/2,cy=vp.h/2;
    const baseR=Math.max(180,Math.min(vp.w,vp.h)*0.32);
    const arr=Array.from(neurons.values());
    const n=Math.max(arr.length,1);
    const out={};
    arr.forEach((ne,i)=>{
      const a=(i/n)*Math.PI*2-Math.PI/2;
      const ring=Math.floor(i/12);
      const r=baseR+ring*70;
      out[ne.id]={x:cx+Math.cos(a)*r,y:cy+Math.sin(a)*r};
    });
    out.__synapse__={x:cx,y:cy};
    return out;
  },[neurons,vp.w,vp.h]);
}

function btn(active){
  return{
    background:active?active+'18':'transparent',
    border:'1px solid '+(active?active+'40':C.borderStrong),
    color:active||C.textDim,
    borderRadius:8,padding:'5px 12px',
    fontSize:12,fontFamily:MONO,cursor:'pointer',transition:'all 0.15s',
  };
}

// ─── blob (neuron / synapse) ─────────────────────────────────────────────
function Blob({x,y,r,color,pulse,label,sublabel,onHover,onLeave,onClick,isSyn}){
  const gid='g_'+(label||'syn').replace(/[^a-zA-Z0-9]/g,'_');
  return html`
    <g transform=${`translate(${x},${y})`} style=${{cursor:'pointer'}}
       onMouseEnter=${onHover} onMouseLeave=${onLeave} onClick=${onClick}>
      <defs>
        <radialGradient id=${gid}>
          <stop offset="0%" stopColor=${color} stopOpacity="0.95"/>
          <stop offset="55%" stopColor=${color} stopOpacity="0.35"/>
          <stop offset="100%" stopColor=${color} stopOpacity="0"/>
        </radialGradient>
      </defs>
      <circle r=${r*2.4} fill=${`url(#${gid})`} style=${{
        opacity:pulse?0.85:0.35,transition:'opacity 0.4s ease',
        filter:`blur(${pulse?'2px':'4px'})`,
      }}/>
      ${pulse&&html`
        <circle r=${r} fill="none" stroke=${color} strokeOpacity="0.7" strokeWidth="2">
          <animate attributeName="r" from=${r} to=${r*3.2} dur="1s" repeatCount="1"/>
          <animate attributeName="stroke-opacity" from="0.8" to="0" dur="1s" repeatCount="1"/>
        </circle>`}
      <circle r=${r} fill=${C.bgCard} stroke=${color}
              strokeWidth=${isSyn?2.5:1.5}
              style=${{filter:`drop-shadow(0 0 ${pulse?16:8}px ${color})`,transition:'filter 0.4s ease'}}/>
      <circle r=${r*0.55} fill=${color} fillOpacity="0.18"/>
      ${isSyn?html`
        <circle r=${r*0.32} fill="none" stroke=${C.accent2} strokeWidth="1.5" strokeOpacity="0.8">
          <animateTransform attributeName="transform" type="rotate" from="0" to="360"
            dur="14s" repeatCount="indefinite"/>
        </circle>
        <circle r=${r*0.18} fill=${C.accent} fillOpacity="0.7">
          <animate attributeName="r" values=${`${r*0.18};${r*0.22};${r*0.18}`}
            dur="2.4s" repeatCount="indefinite"/>
        </circle>
      `:html`<circle r=${r*0.28} fill=${color} fillOpacity="0.85"/>`}
      ${label&&html`
        <text y=${r+22} textAnchor="middle" fontSize="12" fontWeight="500"
              fill=${C.text} style=${{fontFamily:MONO}}>${label}</text>`}
      ${sublabel&&html`
        <text y=${r+38} textAnchor="middle" fontSize="10"
              fill=${C.textFaint} style=${{fontFamily:MONO}}>${sublabel}</text>`}
    </g>`;
}

function Tendril({from,to,active}){
  if(!from||!to)return null;
  const mx=(from.x+to.x)/2,my=(from.y+to.y)/2;
  const d=`M ${from.x} ${from.y} Q ${mx} ${my}, ${to.x} ${to.y}`;
  return html`
    <path d=${d} fill="none"
      stroke=${C.accent} strokeOpacity=${active?0.55:0.12}
      strokeWidth=${active?1.5:0.8}
      style=${{transition:'stroke-opacity 0.4s,stroke-width 0.4s'}}/>`;
}

function Particle({id,from,to,color,onDone}){
  useEffect(()=>{
    const t=setTimeout(()=>onDone(id),1100);
    return()=>clearTimeout(t);
  },[id,onDone]);
  if(!from||!to)return null;
  const mx=(from.x+to.x)/2,my=(from.y+to.y)/2;
  const path=`M ${from.x} ${from.y} Q ${mx} ${my}, ${to.x} ${to.y}`;
  return html`
    <g>
      <circle r="4" fill=${color}
        style=${{filter:`drop-shadow(0 0 6px ${color})`}}>
        <animateMotion dur="1s" repeatCount="1" path=${path} fill="freeze"/>
        <animate attributeName="r" values="4;6;3" dur="1s" repeatCount="1"/>
      </circle>
    </g>`;
}

function Header(p){
  return html`
    <div style=${{
      position:'absolute',top:0,left:0,right:0,zIndex:5,
      display:'flex',alignItems:'center',gap:14,padding:'12px 20px',
      background:'rgba(7,8,12,0.7)',
      WebkitBackdropFilter:'blur(20px)',backdropFilter:'blur(20px)',
      borderBottom:'1px solid '+C.border,
    }}>
      <div onClick=${p.onBack} style=${{display:'flex',alignItems:'center',gap:10,cursor:'pointer'}}>
        <div style=${{
          width:22,height:22,borderRadius:6,position:'relative',
          background:'conic-gradient(from 180deg at 50% 50%,#8b5cf6,#22d3ee,#f472b6,#8b5cf6)',
          boxShadow:'0 0 18px '+C.glow,animation:'spin 8s linear infinite',
        }}>
          <div style=${{position:'absolute',inset:5,borderRadius:3,background:C.bg}}/>
        </div>
        <span style=${{fontWeight:700,fontSize:15}}>Cosmonapse</span>
        <span style=${{color:C.textDim,fontWeight:500,fontSize:15}}>Prism</span>
      </div>
      <span style=${{color:C.textFaint}}>│</span>
      <span style=${{color:C.accent2,fontFamily:MONO,fontSize:12.5}}>${BASE_URL}</span>
      <span style=${{color:C.textFaint,fontFamily:MONO,fontSize:12.5}}>/${NAMESPACE}</span>
      <div style=${{marginLeft:'auto',display:'flex',alignItems:'center',gap:10}}>
        <span style=${{color:p.connected?'#34d399':'#f87171',fontSize:12,fontFamily:MONO}}>
          ${p.connected?'● connected':'○ reconnecting…'}
        </span>
        <span style=${{color:C.textFaint,fontSize:12,fontFamily:MONO}}>${p.total} signals</span>
        <button onClick=${()=>p.setPaused(x=>!x)} style=${btn(p.paused?'#fbbf24':null)}>
          ${p.paused?'▶ resume':'⏸ pause'}
        </button>
        <button onClick=${p.onClear} style=${btn(null)}>clear</button>
        <button onClick=${()=>p.setSidebarOpen(x=>!x)} style=${btn(p.sidebarOpen?'#a78bfa':null)}>
          ${p.sidebarOpen?'hide signals ›':'‹ signals'}
        </button>
      </div>
    </div>`;
}

function Tooltip({neuron,x,y}){
  if(!neuron)return null;
  return html`
    <div style=${{
      position:'absolute',left:x+18,top:y+18,zIndex:10,
      background:'rgba(15,17,26,0.96)',border:'1px solid '+C.borderStrong,
      borderRadius:10,padding:'12px 14px',minWidth:240,maxWidth:320,
      boxShadow:'0 30px 80px -20px rgba(0,0,0,0.6)',
      WebkitBackdropFilter:'blur(20px)',backdropFilter:'blur(20px)',
      pointerEvents:'none',
    }}>
      <div style=${{
        fontFamily:MONO,fontSize:12.5,color:'#c4b5fd',
        fontWeight:600,marginBottom:6,wordBreak:'break-all',
      }}>${neuron.id}</div>
      ${neuron.capabilities&&neuron.capabilities.length>0&&html`
        <div style=${{display:'flex',flexWrap:'wrap',gap:4,marginBottom:8}}>
          ${neuron.capabilities.map(c=>html`
            <span style=${{
              fontSize:10.5,fontFamily:MONO,padding:'2px 7px',borderRadius:4,
              background:'rgba(34,211,238,0.08)',color:'#67e8f9',
              border:'1px solid rgba(34,211,238,0.2)',
            }}>${c}</span>`)}
        </div>`}
      <div style=${{
        display:'grid',gridTemplateColumns:'auto 1fr',gap:'4px 12px',
        fontSize:11.5,color:C.textDim,fontFamily:MONO,
      }}>
        <span style=${{color:C.textFaint}}>signals</span><span>${neuron.count}</span>
        ${neuron.lastType&&html`
          <span style=${{color:C.textFaint}}>last</span>
          <span style=${{color:colorFor(neuron.lastType)}}>${neuron.lastType}</span>`}
        ${neuron.lastTs&&html`
          <span style=${{color:C.textFaint}}>at</span>
          <span>${new Date(neuron.lastTs).toLocaleTimeString()}</span>`}
        ${neuron.version&&html`
          <span style=${{color:C.textFaint}}>version</span><span>${neuron.version}</span>`}
      </div>
    </div>`;
}

function Sidebar({open,signals,selected,setSelected}){
  return html`
    <aside style=${{
      position:'absolute',top:64,right:0,bottom:0,
      width:open?380:0,background:'rgba(7,8,12,0.85)',
      WebkitBackdropFilter:'blur(20px)',backdropFilter:'blur(20px)',
      borderLeft:open?'1px solid '+C.border:'none',
      transition:'width 0.25s ease',overflow:'hidden',
      zIndex:4,display:'flex',flexDirection:'column',
    }}>
      <div style=${{
        flexShrink:0,padding:'14px 16px',borderBottom:'1px solid '+C.border,
        display:'flex',alignItems:'center',gap:10,
      }}>
        <span style=${{
          fontFamily:MONO,fontSize:11,color:C.accent,
          letterSpacing:'0.14em',textTransform:'uppercase',
        }}>Signal stream</span>
        <span style=${{marginLeft:'auto',color:C.textFaint,fontSize:12,fontFamily:MONO}}>${signals.length}</span>
      </div>
      <div style=${{flex:1,overflowY:'auto'}}>
        ${signals.length===0&&html`
          <div style=${{padding:48,textAlign:'center',color:C.textFaint,fontSize:13}}>
            Waiting for signals…
          </div>`}
        ${signals.map((sig,i)=>{
          const c=colorFor(sig.type);
          const ts=new Date(sig.ts).toISOString().slice(11,23);
          const isSel=selected===sig;
          return html`
            <div key=${sig.id||i}
              onClick=${()=>setSelected(s=>s===sig?null:sig)}
              style=${{
                padding:'10px 16px',cursor:'pointer',
                borderBottom:'1px solid '+C.border,
                background:isSel?'rgba(139,92,246,0.08)':'transparent',
                transition:'background 0.15s',
              }}>
              <div style=${{display:'flex',alignItems:'center',gap:8,marginBottom:4}}>
                <span style=${{
                  display:'inline-block',width:8,height:8,borderRadius:'50%',
                  background:c,boxShadow:`0 0 6px ${c}`,
                }}/>
                <span style=${{
                  color:c,fontFamily:MONO,fontSize:11.5,
                  fontWeight:600,letterSpacing:'0.03em',
                }}>${sig.type}</span>
                <span style=${{
                  marginLeft:'auto',color:C.textFaint,fontSize:10.5,fontFamily:MONO,
                }}>${ts}</span>
              </div>
              <div style=${{
                color:C.textDim,fontSize:11.5,fontFamily:MONO,
                whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis',
              }}>
                ${sig.neuron||'—'}
                <span style=${{color:C.textFaint}}> · ${(sig.trace_id||'').slice(4,12)}</span>
              </div>
              ${isSel&&sig.payload&&html`
                <pre style=${{
                  marginTop:8,padding:8,
                  background:'rgba(0,0,0,0.3)',borderRadius:6,
                  color:C.textDim,fontSize:10.5,fontFamily:MONO,
                  whiteSpace:'pre-wrap',wordBreak:'break-all',
                  maxHeight:240,overflowY:'auto',
                }}>${JSON.stringify(sig.payload,null,2)}</pre>`}
            </div>`;
        })}
      </div>
    </aside>`;
}

function App(){
  const[signals,setSignals]=useState([]);
  const[neurons,setNeurons]=useState(()=>new Map());
  const[vp,setVp]=useState({w:window.innerWidth,h:window.innerHeight-64});
  const[connected,setConnected]=useState(false);
  const[paused,setPaused]=useState(false);
  const[total,setTotal]=useState(0);
  const[particles,setParticles]=useState([]);
  const[pulses,setPulses]=useState(new Set());
  const[tendrilsOn,setTendrilsOn]=useState(new Set());
  const[hover,setHover]=useState(null);
  const[mouse,setMouse]=useState({x:0,y:0});
  const[selected,setSelected]=useState(null);
  const[sidebarOpen,setSidebarOpen]=useState(true);
  const pausedRef=useRef(false);pausedRef.current=paused;
  const pulseTimers=useRef(new Map());
  const tendrilTimers=useRef(new Map());

  useEffect(()=>{
    const r=()=>setVp({w:window.innerWidth,h:window.innerHeight-64});
    window.addEventListener('resize',r);
    return()=>window.removeEventListener('resize',r);
  },[]);
  useEffect(()=>{
    const m=e=>setMouse({x:e.clientX,y:e.clientY});
    window.addEventListener('mousemove',m);
    return()=>window.removeEventListener('mousemove',m);
  },[]);

  useEffect(()=>{
    if(!BASE_URL)return;
    let ws,closed=false;
    function conn(){
      const qs=new URLSearchParams({url:BASE_URL,namespace:NAMESPACE}).toString();
      ws=new WebSocket(`ws://${location.host}/ws?${qs}`);
      ws.onopen=()=>setConnected(true);
      ws.onclose=()=>{setConnected(false);if(!closed)setTimeout(conn,2000);};
      ws.onerror=()=>ws.close();
      ws.onmessage=e=>{
        if(pausedRef.current)return;
        try{handle(JSON.parse(e.data));}catch{}
      };
    }
    conn();
    return()=>{closed=true;ws&&ws.close();};
  },[]);

  const pulse=useCallback(id=>{
    setPulses(p=>{const n=new Set(p);n.add(id);return n;});
    const o=pulseTimers.current.get(id);if(o)clearTimeout(o);
    const t=setTimeout(()=>{
      setPulses(p=>{const n=new Set(p);n.delete(id);return n;});
      pulseTimers.current.delete(id);
    },800);
    pulseTimers.current.set(id,t);
  },[]);

  const flash=useCallback(k=>{
    setTendrilsOn(p=>{const n=new Set(p);n.add(k);return n;});
    const o=tendrilTimers.current.get(k);if(o)clearTimeout(o);
    const t=setTimeout(()=>{
      setTendrilsOn(p=>{const n=new Set(p);n.delete(k);return n;});
      tendrilTimers.current.delete(k);
    },1100);
    tendrilTimers.current.set(k,t);
  },[]);

  function handle(sig){
    setTotal(t=>t+1);
    setSignals(prev=>[sig,...prev].slice(0,500));
    const nid=sig.neuron;

    // (1) Registry — every neuron we see becomes a blob.
    if(nid){
      setNeurons(prev=>{
        const next=new Map(prev);
        const ex=next.get(nid)||{id:nid,count:0,capabilities:[],firstSeen:sig.ts};
        ex.count++;ex.lastType=sig.type;ex.lastTs=sig.ts;
        if(sig.type==='REGISTER'){
          ex.capabilities=sig.payload?.capabilities||ex.capabilities||[];
          ex.version=sig.payload?.version||ex.version;
          ex.deregistered=false;
        }
        if(sig.type==='DEREGISTER')ex.deregistered=true;
        next.set(nid,ex);return next;
      });
    }

    // (2) Source / destination per the protocol rules.
    let src,dst;
    if(nid&&AXON.has(sig.type)){src=nid;dst='__synapse__';}
    else if(nid&&TARGET.has(sig.type)){src='__synapse__';dst=nid;}
    else if(nid){src='__synapse__';dst=nid;}
    else{src='__synapse__';dst='__synapse__';}

    // (3) Pulse source + synapse + destination.
    pulse('__synapse__');
    if(src!=='__synapse__')pulse(src);
    if(dst!=='__synapse__')pulse(dst);

    // (4) Flash the tendril + send a coloured particle along it.
    if(src!==dst){
      flash(`${src}::${dst}`);
      const pid=`${sig.id||Math.random()}_${Date.now()}`;
      setParticles(p=>[...p,{id:pid,from:src,to:dst,color:colorFor(sig.type)}]);
    }
  }

  const drop=useCallback(id=>setParticles(p=>p.filter(x=>x.id!==id)),[]);
  const layout=useLayout(neurons,vp);

  const tendrils=useMemo(()=>{
    const o=[];
    for(const ne of neurons.values()){
      o.push({
        id:ne.id,from:layout[ne.id],to:layout.__synapse__,
        k1:`${ne.id}::__synapse__`,k2:`__synapse__::${ne.id}`,
      });
    }
    return o;
  },[neurons,layout]);

  const hoverInfo=hover?neurons.get(hover):null;
  const onClear=()=>{setSignals([]);setTotal(0);setSelected(null);};
  const onBack=()=>{location.href='/';};

  if(!BASE_URL){
    return html`
      <div style=${{display:'flex',alignItems:'center',justifyContent:'center',
        height:'100vh',color:C.textDim,gap:8}}>
        No synapse URL provided.
        <a href="/" style=${{color:C.accent}}>Go back →</a>
      </div>`;
  }

  return html`
    <${Header} connected=${connected} total=${total}
      paused=${paused} setPaused=${setPaused}
      sidebarOpen=${sidebarOpen} setSidebarOpen=${setSidebarOpen}
      onClear=${onClear} onBack=${onBack}/>

    <svg width=${vp.w} height=${vp.h}
      style=${{position:'absolute',top:64,left:0,
               marginRight:sidebarOpen?380:0,
               transition:'margin-right 0.25s ease'}}>
      <defs>
        <radialGradient id="centerGlow">
          <stop offset="0%" stopColor=${C.accent} stopOpacity="0.3"/>
          <stop offset="100%" stopColor=${C.accent} stopOpacity="0"/>
        </radialGradient>
      </defs>
      <circle cx=${vp.w/2} cy=${vp.h/2} r="260"
        fill="url(#centerGlow)" style=${{pointerEvents:'none'}}/>

      ${tendrils.map(t=>html`
        <${Tendril} key=${t.id} from=${t.from} to=${t.to}
          active=${tendrilsOn.has(t.k1)||tendrilsOn.has(t.k2)}/>`)}

      ${particles.map(p=>html`
        <${Particle} key=${p.id} id=${p.id}
          from=${layout[p.from]} to=${layout[p.to]}
          color=${p.color} onDone=${drop}/>`)}

      <${Blob} x=${layout.__synapse__.x} y=${layout.__synapse__.y}
        r=${56} color=${C.accent}
        pulse=${pulses.has('__synapse__')}
        label="synapse" sublabel=${NAMESPACE} isSyn=${true}
        onHover=${()=>{}} onLeave=${()=>{}} onClick=${()=>{}}/>

      ${Array.from(neurons.values()).map(ne=>{
        const p=layout[ne.id];if(!p)return null;
        const color=ne.deregistered?C.textFaint:colorFor(ne.lastType||'REGISTER');
        return html`
          <${Blob} key=${ne.id} x=${p.x} y=${p.y}
            r=${28} color=${color}
            pulse=${pulses.has(ne.id)}
            label=${ne.id.length>18?ne.id.slice(0,16)+'…':ne.id}
            sublabel=${ne.capabilities&&ne.capabilities.length>0?ne.capabilities[0]:''}
            onHover=${()=>setHover(ne.id)}
            onLeave=${()=>setHover(null)}
            onClick=${()=>setHover(h=>h===ne.id?null:ne.id)}/>`;
      })}

      ${neurons.size===0&&html`
        <text x=${vp.w/2} y=${vp.h/2+160} textAnchor="middle"
          fill=${C.textFaint} fontSize="13" fontFamily=${MONO}>
          Waiting for neurons to register…
        </text>`}
    </svg>

    <${Tooltip} neuron=${hoverInfo} x=${mouse.x} y=${mouse.y}/>
    <${Sidebar} open=${sidebarOpen} signals=${signals}
      selected=${selected} setSelected=${setSelected}/>
  `;
}

createRoot(document.getElementById('root')).render(html`<${App}/>`);
</script>
</body>
</html>"""
