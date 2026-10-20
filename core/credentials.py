# -*- coding: utf-8 -*-
"""凭据层 —— 独立文件 + **只写接口**。

【为什么不用 `.env`】
  ① UI 无法安全地"只写" —— 要让用户在设置里填 key 就得能写入，而 `.env` 同时被程序整体读取，
     **写入路径与读取路径混在一起**；
  ② 会被明文回显 —— 设置界面若要显示"当前 key"就只能读出来；
  ③ 轮换不便 —— 换一家服务商要手工编辑文本文件。

【只写接口是核心】UI **只能**调 `has_key()` / `fingerprint()`，
永远拿不到完整密钥。`get_key()` 存在，但**仅供内部构造客户端用**，
守卫会断言它**不出现在 `ui/` 目录的任何文件里**。

【安全边界：诚实说明做不到什么】
  · 本文件是**明文**。桌面端加密是**安全剧场** —— 解密所需的密钥必须和密文放在同一台机器上，
    攻击者拿到文件也就拿到了密钥。
  · 真正的保护来自**操作系统的用户级 ACL**：文件落在 `app_data_dir()`（`%APPDATA%`），
    同一台机器的其它 Windows 用户读不到。
  · 可选强化：Windows **DPAPI**（`CryptProtectData`）用"用户登录凭据"加密，是**真实的**额外一层。
    它需要引依赖，**留作后续可选，不在首期**。

【向后兼容】优先级是 `credentials.json` → `.env` → 空。
所以现有 `.env` 仍然可用，**不会因为引入新层而让老配置失效**。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from config import AppConfig, app_data_dir

#: 凭据文件名（放在运行数据区，与 `session_state.json` 同级）
CREDENTIALS_NAME = "credentials.json"

#: 只写接口允许暴露的掩码形态：前 4 + 末 4
MASK_HEAD = 4
MASK_TAIL = 4


def credentials_path() -> Path:
    """凭据文件路径（供诊断与守卫使用；**不暴露内容**）。"""
    return Path(app_data_dir()) / CREDENTIALS_NAME


# ---------------------------------------------------------------------------
# 读写（内部）
# ---------------------------------------------------------------------------

def _load() -> dict:
    """读凭据文件。**任何异常都降级为空 dict**（损坏的文件不该让应用起不来）。"""
    p = credentials_path()
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def _save(data: dict) -> None:
    """原子写（临时文件 + `os.replace`），与项目既有约定一致。"""
    p = credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".cred_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, p)          # 原子替换：不会出现"写一半"的文件
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _providers(data: dict) -> dict:
    got = data.get("providers")
    return got if isinstance(got, dict) else {}


# ---------------------------------------------------------------------------
# 只写接口（UI 只许用这几个）
# ---------------------------------------------------------------------------

def set_key(provider: str, value: str) -> None:
    """写入某服务商的密钥。`value` 为空串时**删除**该条目。"""
    data = _load()
    provs = _providers(data)
    if value:
        provs[provider] = {"api_key": value}
    else:
        provs.pop(provider, None)
    data["version"] = 1
    data["providers"] = provs
    _save(data)


def delete_key(provider: str) -> None:
    """删除某服务商的密钥（幂等）。"""
    set_key(provider, "")


def has_key(provider: str) -> bool:
    """★ 该服务商**是否已配置密钥** —— 只回布尔，**不回值**。"""
    return bool(_lookup(provider))


def fingerprint(provider: str) -> str:
    """★ 掩码指纹（形如 `sk-b…c42b`）—— **仍不回完整值**。

    键太短（< 头+尾）时返回 `"已配置（过短，不显示）"`，避免"掩码"把整串露出来。
    """
    v = _lookup(provider)
    if not v:
        return ""
    if len(v) < MASK_HEAD + MASK_TAIL + 1:
        return "已配置（过短，不显示）"
    return f"{v[:MASK_HEAD]}…{v[-MASK_TAIL:]}"


def source(provider: str) -> str:
    """密钥来自哪里：`"file"` / `"env"` / `""`（供界面显示，不泄露值）。"""
    data = _load()
    entry = _providers(data).get(provider)
    if isinstance(entry, dict) and entry.get("api_key"):
        return "file"
    if _env_key(provider):
        return "env"
    return ""


# ---------------------------------------------------------------------------
# 内部取值（**UI 不得调用**）
# ---------------------------------------------------------------------------

def _env_key(provider: str) -> str:
    """从环境变量/`AppConfig` 取——**延迟导入 providers 避免循环**。"""
    try:
        from core.providers import get
        env_name = get(provider).key_env
    except Exception:                                        # noqa: BLE001
        env_name = ""
    if not env_name:
        return ""
    return os.getenv(env_name, "") or getattr(AppConfig, env_name, "") or ""


def _lookup(provider: str) -> str:
    """凭据文件 → 环境变量 → 空（与模块 docstring 的优先级一致）。"""
    data = _load()
    entry = _providers(data).get(provider)
    if isinstance(entry, dict):
        val = entry.get("api_key")
        if isinstance(val, str) and val:
            return val
    return _env_key(provider)


def get_key(provider: str) -> str:
    """★ **仅供内部构造客户端**。UI 层**不得调用**（守卫有源码级断言）。

    命名上刻意与 `has_key`/`fingerprint` 区分，让"谁在用完整值"一眼可查。
    """
    return _lookup(provider)