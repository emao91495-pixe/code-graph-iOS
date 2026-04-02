# Stub 解析：用 Xcode Index Store 还原调用关系

## 什么是 Stub？

我们的 tree-sitter 解析器在提取调用关系时，如果无法在当前文件里确定被调用函数的具体定义位置，会创建一个"stub"节点：

```
func:unresolved::methodName
func:unresolved::ClassName.methodName
```

在初次全量 build 后（5331 个文件），约 275,646 条 CALLS 边（67.4%）指向 stub 节点。

**为什么会有 stub？**
- 被调用的函数定义在另一个文件（跨文件调用）
- ObjC 的 message send (`[obj method:]`) 无法静态确定类型
- 动态派发 (`performSelector:`)
- Swift protocol/delegate 调用

---

## 解决方案：Xcode Index Store

Xcode 在每次 build 时，编译器会把跨文件符号引用写入 **Index Store**（位于 DerivedData）。这是编译器级别的精确引用，完全解决了 tree-sitter 的跨文件盲区。

### Index Store 路径

```
~/Library/Developer/Xcode/DerivedData/<Project>-<hash>/Index.noindex/DataStore/
  v5/
    units/    # 38,105 个编译单元（含多 target 重复）
    records/  # 24,260 个符号记录文件（按 2 字符前缀分桶）
```

### 解析结果（2026-03-16）

| 指标 | 数值 |
|------|------|
| 处理 units | 38,105 |
| 唯一 records | 23,290 |
| 提取 definitions | ~750,000 |
| 提取 call sites | ~715,000 |
| 新解析 CALLS 边 | 67,265 |
| 解析率 | 24.4% |
| 总 CALLS 覆盖率（解析后） | **46.6%** |

### 未解析的原因

| 原因 | 数量 | 说明 |
|------|------|------|
| SDK/外部 (UIKit, Foundation...) | ~124,000 | 预期，无法解析 |
| 项目内但行号不匹配 | ~62,000 | 可能是 Pods 或微小偏差 |
| IndexStore 无记录（未 build 的文件） | ~21,000 | 部分文件未被 Xcode 编译 |

---

## 使用方法

### 前提条件

1. Xcode 已成功 build 过项目（至少 Dev scheme）
2. DerivedData 未被清理（`Product > Clean Build Folder` 会清掉）

### 运行解析

```bash
cd /path/to/code-intelligence-graph
source venv/bin/activate

# Preview only — no Neo4j writes
python3 scripts/resolve_stubs_indexstore.py --dry-run

# Apply
python3 scripts/resolve_stubs_indexstore.py

# Specify DataStore path explicitly (optional — auto-detected by default)
python3 scripts/resolve_stubs_indexstore.py \
  --store ~/Library/Developer/Xcode/DerivedData/YourProject-xxx/Index.noindex/DataStore
```

### 重新 build 后重跑

每次 Xcode build 后，IndexStore 都会更新。如果有新文件或代码变更，需要重跑：

```bash
python3 resolve_stubs_indexstore.py
```

脚本会通过 `MERGE` 避免重复写入，只更新新解析的边。

---

## 技术实现细节

### ctypes 绑定 libIndexStore.dylib

Xcode 自带 `libIndexStore.dylib`：
```
/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/libIndexStore.dylib
```

**ARM64 macOS ctypes 的关键陷阱**：

1. **Python→C 传 StringRef 必须拆成两个参数**：
   - `indexstore_symbol_get_usr` 等返回 `indexstore_string_ref_t {char*, size_t}` — 这是 16 字节 struct
   - 当 Python 调 C 函数并传 struct by value 时，ctypes 在 ARM64 上有 ABI 问题
   - **Fix**: 所有接收 StringRef 的 C 函数，在 `argtypes` 里拆成 `[c_char_p, c_size_t]`
   - 相关函数：`unit_reader_create`, `record_reader_create`

2. **C→Python 回调接收 StringRef 用 Structure 是正确的**：
   - `CFUNCTYPE(c_bool, c_void_p, StringRef)` 在 ARM64 上可以正确接收 StringRef

3. **必须用 `*_apply_f` 版本，不能用 `*_apply`**：
   - `*_apply` 接受 ObjC block（`^`），ctypes 无法传 block
   - `*_apply_f` 接受普通 C 函数指针 + void* ctx，ctypes 可以传

4. **`record_reader_create` 在记录文件不存在时会 crash**：
   - 不是返回 NULL，而是直接 SIGSEGV
   - **Fix**: 先检查文件：`records/<last-2-chars>/<record-name>` 是否存在

5. **必须显式设置所有 argtypes**：
   - 不设 argtypes 时，ctypes 默认用 32-bit int 传参，在 64-bit 指针上会截断

### 依赖类型说明

从 `.o` 编译单元的依赖里：
- `kind=1`（DEP_RECORD）: 外部模块记录（`.swiftinterface`, `.pch`, `.pcm`）
- `kind=2`（DEP_UNIT）: Swift/ObjC 源文件 — 虽然叫 "unit"，但实际以 record 形式存储，可用 `record_reader_create` 读取

**项目源文件的符号在 kind=2 依赖里，kind=1 是 SDK/系统模块。**

### 匹配逻辑

```
stub CALLS 边
  ↓ callSite（行号）+ srcFile（调用方文件）
IndexStore call_sites[(srcFile, callSite)] → [calleeUSR, ...]
  ↓ calleeRaw 名称过滤（避免同行多调用误匹配）
usr_to_def[calleeUSR] → {filePath, line}
  ↓
Neo4j Function 节点 by (filePath, lineStart)
  ↓
重定向 CALLS 边 (stub → real Function, confidence=99)
```

### 名称过滤的重要性

同一行可能有多个函数调用，例如：
```swift
guard let x = UIImage(named: "icon") else { fatalError("missing") }
```
这行有 `UIImage.init(named:)` 和 `fatalError` 两个调用。

如果不过滤，`fatalError` (stub) 可能被错误匹配到 `UIImage.init` 的 Function 节点。
用 `calleeRaw`（从 CALLS 边 props 读取）做名称过滤可以避免这个问题。

---

## 还有哪些 stub 没被解析？

剩余 ~208,000 stub 边：

1. **SDK/系统框架调用**（最多）：`UIView.addSubview`, `String.init`, `print` 等。这类调用本来就不需要追踪，不影响业务逻辑分析。

2. **Pods 内部函数**：我们不 parse Pods，但项目代码调用了 Pods 里的函数。如果需要，可以把 Pods 加入解析范围。

3. **ObjC 动态调用**：`performSelector:`, KVO 等，编译器也无法静态解析。

4. **行号微小偏差**：IndexStore 用 1-based 行号，tree-sitter 用 0-based 或 1-based，需要验证是否一致。

---

## 效果对比

| 指标 | 解析前 | 解析后 |
|------|--------|--------|
| CALLS 边总数 | 408,756 | 389,909 |
| 指向真实 Function | 133,110 (32.6%) | 181,547 (46.6%) |
| 指向 stub | 275,646 (67.4%) | 208,362 (53.4%) |
| `onRouteDeviation` 追踪 | 0 层 callers（stub 阻断） | 部分恢复 |
