"""SDK 路径引导。优先使用已安装的 pip 包。"""

import sys


def ensure_openhands_importable():
    """确保 openhands SDK 可导入。优先用 pip 安装的包。"""
    try:
        import openhands.sdk  # noqa: F401
        return
    except ImportError:
        pass

    print("错误：找不到 OpenHands SDK。请运行：")
    print("  pip install openhands-sdk openhands-tools")
    sys.exit(1)
