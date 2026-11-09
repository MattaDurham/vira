// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Folio",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "Folio", targets: ["Folio"])],
    targets: [
        .executableTarget(name: "Folio"),
        .testTarget(name: "FolioTests", dependencies: ["Folio"])
    ]
)
