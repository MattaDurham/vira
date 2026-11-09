import Foundation
import CoreGraphics
import Vision

enum CameraQuality: String, CaseIterable, Identifiable {
    case best = "Best available (up to 4K)"
    case fullHD = "Full HD · 1080p"
    case compatible = "Compatibility · 720p"
    var id: String { rawValue }
}

struct ScanPoint: Codable, Equatable, Sendable {
    var x: Double
    var y: Double
    var cgPoint: CGPoint { CGPoint(x: x, y: y) }
}

struct PageCorners: Codable, Equatable, Sendable {
    // Clockwise, with a top-left origin, matching the editor's coordinate system.
    var points: [ScanPoint]
    static let inset = PageCorners(points: [.init(x: 0.06, y: 0.06), .init(x: 0.94, y: 0.06), .init(x: 0.94, y: 0.94), .init(x: 0.06, y: 0.94)])
    static let full = PageCorners(points: [.init(x: 0, y: 0), .init(x: 1, y: 0), .init(x: 1, y: 1), .init(x: 0, y: 1)])
    init(points: [ScanPoint]) { self.points = points }
    init(_ observation: VNRectangleObservation) {
        points = [observation.topLeft, observation.topRight, observation.bottomRight, observation.bottomLeft].map { ScanPoint(x: $0.x, y: 1 - $0.y) }
    }
    var isValid: Bool {
        guard points.count == 4 else { return false }
        return points.indices.allSatisfy { i in
            let a = points[i], b = points[(i + 1) % 4], c = points[(i + 2) % 4]
            return a.x.isFinite && a.y.isFinite && (0...1).contains(a.x) && (0...1).contains(a.y) &&
                (b.x - a.x) * (c.y - b.y) - (b.y - a.y) * (c.x - b.x) > 0.0001
        }
    }
}

enum PageFinish: String, Codable, CaseIterable, Identifiable, Sendable {
    case color, grayscale, document
    var id: String { rawValue }
    var title: String { switch self { case .color: "Original color"; case .grayscale: "Grayscale"; case .document: "Black & white" } }
    var symbol: String { switch self { case .color: "circle.lefthalf.filled"; case .grayscale: "circle.dotted.circle"; case .document: "doc.text" } }
}
enum PaperSize: String, CaseIterable, Identifiable {
    case letter = "US Letter", a4 = "A4", image = "Fit to image"
    var id: String { rawValue }
    func dimensions(image: CGSize) -> CGSize {
        var size: CGSize
        switch self {
        case .letter: size = CGSize(width: 612, height: 792)
        case .a4: size = CGSize(width: 595.28, height: 841.89)
        case .image: return CGSize(width: image.width * 72 / 150, height: image.height * 72 / 150)
        }
        if image.width > image.height { size = CGSize(width: size.height, height: size.width) }
        return size
    }
}
struct RecognizedLine: Codable, Sendable {
    var text: String
    var x: Double
    var y: Double
    var width: Double
    var height: Double
    var rect: CGRect { CGRect(x: x, y: y, width: width, height: height) }
}
struct ScanPage: Codable, Identifiable, Sendable {
    var id: UUID
    var originalFile: String
    var imageFile: String
    var corners: PageCorners
    var finish: PageFinish
    var rotation: Int
    var width: Int
    var height: Int
    var lines: [RecognizedLine]
    var text: String { lines.map(\.text).joined(separator: "\n") }
}
struct ScanSession: Codable, Sendable {
    var version = 1
    var title = "Untitled document"
    var pages: [ScanPage] = []
}
struct CaptureDraft {
    var original: Data
    var corners: PageCorners
    var finish: PageFinish = .color
    var rotation = 0
    var editingID: UUID?
}
enum ScannerError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(text) = self { return text }; return nil }
}
