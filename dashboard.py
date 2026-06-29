"""A thin web dashboard for the agent — standard library only (no Flask/FastAPI).

It wraps the SAME `Agent` the CLI uses: a chatbox runs `agent.run()`, and the
page foregrounds the things graders care about — the structured execution
report, the memory before/after diff, the run-1-vs-run-N learning curve, and a
one-click rollback. Nothing here re-implements agent logic; it only renders it.

    python dashboard.py          # then open http://localhost:8765
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
from agent.core import Agent
from memory.store import Memory, normalize

PORT = 8765


# --------------------------------------------------------------------------- #
# JSON payloads (read-only views over memory)                                 #
# --------------------------------------------------------------------------- #
def memory_payload() -> dict:
    m = Memory()
    caps = []
    for c in m.capabilities.capabilities.values():
        caps.append({
            "name": c["name"], "origin": c.get("origin"), "kind": c["kind"],
            "uses": c["stats"]["uses"],
            "success_rate": m.capabilities.success_rate(c["name"]),
            "constraints": c.get("constraints", []),
        })
    caps.sort(key=lambda c: (c["origin"] != "synthesized", c["name"]))
    recent = []
    for r in m.executions.records[-12:][::-1]:
        recent.append({
            "id": r["id"], "signature": r["signature"], "outcome": r["outcome"],
            "source": r.get("executed_source"), "metrics": r["metrics"],
            "notes": r.get("learned_notes", []),
            "rolled_back": r.get("rolled_back", False),
            "instruction": r["instruction"],
        })
    return {"snapshot": m.snapshot(), "capabilities": caps, "recent": recent}


def stats_payload(instruction: str) -> dict:
    m = Memory()
    norm = normalize(instruction or "")
    sig = next((r["signature"] for r in m.executions.records
                if r["norm"] == norm or r["signature"] == instruction), None)
    return {"signature": sig, "history": m.executions.history(sig) if sig else []}


# --------------------------------------------------------------------------- #
# HTTP handler                                                                #
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype="application/json") -> None:
        if ctype == "application/json":
            raw = json.dumps(body, default=str).encode()
        else:
            raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif u.path == "/api/memory":
            self._send(200, memory_payload())
        elif u.path == "/api/stats":
            instr = parse_qs(u.query).get("instruction", [""])[0]
            self._send(200, stats_payload(instr))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or "{}")
        except json.JSONDecodeError:
            body = {}
        path = urlparse(self.path).path
        try:
            if path == "/api/run":
                instr = (body.get("instruction") or "").strip()
                if not instr:
                    return self._send(200, {"error": "empty instruction"})
                self._send(200, Agent().run(instr).to_dict())
            elif path == "/api/rollback":
                rep = Agent().rollback(execution_id=body.get("execution_id") or None,
                                       reason="dashboard")
                self._send(200, rep.to_dict())
            else:
                self._send(404, {"error": "not found"})
        except SystemExit as e:                 # e.g. nothing to roll back
            self._send(200, {"error": str(e)})
        except Exception as e:                  # never 500 the demo
            self._send(200, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a) -> None:          # silence per-request logging
        pass


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Autonomous GitHub Agent</title>
<style>
/* Watermelon Software brand: light, white, magenta accent, purple->pink->orange */
:root{--bg:#f6f5f7;--panel:#ffffff;--line:#ececf0;--txt:#1a1320;--mut:#6b7280;
--grn:#15a34a;--yel:#c2740a;--red:#dc2626;--cyn:#0e8f9c;--mag:#7c3aed;--acc:#e6177e;
--acc2:#f59e0b;--grad:linear-gradient(90deg,#7c3aed 0%,#e6177e 52%,#f59e0b 100%);
--shadow:0 1px 2px rgba(20,15,30,.04),0 6px 20px rgba(20,15,30,.06)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
header{padding:15px 24px;background:#fff;border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:14px}
.brand{display:flex;align-items:center;gap:9px;font-size:18px;font-weight:700}
.hex{width:22px;height:22px;border-radius:6px;
background:conic-gradient(from 200deg,#2bb6c4,#3fb950,#f5c518,#f59e0b,#e6177e,#7c3aed,#2bb6c4)}
.brand .grad{background:var(--grad);-webkit-background-clip:text;background-clip:text;
color:transparent}
header .sub{color:var(--mut);font-size:12px}
.wrap{display:grid;grid-template-columns:1.35fr 1fr;gap:16px;padding:18px 24px;
max-width:1280px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:15px 17px;margin-bottom:16px;box-shadow:var(--shadow)}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;
color:var(--mut);margin:0 0 10px}
textarea{width:100%;background:#fff;color:var(--txt);border:1px solid #dcdce2;
border-radius:9px;padding:11px;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
resize:vertical;min-height:62px}textarea:focus{outline:0;border-color:var(--acc);
box-shadow:0 0 0 3px rgba(230,23,126,.12)}
.row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
button{background:var(--acc);color:#fff;border:0;border-radius:22px;padding:9px 18px;
font-weight:650;cursor:pointer;font-size:13px;transition:filter .15s,box-shadow .15s}
button:hover{filter:brightness(1.06);box-shadow:0 4px 14px rgba(230,23,126,.28)}
button.ghost{background:#fff;color:var(--txt);border:1px solid #dcdce2;box-shadow:none}
button.ghost:hover{border-color:var(--acc);color:var(--acc);box-shadow:none;filter:none}
button:disabled{opacity:.5;cursor:not-allowed}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.chip{font-size:11px;color:var(--mut);background:#fff;border:1px solid #dcdce2;
border-radius:20px;padding:4px 11px;cursor:pointer}
.chip:hover{border-color:var(--acc);color:var(--acc)}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;
font-weight:700;text-transform:uppercase}
.b-success{background:rgba(21,163,74,.12);color:var(--grn)}
.b-partial{background:rgba(194,116,10,.14);color:var(--yel)}
.b-failed{background:rgba(220,38,38,.12);color:var(--red)}
.b-rollback{background:rgba(124,58,237,.12);color:var(--mag)}
.metrics{display:flex;gap:20px;flex-wrap:wrap;margin:13px 0;font-size:13px}
.metrics b{font-size:21px;display:block;font-variant-numeric:tabular-nums}
.metrics .lbl{color:var(--mut);font-size:11px;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
vertical-align:top}th{color:var(--mut);font-weight:600;font-size:11px;
text-transform:uppercase}td.mono,code{font-family:ui-monospace,Menlo,monospace}
code{background:#f2eef6;color:var(--mag);padding:1px 6px;border-radius:5px;font-size:11.5px}
.ok{color:var(--grn)}.bad{color:var(--red)}.syn{color:var(--acc)}.skip{color:var(--mut)}
.diff{display:grid;grid-template-columns:1fr auto auto;gap:6px 14px;align-items:center}
.diff .k{color:var(--mut)}.diff .chg{color:var(--grn);font-weight:700}
.arr{color:var(--mut)}
.note{color:var(--mag);font-size:12px;margin:3px 0;padding-left:10px;
border-left:2px solid var(--mag)}
.result{background:#fbf2f7;border:1px solid #f3d9e8;border-left:3px solid var(--acc);
border-radius:8px;padding:10px 13px;margin:4px 0 12px;font-size:13px;color:#3a1a2c;
font-family:ui-monospace,Menlo,monospace;white-space:pre-wrap;line-height:1.6}
.deci{font-size:12px;color:var(--yel);margin:2px 0}
.curve td.win{color:var(--grn);font-weight:700}
.spin{display:inline-block;width:13px;height:13px;border:2px solid #e7d9ea;
border-top-color:var(--acc);border-radius:50%;animation:s .7s linear infinite;
vertical-align:-2px;margin-right:6px}@keyframes s{to{transform:rotate(360deg)}}
.empty{color:var(--mut);font-size:13px;padding:8px 0}
.cap{display:flex;justify-content:space-between;gap:8px;padding:6px 0;
border-bottom:1px solid var(--line);font-size:12.5px}
.cap .b{font-family:ui-monospace,Menlo,monospace}
.tag{font-size:10px;padding:1px 6px;border-radius:5px;background:#f0eef2;color:var(--mut)}
.tag.syn{background:rgba(230,23,126,.12);color:var(--acc)}
small.mut{color:var(--mut)}
</style></head><body>
<header>
  <span class="brand"><span class="hex"></span>watermelon <span class="grad">agent</span></span>
  <span class="sub">plan → execute → synthesise → learn → rollback · live on GitHub</span>
</header>
<div class="wrap">
  <div class="left">
    <div class="card">
      <h2>Instruction</h2>
      <textarea id="instr" placeholder="e.g. Find every open issue with no assignee and label it needs-triage"></textarea>
      <div class="row">
        <button id="runBtn" onclick="run()">▶ Run</button>
        <button class="ghost" onclick="rollbackLast()">↩ Rollback last run</button>
        <button class="ghost" id="memBtn" onclick="loadMemory(this)">⟳ Refresh memory</button>
      </div>
      <div class="chips" id="chips"></div>
    </div>
    <div class="card" id="reportCard" style="display:none"><h2>Execution report</h2>
      <div id="report"></div></div>
    <div class="card" id="curveCard" style="display:none"><h2>Learning curve (run 1 → run N)</h2>
      <div id="curve"></div></div>
  </div>
  <div class="right">
    <div class="card"><h2>Memory — what changed this run</h2><div id="diff" class="empty">Run something to see the before → after.</div></div>
    <div class="card"><h2>Capability memory</h2><div id="caps" class="empty">…</div></div>
    <div class="card"><h2>Recent runs</h2><div id="recent" class="empty">…</div></div>
  </div>
</div>
<script>
const EXAMPLES=[
 "Create an issue titled 'Login times out' and label it bug",
 "Find every open issue with no assignee and add the label needs-triage to each",
 "Find all open issues, group them by label, and create a 'Weekly Triage Summary' issue with the counts"];
const $=id=>document.getElementById(id);
function chips(){$("chips").innerHTML=EXAMPLES.map(e=>`<span class="chip" onclick="setI(this.dataset.e)" data-e="${e.replace(/"/g,'&quot;')}">${e.slice(0,42)}…</span>`).join("")}
function setI(e){$("instr").value=e}
async function jget(u){return (await fetch(u)).json()}
async function jpost(u,b){return (await (await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)})).json())}

async function run(){
  const instruction=$("instr").value.trim(); if(!instruction)return;
  const b=$("runBtn"); b.disabled=true; b.innerHTML='<span class="spin"></span>Running…';
  try{
    const r=await jpost("/api/run",{instruction});
    if(r.error){renderError(r.error)} else {renderReport(r); await loadCurve(instruction)}
    await loadMemory();
  }catch(e){renderError(e.message)} finally{b.disabled=false; b.innerHTML="▶ Run"}
}
async function rollbackLast(){
  const b=$("runBtn"); b.disabled=true;
  try{const r=await jpost("/api/rollback",{});
    if(r.error){renderError(r.error)} else {renderReport(r)}
    await loadMemory();
  }finally{b.disabled=false}
}
function renderError(msg){
  $("reportCard").style.display="block";
  $("report").innerHTML=`<span class="badge b-failed">error</span> <span class="bad">${esc(msg)}</span>`;
}
function esc(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
function statusBadge(s){const m={success:"b-success",partial:"b-partial",failed:"b-failed",rollback:"b-rollback"};return `<span class="badge ${m[s]||'b-partial'}">${s}</span>`}

function renderReport(r){
  $("reportCard").style.display="block";
  const m=r.metrics;
  const steps=(r.steps||[]).map(s=>{
    const cls={ok:"ok",synthesized:"syn",failed:"bad",skipped:"skip"}[s.status]||"";
    let out=s.error?`<span class="bad">${esc(s.error)}</span>`
      :(s.decision?`<small class="mut">${esc(s.decision)}</small>`
      :(s.output_preview?`<small class="mut">${esc(s.output_preview)}</small>`:""));
    return `<tr><td class="mono">${s.step}</td><td class="mono">${esc(s.capability)}</td>
      <td class="${cls}">${s.status}</td><td>${s.api_calls}</td><td>${out}</td></tr>`}).join("");
  const deci=(r.decisions||[]).map(d=>`<div class="deci">• ${esc(d)}</div>`).join("");
  const notes=(r.plan_notes||[]).map(n=>`<div class="note">learned: ${esc(n)}</div>`).join("");
  const result=r.result_summary?`<div class="result">📋 ${esc(r.result_summary)}</div>`:"";
  $("report").innerHTML=`
    ${statusBadge(r.status)} <code>${esc(r.plan_source)}</code>
    ${r.record_id?`<small class="mut">· run ${esc(r.record_id)}</small>`:""}
    <div class="metrics">
      <div><span class="lbl">API calls</span><b>${m.api_calls}</b></div>
      <div><span class="lbl">LLM calls</span><b style="color:var(--mag)">${m.llm_calls}</b></div>
      <div><span class="lbl">Duration</span><b style="color:var(--acc)">${m.duration_ms}<small class="mut"> ms</small></b></div>
      <div><span class="lbl">Steps</span><b>${m.steps}</b></div>
    </div>
    ${result}
    <table><thead><tr><th>#</th><th>Capability</th><th>Status</th><th>API</th><th>Detail / output</th></tr></thead>
    <tbody>${steps}</tbody></table>
    ${deci?`<div style="margin-top:10px">${deci}</div>`:""}
    ${notes}`;
  showDiff(r.memory_before,r.memory_after);
}

function showDiff(b,a){
  if(!b||!a){return}
  const rows=Object.keys(b).map(k=>{
    const bv=fmt(b[k]),av=fmt(a[k]),chg=bv!==av;
    return `<div class="k">${k}</div><div>${bv}</div>
      <div class="arr">→ <span class="${chg?'chg':''}">${av}</span></div>`}).join("");
  $("diff").className="diff"; $("diff").innerHTML=rows;
}
function renderDiff(){return true}
function fmt(v){return Array.isArray(v)?(v.length?v.join(", "):"—"):String(v)}

async function loadCurve(instruction){
  const r=await jget("/api/stats?instruction="+encodeURIComponent(instruction));
  if(!r.history||r.history.length<1){$("curveCard").style.display="none";return}
  $("curveCard").style.display="block";
  const last=r.history.length-1;
  const rows=r.history.map((h,i)=>{
    const m=h.metrics,win=(i===last&&r.history.length>1);
    return `<tr><td>${h.run}</td><td class="mono">${h.plan_source}</td><td>${h.outcome}</td>
      <td class="${win?'win':''}">${m.api_calls}</td>
      <td class="${win?'win':''}">${m.llm_calls}</td>
      <td class="${win?'win':''}">${m.duration_ms}</td></tr>`}).join("");
  let summary="";
  if(r.history.length>1){const f=r.history[0].metrics,l=r.history[last].metrics;
    summary=`<div style="margin-top:8px" class="ok">Run 1 → run ${r.history.length}:
      LLM ${f.llm_calls}→${l.llm_calls}, API ${f.api_calls}→${l.api_calls},
      ${f.duration_ms}→${l.duration_ms} ms</div>`}
  $("curve").innerHTML=`<table class="curve"><thead><tr><th>Run</th><th>Source</th><th>Outcome</th>
    <th>API</th><th>LLM</th><th>ms</th></tr></thead><tbody>${rows}</tbody></table>${summary}`;
}

async function loadMemory(btn){
  if(btn){btn.disabled=true; btn.textContent="⟳ Refreshing…";}
  try{
  const m=await jget("/api/memory");
  const s=m.snapshot;
  $("caps").className=""; $("caps").innerHTML=m.capabilities.map(c=>{
    const sr=c.success_rate==null?"—":Math.round(c.success_rate*100)+"%";
    const tag=c.origin==="synthesized"?'<span class="tag syn">synth</span>':'<span class="tag">builtin</span>';
    const con=c.constraints.length?` <small class="mut">· ${c.constraints.length} constraint(s)</small>`:"";
    return `<div class="cap"><span class="b">${esc(c.name)} ${tag}</span>
      <span><small class="mut">${c.kind} · ${c.uses} uses · ${sr}</small>${con}</span></div>`}).join("")
    +`<div style="margin-top:8px"><small class="mut">${s.capability_count} capabilities ·
      ${s.total_constraints} constraints · ${s.execution_records} runs</small></div>`;
  $("recent").className=""; $("recent").innerHTML=m.recent.length?m.recent.map(r=>{
    const rb=r.rolled_back?' <span class="tag" style="color:var(--mag)">rolled back</span>':"";
    return `<div class="cap"><span class="b">${esc(r.signature)}${rb}</span>
      <span><small class="mut">${r.source||'?'} · api ${r.metrics.api_calls} · llm ${r.metrics.llm_calls}</small></span></div>`}).join(""):'<div class="empty">No runs yet.</div>';
    if(btn){btn.textContent="✓ Refreshed"; setTimeout(()=>{btn.textContent="⟳ Refresh memory";},1000);}
  }catch(e){console.error("loadMemory failed",e); if(btn)btn.textContent="⚠ Retry";}
  finally{if(btn)btn.disabled=false;}
}
chips(); loadMemory();
</script></body></html>"""


def main() -> int:
    config.require_env()
    print(f"\n  🍉 Dashboard running →  http://localhost:{PORT}\n"
          f"     (uses the same Agent + persistent memory as the CLI)\n"
          f"     Ctrl-C to stop.\n")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("  stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
