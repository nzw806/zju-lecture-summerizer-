# -*- coding: utf-8 -*-
"""
智云课堂登录：用内置浏览器（Playwright）弹一次官方登录页，登录成功后把会话
Cookie 存进系统钥匙串（见 secure_store）。之后抓取阶段无需再开浏览器。

设计要点（对齐参考项目 ZhiyunAssistant 的做法）：
- 鉴权 = Cookie + 从 Cookie 里 _token 解出的 Authorization: Bearer。
- 密码只进浙大官方页，本模块不读取、不保存密码，只拿登录后的 Cookie。
- Playwright 只用于"交互式登录"；未安装时优雅降级（抛清晰提示，仍可手动粘贴 Cookie）。
"""
import json
import time
import urllib.error
import urllib.request

import pipeline
import secure_store

BASE = pipeline.BASE          # https://classroom.zju.edu.cn
UA = pipeline.UA
SSL_CTX = pipeline.SSL_CTX
LOGIN_TIMEOUT = 300           # 留给用户在弹窗里完成登录的最长秒数


class LoginError(Exception):
    pass


# ------------------------------------------------------------------ token

# token 提取与鉴权头构造统一复用 pipeline 里的实现，避免两份逻辑漂移
bearer_from_cookie = pipeline.bearer_from_cookie


def _auth_headers(cookie):
    return pipeline.ppt_auth_headers(cookie, BASE)


def validate_cookie(cookie, timeout=20):
    """用轻量鉴权接口验证 Cookie 是否仍然有效。
    有效返回用户信息 dict（含 id/name），无效返回 None。"""
    if not cookie:
        return None
    url = BASE + "/userapi/v1/infosimple"
    req = urllib.request.Request(url, headers=_auth_headers(cookie))
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=SSL_CTX) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return None            # 401/403 等都视为无效
    except Exception:
        return None
    if not body.lstrip().startswith("{"):
        return None            # 返回了登录页 HTML
    try:
        data = json.loads(body)
    except Exception:
        return None
    params = data.get("params") or {}
    if params.get("id"):
        return {"id": str(params["id"]),
                "name": params.get("realname") or params.get("name") or "",
                "tenant_id": params.get("tenant_id") or 112}
    return None


# ------------------------------------------------------------------ 登录

def _cookies_to_header(context, origin):
    """把 Playwright context 里属于 origin 的 cookie 拼成请求头字符串。"""
    pairs = []
    for c in context.cookies(origin):
        if c.get("name") and c.get("value") is not None:
            pairs.append(f"{c['name']}={c['value']}")
    return "; ".join(pairs)


def _interactive_login(progress=None):
    """弹出浏览器让用户登录，成功后返回 Cookie 字符串。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise LoginError(
            "未安装 Playwright，无法弹出登录窗口。请在终端运行一次：\n"
            "    pip install playwright\n"
            "    python -m playwright install chromium\n"
            "或在界面里改用「手动粘贴 Cookie」。")

    def log(msg):
        if progress:
            progress(msg)
        else:
            print(msg)

    log("正在打开内置浏览器登录窗口…")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=False)
        except Exception as e:
            raise LoginError(
                f"启动内置浏览器失败：{e}\n"
                "请先运行：python -m playwright install chromium")
        context = browser.new_context(user_agent=UA)
        page = context.new_page()
        try:
            page.goto(BASE + "/", timeout=60000)
        except Exception:
            pass               # 首屏可能重定向到统一认证，超时无妨
        log("请在弹出的窗口里完成浙大统一身份认证登录（最多等待 "
            f"{LOGIN_TIMEOUT // 60} 分钟）。登录成功后会自动继续…")
        deadline = time.time() + LOGIN_TIMEOUT
        cookie = ""
        while time.time() < deadline:
            cookie = _cookies_to_header(context, BASE)
            if cookie and validate_cookie(cookie):
                break
            time.sleep(2)
        else:
            browser.close()
            raise LoginError("登录超时或未检测到有效登录态，请重试。")
        browser.close()
    if not cookie:
        raise LoginError("没能从浏览器取到 Cookie。")
    return cookie


def ensure_login(force=False, progress=None):
    """确保有可用登录态，返回 (cookie, user_info)。
    优先复用钥匙串里已存的 Cookie；无效或 force=True 时弹浏览器重新登录并存回。"""
    def log(msg):
        if progress:
            progress(msg)

    if not force:
        stored = secure_store.load_cookie()
        if stored:
            info = validate_cookie(stored)
            if info:
                if log:
                    log(f"复用钥匙串里的登录态（{info.get('name') or info['id']}）")
                return stored, info
            if log:
                log("钥匙串里的登录态已失效，需要重新登录…")

    cookie = _interactive_login(progress)
    info = validate_cookie(cookie)
    if not info:
        raise LoginError("登录似乎成功了，但校验登录态失败，请重试。")
    secure_store.save_cookie(cookie)
    if log:
        log(f"登录成功（{info.get('name') or info['id']}），"
            f"已存入{secure_store.backend_name()}。")
    return cookie, info


def logout():
    """清除本地登录态。"""
    secure_store.clear_cookie()


if __name__ == "__main__":
    ck, who = ensure_login(progress=lambda m: print("[login]", m))
    print("登录用户：", who)
    print("Cookie 长度：", len(ck), "| 含 _token：", bool(bearer_from_cookie(ck)))
