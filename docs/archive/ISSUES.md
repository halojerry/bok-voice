# Issues / 待办

## 已修复

- ISSUE-F1：对象建档字段名不符（`name/role`→`display_name/role_template`）导致 422；已修 UI 与 API。
- ISSUE-F2：人设保存字段名不符（`speech_style`→`tone`）；已修。
- ISSUE-F3：`list_personas` 读内存 dict（只对 InMemory 生效），Postgres 下返回空；已改为走 repository。
- ISSUE-F4：通话状态从不落库（`hangup`/`takeover`/`transfer` 只改内存 dict）；新增 `update_call()` 持久化，`token→active`、`takeover→escalated_to_human`、`hangup→ended` 已生效。
- ISSUE-F5：用户转向被记录两次（`user_input_transcribed` 与 `conversation_item_added` 双触发）；改用唯一来源 `conversation_item_added`。
- ISSUE-F6：新建通话前端频繁 404（预拉 settlement 未生成）；改为只在 `/calls/[id]` 或挂断后拉取。
- ISSUE-F7：主管台按钮为占位；补 `supervisorJoin/Pause/Takeover/Transfer` API 并接线。

## 待办

- ISSUE 1：OCR / 导入 .md 与表格批量建对象。
- ISSUE 2：Provider 熔断与降级状态机（熔断器 + monitoring）。
- ISSUE 3：账号隔离审计（A 不可读 B，行级 + 向量过滤）。当前知识隔离已在 pipeline E2E 验证；业务行级与审计告警待强化。
- ISSUE 4：结算幂等与补投；对象主题合并与全局洞察蒸馏。
- ISSUE 5：全局知识库脱敏与授权（跨账号共享的脱敏抽象经验）。
- ISSUE 6：主管台接管状态机（真实接管到人 + whisper 私语）。
- ISSUE 7：录音合规与保留期、删除/被遗忘。
- ISSUE 8：20–50 路并发压测与容量告警。
- ISSUE 9：真实 sherpa / GPT-SoVITS / 火山 / 讯飞 / Ollama 适配器接入与基准。
- ISSUE 10：火山 TTS V3 参数校准（`seed-tts-2.0` 音色 + `explicit_language`/`explicit_dialect`，覆盖越南语/粤语；双向流式 `StartConnection→StartSession→TaskRequest` 待按需开启）。
- ISSUE 11：Agent `entrypoint` 生命周期收尾（`session.end()`/`finally`，消除 `did not exit in time` 告警）。
- ISSUE 12：CI（lint + pytest + next build + browser e2e）。
