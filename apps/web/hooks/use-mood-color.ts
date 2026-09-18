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
//
// P4 light-UI calibration (2026-09-18): values darkened one step where the old hex
// fell under 3:1 contrast on the white background (--background: oklch 1 0 0).
// Mapping logic / mood names / motion transition unchanged — value table only.
// WCAG ratio vs white: before → after.
export const MOOD_COLORS: Record<AgentMood, `#${string}`> = {
  angry: "#F5222D", // 4.08 (kept)
  excited: "#FA541C", // #FF7A45 2.59 → 3.31 (one ramp step down)
  happy: "#D97706", // #FFC53D 1.58 → 3.19 (gold ramp tops out at 2.87; amber-600)
  playful: "#EB2F96", // #F759AB 3.01 → 3.90 (headroom on soft card bg)
  surprised: "#9254DE", // #B37FEB 2.94 → 4.62
  anxious: "#D46B08", // 3.56 (kept)
  hopeful: "#389E0D", // #52C41A 2.27 → 3.46 (one ramp step down)
  empathetic: "#08979C", // #36CFC9 1.92 → 3.55 (cyan-5 fails at 2.21)
  curious: "#6600FF", // 6.98 (kept)
  sad: "#2F54EB", // 5.85 (kept)
  calm: "#0891B2", // #1FD5F9 1.76 → 3.68 (cyan-600, same family as --live)
};

// Shown when the agent hasn't expressed anything recently.
// Deliberately uncalibrated: this is the --live brand accent (青只作活信号), not a mood.
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
