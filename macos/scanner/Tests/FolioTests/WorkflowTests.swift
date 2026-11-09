import Testing
import AppKit
import PDFKit
@testable import Folio

struct WorkflowTests {
    @Test func exportNamesUseLocalTimeAndSortChronologically() throws {
        let date = try #require(ISO8601DateFormatter().date(from: "2026-11-06T22:05:09Z"))
        let zone = try #require(TimeZone(identifier: "America/New_York"))
        let name = ScanExport.suggestedFilename(at: date, timeZone: zone)
        #expect(name == "Scan 2026-11-06 17-05-09.pdf")
        #expect(name < ScanExport.suggestedFilename(at: date.addingTimeInterval(1), timeZone: zone))
        #expect(name < ScanExport.suggestedFilename(at: date.addingTimeInterval(86400), timeZone: zone))
    }

    @Test @MainActor func shortcutDefaultsPersistsAndAvoidsExistingCommands() throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = try SessionStore(directory: directory)
        let suite = UUID().uuidString
        let preferences = try #require(UserDefaults(suiteName: suite))
        defer { preferences.removePersistentDomain(forName: suite) }
        let model = AppModel(store: store, preferences: preferences)
        #expect(model.captureShortcut == .default)
        #expect(model.captureShortcut.display == "Return")
        let shortcut = CaptureShortcut(key: "k", modifiers: NSEvent.ModifierFlags([.command, .shift]).rawValue)
        #expect(shortcut.validationMessage == nil)
        model.captureShortcut = shortcut
        #expect(CaptureShortcut.load(from: preferences) == shortcut)
        #expect(AppModel(store: store, preferences: preferences).captureShortcut == shortcut)
        #expect(CaptureShortcut(key: "\r", modifiers: NSEvent.ModifierFlags.command.rawValue).validationMessage != nil)
        #expect(CaptureShortcut(key: "e", modifiers: NSEvent.ModifierFlags([.command, .shift]).rawValue).validationMessage != nil)
        preferences.set(Data("invalid".utf8), forKey: CaptureShortcut.preferencesKey)
        #expect(CaptureShortcut.load(from: preferences) == .default)
    }

    @Test @MainActor func addingPageReturnsToScannerAndPreviewPreservesDocument() async throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = AppModel(store: try SessionStore(directory: directory))
        model.recognizeText = false
        model.draft = CaptureDraft(original: try image(), corners: .full)
        model.addPage()
        try await finishWork(model)
        #expect(model.error == nil)
        #expect(model.document.pages.count == 1)
        #expect(model.selectedPageID == nil)
        #expect(model.draft == nil)
        #expect(model.draftImage == nil)
        model.exportPDF(preview: true)
        try await finishWork(model)
        #expect(model.previewPDF != nil)
        #expect(model.document.pages.count == 1)
        #expect(try model.store?.load().pages.count == 1)
    }

    @Test @MainActor func successfulExportSavesPDFAndFreshSession() async throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = try populatedModel(directory)
        let output = directory.appendingPathComponent("export.pdf")
        try await model.exportPDF(to: output)
        #expect(PDFDocument(url: output)?.pageCount == 1)
        #expect(model.document.pages.isEmpty)
        #expect(model.document.title == "Untitled document")
        #expect(model.selectedPageID == nil)
        #expect(model.previewPDF == nil)
        #expect(!model.showText)
        #expect(try model.store?.load().pages.isEmpty == true)
        #expect(AppModel(store: try SessionStore(directory: directory)).document.pages.isEmpty)
    }

    @Test @MainActor func failedExportAndPendingCaptureKeepAllPages() async throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = try populatedModel(directory)
        let originalID = model.document.pages[0].id
        do {
            try await model.exportPDF(to: directory.appendingPathComponent("missing/export.pdf"))
            Issue.record("Export to a missing directory should fail")
        } catch {}
        #expect(model.document.pages.first?.id == originalID)
        #expect(try model.store?.load().pages.first?.id == originalID)
        model.draft = CaptureDraft(original: try image(), corners: .full)
        let output = directory.appendingPathComponent("unfinished.pdf")
        do {
            try await model.exportPDF(to: output)
            Issue.record("An unfinished capture should prevent document reset")
        } catch {}
        #expect(model.document.pages.first?.id == originalID)
        #expect(model.draft != nil)
        #expect(!FileManager.default.fileExists(atPath: output.path))
    }

    @Test @MainActor func failedFreshSessionSaveRetainsPagesAfterPDFWasWritten() async throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = try populatedModel(directory)
        let originalID = model.document.pages[0].id
        // A directory at the manifest path makes atomic replacement fail.
        let manifest = directory.appendingPathComponent("session.json")
        try FileManager.default.removeItem(at: manifest)
        try FileManager.default.createDirectory(at: manifest, withIntermediateDirectories: false)
        let output = directory.appendingPathComponent("export.pdf")
        do {
            try await model.exportPDF(to: output)
            Issue.record("The fresh session save should fail")
        } catch {
            #expect(error.localizedDescription.contains("PDF was saved"))
        }
        #expect(PDFDocument(url: output)?.pageCount == 1)
        #expect(model.document.pages.first?.id == originalID)
    }

    @MainActor private func populatedModel(_ directory: URL) throws -> AppModel {
        let store = try SessionStore(directory: directory)
        let name = try store.writeImage(image())
        let page = ScanPage(id: UUID(), originalFile: name, imageFile: name, corners: .full, finish: .color, rotation: 0, width: 200, height: 300, lines: [])
        try store.save(ScanSession(title: "Test scan", pages: [page]))
        let model = AppModel(store: store)
        model.selectedPageID = page.id; model.showText = true
        return model
    }
    @MainActor private func finishWork(_ model: AppModel) async throws {
        let deadline = ContinuousClock.now + .seconds(10)
        while model.isBusy, ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(10)) }
        #expect(!model.isBusy)
    }
    private func temporaryDirectory() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString) }
    private func image() throws -> Data {
        let context = try #require(CGContext(data: nil, width: 200, height: 300, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.setFillColor(CGColor(gray: 1, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: 200, height: 300))
        return try ImageProcessor.jpeg(#require(context.makeImage()))
    }
}
