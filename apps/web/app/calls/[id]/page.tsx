import { CallStudio } from "@/components/CallStudio";
export default async function CallPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <main>
      <div className="mb-6">
        <h1 className="text-xl font-semibold">通话详情</h1>
        <p className="mt-1 text-sm text-[var(--muted)]">会话 {id}</p>
      </div>
      <CallStudio callId={id} />
    </main>
  );
}
