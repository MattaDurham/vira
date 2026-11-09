import AppKit
import CoreImage
import CoreImage.CIFilterBuiltins
import Vision
import ImageIO
import UniformTypeIdentifiers

enum ImageProcessor {
    static let context = CIContext(options: [.cacheIntermediates: false])

    static func decode(_ data: Data) throws -> CGImage {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: 6000
              ] as CFDictionary) else { throw ScannerError.message("This image could not be opened. Try a JPEG, PNG, HEIC, or TIFF file.") }
        return image
    }

    static func jpeg(_ image: CGImage, quality: Double = 0.96) throws -> Data {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) else { throw ScannerError.message("Could not encode this image.") }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { throw ScannerError.message("Could not encode this image.") }
        return data as Data
    }

    static func detect(_ image: CGImage) throws -> PageCorners? {
        let request = VNDetectRectanglesRequest()
        request.maximumObservations = 1
        request.minimumConfidence = 0.55
        request.minimumAspectRatio = 0.2
        request.minimumSize = 0.15
        request.quadratureTolerance = 35
        try VNImageRequestHandler(cgImage: image).perform([request])
        guard let result = request.results?.first else { return nil }
        let corners = PageCorners(result)
        return corners.isValid ? corners : nil
    }

    static func render(_ draft: CaptureDraft) throws -> CGImage {
        guard draft.corners.isValid else { throw ScannerError.message("The crop corners must form a rectangle without crossing edges.") }
        let original = try decode(draft.original)
        let image = CIImage(cgImage: original)
        let correction = CIFilter.perspectiveCorrection()
        correction.inputImage = image
        let p = draft.corners.points.map { CGPoint(x: $0.x * Double(original.width), y: (1 - $0.y) * Double(original.height)) }
        correction.topLeft = p[0]; correction.topRight = p[1]; correction.bottomRight = p[2]; correction.bottomLeft = p[3]
        guard var result = correction.outputImage else { throw ScannerError.message("The page could not be straightened. Try adjusting its corners.") }
        if draft.rotation != 0 { result = result.transformed(by: CGAffineTransform(rotationAngle: -Double(draft.rotation) * .pi / 180)) }
        result = result.transformed(by: CGAffineTransform(translationX: -result.extent.minX, y: -result.extent.minY))
        if draft.finish != .color {
            let filter = CIFilter.colorControls()
            filter.inputImage = result; filter.saturation = 0
            filter.contrast = draft.finish == .document ? 1.5 : 1.05
            filter.brightness = draft.finish == .document ? 0.08 : 0
            result = filter.outputImage ?? result
        }
        if draft.finish == .document {
            let threshold = CIFilter.colorThreshold()
            threshold.inputImage = result; threshold.threshold = 0.56
            result = threshold.outputImage ?? result
        }
        guard result.extent.width > 8, result.extent.height > 8,
              let output = context.createCGImage(result, from: result.extent) else { throw ScannerError.message("The selected area is too small. Move the corners farther apart.") }
        return output
    }

    static func recognize(_ image: CGImage) throws -> [RecognizedLine] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.automaticallyDetectsLanguage = true
        try VNImageRequestHandler(cgImage: image).perform([request])
        return (request.results ?? []).compactMap { observation in
            guard let text = observation.topCandidates(1).first?.string else { return nil }
            let rect = observation.boundingBox
            return RecognizedLine(text: text, x: rect.minX, y: rect.minY, width: rect.width, height: rect.height)
        }
    }

    static func sample() throws -> Data {
        let image = NSImage(size: NSSize(width: 1400, height: 1100))
        image.lockFocus()
        NSColor(calibratedRed: 0.35, green: 0.39, blue: 0.36, alpha: 1).setFill(); NSRect(x: 0, y: 0, width: 1400, height: 1100).fill()
        NSColor(calibratedWhite: 0.99, alpha: 1).setFill(); NSRect(x: 360, y: 95, width: 680, height: 910).fill()
        func text(_ string: String, x: Double, y: Double, size: Double, bold: Bool = false) {
            (string as NSString).draw(at: NSPoint(x: x, y: y), withAttributes: [.font: bold ? NSFont.boldSystemFont(ofSize: size) : NSFont.systemFont(ofSize: size), .foregroundColor: NSColor(calibratedWhite: 0.16, alpha: 1)])
        }
        text("FOLIO / FIELD NOTES", x: 410, y: 925, size: 20, bold: true)
        text("A fresh page.", x: 410, y: 820, size: 44, bold: true)
        text("A small guide to a clearer workspace.", x: 410, y: 770, size: 21)
        let sections = [("01   Find your light", "Even, natural light keeps details sharp.", "Avoid casting a shadow over your page."), ("02   Keep it flat", "Place your document on a plain surface.", "Make sure all four corners are visible."), ("03   Make it yours", "Adjust the corners and choose a finish.", "Collect your pages, then export a PDF.")]
        for (i, section) in sections.enumerated() {
            let y = 650.0 - Double(i) * 155
            text(section.0, x: 410, y: y, size: 23, bold: true)
            text(section.1, x: 410, y: y - 42, size: 19)
            text(section.2, x: 410, y: y - 75, size: 19)
        }
        text("SAMPLE DOCUMENT · PROCESSED ON YOUR MAC", x: 410, y: 155, size: 15)
        image.unlockFocus()
        guard let tiff = image.tiffRepresentation, let source = CGImageSourceCreateWithData(tiff as CFData, nil), let cg = CGImageSourceCreateImageAtIndex(source, 0, nil) else { throw ScannerError.message("Could not create the sample.") }
        return try jpeg(cg)
    }
}
