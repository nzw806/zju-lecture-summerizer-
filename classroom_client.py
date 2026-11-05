# -*- coding: utf-8 -*-
"""智云课堂只读课程查询客户端。

Cookie 只在本地请求智云课堂时使用；本模块不把 Cookie、Bearer Token
或原始响应写入日志、文件或 AI 请求。
"""
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import date

BASE = "https://classroom.zju.edu.cn"
DEFAULT_TENANT = "112"
TARGET_HOST = "classroom.zju.edu.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
SSL_CTX = ssl.create_default_context()


class ClassroomError(RuntimeError):
    """对用户安全可显示的智云课堂错误，不包含凭据。"""


class ClassroomAuthError(ClassroomError):
    """登录态失效或无法提取智云 Bearer Token。"""


def validate_day(value):
    value = str(value or "").strip()
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ClassroomError("日期格式应为 YYYY-MM-DD") from None
    return value


def _safe_id(value, label):
    value = str(value or "").strip()
    if not value or not re.fullmatch(r"[0-9]+", value):
        raise ClassroomError(f"智云课堂返回的 {label} 无效")
    return value


def extract_bearer_token(cookie):
    """从智云 Cookie 中提取 URL 编码 PHP 序列化的 _token 值。"""
    if not cookie or any(ch in cookie for ch in "\r\n"):
        raise ClassroomAuthError("请先粘贴智云课堂 Cookie")
    decoded = urllib.parse.unquote(cookie)
    match = re.search(
        r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"([^"\r\n]+)";',
        decoded)
    if not match:
        raise ClassroomAuthError(
            "Cookie 中没有找到智云课堂登录态，请在 classroom.zju.edu.cn 请求中重新复制 Cookie")
    token = match.group(1).strip()
    if not token or len(token) > 4096:
        raise ClassroomAuthError("智云课堂登录态中的 Token 无效")
    return token


def _request_json(path, cookie, timeout=30):
    token = extract_bearer_token(cookie)
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Cookie": cookie,
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ClassroomAuthError("智云课堂登录态已失效，请重新复制 Cookie") from None
        raise ClassroomError(f"智云课堂接口返回 HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise ClassroomError(f"智云课堂连接失败：{exc.reason}") from None
    except TimeoutError:
        raise ClassroomError("智云课堂请求超时，请稍后重试") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ClassroomError("智云课堂返回了无法解析的数据，可能是登录跳转或接口已变化") from None
    if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0"):
        message = str(payload.get("msg") or payload.get("message") or "接口返回失败")
        if any(word in message.lower() for word in ("login", "auth", "token", "登录", "认证")):
            raise ClassroomAuthError("智云课堂登录态已失效，请重新复制 Cookie")
        raise ClassroomError(f"智云课堂接口失败：{message[:120]}")
    return payload


def _course_rows(payload):
    groups = payload.get("list") if isinstance(payload, dict) else None
    if isinstance(groups, dict):
        groups = list(groups.values())
    if not isinstance(groups, list):
        raise ClassroomError("“我的课程”接口返回结构发生变化：找不到课程列表")
    for group in groups:
        rows = group.get("course") if isinstance(group, dict) else None
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    yield row


def get_my_course_day(day, cookie):
    """按日期返回课程分组，每组含可选课堂，不返回原始响应。"""
    day = validate_day(day)
    query = urllib.parse.urlencode({"day": day})
    payload = _request_json(f"/courseapi/v2/course-live/get-my-course-day?{query}", cookie)
    grouped = OrderedDict()
    for row in _course_rows(payload):
        course_id = _safe_id(row.get("id") or row.get("course_id"), "课程 ID")
        sub_id = _safe_id(row.get("sub_id"), "课次 ID")
        course_name = str(row.get("title") or row.get("course_name") or "未命名课程").strip()
        session_name = str(row.get("sub_title") or "未命名课堂").strip()
        teacher = str(row.get("realname") or row.get("lecturer_name") or "").strip()
        course = grouped.setdefault(course_id, {
            "id": course_id,
            "name": course_name,
            "teacher": teacher,
            "date": day,
            "sessions": [],
        })
        if teacher and not course["teacher"]:
            course["teacher"] = teacher
        if not any(item["sub_id"] == sub_id for item in course["sessions"]):
            course["sessions"].append({
                "sub_id": sub_id,
                "name": session_name,
                "teacher": teacher or course["teacher"],
                "date": day,
                "tenant_code": DEFAULT_TENANT,
            })
    courses = list(grouped.values())
    courses.sort(key=lambda item: (item["name"], item["id"]))
    for course in courses:
        course["sessions"].sort(key=lambda item: (item["name"], item["sub_id"]))
    return courses


def get_sub_info(course_id, sub_id, cookie, tenant_code=DEFAULT_TENANT):
    """按需检查课次回放内容；不返回原始媒体地址或原始响应。"""
    course_id = _safe_id(course_id, "课程 ID")
    sub_id = _safe_id(sub_id, "课次 ID")
    query = urllib.parse.urlencode({"course_id": course_id, "sub_id": sub_id})
    payload = _request_json(
        f"/courseapi/v3/portal-home-setting/get-sub-info?{query}", cookie)
    data = payload.get("data") if isinstance(payload, dict) else None
    content = data.get("content") if isinstance(data, dict) else None
    playback = content.get("save_playback") if isinstance(content, dict) else None
    contents = playback.get("contents") if isinstance(playback, dict) else None
    if contents is None:
        status, available, count = "unknown", None, None
    elif isinstance(contents, str):
        count = 1 if contents.strip() else 0
        status, available = ("available", True) if count else ("unavailable", False)
    elif isinstance(contents, (list, tuple, dict)):
        count = len(contents)
        status, available = ("available", True) if count else ("unavailable", False)
    else:
        count = None
        status, available = "unknown", None
    return {
        "course_id": course_id,
        "sub_id": sub_id,
        "tenant_code": str(tenant_code or DEFAULT_TENANT),
        "available": available,
        "status": status,
        "content_count": count,
    }
