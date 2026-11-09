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
tax_shui5 / tax_sogou / tax_wechat / tax_web_search 各自维护 requests.Session
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

attempt() 管的是第三件事："失败要不要再发一次、发过几次要留痕"。它不自己发请求，
调用方把请求包成无参闭包交进来，因此上面那几个自管 Session 的模块也能只用它的
台账与重试，不必把各自的 Session 形状搬进这一层。TLS 熔断名单是进程内状态，
与串行闸的跨进程状态刻意分开：证书链坏了是本机这一趟的事，不该由一个锁文件
替所有进程决定。
"""

import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

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
        self.path = Path(path) if path else Path(tempfile.gettempdir()) / "tax-analysis-http.lock"
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



# ── 取数留痕、统一重试与 TLS 短路 ─────────────────────────────────────────
# 为什么要有台账：上一轮分析「碳排放权交易的财税处理」时，取数层既不计也不数，
# 成本归因只能靠人逐条点数工具回显（SSLError 8 次、5xx 与空响应 3 次、动态壳页
# 3 次）。四元组 `(host, 异常类名或 HTTP 状态码, 耗时 ms, 第几次尝试)` 就是补上
# 的那本账——谁在哪个站上花了几次、每次败在哪，随返回值一起交出去。
#
# 为什么要统一重试：同一件事（失败了再发一次）原先在三处各写一遍——
# tax_search 的 429/5xx/断连/挑战页四轮、tax_sogou 的三轮、tax_web_search 与
# tax_fgk 的"一次不通就放弃"。前两种各定一套退避，第三种直接把可恢复的瞬时失败
# 报成取不到。现在退避与次数在这一层定一份，站点自己的形状（闸、节流间隔、
# 挑战页判据）仍留在调用点，用 `retry` 与 `backoff` 两个参数接进来。
DEFAULT_ATTEMPTS = 3        # 同一件事最多发三次（首次 + 两次重试）
#: 线性退避的基数：第 i 次失败后等 `基数 × i` 秒。读环境变量是为了让离线门禁
#: 不必真等 4.5 秒（`tests/run_all.py` 给离线组置 0）；取用时读，用例里改了立刻生效。
BACKOFF_BASE = float(os.getenv("TAX_HTTP_BACKOFF", "1.5"))
TLS_STREAK_LIMIT = 2        # 同一 host 连续两次 TLS 握手失败即判该通道不可用

# 一次会话内的 TLS 连续失败计数与熔断名单。脚本是串行跑的一次任务，进程内状态
# 就等于"本会话"；跨进程不共享，因为跨进程的 TLS 失败是各进程自己的网络栈问题。
_tls_streak: dict = {}
_tls_blocked: set = set()
#: 取正文改有界并发后，几个线程会同时对同一 host 做 `_tls_streak[host]=get+1`
#: 这种读-改-写。无锁时几个线程都读到旧值、都写回 old+1，自增被吞（实测 64 线程
#: 只数到 61），连续失败数到不了阈值、熔断失灵。所有 _tls_streak/_tls_blocked
#: 的读写都经这把锁；锁只圈住状态变更，不圈 fn() 与 sleep()，并发取数不被串行化。
_tls_lock = threading.Lock()

#: 值得重发的 HTTP 状态码：429 是限流，5xx 在实测里多是反代抖动而非"这个资源没有"
RETRY_STATUS = (429, 500, 502, 503)

#: 本进程发过的每一次尝试，逐条累加，形状与 `note_attempt` 写进调用方台账的四元组
#: 完全一致。调用方的 `_attempts` 只覆盖自己那一次取数，而一次任务要打好几个站、
#: 有的取数函数（NPC 检索、搜狗微信）的返回形状轮不到这一层插字段——那些尝试
#: 照样得留下账。会话级台账就是它们的落点：`ledger_size` 取快照、`ledger_from`
#: 截出某一段，取数函数只多两行就能把"这一段发过几次请求"随返回值交出去。
SESSION_LEDGER: list = []


def new_log() -> list:
    """建一份空台账。调用方把它传给 `attempt`，取数结束后原样放进返回值。"""
    return []


def note_attempt(log: list, url: str, outcome: str, elapsed_ms: float, nth: int) -> list:
    """记一次尝试：`(host, 异常类名或 HTTP 状态码, 耗时 ms, 第几次尝试)`。

    同一条账同时记进会话级 `SESSION_LEDGER`，除调用方把 `SESSION_LEDGER` 自己
    当台账传入的情况——那时只记一次，否则整本账每条翻倍。
    """
    entry = (host_of(url), str(outcome), int(elapsed_ms), int(nth))
    log.append(entry)
    if log is not SESSION_LEDGER:
        SESSION_LEDGER.append(entry)
    return log


def ledger_size() -> int:
    """会话级台账当前的条数，取数开始前拍一张快照用。"""
    return len(SESSION_LEDGER)


def ledger_from(mark: int) -> list:
    """截出快照之后的那一段台账，用作一次取数自己的 `_attempts`。"""
    return list(SESSION_LEDGER[max(0, int(mark)):])


def reset_ledger() -> None:
    """清空调用方台账以外的会话级账（用例复位用）。"""
    SESSION_LEDGER.clear()


def host_of(url: str) -> str:
    """取 URL 的 host；解析不出来时退回原串，台账里不留空字段。"""
    return urlparse(url or "").netloc or (url or "")


def reset_tls_state() -> None:
    """清空 TLS 计数与熔断名单（用例复位用；新一轮任务不必调，进程本就新建）。"""
    with _tls_lock:
        _tls_streak.clear()
        _tls_blocked.clear()


def tls_blocked(url_or_host: str) -> bool:
    """这一地址本会话是否已判定不可用。"""
    with _tls_lock:
        return host_of(url_or_host) in _tls_blocked


def _tls_snapshot(host: str) -> tuple:
    """读该 host 的 (是否已熔断, 连续失败计数)，供短路分支拼返回值。

    计数取不到时退回 TLS_STREAK_LIMIT，与原先 `_tls_streak.get(host, LIMIT)` 一致。
    """
    with _tls_lock:
        return (host in _tls_blocked, _tls_streak.get(host, TLS_STREAK_LIMIT))


def _tls_record_failure(host: str) -> bool:
    """记一次 TLS 握手失败，返回本会话是否就此判定该 host 不可用。

    自增、阈值判定、写熔断名单在同一把锁里完成：并发取正文时多线同撞一个 host，
    读-改-写不加锁会丢自增（实测 64 线程只数到 61），阈值也就判不准。
    """
    with _tls_lock:
        streak = _tls_streak.get(host, 0) + 1
        _tls_streak[host] = streak
        if streak >= TLS_STREAK_LIMIT:
            _tls_blocked.add(host)
            return True
        return False


def _tls_clear(host: str) -> None:
    """连上了（或这次失败不在 TLS 层）就把该 host 的连续计数清零。"""
    with _tls_lock:
        _tls_streak[host] = 0


def is_tls_failure(exc: BaseException) -> bool:
    """这次失败是不是 TLS 握手层面的。

    实测两种形态：直接抛 `requests.exceptions.SSLError`，以及 urllib3 把 SSLError
    嵌在 `ConnectionError` 的原因里（错误文本那句 "(Caused by SSLError(...))"）。
    只看最外层类名会漏掉后一种，而漏掉就意味着这条短路在真实故障下从不生效。
    """
    seen, stack = set(), [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if (isinstance(cur, requests.exceptions.SSLError)
                or type(cur).__name__.startswith("SSL")):
            return True
        stack.append(cur.__cause__ or cur.__context__)
        stack.extend(a for a in getattr(cur, "args", ()) if isinstance(a, BaseException))
    return False


def _retryable_status(value) -> str:
    """默认的返回值复判：只看 HTTP 状态码，命中 `RETRY_STATUS` 就要重发。"""
    code = getattr(value, "status_code", None)
    return f"HTTP {code}" if code in RETRY_STATUS else ""


def _wait_seconds(backoff, nth: int, value) -> float:
    """退避时长：数字按 `基数 × 第几次` 线性算，函数交给调用方定（NPC 用 2 的幂）。

    None 表示用模块常量 `BACKOFF_BASE`，且在取用时才读——离线门禁把它设成 0，
    用例就不会为一次打桩的失败真等一秒半。
    """
    if callable(backoff):
        return float(backoff(nth, value))
    base = BACKOFF_BASE if backoff is None else float(backoff)
    return base * nth


def attempt(fn, url: str, log: list, *, attempts: int = DEFAULT_ATTEMPTS,
            backoff=None, sleep=time.sleep, retry=None) -> tuple[object, str, bool]:
    """发一次取数，失败就退避重试，每次尝试都记进 `log`。

    Args:
        fn: 无参可调用对象，请求参数由调用方包在闭包里（闸、节流、站点特有的
            请求形状都留在调用点，这一层只管"再发一次要不要等、等到几次算败"）。
        url: 目标地址，只用来取 host 与写台账，本层不碰它。
        log: `new_log()` 建的台账列表，原地追加。
        attempts: 最多发几次；默认 3 次是登记的口径（任何异常都重试到这一数）。
        backoff: 退避时长。数字表示"基数 × 第几次"（默认 1.5×i 秒）；函数则收
            `(第几次, 末次返回值)` 自己算——NPC 的挑战页实测要 2/4/8 秒，就用函数。
        sleep: 等待函数，用例打桩用；不打桩时一次失败真的等 1.5 秒。
        retry: 返回值复判函数 `retry(值) -> str`，给出非空说明就重发；默认只看
            状态码（429 与 5xx，即原先写在 tax_search 里的那一档）。

    Returns:
        `(值, 失败说明, 本会话是否已判定该通道不可用)`。
        成功时后两项是空串与 False。失败说明一定带着"试了几次"与末次原因
        （`4 次尝试仍失败：SSLError：…`），让"试了几次都不通"与"一次不通"在
        返回值上就能分开读；末次是状态码问题时值仍带回最后那个响应，
        调用方要看正文或看状态码都拿得到。

    TLS 短路：同一 host 连续 `TLS_STREAK_LIMIT` 次握手失败后，本会话内对它的
    请求不再上网，直接回"该通道本会话已判定不可用"。判据是 `is_tls_failure`，
    中间夹着一次非 TLS 的失败就重新计数。
    """
    host = host_of(url)
    blocked, streak = _tls_snapshot(host)
    if blocked:
        note_attempt(log, url, "TLS 熔断：本会话未再发请求", 0, streak + 1)
        return (None, f"该通道本会话已判定不可用（连续 {streak} 次 TLS 握手失败）", True)

    check = retry or _retryable_status
    limit = max(1, int(attempts))
    last_value, last_err = None, ""
    for nth in range(1, limit + 1):
        started = time.monotonic()
        try:
            value = fn()
        except Exception as e:                  # 任何异常都算一次尝试失败
            note_attempt(log, url, type(e).__name__, (time.monotonic() - started) * 1000, nth)
            last_value, last_err = None, short_reason(e)
            if is_tls_failure(e):
                if _tls_record_failure(host):
                    return (None, f"该通道本会话已判定不可用（连续 {TLS_STREAK_LIMIT} 次 "
                                  f"TLS 握手失败，末次：{last_err}）", True)
            else:
                _tls_clear(host)
            if nth < limit:
                sleep(_wait_seconds(backoff, nth, None))
            continue

        note_attempt(log, url, getattr(value, "status_code", "OK") or "OK",
                     (time.monotonic() - started) * 1000, nth)
        last_value = value
        reason = check(value)
        if not reason:
            _tls_clear(host)
            return (value, "", False)
        last_err = reason
        _tls_clear(host)                        # 连上了就不是 TLS 问题
        if nth < limit:
            sleep(_wait_seconds(backoff, nth, value))

    blocked, _ = _tls_snapshot(host)
    if blocked:
        return (last_value, f"该通道本会话已判定不可用（连续 {TLS_STREAK_LIMIT} 次 "
                            f"TLS 握手失败）", True)
    return (last_value, f"{limit} 次尝试仍失败：{last_err}", False)


def failure_note(err: str, log: list) -> str:
    """把失败说明与台账拼成一句给 `_error`/`_fetch_failed` 用的话。

    台账只在末次原因之外补一句"共发 N 次"，让读的人一眼看出重试确实跑过；
    `attempt` 给不出说明时（调用方自己判的失败）也照样把次数带上。
    """
    if not log:
        return err
    head = f"（共发 {len(log)} 次请求）"
    return f"{err}{head}" if err else head


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
