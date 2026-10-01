# Mimosa 深度扫描分流记录（2026-10-01）

- **Scan ID**: `scan-2026-10-01T18-47-23.828Z-7bbaa3aad397`
- **Seal**: `sha256:fa7ff244ff6a80569e20c43845cd0f34694a71bfc122087da365d7c1f728335a`
- **产物目录**: `~/.mimosa/security-scans/project-d1f7dd6347bab9ae910b681c/scan-2026-10-01T18-47-23.828Z-7bbaa3aad397`
- **范围**: worktree `fix/web-ux-root-cause` @ fa27715+未提交三刀（UX 性能/数据层/交互逻辑波）
- **结论**: 104 findings（51 high / 44 medium / 9 low）——**逐类亲验后零条成立为可执行真漏洞**，全部归因于两类系统性误报 + 一次 docstring 解析错误。未做任何"修复"（对误报打补丁=churn）。

## 分流矩阵（51 high 全覆盖）

| # | 类别 | 条数 | 处置 | 亲验证据 |
|---|---|---|---|---|
| 1 | 代码注入 eval `services/laya-sidecar/app.py:25` | 1 | 误报 | 第 25 行是模块 docstring 英文（"the eval measured 199 q/s"）；`grep eval(` 代码区零命中 |
| 2 | SSRF/路径穿越 @ scripts/*（探针/压测脚本）+ tools/bok.py | ~46 | 不适用（env 污点过度报告） | 每条污点链源=「环境变量」。本地优先产品：env 全为操作员部署面、目标=自家 localhost；给 30+ 开发脚本加 URL 校验零安全收益 |
| 3 | 所有权缺失 @ main.py:1342（DELETE /api/tts/minimax-voices/{voice_id}） | 1 | 误报 | handler 首行 `auto_gate_management(request)`——闸在函数体/依赖里，扫描器只认 middleware/guard 形状 |
| 4 | 所有权缺失 @ main.py:5131（GET /api/qa/{id}/canned-audio） | 1 | 误报 | `_gate_page("qa")` + `deny_cross_account` 双闸齐全 |
| 5 | 穿越链 @ main.py:6488（provider-health） | 1 | 不适用（env 污点） | 路径=VAULT_ROOT env 的兄弟目录拼接，无用户输入进路径 |
| 6 | node_agent 出站 SSRF ×4 + 穿越 ×1 | 5 | 不适用 | 四个调用点 URL 全部 `f"{cfg.cp_url}/api/nodes/..."` 钉死 CP 域；下载 URL=CP 命令版本号拼接，无远程任意 URL 面 |
| 7 | markdown_source.py:80 SSRF | 1 | 不适用 | `BokMarkdownSource` base_url=`BOK_URL` env（注释明说 "later milestone" 未接线）；配置面非请求面 |

## medium/low 未修依据

- **30 条 business-logic（inconclusive candidate）**：Mimosa 自身契约 `evidenceState=candidate` ≠ finding；抽查样例（`get_settings`"未见权限"）即闸盲区（实际 PUT root 闸、GET 掩码层）。
- **14 条 cross-file**：同 env 污点族（env → qwen3-tts.js fetch）。
- **9 low static**：同族低危。

## 依赖面盲区（待 Mimosa 侧改进）

`offlineAdvisory: matchedPackages=1 / matchedAdvisories=2 / unknown=356`——**产物未给命中包名**（`packages: []`），无法定位；356 unknown=离线库覆盖缺口。下次复扫若给出包名再处理。

## 遗留纵深项（可选，未做——需产品级拍板）

1. **node_agent 出站钉扎**：强制 https + host==cfg.cp_url——防节点配置文件被篡改后的外传面；属设计决策非快修。
2. **复扫降噪**：为 env 类污点源配置白名单（若 Mimosa 支持），否则每次深扫重复 ~80 条同族噪音。

## 复扫对照基线

本文件记录的误报形状（docstring 解析 / env 污点 / FastAPI 依赖闸盲区）可作为下次扫描 diff 时的已知噪音清单；新出现的 finding 先对照本矩阵三类形状，不在形状内才进人工分流。
