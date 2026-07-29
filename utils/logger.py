"""日志工具。提供结构化的翻译进度输出，屏蔽 SDK 底层日志。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_INVALID_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*+\s]+')
_SUPPRESS_FILTER_NAME = "opentrans-openhands-filter"
_SECRET_FIELD_NAMES = {
    "api_key", "token", "authorization", "password", "secret", "credential",
    "llm_api_key", "api_secret",
}
_SECRET_VALUE_PATTERNS = re.compile(
    r'(?:sk-[a-zA-Z0-9]{10,}|ghp_[a-zA-Z0-9]{10,}|xox[bpras]-[a-zA-Z0-9-]{10,})',
)
_DEFAULT_TRACE_MAX_FIELD_CHARS = 20000


class OpenhandsFilter(logging.Filter):
    """屏蔽 openhands 子系统 INFO/DEBUG，只保留 WARNING+。"""

    opentrans_filter_name = _SUPPRESS_FILTER_NAME

    def filter(self, record):
        if record.name.startswith("openhands"):
            return record.levelno >= logging.WARNING
        return True


# 项目日志器（只输出关键信息）
logger = logging.getLogger("opentrans")
logger.setLevel(logging.INFO)
logger.propagate = False  # 防止日志传播到根 logger，避免重复输出


def configure_logger(force: bool = False) -> logging.Logger:
    """配置项目 stdout logger。force=True 时重置已有 handler。"""
    if force:
        logger.handlers.clear()
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
    return logger


configure_logger()


def suppress_sdk_logging():
    """屏蔽 SDK 和第三方库的 INFO 日志；多次调用保持幂等。"""
    root_logger = logging.getLogger()

    # 双重保险：设置层级级别 + 根过滤器
    logging.getLogger("openhands").setLevel(logging.WARNING)
    if not any(getattr(f, "opentrans_filter_name", None) == _SUPPRESS_FILTER_NAME for f in root_logger.filters):
        root_logger.addFilter(OpenhandsFilter())

    logging.getLogger("LiteLLM").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    root_logger.setLevel(logging.WARNING)

    # 屏蔽第三方库的弃用警告
    warnings.filterwarnings("ignore", message="Cost calculation failed")
    warnings.filterwarnings("ignore", module="fastapi")


def _safe_path_part(value: str, fallback: str = "unknown") -> str:
    """将模型名/项目名等清洗成安全路径片段。"""
    cleaned = _INVALID_PATH_CHARS_RE.sub("_", (value or "").strip()).strip("._")
    return cleaned or fallback


def setup_log_dir(model: str, project_name: str,
                  source_language: str, target_language: str) -> Path:
    """创建并返回本次运行的日志目录。
    目录结构: logs/{model}/{project}_{source}_to_{target}_{timestamp}/
    """
    model_safe = _safe_path_part(model, "unknown_model")
    project_safe = _safe_path_part(project_name, "unknown_project")
    source_safe = _safe_path_part(source_language, "source")
    target_safe = _safe_path_part(target_language, "target")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_dir = Path("logs") / model_safe / \
        f"{project_safe}_{source_safe}_to_{target_safe}_{timestamp}"
    log_dir.mkdir(parents=True, exist_ok=False)
    return log_dir


def save_prompt_to(log_dir: Path, prompt_text: str) -> Path:
    """将 system prompt 写入日志目录。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "system_prompt.txt"
    path.write_text(prompt_text, encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════
#  功能调用追踪日志（TranslationTraceLogger）
# ═══════════════════════════════════════════════════════════════

class TranslationTraceLogger:
    """双通道日志的“文件追踪”一侧：将每一步 LLM 请求/响应、工具调用/观察写入 JSONL。

    与 stdout 进度 logger 完全隔离，目的是排查模型空转、修复循环退化等疑难问题。
    """

    def __init__(
        self,
        log_dir: Path,
        *,
        run_id: str = "",
        project_name: str = "",
        model: str = "",
        source_language: str = "",
        target_language: str = "",
        max_field_chars: int = _DEFAULT_TRACE_MAX_FIELD_CHARS,
        redact_secrets: bool = True,
    ):
        self._log_dir = log_dir
        self._run_id = run_id
        self._project_name = project_name
        self._model = model
        self._source_language = source_language
        self._target_language = target_language
        self._max_field_chars = max_field_chars
        self._redact_secrets = redact_secrets
        self._lock = threading.Lock()
        self._context: dict[str, Any] = {}

        log_dir.mkdir(parents=True, exist_ok=True)
        self._path = log_dir / "translation_trace.jsonl"
        self._file = None
        self._written_count = 0
        self._event_counts: dict[str, int] = {}
        self._summary_written = False

    # -- 运行时上下文（layer / round / step） ---------------------

    def set_context(self, **kwargs: Any) -> None:
        self._context.update(kwargs)

    # -- OpenHands Conversation 回调 -------------------------------

    def on_event(self, event) -> None:
        """作为 `Conversation(callbacks=[...])` 的回调，记录 Action / Observation 事件。"""
        try:
            from openhands.sdk.event import ActionEvent, MessageEvent, ObservationEvent
        except ImportError:
            return
        try:
            if isinstance(event, ActionEvent):
                self.write("action_event", **self._dump_action_event(event))
            elif isinstance(event, ObservationEvent):
                self.write("observation_event", **self._dump_observation_event(event))
            elif isinstance(event, MessageEvent):
                self.write("message_event", **self._dump_message_event(event))
        except Exception:
            pass  # trace 写入失败不应中断翻译运行

    # -- 写入核心 -------------------------------------------------

    def write(self, event_type: str, **kwargs: Any) -> None:
        with self._lock:
            try:
                safe = self._redact(self._truncate(kwargs))
                record: dict[str, Any] = {
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "run_id": self._run_id,
                    "step": self._context.get("step"),
                    "layer_idx": self._context.get("layer_idx"),
                    "round_idx": self._context.get("round_idx"),
                    "event_type": event_type,
                }
                record.update({k: v for k, v in self._context.items()
                              if k not in ("step", "layer_idx", "round_idx")})
                record.update(safe)
                line = json.dumps(record, ensure_ascii=False, default=str)
                self._ensure_file()
                if self._file:
                    self._file.write(line + "\n")
                    self._file.flush()
                    self._written_count += 1
                    self._event_counts[event_type] = self._event_counts.get(event_type, 0) + 1
            except Exception:
                pass  # trace 写入失败不应中断翻译运行

    def _ensure_file(self) -> None:
        if self._file is None:
            self._file = open(str(self._path), "a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def summary_path(self) -> Path:
        return self._log_dir / "translation_trace_summary.md"

    @property
    def index_path(self) -> Path:
        return self._log_dir / "trace_index.json"

    @property
    def written_count(self) -> int:
        return self._written_count

    # -- 数据安全 / 截断 -------------------------------------------

    def _truncate(self, obj: Any, depth: int = 0) -> Any:
        if depth > 10:
            return "[max recursion]"
        if isinstance(obj, dict):
            return {k: self._truncate(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._truncate(v, depth + 1) for v in obj]
        if isinstance(obj, str) and len(obj) > self._max_field_chars:
            return {
                "preview": obj[:self._max_field_chars],
                "truncated": True,
                "original_chars": len(obj),
                "sha256": hashlib.sha256(obj.encode("utf-8", errors="replace")).hexdigest(),
            }
        return obj

    def _redact(self, obj: Any) -> Any:
        if not self._redact_secrets:
            return obj
        if isinstance(obj, dict):
            return {k: "[REDACTED]" if k.lower() in _SECRET_FIELD_NAMES else self._redact(v)
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._redact(v) for v in obj]
        if isinstance(obj, str) and _SECRET_VALUE_PATTERNS.search(obj):
            return _SECRET_VALUE_PATTERNS.sub("[REDACTED]", obj)
        return obj

    # -- 事件 dump 辅助 --------------------------------------------

    @staticmethod
    def _dump_action_event(event) -> dict[str, Any]:
        action = getattr(event, "action", None)
        return {
            "event_id": getattr(event, "id", None),
            "source": getattr(event, "source", "agent"),
            "tool_name": getattr(event, "tool_name", None),
            "tool_call_id": getattr(event, "tool_call_id", None),
            "llm_response_id": getattr(event, "llm_response_id", None),
            "thought": getattr(event, "thought", None),
            "action_data": action.model_dump(mode="json", exclude_none=True) if action and hasattr(action, "model_dump") else str(action),
            "summary": getattr(event, "summary", None),
        }

    @staticmethod
    def _dump_observation_event(event) -> dict[str, Any]:
        obs = getattr(event, "observation", None)
        return {
            "event_id": getattr(event, "id", None),
            "action_id": getattr(event, "action_id", None),
            "tool_name": getattr(event, "tool_name", None),
            "tool_call_id": getattr(event, "tool_call_id", None),
            "is_error": obs.is_error if obs and hasattr(obs, "is_error") else None,
            "text": getattr(event, "content", None) or (obs.text if obs and hasattr(obs, "text") else None),
            "observation_data": obs.model_dump(mode="json", exclude_none=True) if obs and hasattr(obs, "model_dump") else str(obs),
        }

    @staticmethod
    def _dump_message_event(event) -> dict[str, Any]:
        return {
            "event_id": getattr(event, "id", None),
            "source": getattr(event, "source", None),
            "role": getattr(getattr(event, "llm_message", None), "role", None),
            "content_text": getattr(event, "content", None),
        }

    # -- 中文摘要 / 索引 -------------------------------------------

    def write_readable_outputs(self) -> None:
        """根据 JSONL trace 生成中文 Markdown 摘要和 JSON 索引。"""
        if self._summary_written or not self._path.exists():
            return
        records = self._load_records()
        self.summary_path.write_text(self._build_markdown_summary(records), encoding="utf-8")
        self.index_path.write_text(
            json.dumps(self._build_index(records), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._summary_written = True

    def _load_records(self) -> list[dict[str, Any]]:
        records = []
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return records

    def _build_index(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        warnings: list[str] = []
        for r in records:
            et = r.get("event_type", "unknown")
            counts[et] = counts.get(et, 0) + 1
        for key, label in [
            ("idle_nudge", "检测到空转提醒"),
            ("invalid_response", "检测到无效响应"),
            ("conversation_error", "检测到运行时错误"),
            ("llm_error", "检测到 LLM 调用错误"),
        ]:
            if counts.get(key):
                warnings.append(f"{label}: {counts[key]} 次")
        return {
            "schema_version": 1,
            "run_id": self._run_id,
            "project": self._project_name,
            "model": self._model,
            "source_language": self._source_language,
            "target_language": self._target_language,
            "trace_file": self._path.name,
            "summary_file": self.summary_path.name,
            "event_counts": counts,
            "warnings": warnings,
        }

    def _build_markdown_summary(self, records: list[dict[str, Any]]) -> str:
        lines = [
            "# 翻译过程追踪摘要",
            "",
            "## 运行信息",
            f"- 项目：{self._project_name or '(unknown)'}",
            f"- 模型：{self._model or '(unknown)'}",
            f"- 语言：{self._source_language} → {self._target_language}",
            f"- Trace 文件：`{self._path.name}`",
            f"- 事件总数：{len(records)}",
            "",
        ]
        warnings = self._build_index(records)["warnings"]
        if warnings:
            lines.extend(["## 可能需要关注的问题", ""])
            lines.extend(f"- {w}" for w in warnings)
            lines.append("")

        lines.extend(["## 时间线", ""])
        current_layer = object()
        current_round = object()
        for r in records:
            layer = r.get("layer_idx")
            round_idx = r.get("round_idx")
            if layer != current_layer:
                current_layer = layer
                current_round = object()
                if layer is not None:
                    lines.extend([f"### Layer {layer}", ""])
            if round_idx != current_round:
                current_round = round_idx
                if round_idx is not None:
                    lines.extend([f"#### Round {round_idx}", ""])
            text = self._record_to_markdown_line(r)
            if text:
                lines.append(text)
        lines.append("")
        return "\n".join(lines)

    def _record_to_markdown_line(self, r: dict[str, Any]) -> str:
        et = r.get("event_type", "unknown")
        step = r.get("step")
        prefix = f"- Step {step}: " if step is not None else "- "
        if et == "llm_request":
            payload = r.get("payload", {})
            return prefix + f"发送 LLM 请求：messages={payload.get('message_count')}, tools={len(payload.get('tools', []))}"
        if et == "llm_response":
            payload = r.get("payload", {})
            tools = [c.get("name") for c in payload.get("tool_calls", []) if isinstance(c, dict)]
            tool_part = f"，工具调用={tools}" if tools else ""
            return prefix + f"收到 LLM 响应：{payload.get('response_type')}{tool_part}"
        if et == "action_event":
            tool = r.get("tool_name") or r.get("payload", {}).get("tool_name")
            return prefix + f"调用工具：`{tool}`"
        if et == "observation_event":
            tool = r.get("tool_name") or r.get("payload", {}).get("tool_name")
            is_error = r.get("is_error")
            status = "失败" if is_error else "成功"
            return prefix + f"工具返回：`{tool}` {status}"
        if et == "idle_nudge":
            payload = r.get("payload", {})
            return prefix + f"⚠️ 空转提醒：{payload.get('reason')}（新文件数={payload.get('new_file_count')}）"
        if et == "invalid_response":
            payload = r.get("payload", {})
            return prefix + f"⚠️ 无效响应：{payload.get('reason')}（{payload.get('count')}/{payload.get('limit')}）"
        if et == "test_analysis_result":
            payload = r.get("payload", {})
            return prefix + f"测试结果：{payload.get('passed_tests')}/{payload.get('total_tests')} 通过，编译={'成功' if payload.get('compilation_success') else '失败'}"
        if et in {"round_start", "round_end", "layer_start", "layer_end", "run_start", "run_end", "feedback_sent", "conversation_error"}:
            return prefix + f"{et}"
        return ""

    # -- 资源管理 -------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
        self.write_readable_outputs()

    def __del__(self) -> None:
        self.close()
