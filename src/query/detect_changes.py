"""
detect_changes.py
git diff → 受影响的执行流
PR 自动影响面报告的核心逻辑（来自 GitNexus）
"""

from __future__ import annotations
import re
import subprocess
import logging
from pathlib import Path
from ..graph.store import Neo4jStore

logger = logging.getLogger(__name__)


def detect_from_diff(store: Neo4jStore, diff_hunks: str,
                     branch: str = "master") -> dict:
    """
    输入：git diff 文本（--unified=0 格式）
    输出：受影响的函数 + 执行流 + 建议测试范围
    """
    changed_functions = _parse_diff_to_functions(store, diff_hunks, branch)

    if not changed_functions:
        return {
            "changed_functions": [],
            "affected_processes": [],
            "affected_domains": [],
            "summary": "No tracked functions changed",
        }

    # 对每个变更函数查影响面
    all_affected_processes = set()
    all_affected_domains = set()
    impact_details = []

    for func in changed_functions:
        # 直接调用者
        callers = store.query("""
            MATCH (caller:Function)-[:CALLS*1..3]->(f:Function {id: $fid})
            RETURN DISTINCT caller.name AS name, caller.domain AS domain
            LIMIT 10
        """, {"fid": func["id"]})

        # 影响的执行流
        processes = store.query("""
            MATCH (f:Function {id: $fid})-[:PART_OF]->(p:Process)
            RETURN p.name AS name, p.domain AS domain, p.entryPoint AS entryPoint
        """, {"fid": func["id"]})

        for p in processes:
            all_affected_processes.add(p["name"])
            all_affected_domains.add(p["domain"])

        impact_details.append({
            "function": func,
            "caller_count": len(callers),
            "callers": callers[:5],
            "processes": processes,
        })

    return {
        "changed_functions": changed_functions,
        "impact_details": impact_details,
        "affected_processes": list(all_affected_processes),
        "affected_domains": list(all_affected_domains),
        "summary": _generate_summary(changed_functions, all_affected_processes, all_affected_domains),
    }


def detect_from_git(store: Neo4jStore, workspace: str,
                    base_ref: str = "HEAD~1",
                    branch: str = "master") -> dict:
    """从 git diff 自动获取变更，调用 detect_from_diff"""
    try:
        diff = subprocess.run(
            ["git", "-C", workspace, "diff", "--unified=0", base_ref, "HEAD",
             "--", "*.swift", "*.m", "*.h"],
            capture_output=True, text=True, check=True
        ).stdout
        return detect_from_diff(store, diff, branch)
    except subprocess.CalledProcessError as e:
        return {"error": f"git diff failed: {e.stderr}"}


def _parse_diff_to_functions(store: Neo4jStore, diff_text: str,
                              branch: str) -> list[dict]:
    """
    解析 diff 文本，提取变更的行号范围，
    映射到图中的函数节点
    """
    changed_functions: list[dict] = []
    seen_ids = set()

    current_file = None
    changed_lines: list[int] = []

    for line in diff_text.splitlines():
        # +++ b/path/to/file.swift
        if line.startswith("+++ b/"):
            if current_file and changed_lines:
                funcs = _lines_to_functions(store, current_file, changed_lines, branch)
                for f in funcs:
                    if f["id"] not in seen_ids:
                        changed_functions.append(f)
                        seen_ids.add(f["id"])

            current_file = line[6:].strip()
            changed_lines = []

        # @@ -a,b +c,d @@ 提取新增行号
        elif line.startswith("@@"):
            m = re.search(r'\+(\d+)(?:,(\d+))?', line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or 1)
                changed_lines.extend(range(start, start + count))

    # 处理最后一个文件
    if current_file and changed_lines:
        funcs = _lines_to_functions(store, current_file, changed_lines, branch)
        for f in funcs:
            if f["id"] not in seen_ids:
                changed_functions.append(f)
                seen_ids.add(f["id"])

    return changed_functions


def _lines_to_functions(store: Neo4jStore, file_path: str,
                         lines: list[int], branch: str) -> list[dict]:
    """根据变更行号，找到覆盖这些行的函数节点"""
    if not lines:
        return []

    min_line = min(lines)
    max_line = max(lines)

    return store.query("""
        MATCH (f:Function {filePath: $fp})
        WHERE f.lineStart <= $max_line AND f.lineEnd >= $min_line
        RETURN f.id AS id, f.name AS name,
               f.qualifiedName AS qualifiedName,
               f.domain AS domain,
               f.filePath AS filePath,
               f.lineStart AS lineStart
        LIMIT 10
    """, {"fp": file_path, "min_line": min_line, "max_line": max_line})


def _generate_summary(changed_funcs: list, processes: set, domains: set) -> str:
    """生成人类可读的影响面摘要（用于 PR 评论）"""
    lines = [
        f"**[Code Intelligence] 影响面分析**",
        f"修改函数：{', '.join(f['name'] for f in changed_funcs[:5])}",
    ]
    if processes:
        lines.append(f"受影响执行流：{', '.join(sorted(processes)[:5])}")
    if domains:
        lines.append(f"涉及业务域：{', '.join(sorted(domains))}")
    if not processes:
        lines.append("未检测到明显执行流影响（可能是工具函数或新增代码）")
    return "\n".join(lines)
