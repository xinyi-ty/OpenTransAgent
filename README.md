# OpenTransAgent

基于 OpenHands SDK 的仓库级代码翻译 Agent，通过"翻译 → 测试 → 反馈"闭环自动完成项目级代码翻译。

**当前支持：** C++ ↔ Python（双向）
**计划支持：** Java、Rust、Go、C#、JavaScript/TypeScript 等

---

## 快速开始

### 环境要求

| 依赖 | 版本要求 | 用途 |
|------|---------|------|
| Python | >= 3.12 | 运行环境 |
| Git | 任意 | 克隆依赖仓库 |
| uv（推荐）或 pip | 最新 | Python 包管理 |

**按目标语言需要安装的编译工具：**

| 目标语言 | 必需工具 | 验证命令 |
|---------|---------|---------|
| C++ | CMake + MinGW-w64 (g++) + Git Bash (make) | `cmake --version`、`g++ --version`、`make --version` |
| Python | 无需额外工具 | — |

> **提示：** 工具装好后确认在系统 PATH 中。如果不在 PATH 中，在 `.env` 中配置 `TOOLCHAIN_PATHS`。

### 1. 安装包管理工具 uv（推荐）

```bash
# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

> 也可用传统 `pip`，后续命令将 `uv` 替换为 `pip` 即可。

### 2. 克隆依赖仓库

```bash
# 1) OpenHands SDK（核心依赖）
git clone https://github.com/OpenHands/software-agent-sdk.git <sdk-path>

# 2) file_topo_sort（可选，用于文件依赖拓扑排序，推荐启用）
git clone https://github.com/Code2Graph/Code2Graph.git <topo-path>
```

> 克隆后需设置环境变量指向实际路径（见下方 **配置** 章节）。

### 3. 安装 Python 依赖

```bash
cd OpenTransAgent
uv sync
```

### 4. 配置

```bash
cp .env.template .env
```

编辑 `.env`：

```env
# ── LLM 配置 ──
LLM_MODEL=openai/qwen3.7-plus          # 格式：provider/model
LLM_API_KEY=sk-xxx                      # API 密钥
LLM_BASE_URL=https://api.example.com/v1 # API 地址（中转站或云服务）
LLM_TIMEOUT=120                         # 单次 LLM 调用超时（秒）
MAX_ITERATIONS=120                      # 每层最大重试轮数

# ── 外部依赖路径（仅在非默认位置时需要）──
# TOPO_SORT_PATH=D:\Code2Graph\file_topo_sort\topo_sort_files.py
# TOOLCHAIN_PATHS=C:\tools\mingw64\bin;C:\Program Files\CMake\bin
```

关键变量说明：

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `OPENHANDS_SDK_PATH` | SDK 源码目录路径 | 需手动设置（见 `config/sdk_path.py`） |
| `TOPO_SORT_PATH` | 拓扑排序脚本路径，**推荐设置** | 自动检测同级 `Code2Graph/` 目录 |
| `TOOLCHAIN_PATHS` | 编译工具链额外路径（分号分隔） | 自动检测 PATH + 常见安装位置 |

> **关于拓扑排序：** 虽然不是必需，但推荐启用。它按文件依赖分析翻译顺序，能显著减少第一轮编译错误的次数。
> 不启用时 Agent 也能完成翻译，只是可能需要额外几轮修复依赖错误。

### 5. 验证安装

```bash
# 确认 Python 版本
python --version          # 需 >= 3.12

# 确认依赖安装
uv run python -c "import openhands; print('SDK OK')"

# 确认编译工具（按目标语言需要）
where cmake               # C++ 目标需要
where g++                 # C++ 目标需要
where make                # C++ 目标需要
```

### 6. 运行

```bash
# C++ → Python 翻译
uv run python run.py \
    --source_path ./examples/cpp_project \
    --source_language cpp \
    --target_language python \
    --project_name my_project

# Python → C++ 翻译
uv run python run.py \
    --source_path ./examples/python_project \
    --source_language python \
    --target_language cpp \
    --project_name my_project
```

---

## 项目结构

```
OpenTransAgent/
├── run.py                       # 单项目翻译入口
├── run_batch.py                 # 批量翻译入口
├── .env.template                # 配置模板（复制为 .env 使用）
├── .env                         # 本地配置（已 gitignore）
├── pyproject.toml               # 项目配置与依赖（uv sync 安装）
│
├── agent/                       # Agent 决策层
│   ├── translation_agent.py     # ReActTranslationAgent
│   └── prompts.py               # System Prompt 构造
│
├── tools/                       # 工具层
│   ├── registry.py              # 工具注册表
│   ├── file_ops.py              # read_file / create_file
│   ├── shell.py                 # execute_command
│   ├── search.py                # search_content
│   ├── context_collector.py     # 5 种上下文收集工具
│   └── reflect.py               # 错误反思
│
├── workspace/                   # 工作空间管理
│   ├── manager.py               # 源码复制、依赖分层、结果提取
│   └── precheck.py              # 目标语言脚手架生成
│
├── config/                      # 配置层
│   ├── settings.py              # 配置读取（.env / 环境变量 / 命令行）
│   ├── sdk_path.py              # SDK 路径发现
│   └── languages.py             # 语言扩展名配置
│
├── analysis/                    # 测试分析
│   └── test_analyzer.py         # 多语言测试结果分析器
│
└── utils/                       # 日志工具
    └── logger.py                # 统一日志输出
```

---

## 架构说明

### 混合循环模式

```
外层: 按依赖层推进（run.py 控制）
       │
       ├─ Layer 0 ─── 内层循环:
       │   ├─ conv.run()       ← SDK 执行最多 30 步
       │   ├─ TestAnalyzer     ← 跑编译 + 测试
       │   ├─ 全部通过? → 解锁 Layer 1
       │   └─ 没通过?   → send_message(反馈) → 继续
       │
       ├─ Layer 1 ─── 内层循环:
       │   ├─ conv.run() / TestAnalyzer / 反馈
       │   └─ 全部通过? → 解锁 Layer 2
       │
       └─ ...
```

### 依赖层控制

`file_topo_sort` 分析文件 import/include 依赖 → `compute_layers()` 分层 → 物理隔离（staging + workspace）。
每层源文件在初始化时复制到 staging，按层逐步移动到 workspace。读取/创建文件不再需要工具层拦截——磁盘上不存在就是不存在。

### 自定义工具（10 种）

| 工具 | 文件 | 功能 |
|------|------|------|
| `read_file` | `tools/file_ops.py` | 读取文件内容 |
| `create_file` | `tools/file_ops.py` | 创建 / 写入文件 |
| `execute_command` | `tools/shell.py` | 执行 shell 命令 |
| `search_content` | `tools/search.py` | 搜索文件关键词 |
| `get_source_class_info` | `tools/context_collector.py` | 提取源类的字段和方法签名 |
| `get_target_class_info` | `tools/context_collector.py` | 提取目标类的字段和方法签名 |
| `find_target_imports` | `tools/context_collector.py` | 获取文件 import / #include |
| `find_target_class` | `tools/context_collector.py` | 搜索类定义位置 |
| `find_target_method` | `tools/context_collector.py` | 搜索方法定义位置 |
| `reflect` | `tools/reflect.py` | 反思错误根因 |

---

## 配置说明

### 配置文件路径

可能因部署环境不同而需要修改的路径，统一通过环境变量配置：

| 环境变量 | 说明 | 配置位置 |
|----------|------|---------|
| `OPENHANDS_SDK_PATH` | SDK 源码目录路径 | `config/sdk_path.py` |
| `TOPO_SORT_PATH` | 拓扑排序脚本路径 | `workspace/manager.py` |
| `TOOLCHAIN_PATHS` | 编译工具链额外路径（分号分隔） | `config/settings.py` |

### LLM 配置优先级

命令行参数 > `.env` 文件 > 环境变量 > 默认值

### 退出码

| 退出码 | 含义 |
|:------:|------|
| 0 | 翻译成功，全部测试通过 |
| 1 | 翻译完成但部分测试未通过 |
| 2 | Agent 卡死 |
| 3 | 运行时异常 |

---

## 参数说明

### run.py

| 参数 | 必填 | 说明 |
|------|------|------|
| `--source_language` | ✅ | 源语言（cpp / python） |
| `--target_language` | ✅ | 目标语言（python / cpp） |
| `--source_path` | ✅ | 源码目录路径 |
| `--project_name` | 否 | 项目名称（默认取 source_path 上级目录名） |
| `--target_path` | 否 | 输出目录（默认 workspace/项目名） |
| `--target_project_path` | 否 | 预构建测试目录 |
| `--llm_model` | 否 | 覆盖 .env 中的模型 |
| `--max_iterations` | 否 | 最大外循环次数（默认 .env → 120） |
| `--persistence_dir` | 否 | 对话持久化目录（支持断点续传） |
| `--no-topo-sort` | 否 | 跳过文件依赖分析 |

### run_batch.py

| 参数 | 必填 | 说明 |
|------|------|------|
| `--source_root` | ✅ | 源项目根目录（包含多个子目录） |
| `--target_root` | ✅ | 翻译结果输出根目录 |
| `--source_language` | 否 | 默认 cpp |
| `--target_language` | 否 | 默认 python |
| `--max_projects` | 否 | 限制翻译项目数（0=全部） |
| `--timeout_per_project` | 否 | 单个项目超时秒数（默认 600） |
| `--resume` | 否 | 跳过已翻译的项目 |
| `--no-topo-sort` | 否 | 跳过文件依赖分析 |
| `--target_project_root` | 否 | 预构建测试项目根目录 |

---

## 常见问题

### 编译工具找不到

```bash
# 检查工具是否安装
where cmake g++ make

# 如果工具已安装但不在 PATH，在 .env 中配置：
TOOLCHAIN_PATHS=C:\tools\mingw64\bin;C:\Program Files\CMake\bin
```

### 拓扑排序脚本找不到

```bash
# 确认 Code2Graph 已克隆，并设置环境变量
set TOPO_SORT_PATH=D:\Code2Graph\file_topo_sort\topo_sort_files.py
```

或者将 `Code2Graph` 克隆到 OpenTransAgent 的同级目录下，脚本会自动检测。

### 翻译到一半中断了

```bash
# 设置 persistence_dir 启用断点续传
python run.py \
    --source_path ./project \
    --source_language cpp \
    --target_language python \
    --persistence_dir ./session
```

---

## 扩展新的语言对

计划支持的语言对：

- [ ] Java ↔ Python
- [ ] Rust ↔ Python
- [ ] Go ↔ Python
- [ ] C# ↔ Python
- [ ] JavaScript/TypeScript ↔ Python

添加新语言对需要修改：

| 文件 | 改动内容 |
|------|---------|
| `config/router.py` | 注册新语言对路由（`TranslationRoute`） |
| `config/languages.py` | 添加源/目标文件扩展名 |
| `workspace/precheck.py` | 添加目标语言脚手架生成函数 |
| `tools/context_collector.py` | 扩展搜索支持的文件后缀 |
| `agent/prompts.py` | 翻译指引（可选） |

---

## 相关项目

- [RepoTransBench](https://github.com/RepoTransBench) — 仓库级代码翻译基准
- [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) — 底层 Agent SDK
- [Code2Graph](https://github.com/Code2Graph) — 文件依赖拓扑排序工具
