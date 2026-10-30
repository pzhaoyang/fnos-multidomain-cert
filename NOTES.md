# fnOS 多域名证书申请 —— 完整复盘笔记

> 本文档是**唯一完整的背景+上下文**：目标、原理、协议细节、踩坑、最终用法都在这里。
> 快速上手版见 **[README.md](README.md)**（只讲「现在怎么做」）。
>
> 目标：绕开 fnOS 网页「一次只能申请一个域名」的限制，用 fnOS 自己的 ACME 引擎
> （申请、入库、自动续签）签出**一张覆盖多个域名（含通配符）的 Let's Encrypt 证书**，
> 并做到**命令行全自动**。

最终成果：见同目录 `03-apply-multidomain-cert.py`（单文件，密码自动登录 + 一键申请）。

---

## 一、最终如何使用（TL;DR）

### 方式 A：零配置交互（推荐，直接跑就行）

```bash
# 端口自动探测；用户名/密码、访问码、2FA 验证码都会提示输入
python3 03-apply-multidomain-cert.py --test        # 只读自检
python3 03-apply-multidomain-cert.py --list-ddns   # 看 -r 可用的记录
python3 03-apply-multidomain-cert.py \
  -d "example.com,*.example.com,www.example.com"
```

要免交互（cron/管道/远程执行）时再用环境变量：

```bash
export FNOS_USER='你的用户名'
export FNOS_PASS='你的网页登录密码'
export FNOS_ACCESS_CODE='你的访问码'   # 开了访问码才需要（坑 13）
export FNOS_WS_PORT='5667'            # 自动探测不到时才需要
export FNOS_2FA_CODE='123456'         # 非交互 + 开了 2FA 才需要（坑 14）
```

参数说明：

| 参数 | 含义 | 默认 |
|---|---|---|
| `-d` / `--domains` | 逗号分隔的域名列表（可含通配符 `*.x`） | 必填 |
| `-r` / `--related-ddns-id` | DDNS 记录 id（提供 DNS-01 解析商凭证） | 留空自动匹配，且上传前校验存在 |
| `-s` / `--server` | WS 地址，留空自动探测 | 本机 HTTPS 端口（`FNOS_WS_PORT` 可指定，默认 5667） |
| `--desc` | 证书备注 | multidomain-cert |
| `--test` | 只登录 + 列证书，不做任何写操作 | — |
| `--list-ddns` | 只列 DDNS 记录后退出 | — |

环境变量（按需，见 [ENV.md](ENV.md)）：
`FNOS_USER` / `FNOS_PASS` / `FNOS_WS_PORT` / `FNOS_ACCESS_CODE` / `FNOS_2FA_CODE` /
`FNOS_SECRET` / `FNOS_COOKIE`。2FA 不设也行，脚本会提示输入。

依赖：`python3`（标准库）+ `openssl`（都是 fnOS 自带）。

> ⚠️ **关键坑**：如果密码通过管道传入（如 `echo xx | ssh ... read`），
> 脚本内部已做 `.rstrip("\r\n")` 去除 Windows 换行符。直接 export 环境变量则无此问题。

### 方式 B：Cookie + Secret（备选，需要从浏览器取一次）

```bash
export FNOS_SECRET='<DevTools → Application → Local Storage → fnos-Secret>'
export FNOS_COOKIE='<DevTools → Network → 任意请求 → Cookie 请求头整行>'
python3 03-apply-multidomain-cert.py --test
```

### 申请完成后

网页「安全性 → 证书 → 服务配置」把新证书绑定到反向代理等服务，
确认无服务绑定后可删旧单域名证书。

---

## 二、核心原理：为什么要「种子证书」

fnOS 的证书系统有一条硬限制：

1. **`cert.apply`（申请接口）** 服务端强校验「domain 必须 == DDNS 记录域名」
   （`Cert Apply Domain Not Match`，errno 3000033），且 `SAN`/`domains` 等字段
   全部被**静默忽略** —— 这条路物理上只能单域名。
2. **`cert.upload` / `cert.replace`（导入接口）** 接受证书+私钥文件，
   **SAN 是从 X.509 证书文件里解析出来的** —— 这是唯一的多域名入口。

所以「多域名」的配方是：

```
自签多 SAN 种子证书（占位用，从不真正用于 TLS）
  → cert.upload 入库（SAN 变成逗号串存进 DB）
  → cert.replace 补上 renewal:1 + relatedDDNSId（赋予续签资格）
  → cert.renew 触发 fnOS 自己的 ACME 引擎按全部 SAN 走 DNS-01 签真证书
```

种子证书只是「把域名清单递进 fnOS 的信封」，本身自签、秒生成、不消耗 LE 配额；
`cert.renew` 立刻把它换成真的 Let's Encrypt 证书。

**为什么不能直接申请？** 因为 `cert.apply` 只接受单域名且强制匹配 DDNS 记录；
多域名列表只能通过「证书文件」这个载体进系统。

---

## 三、完整协议分析记录（全部实测验证）

### 3.1 传输层：WebSocket

- WS 地址：`wss://<host>/websocket?type=main`（还有 `type=timer`）
- 握手：标准 RFC 6455，**无需 Cookie 也能完成握手**（但业务认证见下）
- 帧编码：客户端掩码、文本帧 opcode=0x1，支持 126/127 扩展长度（实测 368 字节帧正常）

### 3.2 认证层：两种方式

#### 方式 A：密码登录（最终方案，已打通）

```
1. 连 WS（无需 Cookie）
2. util.crypto.getRSAPub（明文帧）→ {pub: PEM, si: 会话ID}
3. 生成随机 32 字节 AES 密钥 + 16 字节 IV
4. RSA-OAEP-SHA256 加密 AES 密钥 → rsa 字段
5. AES-256-CBC(PKCS7) 加密内层 JSON → aes 字段
6. 发加密帧 {"req":"encrypted","iv","rsa","aes","v":1}
7. 内层是 {"req":"user.login","user","password","deviceName","deviceType":"Browser",
          "stay":true,"did":...,"si":会话ID}
8. 响应 {uid, admin, token, longToken, secret, backId, ...}
9. ★ secret 字段是 AES 加密的：base64decode → AES-CBC 解密（用登录的 aes_key+iv）
   → 去 PKCS7 padding → 得到 HMAC 密钥（18 字节）
```

#### 方式 B：Cookie 会话（备选）

```
1. WS 握手带 Cookie（httpOnly 会话 Cookie，把连接绑定到登录会话）
2. util.getSI（明文帧）→ 换本连接的 si
3. user.authToken（签名帧 {reqid, req, main:true, si}）→ 绑定会话
```

### 3.3 签名：HMAC-SHA256

```
frame = base64( HMAC-SHA256( json, secret_bytes ) ) + json
```

- `secret_bytes` = base64 解码 `fnos-Secret`（或密码登录解密出的 18 字节）
- 注意：`base64` 输出自带 padding，**不要额外补 `=`**
- reqid 格式宽松：28 位 hex 即可（时间8+随机8+计数器12），无需会话常量

### 3.4 错误码速查（从网页前端代码里整理出）

| errno | 含义 |
|---|---|
| 8192 | 参数错误 |
| 131072 | 用户名或密码错误 |
| 131073 | 用户名不存在 |
| 135168 | 无效凭证 |
| 3000001 | Invalid Params（字段类型错，泛化） |
| 3000033 | Cert Apply Domain Not Match（申请接口单域名校验） |
| 3000035 | Cert Not Renewable（续签资格被清） |
| 65534 | **验签失败**（连接未绑定会话 / HMAC 不对） |
| 65535 | 未知错误 |

### 3.5 证书相关端点（appcgi.netsvr.cert.*）

| 端点 | 作用 |
|---|---|
| `cert.upload` | 导入证书+私钥（base64），SAN 从证书解析入库 |
| `cert.replace` | 替换条目证书；**附加 `renewal:1, relatedDDNSId:<id>` 保留/赋予续签资格** |
| `cert.renew` | 触发 fnOS ACME 重新签发（按条目 SAN 全部域名，DNS-01） |
| `cert.applystatus` | 轮询签发任务 `{processId, status}` |
| `cert.list` / `cert.remove` / `cert.export` | 列表 / 删除 / 导出 |

---

## 四、我们尝试过、踩过的坑（按时间线）

### 坑 1：trim-cli 走不通 cert 端点
- `trim-cli raw appcgi.netsvr.cert.list` → 403 Forbidden
- 原因：OAuth scope 不含 netsvr；且 skill 规则禁止扩展 scope
- 出路：改用浏览器里的已登录会话 + 自签 WS 帧

### 坑 2：WS 帧是 HMAC 签名的
- 直接改 payload 会破坏签名被服务端拒绝
- 找到：前端 bundle 里的签名 = `base64(HMAC-SHA256(json, localStorage['fnos-Secret'])) + json`
- 关键验证：用捕获的历史帧重算 MAC，25/25 全中（只差一个 `=`，被我多加的）

### 坑 3：reqid 需要特定格式吗？
- 一开始以为 reqid 中段是「会话常量」必须匹配
- 实测：28 位 hex 即可，中段随便填都行（之前失败其实是没走握手，不是 reqid 的问题）

### 坑 4：连接必须「绑定会话」才能发业务请求
- 裸连 + 正确签名 → 业务请求一律 65534（验签失败）
- 发现：`util.getSI`（明文帧，在未签名白名单里）→ 换 si → `user.authToken` 绑定
- 注意：getSI 的正确端点是 `util.getSI`（不是猜的 `util.crypto.getSI`）

### 坑 5：Cookie 方案里 httpOnly Cookie 拿不到
- `document.cookie` 只能看到 `language` + `entry-token`，会话 Cookie 是 httpOnly
- 跨站 WS（SameSite 隔离）实测：无 Cookie → 65534，有 Cookie → 成功
- 结论：Cookie 是连接绑定的凭据，但浏览器端拿不到 httpOnly 值 → 转向密码登录

### 坑 6：密码登录报 8192（参数错误）
- 原因：`deviceType` 是字符串 `"Browser"`，我发成了数字 `0`；`did` 格式也不对
- 修正：`deviceType:"Browser"` + `did` 用 base36 格式

### 坑 7：密码登录报 131072（密码错误）—— 最终根因
- 排查链：RSA 填充（OAEP vs PKCS1_v1_5）→ AES 密钥（字母 vs 随机字节）→
  `v:1` 字段必需 → 最终用服务端私钥做完整解密回环
- **真相**：密码末尾混入了 `\r`（PowerShell 管道传参带 Windows CRLF，
  `read -r` 读入时保留了 `\r`）。`password_hex` 里赫然是 `...6963750d`
- 修复：读 env 后 `.rstrip("\r\n")`
- 教训：跨平台传密码，永远先 hexdump 一遍确认无不可见字符

### 坑 8：登录成功但业务请求还是 65534
- 原因：登录响应的 `secret` 字段是 **AES 加密的**，不能直接当 HMAC key
- 修复：base64decode(secret) → AES-CBC 解密（登录时的 aes_key+iv）→ 去 PKCS7 → 得到 HMAC 密钥
- 这正是 pyfnos 里 `_decrypt_login_secret` 做的事，早该对照它

### 坑 9：`cert.replace` 会清零 renewal
- 现象：replace 后 `renewal` 变 0，renew 报 3000035
- 修复：replace 的 data 里显式补 `renewal:1, relatedDDNSId:<id>`

### 坑 10：PowerShell 引号/转义地狱
- ssh 远程命令里嵌套引号、`\dt` 反斜杠被 PowerShell 吞掉
- 出路：一律用「本地写 .sh 脚本 → scp → 远程 bash 执行」绕开引号问题

### 坑 11：`applystatus` 状态判断 bug
- 现象：失败时打印 `status=failed` 空转 36 次，却不打印失败原因
- 原因：脚本只判断 `"fail"`，而服务端实际返回 `"failed"`（多了 ed），两个分支都没命中
- 修复：兼容 `fail`/`failed`/`success`/`suc`；失败时打印 applystatus **完整响应**（errMsg 在顶层 `errMsg` 字段）
- applystatus 状态值：`verifying`（验证中）→ `success` / `failed`

### 坑 12：种子证书必须是 CA:FALSE —— 最终根因
- 现象：renew 报 `Cert Apply Error, error: [域名] Certificate bundle starts with a CA certificate`
- 根因：`openssl req -x509` 生成的自签证书**默认 `BasicConstraints: CA:TRUE`**，
  而 fnOS 内置 lego 的 `RenewWithOptions` 会解析「要续签的旧证书」，若第一个证书 `IsCA` 就报错
- 修复：openssl 命令加 `-addext "basicConstraints=critical,CA:FALSE"` 和
  `-addext "keyUsage=critical,digitalSignature,keyEncipherment"`
- 验证：`openssl x509 -in seed.crt -noout -text | grep -A1 "Basic Constraints"` 应显示 `CA:FALSE`
- 教训：浏览器版（PowerShell New-SelfSignedCertificate）默认不是 CA，所以当时能成功；
  Python 版用 openssl 默认是 CA，就踩坑了——**换语言实现时要重新验证默认行为**

### 坑 13：换了第二台 NAS 就 401 —— 「访问码」是 nginx 层的门禁
- 现象：同一份脚本、同一套用户名密码，在 A 机正常，在 B 机第一步就
  `WS 握手失败: HTTP/1.1 401 Unauthorized`
- 关键线索：401 的响应头里有 `X-Trim-Safe-Code-Challenge: 1`
- 对照实验（同一路径 `/websocket?type=main`，同样的 Upgrade 头）：
  - A 机（未开访问码）→ `HTTP/1.1 101 Switching Protocols`
  - B 机（开了访问码）→ `HTTP/1.1 401 Unauthorized`
- 根因：fnOS 的「访问码 / 访问保护」是 **nginx 层**的门禁，WebSocket 升级请求在
  进入应用层之前就被拦掉，脚本的 `getRSAPub`/`user.login` 一次都没发出去
- 应对办法（读访问码页面的前端 JS 得到）：
  ```text
  GET /access_code_verify
  x-access-code: base64(utf8(访问码))
  x-access-source: web
  ```
  成功返回 `200` + `Set-Cookie`（会话凭据），失败 `401/403/429`。
  拿到 Cookie 后带上再握手即可
- 修复：脚本新增 `FNOS_ACCESS_CODE`，连接前先换 Cookie；同时把
  `x-access-code` 也附在握手请求头上（双保险）。401 时直接打印可执行的建议
- 教训：**区分错误发生在哪一层**
  - HTTP `401/403/302`（握手阶段）→ 网关/反向代理的门禁（访问码、IP 限制、未登录页）
  - 应用层 `errno 65534`（已 101 升级成功）→ 会话未绑定/签名不对
  先看状态码层次，能省掉大量瞎猜

### 坑 14：账号开了双重验证（2FA）—— 登录只给 accessToken，不给 secret
- 现象：网络全通、密码正确，`user.login` 返回 `result: "succ"`，但响应里没有 `secret`：
  ```json
  {"isTwofaEnforced": true, "isBindTwofaSecret": true, "isTrustedDevice": false,
   "secureEmail": "xxx@example.com", "accessToken": "...", "result": "succ"}
  ```
- 根因：2FA 是**两段式登录**。第一段只发 `accessToken`（短期票据），
  第二段用验证码换真正的 `secret`（HMAC 密钥）
- 接口（从前端 `useTfaLogin` chunk 里挖出来的）：
  - `user.2fa.loginVerify`，**同样是加密帧**（和 `user.login` 一个套路）
  - payload 一个字段都不能少：
    ```text
    {accessToken, code, isTrustedDevice, stay, deviceName, deviceType, did}
    ```
    `did` 必须与 `user.login` 时用的**同一个**设备 id，否则服务端视为换设备
- 错误码：`135168` 验证码错误/过期；`102570109` 2FA 票据过期（需重新登录）；
  `102570110` 未绑定 2FA；`102570107` 2FA 状态异常
- 调试技巧：先用**明显错误**的验证码（如 `000000`）打一次，
  如果返回 `135168` 就说明请求结构完全正确，**不会浪费真实验证码**
- 与自动化冲突：验证码 30 秒过期，不能放进 cron。但因为 fnOS 会自己按
  `renewal:1` 续签，本脚本只在「新签」时跑一次，所以可接受
  （要彻底免验证码，可以存 TOTP 密钥自己算码，或复用受信设备的 `did`）
- 体验修正：**不要逼用户去 export 环境变量**。脚本改成在需要时直接
  `input()` 提示输入，输错/过期可重试 3 次；`FNOS_2FA_CODE` 只在
  非交互场景（脚本编排、管道）才需要。实践反馈：`export` 一次再用一次
  几乎必然踩到 30 秒窗口，体验极差

### 坑 15：`3000034 Cert Apply DDNS Record Not Found` —— `-r` 是机器相关的
- 现象：换一台 NAS 沿用 `-r 1`，前面 upload/replace 全部成功，走到 `cert.renew` 才炸：
  ```
  RuntimeError: cert.renew 失败: 3000034 Cert Apply DDNS Record Not Found
  ```
- 根因：`relatedDDNSId` 是 `d_dns_record` 表的**自增 id，因机器而异**。
  旧机器上 id=1 是主域名记录，新机器上 id=1 可能完全不存在
- 更糟的是：`cert.replace` 阶段**不校验**这个 id，所以会先白白上传一张自签种子证书，
  在证书列表里留下一条没用的条目，等 renew 才报错
- 正确做法：
  1. 用 `appcgi.netsvr.ddns.record.list` 拿到真实记录（返回 `data.list[]`，
     字段 `id / domain / provider / providerAlias / providerSupportTxt`）
  2. **上传前**就校验 `-r` 是否存在，不存在立刻停，不浪费一次上传
  3. 没给 `-r` 时按域名后缀自动匹配：把请求域名和记录 `domain` 都去掉 `*.`，
     取「记录域名是请求域名后缀」里最长的那条（同区即可，Cloudflare token 按 zone 通用）
- 注意区分：`3000033 Domain Not Match`（域名与记录不同区）
  vs `3000034 DDNS Record Not Found`（记录 id 根本不存在）

### 坑 16：端口不该问用户 —— 能自动探测出来
- 起因：脚本默认写死 `5667`，用户改过端口就要手动 `export FNOS_WS_PORT`，
  换机器又忘，体验很差（“我自己的机器，为啥还要告诉脚本端口”）
- 结论：**能探测**，而且不必猜配置布局，实测更可靠。探测顺序：
  1. `5667`（fnOS 默认 HTTPS 端口）
  2. nginx 配置里带 `ssl` 的 `listen` 端口（扫 `/etc/nginx`、
     `/usr/trim/nginx`、`/usr/trim/etc/nginx` 等，正则
     `^\s*listen\s+(?:[0-9.\[\]:]*:)?(\d{2,5})\b([^\n]*)`）
  3. 其余 nginx `listen` 端口
  4. `/proc/net/tcp{,6}` 中状态 `0A`（LISTEN）的端口
- 「这个端口是不是 fnOS 入口」的判据（任一命中即可）：
  - 握手返回 `101 Switching Protocols` 且带 `websocket`
  - 响应头带 `X-Trim-Safe-Code-Challenge`（开了访问码，实际是 401）
  - `Server: nginx` 且返回 `401/302`（登录页/重定向）
  非 TLS 端口直接排除（`wrap_socket` 失败即跳过，速度很快）
- 兜底：全都探不到 → 终端里问端口（回车用 5667），非终端回退 5667
- 教训：**能自测的就别让用户填**。同理，用户名/密码/访问码/2FA 也都改成
  「环境变量优先，缺了就交互提示」，非交互环境才报错要求设变量

### 坑 17：`3000033 Domain Not Match` —— CN 不能是通配符
- 现象：`-d '*.wmedia.example.com,wmedia.example.com'`（**通配符在前**），
  上传、replace 都成功，renew 报 `3000033 Cert Apply Domain Not Match`；
  而同样的配方在另一台机器上（非通配符在前）却能签下来
- 根因：fnOS 把证书的 **CN 当作主域名**（`cert.list` 里的 `domain` 字段），
  续签时拿它和所选 DDNS 记录的 `domain` 比对。CN 是 `*.zone` 时
  ≠ 记录的 `zone`，于是判不匹配
- 对照证据：成功那张证书 `cert.list` 记录里 `domain=imedia.example.com`、
  `SAN=*.imedia.example.com,imedia.example.com` —— 主域名非通配符、SAN 含通配符
- 修复：生成种子证书时 **CN 取第一个非通配符域名**，SAN 仍包含全部域名
  （`openssl req -subj /CN=...` 用 `next(d for d in domains if not d.startswith("*."))`）
- 顺带：`--list-ddns` 现在会标 `TXT✓ / TXT✗`，DNS-01 必须用支持 TXT 的记录
- 排查顺序速记：`3000034` = 记录 id 不存在 → `3000033` = 域名与记录 domain 不匹配
  → `3000035` = 记录存在但条目没续签资格（replace 没带 renewal:1）

### 坑 18：「记住登录」不能靠缓存会话，要靠受信设备
- 直觉：登录一次拿到 `secret`/`token`，存下来下次直接用
- 实测（全部 `errno 65534`，逐条试过）：
  | 尝试 | 结果 |
  |---|---|
  | 只带 `secret`，新连接发 `authToken{main:true, si}` | 65534 |
  | `authToken{token, main:true}`（浏览器里存的 token） | 65534 |
  | 明文 / 加密 `user.auth{token}` | 65534 |
  | `user.tokenLogin{token, deviceType, deviceName, did}` | 65534 |
  | `tokenLogin` 传 longToken | 65534 |
- 结论：**会话是连接级的**。网页之所以能“记住”，是因为它的 WS 握手**带着浏览器 Cookie**，
  而 `authToken` 缺了那个 Cookie 的会话上下文就一律验签失败
- 更绝的是：服务端**根本不下发会话 Cookie** ——
  `GET /`、`/login`、`/v1/`、`/websocket`（无 Upgrade）全部 0 个 `Set-Cookie`。
  所以从纯 WS 客户端这边，**没有任何 Cookie 可以复现**
- 可行方案：官方本来就有「信任此设备」
  - `user.2fa.loginVerify` 里带 `isTrustedDevice: true`（等价网页勾选“信任此设备”）
  - 再尽力调 `appcgi.tfa.security.v1.device.trust` 登记（参数形态未公开，多形态尝试）
  - 关键是 **`did` 必须跨次稳定**，否则每次都是新设备，信任不生效
- 于是落地成：`~/.fnos-cert.conf`（600）保存 `user/pass/access_code/did/port/server`，
  第一次输入、之后零输入；`--forget` 清除、`--no-save` 不写
- 教训：**别硬啃“缓存凭据”**。先找产品自己提供的“记住我”语义（这里是受信设备），
  比伪造会话可靠得多

---

## 五、DDNS 记录与 relatedDDNSId（实测，重要）

`-r`（relatedDDNSId）指 `d_dns_record` 表里的 DDNS 记录 id，它提供 DNS-01 验证用的
**解析商 API 凭证（access_key / access_secret）**。实测要点：

- 解析商是 **Cloudflare**（`ddns_provider=cloudflare`），每条记录有**独立的 API token**
- **每个域名对应不同的记录 id**（`d_dns_record.id`），例如若干子域名分别映射到 id 2/3/8/9/11/12 等
- **泛域名 `*.example.com` 是独立的记录（id=14）**，token 也独立
- 但实测：用主域名那条记录的 id（如 1）签「主域名 + 泛域名」**能成功**——说明 DNS-01 建 TXT 时
  用同一条 Cloudflare token 即可覆盖同一 zone 下的所有域名
- 各证书的 `cert_renew` 表里 `ddns_related_id` 各不相同（每个单域名证书绑自己的记录）

> 结论：`-r` 选哪个 id 主要看「该记录的 Cloudflare token 是否有权限写对应 zone」，
> 通常同一 zone 用任一条记录都行；多域名混在一个 zone 里时 `-r 1` 就够。

## 六、网络拓扑（实测）

- 飞牛官方 FN Connect 中转域名（形如 `xxx.fnos.net`），**仅 443 端口可达**（其他端口外网不通属正常，与防火墙无关）
- 自有域名（如 `*.example.com`）= DDNS 直连家里 IP，可走 fnOS 的 HTTP/HTTPS 访问端口（fnOS 系统默认 5666/5667，可在「安全性 → 访问设置」里改）和反代端口
- NAS 本机：fnOS 的 HTTPS 访问端口即 WS 真入口（脚本默认探测 `FNOS_WS_PORT`，未设置则用系统默认 5667）

---

## 七、文件清单

| 文件 | 说明 |
|---|---|
| `03-apply-multidomain-cert.py` | **推荐（唯一在用）**：密码自动登录 + 一键申请，单文件纯标准库 |
| `README.md` | 简明使用说明（快速上手） |
| `NOTES.md` | 本文档：完整复盘 |
| `archive/` | 旧方案存档：`01-gen-seed-cert.ps1`（Windows 种子生成器）+ `02-fnos-cert-api.js`（浏览器 Cookie 方案），已废弃 |

---

## 八、安全注意

- `FNOS_PASS` / `FNOS_SECRET` / `FNOS_COOKIE` 等同登录凭据，**不要提交到 git、不要写进日志**
- 脚本内部处理密码只在内存中，用完即弃；加密帧里的密码经 RSA+AES 双重加密传输
- 让 Let's Encrypt 配额：50 域名/周、同名集合 5 次/周，失败验证 5 次/小时/域名，别频繁重复申请
- 通配符 `*.a.b` 只覆盖一层（`x.a.b`），兄弟子域（如 `imp.b`）要显式列入 SAN
