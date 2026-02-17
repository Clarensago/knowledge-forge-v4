"""
LLM 客户端 — OpenAI 兼容协议封装

支持 stream/sync 两种模式、多轮对话、用量统计。
从 v3 迁移核心逻辑，接口保持简洁。
"""

import json
import sys
import time
import logging
import threading
from typing import Optional

import requests

logger = logging.getLogger("knowledge-forge.llm")


class UsageStats:
    """API 调用用量统计（线程安全）"""

    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_embedding_tokens = 0
        self.total_calls = 0
        self._lock = threading.Lock()

    def record(self, input_tokens: int, output_tokens: int):
        with self._lock:
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_calls += 1

    def record_embedding(self, tokens: int):
        with self._lock:
            self.total_embedding_tokens += tokens
            self.total_calls += 1

    def summary(self) -> str:
        with self._lock:
            total = self.total_input_tokens + self.total_output_tokens + self.total_embedding_tokens
            parts = [
                f"API 用量: {self.total_calls} 次",
                f"输入 {self.total_input_tokens:,}",
                f"输出 {self.total_output_tokens:,}",
            ]
            if self.total_embedding_tokens > 0:
                parts.append(f"Embedding {self.total_embedding_tokens:,}")
            parts.append(f"合计 {total:,} tokens")
            return ", ".join(parts)


class LLMClient:
    """LLM 客户端（OpenAI 兼容协议）"""

    _stdout_lock = threading.Lock()

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: int = 300,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.usage = UsageStats()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def check_health(self) -> bool:
        try:
            resp = requests.get(
                f"{self.base_url}/v1/models",
                headers=self._headers(), timeout=15,
            )
            if resp.status_code in (401, 403):
                logger.error("API Key 无效")
                return False
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return False

    def generate(
        self, prompt: str, temperature: float = 0.2, max_tokens: int = 8192,
        stream: bool = True, system_message: str = None, task_label: str = "",
    ) -> str:
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, temperature=temperature, max_tokens=max_tokens,
                         stream=stream, task_label=task_label)

    def chat(
        self, messages: list[dict], temperature: float = 0.2, max_tokens: int = 8192,
        stream: bool = True, task_label: str = "",
    ) -> str:
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        try:
            if stream:
                return self._stream(url, payload, task_label)
            return self._sync(url, payload)
        except requests.ConnectionError:
            raise ConnectionError(f"无法连接到 API ({self.base_url})")
        except requests.Timeout:
            raise TimeoutError(f"API 超时 ({self.timeout}s)")

    def _stream(self, url: str, payload: dict, task_label: str = "") -> str:
        resp = requests.post(
            url, json=payload, headers=self._headers(),
            stream=True, timeout=(30, self.timeout),
        )
        self._check_error(resp)

        chunks = []
        token_count = 0
        start = time.time()
        usage_data = {}

        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            if not line_str.startswith("data: "):
                continue
            data_str = line_str[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if "error" in chunk:
                raise RuntimeError(f"API 错误: {chunk['error']}")
            if "usage" in chunk:
                usage_data = chunk["usage"]
            choices = chunk.get("choices", [])
            if choices:
                token = choices[0].get("delta", {}).get("content", "")
                if token:
                    chunks.append(token)
                    token_count += 1
                    if token_count % 50 == 0:
                        elapsed = time.time() - start
                        label = f"[{task_label}] " if task_label else "  "
                        with self._stdout_lock:
                            sys.stdout.write(
                                f"\r{label}生成中... {token_count} tokens "
                                f"({token_count / elapsed:.1f} t/s)"
                            )
                            sys.stdout.flush()
                if choices[0].get("finish_reason"):
                    break

        elapsed = time.time() - start
        label = f"[{task_label}] " if task_label else "  "
        with self._stdout_lock:
            sys.stdout.write(
                f"\r{label}完成: {token_count} tokens, "
                f"{elapsed:.1f}s ({token_count / elapsed:.1f} t/s)\n"
            )
            sys.stdout.flush()

        self.usage.record(
            usage_data.get("prompt_tokens", 0),
            usage_data.get("completion_tokens", token_count),
        )
        return "".join(chunks)

    def _sync(self, url: str, payload: dict) -> str:
        payload["stream"] = False
        resp = requests.post(
            url, json=payload, headers=self._headers(), timeout=self.timeout,
        )
        self._check_error(resp)
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"API 错误: {data['error']}")
        usage = data.get("usage", {})
        self.usage.record(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        choices = data.get("choices", [])
        return choices[0]["message"]["content"] if choices else ""

    @staticmethod
    def _check_error(resp):
        if resp.status_code == 401:
            raise RuntimeError("API Key 无效")
        if resp.status_code == 429:
            raise RuntimeError("API 限流，请稍后重试")
        if resp.status_code != 200:
            raise RuntimeError(f"API 错误 {resp.status_code}: {resp.text[:500]}")

    def embed(self, texts: list[str], model: str = None) -> Optional[list[list[float]]]:
        if not texts:
            return []
        url = f"{self.base_url}/v1/embeddings"
        payload = {
            "model": model or self.model,
            "input": texts,
            "encoding_format": "float",
        }
        try:
            resp = requests.post(
                url, json=payload, headers=self._headers(), timeout=self.timeout,
            )
            self._check_error(resp)
            data = resp.json()
            if "error" in data:
                logger.warning(f"Embedding 错误: {data['error']}")
                return None
            usage = data.get("usage", {})
            self.usage.record_embedding(usage.get("total_tokens", 0))
            embeddings = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
            return [item["embedding"] for item in embeddings]
        except Exception as e:
            logger.warning(f"Embedding 失败: {e}")
            return None
