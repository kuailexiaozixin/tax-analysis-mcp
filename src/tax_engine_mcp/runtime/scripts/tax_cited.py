# -*- coding: utf-8 -*-
"""文号 → 官方原文链接的持久缓存，与"是不是总局官方域"的判据。

两件事都放在这里，因为它们服务同一个动作——把已经核实过的原文链接沉淀下来，
下次按文号直接复用，不再重复检索、也不再让非官方来源混进来：

  - is_official(url)：只认 chinatax.gov.cn 及其任意子域（www./fgk./各省.），
    拒掉商业站与自媒体。这是全站通用判据，比 tax_fgk 里那条"URL 含
    fgk.chinatax.gov.cn"的分层标记宽——后者区分的是"是不是法规库条目"，
    不是"是不是官方来源"，两者不能互相替代。
  - cited_links.json：文号 → 已核实 URL。缓存的是"这份文件的原文在哪"，
    命中即零网络；只有官方域 URL 才允许入库。

纯字符串 + 本地文件读写，不联网、不发请求，可离线逐条钉住。缓存默认落在
~/.cache/tax-analysis-skill/cited_links.json，与仓库解耦（随人随机器长）。
"""

import json
import os
import urllib.parse
from pathlib import Path

OFFICIAL_HOSTS = ("chinatax.gov.cn",)
CACHE_DIR = Path(os.environ.get("TAX_CACHE_DIR",
                                str(Path.home() / ".cache" / "tax-analysis-skill")))
CITED_LINKS = CACHE_DIR / "cited_links.json"


def is_official(url: str) -> bool:
    """URL 主机是否为 chinatax.gov.cn 本身或其任一子域。

    用 urlparse 取 hostname 再比后缀，而不是在字符串里找 "chinatax.gov.cn"——
    否则 `http://evil/chinatax.gov.cn` 这种把官方域名塞进路径的假链接会被误判为真。
    端口与大小写由 hostname 归一化处理掉。
    """
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_cited_links(path: Path = None) -> dict:
    return _load(path or CITED_LINKS)


def get_cited_link(doc_number: str, path: Path = None) -> str:
    """按文号取已缓存的官方链接；取不到或缓存里的链接已不属官方域给空串。"""
    if not doc_number:
        return ""
    url = _load(path or CITED_LINKS).get(doc_number, "")
    return url if is_official(url) else ""


def put_cited_link(doc_number: str, url: str, path: Path = None) -> bool:
    """把一枚核实过的文号→官方链接写入缓存。非官方域一律拒收，返回 False。"""
    doc_number = (doc_number or "").strip()
    if not doc_number:
        return False
    if not is_official(url):
        return False
    target = path or CITED_LINKS
    cache = _load(target)
    cache[doc_number] = url.strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return True
