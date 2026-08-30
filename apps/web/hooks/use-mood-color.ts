"use client";

/**
 * 官方 Expressive Agents 模式：把 agent 情绪（mood）映射为可视化器颜色，
 * 并用 motion + chroma-js 平滑过渡（避免颜色跳变）。
 * 来源：https://docs.livekit.io/frontends/agents-ui/audio-visualizer/expression
 */
import { useEffect, useState } from "react";
import { animate, useMotionValue, useMotionValueEvent, useTransform } from "motion/react";
import chroma from "chroma-js";
import type { AgentMood } from "@livekit/components-react";

// Hue carries valence (warm for bright moments, cool for heavy ones); saturation carries
// intensity, so a quiet mood never out-shouts a strong one.
export const MOOD_COLORS: Record<AgentMood, `#${string}`> = {
  angry: "#F5222D",
  excited: "#FF7A45",
  happy: "#FFC53D",
  playful: "#F759AB",
  surprised: "#B37FEB",
  anxious: "#D46B08",
  hopeful: "#52C41A",
  empathetic: "#36CFC9",
  curious: "#6600FF",
  sad: "#2F54EB",
  calm: "#1FD5F9",
};

// Shown when the agent hasn't expressed anything recently.
export const NEUTRAL_COLOR: `#${string}` = "#1FD5F9";

export function useMoodColor(
  mood: AgentMood | null,
  moodColors: Record<AgentMood, `#${string}`> = MOOD_COLORS,
): `#${string}` {
  const targetColor = mood ? moodColors[mood] : NEUTRAL_COLOR;
  const colorProgress = useMotionValue<string>(targetColor);
  const hexColor = useTransform(colorProgress, (latestRgba) => chroma(latestRgba).hex());
  const [color, setColor] = useState<`#${string}`>(targetColor);

  useMotionValueEvent(hexColor, "change", (latestHex) => setColor(`#${latestHex.slice(1)}`));

  useEffect(() => {
    const controls = animate(colorProgress, targetColor, { duration: 1, ease: "linear" });
    return () => controls.stop();
  }, [targetColor, colorProgress]);

  return color;
}
