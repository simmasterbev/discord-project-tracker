"""Small read-only web view for the Discord Project Progress Tracker."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import db


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("TRACKER_DB_PATH", ROOT / "tracker.db"))


def guild_id() -> int:
    value = os.environ.get("TRACKER_GUILD_ID", "").strip()
    if not value.isdigit():
        raise RuntimeError("Set TRACKER_GUILD_ID to the Discord server ID before starting the dashboard.")
    return int(value)


def public_state() -> dict:
    """Return public tracker data only; private milestone text never leaves SQLite."""
    gid = guild_id()
    nodes = [node for node in db.tree_state(gid) if not node["private"]]
    public_keys = {node["key"] for node in nodes}
    for node in nodes:
        node["prereqs"] = [key for key in node["prereqs"] if key in public_keys]
        node["blocked_by"] = [key for key in node["blocked_by"] if key in public_keys]
        node.pop("private", None)

    trees = []
    for tree in db.list_trees(gid):
        members = sorted(db.tree_members(tree["id"]) & public_keys)
        trees.append({"key": tree["key"], "name": tree["name"], "members": members})

    projects = []
    for project in db.list_projects(gid):
        projects.append({
            "name": project["name"],
            "description": project["description"],
            "progress": db.progress(project["id"]),
        })
    return {"trees": trees, "milestones": nodes, "projects": projects}


PAGE = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Tracker</title>
<style>
:root{color-scheme:dark;--bg:#101216;--panel:#161b24;--line:#283548;--text:#f3f5f8;--muted:#9ba7b8;--blue:#1687f9;--green:#32d583;--gold:#f3b63d;--red:#f46565}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 88% -10%,#143154 0,transparent 35%),var(--bg);color:var(--text);font:15px system-ui,-apple-system,Segoe UI,sans-serif}.top{padding:18px max(5vw,24px);border-bottom:1px solid var(--line);background:#0d0f13;display:flex;gap:16px;align-items:center}.mark{width:11px;height:31px;border-radius:8px;background:var(--blue);box-shadow:0 0 24px #1687f988}.top h1{font-size:20px;margin:0}.top p{margin:2px 0 0;color:var(--muted);font-size:13px}main{max-width:1400px;margin:auto;padding:34px max(5vw,24px) 60px}h2{font-size:24px;margin:0 0 8px}section{margin-top:38px}.sub{margin:0 0 18px;color:var(--muted)}select{background:#101721;color:var(--text);border:1px solid var(--line);padding:9px 12px;border-radius:7px;font:inherit}.tree-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px;background:#0d121a;min-height:280px}.graph{min-width:740px;min-height:260px;position:relative;padding:30px}.edge{stroke:#536379;stroke-width:2;fill:none;stroke-dasharray:5 5}.node{position:absolute;width:205px;min-height:124px;padding:14px;border:1px solid var(--line);border-radius:10px;background:#161e2a;box-shadow:0 8px 20px #0005}.node.complete{border-color:var(--green);background:#103227}.node.available,.node.active{border-color:var(--blue);background:#102b46}.node.pending{border-color:var(--gold);background:#342911}.node.locked{opacity:.68}.node .state{font-size:10px;font-weight:800;letter-spacing:.07em;color:var(--muted)}.node .name{font-weight:750;margin:10px 0 12px;line-height:1.2}.bar{height:7px;border-radius:9px;background:#283548;overflow:hidden}.bar i{display:block;height:100%;background:var(--blue)}.complete .bar i{background:var(--green)}.pending .bar i{background:var(--gold)}.pct{color:var(--muted);font-size:12px;margin-top:7px}.empty{padding:54px;color:var(--muted);text-align:center}.projects{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}.project{border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:17px}.project h3{margin:0 0 7px;font-size:17px}.project p{color:var(--muted);min-height:36px;margin:0 0 15px}.project small{color:var(--muted);display:block;margin-top:8px}.footer{color:var(--muted);font-size:12px;margin-top:38px}@media(max-width:600px){main{padding-top:24px}.top{padding:14px 18px}}
</style>
<body><header class="top"><div class="mark"></div><div><h1>Project Tracker</h1><p>Live progress from Discord</p></div></header><main>
<section><h2>Tech tree</h2><p class="sub">Completed work unlocks the next available milestone.</p><select id="tree" aria-label="Choose a tech tree"></select><div class="tree-wrap"><div id="graph" class="graph"></div></div></section>
<section><h2>Projects</h2><p class="sub">The work currently moving the tree forward.</p><div id="projects" class="projects"></div></section><p class="footer" id="updated"></p></main>
<script>
const el=id=>document.getElementById(id), esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function make(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}
function draw(data,key){const graph=el('graph');graph.replaceChildren();let nodes=data.milestones.filter(n=>!key||data.trees.find(t=>t.key===key).members.includes(n.key));if(!nodes.length){graph.append(make('div','empty','No public milestones in this tree yet.'));return}const map=Object.fromEntries(nodes.map(n=>[n.key,n]));const depth=(n,seen=new Set)=>seen.has(n.key)?0:Math.max(0,...n.prereqs.filter(k=>map[k]).map(k=>depth(map[k],new Set([...seen,n.key]))+1));const cols={};nodes.forEach(n=>(cols[depth(n)]??=[]).push(n));const max=Math.max(...Object.values(cols).map(x=>x.length));graph.style.height=Math.max(280,max*155+60)+'px';graph.style.minWidth=Math.max(740,Object.keys(cols).length*250+80)+'px';const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('width','100%');svg.setAttribute('height','100%');svg.style.position='absolute';svg.style.inset='0';graph.append(svg);const pos={};Object.entries(cols).forEach(([col,list])=>list.forEach((n,row)=>pos[n.key]={x:30+Number(col)*250,y:30+row*155}));nodes.forEach(n=>n.prereqs.filter(k=>pos[k]).forEach(k=>{const p=pos[k],q=pos[n.key],line=document.createElementNS('http://www.w3.org/2000/svg','path');line.setAttribute('d',`M ${p.x+205} ${p.y+62} L ${q.x} ${q.y+62}`);line.setAttribute('class','edge');svg.append(line)}));nodes.forEach(n=>{const card=make('article','node '+n.state);card.style.left=pos[n.key].x+'px';card.style.top=pos[n.key].y+'px';card.append(make('div','state',n.state.replace('_',' ').toUpperCase()));card.append(make('div','name',n.name));const bar=make('div','bar'),fill=make('i');fill.style.width=n.pct+'%';bar.append(fill);card.append(bar);card.append(make('div','pct',n.pct+'% complete'));graph.append(card)})}
function projects(items){const wrap=el('projects');wrap.replaceChildren();if(!items.length){wrap.append(make('div','empty','No active projects yet.'));return}items.forEach(p=>{const card=make('article','project');card.append(make('h3','',p.name));card.append(make('p','',p.description||'No description yet.'));const bar=make('div','bar'),fill=make('i');fill.style.width=p.progress.pct+'%';bar.append(fill);card.append(bar);card.append(make('small','',`${p.progress.pct}% · ${p.progress.done}/${p.progress.count} tasks done`));wrap.append(card)})}
fetch('/api/state',{cache:'no-store'}).then(r=>r.json()).then(data=>{const select=el('tree');select.append(new Option('All milestones',''));data.trees.forEach(t=>select.append(new Option(t.name,t.key)));select.onchange=()=>draw(data,select.value);draw(data,'');projects(data.projects);el('updated').textContent='Updated '+new Date().toLocaleString()}).catch(()=>el('graph').append(make('div','empty','The dashboard cannot reach tracker data right now.')));
</script></body></html>'''


class Dashboard(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def send(self, status: int, content_type: str, body: str) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send(200, "text/plain", "ok\n")
        elif self.path == "/api/state":
            try:
                self.send(200, "application/json", json.dumps(public_state()))
            except Exception as exc:
                self.send(503, "application/json", json.dumps({"error": str(exc)}))
        elif self.path == "/" or self.path == "/index.html":
            self.send(200, "text/html", PAGE)
        else:
            self.send(404, "text/plain", "Not found\n")


def main() -> None:
    host = os.environ.get("TRACKER_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("TRACKER_DASHBOARD_PORT", "8080"))
    guild_id()  # fail clearly at startup rather than serving an empty site
    db.connect(DB_PATH)
    print(f"Dashboard listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Dashboard).serve_forever()


if __name__ == "__main__":
    main()
