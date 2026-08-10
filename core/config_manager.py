"""
配置管理器 — 读写 .env 文件中的 API Key

支持保留 .env 中的注释和格式,
仅更新被修改的键值。
"""

from pathlib import Path

from config import app_data_dir

ENV_PATH = app_data_dir() / ".env"

MANAGED_KEYS = {
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "AMAP_API_KEY",
    "TAVILY_API_KEY",
    "EMBEDDING_API_KEY",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "HF_ENDPOINT",
}


class ConfigManager:
    """读取 / 写入 .env 文件中的 API 配置。"""

    @staticmethod
    def load() -> dict[str, str]:
        """从 .env 读取所有受管键，返回键值字典。"""
        result: dict[str, str] = {}
        if not ENV_PATH.exists():
            return result
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if "=" in stripped:
                        k, v = stripped.split("=", 1)
                        k = k.strip()
                        if k in MANAGED_KEYS:
                            result[k] = v.strip()
        except OSError as e:
            print(f"[ConfigManager] 读取失败: {e}")
        return result

    @staticmethod
    def save(updates: dict[str, str]) -> None:
        """将更新写回 .env，保留原有行和注释不变。

        .env 中已存在的键 → 值替换
        不存在的键     → 追加到文件末尾
        """
        lines: list[str] = []
        updated: set[str] = set()

        if ENV_PATH.exists():
            try:
                with open(ENV_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if (
                            stripped
                            and not stripped.startswith("#")
                            and "=" in stripped
                        ):
                            k = stripped.split("=", 1)[0].strip()
                            if k in updates:
                                lines.append(f"{k}={updates[k]}\n")
                                updated.add(k)
                                continue
                        lines.append(line)
            except OSError:
                pass

        for k, v in updates.items():
            if k not in updated:
                lines.append(f"\n{k}={v}\n")

        try:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError as e:
            print(f"[ConfigManager] 写入失败: {e}")
            raise
