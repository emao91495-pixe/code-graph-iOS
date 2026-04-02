import SwiftSyntax
import Foundation

// MARK: - Main Visitor

final class CallGraphVisitor: SyntaxVisitor {

    let filePath: String
    let source: String
    let lines: [String]

    var classes:   [ClassInfo]    = []
    var functions: [FunctionInfo] = []
    var calls:     [CallInfo]     = []
    var imports:   [ImportInfo]   = []
    var errors:    [String]       = []

    // 当前所属 class/struct 名称栈（支持嵌套类型）
    private var classStack: [String] = []
    private var currentClass: String? { classStack.last }

    // 当前正在解析的函数栈（支持嵌套函数）
    private var functionStack: [String] = []
    private var currentFunction: String? { functionStack.last }

    // 闭包内的 self alias 栈：每进入一个 ClosureExprSyntax 推入一个 Set
    private var selfAliasStack: [Set<String>] = []
    private var currentSelfAliases: Set<String> {
        selfAliasStack.reduce(into: Set<String>()) { $0.formUnion($1) }
    }

    init(filePath: String, source: String) {
        self.filePath = filePath
        self.source = source
        self.lines = source.components(separatedBy: "\n")
        super.init(viewMode: .sourceAccurate)
    }

    // MARK: - Helpers

    private func lineNumber(of position: AbsolutePosition) -> Int {
        let offset = position.utf8Offset
        var line = 1
        var current = 0
        for ch in source.utf8 {
            if current >= offset { break }
            if ch == UInt8(ascii: "\n") { line += 1 }
            current += 1
        }
        return line
    }

    private func endLine(of node: some SyntaxProtocol) -> Int {
        return lineNumber(of: node.endPosition)
    }

    private func isPublicModifier(_ modifiers: DeclModifierListSyntax) -> Bool {
        modifiers.contains { mod in
            mod.name.tokenKind == .keyword(.public) ||
            mod.name.tokenKind == .keyword(.open)
        }
    }

    private func isStaticModifier(_ modifiers: DeclModifierListSyntax) -> Bool {
        modifiers.contains { mod in
            mod.name.tokenKind == .keyword(.static) ||
            mod.name.tokenKind == .keyword(.class)
        }
    }

    /// 从节点前的注释中提取 CIGTerms
    private func extractCIGTerms(leadingTrivia: Trivia) -> [String] {
        var terms: [String] = []
        for piece in leadingTrivia {
            let text: String
            switch piece {
            case .lineComment(let s), .blockComment(let s),
                 .docLineComment(let s), .docBlockComment(let s):
                text = s
            default:
                continue
            }
            if let range = text.range(of: "CIGTerms:") {
                let after = String(text[range.upperBound...])
                    .trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "*/"))
                    .trimmingCharacters(in: .whitespaces)
                terms.append(contentsOf:
                    after.components(separatedBy: ",")
                         .map { $0.trimmingCharacters(in: .whitespaces) }
                         .filter { !$0.isEmpty }
                )
            }
        }
        return terms
    }

    /// 从 InheritanceClauseSyntax 提取协议/父类名
    private func extractInheritance(_ clause: InheritanceClauseSyntax?) -> [String] {
        guard let clause else { return [] }
        return clause.inheritedTypes.compactMap { inherited -> String? in
            let text = inherited.type.trimmedDescription
            // 去掉泛型参数：Array<T> → Array
            return text.components(separatedBy: "<").first?.trimmingCharacters(in: .whitespaces)
        }
    }

    // MARK: - Import

    override func visit(_ node: ImportDeclSyntax) -> SyntaxVisitorContinueKind {
        let path = node.path.map { $0.name.text }.joined(separator: ".")
        if !path.isEmpty {
            imports.append(ImportInfo(
                module: path,
                filePath: filePath,
                lineNo: lineNumber(of: node.position)
            ))
        }
        return .skipChildren
    }

    // MARK: - Type declarations

    override func visit(_ node: ClassDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = node.name.text
        let allInherited = extractInheritance(node.inheritanceClause)
        let cls = ClassInfo(
            name: name,
            kind: "class",
            filePath: filePath,
            lineStart: lineNumber(of: node.position),
            lineEnd: endLine(of: node),
            isPublic: isPublicModifier(node.modifiers),
            inherits: Array(allInherited.prefix(1)),     // 第一个是父类
            implements: Array(allInherited.dropFirst())   // 其余是协议
        )
        classes.append(cls)
        classStack.append(name)
        return .visitChildren
    }

    override func visitPost(_ node: ClassDeclSyntax) {
        classStack.removeLast()
    }

    override func visit(_ node: StructDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = node.name.text
        let inherited = extractInheritance(node.inheritanceClause)
        classes.append(ClassInfo(
            name: name, kind: "struct", filePath: filePath,
            lineStart: lineNumber(of: node.position), lineEnd: endLine(of: node),
            isPublic: isPublicModifier(node.modifiers),
            inherits: [], implements: inherited
        ))
        classStack.append(name)
        return .visitChildren
    }

    override func visitPost(_ node: StructDeclSyntax) { classStack.removeLast() }

    override func visit(_ node: EnumDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = node.name.text
        let inherited = extractInheritance(node.inheritanceClause)
        classes.append(ClassInfo(
            name: name, kind: "enum", filePath: filePath,
            lineStart: lineNumber(of: node.position), lineEnd: endLine(of: node),
            isPublic: isPublicModifier(node.modifiers),
            inherits: [], implements: inherited
        ))
        classStack.append(name)
        return .visitChildren
    }

    override func visitPost(_ node: EnumDeclSyntax) { classStack.removeLast() }

    override func visit(_ node: ProtocolDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = node.name.text
        let inherited = extractInheritance(node.inheritanceClause)
        classes.append(ClassInfo(
            name: name, kind: "protocol", filePath: filePath,
            lineStart: lineNumber(of: node.position), lineEnd: endLine(of: node),
            isPublic: isPublicModifier(node.modifiers),
            inherits: [], implements: inherited
        ))
        classStack.append(name)
        return .visitChildren
    }

    override func visitPost(_ node: ProtocolDeclSyntax) { classStack.removeLast() }

    override func visit(_ node: ExtensionDeclSyntax) -> SyntaxVisitorContinueKind {
        // extension TargetType: Protocol1, Protocol2
        let name = node.extendedType.trimmedDescription
            .components(separatedBy: "<").first?
            .trimmingCharacters(in: .whitespaces) ?? ""
        let inherited = extractInheritance(node.inheritanceClause)
        if !name.isEmpty {
            classes.append(ClassInfo(
                name: name, kind: "extension", filePath: filePath,
                lineStart: lineNumber(of: node.position), lineEnd: endLine(of: node),
                isPublic: isPublicModifier(node.modifiers),
                inherits: [], implements: inherited
            ))
            classStack.append(name)
        }
        return .visitChildren
    }

    override func visitPost(_ node: ExtensionDeclSyntax) {
        if !classStack.isEmpty { classStack.removeLast() }
    }

    // MARK: - Function declarations

    override func visit(_ node: FunctionDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = node.name.text
        let qualified = currentClass.map { "\($0).\(name)" } ?? name
        let sig = node.trimmedDescription.components(separatedBy: "{").first?
                      .trimmingCharacters(in: .whitespaces) ?? ""
        let cigTerms = extractCIGTerms(leadingTrivia: node.leadingTrivia)

        functions.append(FunctionInfo(
            name: name,
            qualifiedName: qualified,
            filePath: filePath,
            lineStart: lineNumber(of: node.position),
            lineEnd: endLine(of: node),
            signature: String(sig.prefix(200)),
            isPublic: isPublicModifier(node.modifiers),
            isStatic: isStaticModifier(node.modifiers),
            parentClass: currentClass,
            cigTerms: cigTerms
        ))

        functionStack.append(qualified)
        return .visitChildren
    }

    override func visitPost(_ node: FunctionDeclSyntax) {
        if !functionStack.isEmpty { functionStack.removeLast() }
    }

    override func visit(_ node: InitializerDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = "init"
        let qualified = currentClass.map { "\($0).init" } ?? "init"
        functions.append(FunctionInfo(
            name: name, qualifiedName: qualified, filePath: filePath,
            lineStart: lineNumber(of: node.position), lineEnd: endLine(of: node),
            signature: String(node.trimmedDescription.components(separatedBy: "{").first?
                               .trimmingCharacters(in: .whitespaces).prefix(200) ?? ""),
            isPublic: isPublicModifier(node.modifiers),
            isStatic: false,
            parentClass: currentClass,
            cigTerms: extractCIGTerms(leadingTrivia: node.leadingTrivia)
        ))
        functionStack.append(qualified)
        return .visitChildren
    }

    override func visitPost(_ node: InitializerDeclSyntax) {
        if !functionStack.isEmpty { functionStack.removeLast() }
    }

    // MARK: - Closure: capture alias 解析

    override func visit(_ node: ClosureExprSyntax) -> SyntaxVisitorContinueKind {
        var aliases = Set<String>()

        // 1. 检测 [weak self] / [unowned self] capture list
        if let captureClause = node.signature?.capture {
            for item in captureClause.items {
                // item.expression 是 self / weakSelf 等
                let exprText = (item.initializer?.value ?? item.expression).trimmedDescription.trimmingCharacters(in: CharacterSet.whitespaces)
                // "weak self" → name is "self"
                if exprText == "self" {
                    aliases.insert("self")
                }
            }
        }

        // 2. 扫描 closure body 前几条语句，找 guard let X = self / let X = self
        for stmt in node.statements {
            // guard let X = self else { return }
            if let guard_ = stmt.item.as(GuardStmtSyntax.self) {
                for cond in guard_.conditions {
                    if case .optionalBinding(let binding) = cond.condition {
                        let initValue = binding.initializer?.value
                        if let ref = initValue?.as(DeclReferenceExprSyntax.self),
                           ref.baseName.text == "self",
                           let patternId = binding.pattern.as(IdentifierPatternSyntax.self) {
                            aliases.insert(patternId.identifier.text)
                        }
                    }
                }
            }
            // let X = self (ValueBindingPatternSyntax inside VariableDeclSyntax)
            if let varDecl = stmt.item.as(VariableDeclSyntax.self) {
                for binding in varDecl.bindings {
                    if let initExpr = binding.initializer?.value,
                       let ref = initExpr.as(DeclReferenceExprSyntax.self),
                       ref.baseName.text == "self",
                       let patternId = binding.pattern.as(IdentifierPatternSyntax.self) {
                        aliases.insert(patternId.identifier.text)
                    }
                }
            }
        }

        selfAliasStack.append(aliases)
        return .visitChildren
    }

    override func visitPost(_ node: ClosureExprSyntax) {
        if !selfAliasStack.isEmpty { selfAliasStack.removeLast() }
    }

    // MARK: - Call expressions

    override func visit(_ node: FunctionCallExprSyntax) -> SyntaxVisitorContinueKind {
        guard let caller = currentFunction else { return .visitChildren }

        var calleeName: String = ""
        var receiver: String? = nil
        var confidence = 90

        let calledExpr = node.calledExpression

        // 情况 1: receiver.method(...)  →  MemberAccessExprSyntax
        if let member = calledExpr.as(MemberAccessExprSyntax.self) {
            calleeName = member.declName.baseName.text

            // 提取 receiver
            if let base = member.base {
                let baseText = base.trimmedDescription
                // self alias → 视为 self
                if currentSelfAliases.contains(baseText) || baseText == "self" || baseText == "super" {
                    receiver = "self"
                    confidence = 90
                } else {
                    receiver = baseText.components(separatedBy: ".").last ?? baseText
                    confidence = 80
                }
            }
        }
        // 情况 2: 直接调用 foo(...)
        else if let ref = calledExpr.as(DeclReferenceExprSyntax.self) {
            calleeName = ref.baseName.text
            confidence = 90
        }
        // 情况 3: 链式调用 a.b.c(...) → 取最后一段
        else {
            let text = calledExpr.trimmedDescription
            if text.contains(".") {
                let parts = text.components(separatedBy: ".")
                calleeName = parts.last ?? text
                receiver = parts.dropLast().last
                confidence = 75
            } else {
                calleeName = text
            }
        }

        // 过滤 Swift 关键字和空名
        let skipNames: Set<String> = [
            "if", "guard", "while", "for", "switch", "return", "throw",
            "init", "super", "self", "nil", "true", "false",
            "print", "fatalError", "precondition", "assert",
            "escaping", "autoclosure", "discardableResult",
        ]
        guard !calleeName.isEmpty,
              !skipNames.contains(calleeName),
              !calleeName.hasPrefix("\""),
              calleeName.first?.isLetter == true || calleeName.first == "_"
        else {
            return .visitChildren
        }

        calls.append(CallInfo(
            callerQualified: caller,
            calleeName: calleeName,
            calleeReceiver: receiver,
            lineNo: lineNumber(of: node.position),
            confidence: confidence
        ))

        return .visitChildren
    }
}
