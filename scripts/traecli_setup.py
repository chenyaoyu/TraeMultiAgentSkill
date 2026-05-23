#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TraeCli Setup - 自动检测与安装 TraeCli 环境

功能：
1. 检测 traecli 是否已安装
2. 自动安装 TraeCli（pip install）
3. 验证安装结果
4. 输出环境诊断信息

使用方法:
    # 检测 + 自动安装
    python traecli_setup.py

    # 仅检测
    python traecli_setup.py --check-only

    # 强制重装
    python traecli_setup.py --force

    # 指定安装源
    python traecli_setup.py --source local --local-path "D:/tools/TraeCli/agent-harness"

    # 诊断模式
    python traecli_setup.py --doctor
"""

import io
import os
import sys
import json
import shutil
import subprocess
import platform
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any


TRAECLI_REPO = "https://github.com/firerlAGI/TraeCli.git"
TRAECLI_PIP_PKG = "traecli"
TRAECLI_LOCAL_SUBPATH = "agent-harness"

MIN_PYTHON_VERSION = (3, 8)


def _log(msg: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = {
        "INFO": "[INFO]",
        "OK": "[OK]",
        "WARN": "[WARN]",
        "ERROR": "[ERROR]",
        "STEP": "[STEP]",
    }.get(level, "[INFO]")
    print(f"[{timestamp}] {prefix} {msg}")


class TraeCliSetup:
    """TraeCli 环境检测与安装"""

    def __init__(
        self,
        local_path: Optional[str] = None,
        force: bool = False,
        verbose: bool = False,
    ):
        self.force = force
        self.verbose = verbose
        self.local_path = local_path
        self.platform = platform.system()
        self.python_exe = sys.executable

    def check_installed(self) -> Dict[str, Any]:
        """检测 traecli 是否已安装"""
        result = {
            "installed": False,
            "path": None,
            "version": None,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": self.platform,
        }

        found = shutil.which("traecli")
        if found:
            result["installed"] = True
            result["path"] = found
            try:
                ver_output = subprocess.run(
                    [found, "--version"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace",
                )
                if ver_output.returncode == 0:
                    result["version"] = ver_output.stdout.strip()
            except Exception:
                pass
            return result

        python_base = Path(sys.executable).parent
        candidates = []
        if self.platform == "Windows":
            candidates = [
                python_base / "Scripts" / "traecli.exe",
                python_base / "traecli.exe",
            ]
        else:
            candidates = [
                python_base / "traecli",
                Path.home() / ".local" / "bin" / "traecli",
                Path("/usr/local/bin/traecli"),
                python_base / "Scripts" / "traecli",
            ]

        for c in candidates:
            if c.exists():
                result["installed"] = True
                result["path"] = str(c)
                break

        return result

    def check_python_version(self) -> bool:
        """检查 Python 版本是否满足要求"""
        return sys.version_info >= MIN_PYTHON_VERSION

    def check_pip_available(self) -> bool:
        """检查 pip 是否可用"""
        try:
            result = subprocess.run(
                [self.python_exe, "-m", "pip", "--version"],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
            )
            return result.returncode == 0
        except Exception:
            return False

    def check_git_available(self) -> bool:
        """检查 git 是否可用"""
        found = shutil.which("git")
        if found:
            return True
        try:
            result = subprocess.run(
                ["git", "--version"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )
            return result.returncode == 0
        except Exception:
            return False

    def install_from_pip(self) -> Dict[str, Any]:
        """通过 pip 安装 traecli"""
        _log("通过 pip 安装 traecli ...", "STEP")
        try:
            result = subprocess.run(
                [self.python_exe, "-m", "pip", "install", "--upgrade", TRAECLI_PIP_PKG],
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace",
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "method": "pip",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "pip install timeout (300s)", "method": "pip"}
        except Exception as e:
            return {"success": False, "error": str(e), "method": "pip"}

    def install_from_local(self) -> Dict[str, Any]:
        """从本地路径安装 traecli"""
        if not self.local_path:
            return {"success": False, "error": "No local path specified", "method": "local"}

        harness_path = Path(self.local_path)
        if not harness_path.exists():
            return {"success": False, "error": f"Path not found: {harness_path}", "method": "local"}

        if not (harness_path / "setup.py").exists() and not (harness_path / "pyproject.toml").exists():
            sub = harness_path / TRAECLI_LOCAL_SUBPATH
            if sub.exists() and (sub / "setup.py" or sub / "pyproject.toml").exists():
                harness_path = sub
            else:
                return {
                    "success": False,
                    "error": f"No setup.py/pyproject.toml in {harness_path} or {sub}",
                    "method": "local",
                }

        _log(f"从本地路径安装: {harness_path}", "STEP")
        try:
            result = subprocess.run(
                [self.python_exe, "-m", "pip", "install", "-e", str(harness_path)],
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace",
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "method": "local",
                "path": str(harness_path),
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "pip install -e timeout (300s)", "method": "local"}
        except Exception as e:
            return {"success": False, "error": str(e), "method": "local"}

    def install_from_git(self) -> Dict[str, Any]:
        """从 GitHub 克隆并安装 traecli"""
        if not self.check_git_available():
            return {"success": False, "error": "git not available", "method": "git"}

        _log(f"从 GitHub 克隆 TraeCli: {TRAECLI_REPO}", "STEP")
        clone_dir = Path.home() / ".traecli_setup_tmp"

        try:
            if clone_dir.exists():
                shutil.rmtree(clone_dir)

            clone_result = subprocess.run(
                ["git", "clone", "--depth", "1", TRAECLI_REPO, str(clone_dir)],
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace",
            )
            if clone_result.returncode != 0:
                return {
                    "success": False,
                    "error": f"git clone failed: {clone_result.stderr}",
                    "method": "git",
                }

            harness_path = clone_dir / TRAECLI_LOCAL_SUBPATH
            if not harness_path.exists():
                return {
                    "success": False,
                    "error": f"agent-harness not found in cloned repo",
                    "method": "git",
                }

            install_result = subprocess.run(
                [self.python_exe, "-m", "pip", "install", "-e", str(harness_path)],
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace",
            )

            shutil.rmtree(clone_dir, ignore_errors=True)

            return {
                "success": install_result.returncode == 0,
                "stdout": install_result.stdout,
                "stderr": install_result.stderr,
                "method": "git",
            }
        except subprocess.TimeoutExpired:
            shutil.rmtree(clone_dir, ignore_errors=True)
            return {"success": False, "error": "git clone/install timeout", "method": "git"}
        except Exception as e:
            shutil.rmtree(clone_dir, ignore_errors=True)
            return {"success": False, "error": str(e), "method": "git"}

    def verify_installation(self) -> Dict[str, Any]:
        """验证安装结果"""
        check = self.check_installed()
        if not check["installed"]:
            return {"success": False, "error": "traecli not found after installation"}

        try:
            result = subprocess.run(
                [check["path"], "--help"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )
            check["help_ok"] = result.returncode == 0
        except Exception as e:
            check["help_ok"] = False
            check["help_error"] = str(e)

        check["success"] = check["installed"] and check.get("help_ok", False)
        return check

    def doctor(self) -> Dict[str, Any]:
        """完整环境诊断"""
        diag = {
            "timestamp": datetime.now().isoformat(),
            "platform": self.platform,
            "python": {
                "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "executable": self.python_exe,
                "version_ok": self.check_python_version(),
            },
            "pip": {
                "available": self.check_pip_available(),
            },
            "git": {
                "available": self.check_git_available(),
            },
            "traecli": self.check_installed(),
        }

        if diag["traecli"]["installed"]:
            try:
                doc_result = subprocess.run(
                    [diag["traecli"]["path"], "doctor"],
                    capture_output=True, text=True, timeout=30,
                    encoding="utf-8", errors="replace",
                )
                diag["traecli"]["doctor"] = {
                    "exit_code": doc_result.returncode,
                    "output": doc_result.stdout[:2000],
                }
            except Exception as e:
                diag["traecli"]["doctor"] = {"error": str(e)}

        diag["ready"] = (
            diag["python"]["version_ok"]
            and diag["pip"]["available"]
            and diag["traecli"]["installed"]
        )

        return diag

    def ensure_available(self, source: str = "auto") -> Dict[str, Any]:
        """
        确保 traecli 可用，不可用则自动安装

        Args:
            source: 安装源
                - "auto": 依次尝试 local → pip → git
                - "pip": 仅 pip 安装
                - "local": 仅本地安装（需指定 local_path）
                - "git": 仅 git 克隆安装

        Returns:
            Dict: {"available": bool, "path": str, "installed_now": bool, "method": str}
        """
        check = self.check_installed()
        if check["installed"] and not self.force:
            _log(f"traecli 已安装: {check['path']}", "OK")
            return {
                "available": True,
                "path": check["path"],
                "installed_now": False,
                "method": "existing",
            }

        if not self.check_python_version():
            _log(f"Python 版本过低: {sys.version_info}, 需要 >= {MIN_PYTHON_VERSION}", "ERROR")
            return {"available": False, "error": "Python version too low"}

        if not self.check_pip_available():
            _log("pip 不可用，无法自动安装", "ERROR")
            return {"available": False, "error": "pip not available"}

        install_methods = []
        if source == "auto":
            if self.local_path:
                install_methods.append(("local", self.install_from_local))
            install_methods.append(("pip", self.install_from_pip))
            if self.check_git_available():
                install_methods.append(("git", self.install_from_git))
        elif source == "pip":
            install_methods.append(("pip", self.install_from_pip))
        elif source == "local":
            install_methods.append(("local", self.install_from_local))
        elif source == "git":
            install_methods.append(("git", self.install_from_git))

        for method_name, method_func in install_methods:
            _log(f"尝试安装方式: {method_name}", "STEP")
            result = method_func()

            if result.get("success"):
                verify = self.verify_installation()
                if verify.get("success"):
                    _log(f"traecli 安装成功 ({method_name}): {verify['path']}", "OK")
                    return {
                        "available": True,
                        "path": verify["path"],
                        "installed_now": True,
                        "method": method_name,
                    }
                else:
                    _log(f"{method_name} 安装后验证失败", "WARN")
                    if self.verbose and result.get("stderr"):
                        _log(f"  stderr: {result['stderr'][:500]}", "WARN")
            else:
                _log(f"{method_name} 安装失败: {result.get('error', 'unknown')}", "WARN")
                if self.verbose and result.get("stderr"):
                    _log(f"  stderr: {result['stderr'][:500]}", "WARN")

        _log("所有安装方式均失败", "ERROR")
        return {
            "available": False,
            "error": "All install methods failed",
            "attempts": [m[0] for m in install_methods],
        }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="TraeCli 环境检测与安装")
    parser.add_argument("--check-only", action="store_true", help="仅检测，不安装")
    parser.add_argument("--force", action="store_true", help="强制重装")
    parser.add_argument("--source", default="auto", choices=["auto", "pip", "local", "git"],
                        help="安装源 (默认: auto)")
    parser.add_argument("--local-path", default=None,
                        help="TraeCli 本地路径 (用于 local 安装源)")
    parser.add_argument("--doctor", action="store_true", help="运行完整环境诊断")
    parser.add_argument("--verbose", action="store_true", help="详细输出")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args()

    setup = TraeCliSetup(
        local_path=args.local_path,
        force=args.force,
        verbose=args.verbose,
    )

    if args.doctor:
        diag = setup.doctor()
        if args.json:
            print(json.dumps(diag, indent=2, ensure_ascii=False))
        else:
            _log("=== TraeCli 环境诊断 ===")
            _log(f"平台: {diag['platform']}")
            _log(f"Python: {diag['python']['version']} ({'OK' if diag['python']['version_ok'] else 'TOO LOW'})")
            _log(f"pip: {'OK' if diag['pip']['available'] else 'NOT AVAILABLE'}")
            _log(f"git: {'OK' if diag['git']['available'] else 'NOT AVAILABLE'}")
            tc = diag["traecli"]
            _log(f"traecli: {'OK' if tc['installed'] else 'NOT INSTALLED'}" +
                 (f" ({tc['path']})" if tc.get("path") else ""))
            if tc.get("doctor"):
                doc = tc["doctor"]
                if doc.get("output"):
                    print(doc["output"][:1000])
            _log(f"总体状态: {'READY' if diag['ready'] else 'NOT READY'}")
        return

    if args.check_only:
        check = setup.check_installed()
        if args.json:
            print(json.dumps(check, indent=2, ensure_ascii=False))
        else:
            if check["installed"]:
                _log(f"traecli 已安装: {check['path']}", "OK")
                if check.get("version"):
                    _log(f"版本: {check['version']}")
            else:
                _log("traecli 未安装", "WARN")
        return

    result = setup.ensure_available(source=args.source)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if result["available"]:
            if result["installed_now"]:
                _log(f"traecli 安装成功! 路径: {result['path']} (方式: {result['method']})", "OK")
            else:
                _log(f"traecli 已就绪: {result['path']}", "OK")
        else:
            _log(f"traecli 安装失败: {result.get('error', 'unknown')}", "ERROR")
            _log("请手动安装: pip install traecli", "WARN")
            _log("或从源码安装: pip install -e tools/TraeCli/agent-harness", "WARN")

    sys.exit(0 if result["available"] else 1)


if __name__ == "__main__":
    main()
