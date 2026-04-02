"""
Swift AST 解析器
使用 SwiftSyntax CLI 解析 Swift 源文件（Apple 官方 parser），提取：
- class / struct / enum / protocol / extension 定义
- func / method 定义（含签名、行号、可见性）
- 函数调用表达式（含闭包 capture alias 解析）
- import 语句

回退链：SwiftSyntax CLI → regex（无 CLI 时）
"""

from __future__ import annotations
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# SwiftSyntax CLI 路径（与本文件相邻的 swift_syntax_extractor 目录）
_CLI_PATH = Path(__file__).parent / "swift_syntax_extractor" / ".build" / "release" / "swift-graph-extractor"
_CLI_AVAILABLE = _CLI_PATH.exists()

_TS_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB 上限


@dataclass
class SwiftClass:
    name: str
    kind: str          # class | struct | enum | protocol | extension
    file_path: str
    line_start: int
    line_end: int
    is_public: bool
    inherits: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)


@dataclass
class SwiftFunction:
    name: str
    qualified_name: str     # ClassName.methodName
    file_path: str
    line_start: int
    line_end: int
    signature: str
    is_public: bool
    is_static: bool
    parent_class: Optional[str] = None
    cig_terms: list[str] = field(default_factory=list)


@dataclass
class SwiftCall:
    caller_qualified: str
    callee_name: str
    callee_receiver: Optional[str]
    line_no: int
    confidence: int         # 100=直接调用, 90=self调用, 80=其他receiver, 75=链式


@dataclass
class SwiftImport:
    module: str
    file_path: str
    line_no: int


@dataclass
class ParseResult:
    file_path: str
    classes: list[SwiftClass]
    functions: list[SwiftFunction]
    calls: list[SwiftCall]
    imports: list[SwiftImport]
    errors: list[str]


def parse_file(file_path: str) -> ParseResult:
    """解析单个 Swift 文件，返回结构化结果"""
    if _CLI_AVAILABLE:
        return _parse_with_swiftsyntax(file_path)
    else:
        path = Path(file_path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ParseResult(file_path, [], [], [], [], [str(e)])
        return _parse_with_regex(file_path, source)


def _parse_with_swiftsyntax(file_path: str) -> ParseResult:
    """调用 SwiftSyntax CLI，解析 JSON 输出，映射为 Python dataclasses"""
    try:
        result = subprocess.run(
            [str(_CLI_PATH), file_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return ParseResult(file_path, [], [], [], [], [f"CLI error: {stderr}"])

        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return ParseResult(file_path, [], [], [], [], ["CLI timeout"])
    except Exception as e:
        return ParseResult(file_path, [], [], [], [], [str(e)])

    classes = [
        SwiftClass(
            name=c["name"],
            kind=c["kind"],
            file_path=c["filePath"],
            line_start=c["lineStart"],
            line_end=c["lineEnd"],
            is_public=c["isPublic"],
            inherits=c.get("inherits", []),
            implements=c.get("implements", []),
        )
        for c in data.get("classes", [])
    ]

    functions = [
        SwiftFunction(
            name=f["name"],
            qualified_name=f["qualifiedName"],
            file_path=f["filePath"],
            line_start=f["lineStart"],
            line_end=f["lineEnd"],
            signature=f.get("signature", ""),
            is_public=f["isPublic"],
            is_static=f["isStatic"],
            parent_class=f.get("parentClass"),
            cig_terms=f.get("cigTerms", []),
        )
        for f in data.get("functions", [])
    ]

    calls = [
        SwiftCall(
            caller_qualified=c["callerQualified"],
            callee_name=c["calleeName"],
            callee_receiver=c.get("calleeReceiver"),
            line_no=c["lineNo"],
            confidence=c.get("confidence", 90),
        )
        for c in data.get("calls", [])
    ]

    imports = [
        SwiftImport(
            module=i["module"],
            file_path=i["filePath"],
            line_no=i["lineNo"],
        )
        for i in data.get("imports", [])
    ]

    errors = data.get("errors", [])
    return ParseResult(file_path, classes, functions, calls, imports, errors)


# ── Regex fallback（无 SwiftSyntax CLI 时使用）─────────────────────────────

_ATTR_PREFIX = r'(?:@\w+(?:\s*\([^)]*\))?\s+)*'

_CLASS_RE = re.compile(
    r'^\s*' + _ATTR_PREFIX +
    r'(public\s+|open\s+|internal\s+|private\s+|fileprivate\s+)?'
    r'(?:' + _ATTR_PREFIX + r')'
    r'(final\s+)?(class|struct|enum|protocol)\s+(\w+)'
    r'(?:\s*:\s*([\w\s,<>]+?))?(?:\s*\{|$)',
    re.MULTILINE
)
_FUNC_RE = re.compile(
    r'^\s*' + _ATTR_PREFIX +
    r'(public\s+|open\s+|internal\s+|private\s+|fileprivate\s+)?'
    r'(?:' + _ATTR_PREFIX + r')'
    r'(static\s+|class\s+|override\s+|final\s+|mutating\s+|nonmutating\s+)*'
    r'(func)\s+(\w+)\s*[\(<]',
    re.MULTILINE
)
_IMPORT_RE = re.compile(r'^\s*import\s+(\w+)', re.MULTILINE)
_CALL_METHOD_RE = re.compile(r'(\w+)\.(\w+)\s*\(', re.MULTILINE)
_CALL_SIMPLE_RE = re.compile(r'(?<!\.)(?<!\w)(\w+)\s*\(', re.MULTILINE)

_SWIFT_KEYWORDS = frozenset([
    'if', 'else', 'for', 'while', 'guard', 'switch', 'case', 'return',
    'let', 'var', 'func', 'class', 'struct', 'enum', 'protocol', 'import',
    'in', 'do', 'try', 'catch', 'throw', 'throws', 'async', 'await',
    'override', 'public', 'private', 'internal', 'static', 'final',
    'init', 'deinit', 'super', 'self', 'nil', 'true', 'false', 'print',
    'where', 'extension', 'open', 'fileprivate', 'lazy', 'weak', 'unowned',
    'typealias', 'associatedtype', 'subscript', 'get', 'set', 'willSet', 'didSet',
])


def _extract_calls_regex(source_slice: str, caller_qualified: str,
                          line_offset: int, calls: list) -> None:
    seen = set()
    for m in _CALL_METHOD_RE.finditer(source_slice):
        receiver = m.group(1)
        callee = m.group(2)
        if callee in _SWIFT_KEYWORDS or receiver in _SWIFT_KEYWORDS:
            continue
        key = (callee, receiver)
        if key in seen:
            continue
        seen.add(key)
        line_no = line_offset + source_slice[:m.start()].count("\n")
        calls.append(SwiftCall(
            caller_qualified=caller_qualified,
            callee_name=callee,
            callee_receiver=receiver,
            line_no=line_no,
            confidence=70,
        ))

    for m in _CALL_SIMPLE_RE.finditer(source_slice):
        callee = m.group(1)
        if callee in _SWIFT_KEYWORDS or (callee, None) in seen:
            continue
        if any(c == callee for (c, _) in seen):
            continue
        seen.add((callee, None))
        line_no = line_offset + source_slice[:m.start()].count("\n")
        calls.append(SwiftCall(
            caller_qualified=caller_qualified,
            callee_name=callee,
            callee_receiver=None,
            line_no=line_no,
            confidence=70,
        ))


def _parse_with_regex(file_path: str, source: str) -> ParseResult:
    """正则回退解析（低精度，无外部依赖）"""
    lines = source.splitlines()
    total_lines = len(lines)
    classes = []
    functions = []
    calls = []
    imports_list = []

    for m in _IMPORT_RE.finditer(source):
        line_no = source[:m.start()].count("\n") + 1
        imports_list.append(SwiftImport(m.group(1), file_path, line_no))

    for m in _CLASS_RE.finditer(source):
        line_no = source[:m.start()].count("\n") + 1
        name = m.group(4)
        kind = m.group(3)
        inherits_raw = m.group(5) or ""
        inherits = [s.strip() for s in inherits_raw.split(",") if s.strip()]
        classes.append(SwiftClass(
            name=name, kind=kind, file_path=file_path,
            line_start=line_no, line_end=line_no + 50,
            is_public=bool(m.group(1) and "public" in m.group(1)),
            inherits=inherits[:1] if kind == "class" else [],
            implements=inherits[1:] if kind == "class" else inherits,
        ))

    func_matches = list(_FUNC_RE.finditer(source))
    func_starts = [(source[:m.start()].count("\n") + 1, m) for m in func_matches]

    for idx, (line_no, m) in enumerate(func_starts):
        name = m.group(4)
        parent = None
        for cls in classes:
            if cls.line_start <= line_no:
                parent = cls.name
        qualified = f"{parent}.{name}" if parent else name

        line_end = func_starts[idx + 1][0] - 1 if idx + 1 < len(func_starts) else total_lines

        cig = []
        for i in range(max(0, line_no - 6), line_no - 1):
            if i < len(lines) and "CIGTerms:" in lines[i]:
                raw = lines[i].split("CIGTerms:")[-1].strip().rstrip("*/").strip()
                cig.extend([t.strip() for t in raw.split(",") if t.strip()])

        functions.append(SwiftFunction(
            name=name, qualified_name=qualified,
            file_path=file_path, line_start=line_no, line_end=line_end,
            signature=m.group(0).strip()[:200],
            is_public=bool(m.group(1) and "public" in m.group(1)),
            is_static=bool(m.group(2) and "static" in m.group(2)),
            parent_class=parent, cig_terms=cig,
        ))

        body_start = sum(len(l) + 1 for l in lines[:line_no - 1])
        body_end = sum(len(l) + 1 for l in lines[:line_end])
        _extract_calls_regex(source[body_start:body_end], qualified, line_no, calls)

    return ParseResult(file_path, classes, functions, calls, imports_list, [])
