// On-device speech-to-text CLI wrapping macOS 26's SpeechAnalyzer + SpeechTranscriber.
//
//   transcribe <audio-file> [locale]
//
// Emits timestamped JSON on stdout:
//   {"engine":"speechtranscriber","locale":"en-US",
//    "segments":[{"start":0.0,"end":1.2,"text":"..."}],
//    "text":"full transcript"}
//
// Everything runs on-device. No API key, no network model call at inference.

import AVFoundation
import Foundation
import Speech

struct Segment: Codable { let start: Double; let end: Double; let text: String }
struct Output: Codable {
    let engine: String
    let locale: String
    let segments: [Segment]
    let text: String
}

func fail(_ msg: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(("[transcribe] " + msg + "\n").data(using: .utf8)!)
    exit(code)
}

@available(macOS 26.0, *)
func transcribe(path: String, localeID: String) async throws -> Output {
    let url = URL(fileURLWithPath: path)
    guard FileManager.default.fileExists(atPath: path) else { fail("no such file: \(path)") }

    let locale = Locale(identifier: localeID)

    // Configure the transcriber to report per-segment audio time ranges.
    let transcriber = SpeechTranscriber(
        locale: locale,
        transcriptionOptions: [],
        reportingOptions: [],
        attributeOptions: [.audioTimeRange]
    )

    // Ensure the on-device model for this locale is installed.
    if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
        FileHandle.standardError.write("[transcribe] installing speech model…\n".data(using: .utf8)!)
        try await request.downloadAndInstall()
    }

    let analyzer = SpeechAnalyzer(modules: [transcriber])

    // Collect results concurrently as the analyzer emits them.
    var segments: [Segment] = []
    let collector = Task {
        for try await result in transcriber.results {
            let attributed = result.text
            let plain = String(attributed.characters)
            var start = CMTime.invalid
            var end = CMTime.invalid
            for run in attributed.runs {
                if let r = run.audioTimeRange {
                    if start == .invalid { start = r.start }
                    end = r.end
                }
            }
            let s = start.isValid ? start.seconds : 0
            let e = end.isValid ? end.seconds : s
            segments.append(Segment(start: s, end: e, text: plain.trimmingCharacters(in: .whitespacesAndNewlines)))
        }
    }

    // Feed the audio file straight into the analyzer.
    let audioFile = try AVAudioFile(forReading: url)
    if let last = try await analyzer.analyzeSequence(from: audioFile) {
        try await analyzer.finalizeAndFinish(through: last)
    } else {
        try await analyzer.finalizeAndFinishThroughEndOfInput()
    }

    try await collector.value

    segments = segments.filter { !$0.text.isEmpty }
    let full = segments.map { $0.text }.joined(separator: " ")
    return Output(engine: "speechtranscriber", locale: localeID, segments: segments, text: full)
}

// --- entry point ---
let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: transcribe <audio-file> [locale]", code: 2) }
let audioPath = args[1]
let localeID = args.count >= 3 ? args[2] : "en-US"

guard #available(macOS 26.0, *) else { fail("requires macOS 26+") }

let sem = DispatchSemaphore(value: 0)
Task {
    do {
        let out = try await transcribe(path: audioPath, localeID: localeID)
        let data = try JSONEncoder().encode(out)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write("\n".data(using: .utf8)!)
        sem.signal()
    } catch {
        fail("transcription failed: \(error)")
    }
}
sem.wait()
