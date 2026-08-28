# LiveKit 本地自托管

配置见 `livekit.yaml`。启动：

```bash
docker compose up -d livekit
```

默认：

- HTTP API / WebRTC
  - `http://localhost:7880`
  - `ws://localhost:7880`
- RTC UDP：`7882`

Agent 使用 `LIVEKIT_URL=ws://localhost:7880`、`LIVEKIT_API_KEY=devkey`、`LIVEKIT_API_SECRET=devsecret` 连接。

生产部署需启用 Redis、TURN/coturn、Egress，并按 `docs/PLAN.md` 的 M6 扩展。
