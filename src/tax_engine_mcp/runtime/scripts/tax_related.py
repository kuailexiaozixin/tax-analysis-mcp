#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法规的实务相关网页：税屋、微信公众号与搜狗全网三路合并。

Flask 的 /api/web-related/<bbbs_id> 与三个适配器的 web_related 共用这一份
组合逻辑。此前它长在 tax_server.py 里，适配器要用就得再抄一遍；两处各判一次
垃圾域名与来源标签，迟早会出现同一篇解读在网页上叫"实务解读"、在命令行里
叫"官方来源"。

三路的边界：
- 税屋（shui5.cn）与微信公众号走各自的检索模块（税屋：搜狗 site: 与站内原生；
  公众号：搜狗微信），不经过全网那一路；
- 全网那一路查搜狗手机入口（m.sogou.com）；Bing、百度与360搜索不作为通道；
- 官方来源标签只按 URL 判断。chinatax.gov.cn 里含 "tax"，不能拿 "tax" 当
  实务域名的判据，否则官方站点会被误标成实务解读。
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

from tax_shui5 import search_shui5
from tax_sogou import sogou_search
from tax_wechat import search_wechat

BLOCKED_DOMAINS = [
    "game", "4399", "7k7k", "xiaoyouxi", "youxi", "yxdown", "3dmgame",
    "gamersky", "duowan", "17173", "52pk", "sina.com.cn/game",
    "bilibili.com/video", "douyin.com", "kuaishou.com", "weibo.com",
    "zhihu.com/question", "tieba.baidu.com", "baike.baidu.com/item",
]
BLOCKED_TITLE_WORDS = [
    "游戏", "小游戏", "手游", "网游", "页游", "棋牌", "捕鱼",
    "娱乐", "八卦", "明星", "综艺", "电影", "电视剧",
    "体育", "彩票", "竞彩", "博彩",
    "小说", "漫画", "动漫",
]
TAX_PRACTICE_SOURCES = [
    {"name": "小颖言税", "query_hint": "小颖言税"},
    {"name": "税海涛声", "query_hint": "税海涛声"},
    {"name": "会计网",   "query_hint": "会计网"},
    {"name": "税小课",   "query_hint": "税小课服务"},
    {"name": "朴税",     "query_hint": "朴税"},
    # 这两个是实际会直连的源；不列进来，税屋和公众号的条目会被打成
    # "实务解读"以外的泛标签。
    {"name": "税屋",     "query_hint": "shui5.cn"},
    {"name": "微信公众号", "query_hint": "mp.weixin.qq.com"},
]
OFFICIAL_DOMAINS = [
    "chinatax.gov.cn", "mof.gov.cn", "gov.cn", "npc.gov.cn",
    "chinatax.cn", "chinatax.gov",
]
PRACTICE_DOMAIN_KEYWORDS = [
    "小颖言税", "税海涛声", "会计网", "税小课", "朴税",
    "mp.weixin.qq.com", "zhuanlan.zhihu.com", "toutiao.com",
    "shui5.cn", "shuiwu", "kuaiji", "chinaacc", "shuilishi", "shuikuai",
]


def is_garbage_result(url: str = "", title: str = "") -> bool:
    """明显与税务无关的娱乐、游戏、小说页面，不进相关网页清单。"""
    lower_url = url.lower()
    for kw in BLOCKED_DOMAINS:
        if kw in lower_url:
            return True
    lower_title = title.lower()
    for kw in BLOCKED_TITLE_WORDS:
        if kw in lower_title:
            return True
    return False


def is_practice_source(label_or_domain: str) -> bool:
    """判断一个标签或域名是不是实务解读源；官方站点优先排除。"""
    s = (label_or_domain or "").lower()
    for dom in OFFICIAL_DOMAINS:
        if dom in s:
            return False
    for kw in PRACTICE_DOMAIN_KEYWORDS:
        if kw.lower() in s:
            return True
    return False


def practice_source_name(domain: str, title: str) -> str:
    """在域名与标题里认已知实务源，认不出返回空串。"""
    combined = f"{domain} {title}".lower()
    for src in TAX_PRACTICE_SOURCES:
        if src["query_hint"] in combined:
            return src["name"]
    return ""


def search_practice_sources(query: str, n: int = 3) -> list[dict]:
    """税屋与微信公众号的实务解读；两源各取 n 条后按 URL 去重。"""
    results = []
    seen = set()

    for name, data in [("税屋", search_shui5(query, size=n)),
                       ("微信公众号", search_wechat(query, size=n))]:
        for item in (data or {}).get("results", []):
            href = item.get("url", "")
            if not href or href in seen or is_garbage_result(href, item.get("title", "")):
                continue
            seen.add(href)
            results.append({
                "title": item.get("title", ""),
                "url": href,
                "date": item.get("date", ""),
                "source": name,
                "source_label": name,
                "snippet": (item.get("snippet") or item.get("content", ""))[:180],
            })
    return results


def search_web_broad(query: str, n: int = 8) -> tuple[list, str]:
    """搜狗全网补充；返回 (结果, 拦截说明)。

    说明非空表示这一路根本没取到数据，调用方不能把 0 条当成"网上没有解读"。
    必须带着空说明返回：调用方按二元组解包，返回裸列表时解包会按列表长度
    成败——1 条与 3 条都抛 ValueError 被吞掉，正好 2 条时两个名字各接住一个
    dict，随后 list(dict) 拿到键名。
    """
    found = sogou_search(f"{query} 税收 政策解读", site="", size=n)
    if found.get("_error"):
        return [], found["_error"]

    results = []
    for item in found["results"]:
        href = item.get("url", "")
        title = item.get("title", "")
        if not href or len(title) < 8 or is_garbage_result(href, title):
            continue
        domain = re.search(r"https?://(?:www\.)?([^/]+)", href)
        domain_label = domain.group(1) if domain else ""
        results.append({
            "title": title,
            "url": href,
            "date": item.get("date", ""),
            "source": domain_label,
            "source_label": "官方来源" if ("chinatax" in href or "gov.cn" in href) else (
                practice_source_name(domain_label, title) or "实务解读"
            ),
            "snippet": item.get("snippet", ""),
        })
    return results, ""


def collect_related(law_title: str, keyword: str = "", *,
                    practice_n: int = 3, web_n: int = 8,
                    practice_fetch=None, web_fetch=None) -> dict:
    """按法规标题合并实务源与全网结果，返回可直接进 JSON 的载荷。

    practice_fetch / web_fetch 只给测试与 Flask 的现有打桩点留入口；不传时
    用本模块的两条真实检索。两路并发跑，任意一路抛异常都只让那一路上报，
    另一路照常返回。
    """
    fetch_practice = practice_fetch or search_practice_sources
    fetch_web = web_fetch or search_web_broad

    short_title = law_title.replace("中华人民共和国", "").strip()
    search_query = f"{short_title} {keyword}" if keyword else short_title

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_practice = pool.submit(fetch_practice, search_query, practice_n)
        future_web = pool.submit(fetch_web, search_query, web_n)
        try:
            web_items, web_error = future_web.result(timeout=20)
        except Exception:
            web_items, web_error = [], ""
        try:
            practice_items = future_practice.result(timeout=20)
        except Exception:
            practice_items = []

    sources = []
    seen = set()
    for item in list(practice_items) + list(web_items):
        if not item.get("url"):
            continue
        if item["url"] in seen or is_garbage_result(item["url"], item.get("title", "")):
            continue
        seen.add(item["url"])
        sources.append(item)

    payload = {
        "law_title": law_title,
        "keyword": keyword,
        "total": len(sources),
        "sources": sources,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if web_error:
        payload["engine_error"] = web_error
    return payload
