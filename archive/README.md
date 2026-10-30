# 归档：旧方案（已废弃）

这两个文件是「多域名证书申请」早期探索阶段的产物，**已被同目录上级的
`03-apply-multidomain-cert.py` 取代，正常情况不要再用**。保留仅为存档/参考。

## 为什么废弃

| 旧文件 | 旧方案 | 废弃原因 |
|---|---|---|
| `01-gen-seed-cert.ps1` | Windows PowerShell 生成自签多 SAN 种子证书 | ① 03 脚本已内置种子生成；② 旧版生成的种子证书 `CA:TRUE`，会触发 lego 报错（03 已修成 `CA:FALSE`） |
| `02-fnos-cert-api.js` | 浏览器 DevTools 控制台 + Cookie 会话 | ① 需要手动从浏览器取 httpOnly Cookie + secret，麻烦；② Cookie 会过期；③ 已被「密码自动登录」方案取代 |

## 什么情况下可能还想看

- 想了解「Cookie 会话认证」的原始分析过程（见 `02` 里的 HMAC 签名实现）
- 需要在**没有 openssl 的 Windows 机器**上单独生成种子证书（`01` 的 PowerShell 逻辑）

## 正确做法

见上级目录 [README.md](../README.md) —— 跑 `03-apply-multidomain-cert.py`，密码自动登录 + 一键申请。
完整背景和踩坑记录见 [NOTES.md](../NOTES.md)。
