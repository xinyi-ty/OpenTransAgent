# OpenTransAgent

基于 OpenHands SDK 的仓库级代码翻译 Agent。支持 C++ ↔ Python 双向翻译，通过外循环 + 内循环的混合模式实现"翻译 → 测试 → 反馈"闭环。

## 快速开始

### 环境要求

- Python 3.12+（OpenHands SDK 要求）

### 1. 克隆依赖仓库

```bash
# OpenHands SDK（核心依赖，可选：pip install openhands-sdk openhands-tools 注：在 Windows 下需 Rust 编译环境）
git clone https://github.com/OpenHands/software-agent-sdk.git

# file_topo_sort（可选，用于文件依赖分析）
git clone https://github.com/Code2Graph/Code2Graph.git
```

> **路径说明**：可在sdk_path.py、manager.py说明克隆路径，默认是D:\

### 2. 安装 Python 依赖

```bash
pip install python-dotenv
```

### 3. 配置 API 密钥

```bash
cp .env.template .env
```

编辑 `.env` 填入你的模型和密钥：

```env
LLM_MODEL=                 # 模型名，格式：provider/model，例：openai/qwen3.7-plus
LLM_API_KEY=               # API 密钥
LLM_BASE_URL=              # API 地址（中转站或云服务平台）
LLM_TIMEOUT=120            # LLM 请求超时秒数（单次 LLM 调用最长等待时间）
MAX_ITERATIONS=120         # 每层最大重试轮数（防止某层卡死，每轮最多 30 步）
```

### 4. 运行(暂时只支持cpp < -- > python )

```bash
# 单项目翻译
python run.py \
    --source_path \
    --target_path \
    --project_name my_project \
    --source_language cpp \
    --target_language python

# 批量翻译(max_projects 5 表示翻译前五个，为可选内容)
python run_batch.py \
    --source_root \
    --target_root \
    --source_language cpp \
    --target_language python \
    --max_projects 5
```

## 项目结构

```
OpenTransAgent/
├── run.py                       # 单项目翻译入口
├── run_batch.py                 # 批量翻译入口
├── .env.template                # 配置模板（复制为 .env 使用）
├── .env                         # 本地配置（已 gitignore）
├── requirements.txt             # Python 依赖（仅 python-dotenv）
│
├── agent/                       # Agent 决策层
│   ├── translation_agent.py     # ReActTranslationAgent，继承 SDK Agent
│   └── prompts.py               # System Prompt 构造
│
├── tools/                       # 工具层（每个文件一类工具）
│   ├── registry.py              # 工具注册表
│   ├── file_ops.py              # read_file / create_file（含层访问控制）
│   ├── shell.py                 # execute_command
│   ├── search.py                # search_content
│   ├── context_collector.py     # 5 种上下文收集工具
│   └── reflect.py               # 错误反思（占位）
│
├── workspace/                   # 工作空间管理
│   ├── manager.py               # 源码复制、依赖分层、结果提取、清理
│   └── precheck.py              # 目标语言脚手架生成（7 种语言）
│
├── config/                      # 配置层
│   ├── settings.py              # LLM 配置读取（.env / 环境变量 / 命令行）
│   ├── sdk_path.py              # SDK 路径发现
│   └── languages.py             # 语言扩展名配置
│
├── analysis/                    # 测试分析
│   └── test_analyzer.py         # 多语言测试结果分析器
│
└── utils/                       # 日志工具
    └── logger.py                # 统一日志输出、SDK 静音
```

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
       │   ├─ 此时 read_file 已开放 Layer 0+1
       │   ├─ conv.run() / TestAnalyzer / 反馈
       │   └─ 全部通过? → 解锁 Layer 2
       │
       └─ ...
```

### 依赖层控制

`file_topo_sort` 分析文件 import/include 依赖 → `compute_layers()` 分层 → `LayerController` 控制 read_file/create_file 的访问权限。LLM 无法读/写未解锁层的文件，从而强制翻译顺序。

### 自定义工具（10 种）

| 工具 | 文件 | 功能 |
|------|------|------|
| `read_file` | `tools/file_ops.py` | 读取文件内容（受 LayerController 约束）|
| `create_file` | `tools/file_ops.py` | 创建 / 写入文件（受 LayerController 约束）|
| `execute_command` | `tools/shell.py` | 执行 shell 命令 |
| `search_content` | `tools/search.py` | 搜索文件关键词 |
| `get_source_class_info` | `tools/context_collector.py` | 提取源类的字段和方法签名 |
| `get_target_class_info` | `tools/context_collector.py` | 提取目标类的字段和方法签名 |
| `find_target_imports` | `tools/context_collector.py` | 获取文件 import / #include |
| `find_target_class` | `tools/context_collector.py` | 搜索类定义位置 |
| `find_target_method` | `tools/context_collector.py` | 搜索方法定义位置 |
| `reflect` | `tools/reflect.py` | 反思错误根因 |

## 配置说明

### 配置文件路径

所有可能因部署环境不同而需要修改的路径，统一通过环境变量配置：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `OPENHANDS_SDK_PATH` | `D:\software-agent-sdk` | SDK 源码目录（定义在 `config/sdk_path.py`）|
| `TOPO_SORT_PATH` | `D:\Code2Graph\file_topo_sort\topo_sort_files.py` | 拓扑排序脚本路径（定义在 `workspace/manager.py`）|

### LLM 配置优先级

命令行参数 > `.env` 文件 > 环境变量 > 默认值

### 退出码

`run.py` 的退出码供 `run_batch.py` 和 CI 系统使用：

| 退出码 | 含义 |
|:------:|------|
| 0 | 翻译成功，全部测试通过 |
| 1 | 翻译完成但部分测试未通过 |
| 2 | Agent 卡死 |
| 3 | 运行时异常 |

## 参数说明

### run.py

| 参数 | 必填 | 说明 |
|------|------|------|
| `--project_name` | ✅ | 项目名称 |
| `--source_language` | ✅ | 源语言（cpp / python） |
| `--target_language` | ✅ | 目标语言（python / cpp） |
| `--source_path` | ✅ | 源码目录路径 |
| `--target_path` | 否 | 输出目录（默认 workspace/项目名）|
| `--llm_model` | 否 | 覆盖 .env 中的模型 |
| `--max_iterations` | 否 | 最大外循环次数（默认 .env → 120）|
| `--persistence_dir` | 否 | 对话持久化目录（支持断点续传）|
| `--no-topo-sort` | 否 | 跳过文件依赖分析 |

### run_batch.py(目前为待优化状态)

| 参数 | 必填 | 说明 |
|------|------|------|
| `--source_root` | ✅ | 源项目根目录（包含多个子目录）|
| `--target_root` | ✅ | 翻译结果输出根目录 |
| `--source_language` | 否 | 默认 cpp |
| `--target_language` | 否 | 默认 python |
| `--max_projects` | 否 | 限制翻译项目数（0=全部）|
| `--timeout_per_project` | 否 | 单个项目超时秒数（默认 600）|
| `--resume` | 否 | 跳过已翻译的项目 |
| `--no-topo-sort` | 否 | 跳过文件依赖分析 |

## 相关项目

- [RepoTransBench](https://github.com/RepoTransBench) — 仓库级代码翻译基准
- [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) — 底层 Agent SDK
- [Code2Graph](https://github.com/Code2Graph) — 文件依赖拓扑排序工具
