"""日志工具。提供结构化的翻译进度输出，屏蔽 SDK 底层日志。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import warnings
from collections import Counter, defaultdict
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
        self._recent_live_records: list[dict[str, Any]] = []
        self._last_live_write_count = 0

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
                    self._update_live_report(record)
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
    def live_path(self) -> Path:
        return self._log_dir / "translation_trace_live.md"

    @property
    def index_path(self) -> Path:
        return self._log_dir / "trace_index.json"

    @property
    def written_count(self) -> int:
        return self._written_count

    # -- 实时 Markdown 进度 -----------------------------------------

    def _update_live_report(self, record: dict[str, Any]) -> None:
        """运行中轻量刷新 translation_trace_live.md，避免等待结束才看到报告。"""
        if record.get("event_type") in {
            "action_event", "observation_event", "llm_response", "test_analysis_result",
            "completeness_check", "idle_nudge", "invalid_response", "conversation_error",
            "round_end", "layer_end", "run_end",
        }:
            self._recent_live_records.append(record)
            self._recent_live_records = self._recent_live_records[-30:]
        key_events = {
            "test_analysis_result", "completeness_check", "idle_nudge", "invalid_response",
            "conversation_error", "round_end", "layer_end", "run_end",
        }
        if record.get("event_type") in key_events or self._written_count - self._last_live_write_count >= 25:
            self._write_live_report()

    def _write_live_report(self) -> None:
        try:
            self.live_path.write_text(self._build_live_markdown(), encoding="utf-8")
            self._last_live_write_count = self._written_count
        except OSError:
            pass

    def _build_live_markdown(self) -> str:
        latest_completeness = self._latest_payload("completeness_check")
        latest_test = self._latest_payload("test_analysis_result")
        last_event = self._recent_live_records[-1] if self._recent_live_records else None
        lines = [
            "# 实时翻译进度",
            "",
            "> 运行中自动刷新；完整展示报告会在运行结束后生成 `translation_trace_summary.md`。",
            "",
            "## 当前状态",
            "",
            f"- 项目：{self._project_name or '(unknown)'}",
            f"- 模型：{self._model or '(unknown)'}",
            f"- 语言：{self._source_language} → {self._target_language}",
            f"- 当前 Layer：{self._context.get('layer_idx', '-')}",
            f"- 当前 Round：{self._context.get('round_idx', '-')}",
            f"- 已写入事件：{self._written_count}",
            f"- 最近事件：{last_event.get('event_type') if last_event else '-'}",
            "",
            "## 最近完整性检查",
            "",
        ]
        if latest_completeness:
            status = "通过" if latest_completeness.get("passed") else "失败"
            lines.extend([
                f"- 结果：{status}",
                f"- 期望/已生成：{latest_completeness.get('expected_count')} / {latest_completeness.get('present_count')}",
                f"- 缺失：{latest_completeness.get('missing_count')}",
            ])
            missing = latest_completeness.get("missing") or []
            if missing:
                lines.append("- 缺失示例：")
                for item in missing[:5]:
                    lines.append(f"  - `{item.get('source')}` → `{item.get('expected')}`")
        else:
            lines.append("- 暂无完整性检查记录。")
        lines.extend(["", "## 最近测试结果", ""])
        if latest_test:
            lines.extend([
                f"- 编译：{'成功' if latest_test.get('compilation_success') else '失败'}",
                f"- 测试：{latest_test.get('passed_tests')}/{latest_test.get('total_tests')}",
            ])
        else:
            lines.append("- 暂无测试结果。")
        lines.extend([
            "",
            "## 事件计数",
            "",
            "| 事件 | 次数 |",
            "| --- | ---: |",
        ])
        for event_type, count in sorted(self._event_counts.items()):
            lines.append(f"| `{event_type}` | {count} |")
        lines.extend(["", "## 最近关键事件", ""])
        for r in self._recent_live_records[-20:]:
            line = self._record_to_markdown_line(r)
            if line:
                lines.append(line)
            else:
                lines.append(f"- {r.get('event_type')}")
        lines.append("")
        return "\n".join(lines)

    def _latest_payload(self, event_type: str) -> dict[str, Any] | None:
        for r in reversed(self._recent_live_records):
            if r.get("event_type") == event_type:
                return r.get("payload") or {}
        return None

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
            ("completeness_check", "执行翻译完整性检查"),
            ("completeness_feedback_sent", "发送完整性补齐反馈"),
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
        """生成面向人类阅读/展示的运行分析报告，而不是原始事件流水。"""
        stats = self._summarize_records(records)
        lines: list[str] = [
            "# 翻译运行分析报告",
            "",
            "## 1. 本次运行概览",
            "",
            f"- 项目：{self._project_name or '(unknown)'}",
            f"- 模型：{self._model or '(unknown)'}",
            f"- 语言：{self._source_language} → {self._target_language}",
            f"- Trace 文件：`{self._path.name}`",
            f"- 事件总数：{len(records)}",
            "",
            "| 指标 | 数值 |",
            "| --- | ---: |",
            f"| 总耗时 | {self._fmt_seconds(stats['total_elapsed'])} |",
            f"| LLM 请求 | {stats['event_counts'].get('llm_request', 0)} 次 |",
            f"| LLM 响应 | {stats['event_counts'].get('llm_response', 0)} 次 |",
            f"| 工具调用 | {sum(stats['tool_counts'].values())} 次 |",
            f"| 最终测试 | {stats['final_tests']} |",
            f"| 最终状态 | {stats['final_status']} |",
            "",
            "## 2. 分层执行结果",
            "",
            "| 层 | 解锁源码文件 | 新增测试文件 | 可见测试文件 | 测试模式 | 测试结果 | 耗时 |",
            "| --- | ---: | ---: | ---: | --- | --- | ---: |",
        ]
        if stats["layers"]:
            for layer, info in sorted(stats["layers"].items()):
                lines.append(
                    f"| Layer {layer} | {info.get('file_count', '-')} | "
                    f"{info.get('new_test_count', '-')} | {info.get('visible_test_count', '-')} | "
                    f"{self._test_scope_label(info.get('test_scope'))} | "
                    f"{info.get('tests', '-')} | {self._fmt_seconds(info.get('elapsed_s'))} |"
                )
        else:
            lines.append("| - | - | - | - | - | - | - |")

        lines.extend([
            "",
            "## 3. 每层做了什么",
            "",
        ])
        if stats["layers"]:
            for layer, info in sorted(stats["layers"].items()):
                lines.extend(self._build_layer_report_section(layer, info))
        else:
            lines.append("未检测到分层事件。")

        lines.extend([
            "",
            "## 4. 工具调用行为分析",
            "",
            "| 工具 | 次数 | 主要用途 |",
            "| --- | ---: | --- |",
        ])
        for tool, count in stats["tool_counts"].most_common():
            lines.append(f"| `{tool}` | {count} | {self._tool_usage_label(tool)} |")
        if not stats["tool_counts"]:
            lines.append("| - | 0 | 未检测到工具调用 |")

        lines.extend([
            "",
            "## 5. 文件生成/修改情况",
            "",
            "### 写入次数较多的文件",
            "",
            "| 文件 | 写入次数 | 说明 |",
            "| --- | ---: | --- |",
        ])
        duplicate_rows = 0
        for path, count in stats["file_writes"].most_common():
            if count <= 1:
                continue
            duplicate_rows += 1
            lines.append(f"| `{path}` | {count} | 可能经过多轮修正或重复覆盖 |")
        if duplicate_rows == 0:
            lines.append("| - | 0 | 未发现重复写入文件 |")

        lines.extend([
            "",
            "### 本次产出/修改的主要文件",
            "",
        ])
        if stats["file_writes"]:
            for path, count in stats["file_writes"].most_common(30):
                suffix = f"（{count} 次）" if count > 1 else ""
                lines.append(f"- `{path}`{suffix}")
        else:
            lines.append("- 未检测到文件写入。")

        lines.extend([
            "",
            "## 6. 翻译完整性检查",
            "",
            "| 阶段 | 层 | 尝试 | 期望文件 | 已生成 | 缺失 | 结果 |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ])
        if stats["completeness_checks"]:
            for check in stats["completeness_checks"]:
                phase = "最终检查" if check.get("phase") == "final" else "层内检查"
                result = "通过" if check.get("passed") else "失败"
                lines.append(
                    f"| {phase} | Layer {check.get('layer')} | {check.get('attempt', '-')} | "
                    f"{check.get('expected_count', '-')} | {check.get('present_count', '-')} | "
                    f"{check.get('missing_count', '-')} | {result} |"
                )
        else:
            lines.append("| - | - | - | - | - | - | 未记录 |")
        final_missing = self._final_missing_items(stats)
        if final_missing:
            lines.extend(["", "### 最终仍缺失的文件", ""])
            for item in final_missing[:30]:
                lines.append(
                    f"- Source: `{item.get('source')}` → Expected: `{item.get('expected')}` "
                    f"（Layer {item.get('layer')}）"
                )
            if len(final_missing) > 30:
                lines.append(f"- ... 还有 {len(final_missing) - 30} 个缺失项")

        lines.extend([
            "",
            "## 7. 异常和需要关注的行为",
            "",
            "| 类型 | 次数 | 说明 |",
            "| --- | ---: | --- |",
        ])
        issue_rows = self._build_issue_rows(stats)
        lines.extend(issue_rows or ["| 无 | 0 | 未检测到明显异常 |"])

        lines.extend([
            "",
            "## 8. 性能分析",
            "",
            "| 层 | LLM 调用 | 平均 LLM 响应 | 最大 LLM 响应 | 层耗时 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for layer, info in sorted(stats["layers"].items()):
            latencies = info.get("llm_latencies", [])
            avg = sum(latencies) / len(latencies) if latencies else None
            max_latency = max(latencies) if latencies else None
            lines.append(
                f"| Layer {layer} | {len(latencies)} | {self._fmt_seconds(avg)} | "
                f"{self._fmt_seconds(max_latency)} | {self._fmt_seconds(info.get('elapsed_s'))} |"
            )
        if not stats["layers"]:
            lines.append("| - | 0 | - | - | - |")

        lines.extend([
            "",
            "## 9. 总体结论",
            "",
            self._build_human_conclusion(stats),
            "",
        ])
        return "\n".join(lines)

    def _summarize_records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        event_counts = Counter(r.get("event_type", "unknown") for r in records)
        tool_counts: Counter[str] = Counter()
        file_writes: Counter[str] = Counter()
        layers: dict[int, dict[str, Any]] = defaultdict(lambda: {
            "tool_counts": Counter(),
            "file_writes": Counter(),
            "llm_latencies": [],
        })
        issues: Counter[str] = Counter()
        completeness_checks: list[dict[str, Any]] = []
        llm_request_ts: dict[tuple[int | None, int | None], datetime] = {}
        final_status = "未完成"
        final_tests = "未检测到测试结果"
        total_elapsed = None

        for r in records:
            et = r.get("event_type")
            layer = r.get("layer_idx")
            payload = r.get("payload") or {}
            if et == "layer_start" and layer is not None:
                layers[layer]["file_count"] = payload.get("file_count")
            elif et == "round_start":
                llm_request_ts.pop((layer, r.get("round_idx")), None)
            elif et == "llm_request":
                try:
                    llm_request_ts[(layer, r.get("round_idx"))] = datetime.strptime(
                        r.get("ts", ""), "%Y-%m-%dT%H:%M:%S.%fZ"
                    )
                except ValueError:
                    pass
            elif et == "llm_response":
                key = (layer, r.get("round_idx"))
                start = llm_request_ts.pop(key, None)
                if start and layer is not None:
                    try:
                        end = datetime.strptime(r.get("ts", ""), "%Y-%m-%dT%H:%M:%S.%fZ")
                        layers[layer]["llm_latencies"].append((end - start).total_seconds())
                    except ValueError:
                        pass
            elif et == "action_event":
                tool = r.get("tool_name") or "unknown"
                tool_counts[tool] += 1
                if layer is not None:
                    layers[layer]["tool_counts"][tool] += 1
                if tool in {"create_file", "edit_file"}:
                    data = r.get("action_data") or {}
                    path = data.get("filepath") if isinstance(data, dict) else None
                    if path:
                        file_writes[path] += 1
                        if layer is not None:
                            layers[layer]["file_writes"][path] += 1
            elif et == "observation_event" and r.get("is_error"):
                issues[f"工具返回错误：{r.get('tool_name') or 'unknown'}"] += 1
            elif et == "invalid_response":
                issues["无效 LLM 响应"] += 1
            elif et == "idle_nudge":
                issues["空转提醒"] += 1
            elif et == "conversation_error":
                issues["Conversation 运行错误"] += 1
            elif et == "completeness_check":
                completeness_checks.append({
                    "layer": payload.get("layer", layer),
                    "phase": payload.get("phase") or "layer",
                    "attempt": payload.get("attempt"),
                    "retry_limit": payload.get("retry_limit"),
                    "passed": payload.get("passed"),
                    "expected_count": payload.get("expected_count"),
                    "present_count": payload.get("present_count"),
                    "missing_count": payload.get("missing_count"),
                    "missing": payload.get("missing") or [],
                })
                if payload.get("missing_count", 0):
                    issues["翻译完整性缺失"] += 1
            elif et == "completeness_feedback_sent":
                issues["完整性补齐反馈"] += 1
            elif et == "test_analysis_start" and layer is not None:
                visible = payload.get("visible_test_files") or []
                new_tests = payload.get("new_test_files") or []
                layers[layer]["test_scope"] = payload.get("test_scope")
                layers[layer]["visible_test_count"] = len(visible)
                layers[layer]["new_test_count"] = len(new_tests)
                layers[layer]["test_command"] = payload.get("test_command")
            elif et == "test_analysis_result" and layer is not None:
                tests = f"{payload.get('passed_tests')}/{payload.get('total_tests')}"
                if payload.get("compilation_success") is False:
                    tests += "（编译失败）"
                layers[layer]["tests"] = tests
                final_tests = tests
            elif et == "round_end" and layer is not None:
                layers[layer]["elapsed_s"] = payload.get("elapsed_s")
            elif et == "run_end":
                total_elapsed = payload.get("elapsed_s")
                if payload.get("all_passed"):
                    final_status = "成功"
                else:
                    final_status = payload.get("exit_reason") or "未完全通过"

        return {
            "event_counts": event_counts,
            "tool_counts": tool_counts,
            "file_writes": file_writes,
            "layers": dict(layers),
            "issues": issues,
            "completeness_checks": completeness_checks,
            "final_status": final_status,
            "final_tests": final_tests,
            "total_elapsed": total_elapsed,
        }

    @staticmethod
    def _fmt_seconds(value: Any) -> str:
        if value is None:
            return "-"
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return "-"
        if seconds >= 60:
            minutes = int(seconds // 60)
            rest = int(seconds % 60)
            return f"{minutes}分{rest}秒"
        return f"{seconds:.1f}s"

    @staticmethod
    def _test_scope_label(scope: Any) -> str:
        return {
            "cumulative_regression": "累计回归测试",
            "auto_detected_regression": "自动检测回归测试",
        }.get(scope, str(scope) if scope else "-")

    @staticmethod
    def _tool_usage_label(tool: str) -> str:
        labels = {
            "read_file": "读取源码、测试或已生成文件",
            "create_file": "创建或全量重写翻译产物",
            "edit_file": "对已有文件做精确局部修改",
            "execute_command": "运行测试、示例或局部验证命令",
            "search_content": "搜索 API、符号或关键实现",
            "list_files": "查看当前 workspace 文件结构",
            "reflect": "分析测试失败根因",
            "think": "模型内部规划",
            "finish": "标记当前层翻译完成",
        }
        return labels.get(tool, "其他工具行为")

    def _build_layer_report_section(self, layer: int, info: dict[str, Any]) -> list[str]:
        lines = [f"### Layer {layer}", ""]
        lines.extend([
            "#### 工具使用摘要",
            "",
            "| 工具 | 次数 | 主要用途 |",
            "| --- | ---: | --- |",
        ])
        tool_counts = info.get("tool_counts") or Counter()
        if tool_counts:
            for tool, count in tool_counts.most_common():
                lines.append(f"| `{tool}` | {count} | {self._tool_usage_label(tool)} |")
        else:
            lines.append("| - | 0 | 未检测到工具调用 |")

        lines.extend(["", "#### 文件写入", ""])
        file_writes = info.get("file_writes") or Counter()
        if file_writes:
            for path, count in file_writes.most_common(20):
                suffix = f"（{count} 次）" if count > 1 else ""
                lines.append(f"- `{path}`{suffix}")
        else:
            lines.append("- 未检测到文件写入。")

        notes: list[str] = []
        if info.get("new_test_count") == 0 and info.get("visible_test_count", 0) > 0:
            notes.append("本层没有新增测试文件，因此运行的是已可见测试的累计回归。")
        duplicates = [p for p, c in file_writes.items() if c > 1]
        if duplicates:
            notes.append(f"存在 {len(duplicates)} 个文件被重复写入，可能表示模型经历了多轮修正。")
        if notes:
            lines.extend(["", "#### 需要关注", ""])
            lines.extend(f"- {n}" for n in notes)
        lines.append("")
        return lines

    @staticmethod
    def _final_missing_items(stats: dict[str, Any]) -> list[dict[str, Any]]:
        checks = stats.get("completeness_checks") or []
        if not checks:
            return []
        final_checks = [c for c in checks if c.get("phase") == "final"]
        check = final_checks[-1] if final_checks else checks[-1]
        return check.get("missing") or []

    def _build_issue_rows(self, stats: dict[str, Any]) -> list[str]:
        rows = []
        for issue, count in stats["issues"].most_common():
            rows.append(f"| {issue} | {count} | 请结合 JSONL 原始日志定位具体上下文 |")
        duplicate_count = sum(1 for c in stats["file_writes"].values() if c > 1)
        if duplicate_count:
            rows.append(f"| 重复写入文件 | {duplicate_count} | 可能存在多轮修正或整文件覆盖，可关注效率 |")
        return rows

    def _build_human_conclusion(self, stats: dict[str, Any]) -> str:
        status = stats["final_status"]
        tests = stats["final_tests"]
        layers = len(stats["layers"])
        llm_calls = stats["event_counts"].get("llm_request", 0)
        tool_calls = sum(stats["tool_counts"].values())
        missing = self._final_missing_items(stats)
        if status == "成功":
            result = f"本次翻译成功完成，最终测试结果为 {tests}。"
        else:
            result = f"本次翻译未完全成功，最终状态为 {status}，测试结果为 {tests}。"
        completeness = (
            "完整性检查通过。"
            if not missing else f"完整性检查仍有 {len(missing)} 个缺失目标文件。"
        )
        return (
            f"{result}{completeness} 项目按 {layers} 个依赖层推进，累计调用 LLM {llm_calls} 次、"
            f"工具 {tool_calls} 次。报告中的重复写入、无效响应、完整性补齐和工具错误可作为后续效率优化重点；"
            f"完整细节仍保留在 `{self._path.name}` 中。"
        )

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
        if et == "completeness_check":
            payload = r.get("payload", {})
            status = "通过" if payload.get("passed") else "失败"
            return prefix + (
                f"完整性检查{status}：{payload.get('present_count')}/"
                f"{payload.get('expected_count')} 已生成，缺失 {payload.get('missing_count')}"
            )
        if et == "completeness_feedback_sent":
            payload = r.get("payload", {})
            return prefix + f"已发送完整性补齐反馈：缺失 {payload.get('missing_count')} 个文件"
        if et in {"round_start", "round_end", "layer_start", "layer_end", "run_start", "run_end", "feedback_sent", "conversation_error"}:
            return prefix + f"{et}"
        return ""

    # -- 资源管理 -------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._write_live_report()
            if self._file:
                self._file.close()
                self._file = None
        self.write_readable_outputs()

    def __del__(self) -> None:
        self.close()
