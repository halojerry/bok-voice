/**
 * 同传一体台「设备角色」纯逻辑（2026-09-12 双麦同源事故收口）。
 *
 * 事故形态（实测 call-b2ff71bb）:保存的我方麦克风是已拔掉的 HUAWEI（id 不在枚举里），
 * 连接期 `switchActiveDevice` 静默失败 → 房间回退到系统默认麦，而那一刻系统默认正是
 * 对方那支（AirPods）→ me/other 两个身份发布的**是同一支物理麦克风**。后果三连:
 *   ① 两份 ASR 对同一段话音各出一份转写（fwd '这是个问题…' 与 rev '这是个问题…' 相隔
 *      0.7s，同一句不同听错）→「没有区分对象讲话和我讲话」;
 *   ② 我方扬声器播的「对方原声」= 我自己那支麦 → 自听回声;
 *   ③ 反向语言钉 en 收到中文 → 识别全废。
 *
 * 不变量:4 个角色槽（我方麦/对方麦/我方扬声器/对方扬声器）默认必须落在 4 台不同物理
 * 设备上;唯一允许的复用是**同侧耳机**（同侧麦+扬声器＝戴在一个人头上）。任何跨侧复用
 * 都是一条物理回环，必须拦住而不是静默产出垃圾字幕。
 *
 * 比对按**设备名归一**而非 id:Chrome 对同一台设备的 input/output 各给一个 id，蓝牙
 * 重连后 id 还会换，跨角色只有名字稳定。名字拿不到时退化成 id 前缀。
 */

export type RoleKey = "meMic" | "othMic" | "meOut" | "othOut";

export type RoleSlot = {
  role: RoleKey;
  label: string;
  /** 设备显示名（拿不到传空串）。 */
  name: string;
  /** 设备 id（可为空串＝系统默认/未知）。 */
  id: string;
  /**
   * Chrome 的物理设备组 id：同一台设备的输入与输出共用一个 groupId，且与 id 无关
   * （id 在蓝牙重连后会换，groupId 不会）。拿不到时传空串。
   */
  groupId?: string;
};

export type RoleIssues = { fatal: string[]; warn: string[] };

/** 「默认 - Mac mini扬声器 (Built-in)」/「Default - X」→「mac mini扬声器」:
 *  去伪条目前缀与括注。英文前缀也要剥——不然 en-US Chrome 下默认项与实体项归一不到一起。 */
export function deviceBaseName(raw: string): string {
  return raw
    .replace(/^(?:默认|default)\s*-\s*/i, "")
    .replace(/\s*\(.*?\)\s*$/, "")
    .trim()
    .toLowerCase();
}

/**
 * 一个槽的「物理身份候选」三件套:groupId > 设备名归一 > id 前缀,取并集判定。
 * 为什么要并集:默认伪条目「默认 - X」在 Chrome 里**没有 groupId**,只有靠名字才认得出
 * 它和实体 X 是同一台(否则用户一侧选「系统默认」另一侧选实体 X 就会绕过冲突判定);
 * 而两支同型号设备名字相同、groupId 不同,由调用处按「组内 groupId 互斥」降级为警告。
 * id 为字面量 "default" 时**不能当身份**:输入与输出的默认伪条目共用这个值。
 */
function identityTokens(s: RoleSlot): string[] {
  const t: string[] = [];
  // 「系统默认」有两种写法（UI 空串 / 伪条目 id==="default"），两者是同一支默认设备，
  // 必须归一到同一个身份，否则一侧选空、另一侧选伪条目 X 就绕过了判定。
  // 标记按**角色种类**分开：输入默认与输出默认是两台不同的设备（默认麦 vs 默认扬声器），
  // 混用同一个标记会把它们误判成同一台。
  if (!s.id || s.id === "default") t.push(s.role.endsWith("Mic") ? "默认:in" : "默认:out");
  if (s.groupId) t.push(`g:${s.groupId}`);
  const base = deviceBaseName(s.name);
  if (base) t.push(`n:${base}`);
  if (s.id && s.id !== "default") t.push(`#${s.id.slice(0, 8)}`);
  return t;
}

/** 按「是否同一台物理设备」把槽聚成组（并查集式合并，槽数 ≤4）。 */
function groupByDevice(slots: RoleSlot[]): RoleSlot[][] {
  const groups: RoleSlot[][] = slots.map((s) => [s]);
  for (let merged = true; merged; ) {
    merged = false;
    for (let i = 0; i < groups.length && !merged; i++) {
      for (let j = i + 1; j < groups.length; j++) {
        const a = groups[i].flatMap(identityTokens);
        const b = groups[j].flatMap(identityTokens);
        if (a.some((t) => b.includes(t))) {
          groups[i] = [...groups[i], ...groups[j]];
          groups.splice(j, 1);
          merged = true;
          break;
        }
      }
    }
  }
  return groups;
}

/** 允许复用的同侧组合:一个人一副耳机，麦与扬声器本来就同属一台。 */
const ALLOWED_PAIRS: RoleKey[][] = [
  ["meMic", "meOut"],
  ["othMic", "othOut"],
];

/**
 * 角色冲突判定。返回 fatal(结构性坏掉，传译不该开始)与 warn(能跑但要提醒)。
 *
 * 名字与 id 都空的槽＝「系统默认」，两侧同时空即同一支默认设备（同一次 getUserMedia
 * 不带 deviceId 只会给系统默认那一支）——所以空槽照常参与判定，不跳过。调用方负责在
 * 枚举未就绪时（micDevices 为空）干脆不传麦克风槽，避免页面一加载就误报。
 */
export function deviceRoleIssues(slots: RoleSlot[]): RoleIssues {
  const fatal: string[] = [];
  const warn: string[] = [];
  for (const dupes of groupByDevice(slots)) {
    if (dupes.length < 2) continue;
    const roles = dupes.map((d) => d.role);
    const display = dupes[0].name || "系统默认（同一支）";
    // 组内出现两个互不相同的非空 groupId = 系统明确说这是两台不同设备（典型:两支
    // 同型号麦名字一模一样）。名字相同只能说明型号相同,不该拦死——降级为需要人确认的
    // 警告,否则「买了两支一样的麦」的客户会被永久挡住且无从下手。
    const gids = new Set(dupes.map((d) => d.groupId).filter(Boolean));
    if (gids.size > 1) {
      warn.push(
        `${dupes.map((d) => d.label).join("、")}看起来是同一型号（都叫「${dupes[0].name}」），但系统把它们识别为两台不同设备——若确实各是一支可以继续；若其实只有一支，请改选。`,
      );
      continue;
    }
    const allowed =
      roles.length === 2 && ALLOWED_PAIRS.some((pair) => pair.includes(roles[0]) && pair.includes(roles[1]));
    if (allowed) {
      // 同侧麦+扬声器合法，但蓝牙设备输入+输出双开在 macOS 下（HFP）输出侧可能没声。
      if (/bluetooth|airpods|蓝牙/i.test(dupes[0].name)) {
        warn.push(
          `${dupes.map((d) => d.label).join("与")}是同一台蓝牙设备「${dupes[0].name}」——macOS 下输入+输出双开可能无声（HFP），建议换一台（有线/内建）。`,
        );
      }
      continue;
    }
    const has = (r: RoleKey) => roles.includes(r);
    const unnamed = !dupes[0].name;
    if (has("meMic") && has("othMic")) {
      fatal.push(
        unnamed
          ? "两侧麦克风都还是「系统默认」——同一次采集只会给系统默认那一支，两个方向收到的是同一个人的声音，无法区分谁在讲。请给两方各指定一支不同的麦克风。"
          : `两侧麦克风是同一支「${display}」——两个方向会收到同一个人说话，无法区分谁在讲，译文会互相复读。请给两方各指定一支不同的麦克风。`,
      );
    } else if (has("othMic") && has("meOut")) {
      fatal.push(
        `对方麦克风与我方扬声器是同一台「${display}」——你会听到自己的原声（自听回声），且两路声音混在一起。请把它们分开。`,
      );
    } else if (has("meMic") && has("othOut")) {
      fatal.push(
        `我方麦克风与对方扬声器是同一台「${display}」——我方的话会直接灌进对方扬声器并被再次翻译，形成回环。请把它们分开。`,
      );
    } else if (has("meOut") && has("othOut")) {
      fatal.push(
        unnamed
          ? "两侧扬声器都还是「系统默认」——两路声音（对方原声+我方译文）会混在同一台设备上。独立双输出档请各指定一台。"
          : `两侧扬声器是同一台「${display}」——两路声音（对方原声+我方译文）会混在同一台设备上。请各指定一台。`,
      );
    } else {
      fatal.push(
        `${dupes.map((d) => d.label).join("、")}落在同一台设备「${display}」上——同一台设备只能扮演一个角色，否则会形成声音回环。请把它们分开。`,
      );
    }
  }
  return { fatal, warn };
}

// ---- 语言对 × 实际文种 ------------------------------------------------------
// 反向语言钉 en 却收到中文时，ASR 会在中文音频上硬解英文词（'补助不会让你补助错了人'
// 与正向同一句的 '不会不会让你不会错掉人' 就是同一段音频被两种 hint 解出的两份结果）
// ——「ASR 识别非常不准」的另一半根因。文种错配用转写文本即可判定，零额外成本。

export type ScriptFamily = "cjk" | "latin" | "unknown";

export function scriptFamily(text: string): ScriptFamily {
  const cjk = (text.match(/[\u3400-\u9fff]/g) ?? []).length;
  const latin = (text.match(/[A-Za-z]/g) ?? []).length;
  if (cjk < 4 && latin < 6) return "unknown";
  if (cjk >= latin) return "cjk";
  return "latin";
}

export function expectedScript(lang: string): ScriptFamily {
  return lang === "en" ? "latin" : "cjk";
}

/** 文种与语言对不符的占比;证据不足（样本 <5 或占比 <70%）返回 null——宁可不报。 */
export function scriptMismatch(samples: string[], expect: ScriptFamily): { bad: number; total: number } | null {
  let bad = 0;
  let total = 0;
  for (const s of samples) {
    const f = scriptFamily(s);
    if (f === "unknown") continue;
    total += 1;
    if (f !== expect) bad += 1;
  }
  if (total < 5 || bad / total < 0.7) return null;
  return { bad, total };
}

export function scriptMismatchWarning(
  sideLabel: string,
  lang: string,
  r: { bad: number; total: number },
): string {
  const want = lang === "en" ? "英文" : "中文";
  const got = lang === "en" ? "中文" : "英文";
  return `${sideLabel}这一侧 ${r.total} 条原文里有 ${r.bad} 条是${got}，但语言对把${sideLabel}设成了${want}——识别会严重走样。请检查:①语言对是否选错;②两侧麦克风是否装反（这一侧的麦实际在收另一侧的话）。`;
}
