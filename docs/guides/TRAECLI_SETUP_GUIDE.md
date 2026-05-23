# TraeCli 环境部署指南

> 版本: v1.0 | 更新: 2026-05-23
> 核心脚本: `scripts/traecli_setup.py`
> GitHub: https://github.com/firerlAGI/TraeCli

## 1. 概述

TraeCli 是 multi-agent-team 技能的子智能体执行引擎，用于通过 `traecli exec` 启动独立 AI 会话并行执行任务。本指南说明如何检测、安装和验证 TraeCli 环境。

### 自动安装流程

```
安装此 Skill
    │
    ├── 检测 traecli 是否已安装
    │       │
    │       ├── 已安装 → 直接使用
    │       │
    │       └── 未安装 → 自动安装
    │               │
    │               ├── 尝试 local（如有本地路径）
    │               ├── 尝试 pip install traecli
    │               └── 尝试 git clone + pip install -e
    │
    └── 验证安装 → 输出环境诊断
```

## 2. 环境要求

| 要求 | 最低版本 | 说明 |
|------|---------|------|
| Python | >= 3.8 | traecli 运行时要求 |
| pip | 可用 | 用于安装 traecli 包 |
| git | 可用（可选） | 用于从 GitHub 克隆安装 |
| Trae IDE | 已安装 | traecli 需要连接 Trae 桌面应用 |

## 3. 自动检测与安装

### 3.1 命令行方式

```bash
# 检测 + 自动安装（推荐）
python scripts/traecli_setup.py

# 仅检测，不安装
python scripts/traecli_setup.py --check-only

# 强制重装
python scripts/traecli_setup.py --force

# 指定安装源
python scripts/traecli_setup.py --source pip
python scripts/traecli_setup.py --source local --local-path "D:/tools/TraeCli/agent-harness"
python scripts/traecli_setup.py --source git

# 完整环境诊断
python scripts/traecli_setup.py --doctor

# JSON 格式输出（方便程序解析）
python scripts/traecli_setup.py --check-only --json
python scripts/traecli_setup.py --doctor --json
```

### 3.2 Python API 方式

#### 通过 TraeTaskTool

```python
from trae_task_tool import TraeTaskTool

tool = TraeTaskTool(backend="traecli")

# 检查环境
env = tool.check_environment()
if not env["ready"]:
    for w in env["warnings"]:
        print(f"WARNING: {w}")

# 自动安装
result = tool.ensure_traecli_available()
if result["available"]:
    print(f"traecli ready: {result['path']}")
else:
    print(f"Install failed: {result.get('error')}")
```

#### 通过 TraeCliAdapter

```python
from traecli_adapter import TraeCliAdapter

adapter = TraeCliAdapter(workspace="e:/pro/myproject")

# 自动安装
result = adapter.ensure_available()
if result["available"]:
    print(f"traecli ready: {result['path']}")
    if result["installed_now"]:
        print(f"Installed via: {result['method']}")
```

#### 直接使用 TraeCliSetup

```python
from traecli_setup import TraeCliSetup

setup = TraeCliSetup(local_path="D:/tools/TraeCli/agent-harness")

# 检测
check = setup.check_installed()
print(f"Installed: {check['installed']}, Path: {check['path']}")

# 自动安装
result = setup.ensure_available(source="auto")

# 完整诊断
diag = setup.doctor()
print(f"Ready: {diag['ready']}")
```

### 3.3 模型自动调用（推荐）

当模型（AI）需要使用 Task Tool 分派子智能体时，应先检查环境：

```python
from trae_task_tool import TraeTaskTool

tool = TraeTaskTool(backend="traecli")

# Step 1: 检查环境
env = tool.check_environment()
if not env["ready"]:
    # Step 2: 自动安装
    result = tool.ensure_traecli_available()
    if not result["available"]:
        # Step 3: 降级到 worker 后端
        tool.default_backend = "worker"
        print("TraeCli unavailable, falling back to worker backend")

# Step 4: 正常分派任务
task_ids = tool.dispatch_agents([...])
```

## 4. 安装源详解

### 4.1 auto（默认）

依次尝试所有安装源，首个成功即停止：

```
local（如有 local_path）→ pip → git
```

### 4.2 pip

```bash
pip install --upgrade traecli
```

适用场景: 有网络访问 PyPI 的环境

### 4.3 local

```bash
pip install -e /path/to/TraeCli/agent-harness
```

适用场景: 已有 TraeCli 源码的本地环境（开发/内网）

### 4.4 git

```bash
git clone --depth 1 https://github.com/firerlAGI/TraeCli.git /tmp/traecli_setup
pip install -e /tmp/traecli_setup/agent-harness
```

适用场景: 需要最新源码但无本地副本

## 5. 验证安装

### 5.1 快速验证

```bash
traecli --help
traecli --version
```

### 5.2 完整诊断

```bash
python scripts/traecli_setup.py --doctor
```

输出示例:
```
[12:00:00] [INFO] === TraeCli 环境诊断 ===
[12:00:00] [INFO] 平台: Windows
[12:00:00] [INFO] Python: 3.10.11 (OK)
[12:00:00] [INFO] pip: OK
[12:00:00] [INFO] git: OK
[12:00:00] [INFO] traecli: OK (D:\tools\python310\Scripts\traecli.EXE)
[12:00:00] [INFO] 总体状态: READY
```

### 5.3 Python 验证

```python
from traecli_setup import TraeCliSetup

setup = TraeCliSetup()
diag = setup.doctor()

assert diag["ready"], "Environment not ready"
assert diag["traecli"]["installed"], "traecli not installed"
assert diag["python"]["version_ok"], "Python version too low"
```

## 6. 故障排除

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| `traecli not found` | 未安装 | 运行 `python scripts/traecli_setup.py` |
| `pip install failed` | 网络问题/权限不足 | 尝试 `--source local` 或检查网络 |
| `git clone failed` | 无 git 或网络问题 | 尝试 `--source pip` 或手动下载 |
| `Python version too low` | Python < 3.8 | 升级 Python |
| `traecli doctor failed` | Trae IDE 未运行 | 启动 Trae 桌面应用 |
| `UnicodeEncodeError` | Windows 控制台编码 | 脚本已内置 `errors='replace'` 处理 |
| 安装后仍 not found | PATH 未刷新 | 重启终端或使用 `setup.check_installed()` 查找路径 |

### 手动安装步骤

如果自动安装失败，可手动执行：

```bash
# 方法 1: pip
pip install --upgrade traecli

# 方法 2: 从源码
git clone https://github.com/firerlAGI/TraeCli.git
cd TraeCli/agent-harness
pip install -e .

# 方法 3: 指定 Python 解释器
D:\tools\python310\python.exe -m pip install -e D:\tools\TraeCli\agent-harness
```

## 7. 环境检测规则

### 模型行为规则

当模型需要使用 Task Tool 分派子智能体时，必须遵循以下检测规则：

| 步骤 | 规则 | 行为 |
|------|------|------|
| 1 | **先检测后使用** | 使用 traecli 后端前，必须先调用 `check_environment()` |
| 2 | **自动安装** | 检测到 traecli 缺失时，调用 `ensure_traecli_available()` 自动安装 |
| 3 | **降级处理** | 自动安装失败时，降级到 worker 后端并告知用户 |
| 4 | **不阻塞** | 环境检测不应阻塞主流程，检测失败时使用降级方案 |
| 5 | **一次检测** | 同一会话中只需检测一次，结果可缓存 |

### 降级策略

```
traecli 后端不可用
    │
    ├── worker 后端可用 → 降级到 worker + traecli Handler
    │       （Worker 内部再尝试 traecli，失败则用其他 Handler）
    │
    └── worker 后端也不可用 → 报错，提示用户手动安装
```

### 检测时机

| 时机 | 检测内容 | 行为 |
|------|---------|------|
| Skill 加载时 | Python 版本、pip 可用性 | 记录到环境状态 |
| 首次使用 traecli 后端 | traecli 是否已安装 | 自动安装或降级 |
| 用户请求并行分派 | 环境是否就绪 | 确认后分派或降级 |
| 执行失败时 | traecli 是否异常 | 重新检测并重试 |
