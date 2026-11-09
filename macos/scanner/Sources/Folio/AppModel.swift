import AppKit
import SwiftUI
import UniformTypeIdentifiers

@MainActor
final class AppModel: ObservableObject {
    @Published var document = ScanSession()
    @Published var selectedPageID: UUID?
    @Published var draft: CaptureDraft?
    @Published var draftImage: NSImage?
    @Published var processedPreview: NSImage?
    @Published var isBusy = false
    @Published var status = "All processing stays on your Mac."
    @Published var error: String?
    @Published var showText = false
    @Published var paperSize: PaperSize = .letter
    @Published var searchablePDF = true
    @Published var cameraQuality: CameraQuality = .best
    @Published var recognizeText = true
    @Published var defaultFinish: PageFinish = .color
    @Published var showNewConfirmation = false
    @Published var previewPDF: Data?
    @Published var showPDFPreview = false
    @Published var captureShortcut: CaptureShortcut {
        didSet { preferences.set(try? JSONEncoder().encode(captureShortcut), forKey: CaptureShortcut.preferencesKey) }
    }
    private let preferences: UserDefaults
    let camera = CameraController()
    let vira: ViraLink
    let store: SessionStore?
    private var saveTask: Task<Void, Never>?
    private var canSave = true
    var selectedPage: ScanPage? { document.pages.first { $0.id == selectedPageID } }

    init(store: SessionStore? = nil, preferences: UserDefaults = .standard, vira: ViraLink? = nil) {
        self.preferences = preferences
        self.vira = vira ?? ViraLink(preferences: preferences)
        self.captureShortcut = CaptureShortcut.load(from: preferences)
        do {
            let store = try store ?? SessionStore(); self.store = store
            do { document = try store.load(); if !document.pages.isEmpty { status = "Previous document restored. Saved on this Mac." } }
            catch { canSave = false; self.error = error.localizedDescription }
        } catch { self.store = nil; self.error = "Local storage could not be opened: \(error.localizedDescription)"; canSave = false }
    }
    func persist() {
        guard let store, canSave else { return }
        do { try store.save(document); status = "Saved on this Mac · \(document.pages.count) \(document.pages.count == 1 ? "page" : "pages")" }
        catch { self.error = "Your changes could not be saved: \(error.localizedDescription). Export a PDF before closing." }
    }
    func titleChanged() {
        saveTask?.cancel()
        saveTask = Task { try? await Task.sleep(for: .milliseconds(400)); guard !Task.isCancelled else { return }; persist() }
    }
    func image(for page: ScanPage) -> NSImage? { store.flatMap { NSImage(contentsOf: $0.url(page.imageFile)) } }
    func capture() {
        guard !isBusy, draft == nil, camera.isRunning, camera.hasFrame else { return }
        perform("Capturing page…") { let data = try await self.camera.capture(); try await self.beginDraft(data) }
    }
    func captureFromKeyboard() {
        guard NSApp.keyWindow?.identifier?.rawValue == "main", NSApp.keyWindow?.attachedSheet == nil else { return }
        if let editor = NSApp.keyWindow?.firstResponder as? NSTextView, editor.isEditable { return }
        capture()
    }
    func importImage() {
        guard !isBusy else { return }
        guard draft == nil else { error = "Add or cancel the current capture before importing another image."; return }
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.image]; panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        perform("Opening image…") {
            let data = try await Task.detached { try Data(contentsOf: url) }.value
            try await self.beginDraft(data)
        }
    }
    func sample() {
        guard !isBusy, draft == nil else { return }
        perform("Preparing sample…") { try await self.beginDraft(ImageProcessor.sample()) }
    }
    private func beginDraft(_ data: Data) async throws {
        let finish = defaultFinish
        let result = try await Task.detached { () -> (Data, PageCorners) in
            let image = try ImageProcessor.decode(data)
            return (try ImageProcessor.jpeg(image), try ImageProcessor.detect(image) ?? .inset)
        }.value
        draft = CaptureDraft(original: result.0, corners: result.1, finish: finish)
        draftImage = NSImage(data: result.0); processedPreview = nil; selectedPageID = nil; showText = false
        status = "Check the detected edges. Drag a corner to adjust it."
    }
    func edit(_ page: ScanPage) {
        guard !isBusy, let store else { return }
        guard draft == nil else { error = "Add or cancel your current capture before editing a saved page."; return }
        do {
            let original = try Data(contentsOf: store.url(page.originalFile))
            draft = CaptureDraft(original: original, corners: page.corners, finish: page.finish, rotation: page.rotation, editingID: page.id)
            draftImage = NSImage(data: original); processedPreview = nil; showText = false
        } catch { self.error = error.localizedDescription }
    }
    func updateCorner(_ index: Int, to point: ScanPoint) {
        guard var draft, !isBusy else { return }
        var next = draft.corners; next.points[index] = point
        guard next.isValid else { return }
        draft.corners = next; self.draft = draft; processedPreview = nil
    }
    func finishChanged(_ finish: PageFinish) { defaultFinish = finish; draft?.finish = finish; processedPreview = nil }
    func rotate() { guard draft != nil, !isBusy else { return }; draft!.rotation = (draft!.rotation + 90) % 360; processedPreview = nil }
    func fullFrame() { guard draft != nil, !isBusy else { return }; draft!.corners = .full; processedPreview = nil }
    func detectEdges() {
        guard let draft, !isBusy else { return }
        perform("Finding page edges…") {
            let corners = try await Task.detached { try ImageProcessor.detect(ImageProcessor.decode(draft.original)) }.value
            if let corners { self.draft?.corners = corners; self.processedPreview = nil }
            else { self.status = "No clear page boundary found. Adjust the four corners manually." }
        }
    }
    func previewFinish() {
        guard let draft, !isBusy else { return }
        perform("Refining preview…") {
            let jpeg = try await Task.detached { try ImageProcessor.jpeg(ImageProcessor.render(draft)) }.value
            self.processedPreview = NSImage(data: jpeg)
            self.status = "Preview ready. Add the page, or return to the corners to adjust it."
        }
    }
    func cancelDraft() { guard !isBusy else { return }; resetDraft() }
    private func resetDraft() { draft = nil; draftImage = nil; processedPreview = nil; status = "Ready for the next page." }
    func addPage() {
        guard let draft, !isBusy, let store, canSave else { return }
        let ocr = recognizeText
        perform(ocr ? "Straightening page and recognizing text…" : "Straightening page…") {
            let result = try await Task.detached { () -> (Data, Int, Int, [RecognizedLine]) in
                let image = try ImageProcessor.render(draft)
                let lines = ocr ? try ImageProcessor.recognize(image) : []
                return (try ImageProcessor.jpeg(image), image.width, image.height, lines)
            }.value
            let existing = self.document.pages.first(where: { $0.id == draft.editingID })
            let originalFile = try existing?.originalFile ?? store.writeImage(draft.original)
            let imageFile = try store.writeImage(result.0)
            let page = ScanPage(id: existing?.id ?? UUID(), originalFile: originalFile, imageFile: imageFile, corners: draft.corners, finish: draft.finish, rotation: draft.rotation, width: result.1, height: result.2, lines: result.3)
            if let index = self.document.pages.firstIndex(where: { $0.id == page.id }) { self.document.pages[index] = page }
            else { self.document.pages.append(page) }
            self.resetDraft(); self.selectedPageID = draft.editingID; self.showText = false; self.persist()
        }
    }
    func moveSelected(by offset: Int) {
        guard !isBusy, let index = document.pages.firstIndex(where: { $0.id == selectedPageID }), document.pages.indices.contains(index + offset) else { return }
        document.pages.swapAt(index, index + offset); persist()
    }
    func movePages(from source: IndexSet, to destination: Int) { guard !isBusy else { return }; document.pages.move(fromOffsets: source, toOffset: destination); persist() }
    func deleteSelected() {
        guard !isBusy, draft == nil, let index = document.pages.firstIndex(where: { $0.id == selectedPageID }) else { return }
        document.pages.remove(at: index)
        selectedPageID = document.pages.isEmpty ? nil : document.pages[min(index, document.pages.count - 1)].id
        persist()
    }
    func newDocument() {
        guard !isBusy else { return }
        saveTask?.cancel(); document = ScanSession(); selectedPageID = nil; showText = false; resetDraft(); previewPDF = nil; canSave = true; persist()
    }
    func copyText() {
        let text = selectedPage?.text ?? document.pages.map(\.text).joined(separator: "\n\n")
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string); status = "Recognized text copied."
    }
    private func makePDF() async throws -> Data {
        guard let store else { throw ScannerError.message("Local storage is unavailable.") }
        let pages = document.pages, title = document.title, paper = paperSize, searchable = searchablePDF
        return try await Task.detached {
            let assets = try pages.map { ($0, try Data(contentsOf: store.url($0.imageFile))) }
            return try PDFExporter.data(pages: assets, title: title, paper: paper, searchable: searchable)
        }.value
    }
    func exportPDF(preview: Bool = false) {
        guard !isBusy, !document.pages.isEmpty else { return }
        if preview {
            perform("Preparing PDF preview…") { self.previewPDF = try await self.makePDF(); self.showPDFPreview = true; self.status = "PDF preview ready." }
            return
        }
        guard draft == nil else { error = "Add or cancel the current capture before exporting the document."; return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [.pdf]; panel.canCreateDirectories = true
        panel.nameFieldStringValue = ScanExport.suggestedFilename()
        guard panel.runModal() == .OK, let url = panel.url else { return }
        perform("Exporting PDF…") {
            try await self.exportPDF(to: url)
        }
    }
    // Keep pages intact until the PDF and the fresh session are both saved.
    // Preview and save-panel cancellation never enter this path.
    func exportPDF(to url: URL) async throws {
        guard draft == nil else { throw ScannerError.message("Add or cancel the current capture before exporting the document.") }
        guard !document.pages.isEmpty, let store else { throw ScannerError.message("There are no saved pages to export.") }
        let data = try await makePDF()
        try await Task.detached { try data.write(to: url, options: .atomic) }.value
        let fresh = ScanSession()
        do { try store.save(fresh) }
        catch { throw ScannerError.message("The PDF was saved, but a new document could not be saved locally. Your pages have been kept. \(error.localizedDescription)") }
        saveTask?.cancel()
        document = fresh; selectedPageID = nil; showText = false; resetDraft()
        previewPDF = nil; showPDFPreview = false; canSave = true
        status = "Exported \(url.lastPathComponent) · Ready for a new document."
    }
    /// Save the document straight into Vira's scans folder (no dialog),
    /// start a fresh document, and tell Vira it arrived.
    func sendToVira() {
        guard !isBusy, !document.pages.isEmpty else { return }
        guard draft == nil else { error = "Add or cancel the current capture before sending the document."; return }
        perform("Sending to Vira…") {
            await self.vira.refresh()
            guard let folder = self.vira.folderURL else {
                throw ScannerError.message(self.vira.reachable == false
                    ? "Vira is not running, and it has not told Folio where scans go yet. Start Vira, or use Export PDF."
                    : "Vira has no scans folder set. Set one in Vira under Config > Intake > Scans folder.")
            }
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let url = ViraLink.freeURL(in: folder, name: ScanExport.suggestedFilename())
            try await self.exportPDF(to: url)
            await self.vira.announce(url)
            self.status = self.vira.reachable == true
                ? "Sent \(url.lastPathComponent) to Vira · Ready for a new document."
                : "Saved \(url.lastPathComponent) in the scans folder. Vira will file it when it is running."
        }
    }
    func exportSelectedImage() {
        guard let page = selectedPage, let store, !isBusy else { return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [.jpeg]; panel.nameFieldStringValue = "Scanned page.jpg"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do { try Data(contentsOf: store.url(page.imageFile)).write(to: url, options: .atomic); status = "Saved page image." }
        catch { self.error = error.localizedDescription }
    }
    func perform(_ message: String, operation: @escaping () async throws -> Void) {
        guard !isBusy else { return }; isBusy = true; status = message
        Task {
            defer { isBusy = false }
            do { try await operation() }
            catch { self.error = error.localizedDescription; self.status = "Ready to try again." }
        }
    }
    func prepareToQuit() { saveTask?.cancel(); persist(); camera.stop() }
}
