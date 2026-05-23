# TraeCli 适配器使用指南

> 版本: v1.0 | 更新: 2026-05-23
> 核心脚本: `scripts/traecli_adapter.py`
> 依赖: TraeCli (`pip install -e tools/TraeCli/agent-harness`)

## 1. 概述

TraeCli 适配器将 `traecli` 命令行工具封装为 Python API，使子智能体调度系统可以通过编程方式调用 Trae AI 执行任务。

### 核心价值

| 能力 | 说明 |
|------|------|
| Python API 封装 | 无需手拼命令行，直接调用 Python 方法 |
| 并行执行 | `exec_batch()` / `dispatch_agents_parallel()` 支持多任务并行 |
| 结构化结果 | 返回 `TraeCliResult` 数据类，包含状态、输出、JSON 解析 |
| 子智能体分派 | `dispatch_agent()` 高层 API，自动组装角色提示词 |

### 与 Task Tool 的关系

```
TraeTaskTool (调度器)
    │
    ├── Worker 后端 → trae_agent_worker.py → TraeCliHandler → traecli_adapter.py
    │
    └── TraeCli 后端 → 直接调用 traecli exec
```

- **Task Tool** 是调度层，管理任务生命周期（分派、启动、等待、收集）
- **TraeCli Adapter** 是执行层，封装 `traecli` 命令调用
- 两者可以独立使用，也可以组合使用

## 2. 安装与验证

### 安装 TraeCli

```bash
cd tools/TraeCli/agent-harness
pip install -e .
```

### 验证安装

```python
from traecli_adapter import TraeCliAdapter

adapter = TraeCliAdapter()
check = adapter.check_available()
# {'available': True, 'path': 'D:\\tools\\python310\\Scripts\\traecli.EXE', ...}
```

或命令行验证：

```bash
traecli --help
traecli doctor
```

## 3. API 参考

### TraeCliAdapter

```python
class TraeCliAdapter:
    def __init__(
        self,
        workspace: str = None,       # 工作目录
        traecli_path: str = None,     # traecli 可执行文件路径（自动检测）
        app_path: str = None,         # Trae 桌面应用路径
        timeout: int = 300,           # 默认超时（秒）
        json_mode: bool = True,       # 启用 JSON 输出模式
        no_alt_screen: bool = True,   # 禁用交互式备用屏
    ): ...
```

### exec() - 一次性执行

```python
def exec(
    self,
    prompt: str,                          # 提示词
    *,
    add_files: List[str] = None,          # 附加文件
    new_chat: bool = True,                # 新建会话
    mode: str = "agent",                  # agent 模式
    dispatch_method: str = "auto",        # 分发方式
    answer_seconds: float = 60.0,         # 等待回答时间
    wait_seconds: float = 0.0,            # 等待时间
    task_id: str = None,                  # 自定义任务 ID
) -> TraeCliResult
```

对应命令: `traecli --json --no-alt-screen -C <workspace> exec "<prompt>" --mode agent --new-chat`

### chat() - 有状态会话

```python
def chat(
    self,
    prompt: str,
    *,
    add_files: List[str] = None,
    new_chat: bool = False,               # 默认续接上下文
    mode: str = "agent",
    dispatch_method: str = "auto",
    answer_seconds: float = 60.0,
    task_id: str = None,
) -> TraeCliResult
```

对应命令: `traecli --json --no-alt-screen -C <workspace> chat "<prompt>"`

**exec vs chat 区别**:
- `exec`: 无状态，不恢复也不保存会话，适合独立任务
- `chat`: 有状态，续接当前 workspace 的已保存上下文，适合多轮对话

### exec_batch() - 并行执行

```python
def exec_batch(
    self,
    tasks: List[Dict[str, Any]],          # 任务列表
    max_parallel: int = 4,                # 最大并行数
) -> Dict[str, TraeCliResult]
```

任务格式:
```python
[
    {
        "prompt": "重构 Team 模块",
        "task_id": "MY-TASK-001",          # 可选
        "add_files": ["path/to/file.cs"],  # 可选
        "new_chat": True,                  # 可选
        "mode": "agent",                   # 可选
        "answer_seconds": 120,             # 可选
    },
    ...
]
```

### dispatch_agent() - 分派子智能体

```python
def dispatch_agent(
    self,
    agent_type: str,                       # 智能体类型
    task_description: str,                 # 任务描述
    context: Dict[str, Any] = None,        # 任务上下文
    *,
    workspace: str = None,                 # 工作目录
    add_files: List[str] = None,           # 附加文件
) -> TraeCliResult
```

自动将 `agent_type` 和 `context` 组装成结构化提示词，通过 `traecli exec` 发送给 Trae AI。

### dispatch_agents_parallel() - 并行分派

```python
def dispatch_agents_parallel(
    self,
    agent_tasks: List[Dict[str, Any]],
    max_parallel: int = 4,
) -> Dict[str, TraeCliResult]
```

任务格式:
```python
[
    {
        "agent_type": "solo-coder",
        "task_description": "转换 Team 模块",
        "context": {"source_dir": "...", "namespace": "..."},
        "workspace": "e:/pro/myproject",
        "add_files": ["..."],
    },
    ...
]
```

### check_available() - 检查可用性

```python
def check_available(self) -> Dict[str, Any]:
    # 返回: {"available": True/False, "path": "...", "workspace": "..."}
```

### doctor() - 环境诊断

```python
def doctor(self) -> Dict[str, Any]:
    # 运行 traecli doctor，检查安装、配置、认证和运行状态
```

### TraeCliResult 数据类

```python
@dataclass
class TraeCliResult:
    task_id: str              # 任务 ID
    prompt: str               # 原始提示词
    exit_code: int            # 退出码（0=成功）
    stdout: str               # 标准输出
    stderr: str               # 标准错误
    json_output: dict = None  # 解析后的 JSON 输出
    started_at: str = None    # 开始时间
    completed_at: str = None  # 完成时间
    duration_seconds: float   # 执行时长

    @property
    def success(self) -> bool: ...       # exit_code == 0

    @property
    def answer(self) -> str: ...         # JSON answer 或原始 stdout
```

## 4. 使用示例

### 示例 1: 单次执行

```python
from traecli_adapter import TraeCliAdapter

adapter = TraeCliAdapter(workspace="e:/pro/myproject")

result = adapter.exec("分析 services/team 目录的代码结构")
if result.success:
    print(result.answer)
else:
    print(f"Failed: {result.stderr}")
```

### 示例 2: 带文件执行

```python
result = adapter.exec(
    "审查这个文件的代码质量",
    add_files=["services/team/model/TeamModel.cs"],
    new_chat=True,
)
```

### 示例 3: 并行分派子智能体

```python
results = adapter.dispatch_agents_parallel([
    {
        "agent_type": "solo-coder",
        "task_description": "重构 Team 模块，使用 MVVM 模式",
        "context": {"namespace": "c1proACT.Services.Team"},
    },
    {
        "agent_type": "solo-coder",
        "task_description": "重构 Friend 模块，使用 MVVM 模式",
        "context": {"namespace": "c1proACT.Services.Friend"},
    },
    {
        "agent_type": "architect",
        "task_description": "设计聊天系统架构",
    },
], max_parallel=3)

for task_id, result in results.items():
    print(f"{task_id}: {'OK' if result.success else 'FAIL'}")
```

### 示例 4: 有状态会话

```python
# 第一轮
result1 = adapter.chat("分析项目结构", new_chat=True)

# 第二轮（续接上下文）
result2 = adapter.chat("基于上面的分析，给出重构建议")
```

## 5. TraeCli 命令映射

| Python API | 对应的 traecli 命令 |
|------------|-------------------|
| `adapter.exec("prompt")` | `traecli exec "prompt" --new-chat` |
| `adapter.chat("prompt")` | `traecli chat "prompt"` |
| `adapter.check_available()` | `traecli --help` |
| `adapter.doctor()` | `traecli doctor` |

### 完整命令行参数映射

```python
adapter.exec(
    prompt="...",
    add_files=["a.cs", "b.cs"],     # → --add-file a.cs --add-file b.cs
    new_chat=True,                   # → --new-chat
    mode="agent",                    # → --mode agent
    dispatch_method="auto",          # → --dispatch-method auto
    answer_seconds=120,              # → --answer-seconds 120
    wait_seconds=5,                  # → --wait-seconds 5
)
```

生成命令:
```bash
traecli --json --no-alt-screen -C <workspace> exec "..." \
    --add-file a.cs --add-file b.cs \
    --new-chat --mode agent --dispatch-method auto \
    --answer-seconds 120 --wait-seconds 5
```

## 6. 智能体角色提示词

`dispatch_agent()` 和 `dispatch_agents_parallel()` 会根据 `agent_type` 自动生成角色提示词：

| agent_type | 生成的角色描述 |
|------------|--------------|
| `solo-coder` | 资深 Unity 游戏开发工程师，精通 C# 和 Lua，遵循 SOLID 原则和 MVVM 模式 |
| `architect` | 资深架构师，设计系统性、前瞻性、可落地、可验证的架构 |
| `tester` | 资深测试专家，确保全面、深入、自动化、可量化的质量保障 |
| `devops` | DevOps 工程师，负责 CI/CD、部署和基础设施 |
| `security` | 安全审计专家，负责漏洞扫描和安全评估 |
| 其他 | `你是一位{agent_type}专家。` |

### 提示词结构

```
# 子智能体任务

## 角色
{根据 agent_type 自动生成}

## 任务
{task_description}

## 上下文（如有）
```json
{context}
```

## 执行要求
1. 按照任务描述完成工作
2. 遵循 Karpathy 四大核心原则
3. 完成后输出工作总结

## 汇报格式
- 完成状态：DONE / DONE_WITH_CONCERNS / BLOCKED / NEEDS_CONTEXT
- 创建/修改的文件列表
- 工作总结
- 遗留问题（如有）
```

## 7. 故障排除

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| `available: False` | traecli 未安装或不在 PATH | `pip install -e tools/TraeCli/agent-harness` |
| `traecli not found` | PATH 中找不到 | 检查 `traecli_path` 参数或 `shutil.which("traecli")` |
| Timeout | AI 执行时间过长 | 增大 `timeout` 或 `answer_seconds` |
| JSON 解析失败 | AI 输出非 JSON | `json_output` 为 None，使用 `stdout` 获取原始输出 |
| Windows 编码错误 | GBK 控制台 | 适配器已内置 `encoding='utf-8', errors='replace'` |
