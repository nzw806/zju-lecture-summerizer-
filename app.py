# -*- coding: utf-8 -*-
"""
智云课堂课程总结 · 本地网页界面
双击 start.command / start.bat / start.sh，或运行: python3 app.py
然后浏览器打开 http://127.0.0.1:8765
"""
import glob
import json
import os
import re
import shutil
import sys
import threading
import traceback
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import pipeline
import browser_cookies
import classroom_client

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
PORT = 8765

JOBS = {}          # job_id -> {state, logs, result, error}
JOBS_LOCK = threading.Lock()
JOB_COUNTER = [0]
FF = {"state": "idle", "logs": [], "error": None, "path": None}
UPLOADS = {}       # upload_id -> {name, text, chars}
UPLOAD_COUNTER = [0]


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.chmod(CONFIG_PATH, 0o600)   # 里面存 key 和 cookie，仅限本人可读


def run_job(job_id, url, api_key, cookie, mode, video_url,
            summary_prompt, upload_id, extra_requirements, base_url, model,
            course_id, sub_id, tenant_code, course_name, session_name):
    def progress(msg):
        with JOBS_LOCK:
            JOBS[job_id]["logs"].append(msg)

    courseware = UPLOADS.get(upload_id, {}).get("text") if upload_id else None
    try:
        result = pipeline.run_pipeline(
            url, api_key=api_key or None, cookie=cookie or None,
            progress=progress, mode=mode, video_url=video_url or None,
            summary_system=summary_prompt or None,
            courseware_text=courseware,
            extra_requirements=extra_requirements or None,
            base_url=base_url, model=model,
            course_id=course_id, sub_id=sub_id, tenant_code=tenant_code,
            course_name=course_name, session_name=session_name)
        with JOBS_LOCK:
            JOBS[job_id].update(state="done", result=result)
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["logs"].append(f"出错：{e}")
            JOBS[job_id].update(state="error", error=str(e))
        traceback.print_exc()


def ff_job():
    try:
        p = pipeline.download_ffmpeg(
            progress=lambda m: FF["logs"].append(m))
        FF.update(state="done", path=p, error=None)
    except Exception as e:
        FF["logs"].append(str(e))
        FF.update(state="error", error=str(e))


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>智云课堂 · 课程总结</title>
<style>
  :root { --bg:#f6f4ee; --card:#fffdf8; --line:#e5ddc9;
          --gold:#a8842c; --gold-dark:#8a6b1e; --gold-soft:#f3ead2;
          --text:#2e2a20; --dim:#8c8270; --ok:#3d7a4f; --err:#b3402f; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,"PingFang SC","Microsoft YaHei",
         "Source Han Sans SC",sans-serif; background:var(--bg); color:var(--text); }
  .wrap { max-width:780px; margin:0 auto; padding:36px 20px 70px; }
  h1 { font-size:23px; margin:0 0 2px; font-weight:700; letter-spacing:.5px; }
  h1 .bar { display:inline-block; width:5px; height:20px; background:var(--gold);
            border-radius:2px; margin-right:10px; vertical-align:-2px; }
  .sub { color:var(--dim); font-size:13px; margin-bottom:26px; }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:10px; padding:22px; margin-bottom:16px;
          box-shadow:0 1px 3px rgba(120,100,40,.06); }
  label { display:block; font-size:13px; font-weight:600; margin:16px 0 6px; }
  label:first-child { margin-top:0; }
  input[type=text], input[type=password], input[type=date], select, textarea {
    width:100%; padding:10px 12px; font-size:14px; border:1px solid var(--line);
    border-radius:7px; background:#fffefb; outline:none; color:var(--text); }
  input:focus, textarea:focus { border-color:var(--gold); }
  textarea { resize:vertical; min-height:54px; font-family:ui-monospace,Menlo,
             Consolas,monospace; font-size:12px; line-height:1.6; }
  .hint { font-size:12px; color:var(--dim); margin-top:5px; line-height:1.7; }
  details { margin-top:8px; font-size:12px; color:var(--dim); }
  summary { cursor:pointer; }
  .row { display:flex; gap:12px; align-items:center; margin-top:18px;
         flex-wrap:wrap; }
  button { background:var(--gold); color:#fff; border:0; border-radius:7px;
           padding:11px 26px; font-size:15px; font-weight:600; cursor:pointer;
           letter-spacing:1px; }
  button:hover { background:var(--gold-dark); }
  button:disabled { background:#d8cba6; cursor:not-allowed; }
  button.sec { background:#fff; color:var(--gold-dark);
               border:1px solid var(--gold); padding:8px 16px; font-size:13px; }
  button.sec:hover { background:var(--gold-soft); }
  label.ck { display:flex; align-items:center; gap:6px; font-weight:400;
             font-size:13px; color:var(--dim); margin:10px 0 0; }
  .modes { border:1px solid var(--line); border-radius:8px; padding:4px 14px;
           background:#fffefb; }
  .modes label { display:flex; gap:8px; align-items:flex-start; margin:10px 0;
                 font-weight:400; font-size:13.5px; cursor:pointer; }
  .modes input { margin-top:3px; accent-color:var(--gold); }
  .modes .d { color:var(--dim); font-size:12px; display:block; margin-top:2px; }
  #mode3box { display:none; border-left:3px solid var(--gold);
              background:var(--gold-soft); border-radius:0 8px 8px 0;
              padding:12px 16px; margin-top:10px; }
  #logbox { display:none; }
  #logs { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px;
          line-height:1.9; white-space:pre-wrap; word-break:break-all;
          max-height:340px; overflow-y:auto; margin:0; color:#4a4232; }
  .badge { display:inline-block; font-size:12px; padding:2px 12px;
           border-radius:20px; margin-left:8px; vertical-align:2px; }
  .b-run { background:var(--gold-soft); color:var(--gold-dark); }
  .b-ok  { background:#e3efe6; color:var(--ok); }
  .b-err { background:#f7e4e0; color:var(--err); }
  #result { display:none; }
  #result a { color:var(--gold-dark); font-weight:600; text-decoration:none;
              border-bottom:1px solid var(--gold); }
  .path { font-family:ui-monospace,monospace; font-size:12px; color:var(--dim);
          word-break:break-all; background:#f4f0e4; padding:8px 10px;
          border-radius:6px; margin-top:10px; }
  h2 { font-size:15px; margin:0 0 10px; }
  .ok { color:var(--ok); font-weight:600; }
  .no { color:var(--err); font-weight:600; }
  .na { color:var(--dim); font-weight:600; }
  a.plain { color:var(--gold-dark); text-decoration:none; font-weight:400;
            margin-left:10px; border-bottom:1px dotted var(--gold); }
</style>
</head>
<body>
<div class="wrap">
  <h1><span class="bar"></span>智云课堂 · 课程总结</h1>
  <div class="sub">粘贴回放地址，自动抓取课堂内容并生成带时间戳的知识总结</div>

  <div class="card">
    <div class="row" style="margin:0 0 4px">
      <button type="button" id="diag" class="sec">环境自检</button>
      <span class="hint">第一次使用建议先运行，确认各项状态</span>
    </div>
    <div id="diagbox" class="hint" style="display:none;margin:12px 0 0;
         line-height:2.1"></div>
  </div>

  <div class="card" id="coursepicker">
    <h2>我的课程与课堂选择</h2>
    <div class="hint">选择日期后获取当天课程，再选择具体课堂；查询不会调用 AI。需要在下方 Cookie 框提供一次智云课堂登录态。</div>
    <div class="row" style="margin-top:12px">
      <input type="date" id="lecturedate" style="width:180px">
      <button type="button" id="loadcourses" class="sec">获取我的课程</button>
      <span class="hint" id="coursestatus"></span>
    </div>
    <label>课程</label>
    <select id="mycourses" disabled><option value="">请先获取我的课程</option></select>
    <label>课堂 / 回放候选</label>
    <select id="myreplays" disabled><option value="">请选择课程</option></select>
    <div class="row" style="margin-top:12px">
      <button type="button" id="checkreplay" class="sec" disabled>检查回放状态</button>
      <button type="button" id="usereplay" disabled>使用此课堂</button>
      <span class="hint" id="replaydetail"></span>
    </div>
    <input type="hidden" id="selected-course-id">
    <input type="hidden" id="selected-sub-id">
    <input type="hidden" id="selected-tenant-code" value="112">
    <input type="hidden" id="selected-course-name">
    <input type="hidden" id="selected-session-name">
  </div>

  <div class="card">
    <label>回放地址（备用，可不填）</label>
    <input type="text" id="url"
      placeholder="https://classroom.zju.edu.cn/livingroom?course_id=...&sub_id=...">

    <label>总结模式</label>
    <div class="modes">
      <label><input type="radio" name="mode" value="transcript">
        <span>只抓字幕<span class="d">不消耗额度，产出带时间戳的字幕全文；无需 API key</span></span>
      </label>
      <label><input type="radio" name="mode" value="summarize" checked>
        <span>字幕 + AI 总结<span class="d">默认模式：将平台字幕交给所选 AI 模型，整理成带时间戳的讲义</span></span>
      </label>
      <label><input type="radio" name="mode" value="enhanced">
        <span>增强识别 + AI 总结<span class="d">下载视频 → Whisper 重新识别音频 → 总结。
        专门改善口音重、平台字幕术语识别不准的问题；较慢，需 API key 与 ffmpeg</span></span>
      </label>
    </div>

    <div id="mode3box">
      <label style="margin-top:0">视频直链（可选）</label>
      <input type="text" id="videourl"
        placeholder="留空则尝试用 Cookie 自动获取；也可粘贴 .mp4 地址">
      <div class="row" style="margin-top:12px">
        <button type="button" id="getff" class="sec">下载 ffmpeg</button>
        <span class="hint" id="ffstate"></span>
      </div>
      <div class="hint" style="margin-top:8px">
        增强识别另需一次性安装识别引擎：终端运行
        <b>pip install faster-whisper</b>（首次运行还会自动下载约 3GB 的 large-v3 模型）。
        识别时会自动带入课程术语提示，减少专业词汇识别错误。</div>
    </div>

    <label>AI 接口地址（OpenAI 兼容 Base URL）</label>
    <input type="text" id="baseurl" placeholder="https://api.example.com/v1">
    <label>模型名称</label>
    <input type="text" id="model" placeholder="填写服务商支持的模型名称">
    <label>AI API Key</label>
    <input type="password" id="apikey" placeholder="服务商的 API key（「只抓字幕」模式可留空）">
    <div class="hint">总结时，字幕和参考课件文字会发送到上方指定的 AI 服务。密钥只保存在本机配置中。</div>
    <label class="ck"><input type="checkbox" id="savekey" checked>
      在本机记住接口、模型、key 与 cookie（config.json，请勿分享）</label>

    <label>Cookie（可选）
      <a class="plain" href="https://classroom.zju.edu.cn/" target="_blank">
        打开智云课堂登录页</a></label>
    <div class="row" style="margin-top:0">
      <select id="cookiebrowser" aria-label="登录所在浏览器"
        style="padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card)">
        <option value="chrome">Chrome（谷歌浏览器）</option>
        <option value="edge">Microsoft Edge</option>
      </select>
      <button type="button" id="autocookie" class="sec">尝试读取所选浏览器登录态</button>
      <span class="hint" id="ckstate"></span>
    </div>
    <textarea id="cookie"
      placeholder="或手动粘贴：浏览器 F12 → Network → 任一 classroom.zju.edu.cn 请求头里的 Cookie 整行"></textarea>
    <div class="hint">选择你实际登录智云课堂的浏览器。新版 Chrome / Edge 可能锁定数据库或使用应用绑定加密；此时需手动复制，不会强行绕过。</div>
    <details id="cookiehelp"><summary>Chrome / Edge 手动获取 Cookie（自动读取失败时使用）</summary>
      <div class="hint">① 在已经登录的 Chrome 或 Edge 中打开智云课堂回放页；
      ② 按 <b>F12</b>，选择 <b>网络（Network）</b>；③ 按 <b>F5</b> 刷新，
      点击域名为 <b>classroom.zju.edu.cn</b> 的请求（可筛选 Fetch/XHR）；
      ④ 在 <b>标头（Headers）→ 请求标头（Request Headers）</b> 找到 <b>Cookie</b>，
      右键该值选择复制值，再粘贴到上面的输入框。只复制 Cookie 的值，不要复制 Set-Cookie、整条 curl 命令或完整请求头。<br>
      Cookie 是登录凭证，不要发到聊天或分享给他人。仅用字幕总结时可先留空；
      获取视频直链与 PPT 文字层时才需要。</div>
    </details>

    <label>参考课件（可选）</label>
    <div class="row" style="margin-top:0">
      <input type="file" id="cwfile" accept=".pptx,.docx,.txt,.md"
        style="flex:1;min-width:200px;font-size:13px">
      <button type="button" id="upload" class="sec">上传课件</button>
    </div>
    <div class="hint" id="cwstate">支持 pptx / docx / txt / md。上传后提取文字，
      作为术语与章节结构参考注入总结，帮助抓住重点、纠正识别错误。</div>

    <label>额外要求（可选）</label>
    <textarea id="extra" rows="3"
      placeholder="在这里写任何特殊要求。例如：重点整理活动选择问题的两种证明；公式单独列成背诵表；用英文输出；每个知识点后附一道练习题……"></textarea>

    <details><summary>高级：总结提示词（已预设，可直接修改）</summary>
      <textarea id="sump" rows="11"></textarea>
    </details>

    <div class="row">
      <button id="go">开始</button>
      <span class="hint" id="state"></span>
    </div>
  </div>

  <div class="card" id="logbox">
    <h2>运行日志 <span class="badge b-run" id="badge">运行中</span></h2>
    <pre id="logs"></pre>
  </div>

  <div class="card" id="result">
    <h2>完成</h2>
    <div class="row" style="margin-top:0" id="resbtns"></div>
    <div class="hint" id="resnote" style="margin-top:10px"></div>
    <div class="path" id="resdir"></div>
  </div>
</div>

<script>
let timer = null, jobId = null, uploadId = '';

fetch('/api/config').then(r=>r.json()).then(c=>{
  document.getElementById('baseurl').value = c.base_url;
  document.getElementById('model').value = c.model;
  if (c.api_key) document.getElementById('apikey').value = c.api_key;
  if (c.cookie)  document.getElementById('cookie').value = c.cookie;
});
fetch('/api/prompts').then(r=>r.json()).then(p=>{
  document.getElementById('sump').value = p.summary;
});
fetch('/api/ffmpeg-status').then(r=>r.json()).then(j=>{
  showFF(j);
});

function setState(t){ document.getElementById('state').textContent = t; }
function mode(){ return document.querySelector('input[name=mode]:checked').value; }

document.querySelectorAll('input[name=mode]').forEach(r=>{
  r.onchange = () => {
    document.getElementById('mode3box').style.display =
      mode()==='enhanced' ? 'block' : 'none';
  };
});


let courseData = [], selectedSession = null, courseRequest = null;
const $ = id => document.getElementById(id);
function courseMessage(text, failed=false){ const el=$('coursestatus'); el.textContent=text; el.className=failed?'hint no':'hint'; }
function clearSelectedClassroom(){ selectedSession=null; ['selected-course-id','selected-sub-id','selected-course-name','selected-session-name'].forEach(id=>$(id).value=''); $('selected-tenant-code').value='112'; $('checkreplay').disabled=true; $('usereplay').disabled=true; $('replaydetail').textContent=''; }
function resetCourseSelects(){ $('mycourses').replaceChildren(new Option('请先获取我的课程','')); $('mycourses').disabled=true; $('myreplays').replaceChildren(new Option('请选择课程','')); $('myreplays').disabled=true; clearSelectedClassroom(); }
async function classroomPost(path, body){ const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,cookie:$('cookie').value.trim(),save:$('savekey').checked})}); const j=await r.json(); if(!r.ok||j.error) throw new Error(j.error||'智云课堂查询失败'); return j; }
function renderCourses(){ const sel=$('mycourses'); sel.replaceChildren(new Option(courseData.length?'请选择课程':'当天没有课程','')); courseData.forEach(c=>sel.add(new Option(c.name+(c.teacher?' · '+c.teacher:''),c.id))); sel.disabled=!courseData.length; $('myreplays').replaceChildren(new Option('请选择课程','')); $('myreplays').disabled=true; clearSelectedClassroom(); }
function renderSessions(){ clearSelectedClassroom(); const c=courseData.find(x=>x.id===$('mycourses').value); const sel=$('myreplays'); const rows=c?c.sessions:[]; sel.replaceChildren(new Option(rows.length?'请选择一节课堂':'该课程没有课堂记录','')); rows.forEach(s=>sel.add(new Option(s.name+(s.teacher?' · '+s.teacher:''),s.sub_id))); sel.disabled=!rows.length; }
async function checkReplay(){ const c=courseData.find(x=>x.id===$('mycourses').value); const s=c&&c.sessions.find(x=>x.sub_id===$('myreplays').value); if(!c||!s)return; selectedSession={course:c,session:s}; $('checkreplay').disabled=true; $('usereplay').disabled=true; $('replaydetail').textContent='查询回放状态中…'; try{ const j=await classroomPost('/api/classroom/replay-status',{course_id:c.id,sub_id:s.sub_id,tenant_code:s.tenant_code}); const label=j.status==='available'?'已检测到回放内容':j.status==='unavailable'?'未检测到保存回放内容':'回放状态未知，可先尝试字幕总结'; $('replaydetail').textContent=label+(j.content_count==null?'':`（${j.content_count} 项）`); $('checkreplay').disabled=false; $('usereplay').disabled=j.status==='unavailable'; }catch(e){ $('replaydetail').textContent='状态查询失败：'+e.message; $('checkreplay').disabled=false; $('usereplay').disabled=false; } }
$('loadcourses').onclick=async()=>{ if(courseRequest)courseRequest.abort(); courseRequest=new AbortController(); resetCourseSelects(); const day=$('lecturedate').value; if(!day){courseMessage('请先选择日期',true);return;} if(!$('cookie').value.trim()){courseMessage('请先在 Cookie 框粘贴智云课堂登录态',true); $('cookiehelp').open=true; $('cookie').focus(); return;} $('loadcourses').disabled=true; courseMessage('正在查询当天的我的课程…'); const timer=setTimeout(()=>courseRequest.abort(),45000); try{ const j=await classroomPost('/api/classroom/courses',{date:day}); courseData=j.courses||[]; renderCourses(); courseMessage(courseData.length?`找到 ${courseData.length} 门课程，请选择课程。`:'当天没有查询到课程，可切换日期。'); }catch(e){courseMessage(e.name==='AbortError'?'查询超时，请稍后重试。':e.message,true);}finally{clearTimeout(timer);$('loadcourses').disabled=false;} };
$('mycourses').onchange=renderSessions;
$('myreplays').onchange=()=>{ const c=courseData.find(x=>x.id===$('mycourses').value); const s=c&&c.sessions.find(x=>x.sub_id===$('myreplays').value); clearSelectedClassroom(); if(c&&s){selectedSession={course:c,session:s};$('checkreplay').disabled=false;checkReplay();} };
$('checkreplay').onclick=checkReplay;
$('usereplay').onclick=()=>{ if(!selectedSession)return; const c=selectedSession.course,s=selectedSession.session; $('selected-course-id').value=c.id; $('selected-sub-id').value=s.sub_id; $('selected-tenant-code').value=s.tenant_code||'112'; $('selected-course-name').value=c.name; $('selected-session-name').value=s.name; $('url').value=''; courseMessage('已选择课堂；点击下方“开始”生成字幕或总结。'); $('go').scrollIntoView({behavior:'smooth',block:'center'}); };
$('lecturedate').value=new Date().toISOString().slice(0,10);
$('cookie').addEventListener('input',resetCourseSelects);
$('autocookie').addEventListener('click',resetCourseSelects);

function showFF(j){
  const el = document.getElementById('ffstate');
  if (j.path)      el.innerHTML = '<span class="ok">ffmpeg 已就绪</span>：' + j.path;
  else if (j.state==='running') el.textContent = '下载中… ' + (j.logs.slice(-1)[0]||'');
  else if (j.state==='error')   el.innerHTML = '<span class="no">下载失败</span>：' + j.error;
  else el.innerHTML = '<span class="na">未检测到 ffmpeg</span>（增强识别模式必需）';
}

document.getElementById('getff').onclick = async () => {
  document.getElementById('getff').disabled = true;
  await fetch('/api/ffmpeg', {method:'POST'});
  const t = setInterval(async ()=>{
    const j = await (await fetch('/api/ffmpeg-status')).json();
    showFF(j);
    if (j.state!=='running'){ clearInterval(t);
      document.getElementById('getff').disabled = false; }
  }, 1500);
};

document.getElementById('autocookie').onclick = async () => {
  const btn = document.getElementById('autocookie');
  const st = document.getElementById('ckstate');
  btn.disabled = true;
  st.className = 'hint';
  st.textContent = '读取中…（仅 macOS 可能出现钥匙串授权）';
  const browser = document.getElementById('cookiebrowser').value;
  try {
    const r = await fetch('/api/autocookie?browser=' + encodeURIComponent(browser));
    const j = await r.json();
    if (j.error){
      st.className = 'hint no'; st.textContent = j.error;
      document.getElementById('cookiehelp').open = true;
    } else {
      document.getElementById('cookie').value = j.cookie;
      st.className = 'hint ok'; st.textContent = '已读取 ' + j.count + ' 个 Cookie';
    }
  } catch(e){
    st.className = 'hint no'; st.textContent = '读取失败，请按下方步骤手动复制 Cookie';
    document.getElementById('cookiehelp').open = true;
  } finally { btn.disabled = false; }
};

document.getElementById('upload').onclick = async () => {
  const f = document.getElementById('cwfile').files[0];
  const st = document.getElementById('cwstate');
  if (!f){ st.innerHTML = '<span class="no">请先选择课件文件</span>'; return; }
  const btn = document.getElementById('upload');
  btn.disabled = true; st.textContent = '上传并提取文字中…';
  try {
    const fd = new FormData();
    fd.append('file', f);
    const r = await fetch('/api/upload', {method:'POST', body:fd});
    const j = await r.json();
    if (j.error){ st.innerHTML = '<span class="no">上传失败</span>：' + j.error; }
    else {
      uploadId = j.upload_id;
      st.innerHTML = '<span class="ok">已上传 ' + j.name + '</span>，提取到 '
        + j.chars + ' 字，将作为参考注入总结';
    }
  } catch(e){ st.innerHTML = '<span class="no">' + e + '</span>'; }
  btn.disabled = false;
};

const DIAG_LABELS = {python:'Python 版本', zju:'智云课堂连通性',
  ai:'AI 接口与模型', edge:'浏览器登录态', ffmpeg:'ffmpeg'};
document.getElementById('diag').onclick = async () => {
  const box = document.getElementById('diagbox');
  box.style.display = 'block'; box.textContent = '检查中…';
  const r = await fetch('/api/diagnose');
  const j = await r.json();
  box.innerHTML = Object.entries(j).map(([k, v]) => {
    const tag = v.ok === true ? '<span class="ok">[通过]</span>'
              : v.ok === false ? '<span class="no">[未通过]</span>'
              : '<span class="na">[可选]</span>';
    return tag + ' <b>' + (DIAG_LABELS[k]||k) + '</b> — ' + v.detail;
  }).join('<br>');
};

document.getElementById('go').onclick = async () => {
  const url = document.getElementById('url').value.trim();
  const courseId = document.getElementById('selected-course-id').value;
  const subId = document.getElementById('selected-sub-id').value;
  if (!url && !subId){ setState('请先选择一节课堂，或粘贴回放地址'); return; }
  const body = {
    url,
    course_id: courseId,
    sub_id: subId,
    tenant_code: document.getElementById('selected-tenant-code').value || '112',
    course_name: document.getElementById('selected-course-name').value,
    session_name: document.getElementById('selected-session-name').value,
    mode: mode(),
    video_url: document.getElementById('videourl').value.trim(),
    api_key: document.getElementById('apikey').value.trim(),
    base_url: document.getElementById('baseurl').value.trim(),
    model: document.getElementById('model').value.trim(),
    cookie:  document.getElementById('cookie').value.trim(),
    save:    document.getElementById('savekey').checked,
    summary_prompt: document.getElementById('sump').value.trim(),
    upload_id: uploadId,
    extra_requirements: document.getElementById('extra').value.trim(),
  };
  const r = await fetch('/api/run', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const j = await r.json();
  if (j.error){ setState(j.error); return; }
  jobId = j.job_id;
  document.getElementById('go').disabled = true;
  document.getElementById('logbox').style.display = 'block';
  document.getElementById('result').style.display = 'none';
  document.getElementById('logs').textContent = '';
  setBadge('运行中','b-run'); setState('');
  timer = setInterval(poll, 1000);
};

function setBadge(t, c){
  const b = document.getElementById('badge');
  b.textContent = t; b.className = 'badge ' + c;
}

async function poll(){
  const r = await fetch('/api/status?job=' + jobId);
  const j = await r.json();
  document.getElementById('logs').textContent = j.logs.join('\\n');
  document.getElementById('logs').scrollTop = 1e9;
  if (j.state === 'running') return;
  clearInterval(timer);
  document.getElementById('go').disabled = false;
  if (j.state === 'done'){
    setBadge('完成','b-ok');
    const res = j.result || {};
    document.getElementById('result').style.display = 'block';
    const btns = document.getElementById('resbtns');
    const note = document.getElementById('resnote');
    if (res.summary_path){
      btns.innerHTML =
        '<a href="/api/download?job='+jobId+'&what=summary"><button>下载总结文档</button></a>'
        + '<a href="/api/download?job='+jobId+'&what=zip"><button class="sec">下载全部资料（zip）</button></a>'
        + '<a class="plain" href="/api/file?job='+jobId+'" target="_blank">在线预览</a>';
      note.textContent = '共 ' + (res.segments||'') + ' 段字幕，时长约 '
        + (res.duration||'') + '；token 消耗：输入 '
        + ((res.usage||{}).prompt_tokens||0) + '，输出 '
        + ((res.usage||{}).completion_tokens||0);
    } else {
      btns.innerHTML =
        '<a href="/api/download?job='+jobId+'&what=transcript"><button>下载字幕全文</button></a>';
      note.textContent = '本次只抓取了字幕（未填 key 或选择了只抓字幕模式）。';
    }
    document.getElementById('resdir').textContent = '服务器暂存目录：' + (res.result_dir||'');
  } else {
    setBadge('出错','b-err');
    setState(j.error || '运行失败，详见日志');
  }
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def log_message(self, *a):   # 静默访问日志
        pass

    def _diagnose(self):
        """环境自检：逐项返回 ok(true/false/null=可选) + 说明。"""
        checks = {}
        v = sys.version_info
        checks["python"] = {
            "ok": v >= (3, 9),
            "detail": f"Python {v.major}.{v.minor}.{v.micro}"}
        try:
            req = urllib.request.Request(
                "https://classroom.zju.edu.cn/",
                headers={"User-Agent": pipeline.UA})
            urllib.request.urlopen(req, timeout=10, context=pipeline.SSL_CTX)
            checks["zju"] = {"ok": True, "detail": "智云课堂可访问"}
        except Exception as e:
            checks["zju"] = {"ok": False, "detail": f"无法访问：{e}"}
        cfg = load_config()
        key = cfg.get("api_key", "")
        if not key:
            checks["ai"] = {"ok": None,
                                  "detail": "未配置 key（「只抓字幕」可用；"
                                            "总结模式需要 key）"}
        else:
            try:
                pipeline.llm_chat(key, "你是助手", "只回复：ok", max_tokens=64,
                                  base_url=cfg.get("base_url"), model=cfg.get("model"),
                                  retries=1, timeout=30)
                checks["ai"] = {"ok": True, "detail": "接口和模型可用，密钥验证通过"}
            except Exception as e:
                checks["ai"] = {"ok": False, "detail": str(e)}
        found = []
        for browser in ("chrome", "edge"):
            try:
                if browser_cookies.find_cookie_databases(browser):
                    found.append(browser_cookies.BROWSER_NAMES[browser])
            except browser_cookies.CookieReadError:
                pass
        checks["edge"] = {"ok": None,
                          "detail": ("检测到 " + " / ".join(found) + " 配置；未读取登录凭证，能否自动读取需单独尝试"
                                     if found else "未找到浏览器配置，可手动粘贴 Cookie；不影响只用字幕总结")}
        ff = pipeline.ffmpeg_path()
        if ff:
            checks["ffmpeg"] = {"ok": True, "detail": f"已就绪：{ff}"}
        else:
            checks["ffmpeg"] = {
                "ok": False,
                "detail": "未安装。装上它才能用「增强识别」模式——从视频抽音频、"
                          "由 Whisper 重新识别，显著提高口音重或平台字幕"
                          "术语识别不准时的总结质量。可在下方点「下载 ffmpeg」"}
        return checks

    # ---------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/config":
            cfg = load_config()
            self._json({"api_key": cfg.get("api_key", ""),
                        "cookie": cfg.get("cookie", ""),
                        "base_url": cfg.get("base_url") or pipeline.DEFAULT_BASE_URL,
                        "model": cfg.get("model") or pipeline.DEFAULT_MODEL})
        elif u.path == "/api/prompts":
            self._json({"summary": pipeline.SUMMARY_SYSTEM})
        elif u.path == "/api/ffmpeg-status":
            self._json({"state": FF["state"], "logs": FF["logs"][-3:],
                        "error": FF["error"],
                        "path": FF["path"] or pipeline.ffmpeg_path()})
        elif u.path == "/api/diagnose":
            self._json(self._diagnose())
        elif u.path == "/api/autocookie":
            try:
                browser = q.get("browser", ["chrome"])[0]
                cookie = browser_cookies.read_browser_cookies(browser, "classroom.zju.edu.cn")
                self._json({"cookie": cookie, "count": len(cookie.split("; "))})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif u.path == "/api/status":
            jid = q.get("job", [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if not job:
                    self._json({"error": "任务不存在"}, 404)
                    return
                out = {"state": job["state"], "logs": list(job["logs"]),
                       "error": job.get("error"), "result": job.get("result")}
            self._json(out)
        elif u.path == "/api/file":
            jid = q.get("job", [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
                path = (job or {}).get("result", {}).get("summary_path") \
                    if job else None
            if not path or not os.path.isfile(path):
                self._json({"error": "文件不存在"}, 404)
                return
            with open(path, encoding="utf-8") as f:
                text = f.read()
            html = ("<meta charset='utf-8'><title>知识总结</title>"
                    "<body style='max-width:820px;margin:24px auto;"
                    "font-family:-apple-system,PingFang SC,sans-serif;"
                    "line-height:1.7;padding:0 16px;color:#2e2a20'>"
                    "<pre style='white-space:pre-wrap;font-family:inherit'>"
                    + text.replace("&", "&amp;").replace("<", "&lt;")
                    + "</pre></body>")
            self._send(200, html, "text/html; charset=utf-8")
        elif u.path == "/api/download":
            jid = q.get("job", [""])[0]
            what = q.get("what", ["summary"])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
                res = (job or {}).get("result") or {}
            rdir = res.get("result_dir")
            if not rdir or not os.path.isdir(rdir):
                self._json({"error": "任务结果不存在"}, 404)
                return
            if what == "zip":
                import io
                import zipfile
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for root, _dirs, files in os.walk(rdir):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            z.write(fp, os.path.relpath(fp, rdir))
                data = buf.getvalue()
                fname = os.path.basename(rdir) + ".zip"
                ctype = "application/zip"
            else:
                path = res.get("summary_path") if what == "summary" \
                    else res.get("transcript_path")
                if not path or not os.path.isfile(path):
                    self._json({"error": "文件不存在"}, 404)
                    return
                with open(path, "rb") as f:
                    data = f.read()
                fname = os.path.basename(path)
                ctype = "text/markdown; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(fname)}")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"error": "not found"}, 404)

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            raise ValueError("请求体解析失败") from None

    # ---------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        if u.path in ("/api/classroom/courses", "/api/classroom/replay-status"):
            try:
                body = self._read_json_body()
                cookie = (body.get("cookie") or "").strip()
                if not cookie:
                    raise classroom_client.ClassroomAuthError("请先粘贴智云课堂 Cookie")
                if body.get("save"):
                    cfg = load_config()
                    cfg["cookie"] = cookie
                    save_config(cfg)
                if u.path.endswith("/courses"):
                    courses = classroom_client.get_my_course_day(body.get("date"), cookie)
                    self._json({"courses": courses})
                else:
                    status = classroom_client.get_sub_info(
                        body.get("course_id"), body.get("sub_id"), cookie,
                        body.get("tenant_code") or classroom_client.DEFAULT_TENANT)
                    self._json(status)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except classroom_client.ClassroomAuthError as exc:
                self._json({"error": str(exc)}, 401)
            except classroom_client.ClassroomError as exc:
                self._json({"error": str(exc)}, 502)
            return
        if u.path == "/api/upload":
            try:
                ctype = self.headers.get("Content-Type", "")
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                import email
                msg = email.message_from_bytes(
                    b"Content-Type: " + ctype.encode() + b"\r\n"
                    b"MIME-Version: 1.0\r\n\r\n" + body)
                fn, data = None, None
                if msg.is_multipart():
                    for part in msg.get_payload():
                        if part.get_filename():
                            fn = part.get_filename()
                            data = part.get_payload(decode=True)
                            break
                if not fn or not data:
                    self._json({"error": "没收到文件"}, 400)
                    return
                up_dir = os.path.join(HERE, "uploads")
                os.makedirs(up_dir, exist_ok=True)
                safe = re.sub(r"[^\w.\-]", "_", os.path.basename(fn))
                saved = os.path.join(up_dir,
                                     f"{datetime.now():%H%M%S}_{safe}")
                with open(saved, "wb") as f:
                    f.write(data)
                text = pipeline.extract_courseware(saved)
                if not text.strip():
                    self._json({"error": "课件里没提取到文字"}, 400)
                    return
                UPLOAD_COUNTER[0] += 1
                uid = f"up{UPLOAD_COUNTER[0]}"
                UPLOADS[uid] = {"name": fn, "text": text,
                                "chars": len(text)}
                self._json({"upload_id": uid, "name": fn,
                            "chars": len(text)})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if u.path == "/api/ffmpeg":
            if FF["state"] == "running":
                self._json({"error": "已在下载中"})
                return
            FF.update(state="running", logs=[], error=None, path=None)
            threading.Thread(target=ff_job, daemon=True).start()
            self._json({"ok": True})
            return
        if u.path != "/api/run":
            self._json({"error": "not found"}, 404)
            return
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        url = str(body.get("url") or "").strip()
        course_id = str(body.get("course_id") or "").strip()
        sub_id = str(body.get("sub_id") or "").strip()
        if not sub_id and not re.search(r"sub_id=", url):
            self._json({"error": "请先选择一节课堂，或粘贴完整的 livingroom 回放链接"}, 400)
            return
        if sub_id and not course_id:
            self._json({"error": "已选择课堂但缺少课程 ID，请重新选择"}, 400)
            return
        if sub_id and (not re.fullmatch(r"[0-9]+", course_id)
                       or not re.fullmatch(r"[0-9]+", sub_id)):
            self._json({"error": "课程 ID 和课次 ID 必须是数字"}, 400)
            return
        mode = body.get("mode") or "summarize"
        if mode not in ("transcript", "summarize", "enhanced"):
            self._json({"error": "未知模式"}, 400)
            return
        api_key = (body.get("api_key") or "").strip()
        cookie = (body.get("cookie") or "").strip()
        cfg = load_config()
        base_url = (body.get("base_url") or cfg.get("base_url")
                    or pipeline.DEFAULT_BASE_URL).strip()
        model = (body.get("model") or cfg.get("model") or pipeline.DEFAULT_MODEL).strip()
        if body.get("save"):
            cfg.update(api_key=api_key, cookie=cookie, base_url=base_url, model=model)
            save_config(cfg)

        with JOBS_LOCK:
            JOB_COUNTER[0] += 1
            jid = f"job{JOB_COUNTER[0]}"
            JOBS[jid] = {"state": "running", "logs": [], "result": None,
                         "error": None}
        t = threading.Thread(
            target=run_job,
            args=(jid, url, api_key, cookie, mode,
                  (body.get("video_url") or "").strip(),
                  (body.get("summary_prompt") or "").strip(),
                  (body.get("upload_id") or "").strip(),
                  (body.get("extra_requirements") or "").strip(),
                  base_url, model, course_id, sub_id,
                  str(body.get("tenant_code") or "112").strip(),
                  str(body.get("course_name") or "").strip(),
                  str(body.get("session_name") or "").strip()),
            daemon=True)
        t.start()
        self._json({"job_id": jid})


def main():
    server, port = None, PORT
    for cand in range(PORT, PORT + 6):        # 端口被占就自动顺延
        try:
            server = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            port = cand
            break
        except OSError:
            continue
    if server is None:
        print(f"端口 {PORT}-{PORT + 5} 全被占用，请关掉占用程序后重试")
        return
    addr = f"http://127.0.0.1:{port}"
    print(f"智云课堂课程总结已启动：{addr}")
    print("按 Ctrl+C 停止")
    threading.Timer(0.8, lambda: webbrowser.open(addr)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
