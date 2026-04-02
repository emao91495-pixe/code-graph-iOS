import SwiftParser
import SwiftSyntax
import Foundation

// MARK: - Entry point

guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: swift-graph-extractor <file.swift>\n", stderr)
    exit(1)
}

let filePath = CommandLine.arguments[1]

guard let source = try? String(contentsOfFile: filePath, encoding: .utf8) else {
    let output = ParseOutput(
        filePath: filePath, classes: [], functions: [], calls: [], imports: [],
        errors: ["Cannot read file: \(filePath)"]
    )
    printJSON(output)
    exit(0)
}

let sourceFile = Parser.parse(source: source)
let visitor = CallGraphVisitor(filePath: filePath, source: source)
visitor.walk(sourceFile)

let output = ParseOutput(
    filePath: filePath,
    classes:   visitor.classes,
    functions: visitor.functions,
    calls:     visitor.calls,
    imports:   visitor.imports,
    errors:    visitor.errors
)

printJSON(output)

// MARK: - JSON output

func printJSON(_ value: some Encodable) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.withoutEscapingSlashes]
    if let data = try? encoder.encode(value),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    }
}
