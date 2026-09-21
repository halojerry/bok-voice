#!/usr/bin/env python3
"""T5/T6/R1 联合探针（2026-09-21）。

R1  真实标注集上复算 V-5 的 margin 工作曲线（scripts/.r1_gold.20260921.json，
    标注=代理人工判读，规则族+逐条覆盖，见 r1_build_gold.py）。
T5  把 SemIf 的「选项 token 自己算 softmax」搬回手搓判定：进程内一次 forward、
    在答案位取 12 个选项字母的 logit 做 softmax → margin 变成真概率差（解 V1-03
    的「服务端 logprob 未归一化」）。服务端 top_logprobs 上限 11（≥12 断连，实测），
    故 9B 侧用窗口内字母 softmax + 缺失质量上界（bounded），4B 侧进程内精确算。
T6  「表达愤怒」选项区分度三变体：V0 现行 12 选项 / V1 合并愤怒+不耐烦（11 选项）/
    V2 描述强化（12 选项带括号说明）。指标：合成集准确率、句 4（金标=表达愤怒）判定、
    真实集上「愤怒/不满」被选次数（污染度）。

用法（分腿，产物各自落盘，可断点续）：
  python3 t56r1_probe.py --leg server --tag 4B   # 4B 服务端（含 9B 同款逻辑）
  python3 t56r1_probe.py --leg server --tag 9B
  python3 t56r1_probe.py --leg inproc            # 4B 进程内精确 softmax + T6 三变体
  python3 t56r1_probe.py --leg analyze           # 汇总分析（只读产物）

安全边界：只读本机文件 + 本机回环 :1235/:1237，路径端口写死白名单，不接受外部输入。
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOWED_SCHEME = "http"
ALLOWED_HOSTS = frozenset({"127.0.0.1"})
ALLOWED_PORTS = frozenset({1235, 1237})
PATH_COMPLETIONS = "/v1/chat/completions"

LOCAL_JSON = HERE / ".probe_hotword_ab.m4pro20260921.json"
CLOUD_JSON = HERE / ".probe_cloud_asr_ab.m4pro_full.json"
REAL_JSON = HERE / ".r1_gold.20260921.json"

M4B = "/Users/halo/.lmstudio/models/avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit"
M9B = "/Users/halo/.lmstudio/models/huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit"
MODELS = {"4B": (M4B, 1235), "9B": (M9B, 1237)}

OPTIONS = ["诈骗质疑", "要求证明", "机器人质疑", "表达愤怒", "索要主管电话",
           "表达不耐烦", "查件去向", "问赔偿金额", "问能否查单号", "问仓库位置",
           "拒绝继续", "其他"]
MARKERS = list("ABCDEFGHIJKL")
ANSWER = {1: "诈骗质疑", 2: "要求证明", 3: "机器人质疑", 4: "表达愤怒", 5: "索要主管电话",
          6: "表达不耐烦", 7: "查件去向", 8: "问赔偿金额", 9: "问能否查单号", 10: "问仓库位置"}

# T6 变体表
OPT_V1 = ["诈骗质疑", "要求证明", "机器人质疑", "表达不满（愤怒／不耐烦）", "索要主管电话",
          "查件去向", "问赔偿金额", "问能否查单号", "问仓库位置", "拒绝继续", "其他"]
OPT_V2 = ["诈骗质疑（怀疑是骗子／假冒快递）", "要求证明（要工牌／证件／公司证明）",
          "机器人质疑（怀疑是 AI／录音）", "表达愤怒（爆粗／骂人／暴怒）",
          "索要主管电话（要找上级／投诉渠道）", "表达不耐烦（催促／嫌啰嗦／想快点结束）",
          "查件去向（问包裹／货物情况）", "问赔偿金额（问赔多少／怎么赔）",
          "问能否查单号（提供或问运单号查询）", "问仓库位置（问仓库／网点在哪）",
          "拒绝继续（明确拒绝／要挂断）", "其他"]
VARIANTS = {"V0": OPTIONS, "V1": OPT_V1, "V2": OPT_V2}

STEP_CTX = (
    "當前通話情形：這是快遞客服外呼，話術第 3 步，該步目標是向客戶索取其本人的 WhatsApp／微信號碼。\n"
    "客戶是接到陌生來電的收件人，常見反應是懷疑是詐騙、不耐煩、追問進度或索賠。\n"
)


def decide_sys(options: list[str]) -> str:
    marks = list(MARKERS[: len(options)])
    return (
        "你是快遞客服語音系統的意圖判定器，只做判定不生成回覆。\n" + STEP_CTX +
        "注意：客戶話語來自電話語音轉寫，可能有錯字、缺字、詞序錯亂。\n"
        "**請結合上面的通話情形，推斷客戶真正想表達什麼**，不要因為字面怪異就選「其他」。\n"
        "可選意圖：\n" + "  ".join(f"{marks[i]}.{o}" for i, o in enumerate(options)) + "\n"
        "只輸出一個字母，不要解釋、不要標點。"
    )


def guarded_url(port: int) -> str:
    if 1235 != port and 1237 != port:
        raise ValueError(f"port 不在白名单: {port!r}")
    url = urllib.parse.urlunsplit(
        (ALLOWED_SCHEME, f"127.0.0.1:{port}", PATH_COMPLETIONS, "", ""))
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != ALLOWED_SCHEME or parts.hostname not in ALLOWED_HOSTS \
            or parts.port not in ALLOWED_PORTS:
        raise ValueError(f"URL 校验失败: {url!r}")
    return url


def call(url: str, model: str, sysmsg: str, user: str, top_k: int = 10):
    body = {"model": model,
            "messages": [{"role": "system", "content": sysmsg},
                         {"role": "user", "content": user}],
            "max_tokens": 1, "temperature": 0,
            "logprobs": True, "top_logprobs": top_k}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310 - URL 已过白名单
        out = json.loads(r.read())
    lp = (out["choices"][0].get("logprobs") or {}).get("content") or []
    return [(t["id"], t["logprob"]) for t in ((lp[0] if lp else {}).get("top_logprobs") or [])]


def calibrate_letters(url: str, model: str) -> dict[str, str]:
    """token id -> 字母。标定即 SemIf 的 tokenization 边界校验。"""
    ids: dict[str, str] = {}
    for m in MARKERS:
        top = call(url, model, "只输出字母。", f"只输出一个大写字母：{m}", 5)
        if top:
            ids.setdefault(top[0][0], m)
    return ids


def build_synthetic() -> list[dict]:
    out, seen = [], set()
    ld = json.load(open(LOCAL_JSON, encoding="utf-8"))
    for r in ld["records"]:
        key = f"L|{r['tier']}|{r['idx']}|{r['transcript']}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"src": f"syn/local/{r['tier']}", "txt": r["transcript"],
                    "gold": ANSWER[r["idx"]], "idx": r["idx"]})
    cd = json.load(open(CLOUD_JSON, encoding="utf-8"))
    for r in cd["records"]:
        if r.get("backend") != "cloud":
            continue
        key = f"C|{r['tier']}|{r['idx']}|{r['transcript']}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"src": f"syn/cloud/{r['tier']}", "txt": r["transcript"],
                    "gold": ANSWER[r["idx"]], "idx": r["idx"]})
    return out


def build_real() -> list[dict]:
    rows = json.load(open(REAL_JSON, encoding="utf-8"))
    return [{"src": f"real/{r['src']}", "txt": r["txt"], "gold": r["gold"],
             "step": r.get("step"), "lang": r.get("lang")} for r in rows]


def softmax(vals: dict[str, float]) -> dict[str, float]:
    mx = max(vals.values())
    ex = {k: math.exp(v - mx) for k, v in vals.items()}
    z = sum(ex.values())
    return {k: v / z for k, v in ex.items()}


def norm_intent(opt: str) -> str:
    """V2 的选项带括号说明，剥回纯名；V1 的合并项归一成「表达不满」。"""
    return opt.split("（")[0]


def gold_ok(gold: str, got_marker: str, opts: list[str]) -> bool:
    """判定命中：选项名相等，或（V1 合并后）愤怒/不耐烦任一落「表达不满」。"""
    marks = MARKERS[: len(opts)]
    if got_marker not in marks:
        return False
    gi = norm_intent(opts[marks.index(got_marker)])
    if gi == gold:
        return True
    return gold in ("表达愤怒", "表达不耐烦") and gi == "表达不满"


# ---------------------------------------------------------------- legs

def leg_server(tag: str) -> None:
    model, port = MODELS[tag]
    url = guarded_url(port)
    lmap = calibrate_letters(url, model)
    inv = {v: k for k, v in lmap.items()}
    if len(lmap) < 12:
        raise SystemExit(f"{tag} 标定不全 {len(lmap)}/12")
    corpus = [("syn", r) for r in build_synthetic()] + [("real", r) for r in build_real()]
    sysmsg = decide_sys(OPTIONS)
    marks = set(MARKERS[:12])
    rows = []
    for corp, r in corpus:
        top = call(url, model, sysmsg, f"客户说：{r['txt']}", 11)
        if not top:
            continue
        got = lmap.get(top[0][0], "?")
        gold = r["gold"]
        ok = gold_ok(gold, got, OPTIONS)
        row = {"corp": corp, "src": r["src"], "txt": r["txt"], "gold": gold,
               "got": got, "ok": ok,
               "old_margin": top[0][1] - top[1][1],
               "top1_lp": top[0][1]}
        found = {lmap.get(t): lp for t, lp in top if lmap.get(t) in marks}
        cov = len(found)
        sm = softmax(found) if found else {}
        order = sorted(sm.items(), key=lambda kv: kv[1], reverse=True)
        p1 = order[0][1] if order else 0.0
        p2 = order[1][1] if len(order) > 1 else 0.0
        z_f = sum(math.exp(v) for v in found.values())
        lmin = min(found.values()) if found else -30.0
        miss_bound = (12 - cov) * math.exp(lmin) / z_f if z_f > 0 else 1.0
        row.update({"sv_p1": p1, "sv_pm": p1 - p2, "cov": cov,
                    "miss_bound": miss_bound,
                    "other": got == MARKERS[OPTIONS.index("其他")]})
        rows.append(row)
        print(f"[{tag}/{corp}] got={got} gold={gold} ok={ok} "
              f"old_margin={row['old_margin']:.2f} sv_pm={row['sv_pm']:.3f} cov={cov}",
              flush=True)
    out = HERE / f".t56r1_server_{tag}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {out} ({len(rows)} rows)")


def leg_inproc() -> None:
    import mlx.core as mx
    from mlx_lm import load
    model, tok = load(M4B)
    letter_ids = {m: tok.encode(m, add_special_tokens=False)[0] for m in MARKERS}
    for m, i in letter_ids.items():
        assert len(tok.encode(m, add_special_tokens=False)) == 1, f"{m} 非单 token"
    id2m = {v: k for k, v in letter_ids.items()}

    corpus = [("syn", r) for r in build_synthetic()] + [("real", r) for r in build_real()]
    all_rows = {}
    for vname, opts in VARIANTS.items():
        sysmsg = decide_sys(opts)
        marks = MARKERS[: len(opts)]
        rows = []
        for corp, r in corpus:
            msgs = [{"role": "system", "content": sysmsg},
                    {"role": "user", "content": f"客户说：{r['txt']}"}]
            # Qwen3.5 混合推理模板默认接 <think>（不关=预测思考内容位，与服务端不一致）
            prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True,
                                             enable_thinking=False)
            ids = tok.encode(prompt)
            lg = model(mx.array([ids]))[0, -1, :]
            mx.eval(lg)               # 强制求值（逐元素 float() 会反复触发惰性图）
            lgv = lg.tolist()         # C 级整行转换，勿逐元素 float()
            srt = sorted(lgv, reverse=True)
            old_margin_ip = srt[0] - srt[1]
            letter_vals = {m: lgv[letter_ids[m]] for m in marks}
            sm = softmax(letter_vals)
            order = sorted(sm.items(), key=lambda kv: kv[1], reverse=True)
            got = order[0][0]
            gold = r["gold"]
            ok = gold_ok(gold, got, opts)
            rows.append({"corp": corp, "src": r["src"], "txt": r["txt"], "gold": gold,
                         "got": got, "ok": bool(ok),
                         "ip_old_margin": old_margin_ip,
                         "p1": order[0][1], "pm": order[0][1] - order[1][1],
                         "top2": order[1][0],
                         "other": got == marks[-1]})
        all_rows[vname] = rows
        print(f"[inproc {vname}] n={len(rows)} acc={sum(r['ok'] for r in rows)}/{len(rows)}",
              flush=True)
    out = HERE / ".t56r1_inproc_4B.json"
    out.write_text(json.dumps(all_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {out}")


def quartile(rows: list[dict], key: str) -> str:
    vals = sorted(rows, key=lambda r: r[key])
    n = len(vals)
    q = max(1, n // 4)
    segs = []
    for i in range(0, n, q):
        seg = vals[i:i + q]
        if not seg:
            continue
        errs = sum(1 for r in seg if not r["ok"])
        segs.append(f"[{seg[0][key]:.2f},{seg[-1][key]:.2f}]{errs}/{len(seg)}错")
    return "  ".join(segs)


def combined(rows: list[dict], key: str, frac: float = 0.3) -> dict:
    n = len(rows)
    k = max(1, int(n * frac))
    thr = sorted(r[key] for r in rows)[k - 1]
    fired = [r for r in rows if r[key] <= thr or r["other"]]
    err = sum(1 for r in rows if not r["ok"])
    caught = sum(1 for r in fired if not r["ok"])
    return {"n": n, "err": err, "fired": len(fired), "caught": caught,
            "lost": len(fired) - caught,
            "prec": caught / len(fired) if fired else 0.0}


def leg_analyze() -> None:
    print("=" * 30, "R1 真实集：margin 工作曲线与组合规则", "=" * 30)
    for tag in ("4B", "9B"):
        p = HERE / f".t56r1_server_{tag}.json"
        if not p.exists():
            continue
        rows = json.load(open(p, encoding="utf-8"))
        real = [r for r in rows if r["corp"] == "real"]
        syn = [r for r in rows if r["corp"] == "syn"]
        for name, rs in (("真实集", real), ("合成集", syn)):
            acc = sum(r["ok"] for r in rs)
            print(f"\n### {tag} {name} n={len(rs)} 命中 {acc}/{len(rs)} = {acc/len(rs):.0%}")
            print("  margin 四位工作曲线:", quartile(rs, "old_margin"))
            c = combined(rs, "old_margin")
            print(f"  组合规则（margin≤30分位 OR 其他）: 触发 {c['fired']}/{c['n']} "
                  f"捕获 {c['caught']}/{c['err']} ({c['caught']/max(1,c['err']):.0%}) "
                  f"误伤 {c['lost']} 精确率 {c['prec']:.0%}")
    print("\n" + "=" * 30, "T5 选项 softmax 后 margin 变真概率差（4B 进程内精确）", "=" * 30)
    p = HERE / ".t56r1_inproc_4B.json"
    if p.exists():
        allrows = json.load(open(p, encoding="utf-8"))
        v0 = allrows["V0"]
        for corp in ("real", "syn"):
            rs = [r for r in v0 if r["corp"] == corp]
            if not rs:
                continue
            acc = sum(r["ok"] for r in rs)
            print(f"\n### 4B(inproc) {corp} n={len(rs)} 命中 {acc}/{len(rs)} = {acc/len(rs):.0%}")
            print("  旧 margin（logit 差，未归一化口径）:", quartile(rs, "ip_old_margin"))
            print("  新 p-margin（选项 softmax 概率差）:", quartile(rs, "pm"))
            c_old = combined(rs, "ip_old_margin")
            c_new = combined(rs, "pm")
            print(f"  组合规则 旧: 触发 {c_old['fired']} 捕获 {c_old['caught']}/{c_old['err']} "
                  f"误伤 {c_old['lost']} 精确率 {c_old['prec']:.0%}")
            print(f"  组合规则 新: 触发 {c_new['fired']} 捕获 {c_new['caught']}/{c_new['err']} "
                  f"误伤 {c_new['lost']} 精确率 {c_new['prec']:.0%}")
            # 触发集重合度
            def fired_set(rs, key):
                s = sorted(rs, key=lambda r: r[key])
                k = max(1, int(len(rs) * 0.3))
                thr = s[k - 1][key]
                return {id(r) for r in rs if r[key] <= thr or r["other"]}
            a, b = fired_set(rs, "ip_old_margin"), fired_set(rs, "pm")
            inter = len(a & b)
            uni = len(a | b)
            print(f"  触发集 Jaccard(旧,新) = {inter}/{uni} = {inter/uni:.0%}")
            pms = [r["pm"] for r in rs]
            print(f"  p-margin 范围 [{min(pms):.3f},{max(pms):.3f}]（应 ⊂ [0,1)）")
    print("\n" + "=" * 30, "T6 表达愤怒选项三变体", "=" * 30)
    if p.exists():
        allrows = json.load(open(p, encoding="utf-8"))
        for vname, rows in allrows.items():
            syn = [r for r in rows if r["corp"] == "syn"]
            real = [r for r in rows if r["corp"] == "real"]
            sacc = sum(r["ok"] for r in syn)
            racc = sum(r["ok"] for r in real)
            # 污染度 = 被选到「愤怒/不满」家（D）的次数；V1 的 D=表达不满（合并项，算吸收）
            poll = sum(1 for r in real if r["got"] == "D")
            poll_syn = sum(1 for r in syn if r["got"] == "D" and r["gold"] != "表达愤怒")
            s4 = [r for r in syn if r["gold"] == "表达愤怒"]
            s4v = f"{sum(1 for r in s4 if r['got'] == 'D')}/{len(s4)}判愤怒/不满"
            ex = [r["txt"][:22] for r in real if r["got"] == "D"][:4]
            print(f"  {vname}: 合成 acc={sacc}/{len(syn)}  句4 {s4v}  "
                  f"合成非愤怒误选D {poll_syn}  真实 acc={racc}/{len(real)}  "
                  f"真实集D被选 {poll}  例={ex}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leg", required=True, choices=["server", "inproc", "analyze"])
    ap.add_argument("--tag", default="4B", choices=["4B", "9B"])
    a = ap.parse_args()
    if a.leg == "server":
        leg_server(a.tag)
    elif a.leg == "inproc":
        leg_inproc()
    else:
        leg_analyze()


if __name__ == "__main__":
    main()
