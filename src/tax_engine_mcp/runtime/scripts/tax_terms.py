#!/usr/bin/env python3
"""检索词整形：把题面与文件标题压成各源真能返回条目的短词。

这里只做字符串变换，不联网、不依赖任何源客户端，所以能被离线用例逐条钉住。
放在源客户端之外是因为同一批规则有两个以上的调用方：路由要用（tax_search）、
装配检索词要用（tax_answer）、按文件全名定位也要用。

三条规则各自的由来写在对应函数上，改之前先读那一段。
"""

import re

# 选项行的起始形态：A. / A、/ A．/ (A) / （A）
_OPT_HEAD = re.compile(r"(?:^|[　\s;；])[\(（]?([A-D])[\)）、.．]")
# 文件全名：书名号里的内容
_CITED_RE = re.compile(r"[《【]([^》】\n]{4,})[》】]")
# 文号三种写法：2026年第13号 / 财税〔2009〕59号 / （2015年第48号）
_DOC_NUM_RES = (
    re.compile(r"(\d{4})\s*年第\s*([0-9一二三四五六七八九十百]+)\s*号"),
    re.compile(r"((?:财税|国税发|国税函|地税发|税总)?\s*〔\s*\d{4}\s*〕"
               r"\s*第?\s*[0-9一二三四五六七八九十百]+号)"),
)
# 发文机关前缀与文种后缀：标题里这两段最不具区分度，拿去检索只会把结果冲散
_ISSUER_PREFIX = ("国家税务总局", "财政部", "税务总局", "海关总署", "人力资源社会保障部")
# 长写在前面：core_of_title 按序取第一个命中的后缀，"暂行条例"要排在"条例"之前
_DOC_TYPE_SUFFIX = ("暂行条例", "公告", "通知", "办法", "意见", "批复",
                    "指引", "规程", "解答", "纪要", "条例")


def strip_options(text: str) -> str:
    """去掉题面里的选项部分，只留题干。

    路由靠别字的字面命中，把选项拼进来就会失焦：一道问车辆购置税的题，
    选项里出现"滞纳金"三个字，横切专题的别名比题干的税种别名更长，
    路由就把整题判给了税务行政处罚。选项是干扰项文本，本来就不该参与归类。

    只在确实看到两个以上并列的 A–D 选项标记时才截断，避免把正文里
    偶然出现的"A."当成选项头。
    """
    if not text:
        return ""
    heads = [(m.start(), m.group(1)) for m in _OPT_HEAD.finditer(text)]
    letters = {L for _, L in heads}
    if len(letters) < 2 or "A" not in letters:
        return text.strip()
    first = min(pos for pos, _ in heads)
    stem = text[:first].strip()
    return stem or text.strip()


def doc_numbers(text: str) -> list:
    """按出现顺序取出一段文字里所有文号，去重。

    `doc_number_of` 只给第一个，而核对实务材料要看整篇：文章标题里那个编号
    往往就是它自己在解读的那份文件，正文里援引的才是它口径的出处。
    """
    found = []
    for rx in _DOC_NUM_RES:
        for m in rx.finditer(text or ""):
            found.append((m.start(), re.sub(r"\s+", "", m.group(0))))
    out, seen = [], set()
    for _, dn in sorted(found):
        if dn not in seen:
            seen.add(dn)
            out.append(dn)
    return out


def doc_number_of(text: str) -> str:
    """从一段文字里取出第一个文号，取不到给空串。"""
    return doc_numbers(text)[0] if doc_numbers(text) else ""


def cited_documents(text: str) -> list:
    """从题面里取出用户点名的文件：标题 + 文号。

    用户把文件全名和文号都写出来时，检索目标就不是"某个税种的规定"，
    而是这一份文件本身。这时候还按税种别名去取本体法，取回来的条文
    与用户要的那份公告没有关系，答案看着完整、实则答非所问。
    """
    body = re.sub(r"\s+", "", text or "")
    out = []
    for title in _CITED_RE.findall(text or ""):
        title = re.sub(r"\s+", "", title)
        if not title:
            continue
        out.append({"title": title,
                    "doc_number": doc_number_of(body),
                    "core": core_of_title(title)})
    return out


def core_of_title(title: str) -> str:
    """剥掉标题的发文机关前缀与文种后缀，留下真正有区分度的中段。

    "国家税务总局关于企业重组业务所得税处理有关征管问题的公告" 与
    "企业重组业务所得税处理有关征管问题" 检索到的东西是一样的，前者还多
    一串会在标题里到处出现的通用字。
    """
    t = title.strip()
    for p in _ISSUER_PREFIX:
        if t.startswith(p):
            t = t[len(p):]
    t = re.sub(r"^[的与和及、，,]", "", t)
    if t.startswith("关于"):
        t = t[2:]
    for s in _DOC_TYPE_SUFFIX:
        if t.endswith(s):
            t = t[:-len(s)]
            break
    t = re.sub(r"[（）()【】\[\]]", "", t)
    t = re.sub(r"^(有关|关于)", "", t)
    t = re.sub(r"(的?有关|的?相关|的?若干)*问题$", "", t)
    t = t.rstrip("的地")
    return t or title.strip()


def title_candidates(title: str, max_len: int = 6) -> list:
    """给出定位一份文件时该依次尝试的检索词（由长到短）。

    长词与短词各管一头：长词精确，但要求库里的标题字面含着整串——用户给的
    标题常常缺发文机关前缀或词序不同，整串就对不上；短词召回广，实测「企业
    重组业务所得税处理」检索返回 3 条且首位就是目标公告，而「企业重组」返回
    103 条、目标公告不在取回的那几页里。所以先发长词，取不到再逐级缩短，
    最后用标题字符命中率把目标文件从一堆同域文件里挑出来。

    返回的每个候选都已经去重且非空，调用方按顺序试即可。
    """
    core = core_of_title(title)
    out = []
    # 第四个候选是"短词＋文种"：只有 core 真被截短过才补文种，
    # 否则 core 本来就短，再加文种只会把「增值税」冲成「增值税 暂行条例」，
    # 反而多一个宽泛词。
    cands = [core, core[:max_len], core[:4]]
    if len(core) > 4:
        cands.append(core[:4] + " " + _doc_type_of(title))
    for cand in cands:
        cand = cand.strip()
        if cand and cand not in out and len(cand) >= 2:
            out.append(cand)
    return out


def _doc_type_of(title: str) -> str:
    for s in _DOC_TYPE_SUFFIX:
        if title.endswith(s):
            return s
    return "公告"


def title_similarity(want: str, got: str) -> float:
    """两个标题的字符命中率：want 里有多少字出现在 got 里。

    定位用户点名的文件时靠这个排序，而不是靠接口的默认相关度——接口对
    短词返回的是整个主题域的文件，命中目标那份要靠标题字面的重合程度认。

    但这一把尺只能用来排序，不能用来判定"就是这一份"：它量的是 want 的字
    有没有出现在 got 里，而"被点名"与"在标题里引用了被点名的东西"在这把尺上
    读数相同。判定用下面的 title_identity。
    """
    a = re.sub(r"\s+", "", want or "")
    b = re.sub(r"\s+", "", got or "")
    if not a or not b:
        return 0.0
    return sum(1 for ch in set(a) if ch in b) / len(set(a))


# 文种：标题末尾那两个字决定了"这是同一份文件"还是"另一份文件提到了它"。
# 长写在前面（暂行条例先于条例），否则 暂行条例 会被 条例 抢先截掉。
_DOC_TYPES = ("法", "暂行条例", "实施细则", "条例", "办法", "公告", "通知",
              "批复", "意见", "规程", "指引", "解答", "纪要")
# 缩略同一允许的长度余量：「税收征管法」↔「税收征收管理法」差 2 个字。
# 超出这个余量就不再是漏字，而是另一份更长的文件（那条 40 字的引用式通知）。
_ABBREV_SLACK = 4
_PUNCT_RE = re.compile(r"[\s《》〈〉“”\"'（）()【】\[\]、，,。．.·\-]")


def _doc_type_of_title(title: str) -> str:
    """标题以哪个文种收尾；认不出返回空串（不用默认值，默认值会造假冲突）。"""
    for s in _DOC_TYPES:
        if title.endswith(s):
            return s
    return ""


def _is_subsequence(short: str, long: str) -> bool:
    """short 的字是否按顺序散落在 long 里（不必相连）。"""
    it = iter(long)
    return all(ch in it for ch in short)


def title_identity(want: str, got: str) -> str:
    """got 这条标题与用户点名的 want 是什么关系："" 不是、"same" 字面同一、
    "abbrev" 缩略同一（漏字简称）。

    为什么需要这一层：实测点名「税收征管法」时，总局法规库按短词召回的六条标题
    里没有一条是《中华人民共和国税收征收管理法》，而命中率 1.0 的那条是
    《国家税务总局关于农业税、牧业税、耕地占用税、契税征收管理暂参照
    《中华人民共和国税收征收管理法》执行的通知》（国税发〔2001〕110号）——
    它只是在标题里引用了那部法。把这种条目当"用户点名的文件本身"，主依据就落在
    一份已废止的 2001 年通知上（19.2 分），而 90.0 分的现行有效法律被挤到并列位。

    三条判据：
      - 文种不同直接否。这一条单独就足以挡住上面那个事故（法 ↔ 通知）。
      - same：去掉发文机关前缀与文种后缀之后的核心标题互相包含。
      - abbrev：核心标题按字序呈子序列关系，且长度差不超过 _ABBREV_SLACK。
        子序列不看连续，所以「征管」与「征收管理」能对上；长度差是必需的，
        不然那条 40 字的引用式通知同样把「税收征管法」五个字按顺序排得下。
    """
    a = core_of_title(_PUNCT_RE.sub("", want or ""))
    b = core_of_title(_PUNCT_RE.sub("", got or ""))
    a = re.sub(r"^中华人民共和国", "", a)
    b = re.sub(r"^中华人民共和国", "", b)
    if not a or not b:
        return ""
    # 文种取在原始标题上：core_of_title 会把 通知/办法/条例 这类后缀截掉，
    # 在核心标题上比就永远比不出冲突。
    ta = _doc_type_of_title(_PUNCT_RE.sub("", want or ""))
    tb = _doc_type_of_title(_PUNCT_RE.sub("", got or ""))
    if ta and tb and ta != tb:
        return ""
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if short in long:
        return "same"
    if (len(short) >= 4 and len(long) - len(short) <= _ABBREV_SLACK
            and _is_subsequence(short, long)):
        return "abbrev"
    return ""

