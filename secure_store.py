# -*- coding: utf-8 -*-
"""
敏感信息的安全存储：优先交给操作系统级保险箱，绝不明文写进项目目录。

存两类条目（kind）：
  - "cookie"    智云课堂登录态
  - "api_key"   DeepSeek API key

后端：
- macOS  ：钥匙串 Keychain（`security` 命令），首次访问系统会弹授权框。
- Windows：DPAPI（ctypes 调 crypt32）加密后存到用户目录下的 0600 文件，
           密钥由当前 Windows 账户托管，换账户/换机器无法解密。
- 其它/失败：退化为 0600 明文文件，并打印醒目警告（不推荐，但保证可用）。

存进去的字符串一律先 base64（纯 ASCII），规避 macOS `security` 对非 ASCII
值做 hex 编码的坑；读出来再解码，保证任意内容都能正确往返。
"""
import base64
import os
import subprocess
import sys

SERVICE = "zju-lecture-summarizer"
KINDS = ("cookie", "api_key", "api_config")   # 允许的条目类型

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# 退化明文存储的位置（仅当钥匙串/DPAPI 都不可用时才用）
_FALLBACK_DIR = os.path.join(os.path.expanduser("~"), ".zju_lecture_summarizer")


def _b64e(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _b64d(data):
    return base64.b64decode(data.encode("ascii")).decode("utf-8")


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError(f"未知条目类型：{kind}")


# ------------------------------------------------------------------ macOS

def _mac_save(kind, value_b64):
    # -U：已存在则更新。用列表参数避免 shell 解释特殊字符。
    r = subprocess.run(
        ["security", "add-generic-password", "-U",
         "-a", kind, "-s", SERVICE, "-w", value_b64],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("写入钥匙串失败：" + (r.stderr or "").strip())


def _mac_load(kind):
    r = subprocess.run(
        ["security", "find-generic-password", "-w",
         "-a", kind, "-s", SERVICE],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None            # 不存在或被拒绝授权
    return r.stdout.strip() or None


def _mac_delete(kind):
    subprocess.run(
        ["security", "delete-generic-password",
         "-a", kind, "-s", SERVICE],
        capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------- Windows

def _dpapi(encrypt, data):
    """encrypt=True 加密、False 解密。返回 bytes。失败抛异常。"""
    import ctypes
    import ctypes.wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = (ctypes.windll.crypt32.CryptProtectData if encrypt
          else ctypes.windll.crypt32.CryptUnprotectData)
    ok = fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise RuntimeError("DPAPI " + ("加密" if encrypt else "解密") + "失败")
    res = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return res


def _win_path(kind):
    return os.path.join(_FALLBACK_DIR, f"{kind}.bin")


def _win_save(kind, value_b64):
    os.makedirs(_FALLBACK_DIR, exist_ok=True)
    enc = _dpapi(True, value_b64.encode("ascii"))
    with open(_win_path(kind), "wb") as f:
        f.write(enc)
    try:
        os.chmod(_win_path(kind), 0o600)
    except OSError:
        pass


def _win_load(kind):
    if not os.path.isfile(_win_path(kind)):
        return None
    with open(_win_path(kind), "rb") as f:
        enc = f.read()
    if not enc:
        return None
    return _dpapi(False, enc).decode("ascii")


def _win_delete(kind):
    try:
        os.unlink(_win_path(kind))
    except OSError:
        pass


# ---------------------------------------------------------------- 退化明文

def _plain_path(kind):
    return os.path.join(_FALLBACK_DIR, f"{kind}.b64")


def _plain_save(kind, value_b64):
    os.makedirs(_FALLBACK_DIR, exist_ok=True)
    with open(_plain_path(kind), "w", encoding="ascii") as f:
        f.write(value_b64)
    try:
        os.chmod(_plain_path(kind), 0o600)
    except OSError:
        pass


def _plain_load(kind):
    if not os.path.isfile(_plain_path(kind)):
        return None
    with open(_plain_path(kind), encoding="ascii") as f:
        return f.read().strip() or None


def _plain_delete(kind):
    try:
        os.unlink(_plain_path(kind))
    except OSError:
        pass


# ------------------------------------------------------------------ 后端选择

_BACKEND = None      # 首次成功操作后锁定，避免反复试探


def _pick_backend():
    """返回 'keychain' / 'dpapi' / 'plain'。优先系统级，失败退化。"""
    global _BACKEND
    if _BACKEND:
        return _BACKEND
    if IS_MAC:
        # 探针：写删一条临时值验证钥匙串可用（首次会弹授权框）
        try:
            subprocess.run(
                ["security", "add-generic-password", "-U",
                 "-a", "probe", "-s", SERVICE + "-probe", "-w", "_probe_"],
                capture_output=True, text=True, timeout=120, check=True)
            subprocess.run(
                ["security", "delete-generic-password",
                 "-a", "probe", "-s", SERVICE + "-probe"],
                capture_output=True, text=True, timeout=120)
            _BACKEND = "keychain"
            return _BACKEND
        except Exception:
            _BACKEND = "plain"
            print("警告：无法使用 macOS 钥匙串，敏感信息将退化为本地明文文件"
                  f"（{_FALLBACK_DIR}，权限 600）。")
            return _BACKEND
    if IS_WIN:
        try:
            _dpapi(True, b"probe")
            _BACKEND = "dpapi"
        except Exception:
            _BACKEND = "plain"
            print("警告：DPAPI 不可用，敏感信息将退化为本地明文文件。")
        return _BACKEND
    _BACKEND = "plain"
    print("警告：当前系统无钥匙串支持，敏感信息将退化为本地明文文件"
          f"（{_FALLBACK_DIR}，权限 600）。")
    return _BACKEND


def backend_name():
    """当前实际使用的存储后端，供界面/自检显示。"""
    return {"keychain": "macOS 钥匙串", "dpapi": "Windows DPAPI",
            "plain": "本地明文文件（不安全）"}.get(_pick_backend(), "未知")


# ------------------------------------------------------------------ 对外 API

def save_secret(kind, value):
    """把一条敏感字符串安全存起来（kind ∈ cookie / api_key）。"""
    _check_kind(kind)
    value = (value or "").strip()
    if not value:
        raise ValueError("值为空，拒绝保存")
    v = _b64e(value)
    b = _pick_backend()
    if b == "keychain":
        _mac_save(kind, v)
    elif b == "dpapi":
        _win_save(kind, v)
    else:
        _plain_save(kind, v)


def load_secret(kind):
    """读回一条敏感字符串；没有则返回 None。"""
    _check_kind(kind)
    b = _pick_backend()
    try:
        if b == "keychain":
            v = _mac_load(kind)
        elif b == "dpapi":
            v = _win_load(kind)
        else:
            v = _plain_load(kind)
    except Exception:
        return None
    if not v:
        return None
    try:
        return _b64d(v)
    except Exception:
        return None


def delete_secret(kind):
    """删除一条已保存的敏感信息（三种后端都清一遍，确保干净）。"""
    _check_kind(kind)
    for fn in (_mac_delete, _win_delete, _plain_delete):
        try:
            fn(kind)
        except Exception:
            pass


# 语义化别名（登录态 / API key）
def save_cookie(cookie):
    save_secret("cookie", cookie)


def load_cookie():
    return load_secret("cookie")


def has_cookie():
    return load_cookie() is not None


def clear_cookie():
    delete_secret("cookie")


def save_api_key(key):
    save_secret("api_key", key)


def load_api_key():
    return load_secret("api_key")


def clear_api_key():
    delete_secret("api_key")


def save_api_config(cfg_json):
    """保存 API 提供商配置（JSON 字符串：id/base/chat/vision）。"""
    save_secret("api_config", cfg_json)


def load_api_config():
    """读回 API 提供商配置 JSON 字符串；没有返回 None。"""
    return load_secret("api_config")


if __name__ == "__main__":
    # 自检：两类条目各存一条再读回，确认往返一致
    for kind, fn_save, fn_load in (
            ("cookie", save_cookie, load_cookie),
            ("api_key", save_api_key, load_api_key)):
        test = f"test-{kind}=1; 中文=值"
        fn_save(test)
        got = fn_load()
        print(f"[{kind}] 后端：{backend_name()} | 往返一致：{got == test}")
        delete_secret(kind)
        print(f"[{kind}] 清除后：{fn_load()}")
