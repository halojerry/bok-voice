// url-guard 单测（Mimosa SSRF 修复，2026-09-23）——node --test 离线跑。
import test from "node:test";
import assert from "node:assert/strict";

import { assertLocalDiagUrl, UrlGuardError } from "../src/lib/url-guard.js";

test("loopback variants pass", () => {
  assert.equal(assertLocalDiagUrl("http://127.0.0.1:8000/api"), "http://127.0.0.1:8000/api");
  assert.ok(assertLocalDiagUrl("http://localhost:8010/"));
  assert.ok(assertLocalDiagUrl("https://[::1]:8788/v1/audio/speech"));
});

test("non-http scheme rejected", () => {
  for (const bad of ["ftp://127.0.0.1/x", "file:///etc/passwd", "", "not a url"]) {
    assert.throws(() => assertLocalDiagUrl(bad), UrlGuardError);
  }
});

test("foreign hosts rejected (exact-match, no suffix tricks)", () => {
  for (const bad of [
    "http://example.com/api",
    "http://192.168.1.5:8000/api",
    "http://169.254.169.254/latest/meta-data",
    "http://evil.localhost/api",
  ]) {
    assert.throws(() => assertLocalDiagUrl(bad), UrlGuardError);
  }
});

test("extraHosts param and env extension work", () => {
  delete process.env.BOK_RT_EXTRA_HOSTS;
  assert.ok(assertLocalDiagUrl("http://cloud-cp.internal:8000/api", ["cloud-cp.internal"]));
  assert.throws(() => assertLocalDiagUrl("http://other.internal/api", ["cloud-cp.internal"]));
  process.env.BOK_RT_EXTRA_HOSTS = "cp.example.net, llm.example.net";
  assert.ok(assertLocalDiagUrl("http://cp.example.net/api"));
  assert.ok(assertLocalDiagUrl("http://llm.example.net:1235/v1"));
  assert.throws(() => assertLocalDiagUrl("http://not-in-env.example.net/api"));
  delete process.env.BOK_RT_EXTRA_HOSTS;
});

test("error message carries no query string", () => {
  try {
    assertLocalDiagUrl("http://evil.example.com/cb?token=sekrit-value");
    assert.fail("should have thrown");
  } catch (err) {
    assert.ok(!(String(err.message).includes("sekrit-value")));
  }
});
