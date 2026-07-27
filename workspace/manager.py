"""工作空间管理：创建目录、复制源码、提取结果、清理。"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from config.languages import get_target_extensions
from utils.logger import logger


def prepare_source_workspace(target_path: str, source_path: str,
                             target_project_path: str | None = None,
                             target_language: str = "python") -> tuple[str, str]:
    """准备源码和工作区。

    staging (target_path/.source_staging): 完整源码副本（LLM 不可见，供按层复制）
    workspace (target_path/.source):      工作区，翻译和测试都在这里

    如果提供了 target_project_path（数据集的预构建测试目录），
    只将其基础设施文件（run_tests.sh、project_structure.txt）复制到工作区，
    测试 .cpp 文件会通过 assign_tests_to_layers 按层分配。

    返回: (staging_path, workspace_path)
    """
    os.makedirs(target_path, exist_ok=True)
    workspace_path = str(Path(target_path) / ".source")
    staging_path = str(Path(target_path) / ".source_staging")
    src = Path(source_path).resolve()

    for d in (workspace_path, staging_path):
        if Path(d).exists():
            _force_rmtree(d)

    # 全部源码 → staging（LLM 不可见，按层逐步加入 workspace）
    shutil.copytree(src, staging_path)

    # 空工作区，创建后填充内容
    Path(workspace_path).mkdir(parents=True)

    # 预构建基础设施文件 → 直接放入 workspace（测试文件由 assign_tests_to_layers 管理）
    if target_project_path:
        tgt = Path(target_project_path).resolve()
        if tgt.exists():
            for item in tgt.iterdir():
                if item.is_dir():
                    continue
                if item.suffix.lower() not in (".cpp", ".py"):
                    dst = Path(workspace_path) / item.name
                    shutil.copy2(str(item), str(dst))
            logger.info(f"  [Precheck] Target project files copied to workspace: {tgt}")

    return staging_path, workspace_path


def assign_tests_to_layers(target_project_path: str,
                           source_layers: list[list[str]]) -> list[list[str]]:
    """将测试文件分配到它们依赖的源文件所在层。

    解析测试文件的 #include（C++）或 import（Python）语句，
    找出所有依赖的文件，分配到其所属的最高层。

    返回: list[list[str]] — 每层需要复制的测试文件列表
    """
    tgt = Path(target_project_path)
    if not tgt.exists():
        return []

    # 构建源文件 stem → 层映射
    stem_to_layer: dict[str, int] = {}
    for layer_idx, layer in enumerate(source_layers):
        for src_path in layer:
            stem_to_layer[Path(src_path).stem] = layer_idx

    def _get_dep_stems(content: str, ext: str) -> set[str]:
        """从文件内容中提取所有依赖的源文件 stem。"""
        stems: set[str] = set()
        if ext == ".cpp":
            # C++: #include "header.h"
            for m in re.finditer(r'#include\s+"([^"]+)"', content):
                stems.add(Path(m.group(1)).stem)
        elif ext == ".py":
            # Python: import module / from module import name
            for m in re.finditer(
                r'^(?:from\s+([.\w]+)\s+import|import\s+(\w+(?:\.\w+)*))',
                content, re.MULTILINE,
            ):
                mod = m.group(1) or m.group(2)
                # 剥离相对导入前缀（from .module → module）
                mod = mod.lstrip(".")
                # 取最末段（from BeastHttp.base.cb → cb）
                stems.add(mod.split(".")[-1])
        return stems

    test_layers: list[list[str]] = [[] for _ in source_layers]
    test_patterns = ("*.cpp", "*.py")

    for pattern in test_patterns:
        for test_file in sorted(tgt.rglob(pattern)):
            max_layer = 0
            try:
                content = test_file.read_text(encoding="utf-8", errors="replace")
                for dep_stem in _get_dep_stems(content, test_file.suffix.lower()):
                    layer = stem_to_layer.get(dep_stem)
                    if layer is not None:
                        max_layer = max(max_layer, layer)
            except Exception:
                pass
            rel = test_file.relative_to(tgt).as_posix()
            test_layers[max_layer].append(rel)

    return test_layers


def copy_test_layer(test_files: list[str],
                    target_project_path: str,
                    workspace_path: str) -> None:
    """将指定层的测试文件从 target_project 复制到工作区。"""
    if not test_files or not target_project_path:
        return
    tgt = Path(target_project_path)
    ws = Path(workspace_path)
    for rel in test_files:
        src = tgt / rel
        if src.exists():
            dst = ws / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))


def copy_source_files(layer_files: list[str],
                      staging_path: str,
                      workspace_path: str) -> None:
    """将指定层的源文件从 staging 复制到工作区（仅源文件，首次用 copy_workspace_files）。"""
    staging = Path(staging_path)
    workspace = Path(workspace_path)
    for rel_path in layer_files:
        src = staging / rel_path
        if src.exists():
            dst = workspace / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def copy_workspace_files(layer_files: list[str],
                        all_source_files: set[str],
                        staging_path: str,
                        workspace_path: str) -> None:
    """初始化工作区文件。

    复制内容:
    1. 当前层源文件（layer_files）
    2. 所有非源码文件（测试脚本、构建配置、Makefile 等）

    "非源码文件" 是指 staging 中存在但不在 all_source_files 中的文件，
    这些文件是测试运行的基础设施，必须从第 0 层起就存在于工作区。
    """
    staging = Path(staging_path)
    workspace = Path(workspace_path)
    copied: set[str] = set()

    def _copy(rel: str):
        if rel in copied:
            return
        src = staging / rel
        if not src.exists() or src.is_dir():
            return
        dst = workspace / rel
        if dst.exists():
            return  # 不覆盖已存在的文件（target_project 的优先）
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.add(rel)

    # 1) 当前层源文件
    for rel_path in layer_files:
        _copy(rel_path)

    # 2) staging 中的非源码文件（只复制翻译/测试必需的基础设施）
    _INFRA_NAMES = {
        "run_tests.sh", "run_public_tests.sh",
        "CMakeLists.txt", "Makefile",
        "requirements.txt", "setup.py", "setup.cfg", "pyproject.toml",
        "pytest.ini", "tox.ini", "conftest.py",
        "Cargo.toml", "go.mod",
        "package.json",
    }
    for src_file in staging.rglob("*"):
        if src_file.is_dir():
            continue
        rel = str(src_file.relative_to(staging).as_posix())
        if rel not in all_source_files and Path(rel).name in _INFRA_NAMES:
            _copy(rel)


# 需跳过的文件/目录（测试代码和预构建脚手架不属于翻译产物）
_SKIP_EXTRACT_PARTS = {"test", "tests", "public_test", "public_tests", "spec", "specs",
                       "conftest.py", "run_tests.sh", "run_public_tests.sh",
                       "__init__.py",  # precheck 生成的占位，非翻译产物
                       "build", "_deps", "CMakeFiles", ".persist"}


def _should_skip_extract(rel_path: str) -> bool:
    """检查路径是否包含应跳过的测试/脚手架目录或文件。"""
    parts = set(Path(rel_path).parts)
    if parts & _SKIP_EXTRACT_PARTS:
        return True
    return Path(rel_path).name in _SKIP_EXTRACT_PARTS


def extract_results(source_workspace: str, target_path: str, target_language: str = "python") -> list[str]:
    """根据目标语言提取对应扩展名的文件到目标目录，保留子目录结构。
    自动跳过测试文件（tests/、public_tests/ 等）和脚手架文件（__init__.py、conftest.py）。
    """
    exts = get_target_extensions(target_language)
    moved = []
    ws = Path(source_workspace)
    for ext in exts:
        for src in ws.rglob(f"*{ext}"):
            if not src.is_file():
                continue
            rel = src.relative_to(ws)
            if _should_skip_extract(str(rel)):
                continue
            dst = Path(target_path) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            moved.append(str(rel))
    return moved


def _force_rmtree(path: str):
    """递归删除目录，遇到只读文件时先解除只读属性。"""
    for root, dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            try:
                os.chmod(p, stat.S_IWRITE)
            except Exception:
                pass
    shutil.rmtree(path, ignore_errors=True)


def cleanup(source_workspace: str):
    """清理工作区，同时删除同级的 staging 目录。"""
    _force_rmtree(source_workspace)
    staging = str(Path(source_workspace).parent / ".source_staging")
    _force_rmtree(staging)


# 指向外部项目 file_topo_sort 的拓扑排序脚本路径。
# 优先级：环境变量 TOPO_SORT_PATH > 同级目录自动检测 > None（跳过）。
_script_env = os.environ.get("TOPO_SORT_PATH", "")
if _script_env:
    TOPO_SORT_SCRIPT: Path | None = Path(_script_env)
else:
    _auto = Path(__file__).resolve().parent.parent.parent / "Code2Graph" / "file_topo_sort" / "topo_sort_files.py"
    TOPO_SORT_SCRIPT = _auto if _auto.exists() else None


def get_topo_sort_order(source_path: str, source_language: str) -> dict | None:
    """调用 topo_sort_files.py 分析项目文件依赖，返回建议的翻译顺序。

    失败或语言不支持时返回 None，不中断翻译流程。
    """
    lang_map = {"python": "python", "cpp": "cpp", "c": "cpp", "c++": "cpp"}
    lang = lang_map.get(source_language.lower())
    if lang is None or not TOPO_SORT_SCRIPT or not TOPO_SORT_SCRIPT.is_file():
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
    """控制翻译层的推进节奏。

    同时追踪每个源文件对应的目标文件名（.h/.cpp），
    用于 create_file 的跨层检查。
    """

    def __init__(self, layers: list[list[str]] | None = None):
        self.layers = layers or []
        self.current = 0
        # 文件名（不含后缀）→ 层号，用于识别 core.h 属于 core.py 的层
        self._stem_map: dict[str, int] = {}
        for i, layer in enumerate(self.layers):
            for f in layer:
                self._stem_map[Path(f).stem] = i

    @property
    def active(self) -> bool:
        return bool(self.layers)

    def is_unlocked(self, filepath: str) -> bool:
        """检查文件是否在当前层或之前层。

        同时识别 .h/.cpp 目标文件——operations.h 的 stem 是 operations，
        会匹配到 operations.py 所在的层。
        """
        if not self.active:
            return True
        stem = Path(filepath).stem
        idx = self._stem_map.get(stem)
        if idx is None:
            return True  # 非项目文件始终可访问
        return idx <= self.current

    def advance(self) -> bool:
        """解锁下一层。返回 False 表示已经是最后一层。"""
        if self.current < len(self.layers) - 1:
            self.current += 1
            return True
        return False

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
