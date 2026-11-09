import AVFoundation

/// Select actual device formats: session presets may silently retain a higher
/// resolution on external cameras, including when asking for compatibility mode.
struct CameraStreamPlan: Equatable {
    var formatIndex: Int
    var width: Int32
    var height: Int32
    var pixelFormat: FourCharCode

    struct Format {
        var width: Int32
        var height: Int32
        var pixelFormat: FourCharCode
    }

    static func frameDuration(minimum: CMTime, maximum: CMTime) -> CMTime {
        // Clamp using rational times. Rounding a fixed UVC rate such as
        // 30.00003 fps through Double can produce an unsupported duration.
        CMTimeMaximum(minimum, CMTimeMinimum(maximum, CMTime(value: 1, timescale: 30)))
    }

    static func attempts(for quality: CameraQuality, formats: [Format]) -> [Self] {
        let limit: (Int32, Int32) = quality == .best ? (3840, 2160) : quality == .fullHD ? (1920, 1080) : (1280, 720)
        let usable = formats.enumerated().filter {
            $0.element.width > 0 && $0.element.height > 0 && $0.element.width <= limit.0 && $0.element.height <= limit.1
        }.sorted {
            let a = Int64($0.element.width) * Int64($0.element.height)
            let b = Int64($1.element.width) * Int64($1.element.height)
            if a != b { return a > b }
            // Packed YUV modes often use less USB bandwidth by offering a lower
            // frame rate. Prefer them immediately in compatibility mode.
            let packed: FourCharCode = kCVPixelFormatType_422YpCbCr8_yuvs
            if quality == .compatible, ($0.element.pixelFormat == packed) != ($1.element.pixelFormat == packed) {
                return $0.element.pixelFormat == packed
            }
            return $0.offset < $1.offset
        }
        guard let first = usable.first else { return [] }
        var result: [Self] = []
        func append(_ candidate: (offset: Int, element: Format)) {
            let format = candidate.element
            // Duplicate resolution/subtype entries commonly expose higher frame
            // rates; retrying them doesn't provide a different transport path.
            guard !result.contains(where: { $0.width == format.width && $0.height == format.height && $0.pixelFormat == format.pixelFormat }) else { return }
            result.append(Self(formatIndex: candidate.offset, width: format.width, height: format.height, pixelFormat: format.pixelFormat))
        }
        append(first)
        // Try the alternate native transport at the requested resolution first,
        // then step down. Never increase resolution during recovery.
        for size in [(first.element.width, first.element.height), (1280, 720), (640, 480)] as [(Int32, Int32)] {
            guard size.0 <= first.element.width && size.1 <= first.element.height else { continue }
            for candidate in usable where candidate.element.width == size.0 && candidate.element.height == size.1 { append(candidate) }
        }
        return result
    }

    static func attempts(for quality: CameraQuality, device: AVCaptureDevice) -> [Self] {
        attempts(for: quality, formats: device.formats.map {
            let size = CMVideoFormatDescriptionGetDimensions($0.formatDescription)
            return Format(width: size.width, height: size.height, pixelFormat: CMFormatDescriptionGetMediaSubType($0.formatDescription))
        })
    }
}
