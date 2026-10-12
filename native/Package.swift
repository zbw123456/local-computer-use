// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LocalComputerUse",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "computer-use-helper", targets: ["ComputerUseHelper"])],
    targets: [.executableTarget(name: "ComputerUseHelper")]
)
