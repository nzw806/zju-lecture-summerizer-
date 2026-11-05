# -*- coding: utf-8 -*-
"""
按用户选择只读查询本机 Chrome / Edge 中适用于指定网站的 Cookie。
仅支持明文及已有的旧版解密方式；新版 Windows AES-GCM / App-Bound
格式明确提示手动复制，不绕过浏览器保护，也不混合不同配置的登录态。
"""
import glob
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

IS_WIN = sys.platform == "win32"
EDGE_ROOT_MAC = os.path.expanduser(
    "~/Library/Application Support/Microsoft Edge")
EDGE_ROOT_WIN = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "Microsoft", "Edge", "User Data")
KEYCHAIN_SERVICE = "Microsoft Edge Safe Storage"
KEYCHAIN_ACCOUNT = "Microsoft Edge"
CHROME_ROOT_MAC = os.path.expanduser("~/Library/Application Support/Google/Chrome")
CHROME_ROOT_WIN = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                               "Google", "Chrome", "User Data")
BROWSER_NAMES = {"chrome": "Chrome", "edge": "Edge"}
TARGET_DOMAIN = "classroom.zju.edu.cn"


class CookieReadError(RuntimeError):
    pass


class CookieEncryptionError(CookieReadError):
    pass


def find_cookie_databases(browser):
    if browser not in BROWSER_NAMES:
        raise CookieReadError("请选择 Chrome 或 Edge")
    if IS_WIN:
        root = CHROME_ROOT_WIN if browser == "chrome" else EDGE_ROOT_WIN
    elif sys.platform == "darwin":
        root = CHROME_ROOT_MAC if browser == "chrome" else EDGE_ROOT_MAC
    else:
        raise CookieReadError("自动读取目前仅支持 Windows 和 macOS，请手动复制 Cookie")
    dbs = (glob.glob(os.path.join(root, "*", "Network", "Cookies"))
           + glob.glob(os.path.join(root, "*", "Cookies")))
    return sorted(dict.fromkeys(dbs), key=lambda p: ("Default" not in Path(p).parts, p))


# --------------------------------------------------------------- AES-128
# 只需要解密（CBC），实现逆密码。按 FIPS-197 测试向量验证过。

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
_INV_SBOX = bytes(_SBOX.index(i) for i in range(256))
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36)


def _xtime(a):
    a <<= 1
    return a ^ 0x1b if a & 0x100 else a


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p & 0xff


def _key_expansion(key):
    """返回 11 个轮密钥，每个 = 4 个字（每字 4 字节），对应 state 的 4 列。"""
    w = [list(key[4 * i:4 * i + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [w[4 * r:4 * r + 4] for r in range(11)]


def _add_round_key(state, rk):
    for c in range(4):
        for r in range(4):
            state[r][c] ^= rk[c][r]


def _inv_shift_rows(s):
    s[1] = s[1][3:] + s[1][:3]
    s[2] = s[2][2:] + s[2][:2]
    s[3] = s[3][1:] + s[3][:1]


def _inv_mix_columns(s):
    for c in range(4):
        a = s[0][c], s[1][c], s[2][c], s[3][c]
        s[0][c] = _gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9)
        s[1][c] = _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13)
        s[2][c] = _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11)
        s[3][c] = _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)


def _aes128_decrypt_block(block, round_keys):
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _add_round_key(s, round_keys[10])
    for rnd in range(9, 0, -1):
        _inv_shift_rows(s)
        s = [[_INV_SBOX[b] for b in row] for row in s]
        _add_round_key(s, round_keys[rnd])
        _inv_mix_columns(s)
    _inv_shift_rows(s)
    s = [[_INV_SBOX[b] for b in row] for row in s]
    _add_round_key(s, round_keys[0])
    return bytes(s[r][c] for c in range(4) for r in range(4))


def aes128_cbc_decrypt(key, iv, data):
    rks = _key_expansion(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        blk = _aes128_decrypt_block(data[i:i + 16], rks)
        out += bytes(b ^ p for b, p in zip(blk, prev))
        prev = data[i:i + 16]
    pad = out[-1]
    if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
        out = out[:-pad]
    return bytes(out)


# --------------------------------------------------------------- Edge 读取

def _keychain_password(browser="edge"):
    """macOS 通过系统授权读取所选浏览器的 Safe Storage 密钥。"""
    service = "Chrome Safe Storage" if browser == "chrome" else KEYCHAIN_SERVICE
    account = "Chrome" if browser == "chrome" else KEYCHAIN_ACCOUNT
    r = subprocess.run(
        ["security", "find-generic-password", "-w",
         "-s", service, "-a", account],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(
            "无法从钥匙串读取所选浏览器密钥（可能点了拒绝）。"
            "请重试并在弹窗中选择『允许』；或改用手动粘贴 Cookie。")
    return r.stdout.rstrip("\n")


def _dpapi_decrypt(data):
    """Windows：用系统 DPAPI 解密（ctypes 调 crypt32，无需第三方库）。"""
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, None, None, None, 0,
            ctypes.byref(out)):
        raise RuntimeError("DPAPI 解密失败（需在登录本机的 Windows 账户下运行）")
    res = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return res


def _decrypt_value(raw, key, host=""):
    raw = bytes(raw)
    if IS_WIN:
        if raw.startswith(b"v20"):
            raise CookieEncryptionError("浏览器使用应用绑定加密（v20），本工具无法自动解密，请按下方步骤手动复制 Cookie")
        if raw[:3] in (b"v10", b"v11"):
            raise CookieEncryptionError("浏览器使用新版 AES-GCM Cookie 加密，本工具暂不支持，请按下方步骤手动复制 Cookie")
        return _dpapi_decrypt(raw).decode("utf-8")
    if raw[:3] in (b"v10", b"v11"):
        dk = hashlib.pbkdf2_hmac("sha1", key.encode("utf-8"), b"saltysalt", 1003, 16)
        value = aes128_cbc_decrypt(dk, b" " * 16, raw[3:])
        # Chromium cookie DB v24+ binds encrypted values to host_key.
        prefix = hashlib.sha256(host.encode("utf-8")).digest()
        if host and value.startswith(prefix):
            value = value[len(prefix):]
        return value.decode("utf-8")
    if raw.startswith(b"v20"):
        raise CookieEncryptionError("不支持此 Cookie 加密格式，请手动复制 Cookie")
    return raw.decode("utf-8")


def _cookie_hosts(domain):
    # For classroom.zju.edu.cn, parent-domain cookies apply, but authserver,
    # unrelated zju subdomains, and suffix lookalikes must never be queried.
    domain = domain.lower().lstrip(".").rstrip(".")
    if domain != TARGET_DOMAIN:
        raise CookieReadError("自动读取仅限智云课堂 classroom.zju.edu.cn")
    return [domain, "." + domain, ".zju.edu.cn"]


def _read_cookie_rows(db, domain):
    hosts = _cookie_hosts(domain)
    placeholders = ",".join("?" for _ in hosts)
    now = int((time.time() + 11644473600) * 1000000)
    # Read the real DB with its WAL, without copying all browser cookies,
    # disabling browser protection, or using immutable/stale snapshots.
    uri = Path(db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as con:
        return con.execute(
            "SELECT host_key, name, encrypted_value, value FROM cookies "
            f"WHERE host_key IN ({placeholders}) "
            "AND (expires_utc = 0 OR expires_utc > ?) ORDER BY length(host_key) DESC",
            (*hosts, now)).fetchall()


def read_browser_cookies(browser="chrome", domain=TARGET_DOMAIN):
    _cookie_hosts(domain)
    dbs = find_cookie_databases(browser)
    label = BROWSER_NAMES[browser]
    if not dbs:
        raise CookieReadError(f"没找到 {label} 的 Cookie 数据库，请确认浏览器及其用户配置；也可手动复制 Cookie")
    blocked = False
    for db in dbs:
        try:
            rows = _read_cookie_rows(db, domain)
        except (OSError, sqlite3.Error):
            blocked = True
            continue
        if not rows:
            continue
        key = None
        if not IS_WIN and any(enc and not plain for _, _, enc, plain in rows):
            key = _keychain_password(browser)
        pairs, seen = [], set()
        for host, name, enc, plain in rows:
            try:
                val = plain or (_decrypt_value(enc, key, host) if enc else "")
            except CookieEncryptionError as e:
                raise CookieReadError(f"{label}：{e}") from None
            except Exception:
                raise CookieReadError(f"无法解密 {label} 的登录态，请手动复制 Cookie；不会返回不完整的登录信息") from None
            if not val or name in seen:
                continue
            if any(c in name + val for c in "\r\n"):
                raise CookieReadError("Cookie 包含无效控制字符，请手动复制 Cookie")
            seen.add(name)
            pairs.append(f"{name}={val}")
        if pairs:
            return "; ".join(pairs)  # Never combine separate browser profiles.
    if blocked:
        raise CookieReadError(
            f"{label} 正在保护或占用 Cookie 数据库（或当前用户无权读取）。"
            "建议直接按下方步骤手动复制；也可自行完全退出该浏览器后再试，"
            "但新版加密仍可能不支持。程序不会自动关闭浏览器。")
    raise CookieReadError(f"{label} 中没有可用的智云课堂 Cookie，请在该浏览器登录后刷新课堂页面，或手动复制 Cookie")


def read_edge_cookies(domain=TARGET_DOMAIN):
    return read_browser_cookies("edge", domain)


if __name__ == "__main__":
    browser = sys.argv[1] if len(sys.argv) > 1 else "chrome"
    cookie = read_browser_cookies(browser)
    print(f"读取完成：{len(cookie.split('; '))} 个 Cookie（不输出登录凭证）")
