// 三语基础数据种子包（PRD 3.7 / 需求8，2026-09-20 第二批）。
// 设计取舍（引擎安全第一）：
// - 意图种子=只建意图不挂绑定——引擎里无绑定的意图命中后零动作（inert），
//   业务在「意图管理」里按话术挂跳转/播快答才激活；语言跟随模板（通话语言整通固定）。
// - 问答种子=通用防诈/身份/赔付口径，经「问答库」tab 的一键导入落库（挂模板第 1 步、
//   template 绑定），不自动写库——0.90 字面匹配直接进真实通话，落库必须经业务确认。
// 关键词是确定性子串命中——只放无歧义说法；模糊语义走 judge 判据。

export type SeedIntent = {
  label: string;
  keywords: string[];
  judge?: string;
};

export type SeedQa = { question: string; answer: string };

type LangSeeds = { intents: SeedIntent[]; qa: SeedQa[] };

export const SEED_PACKS: Record<string, LangSeeds> = {
  zh: {
    intents: [
      {
        label: "客户肯定",
        keywords: ["好的", "可以", "没问题", "行", "嗯好", "同意", "办理吧"],
      },
      {
        label: "客户拒绝",
        keywords: ["不用了", "不需要", "不要", "算了", "别打了"],
        judge: "客户明确表达不愿意继续通话或不需要办理才算命中；询问细节、有犹豫不算。",
      },
      {
        label: "求转人工",
        keywords: ["人工", "真人", "客服转", "转接", "跟人讲"],
      },
      {
        label: "防诈质疑",
        keywords: ["诈骗", "骗子", "你怎么有我号码", "真的假的", "可信吗"],
        judge: "客户怀疑本次来电是诈骗、质疑来电身份才算命中；单纯询问公司名不算。",
      },
    ],
    qa: [
      { question: "你们是哪家公司", answer: "这里是{物流公司}的客服中心，关于您的包裹有重要事项跟您确认。" },
      { question: "你是机器人吗", answer: "我是{物流公司}的智能助理，全程录音，您说的每句话我都能听懂，请讲。" },
      { question: "是不是诈骗", answer: "理解您的谨慎。我们是{物流公司}官方客服，您可以挂断后拨打官网电话核实，我再跟您说明来意。" },
      { question: "怎么赔付", answer: "包裹遗失是我们的责任，我们已购买运费保险，按规则给您赔付，不需要您自己承担。" },
      { question: "钱多久到账", answer: "赔付审核通过后会直接打给您，一般几个工作日内到账，到账前我们会再跟您确认一次。" },
      { question: "我不需要了", answer: "好的，打扰您了。如果后续有需要，随时可以联系{物流公司}客服，祝您生活愉快。" },
    ],
  },
  cantonese: {
    intents: [
      {
        label: "客戶肯定",
        keywords: ["好呀", "得", "冇問題", "可以呀", "好啊", "辦啦"],
      },
      {
        label: "客戶拒絕",
        keywords: ["唔使", "唔需要", "唔好", "算啦", "咪打嚟"],
        judge: "客戶明確表達唔想繼續通話或唔需要辦理先算命中；問細節、有猶豫唔算。",
      },
      {
        label: "求轉人工",
        keywords: ["人工", "真人", "轉接", "同真人講"],
      },
      {
        label: "防詐質疑",
        keywords: ["呃人", "老翻", "詐騙", "你點知我電話", "真嘅假嘅"],
        judge: "客戶懷疑呢次來電係詐騙、質疑來電身份先算命中；淨係問公司名唔算。",
      },
    ],
    qa: [
      { question: "你哋係邊間公司", answer: "呢度係{物流公司}嘅客服中心，關於你個包裹有重要事項同你確認。" },
      { question: "你係唔係機器人", answer: "我係{物流公司}嘅智能助理，全程錄音，你講嘅每句我都聽得明，請講。" },
      { question: "係咪呃人㗎", answer: "明白你嘅謹慎。我哋係{物流公司}官方客服，你可以收線之後打官網電話核實，我再同你講解。" },
      { question: "點賠償", answer: "包裹遺失係我哋嘅責任，我哋已經買咗運費保險，按規則賠俾你，唔使你自己承擔。" },
      { question: "幾耐到賬", answer: "賠償審核通過之後會直接過數俾你，一般幾個工作天內到賬，到賬之前我哋會再同你確認一次。" },
      { question: "唔需要喇", answer: "好嘅，打攪晒。如果之後有需要，隨時聯絡{物流公司}客服，祝你生活愉快。" },
    ],
  },
  en: {
    intents: [
      {
        label: "Customer confirms",
        keywords: ["okay", "sure", "yes please", "that works", "go ahead"],
      },
      {
        label: "Customer declines",
        keywords: ["no thanks", "not interested", "stop calling", "no need"],
        judge: "Only counts when the customer clearly refuses to continue or does not need the service; asking details or hesitating does not count.",
      },
      {
        label: "Asks for human agent",
        keywords: ["human", "real person", "agent please", "transfer me"],
      },
      {
        label: "Scam suspicion",
        keywords: ["scam", "fraud", "how did you get my number", "is this legit"],
        judge: "Only counts when the customer suspects this call is a scam or questions our identity; merely asking the company name does not count.",
      },
    ],
    qa: [
      { question: "which company is this", answer: "This is the customer service center of {物流公司}. We are calling about an important matter regarding your parcel." },
      { question: "are you a robot", answer: "I am the smart assistant of {物流公司}. This call is recorded, and I understand everything you say. Please go ahead." },
      { question: "is this a scam", answer: "I understand your concern. We are the official customer service of {物流公司}. You may hang up and verify through the official website, and I will explain the purpose of this call." },
      { question: "how will you compensate", answer: "The lost parcel is our responsibility. We have shipping insurance in place and will compensate you according to the policy, at no cost to you." },
      { question: "when will I get the refund", answer: "Once the compensation is approved, the payment will be sent to you directly, usually within a few business days. We will confirm with you again before it arrives." },
      { question: "I don't need this", answer: "Understood, sorry for the interruption. If you need anything later, feel free to contact {物流公司} customer service. Have a nice day." },
    ],
  },
};

/** 取语言种子包（未知语言退中文包——模板语言三态 zh/cantonese/en）。 */
export function seedPackFor(lang: string): LangSeeds {
  return SEED_PACKS[String(lang ?? "").toLowerCase()] ?? SEED_PACKS.zh;
}
