"""Read-only web dashboard for the Discord Project Progress Tracker."""

from __future__ import annotations

import csv
import io
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
    """Return dashboard data without private milestone text or Discord user IDs."""
    gid = guild_id()
    nodes = [dict(node) for node in db.tree_state(gid) if not node["private"]]
    public_keys = {node["key"] for node in nodes}
    for node in nodes:
        node["prereqs"] = [key for key in node["prereqs"] if key in public_keys]
        node["blocked_by"] = [key for key in node["blocked_by"] if key in public_keys]
        node["projects"] = [project["name"] for project in db.milestone_projects(node["id"])]
        for field in ("id", "people", "completed_by", "private"):
            node.pop(field, None)

    trees = []
    for tree in db.list_trees(gid):
        members = sorted(db.tree_members(tree["id"]) & public_keys)
        trees.append({"key": tree["key"], "name": tree["name"], "members": members})

    projects, all_tasks = [], []
    for project in db.list_projects(gid):
        tasks = [{
            "title": task["title"], "status": task["status"], "due": task["due_date"],
            "weight": task["weight"], "assigned": bool(task["assignee_id"]),
        } for task in db.list_tasks(project["id"])]
        all_tasks.extend({**task, "project": project["name"]} for task in tasks)
        projects.append({
            "name": project["name"], "description": project["description"],
            "progress": db.progress(project["id"]), "tasks": tasks,
        })

    milestone_states = {state: sum(node["state"] == state for node in nodes)
                        for state in ("complete", "active", "available", "pending", "locked")}
    task_states = {state: sum(task["status"] == state for task in all_tasks)
                   for state in ("done", "doing", "blocked", "todo")}
    return {
        "trees": trees, "milestones": nodes, "projects": projects, "tasks": all_tasks,
        "summary": {
            "project_count": len(projects), "milestones": milestone_states, "tasks": task_states,
        },
    }


def csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerows(rows)
    return out.getvalue().encode()


def project_report(state: dict) -> bytes:
    return csv_bytes(
        ["Project", "Description", "Progress", "Tasks", "Done", "Doing", "Blocked", "Not started"],
        [[p["name"], p["description"], f'{p["progress"]["pct"]}%', p["progress"]["count"],
          p["progress"]["done"], p["progress"]["doing"], p["progress"]["blocked"],
          p["progress"]["todo"]] for p in state["projects"]],
    )


def task_report(state: dict) -> bytes:
    return csv_bytes(
        ["Project", "Task", "Status", "Due date", "Weight", "Assigned"],
        [[t["project"], t["title"], t["status"], t["due"] or "", t["weight"],
          "yes" if t["assigned"] else "no"] for t in state["tasks"]],
    )


PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Tracker</title>
<style>
:root{color-scheme:dark;--bg:#0c1017;--panel:#121b28;--line:#29405c;--text:#eff6ff;--muted:#9db0c6;--blue:#168ff7;--cyan:#37d7e8;--green:#38d68a;--gold:#f5c248;--red:#fb7185}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 84% -8%,#164d81 0,transparent 31%),linear-gradient(145deg,#0e1622,var(--bg) 42%);color:var(--text);font:15px system-ui,-apple-system,Segoe UI,sans-serif}.top{position:sticky;top:0;z-index:2;padding:14px max(5vw,24px);border-bottom:1px solid #ffffff14;background:#0a0e15ef;backdrop-filter:blur(14px);display:flex;gap:14px;align-items:center}.mark{width:12px;height:34px;border-radius:7px;background:linear-gradient(var(--cyan),var(--blue));box-shadow:0 0 26px #168ff799}.brand h1{font-size:18px;margin:0}.brand p{margin:2px 0 0;color:var(--muted);font-size:12px}.nav{margin-left:auto;display:flex;gap:16px}.nav a,.download{color:var(--muted);text-decoration:none;font-size:13px}.nav a:hover{color:white}main{max-width:1440px;margin:auto;padding:36px max(5vw,24px) 72px}section{margin-top:40px}.eyebrow{color:var(--cyan);font-size:12px;font-weight:800;letter-spacing:.13em;text-transform:uppercase}.hero h2,h2{font-size:30px;margin:6px 0}.sub{margin:0;color:var(--muted);max-width:680px;line-height:1.55}.stats{display:grid;grid-template-columns:repeat(4,minmax(145px,1fr));gap:12px;margin-top:24px}.stat,.panel,.project,.tool{border:1px solid var(--line);background:linear-gradient(145deg,#162235cc,#101925cc);border-radius:13px;box-shadow:0 16px 34px #0003}.stat{padding:16px}.stat strong{display:block;font-size:26px;margin-top:7px}.stat span,.label{font-size:12px;color:var(--muted)}.focus{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}.panel{padding:19px}.panel h3{margin:0 0 13px;font-size:16px}.focus-list{display:grid;gap:9px}.focus-item{padding:11px 0;border-top:1px solid #ffffff12}.focus-item:first-child{border-top:0;padding-top:0}.focus-item b{display:block;margin-top:4px}.focus-item small{color:var(--muted)}.pill{display:inline-block;border-radius:999px;padding:3px 8px;font-size:10px;font-weight:800;background:#1f3047;color:#cfe7ff;text-transform:uppercase}.pill.blocked{background:#492133;color:#ffc1ca}.pill.available{background:#163b45;color:#9ff4ff}.pill.active{background:#173a64;color:#b7d8ff}.toolbar{display:flex;gap:10px;align-items:center;margin:16px 0}select,input{background:#0d1623;color:var(--text);border:1px solid var(--line);padding:10px 12px;border-radius:8px;font:inherit}input{margin-left:auto;width:min(300px,100%)}.tree-wrap{overflow:auto;border:1px solid var(--line);border-radius:13px;background:linear-gradient(#0c1420ee,#0a111bdc),linear-gradient(90deg,#ffffff08 1px,transparent 1px),linear-gradient(#ffffff08 1px,transparent 1px);background-size:auto,32px 32px,32px 32px;min-height:320px}.graph{min-width:800px;min-height:300px;position:relative}.edge{stroke:#5f7896;stroke-width:2;fill:none;stroke-dasharray:5 5}.node{position:absolute;width:226px;min-height:158px;padding:14px;border:1px solid #3b506a;border-radius:11px;background:#152132;box-shadow:0 9px 22px #0006}.node.complete{border-color:var(--green);background:#10352d}.node.available,.node.active{border-color:var(--cyan);background:#113543}.node.pending{border-color:var(--gold);background:#3a2e15}.node.locked{opacity:.69}.node-top{display:flex;justify-content:space-between;gap:8px}.node .state{font-size:10px;font-weight:800;letter-spacing:.07em;color:var(--muted)}.node .xp{font-size:11px;color:#d7e8fa}.node .name{font-weight:780;margin:8px 0 4px;line-height:1.2}.node .desc{font-size:12px;color:var(--muted);line-height:1.35;height:32px;overflow:hidden}.node .meta{font-size:11px;color:#b7cae0;margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.bar{height:7px;border-radius:9px;background:#273c56;overflow:hidden}.bar i{display:block;height:100%;background:var(--blue)}.complete .bar i{background:var(--green)}.pending .bar i{background:var(--gold)}.pct{color:var(--muted);font-size:11px;margin-top:6px}.projects,.tools{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}.project,.tool{padding:18px}.project header{display:flex;justify-content:space-between;gap:12px}.project h3,.tool h3{margin:0;font-size:17px}.project p,.tool p{color:var(--muted);min-height:42px;line-height:1.45;margin:10px 0 15px}.tool ol{padding-left:20px;color:var(--muted);line-height:1.65}.tool code{color:#d8edff}.task-summary{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.chip{font-size:11px;border-radius:7px;padding:4px 7px;background:#203046;color:#c6d8eb}.chip.doing{background:#173d6b}.chip.blocked{background:#4b2435;color:#ffc1cb}.chip.done{background:#143d2e;color:#adf1cf}.reports{display:flex;flex-wrap:wrap;gap:10px}.download{display:inline-flex;align-items:center;border:1px solid var(--line);background:#142238;border-radius:8px;padding:10px 13px;color:#dcecff}.download:hover{border-color:var(--blue);background:#193457}.empty{padding:48px;color:var(--muted);text-align:center}.footer{color:var(--muted);font-size:12px;margin-top:42px}@media(max-width:780px){.nav{display:none}.focus{display:block}.stats{grid-template-columns:repeat(2,1fr)}.focus .panel+.panel{margin-top:14px}input{margin-left:0}.toolbar{align-items:stretch;flex-wrap:wrap}}@media(max-width:420px){main{padding:24px 16px}.top{padding:13px 16px}.stats{gap:8px}.hero h2{font-size:27px}}
</style>
<body><header class="top"><div class="mark"></div><div class="brand"><h1>Project Tracker</h1><p>Live progress from Discord</p></div><nav class="nav"><a href="#overview">Overview</a><a href="#tree-section">Tech tree</a><a href="#projects-section">Projects</a><a href="#reports">Reports</a></nav></header><main>
<section id="overview"><div class="eyebrow">Command center</div><div class="hero"><h2>Where the work is moving next.</h2><p class="sub">A read-only view of your Discord tracker: what is ready, what is blocked, and how each project moves the roadmap forward.</p></div><div id="stats" class="stats"></div></section>
<section><div class="focus"><article class="panel"><h3>Ready to move</h3><div id="ready" class="focus-list"></div></article><article class="panel"><h3>Needs attention</h3><div id="attention" class="focus-list"></div></article></div></section>
<section id="tree-section"><div class="eyebrow">Roadmap</div><h2>Tech tree</h2><p class="sub">Each connection is a prerequisite. Complete work opens the next path.</p><div class="toolbar"><select id="tree" aria-label="Choose a tech tree"></select></div><div class="tree-wrap"><div id="graph" class="graph"></div></div></section>
<section id="projects-section"><div class="eyebrow">Execution</div><h2>Projects</h2><p class="sub">Task-level progress, including work in progress and blockers.</p><div class="toolbar"><input id="search" type="search" placeholder="Filter projects or tasks" aria-label="Filter projects"></div><div id="projects" class="projects"></div></section>
<section id="tools"><div class="eyebrow">Build plans</div><h2>Planner help</h2><p class="sub">These helpers run only in your browser. Nothing changes in Discord until you upload the file back to the bot.</p><div class="tools"><article class="tool"><h3>Build a roadmap</h3><p>Use this when you want to plan a whole tree, including projects and tasks, before putting it in Discord.</p><ol><li>Open the planner and add milestones. Add projects/tasks only when you want the bot to track the work.</li><li>Click <code>Download plan</code>.</li><li>In Discord run <code>/tree import</code>, then attach the downloaded file.</li></ol><a class="download" href="/tools/planner.html" target="_blank" rel="noopener">Open tree planner</a></article><article class="tool"><h3>Edit server settings</h3><p>Use this only for leadership settings: roles, permissions, groups, regions, teams, and XP levels.</p><ol><li>In Discord run <code>/config export</code> and download the attached file.</li><li>Open the settings editor and load that <code>config.json</code>.</li><li>Download it again, then run <code>/config import</code> in Discord and attach it.</li></ol><a class="download" href="/tools/config_panel.html" target="_blank" rel="noopener">Open settings editor</a></article></div></section>
<section id="reports"><div class="eyebrow">Download</div><h2>Reports</h2><p class="sub">Download a shareable project summary, task list, or the public dashboard data.</p><div class="reports"><a class="download" href="/reports/projects.csv">Download project report (CSV)</a><a class="download" href="/reports/tasks.csv">Download task report (CSV)</a><a class="download" href="/reports/summary.json">Download public snapshot (JSON)</a></div></section><p class="footer" id="updated"></p></main>
<script>
const el=id=>document.getElementById(id);function make(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}function bar(pct){const b=make('div','bar'),i=make('i');i.style.width=pct+'%';b.append(i);return b}function item(title,detail,kind){const a=make('div','focus-item');a.append(make('span','pill '+kind,kind));a.append(make('b','',title));a.append(make('small','',detail));return a}
function draw(data,key){const graph=el('graph');graph.replaceChildren();const tree=data.trees.find(t=>t.key===key);const nodes=data.milestones.filter(n=>!key||tree.members.includes(n.key));if(!nodes.length){graph.append(make('div','empty','No public milestones in this tree yet.'));return}const map=Object.fromEntries(nodes.map(n=>[n.key,n]));const depth=(n,seen=new Set)=>seen.has(n.key)?0:Math.max(0,...n.prereqs.filter(k=>map[k]).map(k=>depth(map[k],new Set([...seen,n.key]))+1));const cols={};nodes.forEach(n=>(cols[depth(n)]??=[]).push(n));const max=Math.max(...Object.values(cols).map(x=>x.length));graph.style.height=Math.max(320,max*190+60)+'px';graph.style.minWidth=Math.max(800,Object.keys(cols).length*270+80)+'px';const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('width','100%');svg.setAttribute('height','100%');svg.style.position='absolute';svg.style.inset='0';graph.append(svg);const pos={};Object.entries(cols).forEach(([col,list])=>list.forEach((n,row)=>pos[n.key]={x:30+Number(col)*270,y:30+row*190}));nodes.forEach(n=>n.prereqs.filter(k=>pos[k]).forEach(k=>{const p=pos[k],q=pos[n.key],line=document.createElementNS('http://www.w3.org/2000/svg','path');line.setAttribute('d',`M ${p.x+226} ${p.y+78} L ${q.x} ${q.y+78}`);line.setAttribute('class','edge');svg.append(line)}));nodes.forEach(n=>{const card=make('article','node '+n.state);card.style.left=pos[n.key].x+'px';card.style.top=pos[n.key].y+'px';const top=make('div','node-top');top.append(make('div','state',n.state.replace('_',' ').toUpperCase()));top.append(make('div','xp',n.xp+' XP'));card.append(top);card.append(make('div','name',n.name));card.append(make('div','desc',n.description||'No description yet.'));card.append(bar(n.pct));card.append(make('div','pct',n.pct+'% complete · '+n.remaining+' task(s) remaining'));card.append(make('div','meta',n.projects.length?'Projects: '+n.projects.join(', '):(n.unlocks||'No linked project yet')));graph.append(card)})}
function projects(items,query=''){const wrap=el('projects');wrap.replaceChildren();const shown=items.filter(p=>(p.name+' '+p.description+' '+p.tasks.map(t=>t.title).join(' ')).toLowerCase().includes(query.toLowerCase()));if(!shown.length){wrap.append(make('div','empty','No projects match that filter.'));return}shown.forEach(p=>{const card=make('article','project'),head=make('header');head.append(make('h3','',p.name));head.append(make('span','pill',p.progress.pct+'%'));card.append(head);card.append(make('p','',p.description||'No description yet.'));card.append(bar(p.progress.pct));const summary=make('div','task-summary');[['doing',p.progress.doing],['blocked',p.progress.blocked],['todo',p.progress.todo],['done',p.progress.done]].forEach(([status,count])=>{if(count)summary.append(make('span','chip '+status,`${count} ${status}`))});card.append(summary);const next=p.tasks.find(t=>t.status==='blocked')||p.tasks.find(t=>t.status==='doing')||p.tasks.find(t=>t.status==='todo');if(next)card.append(make('p','label','Next: '+next.title+(next.due?' · due '+next.due:'')));wrap.append(card)})}
function overview(data){const s=data.summary,ready=data.milestones.filter(n=>n.state==='available'||n.state==='active'),blocked=data.tasks.filter(t=>t.status==='blocked'),cards=[['Projects',s.project_count],['Milestones',data.milestones.length],['Ready now',ready.length],['Blocked tasks',s.tasks.blocked]],stats=el('stats');cards.forEach(([name,value])=>{const c=make('article','stat');c.append(make('span','',name));c.append(make('strong','',value));stats.append(c)});const readyWrap=el('ready'),attention=el('attention');if(ready.length)ready.slice(0,5).forEach(n=>readyWrap.append(item(n.name,n.projects.length?'Project: '+n.projects.join(', '):n.description||'Ready to start',n.state)));else readyWrap.append(make('div','label','Nothing is ready yet.'));if(blocked.length)blocked.slice(0,5).forEach(t=>attention.append(item(t.title,t.project+(t.due?' · due '+t.due:''),'blocked')));else attention.append(make('div','label','No blocked tasks right now.'))}
fetch('/api/state',{cache:'no-store'}).then(r=>r.json()).then(data=>{const select=el('tree');select.append(new Option('All milestones',''));data.trees.forEach(t=>select.append(new Option(t.name,t.key)));select.onchange=()=>draw(data,select.value);draw(data,'');overview(data);projects(data.projects);el('search').oninput=e=>projects(data.projects,e.target.value);el('updated').textContent='Last refreshed '+new Date().toLocaleString()+' · Refresh this page to pull new Discord changes.'}).catch(()=>el('graph').append(make('div','empty','The dashboard cannot reach tracker data right now.')));
</script></body></html>"""


class Dashboard(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def send(self, status: int, content_type: str, body: str) -> None:
        self.send_bytes(status, content_type, body.encode())

    def send_bytes(self, status: int, content_type: str, body: bytes, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self.send(200, "text/plain", "ok\n")
        elif path == "/api/state":
            try:
                self.send(200, "application/json", json.dumps(public_state()))
            except Exception as exc:
                self.send(503, "application/json", json.dumps({"error": str(exc)}))
        elif path.startswith("/reports/"):
            try:
                state = public_state()
                if path == "/reports/projects.csv":
                    self.send_bytes(200, "text/csv", project_report(state), "project-tracker-projects.csv")
                elif path == "/reports/tasks.csv":
                    self.send_bytes(200, "text/csv", task_report(state), "project-tracker-tasks.csv")
                elif path == "/reports/summary.json":
                    self.send_bytes(200, "application/json", json.dumps(state, indent=2).encode(), "project-tracker-summary.json")
                else:
                    self.send(404, "text/plain", "Not found\n")
            except Exception as exc:
                self.send(503, "text/plain", f"Could not build report: {exc}\n")
        elif path == "/tools/planner.html":
            self.send_bytes(200, "text/html", (ROOT / "planner.html").read_bytes())
        elif path == "/tools/config_panel.html":
            self.send_bytes(200, "text/html", (ROOT / "config_panel.html").read_bytes())
        elif path in ("/", "/index.html"):
            self.send(200, "text/html", PAGE)
        else:
            self.send(404, "text/plain", "Not found\n")


def main() -> None:
    host = os.environ.get("TRACKER_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("TRACKER_DASHBOARD_PORT", "8080"))
    guild_id()
    db.connect(DB_PATH)
    print(f"Dashboard listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Dashboard).serve_forever()


if __name__ == "__main__":
    main()
