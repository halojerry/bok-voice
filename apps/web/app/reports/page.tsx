export default function ReportsPage() {
  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">报表</h1>
        <p className="page-sub">用量 · 成本 · 质量分 · 每段 Provider 明细</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {[
          ["通话时长", "4h 12m"],
          ["总成本", "¥ 36.20"],
          ["ASR 调用", "128 次"],
          ["LLM tokens", "96k"],
        ].map(([k, v]) => (
          <div key={k} className="card">
            <p className="label">{k}</p>
            <p className="mt-2 text-2xl font-semibold">{v}</p>
          </div>
        ))}
      </div>

      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[1fr_360px]">
        <section className="card">
          <span className="label">通话记录</span>
          <table className="mt-3 w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-[var(--muted)]">
                <th className="pb-2">对象</th>
                <th className="pb-2">状态</th>
                <th className="pb-2">时长</th>
                <th className="pb-2">成本</th>
                <th className="pb-2">质量分</th>
              </tr>
            </thead>
            <tbody>
              {[
                ["Nguyen", "已结算", "08:12", "¥1.4", "88"],
                ["Marco", "已结算", "05:40", "¥0.9", "92"],
                ["李经理", "进行中", "12:03", "¥2.1", "—"],
              ].map((r) => (
                <tr key={r[0]} className="border-t border-[var(--card-border)]">
                  <td className="py-2">{r[0]}</td>
                  <td className="py-2 text-[var(--muted)]">{r[1]}</td>
                  <td className="py-2">{r[2]}</td>
                  <td className="py-2">{r[3]}</td>
                  <td className="py-2 text-[var(--accent)]">{r[4]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card">
          <span className="label">质量概览</span>
          <div className="mt-3 space-y-2 text-sm">
            <div className="flex justify-between"><span className="text-[var(--muted)]">接通率</span><span>99%</span></div>
            <div className="flex justify-between"><span className="text-[var(--muted)]">打断成功率</span><span>≥99%</span></div>
            <div className="flex justify-between"><span className="text-[var(--muted)]">知识命中率</span><span>94%</span></div>
            <div className="flex justify-between"><span className="text-[var(--muted)]">坐席接管成功率</span><span>100%</span></div>
          </div>
        </section>
      </div>
    </div>
  );
}
