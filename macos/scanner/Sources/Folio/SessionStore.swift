import Foundation

final class SessionStore {
    let directory: URL
    init(directory: URL? = nil) throws {
        self.directory = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Folio", isDirectory: true)
        try FileManager.default.createDirectory(at: self.directory, withIntermediateDirectories: true)
    }
    func url(_ name: String) -> URL { directory.appendingPathComponent(name) }
    func load() throws -> ScanSession {
        let manifest = url("session.json")
        guard FileManager.default.fileExists(atPath: manifest.path) else { return ScanSession() }
        let session = try JSONDecoder().decode(ScanSession.self, from: Data(contentsOf: manifest))
        guard session.version == 1 else { throw ScannerError.message("This saved session was created by a newer version of Folio.") }
        for page in session.pages {
            guard [page.originalFile, page.imageFile].allSatisfy({ $0 == URL(fileURLWithPath: $0).lastPathComponent && FileManager.default.fileExists(atPath: url($0).path) }) else { throw ScannerError.message("A saved page is missing. Your existing files have been kept in \(directory.path).") }
        }
        return session
    }
    func save(_ session: ScanSession) throws {
        try JSONEncoder().encode(session).write(to: url("session.json"), options: .atomic)
    }
    func writeImage(_ data: Data) throws -> String {
        let name = UUID().uuidString + ".jpg"
        try data.write(to: url(name), options: .atomic)
        return name
    }
}
