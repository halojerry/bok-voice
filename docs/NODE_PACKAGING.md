# node-agent 二进制打包（PyInstaller）

本文描述 `tools/node_agent.py` 的单文件二进制打包：构建方式、产物边界与后续路线。

## 打包范围与边界（诚实范围）

**本包只含 node-agent 本体**——薄节点守护（spec §4.2/§4.3，P0 职责）：

1. 向云 CP 心跳上报（`POST /api/nodes/heartbeat`，失联 ≥max_missed 置 `REFUSE_JOBS` 旗标）；
2. UI 配置注入代理（`--ui-dir` 时写 `runtime-config.js`，`window.__BOK_CONFIG__` 的 cpUrl/livekitUrl）；
3. 节点注册载体（`--node-token` 来自 NodeStore 签发，本包只透传使用）。

**本二进制的受支持模式是 `--heartbeat-only`**（可叠加 `--ui-dir`）。完整节点栈——
LiveKit server、agent/interp workers、模型 sidecar、`tools/bok.py` 的 `cmd_up/cmd_down`
全栈编排——**仍需 venv，不在本包**：spec 里 `excludes=['bok']` 刻意排除了
node_agent 唯一的非 stdlib 懒加载 import，不带 `--heartbeat-only` 运行会得到明确的
`ModuleNotFoundError: bok`，而不是半残的全栈行为。节点跑全栈 = 本二进制（心跳/配置注入）+ venv 栈，二者并存。

### import 面分析（哪些依赖进了包）

`tools/node_agent.py` 模块级 import 全部为 stdlib：
`argparse / json / sys / threading / time / urllib.request / dataclasses / pathlib`。

- 因此构建 venv（`.venv-pyinstaller`）**只需安装 pyinstaller**，产物内零第三方运行时依赖；
- 唯一的非 stdlib import 是 `main()` 内的懒加载 `import bok`（tools/bok.py 全栈编排器，
  会连带 torch/hf_transfer/certifi 等重型运行时），已按边界排除，不进包；
- spec 的 `pathex` 含仓库根 + `tools/`，与源码运行时把 `tools/` 插 `sys.path` 的口径一致；
- `hiddenimports` 为空即「列全」——stdlib 由 PyInstaller 静态分析自动覆盖，无隐藏导入缺口。

## 为什么先 PyInstaller 后 Nuitka（P2 原计划不变）

- **PyInstaller 现在做**：成熟稳定、构建快、失败面小，先把「源码 → 单文件产物 → 分发 →
  sha256 校验」这条链路跑通，验证产物形状与节点侧部署体验；
- **Nuitka 留到 P2**：真编译到 C，抗逆向与性能更强，但工具链重、对编译器/版本敏感、
  构建慢。等 license/机器码鉴权落地、对产物混淆有硬需求时再切换，届时只换 spec 层，
  打包入口与分发链路不变。

## 构建步骤

```bash
scripts/build_node_agent.sh            # 构建 + 验证 + 报告（sha256），清 build/ 中间物
scripts/build_node_agent.sh --clean    # 同上，但先清 pyinstaller 缓存（怀疑缓存污染时）
PYTHON=/path/to/python3.12 scripts/build_node_agent.sh   # 覆盖解释器（默认同 bootstrap.sh）
```

脚本行为：

1. 创建/复用独立构建 venv `.venv-pyinstaller`（`.gitignore` 的 `.venv*/` 已覆盖，不污染 `.venv312`）；
2. 只装 `pyinstaller>=6.10,<7`（node_agent 运行面纯 stdlib，无其他依赖）；
3. `pyinstaller scripts/node_agent.spec --noconfirm` 产出 onefile `dist/node-agent`；
4. 验证 `./dist/node-agent --help` 退出 0 且输出含 usage（`set -e` 保证非 0 即失败）；
5. 打印产物大小与 sha256；验证通过后删除 `build/` 中间物。
   `dist/` 与 `build/` 均已 gitignore，**构建产物不入库**。

## 产物校验

```bash
shasum -a 256 dist/node-agent          # 与分发渠道公布的摘要比对
./dist/node-agent --help               # 冒烟：打印三语参数用法（--cp-url/--node-token 必填）
```

节点侧落地示例：

```bash
./node-agent --cp-url https://cp.example.com --node-token <node_token> \
  --heartbeat-only --ui-dir /srv/bok/web-out
```

## 签名与升级验签（后续轮，本轮不带）

**本轮产物不带任何签名**：PyInstaller `codesign_identity=None`，无 embedded 签名清单，
无升级验签逻辑。分发渠道当前只提供 sha256 完整性比对（防损坏，不防伪造）。
升级验签（签名产物 + 端上验签 + 回滚策略）在后续轮与鉴权体系一并落地。

## 机器码 / license 客户端路线

node-agent 是后续 license/机器码鉴权的**客户端载体**：

- 机器码采集（platform/硬件指纹）与 license 校验将挂在 node-agent 启动路径——
  心跳上报附带机器指纹，云端比对决定派发；
- 未授权节点在 agent 侧只表现为心跳被拒/不派发（现有 `REFUSE_JOBS` 通道复用），
  部署形态无需改动，升级本二进制即可。
