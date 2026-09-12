"use client";

/**
 * 坐席一体台（单页双通道工作台,参考金喜同传的单页操作形态）:
 * 两人同机各一支麦,一个控制台同时接入同传房间的 me/other 两个身份,
 * 同页看双向原文+译文字幕;出声单向——我方译文 TTS 播给对方听。
 *
 * - me 身份走 interpret 页同款官方会话(AgentSessionProvider + useTranscriptions)
 *   ——字幕沿用已验证管线(lk.transcription 全量广播,同页双向原文译文都看得到)。
 * - other 身份是纯手动 livekit Room:只负责「对象的麦克风收音 + 对象译文放音」,
 *   不重复渲染字幕(同一房间两边看到的是同一份字幕)。
 * - 听感拓扑(2026-09-12 终版):对方=听我方译文 TTS(fwd,trans-<对方语言>,other
 *   房只挂 trans- 轨);我方=听对方麦克风原声(me 渲染器,me 房唯一远端音频),
 *   rev 译文纯字幕零 TTS——同传台姿势:听原声+看译文。双输出档:「对方扬声器」
 *   =译文指到朝向对方的音箱,「我方扬声器」=对方原声指到我方耳机/音箱。
 * - 自动半双工(默认开):我方译文出声时自动暂让对方麦克风——共享扬声器外放,
 *   对方麦会拾到译文原声,不暂让会把译文再翻译一遍(串译死循环)。
 * - 离开 = 结束我方连接 + hangup 整个 call(两个 interpreter 与对象端一起被踢)。
 *
 * 设备角色不变量(2026-09-12 双麦同源事故收口,判定在 lib/device-roles.ts):4 个角色槽
 * (我方麦/对方麦/我方扬声器/对方扬声器)默认必须落在 4 台不同物理设备上,唯一允许的复用
 * 是同侧耳机(同侧麦+扬声器同台)。跨侧复用=物理回环=垃圾字幕,红色横幅+启动拦截。
 * 判定按**实际**设备(麦克风 getSettings 回读),不按下拉存值——存值可能已失效,而房间
 * 实际在用系统默认(本轮事故:下拉说 HUAWEI、两个身份其实都发布着对方那支 AirPods)。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ConnectionState,
  Room,
  RoomEvent,
  TokenSource,
  Track,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
} from "livekit-client";
import { useSession, useTranscriptions } from "@livekit/components-react";
import { AgentSessionProvider } from "@/components/agents-ui/agent-session-provider";
import { api, apiBase } from "@/lib/api";
import { describeConnectError } from "@/lib/api-ready";
import { wlog, wlogBindCall } from "@/lib/weblog";
import {
  deviceRoleIssues,
  expectedScript,
  scriptMismatch,
  scriptMismatchWarning,
  type RoleSlot,
} from "@/lib/device-roles";
import {
  listAudioDevicesOf,
  requestMicPermission,
  savedMicDevice,
  savedOutputDevice,
  saveMicDevice,
  saveOutputDevice,
  switchWebOutputDevice,
  webCanSwitchOutput,
  type AudioDeviceInfo,
} from "@/lib/audio";

const LANG_SHORT: Record<string, string> = { zh: "中", cantonese: "粤", en: "EN" };

export type ConsoleProps = {
  account: string;
  callId: string;
  myLang: string;
  otherLang: string;
  onExit: () => void;
};

export default function InterpretConsole({ account, callId, myLang, otherLang, onExit }: ConsoleProps) {
  wlogBindCall(callId);
  // 独立双输出(两个 room 各自 setSinkId)仅 Chromium 可用;探测放 effect 避开 SSR。
  // 2026-09-12 用户拍板:支持 setSinkId 的内核**默认双独立输出**(我方输出走对方
  // 原声、对方输出走译文 TTS,各走各的);WKWebView/Safari 等不支持内核保持共享档。
  const [canDual, setCanDual] = useState(false);
  useEffect(() => {
    const can = webCanSwitchOutput();
    setCanDual(can);
    if (can) setOutputMode("dual");
  }, []);
  const [outputMode, setOutputMode] = useState<"shared" | "dual">("shared");
  const outputModeRef = useRef(outputMode);
  useEffect(() => {
    outputModeRef.current = outputMode;
  }, [outputMode]);

  // ---- 设备枚举(两端共用一份列表,各存各的选择) ----
  const [micDevices, setMicDevices] = useState<AudioDeviceInfo[]>([]);
  const [outDevices, setOutDevices] = useState<AudioDeviceInfo[]>([]);
  // meRoom 声明在本函数更下方:deps 数组渲染期求值会 TDZ,枚举器经 ref 取房。
  const meRoomRef = useRef<Room | null>(null);
  const refreshDevices = useCallback(async () => {
    await requestMicPermission().catch(() => false);
    {
      const mics = await listAudioDevicesOf("input");
      setMicDevices(mics);
      const outs = await listAudioDevicesOf("output");
      setOutDevices(outs);
      wlog("devices", {
        // id 前 6 位随名记录:sink_apply 只记 id 前缀,没名字对照就没法定案
        // 「两个 sink 各落在哪台物理设备」(2026-09-12 c199e001 排障缺的最后拼图)。
        mics: mics.map((d) => ({ n: d.name, def: d.is_default, id: d.id.slice(0, 6), g: d.groupId.slice(0, 6) })),
        outs: outs.map((d) => ({ n: d.name, def: d.is_default, id: d.id.slice(0, 6), g: d.groupId.slice(0, 6) })),
        saved: { meMic: savedMicDevice("me"), othMic: savedMicDevice("other"), meOut: savedOutputDevice("me"), othOut: savedOutputDevice("other") },
      });
        // 双麦自动分配(2026-09-12「没有分我的麦克风和对方麦克风」根因):两个下拉
        // 默认「系统默认」=me/other 同抢一支默认麦,两方向收到同一个人。有 ≥2 支
        // 输入就有空缺/撞车时补一台(跳过 default 伪条目,它与显式设备同一物理麦),
        // 持久化;方向装反了用下拉对调。
        //
        // 麦克风「死 id 自愈」(2026-09-12 双麦同源事故,实测 call-b2ff71bb):保存的
        // 麦已不在枚举(耳机拔了/蓝牙重连换 id)时,连接期 switchActiveDevice 静默失败
        // →房间回退系统默认麦,而那一刻系统默认往往正是对方那支 → 两个身份发布同一支
        // 物理麦:两边转写同一段话音、我方扬声器放的就是我自己那支麦(自听回声)、反向
        // 语言钉 en 收到中文。输出档 0912 已有 out_stale_reset,输入侧一直缺这一环,
        // UI 才会「显示 HUAWEI、实际 AirPods」。这里的分配条件也从「两边都从未选过」
        // 放宽到「有空缺或两侧撞同一支」——本轮最危险的组合(一侧死 id + 一侧显式)恰
        // 好不满足旧条件。
        const micStale = (id: string) => Boolean(id) && !mics.some((d) => d.id === id);
        const staleMe = micStale(savedMicDevice("me"));
        const staleOth = micStale(savedMicDevice("other"));
        if (staleMe || staleOth) {
          wlog("mic_stale_reset", {
            who: [staleMe ? "我方" : "", staleOth ? "对方" : ""].filter(Boolean).join("+"),
            me: staleMe ? savedMicDevice("me").slice(0, 12) : null,
            oth: staleOth ? savedMicDevice("other").slice(0, 12) : null,
          });
          if (staleMe) {
            saveMicDevice("", "me");
            setMeMicId("");
          }
          if (staleOth) {
            saveMicDevice("", "other");
            setOthMicId("");
          }
        }
        const realMic = mics.filter((d) => !d.is_default);
        if (realMic.length >= 2) {
          const curMe = staleMe ? "" : savedMicDevice("me");
          const curOth = staleOth ? "" : savedMicDevice("other");
          if (!curMe || !curOth || curMe === curOth) {
            const me = curMe || realMic.find((d) => d.id !== curOth)?.id || realMic[0].id;
            const oth = curOth && curOth !== me ? curOth : realMic.find((d) => d.id !== me)?.id || "";
            if (me && oth && (me !== curMe || oth !== curOth)) {
              const nameOf = (id: string) => realMic.find((d) => d.id === id)?.name ?? mics.find((d) => d.id === id)?.name ?? "";
              wlog("mic_auto_assign", { me: nameOf(me), oth: nameOf(oth) });
              saveMicDevice(me, "me");
              saveMicDevice(oth, "other");
              setMeMicId(me);
              setOthMicId(oth);
              // me 会话可能已在连/已用默认麦:当即切换(已发布轨热切,未发布走
              // audioCaptureDefaults);other 房尚未连接,连接 effect 会读到新值。
              meRoomRef.current?.switchActiveDevice("audioinput", me, true).catch(() => {});
              // other 房可能已连(权限弹窗令枚举晚于连接,review P1):连接期的
              // switchActiveDevice 已跑过,不补切会停留在默认麦而下拉显示已分配。
              otherRoomRef.current?.switchActiveDevice("audioinput", oth, true).catch(() => {});
            }
          }
        }
        // 双扬声器自动分配(2026-09-12「我的扬声器还听到译文 TTS」根因):双输出档
        // 两个下拉默认「系统默认」=两路声音(对方原声+我方译文 TTS)全混进默认输出,
        // 我方耳机两样都放。≥2 台输出且从未选过 → 第一台=我方(放对方原声)、第二台
        // =对方(放译文 TTS);分配后 dual 档的 sink effect 会自动路由,装反了下拉对调。
      if (
        webCanSwitchOutput() && outs.length >= 2 &&
        !savedOutputDevice("me") && !savedOutputDevice("other")
      ) {
        const realOut = outs.filter((d) => !d.is_default);
        if (realOut.length >= 2) {
          wlog("out_auto_assign", { me: realOut[0].name, oth: realOut[1].name });
          saveOutputDevice(realOut[0].id, "me");
          saveOutputDevice(realOut[1].id, "other");
          setMeOutId(realOut[0].id);
          setOthOutId(realOut[1].id);
        }
      }
    }
  }, []);
  useEffect(() => {
    void refreshDevices();
    // 蓝牙/USB 声卡晚接入(2026-09-12「对方扬声器没声,两路混进默认输出」根因之一:
    // 首次枚举看不见晚接入设备,自动分配跳过,两路 sink 全落系统默认):热插拔重枚举
    // + 自动分配重试(选过的不动,只补从未选过的)。
    const h = () => {
      wlog("devicechange");
      void refreshDevices();
    };
    navigator.mediaDevices?.addEventListener?.("devicechange", h);
    return () => navigator.mediaDevices?.removeEventListener?.("devicechange", h);
  }, [refreshDevices]);

  // ---- 官方会话(我方 me)：TokenSource.custom 直连 CP,身份钉 me-<callId> ----
  const tokenMe = useMemo(
    () =>
      TokenSource.custom(async () => {
        return await fetchToken(account, callId, "me");
      }),
    [account, callId],
  );
  const meSession = useSession(tokenMe, { roomName: callId });
  // deps 用稳定的 Room 实例而非 meSession 对象:后者是 useMemo 产物、身份随本地轨
  // publish/mute 变动(半双工每轮暂让都在换),旧 deps [meSession] 令连接 effect 反复
  // 重跑、leave 窗口还会重连污染结算(2026-09-11 审计 P1-3)。
  const meRoom = meSession.room;
  meRoomRef.current = meRoom;
  const otherRoomRef = useRef<Room | null>(null);
  // other 房间重建计数:驱动半双工 watcher 重新挂载(重挂/StrictMode 下 watcher
  // 曾盯着已 disconnect 的死房,othHeld 永不生效,2026-09-11 审计 P1-4)。
  const [otherRoomVersion, setOtherRoomVersion] = useState(0);
  const leavingRef = useRef(false);
  const [leaving, setLeaving] = useState(false);

  const [meConnected, setMeConnected] = useState(false);
  const [otherConnected, setOtherConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);

  // 我方设备(带 me 后缀持久化)
  const [meMicId, setMeMicId] = useState("");
  const [meOutId, setMeOutId] = useState("");
  // 对象设备(带 other 后缀持久化)
  const [othMicId, setOthMicId] = useState("");
  const [othOutId, setOthOutId] = useState("");
  const [meMicOn, setMeMicOn] = useState(true);
  const [othMicOn, setOthMicOn] = useState(true);
  // 各房**实际**在用的麦克风设备（getSettings 回读）。配置值只是意图，switchActiveDevice
  // 失败/设备拔掉时房间会用系统默认，配置与真实就此分叉（2026-09-12 双麦同源事故的
  // 「UI 说我方是 HUAWEI、实际是对方那支」）。真值层单列，UI 与角色冲突判定都读它。
  const [meMicLive, setMeMicLive] = useState("");
  const [othMicLive, setOthMicLive] = useState("");

  useEffect(() => {
    setMeMicId(savedMicDevice("me"));
    setMeOutId(savedOutputDevice("me"));
    setOthMicId(savedMicDevice("other"));
    setOthOutId(savedOutputDevice("other"));
  }, []);
  useEffect(() => {
    meOutIdRef.current = meOutId;
  }, [meOutId]);
  useEffect(() => {
    othOutIdRef.current = othOutId;
  }, [othOutId]);

  // 生效麦克风回读（2s 轮询：设备可被系统热切换，getSettings 无 I/O 开销）。变化才打点，
  // 打点带「意图 vs 实际」，事故复盘一眼看出「配置说 HUAWEI、房间其实在用 AirPods」。
  const micLiveRef = useRef({ me: "", oth: "" });
  useEffect(() => {
    if (!meConnected && !otherConnected) return;
    const read = () => {
      const me = effectiveMicDeviceId(meRoom);
      const oth = effectiveMicDeviceId(otherRoomRef.current);
      if (me && me !== micLiveRef.current.me) {
        micLiveRef.current.me = me;
        wlog("mic_effective", { who: "我方", id: me.slice(0, 12), want: (savedMicDevice("me") || "").slice(0, 12) });
      }
      if (oth && oth !== micLiveRef.current.oth) {
        micLiveRef.current.oth = oth;
        wlog("mic_effective", { who: "对方", id: oth.slice(0, 12), want: (savedMicDevice("other") || "").slice(0, 12) });
      }
      setMeMicLive(me);
      setOthMicLive(oth);
    };
    read();
    const t = window.setInterval(read, 2000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meConnected, otherConnected]);

  // 角色冲突（设备身份层）:4 槽默认必须落在 4 台不同物理设备上，同侧耳机（麦+扬声器同台）
  // 是唯一允许的复用。共享扬声器档两路本来就共用系统默认输出，故不传扬声器槽。
  const roleSlots: RoleSlot[] = useMemo(() => {
    const slots: RoleSlot[] = [];
    // 「系统默认」在这份数据里有两种写法:UI 的空串、以及枚举里 id==="default" 的伪条目
    // （名字「默认 - X」）。两者是**同一台**物理设备,必须归一到同一个身份——否则用户一侧
    // 选「系统默认」、另一侧选伪条目 X 就会绕过冲突判定(审查 2026-09-13 指出的漏网形态)。
    // 归一办法:id 解析不到实体时落到该类型的默认伪条目上,名字里那个「默认 - X」剥离后
    // 与实体 X 同名(groupId 伪条目为空,只能靠名字)。
    const slot = (
      role: RoleSlot["role"],
      label: string,
      id: string,
      devices: AudioDeviceInfo[],
    ): RoleSlot => {
      const hit = devices.find((d) => d.id === id) ?? devices.find((d) => d.is_default);
      return { role, label, name: hit?.name ?? "", id: hit?.id ?? id, groupId: hit?.groupId ?? "" };
    };
    if (micDevices.length > 0) {
      // 优先用回读到的生效设备（真值），回读还没上来时退回配置值。
      slots.push(
        slot("meMic", "我方麦克风", meMicLive || meMicId, micDevices),
        slot("othMic", "对方麦克风", othMicLive || othMicId, micDevices),
      );
    }
    if (outputMode === "dual" && outDevices.length > 0) {
      slots.push(
        slot("meOut", "我方扬声器", meOutId, outDevices),
        slot("othOut", "对方扬声器", othOutId, outDevices),
      );
    }
    return slots;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [micDevices, outDevices, meMicId, othMicId, meOutId, othOutId, meMicLive, othMicLive, outputMode]);
  const roleIssues = useMemo(() => deviceRoleIssues(roleSlots), [roleSlots]);
  const roleIssuesRef = useRef(roleIssues);
  const roleFatalSigRef = useRef("");
  useEffect(() => {
    roleIssuesRef.current = roleIssues;
    // 只在冲突内容变化时打点:devicechange 会连续重枚举(dev 下还会双跑 effect),
    // 每次重算都打会把日志淹掉(同 held 的教训)。
    const sig = roleIssues.fatal.join("|");
    if (sig && sig !== roleFatalSigRef.current) {
      roleFatalSigRef.current = sig;
      wlog("role_conflict", { fatal: roleIssues.fatal });
    }
  }, [roleIssues]);

  // ---- 自动半双工(防串译,默认开) ----
  // meHeld   = 对方译文(trans-<我方语言>,rev)出声中 → 我方麦暂让;
  // othHeld  = 我方译文(trans-<对方语言>,fwd)经共享扬声器外放中 → 对方麦暂让
  //            (对方麦拾到英文译文再翻一遍就是串译死循环)。
  const [halfDuplex, setHalfDuplex] = useState(true);
  const [meHeld, setMeHeld] = useState(false);
  const [othHeld, setOthHeld] = useState(false);
  // 声源仲裁让麦(2026-09-12 对方耳机听到自己话被译一遍的根因=同桌物理串音:
  // 对方的声音漏进我方麦,fwd 把它也翻译播出)。谁的原文转写流活跃,对面的麦
  // 就暂让——由字幕流驱动(半双工是 TTS 播放驱动,另一维度);双方同时活跃
  // (真插话)不拦。
  const [voiceHoldMe, setVoiceHoldMe] = useState(false);
  const [voiceHoldOth, setVoiceHoldOth] = useState(false);
  const logHold = useCallback((who: "me" | "oth") => {
    // 只打状态翻转:watchTransAudio 每 120ms 回调,旧版每个 true tick 都打一条,
    // 单会话 500+ 行噪音把 sink_apply 等关键事件淹没(2026-09-12 日志实证)。
    // 且旧版只打日志不落 state——meHeld/othHeld 恒 false,半双工暂让是死代码,
    // 这里把 setter 接回来(注释块宣称的行为这才真正生效)。
    let last: boolean | null = null;
    return (v: boolean) => {
      if (v !== last) {
        last = v;
        wlog("held", { who, on: v });
      }
      (who === "me" ? setMeHeld : setOthHeld)(v);
    };
  }, []);
  const onVoiceActivity = useCallback((meActive: boolean, othActive: boolean) => {
    setVoiceHoldMe(othActive && !meActive);
    setVoiceHoldOth(meActive && !othActive);
  }, []);

  // ---- 输出路由应用(2026-09-12 双输出「译文全进我方扬声器」排查后统一入口) ----
  // 实证:room 级 switchActiveDevice 的 ok:true 只代表「没抛异常」——元素级
  // setSinkId 失败被 livekit 吞进浏览器 console,wlog/UI 全盲;「没 id」更是静默
  // return 连日志都没有。连接时/选择器/重投 effect 全走这里,无静默路径。
  const applyDualOutput = useCallback(
    async (
      room: { switchActiveDevice: (kind: string, id: string, exact?: boolean) => Promise<boolean> } | null,
      id: string,
      who: string,
    ) => {
      if (!room || !id) {
        wlog("sink_apply", { who, id: id ? id.slice(0, 12) : null, ok: false, reason: !room ? "no_room" : "no_id" });
        return;
      }
      const ok = await switchWebOutputDevice(room, id).catch(() => false);
      wlog("sink_apply", { who, id: id.slice(0, 12), ok });
      if (!ok) setError(`无法把${who}的译文路由到所选扬声器（setSinkId 失败）——请换一台输出设备或改用共享扬声器。`);
    },
    [],
  );
  // 远端放音路由(2026-09-12 终版,crbug 40647375):element.setSinkId 对 WebRTC
  // 远端流静默失效——读回 match:true 但声音仍走默认输出,这就是「只有一个扬声器
  // 响」的真身;本机 Chrome 实验同时证明 AudioContext.setSinkId 物理路由可用
  // (ctx.sinkId 会被 Chrome 解析成真实设备 id 并真正送声)。全部远端放音改走
  // AudioContext:轨 → createMediaStreamSource → ctx.destination,输出设备用
  // ctx.setSinkId(Chrome 110+)。
  const routersRef = useRef<{ me: ReturnType<typeof createAudioRouter> | null; oth: ReturnType<typeof createAudioRouter> | null }>({ me: null, oth: null });
  const getRouter = useCallback((who: "me" | "oth") => {
    if (!routersRef.current[who]) routersRef.current[who] = createAudioRouter(who);
    return routersRef.current[who]!;
  }, []);
  // AudioContext 受自动播放策略管:手势前 suspended,任意点击唤醒两路放音。
  useEffect(() => {
    const wake = () => {
      routersRef.current.me?.resume();
      routersRef.current.oth?.resume();
    };
    document.addEventListener("click", wake);
    return () => document.removeEventListener("click", wake);
  }, []);
  useEffect(
    () => () => {
      routersRef.current.me?.dispose();
      routersRef.current.oth?.dispose();
    },
    [],
  );
  // 输出 id 的 ref 快照:连接期注册的 TrackSubscribed handler 只能读 ref 拿「当前」输出。
  const meOutIdRef = useRef("");
  const othOutIdRef = useRef("");

  // ---- 传译总开关(2026-09-12 用户拍板:进房不自动开始) ----
  // 停止 = 两端麦克风全部静默:无音频→无 ASR→无翻译,原文/译文字幕一并暂停,
  // 在播的译文念完即止;启动 = 恢复采集。与半双工暂让共用同一套麦克风生效
  // effect(生效值 = 人工开关 && !自动暂让 && 传译开)。
  const [interpOn, setInterpOn] = useState(false);
  const interpOnRef = useRef(interpOn);
  useEffect(() => {
    interpOnRef.current = interpOn;
  }, [interpOn]);
  const toggleInterp = useCallback(() => {
    // 结构性坏配置不放行（2026-09-12 双麦同源事故）：两侧同一支麦/跨侧复用设备时开传译
    // 只会产出「两边转写同一段话音 + 自听回声 + 语种全错」的垃圾字幕，先把原因摆在面前，
    // 而不是让操作员自己听出来。
    if (!interpOnRef.current && roleIssuesRef.current.fatal.length) {
      setError(roleIssuesRef.current.fatal[0]);
      return;
    }
    setInterpOn((v) => {
      wlog("interp_toggle", { on: !v });
      return !v;
    });
  }, []);

  // ---- 我方连接(照抄 interpret 页/CallStudio 已验证路径) ----
  useEffect(() => {
    if (meRoom.state === ConnectionState.Connected || meRoom.state === ConnectionState.Connecting) return;
    let cancelled = false;
    (async () => {
      setBusy(true);
      try {
        // 连接 effect 可能早于设备恢复 state,直接用已存值兜底。
        const micId = meMicId || savedMicDevice("me");
        const outId = meOutId || savedOutputDevice("me");
        // 结果落日志 + **硬约束(exact=true)**:第三参传 false 是软约束,设备不在场时
        // Chrome 会另挑一支(=系统默认)而不报错,两个房间便双双落到同一支默认麦上——
        // 这正是本轮「两侧同一支麦」的机关:存值失效 + 软约束静默换麦 + 未发布时
        // success 恒 true,三层都不出声。改硬约束后「设备没了」变成一次可见的
        // OverconstrainedError,由下面的错误分支让操作员重选,而不是偷偷换一支。
        if (micId) {
          const ok = await meRoom.switchActiveDevice("audioinput", micId, true).catch(() => false);
          wlog("mic_apply", { who: "我方", id: micId.slice(0, 12), ok });
        }
        // 麦克风采集放进 session.start 的 tracks(与 token/连房并行,CallStudio 同款)。
        // 绝不能先在未连接的房间上 await setMicrophoneEnabled——发布等连接、连接又
        // 等这行返回,互等死到 livekit 内部 ~15s 超时,且超时会 track.stop() 杀掉
        // 已采集的麦克风轨、错误被吞,房间照常连接 = 「已接入」假象 + fwd 收不到
        // 任何音频(2026-09-11 同传审计:09-10「fwd 进房 6 分钟零译文」根因)。
        await meSession.start({
          // 传译总开关(默认关):进房只连接不采麦,按「启动传译」才开始——已连接
          // 房间上后开采集无 15s 死锁风险(那死锁只发生在未连接房间上 await)。
          tracks: { microphone: { enabled: interpOnRef.current, publishOptions: { preConnectBuffer: true } } },
        });
        // 确保我方麦克风真正发布:失败(权限被拒/设备被占)显式报错并把开关拉回
        // 现实,不再静默装「已接入」。传译未启动时跳过探活(探活会把麦打开)。
        if (interpOnRef.current) {
          try {
            const pub = await meRoom.localParticipant.setMicrophoneEnabled(true);
            setMeMicOn(Boolean(pub));
            if (!pub) setError("无法开启我方麦克风：请检查浏览器麦克风权限——已连接,但同传听不到我方说话。");
          } catch {
            setMeMicOn(false);
            setError("无法开启我方麦克风：请检查浏览器麦克风权限——已连接,但同传听不到我方说话。");
          }
        }
        // 共享扬声器(默认)不碰输出路由;独立双输出才 setSinkId(统一走
        // applyDualOutput——没 id 也落日志,不再静默走系统默认)。
        if (outputModeRef.current === "dual") {
          await applyDualOutput(meRoom, outId, "我方");
        }
      } catch (e) {
        // session.start 内部 token/连房与麦克风并行:麦克风失败时房间可能仍连上。
        const raw = e instanceof Error ? e.message : String(e ?? "");
        // overconstrained/请求设备不存在 = 存下来的那支麦已不在场（拔了/蓝牙换 id），
        // 报「麦克风」比报「连接失败」贴切——同时把存值清掉，下次按在场设备重选。
        if (/notallowed|permission|notreadable|track invalid|device in use|overconstrained|requested device not found/i.test(raw)) {
          if (!cancelled) {
            setMeMicOn(false);
            setError(
              /overconstrained|requested device not found/i.test(raw)
                ? "我方麦克风已不在设备列表（可能拔了/蓝牙重连换了 id）——请在「声音设备」里重新指定一支。"
                : "无法开启我方麦克风：请检查浏览器麦克风权限——已连接,但同传听不到我方说话。",
            );
          }
        } else if (!cancelled) setError(describeConnectError(e, "join-session"));
      } finally {
        // 无条件复位:cancelled(权限拒绝/依赖重挂载)路径若不复位,结束按钮
        // 会永久锁死(2026-09-09 QA B5 实测);busy 只表达「连接尝试进行中」。
        setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meRoom]);

  // meConnected 由房间状态驱动,不押在 start() resolve 上——其末尾「等 agent 就绪」
  // 无超时、agent 慢/异常时永久挂起,旧写法 meConnected/时钟/busy 全部死在 await 后
  // (2026-09-11 审计 P0:一体台恒显「连接中」的直接根因)。
  useEffect(() => {
    if (meRoom.state !== ConnectionState.Connected) return;
    wlog("me_connected");
    setMeConnected(true);
    setStartedAt((prev) => prev ?? Date.now());
    setBusy(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meRoom, meRoom.state]);

  // 我方侧「对方原声」放音(2026-09-12 终版):对方麦轨订阅即接入 me 路放音
  // AudioContext(输出设备由 ctx_sink effect 管理)。监听挂载晚于订阅时补扫在房轨。
  useEffect(() => {
    if (!meConnected) return;
    const want = (track: RemoteTrack, participant: RemoteParticipant) =>
      track.kind === "audio" && participant.identity === `other-${callId}`;
    const onSub = (track: RemoteTrack, _pub: RemoteTrackPublication, participant: RemoteParticipant) => {
      if (!want(track, participant)) return;
      wlog("orig_elem", { route: "ctx", out: (meOutIdRef.current || savedOutputDevice("me") || "default").slice(0, 12) });
      getRouter("me").add(track.sid, track.mediaStreamTrack);
    };
    const onUnsub = (track: RemoteTrack) => getRouter("me").remove(track.sid);
    meRoom.on(RoomEvent.TrackSubscribed, onSub);
    meRoom.on(RoomEvent.TrackUnsubscribed, onUnsub);
    for (const p of meRoom.remoteParticipants.values()) {
      for (const pub of p.trackPublications.values()) {
        if (pub.track && want(pub.track as RemoteTrack, p)) getRouter("me").add(pub.track.sid, pub.track.mediaStreamTrack);
      }
    }
    return () => {
      meRoom.off(RoomEvent.TrackSubscribed, onSub);
      meRoom.off(RoomEvent.TrackUnsubscribed, onUnsub);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meRoom, meConnected, callId]);

  // ---- 对象连接(纯手动 Room:麦克风收音 + 译文放音) ----
  useEffect(() => {
    // 顺序契约(2026-09-12 一体台「没翻译」根因):RoomAgentDispatch 只在首个
    // 参与者建房时生效且只挂 me 端 token——两端同时起跑时 other(无 dispatch)
    // 抢先建房,me 的 dispatch 永不激活=同传 AI 拉不起来。other 必须等 me 连上
    // (房间已由 me 建好)再进。
    if (!meConnected) return;
    let cancelled = false;
    let room: Room | null = null;
    (async () => {
      try {
        const tok = await fetchToken(account, callId, "other");
        // fetch 挂起期间 cleanup 已跑:直接放弃。旧写法在此之后才查 cancelled,
        // 会把 room 完整连上后弃管(无 disconnect),靠同身份重连互踢才收敛。
        if (cancelled) return;
        room = new Room();
        otherRoomRef.current = room;
        setOtherRoomVersion((v) => v + 1);
        // 译文放音(2026-09-12 终版,crbug 40647375):不再 attach <audio> 元素——
        // element.setSinkId 对 WebRTC 远端流静默失效(读回 match:true 但声音走默认
        // 输出)。trans-* 轨接入 oth 路放音 AudioContext,输出设备由 ctx.setSinkId
        // 管理(见 ctx_sink effect)。仍只路由 trans-* 译文轨:把 me-<room> 的麦克
        // 风轨也放出来=客户听到自己原声+译文双声。
        room.on(RoomEvent.TrackSubscribed, (track, pub) => {
          if (track.kind !== "audio" || !String(pub.trackName ?? "").startsWith("trans-")) return;
          wlog("trans_elem", { route: "ctx", out: (othOutIdRef.current || savedOutputDevice("other") || "default").slice(0, 12) });
          getRouter("oth").add(track.sid, track.mediaStreamTrack);
        });
        room.on(RoomEvent.TrackUnsubscribed, (track) => {
          getRouter("oth").remove(track.sid);
        });
        // 连接 effect 可能早于设备恢复 state,直接用已存值兜底。
        const micId = othMicId || savedMicDevice("other");
        const outId = othOutId || savedOutputDevice("other");
        // 结果落日志（同我方侧:未发布恒 true，真值看回读）。
        if (micId) {
          const ok = await room.switchActiveDevice("audioinput", micId, true).catch(() => false);
          wlog("mic_apply", { who: "对方", id: micId.slice(0, 12), ok });
        }
        await room.connect(tok.serverUrl, tok.participantToken);
        if (cancelled) {
          room.disconnect().catch(() => {});
          return;
        }
        await room.localParticipant.setMicrophoneEnabled(othMicOn && interpOnRef.current);
        // 共享扬声器(默认)不碰输出路由;独立双输出才 setSinkId(统一走
        // applyDualOutput——没 id 也落日志,不再静默走系统默认)。
        if (outputModeRef.current === "dual") {
          await applyDualOutput(room, outId, "对方");
        }
        wlog("other_connected");
        setOtherConnected(true);
        setOtherRoomVersion((v) => v + 1);
        await room.startAudio().catch(() => {});
      } catch (e) {
        if (!cancelled) {
          const raw = e instanceof Error ? e.message : String(e ?? "");
          setError(
            /overconstrained|requested device not found/i.test(raw)
              ? "对方麦克风已不在设备列表（可能拔了/蓝牙重连换了 id）——请在「声音设备」里重新指定一支。"
              : describeConnectError(e, "join-session"),
          );
        }
      }
    })();
    return () => {
      cancelled = true;
      const r = otherRoomRef.current;
      otherRoomRef.current = null;
      if (r) r.disconnect().catch(() => {});
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callId, meConnected]);

  // ---- 设备选择应用(applyDualOutput 已上移到输出路由统一入口处) ----
  // 麦克风热切换(2026-09-12 call-ae8fece8 实证):switchActiveDevice 内部会重启采集
  // (getUserMedia 换设备),失败(设备被另一会话占用/macOS 蓝牙 HFP 双开必败)时旧轨
  // 已停、新轨没起来 = 房里挂着一条「活着但全静音」的死轨,rev 方向从此收不到声,
  // 且旧代码 .catch(()=>{}) 静默吞掉。失败必须可见+回滚救活:切回旧设备并做一次
  // 关-开重采集,仍失败亮错误条让操作员换设备。
  const applyMicSwitch = useCallback(
    async (
      room: Room | null,
      id: string,
      prevId: string,
      who: string,
      revert: () => void,
    ) => {
      if (!room) return;
      const ok = await room.switchActiveDevice("audioinput", id, true).catch(() => false);
      wlog("mic_switch", { who, id: id.slice(0, 12), prev: prevId.slice(0, 12), ok });
      if (ok) return;
      console.warn(`mic hot-switch failed: ${who} -> ${id}, rolling back to ${prevId || "default"}`);
      try {
        if (prevId) await room.switchActiveDevice("audioinput", prevId, true).catch(() => {});
        await room.localParticipant.setMicrophoneEnabled(false).catch(() => {});
        await room.localParticipant.setMicrophoneEnabled(true).catch(() => {});
      } catch {
        /* 回滚尽力而为 */
      }
      revert();
      setError(
        `切换${who}麦克风失败（设备被占用或不支持同时双开，如蓝牙耳机麦）——已切回原设备；请给${who}换一支独立麦克风。`,
      );
    },
    [],
  );
  const pickMeMic = useCallback(
    (id: string) => {
      const prev = meMicId;
      setMeMicId(id);
      saveMicDevice(id, "me");
      if (meConnected) void applyMicSwitch(meRoom, id, prev, "我方", () => {
        setMeMicId(prev);
        saveMicDevice(prev, "me");
      });
    },
    [meSession, meConnected, meMicId, applyMicSwitch],
  );
  const pickMeOut = useCallback(
    (id: string) => {
      setMeOutId(id);
      saveOutputDevice(id, "me");
      if (outputModeRef.current === "dual" && meConnected) void applyDualOutput(meRoom, id, "我方");
    },
    [meSession, meConnected, applyDualOutput],
  );
  const pickOthMic = useCallback(
    (id: string) => {
      const prev = othMicId;
      setOthMicId(id);
      saveMicDevice(id, "other");
      const r = otherRoomRef.current;
      if (r) void applyMicSwitch(r, id, prev, "对方", () => {
        setOthMicId(prev);
        saveMicDevice(prev, "other");
      });
    },
    [othMicId, applyMicSwitch],
  );
  const pickOthOut = useCallback((id: string) => {
    setOthOutId(id);
    saveOutputDevice(id, "other");
    const r = otherRoomRef.current;
    if (outputModeRef.current === "dual" && r) void applyDualOutput(r, id, "对方");
  }, [applyDualOutput]);

  // ---- 麦克风开关:按钮只改人工意图,生效值由本 effect 统一投到房间 ----
  // 生效值 = 人工开关 && !自动暂让——暂让结束后按人工意图恢复,人工静音始终优先。
  const toggleMeMic = useCallback(() => setMeMicOn((v) => !v), []);
  const toggleOthMic = useCallback(() => setOthMicOn((v) => !v), []);
  useEffect(() => {
    if (!meConnected) return;
    meRoom.localParticipant.setMicrophoneEnabled(meMicOn && !meHeld && !voiceHoldMe && interpOn).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meMicOn, meHeld, voiceHoldMe, meConnected, interpOn]);
  useEffect(() => {
    const r = otherRoomRef.current;
    if (!otherConnected || !r) return;
    r.localParticipant.setMicrophoneEnabled(othMicOn && !othHeld && !voiceHoldOth && interpOn).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [othMicOn, othHeld, voiceHoldOth, otherConnected, interpOn]);

  // ---- 输入电平(「为什么没有输入电平」) ----
  // 采集轨是本地轨,直接进 WebAudio 读 RMS。只在「我们要求这支麦打开」时才判静音,
  // 免得把「手动静音/半双工暂让」误报成没信号;连续 5s < 0.002 才亮警告(蓝牙麦被别的
  // 程序占用时表现为数字静音且不报错,没有这条操作员只能靠猜)。
  const metersRef = useRef<{ me: ReturnType<typeof createMicMeter> | null; oth: ReturnType<typeof createMicMeter> | null }>({ me: null, oth: null });
  const getMeter = useCallback((who: "me" | "oth") => {
    if (!metersRef.current[who]) metersRef.current[who] = createMicMeter();
    return metersRef.current[who]!;
  }, []);
  useEffect(
    () => () => {
      metersRef.current.me?.dispose();
      metersRef.current.oth?.dispose();
    },
    [],
  );
  const [micLevels, setMicLevels] = useState({ me: -1, oth: -1 });
  const [micSilent, setMicSilent] = useState({ me: false, oth: false });
  const silentSinceRef = useRef({ me: 0, oth: 0 });
  // tick 读的是最新值,state 快照经 ref 同步(避免把 effect 挂在每次暂让上重挂)。
  const meMicOnRef = useRef(meMicOn);
  const othMicOnRef = useRef(othMicOn);
  const heldRef = useRef({ me: meHeld, oth: othHeld });
  const voiceHoldRef = useRef({ me: voiceHoldMe, oth: voiceHoldOth });
  useEffect(() => {
    meMicOnRef.current = meMicOn;
    othMicOnRef.current = othMicOn;
    heldRef.current = { me: meHeld, oth: othHeld };
    voiceHoldRef.current = { me: voiceHoldMe, oth: voiceHoldOth };
  }, [meMicOn, othMicOn, meHeld, othHeld, voiceHoldMe, voiceHoldOth]);
  useEffect(() => {
    if (!meConnected && !otherConnected) return;
    const tick = () => {
      const read = (room: Room | null, requested: boolean, who: "me" | "oth") => {
        const tr = room?.localParticipant.getTrackPublication(Track.Source.Microphone)?.track?.mediaStreamTrack;
        const lvl = requested ? getMeter(who).level(tr) : -1;
        const s = silentSinceRef.current;
        if (lvl >= 0 && lvl < 0.002) {
          if (!s[who]) s[who] = Date.now();
        } else {
          s[who] = 0;
        }
        return { lvl, silent: Boolean(s[who]) && Date.now() - s[who] > 5000 };
      };
      const meOn = interpOnRef.current && meMicOnRef.current && !heldRef.current.me && !voiceHoldRef.current.me;
      const othOn = interpOnRef.current && othMicOnRef.current && !heldRef.current.oth && !voiceHoldRef.current.oth;
      const a = read(meRoom, meOn, "me");
      const b = read(otherRoomRef.current, othOn, "oth");
      setMicLevels((prev) => (prev.me === a.lvl && prev.oth === b.lvl ? prev : { me: a.lvl, oth: b.lvl }));
      setMicSilent((prev) => (prev.me === a.silent && prev.oth === b.silent ? prev : { me: a.silent, oth: b.silent }));
    };
    tick();
    const t = window.setInterval(tick, 250);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meConnected, otherConnected, getMeter]);

  // ---- 输出模式切换:对已连接房间重投路由 ----
  // dual 模式不变量(2026-09-12 根因收口):两路必须都有指认,有洞必补。根因链=
  // 用户早前手动选过我方扬声器(持久化)→自动分配的「双空才跑」条件被挡→对方路
  // 恒空=走系统默认→默认恰是我方那台→两路(原声+译文)全混同一设备,对方音箱静默。
  // 补洞无视 localStorage 空值历史(显式非空选择不动);只在有 ≥2 台真实输出时补。
  useEffect(() => {
    if (outputMode !== "dual" || !canDual) return;
    const real = outDevices.filter((d) => !d.is_default);
    if (real.length < 2) return;
    // 死 id 自愈(2026-09-12 终审判决落地):蓝牙设备断开重连后 Chrome 侧输出
    // deviceId 过期——存的选择不在当前枚举=死设备,setSinkId 抛 NotFoundError、
    // 译文落默认输出(实测 AirPods b7f9c1 反复踩中)。重置为空交给下方补洞逻辑
    // 换一台在场真设备,不再抱着死 id 硬路由。
    const meStale = Boolean(meOutId) && !outDevices.some((d) => d.id === meOutId);
    const othStale = Boolean(othOutId) && !outDevices.some((d) => d.id === othOutId);
    if (meStale || othStale) {
      wlog("out_stale_reset", { me: meStale ? meOutId.slice(0, 12) : null, oth: othStale ? othOutId.slice(0, 12) : null });
      if (meStale) {
        setMeOutId("");
        saveOutputDevice("", "me");
      }
      if (othStale) {
        setOthOutId("");
        saveOutputDevice("", "other");
      }
      return; // 本轮只重置,下一轮 effect 走补洞
    }
    const meEmpty = !meOutId, othEmpty = !othOutId;
    if (!meEmpty && !othEmpty) return;
    wlog("out_fill", { meEmpty, othEmpty });
    if (meEmpty && othEmpty) {
      wlog("out_fill_both", { me: real[0].name, oth: real[1].name });
      saveOutputDevice(real[0].id, "me");
      saveOutputDevice(real[1].id, "other");
      setMeOutId(real[0].id);
      setOthOutId(real[1].id);
      return;
    }
    if (meEmpty) {
      const cand = real.find((d) => d.id !== othOutId);
      if (cand) {
        wlog("out_fill_me", { picked: cand.name });
        saveOutputDevice(cand.id, "me");
        setMeOutId(cand.id);
      }
    } else {
      const cand = real.find((d) => d.id !== meOutId);
      if (cand) {
        wlog("out_fill_oth", { picked: cand.name });
        saveOutputDevice(cand.id, "other");
        setOthOutId(cand.id);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, canDual, outDevices, meOutId, othOutId]);

  useEffect(() => {
    if (outputMode !== "dual" || !canDual || !meConnected) return;
    const id = meOutId || savedOutputDevice("me");
    if (id) void applyDualOutput(meRoom, id, "我方");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, meOutId, meConnected, canDual]);
  useEffect(() => {
    const r = otherRoomRef.current;
    if (outputMode !== "dual" || !canDual || !otherConnected || !r) return;
    const id = othOutId || savedOutputDevice("other");
    if (id) void applyDualOutput(r, id, "对方");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, othOutId, otherConnected, canDual]);
  // 放音上下文输出路由(ctx_sink)——这是远端音频「真实」的输出去向:双输出两路
  // 各自钉设备,共享档回默认。元素 sink 已被 crbug 40647375 废掉,ctx.setSinkId
  // 才是有效指令;输出热切换/补洞/死 id 自愈后 deps 变化自动重投。
  useEffect(() => {
    if (outputMode !== "dual" || !canDual || !meConnected) return;
    const id = meOutId || savedOutputDevice("me");
    void getRouter("me").setSink(id || "default");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, meOutId, meConnected, canDual]);
  useEffect(() => {
    if (outputMode !== "dual" || !canDual || !otherConnected) return;
    const id = othOutId || savedOutputDevice("other");
    void getRouter("oth").setSink(id || "default");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, othOutId, otherConnected, canDual]);
  useEffect(() => {
    if (outputMode !== "shared" || !canDual) return;
    void getRouter("me").setSink("default");
    void getRouter("oth").setSink("default");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, meConnected, otherConnected, canDual]);
  // 切回共享扬声器:显式回系统默认输出(sinkId="default"),避免残留上一档路由。
  useEffect(() => {
    if (outputMode !== "shared" || !canDual) return;
    if (meConnected) meRoom.switchActiveDevice("audiooutput", "default", false).catch(() => {});
    const r = otherRoomRef.current;
    if (otherConnected && r) r.switchActiveDevice("audiooutput", "default", false).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputMode, meConnected, otherConnected, canDual]);

  // ---- 自动半双工:监听两个房间各自收到的 trans-* 译文轨出声 ----
  useEffect(() => {
    if (!halfDuplex) {
      setMeHeld(false);
      setOthHeld(false);
      return;
    }
    const stopMe = watchTransAudio(meRoom, logHold("me"));
    const stopOth = watchTransAudio(otherRoomRef.current, logHold("oth"));
    return () => {
      stopMe();
      stopOth();
    };
    // otherRoomVersion:other 房间(重)建后重挂 watcher,防盯死房。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [halfDuplex, meConnected, otherConnected, otherRoomVersion]);

  // 同设备告警（2026-09-12 收口到 device-roles 单一判定）：同侧麦+扬声器同台合法（一副
  // 耳机），但蓝牙双开有 HFP 无声风险 → warn；任何跨侧复用都是物理回环 → fatal（另出
  // 红色横幅并在「启动传译」处拦下），不再只靠 id 相等这种表面比较。
  const sameDeviceWarning = roleIssues.warn.join(" ");

  // 渲染器层不再放声(2026-09-12 终版,crbug 40647375):element.setSinkId 对
  // WebRTC 远端流静默失效,AgentSessionProvider 只留会话语义;对方原声由 me 路
  // 放音 AudioContext 承担(见 orig_elem effect),译文由 oth 路承担(trans_elem)。
  return (
    <AgentSessionProvider session={meSession} disableAudio>
      <ConsoleLive
        room={meRoom}
        callId={callId}
        myLang={myLang}
        otherLang={otherLang}
        meConnected={meConnected}
        otherConnected={otherConnected}
        error={error}
        setError={setError}
        busy={busy}
        leaving={leaving}
        micDevices={micDevices}
        outDevices={outDevices}
        meMicId={meMicId}
        meOutId={meOutId}
        othMicId={othMicId}
        othOutId={othOutId}
        meMicOn={meMicOn}
        othMicOn={othMicOn}
        meHeld={meHeld}
        othHeld={othHeld}
        halfDuplex={halfDuplex}
        outputMode={outputMode}
        canDual={canDual}
        startedAt={startedAt}
        sameDeviceWarning={sameDeviceWarning}
        roleFatal={roleIssues.fatal}
        meMicLive={meMicLive}
        othMicLive={othMicLive}
        micLevels={micLevels}
        micSilent={micSilent}
        pickMeMic={pickMeMic}
        pickMeOut={pickMeOut}
        pickOthMic={pickOthMic}
        pickOthOut={pickOthOut}
        interpOn={interpOn}
        toggleInterp={toggleInterp}
        onVoiceActivity={onVoiceActivity}
        toggleMeMic={toggleMeMic}
        toggleOthMic={toggleOthMic}
        setHalfDuplex={setHalfDuplex}
        setOutputMode={setOutputMode}
        leave={leave}
      />
    </AgentSessionProvider>
  );

  async function leave() {
    // 防抖:CP 断房后台化之前,hangup 慢时连点曾打出 7 连发(2026-09-10 实证);
    // onExit 收进 finally——任何一步挂起/抛错都放行退出,不再把用户锁在页面里。
    if (leavingRef.current) return;
    leavingRef.current = true;
    setLeaving(true);
    try {
      try {
        await Promise.race([meSession.end(), new Promise((r) => setTimeout(r, 5000))]);
      } catch {
        /* ignore */
      }
      const r = otherRoomRef.current;
      otherRoomRef.current = null;
      if (r) await Promise.race([r.disconnect(), new Promise((res) => setTimeout(res, 3000))]).catch(() => {});
      await api.hangup(callId).catch((e) => {
        if (!String(e).includes("404")) console.warn("hangup failed", e);
      });
    } finally {
      leavingRef.current = false;
      setLeaving(false);
      onExit();
    }
  }
}

type LiveProps = {
  room: Room;
  callId: string;
  myLang: string;
  otherLang: string;
  meConnected: boolean;
  otherConnected: boolean;
  error: string | null;
  busy: boolean;
  leaving: boolean;
  micDevices: AudioDeviceInfo[];
  outDevices: AudioDeviceInfo[];
  meMicId: string;
  meOutId: string;
  othMicId: string;
  othOutId: string;
  setError: (s: string) => void;
  onVoiceActivity: (meActive: boolean, othActive: boolean) => void;
  interpOn: boolean;
  toggleInterp: () => void;
  meMicOn: boolean;
  othMicOn: boolean;
  meHeld: boolean;
  othHeld: boolean;
  halfDuplex: boolean;
  outputMode: "shared" | "dual";
  canDual: boolean;
  startedAt: number | null;
  sameDeviceWarning: string;
  /** 结构性坏配置（两侧同一支麦/跨侧复用设备）：红色横幅 + 传译启动拦截。 */
  roleFatal: string[];
  /** 各房**实际**在用的麦克风设备 id（getSettings 回读），配置分叉时点名。 */
  meMicLive: string;
  othMicLive: string;
  /** 输入电平 RMS（负值 = 未开麦），以及「连续 5s 无电平」判定。 */
  micLevels: { me: number; oth: number };
  micSilent: { me: boolean; oth: boolean };
  pickMeMic: (id: string) => void;
  pickMeOut: (id: string) => void;
  pickOthMic: (id: string) => void;
  pickOthOut: (id: string) => void;
  toggleMeMic: () => void;
  toggleOthMic: () => void;
  setHalfDuplex: (v: boolean) => void;
  setOutputMode: (v: "shared" | "dual") => void;
  leave: () => void;
};

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-(--card-border) bg-white/5 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-(--stage-muted)">{label}</div>
      <div className="mt-0.5 flex items-center font-mono text-[12px]">{children}</div>
    </div>
  );
}

function Dot({ on }: { on: boolean }) {
  return <span className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full ${on ? "bg-emerald-400" : "bg-neutral-500"}`} />;
}

function SessionClock({ startedAt }: { startedAt: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt == null) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [startedAt]);
  if (startedAt == null) return <span>—</span>;
  const s = Math.max(0, Math.floor((now - startedAt) / 1000));
  const hh = String(Math.floor(s / 3600)).padStart(2, "0");
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return (
    <span>
      {hh}:{mm}:{ss}
    </span>
  );
}

function ConsoleLive(p: LiveProps) {
  const transcriptions = useTranscriptions();
  // 🔊 试听(2026-09-12):在指定输出设备上放一句语音——自动分配是按枚举顺序猜的,
  // 「我方扬声器出译文/对方扬声器没声」九成是两路方向猜反或设备不在列表;试听令
  // 物理指认 5 秒锁定,选完下拉即记住。走本地 TTS sidecar 预览端点,零云端开销。
  const playSinkTest = useCallback(async (deviceId: string, label: string) => {
    try {
      const r = await fetch(`${apiBase()}/api/tts/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: "qwen3_tts", voice: "Vivian", language: "zh", text: label }),
      });
      if (!r.ok) throw new Error(`preview ${r.status}`);
      const el = new Audio(URL.createObjectURL(await r.blob()));
      // 试听硬化(2026-09-12):setSinkId 结果必须落 wlog 且失败亮错误——旧版 catch
      // 吞掉,「点了没声」无线索(实测 3 秒连点 5 次对方输出的重试形态)。
      const sinkEl = el as HTMLAudioElement & { setSinkId?: (id: string) => Promise<void>; sinkId?: string };
      if (!sinkEl.setSinkId) {
        wlog("sink_test", { label, id: deviceId.slice(0, 12), ok: true, note: "no_setSinkId" });
      } else {
        try {
          await sinkEl.setSinkId(deviceId || "default");
          wlog("sink_test", { label, id: deviceId.slice(0, 12), ok: true, got: String(sinkEl.sinkId ?? "").slice(0, 12) });
        } catch (e) {
          const err = e instanceof Error ? e.name : String(e);
          wlog("sink_test", { label, id: deviceId.slice(0, 12), ok: false, err });
          p.setError(`「${label}」路由失败（${err}）——该输出设备可能被占用或已不可用，请换一台。`);
          return;
        }
      }
      await el.play();
      el.addEventListener("ended", () => URL.revokeObjectURL(el.src));
    } catch (e) {
      p.setError(`试听失败(${e instanceof Error ? e.message : String(e)})——请确认本地 TTS 服务在跑。`);
    }
  }, [p.setError]);
  const listRef = useRef<HTMLDivElement | null>(null);
  const [filter, setFilter] = useState<"both" | "me" | "other">("both");
  const [clearedCount, setClearedCount] = useState(0);
  const items = useMemo(() => transcriptions.slice(-80), [transcriptions]);
  // 声源仲裁:两侧「原文转写流」的最近更新时刻=谁在说话;400ms 轮询衰减
  // (1.5s 窗)。AGT 译文(meHeld/othHeld 的 TTS 暂让已覆盖)不算说话。
  const lastSpokeRef = useRef<{ me: number; oth: number }>({ me: 0, oth: 0 });
  useEffect(() => {
    const now = Date.now();
    for (const tr of transcriptions) {
      const id = String(tr.participantInfo?.identity ?? "");
      if (id.startsWith("me-")) lastSpokeRef.current.me = now;
      else if (id.startsWith("other-")) lastSpokeRef.current.oth = now;
    }
  }, [transcriptions]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      const now = Date.now();
      p.onVoiceActivity(now - lastSpokeRef.current.me < 1500, now - lastSpokeRef.current.oth < 1500);
    }, 400);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const offset = transcriptions.length - items.length;
  const dstCount = useMemo(
    () => transcriptions.filter((t) => whoIs(t, p.room, p.myLang, p.otherLang).kind === "dst").length,
    [transcriptions, p.room, p.myLang, p.otherLang],
  );
  // 语言对 × 实际文种错配（2026-09-12）:某侧的「原文」文种与钉定语言不符＝语言对选错
  // 或两侧麦克风装反。反向钉 en 却收到中文时 ASR 会在中文音频上硬解英文词（实测同一段
  // 音频被两种 hint 解成 '补助不会让你补助错了人' / '不会不会让你不会错掉人'），这就是
  // 「ASR 识别非常不准」的另一半根因。只看原文条：译文天然是目标语言，不参与判定。
  const scriptWarnings = useMemo(() => {
    const samplesOf = (prefix: string) =>
      transcriptions
        .filter((t) => String(t.participantInfo?.identity ?? "").startsWith(prefix))
        .slice(-40)
        .map((t) => String(t.text ?? ""));
    const out: string[] = [];
    const me = scriptMismatch(samplesOf("me-"), expectedScript(p.myLang));
    if (me) out.push(scriptMismatchWarning("我方", p.myLang, me));
    const oth = scriptMismatch(samplesOf("other-"), expectedScript(p.otherLang));
    if (oth) out.push(scriptMismatchWarning("对方", p.otherLang, oth));
    return out;
  }, [transcriptions, p.myLang, p.otherLang]);
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items]);
  const liveBusy = p.meHeld || p.othHeld;

  return (
    <div className="flex flex-col gap-4 lg:h-[calc(100vh-7.5rem)]">
      {p.roleFatal.map((m) => (
        <div
          key={m}
          className="shrink-0 rounded-md border border-red-500/60 bg-red-500/10 px-3 py-2 text-xs leading-relaxed text-red-300"
        >
          ⛔ {m}
          <span className="block text-red-200/80">这个组合下开传译只会产出错字幕，先改设备再启动。</span>
        </div>
      ))}
      {scriptWarnings.map((m) => (
        <div
          key={m}
          className="shrink-0 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-200"
        >
          ⚠ {m}
        </div>
      ))}
      <div className="grid shrink-0 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {/* ① 声音设备卡 */}
        <section className="card flex flex-col gap-3 p-4">
          <span className="label">声音设备</span>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-(--stage-muted)">我方麦克风</span>
            <select className="select" value={p.meMicId} onChange={(e) => p.pickMeMic(e.target.value)}>
              <option value="">系统默认</option>
              {p.micDevices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-(--stage-muted)">对方麦克风</span>
            <select className="select" value={p.othMicId} onChange={(e) => p.pickOthMic(e.target.value)}>
              <option value="">系统默认</option>
              {p.micDevices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
          {/* 真值行:下拉只是意图，switchActiveDevice 失败/设备拔掉时房间会用系统默认。
              这里显示各房**实际**在用的麦，配置与真实分叉时点名（2026-09-12 双麦同源
              事故现场是「下拉说 HUAWEI、房间其实在用对方那支 AirPods」）。 */}
          {(p.meMicLive || p.othMicLive) && (
            <p className="-mt-1 text-[10px] leading-relaxed text-(--stage-muted)">
              实际在用：
              {(
                [
                  ["我方", p.meMicLive, p.meMicId],
                  ["对方", p.othMicLive, p.othMicId],
                ] as const
              ).map(([who, live, sel]) => {
                const name = p.micDevices.find((d) => d.id === live)?.name ?? `${live.slice(0, 6)}…`;
                const fellBack = Boolean(sel) && sel !== live;
                return (
                  <span key={who}>
                    {who}→<span className={fellBack ? "text-amber-300" : ""}>{name}</span>
                    {fellBack ? "（已回退系统默认）" : ""}
                    {"  "}
                  </span>
                );
              })}
            </p>
          )}
          {/* 输入电平:蓝牙麦被别的页面/程序占用时是数字静音且不报错,只有这条能看出来
              （2026-09-12 实测:两个采集同时开着,后开的那个恒 0）。进房即显示，未开麦
              时标「未开麦」——启动传译前后都能立刻确认这支麦是否真的在拾音。 */}
          {(p.meConnected || p.otherConnected) && (
            <p className="-mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] leading-relaxed text-(--stage-muted)">
              <span>输入电平：</span>
              {(
                [
                  ["我方", p.micLevels.me],
                  ["对方", p.micLevels.oth],
                ] as const
              ).map(([who, lvl]) => (
                <span key={who} className="inline-flex items-center gap-1">
                  {who}
                  <span className="inline-block h-1.5 w-14 overflow-hidden rounded bg-white/10 align-middle">
                    <span
                      className={`block h-full transition-[width] duration-150 ${lvl >= 0.02 ? "bg-emerald-400" : "bg-neutral-500"}`}
                      style={{ width: `${lvl > 0 ? Math.min(100, Math.round(lvl * 900)) : 0}%` }}
                    />
                  </span>
                  <span className="font-mono">
                    {lvl < 0 ? "未开麦" : lvl > 0.0005 ? `${Math.round(20 * Math.log10(lvl))}dB` : "静音"}
                  </span>
                </span>
              ))}
            </p>
          )}
          <div className="flex flex-col gap-1.5 text-xs">
            <span className="text-(--stage-muted)">扬声器输出</span>
            <div className="grid grid-cols-2 gap-2">
              <label
                className={`flex items-center gap-1.5 rounded-md border border-(--card-border) px-2 py-1.5 ${
                  p.outputMode === "shared" ? "ring-1 ring-(--accent)" : ""
                }`}
              >
                <input type="radio" name="out-mode" checked={p.outputMode === "shared"} onChange={() => p.setOutputMode("shared")} />
                共享扬声器
              </label>
              <label
                className={`flex items-center gap-1.5 rounded-md border border-(--card-border) px-2 py-1.5 ${
                  p.outputMode === "dual" ? "ring-1 ring-(--accent)" : ""
                } ${p.canDual ? "" : "opacity-50"}`}
                title={p.canDual ? "两人各戴一副耳机,分路独立输出" : "需要桌面 Chrome(setSinkId)"}
              >
                <input
                  type="radio"
                  name="out-mode"
                  disabled={!p.canDual}
                  checked={p.outputMode === "dual"}
                  onChange={() => p.setOutputMode("dual")}
                />
                独立双输出（默认）
              </label>
            </div>
            {p.outputMode === "shared" ? (
              <p className="text-[10px] leading-relaxed text-(--stage-muted)">
                双向译文都从<b>系统当前默认输出</b>出声;任何内核可用(Mac/Windows),跟系统走。当前=
                <span className="text-amber-200">
                  {p.outDevices.find((d) => d.is_default)?.name ?? "未知设备"}
                </span>
                <span className="block text-amber-300/90">
                  注意:这是 macOS/Windows 的默认输出，不是你在这里选的设备——蓝牙设备进出会让它自己跳走；
                  双音箱场景请改用「独立双输出」逐路指定。
                </span>
              </p>
            ) : (
              <>
              <div className="grid grid-cols-2 gap-2">
                <label className="flex flex-col gap-1">
                  <span className="text-(--stage-muted)">我方扬声器</span>
                  <div className="flex gap-1">
                    <select className="select min-w-0 flex-1" value={p.meOutId} onChange={(e) => p.pickMeOut(e.target.value)}>
                      <option value="" disabled>系统默认（双输出档需指定）</option>
                      {p.outDevices.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}
                        </option>
                      ))}
                    </select>
                    <button
                      className="stage-btn-secondary shrink-0 px-2"
                      title="在这台设备放一句试听,确认它就是你想的那台"
                      onClick={() => void playSinkTest(p.meOutId, "我方输出,播放对方原声")}
                    >
                      🔊
                    </button>
                  </div>
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-(--stage-muted)">对方扬声器</span>
                  <div className="flex gap-1">
                    <select className="select min-w-0 flex-1" value={p.othOutId} onChange={(e) => p.pickOthOut(e.target.value)}>
                      <option value="" disabled>系统默认（双输出档需指定）</option>
                      {p.outDevices.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}
                        </option>
                      ))}
                    </select>
                    <button
                      className="stage-btn-secondary shrink-0 px-2"
                      title="在这台设备放一句试听,确认它就是你想的那台"
                      onClick={() => void playSinkTest(p.othOutId, "对方输出,播放我方译文")}
                    >
                      🔊
                    </button>
                  </div>
                </label>
              </div>
              <p className="text-[10px] leading-relaxed text-(--stage-muted)">
                当前路由：我方→{p.outDevices.find((d) => d.id === p.meOutId)?.name ?? "系统默认"}（放对方原声）
                · 对方→{p.outDevices.find((d) => d.id === p.othOutId)?.name ?? "系统默认"}（放我方译文）。
                {(!p.meOutId || !p.othOutId) && (
                  <span className="text-amber-300"> 有输出未指定＝该路走系统默认，两路会混进同一台设备！</span>
                )}
                <span className="block">我方建议戴耳机：Chrome 回声消除只覆盖默认输出，双输出档外放对方原声可能串进我方麦。</span>
              </p>
              </>
            )}
            {!p.canDual && (
              <p className="text-[10px] text-(--stage-muted)">独立双输出需桌面 Chrome;当前内核走共享扬声器。</p>
            )}
          </div>
        </section>

        {/* ② 会话监控卡 */}
        <section className="card flex flex-col gap-3 p-4">
          <span className="label">会话监控</span>
          <div className="grid grid-cols-2 gap-2">
            <Stat label="我方连接">
              <Dot on={p.meConnected} />
              {p.meConnected ? "已接入" : "连接中"}
            </Stat>
            <Stat label="对象连接">
              <Dot on={p.otherConnected} />
              {p.otherConnected ? "已接入" : "未接入"}
            </Stat>
            <Stat label="同传服务">
              <Dot on={dstCount > 0 || liveBusy} />
              {dstCount > 0 || liveBusy ? "出译中" : "待命"}
            </Stat>
            <Stat label="半双工">{p.halfDuplex ? (liveBusy ? "暂让中" : "值守") : "关闭"}</Stat>
            <Stat label="会话时长">
              <SessionClock startedAt={p.startedAt} />
            </Stat>
            <Stat label="翻译条数">{dstCount}</Stat>
            <Stat label="会话编号">
              <span className="truncate" title={p.callId}>{p.callId}</span>
            </Stat>
          </div>
          <p className="text-xs leading-relaxed text-(--stage-muted)">
            语言对:我方 {LANG_SHORT[p.myLang] ?? p.myLang} ⇄ 对方 {LANG_SHORT[p.otherLang] ?? p.otherLang}
            (建房时已钉死,换语言对需结束并重建房间)。
          </p>
        </section>

        {/* ③ 控制卡 */}
        <section className="card flex flex-col gap-2.5 p-4">
          <span className="label">控制</span>
          <button
            className={p.interpOn ? "stage-btn-secondary" : "stage-btn-primary"}
            onClick={p.toggleInterp}
            disabled={!p.meConnected}
            title="进房不自动开始:启动前两端麦克风静默,原文/译文都不产生"
          >
            {p.interpOn ? "■ 停止传译" : "▶ 启动传译"}
          </button>
          <p className="-mt-1 text-[10px] leading-relaxed text-(--stage-muted)">
            传译{p.interpOn ? "进行中" : "未启动"}——停止后两端静默,字幕与译文暂停,在播译文念完即止。
          </p>
          <div className="grid grid-cols-2 gap-2">
            <button className="stage-btn-secondary" onClick={p.toggleMeMic} disabled={!p.meConnected}>
              {p.meHeld ? "暂让中…" : p.meMicOn ? "静音我方麦" : "开我方麦"}
            </button>
            <button className="stage-btn-secondary" onClick={p.toggleOthMic} disabled={!p.otherConnected}>
              {p.othHeld ? "暂让中…" : p.othMicOn ? "静音对方麦" : "开对方麦"}
            </button>
          </div>
          <label className="flex items-start gap-2 text-xs leading-relaxed">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={p.halfDuplex}
              onChange={(e) => p.setHalfDuplex(e.target.checked)}
            />
            <span>自动半双工:译文播报时暂让对向麦克风,防共享扬声器串译(外放建议开;戴耳机可关)</span>
          </label>
          <button className="stage-btn-secondary" onClick={() => setClearedCount(transcriptions.length)}>
            清空字幕
          </button>
          {/* 结束按钮只在「已在退出中」时禁用(防 7 连发),连接/busy 中都保持可点。 */}
          <button className="stage-btn-secondary mt-auto text-red-300" onClick={p.leave} disabled={p.leaving}>
            {p.leaving ? "结束中…" : "结束一体台会话"}
          </button>
        </section>
      </div>

      {/* ④ 双语字幕卡 */}
      <section className="card flex min-h-[320px] flex-1 flex-col gap-2 overflow-hidden">
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
          <span className="label">一体台 · 双语字幕</span>
          <div className="flex items-center gap-1.5">
            {([
              ["both", "双方"],
              ["me", "仅我方"],
              ["other", "仅对方"],
            ] as const).map(([v, label]) => (
              <button
                key={v}
                onClick={() => setFilter(v)}
                className={`rounded-full px-2.5 py-1 text-[11px] ${
                  filter === v
                    ? "bg-(--accent) font-medium text-(--accent-ink)"
                    : "border border-(--card-border) text-(--stage-muted)"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <div ref={listRef} className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-1 py-2">
          {items.length === 0 && (
            <div className="flex flex-1 items-center justify-center font-mono text-[10px] uppercase tracking-[0.16em] text-(--stage-muted)">
              等说话…开口即译
            </div>
          )}
          {items.map((t, i) => {
            const idx = offset + i;
            if (idx < clearedCount) return null;
            const who = whoIs(t, p.room, p.myLang, p.otherLang);
            if (filter === "me" && who.side !== "right") return null;
            if (filter === "other" && who.side !== "left") return null;
            return (
              <div key={`${who.text}-${idx}`} className={`flex ${who.side === "right" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[85%] rounded-lg px-3 py-2 text-[13px] leading-relaxed ${
                    who.kind === "dst"
                      ? "border border-(--card-border) bg-white/5 text-(--foreground)"
                      : "bg-(--accent) text-(--accent-ink)"
                  }`}
                >
                  <span className="mr-1.5 font-mono text-[10px] font-bold uppercase opacity-70">{who.text}</span>
                  {String(t.text ?? "")}
                </div>
              </div>
            );
          })}
        </div>
        {liveBusy && (
          <p className="shrink-0 text-[11px] text-amber-300">
            {p.othHeld ? "我方译文播报中 · 对方麦克风暂让" : "对方译文播报中 · 我方麦克风暂让"}
          </p>
        )}
        {p.sameDeviceWarning && <p className="shrink-0 text-xs text-amber-300">{p.sameDeviceWarning}</p>}
        {(p.micSilent.me || p.micSilent.oth) && (
          <p className="shrink-0 text-[11px] leading-relaxed text-amber-300">
            {p.micSilent.me && p.micSilent.oth ? "两侧麦克风" : p.micSilent.me ? "我方麦克风" : "对方麦克风"}
            连续 5 秒没有电平——这支设备可能被别的页面/程序占用（蓝牙麦同一时刻只能给一个程序用），或它根本没在拾音。换一支设备，
            或关掉占用它的窗口/程序再试。
          </p>
        )}
        {p.error && <p className="shrink-0 text-xs text-red-400">{p.error}</p>}
      </section>
    </div>
  );
}

type Bubble = { text: string; side: "left" | "right"; kind: "src" | "dst" };

type TextStreamEntry = {
  text?: unknown;
  participantInfo?: { identity?: string };
  streamInfo?: { attributes?: Record<string, string> };
};

/** 字幕归属（照抄 interpret 页 subtitleLabel 的解析，另给译文标注听众端）：
 * 人端 identity=原文说话方;agent 转写看 lk.transcribed_track_id——指向人端轨=原文,
 * 指向 agent 自己的 trans-<lang> 轨=译文。译文听众 = 该目标语言那一端。 */
function whoIs(t: TextStreamEntry, room: Room, myLang: string, otherLang: string): Bubble {
  const id = String(t.participantInfo?.identity ?? "");
  if (id.startsWith("me-")) return { text: "我方说的", side: "right", kind: "src" };
  if (id.startsWith("other-")) return { text: "对方说的", side: "left", kind: "src" };
  const trackSid = t.streamInfo?.attributes?.["lk.transcribed_track_id"] ?? "";
  if (trackSid) {
    const pools = [room.remoteParticipants.values(), [room.localParticipant].values()];
    for (const pool of pools) {
      for (const participant of pool) {
        for (const pub of Object.values(participant.trackPublications ?? {})) {
          if (pub?.trackSid !== trackSid) continue;
          const owner = String(participant.identity ?? "");
          if (owner.startsWith("me-")) return { text: "我方说的", side: "right", kind: "src" };
          if (owner.startsWith("other-")) return { text: "对方说的", side: "left", kind: "src" };
          const name = String(pub.trackName ?? "");
          if (name.startsWith("trans-")) {
            const lang = name.slice("trans-".length);
            const ear = LANG_SHORT[lang] ?? lang;
            return { text: `译文·${ear}`, side: lang === myLang ? "right" : "left", kind: "dst" };
          }
        }
      }
    }
  }
  return { text: "同传", side: "left", kind: "dst" };
}

/**
 * 监听一个房间的 trans-* 译文音轨出声,出声(含 600ms 余量)期间 setHeld(true)。
 * analyser 挂不上(内核限制/上下文 suspended)就静默退化——半双工不生效但链路不受影响,
 * 用户仍可用手动静音按钮兜底。
 */
/**
 * 远端轨放音路由器(2026-09-12 终版):AudioContext + ctx.setSinkId。
 * Chromium 40647375:element.setSinkId 对 WebRTC 远端流静默失效——本机实验实证
 * AudioContext.setSinkId 可用且 Chrome 会把枚举 id 解析成真实设备(读回不同 id
 * 是正常现象)。每方向一个 router,输出设备切到哪台全 follow ctx_sink effect。
 */
/**
 * 房间当前**实际**在用的麦克风设备 id（真值层）。livekit 的 `switchActiveDevice`
 * 遇到不存在的 deviceId 只返回 false 并保留原设备（未发布时即系统默认），UI 上仍显示
 * 用户选的那支——「配置说我方是 HUAWEI、实际是对方那支 AirPods」就是这个断层，
 * `getSettings().deviceId` 是唯一事实来源（2026-09-12 双麦同源事故）。
 */
function effectiveMicDeviceId(room: Room | null): string {
  try {
    if (!room) return "";
    const mt = room.localParticipant.getTrackPublication(Track.Source.Microphone)?.track?.mediaStreamTrack;
    const fromTrack = String(mt?.getSettings().deviceId ?? "");
    if (fromTrack) return fromTrack;
    // 未发布采集时没有轨可读:退回 livekit 自己记的当前输入设备(与我们的 localStorage
    // 是两份独立来源，分叉同样说明「下拉 ≠ 实际」)。未切换前该值是字面量 'default'
    // （不是设备 id），不能当作「已回退」的证据，一并当空。
    const raw = room.getActiveDevice("audioinput");
    return typeof raw === "string" && raw !== "default" && /^[\w.-]{6,}$/.test(raw) ? raw : "";
  } catch {
    return "";
  }
}

/**
 * 麦克风电平表(本地采集轨)。接 AnalyserNode 读 RMS——**本地轨**不受 Chromium
 * 「远端流必须挂 media element」那条限制,可以直接进 WebAudio 图。
 *
 * 存在的理由(2026-09-12 实测):macOS 上蓝牙麦(HFP)同一时刻只能被**一个**客户端占用,
 * 被别的页面/程序占着时,我们这边 `getUserMedia` 照常成功、不报错,但拿到的是**数字
 * 静音**(实测 RMS 恒 0)。此前只能靠外部探针才发现,操作员完全分不清「没人说话」与
 * 「这支麦根本没信号」——正是「为什么没有输入电平」的由来。
 */
function createMicMeter() {
  let ctx: AudioContext | null = null;
  let src: MediaStreamAudioSourceNode | null = null;
  let an: AnalyserNode | null = null;
  let wired: MediaStreamTrack | null = null;
  const buf = new Float32Array(512);
  return {
    /** 返回 RMS;负值 = 没有可用采集轨(未开麦)。 */
    level(track?: MediaStreamTrack | null): number {
      if (!track || track.readyState !== "live") {
        if (wired) {
          src?.disconnect();
          src = null;
          an = null;
          wired = null;
        }
        return -1;
      }
      if (track !== wired) {
        try {
          ctx = ctx ?? new AudioContext();
          if (ctx.state === "suspended") void ctx.resume().catch(() => {});
          src?.disconnect();
          an = ctx.createAnalyser();
          an.fftSize = 1024;
          src = ctx.createMediaStreamSource(new MediaStream([track]));
          src.connect(an);
          wired = track;
        } catch {
          return -1;
        }
      }
      if (!an) return -1;
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      return Math.sqrt(sum / buf.length);
    },
    dispose() {
      try {
        src?.disconnect();
      } catch {
        /* 已断 */
      }
      src = null;
      an = null;
      wired = null;
      ctx?.close().catch(() => {});
      ctx = null;
    },
  };
}

function createAudioRouter(who: string) {
  let ctx: AudioContext | null = null;
  let lastSink = "";
  const sources = new Map<string, { src: MediaStreamAudioSourceNode; primer: HTMLAudioElement }>();
  const ensure = () => {
    ctx = ctx ?? new AudioContext();
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    return ctx;
  };
  const applySink = async (id: string) => {
    const c = ensure();
    const sinkCtx = c as AudioContext & { setSinkId?: (id: string) => Promise<void>; sinkId?: string };
    await sinkCtx.setSinkId?.(id || "default");
    wlog("ctx_sink", { who, want: (id || "default").slice(0, 12), got: String(sinkCtx.sinkId ?? "").slice(0, 12), ctxState: c.state });
  };
  return {
    add(trackSid: string, mediaTrack: MediaStreamTrack) {
      try {
        const c = ensure();
        // 引活元素(Chromium 40094084 官方口径:「远端流必须挂到 media element 上
        // WebAudio 才会出声」)——静音隐藏元素不贡献声音,只令 Chrome 开始拉帧进
        // WebAudio 图;出声仍走 ctx.destination + ctx.setSinkId 路由。
        const primer = document.createElement("audio");
        primer.srcObject = new MediaStream([mediaTrack]);
        primer.muted = true;
        primer.autoplay = true;
        primer.style.display = "none";
        document.body.appendChild(primer);
        primer.play().catch(() => {});
        const src = c.createMediaStreamSource(new MediaStream([mediaTrack]));
        src.connect(c.destination);
        sources.set(trackSid, { src, primer });
        wlog("ctx_src", { who, track: trackSid.slice(0, 10), ctxState: c.state });
      } catch (e) {
        wlog("ctx_src", { who, err: String(e) });
      }
    },
    remove(trackSid: string) {
      const s = sources.get(trackSid);
      if (s) {
        try {
          s.src.disconnect();
        } catch {
          /* 已断 */
        }
        s.primer.srcObject = null;
        s.primer.remove();
        sources.delete(trackSid);
      }
    },
    async setSink(id: string) {
      lastSink = id;
      try {
        await applySink(id);
      } catch (e) {
        wlog("ctx_sink", { who, want: (id || "default").slice(0, 12), err: e instanceof Error ? e.name : String(e) });
      }
    },
    // 手势唤醒后重投上次目标:suspended 期间 setSinkId 可能被拒,恢复 running 必须
    // 补一枪,否则一路会静挂在上次的设备(或默认)上。
    resume() {
      if (ctx && ctx.state === "suspended") {
        ctx.resume()
          .then(() => {
            if (lastSink) void applySink(lastSink).catch(() => {});
          })
          .catch(() => {});
      }
    },
    dispose() {
      for (const s of sources.values()) {
        try {
          s.src.disconnect();
        } catch {
          /* 已断 */
        }
        s.primer.srcObject = null;
        s.primer.remove();
      }
      sources.clear();
      ctx?.close().catch(() => {});
      ctx = null;
    },
  };
}

function watchTransAudio(room: Room | null, setHeld: (v: boolean) => void): () => void {
  if (!room) return () => {};
  let ctx: AudioContext | null = null;
  const nodes = new Map<string, { source: MediaStreamAudioSourceNode; analyser: AnalyserNode }>();

  const attach = (track: RemoteTrack, pub: RemoteTrackPublication) => {
    if (!String(pub.trackName ?? "").startsWith("trans-")) return;
    try {
      ctx = ctx ?? new AudioContext();
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      const source = ctx.createMediaStreamSource(new MediaStream([track.mediaStreamTrack]));
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      nodes.set(track.sid, { source, analyser });
    } catch {
      /* ignore */
    }
  };
  const detach = (track: RemoteTrack) => {
    nodes.delete(track.sid);
  };
  const onSub = (track: RemoteTrack, pub: RemoteTrackPublication) => attach(track, pub);
  room.on(RoomEvent.TrackSubscribed, onSub);
  room.on(RoomEvent.TrackUnsubscribed, detach);
  // 监听启动晚于发轨(如连接已完成)时,补挂已在房的轨。
  for (const participant of room.remoteParticipants.values()) {
    for (const pub of participant.trackPublications.values()) {
      const t = pub.track;
      if (t) attach(t as RemoteTrack, pub);
    }
  }

  const buf = new Float32Array(512);
  let busyUntil = 0;
  let heldSince = 0;
  let quietUntil = 0;
  const timer = window.setInterval(() => {
    // 看门狗(2026-09-11 审计 P0-3):AudioContext 被系统挂起(WKWebView 切后台/
    // 长会话音频路由切换)时 analyser 数据会冻结在最后一帧——冻结在响段令
    // busyUntil 无限续期、对向麦克风被永久暂让(fail-closed = 零翻译)。持续
    // resume + 单次连续 hold 超 10s 强制释放并给 5s 说话冷却窗(fail-open 串译
    // 优于永久压麦,用户可用手动静音按钮兜底)。
    if (ctx && ctx.state !== "running") ctx.resume().catch(() => {});
    const now = Date.now();
    let loud = false;
    if (now >= quietUntil) {
      for (const { analyser } of nodes.values()) {
        analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        if (Math.sqrt(sum / buf.length) > 0.012) {
          loud = true;
          break;
        }
      }
    }
    if (loud) busyUntil = now + 600;
    const held = now < busyUntil;
    if (held && !heldSince) heldSince = now;
    if (!held) heldSince = 0;
    if (heldSince && now - heldSince > 10_000) {
      busyUntil = 0;
      heldSince = 0;
      quietUntil = now + 5_000;
    }
    setHeld(held);
  }, 120);

  return () => {
    window.clearInterval(timer);
    room.off(RoomEvent.TrackSubscribed, onSub);
    room.off(RoomEvent.TrackUnsubscribed, detach);
    for (const { source } of nodes.values()) {
      try {
        source.disconnect();
      } catch {
        /* ignore */
      }
    }
    nodes.clear();
    if (ctx) ctx.close().catch(() => {});
  };
}

async function fetchToken(account: string, callId: string, role: "me" | "other"): Promise<{ serverUrl: string; participantToken: string }> {
  const resp = await fetch(`${apiBase()}/api/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: account, call_id: callId, participant_identity: `${role}-${callId}` }),
  });
  if (!resp.ok) throw new Error(`token http ${resp.status}`);
  const data = (await resp.json()) as { serverUrl?: string; participantToken?: string };
  if (!data.serverUrl || !data.participantToken) throw new Error("token 响应缺 serverUrl/participantToken");
  return { serverUrl: data.serverUrl, participantToken: data.participantToken };
}
