#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TraeCli Adapter - 将 traecli exec 封装为 Python API

核心能力：
1. traecli exec "prompt" - 无状态一次性执行，每个子 agent 启动独立会话
2. traecli chat "prompt" - 有状态会话执行，可续接上下文
3. traecli --json - 输出机器可读 JSON

使用方法:
    from traecli_adapter import TraeCliAdapter
    
    adapter = TraeCliAdapter(workspace="e:/pro/myproject")
    
    # 一次性执行
    result = adapter.exec("将 FriendModel.lua 转换为 C#")
    
    # 并行执行多个任务
    results = adapter.exec_batch([
        "转换 Team 模块",
        "转换 Friend 模块",
    ])
    
    # 带上下文执行
    result = adapter.exec(
        "转换 Lua 文件为 C#",
        add_files=["model/FriendModel.lua"],
        new_chat=True
    )
"""

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TraeCliResult:
    task_id: str
    prompt: str
    exit_code: int
    stdout: str
    stderr: str
    json_output: Optional[Dict[str, Any]] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: float = 0.0

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def answer(self) -> str:
        if self.json_output and isinstance(self.json_output, dict):
            return self.json_output.get("answer", self.stdout)
        return self.stdout


class TraeCliAdapter:
    """
    TraeCli 适配器
    
    将 traecli 命令行工具封装为 Python API，支持：
    - exec: 无状态一次性执行（每个子 agent 独立会话）
    - chat: 有状态会话执行
    - exec_batch: 并行执行多个任务
    """

    def __init__(
        self,
        workspace: Optional[str] = None,
        traecli_path: Optional[str] = None,
        app_path: Optional[str] = None,
        timeout: int = 300,
        json_mode: bool = True,
        no_alt_screen: bool = True,
    ):
        self.workspace = workspace or os.getcwd()
        self.timeout = timeout
        self.json_mode = json_mode
        self.no_alt_screen = no_alt_screen
        self.app_path = app_path
        
        self._traecli_path = traecli_path or self._find_traecli()
        
        self._results: Dict[str, TraeCliResult] = {}
        self._lock = threading.Lock()

    def _find_traecli(self) -> str:
        """查找 traecli 可执行文件"""
        found = shutil.which("traecli")
        if found:
            return found
        
        python_base = Path(sys.executable).parent
        candidates = [
            python_base / "Scripts" / "traecli.exe",
            python_base / "Scripts" / "traecli",
            python_base / "traecli.exe",
            Path.home() / ".local" / "bin" / "traecli",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        
        return "traecli"

    def _build_base_args(self) -> List[str]:
        """构建基础命令参数"""
        args = [self._traecli_path]
        
        if self.json_mode:
            args.append("--json")
        
        if self.no_alt_screen:
            args.append("--no-alt-screen")
        
        if self.workspace:
            args.extend(["-C", self.workspace])
        
        if self.app_path:
            args.extend(["--app-path", self.app_path])
        
        return args

    def exec(
        self,
        prompt: str,
        *,
        add_files: Optional[List[str]] = None,
        new_chat: bool = True,
        mode: str = "agent",
        dispatch_method: str = "auto",
        answer_seconds: float = 60.0,
        wait_seconds: float = 0.0,
        task_id: Optional[str] = None,
    ) -> TraeCliResult:
        """
        执行一次性 traecli exec 命令
        
        Args:
            prompt: 提示词
            add_files: 附加文件列表
            new_chat: 是否开启新会话
            mode: agent 模式 (agent/builder)
            dispatch_method: 分发方式 (auto/cli/cdp/uri/command)
            answer_seconds: 等待回答时间
            wait_seconds: 等待时间
            task_id: 任务 ID
            
        Returns:
            TraeCliResult: 执行结果
        """
        tid = task_id or f"EXEC-{uuid.uuid4().hex[:8].upper()}"
        
        args = self._build_base_args()
        args.extend(["exec", prompt])
        
        args.extend(["--mode", mode])
        args.extend(["--dispatch-method", dispatch_method])
        args.extend(["--answer-seconds", str(answer_seconds)])
        
        if wait_seconds > 0:
            args.extend(["--wait-seconds", str(wait_seconds)])
        
        if new_chat:
            args.append("--new-chat")
        
        if add_files:
            for f in add_files:
                args.extend(["--add-file", f])
        
        return self._run_command(tid, args, prompt)

    def chat(
        self,
        prompt: str,
        *,
        add_files: Optional[List[str]] = None,
        new_chat: bool = False,
        mode: str = "agent",
        dispatch_method: str = "auto",
        answer_seconds: float = 60.0,
        task_id: Optional[str] = None,
    ) -> TraeCliResult:
        """
        执行 traecli chat 命令（有状态会话）
        
        与 exec 的区别：chat 会续接当前 workspace 的已保存上下文
        """
        tid = task_id or f"CHAT-{uuid.uuid4().hex[:8].upper()}"
        
        args = self._build_base_args()
        args.extend(["chat", prompt])
        
        args.extend(["--mode", mode])
        args.extend(["--dispatch-method", dispatch_method])
        args.extend(["--answer-seconds", str(answer_seconds)])
        
        if new_chat:
            args.append("--new-chat")
        
        if add_files:
            for f in add_files:
                args.extend(["--add-file", f])
        
        return self._run_command(tid, args, prompt)

    def exec_batch(
        self,
        tasks: List[Dict[str, Any]],
        max_parallel: int = 4,
    ) -> Dict[str, TraeCliResult]:
        """
        并行执行多个任务
        
        Args:
            tasks: [{"prompt": str, "add_files": [...], "task_id": str, ...}, ...]
            max_parallel: 最大并行数
            
        Returns:
            Dict[str, TraeCliResult]: task_id -> result
        """
        results: Dict[str, TraeCliResult] = {}
        semaphore = threading.Semaphore(max_parallel)
        
        def _run_task(task_spec: Dict[str, Any]):
            with semaphore:
                prompt = task_spec["prompt"]
                tid = task_spec.get("task_id", f"BATCH-{uuid.uuid4().hex[:8].upper()}")
                result = self.exec(
                    prompt,
                    add_files=task_spec.get("add_files"),
                    new_chat=task_spec.get("new_chat", True),
                    mode=task_spec.get("mode", "agent"),
                    dispatch_method=task_spec.get("dispatch_method", "auto"),
                    answer_seconds=task_spec.get("answer_seconds", 60.0),
                    task_id=tid,
                )
                with self._lock:
                    results[tid] = result
        
        threads = []
        for task_spec in tasks:
            t = threading.Thread(target=_run_task, args=(task_spec,))
            t.start()
            threads.append(t)
        
        for t in threads:
            t.join(timeout=self.timeout)
        
        return results

    def dispatch_agent(
        self,
        agent_type: str,
        task_description: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        workspace: Optional[str] = None,
        add_files: Optional[List[str]] = None,
    ) -> TraeCliResult:
        """
        分派一个子智能体任务（高层 API）
        
        将 agent_type 和 context 组装成结构化提示词，
        通过 traecli exec 发送给 Trae AI 执行
        
        Args:
            agent_type: 智能体类型 (solo-coder, architect, tester, etc.)
            task_description: 任务描述
            context: 任务上下文
            workspace: 工作目录
            add_files: 附加文件
            
        Returns:
            TraeCliResult
        """
        prompt = self._build_agent_prompt(agent_type, task_description, context)
        
        saved_workspace = self.workspace
        if workspace:
            self.workspace = workspace
        
        try:
            return self.exec(
                prompt,
                add_files=add_files,
                new_chat=True,
                task_id=f"AGENT-{agent_type.upper()}-{uuid.uuid4().hex[:6].upper()}",
            )
        finally:
            self.workspace = saved_workspace

    def dispatch_agents_parallel(
        self,
        agent_tasks: List[Dict[str, Any]],
        max_parallel: int = 4,
    ) -> Dict[str, TraeCliResult]:
        """
        并行分派多个子智能体
        
        Args:
            agent_tasks: [
                {
                    "agent_type": "solo-coder",
                    "task_description": "转换 Team 模块",
                    "context": {...},
                    "workspace": "...",
                    "add_files": [...]
                },
                ...
            ]
        """
        batch_tasks = []
        for at in agent_tasks:
            prompt = self._build_agent_prompt(
                at["agent_type"],
                at["task_description"],
                at.get("context"),
            )
            batch_tasks.append({
                "prompt": prompt,
                "task_id": at.get("task_id", f"AGENT-{at['agent_type'].upper()}-{uuid.uuid4().hex[:6].upper()}"),
                "add_files": at.get("add_files"),
                "new_chat": True,
                "mode": at.get("mode", "agent"),
            })
        
        return self.exec_batch(batch_tasks, max_parallel)

    def _build_agent_prompt(
        self,
        agent_type: str,
        task_description: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """构建子智能体提示词"""
        role_map = {
            "solo-coder": "你是一位资深 Unity 游戏开发工程师，精通 C# 和 Lua，擅长将 Lua 代码转换为优雅的 C# 代码。遵循 SOLID 原则和 MVVM 模式。",
            "architect": "你是一位资深架构师，职责是设计系统性、前瞻性、可落地、可验证的架构。",
            "tester": "你是一位资深测试专家，职责是确保全面、深入、自动化、可量化的质量保障。",
            "devops": "你是一位 DevOps 工程师，负责 CI/CD、部署和基础设施。",
            "security": "你是一位安全审计专家，负责漏洞扫描和安全评估。",
        }
        
        role = role_map.get(agent_type, f"你是一位{agent_type}专家。")
        
        prompt = f"""# 子智能体任务

## 角色
{role}

## 任务
{task_description}
"""
        
        if context:
            prompt += f"""
## 上下文
```json
{json.dumps(context, indent=2, ensure_ascii=False)}
```
"""
        
        prompt += """
## 执行要求
1. 按照任务描述完成工作
2. 遵循 Karpathy 四大核心原则：Think Before Coding, Simplicity First, Surgical Changes, Goal-Driven
3. 完成后输出工作总结

## 汇报格式
完成后请输出：
- 完成状态：DONE / DONE_WITH_CONCERNS / BLOCKED / NEEDS_CONTEXT
- 创建/修改的文件列表
- 工作总结
- 遗留问题（如有）
"""
        
        return prompt

    def _run_command(
        self,
        task_id: str,
        args: List[str],
        prompt: str,
    ) -> TraeCliResult:
        """执行 traecli 命令并收集结果"""
        started_at = datetime.now().isoformat()
        
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                encoding='utf-8',
                errors='replace',
                cwd=self.workspace,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            
            json_output = None
            if self.json_mode and result.stdout.strip():
                try:
                    json_output = json.loads(result.stdout.strip())
                except json.JSONDecodeError:
                    pass
            
            cli_result = TraeCliResult(
                task_id=task_id,
                prompt=prompt,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                json_output=json_output,
                started_at=started_at,
                completed_at=datetime.now().isoformat(),
            )
            
        except subprocess.TimeoutExpired as e:
            cli_result = TraeCliResult(
                task_id=task_id,
                prompt=prompt,
                exit_code=-1,
                stdout=e.stdout or "",
                stderr=f"Timeout after {self.timeout}s",
                started_at=started_at,
                completed_at=datetime.now().isoformat(),
            )
        except Exception as e:
            cli_result = TraeCliResult(
                task_id=task_id,
                prompt=prompt,
                exit_code=-1,
                stdout="",
                stderr=str(e),
                started_at=started_at,
                completed_at=datetime.now().isoformat(),
            )
        
        with self._lock:
            self._results[task_id] = cli_result
        
        return cli_result

    def check_available(self) -> Dict[str, Any]:
        """检查 traecli 是否可用"""
        try:
            result = subprocess.run(
                [self._traecli_path, "--help"],
                capture_output=True,
                text=True,
                timeout=10,
                encoding='utf-8',
                errors='replace',
            )
            return {
                "available": result.returncode == 0,
                "path": self._traecli_path,
                "workspace": self.workspace,
            }
        except Exception as e:
            return {
                "available": False,
                "path": self._traecli_path,
                "error": str(e),
            }

    def ensure_available(
        self,
        source: str = "auto",
        local_path: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        确保 traecli 可用，不可用则自动安装

        Args:
            source: 安装源 - "auto"(依次尝试 local→pip→git) / "pip" / "local" / "git"
            local_path: TraeCli 本地路径（source="local" 时必须）
            force: 是否强制重装

        Returns:
            {"available": bool, "path": str, "installed_now": bool, "method": str}
        """
        from traecli_setup import TraeCliSetup

        setup = TraeCliSetup(local_path=local_path, force=force)
        result = setup.ensure_available(source=source)

        if result.get("available"):
            self._traecli_path = result["path"]

        return result

    def doctor(self) -> Dict[str, Any]:
        """运行 traecli doctor 检查环境"""
        args = self._build_base_args()
        args.extend(["doctor"])
        
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            encoding='utf-8',
            errors='replace',
            cwd=self.workspace,
        )
        
        return {
            "exit_code": result.returncode,
            "output": result.stdout,
            "errors": result.stderr,
        }


if __name__ == '__main__':
    adapter = TraeCliAdapter()
    
    print("=" * 60)
    print("TraeCli Adapter - 子智能体调度适配器")
    print("=" * 60)
    
    check = adapter.check_available()
    print(f"\nTraeCli 可用: {check['available']}")
    print(f"TraeCli 路径: {check['path']}")
    print(f"工作目录: {check['workspace']}")
    
    if check['available']:
        print("\n运行 doctor 检查...")
        doc = adapter.doctor()
        print(doc.get('output', 'No output'))
    
    print("\n使用示例:")
    print("""
    from traecli_adapter import TraeCliAdapter
    
    adapter = TraeCliAdapter(workspace="e:/pro/myproject")
    
    # 单任务执行
    result = adapter.exec("将 FriendModel.lua 转换为 C#")
    print(result.answer)
    
    # 分派子智能体
    result = adapter.dispatch_agent(
        "solo-coder",
        "转换 Team 模块 Lua->C#",
        {"source_dir": "...", "namespace": "MyProject"}
    )
    
    # 并行分派多个子智能体
    results = adapter.dispatch_agents_parallel([
        {"agent_type": "solo-coder", "task_description": "转换 Team 模块"},
        {"agent_type": "solo-coder", "task_description": "转换 Friend 模块"},
        {"agent_type": "solo-coder", "task_description": "转换 Email 模块"},
        {"agent_type": "solo-coder", "task_description": "转换 Chat 模块"},
    ])
    """)
