# 新 Mac 一键演示（demo-cloud 档）

目标：一台全新 Mac 上，三条命令进入可演示状态——`bootstrap.sh` → `bok demo-setup` → `bok serve`。
姿势 = **A 线全云**（豆包 SAUC ASR + DeepSeek a_reply/judge/settle + MiniMax TTS）+ **B 线 MT 本地**（:1236，Hy-MT2）；本地盘只落真正需要的模型（首跑自动 `download --only mt`）。姿势判定与 ensure 收窄机制见 `docs/RUNTIME_TOPOLOGY.md` 与 AGENTS.md「云端演示档 serve 云感知」条。

## ① 前置

- macOS（Apple Silicon）+ Xcode CLT（`xcode-select --install`）+ git。
- Homebrew `python@3.12`（`brew install python@3.12`）。
- 磁盘：源码 + venv 约 3 GB；演示档模型只下 MT2 约 2 GB（本地 ASR/主 LLM/TTS 权重都不需要）。
- 三把云凭据：豆包（新式单 Key + 资源 ID）、MiniMax、DeepSeek。

## ② 装依赖

```bash
git clone <repo> && cd voice-assistant
./scripts/bootstrap.sh          # python3.12 不在标准路径时: PYTHON=$(command -v python3.12) ./scripts/bootstrap.sh
```

## ③ 一键配置演示档

```bash
python tools/bok.py demo-setup \
  --asr-key <豆包单Key> --asr-resource-id <豆包资源ID> \
  --tts-key <MiniMax Key> --deepseek-key <DeepSeek Key>
```

- **key 不落终端史册的形态**：`BOK_DEMO_ASR_KEY` / `BOK_DEMO_ASR_RESOURCE_ID` / `BOK_DEMO_TTS_KEY` / `BOK_DEMO_DEEPSEEK_KEY` 环境变量预先 export，命令行省略对应旗标（argv 有值时 argv 优先）。keys 只进设置库（`~/Library/Application Support/BokVoice/bok_voice.db`），不进仓、不回显——确认行只有 `<set:Nch>` 掩码。
- **必须在栈下执行**：control-plane（:8000）在跑会拒绝退出（先 `python tools/bok.py down`）。
- 可选旗标：`--deepseek-base-url`（缺省 `https://api.deepseek.com/v1`）、`--mt-model`（B 线 MT 显式模型路径；缺省留空走 env 缺省链 :1236 的 MT2）。
- 写入面：asr 段（provider=doubao + 凭据）、tts 段（provider=minimax + key）、模型路由五车道（a_reply/judge=deepseek-flash 关思考、settle=deepseek-v4-pro **开**思考（纪要）、mining=deepseek-v4-pro 关思考、mt=本地 :1236）+ 预置 `demo-cloud`（无密钥快照）。既有其他设置键/车道/预置全部保留；重跑幂等，换 key 重跑即更新。

## ④ 拉栈

```bash
python tools/bok.py serve
```

首跑自动按姿势收窄下载（只 `mt`），:8787/:1235/:1237/:8788 四个本地腿如实标 `skipped (cloud: …)` 不拉起；就绪后 `bok status` 全绿。

## ⑤ 演示

浏览器开 `http://127.0.0.1:3000`（本机回环缺省无鉴权；要暴露局域网先按 AGENTS.md「auth-on 开发栈标准姿势」配置再起）。

## 回切本地档

- **LLM 车道**：`curl -X POST http://127.0.0.1:8000/api/model-routing/presets/local-9b/apply -d '{}'`（套档不清密钥；切回演示档=`…/presets/demo-cloud/apply`——demo-setup 已替你写好这档）。注意 `local-9b` 预置不是新机自带：路由预置存设置库，需要在 web 设置 → 模型路由里保存过一档才存在。
- **serve 侧强制本地腿**（不改任何设置，下一通/下次 serve 生效）：`BOK_LOCAL_ASR=1 BOK_LOCAL_TTS=1 BOK_LOCAL_LLM=1 python tools/bok.py serve`。
- **永久切回本地 ASR/TTS**：web 设置页把 `asr.provider`/`tts.provider` 改回本地档；`BOK_DOUBAO_ASR=0` 是 ASR 云档的显式回退口（绝不静默上云/哑掉）。

## 探针测量腿须知

E2E/延迟探针用本地 :8787/:8788 渲染客户话音、验 agent 出声语言与文本——全云档跑探针必须 `BOK_LOCAL_TTS=1 BOK_LOCAL_ASR=1` 增量补起这两个测量仪器（产品车道不受影响，纯仪器位）。探针清单一页表见 `docs/RUNBOOK.md`；延迟预算见 `docs/LATENCY_BUDGETS.md`。

## 故障

一行去处：`docs/RUNBOOK.md`（★症状→入口；含回退开关全表与僵尸 worker 清理姿势）。
