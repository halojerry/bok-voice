"use client";

import { useParams } from "next/navigation";
import { CallStudio } from "@/components/CallStudio";

export function CallStudioRoute() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  return <CallStudio callId={id} />;
}
