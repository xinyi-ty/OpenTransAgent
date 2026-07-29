# OpenTransAgent

基于 OpenHands SDK 的仓库级代码翻译 Agent，通过“翻译 → 测试 → 反馈 → 反思修复”的闭环自动完成项目级代码翻译。

**当前支持：** C++ ↔ Python（双向）

---

## 核心能力

- **OpenHands SDK Agent 循环**：使用 `Conversation.run()` 驱动 ReAct 翻译步骤。
- **依赖分层翻译**：可接入 Code2Graph/file_topo_sort，按文件依赖分层推进。
- **双目录物理隔离**：完整源码放在 `.source_staging`，LLM 只看到当前层解锁后的 `.source`。
- **测试反馈闭环**：每轮 `finish` 后自动运行测试分析，把编译/测试失败反馈给 LLM。
- **翻译完整性守卫**：每次 `finish` 后、测试前检查当前累计层目标文件是否完整，缺失时结构化提醒补齐并限制重试，避免跳文件和空转。
- **反思纠错工具**：失败后可提示并调用 `reflect` 分析根因。
- **功能调用追踪日志**：单独生成 JSONL trace，记录 LLM 请求/响应、工具调用、Observation、外层轮次事件，便于排查空转。
- **多语言配置基础设施**：已具备语言扩展名、脚手架、测试分析等扩展点。

---

## 快速开始

### 环境要求

| 依赖 | 版本要求 | 用途 |
| ------ | -------- | ------ |
| Python | >= 3.12 | 运行环境 |
| uv（推荐）或 pip | 最新 | Python 包管理 |
| OpenHands SDK | 通过 Python 依赖安装 | Agent / Conversation 运行时 |

**按目标语言需要安装的编译工具：**

| 目标语言 | 必需工具 | 验证命令 |
| ------- | ------- | ------- |
| C++ | CMake + MinGW-w64/g++ + Git Bash/make | `cmake --version`、`g++ --version`、`make --version` |
| Python | pytest（开发依赖已声明） | `python -m pytest --version` |

> 工具装好后请确认在系统 PATH 中。如果不在 PATH 中，可在 `.env` 中配置 `TOOLCHAIN_PATHS`。

### 1. 安装 uv（推荐）

```bash
# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 安装 Python 依赖

```bash
cd OpenTransAgent
uv sync
```

如使用 pip：

```bash
python -m pip install -e . pytest
```

### 3. 配置 `.env`

```bash
cp .env.template .env
```

编辑 `.env`，至少填写：

```env
LLM_MODEL=openai/your-model
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.example.com/v1
```

常用配置示例：

```env
LLM_TIMEOUT=120
MAX_ITERATIONS=120
STEPS_PER_ROUND=50
ROUND_TIMEOUT=1800
REFLECTION_ENABLED=1
TRACE_LOG_ENABLED=1
```

### 4. 可选：启用拓扑排序

```env
TOPO_SORT_PATH=D:\Code2Graph\file_topo_sort\topo_sort_files.py
```

如果未配置，程序会尝试自动检测 OpenTransAgent 同级目录下的 `Code2Graph/file_topo_sort/topo_sort_files.py`；检测失败则跳过拓扑排序，不中断翻译流程。

### 5. 验证安装

```bash
python --version
uv run python -c "import openhands; print('SDK OK')"
uv run python -m pytest -q
```

---

## 运行示例

```bash
# C++ → Python
uv run python run.py \
    --source_path ./examples/cpp_project \
    --source_language cpp \
    --target_language python \
    --project_name my_project

# Python → C++
uv run python run.py \
    --source_path ./examples/python_project \
    --source_language python \
    --target_language cpp \
    --project_name my_project
```

输出默认写入：

```text
workspace/<project_name>/
```

日志默认写入：

```text
logs/<model>/<project>_<source>_to_<target>_<timestamp>/
├── system_prompt.txt
└── translation_trace.jsonl
```

---

## 配置说明

### 配置优先级

命令行参数 > `.env` / 环境变量 > 默认值。

### LLM 配置

| 环境变量 | CLI 参数 | 默认值 | 说明 |
| ---------- | ---------- | ------ | ------ |
| `LLM_MODEL` | `--llm_model` | 空 | 模型名，如 `openai/deepseek-v4-flash` |
| `LLM_API_KEY` | `--llm_api_key` | 空 | API 密钥 |
| `LLM_BASE_URL` | `--llm_base_url` | 空 | OpenAI-compatible base URL |
| `LLM_TIMEOUT` | `--llm_timeout` | `120` | 单次 LLM 调用超时秒数 |

### 翻译循环配置

| 环境变量 | CLI 参数 | 默认值 | 说明 |
| ---------- | ---------- | ------ | ------ |
| `MAX_ITERATIONS` | `--max_iterations` | `120` | 每层最大外循环轮数 |
| `STEPS_PER_ROUND` | `--steps_per_round` | `50` | 每轮 `Conversation.run()` 最多执行的 SDK Agent step 数 |
| `ROUND_TIMEOUT` | `--round_timeout` | `1800` | 单轮 `Conversation.run()` 超时秒数 |
| `INVALID_RESPONSE_LIMIT` | `--invalid_response_limit` | `3` | 连续无效 LLM 响应上限 |
| `RUNTIME_ERROR_LIMIT` | `--runtime_error_limit` | `3` | 连续可恢复运行时错误上限 |
| `COMPLETENESS_RETRY_LIMIT` | `--completeness_retry_limit` | `3` | 翻译完整性检查失败后的连续补齐重试上限 |
| `REFLECTION_ENABLED` | `--no-reflection` | `1` | 是否启用失败后的 reflect 指引和工具 |

### 工具与测试配置

| 环境变量 | CLI 参数 | 默认值 | 说明 |
| ---------- | ---------- | ------ | ------ |
| `TOOL_COMMAND_TIMEOUT` | `--tool_command_timeout` | `60` | `execute_command` 默认超时秒数 |
| `SEARCH_MAX_RESULTS` | `--search_max_results` | `10` | `search_content` 默认最大结果数 |
| `TEST_TIMEOUT` | `--test_timeout` | `300` | 测试分析器超时秒数 |
| `TEST_RAW_OUTPUT_LIMIT` | `--test_raw_output_limit` | `5000` | 反馈给 LLM 的测试原始输出上限；`0` 表示不截断 |

### 追踪日志配置

| 环境变量 | CLI 参数 | 默认值 | 说明 |
| ---------- | ---------- | ------ | ------ |
| `TRACE_LOG_ENABLED` | `--no_trace_log` | `1` | 是否生成 `translation_trace.jsonl` |
| `TRACE_LOG_MAX_FIELD_CHARS` | `--trace_log_max_field_chars` | `20000` | 单字段最大记录字符数 |
| `TRACE_LOG_REDACT_SECRETS` | `--no_trace_redact` | `1` | 是否对密钥/Token 做 redaction |

### 路径配置

| 环境变量 | 默认值 | 说明 |
| ---------- | ------ | ------ |
| `TOPO_SORT_PATH` | 自动检测同级 `Code2Graph/` | Code2Graph 拓扑排序脚本路径 |
| `TOOLCHAIN_PATHS` | 空 | 编译工具链额外路径，分号分隔 |

---

## `run.py` 参数

| 参数 | 必填 | 说明 |
| ------ | ------ | ------ |
| `--source_language` | ✅ | 源语言，当前路由支持 `cpp` / `python` |
| `--target_language` | ✅ | 目标语言，当前路由支持 `python` / `cpp` |
| `--source_path` | ✅ | 源码目录路径 |
| `--project_name` | 否 | 项目名称，默认取 `source_path` 上级目录名 |
| `--target_path` | 否 | 输出目录根路径，默认 `workspace/项目名` |
| `--target_project_path` | 否 | 预构建测试目录，默认尝试检测 `source_path` 同级 `target_project` |
| `--workspace_dir` | 否 | 默认工作区根目录 |
| `--persistence_dir` | 否 | SDK Conversation 持久化目录 |
| `--no-topo-sort` | 否 | 跳过文件依赖分析 |
| `--no-reflection` | 否 | 禁用失败后的 reflect 指引和工具 |
| `--no_trace_log` | 否 | 禁用功能调用追踪日志 |
| `--no_trace_redact` | 否 | 禁用追踪日志敏感信息 redaction |

其余 LLM、循环、工具、测试参数见上方配置表，均支持 CLI 覆盖。

---

## 项目结构

```text
OpenTransAgent/
├── run.py                       # 单项目翻译入口
├── run_batch.py                 # 批量翻译入口
├── .env.template                # 配置模板（复制为 .env 使用）
├── pyproject.toml               # 项目配置与依赖
├── uv.lock                      # uv 锁文件
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
│   ├── context_collector.py     # 上下文收集工具
│   └── reflect.py               # 错误反思
│
├── workspace/                   # 工作空间管理
│   ├── manager.py               # 源码复制、依赖分层、结果提取
│   └── precheck.py              # 目标语言脚手架生成
│
├── config/                      # 配置层
│   ├── settings.py              # 配置读取（.env / 环境变量 / 命令行）
│   ├── router.py                # 支持的语言对路由
│   └── languages.py             # 语言扩展名配置
│
├── analysis/                    # 测试分析
│   └── test_analyzer.py         # 多语言测试结果分析器
│
├── utils/                       # 日志工具
│   └── logger.py                # 终端日志与 trace 日志
│
└── tests/                       # 单元测试
```

---

## 架构说明

### 混合循环模式

```text
外层：run.py 按依赖层推进
      │
      ├─ Layer 0
      │   ├─ Conversation.run()  ← SDK 内层 step 循环
      │   ├─ TestAnalyzer        ← 编译 / 测试 / 解析结果
      │   ├─ 通过? → 解锁下一层
      │   └─ 失败? → send_message(测试反馈) → 继续修复
      │
      ├─ Layer 1
      │   └─ 同上
      │
      └─ ...
```

### 双目录物理隔离

```text
workspace/<project>/
├── .source_staging/   # 完整源项目副本，LLM 不可见
└── .source/           # 当前层可见 workspace，LLM 工具只操作这里
```

分层推进时，只有当前层及之前层的源码会从 `.source_staging` 复制到 `.source`，避免模型提前读取未解锁依赖。

### 功能调用追踪日志

终端日志只展示翻译进度；详细排障信息写入 `translation_trace.jsonl`，包括：

- run/layer/round start/end
- LLM request/response 摘要
- tool call / observation 事件
- idle nudge
- test analysis start/result
- conversation runtime error

追踪日志默认开启，并会对常见密钥字段和 `sk-...` 模式做 redaction。

---

## 退出码

| 退出码 | 含义 |
| :----: | ------ |
| 0 | 翻译成功，全部测试通过 |
| 1 | 翻译完成但部分测试未通过，或无可验证测试 |
| 2 | Agent 卡住 / 超时 |
| 3 | 运行时异常 |

---

## 常见问题

### 编译工具找不到

```bash
where cmake
where g++
where make
```

如果工具已安装但不在 PATH：

```env
TOOLCHAIN_PATHS=C:\tools\mingw64\bin;C:\Program Files\CMake\bin
```

### 拓扑排序脚本找不到

```env
TOPO_SORT_PATH=D:\Code2Graph\file_topo_sort\topo_sort_files.py
```

或者将 `Code2Graph` 克隆到 OpenTransAgent 的同级目录下，程序会自动检测。

### 想排查模型空转或工具调用异常

查看本次运行日志目录下的：

```text
translation_trace.jsonl
```

如果日志太大，可调低：

```env
TRACE_LOG_MAX_FIELD_CHARS=8000
```

如需临时关闭：

```bash
uv run python run.py ... --no_trace_log
```

---

## 扩展新的语言对

添加新语言对通常需要修改：

| 文件 | 改动内容 |
| ------ | -------- |
| `config/router.py` | 注册新语言对路由 |
| `config/languages.py` | 添加源/目标文件扩展名 |
| `workspace/precheck.py` | 添加目标语言脚手架生成函数 |
| `analysis/test_analyzer.py` | 扩展测试框架检测和结果解析 |
| `tools/context_collector.py` | 扩展上下文收集的文件后缀 / 语法规则 |
| `agent/prompts.py` | 翻译指引（可选） |

---

## 相关项目

- [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) — 底层 Agent SDK
- [Code2Graph](https://github.com/Code2Graph) — 文件依赖拓扑排序工具
