import { CallStudio } from "@/components/CallStudio";

export default function NewCallPage() {
  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">通话工作台</h1>
        <p className="page-sub">AI 扮演我方上线接听，带对象档案 + 知识库 + 历史上下文。</p>
      </div>
      <CallStudio />
    </div>
  );
}
