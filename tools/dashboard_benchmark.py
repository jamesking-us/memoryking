#!/usr/bin/env python3
"""Loopback-only synthetic graph renderer benchmark; no database, models or credentials."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/memoryking/adapters/web"
HTML = b"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Synthetic graph rendering benchmark</title><link rel="stylesheet" href="/app.css"><body><main style="margin:0;padding:24px"><h1>Renderer-only synthetic fixture</h1><p>No corpus, service latency, authority or capacity claim. Actual packaged graph and worker.</p><button id="overview">Run 20 overview samples</button><button id="limit">Run 20 limit samples</button><button id="fallback">Worker unavailable fallback</button><pre id="metrics" aria-live="polite"></pre><div id="graph"></div></main><script type="module" src="/benchmark.js"></script></body></html>"""
JS = b"""
import {AtlasView} from '/graph.js';
const host=document.querySelector('#graph'), metrics=document.querySelector('#metrics');
let view;
function data(n,m){return {nodes:Array.from({length:n},(_,i)=>({id:'node'+i,type:i%3?'claim':'observation',label:'Synthetic node '+i})),edges:Array.from({length:m},(_,i)=>({id:'edge'+i,source:'node'+(i%n),target:'node'+((i*17+11)%n),type:'evidence',provenance:{fixture:true}})),coverage:'synthetic renderer fixture only'};}
async function run(n,m,fallback=false){
 document.querySelectorAll('button').forEach(b=>b.disabled=true); const samples=[];
 for(let i=0;i<(fallback?1:20);i++){
  view?.clear();host.replaceChildren();view=new AtlasView(host,()=>{});
  const start=performance.now();view.set(data(n,m));
  await new Promise(resolve=>{const poll=()=>{const t=host.querySelector('[role=status]').textContent;if(!t.includes('Settling'))resolve();else setTimeout(poll,5)};poll()});
  await new Promise(requestAnimationFrame);
  const status=host.querySelector('[role=status]').textContent;
  samples.push({ready_ms:Math.round((performance.now()-start)*10)/10,draw_ms:Number(host.dataset.drawMs),layout_ms:Number(/layout ([0-9.]+)/.exec(status)?.[1]||0),status});
  metrics.textContent=JSON.stringify({nodes:n,edges:m,fixture:'renderer_only',samples},null,2);
 }
 const p95=k=>samples.map(s=>s[k]).sort((a,b)=>a-b)[Math.ceil(samples.length*.95)-1];
 metrics.textContent=JSON.stringify({nodes:n,edges:m,fixture:'renderer_only',p95:{ready_ms:p95('ready_ms'),draw_ms:p95('draw_ms'),layout_ms:p95('layout_ms')},samples},null,2);
 document.querySelectorAll('button').forEach(b=>b.disabled=false);
}
document.querySelector('#overview').onclick=()=>run(200,400);
document.querySelector('#limit').onclick=()=>run(500,1500);
document.querySelector('#fallback').onclick=()=>{const original=window.Worker;window.Worker=class {constructor(){throw Error('fixture denied worker')}};run(200,400,true).finally(()=>window.Worker=original)};
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            raw, mime = HTML, "text/html"
        elif self.path == "/benchmark.js":
            raw, mime = JS, "text/javascript"
        elif self.path in {"/app.css", "/graph.js", "/graph-worker.js", "/graph-layout.js"}:
            raw = (ASSETS / self.path[1:]).read_bytes()
            mime = "text/css" if self.path.endswith("css") else "text/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}), flush=True)
        server.serve_forever()
