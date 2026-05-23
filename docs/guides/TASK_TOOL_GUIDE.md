# 通用 Task Tool 使用指南

> 版本: v2.0 | 更新: 2026-05-23
> 核心脚本: `scripts/trae_task_tool.py`, `scripts/trae_agent_worker.py`, `scripts/traecli_adapter.py`

## 1. 概述

通用 Task Tool 是 multi-agent-team 技能的子智能体调度核心，支持将任务分派给独立的子智能体进程并行执行。与 v1 版本（仅支持 LuaToCSharpConverter）不同，v2 版本是**通用任务调度器**，可以执行任何 AI 可理解的任务。

### 核心能力

| 能力 | 说明 |
|------|------|
| 双后端执行 | Worker 后端（可插拔 Handler）+ TraeCli 后端（直接调 AI） |
| 并行分派 | 多个子智能体同时运行，互不阻塞 |
| 上下文隔离 | 每个任务独立的上下文文件和结果文件 |
| 自动后端选择 | 根据任务类型自动推断最优执行后端 |
| 结果聚合 | 自动收集所有子智能体结果并生成汇总 |

### 架构图

```
TraeTaskTool (调度器)
    │
    ├── Worker 后端 ──→ trae_agent_worker.py
    │       │
    │       ├── TraeCliHandler (traecli exec → AI 执行)
    │       ├── LocalLuaToCSharpHandler (纯 Python 转换)
    │       └── ShellCommandHandler (shell 命令)
    │
    └── TraeCli 后端 ──→ traecli exec (直接调 AI)
```

## 2. 快速开始

### 2.1 环境要求

- Python 3.10+
- TraeCli 已安装（用于 traecli 后端）: `pip install -e tools/TraeCli/agent-harness`

### 2.2 最简用法

```python
from trae_task_tool import TraeTaskTool

tool = TraeTaskTool(backend="traecli")

# 分派单个任务
task_id = tool.dispatch_and_start(
    "solo-coder",
    "重构 Team 模块的 model 层，使用 MVVM 模式",
    {"workspace": "e:/pro/myproject"}
)

# 等待结果
result = tool.wait_for_result(task_id)
```

### 2.3 并行分派多个任务

```python
tool = TraeTaskTool(backend="traecli")

task_ids = tool.dispatch_agents([
    {"agent_type": "solo-coder", "description": "重构 Team 模块"},
    {"agent_type": "solo-coder", "description": "重构 Friend 模块"},
    {"agent_type": "architect",  "description": "设计聊天系统架构"},
    {"agent_type": "tester",     "description": "编写 Email 模块单元测试"},
])

results = tool.wait_for_all(task_ids)
summary = tool.collect_results(task_ids)
```

## 3. 后端详解

### 3.1 Worker 后端

Worker 后端通过 Python 子进程执行 `trae_agent_worker.py`，支持可插拔的 TaskHandler。

**适用场景**: 需要自定义执行逻辑、本地批量处理、结构化任务

```python
tool = TraeTaskTool(backend="worker")

# 使用 local-lua2cs Handler（自动检测 .lua 文件时自动选择）
task_id = tool.dispatch_and_start(
    "solo-coder",
    "转换 Team 模块 Lua 为 C#",
    {
        "source_dir": "e:/pro/pro_c3pro/trunk/.../services/team",
        "namespace": "c1proACT.Services.Team",
        "worker_backend": "local-lua2cs",  # 显式指定 Handler
    }
)

# 使用 traecli Handler（让 AI 在 Worker 子进程内执行）
task_id = tool.dispatch_and_start(
    "solo-coder",
    "重构 Team 模块",
    {
        "workspace": "e:/pro/myproject",
        "worker_backend": "traecli",
    }
)

# 使用 shell Handler
task_id = tool.dispatch_and_start(
    "solo-coder",
    "运行编译",
    {
        "worker_backend": "shell",
        "command": "dotnet build MyProject.csproj",
        "cwd": "e:/pro/myproject",
    }
)
```

### 3.2 TraeCli 后端

TraeCli 后端直接启动 `traecli exec` 子进程，每个任务启动一个独立的 Trae AI 会话。

**适用场景**: 纯 AI 驱动的通用任务、需要 AI 自主决策的场景

```python
tool = TraeTaskTool(backend="traecli")

task_id = tool.dispatch_and_start(
    "solo-coder",
    "分析 services/team 目录下的代码结构，输出架构文档",
    {
        "workspace": "e:/pro/myproject",
        "add_files": ["services/team/model/TeamModel.cs"],
        "answer_seconds": 120,
    }
)
```

### 3.3 自动后端选择

当 `backend="auto"` 或不指定时，系统根据任务上下文自动选择：

| 条件 | 选择的后端 |
|------|-----------|
| `context["backend"]` 显式指定 | 使用指定后端 |
| `source_dir` 存在且含 `.lua` 文件 | `local-lua2cs` |
| 默认 | `traecli` |

## 4. TaskHandler 注册表

Handler 是 Worker 后端的核心执行单元，通过 `HANDLER_REGISTRY` 注册。

| Handler 名 | 类 | 说明 |
|------------|-----|------|
| `traecli` | `TraeCliHandler` | 通过 `traecli exec` 让 Trae AI 执行任务 |
| `local-lua2cs` | `LocalLuaToCSharpHandler` | 纯 Python Lua→C# 转换 |
| `shell` | `ShellCommandHandler` | 执行 shell 命令 |

### 自定义 Handler

```python
from trae_agent_worker import TaskHandler, HANDLER_REGISTRY

class MyCustomHandler(TaskHandler):
    @property
    def name(self) -> str:
        return "my-custom"

    def execute(self, task_data: dict) -> dict:
        # 实现自定义执行逻辑
        return {
            "status": "DONE",
            "files_created": [],
            "summary": "Custom task completed",
            "errors": [],
            "concerns": [],
        }

# 注册
HANDLER_REGISTRY["my-custom"] = MyCustomHandler
```

## 5. API 参考

### TraeTaskTool

```python
class TraeTaskTool:
    def __init__(
        self,
        skill_root: str = None,     # 技能根目录
        max_parallel: int = 4,       # 最大并行数
        backend: str = "worker",     # 默认后端: "worker" | "traecli"
        workspace: str = None,       # 默认工作目录
    ): ...

    def dispatch(self, agent_type, description, context=None, backend=None) -> str:
        """分派任务，返回 task_id"""

    def start(self, task_id) -> bool:
        """启动已分派的任务"""

    def dispatch_and_start(self, agent_type, description, context=None, backend=None) -> str:
        """分派并启动，返回 task_id"""

    def dispatch_batch(self, tasks, backend=None) -> List[str]:
        """批量分派: [(agent_type, description, context), ...]"""

    def dispatch_agents(self, agent_tasks) -> List[str]:
        """高层 API: [{"agent_type", "description", "context", "backend"}, ...]"""

    def get_status(self, task_id) -> TaskStatus:
        """获取任务状态"""

    def wait_for_result(self, task_id, timeout=600, poll_interval=5) -> dict:
        """等待任务完成"""

    def wait_for_all(self, task_ids, timeout=600, poll_interval=5) -> dict:
        """等待所有任务完成"""

    def collect_results(self, task_ids) -> dict:
        """收集所有结果并生成汇总"""

    def get_progress_report(self) -> str:
        """获取进度报告"""
```

### TaskStatus 枚举

| 值 | 说明 |
|----|------|
| `PENDING` | 已分派，未启动 |
| `RUNNING` | 正在执行 |
| `COMPLETED` | 执行完成 |
| `FAILED` | 执行失败 |
| `BLOCKED` | 被阻塞 |
| `NEEDS_CONTEXT` | 需要更多上下文 |

### 任务结果格式

```json
{
    "status": "DONE | DONE_WITH_CONCERNS | FAILED | BLOCKED",
    "files_created": ["path/to/file1.cs", "path/to/file2.cs"],
    "files_count": 2,
    "summary": "任务执行总结",
    "errors": [],
    "concerns": [],
    "raw_output": "AI 原始输出（traecli 后端）",
    "exit_code": 0
}
```

## 6. 文件系统约定

### 目录结构

```
context/
├── tasks/
│   └── TASK-XXXXXXXX/          # 每个任务一个目录
│       ├── task_context.json   # 任务上下文
│       ├── agent_prompt.md     # 生成的提示词
│       └── execution.log       # 执行日志
└── results/
    └── TASK-XXXXXXXX.json      # 执行结果
```

### task_context.json 格式

```json
{
    "task_id": "TASK-XXXXXXXX",
    "agent_type": "solo-coder",
    "description": "任务描述",
    "context": {
        "source_dir": "...",
        "workspace": "...",
        "namespace": "...",
        "worker_backend": "auto",
        "add_files": [],
        "answer_seconds": 120,
        "timeout": 300
    },
    "backend": "worker",
    "created_at": "2026-05-23T12:00:00",
    "status": "pending"
}
```

## 7. 智能体类型

| agent_type | 角色描述 | 适用任务 |
|------------|---------|---------|
| `solo-coder` | 资深 Unity 游戏开发工程师 | 代码转换、功能开发、重构 |
| `architect` | 资深架构师 | 架构设计、技术选型、系统分析 |
| `tester` | 资深测试专家 | 测试策略、用例编写、质量保障 |
| `devops` | DevOps 工程师 | CI/CD、部署、基础设施 |
| `security` | 安全审计专家 | 漏洞扫描、安全评估 |

自定义 agent_type 时，系统会自动生成通用角色提示词：`"你是一位{agent_type}专家。"`

## 8. 常见用法模式

### 模式 A: Lua→C# 批量转换

```python
tool = TraeTaskTool(backend="worker")

task_ids = tool.dispatch_agents([
    {
        "agent_type": "solo-coder",
        "description": "转换 Team 模块",
        "context": {
            "source_dir": "e:/pro/.../services/team",
            "namespace": "c1proACT.Services.Team",
            "priority_order": ["model", "controller", "facade", "const", "view"],
            "worker_backend": "local-lua2cs",
        },
    },
    # ... 更多模块
])

results = tool.wait_for_all(task_ids)
```

### 模式 B: AI 驱动的通用任务

```python
tool = TraeTaskTool(backend="traecli")

task_ids = tool.dispatch_agents([
    {
        "agent_type": "architect",
        "description": "分析 services/team 目录架构，输出改进建议",
        "context": {"workspace": "e:/pro/myproject"},
    },
    {
        "agent_type": "solo-coder",
        "description": "为 TeamModel 添加 INotifyPropertyChanged 实现",
        "context": {
            "workspace": "e:/pro/myproject",
            "add_files": ["services/team/model/TeamModel.cs"],
        },
    },
])
```

### 模式 C: 混合后端

```python
tool = TraeTaskTool()

# 本地转换 + AI 审查并行
task_ids = [
    tool.dispatch_and_start(
        "solo-coder", "转换 Team 模块",
        {"source_dir": "...", "worker_backend": "local-lua2cs"},
        backend="worker",
    ),
    tool.dispatch_and_start(
        "architect", "审查 Friend 模块架构",
        {"workspace": "..."},
        backend="traecli",
    ),
]

results = tool.wait_for_all(task_ids)
```

## 9. 故障排除

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| `traecli not found` | TraeCli 未安装 | `pip install -e tools/TraeCli/agent-harness` |
| 任务一直 RUNNING | 子进程卡住 | 检查 `execution.log`，调整 `timeout` |
| 结果文件为空 | Worker 执行异常 | 查看 `execution.log` 中的错误信息 |
| UnicodeEncodeError | Windows 控制台编码 | Worker 已内置 `errors='replace'` 处理 |
| 并行数过多 | 资源竞争 | 调整 `max_parallel` 参数 |

## 10. 与 TraeCli 适配器的关系

`traecli_adapter.py` 是 TraeCli 的独立 Python API 封装，可以脱离 Task Tool 单独使用：

```python
from traecli_adapter import TraeCliAdapter

adapter = TraeCliAdapter(workspace="e:/pro/myproject")

# 单次执行
result = adapter.exec("分析项目结构")

# 并行执行
results = adapter.exec_batch([
    {"prompt": "重构 Team 模块"},
    {"prompt": "重构 Friend 模块"},
])

# 分派子智能体
result = adapter.dispatch_agent("solo-coder", "转换代码", {"source_dir": "..."})
```

详见 [TRAECLI_ADAPTER_GUIDE.md](./TRAECLI_ADAPTER_GUIDE.md)。
