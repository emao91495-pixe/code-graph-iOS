// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "swift-graph-extractor",
    platforms: [.macOS(.v13)],
    dependencies: [
        .package(url: "https://github.com/swiftlang/swift-syntax.git", from: "601.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "swift-graph-extractor",
            dependencies: [
                .product(name: "SwiftSyntax", package: "swift-syntax"),
                .product(name: "SwiftParser", package: "swift-syntax"),
            ],
            path: "Sources/swift-graph-extractor"
        ),
    ]
)
