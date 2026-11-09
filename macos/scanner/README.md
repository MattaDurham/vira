# Folio

Vira's document scanner: a native macOS app for USB webcams, built with SwiftUI, AVFoundation, Vision, Core Image, and PDFKit. EYE TO EYE cameras are selected by default when connected. There are no third-party dependencies, and the only network peer is the Vira server on this Mac.

## With Vira

Vira owns the scans folder (Config > Intake > Scans folder). **Send to Vira** (Cmd-S) saves the document there as a searchable PDF with no dialog, starts a fresh document, and tells Vira it arrived (`POST /api/scans/arrived`). Vira then reads it with an AI session, renames it, saves copies in the vault folders it belongs in, writes a summary note, and lists any to-dos in Attention. The Vira panel in Folio's right column follows the scan until it is filed; **Open Scans in Vira** shows it there. Vira's Scans window has an **Open scanner** button that starts this app.

If Vira is not running, Send to Vira still saves into the last folder Vira named, and Vira files the scan when it starts. Folio > Settings > Vira holds the server address (default `http://localhost:8377`).

## Run

Requires macOS 14 or later and Apple's Swift command-line tools (or Xcode).

```sh
./scripts/build.sh
open build/Folio.app
```

The build produces a locally signed app bundle. You can copy `build/Folio.app` to Applications. This is a local development build, without Apple notarization.

Use the app bundle rather than `swift run`, so macOS can associate camera permission with Folio's bundle identifier and camera usage description.

## Scan

1. Select your webcam and click **Connect Camera**. Allow camera access when macOS asks. If access was previously denied, enable Folio under **System Settings → Privacy & Security → Camera**.
2. Position the webcam so all page corners are visible. Capture with **Capture Page** or **Return**. Change the key (including modifiers) in **Folio → Settings → Capture Page**, or click the shortcut shown in the camera controls. The choice is remembered between launches. Images are not mirrored.
3. Review the detected page boundary. Drag the four corner handles, or focus a handle and use the arrow keys (Shift makes larger adjustments). **Full Frame** includes the entire image.
4. Choose color, grayscale, or black and white. Rotate if needed, then use **Preview Finish** to inspect the result.
5. Click **Add to Document** (⌘Return). Local text recognition runs when enabled. Adding a new page returns to the scanner immediately, ready for the next capture.
6. Select pages to inspect or edit them. Reorder with the arrows or by dragging in the page list. **Show Text** displays recognized text.
7. Choose a PDF paper size and **Export PDF** (⇧⌘E). Searchable PDF adds an invisible text layer when OCR results are available. The save dialog suggests `Scan yyyy-MM-dd HH-mm-ss.pdf` using local time, so filenames sort chronologically (sort names descending for newest first). After the PDF is successfully saved, Folio automatically starts a fresh document and stays in the scanner. Canceling the dialog, previewing, or a failed PDF write keeps the current document. Add or cancel any unfinished capture before exporting. You can preview the PDF first, or save a selected page as JPEG from its context menu.

Use **Import Image** (⌘O) for JPEG, PNG, HEIC, TIFF, and other formats supported by macOS. **Try a sample document** exercises the crop and OCR workflow without camera access.

## Local storage

The current document, original images, processed images, and recognized text are saved under `~/Library/Application Support/Folio/`. The session is restored at launch. Successful PDF export starts and saves a fresh document automatically. You can also use **File → New Document** (⌘N) to start over manually after confirmation. Original images remain available for editing without repeatedly degrading image quality. Files from previous sessions are retained in that folder; this version does not provide a document library or automatic cleanup.

Images are decoded with orientation correction and limited to a 6000-pixel maximum edge for processing. The camera selects an explicit device format up to 4K, 1080p, or 720p according to the Quality menu. Compatibility mode prefers native packed YUV when available, including slower frame rates offered by USB webcams. If the stream starts without frames or later stalls, Folio automatically tries alternate native formats and lower resolutions (down to 640 × 480 when supported). The displayed resolution reflects the active stream, which may be lower after recovery. Capture is enabled only after the camera delivers a frame. Autofocus locking appears only on cameras that expose it through AVFoundation. Resolution and focus support depend on the webcam. This app requests no microphone access.

If the camera connection waits for permission, check the macOS prompt and Camera privacy settings. You can cancel a pending connection. Rebuilding this locally signed app changes its signing identity and may require granting camera permission again. If you see no video, check the webcam's privacy switch, its USB connection, and other apps using it, then try the compatibility quality setting.

## Development and checks

```sh
swift build
./scripts/test.sh
./scripts/build.sh
```

Tests use Swift Testing, which is bundled with current command-line tools. They verify export reset and failure recovery, return to scanner after adding pages, persisted capture shortcuts, chronologically sortable export names, camera quality limits and fallback format selection, crop validity, perspective correction, rotation, grayscale, document detection, OCR, searchable multipage PDF output, and session recovery. Live camera delivery still requires a hardware check with macOS camera permission granted.

`scripts/make-icon.swift` generates the app icon using native drawing. To regenerate:

```sh
swift scripts/make-icon.swift /tmp/Folio.iconset
iconutil -c icns /tmp/Folio.iconset -o Resources/Folio.icns
```

Camera quality still depends on mounting distance, lighting, focus, and sensor resolution. A centered webcam may need repositioning or a downward-facing mount to scan a flat page. The scanner corrects perspective; it does not flatten curved book pages.
