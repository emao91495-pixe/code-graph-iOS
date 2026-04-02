"""
extractor.py
Converts ParseResult into graph (node, edge) records for Neo4j ingestion.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml

from .swift_parser import ParseResult as SwiftResult
from .objc_parser import ParseResult as ObjcResult

# Swift keywords / attributes that get mis-parsed as function calls — filter them out
# to avoid creating invalid stub nodes.
_CALLEE_SKIP = {
    "escaping", "autoclosure", "noescape", "convention",
    "throws", "async", "await", "rethrows",
    "available", "discardableResult", "selector",
    "objc", "nonobjc",
    "completion", "callback", "handler", "block",
    "super", "self", "init", "deinit",
    "fatalError", "preconditionFailure", "assertionFailure",
}


# ── Output data structures ────────────────────────────────────────────────────

@dataclass
class NodeRecord:
    """Unified node record"""
    id: str              # globally unique: file_path + qualified_name
    label: str           # Function | Class | File
    props: dict


@dataclass
class EdgeRecord:
    """Unified edge record"""
    src_id: str
    dst_id: str
    rel: str             # CALLS | CONTAINS | INHERITS | IMPLEMENTS | IMPORTS
    props: dict          # confidence, callSite, etc.


@dataclass
class ExtractionResult:
    nodes: list[NodeRecord]
    edges: list[EdgeRecord]
    file_path: str
    errors: list[str]


# ── Main extraction logic ─────────────────────────────────────────────────────

def extract(parse_result, domain_mapping: dict, branch: str = "master",
            workspace: str = "") -> ExtractionResult:
    """
    Extract (node, edge) lists from a ParseResult.
    domain_mapping: directory-prefix → domain dict
    workspace: absolute path to the iOS workspace root (used for domain fallback)
    """
    fp = parse_result.file_path
    nodes: list[NodeRecord] = []
    edges: list[EdgeRecord] = []
    errors: list[str] = getattr(parse_result, 'errors', [])

    # 1. File node
    file_domain = _infer_domain(fp, domain_mapping, workspace)
    file_id = _file_id(fp)
    nodes.append(NodeRecord(
        id=file_id,
        label="File",
        props={
            "path": fp,
            "module": _infer_module(fp),
            "domain": file_domain,
            "branch": branch,
        }
    ))

    # 2. Class / Struct / Enum / Protocol nodes
    for cls in parse_result.classes:
        cls_id = _class_id(cls.name, fp)
        nodes.append(NodeRecord(
            id=cls_id,
            label="Class",
            props={
                "name": cls.name,
                "kind": cls.kind,
                "filePath": fp,
                "lineStart": cls.line_start,
                "lineEnd": cls.line_end,
                "isPublic": cls.is_public,
                "domain": file_domain,
                "branch": branch,
            }
        ))
        # File → CONTAINS → Class
        edges.append(EdgeRecord(
            src_id=file_id, dst_id=cls_id,
            rel="CONTAINS", props={"confidence": 100}
        ))
        # INHERITS / IMPLEMENTS
        for parent in cls.inherits:
            edges.append(EdgeRecord(
                src_id=cls_id, dst_id=_class_id_unresolved(parent),
                rel="INHERITS", props={"confidence": 90}
            ))
        for proto in cls.implements:
            edges.append(EdgeRecord(
                src_id=cls_id, dst_id=_class_id_unresolved(proto),
                rel="IMPLEMENTS", props={"confidence": 85}
            ))

    # 3. Function nodes
    func_id_map: dict[str, str] = {}  # qualified_name → id
    for func in parse_result.functions:
        func_id = _func_id(func.qualified_name, fp)
        func_id_map[func.qualified_name] = func_id

        nodes.append(NodeRecord(
            id=func_id,
            label="Function",
            props={
                "name": func.name,
                "qualifiedName": func.qualified_name,
                "filePath": fp,
                "lineStart": func.line_start,
                "lineEnd": func.line_end,
                "signature": func.signature,
                "isPublic": func.is_public,
                "isStatic": func.is_static,
                "domain": file_domain,
                "cigTerms": func.cig_terms,
                "branch": branch,
            }
        ))

        # Class → CONTAINS → Function
        if func.parent_class:
            cls_id = _class_id(func.parent_class, fp)
            edges.append(EdgeRecord(
                src_id=cls_id, dst_id=func_id,
                rel="CONTAINS", props={"confidence": 100}
            ))
        else:
            edges.append(EdgeRecord(
                src_id=file_id, dst_id=func_id,
                rel="CONTAINS", props={"confidence": 100}
            ))

    # 4. CALLS edges
    for call in parse_result.calls:
        caller_id = func_id_map.get(call.caller_qualified)
        if not caller_id:
            continue
        # Skip Swift keywords / attributes mis-parsed as function calls
        if call.callee_name in _CALLEE_SKIP:
            continue
        # callee uses unresolved id (import_resolver fills this in Phase 3)
        callee_id = _func_id_unresolved(call.callee_name)
        edges.append(EdgeRecord(
            src_id=caller_id,
            dst_id=callee_id,
            rel="CALLS",
            props={
                "confidence": call.confidence,
                "callSite": call.line_no,
                "calleeRaw": call.callee_name,
                "resolved": False,   # Phase 3 sets this to True
            }
        ))

    # 5. IMPORTS edges
    for imp in parse_result.imports:
        edges.append(EdgeRecord(
            src_id=file_id,
            dst_id=f"module:{imp.module}",
            rel="IMPORTS",
            props={"confidence": 100, "lineNo": imp.line_no}
        ))

    return ExtractionResult(nodes=nodes, edges=edges, file_path=fp, errors=errors)


# ── Helper functions ──────────────────────────────────────────────────────────

def _file_id(path: str) -> str:
    return f"file:{path}"


def _class_id(name: str, file_path: str) -> str:
    return f"class:{file_path}::{name}"


def _class_id_unresolved(name: str) -> str:
    """Unresolved class name, uses placeholder id"""
    return f"class:unresolved::{name}"


def _func_id(qualified: str, file_path: str) -> str:
    return f"func:{file_path}::{qualified}"


def _func_id_unresolved(name: str) -> str:
    return f"func:unresolved::{name}"


def _infer_domain(file_path: str, domain_mapping: dict, workspace: str = "") -> str:
    """
    Infer domain using longest-prefix match against domain_mapping.yaml.
    Falls back to the top-level subdirectory name within the workspace,
    so the tool is immediately useful even without a configured mapping.
    """
    best_prefix = ""
    best_domain = None
    for prefix, domain in domain_mapping.items():
        if prefix in file_path and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_domain = domain

    if best_domain:
        return best_domain

    # Fallback: derive from the first path component under the workspace root
    if workspace:
        try:
            rel = Path(file_path).relative_to(workspace)
            parts = rel.parts
            if parts:
                return parts[0]
        except ValueError:
            pass

    # Last resort: top-level directory name of the file path
    parts = Path(file_path).parts
    if len(parts) >= 2:
        return parts[-2]  # parent directory name

    return "unknown"


def _infer_module(file_path: str) -> str:
    """Infer module name from file path."""
    path = Path(file_path)
    parts = path.parts
    # Modules/XXX/Sources/XXX → module name
    if "Modules" in parts:
        idx = parts.index("Modules")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    # Use the top-level directory under common iOS project roots as the module
    for marker in ("Sources", "Source", "Classes", "App"):
        if marker in parts:
            idx = parts.index(marker)
            if idx > 0:
                return parts[idx - 1]
    # Default: second-to-last path component (parent directory of the file)
    if len(parts) >= 2:
        return parts[-2]
    return "app"


def load_domain_mapping(yaml_path: str) -> dict:
    """Load domain_mapping.yaml"""
    try:
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)
        return data.get("mappings", {}) if data else {}
    except Exception:
        return {}
