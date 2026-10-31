# -*- coding: utf-8 -*-
"""模型清单缓存 —— 让「选模型」不必每次先点一遍「测试连接」。

【它解决什么】`GET /models` 是网络请求，而用户打开对话框只是想**看一眼**有哪些模型。
        每次都先点「测试连接」再点下拉，等于把一次只读操作变成两次点击加一次等待。
        把上次拿到的清单存下来，打开就能选；后台刷新是锦上添花，不影响可用性。

【两条纪律】

  ① **缓存是"上次见到的"，不是"现在一定有的"** —— 所以界面上都要保留手输，
     并且明确允许用户填不在缓存里的名字（模型下线/新上线都不会提前通知我们）。
  ② **过期不删** —— 拿不到新清单时，旧清单仍然比空列表有用。
     真正的失效由"刷新失败"的提示来承担，而不是把缓存清空。

【存在哪】`app_data_dir()/model_cache.json`，按 `命名空间/服务商` 分组。
    命名空间只有两个：`llm` 与 `embedding` —— 两边的服务商可能同名
    （比如都叫 openai），不分开会互相覆盖。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger("ModelCache")

#: 超过这个时长就当"旧的"，界面可以提示刷新（但**仍然展示**）
DEFAULT_TTL_S = 24 * 3600


def cache_path() -> Path:
    """缓存文件路径（惰性取 `app_data_dir()`，避免 import 期触碰配置）。"""
    from config import app_data_dir
    return Path(app_data_dir()) / "model_cache.json"


def _load() -> dict:
    """读整个文件。**任何异常都降级为空**（缓存坏了不该影响选模型）。"""
    try:
        p = cache_path()
        if not p.is_file():
            return {}
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                         # noqa: BLE001
        return {}


def _save(payload: dict) -> None:
    """原子写（临时文件 + `os.replace`）。失败只记日志 —— 缓存写不进去不该报错。"""
    p = cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".models_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError as e:
        log.warning("模型清单缓存写不进去：%s", e)


def _bucket(namespace: str) -> dict:
    got = _load().get(str(namespace or "").strip()) or {}
    return got if isinstance(got, dict) else {}


def get(namespace: str, name: str) -> list[str]:
    """取上次缓存的模型清单。没有就返回空列表（不是 None，调用方不必判空）。"""
    entry = _bucket(namespace).get(str(name or ""))
    ids = (entry or {}).get("ids") if isinstance(entry, dict) else None
    if not isinstance(ids, list):
        return []
    return [str(i) for i in ids if i]


def fetched_at(namespace: str, name: str) -> float:
    """上次拉取的时间戳（0 = 从没拉过）。"""
    entry = _bucket(namespace).get(str(name or ""))
    ts = (entry or {}).get("ts") if isinstance(entry, dict) else None
    try:
        return float(ts or 0)
    except (TypeError, ValueError):
        return 0.0


def is_stale(namespace: str, name: str, *, ttl_s: float = DEFAULT_TTL_S,
             now: float | None = None) -> bool:
    """缓存是否该刷新了。**没缓存也算 stale**（要拉一次）。"""
    ts = fetched_at(namespace, name)
    if ts <= 0:
        return True
    return ((now if now is not None else time.time()) - ts) > ttl_s


def put(namespace: str, name: str, ids: list[str]) -> None:
    """存一份清单。**空清单不覆盖** —— 拉失败时别把上次的好数据冲掉。"""
    clean = [str(i) for i in (ids or []) if str(i).strip()]
    if not clean:
        return
    payload = _load()
    key = str(namespace or "").strip()
    bucket = payload.get(key)
    if not isinstance(bucket, dict):
        bucket = {}
    bucket[str(name or "")] = {"ids": clean, "ts": time.time()}
    payload[key] = bucket
    _save(payload)


def forget(namespace: str, name: str) -> bool:
    """删掉某个服务商的缓存（服务商被删除时顺手清）。返回是否真的删了。"""
    payload = _load()
    bucket = payload.get(str(namespace or "").strip())
    if not isinstance(bucket, dict) or str(name or "") not in bucket:
        return False
    del bucket[str(name)]
    _save(payload)
    return True


__all__ = ["DEFAULT_TTL_S", "cache_path", "fetched_at", "forget", "get",
           "is_stale", "put"]