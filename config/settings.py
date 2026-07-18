"""从 .env 和环境变量读取配置。"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def get_llm_config(args=None):
    """获取 LLM 配置。优先级：命令行 > .env > 默认值。"""
    model = (args.llm_model if args else None) or os.environ.get("LLM_MODEL", "")
    api_key = (args.llm_api_key if args else None) or os.environ.get("LLM_API_KEY", "")
    base_url = (args.llm_base_url if args else None) or os.environ.get("LLM_BASE_URL") or None
    timeout = (args.llm_timeout if args else None) or int(os.environ.get("LLM_TIMEOUT", 120))
    return model, api_key, base_url, timeout


def get_max_iterations(args=None):
    """获取最大迭代次数。"""
    if args and getattr(args, 'max_iterations', None):
        return args.max_iterations
    return int(os.environ.get("MAX_ITERATIONS", 120))
