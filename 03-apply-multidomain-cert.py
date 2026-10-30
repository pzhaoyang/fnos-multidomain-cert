#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
03-apply-multidomain-cert.py —— 在 fnOS NAS 上直接申请多域名证书（含通配符）

原理（与 02-fnos-cert-api.js 相同）：
    自签多 SAN 种子证书 → cert.upload 入库 → cert.replace(带 renewal:1 + relatedDDNSId)
    → cert.renew 触发 fnOS 自己的 ACME 引擎（DNS-01）按全部 SAN 签发真 Let's Encrypt 证书
    之后由 fnOS 自动续签（renewal:1），无需本脚本参与。

依赖：python3（标准库）+ openssl（加密登录 + 生成种子证书）

用法（两种认证方式任选其一）：
    # 方式 A（推荐）：密码自动登录，完全自动化
    export FNOS_USER='你的用户名'
    export FNOS_PASS='你的密码'
    export FNOS_WS_PORT='5667'          # 按需：改过 HTTPS 端口才需要
    python3 03-apply-multidomain-cert.py --test
    python3 03-apply-multidomain-cert.py -d "example.com,*.example.com" -r 1

    # 方式 B（备选）：手动从浏览器取 secret + cookie
    export FNOS_SECRET='<fnos-Secret>'
    export FNOS_COOKIE='<Cookie 请求头整行>'
    python3 03-apply-multidomain-cert.py --test

按需追加的环境变量：
    export FNOS_ACCESS_CODE='你的访问码'   # NAS 开了「访问码/访问保护」时
    export FNOS_2FA_CODE='123456'         # 账号开了「双重验证」时，30 秒内有效

如果 NAS 开启了「访问码（访问保护）」，握手会在 nginx 层被 401 拦下
（响应头 X-Trim-Safe-Code-Challenge: 1）。此时再补一个环境变量即可：
    export FNOS_ACCESS_CODE='你的访问码'
脚本会先调用 /access_code_verify 换取会话 Cookie，再建立 WS 连接。

如果账号开启了「双重验证（2FA）」，登录只返回 accessToken、不返回 secret，
此时需再补：
    export FNOS_2FA_CODE='验证器里的 6 位码'
脚本会自动补一步 user.2fa.loginVerify 换取 secret，然后照常签发。

协议要点（全部实测得出）：
    - 密码登录：getRSAPub → RSA-OAEP-SHA256 加密随机 AES-256 密钥 →
      AES-256-CBC 加密 user.login（明文密码，deviceType=Browser, v:1）→ 拿 secret
    - 签名帧：base64(HMAC-SHA256(json, secret)) + json；reqid 28 位 hex 即可
    - Cookie 方式：WS 握手必须携带会话 Cookie（连接绑定），再 getSI → authToken

注意：
    - 通配符 *.a.b 只覆盖一层（x.a.b），兄弟子域（imp.b）要显式列进 -d
    - Let's Encrypt 配额：50 域名/周、同名集合 5 次/周
    - FNOS_COOKIE / FNOS_SECRET 等同登录凭据，不要提交到 git/共享
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def _default_candidates():
    """默认候选端点：本机 loopback 的 HTTPS 端口。
    fnOS 系统默认 HTTPS 访问端口是 5667；若你改过端口，用环境变量 FNOS_WS_PORT 覆盖。"""
    port = os.environ.get("FNOS_WS_PORT", "").rstrip("\r\n") or "5667"
    return [
        f"wss://127.0.0.1:{port}/websocket?type=main",
    ]


DEFAULT_CANDIDATES = _default_candidates()


# ---------------- 访问码（访问保护）----------------
def _verify_access_code(host, port, scheme, code, timeout=15):
    """fnOS「访问码」开启时，握手前必须先换取会话 Cookie。

    网页做法：GET /access_code_verify
              x-access-code: base64(utf8(访问码))
              x-access-source: web
    成功返回 200 + Set-Cookie（会话凭据），失败返回 401/403/429。
    这里把 Set-Cookie 全部抽出来，拼成后续请求的 Cookie 头。
    """
    raw = socket.create_connection((host, port), timeout=timeout)
    if scheme == "wss":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = ctx.wrap_socket(raw, server_hostname=host)
    ac = base64.b64encode(code.encode()).decode()
    raw.sendall((
        f"GET /access_code_verify HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"x-access-code: {ac}\r\nx-access-source: web\r\n"
        f"Connection: close\r\n\r\n"
    ).encode())
    data = b""
    try:
        while True:
            chunk = raw.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        try:
            raw.close()
        except OSError:
            pass

    lines = data.split(b"\r\n")
    status = lines[0].decode(errors="replace") if lines else ""
    parts = status.split(" ")
    code_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    # 实测：校验成功返回 204 No Content（不是 200），所以按 2xx 判断
    if not (200 <= code_num < 300):
        if code_num in (401, 403):
            raise RuntimeError("访问码校验失败：访问码不正确（HTTP %d）" % code_num)
        if code_num == 429:
            raise RuntimeError("访问码校验失败：尝试次数过多被限流（HTTP 429），稍后再试")
        raise RuntimeError("访问码校验失败：" + (status or "无响应"))

    jar = []
    for ln in lines[1:]:
        s = ln.decode(errors="replace")
        if s.lower().startswith("set-cookie:"):
            kv = s[len("set-cookie:"):].strip().split(";", 1)[0]
            if "=" in kv:
                jar.append(kv)
    return "; ".join(jar), status


# ---------------- WebSocket 客户端（标准库） ----------------
class WS:
    def __init__(self, url, cookie="", access_code=""):
        u = urlparse(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path + (("?" + u.query) if u.query else "")
        raw = socket.create_connection((host, port), timeout=15)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE      # NAS 本地可能自签
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        ac_hdr = ""
        if access_code:
            ac_cookie, _ = _verify_access_code(host, port, u.scheme, access_code)
            if ac_cookie:
                cookie = "; ".join(x for x in (cookie, ac_cookie) if x)
            # 有的网关也直接认请求头上的访问码，双保险
            ac_hdr = (f"x-access-code: {base64.b64encode(access_code.encode()).decode()}\r\n"
                      f"x-access-source: web\r\n")
        cookie_hdr = f"Cookie: {cookie}\r\n" if cookie else ""
        raw.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"{cookie_hdr}"
            f"{ac_hdr}"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = raw.recv(4096)
            if not chunk:
                raise RuntimeError("WS 握手：连接被关闭")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            first = resp.split(b"\r\n", 1)[0].decode(errors="replace")
            head = resp[:2048].decode(errors="replace")
            if "X-Trim-Safe-Code-Challenge" in head:
                raise RuntimeError(
                    "WS 握手失败: " + first +
                    "\n  该 fnOS 开启了「访问码 / 访问保护」，请求在 nginx 层就被拦截了"
                    "（响应头 X-Trim-Safe-Code-Challenge: 1），脚本的密码登录还没机会执行。"
                    "\n  解决（任选其一）："
                    "\n    a) export FNOS_ACCESS_CODE='你的访问码'  —— 脚本会先换会话 Cookie 再握手"
                    "\n    b) fnOS 网页 → 安全性 → 访问设置 → 关闭访问码，然后重试"
                )
            raise RuntimeError("WS 握手失败: " + first)
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def send(self, text):
        payload = text.encode()
        mask = os.urandom(4)
        hdr = bytearray([0x81])
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        hdr += mask
        hdr += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(hdr))

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WS 连接已关闭")
            self.buf += chunk

    def recv_text(self, timeout=15):
        self.sock.settimeout(timeout)
        while True:
            self._need(2)
            b0, b1 = self.buf[0], self.buf[1]
            opcode, ln = b0 & 0x0F, b1 & 0x7F
            off = 2
            if ln == 126:
                self._need(off + 2)
                ln = struct.unpack(">H", self.buf[off:off + 2])[0]
                off += 2
            elif ln == 127:
                self._need(off + 8)
                ln = struct.unpack(">Q", self.buf[off:off + 8])[0]
                off += 8
            self._need(off + ln)
            data = self.buf[off:off + ln]
            self.buf = self.buf[off + ln:]
            if opcode == 0x9:                      # ping → pong
                mask = os.urandom(4)
                self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + mask
                                 + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                continue
            if opcode == 0x8:
                raise RuntimeError("WS 被服务端关闭")
            if opcode == 0x1:
                return data.decode()


# ---------------- API 客户端 ----------------
class TwofaRequired(RuntimeError):
    """账号开了双重验证：登录只拿到 accessToken，还差一步验证码"""

    def __init__(self, resp):
        self.resp = resp
        self.access_token = resp.get("accessToken") or ""
        super().__init__("该 fnOS 账号开启了双重验证（2FA），需要验证码才能继续")


class FnosCert:
    def __init__(self, server, secret_b64="", cookie="", access_code=""):
        self.ws = WS(server, cookie, access_code)
        self._counter = random.randint(0x1000, 0xEFFFFF)
        self.secret = base64.b64decode(secret_b64) if secret_b64 else b""
        self.device_name = "Linux-Chrome"
        self.did = ""
        self.access_token = ""
        self.login_uid = None
        self.login_admin = None

    # ---------- 加密帧：user.login / user.2fa.loginVerify 这类敏感请求 ----------
    def _encrypted_call(self, req, flat, timeout=20):
        """发送一条「加密帧」请求，返回 (响应, aes_key, iv)。

        格式：{"req":"encrypted","iv":b64(iv),"rsa":b64(RSA(aes_key)),"aes":b64(AES(明文)),"v":1}
        - 每次请求都用**新的** 32 字节 AES 密钥 + 16 字节 IV
        - AES 密钥用服务端 RSA 公钥按 OAEP-SHA256 加密
        - 明文 = 请求对象本身（含 si），AES-256-CBC + PKCS7
        """
        obj = {"reqid": self._reqid(), "req": "util.crypto.getRSAPub"}
        self.ws.send(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        r0 = self._wait(obj["reqid"])
        pub = r0.get("pub") or ""
        si = str(r0.get("si") or "")
        if not pub:
            raise RuntimeError("getRSAPub 失败: " + json.dumps(r0))

        work = tempfile.mkdtemp(prefix="fnos-enc-")
        try:
            pubf = os.path.join(work, "pub.pem")
            with open(pubf, "w") as f:
                f.write(pub)
            aes_key = os.urandom(32)
            iv = os.urandom(16)
            keyf = os.path.join(work, "key.bin")
            encf = os.path.join(work, "key.enc")
            with open(keyf, "wb") as f:
                f.write(aes_key)
            subprocess.run(
                ["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", pubf,
                 "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
                 "-pkeyopt", "rsa_mgf1_md:sha256", "-in", keyf, "-out", encf],
                check=True, capture_output=True,
            )
            rsa_b64 = base64.b64encode(open(encf, "rb").read()).decode()

            inner = {"reqid": self._reqid(), "req": req}
            inner.update(flat)
            inner["si"] = si
            ij = json.dumps(inner, separators=(",", ":"), ensure_ascii=False)
            enc = subprocess.run(
                ["openssl", "enc", "-aes-256-cbc", "-K", aes_key.hex(), "-iv", iv.hex(),
                 "-base64", "-A"], input=ij.encode(), capture_output=True, check=True,
            )
            frame = {"req": "encrypted", "iv": base64.b64encode(iv).decode(),
                     "rsa": rsa_b64, "aes": enc.stdout.decode().strip(), "v": 1}
            self.ws.send(json.dumps(frame, separators=(",", ":"), ensure_ascii=False))
            return self._wait(inner["reqid"], timeout=timeout), aes_key, iv
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _unwrap_secret(r, aes_key, iv):
        """响应里的 secret 是 AES 加密的 HMAC 密钥：解密 + 去 PKCS7 padding。

        去掉 padding 后的原始字节就是签帧用的 HMAC key（base64 后等于网页的 fnos-Secret）。
        """
        secret_b64 = r.get("secret") or ""
        if not secret_b64:
            return None
        dec = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-K", aes_key.hex(), "-iv", iv.hex()],
            input=base64.b64decode(secret_b64), capture_output=True,
        )
        raw = dec.stdout
        padlen = raw[-1] if raw else 0
        if 0 < padlen <= 16:
            raw = raw[:-padlen]
        return raw

    def login(self, username, password):
        """密码登录。

        若账号开了双重验证（2FA），服务端只返回 accessToken、不返回 secret，
        此时抛 TwofaRequired，由调用方用验证码补一步 twofa_verify()。
        """
        self.did = "x" + "".join(random.choice("0123456789abcdef") for _ in range(20))
        payload = {
            "user": username, "password": password,
            "deviceName": self.device_name, "deviceType": "Browser",
            "stay": True, "did": self.did,
        }
        r, key, iv = self._encrypted_call("user.login", payload)
        errno = r.get("errno")
        if r.get("result") != "succ":
            if errno == 131072:
                raise RuntimeError("登录失败：用户名或密码错误（131072）。请检查 FNOS_USER / FNOS_PASS")
            raise RuntimeError(f"登录失败: errno={errno} {r.get('errmsg')}")

        raw_secret = self._unwrap_secret(r, key, iv)
        if not raw_secret:
            # 没给 secret 说明卡在 2FA（isTwofaEnforced / isBindTwofaSecret 且设备不受信）
            if r.get("accessToken"):
                raise TwofaRequired(r)
            raise RuntimeError("登录响应缺少 secret: " + json.dumps({k: v for k, v in r.items() if k != 'secret'}))

        self.secret = raw_secret
        self.login_uid = r.get("uid")
        self.login_admin = r.get("admin")
        return r

    def twofa_verify(self, code, access_token=None, trusted=False):
        """提交 2FA 验证码，换取真正的 secret。

        payload 照抄网页 useTfaLogin 的实现（字段一个都不能少）：
        {accessToken, code, isTrustedDevice, stay, deviceName, deviceType, did}
        """
        token = access_token or self.access_token
        if not token:
            raise RuntimeError("2FA 验证缺少 accessToken（应先调用 login()）")
        base = {
            "accessToken": token,
            "code": str(code).strip(),
            "isTrustedDevice": bool(trusted),
            "stay": 1,
            "deviceName": self.device_name,
            "deviceType": "Browser",
            "did": self.did,
        }
        r, key, iv = self._encrypted_call("user.2fa.loginVerify", base)
        # 万一服务端要 data 包一层（不同小版本可能不同），参数类错误时自动重试一次
        if r.get("errno") in (8192, 3000001):
            r, key, iv = self._encrypted_call("user.2fa.loginVerify", {"data": base})

        errno = r.get("errno")
        if r.get("result") != "succ":
            if errno == 135168:
                raise RuntimeError("双重验证码不正确或已过期（135168），请重新获取验证码")
            if errno == 102570109:
                raise RuntimeError("2FA 凭据已过期（102570109），请重新登录")
            raise RuntimeError(f"2FA 验证失败: errno={errno} {r.get('errmsg')}")

        raw_secret = self._unwrap_secret(r, key, iv)
        if not raw_secret:
            raise RuntimeError("2FA 响应缺少 secret: " + json.dumps({k: v for k, v in r.items() if k != 'secret'}))
        self.secret = raw_secret
        self.login_uid = r.get("uid") or self.login_uid
        return r

    def _reqid(self):
        self._counter += 1
        return f"{int(time.time()):08x}{random.getrandbits(32):08x}{self._counter:012x}"

    def _sign(self, obj):
        j = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        mac = hmac.new(self.secret, j.encode(), hashlib.sha256).digest()
        return base64.b64encode(mac).decode() + j

    def _wait(self, reqid, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = json.loads(self.ws.recv_text(timeout=max(1, deadline - time.time())))
            if r.get("reqid") == reqid:
                return r
        raise RuntimeError(f"reqid={reqid} 超时无响应")

    def call(self, req, data=None, flat=None, timeout=20):
        obj = {"reqid": self._reqid(), "req": req}
        if data is not None:
            obj["data"] = data
        if flat:
            obj.update(flat)
        self.ws.send(self._sign(obj))
        return self._wait(obj["reqid"], timeout)

    def handshake(self):
        # 1) util.getSI —— 明文帧
        obj = {"reqid": self._reqid(), "req": "util.getSI"}
        self.ws.send(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        r = self._wait(obj["reqid"])
        si = str(r.get("si") or "")
        if not si:
            raise RuntimeError("getSI 失败: " + json.dumps(r))
        print(f"      si={si}")
        # 2) user.authToken —— 签名帧
        r = self.call("user.authToken", flat={"main": True, "si": si})
        if r.get("result") != "succ":
            if r.get("errno") == 65534:
                raise RuntimeError(
                    "authToken 失败: errno 65534（连接未绑定会话）。\n"
                    "  请 export FNOS_COOKIE='...' —— 取【完整】Cookie 请求头整行值：\n"
                    "  浏览器 DevTools → Network → 任意请求 → Request Headers → cookie"
                )
            raise RuntimeError("authToken 失败: " + json.dumps(r))
        print(f"      已认证 uid={r.get('uid')} admin={r.get('admin')}")
        return r

    def must(self, r, what):
        if r.get("result") != "succ":
            if r.get("errno") == 65534:
                raise RuntimeError(f"{what} 失败: errno 65534（连接未绑定会话——检查 FNOS_COOKIE 是否完整）")
            raise RuntimeError(f"{what} 失败: {r.get('errno')} {r.get('errmsg')}")
        return r.get("data")


# ---------------- 种子证书 ----------------
def gen_seed_cert(domains):
    cn = domains[0]
    san_ext = ",".join(f"DNS:{d}" for d in domains)
    work = tempfile.mkdtemp(prefix="fnos-seed-")
    crt = os.path.join(work, "seed.crt")
    key = os.path.join(work, "seed.key")
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", crt, "-days", "365",
             "-subj", f"/CN={cn}",
             # 关键：明确 CA:FALSE，否则自签证书默认 CA:TRUE，
             # fnOS 的 lego 在 cert.renew 时读到「第一个证书是 CA」会报
             # "Certificate bundle starts with a CA certificate"
             "-addext", f"subjectAltName={san_ext}",
             "-addext", "basicConstraints=critical,CA:FALSE",
             "-addext", "keyUsage=critical,digitalSignature,keyEncipherment"],
            check=True, capture_output=True,
        )
        with open(crt, "rb") as f:
            cert_b64 = base64.b64encode(f.read()).decode()
        with open(key, "rb") as f:
            key_b64 = base64.b64encode(f.read()).decode()
        return cert_b64, key_b64
    finally:
        for p in (crt, key):
            try:
                os.remove(p)
            except OSError:
                pass
        os.rmdir(work)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="fnOS 多域名证书申请（一张证书覆盖多域名 + 通配符）")
    ap.add_argument("-d", "--domains", help="逗号分隔的域名列表（可含通配符）")
    ap.add_argument("-r", "--related-ddns-id", type=int, default=1,
                    help="DDNS 记录 id（DNS-01 解析商凭证），默认 1")
    ap.add_argument("-s", "--server", default="",
                    help="WS 地址；留空自动探测本机 HTTPS 端口（FNOS_WS_PORT 可指定端口，默认 5667）")
    ap.add_argument("--desc", default="multidomain-cert", help="证书备注（≤32 字符）")
    ap.add_argument("--test", action="store_true", help="只测连通性（列证书，不写任何东西）")
    args = ap.parse_args()

    username = os.environ.get("FNOS_USER", "").rstrip("\r\n")
    password = os.environ.get("FNOS_PASS", "").rstrip("\r\n")
    secret = os.environ.get("FNOS_SECRET", "").rstrip("\r\n")
    cookie = os.environ.get("FNOS_COOKIE", "").rstrip("\r\n")
    access_code = os.environ.get("FNOS_ACCESS_CODE", "").rstrip("\r\n")
    twofa_code = os.environ.get("FNOS_2FA_CODE", "").rstrip("\r\n")

    use_password = bool(username and password)
    if not use_password and not secret:
        sys.exit("错误：请设置 FNOS_USER+FNOS_PASS（自动登录），或 FNOS_SECRET（手动从浏览器取）")

    if not args.test and not args.domains:
        sys.exit("错误：-d 必填（逗号分隔域名列表），或用 --test 只测连通")
    domains = [x.strip() for x in (args.domains or "").split(",") if x.strip()]

    cert_b64 = key_b64 = ""
    if not args.test:
        print(f"[1/6] 生成多 SAN 种子证书（{','.join(domains)}）...")
        cert_b64, key_b64 = gen_seed_cert(domains)

    # 连接（自动探测端点——可用 FNOS_WS_PORT 指定端口，默认 fnOS 系统 HTTPS 端口）
    candidates = [args.server] if args.server else DEFAULT_CANDIDATES
    api = last_err = None
    for u in candidates:
        try:
            api = FnosCert(u, secret if not use_password else "", cookie, access_code)
            print("[0/6] WS 已连接:", u)
            if use_password:
                print("      密码自动登录中...")
                try:
                    api.login(username, password)
                except TwofaRequired as e:
                    api.access_token = e.access_token
                    print("      该账号开启了双重验证（2FA）")
                    if not twofa_code:
                        sys.exit(
                            "需要 2FA 验证码：请 export FNOS_2FA_CODE='验证器里的 6 位数字' 后重跑。\n"
                            "  （验证码 30 秒内有效，所以脚本一次跑完：登录 → 提交验证码 → 调接口）"
                        )
                    print("      提交验证码...")
                    api.twofa_verify(twofa_code)
                print(f"      登录成功 uid={api.login_uid} admin={api.login_admin}")
            break
        except Exception as e:
            print(f"      端点不可用或登录失败 {u}: {e}")
            last_err = e
            api = None
    if not api:
        sys.exit(f"所有端点均不可用，最后一个错误: {last_err}")

    if not use_password:
        api.handshake()

    if args.test:
        rows = api.must(api.call("appcgi.netsvr.cert.list"), "cert.list")
        print(f"连通性 OK，共 {len(rows)} 张证书：")
        for e in rows:
            print(f"  id={e['id']:<3} issuedBy={e.get('issuedBy', ''):<28} SAN={e.get('SAN', '')}")
        return

    print("[2/6] cert.upload 导入种子证书...")
    api.must(api.call("appcgi.netsvr.cert.upload", {
        "privateKeyBase64": key_b64, "certificateBase64": cert_b64,
        "issuerCertificateBase64": "", "desc": args.desc, "isDefault": 0,
        "privateKeyPath": "", "certificatePath": "", "issuerCertificatePath": "",
    }), "cert.upload")

    print("[3/6] 定位新条目 id...")
    rows = api.must(api.call("appcgi.netsvr.cert.list"), "cert.list")
    entry = next((e for e in rows if e.get("desc") == args.desc), None)
    if not entry:
        sys.exit(f"未在列表中找到 desc={args.desc} 的新条目")
    print(f"      id={entry['id']}  SAN={entry.get('SAN', '')}")

    print(f"[4/6] cert.replace 赋予续签资格 (renewal:1 + relatedDDNSId={args.related_ddns_id})...")
    api.must(api.call("appcgi.netsvr.cert.replace", {
        "privateKeyBase64": key_b64, "certificateBase64": cert_b64,
        "issuerCertificateBase64": "", "desc": args.desc, "id": entry["id"], "isDefault": 0,
        "privateKeyPath": "", "certificatePath": "", "issuerCertificatePath": "",
        "renewal": 1, "relatedDDNSId": args.related_ddns_id,   # ← 关键：这两个字段
    }), "cert.replace")

    print("[5/6] cert.renew 触发 fnOS ACME 签发...")
    proc = api.must(api.call("appcgi.netsvr.cert.renew", {"id": entry["id"]}), "cert.renew")
    print(f"      processId={proc}")

    print("[6/6] 轮询签发任务（DNS-01，通常 20~90 秒）...")
    for _ in range(36):
        time.sleep(5)
        st = api.must(api.call("appcgi.netsvr.cert.applystatus", {"processId": proc}), "applystatus")
        status = st.get("status")
        print(f"      status={status}")
        if status in ("success", "suc"):
            break
        if status in ("fail", "failed"):
            # 打印完整响应，拿到真实失败原因
            print("      applystatus 完整响应:", json.dumps(st, ensure_ascii=False))
            # 失败信息可能在多个字段里
            errmsg = st.get("errMsg") or st.get("errmsg") or st.get("msg") or st.get("error") or ""
            errcode = st.get("errCode") or st.get("errcode") or ""
            sys.exit(f"签发失败: {errmsg} (errCode={errcode})")
    else:
        sys.exit("等待超时，稍后可用 cert.list 查看结果")

    rows = api.must(api.call("appcgi.netsvr.cert.list"), "cert.list")
    final = next(e for e in rows if e["id"] == entry["id"])
    print("DONE.")
    print(f"  issuedBy = {final.get('issuedBy')}")
    print(f"  SAN      = {final.get('SAN')}")
    print(f"  renewal  = {final.get('renewal')}   (1=fnOS 自动续签)")
    print("可在 fnOS 网页「安全性→证书→服务配置」把新证书绑定到反向代理等服务。")


if __name__ == "__main__":
    main()
