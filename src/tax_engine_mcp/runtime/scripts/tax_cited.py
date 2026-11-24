# -*- coding: utf-8 -*-
"""文号 → 官方原文链接的持久缓存，与"是不是总局官方域"的判据。

两件事都放在这里，因为它们服务同一个动作——把已经核实过的原文链接沉淀下来，
下次按文号直接复用，不再重复检索、也不再让非官方来源混进来：

  - is_official(url)：只认 chinatax.gov.cn 及其任意子域（www./fgk./各省.），
    拒掉商业站与自媒体。这是全站通用判据，比 tax_fgk 里那条"URL 含
    fgk.chinatax.gov.cn"的分层标记宽——后者区分的是"是不是法规库条目"，
    不是"是不是官方来源"，两者不能互相替代。
  - cited_links.json：文号 → 已核实的原文地址。缓存的是"这份文件的原文在哪"，
    命中即零网络；只有官方域 URL 才允许入库。

值形状（3.62.20 起）是 `{"url": …, "title": …}`：**光有地址不够，还得记住这条
地址当初是跟着哪个标题核实下来的**。按文号取缓存时，读侧要拿存着的标题与用户
这一轮点名的标题现判同一性——同一个文号能从两类标题里抽出来（被点名的那份，
和"在标题里引用了被点名那份"的另一份，实测引用式标题的第一个文号就是别人的），
只比文号等于让上一轮的命中冒充这一轮的目标。`title` 为空的旧条目（本版本之前
写下的纯字符串值）判不了，一律不采信，照常检索后带标题重写。

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


def _norm_entry(value) -> dict:
    """把盘上的一条值折成 {"url","title"}；认不出的形状给空条目。

    兼容三种来路：3.62.20 起的字典值；更早版本写的裸字符串（那时没有标题，
    title 只能给空）；以及手工改坏的其它类型（当成空条目，绝不抛）。
    """
    if isinstance(value, dict):
        return {"url": str(value.get("url") or ""),
                "title": str(value.get("title") or "")}
    if isinstance(value, str):
        return {"url": value, "title": ""}
    return {"url": "", "title": ""}


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
    """按文号取已缓存的官方链接；取不到或缓存里的链接已不属官方域给空串。

    这一枚只管"地址在不在、还是不是官方域"，不判"这条地址是不是你要的那份
    文件"——它服务的是补链接的工作清单与偏好项（有地址就有价值）。定位点名
    文件要走 get_cited_entry，那里才核标题。
    """
    if not doc_number:
        return ""
    url = _norm_entry(_load(path or CITED_LINKS).get(doc_number, "")).get("url", "")
    return url if is_official(url) else ""


def get_cited_entry(doc_number: str, path: Path = None) -> dict:
    """按文号取缓存条目 `{"url","title"}`；取不到或非官方域给 `{"",""}`。

    title 是这条地址当初核实下来时对应的文件标题，可能是空串（本版本之前写的
    条目没有这一项）。调用方必须把空 title 当"判不了同一性"，不能当"同一"。
    """
    if not doc_number:
        return {"url": "", "title": ""}
    entry = _norm_entry(_load(path or CITED_LINKS).get(doc_number, ""))
    if not is_official(entry["url"]):
        return {"url": "", "title": ""}
    return entry


def put_cited_link(doc_number: str, url: str, path: Path = None,
                   title: str = "") -> bool:
    """把一枚核实过的文号→官方链接写入缓存。非官方域一律拒收，返回 False。

    title 带上这条地址对应的文件标题，读侧才判得出"这一轮的点名是不是同一份"
    （见 get_cited_entry）。拿不到标题时宁可存空：空标题的条目在定位路上不被
    采信，等于没写，总好过存一枚下次谁也不敢信的地址。
    """
    doc_number = (doc_number or "").strip()
    if not doc_number:
        return False
    if not is_official(url):
        return False
    target = path or CITED_LINKS
    cache = _load(target)
    cache[doc_number] = {"url": url.strip(), "title": (title or "").strip()}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return True
