import AppKit
import Foundation

/// Folio's connection to the Vira server on this Mac.
///
/// Vira owns the scans folder (Config > Intake > Scans folder). Folio asks for
/// it, saves each finished PDF there, tells Vira the file arrived, and then
/// follows that scan until Vira has filed it, so the scanner window can say
/// where the paper went. The folder is the source of truth: when Vira is not
/// running, Folio still saves into the last folder Vira named, and Vira picks
/// the file up when it starts.
struct ViraScan: Decodable, Equatable {
    struct Decision: Decodable, Equatable { var title: String?; var filename: String? }
    struct Copy: Decodable, Equatable { var rel: String }
    struct Filing: Decodable, Equatable { var copies: [Copy]? }
    var id: String
    var state: String
    var name: String?
    var error: String?
    var decision: Decision?
    var filing: Filing?

    var settled: Bool { ["filed", "ready", "failed", "ignored", "missing"].contains(state) }
    var headline: String {
        let title = decision?.title ?? name ?? "Your scan"
        switch state {
        case "new": return "Vira has it and will read it next."
        case "reading": return "Vira is reading it."
        case "filing": return "Vira is filing it."
        case "filed":
            let n = filing?.copies?.count ?? 0
            return "Filed: \(title)" + (n > 0 ? " · \(n) \(n == 1 ? "copy" : "copies") saved" : "")
        case "ready": return "Waiting for you in Vira: \(title)"
        case "failed": return "Not filed: \(error ?? "open Vira to see why")"
        default: return "\(title): \(state)"
        }
    }
}

@MainActor
final class ViraLink: ObservableObject {
    static let addressKey = "ViraAddress"
    static let folderKey = "ViraScansFolder"
    @Published var address: String { didSet { preferences.set(address, forKey: Self.addressKey) } }
    @Published private(set) var folder: String
    @Published private(set) var reachable: Bool?
    @Published private(set) var filingOn = true
    @Published private(set) var lastScan: ViraScan?
    @Published private(set) var lastPath: String?
    private let preferences: UserDefaults
    private let session: URLSession
    private var follow: Task<Void, Never>?

    init(preferences: UserDefaults = .standard, session: URLSession = .shared) {
        self.preferences = preferences
        self.session = session
        address = preferences.string(forKey: Self.addressKey) ?? "http://localhost:8377"
        folder = preferences.string(forKey: Self.folderKey) ?? ""
    }

    var base: URL? { URL(string: address.trimmingCharacters(in: .whitespaces)) }
    var folderURL: URL? { folder.isEmpty ? nil : URL(fileURLWithPath: (folder as NSString).expandingTildeInPath, isDirectory: true) }

    private struct Config: Decodable { var enabled: Bool; var folder: String }
    private struct Arrived: Decodable { var id: String? }

    /// Ask Vira where scans go. Keeps the last answer when Vira is down.
    func refresh() async {
        guard let url = base?.appendingPathComponent("api/scans/config") else { reachable = false; return }
        do {
            var request = URLRequest(url: url); request.timeoutInterval = 4
            let (data, response) = try await session.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { reachable = false; return }
            let config = try JSONDecoder().decode(Config.self, from: data)
            reachable = true
            filingOn = config.enabled
            if !config.folder.isEmpty { folder = config.folder; preferences.set(config.folder, forKey: Self.folderKey) }
        } catch { reachable = false }
    }

    /// Tell Vira a file arrived, then follow it until Vira settles it.
    func announce(_ file: URL) async {
        lastPath = file.path
        lastScan = nil
        guard let url = base?.appendingPathComponent("api/scans/arrived") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"; request.timeoutInterval = 6
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["path": file.path])
        guard let (data, response) = try? await session.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let id = (try? JSONDecoder().decode(Arrived.self, from: data))?.id, !id.isEmpty else {
            reachable = false
            return
        }
        reachable = true
        followScan(id)
    }

    func followScan(_ id: String) {
        follow?.cancel()
        follow = Task { [weak self] in
            let deadline = Date().addingTimeInterval(20 * 60)
            while !Task.isCancelled, Date() < deadline {
                guard let self, let url = self.base?.appendingPathComponent("api/scans/\(id)") else { return }
                if let (data, response) = try? await self.session.data(from: url),
                   (response as? HTTPURLResponse)?.statusCode == 200,
                   let scan = try? JSONDecoder().decode(ViraScan.self, from: data) {
                    self.lastScan = scan
                    if scan.settled { return }
                } else if self.lastScan == nil {
                    // Vira has not seen the file yet (it looks every few seconds).
                }
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    func openInVira() {
        guard let base else { return }
        let hash = lastScan.map { "#scans/\($0.id)" } ?? "#scans"
        if let url = URL(string: base.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/" + hash) {
            NSWorkspace.shared.open(url)
        }
    }

    /// A file name in `folder` that does not exist yet.
    nonisolated static func freeURL(in folder: URL, name: String) -> URL {
        var url = folder.appendingPathComponent(name)
        let stem = (name as NSString).deletingPathExtension, ext = (name as NSString).pathExtension
        var n = 2
        while FileManager.default.fileExists(atPath: url.path) {
            url = folder.appendingPathComponent("\(stem) \(n).\(ext)"); n += 1
        }
        return url
    }
}
