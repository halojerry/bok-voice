// 出站 baseUrl 守卫（Mimosa SSRF 修复，2026-09-23）。
// 镜像 packages/core/bok_voice_core/urlguard.py 的「本地诊断」语义：
// 只放行 http(s) + 环回 host；云端/内网端点经 env BOK_RT_EXTRA_HOSTS
// （逗号分隔）或构造参数 extraHosts 显式扩展。字符串精确匹配、不做后缀匹配。
// 供 realtime-translation 各 provider 在构造期校验 baseUrl，fail-fast。

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

export class UrlGuardError extends Error {
  constructor(message) {
    super(message);
    this.name = "UrlGuardError";
  }
}

export function assertLocalDiagUrl(raw, extraHosts = []) {
  let u;
  try {
    u = new URL(String(raw || "").trim());
  } catch {
    throw new UrlGuardError("invalid url");
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") {
    throw new UrlGuardError(`scheme not http/https: ${u.protocol}`);
  }
  // Node 的 URL.hostname 对 IPv6 保留方括号（"[::1]"，与 Python urlsplit 相反）——
  // 统一剥掉再比对，两侧白名单语义一致。
  const host = (u.hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  if (!host) {
    throw new UrlGuardError("url has no host");
  }
  const envExtra = (process.env.BOK_RT_EXTRA_HOSTS || "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  const allowed = new Set([
    ...LOOPBACK_HOSTS,
    ...extraHosts.map((h) => String(h).trim().toLowerCase()).filter(Boolean),
    ...envExtra,
  ]);
  if (!allowed.has(host)) {
    throw new UrlGuardError(`host not in local-diag allowlist: ${host}`);
  }
  return raw;
}
