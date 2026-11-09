import AppKit
import AVFoundation
import CoreImage
import Combine
import OSLog

struct CameraChoice: Identifiable, Equatable {
    var id: String
    var name: String
}

final class CameraController: NSObject, ObservableObject, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    let session = AVCaptureSession()
    @Published var devices: [CameraChoice] = []
    @Published var selectedID = ""
    @Published var isRunning = false
    @Published var isConnecting = false
    @Published var hasFrame = false
    @Published var streamWarning: String?
    @Published var resolution = "Camera is off"
    @Published var error: String?
    @Published var supportsFocus = false
    @Published var focusLocked = false
    private let sessionQueue = DispatchQueue(label: "folio.camera.session")
    private let frameQueue = DispatchQueue(label: "folio.camera.frames", qos: .userInitiated)
    private var latestFrame: CVPixelBuffer?
    private var latestTimestamp: CFAbsoluteTime = 0
    private var lastPublishedTimestamp: CFAbsoluteTime = 0
    private var input: AVCaptureDeviceInput?
    private var output: AVCaptureVideoDataOutput?
    private var frameOutput: AVCaptureVideoDataOutput?
    private var frameToken = 0
    private let logger = Logger(subsystem: "app.vira.folio", category: "Camera")
    private let generationLock = NSLock()
    private var observers: [NSObjectProtocol] = []
    private var sleepObserver: NSObjectProtocol?
    private var generation = 0

    override init() {
        super.init()
        refreshDevices()
        for name in [AVCaptureDevice.wasConnectedNotification, AVCaptureDevice.wasDisconnectedNotification] {
            observers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                guard let self else { return }
                let activeID = self.selectedID
                self.refreshDevices()
                if self.isRunning && !self.devices.contains(where: { $0.id == activeID }) {
                    self.stop(); self.error = "The camera was disconnected. Plug in your webcam and connect again."
                }
            })
        }
        observers.append(NotificationCenter.default.addObserver(forName: AVCaptureSession.runtimeErrorNotification, object: session, queue: .main) { [weak self] note in
            self?.stop(); self?.error = (note.userInfo?[AVCaptureSessionErrorKey] as? Error)?.localizedDescription ?? "The camera stopped. Reconnect to continue."
        })
        sleepObserver = NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in self?.stop() }
    }
    deinit {
        observers.forEach(NotificationCenter.default.removeObserver)
        if let sleepObserver { NSWorkspace.shared.notificationCenter.removeObserver(sleepObserver) }
    }

    private static func availableDevices() -> [AVCaptureDevice] {
        AVCaptureDevice.DiscoverySession(deviceTypes: [.external, .builtInWideAngleCamera, .continuityCamera], mediaType: .video, position: .unspecified).devices
    }
    func refreshDevices() {
        devices = Self.availableDevices().map { CameraChoice(id: $0.uniqueID, name: $0.localizedName) }
        if !devices.contains(where: { $0.id == selectedID }) {
            selectedID = devices.first(where: { $0.name.range(of: "eye to eye", options: .caseInsensitive) != nil })?.id ?? devices.first?.id ?? ""
        }
    }
    func connect(quality: CameraQuality) {
        guard !isConnecting else { return }
        isConnecting = true; hasFrame = false; streamWarning = nil; error = nil
        resolution = AVCaptureDevice.authorizationStatus(for: .video) == .notDetermined ? "Waiting for macOS camera permission…" : "Starting camera…"
        let id = selectedID
        let token = nextGeneration()
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self] in
            guard let self, self.isCurrent(token), self.isConnecting,
                  AVCaptureDevice.authorizationStatus(for: .video) == .notDetermined else { return }
            self.streamWarning = "Allow camera access in the macOS prompt. If no prompt appears, check Folio in Camera privacy settings. Rebuilding this local app can require permission again."
        }
        Task { @MainActor in
            let permission: Bool
            switch AVCaptureDevice.authorizationStatus(for: .video) {
            case .authorized: permission = true
            case .notDetermined: permission = await AVCaptureDevice.requestAccess(for: .video)
            default: permission = false
            }
            guard self.isCurrent(token) else { return }
            guard permission else {
                self.isConnecting = false
                self.error = "Allow Folio to use the camera in System Settings → Privacy & Security → Camera, then connect again."
                return
            }
            self.sessionQueue.async { [weak self] in
                guard let self, self.isCurrent(token) else { return }
                do {
                    guard let device = Self.availableDevices().first(where: { $0.uniqueID == id }) else { throw ScannerError.message("The selected camera is unavailable. Check its USB connection.") }
                    let plans = CameraStreamPlan.attempts(for: quality, device: device)
                    guard !plans.isEmpty else { throw ScannerError.message("This camera has no supported format at the selected quality. Try another quality setting.") }
                    try self.start(device: device, plans: plans, attempt: 0, token: token)
                } catch { self.fail(error, token: token) }
            }
        }
    }

    // Generation checks are shared by the main, session, and frame queues. A
    // cancelled connection must never restart a camera or publish an old frame.
    private func nextGeneration() -> Int {
        generationLock.lock(); defer { generationLock.unlock() }
        generation += 1
        return generation
    }
    private func isCurrent(_ token: Int) -> Bool {
        generationLock.lock(); defer { generationLock.unlock() }
        return generation == token
    }

    private func start(device: AVCaptureDevice, plans: [CameraStreamPlan], attempt: Int, token: Int) throws {
        guard isCurrent(token) else { return }
        session.stopRunning()
        output?.setSampleBufferDelegate(nil, queue: nil)
        frameQueue.sync {
            latestFrame = nil; latestTimestamp = 0; lastPublishedTimestamp = 0
            frameOutput = nil; frameToken = token
        }
        guard isCurrent(token) else { return }
        let plan = plans[attempt]
        let input = try AVCaptureDeviceInput(device: device)
        let output = AVCaptureVideoDataOutput()
        session.beginConfiguration()
        do {
            defer { session.commitConfiguration() }
            session.inputs.forEach(session.removeInput)
            session.outputs.forEach(session.removeOutput)
            guard session.canAddInput(input) else { throw ScannerError.message("This camera cannot provide a video stream. Close other camera apps and try again.") }
            session.addInput(input)
            guard session.canAddOutput(output) else { throw ScannerError.message("This camera cannot deliver video frames to Folio.") }
            session.addOutput(output)
            try device.lockForConfiguration()
            device.activeFormat = device.formats[plan.formatIndex]
            if let range = device.activeFormat.videoSupportedFrameRateRanges.first {
                let duration = CameraStreamPlan.frameDuration(minimum: range.minFrameDuration, maximum: range.maxFrameDuration)
                device.activeVideoMinFrameDuration = duration
                device.activeVideoMaxFrameDuration = duration
            }
            device.unlockForConfiguration()
            // Specify dimensions as well as subtype on macOS. Otherwise adding
            // an output/preview can renegotiate back to the high preset's size.
            let pixelFormat = output.availableVideoPixelFormatTypes.contains(plan.pixelFormat) ? plan.pixelFormat : kCVPixelFormatType_32BGRA
            output.videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String: pixelFormat,
                kCVPixelBufferWidthKey as String: plan.width,
                kCVPixelBufferHeightKey as String: plan.height
            ]
            output.alwaysDiscardsLateVideoFrames = true
            frameQueue.sync { frameOutput = output }
            output.setSampleBufferDelegate(self, queue: frameQueue)
            if let connection = output.connection(with: .video) {
                if connection.isVideoMinFrameDurationSupported {
                    connection.videoMinFrameDuration = device.activeVideoMinFrameDuration
                }
                if connection.isVideoMirroringSupported {
                    connection.automaticallyAdjustsVideoMirroring = false; connection.isVideoMirrored = false
                }
            }
            self.input = input; self.output = output
        }
        guard isCurrent(token) else { return }
        logger.info("Starting format \(plan.formatIndex): \(plan.width)x\(plan.height), subtype \(plan.pixelFormat), attempt \(attempt + 1)")
        session.startRunning()
        let running = session.isRunning
        let dimensions = CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription)
        logger.info("Active stream: \(dimensions.width)x\(dimensions.height), subtype \(CMFormatDescriptionGetMediaSubType(device.activeFormat.formatDescription))")
        DispatchQueue.main.async {
            guard self.isCurrent(token) else { return }
            self.isRunning = running; self.isConnecting = false
            self.hasFrame = false
            self.resolution = "\(dimensions.width) × \(dimensions.height)"
            self.supportsFocus = device.isFocusModeSupported(.locked) && device.isFocusModeSupported(.continuousAutoFocus)
            self.focusLocked = device.focusMode == .locked
            self.streamWarning = attempt > 0 ? "Trying a more compatible camera mode…" : nil
            if !running { self.error = "The camera did not start. Check its connection and try again." }
        }
        if running { monitor(device: device, plans: plans, attempt: attempt, token: token, startup: true) }
    }

    private func monitor(device: AVCaptureDevice, plans: [CameraStreamPlan], attempt: Int, token: Int, startup: Bool) {
        sessionQueue.asyncAfter(deadline: .now() + (startup ? 6 : 2)) { [weak self] in
            guard let self, self.isCurrent(token), self.session.isRunning else { return }
            let recent = self.frameQueue.sync { self.latestFrame != nil && CFAbsoluteTimeGetCurrent() - self.latestTimestamp < 3 }
            if recent {
                self.monitor(device: device, plans: plans, attempt: attempt, token: token, startup: false)
            } else if attempt + 1 < plans.count {
                DispatchQueue.main.async {
                    guard self.isCurrent(token) else { return }
                    self.hasFrame = false; self.streamWarning = "Trying a more compatible camera mode…"
                }
                do { try self.start(device: device, plans: plans, attempt: attempt + 1, token: token) }
                catch { self.fail(error, token: token) }
            } else {
                self.frameQueue.sync { self.latestFrame = nil }
                DispatchQueue.main.async {
                    guard self.isCurrent(token) else { return }
                    self.hasFrame = false
                    self.streamWarning = "No video frames received after trying compatible camera modes. Check the camera’s privacy switch and USB connection, then disconnect and reconnect the camera."
                }
                // Keep checking so uncovering/reconnecting a stalled camera can
                // restore capture without leaving a stale LIVE indicator.
                self.monitor(device: device, plans: plans, attempt: attempt, token: token, startup: false)
            }
        }
    }

    private func fail(_ error: Error, token: Int) {
        guard isCurrent(token) else { return }
        session.stopRunning()
        frameQueue.sync { latestFrame = nil; frameOutput = nil }
        DispatchQueue.main.async {
            guard self.isCurrent(token) else { return }
            self.isConnecting = false; self.isRunning = false; self.hasFrame = false
            self.resolution = "Camera is off"; self.supportsFocus = false; self.streamWarning = nil
            self.error = error.localizedDescription
        }
    }
    func stop() {
        _ = nextGeneration(); isRunning = false; isConnecting = false; hasFrame = false; streamWarning = nil; resolution = "Camera is off"; supportsFocus = false
        sessionQueue.async { [weak self] in
            self?.session.stopRunning()
            self?.output?.setSampleBufferDelegate(nil, queue: nil)
            self?.frameQueue.sync { self?.latestFrame = nil; self?.frameOutput = nil; self?.latestTimestamp = 0 }
        }
    }
    func setFocusLocked(_ locked: Bool) {
        sessionQueue.async { [weak self] in
            guard let device = self?.input?.device else { return }
            do {
                try device.lockForConfiguration(); defer { device.unlockForConfiguration() }
                let mode: AVCaptureDevice.FocusMode = locked ? .locked : .continuousAutoFocus
                guard device.isFocusModeSupported(mode) else { return }
                device.focusMode = mode
                DispatchQueue.main.async { self?.focusLocked = locked }
            } catch { DispatchQueue.main.async { self?.error = error.localizedDescription } }
        }
    }
    func capture() async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            frameQueue.async { [weak self] in
                guard let self, self.isCurrent(self.frameToken), let buffer = self.latestFrame, CFAbsoluteTimeGetCurrent() - self.latestTimestamp < 3 else { continuation.resume(throwing: ScannerError.message("The camera has not delivered a recent frame. Wait a moment, or reconnect it.")); return }
                do {
                    let image = CIImage(cvPixelBuffer: buffer)
                    guard let cg = ImageProcessor.context.createCGImage(image, from: image.extent) else { throw ScannerError.message("Could not capture this frame.") }
                    continuation.resume(returning: try ImageProcessor.jpeg(cg, quality: 0.98))
                } catch { continuation.resume(throwing: error) }
            }
        }
    }
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard output === frameOutput, isCurrent(frameToken), let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        latestFrame = buffer
        let token = frameToken
        latestTimestamp = CFAbsoluteTimeGetCurrent()
        if latestTimestamp - lastPublishedTimestamp > 0.5 {
            lastPublishedTimestamp = latestTimestamp
            DispatchQueue.main.async { [weak self] in
                guard let self, self.isCurrent(token), self.isRunning, self.frameQueue.sync(execute: { self.frameOutput === output }) else { return }
                if !self.hasFrame {
                    self.hasFrame = true; self.streamWarning = nil
                    self.resolution = "\(CVPixelBufferGetWidth(buffer)) × \(CVPixelBufferGetHeight(buffer))"
                    self.logger.info("Camera delivered video frames")
                }
            }
        }
    }
}
