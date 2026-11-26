#!/usr/bin/env python3
"""
Tax Web Search — 国家税务总局站点检索。

检索接口用的是 www.chinatax.gov.cn 的 search5 检索服务（JSON），
站点编号 siteCode=bm29000002，覆盖总局站点与 fgk 法规库
（结果 url 中出现 fgk.chinatax.gov.cn 即为法规库条目）。

旧的 /was5/web/search 接口已下线：实测返回 HTTP 404 并把首页 HTML
当成响应体（144,077 字节），无法据此解析结果，故不再使用。

Usage:
  python tax_web_search.py "增值税" --size 10
  python tax_web_search.py "小微企业优惠" --size 10 --json
"""

import argparse
import datetime
import json
import math
import re
import sys
import time
from html import unescape
from typing import Optional

import requests
import urllib3

import tax_http

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.chinatax.gov.cn"
SEARCH_URL = f"{BASE_URL}/search5/search/s"
# 总局站点编号，检索结果同时覆盖 fgk.chinatax.gov.cn
SITE_CODE = "bm29000002"
# 法规库条目标识，用于给结果分层
FGK_MARKER = "fgk.chinatax.gov.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": f"{BASE_URL}/",
}
TIMEOUT = 20

_DOC_NUM_RE = re.compile(
    r'(财政部[、\s]*税务总局公告\d{4}年第\d+号|'
    r'国家税务总局公告\d{4}年第\d+号|'
    r'财税\[\d{4}\]\d+号|'
    r'税总发\[\d{4}\]\d+号)'
)
_TAG_RE = re.compile(r"<[^>]+>")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")
_NO_RE = re.compile(r"^\d{1,5}$")

# 关联接口与相对正文链接各自的域名。POST queryManuscriptAssociation 只在 www
# 域返回 200（实测同一 id 打 fgk 域回 404），而它返回的 /zcfgk/… 相对链接要拼
# fgk 域才取得到正文（拼 www 回 404）。两个域名不能混用。
CHINATAX_HOST = "https://www.chinatax.gov.cn"
FGK_HOST = "https://fgk.chinatax.gov.cn"

# ── search5 的 label 维：检索落在哪些内容类型上 ──
# 不发这一维等于搜全站。本机 2026-10-02 实测「小微企业」全站 4822 条，首屏十条的
# 类型是 亚洲 / 各地动态×3 / 媒体视点×4 / 视频图解 / 视频政策解读，没有一条是文件；
# 只发下面十个「文件类」标签时同一检索词 184 条，首屏十条全在 其他文件 / 工作通知 /
# 财税文件 / 文字政策解读 上。名单里留着「文字政策解读」，所以收窄不会把官方解读
# 一起挡掉（规则陈述类与其余法定依据那一栏要用它）。
FILE_LABELS = ("法律,行政法规,国务院文件,税务部门规章,税务规范性文件,"
               "财税文件,其他文件,工作通知,政策指引,文字政策解读")
# 显式要全站时的值（命令行 --all-labels，界面 file_only=false）。
ALL_LABELS = ""

# ── 时效与效力等级两维的取值域 ──
# 每个值都实测过"发出去真的会收窄"，基线是 增值税×FILE_LABELS（这一档随上游录入在漂：
# 2026-10-02 读到 1908、2026-10-20 读到 1910，下面那两行单发读数取的是 1908 那一次）：
#   xxgkAging        全文有效 252 / 已修改 163 / 全文失效 9 / 全文废止 318 / 尚未生效 1
#   xxgkEffectLevel  法律 9 / 行政法规 10 / 国务院文件 9 / 税务部门规章 30 /
#                    税务规范性文件 681 / 财税文件 636 / 其他文件 61 / 工作通知 190
# 域外的值不发出去，而是在 build_filters 里报错：接口对不认识的值回 total=0（实测
# xxgkEffectLevel=法规 与 xxgkAging=有效 两发都回 0，同一次跑的基线是 1910），
# 它与"这一维确实没有内容"在响应里是同一个形态——放过去等于把"没筛成功"读成"筛出来是空"。
AGING_VALUES = ("全文有效", "已修改", "全文失效", "全文废止", "尚未生效")
EFFECT_LEVEL_VALUES = ("法律", "行政法规", "国务院文件", "税务部门规章",
                       "税务规范性文件", "财税文件", "其他文件", "工作通知")

# 八档效力等级里有三档名下**根本不录时效**，与 xxgkAging 同发必然 0 条。
# 2026-11-25 二十四发实测（检索词「增值税」× FILE_LABELS 基线 1910，逐发间隔 2.6 秒，
# 读 searchResultAll.total 与同格 agingList；另换「留抵退税」基线 58 交叉复认三档同发仍为 0）：
#   单发带时效桶的五档 —— 同发正常：法律 9→8、行政法规 10→7、国务院文件 9→7、
#                          税务部门规章 30→15、税务规范性文件 681→216（配「全文有效」）
#   单发只有空桶的三档 —— 同发归零：财税文件 637（agingList 空串 462 + "null" 191）→ 0、
#                          其他文件 62（只有空串 63）→ 0、工作通知 190（只有空串 190）→ 0
# 归零跟着效力等级这一族走、不跟着检索词走，所以能在发之前判死：这三档一旦与时效同拼，
# 接口回的是 total=0，与「这一维确实筛空了」「库里没有」在响应里同一个形态。
AGING_UNRECORDED_LEVELS = ("财税文件", "其他文件", "工作通知")

# orderBy 的取值与实测语义。日期倒序是 1，不是别处文档写的 2：发 1 时三个检索词的
# 成文日期序列都单调递减（增值税自 2026-09-04 起、小微企业、研发费用同）；发 2 时
# 首屏日期在 2026-01-30 与 2026-02-10 之间来回跳，且同一类别先聚到一起，它排的是类别。
ORDER_VALUES = {"relevance": "5", "date_desc": "1", "category": "2",
                "date_asc": "3"}


def label_scope_text(file_only: bool) -> str:
    """把 file_only 翻成一句人话，供命令行回显实际生效的检索范围。"""
    return ("文件类白名单（十类，不含新闻/视频/各地动态）" if file_only
            else "全站（含新闻、视频、各地动态）")


def order_text(order: str) -> str:
    """把 order 名翻成「语义 + 上线值」，让回显能核对到接口参数。"""
    return f"{order}（orderBy={ORDER_VALUES[order]}）"


def keyword_terms(keyword: str) -> list:
    """按空白切出检索词里的各枚词，丢掉空白段。"""
    return [t for t in re.split(r"\s+", (keyword or "").strip()) if t]


def keyword_note(keyword: str, results: list, total: int) -> str:
    """多枚词拼成一串时，用已经取回的那一页说清本轮由哪枚词主导。不多发请求。

    search5 对空格分隔的多词按**任一词命中**，不是 AND。2026-11-10 本机实测
    （size=10，FILE_LABELS 默认档，逐发读数见 search_source/chinatax/NOTE.md）：
    「碳排放权 增值税」1911 条、「碳排放权」1 条，前者首屏十条全是小规模纳税人
    减免增值税公告，标题含「碳排放」0 条；加 wordPlace=1 收到 909 条、首屏仍是
    同一批；加 participleRule=5 收到 73 条，单独发「碳排放权」命中的那一份
    （c100013/c5194808）升到首屏第 3 位，但它的标题里不含专题词，按标题计数仍是 0。
    所以"专题词＋税种词"这种拼法必然被基数大的那枚词顶掉，而仅标题匹配改的是
    匹配域、不改这个语义。

    只在"多枚词且至少一枚在首屏标题里 0 命中"时出声：单词检索、几枚词都命中、
    以及真的 0 条这三种情形都不该多一句提醒。计数只看标题，所以结论也只能说到
    标题这一层——正文命中的那一份可能就在首屏里，写成"首屏没有条目"会把它说没。
    """
    terms = keyword_terms(keyword)
    if len(terms) < 2 or not results:
        return ""
    counts = {t: sum(1 for r in results if t in (r.get("title") or "")) for t in terms}
    absent = [t for t in terms if not counts[t]]
    if not absent:
        return ""
    present = [t for t in terms if counts[t]]
    tally = "、".join(f"「{t}」{counts[t]} 条" for t in terms)
    head = (f"检索词「{keyword}」含 {len(terms)} 枚词，search5 按任一词命中、不是 AND："
            f"取回的 {len(results)} 条标题里 {tally}，接口报命中 {total} 条。")
    if present:
        middle = (f"本轮由{'、'.join(f'「{t}」' for t in present)}主导，"
                  f"{'、'.join(f'「{t}」' for t in absent)}在首屏标题里没有条目。")
    else:
        middle = (f"{'、'.join(f'「{t}」' for t in absent)}都没出现在首屏标题里，"
                  f"命中来自正文或摘要，标题这一层看不出本轮召回的是什么。")
    return (head + middle
            + f"要按专题取就单独发「{absent[0]}」；要几枚词都命中加 --precise（精准分词）。"
              f"--in-title 只把匹配域收到标题，不改任一词命中的语义。")


def build_filters(in_title: bool = False, precise: bool = False,
                  tax_type: str = "", doc_type: str = "", doc_year: str = "",
                  doc_no: str = "", cwrq_from: str = "", cwrq_to: str = "",
                  aging: str = "", effect_level: str = "") -> dict:
    """把可选的收窄维度拼成 search5 的过滤参数 dict。

    只发填了的维度，返回的 dict 直接喂给 search_chinatax(filters=…)。各项取值
    来自本机单发实测：开关清单在 references/commands.md，逐档取值域与单发
    读数在 search_source/chinatax/NOTE.md。不要凭记忆改。哪些维度真的会被发出去
    分两种：范围／匹配／日期／时效
    （aging）由网页界面那四个控件驱动（tax_server._ui_filters），效力等级与文种、
    税种那几组目前只有命令行用得到（--effect-level / --doc-type / --tax-type）。

    Args:
        in_title: 仅标题匹配（wordPlace=1，默认 0 是全文）
        precise: 精准分词（participleRule=5，默认 0 是模糊）
        tax_type: 税种分面，如「增值税」（xxgkSonTaxPolicy）
        doc_type: 文种，如「财政部税务总局公告」（docType）
        doc_year: 发文字号里的年份四位（docYear，不是成文年份）
        doc_no: 文号数字（docNo）
        cwrq_from / cwrq_to: 成文日期区间，YYYY-MM-DD（cwrqStart/cwrqEnd）
        aging: 时效，取值限 AGING_VALUES 五项（xxgkAging）
        effect_level: 效力等级，取值限 EFFECT_LEVEL_VALUES 八项（xxgkEffectLevel）

    doc_type 会去掉内部空白：接口对带空格的写法是宽松误命中（实测「财政部 税务总局
    公告」配 docYear 归 0，去空格「财政部税务总局公告」才收窄到本尊），页面上印的
    是带空格的，用户很可能直接抄过来，所以这里替他去空格。

    Raises:
        ValueError: 日期/年份/编号格式不合法；时效或效力等级不在实测过的取值域里；
            或时效与 AGING_UNRECORDED_LEVELS 那三档效力等级同发——那三族条目名下
            不录时效，一起发必回 0 条，而这与"库里没有"长得一模一样。
    """
    out: dict = {}
    if in_title:
        out["wordPlace"] = "1"
    if precise:
        out["participleRule"] = "5"
    if tax_type:
        out["xxgkSonTaxPolicy"] = tax_type.strip()
    if aging:
        a = aging.strip()
        # 填了内容却 strip 成空，与"根本没填"不是一回事：那多半是复制粘贴带进来的
        # 空白或占位符，静默当成不发这一维就等于把用户以为筛过的事实抹掉。
        if not a:
            raise ValueError(f"aging 只有空白字符（{aging!r}）——"
                             "要发这一维就给 %s 之一，不要就整个不填"
                             % "、".join(AGING_VALUES))
        if a not in AGING_VALUES:
            raise ValueError(f"aging 只认 {AGING_VALUES}，收到 {aging!r}——"
                             "域外的值发出去接口回 total=0，与这一维真的筛空了分不开")
        out["xxgkAging"] = a
    if effect_level:
        e = effect_level.strip()
        if not e:
            raise ValueError(f"effect_level 只有空白字符（{effect_level!r}）——"
                             "要发这一维就给 %s 之一，不要就整个不填"
                             % "、".join(EFFECT_LEVEL_VALUES))
        if e not in EFFECT_LEVEL_VALUES:
            raise ValueError(f"effect_level 只认 {EFFECT_LEVEL_VALUES}，收到 "
                             f"{effect_level!r}")
        out["xxgkEffectLevel"] = e
    # 两维同发的程序拦截：判据与逐档读数见上面 AGING_UNRECORDED_LEVELS 那段。
    # 只拦这三档，另外五档同发是正常交集（实测法律 9→8、税务规范性文件 681→216），
    # 一并拦掉就是把能用的组合也堵死。
    if "xxgkAging" in out and out.get("xxgkEffectLevel") in AGING_UNRECORDED_LEVELS:
        raise ValueError(
            f"effect_level={out['xxgkEffectLevel']!r} 这一族条目名下不录时效，"
            f"与 aging={out['xxgkAging']!r} 同发必然回 0 条（2026-11-25 二十四发实测："
            "财税文件 637、其他文件 62、工作通知 190 单发都有货，配任一时效值都归零）——"
            "两个一起筛要换一个维度：按效力等级取清单就别带时效，时效回来读条目里的 "
            "status；确实要「现行有效的某族文件」，用 tax_type 那一维配时效（实测 "
            "xxgkSonTaxPolicy=增值税 + 全文有效 正常给 159）")
    if doc_type:
        out["docType"] = re.sub(r"\s+", "", doc_type)
    if doc_year:
        y = doc_year.strip()
        if not _YEAR_RE.match(y):
            raise ValueError(f"doc_year 要四位年份，收到 {doc_year!r}")
        out["docYear"] = y
    if doc_no:
        n = doc_no.strip()
        if not _NO_RE.match(n):
            raise ValueError(f"doc_no 要 1-5 位数字，收到 {doc_no!r}")
        out["docNo"] = n
    for key, val in (("cwrqStart", cwrq_from), ("cwrqEnd", cwrq_to)):
        if not val:
            continue
        d = val.strip()
        # 先要形状是补零的 YYYY-MM-DD（strptime 会放过 2024-1-1 这种非补零写法，
        # 而接口那侧要的是补零形态），再要它是个真实日期（挡住 2024-13-01 等越界）。
        if not _DATE_RE.match(d):
            raise ValueError(f"{key} 要 YYYY-MM-DD（补零）格式，收到 {val!r}")
        try:
            datetime.datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"{key} 不是真实日期，收到 {val!r}")
        # 接口这一维要完整时间戳：只给日期时日期值不参与比较，这一发换成"这一维有没有值"
        # 的过滤——全站基数下「2024 全年」「只发起点」「只发终点」「2025 上半年」四种裸写法
        # 同回 2432，带整点时间戳才按区间收到 207。逐发读数见 search_source/chinatax/NOTE.md 第三节。
        out[key] = d + (" 00:00:00" if key == "cwrqStart" else " 23:59:59")
    return out


def column_unhonoured(column: Optional[str], payload: dict) -> bool:
    """发了栏目号码、站点却没认这个值——判据只有一条：响应根上的回显。

    实测（2026-11-25，九路对照，逐发读数见 references/source_defects.md 的
    「案例通道实测台账」栏目维那一行与 search_source/chinatax/NOTE.md）：

    - `column=5739`（总局概况，这一主题下真 0 条）⇒ `columnId="5739"`、
      `column="总局概况"`、total=0。**认了，且确实是空栏目。**
    - `column=9999` / `0` / `-1`（不存在的号码）⇒ HTTP 200 但**响应体 0 字节**，
      连 JSON 都不是；这一档在 `r.json()` 那一步就回 _error，走不到这里。
    - `column=abc`（不是号码）⇒ 合法 JSON、total=0、**`columnId` 为 None**、
      `column` 原样回显 "abc"。
    - `column=不存在的栏目名` ⇒ 同上：total=0、`columnId=None`、回显原样。

    所以「发了号码而 `columnId` 不等于它」就是站点没认这个值，不能读成
    「这个栏目里没有」。这一判据只用于**发出去的是号码**（纯数字串）的场合：
    名字那一维（`column=政策法规` 这类分面名）认了也不回 `columnId`——实测
    `column=政策法规` 回 total=41 而 `columnId=None`，所以发名字时分不清
    「没认」与「确实没有」，这里一律回 False，不假装能判。

    不写进判据的两枚读数：`classification` 缺 `style` 键只分得出走没走号码
    那条路（abc 与不存在的栏目名都缺，5739 真 0 条不缺），但它与 columnId
    同源、不如 columnId 直白；响应字节数（无效值 1.0–1.1 万 vs 合法号 2.3 万）
    是脆弱读数，改版就漂。
    """
    if not column or not column.strip().isdigit():
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get("columnId") or "") != column.strip()


def column_echo_absent(column: Optional[str], payload: dict) -> bool:
    """发的是栏目名（或乱写的值）而响应没回显 columnId——这一维认没认，程序分不出。

    与上一枚判据是一对：`column_unhonoured` 只在发号码时下硬结论，这一枚负责
    把「发名字取到 0 条」这种**分不清**的情形说出口，不假装能判。实测依据
    （2026-11-25，probe32g/32h/32i）：

    - `column=政策法规`（分面里的名字、不在 columns 表里）⇒ total=41、
      **columnId=None**、`column` 原样回显。这一维认了，也不回 columnId。
    - `column=abc` 与 `column=不存在的栏目名` ⇒ 同样 columnId=None、原样回显，
      与上面那一发**同形**。
    - `column=5739`（表里的合法号、这一主题下真 0 条）⇒ **columnId='5739'**。

    所以名字那一维的 0 条，既可能是「这个栏目真没有」，也可能是「站点没接这个
    名字」，程序拿不到区分依据（classification 缺 style 那一枚与字节数都不稳）。
    判据只说「分不清、换号码重发」，不说哪一种是真相。
    """
    if not column or column.strip().isdigit():
        return False
    if not isinstance(payload, dict):
        return False
    return not str(payload.get("columnId") or "")


def search_chinatax(keyword: str, page: int = 1, size: int = 10,
                    filters: Optional[dict] = None, file_only: bool = True,
                    order: str = "relevance", column: Optional[str] = None) -> dict:
    """
    检索国家税务总局站点。

    Args:
        keyword: 检索词
        page: 页码，从 1 开始（对应接口的 pageNum 从 0 开始，见下）
        size: 返回条数上限
        filters: build_filters 产出的收窄维度，直接并入请求参数；为空不发
        file_only: 默认 True，只在这十个「文件类」标签里检索（见 FILE_LABELS 那段
            实测）；要连新闻、视频、各地动态一起搜时传 False
        order: 排序，取值限 ORDER_VALUES：relevance（默认）/ date_desc（最新在前的
            成文日期倒序）/ category / date_asc
        column: 栏目号，如 "5741"（新闻发布）。默认 None 与原行为一致——发空串
            等于不限栏目。要用栏目收窄时给值；这一维与 label 不一样：实测
            2026-10-04 发 column=5741 时「骗取出口退税」189 条收窄到 119 条。
            2026-11-25 把整张栏目表量到手（响应根上 columns 那八枚）：
            5738 全部 / 5739 总局概况 / 5740 信息公开 / 5741 新闻发布 /
            5742 税收政策 / 5743 纳税服务 / 5744 税务视频 / 5745 互动交流，
            同一检索词「骗取出口退税」发 5742 得 7 条、发 5745 得 8 条——旧记录
            那句「税收政策栏仅 7、互动 8」当场对上，只是当时号没取到。三件事要记：
            ①发号码与发栏目名同效（column=税收政策 与 column=5742 都回 7 条，
            站点自己把名字换成号并回显 columnId）；②「全部」不是并集，单栏能大于
            不限栏目（5743 配「增值税」回 13376 > 基线 13225）；③发了号码而响应
            columnId 没回显它，是站点没认这个值，不是栏目里空——判据见
            column_unhonoured，这一路的 0 条写成 _empty_reason、不打 _zero_hit。
            label 收的是条目自带的标签名（响应分面 labelList 那 159 桶里的名字），
            发这一桶里不存在的名字回 0 条、不回基线：2026-10-20 复测「增值税」
            发 label=新闻 回 0，换成站点真实的那枚「新闻发布」回 2 条。所以案例
            检索走 column 的依据是栏目覆盖得全（同一枚「新闻发布」在 columnList
            是 12,924 条），不是"label 只认文件类那十枚"
            （逐发读数见 references/source_defects.md 与 search_source/chinatax/NOTE.md）。

    页码基准：search5 的 pageNum 从 0 起算。实测同一检索词「企业重组」
    发 pageNum=0 与 pageNum=1 各回 10 条、url 交集为空，且 pageNum=0 那组
    才是相关度最高的一页；发 pageNum=1 等于整轮检索永远丢掉首屏，命中数
    不足 10 条时（如「企业重组业务所得税处理」共 3 条）第一页就是唯一一页，
    取回的空列表会被上层读成"库里没有这份文件"。本函数对外仍按 1 起算，
    发请求时减 1。NPC 法规库那个接口（tax_search.py）经实测是从 1 起算，
    两边基准不同，不要照抄。

    Returns:
        {"keyword","total","results","searched_at","source","_error"?,"_from_cache",
         "_attempts","filters"?,"_filter_note"?,"_keyword_note"?}
        total 是检索命中的总条数（可能远大于 results 长度）；
        total 为 0 时若有 _error，说明是请求失败而非无结果。
        _attempts 是这一轮发过的每一次请求：`(host, 异常类名或 HTTP 状态码,
        耗时 ms, 第几次尝试)`，由 `tax_http.attempt` 记账，成本归因看它。
        传了 filters 时把 filters 原样回显；filters 收窄到 0 条且非请求失败时，
        补一句 _filter_note 说明是维度拼窄了还是库里真没有。
        检索词含多枚词而首屏标题缺其中一枚时补一句 _keyword_note：search5 对
        空格拼串按任一词命中，这一句只用已经取回的这一页算，不多发请求。
        条目侧的 status_note（3.62.29）是接口录入项 xxgk_description 的原文，
        装「依据哪份文件、从哪天起全文废止/失效/被修改」；这一栏不结构化，
        判某日是否已废止仍要读正文或走现行/废止版配对，理由见 _status_note_of。
    """
    if order not in ORDER_VALUES:
        raise ValueError(f"order 只认 {tuple(ORDER_VALUES)}，收到 {order!r}")
    params = {
        "siteCode": SITE_CODE,
        "searchWord": keyword,
        "type": "1",
        # 接口这一维是从 0 起算的页码，本项目对外的 page 从 1 起算，
        # 所以发请求时要减回去。
        "pageSize": max(size, 10),
        "pageNum": max(page, 1) - 1,
        "orderBy": ORDER_VALUES[order],
        "column": column or "",
        "label": FILE_LABELS if file_only else ALL_LABELS,
    }
    params.update(filters or {})

    log = tax_http.new_log()
    # 一次不通与三次不通要分得开：这一路本轮实测过 8 次 SSLError，原先一发就弃，
    # 上层只能把"瞬时没连上"读成"这轮检索没结果"。重试与留痕都在 tax_http.attempt。
    r, err, _blocked = tax_http.attempt(
        lambda: requests.get(SEARCH_URL, params=params, headers=HEADERS,
                             timeout=TIMEOUT, verify=False),
        SEARCH_URL, log)
    if err:
        return _empty_result(keyword, err, filters, attempts=log)
    if r.status_code != 200:
        return _empty_result(keyword, f"HTTP {r.status_code}", filters, attempts=log)

    try:
        payload = r.json()
    except ValueError as e:
        return _empty_result(keyword, f"响应不是 JSON: {e}", filters, attempts=log)

    block = payload.get("searchResultAll") or {}
    items = block.get("searchTotal") or []
    total = block.get("total") or 0

    results = []
    for it in items[:size]:
        url = (it.get("url") or "").strip()
        title = _clean(it.get("title") or "")
        if not url or not title:
            continue
        content = _clean(it.get("content") or "")
        # 文号：接口把结构化文号放在 govDoc 里，比从标题正则抠更可靠
        # （标题常不含文号，而 govDoc.docNum 是录入项）。实测 2026 年第 13 号
        # 公告的 docNum = "国家税务总局公告2026年第13号"。
        gov = it.get("govDoc") or {}
        doc_num = (gov.get("docNum") or "").strip() or \
            _first_match(_DOC_NUM_RE, title) or _first_match(_DOC_NUM_RE, content)
        row = {
            "title": title,
            "url": url,
            "date": (it.get("pubDate") or "")[:10],
            "document_number": doc_num,
            "snippet": content[:200],
            "publisher": it.get("pubName") or "",
            "source": "chinatax.gov.cn",
            "source_label": "税务总局法规库" if FGK_MARKER in url else "税务总局",
        }
        # 时效与效力级别是接口的录入项（xxgk_aging / xxgk_effectLevel），只有
        # 政策法规条目会填，解读和新闻这两栏为空。带上它们，法规库条目才有
        # 明文时效可判——不带的话每条都只能报"时效未标明"。
        aging = aging_of(it.get("xxgk_aging"))
        if aging:
            row["status"] = aging
            row["status_from"] = "法规库录入项 xxgk_aging"
        eff_level = (it.get("xxgk_effectLevel") or "").strip()
        if eff_level:
            row["effect_level"] = eff_level
        cwrq = (it.get("cwrq") or "")[:10]
        if cwrq:
            row["publish_date"] = cwrq
        atts = _attachments(it.get("appendix"))
        if atts:
            row["attachments"] = atts
        # 时效处置说明（xxgk_description）：这一栏装的是「依据哪份文件、从哪一天
        # 起全文废止／失效／被修改」的站点原文，判据与限长见 _status_note_of。
        note = _status_note_of(it.get("xxgk_description"))
        if note:
            row["status_note"] = note
            row["status_note_from"] = "法规库录入项 xxgk_description"
        results.append(row)

    out = {
        "keyword": keyword,
        "total": total,
        "results": results,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "chinatax.gov.cn",
        "_from_cache": False,
        "_attempts": log,
    }
    if filters:
        out["filters"] = filters
    note = keyword_note(keyword, results, total)
    if note:
        out["_keyword_note"] = note
    # 命中数大于 0 但这一页没有条目。两种成因要分开写，处置完全不同：
    # 页号越过末页（每页固定 10 条，命中 3 条时只有第 1 页）是正常收尾；
    # 第 1 页就空则是接口这一轮没给清单，不能读成"库里没有"。
    # 空页都不是请求失败，所以不进 _error。
    if total and not items:
        last_page = math.ceil(total / 10)
        if page > last_page:
            out["_empty_reason"] = (
                f"已翻过末页：命中 {total} 条只占 {last_page} 页，"
                f"第 {page} 页本来就空，按已取回的清单下结论即可")
            # 翻过末页是正常收尾：本轮的合法空档，契约里打 _zero_hit。
            out["_zero_hit"] = True
        else:
            out["_empty_reason"] = (
                f"接口报告命中 {total} 条，第 {page} 页却没给条目清单"
                f"（该页在末页之内，不是翻页越界）；换一个检索词再取一轮"
                f"才有结论，不能据此说库里没有")
    # 发了栏目号码却 0 条，而响应没把这一维认回去（columnId 不等于发出去的号）：
    # 站点根本没按这一维筛，"0 条"说的是它没接这个值，不是栏目里没有。3.62.32
    # 实测的三种无效写法（9999 走 0 字节 _error、abc 与不存在的栏目名走这里）
    # 与真 0 条的合法号（5739 回显 columnId='5739'）就是靠这一支分开的。
    # 这一支不打 _zero_hit：那枚键的语义是"本轮请求成功、答案真是 0 条"。
    elif column_unhonoured(column, payload) and not out.get("_error"):
        out["_empty_reason"] = (
            f"发出的栏目号 {column} 没有被站点认下（响应 columnId 没回显它）："
            f"这一路的 0 条说的是接口没接这个值，不是「{column}」栏目里没有；"
            f"栏目号与栏目名以响应根上 columns 那张表为准，改值重发才有结论")
    # 发的是栏目名而响应不回显 columnId：这一维认没认，程序拿不到区分依据。
    # 实测「政策法规」（认，回 41 条）与「不存在的栏目名」（不认，回 0 条）在
    # columnId 这一枚上同形，所以只能说分不清，不能替站点选一种。
    elif column_echo_absent(column, payload) and not out.get("_error"):
        out["_empty_reason"] = (
            f"发出的栏目名「{column}」这一维站点没有回显 columnId，"
            f"所以这一路的 0 条分不清是「这个栏目真没有」还是「接口没接这个名字」"
            f"（实测合法栏目名与乱写的名字都不回 columnId）；改用响应根上 columns "
            f"那张表里的栏目号重发才分得开")
    # 传了收窄维度却 0 条，且不是请求失败。接口对拼窄与库里真没有给的是同一个
    # total=0，程序分不出，只能把这句话递出去，让上层决定放宽哪一维——别把
    # "这一维拼过头"当成"库里没有这份文件"。实测效力等级 × 时效两维同时发必然 0。
    elif filters and total == 0 and not out.get("_error"):
        out["_filter_note"] = (
            f"在检索词「{keyword}」上叠加了 {len(filters)} 个收窄维度后命中 0 条；"
            f"接口对「维度拼窄」与「该库没有」回的是同一个 0，分不清。放宽一维重取"
            f"才有结论（发出去的是 {filters}）")
    elif not items and not out.get("_error"):
        # 没拼维度、接口也没报命中、页面也没给条目：这一轮请求成功、答案是 0 条。
        out["_zero_hit"] = True
    return out


def _clean(fragment: str) -> str:
    """去高亮标签、解实体、压空白。检索结果标题里带 <span> 标记命中词。"""
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub("", fragment))).strip()


SUGGEST_URL = f"{CHINATAX_HOST}/search5/search/suggestFGK"
# public.js 里这个端点的 column/label 是写死在 URL 字符串上的（2026-11-26 从脚本
# 原文逐字抄下），不是前端参数对象。label 那十枚与上面 FILE_LABELS 同一族但顺序不同。
SUGGEST_COLUMN = "政策法规,政策解读,政策指引"


def suggest_fgk(keyword: str, typename: str = "文件") -> dict:
    """法规库标题联想：交回站点给的标题清单，只读旁路，不接进检索主链。

    这一档 2026-11-26 首次实测（此前只从 `public.js` 源码读出过端点与消费方式，
    从没发过请求；读数见 search_source/chinatax/NOTE.md 第四节）：
      * 匿名可达，无墙；`GET https://www.chinatax.gov.cn/search5/search/suggestFGK`
        （**www 主机**，不是 fgk 域），`Content-Type: application/json;charset=UTF-8`；
      * **正文是双层 JSON**——顶层 parse 出来还是一个字符串，要再 parse 一次才拿到
        `{"suggestFGKList": […]}`。public.js 里 `JSON.parse(res).suggestFGKList`
        那句正是这个意思（`$.ajax` 没声明 dataType，jQuery 把响应当字符串交回）；
      * 清单条目是**带 `<span>` 高亮的标题字符串**，不是对象：没有 url、没有文号，
        所以它只能当「这个说法在库里叫什么名字」用，不能当命中数用；
      * 五发实测（增值税 602B / 增值 576B / 留抵退税 773B / 研发费用加计扣除 1024B /
        小规模纳税人 669B）每次都是 6 条；**空检索词也回 6 条**（426B），说明空态是
        内容不是报错；
      * `typename` 只认「文件」：同词改 `typename=新闻` 回 25B 的 `{"suggestFGKList":[]}`。

    为什么不接进 `search_chinatax`：它给的是标题建议，性质是「文件类的命名基数」，
    接进主链就等于让一个说不清统计口径的端点参与「库里有没有这份文件」的结论。

    Returns:
        {"keyword","titles","count","source","_from_cache"}；空清单带 `_zero_hit`
        （这一路跑成了、就是这个频道没有联想），取数失败带 `_error` + `_fetch_failed`。
    """
    params = {
        "siteCode": SITE_CODE,
        "indexCode": "1",
        "typename": typename,
        "searchWord": keyword,
        "column": SUGGEST_COLUMN,
        "label": FILE_LABELS,
    }
    log = tax_http.new_log()
    r, err, _blocked = tax_http.attempt(
        lambda: requests.get(SUGGEST_URL, params=params, headers=HEADERS,
                             timeout=TIMEOUT, verify=False),
        SUGGEST_URL, log)
    out = {"keyword": keyword, "titles": [], "count": 0,
           "source": "chinatax.gov.cn", "_from_cache": False}
    if err:
        out.update({"_error": f"联想接口没连上：{err}", "_fetch_failed": True,
                    "_attempts": list(log)})
        return out
    if r.status_code != 200:
        out.update({"_error": f"联想接口回 HTTP {r.status_code}",
                    "_fetch_failed": True, "_attempts": list(log)})
        return out
    try:
        payload = json.loads(r.text)
    except ValueError as e:
        out.update({"_error": f"联想响应不是 JSON: {e}", "_fetch_failed": True,
                    "_attempts": list(log)})
        return out
    if isinstance(payload, str):
        # 双层 JSON：外层 parse 出来还是字符串。这一层解不开就是改版，不是空清单。
        try:
            payload = json.loads(payload)
        except ValueError as e:
            out.update({"_error": f"联想内层 JSON 解不开: {e}",
                        "_fetch_failed": True, "_attempts": list(log)})
            return out
    if not isinstance(payload, dict):
        out.update({"_error": f"联想回显顶层不是对象而是 {type(payload).__name__}",
                    "_fetch_failed": True, "_attempts": list(log)})
        return out
    raw = payload.get("suggestFGKList")
    if raw is None:
        out.update({"_error": "联想回显里没有 suggestFGKList 这个键（站点改了字段名）",
                    "_fetch_failed": True, "_attempts": list(log)})
        return out
    if not isinstance(raw, list):
        out.update({"_error": f"suggestFGKList 不是清单而是 {type(raw).__name__}",
                    "_fetch_failed": True, "_attempts": list(log)})
        return out
    titles = [_clean(x) for x in raw if isinstance(x, str)]
    titles = [x for x in titles if x]
    out["titles"] = titles
    out["count"] = len(titles)
    if not titles:
        # 跑成了、就是这个频道没有联想（实测 typename=新闻 那一档）。
        out["_zero_hit"] = True
    return out


def _attachments(raw) -> list:
    """把接口 appendix 栏的附件收成 [{name,type,url}]。

    随文的税率表、减免税清单常常只做成附件（2026-10-04 实测「消费税 成品油」
    首屏 10 条里 6 条带附件，其中一个就叫「成品油消费税纳税申报表.doc」），
    正文容器里没有这张表。不接这一栏，答案只能说"正文没有表"，指不到
    "表在附件《X》"这一层。名字或链接缺一项的丢掉——只给个文件名读者点不开，
    等于没给。
    """
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("appendixName") or "")
        url = (item.get("appendixUrl") or "").strip()
        if name and url:
            out.append({"name": name,
                        "type": (item.get("appendixType") or "").strip().lower(),
                        "url": url})
    return out


# 时效录入项里出现的"这一栏没填"写法。接口对没录时效的条目回的是字符串
# "null"（实测财税〔2003〕16 号），照原样带上下游会把 null 当时效文本读。
_AGING_BLANKS = {"", "null", "none", "nil", "-", "—", "/"}


def aging_of(raw) -> str:
    """把接口的 xxgk_aging 归一成状态文本或空串。"""
    text = str(raw or "").strip()
    return "" if text.lower() in _AGING_BLANKS else text


# xxgk_description 的截断长度。2026-11-25 六发 54 条实测（`.tmp-inspect\probe29c.py`）
# 最长的一条是「已修改」档的逐条修改史，983 字符；全文废止档最长 372。清单层要的是
# 「依据哪份文件、哪天废止」这一句，不是整部修改史，200 字符够装下依据文号加日期
# （实测最长的那句废止依据 157 字符），再长就是多条列举，指回详情页看。
STATUS_NOTE_LIMIT = 200


def _status_note_of(raw) -> str:
    """把接口的 xxgk_description 归一成时效处置说明或空串。

    这一栏不是结构化的废止日期字段：全文废止/已修改/全文失效三档装的是「根据
    《某决定》（某文号），自某年某月某日起，全文废止」这类站点原文；全文有效档
    偶尔装的是立法与施行沿革（实测增值税|全文有效首屏 10 条只有 1 条有值，就是
    「2024年12月25日…通过 自2026年1月1日起施行」）；文字政策解读与政策指引两档
    实测 10/10 全空。所以这里**只带原文、不抠日期**——把沿革句里的施行日读成
    废止日就是编造，判某日是否已废止仍要读正文或走现行/废止版配对。

    空值写法与 xxgk_aging 同族：空串与字符串 "null"（六发 54 条里没有空白串、
    没有 "-"），照原样带上下游会把 "null" 当成一句处置说明引用出去。
    """
    text = _clean(str(raw or ""))
    if not text or text.lower() in _AGING_BLANKS:
        return ""
    if len(text) <= STATUS_NOTE_LIMIT:
        return text
    return text[:STATUS_NOTE_LIMIT] + "…（原文更长，见详情页）"


def _first_match(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(text)
    return m.group(1) if m else ""


def _empty_result(keyword: str, error: str = "", filters: dict = None,
                  attempts: list = None) -> dict:
    out = {
        "keyword": keyword,
        "total": 0,
        "results": [],
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "chinatax.gov.cn",
        "_error": error,
        "_from_cache": False,
        # 只有请求真的失败才走这里，用它把"没取到数据"与"翻了没筛出/库里没有"
        # 分开——两者都叫 0 条，但界面该说的不是一句话。空态分流在
        # frontend/index.html（页码超总页 → _fetch_failed → _filter_note →
        # _empty_reason → 其余 _error），不在 tax_server；统一判据见
        # tax_http.is_fetch_failure。
        "_fetch_failed": True,
        # 每一次尝试的 (host, 异常类名或状态码, 耗时 ms, 第几次)，取自 tax_http
        "_attempts": list(attempts or []),
    }
    if filters:
        out["filters"] = filters
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────
def add_filter_args(parser):
    """给命令行挂上收窄维度与检索选项（tax_web_search 与 tax_fgk 共用一套）。

    取值全部转成 build_filters 的关键字参数；维度本身是可选的，不填就不发。
    --order 与 --all-labels 不是收窄维度，是 search_chinatax 的检索选项，
    由 search_opts_from_args 收。
    """
    g = parser.add_argument_group("收窄维度（都可选；开关见 references/commands.md，值域读数见子技能 NOTE）")
    g.add_argument("--in-title", action="store_true",
                   help="仅标题匹配（wordPlace=1，默认全文）")
    g.add_argument("--precise", action="store_true",
                   help="精准分词（participleRule=5，默认模糊）")
    g.add_argument("--tax-type", default="",
                   help="税种分面，如「增值税」（xxgkSonTaxPolicy）")
    g.add_argument("--doc-type", default="",
                   help="文种，如「财政部税务总局公告」（docType，内部空格会被去掉）")
    g.add_argument("--doc-year", default="", metavar="YYYY",
                   help="发文字号里的年份四位（docYear）；成文日期区间用 --cwrq-from/--cwrq-to")
    g.add_argument("--doc-no", default="", help="文号数字（docNo）")
    g.add_argument("--cwrq-from", default="", help="成文日期起 YYYY-MM-DD（cwrqStart）")
    g.add_argument("--cwrq-to", default="", help="成文日期止 YYYY-MM-DD（cwrqEnd）")
    g.add_argument("--aging", default="", metavar="时效",
                   help="时效分面，只认 %s（xxgkAging）" % "、".join(AGING_VALUES))
    g.add_argument("--effect-level", default="", metavar="效力等级",
                   help="效力等级分面，只认 %s（xxgkEffectLevel）；与 --aging 同发时，"
                        "%s 这三档名下不录时效，会被直接拦下"
                        % ("、".join(EFFECT_LEVEL_VALUES),
                           "、".join(AGING_UNRECORDED_LEVELS)))
    g.add_argument("--order", default="relevance", metavar="序",
                   choices=sorted(ORDER_VALUES),
                   help="排序：relevance（默认）/ date_desc（成文日期最新在前）/ "
                        "category / date_asc；date_desc 实测是 orderBy=1，"
                        "orderBy=2 排的是类别不是日期")
    g.add_argument("--all-labels", action="store_true",
                   help="关掉文件类白名单，连新闻/视频/各地动态一起搜（默认只在 "
                        "FILE_LABELS 那十类里搜）")
    return parser


def filters_from_args(args) -> dict:
    """把 add_filter_args 挂上的收窄参数收成 build_filters 的调用。"""
    return build_filters(
        in_title=args.in_title, precise=args.precise,
        tax_type=args.tax_type, doc_type=args.doc_type,
        doc_year=args.doc_year, doc_no=args.doc_no,
        cwrq_from=args.cwrq_from, cwrq_to=args.cwrq_to,
        aging=getattr(args, "aging", ""),
        effect_level=getattr(args, "effect_level", ""))


def search_opts_from_args(args) -> dict:
    """把 --order / --all-labels 收成 search_chinatax 的关键字参数。

    用 getattr 兜默认值：调用方是别的脚本时（例如只挂了部分参数），缺这两项
    就该退回默认行为，而不是 AttributeError。
    """
    return {"file_only": not getattr(args, "all_labels", False),
            "order": getattr(args, "order", "relevance")}


def main():
    p = argparse.ArgumentParser(
        description="检索国家税务总局站点（总局 + 法规库）"
    )
    p.add_argument("keyword", help="检索词")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--json", action="store_true", help="输出 JSON")
    add_filter_args(p)

    args = p.parse_args()
    try:
        filters = filters_from_args(args)
        opts = search_opts_from_args(args)
    except ValueError as e:
        p.error(str(e))
    result = search_chinatax(args.keyword, size=args.size, filters=filters, **opts)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"🔍 chinatax.gov.cn 搜索 \"{args.keyword}\" | {result['searched_at']}")
    print(f"   检索范围: {label_scope_text(opts['file_only'])} | 排序: {order_text(opts['order'])}")
    if result.get("filters"):
        print(f"   收窄维度: {result['filters']}")
    if result.get("_error"):
        print(f"⚠️  {result['_error']}")
    if result.get("_filter_note"):
        print(f"ℹ️  {result['_filter_note']}")
    # 栏目号没被站点认下时，"命中 0 条"是最容易读错的一行：成因句要当场可见，
    # 不能只躺在 --json 里（3.62.32 那一路的判据在 column_unhonoured）。
    if result.get("_empty_reason"):
        print(f"ℹ️  {result['_empty_reason']}")
    if result.get("_keyword_note"):
        print(f"ℹ️  {result['_keyword_note']}")
    print(f"命中 {result['total']} 条，取回 {len(result['results'])} 条\n")

    for item in result.get("results", []):
        print(f"  📋 {item['title']}")
        if item.get("document_number"):
            print(f"     文号: {item['document_number']}")
        if item.get("date"):
            print(f"     日期: {item['date']}")
        if item.get("publisher"):
            print(f"     来源: {item['publisher']}")
        # 时效与处置说明不打出来，读者就只能去 --json 里翻：而「这条现在还作不
        # 作数、哪天被谁废止」正是看一条清单时最该当场看到的一行。
        if item.get("status"):
            print(f"     时效: {item['status']}")
        if item.get("status_note"):
            print(f"     处置: {item['status_note']}")
        if item.get("snippet"):
            print(f"     摘要: {item['snippet'][:100]}")
        print(f"     {item['url']}")
        print()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    main()
