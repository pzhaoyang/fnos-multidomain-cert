# 环境变量完整参考

> 脚本**只从环境变量读配置**，不读写任何配置文件，也不接受配置文件路径。
> 所有变量的值末尾的 `\r\n` 会被自动去掉（Windows / 管道传值时常见）。

## 一、总表

| 变量 | 作用 | 何时必需 | 取值示例 | 默认 |
|---|---|---|---|---|
| `FNOS_USER` | 网页登录用户名 | 密码登录方式 | `admin` | — |
| `FNOS_PASS` | 网页登录密码 | 密码登录方式 | `你的密码` | — |
| `FNOS_WS_PORT` | 本机 HTTPS 访问端口 | 端口不是 `5667` 时 | `5667` | `5667` |
| `FNOS_ACCESS_CODE` | 「访问码 / 访问保护」的访问码 | NAS 开了访问码时 | `123456` | 空（不启用） |
| `FNOS_2FA_CODE` | 验证器 App 当前 6 位码 | 账号开了双重验证时 | `123456` | 空（不启用） |
| `FNOS_SECRET` | 浏览器里的 `fnos-Secret`（HMAC 密钥） | Cookie 方式 | `base64 字符串` | 空 |
| `FNOS_COOKIE` | 浏览器 Cookie 请求头整行 | Cookie 方式 | `trim_session=...; other=...` | 空 |

约定：

- `FNOS_USER` + `FNOS_PASS` 与 `FNOS_SECRET` **二选一**；两者都给时以密码为准。
- `FNOS_COOKIE` 在两种模式下都会作为 `Cookie:` 头发给 WS 握手（访问码 Cookie 是另一回事，见下文）。
- 没开的功能**不要设**——设了不影响功能，但等于把无用凭据留在环境里。

## 二、五种典型组合

### 1) 什么都没开（最常见）

```bash
export FNOS_USER='admin'
export FNOS_PASS='你的密码'
python3 03-apply-multidomain-cert.py --test
```

### 2) 开了「访问码」

```bash
export FNOS_USER='admin'
export FNOS_PASS='你的密码'
export FNOS_ACCESS_CODE='123456'
python3 03-apply-multidomain-cert.py --test
```

### 3) 访问码 + 双重验证（2FA）

```bash
export FNOS_USER='admin'
export FNOS_PASS='你的密码'
export FNOS_ACCESS_CODE='123456'
# 验证码 30 秒过期，必须在同一次执行里给
export FNOS_2FA_CODE='123456'
python3 03-apply-multidomain-cert.py --test
```

### 4) 端口不是 5667（改过访问端口 / 要走域名或反代）

```bash
# 方式 a：只改端口
export FNOS_WS_PORT='8443'
python3 03-apply-multidomain-cert.py --test

# 方式 b：直接给完整地址（优先于 FNOS_WS_PORT）
python3 03-apply-multidomain-cert.py \
  -s 'wss://127.0.0.1:8443/websocket?type=main' --test
```

### 5) 不想用密码：Cookie + Secret（也能跳过 2FA）

```bash
export FNOS_SECRET='<DevTools → Application → Local Storage → fnos-Secret>'
export FNOS_COOKIE='<DevTools → Network → 任意请求 → Cookie 请求头整行>'
python3 03-apply-multidomain-cert.py --test
```

> Cookie 已经是验证过的会话，所以这条路径**不需要** `FNOS_2FA_CODE`；
> 代价是 Cookie / Secret 会过期，需要重新从浏览器取。

## 三、脚本内部的生效逻辑

```text
1. 读入全部变量，去掉末尾 \r\n
2. 认证模式判定
   有 FNOS_USER 且 FNOS_PASS → 密码模式（FNOS_SECRET 被忽略）
   否则有 FNOS_SECRET        → Cookie 模式（getSI + authToken 绑定会话）
   两者都没有                → 直接退出，提示怎么设
3. 连接地址
   命令行 -s 非空 → 用它（完整 wss:// 地址）
   否则           → wss://127.0.0.1:${FNOS_WS_PORT:-5667}/websocket?type=main
4. 访问码（可选）
   FNOS_ACCESS_CODE 非空 → 先 GET /access_code_verify 换会话 Cookie，再握手
5. 密码模式登录后，若服务端声明需要 2FA
   FNOS_2FA_CODE 非空 → 提交验证码（user.2fa.loginVerify）换 secret
   为空               → 报错退出，提示怎么设
6. 业务请求统一用 secret 签 HMAC 帧
```

优先级一览：

| 场景 | 谁说了算 |
|---|---|
| 连接地址 | `-s` > `FNOS_WS_PORT` > 默认 `5667` |
| 认证方式 | 密码（`FNOS_USER`+`FNOS_PASS`）> `FNOS_SECRET` |
| 2FA | 只有服务端要求时才用 `FNOS_2FA_CODE`；不需要时设了也会被忽略 |
| 访问码 | 只要 `FNOS_ACCESS_CODE` 非空就先走校验；没开访问码时该值为空 |

## 四、怎么设置这些变量

```bash
# 1) 当前 shell 会话内（登出即失效）
export FNOS_USER='admin'

# 2) 只对这一次命令生效（推荐给 FNOS_2FA_CODE，避免误复用过期码）
FNOS_USER='admin' FNOS_PASS='***' FNOS_2FA_CODE='123456' \
  python3 03-apply-multidomain-cert.py --test

# 3) 不想留在 shell 历史里（交互输入密码）
read -rsp '密码: ' FNOS_PASS; echo; export FNOS_PASS

# 4) systemd 服务里
#    /etc/systemd/system/fnos-cert.service
#    [Service]
#    Environment=FNOS_USER=admin
#    Environment=FNOS_WS_PORT=5667
#    # 密码建议用 EnvironmentFile 且 chmod 600
#    EnvironmentFile=/etc/fnos-cert.env
```

注意事项：

- `export` 只影响当前 shell；换 SSH 会话就没了，需要重新设。
- 变量名区分大小写，全部以 `FNOS_` 开头。
- 用 `bash` 时值里若有特殊字符，用单引号包起来。

## 五、安全注意

- **不要提交到 git**：密码、访问码、Cookie、Secret 都等同登录凭据。
- 环境变量对同机 `root` 可见（`/proc/<pid>/environ`、`ps e`），单用户 NAS 上可接受。
- 放文件的用 `chmod 600`，别用 world-readable 的 `.env`。
- `FNOS_2FA_CODE` 是一次性的短时值，过期无效，但也不要贴到公开渠道。
- 证书签发成功后由 fnOS 自己按 `renewal:1` 续签；**不要**为了“自动化”把长期凭据写进 cron。

## 六、常见误用

| 现象 | 原因 |
|---|---|
| 设了 `FNOS_WS_PORT` 却仍连旧端口 | 同时给了 `-s`，`-s` 优先 |
| 设了 `FNOS_2FA_CODE` 仍提示需要验证码 | 变量与脚本不在同一次执行；或码已超过 30 秒 |
| 同时设了密码和 `FNOS_SECRET`，行为跟预期不符 | 密码模式优先，`FNOS_SECRET` 被忽略 |
| 端口改了不生效 | 变量名拼错（必须 `FNOS_WS_PORT`）；或被 `-s` 覆盖 |
| 访问码明明正确还是 `401` | 该端口指向的是别的服务/反代，换端口或改用 `-s` |
| Cookie 方式仍报 `errno 65534` | `FNOS_COOKIE` 没取「完整整行」，或会话已过期 |
| 登录报 `131072` | 用户名或密码错；注意别把访问码填进 `FNOS_PASS` |
