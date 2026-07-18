"""SDK 路径引导。优先使用已安装的 pip 包，失败时搜索本地 SDK 路径。

可配置的环境变量：
    OPENHANDS_SDK_PATH : SDK 源码目录的路径
                         默认搜索顺序: pip 包 → 上级目录 → D:\\software-agent-sdk
"""

import os
import sys


def ensure_openhands_importable():
    try:
        import openhands.sdk  # noqa: F401
        return
    except ImportError:
        pass

    # SDK 搜索路径列表（按优先级排序）
    # 如需自定义路径，设置环境变量 OPENHANDS_SDK_PATH
    _sdk_env = os.environ.get("OPENHANDS_SDK_PATH")
    _search_paths = [
        _sdk_env,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "software-agent-sdk"),
        r"D:\software-agent-sdk",  # 默认本地路径，按需修改
    ]
    for _root in _search_paths:
        if not _root:
            continue
        _root = os.path.abspath(_root)
        if os.path.isdir(_root):
            for _pkg in ["openhands-sdk", "openhands-tools"]:
                _path = os.path.join(_root, _pkg)
                if os.path.isdir(_path) and _path not in sys.path:
                    sys.path.insert(0, _path)
            return

    print("错误：找不到 OpenHands SDK。请运行：")
    print("  pip install openhands-sdk openhands-tools")
    print("或通过环境变量 OPENHANDS_SDK_PATH 指定 SDK 路径")
    sys.exit(1)
