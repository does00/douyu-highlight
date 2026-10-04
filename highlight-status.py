#!/usr/bin/env python3
"""精彩片段流水线管理页（独立项目）。

Tab1 状态：运行状态、投稿记录、本地片段、日志
Tab2 房间管理：增删房间、启用/禁用、基础参数
Tab3 剪辑管理：按房间配置剪辑参数
Tab4 AI 配置：全局 AI + 按房间开关

127.0.0.1:18022，systemd 服务 highlight-status.service
"""
import json
import os
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(os.environ.get("HL_BASE", "/home/hatch/workspace/bililive-cli"))
STATE = Path(os.environ.get("STATE", str(BASE / "highlight-state.json")))
LOG = Path(os.environ.get("LOG", str(BASE / "config" / "highlight.log")))
LOCK = BASE / "highlight.lock"
HL_DIR = Path(os.environ.get("WORKDIR", str(BASE / "highlights")))
ROOMS_CFG = BASE / "highlight-rooms.json"
PORT = int(os.environ.get("PORT", "18022"))

# 多平台配置：{providerId: (中文名, 房间链接模板, 房间号提取正则)}
PLATFORMS = {
    "DouYu":    ("斗鱼",   "https://www.douyu.com/{id}",      r"douyu\.com/(\d+)"),
    "HuYa":     ("虎牙",   "https://www.huya.com/{id}",       r"huya\.com/([^/?#]+)"),
    "DouYin":   ("抖音",   "https://live.douyin.com/{id}",    r"live\.douyin\.com/(\d+)"),
    "TikTok":   ("TikTok", "https://www.tiktok.com/{id}/live", r"tiktok\.com/(@[^/?#]+)"),
    "Bilibili": ("B站",    "https://live.bilibili.com/{id}",  r"live\.bilibili\.com/(\d+)"),
}
# 各平台默认清晰度（录制器 usedStream；不填则用录制器默认）
PLATFORM_STREAM = {"DouYu": "蓝光4M"}


def load_rooms():
    try:
        return json.loads(ROOMS_CFG.read_text())
    except Exception:
        return {"rooms": [], "ai_global": {}}


def save_rooms(cfg):
    ROOMS_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def get_status():
    # 聚合所有房间的 state（多房间：highlight-state-{rid}.json）
    import glob
    all_processed, all_uploaded = 0, 0
    recent = []
    for sf in sorted(glob.glob(str(BASE / "highlight-state-*.json"))):
        try:
            d = json.load(open(sf))
            all_processed += len(d.get("processed", []))
            all_uploaded += len(d.get("uploaded", []))
            for u in d.get("uploaded", [])[-5:]:
                recent.append({
                    "title": u.get("title", ""),
                    "score": u.get("score", 0),
                    "reasons": u.get("reasons", []),
                    "task": u.get("task", "")[:8],
                    "dur": round(u.get("end", 0) - u.get("start", 0)),
                    "at": u.get("at", 0),
                })
        except Exception:
            pass
    recent = sorted(recent, key=lambda x: x.get("at", 0), reverse=True)[:10]
    # 兼容旧单文件（没有多房间文件时）
    if not recent:
        try:
            state = json.loads(STATE.read_text()) if STATE.exists() else {}
        except Exception:
            state = {}
        uploaded = state.get("uploaded", [])
        all_processed = len(state.get("processed", []))
        all_uploaded = len(uploaded)
        for u in uploaded[-20:][::-1]:
            recent.append({
                "title": u.get("title", ""),
                "score": u.get("score", 0),
                "reasons": u.get("reasons", []),
                "task": u.get("task", "")[:8],
                "dur": round(u.get("end", 0) - u.get("start", 0)),
            })
    logs = []
    try:
        if LOG.exists():
            logs = LOG.read_text().strip().split("\n")[-40:]
    except Exception:
        pass
    clips = []
    try:
        for p in sorted(HL_DIR.glob("*.mp4"),
                        key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
            clips.append({"name": p.name,
                          "size_mb": round(p.stat().st_size / 1048576, 1)})
    except Exception:
        pass
    cfg = load_rooms()
    return {
        "running": LOCK.exists(),
        "processed": all_processed,
        "uploaded_count": all_uploaded,
        "recent": recent,
        "clips": clips,
        "logs": logs,
        "rooms": cfg.get("rooms", []),
        "ai_global": {k: (v if k != "api_key" else None) for k, v in cfg.get("ai_global", {}).items()} | {"has_key": bool(cfg.get("ai_global", {}).get("api_key"))},
        "has_bili_sessdata": bool(cfg.get("bili_sessdata")),
        "notify": {k: (v if k != "api_key" else None) for k, v in cfg.get("notify", {}).items()} | {"has_key": bool(cfg.get("notify", {}).get("api_key"))},
        "recorders": {rid: {"recording": bool(r.get("recordHandle")),
                            "live": bool(r.get("liveInfo", {}).get("living"))}
                      for rid, r in _get_recorders().items()},
        "updated": datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S"),
    }


PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>精彩片段流水线</title>
<style>
body{background:#0f1420;color:#dbe2f0;font-family:system-ui,sans-serif;margin:0;padding:16px}
h1{font-size:20px;margin:0 0 12px}h2{font-size:15px;margin:14px 0 8px;color:#9fb2d8}
.card{background:#182032;border:1px solid #26314a;border-radius:10px;padding:14px;margin-bottom:12px}
.badge{display:inline-block;background:#24304d;border-radius:12px;padding:3px 10px;margin:2px 4px 2px 0;font-size:13px}
.badge.ok{background:#1d4d2e}.badge.run{background:#4d3a1d}.badge.off{background:#4d1d1d}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #26314a}
th{color:#9fb2d8;font-weight:600}.hint{color:#7a89a8}
.logs{background:#0b0f1a;border-radius:6px;padding:10px;font:12px/1.6 monospace;
white-space:pre-wrap;max-height:300px;overflow:auto}
button{background:#2b3a5e;color:#fff;border:0;border-radius:6px;padding:6px 14px;cursor:pointer;margin:2px}
button.danger{background:#5e2b2b}button.ok{background:#1d4d2e}
input,select{background:#0b0f1a;color:#dbe2f0;border:1px solid #26314a;border-radius:6px;padding:6px 10px;margin:2px}
label{display:inline-block;min-width:110px;color:#9fb2d8;font-size:13px}
.row{display:flex;gap:16px;flex-wrap:wrap}.row>div{flex:1;min-width:300px}
.tabs{display:flex;gap:4px;margin-bottom:12px}
.tabs button{background:#182032;border:1px solid #26314a;border-radius:8px 8px 0 0;padding:8px 20px}
.tabs button.active{background:#2b3a5e;border-bottom:2px solid #5e8bff}
.tab{display:block;margin-bottom:20px}
.form-row{margin:8px 0}
</style></head><body>
<h1>🎬 精彩片段流水线</h1>
<div class="tabs" style="display:none"></div>

<div id="tab-rooms" class="tab active">
<div class="card"><h2>房间列表</h2>
<table><thead><tr><th>平台</th><th>房间号</th><th>主播</th><th>流水线</th><th>录制器</th><th>合集ID</th><th>录像目录</th><th>管理</th></tr></thead>
<tbody id="t_rooms"></tbody></table></div>
<div class="card"><h2>添加房间</h2>
<div class="form-row"><label>平台</label><select id="nr_platform">
<option value="DouYu">斗鱼</option><option value="HuYa">虎牙</option>
<option value="DouYin">抖音</option><option value="TikTok">TikTok</option>
<option value="Bilibili">B站</option></select>
<span class="hint">粘贴直播间链接可自动识别平台</span></div>
<div class="form-row"><label>房间链接/号</label><input id="nr_id" style="width:300px" placeholder="粘贴直播间链接或填房间号" oninput="detectPlatform()">
<label style="margin-left:12px">主播名</label><input id="nr_streamer" style="width:150px" placeholder="选填，跳过自动获取">
<button class="ok" onclick="addRoom()">添加</button>
<span class="hint">主播名留空则自动获取</span></div>
<div class="hint" id="msg_rooms"></div></div>
</div>

<div id="tab-clips" class="tab">
<div class="card"><h2>剪辑参数</h2>
<div class="form-row"><label>选择房间</label><select id="clip_room" onchange="loadClipForm()"></select></div>
<div id="clip_form"></div>
<div class="form-row"><button class="ok" onclick="saveClip()">保存</button></div>
<div class="hint" id="msg_clips"></div></div>
</div>

<div id="tab-ai" class="tab">
<div class="card"><h2>全局 AI 设置</h2>
<div class="form-row"><label>服务商</label><select id="ai_provider" onchange="onProviderChange()">
<option value="deepseek">DeepSeek</option><option value="gemini">Gemini</option><option value="relay">中继</option>
</select></div>
<div class="form-row"><label>中继地址</label><input id="ai_relay" style="width:320px"></div>
<div class="form-row"><label>API Key</label><input id="ai_key" type="password" style="width:320px" placeholder="AIza…（直连模式用，留空则用中继）"></div>
<div class="form-row"><label>代理地址</label><input id="ai_proxy" style="width:320px" placeholder="http://代理:端口（仅 Gemini 需要）"></div>
<div class="form-row"><label>模型</label><select id="ai_model" style="width:260px"><option>加载中…</option></select></div>
<div class="form-row"><label>挑段提示词</label><textarea id="ai_prompt" rows="6" style="width:100%" placeholder="留空用默认提示词"></textarea>
<span class="hint">只写你的要求，不用写[转写][弹幕]标签和JSON格式（系统自动拼）。例："只选打架吵架破防的时刻，不要闲聊碎碎念，宁可不选也别凑数"。留空恢复默认。时长（180-600秒）自动从剪辑管理读取。</span></div>
<button onclick="loadModels()">刷新模型列表</button></div>
<div class="form-row"><button onclick="testAI()">🔌 测试连通性</button>
<span id="ai_test_result" class="hint"></span></div>
<div class="form-row"><button class="ok" onclick="saveAI()">保存全局设置</button></div></div>
<div class="card"><h2>B 站合集</h2>
<div class="form-row"><label>SESSDATA</label><input id="bili_sessdata" type="password" style="width:320px" placeholder="投稿后自动加合集用"></div>
<div class="form-row"><button class="ok" onclick="saveBili()">保存</button>
<span class="hint" id="msg_bili"></span></div>
<div class="hint">合集 ID 在房间管理里按房间配置（season_id）</div></div>
<div class="card"><h2>按房间 AI 开关</h2>
<table><thead><tr><th>房间</th><th>主播</th><th>AI 标题</th><th>操作</th></tr></thead>
<tbody id="t_ai_rooms"></tbody></table>
<div class="hint" id="msg_ai"></div></div>
<div class="card"><h2>QQ 投稿通知（AstrBot）</h2>
<div class="form-row"><label>AstrBot 地址</label><input id="nt_url" style="width:320px" placeholder="http://IP:6185"></div>
<div class="form-row"><label>API Key</label><input id="nt_key" type="password" style="width:320px" placeholder="im 权限的 API Key，留空不修改"></div>
<div class="form-row"><label>通知目标</label><input id="nt_umo" style="width:320px" placeholder="平台ID:FriendMessage:openid"></div>
<div class="hint">格式：平台ID:消息类型:会话ID。私聊填 FriendMessage:用户openid，群聊填 GroupMessage:群openid；openid 获取：给机器人发条消息，去 AstrBot 日志里找那一行的 session id</div>
<div class="form-row"><label>启用通知</label><input id="nt_enabled" type="checkbox" style="width:20px"></div>
<div class="form-row"><button class="ok" onclick="saveNotify()">保存</button>
<button onclick="testNotify()">🔔 发送测试消息</button>
<span class="hint" id="msg_notify"></span></div>
<div class="hint">投稿成功后经 AstrBot OpenAPI 发 QQ 私聊通知</div></div>

<div id="tab-status" class="tab">
<div class="card"><span class="badge" id="b_run">…</span><span class="badge" id="b_cnt">…</span>
<span class="hint" id="b_upd" style="margin-left:8px"></span>
<button style="float:right" onclick="loadStatus()">刷新</button></div>
<div class="row"><div class="card"><h2>最近投稿</h2>
<table><thead><tr><th>标题</th><th>分</th><th>时长</th><th>理由</th></tr></thead>
<tbody id="t_recent"><tr><td colspan="4" class="hint">加载中…</td></tr></tbody></table></div>
<div class="card"><h2>本地片段</h2>
<table><thead><tr><th>文件</th><th>大小</th></tr></thead>
<tbody id="t_clips"><tr><td colspan="2" class="hint">加载中…</td></tr></tbody></table></div></div>
<div class="card"><h2>运行日志</h2><div class="logs" id="t_logs">加载中…</div></div>
</div>
<div style='text-align:center;color:#666;font-size:12px;padding:10px'>v20261003-1628</div>

<script>
let G={rooms:[],ai_global:{}};
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
function showTab(n,el){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('active'));
document.getElementById('tab-'+n).classList.add('active');
if(el)el.classList.add('active');
if(n==='rooms')renderRooms();if(n==='clips')initClipTab();if(n==='ai')renderAI();}
async function api(p,o={}){const r=await fetch(p,Object.assign({headers:{'Content-Type':'application/json'}},o));
const j=await r.json();if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j;}

// 状态页
async function loadStatus(){try{const h=await api('/api/status');
G.rooms=h.rooms||[];G.ai_global=h.ai_global||{};G.recorders=h.recorders||{};G.notify=h.notify||{};
const br=document.getElementById('b_run');
br.textContent=h.running?'● 运行中':'○ 空闲';br.className='badge '+(h.running?'run':'');
document.getElementById('b_cnt').textContent='已处理 '+h.processed+' / 已投稿 '+h.uploaded_count;
document.getElementById('b_upd').textContent='更新于 '+h.updated;
document.getElementById('t_recent').innerHTML=h.recent.length?h.recent.map(r=>
'<tr><td>'+esc(r.title)+'</td><td>'+r.score+'</td><td>'+r.dur+'s</td><td class="hint">'+esc(r.reasons.join(' '))+'</td></tr>').join('')
:'<tr><td colspan="4" class="hint">暂无投稿</td></tr>';
document.getElementById('t_clips').innerHTML=h.clips.length?h.clips.map(c=>
'<tr><td>'+esc(c.name)+'</td><td>'+c.size_mb+'MB</td></tr>').join('')
:'<tr><td colspan="2" class="hint">暂无本地片段</td></tr>';
document.getElementById('t_logs').textContent=h.logs.slice().reverse().join('\\n')||'(暂无日志)';
}catch(e){document.getElementById('t_logs').textContent='加载失败：'+e.message;}}

// 房间管理
const PNAME={DouYu:'斗鱼',HuYa:'虎牙',DouYin:'抖音',TikTok:'TikTok',Bilibili:'B站',XHS:'小红书'};
function detectPlatform(){const v=document.getElementById('nr_id').value.trim();
const sel=document.getElementById('nr_platform');
if(/douyu\.com\/(\d+)/i.test(v))sel.value='DouYu';
else if(/huya\.com\/([^/?#]+)/i.test(v))sel.value='HuYa';
else if(/live\.bilibili\.com\/(\d+)/i.test(v))sel.value='Bilibili';
else if(/live\.douyin\.com\/(\d+)/i.test(v))sel.value='DouYin';
else if(/tiktok\.com\/@([^/?#]+)/i.test(v))sel.value='TikTok';}
function renderRooms(){const tb=document.getElementById('t_rooms');
tb.innerHTML=G.rooms.map((r,i)=>'<tr><td>'+esc(PNAME[r.platform||'DouYu']||r.platform||'斗鱼')+'</td><td>'+esc(r.room_id)+'</td><td>'+esc(r.streamer)+'</td>'+
'<td><button class="'+(r.enabled?'ok':'')+'" data-i="'+i+'" onclick="toggleRoomByIdx(this)" title="'+(r.enabled?'点击暂停流水线（只录像不处理）':'点击恢复流水线处理')+'">'+(r.enabled?'处理中':'已暂停')+'</button></td>'+
'<td>'+(G.recorders&&G.recorders[r.room_id]?(G.recorders[r.room_id].recording?'<span class="badge ok">●录制中</span> <button data-i="'+i+'" onclick="stopRecorderByIdx(this)" title="暂停录制">⏸</button>':(G.recorders[r.room_id].live?'<span class="badge">直播中</span> <button data-i="'+i+'" onclick="startRecorderByIdx(this)" title="开始录制">▶</button> <button data-i="'+i+'" onclick="delRecorderByIdx(this)" title="删除录制器">删</button>':'<span class="hint" title="主播未开播，等待中">待机</span> <button data-i="'+i+'" onclick="delRecorderByIdx(this)" title="删除录制器（以后开播也不录）">删</button>')):'<button data-i="'+i+'" onclick="addRecorderByIdx(this)">+录制器</button>')+'</td>'+
'<td><input id="season_'+i+'" style="width:80px" value="'+(r.season_id||'')+'" placeholder="合集ID"> '+
'<button onclick="setSeason('+i+')">保存</button></td>'+
'<td class="hint">'+esc(r.segdir||'')+'</td>'+
'<td><button class="danger" onclick="delRoom('+i+')">删除</button></td></tr>').join('')
||'<tr><td colspan="8" class="hint">暂无房间</td></tr>';}
async function setSeason(i){const el=document.getElementById('season_'+i);
const v=el.value.trim();el.disabled=true;
try{await api('/api/rooms',{method:'POST',
body:JSON.stringify({action:'season',index:i,season_id:v?parseInt(v):null})});
el.style.borderColor='#4caf50';setTimeout(()=>el.style.borderColor='',1500);
await loadStatus();renderRooms();
}catch(e){el.style.borderColor='#f44336';}
el.disabled=false;}
async function addRoom(){const id=document.getElementById('nr_id').value.trim();
const platform=document.getElementById('nr_platform').value;
const streamer=document.getElementById('nr_streamer').value.trim();
if(!id){document.getElementById('msg_rooms').textContent='房间链接/号必填';return;}
document.getElementById('msg_rooms').textContent=streamer?'添加中…':'获取主播信息中…';
try{const r=await api('/api/rooms',{method:'POST',body:JSON.stringify({action:'add',room_id:id,platform,streamer})});
document.getElementById('msg_rooms').textContent='已添加：'+r.streamer;
document.getElementById('nr_id').value='';document.getElementById('nr_streamer').value='';await loadStatus();renderRooms();
}catch(e){document.getElementById('msg_rooms').textContent='失败：'+e.message;}}
async function toggleRoom(i){try{await api('/api/rooms',{method:'POST',
body:JSON.stringify({action:'toggle',index:i})});await loadStatus();renderRooms();}catch(e){}}
async function delRoom(i){if(!confirm('删除房间 '+G.rooms[i].room_id+'？'))return;
try{await api('/api/rooms',{method:'POST',body:JSON.stringify({action:'del',index:i})});
await loadStatus();renderRooms();}catch(e){}}

// 剪辑管理
function initClipTab(){const s=document.getElementById('clip_room');
s.innerHTML=G.rooms.map((r,i)=>'<option value="'+i+'">'+esc(r.room_id)+' '+esc(r.streamer)+'</option>').join('');
loadClipForm();}
function loadClipForm(){const i=document.getElementById('clip_room').value;
if(i===''){document.getElementById('clip_form').innerHTML='';return;}
const c=G.rooms[i].clip||{};
document.getElementById('clip_form').innerHTML=
'<div class="form-row"><label>窗口秒数</label><input id="c_win" type="number" value="'+(c.win_sec||180)+'">'+
'<span class="hint">一次看多少秒的内容来打分，越长越不容易漏，但越容易把前后无聊的部分也带进来</span></div>'+
'<div class="form-row"><label>步长秒数</label><input id="c_step" type="number" value="'+(c.win_step||60)+'">'+
'<span class="hint">窗口每次滑动多少秒，越小越精细但越慢，一般设为窗口的三分之一</span></div>'+
'<div class="form-row"><label>保留阈值</label><input id="c_score" type="number" step="0.5" value="'+(c.keep_score??10)+'">'+
'<span class="hint">打分低于这个的直接扔掉。5=平衡，10=只要高质量（默认，数量少），15=只留最炸的。实测优质片段一般在13-17分</span></div>'+
'<div class="form-row"><label>最多片段</label><input id="c_max" type="number" value="'+(c.max_keep??3)+'">'+
'<span class="hint">每个录像文件最多剪几个片段投稿，多了容易审美疲劳</span></div>'+
'<div class="form-row"><label>保留天数</label><input id="c_ret" type="number" value="'+(c.retention_days??1)+'">'+
'<span class="hint">原始录像处理完后保留几天再删除，0=永久保留（硬盘要够大）</span></div>'+
'<div class="form-row"><label>最短时长</label><input id="c_min" type="number" value="'+(c.min_clip??180)+'">'+
'<span class="hint">单个片段最短多少秒，太短了讲不清故事，建议不低于30秒</span></div>'+
'<div class="form-row"><label>最长时长</label><input id="c_maxd" type="number" value="'+(c.max_clip||300)+'">'+
'<span class="hint">单个片段最长多少秒，太长了观众看不完，建议不超过5分钟</span></div>';}
function savedMsg(id){const d=new Date();
document.getElementById(id).textContent='已保存 '+d.getHours()+':'+String(d.getMinutes()).padStart(2,'0')+':'+String(d.getSeconds()).padStart(2,'0');}
function clearMsgOnChange(ids,msgId){ids.forEach(id=>{const el=document.getElementById(id);
if(el)el.addEventListener('input',()=>{document.getElementById(msgId).textContent='';});});}
async function saveClip(){const i=document.getElementById('clip_room').value;
const clip={win_sec:+document.getElementById('c_win').value,win_step:+document.getElementById('c_step').value,
keep_score:+document.getElementById('c_score').value,max_keep:+document.getElementById('c_max').value,
retention_days:+document.getElementById('c_ret').value,
min_clip:+document.getElementById('c_min').value,max_clip:+document.getElementById('c_maxd').value};
try{await api('/api/rooms',{method:'POST',body:JSON.stringify({action:'clip',index:+i,clip})});
savedMsg('msg_clips');await loadStatus();
}catch(e){document.getElementById('msg_clips').textContent='失败：'+e.message;}}

// AI 配置
async function loadModels(){
const pv=document.getElementById('ai_provider').value||'deepseek';
const sel=document.getElementById('ai_model');
try{
const h=await api('/api/models?provider='+encodeURIComponent(pv));
const models=(h&&h.models)||[];
const cur=G.ai_global.model||'';
sel.innerHTML=models.map(m=>'<option value="'+esc(m)+'"'+(m===cur?' selected':'')+'>'+esc(m)+'</option>').join('')
||'<option value="">（无可用模型）</option>';
if(cur&&models.indexOf(cur)>=0)sel.value=cur;
}catch(e){sel.innerHTML='<option value="">加载失败，点刷新重试</option>';}}
function onProviderChange(){loadModels();}
function renderAI(){renderNotify();
document.getElementById('ai_relay').value=G.ai_global.relay_url||'';
document.getElementById('ai_provider').value=G.ai_global.provider||'deepseek';
document.getElementById('ai_prompt').value=G.ai_global.prompt||'';
loadModels();
document.getElementById('t_ai_rooms').innerHTML=G.rooms.map((r,i)=>{
const en=r.ai&&r.ai.enabled!==false;
return '<tr><td>'+esc(r.room_id)+'</td><td>'+esc(r.streamer)+'</td>'+
'<td><span class="badge '+(en?'ok':'off')+'">'+(en?'开':'关')+'</span></td>'+
'<td><button onclick="toggleAI('+i+')">'+(en?'关闭':'开启')+'</button></td></tr>';}).join('');}
async function saveAI(){try{await api('/api/ai',{method:'POST',body:JSON.stringify({
relay_url:document.getElementById('ai_relay').value.trim(),
proxy:document.getElementById('ai_proxy').value.trim(),
api_key:document.getElementById('ai_key').value.trim(),
provider:document.getElementById('ai_provider').value,
model:document.getElementById('ai_model').value,
prompt:document.getElementById('ai_prompt').value.trim()})});
savedMsg('msg_ai');await loadStatus();renderNotify();
}catch(e){document.getElementById('msg_ai').textContent='失败：'+e.message;}}
// QQ 投稿通知（AstrBot）
function renderNotify(){const n=G.notify||{};
document.getElementById('nt_url').value=n.url||'';
document.getElementById('nt_umo').value=n.umo||'';
document.getElementById('nt_enabled').checked=n.enabled!==false;
document.getElementById('nt_key').value='';
document.getElementById('nt_key').placeholder=n.has_key?'已设置（留空不修改）':'im 权限的 API Key';}
async function saveNotify(){const msg=document.getElementById('msg_notify');
try{await api('/api/notify',{method:'POST',body:JSON.stringify({
url:document.getElementById('nt_url').value.trim(),
api_key:document.getElementById('nt_key').value,
umo:document.getElementById('nt_umo').value.trim(),
enabled:document.getElementById('nt_enabled').checked})});
savedMsg('msg_notify');await loadStatus();renderNotify();
}catch(e){msg.textContent='失败：'+e.message;}}
async function testNotify(){const msg=document.getElementById('msg_notify');
msg.textContent='发送中…';
try{const r=await api('/api/notify/test');
msg.textContent=r.ok?'✅ 已发送，请查收 QQ':'❌ 失败：'+(r.error||'未知错误');
}catch(e){msg.textContent='❌ 请求失败：'+e.message;}}
async function saveBili(){const v=document.getElementById('bili_sessdata').value.trim();
try{await api('/api/bili',{method:'POST',body:JSON.stringify({sessdata:v})});
savedMsg('msg_bili');
document.getElementById('bili_sessdata').value='';await loadStatus();renderAI();
}catch(e){document.getElementById('msg_bili').textContent='失败：'+e.message;}}
async function delRecorderByIdx(btn){const i=+btn.dataset.i;const r=G.rooms[i];
if(!r||!confirm('删除 '+r.streamer+' 的录制器？以后开播也不会自动录了。'))return;
try{await api('/api/recorder/delete',{method:'POST',body:JSON.stringify({room_id:r.room_id})});
await loadStatus();renderRooms();}catch(e){alert('失败：'+e.message);}}
async function startRecorderByIdx(btn){const i=+btn.dataset.i;const r=G.rooms[i];
if(!r||!confirm('开始录制 '+r.streamer+'？'))return;
try{await api('/api/recorder/start',{method:'POST',body:JSON.stringify({room_id:r.room_id})});
await loadStatus();renderRooms();}catch(e){alert('失败：'+e.message);}}
async function stopRecorderByIdx(btn){const i=+btn.dataset.i;const r=G.rooms[i];
if(!r||!confirm('暂停 '+r.streamer+' 的录制？录制器保留，下次开播自动继续。'))return;
try{await api('/api/recorder/stop',{method:'POST',body:JSON.stringify({room_id:r.room_id})});
await loadStatus();renderRooms();}catch(e){alert('失败：'+e.message);}}
async function addRecorderByIdx(btn){const i=+btn.dataset.i;const r=G.rooms[i];
if(!r)return;addRecorder(r.room_id,r.streamer,r.platform||'DouYu');}
async function addRecorder(rid, streamer, platform){
if(!confirm('给 '+streamer+'（'+rid+'）添加录制器？'))return;
try{await api('/api/recorder',{method:'POST',body:JSON.stringify({room_id:rid,streamer,platform:platform||'DouYu'})});
await loadStatus();renderRooms();
}catch(e){alert('失败：'+e.message);}}
async function toggleRoomByIdx(btn){const i=+btn.dataset.i;
try{await api('/api/rooms',{method:'POST',body:JSON.stringify({action:'toggle',index:i})});
await loadStatus();renderRooms();}catch(e){alert('失败：'+e.message);}}
async function toggleAI(i){try{await api('/api/ai',{method:'POST',
body:JSON.stringify({room_index:i})});await loadStatus();renderAI();}catch(e){}}
async function testAI(){const el=document.getElementById('ai_test_result');
el.textContent='测试中…';el.className='hint';
try{const r=await api('/api/ai/test');
if(r.ok){el.textContent='✅ 通畅['+r.mode+']，延迟 '+r.latency_ms+'ms，模型 '+r.model;el.className='hint';}
else{el.textContent='❌ 失败（'+r.latency_ms+'ms）：'+r.error;el.className='hint';}
}catch(e){el.textContent='❌ 请求失败：'+e.message;}}

loadStatus().then(()=>{renderRooms();initClipTab();renderAI();});setInterval(loadStatus,60000);
</script></body></html>"""



def _get_recorders():
    """从 bililive 获取录制器列表 {channelId: recorder}。"""
    import urllib.request
    try:
        passkey = json.load(open(str(BASE / "config" / "appConfig.json")))["passKey"]
        req = urllib.request.Request(
            "http://127.0.0.1:18010/recorder/list",
            headers={"Authorization": passkey})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        return {str(x.get("channelId")): x for x in d["payload"]["data"]}
    except Exception:
        return {}


def _egress_proxy():
    """沙箱出站直连会被透明劫持 RST：取 CONNECT 代理地址。"""
    p = (os.environ.get("PROXY_URL") or os.environ.get("HTTPS_PROXY")
         or os.environ.get("HTTP_PROXY") or "")
    if not p:
        try:
            with open("/home/hatch/agsbx/.proxyenv") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("EGRESS_PROXY="):
                        p = line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
    return p


def _urlopen(req, timeout):
    """经 egress CONNECT 代理请求（沙箱直连会被 RST）；无代理配置时直连。"""
    import urllib.request
    proxy = _egress_proxy()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _check_auth(self):
        """HTTP Basic 认证。密码从环境变量 HIGHLIGHT_AUTH 或 WEBUI_PASSWORD 读，默认 highlight。"""
        import os, base64
        expected = os.environ.get("HIGHLIGHT_AUTH") or os.environ.get("WEBUI_PASSWORD") or "highlight"
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            _, pwd = decoded.split(":", 1)
            return pwd == expected
        except Exception:
            return False

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="highlight"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln) or b"{}")

    def do_GET(self):
        if not self._check_auth():
            self._require_auth()
            return
        try:
            path = urlparse(self.path).path
            if path == "/api/status":
                self._json(get_status())
            elif path == "/api/models":
                self._handle_models()
            elif path == "/api/ai/test":
                self._handle_ai_test()
            elif path == "/api/notify/test":
                self._handle_notify_test()
            else:
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self._check_auth():
            self._require_auth()
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/rooms":
                self._handle_rooms()
            elif path == "/api/ai":
                self._handle_ai()
            elif path == "/api/notify":
                self._handle_notify()
            elif path == "/api/bili":
                self._handle_bili()
            elif path == "/api/recorder":
                self._handle_recorder()
            elif path == "/api/recorder/stop":
                self._handle_recorder_stop()
            elif path == "/api/recorder/start":
                self._handle_recorder_start()
            elif path == "/api/recorder/delete":
                self._handle_recorder_delete()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


    def _fetch_streamer(self, rid):
        """从斗鱼 API 获取主播名。"""
        import urllib.request
        try:
            req = urllib.request.Request(
                f"https://www.douyu.com/betard/{rid}",
                headers={"User-Agent": "Mozilla/5.0"})
            # 走代理（沙箱需要）
            import os
            proxy = os.environ.get("PROXY_URL") or os.environ.get("HTTP_PROXY")
            if proxy:
                req.set_proxy(proxy, "http")
                req.set_proxy(proxy, "https")
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            room = data.get("room", {})
            name = (room.get("owner_name") or "").strip()
            return name or None
        except Exception:
            return None

    @staticmethod
    def _extract_room_id(platform, raw):
        """从链接或纯房间号提取规范房间号（TikTok 保留 @ 前缀）。"""
        import re
        raw = (raw or "").strip()
        pat = PLATFORMS.get(platform, (None, None, None))[2]
        if pat:
            m = re.search(pat, raw, re.I)
            if m:
                return m.group(1)
        # 纯房间号 / @id 直接用
        m = re.search(r"(@?[\w\-]+)$", raw)
        return m.group(1) if m else raw

    def _resolve_channel(self, platform, rid):
        """调录制器 batchResolveChannel 解析，返回 {providerId,channelId,owner,channelURL}。"""
        import urllib.request
        tpl = PLATFORMS.get(platform, (None, None, None))[1]
        if not tpl:
            return None
        url = tpl.format(id=rid)
        try:
            passkey = json.load(open(str(BASE / "config" / "appConfig.json")))["passKey"]
            req = urllib.request.Request(
                "http://127.0.0.1:18010/recorder/manager/batchResolveChannel",
                data=json.dumps({"channelURLs": [url]}).encode(),
                headers={"Content-Type": "application/json", "Authorization": passkey})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            results = (d.get("payload") or {}).get("results") or d.get("results") or []
            if results and results[0].get("success"):
                return results[0].get("data") or {}
            return None
        except Exception:
            return None

    def _handle_models(self):
        """GET /api/models：从 Gemini API 拉取可用模型列表。"""
        import urllib.request
        cfg = load_rooms()
        proxy = cfg.get("ai_global", {}).get("proxy", "")
        # 通过中继获取模型列表（中继会处理认证）
        relay = cfg.get("ai_global", {}).get("relay_url", "http://127.0.0.1:18020/generate")
        # 中继的 /models 端点（如果支持），否则返回常用列表
        try:
            # 尝试直接调 Gemini API 的 models 列表
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            # 注意：这里需要 API key，但我们没有直接访问权限
            # 返回已知可用模型列表
            from urllib.parse import urlparse as _up, parse_qs as _pq
            q = _pq(_up(self.path).query)
            pv = (q.get("provider", ["deepseek"])[0] or "deepseek").lower()
            if pv == "deepseek":
                models = ["deepseek-chat", "deepseek-reasoner"]
            elif pv == "gemini":
                models = ["gemini-3-flash-preview", "gemini-2.0-flash",
                          "gemini-1.5-flash", "gemini-1.5-pro"]
            else:
                models = ["gemini-3-flash-preview"]
            self._json({"models": models})
        except Exception as e:
            self._json({"models": ["gemini-3-flash-preview"], "error": str(e)})

    def _handle_ai_test(self):
        """GET /api/ai/test：测试 AI 连通性和延迟（直连优先，否则中继）。"""
        import time
        import sys
        sys.path.insert(0, str(BASE))
        t0 = time.time()
        try:
            from llm_highlights import _call_llm, USE_DIRECT, GEMINI_MODEL
            # 同步配置里的模型选择
            import llm_highlights
            cfg = load_rooms()
            g = cfg.get("ai_global", {})
            pv = g.get("provider", "deepseek")
            llm_highlights.LLM_PROVIDER = pv
            # 模型按 provider 纠正（防止存的是别的 provider 的模型名）
            # 注意：deepseek-flash 实测返回空内容，已从合法列表移除
            valid_ds = ("deepseek-chat", "deepseek-reasoner")
            valid_gm = ("gemini-3-flash-preview", "gemini-2.0-flash",
                        "gemini-1.5-flash", "gemini-1.5-pro")
            m = g.get("model", "")
            if pv == "deepseek":
                llm_highlights.DEEPSEEK_MODEL = m if m in valid_ds else "deepseek-chat"
            elif pv == "gemini":
                llm_highlights.GEMINI_MODEL = m if m in valid_gm else "gemini-3-flash-preview"
            if g.get("proxy"):
                llm_highlights.HTTP_PROXY = g["proxy"]
            if g.get("api_key"):
                if pv == "deepseek":
                    llm_highlights.DEEPSEEK_API_KEY = g["api_key"]
                else:
                    llm_highlights.GEMINI_API_KEY = g["api_key"]
                llm_highlights.LLM_OK = True
                llm_highlights.USE_DIRECT = True
            # 重新计算 LLM_MODEL
            llm_highlights.LLM_MODEL = (llm_highlights.DEEPSEEK_MODEL
                if pv == "deepseek" else llm_highlights.GEMINI_MODEL)
            mode = {"deepseek": "DeepSeek直连", "gemini": "Gemini直连"}.get(
                pv, "中继") if llm_highlights.USE_DIRECT else "中继"
            resp = _call_llm("hi", max_tokens=10)
            latency = round((time.time() - t0) * 1000)
            self._json({"ok": True, "latency_ms": latency,
                        "mode": mode, "model": llm_highlights.LLM_MODEL,
                        "reply": resp[:100]})
        except Exception as e:
            latency = round((time.time() - t0) * 1000)
            self._json({"ok": False, "latency_ms": latency,
                        "error": f"{type(e).__name__}: {str(e)[:200]}"})

    def _handle_bili(self):
        """POST /api/bili：保存 B 站 SESSDATA（不回显）。"""
        body = self._read_json()
        cfg = load_rooms()
        if body.get("sessdata"):
            cfg["bili_sessdata"] = body["sessdata"].strip()
            save_rooms(cfg)
        self._json({"ok": True})

    def _handle_notify(self):
        """保存 QQ 投稿通知配置（AstrBot 地址 / API Key / umo / 开关）。"""
        body = self._read_json()
        cfg = load_rooms()
        n = cfg.setdefault("notify", {})
        if "url" in body:
            n["url"] = body["url"].strip().rstrip("/")
        if body.get("api_key"):
            n["api_key"] = body["api_key"]
        if "umo" in body:
            n["umo"] = body["umo"].strip()
        if "enabled" in body:
            n["enabled"] = bool(body["enabled"])
        save_rooms(cfg)
        self._json({"ok": True})

    def _handle_notify_test(self):
        """发一条测试消息到 QQ，验证 AstrBot 链路。"""
        import urllib.request
        cfg = load_rooms().get("notify", {}) or {}
        url = (cfg.get("url", "") or "").rstrip("/")
        key = cfg.get("api_key", "")
        umo = cfg.get("umo", "")
        if not (url and key and umo):
            self._json({"ok": False, "error": "地址 / API Key / 通知目标未填全"})
            return
        payload = {"umo": umo, "message": "🔔 QQ 投稿通知测试：链路正常"}
        try:
            req = urllib.request.Request(
                url + "/api/v1/im/messages",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "X-API-Key": key})
            with _urlopen(req, timeout=30) as r:
                self._json({"ok": r.status == 200})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _handle_recorder(self):
        """POST /api/recorder：给房间添加 bililive 录制器（多平台）。"""
        import urllib.request
        body = self._read_json()
        rid = str(body.get("room_id", "")).strip()
        streamer = str(body.get("streamer", "")).strip()
        platform = str(body.get("platform", "")).strip() or "DouYu"
        if platform not in PLATFORMS:
            platform = "DouYu"
        if not rid:
            return self._json({"error": "房间号必填"}, 400)
        try:
            passkey = json.load(open(str(BASE / "config" / "appConfig.json")))["passKey"]
            cname, url_tpl, _ = PLATFORMS[platform]
            payload = {
                "providerId": platform, "channelId": rid,
                "remarks": f"{streamer}-{rid}" if streamer else rid,
                "channelURL": url_tpl.format(id=rid),
                "usedSource": "ws",
                "recorderType": "ffmpeg", "proxy": "http://198.19.0.1:3128",
                "noGlobalFollowFields": ["recorderType", "proxy"], "autoRecord": True,
            }
            if platform in PLATFORM_STREAM:
                payload["usedStream"] = PLATFORM_STREAM[platform]
            req = urllib.request.Request(
                "http://127.0.0.1:18010/recorder/add",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": passkey})
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.load(r)
            self._json({"ok": True, "id": j["payload"]["id"]})
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_recorder_stop(self):
        """POST /api/recorder/stop：停止指定房间的录制。"""
        import urllib.request
        body = self._read_json()
        rid = str(body.get("room_id", "")).strip()
        if not rid:
            return self._json({"error": "房间号必填"}, 400)
        try:
            recs = _get_recorders()
            rec = recs.get(rid)
            if not rec:
                return self._json({"error": "找不到录制器"}, 404)
            passkey = json.load(open(str(BASE / "config" / "appConfig.json")))["passKey"]
            req = urllib.request.Request(
                f"http://127.0.0.1:18010/recorder/{rec['id']}/stop",
                data=b"{}", method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": passkey})
            with urllib.request.urlopen(req, timeout=15):
                pass
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_recorder_start(self):
        """POST /api/recorder/start：启动指定房间的录制。"""
        import urllib.request
        body = self._read_json()
        rid = str(body.get("room_id", "")).strip()
        if not rid:
            return self._json({"error": "房间号必填"}, 400)
        try:
            recs = _get_recorders()
            rec = recs.get(rid)
            if not rec:
                return self._json({"error": "找不到录制器"}, 404)
            passkey = json.load(open(str(BASE / "config" / "appConfig.json")))["passKey"]
            req = urllib.request.Request(
                f"http://127.0.0.1:18010/recorder/{rec['id']}/start",
                data=b"{}", method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": passkey})
            with urllib.request.urlopen(req, timeout=15):
                pass
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_recorder_delete(self):
        """POST /api/recorder/delete：删除指定房间的录制器。"""
        import urllib.request
        body = self._read_json()
        rid = str(body.get("room_id", "")).strip()
        if not rid:
            return self._json({"error": "房间号必填"}, 400)
        try:
            recs = _get_recorders()
            rec = recs.get(rid)
            if not rec:
                return self._json({"error": "找不到录制器"}, 404)
            passkey = json.load(open(str(BASE / "config" / "appConfig.json")))["passKey"]
            req = urllib.request.Request(
                f"http://127.0.0.1:18010/recorder/{rec['id']}",
                method="DELETE",
                headers={"Authorization": passkey})
            with urllib.request.urlopen(req, timeout=15):
                pass
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_rooms(self):
        body = self._read_json()
        cfg = load_rooms()
        action = body.get("action")
        if action == "add":
            raw = str(body.get("room_id", "")).strip()
            platform = str(body.get("platform", "")).strip() or "DouYu"
            if platform not in PLATFORMS:
                platform = "DouYu"
            cname = PLATFORMS[platform][0]
            rid = self._extract_room_id(platform, raw)
            if not rid:
                return self._json({"error": "房间号必填"}, 400)
            if any(r.get("room_id") == rid and r.get("platform", "DouYu") == platform
                   for r in cfg["rooms"]):
                return self._json({"error": "房间已存在"}, 400)
            # 解析主播名：手动填的优先，否则走录制器 resolve，斗鱼失败时回退 betard
            st = str(body.get("streamer", "")).strip() or None
            info = None if st else self._resolve_channel(platform, rid)
            if info:
                st = (info.get("owner") or "").strip() or None
                # 用解析出的规范 channelId
                if info.get("channelId"):
                    rid = str(info["channelId"]).strip()
            if not st and platform == "DouYu":
                st = self._fetch_streamer(rid)
            if not st:
                return self._json({"error": "获取主播名失败，请检查链接/房间号"}, 400)
            segdir = os.path.join(os.environ.get("RECORD_BASE", "/home/hatch/Downloads"), cname, st)
            cfg["rooms"].append({
                "room_id": rid, "platform": platform, "streamer": st, "enabled": True,
                "segdir": segdir,
                "clip": {"win_sec": 180, "win_step": 60, "keep_score": 10,
                         "max_keep": 3, "retention_days": 1,
                         "min_clip": 180, "max_clip": 300},
                "ai": {"enabled": True},
            })
            save_rooms(cfg)
            return self._json({"ok": True, "streamer": st})
        elif action == "toggle":
            i = int(body.get("index", -1))
            cfg["rooms"][i]["enabled"] = not cfg["rooms"][i].get("enabled", True)
        elif action == "del":
            i = int(body.get("index", -1))
            cfg["rooms"].pop(i)
        elif action == "season":
            idx = body.get("index")
            sid = body.get("season_id")
            if isinstance(idx, int) and 0 <= idx < len(cfg["rooms"]):
                if sid:
                    cfg["rooms"][idx]["season_id"] = int(sid)
                else:
                    cfg["rooms"][idx].pop("season_id", None)
                save_rooms(cfg)
            return self._json({"ok": True})
        elif action == "clip":
            i = int(body.get("index", -1))
            cfg["rooms"][i]["clip"] = body.get("clip", {})
        else:
            return self._json({"error": "未知操作"}, 400)
        save_rooms(cfg)
        self._json({"ok": True})

    def _handle_ai(self):
        body = self._read_json()
        cfg = load_rooms()
        if "room_index" in body:
            i = int(body["room_index"])
            ai = cfg["rooms"][i].setdefault("ai", {})
            ai["enabled"] = not ai.get("enabled", True)
        else:
            g = cfg.setdefault("ai_global", {})
            if body.get("relay_url"):
                g["relay_url"] = body["relay_url"]
            if "proxy" in body:
                g["proxy"] = body["proxy"]
            if body.get("api_key"):
                g["api_key"] = body["api_key"]
            if body.get("provider"):
                g["provider"] = body["provider"]
            if body.get("model"):
                g["model"] = body["model"]
            if "prompt" in body:
                g["prompt"] = body["prompt"]
        save_rooms(cfg)
        self._json({"ok": True})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
