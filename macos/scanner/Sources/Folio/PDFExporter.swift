import Foundation
import CoreGraphics
import CoreText

enum PDFExporter {
    static func data(pages: [(ScanPage, Data)], title: String, paper: PaperSize, searchable: Bool) throws -> Data {
        guard !pages.isEmpty else { throw ScannerError.message("Add at least one page before exporting.") }
        let output = NSMutableData()
        guard let consumer = CGDataConsumer(data: output), let context = CGContext(consumer: consumer, mediaBox: nil, [kCGPDFContextTitle: title, kCGPDFContextCreator: "Folio"] as CFDictionary) else { throw ScannerError.message("Could not create the PDF.") }
        for (page, data) in pages {
            let image = try ImageProcessor.decode(data)
            let imageSize = CGSize(width: image.width, height: image.height)
            let size = paper.dimensions(image: imageSize)
            var box = CGRect(origin: .zero, size: size)
            let boxData = NSData(bytes: &box, length: MemoryLayout<CGRect>.size)
            context.beginPDFPage([kCGPDFContextMediaBox: boxData] as CFDictionary)
            context.setFillColor(CGColor(gray: 1, alpha: 1)); context.fill(box)
            let margin = paper == .image ? 0.0 : 18.0
            let scale = min((size.width - margin * 2) / imageSize.width, (size.height - margin * 2) / imageSize.height)
            let target = CGRect(x: (size.width - imageSize.width * scale) / 2, y: (size.height - imageSize.height * scale) / 2, width: imageSize.width * scale, height: imageSize.height * scale)
            context.draw(image, in: target)
            if searchable {
                for line in page.lines {
                    let rect = CGRect(x: target.minX + line.x * target.width, y: target.minY + line.y * target.height, width: line.width * target.width, height: line.height * target.height)
                    let font = CTFontCreateWithName("Helvetica" as CFString, max(1, rect.height), nil)
                    let attributed = NSAttributedString(string: line.text, attributes: [NSAttributedString.Key(kCTFontAttributeName as String): font])
                    let text = CTLineCreateWithAttributedString(attributed)
                    let width = CTLineGetTypographicBounds(text, nil, nil, nil)
                    context.saveGState()
                    context.translateBy(x: rect.minX, y: rect.minY + rect.height * 0.14)
                    context.scaleBy(x: width > 0 ? rect.width / width : 1, y: 1)
                    context.textMatrix = .identity; context.textPosition = .zero
                    context.setTextDrawingMode(.invisible)
                    CTLineDraw(text, context)
                    context.restoreGState()
                }
            }
            context.endPDFPage()
        }
        context.closePDF()
        return output as Data
    }
}
