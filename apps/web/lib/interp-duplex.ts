/**
 * 同传控制台闭麦闸缺省档（W8-A3 2026-10-09，Ethan 拍板全双工拓扑）。
 *
 * 拓扑定案：远程双方各戴耳机、各自设备——me/other 两线天然独立，无串译环，
 * 全双工天然成立。自动半双工（meHeld/othHeld 暂让 + 声源仲裁）是「两人同机
 * 一体台 + 共享扬声器外放」历史遗产，在耳机拓扑下是纯伤害（对方说话被暂让
 * 白丢）。缺省 = 互不闭麦（真全双工）。
 *
 * 同机一体演示档（两人同机各一支麦 + 共享扬声器外放）保留为逃生门：外放同桌
 * 不开暂让 = 译文被对向麦拾回再译 = 串译死循环，此时必须打开。web 静态导出
 * 无 env，不接 CP 设置面（演示档是操作员本地姿势）——开关经 URL query
 * `?halfDuplex=1` 临时打开（`=0` 显式关），或把 HALF_DUPLEX_DEFAULT 改 true。
 */

/** 编译期常量（缺省关=全双工）。同机演示部署可改 true 一次性翻回旧档。 */
export const HALF_DUPLEX_DEFAULT = false;

/**
 * 初始半双工档（纯函数，node 单测直喂）：query 显式值优先于编译期常量。
 * `?halfDuplex=1` 强开 / `?halfDuplex=0` 强关 / 缺席或坏值 = HALF_DUPLEX_DEFAULT。
 */
export function halfDuplexInitial(search: string): boolean {
  let raw: string | null = null;
  try {
    raw = new URLSearchParams(search || "").get("halfDuplex");
  } catch {
    raw = null;
  }
  if (raw === "1") return true;
  if (raw === "0") return false;
  return HALF_DUPLEX_DEFAULT;
}
