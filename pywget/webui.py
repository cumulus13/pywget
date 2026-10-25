"""Tiny embedded web UI (served at ``/`` when the UI is enabled)."""

INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>pywget</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#1c1f23;--mut:#6b7280;--line:#d8dce2;--acc:#2563eb}
@media(prefers-color-scheme:dark){:root{--bg:#14171a;--fg:#e6e8eb;--mut:#9aa3ad;--line:#2b3138;--acc:#60a5fa}}
body{margin:0;font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:1000px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 12px}
form,.bar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
input{flex:1;min-width:160px;padding:7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer}
button.p{background:var(--acc);border-color:var(--acc);color:#fff}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
td.n{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mut{color:var(--mut)} progress{width:110px}
#msg{color:#dc2626;min-height:1.2em}
</style></head><body><main>
<h1>pywget <span class="mut" id="mode"></span></h1>
<div class="bar"><input id="token" type="password" placeholder="API token" autocomplete="off">
<button id="save">Connect</button><span class="mut" id="stats"></span></div>
<form id="add"><input id="url" placeholder="https://example.com/file.iso" required>
<input id="path" placeholder="sub-folder (optional)"><button class="p">Add</button></form>
<div id="msg"></div>
<table><thead><tr><th>Name</th><th>Status</th><th>Progress</th><th>Speed</th><th></th></tr></thead><tbody id="rows"></tbody></table>
<script>
const $=id=>document.getElementById(id);let token=sessionStorage.getItem('pywget-token')||'';$('token').value=token;
const fmt=n=>{if(n==null)return'-';const u=['B','KiB','MiB','GiB','TiB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?1:0)+' '+u[i]};
async function api(m,p,b){const r=await fetch(p,{method:m,headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});
const t=await r.text();const d=t?JSON.parse(t):null;if(!r.ok)throw new Error((d&&d.error)||r.statusText);return d}
function cell(tr,text,cls){const td=document.createElement('td');td.textContent=text;if(cls)td.className=cls;tr.appendChild(td);return td}
async function refresh(){try{const[s,j]=await Promise.all([api('GET','/api/v1/stats'),api('GET','/api/v1/jobs')]);
$('mode').textContent=s.mode;$('stats').textContent=fmt(s.speed)+'/s';$('msg').textContent='';const body=$('rows');body.replaceChildren();
for(const x of j.jobs.slice().reverse()){const tr=document.createElement('tr');
const name=x.name||x.url;cell(tr,name,'n').title=x.url;
cell(tr,x.status+(x.error?' - '+x.error:''));const td=document.createElement('td');const pr=document.createElement('progress');pr.max=100;pr.value=x.percent||0;td.appendChild(pr);td.append(' '+(x.percent!=null?x.percent+'%':''));tr.appendChild(td);
cell(tr,x.status==='running'?fmt(x.speed)+'/s':'-','mut');const act=document.createElement('td');
const btn=(label,fn)=>{const b=document.createElement('button');b.textContent=label;b.onclick=async()=>{try{await fn();refresh()}catch(e){$('msg').textContent=e.message}};act.appendChild(b)};
const id='/api/v1/jobs/'+x.id;
if(x.status==='running'||x.status==='queued')btn('Pause',()=>api('POST',id+'/pause'));
if(x.status==='paused')btn('Resume',()=>api('POST',id+'/resume'));
if(x.status==='failed'||x.status==='cancelled')btn('Retry',()=>api('POST',id+'/retry'));
if(['running','queued','paused'].includes(x.status))btn('Cancel',()=>api('POST',id+'/cancel'));else btn('Remove',()=>api('DELETE',id));
tr.appendChild(act);body.appendChild(tr)}}catch(e){$('msg').textContent=e.message}}
$('save').onclick=()=>{token=$('token').value.trim();sessionStorage.setItem('pywget-token',token);refresh()};
$('add').onsubmit=async e=>{e.preventDefault();try{await api('POST','/api/v1/jobs',{url:$('url').value.trim(),path:$('path').value.trim()});$('url').value='';refresh()}catch(x){$('msg').textContent=x.message}};
refresh();setInterval(refresh,1500);
</script></main></body></html>
"""
