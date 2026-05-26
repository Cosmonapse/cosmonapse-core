"""
cosmo doppler
~~~~~~~~~~~~~
Attach a read-only Doppler to a running Synapse, streaming Signals to stdout
or a live browser UI.

Usage
-----
    cosmo doppler --synapse=cosmo://127.0.0.1:7070/dev
    cosmo doppler --synapse=nats://localhost:4222/prod
    cosmo doppler --synapse=cosmo://127.0.0.1:7070/dev --ui
    cosmo doppler --synapse=cosmo://127.0.0.1:7070/dev --type TASK --type ERROR
    cosmo doppler --synapse=cosmo://127.0.0.1:7070/dev --json

Synapse URL format
------------------
    <scheme>://<host>:<port>/<namespace>

    cosmo://127.0.0.1:7070/dev   → DevSynapse (TCP+NDJSON)
    nats://localhost:4222/prod   → NatsSynapse
    kafka://localhost:9092/prod  → KafkaSynapse

    The path component is the namespace. Omitting it defaults to "dev".
"""

from __future__ import annotations

import asyncio
import json
import signal as _signal
import sys
import webbrowser
from typing import Optional
from urllib.parse import urlparse

import click

try:
    from rich.console import Console
    from rich.text import Text
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

from cosmonapse import Signal, SignalType

if _HAS_RICH:
    console = Console()

_TYPE_COLOURS: dict[str, str] = {
    "TASK": "cyan",
    "AGENT_OUTPUT": "green",
    "FINAL": "bold green",
    "ERROR": "bold red",
    "CLARIFICATION": "yellow",
    "REGISTER": "blue",
    "DEREGISTER": "blue",
    "HEARTBEAT": "dim blue",
    "TASK_OFFER": "magenta",
    "BID": "magenta",
    "TASK_AWARDED": "bold magenta",
    "TASK_DECLINED": "dim magenta",
    "THOUGHT_DELTA": "dim white",
    "PLAN": "white",
    "TOOL_CALL": "bright_white",
    "TOOL_RESULT": "bright_white",
    "MEMORY_APPEND": "bright_cyan",
    "ESCALATION": "bold yellow",
    "CONSENSUS": "bold cyan",
    "CONTEXT_SYNC": "cyan",
    "CRITIQUE": "yellow",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_synapse_arg(synapse_arg: str) -> tuple[str, str]:
    """
    Split --synapse=<url>/<namespace> into (base_url, namespace).

    cosmo://127.0.0.1:7070/dev  →  ("cosmo://127.0.0.1:7070", "dev")
    nats://localhost:4222/prod   →  ("nats://localhost:4222",  "prod")
    cosmo://127.0.0.1:7070       →  ("cosmo://127.0.0.1:7070", "dev")
    """
    parsed = urlparse(synapse_arg)
    namespace = parsed.path.lstrip("/") or "dev"
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return base_url, namespace


def _make_synapse(base_url: str):
    """Return the appropriate Synapse instance for the given URL scheme."""
    scheme = base_url.split("://")[0].lower()
    if scheme == "cosmo":
        from cosmonapse.synapse.dev import DevSynapse
        return DevSynapse(url=base_url)
    elif scheme == "nats":
        from cosmonapse.synapse.nats import NatsSynapse
        return NatsSynapse(url=base_url)
    elif scheme == "kafka":
        from cosmonapse.synapse.kafka import KafkaSynapse
        broker = base_url.replace("kafka://", "")
        return KafkaSynapse(bootstrap_servers=broker)
    else:
        raise click.ClickException(
            f"Unknown synapse scheme {scheme!r}. "
            "Use cosmo://, nats://, or kafka://."
        )


def _render_signal(subject: str, sig: Signal, show_payload: bool = False) -> None:
    if _HAS_RICH:
        colour = _TYPE_COLOURS.get(sig.type.value, "white")
        ts = sig.ts.strftime("%H:%M:%S.%f")[:-3]
        neuron = sig.neuron or "—"
        trace = sig.trace_id[4:12]
        t = Text()
        t.append(f"  {ts}  ", style="dim")
        t.append(f"{sig.type.value:<18}", style=colour)
        t.append(f"  {trace}  ", style="dim")
        t.append(f"{neuron}", style="italic")
        if show_payload and sig.payload:
            payload_str = json.dumps(sig.payload, default=str)
            if len(payload_str) > 80:
                payload_str = payload_str[:77] + "…"
            t.append(f"\n    {payload_str}", style="dim")
        console.print(t)
    else:
        ts = sig.ts.strftime("%H:%M:%S")
        print(f"  {ts}  {sig.type.value:<18}  {sig.trace_id[4:12]}  {sig.neuron or '—'}")


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--synapse", "synapse_arg", required=True, metavar="URL/NAMESPACE",
    help="Synapse URL with namespace, e.g. cosmo://127.0.0.1:7070/dev",
)
@click.option("--ui", "show_ui", is_flag=True, default=False,
              help="Launch a browser UI instead of streaming to stdout.")
@click.option("--port", default=7072, show_default=True,
              help="Local port for the browser UI server (--ui mode).")
@click.option(
    "--type", "filter_types", multiple=True,
    type=click.Choice([t.value for t in SignalType], case_sensitive=False),
    help="Filter to specific signal types (repeatable).",
)
@click.option("--trace", default=None,
              help="Filter to a specific trace_id.")
@click.option("--neuron", default=None,
              help="Filter to a specific neuron ID.")
@click.option("--json", "output_json", is_flag=True,
              help="Output one JSON object per line (CLI mode only).")
@click.option("--payload", is_flag=True,
              help="Show payload preview alongside each signal (CLI mode only).")
def doppler(
    synapse_arg: str,
    show_ui: bool,
    port: int,
    filter_types: tuple[str, ...],
    trace: Optional[str],
    neuron: Optional[str],
    output_json: bool,
    payload: bool,
) -> None:
    """Attach a read-only Doppler to a Synapse namespace.

    \b
    Stream to stdout:
      cosmo doppler --synapse=cosmo://127.0.0.1:7070/dev

    Open browser UI:
      cosmo doppler --synapse=cosmo://127.0.0.1:7070/dev --ui

    Filter by type:
      cosmo doppler --synapse=cosmo://127.0.0.1:7070/dev --type TASK --type ERROR
    """
    base_url, namespace = _parse_synapse_arg(synapse_arg)

    if show_ui:
        asyncio.run(_run_ui(base_url=base_url, namespace=namespace, port=port))
    else:
        asyncio.run(_run_cli(
            base_url=base_url,
            namespace=namespace,
            filter_types=set(filter_types),
            trace=trace,
            neuron_filter=neuron,
            output_json=output_json,
            show_payload=payload,
        ))


# ---------------------------------------------------------------------------
# CLI mode
# ---------------------------------------------------------------------------

async def _run_cli(
    base_url: str,
    namespace: str,
    filter_types: set[str],
    trace: str | None,
    neuron_filter: str | None,
    output_json: bool,
    show_payload: bool,
) -> None:
    try:
        syn = _make_synapse(base_url)
    except click.ClickException as e:
        click.echo(f"  Error: {e.format_message()}", err=True)
        raise SystemExit(1)

    try:
        await syn.connect()
    except ImportError as e:
        click.echo(f"  {e}", err=True)
        raise SystemExit(1)
    except (ConnectionRefusedError, OSError) as e:
        click.echo(f"  Cannot connect to {base_url}: {e}", err=True)
        raise SystemExit(1)

    if not output_json:
        if _HAS_RICH:
            console.print()
            console.print(f"  [bold cyan]cosmo doppler[/bold cyan]  "
                          f"[cyan]{base_url}[/cyan][dim]/{namespace}[/dim]")
            if filter_types:
                console.print(f"  Filtering: {', '.join(sorted(filter_types))}")
            if trace:
                console.print(f"  Trace:  [dim]{trace}[/dim]")
            if neuron_filter:
                console.print(f"  Neuron: [italic]{neuron_filter}[/italic]")
            console.print()
            console.print("  [dim]Observing — Ctrl-C to detach[/dim]")
            console.print("  " + "─" * 60)
            console.print()
        else:
            print(f"\n  cosmo doppler  {base_url}/{namespace}")
            print("  Observing — Ctrl-C to detach")
            print("  " + "─" * 60 + "\n")

    signal_count = 0

    async def handle(sig: Signal) -> None:
        nonlocal signal_count
        if filter_types and sig.type.value not in filter_types:
            return
        if trace and sig.trace_id != trace:
            return
        if neuron_filter and sig.neuron != neuron_filter:
            return
        signal_count += 1
        if output_json:
            print(sig.model_dump_json(), flush=True)
        else:
            _render_signal(f"cosmonapse.{namespace}.{sig.type.value}", sig,
                           show_payload=show_payload)

    subject = f"cosmonapse.{namespace}.>"
    await syn.subscribe(subject, handle, queue_group=None)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for s in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await syn.close()
        if not output_json:
            if _HAS_RICH:
                console.print()
                console.print(f"  [dim]Doppler detached.  {signal_count} signals observed.[/dim]")
                console.print()
            else:
                print(f"\n  Doppler detached.  {signal_count} signals observed.\n")


# ---------------------------------------------------------------------------
# Browser UI mode
# ---------------------------------------------------------------------------

_UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Doppler — __NAMESPACE__</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{
  background:#0d0f14;color:#c9d1e0;
  font-family:'Cascadia Code','SF Mono','Consolas',monospace;
  font-size:12.5px;
}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:#0d0f14}
::-webkit-scrollbar-thumb{background:#2a2f3e;border-radius:3px}
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
import React,{useState,useEffect,useRef,useMemo,useCallback} from 'react';
import{createRoot}from 'react-dom/client';
import{html}from 'htm/react';

const NAMESPACE='__NAMESPACE__';
const BASE_URL='__BASE_URL__';

const TYPE_COLORS={
  TASK:'#38bdf8',AGENT_OUTPUT:'#34d399',FINAL:'#10b981',ERROR:'#f87171',
  CLARIFICATION:'#fbbf24',REGISTER:'#60a5fa',DEREGISTER:'#60a5fa',
  HEARTBEAT:'#334155',TASK_OFFER:'#c084fc',BID:'#c084fc',
  TASK_AWARDED:'#a855f7',TASK_DECLINED:'#7c3aed',THOUGHT_DELTA:'#475569',
  PLAN:'#94a3b8',TOOL_CALL:'#e2e8f0',TOOL_RESULT:'#e2e8f0',
  MEMORY_APPEND:'#22d3ee',ESCALATION:'#fb923c',CONSENSUS:'#06b6d4',
  CONTEXT_SYNC:'#22d3ee',CRITIQUE:'#fbbf24',
};

function Badge({type}){
  const c=TYPE_COLORS[type]||'#94a3b8';
  return html`<span style=${{
    color:c,background:c+'1a',border:`1px solid ${c}30`,
    borderRadius:'3px',padding:'1px 7px',fontSize:'11px',
    fontWeight:600,letterSpacing:'0.04em',whiteSpace:'nowrap',
    fontFamily:'inherit',
  }}>${type}</span>`;
}

function Row({sig,selected,onClick}){
  const ts=new Date(sig.ts).toISOString().slice(11,23);
  const[hov,setHov]=useState(false);
  return html`
    <div onClick=${onClick}
      onMouseEnter=${()=>setHov(true)} onMouseLeave=${()=>setHov(false)}
      style=${{
        display:'grid',gridTemplateColumns:'110px minmax(140px,1fr) 80px 1fr',
        gap:'12px',padding:'5px 16px',cursor:'pointer',alignItems:'center',
        background:selected?'#1e293b':hov?'#131720':'transparent',
        borderBottom:'1px solid #111520',
      }}>
      <span style=${{color:'#334155',fontVariantNumeric:'tabular-nums',fontSize:'11.5px'}}>${ts}</span>
      <${Badge} type=${sig.type}/>
      <span style=${{color:'#334155',fontFamily:'inherit'}}>${(sig.trace_id||'').slice(4,12)||'—'}</span>
      <span style=${{color:'#475569',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',fontStyle:'italic'}}>${sig.neuron||'—'}</span>
    </div>`;
}

function TypeChip({type,count,active,onClick}){
  const c=TYPE_COLORS[type]||'#94a3b8';
  return html`
    <button onClick=${onClick} style=${{
      display:'flex',alignItems:'center',gap:'5px',
      background:active?c+'18':'transparent',
      border:`1px solid ${active?c+'40':'#1e293b'}`,
      borderRadius:'4px',padding:'2px 8px',cursor:'pointer',
      color:active?c:'#475569',fontSize:'11px',fontFamily:'inherit',
    }}>
      <span style=${{color:c}}>${type}</span>
      <span style=${{color:'#475569',background:'#131720',borderRadius:'3px',padding:'0 4px'}}>${count}</span>
    </button>`;
}

function App(){
  const[signals,setSignals]=useState([]);
  const[connected,setConnected]=useState(false);
  const[paused,setPaused]=useState(false);
  const[typeFilter,setTypeFilter]=useState(null);
  const[textFilter,setTextFilter]=useState('');
  const[selected,setSelected]=useState(null);
  const[counts,setCounts]=useState({});
  const[total,setTotal]=useState(0);
  const pausedRef=useRef(false);
  pausedRef.current=paused;

  useEffect(()=>{
    function connect(){
      const ws=new WebSocket(`ws://${location.host}/ws`);
      ws.onopen=()=>setConnected(true);
      ws.onclose=()=>{setConnected(false);setTimeout(connect,2000);};
      ws.onerror=()=>ws.close();
      ws.onmessage=(e)=>{
        if(pausedRef.current)return;
        try{
          const sig=JSON.parse(e.data);
          setTotal(t=>t+1);
          setSignals(prev=>[sig,...prev].slice(0,2000));
          setCounts(prev=>({...prev,[sig.type]:(prev[sig.type]||0)+1}));
        }catch{}
      };
    }
    connect();
  },[]);

  const filtered=useMemo(()=>{
    let s=signals;
    if(typeFilter)s=s.filter(x=>x.type===typeFilter);
    if(textFilter){
      const q=textFilter.toLowerCase();
      s=s.filter(x=>
        (x.neuron||'').toLowerCase().includes(q)||
        (x.trace_id||'').toLowerCase().includes(q)||
        JSON.stringify(x.payload||{}).toLowerCase().includes(q)
      );
    }
    return s;
  },[signals,typeFilter,textFilter]);

  const sortedTypes=useMemo(()=>
    Object.entries(counts).sort((a,b)=>b[1]-a[1]),
  [counts]);

  const hdr={color:'#1e293b',fontSize:'11px',letterSpacing:'0.06em',textTransform:'uppercase'};

  return html`
    <div style=${{height:'100vh',display:'flex',flexDirection:'column',overflow:'hidden'}}>

      <!-- Header -->
      <div style=${{
        display:'flex',alignItems:'center',gap:'10px',
        padding:'9px 16px',background:'#080a0f',
        borderBottom:'1px solid #1a1e2a',flexShrink:0,
      }}>
        <span style=${{color:'#38bdf8',fontWeight:700,fontSize:'13px',letterSpacing:'0.05em'}}>◉ doppler</span>
        <span style=${{color:'#1e293b'}}>│</span>
        <span style=${{color:'#60a5fa',fontSize:'12px'}}>${BASE_URL}</span>
        <span style=${{color:'#334155',fontSize:'12px'}}>/${NAMESPACE}</span>

        <input
          placeholder="filter trace / neuron / payload…"
          value=${textFilter}
          onInput=${e=>setTextFilter(e.target.value)}
          style=${{
            marginLeft:'auto',background:'#0d0f14',border:'1px solid #1e293b',
            borderRadius:'4px',padding:'3px 10px',color:'#94a3b8',
            fontFamily:'inherit',fontSize:'11.5px',width:'240px',outline:'none',
          }}
        />

        <span style=${{color:connected?'#34d399':'#f87171',fontSize:'11px',marginLeft:'8px'}}>
          ${connected?'● connected':'○ reconnecting…'}
        </span>
        <span style=${{color:'#1e293b',fontSize:'11px'}}>${total} total</span>

        <button onClick=${()=>setPaused(p=>!p)} style=${{
          background:paused?'#fbbf2415':'transparent',
          border:`1px solid ${paused?'#fbbf2440':'#1e293b'}`,
          borderRadius:'4px',padding:'3px 10px',
          color:paused?'#fbbf24':'#475569',cursor:'pointer',
          fontFamily:'inherit',fontSize:'11px',
        }}>${paused?'▶ resume':'⏸ pause'}</button>

        <button onClick=${()=>{setSignals([]);setCounts({});setTotal(0);setSelected(null);}} style=${{
          background:'transparent',border:'1px solid #1e293b',
          borderRadius:'4px',padding:'3px 10px',
          color:'#475569',cursor:'pointer',fontFamily:'inherit',fontSize:'11px',
        }}>clear</button>
      </div>

      <!-- Type chips -->
      ${sortedTypes.length>0&&html`
        <div style=${{
          display:'flex',gap:'5px',padding:'7px 16px',
          background:'#080a0f',borderBottom:'1px solid #111520',
          flexWrap:'wrap',flexShrink:0,
        }}>
          ${sortedTypes.map(([type,count])=>html`
            <${TypeChip} key=${type} type=${type} count=${count}
              active=${typeFilter===type}
              onClick=${()=>setTypeFilter(f=>f===type?null:type)}
            />`)}
        </div>
      `}

      <!-- Column headers -->
      <div style=${{
        display:'grid',gridTemplateColumns:'110px minmax(140px,1fr) 80px 1fr',
        gap:'12px',padding:'5px 16px',
        borderBottom:'1px solid #111520',flexShrink:0,
      }}>
        <span style=${hdr}>time</span>
        <span style=${hdr}>type</span>
        <span style=${hdr}>trace</span>
        <span style=${hdr}>neuron</span>
      </div>

      <!-- Body -->
      <div style=${{flex:1,display:'flex',overflow:'hidden'}}>

        <!-- Signal list -->
        <div style=${{
          flex:selected?'0 0 55%':'1',overflowY:'auto',
          background:'#0d0f14',
        }}>
          ${filtered.length===0&&html`
            <div style=${{padding:'48px',textAlign:'center',color:'#1e293b'}}>
              ${connected?'Waiting for signals…':'Connecting to synapse…'}
            </div>`}
          ${filtered.map((sig,i)=>html`
            <${Row} key=${sig.id||i} sig=${sig}
              selected=${selected===sig}
              onClick=${()=>setSelected(s=>s===sig?null:sig)}
            />`)}
        </div>

        <!-- Detail pane -->
        ${selected&&html`
          <div style=${{
            flex:'0 0 45%',borderLeft:'1px solid #1a1e2a',
            background:'#080a0f',display:'flex',flexDirection:'column',overflowY:'auto',
          }}>
            <div style=${{
              display:'flex',alignItems:'center',gap:'10px',
              padding:'9px 16px',borderBottom:'1px solid #111520',flexShrink:0,
            }}>
              <${Badge} type=${selected.type}/>
              <span style=${{color:'#334155',fontSize:'11px',flex:1}}>
                ${new Date(selected.ts).toISOString()}
              </span>
              <button onClick=${()=>setSelected(null)} style=${{
                background:'transparent',border:'none',
                color:'#334155',cursor:'pointer',fontSize:'18px',lineHeight:1,
              }}>×</button>
            </div>
            <pre style=${{
              padding:'16px',color:'#64748b',fontSize:'11.5px',
              lineHeight:'1.65',overflowX:'auto',
              whiteSpace:'pre-wrap',wordBreak:'break-all',flex:1,
            }}>${JSON.stringify(selected,null,2)}</pre>
          </div>
        `}
      </div>
    </div>`;
}

createRoot(document.getElementById('root')).render(html`<${App}/>`);
</script>
</body>
</html>"""


async def _run_ui(base_url: str, namespace: str, port: int) -> None:
    """Start an aiohttp server + WebSocket bridge, open browser."""
    try:
        from aiohttp import web
    except ImportError:
        click.echo(
            "  aiohttp is required for --ui mode.\n"
            "  Install it with: pip install aiohttp\n",
            err=True,
        )
        raise SystemExit(1)

    # Build the synapse connection
    try:
        syn = _make_synapse(base_url)
    except click.ClickException as e:
        click.echo(f"  Error: {e.format_message()}", err=True)
        raise SystemExit(1)

    try:
        await syn.connect()
    except ImportError as e:
        click.echo(f"  {e}", err=True)
        raise SystemExit(1)
    except (ConnectionRefusedError, OSError) as e:
        click.echo(f"  Cannot connect to {base_url}: {e}", err=True)
        raise SystemExit(1)

    # Build the HTML with placeholders replaced
    html_page = (
        _UI_HTML
        .replace("__NAMESPACE__", namespace)
        .replace("__BASE_URL__", base_url)
    )

    # Track WebSocket clients
    ws_clients: set = set()

    async def handle_index(request):
        return web.Response(text=html_page, content_type="text/html")

    async def handle_ws(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        ws_clients.add(ws)
        try:
            async for _ in ws:
                pass  # we only send, never read
        finally:
            ws_clients.discard(ws)
        return ws

    # Subscribe to the synapse and broadcast to all WS clients
    async def handle_signal(sig: Signal) -> None:
        if not ws_clients:
            return
        data = sig.model_dump_json()
        dead = set()
        for ws in list(ws_clients):
            try:
                await ws.send_str(data)
            except Exception:
                dead.add(ws)
        ws_clients -= dead

    subject = f"cosmonapse.{namespace}.>"
    await syn.subscribe(subject, handle_signal, queue_group=None)

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    ui_url = f"http://127.0.0.1:{port}"
    if _HAS_RICH:
        console.print()
        console.print(f"  [bold cyan]cosmo doppler[/bold cyan]  [dim]--ui[/dim]")
        console.print(f"  Synapse:   [cyan]{base_url}/{namespace}[/cyan]")
        console.print(f"  UI:        [underline cyan]{ui_url}[/underline cyan]")
        console.print()
        console.print("  [dim]Ctrl-C to stop[/dim]")
        console.print("  " + "─" * 60)
        console.print()
    else:
        print(f"\n  cosmo doppler --ui")
        print(f"  Synapse: {base_url}/{namespace}")
        print(f"  UI:      {ui_url}")
        print("  Ctrl-C to stop\n")

    webbrowser.open(ui_url)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for s in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await runner.cleanup()
        await syn.close()
        if _HAS_RICH:
            console.print()
            console.print("  [dim]Doppler UI stopped.[/dim]")
            console.print()
        else:
            print("\n  Doppler UI stopped.\n")
