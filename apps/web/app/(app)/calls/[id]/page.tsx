import { CallStudioRoute } from "./CallStudioRoute";

// output:export 要求动态段在构建时枚举。此处生成一个哨兵参数作为静态壳，
// 实际 id 由客户端从 /api/calls/{id} 拉取（SPA 内跳转仍实时解析）。
export function generateStaticParams() {
  return [{ id: "call" }];
}

export default function CallPage() {
  return (
    <main>
      <div className="mb-8">
        <h1 className="page-title">通话详情</h1>
        <p className="page-sub">会话详情</p>
      </div>
      <CallStudioRoute />
    </main>
  );
}
