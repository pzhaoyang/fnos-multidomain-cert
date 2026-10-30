# fnOS 多域名证书申请（一张证书覆盖多域名 + 通配符）

> **仅供个人学习与技术研究使用。** 本项目基于对 fnOS 官方 API 协议的分析整理而成，
> 请仅在你拥有合法使用权的设备上使用；使用本项目产生的一切后果由使用者自行承担。

> 完整背景、原理、协议细节、踩坑记录见 **[NOTES.md](NOTES.md)**。
> 本文档是「现在该怎么做」的快速上手。

![效果截图](fnos-multidomain-screenshot.png)

## 你要做的事（一句话）

在 fnOS NAS 上跑一个 Python 脚本，用**密码自动登录**，一张证书签出多个域名（含通配符），
之后由 fnOS 自动续签。**全程不需要操作浏览器、不需要手动申请。**

## 快速开始

### 1. 先确认端口

脚本默认连 **本机 HTTPS 访问端口 `5667`**（fnOS 系统默认值；HTTP 是 `5666`）。
判断方法：

- 浏览器能打开 `https://<NAS地址>:<端口>` —— 那个端口就是；
- 或看 fnOS 网页「安全性 → 访问设置」里的 HTTP/HTTPS 端口。

改过端口就必须告诉脚本，两种方式等价（`-s` 优先）：

```bash
export FNOS_WS_PORT='5667'      # 只改端口，走本机 loopback
# 或给完整地址（走域名、反代端口也行）
python3 03-apply-multidomain-cert.py -s 'wss://127.0.0.1:5667/websocket?type=main' --test
```

> 端口填错的表现是 `Connection refused` / 超时，**不是** `401` —— 两者别混。

### 2. 设置环境变量

下面两组按需叠加，**没开的功能不要设**：

```bash
# —— 密码自动登录（推荐）——
export FNOS_USER='你的用户名'
export FNOS_PASS='你的网页登录密码'

# —— 按需：NAS 开了「访问码 / 访问保护」——
export FNOS_ACCESS_CODE='你的访问码'

# 2FA 验证码不用设：脚本会直接提示你输入（见下文）
```

### 3. 只读自检

```bash
python3 03-apply-multidomain-cert.py --test
```

会真实登录一次并列出所有证书，**不做任何写操作**。看到证书列表才算通了。

### 4. 正式申请

```bash
python3 03-apply-multidomain-cert.py \
  -d "example.com,*.example.com,www.example.com"
```

`-r` 可以省略：脚本会读 NAS 上的 DDNS 记录，按域名后缀自动挑最匹配的那条。
要自己指定就先查 id：

```bash
python3 03-apply-multidomain-cert.py --list-ddns    # 只读，列出 id / 服务商 / 域名
python3 03-apply-multidomain-cert.py -d "..." -r 3
```

参数：

| 参数 | 含义 | 默认 |
|---|---|---|
| `-d` / `--domains` | 逗号分隔域名列表（可含通配符 `*.x`） | 必填 |
| `-r` / `--related-ddns-id` | DDNS 记录 id（提供 DNS-01 解析商凭证） | **留空自动匹配**，先校验存在 |
| `-s` / `--server` | WS 地址，留空自动探测 | 本机 HTTPS 端口（`FNOS_WS_PORT` 可指定，默认 5667） |
| `--desc` | 证书备注 | multidomain-cert |
| `--test` | 只登录 + 列证书，不做写操作 | — |
| `--list-ddns` | 只列 DDNS 记录后退出 | — |

依赖：`python3`（标准库）+ `openssl`（fnOS 自带）。

## 环境变量

脚本只从环境变量读配置。完整说明（优先级、生效逻辑、四种组合、误用排查）见
**[ENV.md](ENV.md)**，速查表：

| 变量 | 作用 | 何时需要 |
|---|---|---|
| `FNOS_USER` / `FNOS_PASS` | 网页登录用户名 / 密码 | 密码登录方式（推荐） |
| `FNOS_WS_PORT` | 本机 HTTPS 端口，默认 `5667` | 端口改过时 |
| `FNOS_ACCESS_CODE` | 访问码 | NAS 开了「访问码 / 访问保护」时 |
| `FNOS_2FA_CODE` | 验证器当前 6 位码 | 账号开了「双重验证」时 |
| `FNOS_SECRET` / `FNOS_COOKIE` | 浏览器里的 Secret 与 Cookie | 改用 Cookie 方式时 |

`FNOS_USER`+`FNOS_PASS` 与 `FNOS_SECRET` 二选一；有了密码登录就不用取 Cookie。

## 两层「门禁」怎么过

fnOS 的认证分两层，**报错长得很像但原因完全不同**，先看在哪一层挂的：

```
[nginx 层] 访问码 → 401 / 302 / 200+登录页      ← HTTP 状态码异常
     ↓ 101 Switching Protocols
[应用层] 密码登录 → 2FA → secret → 签帧调用      ← JSON 里的 errno 异常
```

### 访问码（nginx 层）

「访问码 / 访问保护」开启后，连 WebSocket 握手都会被拒，脚本的密码登录根本没机会执行：

```
WS 握手失败: HTTP/1.1 401 Unauthorized
```

判决依据：响应头里有 `X-Trim-Safe-Code-Challenge: 1`。两种解法：

```bash
# a) 把访问码交给脚本（推荐，保持全自动）
export FNOS_ACCESS_CODE='你的访问码'
```

脚本会先 `GET /access_code_verify`（带 `x-access-code: base64(访问码)`）换取
`os-access-code` 会话 Cookie，再带着 Cookie 建立连接。

```text
# b) 关闭访问码：fnOS 网页 → 安全性 → 访问设置 → 访问码 → 关闭
```

### 双重验证 2FA（应用层）

开了 2FA 后，`user.login` 只返回 `accessToken`，**不返回签帧用的 `secret`**，
所以必须先补一步验证码。**直接跑脚本即可，它会在需要时提示你输入**：

```text
[0] WS 已连接: wss://127.0.0.1:5667/websocket?type=main
      密码自动登录中...
      该账号开启了双重验证（2FA）
      请输入验证器 App 当前的 6 位验证码: _
      登录成功 uid=1000
```

输错或超时不会中断，会提示重新输入（最多 3 次）。也可以预先把码塞进环境变量，
适合脚本编排：

```bash
export FNOS_2FA_CODE='123456'    # 取完立刻跑，别等
python3 03-apply-multidomain-cert.py --test
```

脚本一次跑完：登录 → `user.2fa.loginVerify`（提交 `accessToken` + 验证码）→ 拿 `secret` → 调接口。
验证码错误或过期会明确报 `135168`，不影响账号安全。

> 验证码 30 秒过期，**不适合放进定时任务**。但证书签发后由 fnOS 自己按 `renewal:1`
> 自动续签，本脚本只在「要新签一张」时才跑一次，所以一次 2FA 不影响长期无人值守。
> 若在非交互环境（无终端）运行，脚本会明确要求改用 `FNOS_2FA_CODE`。

## 报错 → 原因 → 解决

| 现象 | 原因 | 解决 |
|---|---|---|
| `Connection refused` / 超时 | 端口不对或服务未开 | 设 `FNOS_WS_PORT` 或用 `-s` 给完整地址 |
| `WS 握手失败: HTTP/1.1 401`，带 `X-Trim-Safe-Code-Challenge` | 访问码拦截 | 设 `FNOS_ACCESS_CODE`，或关闭访问码 |
| `WS 握手失败: HTTP/1.1 401`，无该响应头 | 端口/反代指向了别的服务 | 核对端口与反代规则 |
| `该账号开启了双重验证（2FA）` / `135168` | 缺验证码或码已过期 | 交互运行让脚本提示输入，或设 `FNOS_2FA_CODE` |
| `登录失败：用户名或密码错误（131072）` | 用户名或密码不对 | 核对 `FNOS_USER` / `FNOS_PASS` |
| `errno 65534`（验签失败） | 已连上但会话未绑定 | 改用 Cookie 方式，且 `FNOS_COOKIE` 要取**完整**整行 |
| `errno 8192` / `3000001` | 参数格式不对 | 确认 `deviceType` 为字符串 `Browser`；2FA 字段不要缺 |
| `3000034 Cert Apply DDNS Record Not Found` | `-r` 指向的 DDNS 记录不存在 | 省略 `-r` 让脚本自动匹配，或 `--list-ddns` 查真实 id |
| `3000033 Cert Apply Domain Not Match` | 域名与 DDNS 记录不同区 | 换 `-r`，或省略让脚本自动匹配 |
| `3000035 Cert Not Renewable` | 条目丢了续签资格 | 脚本已在 `cert.replace` 带 `renewal:1`，无需手动处理 |
| `Certificate bundle starts with a CA certificate` | 种子证书是 CA:TRUE | 脚本已用 `CA:FALSE` 生成，无需手动处理 |

### 申请完成后

网页「安全性 → 证书 → 服务配置」把新证书绑定到反向代理等服务，
确认无服务绑定后可删旧单域名证书。

## 备选：Cookie + Secret（仅当不想用密码登录时）

```bash
export FNOS_SECRET='<DevTools → Application → Local Storage → fnos-Secret>'
export FNOS_COOKIE='<DevTools → Network → 任意请求 → Cookie 请求头整行>'
python3 03-apply-multidomain-cert.py --test
```

> 说明：这个备选方式需要从浏览器手动取凭据，且 Cookie 会过期，不如密码登录省事。
> 它的唯一优势是**能绕开 2FA 验证码**（Cookie 已经是验证过的会话）。

## 其余文件是干什么的

| 文件 | 用途 |
|---|---|
| `03-apply-multidomain-cert.py` | **你现在唯一需要跑的脚本**（密码登录 + 一键申请） |
| `ENV.md` | 环境变量完整参考：总表、四种组合、生效逻辑、误用排查 |
| `NOTES.md` | 完整背景、原理、协议细节、踩坑记录 |
| `archive/` | **旧方案存档**（已弃用）：浏览器 Cookie 方案 + Windows 种子证书生成器，见 `archive/README.md` |

## 关键 API 与协议（摘要）

- 核心配方：`cert.upload`（导入自签多 SAN 种子证书）→ `cert.replace`（补 `renewal:1 + relatedDDNSId`）
  → `cert.renew`（触发 fnOS 自己的 ACME 按全部 SAN 走 DNS-01 签真证书）
- WS 帧签名：`base64(HMAC-SHA256(json, secret)) + json`
- **坑**：种子证书必须 `CA:FALSE`（`openssl req -x509` 默认 CA:TRUE 会导致 renew 报错）
- 完整错误码表、协议细节、全部踩坑记录 → 见 [NOTES.md](NOTES.md)
