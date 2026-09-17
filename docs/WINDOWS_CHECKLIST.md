# Windows 实机验收 Checklist（节点分发行，2026-09-17 修订）

> 本机（Mac）无法实机验收 Windows；以下清单在 NVIDIA GPU 的 Windows 机器上逐项执行，全部通过后回填结果并归档。

## 硬件与安装（install-node.ps1 -Fetch 路径）

- [ ] Windows 10/11 x64，NVIDIA GPU（驱动 >= 550，显存 >= 8GB）
- [ ] `install-node.ps1 -CpUrl … -LicenseKey bokn_… -LivekitUrl ws://<内网IP>:7880 -Fetch` 自举：
      代码包+运行时包从云 CP 拉取（sha256 校验）、解到 %USERPROFILE%\bok-voice
- [ ] 首启模型下载：Qwen3-ASR/TTS + GGUF Q4_K_M（约 5.6GB）断点续传成功
- [ ] bok.py setup status -> ready: true
- [ ] 无 GPU / 驱动旧 / 显存不足机器：doctor 阻止 LLM 启动并给出明确文案
- [ ] -InstallService 注册 Task Scheduler bok-node-agent：开机自起 + 崩溃拉回；
      远程 update 后 exit(75) 被拉回且版本收敛

## 服务拓扑（七项端口）

- [ ] control-plane :8000 / ASR :8787 / TTS :8788 / LLM :1235 / B-line :8790 / LiveKit :7880 全 UP
- [ ] LLM 为 llama-server CUDA（--jinja + enable_thinking=false，回复无 reasoning、首 token 快）
- [ ] ASR 为 Qwen3-ASR transformers+CUDA：zh/cantonese/en 三语转写正确（粤语：我哋支持粵語）

## A 线

- [ ] 页面即时加载，对象/人设选择后可接通（agent worker 已注册 LiveKit）
- [ ] 普通话/粤语/英语通话：转写、回复语言、音色正确；打断生效
- [ ] 语音克隆：三语参考音频注册、试听、通话使用
- [ ] 重启 App 后对象/人设/通话/审计仍在（SQLite）

## B 线

- [ ] zh->en、cantonese->zh 双通道并发，字幕/音频正常
- [ ] 积压时 queueDepth/backlog 变化、丢弃生效；指标写 app-data

## 上线合规

- [ ] Windows 代码签名（可选但推荐，避免 SmartScreen 拦截）
- [ ] 卸载/重装、麦克风权限、防火墙（仅 127.0.0.1 绑定）记录
