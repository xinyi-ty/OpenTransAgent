"""工作空间管理：创建目录、复制源码、提取结果、清理。"""

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from config.languages import get_target_extensions


def prepare_source_workspace(target_path: str, source_path: str) -> str:
    """在 target_path/.source 下创建源码副本（全部复制）。"""
    os.makedirs(target_path, exist_ok=True)
    source_ws = str(Path(target_path) / ".source")
    src = Path(source_path).resolve()
    dst = Path(source_ws)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return source_ws


def extract_results(source_workspace: str, target_path: str, target_language: str = "python") -> list[str]:
    """根据目标语言提取对应扩展名的文件到目标目录。"""
    exts = get_target_extensions(target_language)
    moved = []
    for ext in exts:
        for f in glob.glob(str(Path(source_workspace) / f"**/*{ext}"), recursive=True):
            src = Path(f)
            dst = Path(target_path) / src.name
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            moved.append(src.name)
    return moved


def cleanup(source_workspace: str):
    shutil.rmtree(source_workspace, ignore_errors=True)


# 指向外部项目 file_topo_sort 的拓扑排序脚本路径。
# 如需自定义路径，设置环境变量 TOPO_SORT_PATH。
# Windows 默认: D:\Code2Graph\file_topo_sort\topo_sort_files.py
# Linux/macOS: 需设置 TOPO_SORT_PATH 指向 clone 后的脚本
TOPO_SORT_SCRIPT = Path(os.environ.get(
    "TOPO_SORT_PATH",
    r"D:\Code2Graph\file_topo_sort\topo_sort_files.py",
))


def get_topo_sort_order(source_path: str, source_language: str) -> dict | None:
    """调用 topo_sort_files.py 分析项目文件依赖，返回建议的翻译顺序。

    返回 dict 结构（--format json 输出）：
        translation_order: list[str]   — 建议的翻译顺序（依赖优先）
        dependencies: list[dict]       — 内部文件依赖
        external_dependencies: list[dict] — 外部依赖（标准库/三方包）
        cycles: list[list[str]]        — 检测到的循环依赖
        broken_edges: list[dict]       — 为解环断开的边

    失败或语言不支持时返回 None，不中断翻译流程。
    """
    lang_map = {"python": "python", "cpp": "cpp", "c": "cpp", "c++": "cpp"}
    lang = lang_map.get(source_language.lower())
    if lang is None or not TOPO_SORT_SCRIPT.is_file():
        return None

    try:
        result = subprocess.run(
            [sys.executable, str(TOPO_SORT_SCRIPT),
             "--source", source_path,
             "--lang", lang,
             "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return {
            "translation_order": data.get("translation_order", []),
            "dependencies": data.get("dependencies", []),
            "external_dependencies": data.get("external_dependencies", []),
            "cycles": data.get("cycles", []),
            "broken_edges": data.get("broken_edges", []),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def compute_layers(translation_order: list[str],
                   dependencies: list[dict]) -> list[list[str]]:
    """将扁平翻译顺序转换为依赖分层。

    返回: [[Layer0文件], [Layer1文件], ...]
        Layer 0: 没有项目内依赖的文件（先翻译）
        Layer N: 只依赖前面各层的文件
    """
    all_files = set(translation_order)
    deps: dict[str, set[str]] = {f: set() for f in translation_order}
    for d in dependencies:
        if d["file"] in all_files and d["depends_on"] in all_files:
            deps.setdefault(d["file"], set()).add(d["depends_on"])

    layers = []
    remaining = set(translation_order)
    while remaining:
        layer = {f for f in remaining if not (deps.get(f, set()) & remaining)}
        if not layer:
            layer = remaining.copy()
        layers.append(sorted(layer))
        remaining -= layer
    return layers


class LayerController:
    """控制文件的可访问范围，基于依赖分层。

    用法:
        ctrl = LayerController(layers)
        ctrl.is_unlocked("utils.hpp")  → True (Layer 0)
        ctrl.advance()                  → 解锁 Layer 1
        ctrl.is_unlocked("parser.hpp") → True
    """

    def __init__(self, layers: list[list[str]] | None = None):
        self.layers = layers or []
        self.current = 0
        self._file_map: dict[str, int] = {}
        for i, layer in enumerate(self.layers):
            for f in layer:
                self._file_map[f] = i

    @property
    def active(self) -> bool:
        return bool(self.layers)

    def is_unlocked(self, filepath: str) -> bool:
        """文件是否在当前层或之前层（即是否可读）。"""
        if not self.active:
            return True
        idx = self._file_map.get(filepath)
        if idx is None:
            return True  # 非项目文件始终可读
        return idx <= self.current

    def advance(self) -> bool:
        """解锁下一层。返回 False 表示已经是最后一层。"""
        if self.current < len(self.layers) - 1:
            self.current += 1
            return True
        return False

    @property
    def current_files(self) -> list[str]:
        return self.layers[self.current] if self.active else []

    @property
    def total_layers(self) -> int:
        return len(self.layers)


def get_project_tree(project_path: str, max_depth: int = 3) -> str:
    try:
        r = subprocess.run(["tree", str(project_path), "-L", str(max_depth)],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    lines = []
    for i, f in enumerate(Path(project_path).rglob("*")):
        if i >= 50:
            lines.append("...")
            break
        try:
            lines.append(str(f.relative_to(Path(project_path))))
        except ValueError:
            continue
    return "\n".join(lines)
