#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trae Task Tool v2 - 通用子智能体调度器

架构设计：
1. 双后端支持 - Worker 进程后端 + TraeCli 直接后端
2. 通用任务分派 - 不限于 Lua->C#，支持任何 AI 可执行的任务
3. 并行执行 - 多个子智能体同时运行
4. 文件系统通信 - 上下文隔离、结果聚合

使用方法:
    from trae_task_tool import TraeTaskTool

    # Worker 后端（通过子进程执行）
    tool = TraeTaskTool(backend="worker")
    task_id = tool.dispatch("solo-coder", "将 FriendModel.lua 转换为 C#", context={...})

    # TraeCli 后端（直接通过 traecli exec 执行）
    tool = TraeTaskTool(backend="traecli")
    task_id = tool.dispatch("solo-coder", "重构 Team 模块", context={...})

    # 并行分派
    task_ids = tool.dispatch_batch([
        ("solo-coder", "转换 Team 模块", {...}),
        ("solo-coder", "转换 Friend 模块", {...}),
    ])
    results = tool.wait_for_all(task_ids)
"""

import io
import os
import sys
import json
import subprocess
import time
import uuid
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_CONTEXT = "needs_context"


@dataclass
class SubAgentTask:
    task_id: str
    agent_type: str
    description: str
    context: Dict[str, Any]
    backend: str = "auto"
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    process_id: Optional[int] = None
    output_dir: Optional[str] = None
    error: Optional[str] = None


class TraeTaskTool:
    """
    Trae IDE 环境下的通用 Task Tool 实现

    双后端架构：
    1. worker: 通过 Python 子进程执行 trae_agent_worker.py
       - 支持可插拔 TaskHandler（traecli, local-lua2cs, shell）
       - 适合需要自定义执行逻辑的场景
    2. traecli: 直接通过 traecli exec 命令执行
       - 每个 Task 启动一个 traecli 进程
       - 适合纯 AI 驱动的通用任务
    """

    def __init__(
        self,
        skill_root: str = None,
        max_parallel: int = 4,
        backend: str = "worker",
        workspace: Optional[str] = None,
    ):
        self.skill_root = Path(skill_root) if skill_root else Path(__file__).parent
        self.max_parallel = max_parallel
        self.default_backend = backend
        self.workspace = workspace or os.getcwd()
        self.tasks: Dict[str, SubAgentTask] = {}
        self._lock = threading.Lock()

        self.task_dir = self.skill_root / "context" / "tasks"
        self.result_dir = self.skill_root / "context" / "results"
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)

        self.worker_script = self.skill_root / "scripts" / "trae_agent_worker.py"
        if not self.worker_script.exists():
            self.worker_script = Path(__file__).parent / "trae_agent_worker.py"

        self._traecli_adapter = None

    def _get_traecli_adapter(self):
        if self._traecli_adapter is None:
            from traecli_adapter import TraeCliAdapter
            self._traecli_adapter = TraeCliAdapter(workspace=self.workspace)
        return self._traecli_adapter

    def ensure_traecli_available(
        self,
        source: str = "auto",
        local_path: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        确保 traecli 可用，不可用则自动安装

        在使用 traecli 后端前调用此方法，自动检测并安装缺失的 traecli。

        Args:
            source: 安装源 - "auto"(依次尝试 local→pip→git) / "pip" / "local" / "git"
            local_path: TraeCli 本地路径（source="local" 时必须）
            force: 是否强制重装

        Returns:
            {"available": bool, "path": str, "installed_now": bool, "method": str}
        """
        adapter = self._get_traecli_adapter()
        return adapter.ensure_available(source=source, local_path=local_path, force=force)

    def check_environment(self) -> Dict[str, Any]:
        """
        检查当前环境是否就绪

        Returns:
            {
                "ready": bool,
                "traecli": {"installed": bool, "path": str},
                "python_version": str,
                "backend": str,
                "warnings": [str],
            }
        """
        warnings = []

        from traecli_setup import TraeCliSetup
        setup = TraeCliSetup(local_path=None)
        check = setup.check_installed()

        if not check["installed"] and self.default_backend in ("traecli", "auto"):
            warnings.append(
                "traecli not installed. Call ensure_traecli_available() or set backend='worker'"
            )

        if not setup.check_python_version():
            warnings.append(f"Python version too low: {sys.version_info}, need >= 3.8")

        return {
            "ready": len(warnings) == 0,
            "traecli": check,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "backend": self.default_backend,
            "warnings": warnings,
        }

    def dispatch(
        self,
        agent_type: str,
        description: str,
        context: Optional[Dict[str, Any]] = None,
        backend: Optional[str] = None,
    ) -> str:
        """
        分派一个子智能体任务

        Args:
            agent_type: 智能体类型 (solo-coder, architect, tester, etc.)
            description: 任务描述
            context: 任务上下文
            backend: 执行后端 (worker, traecli, auto)

        Returns:
            str: 任务 ID
        """
        task_id = f"TASK-{uuid.uuid4().hex[:8].upper()}"
        resolved_backend = backend or self.default_backend

        task = SubAgentTask(
            task_id=task_id,
            agent_type=agent_type,
            description=description,
            context=context or {},
            backend=resolved_backend,
        )

        task_dir = self.task_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        task.output_dir = str(task_dir)

        context_file = task_dir / "task_context.json"
        with open(context_file, 'w', encoding='utf-8') as f:
            json.dump({
                "task_id": task_id,
                "agent_type": agent_type,
                "description": description,
                "context": context or {},
                "backend": resolved_backend,
                "created_at": task.created_at,
                "status": "pending",
            }, f, indent=2, ensure_ascii=False)

        prompt_file = task_dir / "agent_prompt.md"
        prompt = self._build_prompt(agent_type, description, context)
        with open(prompt_file, 'w', encoding='utf-8') as f:
            f.write(prompt)

        with self._lock:
            self.tasks[task_id] = task

        return task_id

    def start(self, task_id: str) -> bool:
        """
        启动一个已分派的任务

        根据 backend 选择不同的启动方式：
        - worker: 启动 Python 子进程执行 trae_agent_worker.py
        - traecli: 启动 traecli exec 子进程
        """
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            if task.status != TaskStatus.PENDING:
                return False

        backend = task.backend

        if backend == "traecli":
            return self._start_traecli(task)
        else:
            return self._start_worker(task)

    def _start_worker(self, task: SubAgentTask) -> bool:
        """通过 Worker 子进程启动"""
        task_dir = self.task_dir / task.task_id
        result_file = self.result_dir / f"{task.task_id}.json"
        log_file = task_dir / "execution.log"

        worker_backend = task.context.get("worker_backend", "auto")

        cmd = [
            sys.executable,
            str(self.worker_script),
            "--task-id", task.task_id,
            "--agent-type", task.agent_type,
            "--task-dir", str(task_dir),
            "--result-file", str(result_file),
            "--log-file", str(log_file),
            "--backend", worker_backend,
        ]

        try:
            with open(log_file, 'w', encoding='utf-8') as log_f:
                process = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.skill_root),
                    env={**os.environ, "TRAE_TASK_ID": task.task_id},
                )

            with self._lock:
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now().isoformat()
                task.process_id = process.pid

            return True
        except Exception as e:
            with self._lock:
                task.status = TaskStatus.FAILED
                task.error = str(e)
            return False

    def _start_traecli(self, task: SubAgentTask) -> bool:
        """通过 TraeCli exec 启动"""
        task_dir = self.task_dir / task.task_id
        result_file = self.result_dir / f"{task.task_id}.json"
        log_file = task_dir / "execution.log"

        prompt = self._build_prompt(task.agent_type, task.description, task.context)

        workspace = task.context.get("workspace") or task.context.get("project_root") or self.workspace

        cmd = [
            "traecli", "--json", "--no-alt-screen",
            "-C", workspace,
            "exec", prompt,
            "--mode", task.context.get("mode", "agent"),
            "--answer-seconds", str(task.context.get("answer_seconds", 120)),
            "--new-chat",
        ]

        add_files = task.context.get("add_files", [])
        for f in add_files:
            cmd.extend(["--add-file", f])

        try:
            log_f = open(log_file, 'w', encoding='utf-8')
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=workspace,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )

            def _read_output(proc, log_file_obj, result_path, task_obj):
                try:
                    stdout, _ = proc.communicate(timeout=task_obj.context.get("timeout", 300))
                    output = stdout.decode('utf-8', errors='replace') if stdout else ""

                    log_file_obj.write(output)
                    log_file_obj.close()

                    result = {
                        "status": "DONE" if proc.returncode == 0 else "FAILED",
                        "files_created": [],
                        "summary": output[:2000],
                        "raw_output": output[:5000],
                        "exit_code": proc.returncode,
                    }

                    if proc.returncode != 0:
                        result["errors"] = [f"traecli exit code: {proc.returncode}"]

                    result_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(result_path, 'w', encoding='utf-8') as rf:
                        json.dump(result, rf, indent=2, ensure_ascii=False)

                except subprocess.TimeoutExpired:
                    proc.kill()
                    log_file_obj.write("TIMEOUT")
                    log_file_obj.close()
                    result = {
                        "status": "FAILED",
                        "files_created": [],
                        "errors": ["Timeout"],
                    }
                    with open(result_path, 'w', encoding='utf-8') as rf:
                        json.dump(result, rf, indent=2, ensure_ascii=False)
                except Exception as e:
                    log_file_obj.write(str(e))
                    log_file_obj.close()

            t = threading.Thread(
                target=_read_output,
                args=(process, log_f, result_file, task),
                daemon=True,
            )
            t.start()

            with self._lock:
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now().isoformat()
                task.process_id = process.pid

            return True
        except Exception as e:
            with self._lock:
                task.status = TaskStatus.FAILED
                task.error = str(e)
            return False

    def dispatch_and_start(
        self,
        agent_type: str,
        description: str,
        context: Optional[Dict[str, Any]] = None,
        backend: Optional[str] = None,
    ) -> str:
        """分派并立即启动"""
        task_id = self.dispatch(agent_type, description, context, backend)
        self.start(task_id)
        return task_id

    def dispatch_batch(
        self,
        tasks: List[Tuple[str, str, Optional[Dict]]],
        backend: Optional[str] = None,
    ) -> List[str]:
        """批量分派并启动（并行执行）"""
        task_ids = []
        for agent_type, description, context in tasks:
            task_id = self.dispatch_and_start(agent_type, description, context, backend)
            task_ids.append(task_id)
        return task_ids

    def dispatch_agents(
        self,
        agent_tasks: List[Dict[str, Any]],
    ) -> List[str]:
        """
        高层 API - 分派多个子智能体

        Args:
            agent_tasks: [
                {
                    "agent_type": "solo-coder",
                    "description": "转换 Team 模块",
                    "context": {...},
                    "backend": "traecli"  # 可选
                },
                ...
            ]

        Returns:
            List[str]: 任务 ID 列表
        """
        task_ids = []
        for at in agent_tasks:
            task_id = self.dispatch_and_start(
                agent_type=at["agent_type"],
                description=at["description"],
                context=at.get("context"),
                backend=at.get("backend"),
            )
            task_ids.append(task_id)
        return task_ids

    def get_status(self, task_id: str) -> TaskStatus:
        """获取任务状态"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return TaskStatus.FAILED

        result_file = self.result_dir / f"{task_id}.json"
        if result_file.exists():
            try:
                with open(result_file, 'r', encoding='utf-8') as f:
                    result = json.load(f)

                with self._lock:
                    task.result = result
                    status_str = result.get("status", "completed").upper()
                    if "DONE" in status_str:
                        task.status = TaskStatus.COMPLETED
                    elif "FAIL" in status_str:
                        task.status = TaskStatus.FAILED
                    elif "BLOCK" in status_str:
                        task.status = TaskStatus.BLOCKED
                    else:
                        task.status = TaskStatus.COMPLETED
                    task.completed_at = datetime.now().isoformat()

                return task.status
            except Exception:
                pass

        if task.process_id:
            try:
                import psutil
                if not psutil.pid_exists(task.process_id):
                    log_file = Path(task.output_dir) / "execution.log"
                    if log_file.exists():
                        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                            log_content = f.read()
                        if "ERROR" in log_content or "FAILED" in log_content:
                            with self._lock:
                                task.status = TaskStatus.FAILED
                                task.error = log_content[-500:]
                            return TaskStatus.FAILED

                    with self._lock:
                        task.status = TaskStatus.COMPLETED
                        task.completed_at = datetime.now().isoformat()
                    return TaskStatus.COMPLETED
            except ImportError:
                pass

        with self._lock:
            return task.status

    def wait_for_result(
        self,
        task_id: str,
        timeout: int = 600,
        poll_interval: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """等待任务完成并返回结果"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            status = self.get_status(task_id)
            if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED):
                with self._lock:
                    return self.tasks[task_id].result
            time.sleep(poll_interval)

        return {"error": "timeout", "task_id": task_id}

    def wait_for_all(
        self,
        task_ids: List[str],
        timeout: int = 600,
        poll_interval: int = 5,
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        """等待所有任务完成"""
        results = {}
        for task_id in task_ids:
            results[task_id] = self.wait_for_result(task_id, timeout, poll_interval)
        return results

    def collect_results(self, task_ids: List[str]) -> Dict[str, Any]:
        """收集所有任务结果并生成汇总"""
        all_results = {}
        total_files = 0
        total_errors = 0
        statuses = {}

        for task_id in task_ids:
            result_file = self.result_dir / f"{task_id}.json"
            if result_file.exists():
                try:
                    with open(result_file, 'r', encoding='utf-8') as f:
                        result = json.load(f)
                    all_results[task_id] = result
                    total_files += len(result.get("files_created", []))
                    total_errors += len(result.get("errors", []))
                    statuses[task_id] = result.get("status", "UNKNOWN")
                except Exception as e:
                    all_results[task_id] = {"status": "FAILED", "errors": [str(e)]}
                    total_errors += 1
                    statuses[task_id] = "FAILED"
            else:
                all_results[task_id] = {"status": "PENDING", "errors": ["No result file"]}
                statuses[task_id] = "PENDING"

        return {
            "summary": {
                "total_tasks": len(task_ids),
                "total_files": total_files,
                "total_errors": total_errors,
                "statuses": statuses,
                "completed_at": datetime.now().isoformat(),
            },
            "results": all_results,
        }

    def get_progress_report(self) -> str:
        """获取进度报告"""
        lines = ["=" * 60, "Sub-Agent Task Progress Report", "=" * 60]

        for task_id, task in self.tasks.items():
            status_label = {
                TaskStatus.PENDING: "[PENDING]",
                TaskStatus.RUNNING: "[RUNNING]",
                TaskStatus.COMPLETED: "[DONE]",
                TaskStatus.FAILED: "[FAILED]",
                TaskStatus.BLOCKED: "[BLOCKED]",
                TaskStatus.NEEDS_CONTEXT: "[NEEDS_CTX]",
            }.get(task.status, "[?]")

            lines.append(f"\n{status_label} [{task_id}] {task.agent_type} ({task.backend})")
            lines.append(f"   Desc: {task.description[:60]}...")
            if task.started_at:
                lines.append(f"   Started: {task.started_at}")
            if task.completed_at:
                lines.append(f"   Completed: {task.completed_at}")
            if task.error:
                lines.append(f"   Error: {task.error[:100]}")
            if task.result:
                files = task.result.get("files_created", [])
                if files:
                    lines.append(f"   Files: {len(files)}")
                    for f in files[:5]:
                        lines.append(f"     - {f}")

        return "\n".join(lines)

    def _build_prompt(
        self,
        agent_type: str,
        description: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """构建子智能体提示词"""
        role_map = {
            'solo-coder': "你是一位资深 Unity 游戏开发工程师，精通 C# 和 Lua，擅长将 Lua 代码转换为优雅的 C# 代码。遵循 SOLID 原则和 MVVM 模式。不添加注释。",
            'architect': "你是一位资深架构师，职责是设计系统性、前瞻性、可落地、可验证的架构。",
            'tester': "你是一位资深测试专家，职责是确保全面、深入、自动化、可量化的质量保障。",
        }

        role = role_map.get(agent_type, f"你是一位{agent_type}专家。")

        prompt = f"""# 子智能体任务

## 角色
{role}

## 任务描述
{description}
"""

        if context:
            source_dir = context.get("source_dir", "")
            output_dir = context.get("output_dir", source_dir)
            namespace = context.get("namespace", "MyProject")

            if source_dir:
                prompt += f"\n## 源文件目录\n{source_dir}\n"
            if output_dir:
                prompt += f"\n## 输出目录\n{output_dir}\n"
            if namespace:
                prompt += f"\n## 命名空间\n{namespace}\n"

            priority_order = context.get("priority_order", [])
            if priority_order:
                prompt += f"\n## 转换优先级\n{' > '.join(priority_order)}\n"

        prompt += """
## Karpathy 四大核心原则
- Think Before Coding: 明确假设，问清楚，不隐藏困惑
- Simplicity First: 最小代码，无 speculative features
- Surgical Changes: 只改必要的，不改无关的
- Goal-Driven: 定义成功标准，验证检查点

## 汇报格式
完成后请输出：
- 完成状态：DONE / DONE_WITH_CONCERNS / BLOCKED
- 创建/修改的文件列表（每行一个完整路径）
- 工作总结
- 遗留问题（如有）
"""
        return prompt


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    tool = TraeTaskTool()

    print("=" * 60)
    print("Trae Task Tool v2 - 通用子智能体调度器")
    print("=" * 60)
    print(f"\nSkill Root: {tool.skill_root}")
    print(f"Task Dir: {tool.task_dir}")
    print(f"Result Dir: {tool.result_dir}")
    print(f"Worker Script: {tool.worker_script}")
    print(f"Default Backend: {tool.default_backend}")
    print(f"Max Parallel: {tool.max_parallel}")

    print("\n使用示例:")
    print("""
    from trae_task_tool import TraeTaskTool

    # Worker 后端（通过子进程 + 可插拔 Handler）
    tool = TraeTaskTool(backend="worker")
    task_id = tool.dispatch_and_start(
        "solo-coder",
        "将 FriendModel.lua 转换为 C#",
        {"source_dir": "...", "namespace": "MyProject"}
    )

    # TraeCli 后端（直接通过 traecli exec 执行）
    tool = TraeTaskTool(backend="traecli")
    task_id = tool.dispatch_and_start(
        "solo-coder",
        "重构 Team 模块",
        {"workspace": "e:/pro/myproject"}
    )

    # 并行分派多个子智能体
    tool = TraeTaskTool(backend="traecli")
    task_ids = tool.dispatch_agents([
        {"agent_type": "solo-coder", "description": "转换 Team 模块"},
        {"agent_type": "solo-coder", "description": "转换 Friend 模块"},
        {"agent_type": "solo-coder", "description": "转换 Email 模块"},
    ])
    results = tool.wait_for_all(task_ids)
    """)
