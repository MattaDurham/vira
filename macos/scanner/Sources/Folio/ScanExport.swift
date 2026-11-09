import Foundation

enum ScanExport {
    static func suggestedFilename(at date: Date = Date(), timeZone: TimeZone = .autoupdatingCurrent) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = timeZone
        formatter.dateFormat = "yyyy-MM-dd HH-mm-ss"
        return "Scan \(formatter.string(from: date)).pdf"
    }
}
