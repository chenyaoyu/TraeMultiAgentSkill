# 子智能体调度员执行模板

> 当模型需要分派子智能体并行执行任务时，使用此模板构建调度方案

## 调度方案模板

```markdown
# 调度方案: {任务名称}

## 1. 任务分解

| 子任务 | 智能体 | 后端 | 描述 |
|--------|--------|------|------|
| {ID-1} | {agent_type} | {backend} | {description} |
| {ID-2} | {agent_type} | {backend} | {description} |

## 2. 依赖关系

- 无依赖 → 全部并行
- 有依赖 → 标注前置任务

## 3. 上下文

| 子任务 | source_dir | namespace | workspace | 附加文件 |
|--------|-----------|-----------|-----------|---------|
| {ID-1} | ... | ... | ... | ... |

## 4. 预期产出

| 子任务 | 预期文件数 | 预期状态 |
|--------|-----------|---------|
| {ID-1} | N | DONE |

## 5. 失败处理

- 重试策略: {重试次数/不重试}
- 降级方案: {traecli → worker/local-lua2cs}
- 上报条件: {连续失败N次}
```

## Python 调度代码模板

```python
import sys, os
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "..", "scripts"
))

from trae_task_tool import TraeTaskTool

tool = TraeTaskTool(
    backend="{traecli|worker}",
    workspace="{project_root}",
    max_parallel={N},
)

task_ids = tool.dispatch_agents([
    {
        "agent_type": "{agent_type}",
        "description": "{task_description}",
        "context": {
            "source_dir": "{source_dir}",
            "namespace": "{namespace}",
            "workspace": "{project_root}",
            "priority_order": ["model", "controller", "facade", "const", "view"],
            "worker_backend": "{auto|traecli|local-lua2cs|shell}",
        },
    },
])

results = tool.wait_for_all(task_ids, timeout=600)
summary = tool.collect_results(task_ids)

total = summary["summary"]["total_tasks"]
files = summary["summary"]["total_files"]
errors = summary["summary"]["total_errors"]
print(f"Tasks: {total}, Files: {files}, Errors: {errors}")

for task_id, status in summary["summary"]["statuses"].items():
    result = summary["results"].get(task_id, {})
    file_count = result.get("files_count", len(result.get("files_created", [])))
    print(f"  {task_id}: {status} ({file_count} files)")
```

## 调度检查清单

分派前必须确认：

- [ ] 每个子任务可独立完成
- [ ] 上下文信息完整（路径存在、命名空间正确）
- [ ] 后端选择合理（结构化→worker，AI驱动→traecli）
- [ ] 超时设置合理（批量转换60s，AI任务300s）
- [ ] 并行数不超过资源限制

完成后必须确认：

- [ ] 所有子任务状态已收集
- [ ] 失败任务已处理（重试/降级/上报）
- [ ] 产出文件路径正确
- [ ] 向用户输出了汇总报告
