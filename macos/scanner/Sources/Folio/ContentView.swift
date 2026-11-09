import SwiftUI

private let accent = Color(nsColor: NSColor(name: NSColor.Name("FolioAccent")) { appearance in
    if appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua {
        return NSColor(calibratedRed: 0.57, green: 0.78, blue: 0.65, alpha: 1)
    }
    return NSColor(calibratedRed: 0.20, green: 0.37, blue: 0.29, alpha: 1)
})

struct ContentView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var camera: CameraController
    @ObservedObject var vira: ViraLink
    init(model: AppModel) { self.model = model; camera = model.camera; vira = model.vira }

    var body: some View {
        NavigationSplitView {
            sidebar.navigationSplitViewColumnWidth(min: 190, ideal: 220, max: 280)
        } detail: {
            HSplitView {
                scanner.frame(minWidth: 460, minHeight: 480)
                inspector.frame(minWidth: 230, idealWidth: 250, maxWidth: 300)
            }
        }
        .tint(accent)
        .frame(minWidth: 1000, minHeight: 680)
        .toolbar {
            ToolbarItem(placement: .principal) { Text("Folio").font(.system(.headline, design: .serif)) }
            ToolbarItemGroup(placement: .primaryAction) {
                Button { model.importImage() } label: { Label("Import", systemImage: "photo.badge.plus") }.disabled(model.isBusy || model.draft != nil).help("Import an image (⌘O)")
                Button { model.exportPDF(preview: true) } label: { Label("Preview PDF", systemImage: "doc.viewfinder") }.disabled(model.isBusy || model.document.pages.isEmpty)
                Button { model.exportPDF() } label: { Label("Export PDF", systemImage: "square.and.arrow.up") }.disabled(model.isBusy || model.document.pages.isEmpty || model.draft != nil).help("Export PDF and start a new document (⇧⌘E)")
                Button { model.sendToVira() } label: { Label("Send to Vira", systemImage: "tray.and.arrow.down") }.disabled(model.isBusy || model.document.pages.isEmpty || model.draft != nil).help("Save into Vira's scans folder; Vira names and files it (⌘S)")
            }
        }
        .alert("Folio", isPresented: Binding(get: { model.error != nil || camera.error != nil }, set: { if !$0 { model.error = nil; camera.error = nil } })) {
            Button("OK") { model.error = nil; camera.error = nil }
            if camera.error?.contains("System Settings") == true {
                Button("Open Camera Settings") { NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera")!); camera.error = nil }
            }
        } message: { Text(model.error ?? camera.error ?? "") }
        .confirmationDialog("Start a new document?", isPresented: $model.showNewConfirmation, titleVisibility: .visible) {
            Button("New Document", role: .destructive) { model.newDocument() }
            Button("Cancel", role: .cancel) {}
        } message: { Text("Export your current document first if you want to keep it. The current session will be replaced.") }
        .sheet(isPresented: $model.showPDFPreview) {
            VStack(spacing: 0) {
                HStack { Text(model.document.title).font(.headline); Spacer(); Button("Done") { model.showPDFPreview = false }.keyboardShortcut(.cancelAction) }.padding()
                Divider()
                if let data = model.previewPDF { PDFPreview(data: data) }
            }.frame(width: 800, height: 720)
        }
        .onChange(of: model.document.title) { _, _ in model.titleChanged() }
        .task { await vira.refresh() }
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack { Image(systemName: "doc.on.doc").foregroundStyle(accent); Text("Your document").font(.headline); Spacer(); Text("\(model.document.pages.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary) }.padding(.horizontal, 16).padding(.top, 18).padding(.bottom, 12)
            TextField("Document title", text: $model.document.title).textFieldStyle(.roundedBorder).padding(.horizontal, 14).padding(.bottom, 12).accessibilityLabel("Document title").disabled(model.isBusy)
            Divider()
            if model.document.pages.isEmpty {
                VStack(spacing: 12) { Image(systemName: "doc.badge.plus").font(.system(size: 30, weight: .ultraLight)).foregroundStyle(.tertiary); Text("A fresh start").font(.headline); Text("Captured pages will appear here, ready to arrange and export.").font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center) }.padding(24).frame(maxHeight: .infinity)
            } else {
                List(selection: $model.selectedPageID) {
                    ForEach(Array(model.document.pages.enumerated()), id: \.element.id) { index, page in
                        HStack(spacing: 12) {
                            if let image = model.image(for: page) { Image(nsImage: image).resizable().scaledToFit().frame(width: 54, height: 70).background(.white).clipShape(RoundedRectangle(cornerRadius: 3)) }
                            VStack(alignment: .leading, spacing: 5) { Text("Page \(index + 1)").font(.callout.weight(.medium)); Text(page.finish.title).font(.caption).foregroundStyle(.secondary); if !page.lines.isEmpty { Label("Text recognized", systemImage: "text.viewfinder").font(.system(size: 10)).foregroundStyle(.secondary) } }
                            Spacer(minLength: 0)
                        }.padding(.vertical, 7).tag(page.id)
                        .contextMenu {
                            Button("Edit Corners & Finish") { model.edit(page) }.disabled(model.draft != nil || model.isBusy)
                            Button("Save Image…") { model.selectedPageID = page.id; model.exportSelectedImage() }
                            Divider()
                            Button("Delete Page", role: .destructive) { model.selectedPageID = page.id; model.deleteSelected() }.disabled(model.draft != nil || model.isBusy)
                        }
                    }.onMove { source, destination in model.movePages(from: source, to: destination) }
                }.listStyle(.sidebar).disabled(model.isBusy || model.draft != nil)
            }
            Divider()
            HStack(spacing: 6) {
                Button { model.moveSelected(by: -1) } label: { Image(systemName: "arrow.up") }.help("Move selected page earlier")
                Button { model.moveSelected(by: 1) } label: { Image(systemName: "arrow.down") }.help("Move selected page later")
                Spacer()
                Button(role: .destructive) { model.deleteSelected() } label: { Image(systemName: "trash") }.help("Delete selected page")
            }.buttonStyle(.borderless).disabled(model.selectedPage == nil || model.isBusy || model.draft != nil).padding(14)
        }
    }

    private var scanner: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text(model.draft != nil ? "Refine your page" : model.selectedPage != nil ? "One page closer." : "Bring paper into focus.").font(.system(size: 27, weight: .regular, design: .serif))
                    Text(model.draft != nil ? "Check the edges. Choose a finish. Make it yours." : "Capture, straighten, and collect your documents.").font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                if model.selectedPage != nil && model.draft == nil { Button { model.selectedPageID = nil; model.showText = false } label: { Label("Scanner", systemImage: "camera") } }
            }.padding(24)
            ZStack {
                Color(nsColor: NSColor(calibratedWhite: 0.12, alpha: 1))
                if let image = model.processedPreview, model.draft != nil {
                    Image(nsImage: image).resizable().scaledToFit().padding(26)
                } else if let image = model.draftImage, model.draft != nil {
                    CropEditor(model: model, image: image)
                } else if let page = model.selectedPage {
                    if model.showText {
                        ScrollView { Text(page.text.isEmpty ? "No text was recognized on this page. Try a sharper capture or turn on text recognition before adding a page." : page.text).font(.body).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(28) }.background(Color(nsColor: .textBackgroundColor))
                    } else if let image = model.image(for: page) { Image(nsImage: image).resizable().scaledToFit().padding(26) }
                } else if camera.isRunning {
                    CameraPreview(session: camera.session)
                    if let warning = camera.streamWarning { VStack(spacing: 12) { Image(systemName: "video.slash").font(.largeTitle); Text(warning).multilineTextAlignment(.center).frame(maxWidth: 340) }.foregroundStyle(.white).padding(24).background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 12)) }
                    VStack { HStack { Label(camera.hasFrame ? "LIVE" : "WAITING FOR VIDEO", systemImage: "circle.fill").font(.system(size: 10, weight: .semibold)).foregroundStyle(.white).padding(9).background(.black.opacity(0.55), in: Capsule()); Spacer(); Text(camera.resolution).font(.caption.monospacedDigit()).foregroundStyle(.white.opacity(0.8)) }; Spacer() }.padding(18).allowsHitTesting(false)
                } else {
                    VStack(spacing: 17) {
                        Image(systemName: "viewfinder").font(.system(size: 64, weight: .ultraLight)).foregroundStyle(.white.opacity(0.55)).padding(.bottom, 10)
                        Text("A little less paper.\nA little more order.").font(.system(size: 30, weight: .regular, design: .serif)).multilineTextAlignment(.center).foregroundStyle(.white)
                        Text("Connect your webcam or import an image\nto start your next document.").font(.callout).foregroundStyle(.white.opacity(0.6)).multilineTextAlignment(.center).lineSpacing(4)
                        Button { camera.connect(quality: model.cameraQuality) } label: { Label(camera.isConnecting ? "Connecting…" : "Connect Camera", systemImage: "camera").padding(.horizontal, 10).padding(.vertical, 4) }.buttonStyle(.borderedProminent).disabled(camera.isConnecting || camera.devices.isEmpty || model.isBusy)
                        if camera.isConnecting, let warning = camera.streamWarning {
                            Text(warning).font(.callout).foregroundStyle(.white.opacity(0.8)).multilineTextAlignment(.center).frame(maxWidth: 360)
                            Button("Open Camera Settings") { NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera")!) }
                        }
                        Button("Try a sample document") { model.sample() }.buttonStyle(.plain).font(.callout).foregroundStyle(.white.opacity(0.7)).padding(.top, 3).disabled(model.isBusy)
                    }.padding(28)
                }
                if model.isBusy { Color.black.opacity(0.35); ProgressView(model.status).padding(24).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12)) }
            }.clipShape(RoundedRectangle(cornerRadius: 10)).padding(.horizontal, 24)
            HStack {
                if let draft = model.draft {
                    if model.processedPreview != nil { Button("Adjust Corners") { model.processedPreview = nil } }
                    else { Button { model.detectEdges() } label: { Label("Detect Edges", systemImage: "viewfinder") }; Button("Full Frame") { model.fullFrame() } }
                    Spacer()
                    Text("\(draft.rotation)°").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    Button { model.rotate() } label: { Image(systemName: "rotate.right") }.help("Rotate output clockwise (⌘R)")
                    Button("Preview Finish") { model.previewFinish() }
                } else if let page = model.selectedPage {
                    Button { model.edit(page) } label: { Label("Edit Page", systemImage: "crop.rotate") }
                    Spacer()
                    Text("\(page.width) × \(page.height)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    Button(model.showText ? "Show Image" : "Show Text") { model.showText.toggle() }
                    Button { model.copyText() } label: { Image(systemName: "doc.on.clipboard") }.help("Copy recognized text")
                } else {
                    Label(camera.isRunning ? "Keep all four page corners in view." : "The EYE TO EYE webcam is preferred automatically.", systemImage: "info.circle").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                }
            }.buttonStyle(.borderless).padding(.horizontal, 27).frame(height: 48).disabled(model.isBusy)
            Divider().padding(.horizontal, 24)
            HStack {
                Button { model.importImage() } label: { Label("Import Image", systemImage: "photo.badge.plus") }.disabled(model.draft != nil || model.isBusy)
                Spacer()
                if model.draft != nil {
                    Button("Cancel") { model.cancelDraft() }.disabled(model.isBusy)
                    Button { model.addPage() } label: { Label(model.draft?.editingID == nil ? "Add to Document" : "Save Changes", systemImage: "checkmark").padding(.horizontal, 8).padding(.vertical, 3) }.buttonStyle(.borderedProminent).disabled(model.isBusy)
                } else {
                    Button { model.selectedPageID = nil; model.capture() } label: { Label("Capture Page", systemImage: "camera.aperture").padding(.horizontal, 10).padding(.vertical, 3) }.buttonStyle(.borderedProminent).disabled(model.isBusy || !camera.hasFrame || camera.isConnecting).help("Capture Page (\(model.captureShortcut.display))")
                }
            }.padding(.horizontal, 24).padding(.vertical, 16)
            HStack(spacing: 7) { Image(systemName: model.isBusy ? "hourglass" : "checkmark.shield"); Text(model.status).lineLimit(2); Spacer() }.font(.caption).foregroundStyle(.secondary).padding(.horizontal, 24).padding(.bottom, 14)
        }.background(Color(nsColor: .windowBackgroundColor))
    }

    private var inspector: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 23) {
                inspectorSection("CAMERA", symbol: "camera") {
                    Picker("Device", selection: $camera.selectedID) {
                        if camera.devices.isEmpty { Text("No camera connected").tag("") }
                        ForEach(camera.devices) { Text($0.name).tag($0.id) }
                    }.labelsHidden().disabled(camera.isConnecting || model.isBusy)
                    .onChange(of: camera.selectedID) { _, _ in if camera.isRunning { camera.connect(quality: model.cameraQuality) } }
                    Picker("Quality", selection: $model.cameraQuality) { ForEach(CameraQuality.allCases) { Text($0.rawValue).tag($0) } }.labelsHidden().disabled(camera.isConnecting || model.isBusy)
                        .onChange(of: model.cameraQuality) { _, _ in if camera.isRunning { camera.connect(quality: model.cameraQuality) } }
                    Text(camera.resolution).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    HStack {
                        Text("Capture shortcut").font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        SettingsLink { Text(model.captureShortcut.display) }.font(.caption).help("Change the Capture Page shortcut")
                    }
                    Button(camera.isConnecting ? "Cancel Connection" : camera.isRunning ? "Disconnect Camera" : "Connect Camera") {
                        if camera.isConnecting || camera.isRunning { camera.stop() } else { camera.connect(quality: model.cameraQuality) }
                    }.disabled(camera.devices.isEmpty || model.isBusy)
                    if camera.supportsFocus { Toggle("Lock autofocus", isOn: Binding(get: { camera.focusLocked }, set: { camera.setFocusLocked($0) })).font(.callout) }
                    Text("Use even light and position your webcam so the whole page is visible.").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Divider()
                inspectorSection("PAGE FINISH", symbol: "slider.horizontal.3") {
                    Picker("Finish", selection: Binding(get: { model.draft?.finish ?? model.defaultFinish }, set: { model.finishChanged($0) })) {
                        ForEach(PageFinish.allCases) { finish in Label(finish.title, systemImage: finish.symbol).tag(finish) }
                    }.pickerStyle(.radioGroup).labelsHidden().disabled(model.isBusy)
                    Toggle("Recognize text", isOn: $model.recognizeText).font(.callout).disabled(model.isBusy)
                    Text("Text recognition happens on your Mac when you add a page.").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Divider()
                inspectorSection("VIRA", symbol: "tray.and.arrow.down") {
                    Button { model.sendToVira() } label: { Label("Send to Vira", systemImage: "tray.and.arrow.down").frame(maxWidth: .infinity) }.buttonStyle(.borderedProminent).disabled(model.isBusy || model.document.pages.isEmpty || model.draft != nil)
                    Text(viraLine).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    if let scan = vira.lastScan {
                        Text(scan.headline).font(.callout).fixedSize(horizontal: false, vertical: true)
                    } else if let path = vira.lastPath {
                        Text("Sent \((path as NSString).lastPathComponent). Waiting for Vira to pick it up.").font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    if vira.reachable == true { Button("Open Scans in Vira") { vira.openInVira() }.font(.callout) }
                }
                Divider()
                inspectorSection("PDF EXPORT", symbol: "doc.richtext") {
                    Picker("Paper", selection: $model.paperSize) { ForEach(PaperSize.allCases) { Text($0.rawValue).tag($0) } }.disabled(model.isBusy)
                    Toggle("Searchable PDF", isOn: $model.searchablePDF).font(.callout).disabled(model.isBusy)
                    Text("Recognized text is included as an invisible, selectable layer.").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    Text("Saving the PDF starts a fresh document automatically.").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    Button { model.exportPDF() } label: { Label("Export PDF…", systemImage: "square.and.arrow.up").frame(maxWidth: .infinity) }.disabled(model.isBusy || model.document.pages.isEmpty || model.draft != nil)
                }
                Spacer(minLength: 15)
                Label("Private by design", systemImage: "lock.shield").font(.callout.weight(.medium)).foregroundStyle(accent)
                Text("No account. Folio talks only to Vira on this Mac. Your current document is saved automatically.").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }.padding(22)
        }.background(.regularMaterial)
    }

    private var viraLine: String {
        switch vira.reachable {
        case .some(true):
            return vira.filingOn ? "Saves into \((vira.folder as NSString).abbreviatingWithTildeInPath). Vira names it, files copies and writes a summary."
                                 : "Vira is running, but its scans folder is not set. Set it in Config > Intake."
        case .some(false):
            return vira.folder.isEmpty ? "Vira is not running at \(vira.address)." : "Vira is not running; scans wait in \((vira.folder as NSString).abbreviatingWithTildeInPath) until it is."
        case .none: return "Looking for Vira…"
        }
    }

    private func inspectorSection<Content: View>(_ title: String, symbol: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 13) { Label(title, systemImage: symbol).font(.system(size: 10, weight: .semibold)).tracking(1.4).foregroundStyle(.secondary); content() }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
