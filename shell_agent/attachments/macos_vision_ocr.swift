import Foundation
import ImageIO
import Vision

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

guard CommandLine.arguments.count == 2 else {
    fail("usage: vision-ocr <image-path>", code: 2)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fail("cannot decode image", code: 3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
let preferredLanguages = ["zh-Hans", "zh-Hant", "en-US"]
if let supportedLanguages = try? request.supportedRecognitionLanguages() {
    let selectedLanguages = preferredLanguages.filter { supportedLanguages.contains($0) }
    if !selectedLanguages.isEmpty {
        request.recognitionLanguages = selectedLanguages
    }
}

do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
} catch let error as NSError {
    fail("Vision OCR failed: \(error.domain) code=\(error.code) \(error.localizedDescription)", code: 4)
}

let observations = (request.results ?? []).sorted { left, right in
    let verticalDifference = abs(left.boundingBox.midY - right.boundingBox.midY)
    if verticalDifference > 0.015 {
        return left.boundingBox.midY > right.boundingBox.midY
    }
    return left.boundingBox.minX < right.boundingBox.minX
}

for observation in observations {
    if let candidate = observation.topCandidates(1).first {
        print(candidate.string)
    }
}
