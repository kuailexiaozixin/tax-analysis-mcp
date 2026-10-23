#!/usr/bin/env python3
"""
税屋 (www.shui5.cn) — 实务解读来源。

为什么需要这个模块：税屋的文章（专栏、实操指引、答疑）是税务总局公告与
NPC 法规库里查不到的实操层内容，是四源之外的必要补充。

税屋访问的两道门槛及本模块的处理：
  1. 检索：两条通道，覆盖的是两种排序，不是彼此的备份。
     - 搜狗的 site:shui5.cn（`tax_sogou`）：不要凭据、不启浏览器，每行带
       标题、链接、摘要与发布日期；没有所属栏目与总条数，栏目/区间/匹配域
       也不能下推。
     - 站内原生检索（帝国 CMS 的 `POST /e/search/index.php`）：每行带日期、
       摘要与所属栏目，页面还给总条数，还能把栏目、发布区间、匹配域三项下推到
       服务端（边界见 search_native），但要先过一次 WAF（约 6 秒）。
     默认 auto：先走搜狗，它被拦或 0 条时才换站内，避免每次检索都启浏览器；
     给了栏目/区间/匹配域/取全时 auto 直接换站内，因为搜狗那一路没有这些维度。
     2026-10-23 实测同一个词「研发费用加计扣除」站内首屏 30 篇、搜狗首屏 8 篇，
     按文章 ID 归一后交集 2 篇。
     注：首页可见的搜索框提交到 zhannei.baidu.com/cse/site，那是百度站点搜索，
     本机 IP 上极不稳定（实测 4 次仅 1 次返回结果），不作为通道。
  2. 阅读：税屋前置阿里云 WAF，无放行 cookie 时所有页面（含 robots.txt、
     一个不存在的路径）都返回同一份 23,682 字节挑战页，页面里带 arg1，
     所以"请求成功"与"取到内容"在这个站是两件事。
     → 纯 HTTP 客户端按公开算法算 acw_sc__v2 也不放行（2026-09-27 实测
       5/5 被拦），因为挑战链里有浏览器行为特征，补不出来。
     → 改用真浏览器跑一次挑战（见 tax_browser），拿到放行 cookie 后转交
       requests 逐篇读正文。实测浏览器过挑战 5.9~7.0 秒，之后每篇 0.2 秒级。
  3. 边界：过 WAF 后 robots.txt 才读得到，里面 `User-agent: *` 禁 /d/ /e/class/
     /e/config/ /e/data/ /e/enews/ /e/update/；文章附件正是挂在 /d/file/p/ 下
     （2026-10-18 连读 12 篇，1 篇带两份 pdf，形如 /d/file/p/2026/08-05/<hash>.pdf），
     所以本模块不下载附件不是"没做"，是站点禁止，任何时候不要改成去取。

本模块的正文抓取顺序：
  浏览器过 WAF 拿 cookie → requests 连读（首选）
  → 纯 HTTP 直连解挑战（浏览器不可用时）
  → Jina Reader（最后兜底）

长文分页：文章页正文里有 <div class="pagebreak">「本文章更多内容」，
按本篇 ID 给出各续页地址（`<ID>$2.html`）。本模块按这些地址逐页读完再拼接，
不靠"往后一页是不是 404"试探——2026-10-18 实测只有三页的 article/42/70138.html
取 $4、$5 都返回 200 与同一份 571 字内容，页码列表反而每次都多列一项。
读到与已读正文重复的那一页即停：那说明页码已经越界，站点把请求钳回了已有内容。

除检索之外的两条取数入口（同样要过一次 WAF，行容器同为 div.xwt2，
所以行判据与检索结果页共用 `_search_rows`）：
  栏目清单 `list_column`：地址是 `/article/<目录>/<classid>_<页>.html`，`?page=` 会被
    忽略并返回第 1 页原样；页数条数读页面控件「共 X 页 Y 条记录」。47 个栏目的
    目录名、classid、页数条数与行内栏目名存在 data/shui5_columns.json，由
    `--update-columns` 逐栏读一次列表页重建——目录名猜不得：45 个拼音串里有
    5 个被站点截断（`GuangDongShengCaiShuiFaGu`）、4 个是站点自己的拼法
    （`NinBoShiFaGui` 宁波、`ShenZhengShiFaGui` 深圳），另有英文的 `Bulletin`
    和数字号的 `category_181`（山西）。
  标签清单 `list_tag`：地址是 `/e/tags/?tagname=<标签>`，翻页用 `&page=N`（实测第 2 页
    与首页行集不重合）；标签页没有页数控件，只能一页一页读到空。标签跨栏目，
    站点也没有可枚举的标签清单，标签名要从页面链接里取（`tags_of`）。

Usage:
  python tax_shui5.py "研发费用加计扣除" --size 5
  python tax_shui5.py "高新技术企业认定" --size 5 --read   # 连正文一起取
  python tax_shui5.py "增值税起征点" --size 3 --json
  python tax_shui5.py "留抵退税" --channel native --size 10   # 只走站内检索
  python tax_shui5.py "研发费用加计扣除" --match title --all   # 只匹配标题、按总数取全
  python tax_shui5.py "增值税" --column 北京,深圳   # 限定栏目（自动换站内那一路）
  python tax_shui5.py --list 实务解读 --pages 2     # 不检索，直接取栏目清单
  python tax_shui5.py --tag 增值税法 --pages 2      # 不检索，按标签取清单
  python tax_shui5.py --find-doc "财税[2019]13号"   # 按文号回站内定位，候选全部交出
  python tax_shui5.py --columns                     # 打印本地栏目表，不发请求
  python tax_shui5.py --update-columns              # 重建栏目表并写盘（首页+每栏一次，48 个请求）
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

import requests
import urllib3

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from tax_sogou import sogou_search
from tax_terms import doc_numbers

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SHUI5_SITE = "shui5.cn"
SHUI5_BASE = "https://www.shui5.cn/"
JINA_READER = "https://r.jina.ai/"
# 一次检索后逐篇取正文，搜狗与税屋都对高频请求敏感
READ_INTERVAL = 2.0
TIMEOUT_READ = 30

# 站内原生检索（帝国 CMS）。表单字段是结果页自己带出来的，2026-10-18 实测照抄可用。
# 站点认的入参只有四个：keyboard、classid（多值只用逗号形态）、starttime+endtime
# （须成对）、show（只用下面 SHOW_BY_MATCH 的两个值）。line、orderby、exact、searchid
# 提交进去被直接忽略：同词同限定下换这三枚任一枚，落点页的字节数、md5 与 searchid
# 都与默认档一字不差——服务端没换查询，不是只换了排版。show 里多写一个站点没有的域
# （subtitle）同样按默认三域返回。tempid 或 tbname 换成别的值则不接受，落点是提示页
# 而不是报错。默认排序已经是发布日期倒序，没有可调排序的余地。
SEARCH_FORM = SHUI5_BASE + "e/search/index.php"
SEARCH_RESULT = SHUI5_BASE + "e/search/result/index.php"
SEARCH_FIELDS = {"tbname": "article", "show": "title,keyboard,smalltext",
                 "tempid": "2", "Submit": "站内搜税"}
# `show` 决定哪些字段参与匹配，不是排版参数：同一个词「研发费用加计扣除」
# 三域 433 条、只标题 150 条（2026-10-18 实测）。
SHOW_BY_MATCH = {"broad": "title,keyboard,smalltext", "title": "title"}
# 站点在提示页里明写的检索间隔，留 1 秒余量
SEARCH_MIN_GAP = 3.0
SEARCH_ROWS_PER_PAGE = 30
SEARCH_MAX_PAGES = 6
# --all 时的页数上限。433 条那一词 15 页取完（page0-13 各 30 条全新、page14 13 条，
# 累计唯一数与页面总数相等）；上限防的是「增值税」那种 27,249 条的宽词，
# 没有它取全会翻上千页。到了上限会在 _status 里写明只取了前若干条。
SEARCH_ALL_MAX_PAGES = 20
# 栏目列表页：分页形态是 <classid>_<页>.html，控件文案「共 178 页 6198 条记录」
COLUMN_PAGES = SCRIPT_DIR.parent / "data" / "shui5_columns.json"
TAG_RESULT = SHUI5_BASE + "e/tags/"
# 续页上限：正文页码列表按篇给出，正常长文不到这个数；上限只防站点页码异常时把连读拖长
MAX_CONTINUATION_PAGES = 12
# 提示页的两份文案。RATE_LIMIT_MARK 一件事对应一份页；NO_RESULT_MARK 那一份
# 有两种成因（真零命中 / 参数不被接受），落点都是 /e/search/index.php，不 302。
NO_RESULT_MARK = "没有搜索到相关的内容"
RATE_LIMIT_MARK = "系统限制的搜索时间间隔"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 税屋前置阿里云 WAF：第一次请求一定返回 23,682 字节的挑战页，页面里
# <textarea id="renderData"> 带 arg1（40 位十六进制），浏览器据此算出
# acw_sc__v2 再请求才放行。
#
# 算法是公开的：固定置换表重排 + 固定掩码逐字节异或，置换表与掩码是该
# 挑战的常量，不随 arg1 变。但算对 cookie 服务端仍不认（实测 5/5 被拦），
# 因为服务端还校验挑战链里的浏览器行为痕迹。留这条路是为了浏览器不可用
# 时还有个不白跑的尝试，同时它也是判断挑战页 arg1 是否变化的取样点。
_UNBOX_POS = [15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23,
              25, 13, 6, 11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4,
              17, 5, 3, 28, 34, 37, 12, 36]
_XOR_MASK = "3000176000856006061501533003690027800375"
_ARG1_RE = re.compile(r"arg1\s*=\s*'([0-9a-fA-F]+)'")

# 正文容器的收口。2026-10-18 抽样 12 篇实测：正文之后第一个兄弟容器是
# div.left2e（点赞/微信/微博/扫码分享块），旧边界里没有它，所以每一篇的正文
# 尾部都混入「点赞（）微信 微博 × 用微信扫码二维码 分享至好友和朋友圈」；
# left2b 与 blank20 排在它之后，只作站点改版时的第二、三道保险。
_ARC_RE = re.compile(r'<div class="arcContent"[^>]*>(.*?)'
                     r'(?=<div class="(?:left2e|bot-share|left2b|blank20)|\Z)', re.S | re.I)
_META_RES_RE = re.compile(r'<div class="articleResource">(.*?)</div>', re.S | re.I)
_META_DES_RE = re.compile(r'<div class="articleDes">(.*?)</div>', re.S | re.I)
_TIME_RE = re.compile(r"时间：\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
_TAG_RE = re.compile(r"<[^>]+>")
_PARA_RE = re.compile(r"</(?:p|div|li|tr|h[1-6])\s*>|<br\s*/?>", re.I)

# 结果页/栏目页/标签页行标题尾部的时效标记，值域按 2026-10-18 检索标记词的标题读数
# 定为五种：条款失效、全文失效、条款废止、全文废止、条款修订。站点写成半角方括号
# （取样到的带标记标题一律如此，`财税[2019]13号 …[全文失效]`），全角也一并收下，
# 站点改用全角时不至于静默漏读；全角形态本轮没在页面上取到。
_STATUS_MARK_RE = re.compile(r"[\[〔]\s*(条款失效|全文失效|条款废止|全文废止|条款修订)\s*[\]〕]")

# 文号里「编号」那一段：〔2019〕13号、[2019]13号、2019年第13号 三种写法都在这里。
# 用在 --find-doc 的输入整形上——只把这一段内部与左侧的空白去掉，机关名之间的
# 空格留着（站点按整串匹配，多一个空格就是另一条查询）。四类括号都要进字符集，
# 漏了六角括号〔〕，「财税〔 2019 〕 13 号」这种带空格的写法就只能整串提交，
# 命中的是站内根本不存在的形态。这一段要求四位年号在前、编号加「号」在后：
# 规章令号「令第108号」没有四位年号，落不进来；「2024年4月15日」在「年」后面
# 接不上一枚「号」，同样落不进来，两处都不会被误动。
_DOC_SPAN_RE = re.compile(r"\s*[\[(［【〔]?\s*\d{4}\s*[年\])］】〕]?\s*第?"
                          r"\s*[0-9一二三四五六七八九十百]+\s*号")

# 正文里站点自加的效力批注「税屋提示——」。出现与否随文件走：标题带时效标记的
# 13 篇取样全有批注，不带标记的 40 篇一篇也没有（逐轮读数与取样口径记在
# subskills/shui5/NOTE.md「栏目与正文形态」）。
# 批注后面紧跟的就是文件正文，正文里也有文号，所以按整段读会多收——按「。」切句，
# 只留与时效有关的那几句。个别批注把修改后的条文整段引了进来，句号落在引文内部，
# 这一类由窗口上限收口；完整文字本来就在 content 里，这一段只作定位。
_TIP_ANCHOR = "税屋提示"
_TIP_MAX = 300
_TIP_HEAD_RE = re.compile(r"^税屋提示[—\-–\s]*")
_TIP_KEEP_RE = re.compile(r"^(?:\d+\.\s*)?(?:依据|本法规|本通知|自\s*\d{4}年)")

# 续页：只用本篇 ID 出现在 pagebreak 里的地址，不按"页面上有没有 $N 形态的链接"数——
# 相关阅读区混着别人文章的续页链接，按形态数会把单页文章也判成分页文章。
_ARTICLE_ID_RE = re.compile(r"/article/[^/]+/(\d+)(?:\$\d+)?\.html")
_PAGE_LINK_RE = re.compile(r'href="([^"]*?/(\d+)\$(\d+)\.html)"', re.I)

# 站内检索结果页的字段落点（2026-10-18 实测行容器 div.xwt2 内五处）
_ROW_SPLIT_RE = re.compile(r'<div class="xwt2"[^>]*>', re.I)
_TOTAL_RE = re.compile(r"(\d+)\s*条关于")
_ROW_A_RE = re.compile(r'<div class="xwt2_a">\s*<a\s+href="([^"]+)"[^>]*?title="([^"]*)"',
                       re.I | re.S)
_ROW_C_RE = re.compile(r'<div class="xwt2_c">\s*<p class="p1">(.*?)</p>', re.I | re.S)
_ROW_P3_RE = re.compile(r'<p class="p3">([^<]*)</p>', re.I)
_ROW_P4_RE = re.compile(r'<p class="p4">.*?<a[^>]*>(.*?)</a>', re.I | re.S)
_SEARCHID_RE = re.compile(r"searchid=(\d+)")

# 栏目列表页的翻页控件同时给两件事：这一栏的 classid（链接形态 <classid>_<页>.html）
# 与站点自己报的量（「共 178 页 6198 条记录」）。取 classid 只认控件里的链接，
# 不从目录名猜——目录名与归属并不同源（见 build_column_table 的注释）。
_COL_PAGE_LINK_RE = re.compile(r'href="[^"]*?/(\d+)_\d+\.html"', re.I)
_COL_CTRL_RE = re.compile(r"共\s*(\d+)\s*页\s*(\d+)\s*条记录")
# 标签链接一律是 /e/tags/?tagname=<已编码值>，首页与文章页都有
_TAG_HREF_RE = re.compile(r'href="[^"]*?[?&]tagname=([^"&]+)"', re.I)
# 首页主栏在 li.mainlevel 那一块；36 个地方栏与 7 个 /s/ 专栏不在里面，
# 是首页其他位置的链接——只按 mainlevel 解析会读成"地方栏 0 个"。
_MAINLEVEL_RE = re.compile(r'<li class="mainlevel"><a\s+href="([^"]+)"[^>]*>(.*?)</a></li>', re.S)


def _to_text(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", fragment)
    fragment = _PARA_RE.sub("\n", fragment)
    fragment = _TAG_RE.sub("", fragment)
    import html as htmllib
    fragment = htmllib.unescape(fragment)
    lines = [ln.replace("　", " ").strip() for ln in fragment.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _solve_waf_challenge(arg1: str) -> str:
    """按阿里云 WAF 的公开算法由 arg1 算出 acw_sc__v2。

    先按 _UNBOX_POS 重排 40 位十六进制串，再与 _XOR_MASK 逐字节异或。
    """
    reordered = "".join(arg1[i - 1] for i in _UNBOX_POS if i - 1 < len(arg1))
    return "".join(
        "%02x" % (int(reordered[i:i + 2], 16) ^ int(_XOR_MASK[i:i + 2], 16))
        for i in range(0, len(reordered) - 1, 2)
    )


def _is_waf_page(page: str) -> bool:
    return "arg1" in page and "renderData" in page


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def _repeal_note(content: str) -> str:
    """从正文里抠出「税屋提示——」批注中与时效有关的分句，没有批注返回空串。

    分句判据：以「依据」「本法规」「本通知」「自YYYY年」开头（可以带 `1.` 这样的序号），
    或句中出现「本法规/本通知」。不满足的是批注后面那篇文件自己的正文。
    实测过的四种句形都能收下：单句 `依据××，自2026年3月5日起本法规全文失效。`、
    不带止期的 `依据××，本通知部分条款修订。`、带序号的多句 `1.依据××。 2.依据××。`、
    以及 `依据××令第108号 …自2025年12月25日起，本法规将第十一条…修改为…`。
    """
    i = content.find(_TIP_ANCHOR)
    if i < 0:
        return ""
    kept = []
    for piece in content[i:i + _TIP_MAX].split("。"):
        seg = re.sub(r"\s+", " ", _TIP_HEAD_RE.sub("", piece.strip()).strip())
        if not seg:
            continue
        if _TIP_KEEP_RE.match(seg) or "本法规" in seg or "本通知" in seg:
            kept.append(seg)
    return "。".join(kept)


def _to_full_brackets(text: str) -> str:
    """把半角方括号里包住四位年号的那一段换成六角括号。

    税屋标题与正文里的文号写成 `财税[2019]13号`，而 `tax_terms.doc_numbers` 认的是
    官方那种 `〔〕`，直接喂过去一枚也抽不到（2026-10-18 取样 11 篇带「税屋提示」的
    文章：按原文抽到 1 枚，换括号后抽到 4 枚；抽不到的那 7 篇里 6 篇批注写的是
    「××令第108号」这类规章令号、1 篇写的是不带编号的公告名，都不在文号形态里）。
    只换包着四位数字的那对括号，别处的方括号不动。
    """
    return re.sub(r"[\[［【]\s*(\d{4})\s*[\]］】]", r"〔\1〕", text or "")


def _parse_article(page: str, url: str) -> dict:
    """从已放行的 HTML 里切出标题、日期、摘要、正文。"""
    out = {"url": url}
    if _is_waf_page(page):
        out["_error"] = ("WAF 挑战未通过：浏览器已过挑战但仍被拦，"
                         "多半是访问过于频繁，稍后重试")
        return out
    m = _ARC_RE.search(page)
    if not m:
        out["_error"] = "未匹配到 arcContent 正文容器"
        return out
    h1 = re.search(r"<h1>(.*?)</h1>", page, re.S)
    out["title"] = _to_text(h1.group(1)) if h1 else ""
    res = _META_RES_RE.search(page)
    if res:
        t = _TIME_RE.search(res.group(1))
        out["date"] = t.group(1) if t else ""
    des = _META_DES_RE.search(page)
    if des:
        out["summary"] = _to_text(des.group(1))
    out["content"] = _to_text(m.group(1))
    if not out["content"]:
        out["_error"] = "正文容器为空"
        return out
    note = _repeal_note(out["content"])
    if note:
        out["repeal_note"] = note
        basis = doc_numbers(_to_full_brackets(note))
        if basis:
            out["repeal_basis"] = basis
    return out


def _browser_session(cookies: dict) -> requests.Session:
    """用浏览器导出的放行 cookie 装一个 requests 会话。"""
    s = _make_session()
    s.headers["Referer"] = SHUI5_BASE
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=SHUI5_SITE, path="/")
    return s


def _pass_waf(warm_url: str = "") -> tuple[requests.Session | None, str]:
    """过一次 WAF，交回可直接连读的会话与这次是怎么过的。

    Returns:
        (会话, 说明)。浏览器不可用时会话为 None，说明里带失败原因；
        调用方据此退回纯 HTTP 那条路（多数会被拦）。
    """
    try:
        from tax_browser import BrowserSession
        b = BrowserSession(warm_url=warm_url or SHUI5_BASE)
        b.warm()
        s = _browser_session(b.cookies_for(SHUI5_SITE))
        how = f"浏览器 {b.channel} 过 WAF 后用 HTTP 连读"
        b.close()
        return s, how
    except Exception as e:                              # 没装浏览器/启动失败
        return None, f"无浏览器，改纯 HTTP 直连（{str(e).splitlines()[0][:70]}）"


def _read_page(session: requests.Session, url: str) -> tuple[str, str]:
    """取一页 HTML，返回 (正文, 错误)。错误非空时正文不可用。

    没有凭据时任何地址都回同一份挑战页且 HTTP 200，所以判"取到内容"要看页面
    是不是挑战页，不能只看状态码。
    """
    r = session.get(url, timeout=TIMEOUT_READ)
    page = r.content.decode("utf-8", errors="replace")
    if r.status_code != 200:
        return "", f"HTTP {r.status_code}"
    if _is_waf_page(page):
        m = _ARG1_RE.search(page)
        if m:
            session.cookies.set("acw_sc__v2", _solve_waf_challenge(m.group(1)))
            r = session.get(url, timeout=TIMEOUT_READ)
            page = r.content.decode("utf-8", errors="replace")
    if _is_waf_page(page):
        return "", ("WAF 挑战未通过：acw_sc__v2 已按公开算法算出，"
                    "但服务端仍要求浏览器执行完整 JS 挑战链")
    return page, ""


def _article_id(url: str) -> str:
    m = _ARTICLE_ID_RE.search(url)
    return m.group(1) if m else ""


def _page_url(url: str, n: int) -> str:
    """本篇第 n 页的地址：<ID>.html / <ID>$2.html。"""
    stem = re.sub(r"(\$\d+)?\.html$", "", url)
    return url if n <= 1 else f"{stem}${n}.html"


def _own_pages(html: str, article_id: str) -> set:
    """这一页里指向本篇续页的页码。

    全页扫描而不切 pagebreak 容器：那个 div 里还可能套别的标记，切早了会把页码
    链接切掉，宁可读一页多余的。归属靠本篇 ID 判——相关阅读区混着别人文章的 `$N`
    链接，按链接形态数会把单页文章判成分页文章。
    """
    if not article_id:
        return set()
    return {int(n) for _, aid, n in _PAGE_LINK_RE.findall(html) if aid == article_id}


def fetch_shui5(url: str, session: requests.Session | None = None,
                page_interval: float = READ_INTERVAL,
                max_pages: int = MAX_CONTINUATION_PAGES) -> dict:
    """
    取一篇文章正文，长文按本篇 pagebreak 给的续页读到末页。

    Args:
        url: 税屋文章地址
        session: 已过 WAF 的会话。传了就直接用（多篇连读时省掉重复握手），
                 传空则自建一个并自行解挑战。
        page_interval: 续页之间的间隔秒数，站点对高频请求敏感
        max_pages: 单篇最多读几页

    Returns:
        {"url","title"?,"date"?,"summary"?,"content"?,"pages",
         "_pages_error"?,"_error"?}
        pages 是实际读到的页数；某一页取不到时正文停在已读部分，
        原因写在 _pages_error，不静默当成一整篇没有。
    """
    own = session is None
    if own:
        session = _make_session()
    try:
        page, err = _read_page(session, url)
        if err:
            return {"url": url, "_error": err}
        art = _parse_article(page, url)
        if art.get("_error"):
            return art

        aid = _article_id(url)
        read = {1}
        todo = sorted(_own_pages(page, aid))
        extra = []
        texts = {art["content"]}
        while todo and len(read) < max_pages:
            n = todo.pop(0)
            time.sleep(page_interval)
            nxt_url = _page_url(url, n)
            page2, err2 = _read_page(session, nxt_url)
            if err2:
                art["_pages_error"] = f"第 {n} 页取不到（{err2}），正文止于第 {max(read)} 页"
                break
            a2 = _parse_article(page2, nxt_url)
            if a2.get("_error"):
                art["_pages_error"] = f"第 {n} 页取不到（{a2['_error']}），正文止于第 {max(read)} 页"
                break
            if a2["content"] in texts:
                # 页码越界时站点不报 404，而是钳回一份已有正文（实测 $4、$5 同为 571 字
                # 且 sha8 一致）。文本与已读页重合即到底，这一页不计页数、不拼正文。
                break
            read.add(n)
            texts.add(a2["content"])
            extra.append(a2["content"])
            # 页码列表可能是窗口式的（只列相邻几页），读一页就把新出现的页码并进来
            todo = sorted(set(todo) | (_own_pages(page2, aid) - read - set(todo)))
        if extra:
            art["content"] = "\n".join([art["content"], *extra])
        art["pages"] = len(read)
        return art
    except requests.RequestException as e:
        return {"url": url, "_error": f"请求失败：{e}"}
    finally:
        if own:
            session.close()


def _row_dict(title: str, url: str, date: str, snippet: str, column: str = "") -> dict:
    """两条检索通道共用的行形状，保证下游（去重、排序、界面）不分辨来源。

    标题里的时效标记另起一个 `status_mark` 键，但没有标记的行不加这个键，
    标记文本仍原样留在 title 里——剥掉它会让标题与页面不一致。
    """
    row = {
        "title": title,
        "url": url,
        "date": date,
        "snippet": snippet[:200],
        "source": "税屋 (shui5.cn)",
        "source_label": "实务解读",
    }
    if column:
        row["column"] = column
    mark = _STATUS_MARK_RE.search(title)
    if mark:
        row["status_mark"] = mark.group(1)
    return row


def _search_rows(page: str) -> list:
    """解站内结果页的 div.xwt2 行：标题、链接、日期、摘要、所属栏目。

    标题取 a 的 title 属性（可见文本被检索词高亮标签切开），高亮标签一律剥掉。
    """
    rows = []
    for chunk in _ROW_SPLIT_RE.split(page)[1:]:
        a = _ROW_A_RE.search(chunk)
        if not a:
            continue
        url = a.group(1)
        if SHUI5_SITE not in url:
            continue
        c = _ROW_C_RE.search(chunk)
        p3 = _ROW_P3_RE.search(chunk)
        p4 = _ROW_P4_RE.search(chunk)
        rows.append(_row_dict(
            title=_to_text(a.group(2)),
            url=url,
            date=(p3.group(1).strip() if p3 else ""),
            snippet=_to_text(c.group(1)) if c else "",
            column=_to_text(p4.group(1)) if p4 else "",
        ))
    return rows


def search_native(keyword: str, size: int = 5, session: requests.Session | None = None,
                  gap: float = SEARCH_MIN_GAP, classid: str | int | None = None,
                  date_from: str = "", date_to: str = "", match: str = "broad",
                  fetch_all: bool = False) -> dict:
    """站内原生检索：POST 检索表单 → 302 落点 → 按 page 从 0 起翻页。

    这一路要一份放行 cookie（约 6 秒过一次 WAF），换来的是搜狗那一路给不出的
    三件事：每行带所属栏目、页面给这一词的总条数（形如「420条关于 留抵退税 的
    搜索结果」）、栏目/发布日期区间/匹配域三项能下推到服务端。日期搜狗也给，
    不是这一路独有的。

    限定一律下推到服务端。三个过滤维度（栏目、发布日期区间、匹配域）的实测边界
    （2026-10-18，逐项读数记在 `subskills/shui5/NOTE.md`）：
      classid    栏目限定，取值见 data/shui5_columns.json；多个用逗号（`152,12673`
                 与 `北京,深圳` 等价），竖线与 classid[] 形态站点不认；
                 父栏覆盖子栏（同一个词限到 145 地方法规 170 条，首屏 30 行里
                 4 行的行内栏目名是北京市法规）。
      endtime    单独提交就过滤。
      starttime  **单独提交不过滤**（27,249 条与不限时间完全相同），必须与 endtime 成对；
                 只给 date_from 时这个缺口写进 _filter_note，不静默当成没限定。
      show       匹配域，broad=标题+关键词+摘要，title=只标题（433 条 / 150 条）。

    这一路只认四个入参：keyboard、classid、starttime+endtime、show。line、orderby、
    exact、searchid 提交进去站点直接忽略（换任一枚，落点页与默认档 md5 相同、
    searchid 也复用同一枚），默认排序已经是发布日期倒序，所以没有可调的排序参数。
    同一查询（词与限定完全相同）跨浏览器会话复用同一个 searchid，
    落点与翻页地址因此可复现。

    提示页两份，按文案判，不按页面大小判：
      「没有搜索到相关的内容」有两种成因，落在同一份页面上分不开（2026-10-18 实测：
      tempid=3、tbname=news、以及两个真的零命中词，读到的页面都是 1,222 字节、
      md5 相同）：这一轮站内检索没命中，或者提交的参数站点不接受。
      本函数只发白名单里的形态，收到它按 0 条记账；站点改版让白名单失效时，
      这一档会整片报成 0 条，所以 `_status` 里把"两种成因分不开"这句一起交出去，
      上层归因时不用翻文档也不会把参数问题读成"这一站没有内容"。
      「系统限制的搜索时间间隔为 2 秒」= 请求太密，等过间隔重发一次（1,253 字节）。
    两份提示页都停在 /e/search/index.php，不做 302，所以没有 searchid 可翻。

    Args:
        keyword: 检索词。空格分隔的多个词不是 AND，站点按整串匹配：「法理阐释」1 条、
                 「法理阐释 增值税留抵退税」0 条，「留抵退税」420 条、「留抵 退税」0 条。
                 一个概念一个词，拆成空格串会把有内容的词读成 0 条。
        size: 返回条数上限，超过一页时按 page 继续取
        session: 已过 WAF 的会话；传空则自己过一次
        gap: 翻页与重试之间的间隔秒数，不低于站点自己声明的 2 秒
        classid: 栏目限定，传 classid、栏目名或目录名（查 data/shui5_columns.json 换算）；
                 多个用逗号，每段各自换算，任一段查不到就整体报缺
        date_from: 发布日期起（YYYY-MM-DD），须与 date_to 成对才起作用
        date_to: 发布日期止（YYYY-MM-DD）
        match: "broad"（默认，标题+关键词+摘要）或 "title"（只标题）
        fetch_all: True 时按这一词的总条数翻到底，上限 SEARCH_ALL_MAX_PAGES 页

    Returns:
        {"results","total","_status","_filter_note"?,"_pages"?}；total 是站点报的
        这一词总条数，取不到为 None。_status 说明这一路本轮走到哪一步，供上层归因，
        不当成命中与否的判据。行的日期是税屋自己的发布日期，不是文件的成文或废止日期。
    """
    own = session is None
    if own:
        session, how = _pass_waf()
        if session is None:
            return {"results": [], "total": None, "_status": f"没走成：{how}"}
    note = ""
    if date_from and not date_to:
        note = ("date_from 单独提交站点不过滤（starttime 单独无效是实测结论），"
                "要限起始日期须同时给 date_to")
    if classid and not str(classid).isdigit():
        cid = classid_spec(classid)
        if not cid:
            return {"results": [], "total": None,
                    "_status": f"没走成：栏目表 data/shui5_columns.json 里没有「{classid}」，"
                               f"或其中某一段查不到"}
        classid = cid
    try:
        data = dict(SEARCH_FIELDS, keyboard=keyword, show=SHOW_BY_MATCH[match])
        if classid:
            data["classid"] = str(classid)
        if date_from:
            data["starttime"] = date_from
        if date_to:
            data["endtime"] = date_to
        r = session.post(SEARCH_FORM, data=data, timeout=TIMEOUT_READ,
                         verify=False, allow_redirects=True)
        page = r.content.decode("utf-8", errors="replace")
        if _is_waf_page(page):
            return {"results": [], "total": None,
                    "_status": "没走成：WAF 挑战未通过（会话凭据失效）"}
        if RATE_LIMIT_MARK in page:
            time.sleep(gap)
            r = session.post(SEARCH_FORM, data=data, timeout=TIMEOUT_READ,
                             verify=False, allow_redirects=True)
            page = r.content.decode("utf-8", errors="replace")
            if RATE_LIMIT_MARK in page:
                return {"results": [], "total": None,
                        "_status": "没走成：站点提示两次检索须间隔 2 秒以上，重发一次仍被限"}
        if NO_RESULT_MARK in page:
            out = {"results": [], "total": 0,
                   "_status": "0 条：站点回「没有搜索到相关的内容」，这一页两种成因分不开"
                              "（站内确实没有 / 这一档参数站点不接受）"}
            if note:
                out["_filter_note"] = note
            return out

        m = _SEARCHID_RE.search(r.url)
        if not m:
            return {"results": [], "total": None,
                    "_status": "没走成：检索落点里没有 searchid"}
        sid = m.group(1)
        total = None
        t = _TOTAL_RE.search(page)
        if t:
            total = int(t.group(1))
        rows = _search_rows(page)
        seen = {x["url"] for x in rows}
        cap = SEARCH_MAX_PAGES
        want = size
        if fetch_all and total:
            cap = min(-(-total // SEARCH_ROWS_PER_PAGE), SEARCH_ALL_MAX_PAGES)
            # 取全不受 size 约束：size 只管"翻页翻到几条就停"，取全要按站点总数走，
            # 否则 --all 配上默认 size=5 会在第一页就停，看着像取全了其实只有 5 条
            want = min(total, cap * SEARCH_ROWS_PER_PAGE)
        # 落点页就是 page=0（实测两页首条与行数一致），继续从 1 翻到够数或到上限
        pn = 1
        read = 1
        while len(rows) < want and pn <= cap:
            time.sleep(gap)
            rp = session.get(f"{SEARCH_RESULT}?page={pn}&searchid={sid}",
                             timeout=TIMEOUT_READ, verify=False)
            nxt = _search_rows(rp.content.decode("utf-8", "replace"))
            if not nxt:
                break
            read += 1
            for x in nxt:
                if x["url"] not in seen:
                    seen.add(x["url"])
                    rows.append(x)
            pn += 1
        got = rows if fetch_all else rows[:size]
        status = f"命中 {len(got)} 条"
        if total is not None:
            status += f"（这一词站内共 {total} 条"
            if fetch_all and len(rows) < total:
                status += f"，翻到上限 {cap} 页只取了 {len(rows)} 条"
            status += "）"
        out = {"results": got, "total": total, "_status": status, "_pages": read}
        if note:
            out["_filter_note"] = note
        return out
    except requests.RequestException as e:
        return {"results": [], "total": None, "_status": f"没走成：请求失败：{e}"}
    finally:
        if own:
            session.close()


def search_shui5(keyword: str, size: int = 5, read_body: bool = False,
                 channel: str = "auto", column: str | int | None = None,
                 date_from: str = "", date_to: str = "", match: str = "broad",
                 fetch_all: bool = False) -> dict:
    """
    在税屋检索政策实务文章。

    Args:
        keyword: 检索词
        size: 返回条数上限
        read_body: True 时再逐篇取正文（较慢，每篇之间等 READ_INTERVAL）
        channel: "auto"（默认）先走搜狗，它被拦或 0 条时才换站内原生检索；
                 "sogou" 只走搜狗（不要凭据、不启浏览器）；
                 "native" 只走站内（每行带日期与栏目，但要过一次 WAF）。
                 带限定参数时 auto 直接换成 native，_channels 里会写明这一轮没问过搜狗。
                 两条通道覆盖的是不同排序：2026-10-23 实测同一个词站内首屏 30 篇、
                 搜狗首屏 8 篇，按文章 ID 归一后交集 2 篇，所以"这一路 0 条"不能
                 读成"另一路也是 0 条"。
        column: 栏目限定（classid、目录名、导航名或首页短标签，多个用逗号）。
                给了它就只走站内那一路——搜狗的 site: 检索没有栏目、时间与匹配域这些维度。
        date_from / date_to: 发布日期区间，走站内那一路；只有 date_from 时起不到限定作用，
                缺口写在结果的 _filter_note 里。
        match: "broad"（默认）或 "title"（只在标题里匹配），走站内那一路。
        fetch_all: 按站点报的总条数翻到底（上限 SEARCH_ALL_MAX_PAGES 页），走站内那一路。

    Returns:
        {"keyword","total","results","searched_at","source","_channel",
         "_channels","_filter_note"?,"_error"?}
        results 每项含 title/url/date/snippet（站内那一路另有 column）；
        标题尾部带站点时效标记的那一行另有 status_mark，取值见 _STATUS_MARK_RE。
        read_body=True 时额外含 content 与 pages；正文开头带「税屋提示——」批注的
        那一篇另有 repeal_note。
        _channels 逐条写明这一轮每条通道走到哪一步——上层据此归因，
        不必再猜"税屋 0 条"是税屋空的还是搜狗连带的。
    """
    scoped = bool(column or date_from or date_to or match != "broad" or fetch_all)
    results: list = []
    used = ""
    note = ""
    searched_at = time.strftime("%Y-%m-%d %H:%M:%S")
    from_cache = False
    channels = {"搜狗": "未走（指定通道）", "站内": "未走（指定通道）"}
    if scoped and channel == "auto":
        # 换路是限定参数逼的，不是搜狗连带的：这句要写清楚，否则读者以为
        # "搜狗也没命中"，其实这一轮压根没问过它。
        channel = "native"
        channels["搜狗"] = ("没问：这一轮要栏目/时间/匹配域/取全，"
                            "这些维度只有站内那一路有")
    elif scoped and channel == "sogou":
        # 指定了搜狗又给了限定参数：搜狗那一路照跑，但限定加不上，只能报缺口
        note = ("这一轮给了栏目/时间/匹配域/取全这类限定，但通道指定成 sogou："
                "这些维度只有站内那一路有，限定没加上")

    if channel in ("auto", "sogou"):
        found = sogou_search(keyword, site=SHUI5_SITE, size=size)
        if found.get("_error"):
            status = "被拦" if found.get("_blocked") else "没走成"
            channels["搜狗"] = f"{status}：{found['_error']}"
        else:
            for item in found["results"]:
                results.append(_row_dict(
                    title=_strip_site_suffix(item["title"]) or item["title"],
                    url=item["url"], date=item.get("date", ""),
                    snippet=item.get("snippet", "")))
            channels["搜狗"] = f"命中 {len(results)} 条" if results else "0 条"
        used = "搜狗" if results else ""
        if results:
            searched_at = found.get("searched_at", searched_at)
            from_cache = bool(found.get("_from_cache"))

    nat = None
    if channel == "auto" and not results:
        nat = search_native(keyword, size=size)
    elif channel == "native":
        nat = search_native(keyword, size=size, classid=column, date_from=date_from,
                            date_to=date_to, match=match, fetch_all=fetch_all)
    if nat is not None:
        channels["站内"] = nat["_status"]
        results = nat["results"]
        used = "站内" if results else ""
        note = nat.get("_filter_note", "")

    err = ""
    if not results:
        failed = {k: v for k, v in channels.items() if v.startswith("没走成") or v.startswith("被拦")}
        if failed:
            err = "两条检索通道都没取到：" + "；".join(f"{k} {v}" for k, v in failed.items())

    how = ""
    if read_body and results:
        # 一次过 WAF、连读全部，不逐篇重启浏览器
        arts = read_articles([r["url"] for r in results])
        for row, art in zip(results, arts):
            row["content"] = art.get("content", "")
            row["pages"] = art.get("pages", 1)
            if art.get("title"):
                row["title"] = _strip_site_suffix(art["title"])
            row["date"] = art.get("date", "") or row["date"]
            if art.get("summary"):
                row["summary"] = art["summary"]
            if art.get("repeal_note"):
                row["repeal_note"] = art["repeal_note"]
            if art.get("repeal_basis"):
                row["repeal_basis"] = art["repeal_basis"]
            if art.get("_error"):
                row["_error"] = art["_error"]
            if art.get("_pages_error"):
                row["_pages_error"] = art["_pages_error"]
        how = arts[-1].get("_how", "") if arts else ""

    out = {
        "keyword": keyword,
        "total": len(results),
        "results": results,
        "searched_at": searched_at,
        "source": "税屋 (shui5.cn)",
        "_channel": used,
        "_channels": channels,
        "_from_cache": from_cache,
    }
    if err:
        out["_error"] = err
    if note:
        out["_filter_note"] = note
    if how:
        out["_how"] = how
    return out


def find_by_doc_number(doc_number: str, session: requests.Session | None = None,
                       size: int = 20) -> dict:
    """按文号回站内定位税屋那几篇：候选全部交出，不替读者挑一条。

    括号形态是这个功能的主要坑。2026-10-18 实测同一枚文号两种写法各查一次：
    `财税[2019]13号`（半角方括号）7 条、`财税〔2019〕13号`（全角六角括号）0 条；
    `财税[2016]36号` 20 条、`财税〔2016〕36号` 4 条——税屋标题里的文号一律写半角，
    官方文本那种六角括号只在少数文章里出现。所以查询串先换半角，0 条再退回全角试一次。
    两种都空才去掉机关名只留「[年]序号」那一截，这一档连机关都不匹配了，
    命中里必然混着同号不同机关的文件，读数里写明。

    即便用完整文号查，站点做的是子串匹配：`财税[2019]13号` 那 7 条里只有 1 条
    是财政部与税务总局发的那份，另外 5 条是沪财税、湘财税、闽财税、桂财税、藏财税
    各自同号的地方文件，还有 1 篇是解读文章。标题、发文机关与栏目要逐条看，
    不能拿第一条当答案。

    只在标题里匹配（match="title"）：正文里援引了这枚文号的文章不算这篇的候选。

    Args:
        doc_number: 文号，全角半角都接受；编号那一段内部与左侧的空白会被去掉，
                    机关名之间的空格按原样提交，不做拆分
        session: 已过 WAF 的会话；传空则自己过一次（约 6 秒）
        size: 最多交出几条候选

    Returns:
        {"doc_number","_query","_queries_tried","results","total","_status","_caveat"?}；
        _query 是这一轮真正提交的查询串，走不通时 results 为空并在 _status 里写明停在哪一步。
    """
    # 编号内部与两侧的空白去掉，机关名之间的空格原样保留：站点按整串匹配，
    # 拆掉「财政部 税务总局公告2019年第13号」中间那个空格就换成另一条查询串了。
    raw = _DOC_SPAN_RE.sub(lambda m: re.sub(r"\s+", "", m.group(0)),
                           (doc_number or "").strip())
    if not raw:
        return {"doc_number": doc_number, "_query": "", "_queries_tried": [],
                "results": [], "total": None, "_status": "没走成：没给文号"}
    ascii_form = raw
    for a, b in (("〔", "["), ("〕", "]"), ("［", "["), ("］", "]"),
                 ("【", "["), ("】", "]")):
        ascii_form = ascii_form.replace(a, b)
    full_form = ascii_form.replace("[", "〔").replace("]", "〕")
    queries = [ascii_form]
    if full_form != ascii_form:
        queries.append(full_form)
    # 第三档：去掉机关名，只留「[2019]13号」或「2019年第39号」
    core = re.search(r"\d{4}年第[0-9一二三四五六七八九十百]+号|\[\d{4}\]第?[0-9一二三四五六七八九十百]+号",
                     re.sub(r"\s+", "", ascii_form))
    if core and core.group(0) not in queries:
        queries.append(core.group(0))

    own = session is None
    if own:
        session, how = _pass_waf()
        if session is None:
            return {"doc_number": doc_number, "_query": queries[0], "_queries_tried": [],
                    "results": [], "total": None, "_status": f"没走成：{how}"}
    last = {"results": [], "total": None, "_status": "没走成", "_queries_tried": []}
    try:
        for q in queries:
            if last["_queries_tried"]:
                time.sleep(SEARCH_MIN_GAP)
            last = search_native(q, size=size, session=session, match="title")
            last["doc_number"] = doc_number
            last["_query"] = q
            last["_queries_tried"] = queries[:queries.index(q) + 1]
            if last["results"]:
                last["_caveat"] = ("站点按子串匹配标题：候选里可能有地方机关在文号前加自家简称的"
                                   "同号文件，也可能有只是提到这枚文号的解读文章，"
                                   "按发文机关与标题逐条认定")
                if q != raw:
                    last["_caveat"] += f"；这一轮提交的查询串是「{q}」，与给进来的写法不同"
                return last
        last["_caveat"] = ("这些写法（%s）在站内标题里都 0 条。"
                           "这不代表税屋没有这份文件——可能标题里根本不带这枚文号"
                           % "、".join(queries))
        return last
    finally:
        if own:
            session.close()


def load_columns() -> list:
    """读 data/shui5_columns.json。表不在就返回空列表——栏目只能从表里查，不作猜。"""
    if not COLUMN_PAGES.exists():
        return []
    try:
        return json.loads(COLUMN_PAGES.read_text(encoding="utf-8")).get("columns", [])
    except (ValueError, OSError):
        return []


def resolve_column(name) -> dict | None:
    """目录名、导航名、首页短标签、classid 或行内栏目名 → 表里那一行。

    顺序：目录名（不分大小写）→ 导航名 → 短标签 → classid → 行内栏目名（只有唯一一栏命中才认）。
    归属的判据是行里的 `column` 字段，不是目录名：山西的目录是 `category_181`，
    而 `ShanXiShengCaiShuiFaGui` 这一栏的行名是陕西省法规，两个目录的文章链接集合重合 0 条。
    """
    rows = load_columns()
    key = str(name or "").strip()
    if not key or not rows:
        return None
    low = key.lower()
    for r in rows:
        if r["dir"].lower() == low:
            return r
    for field in ("nav", "label"):
        for r in rows:
            if r.get(field) == key:
                return r
    if key.isdigit():
        for r in rows:
            if str(r["classid"]) == key:
                return r
    same = [r for r in rows if key in (r.get("columns") or [])]
    return same[0] if len(same) == 1 else None


def classid_of(name) -> str:
    r = resolve_column(name)
    return str(r["classid"]) if r else ""


def classid_spec(spec) -> str:
    """把「152,12673」或「北京,深圳」这类写法换成站点认的逗号 classid 串。

    站点只认逗号：竖线 `152|12673` 与数组 `classid[]=152` 都不起作用（2026-10-18 实测）。
    任一段查不到就整体返回空串，由调用方报缺——半截限定会让结果看着像收窄了其实没有。
    """
    out = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        cid = part if part.isdigit() else classid_of(part)
        if not cid:
            return ""
        out.append(cid)
    return ",".join(out)


def _col_control(page: str) -> tuple | None:
    """列表页控件里的「共 X 页 Y 条记录」：读到回 (页数, 条数)，没有这个控件回 None。

    不写成 `tuple[int, int] | None`：门禁 `TestTupleReturnContract` 按 `-> tuple[...]`
    前缀扫，要求每个 return 都给同长元组字面量，回 None 即判违规。
    """
    c = _COL_CTRL_RE.search(re.sub(r"<[^>]+>", " ", page))
    return (int(c.group(1)), int(c.group(2))) if c else None


def list_column(column, pages: int = 1, session: requests.Session | None = None,
                gap: float = SEARCH_MIN_GAP) -> dict:
    """按栏目取清单：`/article/<目录>/<classid>_<页>.html`。

    行容器与检索结果页同为 div.xwt2，所以 `_search_rows` 一份判据通吃这两类页面。
    换成全页 a[title=] 取链接会把页头推荐区、侧栏与页脚的共有块也算进来——同一栏目
    第 1、2 页那样取有 64 条重合，按 xwt2 行取重合 0 条（2026-10-18 实测）。
    `?page=2` 那种猜法返回第 1 页原样（参数被忽略，不报错），所以翻页只能用控件里的
    `<classid>_<页>.html` 形态。

    Args:
        column: 目录名、导航名、classid 或行内栏目名，查 data/shui5_columns.json
        pages: 从第 1 页起连读几页；每页 30–35 行
        session: 已过 WAF 的会话，多篇连读时传进来
        gap: 页间隔

    Returns:
        {"results","_status","_column","_read_pages","_error"?}；行的 column 字段是
        这篇真正所属的栏目，父栏（如 145 地方法规）的页面里会混着各子栏的名字。
    """
    row = resolve_column(column)
    if not row:
        return {"results": [], "_read_pages": 0,
                "_status": f"没走成：栏目表里没有「{column}」；"
                           f"表缺失或改版时跑 python scripts/tax_shui5.py --update-columns"}
    own = session is None
    if own:
        session, how = _pass_waf()
        if session is None:
            return {"results": [], "_read_pages": 0, "_status": f"没走成：{how}"}
    try:
        stem = f"{SHUI5_BASE}article/{row['dir']}/"
        rows, seen, errs, ctrl, read = [], set(), [], None, 0
        for n in range(1, max(1, int(pages)) + 1):
            if n > 1:
                time.sleep(gap)
            page, err = _read_page(session, stem if n == 1
                                   else f"{stem}{row['classid']}_{n}.html")
            if err:
                errs.append(f"第 {n} 页：{err}")
                break
            read += 1
            if ctrl is None:
                ctrl = _col_control(page)
            for x in _search_rows(page):
                if x["url"] not in seen:
                    seen.add(x["url"])
                    rows.append(x)
        out = {"results": rows, "_column": row, "_read_pages": read,
               "_status": (f"命中 {len(rows)} 条（栏目 {row.get('nav') or row['dir']}，"
                           + (f"共 {ctrl[0]} 页 {ctrl[1]} 条）"
                              if ctrl else "控件未读到页数）"))}
        if errs:
            out["_error"] = "；".join(errs)
        return out
    finally:
        if own:
            session.close()


def build_column_table(session: requests.Session | None = None,
                       gap: float = SEARCH_MIN_GAP) -> dict:
    """逐栏取一次列表页，重建栏目表：目录名、导航名、classid、页数条数与行内栏目名。

    目录名与 classid 都只能从页面链接里取：47 个目录名里 45 个是拼音串，但拼音串里
    5 个被站点截断（`GuangDongShengCaiShuiFaGu`、`XinJiangZiZhiQuCaiShuiFaG`）、
    4 个按站点自己的拼法（`NinBoShiFaGui`、`DaiLianShiFaGui`、`ShenZhengShiFaGui`、
    `XiaMenShiFaGui`），另外 `Bulletin` 是英文、`category_181` 是数字号；
    按拼音拼会拼出站点上不存在的地址。
    """
    own = session is None
    if own:
        session, how = _pass_waf()
        if session is None:
            return {"columns": [], "_status": f"没走成：{how}"}
    try:
        home, err = _read_page(session, SHUI5_BASE)
        if err:
            return {"columns": [], "_status": f"没走成：首页取不到（{err}）"}
        dirs = set()
        for h in re.findall(r'href="([^"]+)"', home):
            p = re.sub(r"^https?://[^/]+", "", h).split("?")[0]
            if re.match(r"^/article/[^/]+/?$", p):
                dirs.add(p.rstrip("/").split("/")[-1])
        # 导航名只在 li.mainlevel 那一块（11 个），36 个地方栏的名字是首页其他位置
        # 的锚文本，站点自己就把它们截了短（黑龙／内蒙／陕西），只当别名用
        nav, label = {}, {}
        for h, text in _MAINLEVEL_RE.findall(home):
            p = re.sub(r"^https?://[^/]+", "", h).split("?")[0]
            if re.match(r"^/article/[^/]+/?$", p):
                nav[p.rstrip("/").split("/")[-1]] = _to_text(text)
        for h, text in re.findall(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', home, re.S):
            p = re.sub(r"^https?://[^/]+", "", h).split("?")[0]
            d = p.rstrip("/").split("/")[-1]
            if d in dirs and _to_text(text) and d not in label:
                label[d] = _to_text(text)
        rows = []
        for i, d in enumerate(sorted(dirs), 1):
            if i > 1:
                time.sleep(gap)
            page, err = _read_page(session, f"{SHUI5_BASE}article/{d}/")
            if err:
                rows.append({"dir": d, "nav": nav.get(d, ""), "_error": err})
                continue
            ids = _COL_PAGE_LINK_RE.findall(page)
            cols = sorted({x["column"] for x in _search_rows(page) if x.get("column")})
            ctrl = _col_control(page)
            rows.append({"dir": d, "nav": nav.get(d, ""), "label": label.get(d, ""),
                         "classid": int(max(set(ids), key=ids.count)) if ids else None,
                         "columns": cols,
                         "pages": ctrl[0] if ctrl else None,
                         "records": ctrl[1] if ctrl else None})
        return {"built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": SHUI5_BASE, "columns": rows,
                "_status": f"取到 {len([r for r in rows if r.get('classid')])} / {len(rows)} 栏"}
    finally:
        if own:
            session.close()


def tags_of(page: str) -> list:
    """页面里站点自己在用的标签名（已解码），按出现顺序去重。

    标签名只能从页面链接里取：站点没有可枚举的标签清单，离线猜名只会落进空结果。
    取的对象是栏目页与标签页。文章页上那一对是页面模板的固定导航（2026-10-18 四篇
    取样回同一对，链接位置比正文容器起点早 5,949–6,482 字符），不是"本篇打了哪些标签"，
    别拿它当相关文章的入口——按标签取文章用 list_tag。
    """
    out, seen = [], set()
    for raw in _TAG_HREF_RE.findall(page):
        name = unquote(raw).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def list_tag(tag: str, pages: int = 1, session: requests.Session | None = None,
             gap: float = SEARCH_MIN_GAP) -> dict:
    """按标签取清单：`/e/tags/?tagname=<标签>`，翻页是 `&page=N`（从 2 起）。

    标签页与检索结果页、栏目列表页同为 div.xwt2 行容器，所以行判据共用一份。
    实测「增值税法」第 1 页与第 2 页各 33 行、首行日期从 2026-03-03 变成 2026-01-23，
    确认可翻；标签页没有「共 X 页 Y 条记录」控件，页数只能一页一页读到空为止。
    标签跨栏目但跨不跨得开看不准：「社保基数」首屏 33 行覆盖 26 个行内栏目名
    （各省、市法规），「增值税法」首屏 33 行全在财税实务一栏。
    所以第 2 页回空有两种成因——这个标签本身只有一页，或翻页失效，
    判之前要先看第 1 页有没有行（probe_shui5 第 10 项按此逐个标签试）。
    """
    own = session is None
    if own:
        session, how = _pass_waf()
        if session is None:
            return {"results": [], "_read_pages": 0, "_status": f"没走成：{how}"}
    try:
        q = quote(tag.strip())
        rows, seen, errs, read = [], set(), [], 0
        for n in range(1, max(1, int(pages)) + 1):
            if n > 1:
                time.sleep(gap)
            page, err = _read_page(session, f"{TAG_RESULT}?tagname={q}"
                                          + ("" if n == 1 else f"&page={n}"))
            if err:
                errs.append(f"第 {n} 页：{err}")
                break
            if NO_RESULT_MARK in page:
                errs.append(f"第 {n} 页：站点回「{NO_RESULT_MARK}」，这个标签名不存在")
                break
            got = _search_rows(page)
            if not got:
                errs.append(f"第 {n} 页：没有 xwt2 行")
                break
            read += 1
            for x in got:
                if x["url"] not in seen:
                    seen.add(x["url"])
                    rows.append(x)
        out = {"results": rows, "_read_pages": read,
               "_status": f"命中 {len(rows)} 条（标签 {tag.strip()}）"}
        if errs:
            out["_error"] = "；".join(errs)
        return out
    finally:
        if own:
            session.close()


def read_articles(urls: list, read_interval: float = READ_INTERVAL) -> list:
    """
    连续取多篇正文，WAF 只过一次。

    这是批量取正文的正确入口：浏览器预热 5.9~7.0 秒过挑战，之后每篇 0.2 秒级；
    逐篇重启浏览器是把 6 秒乘以篇数。浏览器不可用时退回纯 HTTP 逐篇握手（多数会被拦）。
    每篇内部还有续页时一并读到末页（见 fetch_shui5），续页之间同样留间隔。

    Args:
        urls: 税屋文章地址列表
        read_interval: 逐篇之间的间隔秒数，站点对高频请求敏感

    Returns:
        与 urls 等长的 dict 列表，每项含 url/content/title/date/summary/pages，
        取不到正文的项带 _error，整体取正文的方式记在 _how。
    """
    if not urls:
        return []

    session, how = _pass_waf(urls[0])

    out = []
    try:
        for i, u in enumerate(urls):
            out.append(fetch_shui5(u, session=session, page_interval=read_interval))
            if i < len(urls) - 1:
                time.sleep(read_interval)
    finally:
        if session is not None:
            session.close()
    for row in out:
        row["_how"] = how
    return out


def read_article(url: str) -> tuple[str, str]:
    """
    取单篇文章正文。

    2026-09-27 实测：浏览器过 WAF 后正文 3/3 取到，3,026~9,858 字；
    纯 HTTP 按公开算法算 cookie 直接读 0/3 被拦；Jina Reader 连接超时。

    Args:
        url: 税屋文章地址

    Returns:
        (正文, 错误信息)。成功时错误信息为空串。
    """
    row = read_articles([url], read_interval=0)[0]
    if row.get("content") and not row.get("_error"):
        header = "".join(
            f"{k}: {row[k]}\n" for k in ("title", "date", "summary") if row.get(k))
        return f"{header}\n{row['content']}", ""

    direct_err = row.get("_error", "正文为空")
    try:
        r = requests.get(JINA_READER + url, timeout=TIMEOUT_READ)
    except requests.RequestException as e:
        return "", f"直连失败({direct_err})；Jina 请求失败：{e}"

    if r.status_code != 200:
        return "", f"直连失败({direct_err})；Jina HTTP {r.status_code}"
    # Jina 的 403 是 Cloudflare 拦截页，正文里不会有 "Markdown Content:"
    if "Markdown Content:" not in r.text:
        return "", (f"直连失败({direct_err})；"
                    "Jina 未返回正文（可能被 Cloudflare 拦截）")
    return r.text, ""


def _strip_site_suffix(title: str) -> str:
    """去掉标题里的税屋站点后缀。

    搜狗与 Jina 给的标题都带同一句站点标语；搜狗还会把整条标题截断，
    截断点落在标语中间（实测 "..._税屋——第一时间传递财..."），所以不能只按
    完整的标语正文切。先切截断形态的 "_税屋——"，再按完整标语与结尾站名兜底。
    """
    if not title:
        return ""
    head = re.split(r"第一时间传递财税政策法规", title)[0]
    # 截断形态：下划线/分隔符 + 税屋 + 至少一枚破折号，后面有没有正文都算后缀
    head = re.split(r"[_|!！·、]*\s*[-—]{0,2}\s*税\s*屋\s*[-—]+", head)[0]
    head = re.split(r"[_|!！·、]*\s*[-—]{0,2}\s*税\s*屋\s*[-—]{0,2}\s*$", head)[0]
    head = re.split(r"[_|!！·、]*\s*[-—]{0,2}\s*屋\s*$", head)[0]
    return head.strip(" _-—!！|·、") or title.strip()


# ── CLI ─────────────────────────────────────────────────────────────────────
def _print_rows(rows: list, read_body: bool = False) -> None:
    for item in rows:
        print(f"  {item['title'][:70]}")
        print(f"     {item['url']}")
        meta = " ".join(x for x in (item.get("date", ""), item.get("column", "")) if x)
        if meta:
            print(f"     {meta}")
        if item.get("_error"):
            print(f"     正文读取失败: {item['_error']}")
        elif item.get("content"):
            body = item["content"].split("Markdown Content:", 1)[-1].strip()
            print(f"     正文 {len(body)} 字／{item.get('pages', 1)} 页，"
                  f"节选：{body[:100]}...")
            if item.get("_pages_error"):
                print(f"     {item['_pages_error']}")
        print()


def _print_table(rows: list) -> None:
    for r in rows:
        print(f"  {r['dir']:34s} classid={str(r.get('classid')):>6} "
              f"{str(r.get('pages')):>5}/{str(r.get('records')):<6} "
              f"{r.get('nav') or r.get('label') or '':6s} "
              f"{'、'.join(r.get('columns') or [])}")


def main():
    p = argparse.ArgumentParser(description="税屋 (shui5.cn) 检索、栏目清单与标签清单")
    p.add_argument("keyword", nargs="?",
                   help="检索词。用 --column / --tag 取清单时可以不填")
    p.add_argument("--size", type=int, default=None,
                   help="返回条数上限，默认 5；--find-doc 那一档默认 20，"
                        "因为同一枚文号的候选常常不止五条")
    p.add_argument("--read", action="store_true", help="同时取回正文（较慢）")
    p.add_argument("--channel", choices=["auto", "sogou", "native"], default="auto",
                   help="auto=先搜狗、被拦或 0 条时换站内；sogou=只走搜狗；"
                        "native=只走站内原生检索（要过一次 WAF，每行带日期与栏目）。"
                        "带 --column/--from/--to/--match title/--all 时这一路只能是站内")
    p.add_argument("--column", help="检索时限定栏目：目录名、导航名或 classid，"
                                    "多个用逗号（如 152,12673、北京,深圳 都行）。"
                                    "站点只认逗号，竖线和 classid[] 写法不起作用")
    p.add_argument("--list", metavar="栏目", help="不检索，改取该栏目的清单")
    p.add_argument("--tag", help="不检索，改按站点标签取清单（标签名照抄页面链接里的值）")
    p.add_argument("--pages", type=int, default=1, help="--list / --tag 连读几页，每页 30–35 行")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                   help="发布日期起。站点只在与 --to 成对时起作用")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="发布日期止")
    p.add_argument("--match", choices=["broad", "title"], default="broad",
                   help="broad=标题+关键词+摘要，title=只在标题里匹配")
    p.add_argument("--all", action="store_true",
                   help=f"站内检索按总条数翻到底，上限 {SEARCH_ALL_MAX_PAGES} 页")
    p.add_argument("--find-doc", metavar="文号",
                   help="不检索关键词，改按文号回站内定位：半角/全角括号与带不带空格都接受，"
                        "候选全部交出（站点按子串匹配，同号不同机关会一起回来）")
    p.add_argument("--columns", action="store_true", help="打印 data 里的栏目表，不发请求")
    p.add_argument("--update-columns", action="store_true",
                   help="重建栏目表并写盘：首页一次 + 每栏一次 = 48 个请求")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.update_columns:
        table = build_column_table()
        if args.json:
            print(json.dumps(table, ensure_ascii=False, indent=2))
            if not table.get("columns"):
                sys.exit(1)
            return
        print(f"栏目表 | {table['_status']}")
        _print_table(table["columns"])
        if table.get("columns"):
            COLUMN_PAGES.write_text(
                json.dumps({"_built_at": table["built_at"], "_source": table["source"],
                            "columns": table["columns"]},
                           ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"\n已写入 {COLUMN_PAGES}")
        else:
            print(f"没有重建：{table['_status']}")
            sys.exit(1)
        return

    if args.columns:
        rows = load_columns()
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        print(f"栏目表 {len(rows)} 栏（{COLUMN_PAGES}）")
        _print_table(rows)
        return

    size = args.size if args.size is not None else (20 if args.find_doc else 5)

    if args.find_doc:
        got = find_by_doc_number(args.find_doc, size=size)
        if args.json:
            print(json.dumps(got, ensure_ascii=False, indent=2))
        else:
            print(f"文号「{got['doc_number']}」| 提交串「{got['_query']}」| {got['_status']}")
            if got.get("_caveat"):
                print(f"注意：{got['_caveat']}")
            _print_rows(got["results"])
        if not got["results"]:
            sys.exit(1)
        return

    if args.tag or args.list:
        got = (list_tag(args.tag, pages=args.pages) if args.tag
               else list_column(args.list, pages=args.pages))
        if args.json:
            print(json.dumps(got, ensure_ascii=False, indent=2))
        else:
            print(got["_status"])
            if got.get("_error"):
                print(f"注意：{got['_error']}")
            _print_rows(got["results"])
        if not got["results"]:
            sys.exit(1)
        return

    if not args.keyword:
        p.error("缺检索词（或改用 --column/--list/--tag/--find-doc/--columns）")

    result = search_shui5(args.keyword, size=size, read_body=args.read,
                          channel=args.channel, column=args.column,
                          date_from=args.date_from, date_to=args.date_to,
                          match=args.match, fetch_all=args.all)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"税屋检索 \"{args.keyword}\" | {result['searched_at']}")
    print("通道：" + "，".join(f"{k} {v}" for k, v in result["_channels"].items()))
    if result.get("_filter_note"):
        print(f"提醒：{result['_filter_note']}")
    if result.get("_error"):
        print(f"注意：{result['_error']}")
    print(f"共 {result['total']} 条\n")
    _print_rows(result["results"], args.read)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    main()
