"use client";

/**
 * 官方 Agent 可视化接口：官方 AgentAudioVisualizerGrid（shader 点阵，官方 agents 页同款）
 * + 情绪驱动颜色（useMoodColor）+ 可选的情绪文案叠层。
 * mood 为 null 时用中性色 #1FD5F9。
 */
import type { ComponentProps } from "react";
import type { AgentMood, AgentState } from "@livekit/components-react";
import { AgentAudioVisualizerAura } from "@/components/agents-ui/agent-audio-visualizer-aura";
import { useMoodColor } from "@/hooks/use-mood-color";

interface VoiceAgentInterfaceProps {
  size?: "icon" | "sm" | "md" | "lg" | "xl";
  state?: AgentState;
  mood?: AgentMood | null;
  audioTrack?: ComponentProps<typeof AgentAudioVisualizerAura>["audioTrack"];
  className?: string;
  /** 官方 Expressive 示例中的情绪文案叠层（mood 名居中显示） */
  showMoodLabel?: boolean;
}

export function VoiceAgentInterface({
  size = "lg",
  state = "connecting",
  mood = null,
  audioTrack,
  className = "",
  showMoodLabel = false,
}: VoiceAgentInterfaceProps) {
  const color = useMoodColor(mood);

  return (
    <div className={`relative inline-flex ${className}`}>
      <AgentAudioVisualizerAura
        size={size}
        state={state}
        color={color}
        audioTrack={audioTrack}
      />
      {showMoodLabel && (
        <span
          className="pointer-events-none absolute inset-0 flex items-center justify-center font-mono text-sm capitalize"
          style={{ color }}
        >
          {mood ?? "neutral"}
        </span>
      )}
    </div>
  );
}
