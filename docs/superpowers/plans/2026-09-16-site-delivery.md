# 站点交付重定位：熔断真实化 + Windows 无头生命周期 + Tauri 启动器收口（探针先行）

日期：2026-09-16 ｜ 分支：`site-delivery-20260916` ｜ 执行：superpowers subagent-driven

## 已证根因（三路探察结论，计划针对它们）

1. **熔断空枪**：`revoke_node`（`nodes_store.py`）只改 status，心跳 401 后 license 流
   node_agent 自愈重注册 → 同指纹复活路径把吊销自动撤销（~60s）；`REFUSE_JOBS` 是无人
   消费的日志；语音派发链（云端 `/api/token` → 节点 LiveKit → worker 接活）与节点授权
   零耦合——revoke 之后客户站照样接电话。
2. **Windows 无头=stub**：`cmd_prod_install` Windows 分支只打印 NSSM 建议且仅列 2/5
   单元；`cmd_down` 在 Windows 静默失效（`os.killpg` AttributeError 被吞）；全仓零
   schtasks/service 代码；**doctor 缩进 bug**：`_nvidia_gate` 嵌在 `if not va_ok:` 内，
   装了 VB-CABLE/BlackHole 的机器会跳过 NVIDIA 硬门。
3. **Tauri 启动器缺口**：无单实例（双开=双份 serve）、无自启；`desktop/src/bridge.ts`
   +`manifest` 命令是零引用死代码；音频命令 macOS-only 已有 web fallback（Windows
   WebView2 的 setSinkId 可用，无需原生投入）。

## Global Constraints（全程约束）

- **探针先行**：每个里程碑先落「目标语义探针」再实现（先红后绿）；探针写法遵循
  `scripts/node_handshake_smoke.py` 范式（纯 stdlib、分步 `[PASS]/[FAIL]` 编号、单行
  汇总、退出码 0=过/1=败/2=环境缺）。
- 门禁全跑：pytest / `compileall -q` / web `tsc --noEmit && build` / cargo test /
  `verify_bundle.sh`。
- DB 变更只走 `deps.py build_engine()` 幂等段 + 重跑 `dump_postgres_ddl.py` 产物
  `scripts/.p0_supabase_schema.sql`（方言可移植，`test_db_portability` 必绿）。
- `docs/RUNTIME_TOPOLOGY.md` / `docs/REPO_MAP.md` 随数据流/路径变化同步更新。
- 新文件不得引入术语门禁禁用字面量（`test_cantonese_terminology` 全仓扫描）。
- Conventional commits，一任务一逻辑变更；严禁 `git add -u/-A`（并发会话 WIP 在树，
  只路径级 staging）。
- 熔断语义定案：root 吊销=sticky（须 root 显式解除）；clone 自动吊销保留复活路径；
  新通话云端即时窒息，存量通话随停栈 ≤1 心跳周期终止。客户强杀 node_agent 可暂避=
  已知边界（威慑非 DRM，与 P1 同立场）。

## M0 探针先行（先落，先红；不接 CI）

### Task 1: `scripts/probe_killswitch.py`
纯 stdlib 目标语义探针（复用 node-handshake 的 CP 环境，auth-on/off 双兼容，
`--base-url` 必选 + 可选 `--root-user/--root-pass`），9 步：
① licensed 注册（加固档 root 签 license）② 心跳 200 ③ root 吊销节点 ④ 心跳 401
"revoked" ⑤ **sticky**：同指纹重注册被拒 401（现存自愈复活→红）⑥ **云端窒息点**：
对 revoked 节点 `POST /api/calls` 与 `/api/token` 返 403（现存→红）⑦ 心跳 401 体
detail 含 `action:"shutdown"`（现存→红）⑧ root `POST /api/nodes/{id}/unrevoke`
（现存 404→红）后重注册复活；未解除前不得复活 ⑨ license 吊销=永久（注册 401，
无 self-heal 出路）。每步独立 FAIL 不崩溃，汇总行退出码。

### Task 2: `scripts/probe_windows_lifecycle.py`
Windows-only（POSIX exit 2 skip）：`cmd_down` 停止语义探针（起 python 子进程树→
pidfile→down→断言全灭，Windows 走 taskkill /T、POSIX 走 killpg 对照）；schtasks
注册→查询→运行→卸载零残留段（仅 Windows 执行）。

### Task 3: 瘦客户端探针
`apps/web/test/apiBase.test.mjs`（node --test：`window.__BOK_CONFIG__.cpUrl` >
build env > 默认 三档 + 缺省 `127.0.0.1:8000`；apps/web 无 JS 测试基建，需加
`"test": "node --test test/*.test.mjs"` script，api.ts 需可被 node 直载或以编译
产物测）+ `scripts/probe_thin_client_static.py`（stdlib：`apps/web/out/index.html`
含 runtime-config script 标签；`public/runtime-config.js` 可被覆写注入
cpUrl/livekitUrl；out 产物无烤死 `http://localhost:8000`——把 verify_bundle 的
grep 门扩到节点打包形态；out 缺建时 exit 2）。

### Task 4: cargo 单实例探针
`desktop/src-tauri` 内新增单实例目标语义测试（`#[ignore = "M3 single-instance"]`
标注防 break merge gate，M3 实装时移除 ignore 转绿）：断言插件注册路径存在/第二
实例行为钩子挂接点。真红由 M3 实装消解。

## M1 熔断真实化（Task 5，probe_killswitch 转绿）

1. **sticky**：nodes 表加 `revoked_source`（''|'root'|'auto_clone'）+`revoked_at`
   （deps 幂等补列+PG schema 产物重跑+portability 绿）；clone 自动吊销打
   auto_clone（复活路径保留）；root 吊销打 root；register 复用路径遇 root-revoked
   拒 401（错误体区分 `node revoked` vs `unknown node token`）。新增 root-only
   `POST /api/nodes/{id}/unrevoke`（清 status+source，审计 `node.unrevoked`）。
2. **云端窒息点**：`POST /api/calls` 与 `/api/token` 校验通话绑定节点 status，
   revoked→403 `node revoked`（云端是 token 唯一签发点=thin-node 拓扑咽喉；单机
   全栈形态无节点绑定不受影响）。
3. **停栈指令**：heartbeat 对 root-revoked 的 401 detail 附 `{"action":"shutdown"}`；
   node_agent `heartbeat_tick` 收到 revoked/shutdown → **不 self-heal** →
   `bok.cmd_down()` → 进程退出；license-revoked 退避确认后同停栈。
   `BOK_NODE_KILL_ON_REVOKE=1` 缺省（0 回退观测档）。
4. **root 操作面**：web `/nodes` 页（root 专属，users 页同构）：列表/status/
   last_seen/吊销/解除。
5. 测试：`test_nodes_hardening` 扩 sticky/unrevoke/窒息点/401 体；node_agent 测试扩
   revoked 不自愈+停栈调用；agent worker 零改动。

## M2 Windows 无头生命周期（Task 6，probe_windows_lifecycle 转绿）

1. `cmd_down` Windows 分支：`taskkill /PID <pid> /T /F` 树杀，POSIX 不动；孤儿清扫
   Windows 分支明文化。
2. `prod install` Windows 实装：Task Scheduler XML（onstart+RestartOnFailure+
   SYSTEM）→ `schtasks /create /xml /f`，对称 uninstall；日志同落 app-data/logs。
   节点包形态=node_agent 本身注册为守护任务（内部 cmd_up 拉栈）；5 单元模式留单机
   全栈。选 schtasks 弃 NSSM（零三方依赖）。
3. 内网放行：bind 0.0.0.0 env 开关 + `netsh advfirewall` 命令生成（默认打印，
   `--open-firewall` 显式执行）。
4. doctor `_nvidia_gate` 缩进修复+回归测试（VB-CABLE 已装仍跑 NVIDIA 门）。
5. `install-node.ps1` 加 `--install-service/--uninstall-service`。

## M3 Tauri 启动器收口（Task 7，cargo 探针转绿）

1. 删 `desktop/src/bridge.ts` + `manifest` 命令（注册、实现一并）。
2. tauri-plugin-single-instance（二实例聚焦旧窗、不重复 spawn serve）+
   tauri-plugin-autostart 登录自启开关（设置卡 checkbox 默认关）。
3. 音频命令保留现状（macOS 单机形态资产），零新增原生投入。

## M4 CI 接线 + 文档（Task 8）

1. `node-handshake.yml` 增 probe_killswitch 步（同 CP 复用，auth-on env）。
2. windows-latest job 跑 probe_windows_lifecycle（sqlite、无 docker）+保留 ps1
   Parser 检查。
3. `ci.yml` web job 增 `npm test`（node --test）+ probe_thin_client_static。
4. RUNTIME_TOPOLOGY.md（停栈契约/Windows 服务档）+ REPO_MAP.md 更新。

## 验收

- probe_killswitch 9/9 绿且进 CI；windows-latest lifecycle 绿；瘦客户端/单实例探针绿。
- 全套 merge gate 绿（pytest、npm test、cargo test、verify_bundle 三模式；
  doctor --packaged 待用户重打包后验证）。
- 语义一句话：新通话云端即时窒息，存量通话随停栈 ≤60s 终止，root 可解除；客户站
  从「重启即活」变成「root 点头才活」。
