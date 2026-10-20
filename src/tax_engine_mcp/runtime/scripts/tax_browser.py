#!/usr/bin/env python3
"""
真浏览器会话 — 用来过前端 JS 挑战型 WAF，之后把 cookie 转交普通 HTTP 客户端。

为什么需要它：税屋前置阿里云 WAF，纯 HTTP 请求无论怎么按公开算法算
acw_sc__v2 都会被弹回同一份挑战页（2026-09-27 实测 5/5 失败）。挑战链
里有浏览器特有的行为特征，纯 HTTP 客户端补不出来。带引擎的浏览器
一次就过，之后 cookie 可直接转给 requests，逐篇读正文从 3~6 秒降到 0.4~1.2 秒。

用哪个浏览器：先探测本机已安装的浏览器可执行文件（Edge、Chrome、Brave、
360、Firefox 等常见安装路径），找到就直接用 Playwright 的
executable_path 驱动，一个都不装。Playwright 自带内核（channel 方式）
只作兜底，且仅在本机确实有对应浏览器时才会成功。

为什么单独成模块：会 WAF 的源不止税屋，会话启动、cookie 转交、失败诊断
三件事也只有一处需要写。放一个模块里，其余源 import 即可。

Usage:
  python tax_browser.py "https://www.shui5.cn/article/90/40872.html"
  python tax_browser.py --check          # 只报本机浏览器探测结果，不抓页
"""

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
VIEWPORT = {"width": 1440, "height": 900}
# 挑战页放行后正文由前端异步插入，先等挑战链结束再等正文出现
WAF_SETTLE_MS = 2500
BODY_WAIT_MS = 4000

# 本机已装浏览器的常见安装位置。子路径是相对各个 Program Files 根的，
# 逐个 exists 探测，命中即用，不下载任何内核。顺序按"过 WAF 成功率 +
# 装机率"排：Chromium 内核的浏览器都具备执行挑战链所需的 JS 特性，
# Firefox 内核也可以，但排在后面。
_BROWSER_CANDIDATES = (
    ("edge", os.path.join("Microsoft", "Edge", "Application", "msedge.exe")),
    ("chrome", os.path.join("Google", "Chrome", "Application", "chrome.exe")),
    ("brave", os.path.join("BraveSoftware", "Brave-Browser",
                           "Application", "brave.exe")),
    ("360se", os.path.join("360", "360Chrome", "Chrome", "360chrome.exe")),
    ("firefox", os.path.join("Mozilla Firefox", "firefox.exe")),
)
# 根目录去重后的探测列表：ProgramFiles 与 ProgramFiles(x86) 指向同一处时要合并
_PROGRAM_ROOTS = (os.environ.get("ProgramFiles", r"C:\Program Files"),
                  os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                  os.environ.get("LOCALAPPDATA", ""))


def _root_list() -> list:
    """Program Files 根去重（64 位机上两个环境变量可能是同一目录）。"""
    out, seen = [], set()
    for root in _PROGRAM_ROOTS:
        if not root:
            continue
        key = os.path.normcase(os.path.normpath(root))
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def find_installed_browsers() -> list:
    """探测本机已安装的浏览器，按优先级返回 [{name, path, engine}]。

    只查文件是否存在，不启动、不下载。返回空列表表示一个都没装，
    这时再由调用方决定是否回落到 Playwright 自带通道。

    Returns:
        [{"name": "edge", "path": "C:...\\msedge.exe", "engine": "chromium"}, ...]
    """
    found = []
    seen = set()
    for name, rel in _BROWSER_CANDIDATES:
        for root in _root_list():
            path = os.path.join(root, rel)
            key = os.path.normcase(os.path.abspath(path))
            if key in seen or not os.path.isfile(path):
                continue
            seen.add(key)
            found.append({
                "name": name,
                "path": path,
                "engine": "firefox" if name == "firefox" else "chromium",
            })
    return found


def _launch(p, browser: dict):
    """按探测到的浏览器信息启动，失败时原样抛出交给上层换下一个。"""
    args = ["--disable-blink-features=AutomationControlled"]
    if browser["engine"] == "firefox":
        return p.firefox.launch(headless=True, executable_path=browser["path"])
    return p.chromium.launch(headless=True, executable_path=browser["path"],
                             args=args)


def probe_browsers() -> dict:
    """逐个试启动已装浏览器，报告可用性与失败原因，供自检和诊断用。

    Returns:
        {"已装": [{"name","path","ok","error"?}], "playwright": {...}}
    """
    from playwright.sync_api import sync_playwright

    installed = find_installed_browsers()
    out = {"已装": [], "playwright": {}}
    with sync_playwright() as p:
        for b in installed:
            row = {"name": b["name"], "path": b["path"], "ok": False}
            try:
                br = _launch(p, b)
                br.close()
                row["ok"] = True
            except Exception as e:                      # 装残了/版本不匹配
                row["error"] = str(e).splitlines()[0][:180]
            out["已装"].append(row)
    return out


class BrowserSession:
    """一次启动、连续复用：先让浏览器过一次 WAF，再把 cookie 交给 requests。

    典型用法是 warm() 一次，然后 read_html() 连抓多篇。

    Attributes:
        channel: 实际用上的浏览器通道名
    """

    def __init__(self, warm_url: str = ""):
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self.channel = ""
        self.warm_url = warm_url
        self.warmed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _ensure(self):
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        errors = []
        for b in find_installed_browsers():
            try:
                self._browser = _launch(self._pw, b)
                self.channel = b["name"]
                break
            except Exception as e:
                errors.append(f"{b['name']}: {str(e).splitlines()[0][:90]}")
        if self._browser is None:
            self._pw.stop()
            raise RuntimeError(
                "本机没有可启动的浏览器（探测路径均不存在或启动失败）— "
                + " / ".join(errors or ["未装 Edge/Chrome/Firefox 等常见浏览器"]))
        self._ctx = self._browser.new_context(user_agent=UA, locale="zh-CN",
                                              viewport=VIEWPORT)
        self._page = self._ctx.new_page()

    def warm(self) -> bool:
        """访问 warm_url 让浏览器跑完 WAF 挑战链，把放行 cookie 收下来。

        Returns:
            True 表示已过挑战；False 表示页面无需挑战或挑战没通过。
        """
        self._ensure()
        if not self.warm_url:
            return False
        try:
            self._page.goto(self.warm_url, wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(WAF_SETTLE_MS)
        except Exception as e:
            raise RuntimeError(f"预热访问失败：{str(e).splitlines()[0][:150]}")
        self.warmed = True
        return not self._is_challenge()

    def read_html(self, url: str) -> str:
        """用浏览器打开一页并返回最终 HTML。WAF 已在 warm() 里过掉。

        万一 cookie 失效，这里会自行再过一次挑战再重试一次。
        """
        self._ensure()
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_timeout(BODY_WAIT_MS)
        if self._is_challenge():
            self._page.reload(wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(WAF_SETTLE_MS)
        return self._page.content()

    def cookies_for(self, domain: str) -> dict:
        """把浏览器里属于该域的 cookie 导成 {name: value}。

        Args:
            domain: 目标域名，如 shui5.cn。
        """
        self._ensure()
        needle = domain.replace("www.", "").lower()
        out = {}
        for c in self._ctx.cookies():
            if needle in (c.get("domain") or "").replace("www.", "").lower():
                out[c["name"]] = c["value"]
        return out

    def _is_challenge(self) -> bool:
        html = self._page.content()
        return "renderData" in html and "arg1" in html

    def close(self):
        for obj in (self._ctx, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None


def main():
    ap = argparse.ArgumentParser(
        description="真浏览器会话：过一次 WAF，再把 cookie 转交 requests",
        epilog="省略 url 等于只做浏览器探测（同 --check）。")
    ap.add_argument("url", nargs="?", help="要抓的页面地址；省略则只探测本机浏览器")
    ap.add_argument("--check", action="store_true",
                    help="只报本机浏览器探测结果，不抓页")
    ns = ap.parse_args()

    if ns.check or not ns.url:
        info = probe_browsers()
        print("本机已装浏览器探测：")
        for row in info["已装"]:
            print(f"  {row['name']:<10s} {'可启动' if row['ok'] else '启动失败'}"
                  f"  {row['path']}"
                  + ("" if row["ok"] else f"\n{'':14s}{row['error']}"))
        if not info["已装"]:
            print("  未装任何常见浏览器（Edge/Chrome/Brave/360/Firefox）")
        if ns.url:
            print()
    if not ns.url:
        return
    url = ns.url
    with BrowserSession(warm_url=url) as s:
        html = s.read_html(url)
        body = ""
        try:
            body = s._page.inner_text("body")            # noqa: SLF001
        except Exception:
            pass
        print(f"浏览器 {s.channel} | HTML {len(html)} 字符"
              f" | 挑战页 {'是' if s._is_challenge() else '否'}")
        print(f"标题 {s._page.title()}")                # noqa: SLF001
        if body:
            print(f"正文节选：{body[:200].replace(chr(10), ' | ')}")


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    main()
