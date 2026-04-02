// JSON 输出结构，与 Python ParseResult 数据类对应

struct ParseOutput: Codable {
    var filePath: String
    var classes: [ClassInfo]
    var functions: [FunctionInfo]
    var calls: [CallInfo]
    var imports: [ImportInfo]
    var errors: [String]
}

struct ClassInfo: Codable {
    var name: String
    var kind: String          // class | struct | enum | protocol | extension
    var filePath: String
    var lineStart: Int
    var lineEnd: Int
    var isPublic: Bool
    var inherits: [String]    // 父类（class 最多1个）
    var implements: [String]  // 协议列表
}

struct FunctionInfo: Codable {
    var name: String
    var qualifiedName: String  // ClassName.methodName
    var filePath: String
    var lineStart: Int
    var lineEnd: Int
    var signature: String
    var isPublic: Bool
    var isStatic: Bool
    var parentClass: String?
    var cigTerms: [String]
}

struct CallInfo: Codable {
    var callerQualified: String
    var calleeName: String
    var calleeReceiver: String?
    var lineNo: Int
    var confidence: Int
}

struct ImportInfo: Codable {
    var module: String
    var filePath: String
    var lineNo: Int
}
