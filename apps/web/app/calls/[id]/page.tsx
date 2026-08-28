import { CallStudio } from "@/components/CallStudio";
export default async function CallPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <main>
      <div className="mb-8">
        <h1 className="page-title">通话详情</h1>
        <p className="page-sub">会话 {id}</p>
      </div>
      <CallStudio callId={id} />
    </main>
  );
}
