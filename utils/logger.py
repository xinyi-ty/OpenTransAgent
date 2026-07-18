"""日志工具。提供结构化的翻译进度输出，屏蔽 SDK 底层日志。"""

import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path

# 项目日志器（只输出关键信息）
logger = logging.getLogger("opentrans")
logger.setLevel(logging.INFO)
logger.propagate = False  # 防止日志传播到根 logger，避免重复输出
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.handlers.clear()
logger.addHandler(_handler)


def suppress_sdk_logging():
    """屏蔽 SDK 和第三方库的 INFO 日志。"""

    class OpenhandsFilter(logging.Filter):
        def filter(self, record):
            # 屏蔽 openhands 子系统的 INFO/DEBUG，只保留 WARNING+
            if record.name.startswith("openhands"):
                return record.levelno >= logging.WARNING
            return True

    # 双重保险：设置层级级别 + 根过滤器
    logging.getLogger("openhands").setLevel(logging.WARNING)
    logging.getLogger().addFilter(OpenhandsFilter())

    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger().setLevel(logging.WARNING)

    # 屏蔽 LiteLLM 成本计算失败的警告（自定义模型 + 代理时常见）
    warnings.filterwarnings("ignore", message="Cost calculation failed")


def setup_log_dir(model: str, project_name: str,
                  source_language: str, target_language: str) -> Path:
    """创建并返回本次运行的日志目录。
    目录结构: logs/{model}/{project}_{source}_to_{target}_{timestamp}/
    """
    model_safe = model.replace("/", "_").replace(":", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path("logs") / model_safe / \
        f"{project_name}_{source_language}_to_{target_language}_{timestamp}"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def save_prompt_to(log_dir: Path, prompt_text: str) -> Path:
    """将 system prompt 写入日志目录。"""
    path = log_dir / "system_prompt.txt"
    path.write_text(prompt_text, encoding="utf-8")
    return path
