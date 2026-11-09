import SwiftUI
import AVFoundation
import PDFKit

struct CameraPreview: NSViewRepresentable {
    let session: AVCaptureSession
    func makeNSView(context: Context) -> PreviewHost { let view = PreviewHost(); view.preview.session = session; return view }
    func updateNSView(_ view: PreviewHost, context: Context) { if view.preview.session !== session { view.preview.session = session } }
    final class PreviewHost: NSView {
        let preview = AVCaptureVideoPreviewLayer()
        override init(frame frameRect: NSRect) {
            super.init(frame: frameRect); wantsLayer = true; layer?.backgroundColor = NSColor.black.cgColor
            preview.videoGravity = .resizeAspect; layer?.addSublayer(preview)
        }
        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
        override func layout() {
            super.layout(); preview.frame = bounds
            if let connection = preview.connection, connection.isVideoMirroringSupported { connection.automaticallyAdjustsVideoMirroring = false; connection.isVideoMirrored = false }
        }
    }
}

struct PDFPreview: NSViewRepresentable {
    var data: Data
    func makeNSView(context: Context) -> PDFView { let view = PDFView(); view.autoScales = true; view.displayMode = .singlePageContinuous; view.document = PDFDocument(data: data); return view }
    func updateNSView(_ nsView: PDFView, context: Context) {}
}

struct CropEditor: View {
    @ObservedObject var model: AppModel
    var image: NSImage
    var body: some View {
        GeometryReader { geometry in
            let ratio = image.size.width / max(1, image.size.height)
            let w = min(geometry.size.width - 52, (geometry.size.height - 52) * ratio)
            let h = w / ratio
            ZStack(alignment: .topLeading) {
                Image(nsImage: image).resizable().interpolation(.high).frame(width: w, height: h)
                if let draft = model.draft {
                    let points = draft.corners.points
                    Path { path in
                        path.addRect(CGRect(x: 0, y: 0, width: w, height: h))
                        path.move(to: CGPoint(x: points[0].x * w, y: points[0].y * h))
                        points.dropFirst().forEach { path.addLine(to: CGPoint(x: $0.x * w, y: $0.y * h)) }; path.closeSubpath()
                    }.fill(.black.opacity(0.55), style: FillStyle(eoFill: true)).allowsHitTesting(false)
                    Path { path in
                        path.move(to: CGPoint(x: points[0].x * w, y: points[0].y * h))
                        points.dropFirst().forEach { path.addLine(to: CGPoint(x: $0.x * w, y: $0.y * h)) }; path.closeSubpath()
                    }.stroke(.white.opacity(0.95), style: StrokeStyle(lineWidth: 1.5, dash: [6, 4])).allowsHitTesting(false)
                    ForEach(0..<4, id: \.self) { index in
                        Button {} label: {
                            Text("\(index + 1)").font(.system(size: 11, weight: .bold, design: .rounded)).foregroundStyle(.white).frame(width: 26, height: 26).background(Color.accentColor, in: Circle()).overlay(Circle().stroke(.white, lineWidth: 2)).shadow(color: .black.opacity(0.3), radius: 3, y: 1)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(["Top left", "Top right", "Bottom right", "Bottom left"][index] + " crop corner")
                        .help("Drag to the page corner, or use arrow keys. Hold Shift for larger steps.")
                        .onKeyPress(.leftArrow) { shift(index, dx: -0.003, dy: 0); return .handled }
                        .onKeyPress(.rightArrow) { shift(index, dx: 0.003, dy: 0); return .handled }
                        .onKeyPress(.upArrow) { shift(index, dx: 0, dy: -0.003); return .handled }
                        .onKeyPress(.downArrow) { shift(index, dx: 0, dy: 0.003); return .handled }
                        .position(x: points[index].x * w, y: points[index].y * h)
                        .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .named("crop")).onChanged { value in
                            model.updateCorner(index, to: ScanPoint(x: min(1, max(0, value.location.x / w)), y: min(1, max(0, value.location.y / h))))
                        })
                    }
                }
            }
            .frame(width: w, height: h).coordinateSpace(name: "crop")
            .position(x: geometry.size.width / 2, y: geometry.size.height / 2)
        }
    }
    private func shift(_ index: Int, dx: Double, dy: Double) {
        guard let points = model.draft?.corners.points else { return }
        let scale = NSEvent.modifierFlags.contains(.shift) ? 5.0 : 1
        model.updateCorner(index, to: ScanPoint(x: min(1, max(0, points[index].x + dx * scale)), y: min(1, max(0, points[index].y + dy * scale))))
    }
}
