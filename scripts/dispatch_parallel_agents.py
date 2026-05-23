#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
并行子智能体调度脚本

一键启动 4 个并行子 agent，分别负责 Team/Friend/Email/Chat 模块的 Lua→C# 转换。

使用方法:
    python dispatch_parallel_agents.py
    
    # 自定义项目根目录
    python dispatch_parallel_agents.py --project-root "e:\pro\pro_c3pro\trunk\pro_sfps"
"""

import os
import sys
import json
import time
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any


AGENTS = [
    {
        "id": "AGENT-TEAM",
        "agent_type": "solo-coder",
        "description": "编队模块(team) Lua→C# 转换",
        "source_dir": "Assets/cs_runtime/hotfix/project/services/team",
        "priority_order": ["model", "controller", "facade", "const", "view"],
    },
    {
        "id": "AGENT-FRIEND",
        "agent_type": "solo-coder",
        "description": "好友模块(friend) Lua→C# 转换",
        "source_dir": "Assets/cs_runtime/hotfix/project/services/friend",
        "priority_order": ["model", "controller", "agent", "facade", "view"],
    },
    {
        "id": "AGENT-EMAIL",
        "agent_type": "solo-coder",
        "description": "邮件模块(email) Lua→C# 转换",
        "source_dir": "Assets/cs_runtime/hotfix/project/services/email",
        "priority_order": ["model", "controller", "agent", "config", "view"],
    },
    {
        "id": "AGENT-CHAT",
        "agent_type": "solo-coder",
        "description": "聊天模块(chat) Lua→C# 转换",
        "source_dir": "Assets/cs_runtime/hotfix/project/services/chat",
        "priority_order": ["model", "controller", "agent", "facade", "view"],
    },
]


def log(msg: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARNING": "⚠️", "AGENT": "🤖"}
    icon = icons.get(level, "")
    print(f"[{timestamp}] {icon} {msg}")


def dispatch_agent(agent_config: Dict, project_root: str, skill_root: str) -> Dict[str, Any]:
    """
    启动一个子智能体进程
    
    Returns:
        Dict: {"process": subprocess.Popen, "task_id": str, "result_file": str}
    """
    agent_id = agent_config["id"]
    source_dir = str(Path(project_root) / agent_config["source_dir"])
    
    task_dir = Path(skill_root) / "context" / "tasks" / agent_id
    task_dir.mkdir(parents=True, exist_ok=True)
    
    result_dir = Path(skill_root) / "context" / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    
    result_file = result_dir / f"{agent_id}.json"
    log_file = task_dir / "execution.log"
    
    context = {
        "source_dir": source_dir,
        "output_dir": source_dir,
        "namespace": "MyProject",
        "priority_order": agent_config["priority_order"],
        "agent_id": agent_id,
        "project_root": project_root,
    }
    
    context_file = task_dir / "task_context.json"
    with open(context_file, 'w', encoding='utf-8') as f:
        json.dump({
            "task_id": agent_id,
            "agent_type": agent_config["agent_type"],
            "description": agent_config["description"],
            "context": context,
            "created_at": datetime.now().isoformat(),
            "status": "running"
        }, f, indent=2, ensure_ascii=False)
    
    worker_script = Path(skill_root) / "scripts" / "trae_agent_worker.py"
    
    cmd = [
        sys.executable,
        str(worker_script),
        "--task-id", agent_id,
        "--agent-type", agent_config["agent_type"],
        "--task-dir", str(task_dir),
        "--result-file", str(result_file),
        "--log-file", str(log_file)
    ]
    
    log(f"启动子智能体 {agent_id}: {agent_config['description']}", "AGENT")
    log(f"  源目录: {source_dir}", "INFO")
    log(f"  结果文件: {result_file}", "INFO")
    
    try:
        with open(log_file, 'w', encoding='utf-8') as log_f:
            process = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                cwd=str(skill_root),
                env={**os.environ, "TRAE_TASK_ID": agent_id}
            )
        
        return {
            "process": process,
            "task_id": agent_id,
            "result_file": str(result_file),
            "log_file": str(log_file),
            "pid": process.pid,
            "status": "running"
        }
    except Exception as e:
        log(f"启动子智能体 {agent_id} 失败: {e}", "ERROR")
        return {
            "process": None,
            "task_id": agent_id,
            "result_file": str(result_file),
            "error": str(e),
            "status": "failed"
        }


def monitor_agents(agent_processes: List[Dict], timeout: int = 600,
                   poll_interval: int = 10) -> Dict[str, Any]:
    """
    监控所有子智能体的执行状态
    
    Returns:
        Dict: 汇总结果
    """
    start_time = time.time()
    results = {}
    
    while time.time() - start_time < timeout:
        all_done = True
        
        for agent_info in agent_processes:
            task_id = agent_info["task_id"]
            
            if task_id in results:
                continue
            
            result_file = Path(agent_info["result_file"])
            if result_file.exists():
                try:
                    with open(result_file, 'r', encoding='utf-8') as f:
                        result = json.load(f)
                    results[task_id] = result
                    status = result.get("status", "UNKNOWN")
                    files_count = result.get("files_count", 0)
                    log(f"{task_id} 完成: 状态={status}, 文件数={files_count}", "SUCCESS")
                except Exception as e:
                    results[task_id] = {"status": "ERROR", "error": str(e)}
                    log(f"{task_id} 读取结果失败: {e}", "ERROR")
                continue
            
            process = agent_info.get("process")
            if process and process.poll() is None:
                all_done = False
            elif process and process.poll() is not None:
                exit_code = process.returncode
                if task_id not in results:
                    if exit_code == 0:
                        log(f"{task_id} 进程已退出(代码0)，等待结果文件...", "INFO")
                    else:
                        log(f"{task_id} 进程异常退出(代码{exit_code})", "ERROR")
                        log_file = Path(agent_info.get("log_file", ""))
                        if log_file.exists():
                            try:
                                with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                                    log_content = f.read()[-500:]
                                log(f"{task_id} 日志尾部:\n{log_content}", "ERROR")
                            except Exception:
                                log(f"{task_id} 无法读取日志文件", "ERROR")
                        results[task_id] = {
                            "status": "FAILED",
                            "error": f"进程退出码: {exit_code}",
                            "files_created": []
                        }
        
        if all_done and len(results) == len(agent_processes):
            break
        
        elapsed = int(time.time() - start_time)
        running = [a["task_id"] for a in agent_processes if a["task_id"] not in results]
        if running:
            log(f"等待中... ({elapsed}s) 运行中: {', '.join(running)}", "INFO")
        
        time.sleep(poll_interval)
    
    for agent_info in agent_processes:
        task_id = agent_info["task_id"]
        if task_id not in results:
            results[task_id] = {"status": "TIMEOUT", "files_created": []}
            log(f"{task_id} 超时!", "ERROR")
    
    return results


def print_summary(results: Dict[str, Any]):
    """打印汇总报告"""
    print("\n" + "=" * 70)
    print("📊 子智能体并行执行汇总报告")
    print("=" * 70)
    
    total_files = 0
    total_errors = 0
    
    for task_id, result in results.items():
        status = result.get("status", "UNKNOWN")
        files = result.get("files_created", [])
        errors = result.get("errors", [])
        
        status_icon = {"DONE": "✅", "DONE_WITH_CONCERNS": "⚠️", "FAILED": "❌"}.get(status, "❓")
        
        print(f"\n{status_icon} {task_id}: {status}")
        print(f"   生成文件: {len(files)} 个")
        total_files += len(files)
        
        if files:
            for f in files[:8]:
                print(f"     - {Path(f).name}")
            if len(files) > 8:
                print(f"     ... 还有 {len(files) - 8} 个文件")
        
        if errors:
            print(f"   错误: {len(errors)} 个")
            total_errors += len(errors)
            for e in errors[:3]:
                print(f"     - {e[:80]}")
    
    print(f"\n{'=' * 70}")
    print(f"📈 总计: 生成 {total_files} 个 C# 文件, {total_errors} 个错误")
    print(f"{'=' * 70}")


def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    
    parser = argparse.ArgumentParser(description='Parallel Agent Dispatcher')
    parser.add_argument('--project-root', type=str,
                       default=r'e:\pro\pro_c3pro\trunk\pro_sfps',
                       help='项目根目录')
    parser.add_argument('--skill-root', type=str,
                       default=r'e:\pro\trace_projects\c1proACT\.trae\skills\multi-agent-team',
                       help='Skill 根目录')
    parser.add_argument('--timeout', type=int, default=600,
                       help='超时时间(秒)')
    parser.add_argument('--poll-interval', type=int, default=5,
                       help='轮询间隔(秒)')
    parser.add_argument('--agents', type=str, nargs='+',
                       choices=['team', 'friend', 'email', 'chat', 'all'],
                       default=['all'],
                       help='要启动的子智能体')
    
    args = parser.parse_args()
    
    log("🚀 并行子智能体调度脚本启动", "SUCCESS")
    log(f"📁 项目根目录: {args.project_root}", "INFO")
    log(f"📁 Skill 根目录: {args.skill_root}", "INFO")
    
    selected_agents = AGENTS
    if 'all' not in args.agents:
        selected_agents = [a for a in AGENTS if a["id"].replace("AGENT-", "").lower() in args.agents]
    
    log(f"🤖 将启动 {len(selected_agents)} 个子智能体", "INFO")
    
    agent_processes = []
    for agent_config in selected_agents:
        result = dispatch_agent(agent_config, args.project_root, args.skill_root)
        agent_processes.append(result)
        if result.get("pid"):
            log(f"  PID: {result['pid']}", "INFO")
    
    log(f"\n⏳ 等待所有子智能体完成 (超时: {args.timeout}s)...", "INFO")
    
    results = monitor_agents(agent_processes, args.timeout, args.poll_interval)
    
    print_summary(results)
    
    summary_file = Path(args.skill_root) / "context" / "results" / "parallel_summary.json"
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "agents": results,
            "total_files": sum(len(r.get("files_created", [])) for r in results.values()),
            "total_errors": sum(len(r.get("errors", [])) for r in results.values()),
        }, f, indent=2, ensure_ascii=False)
    
    log(f"\n📄 汇总报告已保存: {summary_file}", "SUCCESS")


if __name__ == '__main__':
    main()
