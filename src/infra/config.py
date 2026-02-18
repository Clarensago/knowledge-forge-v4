"""
配置管理器

支持 JSON + YAML 双格式：
- config.json — LLM 参数、并发数、功能开关
- structure.yaml — 结构解析规则（正则模式、chunk 参数、TOC 检测）

设计原则：实现归实现，配置归配置。修改配置不需要改代码。
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger("knowledge-forge.config")

# 主配置默认值
DEFAULT_CONFIG = {
    "llm": {
        "model": "deepseek-chat",
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "timeout": 300,
        "max_retries": 3,
        "retry_delay": 5,
    },
    "llm_heavy": None,
    "processing": {
        "temperature": 0.2,
        "max_tokens": 16384,
        "min_output_length": 1500,
        "required_sections_min": 3,
        "dual_round": False,
        "stream": True,
    },
    "concurrency": {
        "stage2_workers": 5,
        "rate_limit_rpm": 30,
    },
    "features": {
        "smart_skip_enabled": True,
        "smart_skip_threshold": 0.75,
        "adaptive_routing_enabled": True,
        "embedding_enabled": True,
    },
    "paths": {
        "inbox": "inbox",
        "staging": "staging",
        "outbox": "outbox",
        "logs": "logs",
    },
}


class Config:
    """统一配置管理器"""

    def __init__(self, project_root: str):
        self.root = Path(project_root)
        self.config_dir = self.root / "config"
        self._main: dict = {}
        self._structure: dict = {}
        self._load_all()

    def _load_all(self):
        """加载所有配置文件"""
        self._main = self._load_json("config.json", DEFAULT_CONFIG)
        self._structure = self._load_yaml("structure.yaml", {})

    def _load_json(self, filename: str, defaults: dict) -> dict:
        """加载 JSON 配置，与默认值深度合并"""
        config = _deep_copy(defaults)
        filepath = self.config_dir / filename
        if filepath.exists():
            try:
                file_data = json.loads(filepath.read_text(encoding="utf-8"))
                _deep_merge(config, file_data)
                logger.info(f"已加载配置: {filepath}")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"配置文件加载失败，使用默认值: {e}")
        else:
            logger.info(f"配置文件 {filename} 不存在，使用默认值")
        return config

    def _load_yaml(self, filename: str, defaults: dict) -> dict:
        """加载 YAML 配置"""
        filepath = self.config_dir / filename
        if filepath.exists():
            try:
                data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
                logger.info(f"已加载结构配置: {filepath}")
                return data or defaults
            except Exception as e:
                logger.warning(f"YAML 配置加载失败: {e}")
        return defaults

    # ── 主配置访问（嵌套 key 用 dot notation）──

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项，支持 'llm.model' 式点号路径"""
        return _get_nested(self._main, key, default)

    def set(self, key: str, value: Any):
        """运行时设置配置项（不持久化）"""
        _set_nested(self._main, key, value)

    # ── LLM 配置快捷属性 ──

    @property
    def llm_model(self) -> str:
        return self.get("llm.model", "deepseek-chat")

    @property
    def llm_api_key(self) -> str:
        return self.get("llm.api_key", "")

    @property
    def llm_base_url(self) -> str:
        return self.get("llm.base_url", "https://api.deepseek.com")

    @property
    def llm_timeout(self) -> int:
        return self.get("llm.timeout", 300)

    @property
    def llm_max_retries(self) -> int:
        return self.get("llm.max_retries", 3)

    @property
    def llm_retry_delay(self) -> int:
        return self.get("llm.retry_delay", 5)

    # ── 处理参数 ──

    @property
    def temperature(self) -> float:
        return self.get("processing.temperature", 0.2)

    @property
    def max_tokens(self) -> int:
        return self.get("processing.max_tokens", 16384)

    @property
    def dual_round(self) -> bool:
        return self.get("processing.dual_round", False)

    @property
    def stream(self) -> bool:
        return self.get("processing.stream", True)

    @property
    def min_output_length(self) -> int:
        return self.get("processing.min_output_length", 1500)

    @property
    def concurrency(self) -> int:
        return max(1, self.get("concurrency.stage2_workers", 5))

    # ── 功能开关 ──

    @property
    def smart_skip_enabled(self) -> bool:
        return self.get("features.smart_skip_enabled", True)

    @property
    def smart_skip_threshold(self) -> float:
        return self.get("features.smart_skip_threshold", 0.75)

    @property
    def adaptive_routing_enabled(self) -> bool:
        return self.get("features.adaptive_routing_enabled", True)

    @property
    def embedding_enabled(self) -> bool:
        return self.get("features.embedding_enabled", True)

    # ── 结构解析配置 ──

    @property
    def structure_config(self) -> dict:
        return self._structure

    @property
    def strategy_priority(self) -> list[str]:
        return self._structure.get("strategy_priority", [
            "epub_native", "llm_toc", "regex_detect",
        ])

    @property
    def chunk_config(self) -> dict:
        return self._structure.get("chunk", {
            "min_size": 1000,
            "max_size": 12000,
            "preferred_size": 8000,
            "merge_threshold": 1500,
        })

    @property
    def regex_patterns(self) -> dict:
        return self._structure.get("regex_patterns", {})

    @property
    def regex_detection(self) -> dict:
        return self._structure.get("regex_detection", {})

    @property
    def toc_parsing(self) -> dict:
        return self._structure.get("toc_parsing", {})

    # ── API Key 管理 ──

    def load_api_key(self) -> Optional[str]:
        if self.llm_api_key:
            return self.llm_api_key
        key_file = self.root / ".api_key"
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
            if key:
                return key
        return None

    def save_api_key(self, key: str):
        import os
        key_file = self.root / ".api_key"
        key_file.write_text(key, encoding="utf-8")
        os.chmod(key_file, 0o600)
        logger.info("API Key 已保存")

    # ── CLI 覆盖 ──

    def apply_cli_overrides(self, **kwargs):
        for key, value in kwargs.items():
            if value is not None:
                self.set(key, value)
                logger.info(f"命令行覆盖: {key} = {value}")


# ━━━━━━━━━━━━━━━━━━━━ 工具函数 ━━━━━━━━━━━━━━━━━━━━

def _deep_copy(d: dict) -> dict:
    """简单深拷贝（只处理 dict/list/标量）"""
    import copy
    return copy.deepcopy(d)


def _deep_merge(base: dict, override: dict):
    """将 override 深度合并到 base（就地修改 base）"""
    for key, value in override.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _get_nested(d: dict, key: str, default: Any = None) -> Any:
    """用 'a.b.c' 式路径获取嵌套值"""
    parts = key.split(".")
    current = d
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def _set_nested(d: dict, key: str, value: Any):
    """用 'a.b.c' 式路径设置嵌套值"""
    parts = key.split(".")
    current = d
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value
