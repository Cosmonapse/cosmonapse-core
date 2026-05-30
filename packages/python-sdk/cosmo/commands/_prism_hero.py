"""
cosmo.commands._prism_hero
~~~~~~~~~~~~~~~~~~~~~~~~~~~
HTML for the Prism hero / landing page.

Two ``__INITIAL_URL__`` and ``__INITIAL_NS__`` placeholders are substituted
by ``_prism.run_prism`` before serving so any CLI-supplied URL pre-fills the
form. The page redirects to ``/view?url=...&namespace=...`` on submit.
"""

HERO_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cosmonapse Prism</title>
<style>
:root{
  --bg:#07080c;--bg-elev:#0c0e15;--bg-card:#0f111a;
  --border:rgba(255,255,255,0.06);--border-strong:rgba(255,255,255,0.12);
  --text:#e6e7ec;--text-dim:#9097a8;--text-faint:#5b6275;
  --accent:#8b5cf6;--accent-2:#22d3ee;--accent-3:#f472b6;
  --glow:rgba(139,92,246,0.35);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);overflow:hidden;}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,sans-serif;
  line-height:1.6;-webkit-font-smoothing:antialiased;
  display:flex;flex-direction:column;
}
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:
    linear-gradient(rgba(255,255,255,0.025) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,0.025) 1px,transparent 1px);
  background-size:56px 56px;
  -webkit-mask-image:radial-gradient(ellipse at top,black 30%,transparent 80%);
          mask-image:radial-gradient(ellipse at top,black 30%,transparent 80%);
}
body::after{
  content:'';position:fixed;top:-20%;left:50%;
  width:1200px;height:800px;transform:translateX(-50%);
  background:radial-gradient(ellipse,var(--glow) 0%,transparent 60%);
  opacity:0.7;filter:blur(40px);pointer-events:none;z-index:0;
}

.nav{
  position:relative;z-index:2;border-bottom:1px solid var(--border);
  background:rgba(7,8,12,0.7);
  -webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);
}
.nav-inner{
  max-width:1180px;margin:0 auto;padding:0 24px;
  height:64px;display:flex;align-items:center;justify-content:space-between;
}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;
      letter-spacing:-0.01em;color:var(--text);text-decoration:none;}
.logo-mark{
  width:22px;height:22px;border-radius:6px;position:relative;
  background:conic-gradient(from 180deg at 50% 50%,var(--accent),var(--accent-2),var(--accent-3),var(--accent));
  box-shadow:0 0 18px var(--glow);
  animation:spin 8s linear infinite;
}
.logo-mark::after{
  content:'';position:absolute;inset:5px;border-radius:3px;background:var(--bg);
}
@keyframes spin{to{transform:rotate(360deg)}}
.tagline{color:var(--text-dim);font-size:13px;font-family:ui-monospace,Menlo,monospace;}
.tagline .dim{color:var(--text-faint);}

main{
  flex:1;display:flex;align-items:center;justify-content:center;
  position:relative;z-index:1;padding:24px;
}
.hero{text-align:center;max-width:640px;width:100%;animation:fadeUp 0.6s ease-out;}
@keyframes fadeUp{
  from{opacity:0;transform:translateY(12px);}
  to{opacity:1;transform:translateY(0);}
}
.badge{
  display:inline-flex;align-items:center;gap:8px;
  padding:6px 14px;border-radius:999px;
  background:rgba(139,92,246,0.08);border:1px solid rgba(139,92,246,0.25);
  font-size:12px;font-weight:500;color:#c4b5fd;
  font-family:ui-monospace,Menlo,monospace;letter-spacing:0.02em;
  margin-bottom:28px;
}
.badge .dot{
  width:6px;height:6px;border-radius:50%;
  background:#a78bfa;box-shadow:0 0 8px #a78bfa;
  animation:pulseDot 2s ease-in-out infinite;
}
@keyframes pulseDot{0%,100%{opacity:1}50%{opacity:0.4}}
h1{
  font-size:clamp(40px,6vw,68px);
  line-height:1.05;font-weight:700;letter-spacing:-0.035em;
  margin-bottom:18px;
}
.gradient-text{
  background:linear-gradient(135deg,#fff 20%,#a78bfa 50%,#67e8f9 80%);
  -webkit-background-clip:text;background-clip:text;
  -webkit-text-fill-color:transparent;
}
.lead{
  font-size:clamp(15px,1.6vw,18px);color:var(--text-dim);
  max-width:520px;margin:0 auto 36px;line-height:1.55;
}

form{
  display:flex;flex-direction:column;gap:14px;
  max-width:520px;margin:0 auto;
  background:linear-gradient(180deg,rgba(15,17,26,0.9),rgba(12,14,21,0.9));
  border:1px solid var(--border);border-radius:14px;padding:24px;
  box-shadow:0 30px 80px -20px rgba(0,0,0,0.6),0 0 60px -20px var(--glow);
}
.row{display:grid;grid-template-columns:1fr 200px;gap:10px;}
@media(max-width:560px){.row{grid-template-columns:1fr;}}
.field{display:flex;flex-direction:column;gap:6px;text-align:left;}
.field label{
  font-family:ui-monospace,Menlo,monospace;font-size:11px;
  color:var(--text-faint);letter-spacing:0.12em;text-transform:uppercase;
}
.field input{
  background:#0a0c12;border:1px solid var(--border-strong);
  border-radius:9px;padding:11px 14px;
  color:var(--text);font-size:14px;
  font-family:ui-monospace,Menlo,monospace;
  outline:none;transition:all 0.15s;
}
.field input:focus{
  border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(139,92,246,0.18);
}
.field input::placeholder{color:var(--text-faint);}
.error{
  color:#fca5a5;font-size:12.5px;
  background:rgba(248,113,113,0.08);
  border:1px solid rgba(248,113,113,0.25);
  padding:8px 12px;border-radius:8px;
  font-family:ui-monospace,Menlo,monospace;
  display:none;
}
.error.show{display:block;}

button.primary{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:13px 22px;border-radius:10px;
  background:#fff;color:#07080c;border:none;
  font-size:14px;font-weight:600;cursor:pointer;
  transition:all 0.15s;font-family:inherit;
  box-shadow:0 0 0 1px rgba(255,255,255,0.1),0 12px 30px -10px rgba(139,92,246,0.5);
}
button.primary:hover{
  transform:translateY(-1px);
  box-shadow:0 0 0 1px rgba(255,255,255,0.15),0 14px 36px -10px rgba(139,92,246,0.65);
}
button.primary:disabled{opacity:0.5;cursor:not-allowed;transform:none;}
button.primary .arrow{transition:transform 0.15s;}
button.primary:hover .arrow{transform:translateX(2px);}

.hint{
  margin-top:24px;color:var(--text-faint);font-size:12px;
  font-family:ui-monospace,Menlo,monospace;
}
.hint code{
  background:rgba(139,92,246,0.08);color:#c4b5fd;
  padding:1px 6px;border-radius:4px;
}
</style>
</head>
<body>
<nav class="nav">
  <div class="nav-inner">
    <a class="logo" href="/">
      <span class="logo-mark"></span>
      Cosmonapse <span style="color:var(--text-dim);font-weight:500;">Prism</span>
    </a>
    <div class="tagline"><span class="dim">◉</span> read-only doppler</div>
  </div>
</nav>

<main>
  <div class="hero">
    <div class="badge">
      <span class="dot"></span>
      Live signal visualization
    </div>
    <h1>Welcome to <span class="gradient-text">Cosmonapse Prism</span></h1>
    <p class="lead">
      Watch every Signal cross the Synapse — Neurons pulse as they fire,
      Dendrites glow as they route, and the bus hums in real time.
    </p>

    <form id="connect-form" autocomplete="off">
      <div class="row">
        <div class="field" style="grid-column:1 / -1;">
          <label for="url">Synapse URL</label>
          <input type="text" id="url" name="url" required
            placeholder="cosmo://127.0.0.1:7070"
            value="__INITIAL_URL__"/>
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label for="namespace">Namespace</label>
          <input type="text" id="namespace" name="namespace"
            placeholder="dev" value="__INITIAL_NS__"/>
        </div>
        <div class="field">
          <label>&nbsp;</label>
          <button type="submit" class="primary">
            Attach Prism <span class="arrow">&rarr;</span>
          </button>
        </div>
      </div>
      <div class="error" id="err"></div>
    </form>

    <div class="hint">
      e.g. <code>cosmo://127.0.0.1:7070</code> &middot;
      <code>nats://localhost:4222</code> &middot;
      <code>kafka://localhost:9092</code>
    </div>
  </div>
</main>

<script>
const form=document.getElementById('connect-form');
const errEl=document.getElementById('err');
form.addEventListener('submit',(e)=>{
  e.preventDefault();
  const url=document.getElementById('url').value.trim();
  const ns=(document.getElementById('namespace').value||'dev').trim();
  if(!url){
    errEl.textContent='Synapse URL is required.';
    errEl.classList.add('show'); return;
  }
  if(!/^(cosmo|nats|kafka):\/\//i.test(url)){
    errEl.textContent='URL must start with cosmo://, nats://, or kafka://';
    errEl.classList.add('show'); return;
  }
  errEl.classList.remove('show');
  const qs=new URLSearchParams({url,namespace:ns}).toString();
  location.href='/view?'+qs;
});
</script>
</body>
</html>"""
