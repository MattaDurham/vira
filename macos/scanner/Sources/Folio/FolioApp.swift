import SwiftUI
import AppKit

@main
struct FolioApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        Window("Folio", id: "main") {
            ContentView(model: model)
                .onAppear { delegate.model = model; NSApp.activate(ignoringOtherApps: true) }
        }
        .defaultSize(width: 1320, height: 840)
        .windowStyle(.titleBar)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Document") { model.showNewConfirmation = true }.keyboardShortcut("n").disabled(model.isBusy)
                Button("Import Image…") { model.importImage() }.keyboardShortcut("o").disabled(model.isBusy || model.draft != nil)
            }
            CommandGroup(replacing: .saveItem) {
                Button("Send to Vira") { model.sendToVira() }.keyboardShortcut("s").disabled(model.isBusy || model.document.pages.isEmpty || model.draft != nil)
                Button("Export PDF…") { model.exportPDF() }.keyboardShortcut("e", modifiers: [.command, .shift]).disabled(model.isBusy || model.document.pages.isEmpty || model.draft != nil)
                Button("Preview PDF") { model.exportPDF(preview: true) }.keyboardShortcut("p").disabled(model.isBusy || model.document.pages.isEmpty)
            }
            CommandMenu("Scan") {
                Button("Capture Page") { model.captureFromKeyboard() }.keyboardShortcut(model.captureShortcut.keyEquivalent, modifiers: model.captureShortcut.eventModifiers).disabled(model.isBusy || model.draft != nil || !model.camera.hasFrame)
                Button("Add Page to Document") { model.addPage() }.keyboardShortcut(.return, modifiers: .command).disabled(model.isBusy || model.draft == nil)
                Button("Rotate Capture") { model.rotate() }.keyboardShortcut("r", modifiers: .command).disabled(model.isBusy || model.draft == nil)
                SettingsLink { Text("Capture Shortcut…") }
                Divider()
                Button("Copy Recognized Text") { model.copyText() }.keyboardShortcut("c", modifiers: [.command, .shift]).disabled(model.isBusy || model.document.pages.isEmpty)
            }
        }
        Settings { TabView {
            CaptureShortcutSettings(model: model).tabItem { Label("Capture", systemImage: "camera") }
            ViraSettings(vira: model.vira).tabItem { Label("Vira", systemImage: "tray.and.arrow.down") }
        } }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var model: AppModel?
    func applicationDidFinishLaunching(_ notification: Notification) { NSApp.setActivationPolicy(.regular) }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if model?.isBusy == true {
            let alert = NSAlert(); alert.messageText = "Folio is still working"; alert.informativeText = "Wait for the current capture or export to finish before quitting."; alert.runModal(); return .terminateCancel
        }
        if model?.draft != nil {
            let alert = NSAlert(); alert.messageText = "Discard the current capture?"; alert.informativeText = "The capture has not been added to your document. Your saved pages will be kept."; alert.addButton(withTitle: "Keep Scanning"); alert.addButton(withTitle: "Discard and Quit")
            if alert.runModal() == .alertFirstButtonReturn { return .terminateCancel }
        }
        model?.prepareToQuit(); return .terminateNow
    }
}

struct ViraSettings: View {
    @ObservedObject var vira: ViraLink
    var body: some View {
        Form {
            TextField("Vira address", text: $vira.address)
            LabeledContent("Scans folder") { Text(vira.folder.isEmpty ? "Not known yet" : (vira.folder as NSString).abbreviatingWithTildeInPath).foregroundStyle(.secondary) }
            Text("Vira chooses the scans folder (Config > Intake > Scans folder). Send to Vira saves there with no dialog; Vira names the scan, files copies in the folders it belongs in and writes a summary note.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Button("Check Now") { Task { await vira.refresh() } }
        }.padding(24).frame(width: 460).navigationTitle("Vira")
    }
}
