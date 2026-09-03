# CI/CD 与代码质量规范化方案

> 状态：**进行中**（2026-09-04 起）。已落地部分标 ✅，未落地标 ◻。
> 目标：把「PR 过了才能合 main」「本地验收通过才 release」从 AGENTS.md 纸面纪律变成仓库机器强制。

## 1. 现状盘点（已核实）

### CI（`.github/workflows/ci.yml`）— 5 个 job，全部只做「编译/测试/类型检查」

| Job | 内容 | 跑什么 |
|---|---|---|
| `python` | Python checks | `compileall` + 全量 pytest |
| `node` | Node tests (realtime-translation) | `npm ci && npm test` |
| `web` | Web typecheck + export | `tsc --noEmit` + `npm run build` |
| `bok` | Launcher smoke | `bok.py manifest/status/doctor` |
| `desktop` | Desktop shell (Rust) | `cargo test` + `cargo check` |

- Trigger：`push: branches: ["**"]` + `pull_request:` → **每个分支每次 push 都全量跑一遍**，浪费且把关位置不对（应该 PR 才全量）。
- `release.yml`：tag `v*` push 触发 → macOS/Windows 打包矩阵 + `verify_bundle.sh` + `gh release`。✅ 已带 5 平台 Cargo/pip 缓存、artifacts 上传。

### 缺口

- **无 lint**：Python（无 ruff/black/mypy，venv 里都没装）、Web（无 eslint）、Rust（`cargo check` 未上 clippy）。
- **web `lint` 脚本是坏的**：`apps/web/package.json` 里 `"lint": "next lint"`，但 **Next 16 已移除 `next lint`**（实测把 `lint` 当目录名解析，直接报错）。CI 也没跑它，所以一直没暴露。
- **origin 默认分支 / main 保护**：本方案配套已把 origin 默认分支设为 `main`（原来误设成 feature 分支），并给 main 加保护。
- **release 不可手动触发**：只能打 tag，无 `workflow_dispatch` dry-run，无法在 CI 里先做 staging 验收再决定发布。
- 单行最长 Python 代码 196 字符（apps/agent/agent_runtime/flow.py 一带），全量上严格 lint 会刷屏，需从 bug 类起步分阶段。

## 2. 目标

1. **质量闸前移**：PR 是全量检查的唯一入口，lint + 类型 + 测试都在合并前强制跑。
2. **main 受保护**：禁直推，只能 PR 合并；CI 必过（`strict` 要求基于最新 main）。
3. **发布有门禁**：tag 前可在 CI 上 staging 验收，验收通过才真正发布。

## 3. 阶段一：补齐 lint（改动小，先做）

### 3.1 Python — ruff
- `requirements-dev.txt` 加 `ruff`。
- 根目录 `ruff.toml`：
  ```toml
  line-length = 110
  target-version = "py312"
  # 先开 bug/未定义/import 类，风格类(F401 之类)跑顺后再决定加不加。
  # 仓库现有单行最长 196，贸然全开规则会大量误伤。
  select = ["E", "F"]
  exclude = ["desktop/runtime", "node_modules"]
  ```
  （不引 black/isort——ruff 自带 `format`/`I` 可后续开，先不动格式。）
- CI `python` job 在 compileall 前插：
  ```yaml
  - name: Lint
    run: ruff check apps packages services tools scripts tests
  ```

### 3.2 Web — ESLint（修 `next lint` 之死）
- `apps/web` 装 `eslint@^9` + `eslint-config-next`（flat config）。
- 建 `apps/web/eslint.config.mjs`（Next 16 官方迁移：`next lint` 已死，flat config 是正路）。
- `package.json`：`"lint": "eslint ."`。
- CI `web` job 加 `npm run lint`（在 `tsc --noEmit` 前）。

### 3.3 Rust — clippy
- CI `desktop` job：`cargo check` 升级为：
  ```yaml
  - name: Clippy
    run: cargo clippy --all-targets -- -D warnings
  ```
  若现有代码 warning 较多，可先 `-D warnings` 改 warn 过渡一轮。

### 3.4 统一收口
- `.github/workflows/ci.yml` 顶部加 `permissions: contents: read`（release.yml 已单独声明 write，最小化原则）。
- 可选：`.editorconfig`（py 4 空格 / TS·JSON 2 空格），`pre-commit` 本地钩子放阶段三。

## 4. 阶段二：PR 门禁

### 4.1 ✅ origin 默认分支 → `main`
GitHub Settings → Branches → default branch（已通过 `gh api` 改为 `main`；合并 feature 后远程不会再有「默认分支是 feature」的混乱）。

### 4.2 ✅ `main` 分支保护（已配置）
Settings → Branches → Branch protection rule（`main`）：
- **Require a pull request before merging**：approval count = **0**。理由：halojerry 是仓库唯一维护者，GitHub 不允许作者给自己的 PR 打 approval，设 1 会自锁合并；以后有第二位 reviewer 再升 1。
- **Require status checks to pass before merging**（5 个 context，`strict` = require branches up to date）：
  `Python checks`、`Node tests (realtime-translation)`、`Web typecheck + export`、`Launcher smoke`、`Desktop shell (Rust)`
- **Enforce admins**：管理员也走 PR，不能直推绕过。
- 效果：feature → PR → CI 绿 → merge main；`main` 直推被 GitHub 拒绝。

### 4.3 ◻ CI trigger 收窄（省钱 + 把关位正确）
`ci.yml` 改：
```yaml
on:
  push:
    branches: [main]        # main 上每次合并后全量回归
  pull_request:             # 各 PR 全量；日常 feature push 不再空跑
```
- 全量检查 = 只有 PR 与 main 合并后；日常 push 触发 `Launcher smoke` 这种秒级 smoke 即可（可另加一个轻量 job 只跑 compileall，可选）。

### 4.4 ◻ PR 模板
`.github/PULL_REQUEST_TEMPLATE.md`：根因 / 改动 / 验证证据（pytest、npm test、cargo test、web build、verify_bundle 各模式）/ 本地验收清单——对齐 AGENTS.md merge gate，reviewer 有据可查。

## 5. 阶段三：发布治理

### 5.1 ◻ release 加 `workflow_dispatch`
`release.yml` 增加手动触发 + `dry-run` 开关（`dry-run=true` 时打包+verify 但不建 Release），让「打 tag 前先在 CI 上跑 staging 验收」成为一条显式路径。

### 5.2 ◻ verify_bundle 显式矩阵
`release.yml` 里把 `verify_bundle.sh --staging/--app/--doctor` 从注释纪律提成显式步骤（跑挂即 fail）。AGENTS.md 已要求：发布前 `doctor --packaged` 必须报 `token endpoint: ok (real JWT)`。

### 5.3 纪律（不机器化）
- **Do not tag or release until full local acceptance passes**（项目政策，AGENTS.md）——tag 只能人工在本地全绿后打。
- 合并流程 = feature 分支 → PR（阶段二保护强制）→ merge → 删远程 feature 分支。

## 6. 明确不做 / 缓做

- **mypy / strict typing**：项目 Python 已 py312 `from __future__ import annotations` 全注解，先靠 ruff E/F + pytest；mypy strict 试点 `apps/agent` 放后续。
- **black/isort**：ruff 可代，暂不开 format，避免大 diff。
- **Rust 镜像**（`~/.cargo/config.toml` → rsproxy）只影响本机拉取，**不改 CI**（GitHub runner 直连 crates.io）。
- **Dependabot / CodeQL / 自动依赖升级**：本地优先项目，人工审，不开自动。

## 7. 落地清单（本仓库配套已完成项）

- ✅ origin `main` 分支已创建（基线 = 上次推送 trunk 6e9e653），默认分支已切 `main`。
- ✅ `main` 分支保护已开（PR + 5 status checks strict + enforce admins，0 approval）。
- ✅ PR #2（feature → main）已开，CI 跑批中；本地 pytest 112 / web build / node 9 / cargo 3 全绿。
- ◻ 阶段一 lint（ruff / eslint / clippy）——下一次迭代。
- ◻ 阶段二 CI trigger 收窄 + PR 模板——下一次迭代。
