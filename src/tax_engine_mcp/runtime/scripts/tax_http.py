#!/usr/bin/env python3
"""统一 HTTP 请求层 —— 本模块是 scripts/ 下裸 requests 调用的唯一出口。

为什么要有这一层
----------------
同一件事（发一个 GET）如果在三个模块里各写一遍：

    tax_detail.py   _request / get_download_url / download_file   3 处
    tax_fgk.py      fetch_fgk_body                                1 处
    tax_search.py   _request                                      1 处

于是 UA、SSL 校验开关、超时值在三处各定一份，改一处容易漏掉另外两处。
收成一个出口之后，"项目里还有谁在发请求、以什么姿势发"能被一眼看全——
想给所有税务站点统一加一个请求头，只需要动这里。

verify 为什么是必填参数
-----------------------
这三个模块的 SSL 策略并不一致：

    tax_fgk.py     走 requests 的默认值 verify=True
    tax_detail.py  走 VERIFY_SSL（默认 False）
    tax_search.py  走 VERIFY_SSL（默认 False）

如果本层给 verify 一个默认值，就等于悄悄改掉其中一方的线上行为。
设成必填，这个差异会留在调用点上、看得见；将来要统一是另一件独立的事。

不进这一层的模块
----------------
tax_shui5 / tax_so360 / tax_wechat / tax_web_search 各自维护 requests.Session
（WAF 挑战应对、cookie 恢复、页面二次跳转），请求形状与这里不同。
强行合并只会把它们的特例塞进通用层，所以它们保持自管。

串行闸为什么也放这里
--------------------
"同一时刻只让一个进程打某个站"这件事有两个站要用：NPC（检索 + 详情同上限流）
与搜狗微信（并发加压触发反爬）。闸的实现在下面只写一份，两个站各配一个锁文件，
免得同一段文件锁逻辑抄两遍、只改一处。

上面那条边界只管"请求怎么发"，不管"失败怎么说"。short_reason() 不碰 requests，
只把一个异常压成给用户看的一句话，所以上面那几个自管 Session 的客户端和
tax_fgk 也共用它——否则同一段 urllib3 内部结构要在三处各解释一遍，
界面与命令行各说各话。
"""

import os
import re
import tempfile
import threading
import time
from pathlib import Path

import requests

# SSL 校验总开关，默认关：NPC 法规库与总局法规库在本机的证书链不完整，
# 打开会直接连不上，需要时用 TAX_SEARCH_VERIFY_SSL=1 打开。
#
# 注意：这个常量是给调用方取用的"默认值来源"，不是本层的隐式默认——
# 每个调用点仍必须把 verify 显式传进来。
VERIFY_SSL = os.getenv("TAX_SEARCH_VERIFY_SSL", "0") == "1"

try:
    import msvcrt                       # Windows
except ImportError:                     # pragma: no cover
    msvcrt = None
try:
    import fcntl                        # Linux / macOS
except ImportError:                     # pragma: no cover
    fcntl = None


class SerialGate:
    """跨进程串行闸：同一时刻只允许一个进程进入临界区。

        with gate:
            do_request()

    两层锁缺一不可：
      - 进程内的 threading.Lock —— Windows 的文件锁按"进程 + 区域"算，同一
        进程里第二次加锁会直接失败（不像 flock 可重入），所以多线程必须先
        在进程内排队；
      - 跨进程的文件锁 —— 用 msvcrt（Windows）或 fcntl（类 Unix），不引依赖。

    两者都不可用时退化成不加锁——保护强度降了，但不引入新的故障。
    """

    #: 超时提示里替换成调用方自己的站点名，好让报错说清是哪个站在等
    site = "该站"

    def __init__(self, path: Path = None, timeout: float = 180.0):
        self.path = Path(path) if path else Path(tempfile.gettempdir()) / "tax-analysis.lock"
        self.timeout = float(timeout)
        self._fh = None
        self._thread_lock = threading.Lock()

    def _try_lock(self):
        if msvcrt is not None:
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self):
        try:
            if msvcrt is not None:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def __enter__(self):
        if msvcrt is None and fcntl is None:
            return self                     # 裸平台：不加锁，也不报错
        self._thread_lock.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a+b")
            if self.path.stat().st_size == 0:   # 要锁 1 字节，文件先得有那 1 字节
                self._fh.write(b"\0")
                self._fh.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._try_lock()
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"等待{self.site}串行闸超过 {self.timeout:.0f} 秒——"
                            f"说明另有进程正在打{self.site}。并发会被限流"
                            f"（且不一定回 429），请等它跑完，或调大对应的"
                            f"TAX_*_LOCK_TIMEOUT 环境变量。")
                    time.sleep(0.2)
        except BaseException:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            self._thread_lock.release()
            raise

    def __exit__(self, *exc):
        if self._fh is not None:
            self._unlock()
            self._fh.close()
            self._fh = None
        self._thread_lock.release()
        return False



def request(method: str, url: str, *, headers: dict, timeout: float,
            verify: bool, **kwargs) -> requests.Response:
    """发一个请求。

    参数一律显式传递，本层不做隐式补全，也不改返回值，好让调用点的行为
    与直接调 requests 逐字等价。kwargs 原样透传给 requests
    （params / data / json / allow_redirects / stream 等）。
    """
    return requests.request(method, url, headers=headers, timeout=timeout,
                            verify=verify, **kwargs)


def get(url: str, *, headers: dict, timeout: float, verify: bool,
        **kwargs) -> requests.Response:
    """GET 的简写，参数含义与 request() 完全一致。

    内部走 requests.get，而不是 request("GET", ...)。两者在 requests 里本就
    等价，但 tax_detail / tax_fgk 原来的调用点用的就是 requests.get，离线用例
    里对 requests.get 打的桩因此不必跟着改（改了桩点等于把测试和实现绑死）。
    """
    return requests.get(url, headers=headers, timeout=timeout,
                        verify=verify, **kwargs)


_LINK_RE = re.compile(r"(?:https?://|url:\s*)\S+")


def short_reason(exc: Exception, limit: int = 90) -> str:
    """把一个 requests 异常压成能给用户看的一句话。

    str(exc) 给的是 urllib3 的内部结构：连接池、端口、参数编码之后的整条查询串。
    实测一次域名解析失败就有三百多字。这一串落在 `_error` 上，命令行按错误行打印，
    界面把它原样接进 `engine_error` 显示在结果头部（`tax_server.api_web_related`），
    行被撑满之后真正的原因——超时、连不上、被反复重定向到验证页——反而读不出来。
    所以先抹掉链接与参数串（host 留在消息里，那是有用的信息），折行压平成一行，
    丢掉 urllib3 追加的 "(Caused by ...)" 嵌套原因，按空格边界截断，末尾带上异常
    类名，让读的人有词可查。
    """
    text = _LINK_RE.sub("[链接已省略]", " ".join(str(exc).split()))
    text = text.split(" (Caused by")[0]
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0]      # 不在词中间切断
    return f"{type(exc).__name__}：{text}"
