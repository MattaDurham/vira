import Testing
import AppKit
import PDFKit
@testable import Folio

/// A fake Vira: answers the three routes Folio calls and records the arrivals.
final class FakeVira: URLProtocol {
    nonisolated(unsafe) static var folder = ""
    nonisolated(unsafe) static var arrived: [String] = []
    nonisolated(unsafe) static var up = true
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard Self.up, let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost)); return
        }
        var body: Any = [:]
        switch url.path {
        case "/api/scans/config": body = ["enabled": true, "folder": Self.folder, "auto": true]
        case "/api/scans/arrived":
            let data = request.httpBody ?? request.httpBodyStream.map { stream -> Data in
                stream.open(); defer { stream.close() }
                var out = Data(); var buf = [UInt8](repeating: 0, count: 4096)
                while stream.hasBytesAvailable { let n = stream.read(&buf, maxLength: buf.count); if n <= 0 { break }; out.append(buf, count: n) }
                return out
            } ?? Data()
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            Self.arrived.append(json?["path"] as? String ?? "")
            body = ["ok": true, "id": "abc123def456"]
        default: body = ["id": "abc123def456", "state": "filed", "name": "2026-11-06 Bill.pdf",
                         "decision": ["title": "Water bill"], "filing": ["copies": [["rel": "raw/Finances/2026-11-06 Bill.pdf"]]]]
        }
        let data = try! JSONSerialization.data(withJSONObject: body)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@Suite(.serialized) struct ViraLinkTests {
    @Test @MainActor func sendToViraSavesInViraFolderAnnouncesAndStartsFresh() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let scans = directory.appendingPathComponent("scans")
        FakeVira.folder = scans.path; FakeVira.arrived = []; FakeVira.up = true
        let (model, preferences) = try populatedModel(directory.appendingPathComponent("session"))
        model.sendToVira()
        try await finishWork(model)
        #expect(model.error == nil)
        let saved = try FileManager.default.contentsOfDirectory(atPath: scans.path)
        #expect(saved.count == 1)
        #expect(saved[0].hasPrefix("Scan ") && saved[0].hasSuffix(".pdf"))
        #expect(PDFDocument(url: scans.appendingPathComponent(saved[0]))?.pageCount == 1)
        #expect(FakeVira.arrived == [scans.appendingPathComponent(saved[0]).path])
        #expect(model.document.pages.isEmpty)
        #expect(preferences.string(forKey: ViraLink.folderKey) == scans.path)
        // Folio follows the scan until Vira has filed it.
        let deadline = ContinuousClock.now + .seconds(5)
        while model.vira.lastScan?.state != "filed", ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(20)) }
        #expect(model.vira.lastScan?.headline == "Filed: Water bill · 1 copy saved")
    }

    @Test @MainActor func viraDownStillSavesIntoTheLastKnownFolder() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let scans = directory.appendingPathComponent("scans")
        FakeVira.up = false; FakeVira.arrived = []
        let (model, preferences) = try populatedModel(directory.appendingPathComponent("session"))
        preferences.set(scans.path, forKey: ViraLink.folderKey)
        let link = ViraLink(preferences: preferences, session: Self.session())
        let model2 = AppModel(store: model.store, preferences: preferences, vira: link)
        model2.sendToVira()
        try await finishWork(model2)
        #expect(model2.error == nil)
        #expect(try FileManager.default.contentsOfDirectory(atPath: scans.path).count == 1)
        #expect(model2.status.contains("Vira will file it when it is running"))
    }

    @Test @MainActor func viraDownWithNoKnownFolderKeepsThePages() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        FakeVira.up = false
        let (model, _) = try populatedModel(directory)
        model.sendToVira()
        try await finishWork(model)
        #expect(model.error?.contains("Vira is not running") == true)
        #expect(model.document.pages.count == 1)
    }

    @Test func freeURLNeverOverwrites() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        try Data("a".utf8).write(to: directory.appendingPathComponent("Scan.pdf"))
        #expect(ViraLink.freeURL(in: directory, name: "Scan.pdf").lastPathComponent == "Scan 2.pdf")
    }

    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [FakeVira.self]
        return URLSession(configuration: config)
    }

    @MainActor private func populatedModel(_ directory: URL) throws -> (AppModel, UserDefaults) {
        let store = try SessionStore(directory: directory)
        let context = try #require(CGContext(data: nil, width: 200, height: 300, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.setFillColor(CGColor(gray: 1, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: 200, height: 300))
        let name = try store.writeImage(ImageProcessor.jpeg(#require(context.makeImage())))
        let page = ScanPage(id: UUID(), originalFile: name, imageFile: name, corners: .full, finish: .color, rotation: 0, width: 200, height: 300, lines: [])
        try store.save(ScanSession(title: "Test scan", pages: [page]))
        let preferences = try #require(UserDefaults(suiteName: UUID().uuidString))
        let model = AppModel(store: store, preferences: preferences, vira: ViraLink(preferences: preferences, session: Self.session()))
        return (model, preferences)
    }
    @MainActor private func finishWork(_ model: AppModel) async throws {
        try await Task.sleep(for: .milliseconds(20))
        let deadline = ContinuousClock.now + .seconds(10)
        while model.isBusy, ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(10)) }
        #expect(!model.isBusy)
    }
}
