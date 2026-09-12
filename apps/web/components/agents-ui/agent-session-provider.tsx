import {
  SessionProvider,
  useTracks,
  AudioTrack,
  RoomAudioRenderer,
  type TrackReference,
  type UseSessionReturn,
  type SessionProviderProps,
  type RoomAudioRendererProps,
} from '@livekit/components-react';
import { Track, type Room } from 'livekit-client';

/**
 * Props for the AgentSessionProvider component.
 * Combines SessionProviderProps with RoomAudioRendererProps.
 */

export type AgentSessionProviderProps = SessionProviderProps &
  RoomAudioRendererProps & {
    /**
     * The room to provide.
     */
    room?: Room;
    /**
     * The volume to set for the audio renderer.
     */
    volume?: number;
    /**
     * Whether to mute the audio renderer.
     */
    muted?: boolean;
    /**
     * The session to provide.
     */
    session: UseSessionReturn;
    /**
     * 只渲染该 participant identity 的远端音频轨(2026-09-12 同传隔离加固):
     * 默认 RoomAudioRenderer 会渲染所有远端 Microphone 源轨——而 interpret worker
     * 的译文轨(trans-*)发布 source 也是 Microphone,服务端订阅权限万一有漏隙,
     * 译文就会从我方扬声器出声(用户实测:所有声音进我方耳机)。钉死只放指定
     * 身份(如 other-<callId>)的麦轨=对方原声,译文轨结构性进不来。
     */
    onlyRemoteIdentity?: string;
    /**
     * 完全不渲染音频(2026-09-12 终版):Chromium 40647375——element.setSinkId 对
     * WebRTC 远端流静默失效(读回 match:true 但声音仍走默认输出);一体台全部远端
     * 放音改走 AudioContext.setSinkId(interpret-console 的 createAudioRouter),
     * 渲染器层只剩 SessionProvider 语义,不再出声。
     */
    disableAudio?: boolean;
    /**
     * The children to render.
     */
    children: React.ReactNode;
  };

/**
 * A provider component for agent sessions that wraps SessionProvider
 * and includes RoomAudioRenderer for audio playback.
 *
 * @example
 * ```tsx
 * <AgentSessionProvider session={agentSession}>
 *   <AgentControlBar />
 *   <AgentChatTranscript />
 * </AgentSessionProvider>
 * ```
 */
export function AgentSessionProvider({
  session,
  children,
  onlyRemoteIdentity,
  disableAudio,
  ...roomAudioRendererProps
}: AgentSessionProviderProps) {
  const audio =
    disableAudio ? null : onlyRemoteIdentity ? (
      <IdentityFilteredAudio session={session} identity={onlyRemoteIdentity} {...roomAudioRendererProps} />
    ) : (
      <RoomAudioRenderer {...roomAudioRendererProps} />
    );
  return (
    <SessionProvider session={session}>
      {children}
      {audio}
    </SessionProvider>
  );
}

/** 只渲染指定 identity 远端参与者的音频轨(每轨独立元素,sink 逐元素生效)。 */
function IdentityFilteredAudio({
  session,
  identity,
  volume,
  muted,
}: {
  session: UseSessionReturn;
  identity: string;
  volume?: number;
  muted?: boolean;
}) {
  const tracks = useTracks([Track.Source.Microphone, Track.Source.ScreenShareAudio, Track.Source.Unknown], {
    room: session.room,
    onlySubscribed: true,
  });
  const wanted = (tracks as TrackReference[]).filter(
    (t) => !t.participant.isLocal && t.participant.identity === identity && t.publication.kind === Track.Kind.Audio,
  );
  return (
    <div style={{ display: 'none' }}>
      {wanted.map((t) => (
        <AudioTrack key={`${t.participant.identity}-${t.publication.trackSid}`} trackRef={t} volume={volume} muted={muted} />
      ))}
    </div>
  );
}
