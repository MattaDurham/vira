import Testing
import AVFoundation
@testable import Folio

struct CameraStreamPlanTests {
    private let nv12 = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
    private let packed = kCVPixelFormatType_422YpCbCr8_yuvs

    @Test func fixedUSBRatesPreserveExactSupportedDurations() {
        let fixed = CMTime(value: 333333, timescale: 10_000_000)
        #expect(CMTimeCompare(CameraStreamPlan.frameDuration(minimum: fixed, maximum: fixed), fixed) == 0)
        let slow = CMTime(value: 1, timescale: 5)
        #expect(CMTimeCompare(CameraStreamPlan.frameDuration(minimum: slow, maximum: slow), slow) == 0)
        #expect(CMTimeCompare(CameraStreamPlan.frameDuration(minimum: CMTime(value: 1, timescale: 60), maximum: CMTime(value: 1, timescale: 15)), CMTime(value: 1, timescale: 30)) == 0)
    }

    @Test func compatibilitySelects720pNativeTransportInsteadOfHighPreset() throws {
        let formats: [CameraStreamPlan.Format] = [
            .init(width: 640, height: 480, pixelFormat: packed),
            .init(width: 1280, height: 720, pixelFormat: nv12),
            .init(width: 1280, height: 720, pixelFormat: packed),
            .init(width: 1920, height: 1080, pixelFormat: nv12)
        ]
        let plan = try #require(CameraStreamPlan.attempts(for: .compatible, formats: formats).first)
        #expect(plan.formatIndex == 2)
        #expect(plan.width == 1280 && plan.height == 720)
        #expect(plan.pixelFormat == packed)
    }

    @Test func recoveryTriesAlternateTransportThenLowerResolutionsWithoutDuplicates() {
        let formats: [CameraStreamPlan.Format] = [
            .init(width: 3840, height: 2160, pixelFormat: nv12),
            .init(width: 1920, height: 1080, pixelFormat: nv12),
            .init(width: 1920, height: 1080, pixelFormat: nv12),
            .init(width: 1920, height: 1080, pixelFormat: packed),
            .init(width: 1280, height: 720, pixelFormat: nv12),
            .init(width: 640, height: 480, pixelFormat: packed)
        ]
        let plans = CameraStreamPlan.attempts(for: .fullHD, formats: formats)
        #expect(plans.map(\.formatIndex) == [1, 3, 4, 5])
        #expect(CameraStreamPlan.attempts(for: .best, formats: formats).first?.formatIndex == 0)
    }

    @Test func smallAndUnavailableCamerasStayWithinQualityLimits() {
        #expect(CameraStreamPlan.attempts(for: .best, formats: []).isEmpty)
        let only4K: [CameraStreamPlan.Format] = [.init(width: 3840, height: 2160, pixelFormat: nv12)]
        #expect(CameraStreamPlan.attempts(for: .compatible, formats: only4K).isEmpty)
        let small: [CameraStreamPlan.Format] = [.init(width: 320, height: 240, pixelFormat: nv12)]
        #expect(CameraStreamPlan.attempts(for: .best, formats: small).map(\.formatIndex) == [0])
    }
}
