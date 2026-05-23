#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trae Agent Worker v2 - 通用子智能体执行引擎

架构设计：
1. 可插拔 TaskHandler 接口 - 每种任务类型一个 Handler
2. TraeCli 后端 - 通过 traecli exec 让 Trae AI 执行任务
3. 本地 Handler 后端 - 纯 Python 执行（如 LuaToCSharpConverter）
4. 自动后端选择 - 根据 task_context.json 中的 backend 字段

使用方法:
    # TraeCli 后端（让 Trae AI 执行）
    python trae_agent_worker.py --task-id AGENT-TEAM --backend traecli ...

    # 本地 LuaToCSharp 后端
    python trae_agent_worker.py --task-id AGENT-TEAM --backend local-lua2cs ...

    # 自动选择
    python trae_agent_worker.py --task-id AGENT-TEAM ...
"""

import io
import os
import sys
import json
import argparse
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any


class TaskHandler(ABC):
    """任务处理器抽象基类"""

    @abstractmethod
    def execute(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行任务

        Args:
            task_data: 任务数据，包含 task_id, agent_type, description, context

        Returns:
            Dict: {
                "status": "DONE" | "DONE_WITH_CONCERNS" | "FAILED" | "BLOCKED",
                "files_created": [...],
                "summary": str,
                "errors": [...],
                "concerns": [...],
            }
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass


class TraeCliHandler(TaskHandler):
    """
    TraeCli 后端 - 通过 traecli exec 让 Trae AI 执行任务

    这是通用后端，可以执行任何 AI 能理解的任务。
    每个 Worker 进程启动一个 traecli exec 子进程，
    Trae AI 会根据提示词自主完成工作。
    """

    def __init__(self, workspace: Optional[str] = None):
        self.workspace = workspace
        self._traecli_path = self._find_traecli()

    @property
    def name(self) -> str:
        return "traecli"

    def execute(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        task_id = task_data.get("task_id", "UNKNOWN")
        agent_type = task_data.get("agent_type", "solo-coder")
        description = task_data.get("description", "")
        context = task_data.get("context", {})

        workspace = context.get("workspace") or context.get("project_root") or self.workspace

        prompt = self._build_prompt(agent_type, description, context)

        args = self._build_args(prompt, workspace, context)

        print(f"[{datetime.now().isoformat()}] [INFO] Launching traecli exec for {task_id}")
        print(f"[{datetime.now().isoformat()}] [INFO] Workspace: {workspace}")
        print(f"[{datetime.now().isoformat()}] [INFO] Prompt length: {len(prompt)} chars")

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=context.get("timeout", 300),
                encoding='utf-8',
                errors='replace',
                cwd=workspace or os.getcwd(),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )

            answer = result.stdout.strip()
            json_output = None

            if answer:
                try:
                    json_output = json.loads(answer)
                except json.JSONDecodeError:
                    pass

            files_created = self._extract_files_from_output(answer, context)

            status = "DONE"
            errors = []
            if result.returncode != 0:
                status = "FAILED"
                errors.append(f"traecli exit code: {result.returncode}")
                if result.stderr:
                    errors.append(result.stderr[:500])

            print(f"[{datetime.now().isoformat()}] [DONE] traecli exec completed for {task_id}")
            print(f"[{datetime.now().isoformat()}] [INFO] Status: {status}, Files: {len(files_created)}")

            return {
                "status": status,
                "files_created": files_created,
                "files_count": len(files_created),
                "summary": answer[:2000] if answer else "",
                "errors": errors,
                "concerns": [],
                "raw_output": answer[:5000] if answer else "",
                "exit_code": result.returncode,
                "json_output": json_output,
            }

        except subprocess.TimeoutExpired:
            return {
                "status": "FAILED",
                "files_created": [],
                "files_count": 0,
                "summary": "Timeout",
                "errors": [f"traecli exec timed out"],
                "concerns": [],
            }
        except FileNotFoundError:
            return {
                "status": "FAILED",
                "files_created": [],
                "files_count": 0,
                "summary": "traecli not found",
                "errors": [f"traecli not found at: {self._traecli_path}"],
                "concerns": [],
            }
        except Exception as e:
            return {
                "status": "FAILED",
                "files_created": [],
                "files_count": 0,
                "summary": str(e),
                "errors": [str(e)],
                "concerns": [],
            }

    def _build_prompt(self, agent_type: str, description: str, context: Dict) -> str:
        role_map = {
            "solo-coder": "你是一位资深 Unity 游戏开发工程师，精通 C# 和 Lua，擅长将 Lua 代码转换为优雅的 C# 代码。遵循 SOLID 原则和 MVVM 模式。不添加注释。",
            "architect": "你是一位资深架构师，职责是设计系统性、前瞻性、可落地、可验证的架构。",
            "tester": "你是一位资深测试专家，职责是确保全面、深入、自动化、可量化的质量保障。",
        }

        role = role_map.get(agent_type, f"你是一位{agent_type}专家。")

        prompt = f"""# 子智能体任务

## 角色
{role}

## 任务
{description}
"""

        source_dir = context.get("source_dir", "")
        if source_dir:
            prompt += f"""
## 源文件目录
{source_dir}
"""

        output_dir = context.get("output_dir", source_dir)
        if output_dir:
            prompt += f"""
## 输出目录
{output_dir}
"""

        namespace = context.get("namespace", "MyProject")
        priority_order = context.get("priority_order", [])

        if namespace or priority_order:
            prompt += f"""
## 代码规范
- 命名空间: {namespace}
- 转换优先级: {' > '.join(priority_order) if priority_order else '按目录结构'}
- 使用 C# 属性替代 Lua getter/setter
- 使用 Dictionary<int, T> 替代 Lua table
- 使用 List<T> 替代 Lua array
- 使用 Action/Func 替代 Lua function
- 使用 int? 替代 nil
- 遵循 SOLID 原则
- 不添加注释
"""

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

    def _build_args(self, prompt: str, workspace: Optional[str], context: Dict) -> List[str]:
        args = [self._traecli_path, "--json", "--no-alt-screen"]

        if workspace:
            args.extend(["-C", workspace])

        args.extend(["exec", prompt])
        args.extend(["--mode", context.get("mode", "agent")])
        args.extend(["--answer-seconds", str(context.get("answer_seconds", 120))])
        args.append("--new-chat")

        add_files = context.get("add_files", [])
        for f in add_files:
            args.extend(["--add-file", f])

        return args

    def _extract_files_from_output(self, output: str, context: Dict) -> List[str]:
        """从 AI 输出中提取创建的文件列表"""
        files = []
        output_dir = context.get("output_dir", context.get("source_dir", ""))

        for line in output.split('\n'):
            line = line.strip()
            if line.endswith('.cs') or line.endswith('.py') or line.endswith('.lua'):
                clean = line.lstrip('- *•').strip()
                if len(clean) > 5 and not clean.startswith('#'):
                    if not os.path.isabs(clean) and output_dir:
                        clean = str(Path(output_dir) / clean)
                    files.append(clean)

        return files

    def _find_traecli(self) -> str:
        import shutil
        found = shutil.which("traecli")
        if found:
            return found
        return "traecli"


class LocalLuaToCSharpHandler(TaskHandler):
    """
    本地 Lua→C# 转换后端

    纯 Python 实现，不依赖 Trae AI。
    适用于结构化的批量转换任务。
    """

    TYPE_MAP = {
        'nil': 'null', 'true': 'true', 'false': 'false',
        'table': 'Dictionary<string, object>', 'number': 'int',
        'string': 'string', 'boolean': 'bool',
    }

    @property
    def name(self) -> str:
        return "local-lua2cs"

    def execute(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        import re

        context = task_data.get("context", {})
        source_dir = context.get("source_dir", "")
        output_dir = context.get("output_dir", source_dir)
        namespace = context.get("namespace", "MyProject")
        priority_order = context.get("priority_order", ['model', 'controller', 'facade', 'const', 'view'])

        if not source_dir or not Path(source_dir).exists():
            return {
                "status": "FAILED",
                "files_created": [],
                "errors": [f"Source dir not found: {source_dir}"],
                "concerns": [],
            }

        source_path = Path(source_dir)
        lua_files = list(source_path.rglob("*.lua"))

        if priority_order:
            def sort_key(f):
                rel = str(f.relative_to(source_path))
                for i, prefix in enumerate(priority_order):
                    if prefix in rel:
                        return i
                return len(priority_order)
            lua_files.sort(key=sort_key)

        files_created = []
        errors = []
        concerns = []

        for lua_file in lua_files:
            try:
                with open(lua_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                class_info = self._parse_lua_class(lua_file, content)
                if not class_info:
                    concerns.append(f"Skipped non-class file: {lua_file}")
                    continue

                cs_content = self._generate_csharp(class_info, namespace)
                rel_path = lua_file.relative_to(source_path)
                cs_path = Path(output_dir) / rel_path.with_suffix('.cs')
                cs_path.parent.mkdir(parents=True, exist_ok=True)

                with open(cs_path, 'w', encoding='utf-8') as f:
                    f.write(cs_content)

                files_created.append(str(cs_path))

            except Exception as e:
                errors.append(f"Failed {lua_file}: {e}")

        status = "DONE"
        if errors and not files_created:
            status = "FAILED"
        elif errors:
            status = "DONE_WITH_CONCERNS"

        return {
            "status": status,
            "files_created": files_created,
            "files_count": len(files_created),
            "errors": errors,
            "concerns": concerns,
        }

    def _parse_lua_class(self, lua_path, content):
        import re
        class_match = re.search(
            r'local\s+M\s*=\s*(?:class|moclass)\s*\(\s*["\'](\w+)["\']\s*(?:,\s*(\w+))?\s*\)',
            content
        )
        if not class_match:
            return None

        return {
            'name': class_match.group(1),
            'base_class': class_match.group(2) or 'object',
            'fields': self._parse_fields(content),
            'methods': self._parse_methods(content),
            'source_file': str(lua_path),
        }

    def _parse_fields(self, content):
        import re
        fields = []
        seen = set()
        for match in re.finditer(r'self\.(_\w+)\s*=\s*(.+?)(?:\s*--\s*(.+))?$', content, re.MULTILINE):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                fields.append({'name': name, 'value': match.group(2).strip(), 'type': self._infer_type(match.group(2).strip())})
        return fields

    def _parse_methods(self, content):
        import re
        methods = []
        for match in re.finditer(r'function\s+M[:.]\s*(\w+)\s*\(([^)]*)\)', content):
            name = match.group(1)
            if name in ('ctor', 'reset', 'clear', 'init'):
                continue
            methods.append({'name': name, 'params': match.group(2).strip(), 'is_private': name.startswith('_')})
        return methods

    def _infer_type(self, value):
        value = value.strip()
        if value in ('nil', 'None'): return 'object'
        if value in ('true', 'false'): return 'bool'
        import re
        if re.match(r'^-?\d+$', value): return 'int'
        if re.match(r'^-?\d+\.\d+$', value): return 'float'
        if value.startswith('"') or value.startswith("'"): return 'string'
        if value.startswith('{'): return 'List<object>'
        return 'object'

    def _generate_csharp(self, class_info, namespace):
        lines = ["using System;", "using System.Collections.Generic;", "", f"namespace {namespace}", "{"]
        base_str = f" : {class_info['base_class']}" if class_info['base_class'] != 'object' else ""
        lines.append(f"    public class {class_info['name']}{base_str}")
        lines.append("    {")

        for f in class_info['fields']:
            fname, ftype = f['name'], f['type']
            fvalue = self._convert_value(f['value'])
            nullable = '?' if ftype in ('int', 'float', 'bool') and fvalue == 'null' else ''
            lines.append(f"        private {ftype}{nullable} {fname} = {fvalue};")

        if class_info['fields']:
            lines.append("")

        for m in class_info['methods']:
            access = "private" if m['is_private'] else "public"
            mname = m['name'].lstrip('_')
            mname = ''.join(p.capitalize() for p in mname.split('_'))
            params = ', '.join(f"object {p.strip()}" for p in m['params'].split(',') if p.strip() and p.strip() != 'self') if m['params'] else ''
            lines.extend([f"        {access} void {mname}({params})", "        {", "        }", ""])

        lines.extend(["    }", "}"])
        return "\n".join(lines)

    def _convert_value(self, value):
        value = value.strip()
        if value == 'nil': return 'null'
        if value == 'true': return 'true'
        if value == 'false': return 'false'
        if value == '{}': return 'new List<object>()'
        import re
        if re.match(r'^-?\d+$', value): return value
        if re.match(r'^-?\d+\.\d+$', value): return value + 'f'
        if value.startswith("'"): return value.replace("'", '"')
        return value


class ShellCommandHandler(TaskHandler):
    """
    Shell 命令后端 - 执行任意 shell 命令

    适用于脚本化任务，如编译、测试、部署等。
    """

    @property
    def name(self) -> str:
        return "shell"

    def execute(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        context = task_data.get("context", {})
        command = context.get("command", "")
        cwd = context.get("cwd", os.getcwd())

        if not command:
            return {
                "status": "FAILED",
                "files_created": [],
                "errors": ["No command specified"],
                "concerns": [],
            }

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=context.get("timeout", 300),
                encoding='utf-8',
                errors='replace',
                cwd=cwd,
            )

            return {
                "status": "DONE" if result.returncode == 0 else "FAILED",
                "files_created": [],
                "summary": result.stdout[:2000],
                "errors": [result.stderr[:500]] if result.stderr else [],
                "concerns": [],
                "exit_code": result.returncode,
            }
        except Exception as e:
            return {
                "status": "FAILED",
                "files_created": [],
                "errors": [str(e)],
                "concerns": [],
            }


HANDLER_REGISTRY: Dict[str, type] = {
    "traecli": TraeCliHandler,
    "local-lua2cs": LocalLuaToCSharpHandler,
    "shell": ShellCommandHandler,
}


def resolve_backend(task_data: Dict[str, Any]) -> str:
    """
    自动选择后端

    优先级：
    1. task_data["context"]["backend"] 显式指定
    2. task_data["backend"] 顶层指定
    3. 根据 agent_type 和 context 自动推断
    """
    context = task_data.get("context", {})

    explicit = context.get("backend") or task_data.get("backend")
    if explicit and explicit in HANDLER_REGISTRY:
        return explicit

    source_dir = context.get("source_dir", "")
    if source_dir and Path(source_dir).exists():
        lua_count = len(list(Path(source_dir).rglob("*.lua")))
        if lua_count > 0:
            return "local-lua2cs"

    return "traecli"


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description='Trae Agent Worker v2')
    parser.add_argument('--task-id', required=True, help='Task ID')
    parser.add_argument('--agent-type', required=True, help='Agent type')
    parser.add_argument('--task-dir', required=True, help='Task dir')
    parser.add_argument('--result-file', required=True, help='Result file')
    parser.add_argument('--log-file', required=True, help='Log file')
    parser.add_argument('--backend', choices=list(HANDLER_REGISTRY.keys()) + ['auto'],
                       default='auto', help='Backend to use')

    args = parser.parse_args()

    print(f"[{datetime.now().isoformat()}] [START] Worker v2: {args.task_id}")
    print(f"[{datetime.now().isoformat()}] [INFO] Agent: {args.agent_type}")
    print(f"[{datetime.now().isoformat()}] [INFO] Backend: {args.backend}")

    context_file = Path(args.task_dir) / "task_context.json"
    if not context_file.exists():
        print(f"[ERROR] Context file not found: {context_file}")
        _write_result(args.result_file, {
            "status": "FAILED",
            "errors": [f"Context file not found: {context_file}"],
            "files_created": []
        })
        return

    with open(context_file, 'r', encoding='utf-8') as f:
        task_data = json.load(f)

    backend = args.backend
    if backend == 'auto':
        backend = resolve_backend(task_data)

    print(f"[{datetime.now().isoformat()}] [INFO] Resolved backend: {backend}")

    handler_class = HANDLER_REGISTRY.get(backend)
    if not handler_class:
        print(f"[ERROR] Unknown backend: {backend}")
        _write_result(args.result_file, {
            "status": "FAILED",
            "errors": [f"Unknown backend: {backend}"],
            "files_created": []
        })
        return

    workspace = task_data.get("context", {}).get("workspace") or task_data.get("context", {}).get("project_root")
    handler = handler_class(workspace=workspace) if backend == "traecli" else handler_class()

    print(f"[{datetime.now().isoformat()}] [INFO] Handler: {handler.name}")
    print(f"[{datetime.now().isoformat()}] [INFO] Starting execution...")

    result = handler.execute(task_data)

    print(f"[{datetime.now().isoformat()}] [DONE] Execution complete")
    print(f"[{datetime.now().isoformat()}] [INFO] Status: {result.get('status')}")
    print(f"[{datetime.now().isoformat()}] [INFO] Files: {result.get('files_count', len(result.get('files_created', [])))}")

    if result.get('errors'):
        print(f"[{datetime.now().isoformat()}] [WARN] Errors: {len(result['errors'])}")

    _write_result(args.result_file, result)

    print(f"[{datetime.now().isoformat()}] [END] Worker done: {args.task_id}")


def _write_result(result_file: str, result: Dict[str, Any]):
    result_path = Path(result_file)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with open(result_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


if __name__ == '__main__':
    main()
