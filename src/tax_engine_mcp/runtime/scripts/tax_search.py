#!/usr/bin/env python3
"""
Tax Policy Search — search China tax laws/regulations via NPC API (flk.npc.gov.cn).

Usage:
  # Title search (default)
  python tax_search.py "增值税" --size 20
  # Exact title search
  python tax_search.py "中华人民共和国增值税法" --exact
  # Date range + sort by publish date
  python tax_search.py "企业所得税" --status 3 --from 2024-01-01 --sort date
  # Verbose output (includes article snippets)
  python tax_search.py "增值税" --status 3 --verbose
  # Enable cache (5min TTL)
  python tax_search.py "增值税" --cache
  # JSON output for piping
  python tax_search.py "个人所得税" --json
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# Fix Windows console encoding
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tax_http  # noqa: E402
import tax_terms as TT  # noqa: E402
from tax_http import VERIFY_SSL  # noqa: E402,F401

# ── Constants ───────────────────────────────────────────────────────────────
_CLEAN_HTML_RE = re.compile(r"<[^>]+>")
BASE_URL = "https://flk.npc.gov.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://flk.npc.gov.cn/",
    "Accept": "application/json, text/plain, */*",
}

# Status code mapping
SXX_MAP = {1: "已废止", 2: "已修改", 3: "现行有效", 4: "尚未生效"}
SXX_REVERSE = {v: k for k, v in SXX_MAP.items()}

# ── Cache (disabled by default, short TTL when enabled) ─────────────────────
# 缓存的唯一实现在 tax_cache.py。本模块不再自带一份：两处各写一份 TTL 与
# 失效规则，早晚会各自演化（"三处重复必漂移"的老毛病）。
# 约定：只缓存"检索清单 / 元数据"，**正文永不缓存**。
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from tax_cache import CacheManager  # noqa: E402

# NPC 检索清单 TTL（秒）。税收政策随时更新，故取短 TTL 保证新鲜度。
CACHE_TTL = 300
_cache = CacheManager(enabled=False, namespace="search")


# ── 税种与专题 → 检索词映射 ──────────────────────────────────────────────────
# authority 标明该专题的依据住在哪一源：
#   "npc"       有本体法，直接按 title 检索该法
#   "sta"       没有本体法。转让定价、反避税这类专题的依据是税务总局的规范性文件
#               （特别纳税调整实施办法、转让定价同期资料文档管理办法等），NPC 库里
#               只有法律、行政法规，搜"转让定价"返回的是国有土地使用权出让和转让
#               暂行条例这类无关法规，搜"反避税"返回 0 条。见到 authority=sta 的
#               专题就改查税务总局，不要拿 NPC 的结果当依据。
#   "overseas"  规则本体在境外辖区立法与国际组织框架里，中国的官方法规库里没有
#               对应文件（取值见 OVERSEAS_REGIME_NOTE 那句实测）。这一类不能按 sta
#               去查文件类标签——那是 0 条；也不能按 npc 去查本体法——那会取回标题
#               里含"企业"二字的无关法律。要查的是关掉文件类标签的全站动态层。

# 这一句由 `tax_answer` 与网页界面原样带进输出，判据与话术只在这一处写。
OVERSEAS_REGIME_NOTE = (
    "支柱二与全球最低税不构成中国的征税依据：2026-10-04 实测总局法规库在文件类标签下"
    "检索「支柱二」「全球最低税」「GloBE」各 0 条；关掉文件类标签的全站范围里"
    "「支柱二」34 条、「全球最低税」41 条、「收入纳入规则」14 条，来源是"
    "「税路通全球税讯」编译的境外辖区立法动态。取回的这些条目说的是别的辖区立了什么、"
    "哪个年度起生效，答案要按辖区与年度写清规则主体是哪一国、生效年度是哪一年；境内"
    "这一侧相邻的是「境外所得」「税收抵免」「受控外国企业」三个专题，各按自己的检索词"
    "另查一轮，不要拿它们顶替支柱二。")

TAX_TYPE_KEYWORDS = {
    # 流转税
    "增值税": {
        # "简易计税"是增值税专有的计税方法名（一般计税相对），题面常只写它、
        # 不写"增值税"三字。试过"营改增"，但它会把非居民企业扣缴类题改判过来，没收。
        "aliases": ["增值税", "VAT", "进项税", "销项税", "留抵退税", "增值税专用发票", "增值税普通发票",
                    "简易计税"],
        "parent_law": "中华人民共和国增值税法",
        "priority": 1,
    },
    "消费税": {
        "aliases": ["消费税", "卷烟", "成品油", "汽车消费税"],
        # 消费税至今没有立法为"消费税法"，NPC 查"中华人民共和国消费税法"返回的是
        # 宪法等无关法规。实际依据是 2008 年修订、现行有效的暂行条例。
        "parent_law": "中华人民共和国消费税暂行条例",
        "priority": 2,
    },
    "关税": {
        # 加"关境"是因为它出自《海关法》而非关税法，不补会掉进税收征管项
        # 加"反倾销"：口语比"反倾销税"更常用，SKILL.md 的口语映射表里列的就是它
        "aliases": ["关税", "进出口税", "反倾销", "反倾销税", "保税", "海关", "关境", "关境内外"],
        "parent_law": "中华人民共和国关税法",
        "priority": 3,
    },
    # 所得税
    "企业所得税": {
        # "所得税"单列：题目常写"所得税处理""所得税优惠"而不带"企业"二字，
        # 只靠"企业所得税"整串命中时，未路由的题就拿原话去检索了。
        # 最长别名优先，所以"个人所得税"仍然归个税，不会被这条抢走。
        # 后半批是"只讲业务事实、不点税种名"的事务词：题面写"长期待摊费用"
        # "政策性搬迁"时全句不出现"企业所得税"，归类为空就只好拿原话去标题检索。
        # 用评测集 1001 题逐题回归：这 11 条净增 13 题命中、零改判。
        # 试过而没收的候选及其代价——应纳税所得额（改判 13 题，把个税、土增、
        # 关税的题抢过来）、业务招待费/广告费（6 题）、收入确认（1 题，且题面已
        # 点名个税）、公益性捐赠（1 题，个人捐赠归个税）、限售股（个人转让限售股
        # 按财产缴个税，不是企税）。共同原因是这些词跨税种通用，而排序按别名长度
        # 定胜负，长通用词会压过题面里真正的税种名。要收它们得先给别名加税域标注。
        "aliases": ["企业所得税", "所得税", "企业重组", "特殊性税务处理",
                    "一般性税务处理", "应税所得", "税前扣除", "加计扣除",
                    "高新技术企业", "小微企业", "西部大开发",
                    "非货币性资产交换", "长期股权投资", "长期待摊费用",
                    "开办费", "生产性生物资产", "政策性搬迁", "资产损失",
                    "股权收购", "资产收购", "债资比例", "股权转让"],
        "parent_law": "中华人民共和国企业所得税法",
        "priority": 1,
    },
    "个人所得税": {
        # 加"个税"：最常用的口语简称，但它是"个人所得税"的非连续子串，子串匹配命不中。
        # 代价是已知的——本函数是 alias in query，"个税"两字会出现在"这个税率""哪个
        # 税种"这类无辜问法里，那时会被路由到这里。2026-09-28 用全量题库 1360 题回归：
        # 只新增 1 条命中、零抢占、零丢失（语料里没有上述问法；那不等于零误差）。
        # 保留它的理由是收益明确——"个税起征点""个税怎么算"这类提问高频；误命中的场景
        # 靠返回值里的 matched_alias == "个税" 可识别，SKILL.md 已把它写成一条踩空提示。
        "aliases": ["个人所得税", "个税", "综合所得", "专项附加扣除", "年度汇算",
                    "劳务报酬", "稿酬所得", "稿酬", "特许权使用费", "远洋船员",
                    # 个税法原文写作"工资、薪金所得"，带顿号；只收"工资薪金"这类
                    # 连写会漏掉按法条原文出题的题面。两种写法都收。
                    "经营所得", "工资、薪金", "工资薪金"],
        "parent_law": "中华人民共和国个人所得税法",
        "priority": 1,
    },
    # 财产行为税
    "房产税": {
        "aliases": ["房产税", "房地产税", "房屋租赁税"],
        "parent_law": "中华人民共和国房产税暂行条例",
        "priority": 4,
    },
    "土地增值税": {
        # "土地増值税"是题库原文里的日式字形（増 U+5827，不是"增"）。它不是 NFKC
        # 能折叠的兼容字符，归一化救不了，只能按字面收进别名表；eval_retrieval 的
        # 表面词表早就收了它，路由表却没收，于是这类题拿原话去检索。
        "aliases": ["土地增值税", "土地増值税", "土增税", "清算"],
        "parent_law": "中华人民共和国土地增值税暂行条例",
        "priority": 4,
    },
    "契税": {
        "aliases": ["契税", "不动产登记"],
        "parent_law": "中华人民共和国契税法",
        "priority": 4,
    },
    "城镇土地使用税": {
        "aliases": ["城镇土地使用税", "土地使用税"],
        "parent_law": "中华人民共和国城镇土地使用税暂行条例",
        "priority": 4,
    },
    # 表里缺这一行时，题面写"占用耕地要缴什么税"就无税种可归，
    # 只能拿原话去 NPC 标题检索。它与城镇土地使用税是两个税种、两部法规：
    # 占地建房缴耕地占用税，持有期间逐年缴城镇土地使用税。
    "耕地占用税": {
        "aliases": ["耕地占用税", "占用耕地", "耕地"],
        "parent_law": "中华人民共和国耕地占用税法",
        "priority": 4,
    },
    "车船税": {
        # 别名里不放"船舶吨税"：吨税由海关按《船舶吨税法》单独征收，
        # 车船税法不含吨税。挂在车船税下，"船舶吨税免征情形"这类题会拿到
        # 车船税法当本体法——上位法指错，后面引哪一条都白搭。
        "aliases": ["车船税", "车船使用税"],
        "parent_law": "中华人民共和国车船税法",
        "priority": 5,
    },
    "船舶吨税": {
        # "吨税"是"船舶吨税"的连续子串，单列只为题面只写简称的情形
        "aliases": ["船舶吨税", "吨税"],
        "parent_law": "中华人民共和国船舶吨税法",
        "priority": 5,
    },
    # 车辆购置税是独立税种、独立立法，不归入车船税：车船税法不含车辆购置税，
    # 两者是并列关系，混在一处会把"买车缴税"的问题指到错误的法上。
    "车辆购置税": {
        "aliases": ["车辆购置税", "车购税", "购车税", "新车购置税", "买车缴税"],
        "parent_law": "中华人民共和国车辆购置税法",
        "priority": 5,
    },
    "印花税": {
        "aliases": ["印花税", "合同印花税", "账簿印花税"],
        "parent_law": "中华人民共和国印花税法",
        "priority": 4,
    },
    "城市维护建设税": {
        "aliases": ["城市维护建设税", "城建税", "教育费附加", "地方教育附加"],
        "parent_law": "中华人民共和国城市维护建设税法",
        "priority": 4,
    },
    # 烟叶税由《烟叶税法》单独立法，是纳税主体为烟草收购方（收购烟叶的单位）的
    # 独立税种，与前面那几项并列为现行十八税种之一。它曾挂在词表末尾一个
    # 「从业/凭证类」段下——那个段名既不是税种也不是专题，界面按词表顺序铺快捷栏时
    # 就把它铺到了最后一屏，18 个税种里只有这一个默认点不到。
    "烟叶税": {
        "aliases": ["烟叶税", "烟叶"],
        "parent_law": "中华人民共和国烟叶税法",
        "priority": 5,
    },
    # 资源环境税
    "资源税": {
        "aliases": ["资源税", "水资源税", "矿产资源税"],
        "parent_law": "中华人民共和国资源税法",
        "priority": 4,
    },
    "环境保护税": {
        "aliases": ["环境保护税", "环保税", "排污税"],
        "parent_law": "中华人民共和国环境保护税法",
        "priority": 4,
    },
    # 其他
    "税收征管": {
        "aliases": ["税收征管", "税收征收管理", "征管", "税务登记", "纳税申报",
                    "发票管理", "发票", "税务稽查", "金税四期"],
        "parent_law": "中华人民共和国税收征收管理法",
        "priority": 1,
    },
    # 税收优惠没有本体法，但依据确实是总局文件（财税〔2008〕1号、各税种优惠
    # 公告等），按 npc 查会拿到税收征管法、实施细则这类无关法律，所以路由到 sta。
    "税收优惠": {
        "aliases": ["税收优惠", "减免税", "退税", "即征即退", "先征后退", "免税"],
        "search_term": "税收优惠",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    # ── 国际税收 ──
    # 这几项的依据全在税务总局规范性文件与税收协定公告里，NPC 库检索无效：
    # 搜"税收协定"命中 29 条全无关，搜"常设机构"命中 70 条全无关，
    # 搜"转让定价"命中 10 条全是土地/森林/矿产资源转让条例。
    # 每一项单列成一个专题，因为它们的依据是各自独立的文件，合在一起就没有
    # 一个能用的检索词——搜"国际税收"命中 988 条，头几条全是外国税改新闻。
    # search_term 是实测能翻出依据的检索词，不填就拿键名去搜。键名只是分类标签，
    # 不是检索词："税收争议救济"、"纳税担保与信用"当检索词都取不到依据。
    "税收协定": {
        "aliases": ["税收协定", "双重征税", "重复征税", "国际重复征税",
                    "双重居民身份", "税收居民身份", "居民企业身份", "税收条约"],
        # "税收协定"这个检索词已经翻不出法规库条目了：实测总局命中 965 条，
        # 前 3 页一条法规库文件都没有；换成"双重征税"首条就是执行双边协定的条文解释。
        "search_term": "双重征税",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    "常设机构": {
        "aliases": ["常设机构", "营业场所", "固定场所", "工程场所", "服务机构"],
        "search_term": "常设机构",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    "非居民企业": {
        "aliases": ["非居民企业", "非居民", "源泉扣缴", "预提所得税", "扣缴义务人",
                    "支付所得"],
        "search_term": "非居民企业",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    "境外所得": {
        "aliases": ["境外所得", "境外投资", "境外股息", "境外权益", "递延纳税"],
        "search_term": "境外所得",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    "税收抵免": {
        "aliases": ["税收抵免", "抵免限额", "抵免额", "分国不分项", "国别抵免"],
        "search_term": "税收抵免",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    "受控外国企业": {
        "aliases": ["受控外国企业", "外国企业股息", "视同股息分配"],
        "search_term": "境外所得",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    "转让定价": {
        "aliases": ["转让定价", "关联交易", "同期资料", "预约定价", "资本弱化",
                    "成本分摊", "国别报告", "特别纳税调整", "受控外国企业",
                    "关联申报"],
        "search_term": "转让定价",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    "反避税": {
        "aliases": ["反避税", "一般反避税", "特别纳税调整", "BEPS", "税基侵蚀"],
        # "反避税"本身命中的多是税收协定文本，"一般反避税管理办法"只有 4 条
        # 且 0 条来自法规库。改用"预约定价安排"（150 条），前 4 条里有
        #《特别纳税调整实施办法（试行）》与预约定价系列公告。
        "search_term": "预约定价安排",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    # 支柱二这一族不按 sta 配：实测文件类标签下「支柱二」「全球最低税」「GloBE」
    # 各 0 条（2026-10-04），按 sta 走等于第 1 轮取回空清单。也不按"BEPS"收进
    # 反避税项：反避税的检索词是「预约定价安排」，取回的是《特别纳税调整实施办法》
    # 那一族境内规则，拿去答"子公司所在国要征多少补足税"是答错了还看着有依据。
    # 别名里没有裸的"最低税"：它会是"最低税额""最低税率"这类写法的前缀，
    # 收进来把环保税、资源税的题抢走。"IIR" 也不单列，中文题面用的是"收入纳入规则"。
    "全球最低税": {
        "aliases": ["支柱二", "全球最低税", "GloBE", "BEPS2.0", "BEPS 2.0",
                    "收入纳入规则", "低税利润规则", "补足税", "并行方案",
                    "合格国内最低补足税"],
        "search_term": "支柱二",
        "parent_law": None,
        "authority": "overseas",
        "priority": 1,
        "note": OVERSEAS_REGIME_NOTE,
        # 境内这一侧由哪个专题管：编排层按这个键取它的 search_term 补一轮，
        # 不在两处各写一个检索词（见 tax_answer._overseas_rounds）。
        "adjacent": "境外所得",
    },
    # ── 税收征管与救济 ──
    # 检索词都是实测出来的，不是按字面推的：
    #   "税收行政处罚" 命中 7 条且 0 条来自法规库，"行政处罚" 命中 1301 条
    #     且前 3 条就有《税务行政处罚"首违不罚"事项清单》公告。
    #   "税收行政复议" 命中 65 条但前 3 条是无关的查补税金文件，
    #     "行政复议" 命中 231 条，首条即《行政复议法》本身。
    #   "纳税担保" 命中 41 条、前 3 条无担保文件，"纳税信用评价与修复"
    #     命中 13 条且首条即《纳税信用评价与修复有关事项的公告》。
    "税务行政处罚": {
        "aliases": ["税务行政处罚", "行政处罚", "听证", "裁量权", "罚款", "滞纳金"],
        "search_term": "行政处罚",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    "税收争议救济": {
        # 键名自己也收进别名：这一档的五个别名都不是"税收争议救济"这串字的子串，
        # 光靠它们归类时，把键名当检索词发出去（网页首页的税种栏就是按键名出的
        # 按钮）会得到"归不出税种"，返回的是按原话字面匹配的结果。
        "aliases": ["税收争议救济", "行政复议", "行政诉讼", "纳税争议", "起诉期限", "复议前置"],
        "search_term": "行政复议",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    "纳税担保与信用": {
        "aliases": ["纳税担保", "纳税保证人", "纳税信用", "失信主体", "纳税信用修复"],
        "search_term": "纳税缴费信用",
        "parent_law": None,
        "authority": "sta",
        "priority": 1,
    },
    # ── 海关与税收立法 ──
    # 关境不是税种，查不出税收实体法，相关表述散在《海关法》《对外贸易法》里，
    # 因此不新设条目，只把"关境"补进既有的关税项。
    # 不单列"关税与关境"项：把"完税价格""保税"收进独立项会抢走
    # 4 道本已正确命中的题（完税价格属增值税/关税题，补进独立项后落到
    # priority 更低的 sta 项）。
    "税收立法权": {
        "aliases": ["税收立法权", "税收立法", "税收法定", "税收法定性",
                    "税收基本法", "税收管理权限", "开征税收", "税种设立",
                    "税种的设立", "税率的确定", "税收基本制度", "授权立法"],
        # 上位法就是《立法法》第十一条：税种的设立、税率的确定和税收征收管理等
        # 税收基本制度只能制定法律。原来配的是 authority=sta 加检索词"税收法定"，
        # 总局法规库走全文检索，搜"税收法定"取回的四条里第一条是"开学第一课：
        # 与税童行"这类顺带提到该词的解读稿，没有一条能当依据。改查人大库
        # 精确检索，首条即《中华人民共和国立法法》。
        "parent_law": "中华人民共和国立法法",
        "authority": "npc",
        "search_term": "税收法定",
        "priority": 2,
    },
}

TAX_RISK_KEYWORDS = [
    "虚开发票", "骗取留抵退税", "骗取出口退税", "偷税", "逃税", "避税",
    "关联交易", "转让定价", "税收风险", "税务合规", "金税四期指标",
    "两税收入差异", "长亏不倒", "税负率异常", "资金闭环回流",
    "进销项不匹配", "四流合一", "私户收款", "账外经营",
]

INVOICE_KEYWORDS = [
    "增值税发票", "专用发票", "普通发票", "电子发票", "全电发票",
    "发票管理办法", "发票领购", "发票开具", "发票红冲", "发票遗失",
    "发票抵扣", "发票认证", "数电票",
]


def adjacent_search_term(info: dict) -> str:
    """overseas 专题补查境内相邻专题时该用的检索词。

    拿 `adjacent` 回到这张表里取那个专题自己的 `search_term`，检索词只住一处：
    在编排层再写一遍"境外所得"，改表时就漏改这一处。
    """
    return (TAX_TYPE_KEYWORDS.get(info.get("adjacent") or "")
            or {}).get("search_term") or ""


def resolve_tax_type(query: str) -> dict:
    """Resolve user query to best-matching tax type.

    返回值多带三个字段：parent_law（该专题的本体法，无则 None）、
    authority（该专题的依据住在哪一源，"npc"／"sta"／"overseas"，取值含义见
    TAX_TYPE_KEYWORDS 上方那段）、search_term（该专题实测可用的检索词，
    authority="sta" 时拿它查总局法规库，"overseas" 时拿它查关掉文件类标签的
    全站层，都别拿分类名去搜）。
    见到 authority="sta" 就不要再查 NPC，改为 tax_fgk.py 查税务总局；
    见到 "overseas" 时两个库都不查，查 tax_web_search.search_chinatax 并把
    file_only 传 False，同时把该专题的 note 原样带进答案。

    两处排序规则：
    1. 先用题干归类，题干里一个税种信号都没有时才拿整句（含选项）兜底。
       选项是干扰项文本，参与归类会把题带偏；但实测全量题库里有不少题
       只在选项里出现税种名（"哪一种税制实行了多次课征制"就是这样），
       一刀切只读题干会让这部分题从命中退回未路由，所以保留回退，
       并在返回值里用 matched_from="options" 标出来。见 strip_options()。
    2. 同样长度的别名命中时，有本体法的税种排在横切专题之前；横切专题要
       赢过税种，别名得比它长两个字以上。理由是税种能直接给出条文，
       横切专题给出的是一堆公告清单——"海关滞纳金按关税还是增值税算"这种
       题归到关税还能拿到税则，归到税务行政处罚就什么都拿不到。
    """
    def _match(text: str):
        best, best_key = None, None
        for tax_type, info in TAX_TYPE_KEYWORDS.items():
            has_parent = bool(info.get("parent_law"))
            for alias in info["aliases"]:
                if alias not in text:
                    continue
                key = (len(alias) - (0 if has_parent else 1), has_parent, len(alias))
                if best_key is None or key > best_key:
                    best_key = key
                    best = {"type": tax_type, "matched_alias": alias, **info}
        return best

    full = (query or "").strip()
    best = _match(TT.strip_options(full))
    if best is None and TT.strip_options(full) != full:
        best = _match(full)
        if best is not None:
            best["matched_from"] = "options"
    if best is not None:
        best.setdefault("authority", "npc")
    return best


# detect_intent 的取值域，按判定优先级从前往后排（发票 → 风险 → 申报 → 资格 → 兜底）。
# 展示层那两张标签表拿它对齐，漏一档就会在界面上印出原始英文码。
INTENTS = ("invoice", "risk_check", "filing_guide", "eligibility", "policy_lookup")


def detect_intent(query: str) -> str:
    """
    Classify user intent to determine search strategy.
    Returns one of INTENTS.

    这一档词表只管检索路的展示（`tax_formatter.INTENT_HEADERS` 出 markdown 标题、
    `tax_server.INTENT_LABELS` 出界面意图标签），不影响实际检索参数；主线的判型
    是另一套九类词表（`tax_analyze.QUESTION_TYPES`）。三处键集合必须一致，
    由 `tests/test_routing_terms.py::test_intent_vocabularies_agree` 钉住。
    """
    q = query.strip()

    # Invoice-related
    if any(kw in q for kw in ["发票", "开票", "红冲", "抵扣认证", "发票遗失"]):
        return "invoice"

    # Risk check
    risk_signals = ["风险", "会不会被查", "预警", "金税", "合规", "稽查", "会被罚款", "合规吗", "违规"]
    if any(kw in q for kw in risk_signals):
        return "risk_check"

    # Filing guide
    filing_signals = ["申报", "汇算清缴", "截止日期", "怎么申报", "年度汇算", "预缴", "报送", "备案"]
    if any(kw in q for kw in filing_signals):
        return "filing_guide"

    # Eligibility check
    eligibility_signals = ["符合条件", "能不能享受", "符不符合", "是否适用",
                           "可以抵扣吗", "适用吗", "资格", "能享受", "可以享受"]
    if any(kw in q for kw in eligibility_signals):
        return "eligibility"

    # Default: policy lookup
    return "policy_lookup"


# ── NPC API Client ──────────────────────────────────────────────────────────
def _title_match_rank(title: str, keyword: str, parent_law: str = "") -> tuple:
    """给标题模糊检索的结果排序用，键越小越相关。

    NPC 标题模糊检索按发布时间排，返回的只是"标题里含检索词部分字"的法律。
    这里按三个维度重排：是否就是该税种的本体法、是否以检索词结尾（"XX法"
    才是用户要的）、检索词在标题里的位置。命中不到的排到最后。
    最后一位恒为 0：并列项保持接口返回的次序，不按标题字母排。

    parent_law 是 resolve_tax_type() 认出来的税种对应的本体法名，需要调用方
    传进来。"税收征管"这类查询靠关键词本身排不出来：本体法全名是"税收征收
    管理法"，标题里没有"税收征管"四字，而两高的司法解释标题里恰好含这四字，
    会被判成高分排在前面。有本体法兜底才排得对。
    """
    kw = (keyword or "").strip()
    t = (title or "").replace("中华人民共和国", "")
    if not kw:
        return (9, 9, 0)
    if parent_law and t == parent_law.replace("中华人民共和国", ""):
        return (0, 0, 0)
    pos = t.find(kw)
    if pos < 0:
        # 检索词被打散命中（"企业所得税" 命中"企业破产法"），排到最后
        return (3, 8, 0)
    ends_with = t.endswith(kw) or t.endswith(kw + "法")
    return (1 if ends_with else 2, pos, 0)


RELIABILITY_NOTES = {
    "medium": "结果已排序但可能偏题（全文分词匹配）；可用于定位法规，"
              "确定条文归属请回到标题检索。",
}

# 精确检索带日期区间时接口丢掉检索词这件事的说明。写清楚"剩下几条"与
# "接口原报几条"两个数，是因为不写的话 0 条会被读成"这个区间里没有相关法规"，
# 而那恰恰是接口把清单换成全库的结果。
DATE_WIDENED_NOTE = (
    "NPC 的精确检索带上公布日期区间后会丢掉检索词，只按区间返回法律清单。"
    "2026-10-02 本机实测的例子：「中华人民共和国增值税法」起 2026-01-01 回 88 条，"
    "逐条核对标题含该词的是 0 条，同一个词不带日期是 2 条。"
    "本次接口原报 {source_total} 条，按「标题是否含检索词」复核后丢掉 {dropped} 条、"
    "留下 {kept} 条；这里的 total 是复核后的条数，原报的 {source_total} 条留在 "
    "source_total 里。要按日期在全库收窄，请改用数据源「税务总局」或「税务法规库」，"
    "那两路的日期是接口自己收的。"
)

_MIN_INTERVAL = 0.6          # NPC 连续请求过快会直接断连，不回 429
_last_request_at = 0.0

# ── NPC 串行闸（跨进程） ─────────────────────────────────────────────────────
# NPC 限流不回 429，而是断连、或回一份挑战页（见 _is_challenge_page）。几个
# 脚本并发跑必现，文档里写"必须串行"太软，这里直接用锁强制：同一时刻只允许
# 一个进程打 NPC。锁在进程退出时由操作系统自动释放，崩溃不会留死锁。
# 闸本体在 tax_http.SerialGate（搜狗微信共用同一份实现），这里只钉 NPC 的默认值。
SERIAL_LOCK_TIMEOUT = float(os.getenv("TAX_NPC_LOCK_TIMEOUT", "180"))
_SERIAL_LOCK_PATH = Path(tempfile.gettempdir()) / "tax-analysis-npc.lock"


class NpcSerialGate(tax_http.SerialGate):
    """NPC 站专用闸：锁文件与超时环境变量都钉在 NPC 上。

    锁的实现只有一份，在 tax_http.SerialGate —— 搜狗微信那边也要用同一套
    （并发加压会触发反爬），两处各抄一遍文件锁代码迟早只改一处。
    这里只负责把 NPC 的默认值固定下来，不改变调用方看到的签名与行为。
    """

    site = "NPC"

    def __init__(self, path: Path = _SERIAL_LOCK_PATH,
                 timeout: float = SERIAL_LOCK_TIMEOUT):
        super().__init__(path=path, timeout=timeout)


npc_gate = NpcSerialGate()


def _fulltext_match_rank(title: str, keyword: str, parent_law: str, score: float) -> tuple:
    """给正文检索的结果重排用，键越小越相关。

    NPC 的 sort=score 是按全文分词打分，通用词会把无关法规拉高——搜"研发费用
    加计扣除"时《诉讼费用交纳办法》（只命中"费用"）排在《企业所得税法》前面。
    这里只认两种命中：检索词整段出现在标题里，或标题是该税种的本体法。
    两者都没有时按接口给的 score 排，让标题没提到但正文确实相关的法规仍能
    浮上来，score 相同则保持接口次序。
    """
    kw = (keyword or "").strip()
    t = (title or "").replace("中华人民共和国", "")
    if not kw:
        return (9, 0.0, 0)
    if kw in t:
        return (0, 0.0, 0)
    if parent_law and t == parent_law.replace("中华人民共和国", ""):
        return (0, 0.0, 0)
    return (1, -score, 0)


def _is_challenge_page(r: requests.Response) -> bool:
    """判断响应是不是反爬挑战页。

    NPC 触发限流时不回 429，而是回 HTTP 200 + 一份 38,499 字节的 HTML，
    内容是 <noscript><h1>Please enable JavaScript</h1></noscript> 加一段
    混淆 JS。直接 .json() 报的是"Expecting value: line 1 column 1"，
    跟限流看不出关系。并行跑多个检索脚本时必现。
    """
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "json" in ctype:
        return False
    head = r.text[:600].lower()
    return "please enable" in head or "<!doctype html" in head


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """Wrapper with retry for 429, 5xx, connection drops and challenge pages."""
    global _last_request_at
    max_retries = 4
    last_exc = None
    for attempt in range(max_retries):
        gap = _MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if gap > 0:
            time.sleep(gap)
        try:
            # 跨进程串行：NPC 并发会被限流，且不回 429（见 _is_challenge_page）
            with npc_gate:
                r = tax_http.request(method, url, verify=VERIFY_SSL, headers=HEADERS,
                                     timeout=15, **kwargs)
        except requests.RequestException as e:
            # 断连与 429 一样是对方在限流，退避后重试
            last_exc = e
            time.sleep(2 ** (attempt + 1))
            continue
        finally:
            _last_request_at = time.monotonic()
        if r.status_code == 429:
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code in {500, 502, 503} and attempt < max_retries - 1:
            time.sleep(1)
            continue
        if _is_challenge_page(r):
            # 挑战页说明这次没拿到数据。退避越久越容易过去，实测 2/4/8 秒有效。
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
        return r
    if last_exc is not None:
        raise last_exc
    return r


def _norm_title(text: str) -> str:
    """标题复核用的归一化：空白与书名号都不参与比较。"""
    return re.sub(r"[\s《》]+", "", text or "")


# gbrq 区间缺下界时补的这个值。本机 2026-10-03 实测「增值税」×「…2020-12-31」
# 两个下界同为 38 条，取更早的那个不额外排除任何一条，又不会因写死 1949 而漏掉
# 更早入库的文本。
DATE_FLOOR = "0001-01-01"
# gbrq 区间缺上界时补的值，沿用原有的 2099 收口。
DATE_CEIL = "2099-12-31"


def check_iso_date(value, name: str) -> str:
    """把 date_from/date_to 收敛成补零的 YYYY-MM-DD，非法值报错。

    接口对格式不对的日期是静默不按区间收窄，调用方看不出区别，所以在发出去
    之前就要挡住（与 tax_web_search.build_filters 对 cwrqStart 的处理同一条理由）。

    Raises:
        ValueError: 不是补零的 YYYY-MM-DD，或不是真实日期。
    """
    if not value:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} 要 YYYY-MM-DD 字符串，收到 {value!r}")
    d = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise ValueError(f"{name} 要 YYYY-MM-DD（补零）格式，收到 {value!r}")
    try:
        time.strptime(d, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{name} 不是真实日期，收到 {value!r}")
    return d


def title_has_keyword(title: str, keyword: str) -> bool:
    """检索词是否原样出现在标题里（忽略空白与书名号）。

    只用来复核精确检索那一路——接口自己承诺的"精确"就是标题命中，正文检索
    不带日期时命中的也只有《增值税法》与其实施条例两条标题（实测同 2 条），
    所以这一眼复核不会错杀真正的正文命中。
    """
    kw = _norm_title(keyword)
    return bool(kw) and kw in _norm_title(title)


def search_tax(keyword: str, *,
               scope: str = "title",
               search_type: int = 2,
               status: Optional[int] = 3,
               date_from: Optional[str] = None,
               date_to: Optional[str] = None,
               page: int = 1,
               size: int = 20,
               sort: str = "relevance") -> dict:
    """
    Search tax policies via NPC API.

    Args:
        keyword: search term
        scope: 'title' (searchRange=1) or 'fulltext' (searchRange=2). fulltext
            is NOT filtered by the search term; it is kept only so callers get an
            explicit _reliability=low marker. Use tax_web_search.py for real
            body-text search.
        search_type: 1=exact, 2=fuzzy
        status: None=all, 3=effective, or any sxx code
        date_from: ISO date string e.g. '2024-01-01'
        date_to: ISO date string e.g. '2026-12-31'. 与 date_from 之一即可触发
            区间——缺 date_to 时按 DATE_CEIL 收口，只给 date_to 时按 DATE_FLOOR
            补下界（2026-10-03 实测只给上界而不下发区间时，「增值税」回 45 条、
            首条日期 2025-12-25，与不带日期完全一样，是空转不是筛过）。
            精确检索带区间时接口会丢掉检索词，取回后按标题复核，复核掉条目时
            写 `_date_note` 与 `source_total`，`total` 换成复核后的条数。
        page: page number
        size: results per page (max 100)
        sort: 'relevance' or 'date'

    Raises:
        ValueError: date_from/date_to 不是补零的 YYYY-MM-DD 真实日期。接口对
            这类值是静默不收窄，报出来比回一份没筛过的清单好。
    """
    date_from = check_iso_date(date_from, "date_from")
    date_to = check_iso_date(date_to, "date_to")
    search_range = 1 if scope == "title" else 2
    sxx = [status] if status is not None else []
    gbrq = []
    if date_from and date_to:
        gbrq = [date_from, date_to]
    elif date_from:
        gbrq = [date_from, DATE_CEIL]
    elif date_to:
        gbrq = [DATE_FLOOR, date_to]

    sort_param = {"order": "", "sort": ""}
    if sort == "date":
        sort_param = {"order": "-1", "sort": "gbrq"}
    elif search_range == 2:
        # 正文检索不给排序参数时按发文时间返回："增值税"首条是《外交特权与
        # 豁免条例》（1986 年）。接口每条都带 score 字段并支持按它降序，
        # 加上后同一查询首条变成《土地增值税法》。
        sort_param = {"order": "-1", "sort": "score"}

    cache_key = _cache._key(
        "search", keyword, str(search_range), str(search_type),
        str(status), str(date_from), str(date_to), str(page), str(size), sort
    )
    cached = _cache.get(cache_key, max_age=CACHE_TTL)
    if cached:
        cached["_from_cache"] = True
        return cached

    payload = {
        "searchRange": search_range,
        "searchType": search_type,
        "searchContent": keyword,
        "pageNum": page,
        "pageSize": min(size, 100),
        "orderByParam": sort_param,
        "flfgCodeId": [],
        "zdjgCodeId": [],
        "sxx": sxx,
        "gbrq": gbrq,
        "sxrq": [],
        "gbrqYear": [],
        "xgzlSearch": False,
    }

    r = _request("POST", f"{BASE_URL}/law-search/search/list", json=payload)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError as e:
        # 限流时对方会回空正文或 HTML 拦截页，HTTP 仍是 200，直接 .json() 报的是
        # "Expecting value: line 1 column 1"，看不出是哪一层坏了。实测并发跑
        # 多个检索脚本时就会触发，所以把真实状态带出来。
        raise RuntimeError(
            f"NPC 返回的不是 JSON（HTTP {r.status_code}，"
            f"Content-Type {r.headers.get('Content-Type')}，"
            f"{len(r.content)} 字节）：{r.text[:120]!r}；"
            f"原异常 {e}。多半是并发请求被限流，间隔几秒重试。") from e
    outer = data.get("data", data)
    total = outer.get("total", 0)
    rows = outer.get("rows", outer.get("list", []))

    # Clean HTML tags from names (uses module-level _CLEAN_HTML_RE)
    def clean_html(s):
        return _CLEAN_HTML_RE.sub("", s) if s else ""

    results = []
    for item in rows:
        sxx_code = item.get("sxx", 0)
        results.append({
            "id": item.get("bbbs", ""),
            "title": clean_html(item.get("flfgname", item.get("title", ""))),
            "publish_date": item.get("gbrq", ""),
            "effective_date": item.get("sxrq", ""),
            "status_code": sxx_code,
            "status": SXX_MAP.get(sxx_code, f"未知({sxx_code})"),
            "issuing_authority": item.get("zdjgName", ""),
            "category": item.get("flxz", ""),
        })

    # 标题模糊检索按发布时间排序，不按相关度："企业所得税" 首条是企业破产法，
    # 目标法落在第 5 位。命中的都是"含检索词部分字"的法律，所以按检索词连续
    # 出现在标题中的位置重排一次，把完整命中的排到前面。相同档次内保持接口
    # 给的次序（Python 的 sort 稳定），不改动并列项的相对顺序。
    #
    # 重排只对取回的这一页有效，页外的条目无论多相关都排不进来。实测 16 个
    # 税种里有 7 个在 size<=3 时首位是错的（消费税返回消费者权益保护法、
    # 税收征管返回两高司法解释），所以向接口多要一些再排，排完再截回调用方
    # 要的条数。取不满时说明总数本来就不足 size，不补。
    # 翻页（page>1）不做过取：第 2 页的语义是接口原序的第 21 条起，掺入第 1 页
    # 的条目会让翻页结果失真。
    #
    # sort=date 单独处理：接口收到 orderByParam={order:-1,sort:gbrq} 也不真按发文
    # 时间排（实测标题检索"增值税"回 2024-12-25、2011-01-08、1994-02-22、
    # 2025-12-25……），不传排序参数时同样乱。所以时间序只能在本地对取回的窗口
    # 排一次；同时跳过按标题命中的相关度重排，那层重排会把时间序再次打散。
    if sort != "date" and search_type == 2 and scope == "fulltext" and page == 1:
        # 正文检索默认按发文时间排，"增值税" 首条是《外交特权与豁免条例》。
        # 接口其实算出了相关度（每条带 score 字段）并支持 sort=score 降序，
        # 加上后首条变成《土地增值税法》。这里再按"检索词整段出现在标题"重排
        # 一次：sort=score 是全文分词打分，"研发费用加计扣除" 会把命中"费用"
        # 的《诉讼费用交纳办法》顶到第一，而《企业所得税法》才真正规定加计扣除。
        scores = [src.get("score") or 0.0 for src in rows]
        parent_law = (resolve_tax_type(keyword) or {}).get("parent_law") or ""
        results = [it for _, it in sorted(
            zip(scores, results),
            key=lambda p: _fulltext_match_rank(p[1]["title"], keyword, parent_law, p[0]))]
        del results[size:]

    if search_type == 2 and scope == "title" and page == 1:
        fetch_size = min(max(size * 3, 20), 100)
        if fetch_size != size:
            extra = _request("POST", f"{BASE_URL}/law-search/search/list",
                             json={**payload, "pageSize": fetch_size})
            extra.raise_for_status()
            extra_outer = extra.json()
            extra_outer = extra_outer.get("data", extra_outer)
            extra_rows = extra_outer.get("rows", extra_outer.get("list", []))
            have = {r.get("bbbs", "") for r in rows}
            for item in extra_rows:
                bbbs = item.get("bbbs", "")
                if bbbs in have:
                    continue
                have.add(bbbs)
                results.append({
                    "id": bbbs,
                    "title": clean_html(item.get("flfgname", item.get("title", ""))),
                    "publish_date": item.get("gbrq", ""),
                    "effective_date": item.get("sxrq", ""),
                    "status_code": item.get("sxx", 0),
                    "status": SXX_MAP.get(item.get("sxx", 0), f"未知({item.get('sxx', 0)})"),
                    "issuing_authority": item.get("zdjgName", ""),
                    "category": item.get("flxz", ""),
                })
        tax_info = resolve_tax_type(keyword)
        parent_law = (tax_info or {}).get("parent_law") or ""
        if sort == "date":
            results.sort(key=lambda it: it.get("publish_date") or "", reverse=True)
        else:
            results.sort(key=lambda it: _title_match_rank(it["title"], keyword, parent_law))
        del results[size:]

    if sort == "date":
        # 标题模糊路径已在截断前排过（必须先排才不会被 size 截掉新文件），
        # 这里补精确检索与正文检索两条路径
        results.sort(key=lambda it: it.get("publish_date") or "", reverse=True)

    # NPC 精确检索一旦带上公布日期区间就会把检索词丢掉。2026-10-02 本机实测：
    # keyword="中华人民共和国增值税法" search_type=1 date_from="2026-01-01"
    # 标题检索回 88 条、正文检索回 1124 条，逐条核对没有一条标题含"增值税"，
    # 而同词不带日期是 2 条；换成短词"增值税法"配同一区间是 0 条——说明接口不是
    # 老老实实做 AND，而是检索词在区间里命中不到时退回"该区间的法律清单"。
    # 模糊检索不吃这一亏（带不带日期都按分词过滤），所以复核只针对精确这一路。
    date_widened_total = 0
    date_dropped = 0
    if search_type == 1 and (date_from or date_to):
        kept = [it for it in results if title_has_keyword(it.get("title", ""), keyword)]
        date_dropped = len(results) - len(kept)
        if date_dropped:
            results = kept
            date_widened_total = total
            total = len(results)

    result = {
        "keyword": keyword,
        "scope": scope,
        "search_type": "exact" if search_type == 1 else "fuzzy",
        "total": total,
        "page": page,
        "page_size": size,
        "results": results,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "_from_cache": False,
    }

    # 可靠性标记。NPC 正文检索确实是按词过滤的（"紫貂养殖" 2,415 条、"zzzqqq123"
    # 只有 7 条，两者条数不同）。真实缺陷不在"过不过滤"，在它默认按发文时间
    # 排序。加 sort=score 降序并按整段命中重排后可用于取依据，但命中的是
    # 全文分词，短词仍会被通用词带偏（"研发费用加计扣除"会带到《诉讼费用
    # 交纳办法》），所以标记为 medium 而不是 high：能用，但查具体条文要回到
    # 标题检索。
    if search_type == 2 and scope == "fulltext":
        result["_reliability"] = "medium"
        result["_reliability_note"] = RELIABILITY_NOTES["medium"]

    if date_dropped:
        result["source_total"] = date_widened_total
        result["_date_note"] = DATE_WIDENED_NOTE.format(
            dropped=date_dropped, kept=len(results),
            source_total=date_widened_total)

    _cache.set(cache_key, result)
    return result


# ── CLI ─────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Search China tax policies via NPC National Laws Database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tax_search.py "增值税" --size 20
  python tax_search.py "中华人民共和国增值税法" --exact
  python tax_search.py "企业所得税" --status 3 --from 2024-01-01 --sort date
  python tax_search.py "增值税" --status 3 --verbose
  python tax_search.py "增值税" --cache
  python tax_search.py --cache-clear
  python tax_search.py --cache-stats
        """
    )
    p.add_argument("keyword", nargs="?", help="Search keyword")
    p.add_argument("--scope", choices=["title", "fulltext"], default="title",
                   help="Search scope (default: title). fulltext does not filter "
                        "by the query and is only useful for confirming that")
    p.add_argument("--exact", action="store_true",
                   help="Exact title match (default: fuzzy)")
    p.add_argument("--status", type=int, default=3,
                   help="Status filter: 1=abolished, 2=amended, 3=effective, "
                        "4=pending. One value per run (this CLI cannot ask for "
                        "all statuses); omitted means 3.")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                   help="公布日期区间下界（gbrq）。只给这一端时上界按 2099-12-31 补")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD",
                   help="公布日期区间上界。只给这一端时下界按 0001-01-01 补——"
                        "不补就是空转，实测「增值税」×「止 2020-12-31」不补下界回 45 条、"
                        "与不带日期一模一样")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--size", type=int, default=20)
    p.add_argument("--sort", choices=["relevance", "date"], default="relevance")
    p.add_argument("--json", action="store_true", help="Output JSON")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p.add_argument("--cache", action="store_true",
                   help=f"Enable cache ({CACHE_TTL // 60}min TTL)")
    p.add_argument("--no-cache", action="store_true", help="Disable cache")
    p.add_argument("--cache-stats", action="store_true", help="Show cache stats")
    p.add_argument("--cache-clear", action="store_true", help="Clear cache")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    global _cache

    # Cache management
    if args.cache:
        _cache = CacheManager(enabled=True, namespace="search")
    elif args.no_cache:
        _cache = CacheManager(enabled=False, namespace="search")

    if args.cache_stats:
        s = _cache.stats()
        print(json.dumps({"cache": s}, ensure_ascii=False, indent=2))
        return
    if args.cache_clear:
        _cache.clear()
        print("Cache cleared.")
        return

    if not args.keyword:
        parser.print_help()
        return

    try:
        result = search_tax(
            args.keyword,
            scope=args.scope if not args.exact else "title",
            search_type=1 if args.exact else 2,
            status=args.status,
            date_from=args.date_from,
            date_to=args.date_to,
            page=args.page,
            size=args.size,
            sort=args.sort,
        )
    except ValueError as e:
        # 日期格式非法在发请求之前就报，别让它静默变成"这一维没生效"
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Human-readable output
    status_icon = {1: "🔴", 2: "🟡", 3: "🟢", 4: "🔵"}
    cache_tag = " [缓存]" if result.get("_from_cache") else ""
    # flk 的 total 随 pageSize／pageNum 换值，不能当命中总数报；只有日期复核那一路的
    # total 是自己数出来的条数，那种情况下才说「共」。
    total_tag = (f"共 {result['total']} 条" if result.get("_date_note")
                 else f"接口 total {result['total']} 条")
    print(f"🔍 搜索 \"{args.keyword}\" | {args.scope}/{result['search_type']} | "
          f"{total_tag} | {result['searched_at']}{cache_tag}")
    if result.get("_reliability"):
        print(f"  ⚠️ _reliability: {result['_reliability']} — "
              f"{result['_reliability_note']}")
    if result.get("_date_note"):
        print(f"  ⚠️ 日期区间: {result['_date_note']}")
        print(f"     接口原报 {result['source_total']} 条，复核后 {result['total']} 条")
    print()

    for item in result["results"]:
        icon = status_icon.get(item["status_code"], "❓")
        print(f"  {icon} [{item['status']}] {item['title']}")
        if args.verbose:
            print(f"     公布: {item['publish_date']}  施行: {item['effective_date']}")
            print(f"     发布机关: {item['issuing_authority']}")
            print(f"     分类: {item['category']}   ID: {item['id']}")
        else:
            print(f"     公布: {item['publish_date']}  ID: {item['id']}")
        print()

    if result["total"] > args.size:
        print(f"  📄 第 {args.page} 页，本屏 {len(result['results'])} 条；"
              f"可达页数不按接口 total 推算（它随 pageSize 与 pageNum 换值，"
              f"判据见 subskills/npc-flk/NOTE.md 第二节）")


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    main()
