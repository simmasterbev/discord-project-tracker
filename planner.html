<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tree planner</title>
<style>
  :root{
    --ground:#0e1116; --grid:#161b22; --surface:#131820; --line:#252c38;
    --ink:#e6edf3; --muted:#8b949e;
    --locked:#4b5563; --ready:#f0b429; --done:#22c55e; --signoff:#a855f7;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
  }
  *{box-sizing:border-box}
  body{
    margin:0;padding:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
    background-image:linear-gradient(var(--grid) 1px,transparent 1px),
                     linear-gradient(90deg,var(--grid) 1px,transparent 1px);
    background-size:34px 34px;
  }
  header{padding:28px 24px 18px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:22px;letter-spacing:-.01em}
  header p{margin:6px 0 0;color:var(--muted);font-size:14px;max-width:62ch}
  .wrap{display:grid;grid-template-columns:minmax(340px,440px) 1fr;gap:0;align-items:start}
  @media(max-width:900px){.wrap{grid-template-columns:1fr}}
  .pane{padding:20px 24px}
  .pane+.pane{border-left:1px solid var(--line)}
  @media(max-width:900px){.pane+.pane{border-left:0;border-top:1px solid var(--line)}}

  label{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.09em;
        text-transform:uppercase;color:var(--muted);margin:0 0 5px}
  input[type=text],textarea,input[type=number]{
    width:100%;background:var(--ground);border:1px solid var(--line);border-radius:7px;
    color:var(--ink);padding:8px 10px;font:inherit;font-size:14px}
  textarea{resize:vertical;min-height:52px}
  input:focus,textarea:focus{outline:2px solid var(--ready);outline-offset:1px;border-color:transparent}
  .field{margin-bottom:12px}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}

  .card{background:var(--surface);border:1px solid var(--line);border-radius:11px;
        padding:15px;margin-bottom:14px}
  .card h3{margin:0 0 12px;font-size:13px;font-family:var(--mono);color:var(--muted);
           display:flex;justify-content:space-between;align-items:center;font-weight:500}
  button{font:inherit;cursor:pointer;border-radius:7px;border:1px solid var(--line);
         background:var(--surface);color:var(--ink);padding:8px 13px;font-size:14px}
  button:hover{border-color:var(--muted)}
  button:focus-visible{outline:2px solid var(--ready);outline-offset:2px}
  .primary{background:var(--ready);color:#0e1116;border-color:var(--ready);font-weight:600}
  .ghost{background:none;border:none;color:var(--muted);padding:2px 6px;font-size:12px}
  .ghost:hover{color:var(--ink)}

  .chips{display:flex;flex-wrap:wrap;gap:6px}
  .chip{font-family:var(--mono);font-size:11px;padding:4px 9px;border-radius:20px;
        border:1px solid var(--line);background:var(--ground);color:var(--muted);cursor:pointer}
  .chip[aria-pressed=true]{border-color:var(--ready);color:var(--ready);background:#2a2008}
  .chips .empty{font-size:12px;color:var(--muted);font-style:italic}

  .toggle{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}

  /* preview */
  .board{display:flex;flex-wrap:wrap;gap:14px}
  .tier{min-width:200px}
  .tier h4{font-family:var(--mono);font-size:10px;letter-spacing:.09em;text-transform:uppercase;
           color:var(--muted);margin:0 0 9px;font-weight:500}
  .node{background:var(--surface);border:1px solid var(--locked);border-left-width:3px;
        border-radius:9px;padding:10px 12px;margin-bottom:10px;width:212px}
  .node.ready{border-color:var(--ready)}
  .node.sign{border-color:var(--signoff)}
  .node .tag{font-family:var(--mono);font-size:9px;letter-spacing:.08em;color:var(--muted)}
  .node .nm{font-size:14px;font-weight:600;margin:3px 0}
  .node .dt{font-size:11px;color:var(--muted);line-height:1.35}
  .node .req{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:6px}
  .warn{color:var(--signoff)}
  .out{margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
  .hint{font-size:12px;color:var(--muted);line-height:1.5;margin:10px 0 0}
  code{font-family:var(--mono);font-size:12px;background:var(--surface);padding:2px 5px;border-radius:4px}
</style>
</head>
<body>
<header>
  <h1>Tree planner</h1>
  <p>Lay out your milestones here, then download a spreadsheet file and hand it to
     <code>seed.py</code>. Nothing is sent anywhere — this page runs entirely in your browser.</p>
</header>

<div class="wrap">
  <div class="pane">
    <div class="field">
      <label for="treename">Tree name</label>
      <input type="text" id="treename" value="Candidate forum" oninput="render()">
    </div>
    <div id="editor"></div>
    <button onclick="addNode()">+ Add milestone</button>
  </div>

  <div class="pane">
    <h4 style="font-family:var(--mono);font-size:10px;letter-spacing:.09em;
               text-transform:uppercase;color:var(--muted);margin:0 0 12px;font-weight:500">
      Preview</h4>
    <div class="board" id="board"></div>
    <div class="out">
      <button class="primary" onclick="download()">Download spreadsheet</button>
      <button onclick="copyCsv()">Copy instead</button>
      <p class="hint">Then run:<br>
        <code>python seed.py your-file.csv --guild YOUR_SERVER_ID</code><br>
        Re-running is safe — it updates rather than duplicating, so you can edit
        the plan here and load it again.</p>
    </div>
  </div>
</div>

<script>
let nodes = [
  {name:"Venue booked", desc:"Call three halls, compare quotes, sign, pay the deposit",
   unlocks:"the date becomes announceable", req:[], xp:150, auto:true},
  {name:"Panel confirmed", desc:"Four candidates agree in writing",
   unlocks:"promotion can start", req:["Venue booked"], xp:250, auto:true}
];

const esc = s => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function addNode(){
  nodes.push({name:"", desc:"", unlocks:"", req:[], xp:100, auto:true});
  render();
  setTimeout(()=>document.querySelector(`#n${nodes.length-1}-name`)?.focus(), 0);
}
function removeNode(i){
  const gone = nodes[i].name;
  nodes.splice(i,1);
  nodes.forEach(n => n.req = n.req.filter(r => r !== gone));
  render();
}
function set(i,k,v){ nodes[i][k] = v; render(); }
function toggleReq(i,name){
  const r = nodes[i].req, at = r.indexOf(name);
  at < 0 ? r.push(name) : r.splice(at,1);
  render();
}

/* depth = longest path from a root, same rule the bot uses */
function tiers(){
  const byName = Object.fromEntries(nodes.filter(n=>n.name).map(n=>[n.name,n]));
  const depth = {}, seen = new Set();
  const walk = n => {
    if(depth[n.name] !== undefined) return depth[n.name];
    if(seen.has(n.name)) return 0;
    seen.add(n.name);
    const ps = n.req.map(r=>byName[r]).filter(Boolean);
    const d = ps.length ? 1 + Math.max(...ps.map(walk)) : 0;
    seen.delete(n.name);
    return depth[n.name] = d;
  };
  nodes.filter(n=>n.name).forEach(walk);
  const out = {};
  nodes.filter(n=>n.name).forEach(n => (out[depth[n.name]] ||= []).push(n));
  return out;
}

function render(){
  document.getElementById("editor").innerHTML = nodes.map((n,i)=>{
    const others = nodes.filter((_,j)=>j!==i).map(o=>o.name).filter(Boolean);
    const chips = others.length
      ? others.map(o=>`<button type="button" class="chip" aria-pressed="${n.req.includes(o)}"
           onclick="toggleReq(${i},'${esc(o).replace(/'/g,"\\'")}')">${esc(o)}</button>`).join("")
      : `<span class="empty">Add another milestone first</span>`;
    return `<div class="card">
      <h3>Milestone ${i+1}<button class="ghost" onclick="removeNode(${i})">Remove</button></h3>
      <div class="field"><label for="n${i}-name">Name</label>
        <input type="text" id="n${i}-name" value="${esc(n.name)}"
               placeholder="Venue booked" oninput="set(${i},'name',this.value)"></div>
      <div class="field"><label for="n${i}-desc">What is it?</label>
        <textarea id="n${i}-desc" placeholder="The steps, the detail, whatever helps"
               oninput="set(${i},'desc',this.value)">${esc(n.desc)}</textarea></div>
      <div class="field"><label for="n${i}-un">What does finishing it make possible?</label>
        <input type="text" id="n${i}-un" value="${esc(n.unlocks)}"
               placeholder="the date becomes announceable" oninput="set(${i},'unlocks',this.value)"></div>
      <div class="field"><label>Must come after</label><div class="chips">${chips}</div></div>
      <div class="row2">
        <div class="field"><label for="n${i}-xp">XP</label>
          <input type="number" id="n${i}-xp" min="0" max="5000" value="${n.xp}"
                 oninput="set(${i},'xp',this.value)"></div>
        <div class="field"><label>Closing</label>
          <div class="toggle"><input type="checkbox" id="n${i}-a" ${n.auto?"checked":""}
               onchange="set(${i},'auto',this.checked)">
            <label for="n${i}-a" style="text-transform:none;letter-spacing:0;font-family:var(--sans);
                   font-size:13px;margin:0">Closes itself at 100%</label></div></div>
      </div>
    </div>`;
  }).join("");

  const t = tiers(), depths = Object.keys(t).sort((a,b)=>a-b);
  document.getElementById("board").innerHTML = depths.length ? depths.map(d=>`
    <div class="tier"><h4>${d==0?"Open from the start":"After "+d+" gate"+(d>1?"s":"")}</h4>
    ${t[d].map(n=>`<div class="node ${d==0?"ready":""} ${n.auto?"":"sign"}">
      <div class="tag">${d==0?"READY TO START":"LOCKED"}${n.auto?"":" · NEEDS SIGN-OFF"} · ${n.xp} XP</div>
      <div class="nm">${esc(n.name)}</div>
      <div class="dt">${esc(n.desc)||"<span class='warn'>no description yet</span>"}</div>
      ${n.unlocks?`<div class="dt" style="margin-top:4px">→ ${esc(n.unlocks)}</div>`:""}
      ${n.req.length?`<div class="req">after ${n.req.map(esc).join(", ")}</div>`:""}
    </div>`).join("")}</div>`).join("")
    : `<p class="hint">Name a milestone and it will appear here.</p>`;
}

function csv(){
  const tree = document.getElementById("treename").value.trim() || "Untitled tree";
  const q = v => `"${String(v==null?"":v).replace(/"/g,'""')}"`;
  const rows = [["tree","milestone","description","unlocks","requires","xp","auto_close"]];
  nodes.filter(n=>n.name.trim()).forEach(n =>
    rows.push([tree, n.name, n.desc, n.unlocks, n.req.join("; "), n.xp || 100, n.auto]));
  return rows.map(r => r.map(q).join(",")).join("\n");
}

function download(){
  const name = (document.getElementById("treename").value.trim() || "tree")
                 .toLowerCase().replace(/[^a-z0-9]+/g,"-");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv()], {type:"text/csv"}));
  a.download = name + ".csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

function copyCsv(){
  navigator.clipboard?.writeText(csv()).then(
    () => alert("Copied. Paste it into a text file ending in .csv"),
    () => prompt("Copy this:", csv())
  );
}

render();
</script>
</body>
</html>
