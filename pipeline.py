# -*- coding: utf-8 -*-
"""
智云课堂回放 → 课程知识总结 流水线
只用 Python 标准库。核心输入是语音字幕（公开接口），可选增强是登录 Cookie 抓 PPT。
"""
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime

import secure_store

HERE = os.path.dirname(os.path.abspath(__file__))


def _make_ssl_context():
    """macOS 官方 Python 常缺根证书：优先 certifi，其次系统默认，最后退化为不校验
    （字幕接口为公开只读数据，退化可接受，但会打印提示）。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        import socket
        with socket.create_connection(("classroom.zju.edu.cn", 443), 10) as sock:
            # 必须 wrap 这个已连接的 sock 才会真正握手；握手成功才说明证书可用
            with ctx.wrap_socket(sock, server_hostname="classroom.zju.edu.cn"):
                return ctx
    except Exception:
        pass
    print("提示：本机 Python 缺少根证书，已退化为不校验 HTTPS 证书。"
          "建议运行一次 /Applications/Python 3.13/Install Certificates.command")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


SSL_CTX = _make_ssl_context()

BASE = "https://classroom.zju.edu.cn"
INTERACTIVE_BASE = "https://interactivemeta.cmc.zju.edu.cn"   # 备用后端
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# API 提供商预设（全部为 OpenAI 兼容 /chat/completions 格式；
# 模型名以各家官网为准，界面/自定义均可覆盖）
PROVIDERS = {
    "deepseek": {"label": "DeepSeek（默认）", "base": DEEPSEEK_URL,
                 "chat": "deepseek-chat", "vision": "deepseek-flash"},
    "zhipu": {"label": "智谱 GLM（open.bigmodel.cn）",
              "base": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
              "chat": "glm-4.7", "vision": "glm-4v-flash"},
    "qwen": {"label": "通义千问（阿里云百炼）",
             "base": "https://dashscope.aliyuncs.com/compatible-mode"
                     "/v1/chat/completions",
             "chat": "qwen-plus", "vision": "qwen-vl-plus"},
    "openai": {"label": "OpenAI",
               "base": "https://api.openai.com/v1/chat/completions",
               "chat": "gpt-4o-mini", "vision": "gpt-4o-mini"},
    "custom": {"label": "自定义（任意 OpenAI 兼容端点）",
               "base": "", "chat": "", "vision": ""},
}


def resolve_api(provider):
    """把界面传来的 provider dict（可为 None）补全为
    (base_url, chat_model, vision_model)。"""
    p = provider or {}
    pid = p.get("id") or "deepseek"
    preset = PROVIDERS.get(pid) or PROVIDERS["deepseek"]
    chat = p.get("chat") or preset["chat"]
    # 视觉模型留空时回落到文本模型（若该模型不支持图片，转写会逐页失败
    # 并跳过，不影响其余流程）
    vision = p.get("vision") or preset["vision"] or chat
    return (p.get("base") or preset["base"], chat, vision)


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class PipelineError(Exception):
    pass


def _log(progress, msg):
    if progress:
        progress(msg)
    else:
        print(msg)


# ---------------------------------------------------------------- URL 解析

def parse_url(url):
    """从 livingroom 回放 URL 里提取 course_id / sub_id / tenant_code。"""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except Exception:
        raise PipelineError("URL 无法解析，请粘贴浏览器地址栏的完整链接")
    sub_id = q.get("sub_id", [None])[0]
    if not sub_id:
        raise PipelineError("URL 里找不到 sub_id 参数，请确认是 livingroom 回放页地址")
    return {
        "course_id": q.get("course_id", [""])[0],
        "sub_id": sub_id,
        "tenant_code": q.get("tenant_code", ["112"])[0],
    }


# ---------------------------------------------------------------- 字幕抓取

def fetch_transcript(sub_id, tenant_code="112", cookie=None, timeout=60):
    """抓平台语音字幕。返回 [(BeginSec, Text), ...] 按时间排序。
    2026-11 起平台收紧：匿名请求一律 403，必须登录态（Cookie + Bearer）。
    有 cookie 时先匿名试一次（保留万一），失败自动带登录态重试，
    并回退到 interactive 备用后端（对齐参考项目）。"""
    path = (f"/courseapi/v3/web-socket/search-trans-result"
            f"?sub_id={urllib.parse.quote(str(sub_id))}"
            f"&tenant_code={tenant_code}")
    attempts = [(BASE + path, {"User-Agent": UA})]
    if cookie:
        for host in (BASE, INTERACTIVE_BASE):
            attempts.append((host + path, ppt_auth_headers(cookie, host)))
    data, last_err = None, None
    for url, headers in attempts:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                                context=SSL_CTX) as r:
                d = json.loads(r.read().decode("utf-8"))
            if d.get("code") == 0 and d.get("list"):
                data = d
                break
            last_err = f"code={d.get('code')}"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
        except Exception as e:
            last_err = str(e)
    if not data:
        if cookie:
            raise PipelineError(
                f"字幕接口抓取失败（{last_err}）：登录态可能已过期或该课无权限，"
                "请点「登录智云课堂」重新登录后再试")
        raise PipelineError(
            f"字幕接口抓取失败（{last_err}）。平台已不再匿名开放字幕，"
            "请先点「登录智云课堂」登录后再运行")
    segs = data["list"][0].get("all_content") or []
    segs = [(int(s["BeginSec"]), (s.get("Text") or "").strip())
            for s in segs if (s.get("Text") or "").strip()]
    segs.sort(key=lambda x: x[0])
    if not segs:
        raise PipelineError("字幕内容为空（可能直播尚未结束或字幕未生成）")
    return segs


def fmt_ts(sec):
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def build_transcript_texts(segs):
    """生成逐段版和按分钟合并版两份文本。"""
    per_seg = "\n".join(f"[{fmt_ts(t)}] {txt}" for t, txt in segs)
    minutes = {}
    for t, txt in segs:
        minutes.setdefault(t // 60, []).append(txt)
    per_min = "\n".join(f"[{m // 60:02d}:{m % 60:02d}] " + "".join(v)
                        for m, v in sorted(minutes.items()))
    return per_seg, per_min, minutes


# ---------------------------------------------------------------- 可选：PPT

def try_fetch_ppt_text(sub_id, tenant_code, cookie, timeout=30):
    """带 Cookie 尝试抓平台 PPT 文字层。失败返回 None（不影响主流程）。"""
    if not cookie:
        return None
    url = (f"{BASE}/pptnote/v1/schedule/search-ppt"
           f"?sub_id={sub_id}&tenant_code={tenant_code}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Cookie": cookie,
        "Referer": f"{BASE}/livingroom?sub_id={sub_id}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                                context=SSL_CTX) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("code") == 0 and data.get("list"):
            return data["list"]
    except Exception:
        pass
    return None


# ------------------------------------------------------- PPT 幻灯片（图片）

# 两个官方后端：主站优先，返回空时回退到 interactive（对齐参考项目）
PPT_ORIGINS = [(BASE, "/pptnote"),
               (INTERACTIVE_BASE, "/pptnoteapi")]
# 幻灯片图片只允许这些域名（防止把 Cookie/请求发给意外主机）
_IMG_HOST_SUFFIX = (".zju.edu.cn", ".chinamcloud.com",
                    ".chinamcloud.cn", ".chinamcache.com")


def bearer_from_cookie(cookie):
    """从 Cookie 里 _token 解出 Authorization 用的 Bearer token。
    _token 是 URL 编码的 PHP 序列化串：{i:0;s:6:"_token";i:1;s:N:"<token>";}
    解不出返回空串。"""
    if not cookie:
        return ""
    m = re.search(r"(?:^|;\s*)_token=([^;]*)", cookie)
    if not m:
        return ""
    raw = urllib.parse.unquote(m.group(1))
    m2 = re.search(r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"(.+?)";\}', raw)
    return m2.group(1) if m2 else ""


def ppt_auth_headers(cookie, origin=BASE):
    h = {"User-Agent": UA, "Cookie": cookie,
         "Referer": origin + "/", "Accept": "application/json"}
    token = bearer_from_cookie(cookie)
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _safe_image_url(value):
    """校验并规范化幻灯片图片地址：仅白名单域名、强制 https。非法返回空串。"""
    if not value:
        return ""
    m = re.match(r"^https?://([^/:?#]+)(?::\d+)?(/[^\s]*)?$", value, re.I)
    if not m or "@" in value:
        return ""
    host = m.group(1).lower()
    if not any(host.endswith(s) for s in _IMG_HOST_SUFFIX):
        return ""
    return re.sub(r"^http:", "https:", value, flags=re.I)


def _parse_ppt_item(p):
    """把 search-ppt 的一条记录解析成 {t_sec, created, img, thumb, note}。"""
    content = {}
    raw = p.get("content")
    if isinstance(raw, str) and raw.strip():
        try:
            content = json.loads(raw)
        except Exception:
            content = {}
    if not content and isinstance(p.get("ppt"), dict):
        content = p["ppt"]
    img = _safe_image_url(content.get("pptimgurl") or "")
    if not img:
        return None
    thumb = _safe_image_url(content.get("pptthumb") or "") or img
    note = re.sub(r"\s+", " ", (content.get("content") or "")).strip()
    created = content.get("created") or 0
    try:
        created = int(created)
    except (TypeError, ValueError):
        created = 0
    t_sec = p.get("created_sec")
    try:
        t_sec = int(t_sec) if t_sec is not None else None
    except (TypeError, ValueError):
        t_sec = None
    return {"t_sec": t_sec, "created": created,
            "img": img, "thumb": thumb, "note": note}


def fetch_ppt_slides(course_id, sub_id, cookie, progress=None, timeout=30):
    """抓取整节课的 PPT 幻灯片列表（翻页 + 双后端回退）。
    返回 [slide, ...]；无课件或失败返回 []。鉴权失败抛 PipelineError。"""
    if not cookie:
        return []
    headers = ppt_auth_headers(cookie, BASE)
    for origin, prefix in PPT_ORIGINS:
        slides, page, received, total = [], 1, 0, None
        broken = False
        while page <= 200:
            url = (f"{origin}{prefix}/v1/schedule/search-ppt"
                   f"?course_id={urllib.parse.quote(str(course_id))}"
                   f"&sub_id={urllib.parse.quote(str(sub_id))}"
                   f"&page={page}&per_page=100")
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=SSL_CTX) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and origin == BASE:
                    raise PipelineError(
                        f"PPT 接口鉴权失败（HTTP {e.code}），登录态可能已过期，"
                        "请重新登录。")
                broken = True
                break
            except Exception:
                broken = True
                break
            if data.get("code") != 0:
                broken = True
                break
            lst = data.get("list") or []
            total = data.get("total")
            for p in lst:
                s = _parse_ppt_item(p)
                if s:
                    slides.append(s)
            received += len(lst)
            if not lst or (total is not None
                           and received >= int(total or 0)):
                break
            page += 1
        if slides:
            _log(progress, f"   PPT 来源：{origin}，共 {len(slides)} 页")
            return slides
        if broken and origin == BASE:
            continue           # 主站异常，试回退后端
    return []


def _img_ext(url):
    path = urllib.parse.urlparse(url).path.lower()
    for e in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if path.endswith(e):
            return ".jpg" if e == ".jpeg" else e
    return ".jpg"


def download_slides(slides, dest_dir, progress=None):
    """把幻灯片图片逐张下载到 dest_dir，并生成 manifest.json / manifest.md。
    返回 manifest（list）。已存在的图片跳过重复下载。"""
    os.makedirs(dest_dir, exist_ok=True)
    manifest = []
    n = len(slides)
    for i, s in enumerate(slides, 1):
        ext = _img_ext(s["img"])
        t = s.get("t_sec")
        stem = f"{i:03d}" + (f"_t{int(t)}s" if t is not None else "")
        fname = stem + ext
        fpath = os.path.join(dest_dir, fname)
        status = "ok"
        if not os.path.isfile(fpath) or os.path.getsize(fpath) == 0:
            try:
                download_file(s["img"], fpath, timeout=120)
            except Exception as e:
                status = f"下载失败：{e}"
        if progress and (i % 10 == 0 or i == n):
            progress(f"   下载 PPT 图片：{i}/{n}")
        manifest.append({"index": i, "file": fname, "t_sec": t,
                         "created": s.get("created"), "note": s.get("note", ""),
                         "img": s["img"], "status": status})
    with open(os.path.join(dest_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    lines = ["# PPT 幻灯片清单", "",
             f"> 共 {n} 页。时间戳为该页在回放视频中出现的时刻（若平台提供）。", ""]
    for m in manifest:
        t = m.get("t_sec")
        ts = fmt_ts(t) if isinstance(t, int) and t >= 0 else "—"
        lines.append(f"## 第 {m['index']} 页 · [{ts}]")
        lines.append(f"![]({m['file']})")
        if m.get("note"):
            lines.append("")
            lines.append(m["note"])
        if m.get("status") != "ok":
            lines.append(f"\n> ⚠️ {m['status']}")
        lines.append("")
    with open(os.path.join(dest_dir, "manifest.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(lines))
    return manifest


# ---------------------------------------------------------------- ffmpeg

def ffmpeg_path():
    """优先用工具自带 bin/ffmpeg（界面下载来的），其次系统 PATH。"""
    local = os.path.join(HERE, "bin",
                         "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if os.path.isfile(local):
        return local
    return shutil.which("ffmpeg")


def _ffmpeg_url():
    """eugeneware/ffmpeg-static 的单文件静态构建（gzip 压缩）。"""
    import platform
    base = ("https://github.com/eugeneware/ffmpeg-static/releases/"
            "download/b6.0/")
    if os.name == "nt":
        return base + "ffmpeg-win32-x64.gz"
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    return base + f"ffmpeg-darwin-{arch}.gz"


def download_ffmpeg(progress=None):
    """下载静态 ffmpeg 到 ./bin/（约 20–25MB）。返回可执行文件路径。"""
    import gzip
    url = _ffmpeg_url()
    bin_dir = os.path.join(HERE, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    want = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    target = os.path.join(bin_dir, want)
    gzpath = target + ".gz"
    _log(progress, f"开始下载 ffmpeg：{url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=900, context=SSL_CTX) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(gzpath, "wb") as f:
                while True:
                    buf = r.read(1 << 16)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if progress and total:
                        progress(f"下载 ffmpeg：{done // (1 << 20)} / "
                                 f"{total // (1 << 20)} MB")
        _log(progress, "解压中…")
        with gzip.open(gzpath, "rb") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    except Exception as e:
        raise PipelineError(f"ffmpeg 下载失败：{e}")
    finally:
        try:
            os.unlink(gzpath)
        except OSError:
            pass
    if os.name != "nt":
        os.chmod(target, 0o755)
    _log(progress, f"ffmpeg 已就绪：{target}")
    return target


# ------------------------------------------------------- 视频与增强识别

def _find_mp4(obj):
    """在 get-config 的 JSON 里递归找第一个 mp4 直链。"""
    if isinstance(obj, str):
        if obj.startswith("http") and ".mp4" in obj.split("?")[0]:
            return obj
        return None
    if isinstance(obj, dict):
        for v in obj.values():
            r = _find_mp4(v)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = _find_mp4(v)
            if r:
                return r
    return None


def fetch_video_url(course_id, sub_id, tenant_code, cookie, timeout=30):
    """带 Cookie 调 get-config，递归找出回放 mp4 直链。失败返回 None。"""
    if not cookie:
        return None
    url = (f"{BASE}/courseapi/v2/play-template/get-config"
           f"?course_id={course_id}&sub_id={sub_id}&tenant_code={tenant_code}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Cookie": cookie,
        "Referer": f"{BASE}/livingroom?sub_id={sub_id}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=SSL_CTX) as r:
            data = json.loads(r.read().decode("utf-8"))
        return _find_mp4(data)
    except Exception:
        return None


def download_file(url, dest, progress=None, timeout=1800):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                buf = r.read(1 << 18)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if progress and total and done % (1 << 22) < (1 << 18):
                    progress(f"下载视频：{done // (1 << 20)} / "
                             f"{total // (1 << 20)} MB")
    return done


# Whisper 提示词必须是"中性 + 与本节课相关"的：写死某门课的术语表会带偏模型，
# 在音乐/静音/噪声段触发大段幻觉（如反复输出"点赞订阅转发"）。
# 这里只放一句中性说明；若用户上传了课件，再用课件开头几个词补充本课术语。
WHISPER_PROMPT_BASE = "以下是浙江大学课堂的中文授课录音，请如实转写讲课内容。"


def build_whisper_prompt(courseware_text=None):
    prompt = WHISPER_PROMPT_BASE
    if courseware_text:
        head = re.sub(r"\s+", " ", courseware_text).strip()[:160]
        if head:
            prompt += "本课涉及：" + head
    return prompt


# 常见的 Whisper 幻觉句（多为片头片尾/静音段产生），命中比例过高即判异常
_HALLUCINATION_HINTS = (
    "请不吝点赞", "点赞订阅", "订阅转发", "打赏支持", "明镜与点点",
    "字幕由", "字幕提供", "amara.org", "谢谢观看", "感谢观看",
)


def _norm_text(t):
    """归一化：去空白与标点，便于比较"是否重复"。"""
    return re.sub(r"[\s\W_]+", "", t).lower()


def check_transcript_quality(segs, progress=None, label="字幕"):
    """检测字幕是否大面积重复/幻觉。异常时抛 PipelineError 中止，
    避免把垃圾字幕送进 AI 总结。正常则返回 None。"""
    total = len(segs)
    if total < 8:
        return
    norms = [_norm_text(t) for _, t in segs if _norm_text(t)]
    if not norms:
        return
    from collections import Counter
    cnt = Counter(norms)
    top_text, top_n = cnt.most_common(1)[0]
    unique = len(cnt)
    # ① 单一文本占比过高（同一句反复出现）
    if top_n / total >= 0.30:
        sample = next((t for _, t in segs if _norm_text(t) == top_text), "")
        raise PipelineError(
            f"{label}识别结果异常：{total} 段中有 {top_n} 段（"
            f"{top_n * 100 // total}%）重复为「{sample[:40]}」。"
            "这通常不是课程内容（常见于 Whisper 幻觉或音频异常），已中止，"
            "不会发送给 AI。建议：确认视频音轨正常；增强识别模式可去掉/更换"
            "提示词，或改用平台字幕。")
    # ② 内容多样性过低
    if total >= 20 and unique / total < 0.20:
        raise PipelineError(
            f"{label}识别结果异常：{total} 段中只有 {unique} 种不同文本，"
            "内容高度重复，已中止，不会发送给 AI。请检查音频或更换识别方式。")
    # ③ 命中常见幻觉句比例过高
    hits = sum(1 for _, t in segs
               if any(h in t for h in _HALLUCINATION_HINTS))
    if hits / total >= 0.50:
        raise PipelineError(
            f"{label}识别结果异常：{hits}/{total} 段包含「点赞/订阅/字幕提供」"
            "等非课程内容，判定为 Whisper 幻觉，已中止，不会发送给 AI。")


def whisper_transcribe(audio_path, progress=None, courseware_text=None,
                       initial_prompt=None):
    """用 faster-whisper 重新识别音频，返回 [(BeginSec, Text), ...]。
    关键参数：condition_on_previous_text=False（切断错误跨窗口传播，防止
    一句话被无限重复）；提示词默认中性（只由课件补术语），不再写死某门课。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise PipelineError(
            "增强识别模式需要 faster-whisper。请在终端运行一次："
            "pip install faster-whisper（约几百 MB，含模型自动下载）")
    prompt = initial_prompt or build_whisper_prompt(courseware_text)
    _log(progress, "加载 Whisper large-v3 模型（首次会自动下载约 3GB）…")
    model = WhisperModel("large-v3", device="auto", compute_type="auto")
    _log(progress, f"开始识别音频（较慢，耐心等）；提示词：{prompt[:60]}…")
    segments, _info = model.transcribe(
        audio_path, language="zh", initial_prompt=prompt,
        condition_on_previous_text=False,
        vad_filter=True,
        beam_size=5,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6)
    segs, n = [], 0
    for s in segments:
        txt = (s.text or "").strip()
        if txt:
            segs.append((int(s.start), txt))
        n += 1
        if progress and n % 50 == 0:
            progress(f"已识别 {n} 句，当前进度 {int(s.start) // 60} 分钟")
    if not segs:
        raise PipelineError("Whisper 没有识别出任何内容")
    check_transcript_quality(segs, progress, "Whisper")
    return segs


def extract_audio(ffmpeg, video_path, audio_path, progress=None):
    _log(progress, "用 ffmpeg 抽取音频（16kHz 单声道 mp3）…")
    r = subprocess.run(
        [ffmpeg, "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
         "-b:a", "32k", audio_path],
        capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.isfile(audio_path):
        raise PipelineError("ffmpeg 抽音频失败：" + (r.stderr or "")[-300:])


# ---------------------------------------------------------------- 分块

def chunk_minutes(minutes, max_chars=11000):
    """按分钟对齐切块，每块不超过 max_chars。返回 [(起分, 止分, 文本)]。"""
    chunks, cur, cur_start, cur_len = [], [], None, 0
    for m, texts in sorted(minutes.items()):
        line = f"[{m // 60:02d}:{m % 60:02d}] " + "".join(texts)
        if cur and cur_len + len(line) > max_chars:
            chunks.append((cur_start, cur[-1][0], "\n".join(x[1] for x in cur)))
            cur, cur_start, cur_len = [], None, 0
        if cur_start is None:
            cur_start = m
        cur.append((m, line))
        cur_len += len(line)
    if cur:
        chunks.append((cur_start, cur[-1][0], "\n".join(x[1] for x in cur)))
    return chunks


# ------------------------------------------------------- AI 课程画像层

PROFILE_SYSTEM = (
    "你是课程信息识别器。用户给你某节课的开头字幕节选（可能附带 PPT 文字），"
    "请识别这门课后，只输出一个 JSON 对象（不要任何解释或代码围栏）：\n"
    '{"course": "课程名称（尽量给正式名，如：数据结构与算法分析）",\n'
    ' "subject": "学科领域",\n'
    ' "topic": "本讲主题",\n'
    ' "terms": ["术语(English)", ...],\n'
    ' "focus": "写给总结助手的一段针对性指引：这门课看重什么'
    '（证明严谨性/公式推导/代码实现/计算训练…），本讲总结应侧重什么，2–4 句"}\n'
    "terms 给 8–15 个本课关键术语（中文(英文)格式）——它们是纠正语音识别"
    "同音错字的权威词表。JSON 必须合法。")


def build_course_profile(api_key, transcript_sample, ppt_sample,
                         progress=None, base_url=None, chat_model=None):
    """总结前先用字幕/PPT 开头识别课程，生成针对性提示词（AI 提示词层）。
    通用约束留在 SUMMARY_SYSTEM；本函数产出"这门课特有"的部分。
    失败返回 None（总结照常进行）。"""
    user = ""
    if transcript_sample:
        user += "[字幕开头节选]\n" + transcript_sample[:3000] + "\n\n"
    if ppt_sample:
        user += "[PPT 文字层节选]\n" + ppt_sample[:2000] + "\n\n"
    if not user.strip():
        return None
    try:
        content, usage = deepseek_chat(api_key, PROFILE_SYSTEM, user,
                                       max_tokens=700, temperature=0.1,
                                       base_url=base_url,
                                       model=chat_model or "deepseek-chat")
    except PipelineError as e:
        _log(progress, f"   课程画像生成失败（跳过，不影响总结）：{e}")
        return None
    m = re.search(r"\{.*\}", content, re.S)      # 容错：剥可能的围栏
    if not m:
        return None
    try:
        prof = json.loads(m.group(0))
    except Exception:
        return None
    return prof if prof.get("course") else None


def profile_block(profile):
    """把画像渲染成注入总结请求的文本块；无画像返回空串。"""
    if not profile:
        return ""
    terms = "、".join(profile.get("terms") or []) or "（无）"
    return ("\n\n[本课画像——AI 预识别，总结时优先遵循]\n"
            f"课程：{profile.get('course', '未知')}；"
            f"学科：{profile.get('subject', '未知')}；"
            f"本讲主题：{profile.get('topic', '未知')}\n"
            f"关键术语词表（字幕中的同音/近音错字一律按此纠正）：{terms}\n"
            f"针对性指引：{profile.get('focus', '无')}\n")


# ---------------------------------------------------------------- DeepSeek

DEEPSEEK_FLASH = "deepseek-flash"      # V4.1 Flash：原生多模态（图+文）


def deepseek_vision(api_key, prompt, image_url, model=DEEPSEEK_FLASH,
                    max_tokens=1024, retries=3, timeout=120,
                    base_url=None):
    """带一张图片调用视觉模型（任意 OpenAI 兼容端点），返回 (文本, usage)。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    last_err = None
    for i in range(retries):
        req = urllib.request.Request(base_url or DEEPSEEK_URL, data=body,
                                     method="POST",
                                     headers={
                                         "Content-Type": "application/json",
                                         "Authorization": f"Bearer {api_key}",
                                         "User-Agent": UA,
                                     })
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                                context=SSL_CTX) as r:
                resp = json.loads(r.read().decode("utf-8"))
            return (resp["choices"][0]["message"]["content"],
                    resp.get("usage", {}))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            last_err = f"HTTP {e.code} {detail}"
            if e.code == 401:
                raise PipelineError("API key 无效（401），请检查是否与所选提供商匹配")
            if e.code in (429, 500, 502, 503):
                time.sleep(3 * (i + 1))
                continue
            raise PipelineError(f"视觉模型调用失败：{last_err}")
        except PipelineError:
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(3 * (i + 1))
    raise PipelineError(f"视觉模型多次重试仍失败：{last_err}")


SLIDE_OCR_PROMPT = (
    "这是大学课堂回放视频里自动截取的一页 PPT 幻灯片。请把这页上的所有文字"
    "完整转写为纯文本（保持原有的条目层级；数学公式用 LaTeX；表格转成 Markdown）。"
    "若页面上有示意图/算法流程图/代码截图，在文字之后另起一行用「[图] …」"
    "简要描述其内容。只输出转写结果，不要任何客套或解释。")


def transcribe_slides(manifest, dest_dir, api_key, progress=None,
                      total_usage=None, group_size=10, max_groups=10,
                      base_url=None, vision_model=None):
    """用 DeepSeek 视觉模型转写幻灯片图片，生成 ppt_text.txt。
    平台大多不给 PPT 文字层（content 为空），图片才是唯一信息源——
    这里把每页图片"看图转写"成文字，供检索与注入总结。

    分发策略：按页码顺序切成每 group_size 页一组（每组相当于一个
    "转写代理"，最多管 10 页）；组间并发（同时最多 max_groups 组，
    其余组排队），组内顺序。输出仍按页码汇总排序，与顺序版一致。
    空 manifest / 全部下载失败时安全返回空串，不写文件。"""
    import concurrent.futures
    import threading
    lock = threading.Lock()
    results = {}                      # 页码 -> (t_sec, text)
    usage_tot = {"p": 0, "c": 0}
    stat = {"ok": 0, "fail": 0, "stop": False, "warned": False}
    todo = sorted((m for m in manifest if m.get("status") == "ok"),
                  key=lambda m: m["index"])
    groups = [todo[i:i + group_size] for i in range(0, len(todo), group_size)]

    def run_group(gi, group):
        for m in group:
            with lock:
                if stat["stop"]:
                    return
            text, usage = "", None
            try:
                text, usage = deepseek_vision(api_key, SLIDE_OCR_PROMPT,
                                              m["img"],
                                              model=vision_model or DEEPSEEK_FLASH,
                                              base_url=base_url)
                with lock:
                    stat["ok"] += 1
            except PipelineError as e:
                warn = False
                with lock:
                    stat["fail"] += 1
                    # 连续多页全失败 → 多半是所选服务商没有可用的视觉模型，
                    # 停止后续请求（图片仍已保存，不影响总结流程）
                    if stat["ok"] == 0 and stat["fail"] >= 3:
                        stat["stop"] = True
                        warn = not stat["warned"]
                        stat["warned"] = True
                if warn:
                    _log(progress, f"   视觉转写已停止：所选服务商的视觉模型不可用"
                                   f"（{e}）。PPT 图片仍已保存，可用兼容的视觉"
                                   f"模型或 DeepSeek 重跑转写。")
            text = re.sub(r"\s+", " ", text).strip()
            with lock:
                if usage:
                    usage_tot["p"] += usage.get("prompt_tokens", 0)
                    usage_tot["c"] += usage.get("completion_tokens", 0)
                results[m["index"]] = (m.get("t_sec"), text)
        span = f"第{group[0]['index']}–{group[-1]['index']}页"
        _log(progress, f"   转写组 {gi + 1}/{len(groups)}（{span}）完成")

    if groups:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(groups), max_groups)) as ex:
            for f in [ex.submit(run_group, i, g)
                      for i, g in enumerate(groups)]:
                f.result()
    if total_usage is not None:
        total_usage["prompt_tokens"] += usage_tot["p"]
        total_usage["completion_tokens"] += usage_tot["c"]
    lines = []
    for idx in sorted(results):
        t, text = results[idx]
        ts = fmt_ts(t) if isinstance(t, int) and t >= 0 else "--:--:--"
        if text:
            lines.append(f"[{ts}] 第{idx}页 {text}")
    out = "\n".join(lines)
    if out:
        with open(os.path.join(dest_dir, "ppt_text.txt"), "w",
                  encoding="utf-8") as f:
            f.write(out)
    return out


def deepseek_chat(api_key, system, user, model="deepseek-chat",
                  max_tokens=4096, retries=5, temperature=0.3,
                  base_url=None):
    """文本对话（任意 OpenAI 兼容端点：DeepSeek/GLM/千问/OpenAI 通用）。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }).encode("utf-8")
    last_err = None
    for i in range(retries):
        req = urllib.request.Request(DEEPSEEK_URL, data=body, method="POST",
                                     headers={
                                         "Content-Type": "application/json",
                                         "Authorization": f"Bearer {api_key}",
                                         "User-Agent": UA,
                                     })
        try:
            with urllib.request.urlopen(req, timeout=300,
                                                context=SSL_CTX) as r:
                resp = json.loads(r.read().decode("utf-8"))
            content = resp["choices"][0]["message"]["content"]
            usage = resp.get("usage", {})
            return content, usage
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            last_err = f"HTTP {e.code} {detail}"
            if e.code == 401:
                raise PipelineError("API key 无效（401），请检查是否与所选提供商匹配")
            if e.code == 402:
                raise PipelineError("API 账户余额不足（402），请充值后重试")
            if e.code in (429, 500, 502, 503):
                time.sleep(3 * (i + 1))   # 限流/服务端抖动，退避重试
                continue
            raise PipelineError(f"AI 调用失败：{last_err}")
        except PipelineError:
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(3 * (i + 1))
    raise PipelineError(f"AI 多次重试仍失败：{last_err}")


# ---------------------------------------------------------------- 课件

def _pdf_decode_string(b):
    """把 PDF 文本操作符里的字符串字节解成文本：处理转义、UTF-16BOM、UTF-8、latin-1。"""
    b = re.sub(
        rb"\\([nrtbf()\\])",
        lambda m: {b"n": b"\n", b"r": b"\r", b"t": b"\t",
                   b"b": b"\b", b"f": b"\f"}.get(m.group(1), m.group(1)),
        b)
    if b.startswith(b"\xfe\xff"):
        return b[2:].decode("utf-16-be", "ignore")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1", "ignore")


def _extract_pdf_text(path):
    """提取 PDF 文字层。优先系统 pdftotext（若有装 poppler），否则内置解析
    （zlib 解压内容流 + 抓取 Tj/TJ 文本操作符）。扫描版/特殊 CID 字体可能
    提取不到——此时给出明确提示。"""
    exe = shutil.which("pdftotext")
    if exe:
        try:
            r = subprocess.run([exe, "-enc", "UTF-8", path, "-"],
                               capture_output=True, timeout=120)
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", "ignore")
        except Exception:
            pass
    import zlib
    with open(path, "rb") as f:
        data = f.read()
    parts = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = m.group(1)
        try:
            dec = zlib.decompress(raw)
        except Exception:
            dec = raw
        for t in re.findall(rb"\(((?:[^()\\]|\\.)*)\)", dec):
            s = _pdf_decode_string(t)
            if s:
                parts.append(s)
    text = " ".join(parts)
    if len(text.strip()) < 20:
        raise PipelineError(
            "这份 PDF 提取不到文字（可能是扫描图片或特殊字体编码）。"
            "建议转成 pptx / docx / txt 后再上传。")
    return text


def extract_courseware(path):
    """从课件文件提取纯文字。支持 pptx / docx / pdf / txt / md。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".txt", ".md"):
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    if ext == ".pdf":
        return _extract_pdf_text(path)
    if ext == ".pptx":
        with zipfile.ZipFile(path) as z:
            slides = sorted(
                (n for n in z.namelist()
                 if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                key=lambda n: int(re.search(r"(\d+)", n).group(1)))
            parts = []
            for n in slides:
                xml = z.read(n).decode("utf-8", "ignore")
                t = " ".join(re.findall(r"<a:t>([^<]*)</a:t>", xml))
                if t.strip():
                    parts.append(t)
            return "\n".join(parts)
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
            return "\n".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))
    raise PipelineError(
        f"不支持的课件格式 {ext or '（无扩展名）'}，目前支持 pptx / docx / pdf / txt / md")


SUMMARY_SYSTEM = (
    "你是顶尖的大学课程笔记编辑，擅长从冗长的课堂口语中提炼知识骨架、抓住重点。"
    "用户给出一整节课的字幕（每分钟一行，行首 [时:分] 时间戳；语音识别可能有错别字，"
    "请结合上下文纠正）。消息里可能附带（按优先级）："
    "『本课画像』——AI 预识别的课程名/术语词表/侧重指引，**术语纠正与详略取舍优先以它为准**；"
    "『参考课件文字』或『本课 PPT 幻灯片文字层』（按页、含每页投出时刻的时间戳，"
    "与字幕同时间轴）——其中术语拼写与内容结构是权威版本，按各参考块内的说明"
    "用它来对齐章节、纠正同音误识别、补全字幕未讲全的定义与步骤。"
    "输出 Markdown，严格按以下结构：\n"
    "## 一、本课速览\n"
    "先用一张表列出各大板块：时间段 | 主题 | 一句话核心。再用 3–5 条列出"
    "『这节课最该带走的结论』——只留精华，让没听课的人 30 秒抓住主线。\n"
    "## 二、分章详解\n"
    "按知识板块组织（不按时间流水账）。每个板块依次写清：\n"
    "① 定义与结论（措辞精确，可直接背诵）；\n"
    "② 推导或证明思路（一步一步，不跳步，尽量配一个具体数字例子）；\n"
    "③ 例题（题目→解法→答案→易错点）；\n"
    "④ 老师特别强调、反复提及或提醒『会考/注意』的内容，单独标出。\n"
    "## 三、公式与算法清单\n"
    "所有关键公式用 LaTeX（除法写上下分式 \\frac{a}{b}，不要 a/b 斜杠式），"
    "算法给伪代码或步骤列表，注明复杂度。\n"
    "## 四、考试与作业信息\n"
    "老师提到的考试时间、范围、形式、作业、点名政策等，原样汇总；没有就写『无』。\n"
    "## 五、最可能考的点\n"
    "5–10 条，每条附一句理由。\n"
    "总要求：主线与闲聊严格区分——点名、设备调试、闲谈一律丢弃；"
    "老师重复或加重语气的内容必须体现；关键处标注来源时间戳 [时:分]；"
    "直接输出正文，不要任何客套话。"
)


CHUNK_SYSTEM = (
    "你是大学课堂笔记整理者。用户会给你一节课某个时段的语音识别字幕"
    "（每分钟一行，行首是 [时:分] 时间戳，识别可能有错别字，需按上下文纠正理解）。"
    "请提取该时段的课程内容，输出 Markdown，要求：\n"
    "1. 按主题组织知识点：定义、定理、公式、算法步骤、伪代码；\n"
    "2. 老师讲的例题：题目、解法、答案、易错点；\n"
    "3. 证明题写出证明思路，分步骤，不要跳步；\n"
    "4. 老师口头强调的考点、提醒、通知（如考试范围/时间）单独列出；\n"
    "5. 闲聊/点名/设备调试等与课程无关的内容直接忽略；\n"
    "6. 关键内容标注来源时间戳 [时:分]，方便回看视频；\n"
    "7. 数学公式用 LaTeX（$...$），除法写成上下分式 \\frac{a}{b}，不要 a/b 斜杠式。\n"
    "直接输出笔记内容，不要输出『好的』『以下是』之类的客套话。"
)

MERGE_SYSTEM = (
    "你是大学课程讲义编辑。用户会给你同一节课按时间顺序分段整理的笔记，"
    "请合并成一份完整、连贯的课程知识总结（Markdown），要求：\n"
    "1. 开头给出本课结构总览（讲了哪几大块、各对应时间段）；\n"
    "2. 按章节重新组织，去除分段之间的重复内容，保留所有时间戳标注；\n"
    "3. 定义、定理、公式、算法、例题、证明思路一个都不要丢；\n"
    "4. 老师强调的考点和通知（考试时间/范围等）汇总成单独一节放在显眼位置；\n"
    "5. 结尾给一个『最容易考的点』清单；\n"
    "6. 数学公式用 LaTeX，除法写成上下分式 \\frac{a}{b}。\n"
    "直接输出总结全文。"
)


# ---------------------------------------------------------------- 主流程

def run_pipeline(url, api_key=None, cookie=None, out_root=None, progress=None,
                 mode="summarize", video_url=None,
                 summary_system=None, courseware_text=None,
                 extra_requirements=None, provider=None, fetch_ppt=True):
    """完整流水线。mode: transcript=只抓字幕 / summarize=字幕+总结 /
    enhanced=下载视频→Whisper 重识别→总结。
    总结默认单次请求（一致性好）；超出模型上下文时自动退化为顺序分块+合并。
    courseware_text: 上传课件提取的文字，作为术语与结构参考。
    extra_requirements: 用户自定义的额外要求，附加在请求末尾。
    provider: dict(id/base/chat/vision)，指定 OpenAI 兼容的任意 API 提供商；
              缺省用 DeepSeek。
    fetch_ppt: 是否抓取课件 PPT（下载图片 + 视觉转写会消耗 token）；
               False 时完全跳过，只做字幕总结。"""
    summary_system = summary_system or SUMMARY_SYSTEM
    api_base, api_chat_model, api_vision_model = resolve_api(provider)
    out_root = out_root or os.path.join(HERE, "output")
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    # Cookie 优先用调用方传入的（如界面手动粘贴）；否则从系统钥匙串取已存登录态。
    cookie = (cookie or "").strip() or secure_store.load_cookie() or None
    # API key 同理：未传时从钥匙串取（命令行方式也能复用界面存的 key）。
    api_key = (api_key or "").strip() or secure_store.load_api_key() or None

    _log(progress, "① 解析回放地址…")
    info = parse_url(url)
    sub_id, tenant = info["sub_id"], info["tenant_code"]
    _log(progress, f"   course_id={info['course_id']}  sub_id={sub_id}")

    result_dir = os.path.join(
        out_root, f"{datetime.now():%Y%m%d}_sub{sub_id}")
    raw_dir = os.path.join(result_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    if mode == "enhanced":
        _log(progress, "② 增强识别模式：准备视频与音频…")
        ff = ffmpeg_path()
        if not ff:
            raise PipelineError(
                "未找到 ffmpeg：请先在界面点「下载 ffmpeg」，或自行安装")
        vurl = video_url or fetch_video_url(
            info["course_id"], sub_id, tenant, cookie)
        if not vurl:
            raise PipelineError(
                "拿不到视频直链：请在界面粘贴 mp4 地址，或填写有效 Cookie")
        video_path = os.path.join(raw_dir, "video.mp4")
        if not os.path.isfile(video_path):
            _log(progress, "   下载回放视频（可能 1–2GB，耐心等）…")
            size = download_file(vurl, video_path, progress)
            _log(progress, f"   视频下载完成：{size // (1 << 20)} MB")
        else:
            _log(progress, "   检测到已下载的视频，跳过重复下载")
        audio_path = os.path.join(raw_dir, "audio.mp3")
        if not os.path.isfile(audio_path):
            extract_audio(ff, video_path, audio_path, progress)
        segs = whisper_transcribe(audio_path, progress,
                                  courseware_text=courseware_text)
        src_note = "Whisper large-v3 重新识别"
    else:
        _log(progress, "② 抓取平台语音字幕"
             + ("（带登录态）…" if cookie else "（未登录，先试匿名）…"))
        segs = fetch_transcript(sub_id, tenant, cookie)
        check_transcript_quality(segs, progress, "平台字幕")
        src_note = "平台字幕"

    per_seg, per_min, minutes = build_transcript_texts(segs)
    dur = fmt_ts(segs[-1][0])
    _log(progress, f"   共 {len(segs)} 段 / {len(per_seg)} 字，课程时长约 {dur}"
                   f"（来源：{src_note}）")
    with open(os.path.join(raw_dir, "transcript.txt"), "w",
              encoding="utf-8") as f:
        f.write(per_seg)
    with open(os.path.join(raw_dir, "transcript_by_minute.txt"), "w",
              encoding="utf-8") as f:
        f.write(per_min)

    ppt_dir = None
    slide_count = 0
    if mode == "transcript":
        _log(progress, "③ 跳过 PPT 抓取（仅抓字幕模式）")
    elif not fetch_ppt:
        _log(progress, "③ 已按设置跳过 PPT 抓取（不下载图片、不消耗视觉转写"
                       " token，只做字幕总结）")
    elif not cookie:
        _log(progress, "③ 跳过 PPT 抓取（未登录）")
    else:
        _log(progress, "③ 用登录态抓取 PPT 幻灯片（图片+文字层，可选增强）…")
        try:
            slides = fetch_ppt_slides(info["course_id"], sub_id, cookie,
                                      progress)
        except PipelineError as e:
            slides = []
            _log(progress, f"   {e}")
        if slides:
            ppt_dir = os.path.join(result_dir, "ppt")
            manifest = download_slides(slides, ppt_dir, progress)
            slide_count = len(manifest)
            ok = sum(1 for m in manifest if m["status"] == "ok")
            _log(progress, f"   PPT 已下载：{ok}/{slide_count} 页图片 → {ppt_dir}")
            # 平台自带文字层（少数课才有）：直接落盘
            platform_notes = [m for m in manifest if m.get("note")]
            if platform_notes:
                with open(os.path.join(ppt_dir, "ppt_text.txt"), "w",
                          encoding="utf-8") as f:
                    for m in platform_notes:
                        ts = fmt_ts(m["t_sec"]) if isinstance(m.get("t_sec"),
                                                  int) and m["t_sec"] >= 0 \
                            else "--:--:--"
                        f.write(f"[{ts}] 第{m['index']}页 {m['note']}\n")
            # 平台没有文字层（大多数课：PPT 是纯图片）→ 用 DeepSeek 视觉模型逐页转写
            if not platform_notes and api_key and ok:
                _log(progress, f"   平台未提供 PPT 文字层，用视觉模型"
                               f"分组转写 {ok} 页（每代理≤10 页，组间并发）…")
                try:
                    transcribe_slides(manifest, ppt_dir, api_key, progress,
                                      total_usage, base_url=api_base,
                                      vision_model=api_vision_model)
                except PipelineError as e:
                    _log(progress, f"   视觉转写中止：{e}")
            elif not platform_notes and not api_key:
                _log(progress, "   提示：平台无 PPT 文字层，填 API key 后重跑"
                               "可用视觉模型自动转写每页内容")
        else:
            _log(progress, "   该课程未提供 PPT 或抓取失败，回退为纯字幕总结")

    if mode == "transcript" or not api_key:
        if mode != "transcript":
            _log(progress, "注意：未填 API key，本次只抓取不总结。")
        _log(progress, f"字幕已保存到：{raw_dir}")
        return {"result_dir": result_dir, "summary_path": None,
                "transcript_path": os.path.join(raw_dir, "transcript.txt"),
                "ppt_dir": ppt_dir, "slide_count": slide_count,
                "segments": len(segs), "duration": dur, "usage": total_usage}

    ref = ""
    ppt_text = ""
    if courseware_text:
        ref = ("\n\n[参考课件文字（节选，用于校准术语与章节结构）]\n"
               + courseware_text[:6000])
    if ppt_dir:
        try:
            with open(os.path.join(ppt_dir, "ppt_text.txt"),
                      encoding="utf-8") as f:
                ppt_text = f.read().strip()
        except OSError:
            ppt_text = ""
        if ppt_text:
            ref += ("\n\n[本课 PPT 幻灯片文字层——按页排列。每行开头的 "
                    "[时:分:秒] 是该页幻灯片在回放视频中被投出的时刻，"
                    "与上方字幕的时间戳同一时间轴。请这样使用它：\n"
                    "① 对齐：某时刻的字幕与同时刻正展示的 PPT 页属于同一"
                    "主题，总结分章时可据此切分；\n"
                    "② 纠错：幻灯片上的术语拼写是权威版本，字幕中同音/近音"
                    "误识别（如人名、算法名、公式符号）一律以幻灯片为准；\n"
                    "③ 补全：幻灯片上的定义、定理编号、步骤列表、伪代码是"
                    "老师备课的骨架，即使字幕里只是口头带过，也必须完整"
                    "进入总结，不得省略。]\n" + ppt_text[:6000])
    extra = ""
    if extra_requirements:
        extra = ("\n\n[用户的额外要求（必须满足，优先级高于上述默认结构）]\n"
                 + extra_requirements)
    # AI 提示词层：先识别课程，生成本课针对性指引（人工层只保留通用约束）
    profile = None
    if api_key:
        _log(progress, "④ AI 识别课程信息，生成本课针对性提示词…")
        profile = build_course_profile(api_key, per_min[:3500],
                                       ppt_text[:2500], progress,
                                       base_url=api_base,
                                       chat_model=api_chat_model)
        if profile:
            _log(progress, f"   识别为：《{profile.get('course')}》· "
                           f"本讲：{profile.get('topic')} · 术语 "
                           f"{len(profile.get('terms') or [])} 个")
        else:
            _log(progress, "   未能识别（跳过画像，按通用提示词总结）")

    meta = (f"课程：智云课堂回放 sub_id={sub_id}"
            f"（course_id={info['course_id']}），时长约 {dur}")
    user_full = (f"{meta}{profile_block(profile)}{ref}"
                 f"\n\n本课完整字幕如下：\n\n{per_min}{extra}")

    _log(progress, "⑤ 调用 AI 生成完整讲义（单次请求）…")
    try:
        final, usage = deepseek_chat(api_key, summary_system, user_full,
                                     model=api_chat_model,
                                     max_tokens=8192, base_url=api_base)
        total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
    except PipelineError as e:
        if "HTTP 400" not in str(e):
            raise
        _log(progress, "   单次请求超出模型上下文，退化为顺序分块处理（不并行）…")
        final = _summarize_chunked(
            api_key, minutes, os.path.join(result_dir, "分段笔记"),
            progress, total_usage, profile_block(profile),
            base_url=api_base, chat_model=api_chat_model)

    header = (f"# 课程知识总结\n\n"
              f"> 来源：智云课堂回放 sub_id={sub_id}"
              f"（course_id={info['course_id']}），时长约 {dur}\n"
              + (f"> 课程识别（AI）：《{profile.get('course')}》· "
                 f"本讲：{profile.get('topic')}\n" if profile else "")
              + f"> 字幕来源：{src_note}"
              + ("；已参考上传课件" if courseware_text else "")
              + (f"；已抓取 PPT {slide_count} 页（见 ppt/ 目录）"
                 if slide_count else "") + "\n"
              f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}\n"
              f"> token 消耗：输入 {total_usage['prompt_tokens']}，"
              f"输出 {total_usage['completion_tokens']}\n\n---\n\n")
    summary_path = os.path.join(result_dir, "知识总结.md")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(header + final)
    _log(progress, f"⑥ 完成。总结已生成，可在界面下载：{summary_path}")
    return {"result_dir": result_dir, "summary_path": summary_path,
            "transcript_path": os.path.join(raw_dir, "transcript.txt"),
            "ppt_dir": ppt_dir, "slide_count": slide_count,
            "segments": len(segs), "duration": dur, "usage": total_usage}


def _summarize_chunked(api_key, minutes, notes_dir, progress, total_usage,
                       prof_block="", base_url=None, chat_model=None):
    """兜底：顺序分块总结 + 合并（不并行，保证与单次请求同样的确定性顺序）。"""
    chunks = chunk_minutes(minutes)
    _log(progress, f"   切成 {len(chunks)} 块，逐块总结…")
    os.makedirs(notes_dir, exist_ok=True)
    notes = []
    for idx, (m0, m1, text) in enumerate(chunks):
        span = (f"{m0 // 60:02d}:{m0 % 60:02d}"
                f"–{m1 // 60:02d}:{(m1 + 1) % 60:02d}")
        user = (f"课程时段 {span} 的字幕如下：{prof_block}\n\n{text}")
        content, usage = deepseek_chat(api_key, CHUNK_SYSTEM, user,
                                       model=chat_model or "deepseek-chat",
                                       base_url=base_url)
        total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        fn = os.path.join(notes_dir,
                          f"{idx + 1:02d}_{span.replace(':', '')}.md")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(content)
        _log(progress, f"   第 {idx + 1}/{len(chunks)} 块（{span}）完成")
        notes.append(content)
    _log(progress, "   合并分段笔记…")
    merged_input = "\n\n---\n\n".join(
        f"## 第 {i + 1} 段笔记\n\n{n}" for i, n in enumerate(notes))
    final, usage = deepseek_chat(api_key, MERGE_SYSTEM, merged_input,
                                 model=chat_model or "deepseek-chat",
                                 max_tokens=8192, base_url=base_url)
    total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
    total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
    return final


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python3 pipeline.py <回放URL>")
        sys.exit(1)
    r = run_pipeline(u)
    print(json.dumps({k: str(v) for k, v in r.items()}, ensure_ascii=False,
                     indent=1))
