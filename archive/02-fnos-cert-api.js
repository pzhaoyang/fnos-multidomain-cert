// 02-fnos-cert-api.js —— fnOS 多域名证书申请（浏览器 DevTools 控制台脚本）
//
// 前置条件：已登录 fnOS 网页端；已用 01-gen-seed-cert.ps1 生成种子证书。
// 用法：F12 → Console → 粘贴本脚本 → 修改下方 CONFIG → 执行：  await applyMultiSan()
//
// 原理：cert.upload 建多 SAN 条目 → cert.replace 带 renewal:1+relatedDDNSId 赋予续签资格
//       → cert.renew 触发 fnOS 自己的 ACME 引擎（DNS-01）按全部 SAN 签发真证书。
//
// 注意：WS 帧 = base64(HMAC-SHA256(json, 会话密钥)) + json；本脚本用页面同款算法自签请求。

const CONFIG = {
  certB64: "-----BEGIN CERTIFICATE-----...(base64 of seed.crt 整个文件内容)...",
  keyB64:  "-----BEGIN PRIVATE KEY-----...(base64 of seed.key 整个文件内容)...",
  desc: "my-multidomain-cert",            // 条目备注（≤32 字符）
  relatedDDNSId: 1,                        // DDNS 记录 id（提供 DNS-01 的解析商凭证，必须与域名同解析商/Zone）
  applyDescAfterRenew: false,              // renew 后 desc 会被清空，可忽略
};

/* ---------- 1. 会话密钥与签名 ---------- */
async function getHmacKey() {
  const secretB64 = localStorage.getItem("fnos-Secret");
  if (!secretB64) throw new Error("未找到 fnos-Secret，请先登录 fnOS 网页");
  const keyBytes = Uint8Array.from(atob(secretB64), c => c.charCodeAt(0));
  return crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
}
const b64 = buf => btoa(String.fromCharCode(...new Uint8Array(buf)));
async function forge(obj, key) {
  const json = JSON.stringify(obj);
  const mac = b64(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(json)));
  return mac + json;                       // 注意：不额外补 "="
}

/* ---------- 2. reqid（hex(秒)8 + 会话常量8 + 计数器12） ---------- */
async function captureReqidTemplate() {
  return new Promise(resolve => {
    const orig = WebSocket.prototype.send;
    WebSocket.prototype.send = function (d) {
      try {
        if (typeof d === "string") {
          const m = d.match(/"reqid":"([0-9a-f]{28})"/);
          if (m) { WebSocket.prototype.send = orig; resolve(m[1]); }
        }
      } catch (e) {}
      return orig.call(this, d);
    };
    setTimeout(() => resolve(null), 15000);  // 页面每 ~3s 有 resmon 流量，一般 3 秒内捕获
  });
}
let _counter = 0x1000;
function makeReqid(tmpl) {
  const sec = Math.floor(Date.now() / 1000).toString(16);
  const constant = tmpl.slice(8, 16);
  _counter += 1;
  return sec + constant + _counter.toString(16).padStart(12, "0");
}

/* ---------- 3. 发送与响应收集（只发一个 socket，避免重复） ---------- */
function hookResponses() {
  window.__resp = [];
  for (const s of window.__wsocksLive) {
    if (s.__hooked) continue;
    s.__hooked = true;
    const orig = s.onmessage;
    s.onmessage = function (ev) {
      try { window.__resp.push(String(ev.data)); } catch (e) {}
      return orig && orig.call(this, ev);
    };
  }
}
function liveSocket() {
  if (!window.__wsocksLive) {
    window.__wsocksLive = [];
    const OS = WebSocket.prototype.send;
    WebSocket.prototype.send = function (d) {
      if (window.__wsocksLive.indexOf(this) < 0) window.__wsocksLive.push(this);
      return OS.call(this, d);
    };
    // 触发一次流量让 wrapper 记录到 socket（等 resmon 自然到来亦可）
  }
  const s = window.__wsocksLive.find(x => x.readyState === 1);
  if (!s) throw new Error("没有可用的 WebSocket 连接");
  return s;
}
async function send(obj, key, tmpl) {
  hookResponses();
  window.__resp.length = 0;
  const sock = liveSocket();
  sock.send(await forge({ reqid: makeReqid(tmpl), ...obj }, key));
  await new Promise(r => setTimeout(r, 2500));
  return window.__resp.filter(d => d.includes(obj.req)).map(d => JSON.parse(d));
}

/* ---------- 4. 主流程 ---------- */
async function applyMultiSan() {
  const key = await getHmacKey();
  console.log("[1/6] 捕获 reqid 模板...");
  const tmpl = await captureReqidTemplate();
  if (!tmpl) throw new Error("reqid 模板捕获失败（页面无流量？）");
  console.log("      模板:", tmpl);

  console.log("[2/6] cert.upload 导入种子证书...");
  let r = await send({ req: "appcgi.netsvr.cert.upload", data: {
    privateKeyBase64: CONFIG.keyB64, certificateBase64: CONFIG.certB64,
    issuerCertificateBase64: "", desc: CONFIG.desc, isDefault: 0,
    privateKeyPath: "", certificatePath: "", issuerCertificatePath: "",
  }}, key, tmpl);
  if (!r.some(x => x.result === "succ")) throw new Error("upload 失败: " + JSON.stringify(r));

  console.log("[3/6] 定位新条目 id...");
  const list = await send({ req: "appcgi.netsvr.cert.list" }, key, tmpl);
  const listObj = list.find(x => x.data && Array.isArray(x.data));
  const entry = listObj && listObj.data.find(e => e.desc === CONFIG.desc);
  if (!entry) throw new Error("未找到新条目");
  console.log("      id=" + entry.id + "  SAN=" + entry.SAN);

  console.log("[4/6] cert.replace 赋予续签资格 (renewal:1 + relatedDDNSId)...");
  r = await send({ req: "appcgi.netsvr.cert.replace", data: {
    privateKeyBase64: CONFIG.keyB64, certificateBase64: CONFIG.certB64,
    issuerCertificateBase64: "", desc: CONFIG.desc, id: entry.id, isDefault: 0,
    privateKeyPath: "", certificatePath: "", issuerCertificatePath: "",
    renewal: 1, relatedDDNSId: CONFIG.relatedDDNSId,          // ← 关键字段
  }}, key, tmpl);
  if (!r.some(x => x.result === "succ")) throw new Error("replace 失败: " + JSON.stringify(r));

  console.log("[5/6] cert.renew 触发 fnOS ACME 签发...");
  r = await send({ req: "appcgi.netsvr.cert.renew", data: { id: entry.id } }, key, tmpl);
  const proc = r.find(x => x.result === "succ" && typeof x.data === "string");
  if (!proc) throw new Error("renew 失败: " + JSON.stringify(r));
  console.log("      processId=" + proc.data);

  console.log("[6/6] 轮询签发任务...");
  for (let i = 0; i < 36; i++) {
    await new Promise(r2 => setTimeout(r2, 5000));
    const st = await send({ req: "appcgi.netsvr.cert.applystatus", data: { processId: proc.data } }, key, tmpl);
    const s = st.find(x => x.data && x.data.status);
    console.log("      status=" + (s ? s.data.status : "?"));
    if (s && s.data.status === "success") break;
    if (s && s.data.status === "fail") throw new Error("签发失败: " + JSON.stringify(s.data));
  }

  const final = await send({ req: "appcgi.netsvr.cert.list" }, key, tmpl);
  const fe = final.find(x => x.data && Array.isArray(x.data)).data.find(e => e.id === entry.id);
  console.log("DONE. 最终证书:  issuedBy=" + fe.issuedBy + "  SAN=" + fe.SAN + "  renewal=" + fe.renewal);
  return fe;
}
