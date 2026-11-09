import Testing
import AppKit
import PDFKit
@testable import Folio

struct ScannerTests {
    @Test func testCropRejectsCrossedAndOutOfBoundsCorners() {
        #expect(PageCorners.full.isValid)
        #expect(PageCorners.inset.isValid)
        var crossed = PageCorners.full
        crossed.points.swapAt(1, 2)
        #expect(!crossed.isValid)
        var outside = PageCorners.full; outside.points[0].x = -0.1
        #expect(!outside.isValid)
        #expect(!PageCorners(points: []).isValid)
    }

    @Test func testPerspectiveCropRotationAndGrayscale() throws {
        let data = try testImage()
        let draft = CaptureDraft(original: data, corners: .full)
        let image = try ImageProcessor.render(draft)
        #expect(abs(image.width - 400) <= 2)
        #expect(abs(image.height - 600) <= 2)
        var rotated = draft; rotated.rotation = 90
        let sideways = try ImageProcessor.render(rotated)
        #expect(abs(sideways.width - image.height) <= 2)
        #expect(abs(sideways.height - image.width) <= 2)
        var cropped = draft
        cropped.corners = PageCorners(points: [.init(x: 0.2, y: 0.1), .init(x: 0.9, y: 0.15), .init(x: 0.85, y: 0.9), .init(x: 0.1, y: 0.85)])
        let output = try ImageProcessor.render(cropped)
        #expect(output.width < image.width)
        #expect(output.height < image.height)
        cropped.finish = .grayscale
        let gray = NSBitmapImageRep(cgImage: try ImageProcessor.render(cropped))
        let color = try #require(gray.colorAt(x: gray.pixelsWide / 2, y: gray.pixelsHigh / 2)?.usingColorSpace(.deviceRGB))
        #expect(abs(color.redComponent - color.greenComponent) < 0.02)
        #expect(abs(color.greenComponent - color.blueComponent) < 0.02)
    }

    @Test func testDetectsSamplePageAndRecognizesText() throws {
        let data = try ImageProcessor.sample()
        let image = try ImageProcessor.decode(data)
        let detected = try ImageProcessor.detect(image)
        let corners = try #require(detected)
        #expect(corners.isValid)
        #expect(corners.points[0].x > 0.1)
        #expect(corners.points[1].x < 0.9)
        let rendered = try ImageProcessor.render(CaptureDraft(original: data, corners: corners))
        let text = try ImageProcessor.recognize(rendered).map(\.text).joined(separator: " ").lowercased()
        #expect(text.contains("fresh page"), "OCR returned: \(text)")
        #expect(text.contains("find your light"))
    }

    @Test func testSearchableMultiPagePDF() throws {
        let data = try testImage()
        let line = RecognizedLine(text: "Folio scanner searchable text", x: 0.1, y: 0.7, width: 0.8, height: 0.05)
        let page = ScanPage(id: UUID(), originalFile: "original.jpg", imageFile: "page.jpg", corners: .full, finish: .color, rotation: 0, width: 400, height: 600, lines: [line])
        let output = try PDFExporter.data(pages: [(page, data), (page, data)], title: "Test document", paper: .letter, searchable: true)
        let pdf = try #require(PDFDocument(data: output))
        #expect(pdf.pageCount == 2)
        #expect(pdf.string?.contains("Folio scanner searchable text") == true)
        #expect(pdf.page(at: 0)?.bounds(for: .mediaBox).width == 612)
        let flat = try PDFExporter.data(pages: [(page, data)], title: "Image", paper: .a4, searchable: false)
        #expect(PDFDocument(data: flat)?.string?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
    }

    @Test func testSessionRoundTripAndMissingAssetDetection() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = try SessionStore(directory: directory)
        #expect(try store.load().pages.isEmpty)
        let image = try testImage(), originalFile = try store.writeImage(image), outputFile = try store.writeImage(image)
        let page = ScanPage(id: UUID(), originalFile: originalFile, imageFile: outputFile, corners: .full, finish: .color, rotation: 0, width: 400, height: 600, lines: [])
        let session = ScanSession(title: "My scan", pages: [page])
        try store.save(session)
        let restored = try SessionStore(directory: directory).load()
        #expect(restored.title == "My scan")
        #expect(restored.pages[0].id == page.id)
        #expect(try Data(contentsOf: store.url(restored.pages[0].originalFile)) == image)
        try FileManager.default.removeItem(at: store.url(outputFile))
        #expect(throws: (any Error).self) { try store.load() }
    }

    private func testImage() throws -> Data {
        let context = try #require(CGContext(data: nil, width: 400, height: 600, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: 400, height: 600))
        context.setFillColor(CGColor(red: 0.7, green: 0.2, blue: 0.1, alpha: 1)); context.fill(CGRect(x: 120, y: 200, width: 160, height: 200))
        return try ImageProcessor.jpeg(#require(context.makeImage()))
    }
}
