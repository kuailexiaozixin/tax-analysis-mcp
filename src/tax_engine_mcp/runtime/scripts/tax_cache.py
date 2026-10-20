#!/usr/bin/env python3
"""
共享的轻量 JSON 文件缓存。

本模块是缓存逻辑的**唯一实现**：`tax_search.py`（NPC 检索）、`tax_fgk.py`
（总局法规库清单）、`tax_detail.py`（法规详情元数据）都从这里导入。

默认关闭，开启后写入 ~/.cache/tax-analysis-skill，按 max_age(TTL, 秒) 失效。

命名空间：同一个缓存目录下会同时躺着检索清单、法规库清单、详情元数据。
三者用 namespace 隔离，靠写盘时记下的 `_ns` 字段区分——**不是**靠文件名或
子目录。原因是键算法是 `sha256(parts)[:16]`，路径改动会让既有缓存全部失联
（hash 一样但路径变了）；只加一个字段则路径不变、键不变，
既有缓存继续命中。

重要原则：只缓存"清单 / 元数据"，**绝不缓存正文**。税法条文必须每次现拉，
否则可能把已废止 / 被修订的旧条文当成现行有效引用——这对合规工具是不可接受的风险。
"""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional


class CacheManager:
    """Lightweight JSON file cache. Disabled by default.

    Args:
        enabled: 是否启用；关闭时 get 恒 miss、set 恒不写。
        namespace: 命名空间，划定 clear() / stats() 的作用范围，
            也记进条目里供日后核对来源。
    """

    def __init__(self, enabled: bool = False, namespace: str = "default"):
        self._enabled = enabled
        self.namespace = namespace
        self.dir = Path.home() / ".cache" / "tax-analysis-skill"

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _key(self, *parts: str) -> str:
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def _read(self, path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _belongs(self, data: Optional[dict]) -> bool:
        """条目是否属于本命名空间；没有 _ns 的老条目按"无主"放行，归属见 clear()。"""
        ns = (data or {}).get("_ns")
        return ns is None or ns == self.namespace

    def _claim(self, path: Path, data: dict) -> None:
        """给没有 `_ns` 字段的条目补上归属，让它从"无主"转正。

        只补这一个字段，**不动 `_cached_at` 也不动 payload**，所以 TTL 不会被
        重置、内容不会被改写。失败静默：缓存目录只读时也不该让读操作失败。
        """
        try:
            data["_ns"] = self.namespace
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def get(self, key: str, max_age: float = 300) -> Optional[dict]:
        if not self._enabled:
            return None
        p = self._path(key)
        if not p.exists():
            return None
        data = self._read(p)
        if not data:
            return None
        ns = data.get("_ns")
        if ns is not None and ns != self.namespace:
            return None
        if time.time() - data.get("_cached_at", 0) > max_age:
            return None
        if ns is None:
            self._claim(p, data)      # 老格式条目：认得下就顺手转正，其余不变
        return data.get("payload")

    def age(self, key: str) -> Optional[float]:
        """返回条目年龄（秒）；未启用、不存在或不属于本命名空间时返回 None。"""
        if not self._enabled:
            return None
        p = self._path(key)
        if not p.exists():
            return None
        data = self._read(p)
        if not data or not self._belongs(data):
            return None
        return time.time() - data.get("_cached_at", 0)

    def set(self, key: str, payload: dict) -> None:
        """写缓存。**失败一律静默**：缓存写不进去不该让"查政策"这件事失败。"""
        if not self._enabled:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        blob = json.dumps({"_cached_at": time.time(), "_ns": self.namespace,
                           "payload": payload}, ensure_ascii=False)
        # 原子写：先落临时文件再 os.replace。直接 write_text 时若进程中途被杀，
        # 会留下半截 JSON——get() 的 try/except 会把它当 miss，程序不会崩，
        # 但会白抓一次，而且现象是"缓存明明在却用不上"，很难查。
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.dir),
                                       prefix=f".{key}.", suffix=".tmp")
        except OSError:
            return                        # 目录不可写（例如只读挂载）就放弃
        done = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(blob)
            done = self._replace_into(tmp, self._path(key))
        finally:
            if not done:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @staticmethod
    def _replace_into(tmp: str, target: Path) -> bool:
        """os.replace，针对 Windows 重试几次。

        Windows 上若目标文件此刻被别的线程或进程打开（杀毒软件、索引服务也可能
        短暂持有句柄），os.replace 会抛 PermissionError——同键并发写时这是常态而
        非异常；而 POSIX 上 rename 是原子的、根本遇不到。重试用尽返回 False。
        """
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                return True
            except PermissionError:
                time.sleep(0.02 * (attempt + 1))
            except OSError:
                return False
        return False

    def _entries(self):
        """遍历缓存目录，产出 (path, data)。目录不存在时产出空。"""
        if not self.dir.exists():
            return
        for f in self.dir.glob("*.json"):
            yield f, self._read(f)

    def clear(self) -> int:
        """清掉**本命名空间**的条目，返回删除条数。

        无 `_ns` 的老条目（更早写下的缓存文件）一并清掉：它们的归属无法判定，
        留着只会让 stats() 数不干净。**不动**其它命名空间的条目——清的范围是
        本命名空间，`tax_search --cache-clear` 删不到 detail 的详情缓存；按目录
        清 `*.json` 就是越界。
        """
        removed = 0
        for f, data in list(self._entries()):
            if not self._belongs(data):
                continue
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def stats(self) -> dict:
        """统计本命名空间；顺带报出目录里的其它命名空间与无主条目。"""
        own = other = legacy = 0
        size = 0
        for f, data in list(self._entries()):
            try:
                sz = f.stat().st_size
            except OSError:
                sz = 0
            ns = (data or {}).get("_ns")
            if ns is None:
                legacy += 1
            elif ns == self.namespace:
                own += 1
                size += sz
            else:
                other += 1
        out = {"namespace": self.namespace, "entries": own,
               "size_kb": round(size / 1024, 1)}
        if other:
            out["other_namespaces"] = other
        if legacy:
            out["legacy_entries"] = legacy
        return out
