#!/usr/bin/env python3
"""
依据定级 — 给检索到的每条依据打上规范层级、时效、与本题的对应关系和角色。

为什么需要这一层：六个源检索回来的东西不是一个量级的凭据。NPC 里的
《企业所得税法》是全国人大立的法，总局的一纸公告是部门规范性文件，税屋的
一篇文章是第三方解读。三者排在一起不加区分地用，读者分不出哪一句是能拿去和
税务机关据理力争的条文、哪一句是要回到原文再核的执行口径。把它们混成一堆贴进
答案，是把检索结果直接当分析结论的通病。

同一层内部还要看时效。政策有生效和失效日期，同一问题在 2023 年和 2026 年
答案可能完全不同；拿一份已废止的公告去回答 2026 年的问题，比不回答更糟，
因为它看起来像个答案。

这一层做四件事，四件事各用一根独立的轴，**不合成成一个分数**：
  1. 判规范层级（LEGAL_RANK）：宪法、法律、行政法规、部门规章、规范性文件、
     地方性法规、地方规范性文件。它只回答一件事——下位规则不得抵触上位规则，
     两份文件打架时谁让位。**它不回答"本题该引哪一份"**，把这两件事乘成
     `位阶 × 时效系数` 一个分数就会混起来出错：宪法
     位阶 100、时效 1.0，按这个乘法是全场最高分、会被挑成主依据，而我国法律
     实践中宪法不得直接作为征税或裁判依据；反过来"小微企业六税两费减免"这
     道题的直接依据是总局公告（规范性文件），引《企业所得税法》位阶再高也
     答不了这题。
  2. 判时效（judge_validity）：给定观察时点，判定每条依据在该时点是现行有效、
     尚未生效、已废止还是查不到时效。这是唯一带"不能用"后果的轴——拿一份
     已废止的公告回答 2026 年的问题比不回答更糟，因为它看起来像个答案。
  3. 判与本题的对应关系（on_topic）：这条材料是不是真的规定了题面那件事。
     判据是标题或正文里能不能找到本题的主题词，找不到就标"未核对"，
     由上层决定要不要补一轮检索。没有这一维时，检索结果跑题只能靠
     各源自带的 `_reliability` 标记把分数清成 0 来间接表达。
  4. 分层与平手裁决：同一件事有多份材料时，按「本题的直接规定 / 上位依据与
     授权 / 执行口径与实务认定 / 政策沿革 / 待核对线索」五种**角色**分层。角色
     不是等级高低——答一道具体题目的标准配置是直接规定、上位授权、执行口径
     三层都有，缺哪层就写明缺哪层。

第 3 步之后不做可靠性否决——`_reliability` 命中不清零分数、不整组剔除 `low`、
界面上也不挂"仅参考"角标。那一套是用降权代替核对：它把"这条可能跑题"
变成了"这条不许引用"，而用户看到的只是一个小标签，既不知道为什么、也无从
下手核对。同样的信息写成逐条的具体提醒（`caveats`）——说清楚存疑在哪
一处、要核对什么——材料本身照常参与分层。

它不做判断题，只给判断提供刻度。真正的判断由上层按 tax_analyze 判出的
问题类型组织。

Usage:
  python tax_evidence.py --rank "中华人民共和国企业所得税法" --topic "企业所得税"
  python tax_evidence.py --rank "国家税务总局公告2018年第28号" --at 2026-09-27 \
      --topic "税前扣除凭证"
  python tax_evidence.py --rank "即征即退的会计与税务处理" --source "税屋 (shui5.cn)"
"""

import re

# ── 规范层级 ───────────────────────────────────────────────────────────────
# 数字是**层级序号**，只回答一件事：两份文件对同一件事规定冲突时谁让位
# （《立法法》确立的位阶：宪法、法律、行政法规、部门规章、地方性法规，
# 再往下是规范性文件）。它不是"该引哪一条"的权重，也不参与任何分数合成——
# 位阶高只意味着下位不得抵触它，不意味着它更适合回答某个具体问题。
# 这张表里**只装《立法法》序列内的层级**：技术性口径、实务解读、未定性都不在
# 序列内（前两个是材料形态，最后一个是没判出来），拿它们比大小没有意义，
# 所以不进表；要取序号一律走 `_tier()`，表里没有的一律按 0 处理。
LEGAL_RANK = {
    "constitution": 100,      # 宪法 —— 位阶最高，但不得直接援引，见 NOT_DIRECTLY_QUOTABLE
    "law": 90,                # 法律（全国人大及其常委会）
    "admin_regulation": 80,   # 行政法规（国务院）
    "department_rule": 70,    # 部门规章（总局、财政部等部委以令形式发布）
    "normative": 55,          # 规范性文件（总局公告、通知、批复，不以令发布）
    "local_regulation": 50,   # 地方性法规与地方政府规章
    "local_normative": 40,    # 地方税务规范性文件
}
RANK_LABEL = {
    "constitution": "宪法",
    "law": "法律",
    "admin_regulation": "行政法规",
    "department_rule": "部门规章",
    "normative": "规范性文件",
    "local_regulation": "地方性法规",
    "local_normative": "地方规范性文件",
    "technical": "技术性口径",
    "interpretation": "实务解读",
    "unknown": "未定性",
}

# 这两档不在《立法法》的位阶序列里，它们是材料形态不是规范层级。混在
# LEGAL_RANK 里靠数字比大小，就出现"一份总局官方解读（25）比地方政府规章
# （50）低"这种没有意义的比较——两者根本不在同一根轴上。
PRACTICE_RANKS = ("technical", "interpretation")

# 指向实务材料来源的标记。这里用子串而不是全等：各源写进 `source` 的是给人看的
# 标签（"税屋 (shui5.cn)""微信公众号 (搜狗微信)"），而搜狗那一路回填的是纯域名
# （`tax_sogou._domain_of` 的返回值，如 "shui5.cn"）。写成全等
# `source in ("shui5.cn", "mp.weixin.qq.com")` 只能命中搜狗回填的那批，
# 税屋与公众号自己发回来的条目一条都不认，实务层因此被按标题形态漏判成"未定性"。
PRACTICE_SOURCES = ("shui5.cn", "mp.weixin.qq.com", "weixin.sogou.com",
                    "税屋", "微信公众号", "搜狗微信")


def _tier(rank_key: str) -> int:
    """取层级序号；不在位阶序列内的形态（含未定性）一律 0。"""
    return LEGAL_RANK.get(rank_key, 0)

# 位阶判定：先按标题形态的强特征，再按发布机关。
# 顺序有讲究——"暂行"条例、"实施条例"这类形态比机关名更能定级，
# 所以形态规则排在机关规则前面。
_RANK_RULES = (
    # 宪法
    (r"^中华人民共和国宪法", "constitution"),
    # 法律：全国人大及其常委会决定，"法"结尾且非其他形态。
    # 结尾允许修饰语，因为法条名常带"（草案）""（修订）"等尾巴
    (r"^中华人民共和国[一-龥]{2,15}法(?:（[^）]*）)?\s*$", "law"),
    # 行政法规：国务院以"条例/实施条例/暂行条例"命名。
    # "实施细则"是国务院实施细则或部门实施细则，落在部门规章一档更常见，
    # 但形态上与部门办法同级，故先归部门规章。
    (r"^中华人民共和国.*?(?:暂行|实施)?条例\s*$", "admin_regulation"),
    (r"^中华人民共和国.*?实施细则\s*$", "department_rule"),
    (r"^[一-龥]{2,10}(?:暂行)?条例\s*$", "admin_regulation"),
    # 部门规章：部委以"令"发布的办法/规程/实施细则
    (r"^中华人民共和国[一-龥]{2,12}(?:办法|规程)\s*$", "department_rule"),
    # 规范性文件：部委的公告/通知/批复/函/意见/决定。
    # 部委名可能多到四字（国家税务总局）或带空格（财政部 国家税务总局），
    # 所以中间段用"非句号字符、至多 60 个"来兜，不要写死部委清单。
    (r"^国家税务总局(?:公告)?\s*(?:第)?\d{4}\s*年?\s*第?\s*\d*\s*号", "normative"),
    # 中间段用 [^。] 而不是 [一-龥]：标题里带书名号时（"国家税务总局关于发布
    # 《企业重组业务企业所得税管理办法》的公告"）汉字类会断在《上，整条判成
    # 未定性，一份规范性文件就这样丢了层级，连带角色也判错。
    (r"^国家税务总局[^。]{0,60}?(?:公告|通知|批复|函|意见|决定|令)", "normative"),
    (r"^(?:财政部|国家发展改革委|商务部|海关总署|国家统计局|国家外汇管理局)"
     r"[^。]{0,60}?(?:公告|通知|批复|函|意见|决定)", "normative"),
    # 地方
    (r"^[一-龥]{2,10}(?:省|自治区|市|自治州|地区|盟)[一-龥]{0,8}?"
     r"(?:人民政府)?(?:暂行)?(?:条例|办法|规定)\s*$", "local_regulation"),
    (r"^[一-龥]{2,10}省[一-龥]{0,6}税务局[^。]{0,20}$", "local_normative"),
    (r"^[一-龥]{2,10}市[一-龥]{0,6}税务局[^。]{0,20}$", "local_normative"),
    # 实务解读
    (r"^解读[:：]|^关于.*的?解读\s*$", "technical"),
    (r"^(?:解读|答问|问答|实务|案例|操作指引|办税指南|指南|辅导)", "technical"),
    (r"政策(?:辅导)?指引|执行指引", "technical"),
)
_COMPILED_RANK_RULES = tuple((re.compile(p), r) for p, r in _RANK_RULES)


def rank_of(title: str, category: str = "", source: str = "") -> dict:
    """判定一条依据的效力位阶。

    Args:
        title: 法规名或文章标题。
        category: NPC 库返回的法律性质字段（flxz），如"法律""行政法规"。
            有它时优先用，接口给的分类比自己按标题猜准。
        source: 来源标识，用来给"实务解读"兜底。

    Returns:
        {"rank","label","tier","by"}，by 说明是按什么判出来的；tier 是层级序号。
    """
    title = (title or "").strip()
    cat = (category or "").strip()

    # 解读件排在校验分类与形态规则之前判：它的标题里整份地嵌着被解读文件的
    # 全名（"国家税务总局关于……的公告的解读"），任何按文件名形态或按
    # effect_level 分类的规则都会先把它认成那份文件本身，把一份技术性口径
    # 当成规范性文件引用出去。
    if title.endswith("解读"):
        return _mk("technical", "标题以「解读」收尾，是被解读文件的说明件")

    if cat:
        for key, label in (("宪法", "constitution"), ("法律", "law"),
                           ("行政法规", "admin_regulation"),
                           ("部门规章", "department_rule"),
                           ("地方性法规", "local_regulation"),
                           ("地方政府规章", "local_regulation"),
                           ("法律性质", "")):
            if key in cat:
                if label:
                    return _mk(label, f"NPC 法律性质字段「{cat}」")
                break

    for pat, key in _COMPILED_RANK_RULES:
        if pat.search(title):
            return _mk(key, f"标题形态匹配「{title[:24]}」")

    if any(d in source for d in PRACTICE_SOURCES) or "解读" in title:
        return _mk("interpretation", "来源或标题指向实务解读")

    return _mk("unknown", "标题形态与来源都不足以定级")


def _mk(key: str, by: str) -> dict:
    # tier 是层级序号（只用于冲突裁决），不是可引用性分。字段名从 score 改成
    # tier 是有意的：留着 score 就会被下游当权重拿去排队。不在位阶序列内的
    # 形态走 `_tier()` 拿 0，不在这里给它们编一个假序号。
    return {"rank": key, "label": RANK_LABEL[key],
            "tier": _tier(key), "by": by}


# ── 时效 ───────────────────────────────────────────────────────────────────
# 三个态：现行有效 / 尚未生效 / 已废止。查不到时效的必须显式标出来，
# 因为"不知道还生效没有"和"确认生效"不能混为一谈。
VALIDITY = {
    "effective": "现行有效",
    "pending": "尚未生效",
    "repealed": "已废止",
    "unknown": "时效未标明",
}

# ── 自载执行期限的止日 ─────────────────────────────────────────────────────
# 到期日在清单层取不到（减免税目录的"有效"栏实测 912 条有效期起/止全空，
# 法规库的时效栏对财税文件也不录），但在正文层取得到：公告普遍以
# "自2023年1月1日至2027年12月31日"或"延续执行至2027年12月31日"写明止期。
# 实测 20 条带正文的法规库条目里 11 条含这两类句式。
_EXPIRY_SIMPLE = re.compile(
    r"(?:执行至|延长至|延续至|延至|有效期至|有效期截至|期限至)"
    r"\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_EXPIRY_RANGE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起?\s*至\s*"
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _iso(year: str, month: str, day: str) -> str:
    """三位数字拼成可比较的 YYYY-MM-DD；不像真日期就返回空串。

    返回补零的定宽串是因为止期与观察时点按字符串比大小，位数不齐会比错
    （"2027-1-1" 排在 "2027-12-31" 之后）。月份越界这类脏值不作判据。
    """
    y, m, d = int(year), int(month), int(day)
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def _body_of(item: dict) -> str:
    """条目把正文放在 content 或 body 两处，拼接后一起扫。"""
    return " ".join(str(x) for x in (item.get("content"), item.get("body")) if x)


def _expiry_set(body: str) -> set:
    """正文里出现过的所有止日候选，脏值与不像日期的已经剔掉。"""
    ends = set()
    for match in _EXPIRY_SIMPLE.finditer(body):
        value = _iso(*match.groups())
        if value:
            ends.add(value)
    for match in _EXPIRY_RANGE.finditer(body):
        value = _iso(*match.groups()[3:])
        if value:
            ends.add(value)
    return ends


def expiry_of(item: dict) -> str:
    """取一条依据自载的执行期限止日，取不到返回空串。

    先读录入项 `expiry_date`（各源目前都给不出，留作显式入口），再从正文抠。
    **正文里出现两个以上不同止期时返回空串**：一份文件可以同时谈多段期限
    （延长执行期限的公告把多项政策各自延到不同日期），替用户挑一个就是猜。
    """
    exp = (item.get("expiry_date") or "").strip()
    if exp:
        return exp
    body = _body_of(item)
    ends = _expiry_set(body) if body else set()
    return ends.pop() if len(ends) == 1 else ""


def period_start_of(item: dict) -> str:
    """取一条依据自载执行期限的起日，取不到或有歧义时返回空串。

    只认 `自X至Y` 这一种成对写法：单独的"执行至2027年12月31日"只交代止期，
    配不出起日。正文里出现两组以上不同区间时返回空串——那种公告分项政策各有一段
    期限，任何一端都不该替用户挑。
    """
    pairs = set()
    for match in _EXPIRY_RANGE.finditer(_body_of(item)):
        start = _iso(*match.groups()[:3])
        end = _iso(*match.groups()[3:])
        if start and end:
            pairs.add((start, end))
    return next(iter(pairs))[0] if len(pairs) == 1 else ""


def judge_validity(item: dict, at: str = "") -> dict:
    """按观察时点判定一条依据的时效。

    Args:
        item: 含 status / status_code / effective_date / publish_date /
            expiry_date（或带执行期限句式的 content、body）之一的字典。
        at: 观察时点 YYYY-MM-DD，空串表示不判生效区间，只看状态字段。

    Returns:
        {"validity","label","as_of","note","qualified"}。as_of 为空表示没做时点
        判定。qualified=True 表示这句 note 里带着"引用前要办的一件事"（文本被
        改过、只列到部分失效、靠别文佐证、日期晚于时点），False 表示它只是在
        交代这条结论从哪个字段得来。区分这两者是因为 note 会被拼进 `caveats`，
        而"按状态字段判定"这种常规交代挤在提醒队列最前面，会把真正要核对的那句
        顶下去，用户看到的就成了没有信息量的废话。
    """
    at = (at or "").strip()
    status = (item.get("status") or "").strip()
    code = item.get("status_code")

    # 时效性分类状态：按录入枚举的整串语义判，子串匹配排在其后。
    # "部分失效/部分废止/部分无效"的含义是全文仍在效、仅个别条款失效，
    # 必须排在"废止/失效"分支之前——否则会被子串"失效"误判成全文废止，
    # 把仍在使用的文件压到不可引用。NPC 那套状态串里有"部分失效"这一档；
    # 清单侧（getFileListByCodeId 与法规库 search5）的取值是
    # `tax_web_search.AGING_VALUES` 那五种，不含"部分"，两套词都从这段过。
    partial = any(k in status for k in ("部分失效", "部分废止", "部分无效"))
    amended = any(k in status for k in ("已修改", "已修订"))

    # NPC 的 sxx 码：3=有效，1=尚未生效，9=已废止（以 SXX_MAP 文字为准）
    if partial:
        base = "effective"
    elif "有效" in status and "尚未" not in status:
        base = "effective"
    elif "尚未生效" in status or "未生效" in status:
        base = "pending"
    elif any(k in status for k in ("废止", "失效", "被废止")):
        base = "repealed"
    elif code == 9:
        base = "repealed"
    elif amended:
        # 法规库把"已修改"与"已废止/全文失效"分开发：标了已修改的仍然在效，
        # 只是文本被改过。判成 unknown 会把仍在使用的配套文件全拖进"时效未
        # 标明"那一档，而答案真正该说的是"引哪一版"。
        base = "effective"
    else:
        base = "unknown"

    tail = "；文本已被修改，引用须按修改后的版本" if amended else ""
    if partial:
        tail += "；仅部分条款已失效，引用前须核对具体条款"

    # 自载执行期限的两端都要与观察时点比过才算数。只比止期会出错判：实测
    # 财政部 税务总局公告2022年第30号（公布 2022-09-30，正文"自2022年10月1日至
    # 2023年12月31日"）在 at=2022-09-30 时会拿到"观察时点在期内"那句 note——起日
    # 还在三天之后。2026年第22号同理（公布 2026-07-27，自2026年9月1日起）。
    #
    # 两端越界都只写提醒、不动 validity。判成 repealed 或 pending 会把可能仍在
    # 使用的主依据整条压掉——降权代替不了核对；而不写又会让用户拿
    # 一段还没开始或已经结束的期限当当期口径。剩下的两种可能（后续文件延续过、
    # 这段期限本属正文引用的另一轮政策）从这一条材料里都判不了。
    expiry = expiry_of(item)
    # 录入项给了施行日期时以它为准，正文那句不再参与起日判断：两处不一致是数据
    # 冲突，而"生效日晚于观察时点"那一格本来就会按录入项判 pending，不该被正文里
    # 某一条款的期限顶掉。
    eff = (item.get("effective_date") or "").strip()
    start = "" if eff else period_start_of(item)
    dated = bool(at and base in ("effective", "unknown"))
    past_expiry = dated and bool(expiry) and expiry < at
    future_start = dated and bool(start) and start > at
    period_core = ""
    if past_expiry:
        period_core = (f"正文自载的执行期限止于 {expiry}，早于观察时点 {at}；"
                       f"先查这段期限之后有没有延续文件，没有延续不得按当期口径引用")
    elif future_start:
        period_core = (f"正文自载的执行期限自 {start} 起，晚于观察时点 {at}；"
                       f"这段规定在观察时点还没开始执行，当期结论不得按它给，"
                       f"要另找观察时点当时适用的文件")
    # 前半句"状态标「全文有效」／本条无时效录入"交代的是这条结论来自哪个录入项，
    # 只有期限那句单独成 note 时才带；与佐证那句拼接时同样的信息已经交代过一遍。
    period_head = ("本条无时效录入" if base == "unknown" else f"状态标「{status}」")
    # 两端都盖住观察时点时只把日期记进 note，不占提醒的位子：眼下没有要核对的事，
    # 而 validity_note 会随条目一起进结构化输出，读者要查这项优惠几时到期时拿得到。
    # 真快到期了也不必在这里替用户编一个"提前 N 天"的门限——门限一写就成了判据，
    # 而判"当期能不能用"靠的就是止期、起期与观察时点的这两次比较。
    note_extra = ""
    # `expiry >= at` 这一项不能省：状态栏空、录入项施行日期又晚于观察时点时，上面的
    # 期限提醒被"尚未生效"那一格让位，这一句会独自进 note——不比止期就把已过去的期限
    # 写成"在期内"，同一条 note 里施行日期未到与期限已过两句反话并存。
    if dated and expiry and expiry >= at:
        note_extra = f"；正文自载执行期限至 {expiry}，观察时点 {at} 在期内"

    # 状态判不出来时，"被现行有效的文件列为制定依据"是这个来源里唯一可得的
    # 在效证据（财税文件那一栏根本不录时效，见 tax_answer 的
    # corroborate_validity_from_target）。已标明废止或未生效的不走这条路，
    # 明文状态优先于援引证据。
    #
    # 援引与自载期限各说一件事，所以这一支要在期限直接返回之前判完：援引证据说的是
    # "这份文件整体还在效"，期限说的是"正文里那一段规定什么时候算数"，后者不能把
    # 前者顶掉。期限分支若先返回，带正文的财税文件一被抠出止期就从
    # "佐证在效"掉回"时效未标明"——`corroborate_validity_from_target` 补的那格
    # 白补，还多付一条"引用前单独核对它是否还在效"。
    cited_by = (item.get("corroborated_by") or "").strip()
    if base == "unknown" and cited_by:
        when = f"，观察时点 {at}" if at else ""
        extra = (f"；{period_core}" if period_core else note_extra)
        return {"validity": "effective", "label": VALIDITY["effective"],
                "as_of": at, "qualified": True,
                "note": f"本条无时效录入，按现行有效的《{cited_by}》正文将其列为"
                        f"制定依据判定在效{when}；引用前按该文自身的时效复核"
                        + extra}

    # 录入项的施行日期晚于观察时点时，让"生效日晚于时点"那一格先判：那份文件本身
    # 还没开始施行，正文里某一段期限的起止就不再是"当期能不能按它答"的问题。
    if period_core and not (at and eff and eff > at):
        return {"validity": base, "label": VALIDITY[base], "as_of": at,
                "qualified": True, "note": f"{period_head}，{period_core}{tail}"}

    if at and base in ("effective", "pending"):
        if eff and eff > at:
            return {"validity": "pending", "label": VALIDITY["pending"],
                    "as_of": at, "qualified": True,
                    "note": f"生效日 {eff} 晚于观察时点 {at}"}
        if base == "pending":
            # 状态说"尚未生效"，就要靠施行日期证明它在观察时点前已经生效。
            # 没有日期不能倒向 effective——那等于把"没查到日期"当成"日期必然
            # 在过去"，一份还没开始施行的公告会直接顶成主依据。
            if not eff:
                return {"validity": "pending", "label": VALIDITY["pending"],
                        "as_of": at, "qualified": True,
                        "note": f"状态标尚未生效，且无施行日期可证实在 {at} 前生效"}
            return {"validity": "effective", "label": VALIDITY["effective"],
                    "as_of": at, "qualified": False,
                    "note": f"生效日 {eff} 不晚于观察时点 {at}"}
        return {"validity": "effective", "label": VALIDITY["effective"],
                "as_of": at, "qualified": bool(tail),
                "note": f"按状态字段判定，效力期间含 {at}{tail}{note_extra}"}

    if base == "effective" and tail:
        return {"validity": base, "label": VALIDITY[base], "as_of": at,
                "qualified": True, "note": "按状态字段判定" + tail}
    return {"validity": base, "label": VALIDITY[base], "as_of": at,
            "qualified": False,
            "note": ("无状态字段可判" if base == "unknown" else "按状态字段判定")
                    + note_extra}


# ── 与本题的对应关系 ───────────────────────────────────────────────────────
# 这一维是定级的第三根轴，因为它才是"该引哪一条"的真正判据：
# 跑题只能靠各源自带的 `_reliability` 标记把分数清成 0 来间接表达，结果是
# "一条《企业所得税法》因为检索方式跑题而被整条禁掉"这种荒谬判定。
TOPIC_FIELDS = ("title", "content", "summary", "body")

# 主题词参数允许是一串词：按这些分隔符切开，`tax_answer` 那边传进来的就是
# 专题名、本体法名、口语短词几样拼在一起的东西，而不是一个词。
_TOPIC_SEP = re.compile(r"[\s、，,；;/|]+")

# 宪法单列：位阶最高，但不能直接拿去当征税或答复的依据。
NOT_DIRECTLY_QUOTABLE = ("constitution",)
CONSTITUTION_NOTE = ("宪法不直接作为征税与执法依据：它要靠《企业所得税法》《税收征收"
                     "管理法》这类法律落实，答案里拿宪法条文当依据顶不住税务机关")


def on_topic_of(item: dict, topic):
    """这条材料有没有真的规定题面那件事。

    Args:
        item: 检索结果字典。
        topic: 本题的主题词。给一个词、一串用空白或顿号分开的词、或直接给词表
            都行——任一词命中即算命中。单词匹配是不够的：《国家税务总局关于
            进一步支持小微企业和个体工商户发展有关税费政策的公告》是"六税两费
            减免"这道题的直接规定，可它的标题里根本没有"六税两费"四个字，只有
            "税费政策"。传进来的词越多，越可能撞中标题里那个说法。

    Returns:
        True  —— 标题或正文里找得到本题的主题词。
        False —— 传了主题词，但这条材料的标题与正文里一处都没有，字面上对不上。
        None  —— 判不了：没传主题词、传的词全太短，或这条材料没有任何可比文本。
                 调用方不得把 None 当 False 用——"没取到正文"写成"对不上本题"，
                 会把一份正文里全是本题规定的文件挤到待核对那一档去。
    """
    terms = _topic_terms(topic)
    if not terms:
        return None
    texts = [item.get(f) for f in TOPIC_FIELDS]
    texts = [v for v in texts if isinstance(v, str) and v]
    if not texts:
        return None
    return _hit_in(texts, terms)


def _hit_in(texts: list, terms: list) -> bool:
    return any(t in s for s in texts for t in terms)


def _topic_terms(topic) -> list:
    """把主题词参数归一成词表；丢掉长度不足 2 的字，避免"税"这种单字到处命中。"""
    if topic is None:
        return []
    raw = (_TOPIC_SEP.split(topic) if isinstance(topic, str)
           else [str(t) for t in topic])
    return [t.strip("《》〈〉“”\"'（）() ：:") for t in raw if len(t.strip()) >= 2]


# ── 角色分层 ───────────────────────────────────────────────────────────────
# 五档是角色不是等级：一份文件"是本题的直接规定"还是"它的上位授权"，与它
# 位阶高低无关；答一道具体题目的标准配置是直接规定、上位授权、执行口径三层
# 都有，缺哪层就写明缺哪层。
ROLE_LABEL = {
    "direct": "本题的直接规定",
    "superior": "上位依据与授权",
    "practice": "执行口径与实务认定",
    "history": "政策沿革",
    "unmatched": "待核对线索",
}
ROLE_ORDER = {"direct": 4, "superior": 3, "practice": 2, "history": 1,
              "unmatched": 0}


def role_of(rank_key: str, validity: str, topic_hit) -> str:
    """这条材料在同一道题里充当什么角色。

    `unmatched` 是给"层级没判出来、主题也没对上"那条的：把它写成"上位依据与
    授权"是在替一份来历不明的材料担保层级关系，而这一组判定里恰恰两处都是空的。
    """
    if validity == "repealed":
        return "history"
    if rank_key in PRACTICE_RANKS:
        return "practice"
    if topic_hit is True:
        return "direct"
    if rank_key == "unknown":
        return "unmatched"
    return "superior"


# ── 可靠性标记的去向：提醒，不是排除 ──────────────────────────────────────
# `low` 不清零分数、不从 `pick_primary` 整组剔除，`medium` 也不只许"定位"。
# 降权是用一个小标签代替核对：用户既不知道存疑在哪一处，也无从下手。
# 这些来源信息写成一句说清"哪一处存疑、要核对什么"的提醒。
# 这两个档位只作兜底：真正说得出存疑在哪一处的，是来源随条目带下来的
# `_reliability_note`（见 `_caveats`）。同一个 `medium` 在三个来源里指的是
# 三件不同的事——NPC 正文检索按全文分词命中、总局法规库第 2 页起排序变松、
# 立法过程件不在六个源的收录范围内。把档位本身翻成某一句具体原因，就会给
# 另外两个来源的条目配一句假提醒（说成"清单靠后的页位"，而全文检索和站内
# 检索根本没有页位这件事）。
RELIABILITY_CAVEAT = {
    "low": ("来源把这一条标为可疑，召回的强弱不足以证明它规定了本题——"
            "引用前回原文确认它到底有没有规定这件事，确认结果写进答案"),
    "medium": ("来源提示这一条可能偏题，命中方式不是按标题精确对上本题——"
               "先据它定位到法规名，再按那份法规的条文引用"),
}

# 时效是唯一带"不能用"后果的轴，但后果仍然写成提醒而不是禁令。
VALIDITY_CAVEAT = {
    "repealed": "已废止，只能用于说明政策沿革，且要写明原施行期间",
    "pending": "尚未生效，不能用来回答当期问题，只能说明将来规则",
    "unknown": "时效没有录入项可判，引用前要单独核对它现在是否还在效",
}


# 实务材料（税屋、公众号）援引的那个文号，回官方库核对的结果。这一档回答的是
# "这篇解读有没有一份现行有效的法定文件托着"——它不是层级问题（解读本来不是法），
# 也不是这条材料自己的时效（文章当然"在效"），它说的是**口径的出处现在还算不算数**。
# 由 `tax_answer.check_practice_citations` 联网核对后写进 `official_status`；本层只
# 把它翻译成人话，不发网络请求。
OFFICIAL_CAVEAT = {
    "effective": ("它援引的《{title}》（{dn}）在{where}查得到，时效判为{vl}"
                  "——这条口径有现行文件托着，答案里把文号写上"),
    "repealed": ("它援引的《{title}》（{dn}）在{where}已判为{vl}"
                 "——这篇讲的是一份不再执行的规则，按现行文件重新取一遍口径"
                 "才能写进答案"),
    "pending": ("它援引的《{title}》（{dn}）在{where}尚未生效"
                "——这篇讲的是到观察时点还没开始执行的规则，当期口径仍按现行文件"),
    "unknown": ("它援引的《{title}》（{dn}）在{where}查得到这一份，但它的时效"
                "没有录入项可判——引用这条口径前要单独确认它现在是否还在效"),
    "not_in_library": ("正文援引的文号{dn}在{where}没有对上同一份文件{note}"
                       "——文号可能抄错、写法不同或不在收录范围，按这个文号回原文"
                       "或向主管税务机关核对后再用"),
    "no_citation": ("这篇实务材料没写出它依据的文号——口径要落到具体文件上，"
                    "引用前向主管税务机关确认现行执行口径"),
    "no_body": ("这一轮只取回标题或摘要、没读正文，所以不知道它援引了哪份文件"
                "——不能据此说这篇没标出处，要看口径出处得取正文再核"
                "（去掉 tax_answer 命令行上的 --no-body 重跑一遍）"),
    "not_checked": ("这一篇援引的文号{dn}没来得及核对，本次核对上限 {limit} 个文号已用满"
                    "——它是本轮没查而不是查无此件，引用前单独回库确认"),
    "lookup_failed": ("这一轮没能连上{where}，文号{dn}的存在与时效未核对"
                      "——不能把「查不到」读成「库里没有」，补一轮再定"),
}


# 核对结果的人话名。计数汇总句要用，不能让答案里出现 "not_in_library 2" 这种
# 只有读代码的人才懂的键名。与 `OFFICIAL_CAVEAT` 必须同键，同键由用例钉住。
OFFICIAL_OUTCOME_LABEL = {
    "effective": "在库且现行有效",
    "repealed": "在库但已废止",
    "pending": "在库但尚未生效",
    "unknown": "在库但时效判不出",
    "not_in_library": "库里查不到同一份",
    "no_citation": "正文未写文号",
    "no_body": "未取正文所以没读文号",
    "lookup_failed": "这一轮没连上库",
    "not_checked": "核对轮次用满没查",
}


def _library_miss_note(status: dict) -> str:
    """把"没对上"到底是怎么个没对上说出来：用的哪个词、接口给了几条命中。

    零命中与有命中但清单里没这份，读者的下一步动作不一样（前者要怀疑检索词
    的写法，后者要怀疑文号本身），所以这一句不能省。取数函数没给命中数时
    就不编，句子退回不带证据的说法。
    """
    term = status.get("searched_as", "")
    if not term:
        return ""
    hits = status.get("hits")
    if hits == 0:
        return f"（按「{term}」检索零命中：是这个词没对上，不是库里没有这一份）"
    if isinstance(hits, int):
        return f"（按「{term}」检索有 {hits} 条命中，取回的清单里没有一份文号与它相同）"
    return f"（按「{term}」检索）"


def official_caveat(status: dict) -> str:
    """把一份 `official_status` 翻译成一句提醒；没有核对结果时给空串。"""
    outcome = (status or {}).get("outcome", "")
    if not outcome:
        return ""
    tpl = OFFICIAL_CAVEAT.get(outcome)
    if not tpl:
        # 认不出的 outcome 不能也返回空串：空串在这层的含义是"这条没核对过"，
        # 漏配一句提醒就会静默消失。把 outcome 原样报出来，让人看得见。
        return (f"这一篇援引的文号{status.get('doc_number', '')}的核对结果是"
                f"未登记的「{outcome}」——本层认不出这个结果，引用前手工回库确认")
    return tpl.format(title=status.get("title", ""), dn=status.get("doc_number", ""),
                      where=status.get("where", "税务总局法规库"),
                      vl=status.get("validity_label", ""),
                      limit=status.get("limit", "未记录"),
                      note=_library_miss_note(status))


def _reliability_of(item: dict) -> str:
    """读出条目上的可靠性标记，归一成小写；没有或认不出就返回空串。"""
    rel = str(item.get("_reliability") or "").strip().lower()
    return rel if rel in RELIABILITY_CAVEAT else ""


def _caveats(rank_key: str, val: dict, topic_hit, rel: str, rel_note: str,
             official=None) -> list:
    """把这条材料身上所有"要核对什么"合成一个有序句子清单。

    顺序按"越靠近这道题的结论越先看"：能不能引（宪法特判）→ 时效 → 与本题
    对不对得上 → 它援引的官方文件现在还算不算数 → 来源自身的可靠性。
    """
    out = []
    if rank_key in NOT_DIRECTLY_QUOTABLE:
        out.append(CONSTITUTION_NOTE)
    if val["validity"] in VALIDITY_CAVEAT:
        out.append(VALIDITY_CAVEAT[val["validity"]])
    if val.get("qualified") and val.get("note"):
        out.append(val["note"])
    if topic_hit is False:
        out.append("标题与正文里都没有本题的主题词，这条是按字面召回的，"
                   "引用前先确认它规定的是不是这件事")
    if topic_hit is None:
        out.append("没有传入本题主题词，这条与题目的对应关系未经核对")
    if official:
        line = official_caveat(official)
        if line:
            out.append(line)
    if rel:
        out.append(rel_note or RELIABILITY_CAVEAT[rel])
    return out


def grade(item: dict, at: str = "", topic: str = "") -> dict:
    """给一条检索结果做完整定级：层级、时效、与本题的对应关系、要提醒什么。

    Args:
        item: 检索结果字典，至少含 title，可含 category/source/status/
            effective_date/publish_date/url/content/summary/_reliability/
            official_status（实务材料援引的文号回官方库核对的结果，
            见 `tax_answer.check_practice_citations` 与 `OFFICIAL_CAVEAT`）。
        at: 观察时点 YYYY-MM-DD。
        topic: 本题的主题词，单词或词表都行（见 `on_topic_of`）。一般是
            `resolve_tax_type` 归出的专题名、本体法名加口语短词。不传则
            `on_topic` 为 None，程序不会把它当 False 用。

    Returns:
        原字段 + rank 组、validity 组、on_topic/role 组、caveats 与 citation_hint。
        **没有 score 字段**——不做可引用性合成，理由见文件头。
    """
    rank = rank_of(item.get("title", ""), item.get("category", ""),
                   item.get("source", ""))
    val = judge_validity(item, at)
    topic_hit = on_topic_of(item, topic)
    rel = _reliability_of(item)
    role = role_of(rank["rank"], val["validity"], topic_hit)
    out = dict(item)
    out["rank"] = rank["rank"]
    out["rank_label"] = rank["label"]
    out["rank_tier"] = rank["tier"]
    out["rank_by"] = rank["by"]
    out["validity"] = val["validity"]
    out["validity_label"] = val["label"]
    out["validity_note"] = val["note"]
    out["on_topic"] = topic_hit
    out["role"] = role
    out["role_label"] = ROLE_LABEL[role]
    out["reliability"] = rel or "ok"
    out["not_directly_quotable"] = rank["rank"] in NOT_DIRECTLY_QUOTABLE
    out["caveats"] = _caveats(rank["rank"], val, topic_hit, rel,
                              item.get("_reliability_note", ""),
                              item.get("official_status"))
    out["citation_hint"] = _hint(out)
    return out


def _hint(g: dict) -> str:
    """一句人话，说明这条材料该以什么身份进答案。

    这句不带分数、也不写"只能作参考材料"，只给角色加首要提醒：
    分数是拿来排队的，不是拿来禁止引用的。
    """
    head = f"{g['role_label']}（{g['rank_label']}、{g['validity_label']}）"
    if g["caveats"]:
        return head + "；" + g["caveats"][0]
    return head + "；按现行有效条文引用"


def pick_primary(graded: list) -> dict:
    """从一组已定级的材料里挑主依据。

    规则（按角色与时效判，不按"可引用性分"降序，也不整组剔除）：
      1. 立法过程件（人大网草案、审议/征求意见公告，标 `legislative_process`）
         出局——它不是已公布的条文，这是事实层面的处置，不是降权。
      2. 不可直接援引的层级（宪法）排在所有可直接援引的材料之后：它在分层里
         照常出现（上位依据那一层本来就该有它），但不占头条。一组里只有宪法时
         仍然挑它，并在 `_why` 里写明"要落到具体法律条文上才能对税务机关用"。
      3. 其余按角色挑：本题的直接规定 > 上位依据与授权 > 执行口径与实务认定 >
         政策沿革 > 待核对线索。
      4. 同一角色内，时效判得出来的排在判不出来之前；再同则取规范层级高的
         （只在可比的两档之间比，`technical`/`interpretation` 不参与比大小）。
      5. `_reliability` 不再影响挑选，只影响这条被选中时附带哪句提醒。

    返回的 dict 带 `_why` 说明为什么选它，以及 `_runners_up` 记下其余候选。
    """
    if not graded:
        return {"_why": "没有任何依据", "_runners_up": []}

    usable = [g for g in graded if not g.get("legislative_process")]
    if not usable:
        return {
            "_why": ("本组只有立法过程线索（人大网草案、审议/征求意见公告），"
                     "不是已公布的条文，不能作为主依据引用"),
            "_runners_up": [_brief(g) for g in graded],
        }

    ordered = sorted(usable, key=_order_key, reverse=True)
    best = dict(ordered[0])
    why = (f"{best.get('role_label', '')}："
           f"{best.get('rank_label', '未定性')}、"
           f"{best.get('validity_label', '时效未标明')}")
    if best.get("not_directly_quotable"):
        why += ("；本组里没有可直接援引的法定文件，头条只能给宪法——它对税务机关"
                "不构成征税依据，答案要补一轮检索落到具体法律条文")
    if best.get("on_topic") is False:
        why += "；本组里没有一条字面上对得上本题主题词，这条是按层级与时效给的"
    if best.get("caveats"):
        why += "；" + "；".join(best["caveats"][:2])
    best["_why"] = why
    best["_runners_up"] = [_brief(g) for g in ordered[1:]]
    return best


def _brief(g: dict) -> dict:
    return {"title": g.get("title", ""), "rank_label": g.get("rank_label", ""),
            "validity_label": g.get("validity_label", ""),
            "role_label": g.get("role_label", ""),
            "on_topic": g.get("on_topic"),
            "not_directly_quotable": g.get("not_directly_quotable", False),
            "reliability": g.get("reliability", ""),
            "caveats": g.get("caveats", [])}


def _order_key(g: dict) -> tuple:
    """一条已定级材料在队列里的位置，`pick_primary` 与 `grade_all` 共用。

    两处各写一遍迟早会分叉——分叉之后"列表里第一位"和"挑出来的主依据"就不是
    同一条，答案会自相矛盾。
    """
    return (0 if g.get("not_directly_quotable") else 1,
            ROLE_ORDER.get(g.get("role"), 0),
            1 if g.get("validity") != "unknown" else 0,
            _tier(g.get("rank", "unknown")),
            1 if g.get("on_topic") is True else 0)


def grade_all(items: list, at: str = "", topic: str = "") -> list:
    """批量定级，按"可直接援引 → 角色 → 时效是否判得出 → 层级"返回（不再按分数排）。"""
    return sorted((grade(i, at, topic) for i in items),
                  key=_order_key, reverse=True)


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    import argparse
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(
        description="给一条依据定层级、时效、角色，并列出引用前要核对什么")
    p.add_argument("--rank", help="法规名或标题，判规范层级")
    p.add_argument("--at", default="", help="观察时点 YYYY-MM-DD")
    p.add_argument("--topic", default="",
                   help="本题的主题词，多个词用空格或顿号分开；不传则「与本题的"
                        "对应关系」判不了，输出里会明说这一条没核对过——"
                        "不拿标题自己顶，那是自证")
    p.add_argument("--source", default="",
                   help="来源标识，用来给实务材料兜底（如「税屋 (shui5.cn)」）")
    args = p.parse_args()

    if args.rank:
        g = grade({"title": args.rank, "source": args.source},
                  at=args.at, topic=args.topic)
        print(f"标题：{args.rank}")
        print(f"层级：{g['rank_label']}（层级序号 {g['rank_tier']}，只用于冲突裁决）"
              f"— {g['rank_by']}")
        print(f"时效：{g['validity_label']}（观察时点 {args.at or '未指定'}）"
              f"；{g['validity_note']}")
        hit = {True: "对得上本题主题词", False: "字面上对不上本题主题词",
               None: "未传主题词，这一维没判"}[g["on_topic"]]
        print(f"角色：{g['role_label']}（{hit}）")
        print(f"来源可靠性：{g['reliability']}")
        if g["not_directly_quotable"]:
            print("不可直接援引：" + CONSTITUTION_NOTE)
        if not g["caveats"]:
            print("提醒：无 —— 按现行有效条文引用即可")
        for c in g["caveats"]:
            print(f"提醒：{c}")
        print(f"一句话：{g['citation_hint']}")
        return

    p.print_help()


if __name__ == "__main__":
    main()
