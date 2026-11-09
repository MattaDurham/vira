import AppKit
import SwiftUI

struct CaptureShortcut: Codable, Equatable {
    var key: String
    var modifiers: UInt
    static let `default` = Self(key: "\r", modifiers: 0)
    static let preferencesKey = "captureShortcut"
    static let modifierMask: NSEvent.ModifierFlags = [.command, .option, .control, .shift]
    var flags: NSEvent.ModifierFlags { NSEvent.ModifierFlags(rawValue: modifiers).intersection(Self.modifierMask) }
    var keyEquivalent: KeyEquivalent { KeyEquivalent(key.first ?? "\r") }
    var eventModifiers: EventModifiers {
        var result: EventModifiers = []
        if flags.contains(.command) { result.insert(.command) }
        if flags.contains(.option) { result.insert(.option) }
        if flags.contains(.control) { result.insert(.control) }
        if flags.contains(.shift) { result.insert(.shift) }
        return result
    }
    var display: String {
        var prefix = ""
        if flags.contains(.control) { prefix += "⌃" }
        if flags.contains(.option) { prefix += "⌥" }
        if flags.contains(.shift) { prefix += "⇧" }
        if flags.contains(.command) { prefix += "⌘" }
        let names = ["\r": "Return", " ": "Space", "\u{F700}": "↑", "\u{F701}": "↓", "\u{F702}": "←", "\u{F703}": "→"]
        if let name = names[key] { return prefix + name }
        if let scalar = key.unicodeScalars.first, (0xF704...0xF717).contains(scalar.value) { return prefix + "F\(scalar.value - 0xF704 + 1)" }
        return prefix + key.uppercased()
    }
    var validationMessage: String? {
        guard key.count == 1, key != "\t", key != "\u{1b}", key != "\u{7f}" else { return "Choose a letter, number, Return, Space, arrow, or function key." }
        let shortcut = Self(key: key, modifiers: flags.rawValue)
        let commandKeys = ["q", "w", "h", "m", "n", "o", "p", "r", "s", ",", "a", "c", "v", "x", "z", "\r"]
        let reserved = commandKeys.map { Self(key: $0, modifiers: NSEvent.ModifierFlags.command.rawValue) }
            + ["z", "e", "c", "h"].map { Self(key: $0, modifiers: NSEvent.ModifierFlags([.command, .shift]).rawValue) }
        return reserved.contains(shortcut) ? "That shortcut is already used by Folio. Choose another." : nil
    }
    static func load(from preferences: UserDefaults) -> Self {
        guard let data = preferences.data(forKey: preferencesKey),
              let value = try? JSONDecoder().decode(Self.self, from: data), value.validationMessage == nil else { return .default }
        return value
    }
}

struct CaptureShortcutSettings: View {
    @ObservedObject var model: AppModel
    @State private var message: String?
    var body: some View {
        Form {
            LabeledContent("Capture Page") {
                ShortcutRecorder(shortcut: model.captureShortcut) { shortcut in
                    if let error = shortcut.validationMessage { message = error }
                    else { model.captureShortcut = shortcut; message = nil }
                }.frame(width: 200, height: 28)
            }
            Text(message ?? "Click the shortcut, then press a key with any modifiers. Escape cancels. Shortcuts are active in Folio while you are scanning; typing in a text field is unaffected.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Button("Reset to Return") { model.captureShortcut = .default; message = nil }
        }.padding(24).frame(width: 460).navigationTitle("Capture Shortcut")
    }
}

private struct ShortcutRecorder: NSViewRepresentable {
    var shortcut: CaptureShortcut
    var onChange: (CaptureShortcut) -> Void
    func makeNSView(context: Context) -> RecorderButton {
        let button = RecorderButton()
        button.bezelStyle = .rounded
        button.target = button; button.action = #selector(RecorderButton.record)
        button.setAccessibilityLabel("Capture Page shortcut")
        return button
    }
    func updateNSView(_ button: RecorderButton, context: Context) {
        button.shortcut = shortcut; button.onChange = onChange
        if !button.recording { button.title = shortcut.display }
    }
    final class RecorderButton: NSButton {
        var shortcut = CaptureShortcut.default
        var onChange: ((CaptureShortcut) -> Void)?
        var recording = false
        override var acceptsFirstResponder: Bool { true }
        @objc func record() {
            recording = true; title = "Press shortcut…"
            window?.makeFirstResponder(self)
        }
        override func resignFirstResponder() -> Bool {
            recording = false; title = shortcut.display
            return super.resignFirstResponder()
        }
        override func performKeyEquivalent(with event: NSEvent) -> Bool {
            if recording, window?.firstResponder === self { keyDown(with: event); return true }
            return super.performKeyEquivalent(with: event)
        }
        override func keyDown(with event: NSEvent) {
            guard recording else { super.keyDown(with: event); return }
            guard !event.isARepeat else { return }
            if event.keyCode == 53 { recording = false; title = shortcut.display; return }
            guard var key = event.charactersIgnoringModifiers?.lowercased(), key.count == 1 else { NSSound.beep(); return }
            if event.keyCode == 76 { key = "\r" }
            let next = CaptureShortcut(key: key, modifiers: event.modifierFlags.intersection(CaptureShortcut.modifierMask).rawValue)
            onChange?(next)
            recording = false; title = shortcut.display
        }
    }
}
