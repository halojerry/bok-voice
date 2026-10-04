#!/usr/bin/env python3
"""粤语/普通话 CSC（拼写纠错）自训数据挖掘管道（2026-09-27）。

只做**数据管道**，不跑训练。产出 JSONL 训练对给 char-level MLM 双头模型
（检测+纠正，等长输出）；配方见任务背景：**粤语混淆集只含 Jyutping 同音**、
**正确粤语句当强负样本（identity）占比 ≥30-50%**、总规模 ≥5 万对。

## 三条数据源

- **源 A `inject`（主料）**：干净话术/QA 句 → 按频率加权灌 1-2 处音近错。
  - 词级：直接用仓内变体表 `packages/core/bok_voice_core/assets/asr_variants.json`
    （reverse：变体 → 正确词），命中即替换——运营域词高频错（順豐→順風、京東→京冬）。
  - 字级（dev 扩展，**可选**）：`/tmp/homophone-research/rime_char.csv` 反查
    **Jyutping 同音字**（粤语车道）；`pypinyin` 的 `pinyin_dict.json` 反查
    **普通话同音字**（zh 车道）。**粤语车道绝不使用普通话拼音**——`係/系`
    粤语同音、普通话不同音，混用正是「粤→普漂移」的根因。缺数据时只跑词级，
    零第三方依赖仍可用。
- **源 B `identity`（强负样本）**：干净句直接 `(src==tgt)` 成对；粤语（含特征字）
  占 identity 的 **≥40%**（默认按 50% 配），治模型把正确粤语也改坏。
- **源 C `replay`（真实回灌）**：扫 app-data 业务库 `turns` 账本里客户轮 raw 文本，
  用运行时同款 `asr_polish.polish_transcript` 跑一遍，`edits` 非空即采
  `(raw, polished)`。库不存在/无命中 → 打印 `source c skipped: no ledger`，不硬造。

## 硬约束（数字零降级铁律）

数字串/中文数字词句：注入器一律绕开**冻结区间**，`import` 运行时同款冻结正则
（`asr_polish._protected_spans` / `_DIGIT_RUN_RE` / `_CN_NUM_RUN_RE`，
**不复制**），落盘前再用 `_num_signature` 复核 src/tgt 数字段一致。

## 输出（默认 ``data/csc/``）

- ``csc_train.jsonl`` —— 全量训练对（大文件，gitignore）。
- ``sample.jsonl`` —— 前 ≤``--sample-limit`` 行样例（进仓）。
- ``csc_testset.json`` —— 从 ``/tmp/csc/testset.json`` 拷来的种子评测集（进仓）。
- ``csc_stats.json`` —— 统计（大文件旁挂；gitignore）。

## dev 依赖（独立 venv，**不要装进仓的 .venv312**；同 build_asr_variants 惯例）

    python3.12 -m venv /tmp/homophone-research/.venv
    /tmp/homophone-research/.venv/bin/pip install pypinyin pycorrector opencc-python-reimplemented
    /tmp/homophone-research/.venv/bin/python scripts/seed/prepare_csc_data.py --count 50000

无 dev 依赖也能跑（仅词级注入 + identity + replay），条数/占比断言不受影响。

## 数据来源与许可

- 变体表：见 ``asr_variants.json`` meta.sources（ToJyutping BSD-2-Clause /
  rime-cantonese rime_char.csv CC-BY-4.0 需署名 / pypinyin MIT /
  pycorrector Apache-2.0 / OpenCC Apache-2.0）。
- 仓内话术种子：``scripts/seed/seed_invite_templates.py``（本项目自产）。
- 业务库 turns / qa_entries / conversation_templates：**只读**，本机运行数据。
- 内置域句种子：本脚本自产（见 ``EMBEDDED_SEED_*``，覆盖快递理赔/邀约/售后域）。

用法示例：

    .venv312/bin/python scripts/seed/prepare_csc_data.py --count 500 --seed 7 --stats --no-db
    /tmp/homophone-research/.venv/bin/python scripts/seed/prepare_csc_data.py --count 50000
"""

from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import argparse
import ast
import csv
import datetime as _dt
import importlib.util
import json
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_CORE = ROOT / "packages" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

# 冻结正则/冻结区间/粤语特征字/纠错核心 = 运行时单源,只 import 不复制。
from bok_voice_core.asr_polish import (  # noqa: E402
    _CN_NUM_RUN_RE,
    _DIGIT_RUN_RE,
    _CANTONESE_MARKERS,
    _overlaps,
    _protected_spans,
    detect_lane,
    load_variant_table,
    polish_transcript,
)

# 输出 lane 只有两态;运行时车道 cantonese ↔ 本管道 yue 的映射单点。
LANE_ZH = "zh"
LANE_YUE = "yue"
_LANE_TO_POLISH = {LANE_ZH: "zh", LANE_YUE: "cantonese"}

DEFAULT_OUT_DIR = ROOT / "data" / "csc"

# ---------------------------------------------------------------------------
# ref 解析单源(2026-10-04 C1):正则三件套改吃 bok_voice_core.branch_syntax
# ——此前本文件自持拷贝(且先一步收了繁体锚),与运行时 flow.py 口径漂移:
# 训练料认「如果客戶」、运行时静默丢弃。现在四处(flow.py/flow-canvas.ts/
# gap_proposals.py/本文件)同源,解析行为逐字节一致。
# ---------------------------------------------------------------------------
from bok_voice_core.branch_syntax import (  # noqa: E402
    BRANCH_ACTION_RE as _BRANCH_ACTION_RE,
    BRANCH_LINE_RE as _BRANCH_LINE_RE,
    NOTE_LINE_RE as _NOTE_LINE_RE,
)

# 含占位符的句子(模板变量未渲染)不进干净语料。
_PLACEHOLDER_RE = re.compile(r"[{}]")
# 句末切分:QA 长答案按句拆,提升语料粒度。
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")

_MIN_SENT_HAN = 4  # 少于 4 个汉字的碎片不入料(短应承/噪声)

# ---------------------------------------------------------------------------
# 内置域句种子(≥300 句;仓库内无足够模板 JSON / 无 DB 时的保底源)。
# 来源:本脚本自产。覆盖快递理赔/平台核实/赔偿方案/邀约回访/售后收尾域,
# 每语言各自把 21 个变体表种子词织进句面,保证零依赖下词级注入有命中面。
# 粤语句全部含粤语特征字(件 B identity 的「含特征字」判据)。
# ---------------------------------------------------------------------------

EMBEDDED_SEED_ZH: list[str] = [
    "您好，请问是陈先生本人吗？",
    "您好，我这边是快递公司的客服专员。",
    "请问您现在方便接听电话吗？",
    "我这边有一件包裹的情况需要跟您核对一下。",
    "麻烦您先确认一下您的姓名，谢谢。",
    "不好意思打扰您，占用您一两分钟时间。",
    "您好，我们这边是售后服务中心。",
    "请问您是机主本人吗？",
    "您之前在我们平台购买过一件商品，对吗？",
    "我们收到反馈说您的包裹出现了异常。",
    "请问您这件商品是在哪个平台购买的？",
    "是拼多多、淘宝、京东还是其他平台呢？",
    "您可以打开订单页面看一下状态。",
    "订单显示已发货但是还没有签收，对吗？",
    "麻烦您确认一下下单时用的是哪个账号。",
    "这件包裹是通过圆通快递发出的。",
    "物流信息显示包裹在中转仓库停留了好几天。",
    "您的快件目前显示运输途中。",
    "单号尾号是四七八二的那件包裹出了问题。",
    "请问您还记得当时买的是什么商品吗？",
    "如果记不清也没关系，我们可以帮您核对。",
    "您可以看一下最近的购物订单记录。",
    "这笔订单的支付金额是三百八十元。",
    "您的收货地址是在广州市天河区，对吗？",
    "麻烦您核对一下收件人的手机号码。",
    "这次确实是我们的责任，给您添麻烦了。",
    "我们会按照平台的赔付标准帮您处理。",
    "您这件商品的赔偿方案可以走快速通道。",
    "金额不足一百元的，可以申请三百到六百元的赔偿。",
    "金额超过两百元的，按照货价的两到三倍赔偿。",
    "超过一千元的订单，我们还会额外给一份保险补偿。",
    "具体的理赔金额需要先核实订单信息。",
    "我们会在核实清楚之后尽快安排赔付。",
    "这个赔偿方案您觉得可以接受吗？",
    "如果对金额有疑问，您可以随时提出来。",
    "我们会尽量帮您争取最高的赔付标准。",
    "理赔流程需要您配合提交订单截图。",
    "审核通过后，赔款会原路退回您的账户。",
    "退款一般会在三到五个工作日内到账。",
    "如果您想退货，我们也可以协助办理。",
    "我完全理解您的心情，确实是我们做得不到位。",
    "您先别着急，我们一定会负责到底。",
    "对于给您造成的不便，我们再次表示歉意。",
    "您的投诉我们已经记录下来了。",
    "我们会把您的问题反馈给专门的售后团队。",
    "请您放心，我们不是诈骗电话。",
    "我们是正规的快递公司，您可以回拨官方热线核实。",
    "如果您担心安全问题，可以自己联系官方客服。",
    "这个问题我帮您转接给人工专员处理。",
    "稍等，我帮您查询一下最新的处理进度。",
    "后续理赔需要专员加您的微信对接。",
    "麻烦您把微信号报给我，我让专员加您。",
    "您方便留一个微信或者电话吗？",
    "专员会通过微信联系您，请您留意好友申请。",
    "您添加之后把订单截图发给他就可以了。",
    "我们不会向您索要验证码或者银行卡密码。",
    "这个号码只用来发送预约确认，请您放心。",
    "请您保存一下我的工单编号。",
    "收到您的号码了，我马上提交给专员。",
    "大约一分钟左右专员就会联系您。",
    "我们想邀请您参加一次免费的延保回访服务。",
    "工程师会上门帮您做一次免费的检测。",
    "整个过程大概二十分钟，不会耽误您太多时间。",
    "这次回访是完全免费的，不收任何费用。",
    "您这周哪天比较方便，我们好安排时间。",
    "周末两天我们都可以安排工程师上门。",
    "如果最近没空，我们可以约在下周。",
    "上门之前工程师会提前给您打电话。",
    "这次服务包括免费更换一些易损配件。",
    "您家里的电器使用还正常吗？",
    "好的，那就不打扰您了，祝您生活愉快。",
    "感谢您的配合，再见。",
    "如果还有什么问题，您可以随时联系我们。",
    "这边就先这样，我稍后把信息发给您。",
    "好的，我记下来了，您放心。",
    "麻烦您保持电话畅通，谢谢。",
    "祝您工作顺利，再见。",
    "那我们就约好了，到时见。",
    "好的，收到，我这边马上处理。",
    "感谢您接听我们的电话。",
    "您的包裹在分拣中心出现了破损。",
    "派件员反馈联系不上收件人。",
    "这件快件被错分到了别的城市。",
    "我们正在联系仓库查找这件货。",
    "如果确认丢失，我们会全额赔付。",
    "您可以选择重新发货或者直接退款。",
    "重新购买的话，差价由我们承担。",
    "我们会补发一件全新的同款商品。",
    "这个单号在系统里查不到记录。",
    "请您提供一下当时的下单凭证。",
    "平台客服已经介入了这笔纠纷。",
    "我们和平台核实过，情况属实。",
    "商家同意按照平台规则赔付。",
    "保险理赔需要等定损结果出来。",
    "您的账户信息我们会严格保密。",
    "所有流程都可以在官方渠道查询到。",
    "请不要向陌生链接转账或者付款。",
    "遇到可疑情况请第一时间报警。",
    "我们这边是正规注册的公司。",
    "您可以拨打官方客服电话核实身份。",
    "请问您的订单编号是多少？",
    "我帮您查一下物流的最新状态。",
    "这件包裹目前显示已签收。",
    "签收人写的是前台代收。",
    "如果您没有收到，我们马上跟进。",
    "快递员说放在了小区的快递柜。",
    "您可以去快递柜取一下件。",
    "取件码稍后会发到您的手机上。",
    "如果柜子已经满了，会转到驿站。",
    "驿站的工作时间是早上八点到晚上八点。",
    "您的地址填写可能有点问题。",
    "麻烦您确认一下门牌号码。",
    "派送范围暂时覆盖不到这个区域。",
    "我们可以改成自提或者转寄。",
    "转寄会产生一笔额外的费用。",
    "这笔费用可以先由我们垫付。",
    "您同意的话我现在就安排。",
    "好的，我已经帮您备注了。",
    "系统显示这笔订单已经完成。",
    "后续如果有问题可以再联系我。",
    "这个商品的保修期是一年。",
    "保修期内可以免费维修。",
    "人为损坏的话需要自费更换配件。",
    "您可以先预约一个上门检测。",
    "检测结果出来我们会通知您。",
    "如果配件缺货，可能需要等几天。",
    "到货之后会第一时间联系您。",
    "我们会尽量缩短您的等待时间。",
    "服务完成后请您给个评价。",
    "您的满意是我们最大的动力。",
    "您好，请问您现在说话方便吗？",
    "我这边信号可能有点不好，请您见谅。",
    "麻烦您再说一遍，刚才没听清楚。",
    "不好意思，我这边有点吵。",
    "请您稍微大声一点，谢谢。",
    "好的，我听清楚了。",
    "那我先跟您确认几个信息。",
    "请问您的姓名怎么称呼？",
    "方便留一个联系方式吗？",
    "我们稍后会把结果通知给您。",
    "这次是系统故障导致包裹延误。",
    "我们已经紧急安排了加急处理。",
    "最快明天就能送到您手上。",
    "给您造成的不便我们深表歉意。",
    "我们会在二十四小时内给您答复。",
    "如果超时没有处理，您可以投诉我们。",
    "投诉电话在我们的官网上可以查到。",
    "我们会认真对待每一条反馈。",
    "感谢您对我们工作的理解。",
    "祝您身体健康，万事如意。",
    "请问您是想咨询退款还是退货？",
    "退款会在审核通过后原路返回。",
    "退货需要保持商品包装完整。",
    "您可以把商品寄回我们的仓库。",
    "寄回的运费由我们承担。",
    "收到退货后我们会尽快办理退款。",
    "请您保留好寄件的单据。",
    "有问题随时联系在线客服。",
    "我们的工作时间是每天九点到十八点。",
    "感谢您的来电，再见。",
    "这件包裹是顺丰快递送来的。",
    "您也可以在中通或者韵达的官网查询。",
    "邮政的件一般会送到附近的驿站。",
    "您是在抖音还是淘宝下单的？",
    "我们和中通、圆通、韵达都有合作。",
    "顺丰的单号一般以字母开头。",
]

EMBEDDED_SEED_YUE: list[str] = [
    "你好，請問你係咪陳先生本人？",
    "你好，我哋係快遞公司嘅客服專員。",
    "請問你而家方便聽電話嗎？",
    "我哋呢邊有一件貨件嘅情況想同你核對下。",
    "麻煩你確認一下你嘅姓名，唔該。",
    "唔好意思打擾你，佔你一分鐘時間。",
    "你好，我哋係售後服務中心。",
    "請問你係咪機主本人？",
    "你之前喺我哋平台買過一件貨品，啱唔啱？",
    "我哋收到通知話你件貨件有問題。",
    "請問你件貨品係喺邊個平台買嘅？",
    "係拼多多、淘寶、京東定係第啲平台？",
    "你可以打開訂單睇下個狀態。",
    "訂單顯示已發貨但係未簽收，啱唔啱？",
    "麻煩你確認一下落單用邊個帳號。",
    "呢件貨件係經順豐快遞寄出嘅。",
    "物流顯示件貨喺中轉倉庫停咗幾日。",
    "你嘅快件而家顯示運輸途中。",
    "單號尾號四七八二嗰件貨出咗問題。",
    "請問你仲記唔記得當時買咗咩貨品？",
    "記唔清都唔緊要，我哋可以幫你核對。",
    "你可以睇下最近嘅購物訂單記錄。",
    "呢單訂單嘅支付金額係三百八十蚊。",
    "你嘅收貨地址係喺廣州市天河區，啱唔啱？",
    "麻煩你核對一下收件人嘅手機號碼。",
    "今次真係我哋嘅責任，為你添麻煩喇。",
    "我哋會按平台嘅賠付標準幫你處理。",
    "你呢件貨品嘅賠償方案可以走快線。",
    "金額唔夠一百蚊嘅，可以申請三百到六百蚊賠償。",
    "金額超過兩百蚊嘅，按貨價兩到三倍賠償。",
    "超過一千蚊嘅訂單，我哋會額外俾一份保險補償。",
    "具體嘅理賠金額要先核實訂單資料。",
    "我哋會喺核實清楚之後盡快安排賠付。",
    "呢個賠償方案你覺得接唔接受？",
    "如果對金額有疑問，你可以隨時提出。",
    "我哋會盡量幫你爭取最高嘅賠付標準。",
    "理賠流程需要你配合提交訂單截圖。",
    "審核通過之後，賠款會原路退返你戶口。",
    "退款一般會喺三到五個工作日內到帳。",
    "如果你想退貨，我哋都可以幫你辦理。",
    "我完全明白你嘅心情，的確係我哋做得唔好。",
    "你唔好急住，我哋一定會負責到底。",
    "為你帶來嘅不便，我哋再次講聲唔好意思。",
    "你嘅投訴我哋已經記錄落嚟。",
    "我哋會將你嘅問題轉俾專門嘅售後團隊。",
    "你放心，我哋唔係詐騙電話。",
    "我哋係正規快遞公司，你可以回撥官方熱線核實。",
    "如果你擔心安全，可以自己聯絡官方客服。",
    "呢個問題我幫你轉接俾人工專員處理。",
    "等一等，我幫你查下最新嘅處理進度。",
    "之後理賠需要專員加你嘅微信對接。",
    "麻煩你將微信號報俾我，我哋叫專員加你。",
    "你方便留一個微信或者電話嗎？",
    "專員會經微信聯絡你，請你留意好友申請。",
    "你加咗之後將訂單截圖發俾佢就得。",
    "我哋唔會向你攞驗證碼或者銀行卡密碼。",
    "呢個號碼淨係用嚟發預約確認，你放心。",
    "請你保存一下我嘅工單編號。",
    "收到你嘅號碼喇，我即刻提交俾專員。",
    "大約一分鐘左右專員就會聯絡你。",
    "我哋想邀請你參加一次免費嘅延保回訪。",
    "工程師會上門幫你做一次免費檢測。",
    "整個過程大概二十分鐘，唔會耽誤你太多時間。",
    "今次回訪係完全免費嘅，唔收任何費用。",
    "你今個禮拜邊日比較方便，我哋好安排時間。",
    "禮拜六禮拜日我哋都可以安排工程師上門。",
    "如果最近冇時間，我哋可以約喺下個禮拜。",
    "上門之前工程師會提早打電話俾你。",
    "今次服務包括免費更換一啲易損配件。",
    "你屋企嘅電器用起上嚟仲正常嗎？",
    "好喇，咁就唔打擾你喇，祝你生活愉快。",
    "多謝你嘅配合，再見。",
    "如果仲有咩問題，你可以隨時聯絡我哋。",
    "呢邊就先咁樣，我遲啲將資料發俾你。",
    "好嘅，我記低喇，你放心。",
    "麻煩你保持電話暢通，唔該。",
    "祝你工作順利，再見。",
    "咁我哋就約好喇，到時見。",
    "好嘅，收到，我即刻處理。",
    "多謝你聽我哋嘅電話。",
    "你件貨件喺分揀中心有破損。",
    "派件員話聯絡唔到收件人。",
    "呢件快件畀錯分咗去第二個城市。",
    "我哋而家聯絡緊倉庫搵呢件貨。",
    "如果確認唔見咗，我哋會全額賠付。",
    "你可以選擇重新發貨或者直接退款。",
    "重新買嘅話，差價由我哋承擔。",
    "我哋會補發一件全新嘅同款貨品。",
    "呢個單號喺系統度查唔到記錄。",
    "請你提供返當時嘅落單憑證。",
    "平台客服已經介入呢單糾紛。",
    "我哋同平台核實過，情況係真嘅。",
    "商家同意按平台規則賠付。",
    "保險理賠要等定損結果出嚟。",
    "你嘅帳戶資料我哋會嚴格保密。",
    "所有流程都可以喺官方渠道查到。",
    "請你唔好向陌生連結轉帳或者付款。",
    "遇到可疑情況請第一時間報警。",
    "我哋呢邊係正規註冊嘅公司。",
    "你可以打官方客服電話核實身份。",
    "請問你嘅訂單編號係幾多？",
    "我幫你查下物流嘅最新狀態。",
    "呢件貨件而家顯示已簽收。",
    "簽收人寫嘅係前台代收。",
    "如果你冇收到，我哋即刻跟進。",
    "快遞員話放咗喺屋苑嘅快遞櫃。",
    "你可以去快遞櫃攞返件貨。",
    "取件碼遲啲會發到你手機。",
    "如果櫃滿咗，會轉去驛站。",
    "驛站嘅工作時間係朝早八點到夜晚八點。",
    "你嘅地址可能填得有啲問題。",
    "麻煩你確認一下門牌號碼。",
    "派送範圍暫時覆蓋唔到呢個區域。",
    "我哋可以改成自提或者轉寄。",
    "轉寄會產生一筆額外費用。",
    "呢筆費用可以先由我哋墊付。",
    "你同意嘅話我而家就安排。",
    "好嘅，我已經幫你備註咗。",
    "系統顯示呢單訂單已經完成。",
    "之後如果有問題可以再聯絡我。",
    "呢件貨品嘅保養期係一年。",
    "保養期內可以免費維修。",
    "人為損壞嘅話要自費更換配件。",
    "你可以先預約一個上門檢測。",
    "檢測結果出嚟我哋會通知你。",
    "如果配件缺貨，可能要等幾日。",
    "到貨之後會第一時間聯絡你。",
    "我哋會盡量縮短你嘅等候時間。",
    "服務完成之後請你畀個評價。",
    "你滿意就係我哋最大嘅動力。",
    "你好，請問你而家講嘢方便嗎？",
    "我哋呢邊信號可能麻麻，請你見諒。",
    "麻煩你再講一次，啱先聽唔清楚。",
    "唔好意思，我哋呢邊有啲嘈。",
    "請你講大聲少少，唔該。",
    "好嘅，我聽清楚喇。",
    "咁我先同你確認幾個資料。",
    "請問你點稱呼？",
    "方便留一個聯絡方式嗎？",
    "我哋遲啲會將結果通知你。",
    "今次係系統故障搞到貨件延誤。",
    "我哋已經緊急安排加急處理。",
    "最快聽日就可以送到你手上。",
    "為你帶來嘅不便我哋深表歉意。",
    "我哋會喺二十四小時內答覆你。",
    "如果超時都未處理，你可以投訴我哋。",
    "投訴電話喺我哋官網度查到。",
    "我哋會認真對待每一條反饋。",
    "多謝你對我哋工作嘅理解。",
    "祝你身體健康，萬事如意。",
    "請問你係想查詢退款定係退貨？",
    "退款會喺審核通過之後原路退返。",
    "退貨需要保持貨品包裝完整。",
    "你可以將貨品寄返我哋倉庫。",
    "寄返嘅運費由我哋承擔。",
    "收到退貨之後我哋會盡快辦理退款。",
    "請你保留好寄件嘅單據。",
    "有問題隨時聯絡在線客服。",
    "我哋嘅工作時間係每日九點到十八點。",
    "多謝你嘅來電，再見。",
    "你嘅微信號係咪呢個？",
    "我哋專員會隔一陣加你，你留意下。",
    "如果收唔到好友申請，你可以再打俾我。",
    "你唔使擔心，手續好簡單。",
    "一步一步跟你講，你照做就得。",
    "你嘅單號我哋已經登記好喇。",
    "核對冇問題嘅話，賠款好快就到。",
    "有咩唔明隨時問我，唔好客氣。",
    "我哋會跟進到你收到賠款為止。",
    "唔該晒你，祝你一切順利。",
    "呢件貨係中通快遞送嘅，你可以查下單號。",
    "韻達同郵政嘅件都會經我哋中轉。",
    "你係喺抖音買定係淘寶買嘅？",
]


# ---------------------------------------------------------------------------
# 干净句抽取
# ---------------------------------------------------------------------------

def _strip_branch_action(resp: str) -> str:
    """剥分支应答首部的【收线】/【转人工】等引擎标记(镜像 parse_branch_action)。"""
    return _BRANCH_ACTION_RE.sub("", str(resp or "")).strip()


def extract_ref_sentences(ref: str) -> list[str]:
    """从一个 step.ref 抽「正稿 + 分支应答」两句面(跳过注意行/占位符句)。

    正稿 = 首个非空非分支非注意行(镜像 flow.parse_step_ref);分支应答按
    「如果客户X→Y」命中。注意行是操作性事实,不是要说的话,不入料。
    """
    out: list[str] = []
    script = ""
    for raw in str(ref or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _BRANCH_LINE_RE.match(line)
        if m:
            resp = _strip_branch_action(m.group("resp"))
            if resp:
                out.append(resp)
            continue
        if _NOTE_LINE_RE.match(line):
            continue
        if not script:
            script = line
    if script:
        out.insert(0, script)
    return out


def _clean_sentence(text: str, lane: str) -> str | None:
    """单句清洗:去空白、丢占位符/无汉字/过短碎句。返回 None 表示丢弃。

    语言门:zh 车道丢弃含粤语特征字的句子(防普通话语料混入粤语——正是
    「普通话模型漂移」要治的那类污染);yue 车道不因缺特征字丢句(口语粤语
    也常无显式特征字),「identity 必须含特征字」的占比要求改由**选样池**
    (见 build_dataset 的 yue_marker_pool)保证。
    """
    s = re.sub(r"\s+", "", str(text or "")).strip()
    if not s or _PLACEHOLDER_RE.search(s):
        return None
    if len(_HAN_RE.findall(s)) < _MIN_SENT_HAN:
        return None
    if lane == LANE_ZH and any(c in _CANTONESE_MARKERS for c in s):
        return None
    return s


def _split_long(text: str) -> list[str]:
    """把长答案按句末标点拆成句,提升粒度(短问句原样)。"""
    parts = [p for p in _SENT_SPLIT_RE.split(str(text or "")) if p.strip()]
    return parts or [str(text or "")]


# ---------------------------------------------------------------------------
# 源:仓内种子话术(scripts/seed/seed_invite_templates.py 的 TEMPLATES 字面量)
# ---------------------------------------------------------------------------

def load_repo_seed_sentences() -> list[tuple[str, str]]:
    """返回 [(lane, sentence)];仅 zh/cantonese 模板,en 车道本管道不产。"""
    path = ROOT / "scripts" / "seed" / "seed_invite_templates.py"
    if not path.exists():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    templates = None
    for node in tree.body:
        # 兼容 `TEMPLATES = [...]` 与带类型注解 `TEMPLATES: list[dict] = [...]`。
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id == "TEMPLATES" and value is not None:
            try:
                templates = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                templates = None
    if not isinstance(templates, list):
        return []
    out: list[tuple[str, str]] = []
    for tpl in templates:
        if not isinstance(tpl, dict):
            continue
        lang = str(tpl.get("language") or "")
        lane = LANE_YUE if lang == "cantonese" else (LANE_ZH if lang == "zh" else None)
        if lane is None:
            continue
        for step in tpl.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for sent in extract_ref_sentences(str(step.get("ref") or "")):
                cs = _clean_sentence(sent, lane)
                if cs:
                    out.append((lane, cs))
        # 模板级字段(可能为空)
        for key in ("opening", "core", "objection", "closing"):
            for chunk in _split_long(str(tpl.get(key) or "")):
                cs = _clean_sentence(chunk, lane)
                if cs:
                    out.append((lane, cs))
    return out


# ---------------------------------------------------------------------------
# 源:业务库(只读)conversation_templates / qa_entries
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    """app-data 业务库路径(镜像 tools/bok.py app_data_dir;不 import tools)。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    else:
        base = Path(
            os.environ.get("XDG_DATA_HOME")
            or str(Path(os.environ.get("HOME", ".")) / ".local" / "share")
        )
    return base / "BokVoice" / "bok_voice.db"


def _open_ro(db_path: Path, timeout: float) -> sqlite3.Connection:
    """只读打开业务库(URI mode=ro),绝不写。"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def load_db_sentences(db_path: Path, timeout: float = 5.0) -> list[tuple[str, str]]:
    """从业务库抽干净句(模板 steps/字段 + QA 问/答);失败/缺表一律返回 [].

    关于「人设 greeting」:``persona_profiles`` 表**没有 greeting 列**——开场白
    的实际来源是话术模板第 1 步正稿(运行时 ``FlowController.opening_text``),已由
    上面的 conversation_templates.steps_json 覆盖,故不另抽人设字段。
    """
    if not db_path.exists():
        return []
    out: list[tuple[str, str]] = []
    try:
        conn = _open_ro(db_path, timeout)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = conn.execute(
                "SELECT language, steps_json, opening, core, objection, closing "
                "FROM conversation_templates WHERE language IN ('zh','cantonese')"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            lang = str(r["language"] or "")
            lane = LANE_YUE if lang == "cantonese" else LANE_ZH
            steps = []
            try:
                steps = json.loads(str(r["steps_json"] or "") or "[]")
            except (ValueError, TypeError):
                steps = []
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict):
                        for sent in extract_ref_sentences(str(step.get("ref") or "")):
                            cs = _clean_sentence(sent, lane)
                            if cs:
                                out.append((lane, cs))
            for key in ("opening", "core", "objection", "closing"):
                for sent in _split_long(str(r[key] or "")):
                    cs = _clean_sentence(sent, lane)
                    if cs:
                        out.append((lane, cs))
        try:
            qrows = conn.execute(
                "SELECT question_text, answer_text, lang FROM qa_entries "
                "WHERE lang IN ('zh','cantonese') AND enabled=1"
            ).fetchall()
        except sqlite3.Error:
            qrows = []
        for q in qrows:
            lang = str(q["lang"] or "")
            lane = LANE_YUE if lang == "cantonese" else LANE_ZH
            for text in (q["question_text"], q["answer_text"]):
                for sent in _split_long(str(text or "")):
                    cs = _clean_sentence(sent, lane)
                    if cs:
                        out.append((lane, cs))
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# 源 C:真实回灌(只读 turns,运行时同款 polish_transcript)
# ---------------------------------------------------------------------------

def load_replay_pairs(
    db_path: Path,
    variant_table,
    *,
    timeout: float = 5.0,
    limit: int | None = None,
) -> list[tuple[str, str, str]]:
    """扫 turns 客户轮 raw → polish_transcript edits 非空即采 (raw, polished, lane)。

    只读;库缺失/无 turns 表/无命中 → 返回 []。lane 用运行时 detect_lane,
    cantonese 归一到 yue,en 车道丢弃(本管道只做 zh/yue)。
    """
    if not db_path.exists():
        return []
    try:
        conn = _open_ro(db_path, timeout)
    except sqlite3.Error:
        return []
    pairs: list[tuple[str, str, str]] = []
    try:
        try:
            rows = conn.execute(
                "SELECT transcript FROM turns WHERE role='user' AND length(transcript)>=4"
            ).fetchall()
        except sqlite3.Error:
            return []
        seen: set[tuple[str, str]] = set()
        for r in rows:
            raw = str(r["transcript"] or "").strip()
            if not raw:
                continue
            pl = detect_lane(raw)
            if pl == "en":
                continue
            lane = LANE_YUE if pl == "cantonese" else LANE_ZH
            res = polish_transcript(raw, _LANE_TO_POLISH[lane], variant_table)
            if not res.edits or res.text == raw:
                continue
            key = (raw, res.text)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((raw, res.text, lane))
            if limit is not None and len(pairs) >= limit:
                break
    finally:
        conn.close()
    return pairs


# ---------------------------------------------------------------------------
# 字级同音混淆(dev-only:rime = 粤语 Jyutping;pypinyin = 普通话)
# ---------------------------------------------------------------------------

def _try_common_gate() -> tuple[set[str], set[str]]:
    """返回 (common_simp, common_trad);缺 pycorrector/opencc 时返回空集。

    dev-only 门控:只放常用字进来,防注入出现生僻字。不用时零影响。
    """
    simp: set[str] = set()
    trad: set[str] = set()
    try:
        spec = importlib.util.find_spec("pycorrector")
        if spec and spec.origin:
            p = Path(spec.origin).resolve().parent / "data" / "common_char_set.txt"
            if p.exists():
                simp = set(p.read_text(encoding="utf-8").split())
        if simp:
            try:
                import opencc  # type: ignore

                trad = set(opencc.OpenCC("s2t").convert("".join(sorted(simp))))
            except Exception:
                trad = set()
    except Exception:
        pass
    return simp, trad


def build_homophone_index(
    pools: dict[str, list[str]],
    data_dir: Path,
) -> dict[str, dict[str, list[str]]]:
    """构建 ``{lane: {char: [同音候选字...]}}``(dev-only,缺数据即空)。

    粤语:rime_char.csv 反查 **同 Jyutping 音节**(含声调)的字。候选门控 =
    语料池字 ∪ 常用繁体字;**粤语特征字**的候选额外放开(保证 係→系 这类
    同音漂移可注入,同时候选仍全部来自 Jyutping 同音,绝不用普通话拼音)。
    普通话:pypinyin 的 pinyin_dict.json 反查 **同 tone3 拼音** 的字。
    """
    index: dict[str, dict[str, list[str]]] = {LANE_ZH: {}, LANE_YUE: {}}
    common_simp, common_trad = _try_common_gate()
    pool_yue = {c for s in pools.get(LANE_YUE, []) for c in s if _HAN_RE.match(c)}
    pool_zh = {c for s in pools.get(LANE_ZH, []) for c in s if _HAN_RE.match(c)}

    # ---- 粤语:rime_char.csv ----
    char_csv = data_dir / "rime_char.csv"
    if char_csv.exists() and pool_yue:
        c2j: dict[str, list[tuple[str, str]]] = {}
        j2c: dict[str, list[str]] = {}
        try:
            with char_csv.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    ch, jy = row.get("char", ""), row.get("jyutping", "")
                    if not ch or not jy:
                        continue
                    c2j.setdefault(ch, []).append((jy, row.get("pron_rank", "")))
                    j2c.setdefault(jy, []).append(ch)
        except (OSError, csv.Error):
            c2j, j2c = {}, {}
        rank_ok = {"預設", "常用"}
        yue_gate = pool_yue | common_trad
        for ch in pool_yue:
            cands: set[str] = set()
            for jy, rank in c2j.get(ch, []):
                if rank in rank_ok:
                    cands.update(j2c.get(jy, []))
            cands.discard(ch)
            cands = {c for c in cands if c in yue_gate or c in _CANTONESE_MARKERS}
            if cands:
                index[LANE_YUE][ch] = sorted(cands)

    # ---- 普通话:pypinyin pinyin_dict.json ----
    if pool_zh:
        try:
            spec = importlib.util.find_spec("pypinyin")
            dict_path = (
                Path(spec.origin).resolve().parent / "pinyin_dict.json" if spec and spec.origin else None
            )
            from pypinyin.contrib.tone_convert import to_tone3  # type: ignore

            if dict_path and dict_path.exists():
                raw = json.loads(dict_path.read_text(encoding="utf-8"))
                t3_to_chars: dict[str, list[str]] = {}
                for cp, readings in raw.items():
                    ch = chr(int(cp))
                    for rd in str(readings).split(","):
                        t3 = to_tone3(rd)
                        if t3:
                            t3_to_chars.setdefault(t3, []).append(ch)
                zh_gate = pool_zh | common_simp
                # 需要 char -> 其拼音:反查 t3_to_chars 得 chars->t3
                for t3, chars in t3_to_chars.items():
                    allowed = sorted({c for c in chars if c in zh_gate})
                    for c in allowed:
                        index[LANE_ZH][c] = [x for x in allowed if x != c]
        except Exception:
            pass

    return index


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------

def _apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    out = text
    for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + repl + out[end:]
    return out


def _num_signature(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """数字段签名(阿拉伯串 + 中文数字词);src/tgt 必须逐项一致。"""
    return (tuple(_DIGIT_RUN_RE.findall(text)), tuple(_CN_NUM_RUN_RE.findall(text)))


def inject_errors(
    sent: str,
    lane: str,
    rng: random.Random,
    table: dict,
    homophones: dict,
) -> str | None:
    """对干净句灌 1-2 处音近错,返回错句;无法注入返回 None。

    候选分两类:词级(变体表 reverse:正确词→变体)与字级(同音混淆,dev-only)。
    所有候选必须落在**冻结区间之外**;数字段由构造与落盘前复核双重保证。
    """
    polish_lane = _LANE_TO_POLISH[lane]
    spans = _protected_spans(sent, polish_lane)
    ops: list[tuple[int, int, str]] = []

    # 词级:变体表里每个正确词在句中所有出现位置
    for correct, variants in (table.get(polish_lane) or {}).items():
        variants = [v for v in (variants or []) if v and v != correct]
        if not variants:
            continue
        start = 0
        while True:
            j = sent.find(correct, start)
            if j < 0:
                break
            end = j + len(correct)
            if not _overlaps(j, end, spans):
                ops.append((j, end, rng.choice(variants)))
            start = j + 1

    # 字级:同音字
    hp = (homophones or {}).get(lane, {})
    for i, ch in enumerate(sent):
        if not _HAN_RE.match(ch):
            continue
        if _overlaps(i, i + 1, spans):
            continue
        cands = hp.get(ch)
        if cands:
            ops.append((i, i + 1, rng.choice(cands)))

    if not ops:
        return None
    rng.shuffle(ops)
    want = 2 if (len(ops) >= 2 and rng.random() < 0.4) else 1
    chosen: list[tuple[int, int, str]] = []
    used: list[tuple[int, int]] = []
    for op in ops:
        if any(_overlaps(op[0], op[1], [u]) for u in used):
            continue
        chosen.append(op)
        used.append((op[0], op[1]))
        if len(chosen) >= want:
            break
    if not chosen:
        return None
    out = _apply_edits(sent, chosen)
    if out == sent or _num_signature(out) != _num_signature(sent):
        return None
    return out


# ---------------------------------------------------------------------------
# 信道劣化增强(`--augment`,2026-09-27)
# ---------------------------------------------------------------------------
# CSC 是文本→文本,故把**信道**的 ASR 错误形状直接模拟到**输入侧**(目标恒为
# 原始干净句):窄带 → 同音字混淆(8k 电话误听),快语速 → 丢字/叠字(剪裁快语速)。
# 铁律不破:数字串/中文数字词(冻结区间)永不改;粤语特征字永不删。全部确定性
# (由传入 rng 决定),默认 `--augment` 为空 → 本段零调用、输出逐字节同旧。

AUGMENT_KINDS = ("narrowband", "speed")


def parse_augment_kinds(spec: str | None) -> list[str]:
    """解析 `--augment narrowband,speed` → 规范化 kind 列表(去重保序)。

    空/None → [];未知项抛 ValueError(拼错即报错,不静默忽略)。
    """
    out: list[str] = []
    for raw in str(spec or "").split(","):
        k = raw.strip().lower()
        if not k:
            continue
        if k not in AUGMENT_KINDS:
            raise ValueError(
                f"unknown augment kind: {raw!r} (allowed: {', '.join(AUGMENT_KINDS)})"
            )
        if k not in out:
            out.append(k)
    return out


def augment_narrowband(
    sent: str, lane: str, rng: random.Random, homophones: dict
) -> str:
    """窄带同音混淆:非冻结区按声取同音字替换(8k 电话信道 ASR 误听形状)。

    复用仓内同音索引(粤=Jyutping 同音、普=pypinyin 同调同音);只在有同音候选
    的字上替换,数字冻结区一律跳过。无候选/无改动 → 原样返回(调用方据
    ``aug == sent`` 判定是否采纳)。
    """
    polish_lane = _LANE_TO_POLISH[lane]
    spans = _protected_spans(sent, polish_lane)
    hp = (homophones or {}).get(lane, {})
    chars = list(sent)
    positions = [
        i for i, ch in enumerate(chars)
        if _HAN_RE.match(ch) and hp.get(ch) and not _overlaps(i, i + 1, spans)
    ]
    if not positions:
        return sent
    rng.shuffle(positions)
    n = 1 if len(positions) == 1 or rng.random() < 0.6 else 2
    for i in positions[:n]:
        chars[i] = rng.choice(hp[chars[i]])
    out = "".join(chars)
    if out == sent or _num_signature(out) != _num_signature(sent):
        return sent
    return out


def augment_speed(sent: str, lane: str, rng: random.Random) -> str:
    """快语速吃字/叠字:非冻结区丢字或重字(剪裁快语速形状)。

    数字冻结区不动;**粤语特征字永不删**(可重不可删);丢字后至少保 2 个汉字,
    否则退原句。无改动 → 原样返回。
    """
    polish_lane = _LANE_TO_POLISH[lane]
    spans = _protected_spans(sent, polish_lane)
    idxs = [
        i for i, ch in enumerate(sent)
        if _HAN_RE.match(ch) and not _overlaps(i, i + 1, spans)
    ]
    if not idxs:
        return sent
    rng.shuffle(idxs)
    droppable = [i for i in idxs if sent[i] not in _CANTONESE_MARKERS]
    dupable = list(idxs)
    use_dup = bool(dupable) and (not droppable or rng.random() < 0.4)
    chars = list(sent)
    if use_dup:
        i = dupable[0]
        chars.insert(i, chars[i])
        out = "".join(chars)
    else:
        max_drop = max(0, len(idxs) - 2)  # 丢完至少留 2 汉字
        if max_drop <= 0 or not droppable:
            return sent
        drop = 1 if max_drop == 1 or rng.random() < 0.7 else 2
        drop = min(drop, max_drop)
        drop_set = set(sorted(droppable)[:drop])
        out = "".join(ch for i, ch in enumerate(chars) if i not in drop_set)
    if out == sent or _num_signature(out) != _num_signature(sent):
        return sent
    if _count_markers(out) < _count_markers(sent):
        return sent  # 特征字丢失保险(构造上不可达,兜底)
    return out


def augment_text(
    sent: str, lane: str, rng: random.Random, homophones: dict, kind: str
) -> str:
    """单 kind 分派(未知 kind 原样返回;kind 集已由 parse_augment_kinds 校验)。"""
    if kind == "narrowband":
        return augment_narrowband(sent, lane, rng, homophones)
    if kind == "speed":
        return augment_speed(sent, lane, rng)
    return sent


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def _draw(pool: list, n: int, rng: random.Random) -> list:
    """确定性可放回抽样(洗牌轮转池;n > len 时循环补)。"""
    if not pool or n <= 0:
        return []
    out = []
    while len(out) < n:
        idx = list(range(len(pool)))
        rng.shuffle(idx)
        for i in idx:
            out.append(pool[i])
            if len(out) >= n:
                break
    return out


def build_clean_pools(
    *,
    db_path: Path | None,
    db_timeout: float,
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """汇总三路干净句(repo 种子 + 业务库 + 内置),按 lane 去重排序。

    返回 (pools, source_counts);source_counts 记录各路子来源贡献的**去重前**条数。
    """
    raw: list[tuple[str, str, str]] = []  # (lane, sentence, source)
    for lane, sent in load_repo_seed_sentences():
        raw.append((lane, sent, "repo"))
    if db_path is not None:
        for lane, sent in load_db_sentences(db_path, db_timeout):
            raw.append((lane, sent, "db"))
    for sent in EMBEDDED_SEED_ZH:
        raw.append((LANE_ZH, re.sub(r"\s+", "", sent), "embedded"))
    for sent in EMBEDDED_SEED_YUE:
        raw.append((LANE_YUE, re.sub(r"\s+", "", sent), "embedded"))

    pools: dict[str, list[str]] = {LANE_ZH: [], LANE_YUE: []}
    seen: dict[str, set[str]] = {LANE_ZH: set(), LANE_YUE: set()}
    counts = {"repo": 0, "db": 0, "embedded": 0}
    for lane, sent, source in raw:
        cs = _clean_sentence(sent, lane)
        if not cs:
            continue
        counts[source] += 1
        if cs in seen[lane]:
            continue
        seen[lane].add(cs)
        pools[lane].append(cs)
    for lane in pools:
        pools[lane].sort()
    return pools, counts


def build_injection_cache(
    pools: dict[str, list[str]],
    table: dict,
    homophones: dict,
    rng: random.Random,
    *,
    per_sentence: int = 3,
) -> dict[str, list[tuple[str, str]]]:
    """预生成每 lane 的 (错句, 对句) 缓存,保证注入抽样必有命中面。"""
    cache: dict[str, list[tuple[str, str]]] = {LANE_ZH: [], LANE_YUE: []}
    for lane in (LANE_ZH, LANE_YUE):
        for sent in pools[lane]:
            for _ in range(max(1, per_sentence)):
                bad = inject_errors(sent, lane, rng, table, homophones)
                if bad and bad != sent:
                    cache[lane].append((bad, sent))
    return cache


def _lane_split_counts(cache: dict, n: int) -> tuple[int, int]:
    """按各 lane 可用条数比例把 n 分给 (yue, zh)(与旧注入段逐式同款)。"""
    yue_avail = len(cache[LANE_YUE])
    zh_avail = len(cache[LANE_ZH])
    total_avail = yue_avail + zh_avail
    if total_avail:
        yue_n = min(int(round(n * (yue_avail / total_avail))), n)
    else:
        yue_n = 0
    return yue_n, n - yue_n


def _append_inject_rows(
    rows: list[dict], cache: dict, n: int, rng: random.Random, origin: str
) -> None:
    """按 lane 比例抽 n 条 (错句,对句) 入 rows(原注入段逐字节同序)。"""
    if n <= 0:
        return
    yue_n, zh_n = _lane_split_counts(cache, n)
    for bad, good in _draw(cache[LANE_YUE], yue_n, rng):
        rows.append({"src": bad, "tgt": good, "lane": LANE_YUE, "origin": origin})
    for bad, good in _draw(cache[LANE_ZH], zh_n, rng):
        rows.append({"src": bad, "tgt": good, "lane": LANE_ZH, "origin": origin})


def _append_augmented_rows(
    rows: list[dict],
    cache: dict,
    n: int,
    rng: random.Random,
    homophones: dict,
    kinds: list[str],
) -> None:
    """从干净句生成信道劣化对(src=劣化、tgt=干净),origin 带 `+kind` 后缀。

    lane 比例与注入段同款;kind 轮转;劣化无改动(如无同音候选)则跳过不采纳,
    故实际条数可能少于请求——by_origin 计数即审计面。
    """
    if n <= 0 or not kinds:
        return
    clean = {
        LANE_YUE: sorted({good for _bad, good in cache[LANE_YUE]}),
        LANE_ZH: sorted({good for _bad, good in cache[LANE_ZH]}),
    }
    yue_n, zh_n = _lane_split_counts(clean, n)
    ki = 0
    for lane, want in ((LANE_YUE, yue_n), (LANE_ZH, zh_n)):
        pool = clean[lane]
        if not pool or want <= 0:
            continue
        made = 0
        attempts = 0
        while made < want and attempts < want * 4 + 8:
            attempts += 1
            sent = rng.choice(pool)
            kind = kinds[ki % len(kinds)]
            ki += 1
            aug = augment_text(sent, lane, rng, homophones, kind)
            if aug == sent:
                continue
            rows.append(
                {"src": aug, "tgt": sent, "lane": lane, "origin": f"inject+{kind}"}
            )
            made += 1


def build_dataset(
    *,
    count: int = 50_000,
    identity_ratio: float = 0.40,
    replay_ratio: float = 0.05,
    seed: int = 20260927,
    data_dir: str | Path = "/tmp/homophone-research",
    db_path: str | Path | None = None,
    use_db: bool = True,
    use_replay: bool = True,
    use_char_inject: bool = True,
    replay_limit: int = 4000,
    augment_kinds: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """构建全量训练对,返回 (rows, stats)。纯内存,不落盘。"""
    count = max(1, int(count))
    identity_ratio = min(0.95, max(0.0, float(identity_ratio)))
    replay_ratio = min(0.95, max(0.0, float(replay_ratio)))
    augment_kinds = list(augment_kinds or [])
    rng = random.Random(seed)
    data_path = Path(data_dir).expanduser()
    dbp: Path | None = None
    if use_db:
        dbp = Path(db_path).expanduser() if db_path else _default_db_path()

    table = load_variant_table()
    pools, pool_counts = build_clean_pools(db_path=dbp, db_timeout=5.0)
    homophones = build_homophone_index(pools, data_path) if use_char_inject else {LANE_ZH: {}, LANE_YUE: {}}
    has_char = bool(homophones.get(LANE_ZH) or homophones.get(LANE_YUE))

    # 源 C:真实回灌
    replay_pairs: list[tuple[str, str, str]] = []
    replay_skipped = False
    if use_replay:
        if dbp is not None and dbp.exists():
            replay_pairs = load_replay_pairs(dbp, table, limit=replay_limit)
        if not replay_pairs:
            replay_skipped = True

    # 预算分配:identity 优先保占比,replay 只在预算内,注入吃掉剩余。
    identity_n = int(round(count * identity_ratio))
    replay_n = min(len(replay_pairs), int(round(count * replay_ratio)))
    if identity_n + replay_n > count:
        replay_n = max(0, count - identity_n)
    inject_n = max(0, count - identity_n - replay_n)

    rows: list[dict] = []

    # ---- 源 B:identity(粤语占 identity ≥40%;默认按 50% 配留余量)----
    # yue identity 只从**含特征字**的粤语句抽——「正确粤语句当强负样本」
    # 的判据面;注入池仍吃全部 yue 句。
    yue_pool = pools[LANE_YUE]
    zh_pool = pools[LANE_ZH]
    yue_marker_pool = [s for s in yue_pool if _count_markers(s) > 0] or yue_pool
    if identity_n:
        if yue_marker_pool and zh_pool:
            yue_n = min(identity_n, max(int(round(identity_n * 0.5)), 0))
        elif yue_marker_pool:
            yue_n = identity_n
        else:
            yue_n = 0
        zh_n = identity_n - yue_n
        for sent in _draw(yue_marker_pool, yue_n, rng):
            rows.append({"src": sent, "tgt": sent, "lane": LANE_YUE, "origin": "identity"})
        for sent in _draw(zh_pool, zh_n, rng):
            rows.append({"src": sent, "tgt": sent, "lane": LANE_ZH, "origin": "identity"})

    # ---- 源 A:错误注入(主料)+ 可选信道劣化增强 ----
    # augment 关时:aug_n=0 → 只跑 _append_inject_rows(inject_n),rng 抽取序与
    # 旧内联段逐字节相同 → 输出与旧版逐字节一致。
    inj_cache = build_injection_cache(pools, table, homophones, rng)
    aug_n = inject_n // 2 if (inject_n and augment_kinds) else 0
    plain_inject_n = inject_n - aug_n
    if plain_inject_n:
        _append_inject_rows(rows, inj_cache, plain_inject_n, rng, "inject")
    if aug_n:
        _append_augmented_rows(rows, inj_cache, aug_n, rng, homophones, augment_kinds)

    # ---- 源 C:真实回灌 ----
    replay_used = 0
    for raw, good, lane in _draw(replay_pairs, replay_n, rng):
        if _num_signature(raw) != _num_signature(good):
            continue
        rows.append({"src": raw, "tgt": good, "lane": lane, "origin": "replay"})
        replay_used += 1

    stats = _compute_stats(rows, pools, pool_counts, homophones, replay_pairs, replay_used, replay_skipped,
                           count=count, seed=seed, has_char=has_char, inject_n=inject_n, identity_n=identity_n)
    # 增强审计面:kind 集合 + 实际落库的 `inject+kind` 计数(按 origin)。
    stats["augment"] = {
        "kinds": list(augment_kinds),
        "requested": aug_n,
        "rows": {k: v for k, v in stats["by_origin"].items() if k.startswith("inject+")},
    }
    return rows, stats


def _ratio(a: int, b: int) -> float:
    return round(a / b, 4) if b else 0.0


def _count_markers(text: str) -> int:
    return sum(1 for c in text if c in _CANTONESE_MARKERS)


def _compute_stats(rows, pools, pool_counts, homophones, replay_pairs, replay_used, replay_skipped,
                   *, count, seed, has_char, inject_n, identity_n) -> dict:
    by_origin: dict[str, int] = {}
    by_lane: dict[str, int] = {}
    id_yue = id_zh = 0
    id_yue_marker = 0
    inject_with_digits = 0
    for r in rows:
        by_origin[r["origin"]] = by_origin.get(r["origin"], 0) + 1
        by_lane[r["lane"]] = by_lane.get(r["lane"], 0) + 1
        if r["origin"] == "identity":
            if r["lane"] == LANE_YUE:
                id_yue += 1
                if _count_markers(r["src"]):
                    id_yue_marker += 1
            else:
                id_zh += 1
        if r["origin"] == "inject" and _DIGIT_RUN_RE.search(r["src"]):
            inject_with_digits += 1
    identity_total = id_yue + id_zh
    return {
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "seed": seed,
        "count_requested": count,
        "count_actual": len(rows),
        "by_origin": by_origin,
        "by_lane": by_lane,
        "identity": {
            "count": identity_total,
            "ratio": _ratio(identity_total, len(rows)),
            "yue": id_yue,
            "zh": id_zh,
            "yue_ratio": _ratio(id_yue, identity_total),
            "yue_with_markers": id_yue_marker,
        },
        "pools": {
            "zh": len(pools[LANE_ZH]),
            "yue": len(pools[LANE_YUE]),
            "yue_with_markers": sum(1 for s in pools[LANE_YUE] if _count_markers(s) > 0),
            "source_counts": pool_counts,
        },
        "char_inject_enabled": has_char,
        "replay": {
            "available": len(replay_pairs),
            "used": replay_used,
            "skipped": replay_skipped,
        },
        "inject_rows_with_digits": inject_with_digits,
        "budget": {"identity": identity_n, "inject": inject_n},
    }


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def write_outputs(
    rows: list[dict],
    stats: dict,
    out_dir: str | Path,
    *,
    sample_limit: int = 50,
    testset_src: str | Path | None = "/tmp/csc/testset.json",
) -> dict[str, str]:
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    train = out / "csc_train.jsonl"
    sample = out / "sample.jsonl"
    statsf = out / "csc_stats.json"
    with train.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with sample.open("w", encoding="utf-8") as f:
        for r in rows[: max(0, int(sample_limit))]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    statsf.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written = {"train": str(train), "sample": str(sample), "stats": str(statsf)}
    # 种子评测集:从 /tmp/csc 拷一份(存在才拷,幂等)
    tsrc = Path(testset_src).expanduser() if testset_src else None
    if tsrc and tsrc.exists():
        tdst = out / "csc_testset.json"
        tdst.write_text(tsrc.read_text(encoding="utf-8"), encoding="utf-8")
        written["testset"] = str(tdst)
    return written


def _print_stats(stats: dict) -> None:
    print("[prepare_csc_data] stats:")
    print(f"  seed={stats['seed']} requested={stats['count_requested']} actual={stats['count_actual']}")
    print(f"  by_origin={stats['by_origin']}")
    print(f"  by_lane={stats['by_lane']}")
    i = stats["identity"]
    print(
        f"  identity={i['count']} ({i['ratio']:.1%}) yue={i['yue']} "
        f"yue_ratio={i['yue_ratio']:.1%} yue_with_markers={i['yue_with_markers']}"
    )
    p = stats["pools"]
    print(f"  pools: zh={p['zh']} yue={p['yue']} sources={p['source_counts']}")
    print(f"  char_inject={stats['char_inject_enabled']} replay={stats['replay']}")
    aug = stats.get("augment") or {}
    if aug.get("kinds"):
        print(f"  augment={aug['kinds']} requested={aug.get('requested')} rows={aug.get('rows')}")
    print("STATS_JSON " + json.dumps(stats, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="粤语/普通话 CSC 自训数据挖掘管道")
    ap.add_argument("--count", type=int, default=50_000, help="目标总条数(注入可放大,默认 50000)")
    ap.add_argument("--identity-ratio", type=float, default=0.40, help="identity 占比(默认 0.40)")
    ap.add_argument("--replay-ratio", type=float, default=0.05, help="replay 占比上限(默认 0.05)")
    ap.add_argument("--seed", type=int, default=20260927, help="随机种子(复现)")
    ap.add_argument("--data-dir", default="/tmp/homophone-research",
                    help="rime_char.csv 所在目录(dev 字级注入;默认 /tmp/homophone-research)")
    ap.add_argument("--db", default=None, help="业务库路径(默认 app-data/bok_voice.db)")
    ap.add_argument("--no-db", action="store_true", help="跳过业务库(仅 repo 种子+内置)")
    ap.add_argument("--no-replay", action="store_true", help="跳过源 C 真实回灌")
    ap.add_argument("--no-char-inject", action="store_true", help="只跑词级注入(零 dev 依赖)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="输出目录(默认 data/csc)")
    ap.add_argument("--sample-limit", type=int, default=50, help="sample.jsonl 行数上限(默认 50)")
    ap.add_argument("--testset-src", default="/tmp/csc/testset.json", help="种子评测集来源(可空)")
    ap.add_argument("--stats", action="store_true", help="打印统计(默认已打印,保留兼容)")
    ap.add_argument("--augment", default="",
                    help="信道劣化增强(逗号分隔):narrowband(窄带同音混淆),speed(快语速丢/叠字);"
                         "默认空=逐字节同旧行为")
    args = ap.parse_args(argv)

    try:
        augment_kinds = parse_augment_kinds(args.augment)
    except ValueError as exc:
        print(f"[prepare_csc_data] --augment 解析失败: {exc}")
        return 2

    rows, stats = build_dataset(
        count=args.count,
        identity_ratio=args.identity_ratio,
        replay_ratio=args.replay_ratio,
        seed=args.seed,
        data_dir=args.data_dir,
        db_path=args.db,
        use_db=not args.no_db,
        use_replay=not args.no_replay,
        use_char_inject=not args.no_char_inject,
        augment_kinds=augment_kinds,
    )
    if stats["replay"]["skipped"]:
        print("source c skipped: no ledger")
    written = write_outputs(rows, stats, args.out_dir, sample_limit=args.sample_limit,
                            testset_src=args.testset_src)
    _print_stats(stats)
    print(f"[prepare_csc_data] wrote: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
