// On-device speech-to-text CLI wrapping macOS 26's SpeechAnalyzer + SpeechTranscriber.
//
//   transcribe <audio-file> [locale]     transcribe; JSON on stdout
//   transcribe --locales                 JSON array of supported BCP-47 locales
//
// Emits timestamped JSON on stdout (stdout carries ONLY the JSON; every
// diagnostic goes to stderr):
//   {"engine":"speechtranscriber","locale":"en-US",
//    "segments":[{"start":0.0,"end":1.2,"text":"..."}],
//    "text":"full transcript"}
//
// Exit codes:
//   0  success
//   2  usage / bad input (missing args, no such file)
//   3  speech model not available for the locale (unsupported, or the one-time
//      model download needs network and failed)
//   4  transcription failed
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

enum ExitCode: Int32 {
    case ok = 0
    case usage = 2
    case modelUnavailable = 3
    case failure = 4
}

enum TranscribeError: Error, CustomStringConvertible {
    case unsupportedLocale(String, [String])
    case modelUnavailable(String, String)

    var description: String {
        switch self {
        case .unsupportedLocale(let id, let supported):
            return "locale \(id) is not supported by SpeechTranscriber; supported: "
                + supported.joined(separator: ", ")
        case .modelUnavailable(let id, let why):
            return "speech model for \(id) is not installed and could not be downloaded "
                + "(the first use of a locale needs network once): \(why)"
        }
    }
}

func stderr(_ msg: String) {
    FileHandle.standardError.write(("[transcribe] " + msg + "\n").data(using: .utf8)!)
}

func fail(_ msg: String, code: ExitCode) -> Never {
    stderr(msg)
    exit(code.rawValue)
}

func emitJSON<T: Encodable>(_ value: T) throws {
    let data = try JSONEncoder().encode(value)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

@available(macOS 26.0, *)
func supportedLocaleIDs() async -> [String] {
    let locales = await SpeechTranscriber.supportedLocales
    return locales.map { $0.identifier(.bcp47) }.sorted()
}

/// The supported locale to run for a requested tag: the exact tag; else the
/// language with its likely region (en -> en-US, fr -> fr-FR, via Foundation's
/// likely subtags — so a bare 'en' lands on en-US rather than the sorted list's
/// en-AU, a model that may not be installed); else the first supported locale
/// with the same language. nil when no supported locale shares the language.
func resolveLocale(_ requested: String, _ supported: [String]) -> String? {
    func find(_ tag: String) -> String? {
        supported.first { $0.caseInsensitiveCompare(tag) == .orderedSame }
    }
    if let exact = find(requested) { return exact }
    let language = Locale.Language(identifier: requested)
    guard let code = language.languageCode?.identifier.lowercased() else { return nil }
    if let region = Locale.Language(identifier: language.maximalIdentifier).region?.identifier,
       let likely = find("\(code)-\(region)") {
        return likely
    }
    return supported.first { $0.split(separator: "-").first.map { $0.lowercased() } == code }
}

@available(macOS 26.0, *)
func transcribe(path: String, localeID: String) async throws -> Output {
    let url = URL(fileURLWithPath: path)
    let requested = Locale(identifier: localeID).identifier(.bcp47)

    // Fall back within the same language before giving up; fail early and
    // clearly (exit 3) only when the framework has no locale for the language.
    let supported = await supportedLocaleIDs()
    guard let wanted = resolveLocale(requested, supported) else {
        throw TranscribeError.unsupportedLocale(localeID, supported)
    }
    if wanted.caseInsensitiveCompare(requested) != .orderedSame {
        stderr("locale \(localeID) is not a supported locale as given; using \(wanted) (same language)")
    }
    let locale = Locale(identifier: wanted)

    // Configure the transcriber to report per-segment audio time ranges.
    let transcriber = SpeechTranscriber(
        locale: locale,
        transcriptionOptions: [],
        reportingOptions: [],
        attributeOptions: [.audioTimeRange]
    )

    // Ensure the on-device model for this locale is installed (one-time
    // download). Ask for the status first: assetInstallationRequest hands
    // back a request even when the assets are already present.
    let status = await AssetInventory.status(forModules: [transcriber])
    if status == .unsupported {
        throw TranscribeError.modelUnavailable(localeID, "AssetInventory reports the locale unsupported")
    }
    if status != .installed {
        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                stderr("installing speech model for \(wanted)… (one-time download, needs network)")
                try await request.downloadAndInstall()
            }
        } catch {
            throw TranscribeError.modelUnavailable(localeID, String(describing: error))
        }
    }

    let analyzer = SpeechAnalyzer(modules: [transcriber])

    // Collect results concurrently as the analyzer emits them. The segments are
    // RETURNED from the task rather than appended to a captured var — mutating
    // captured state from concurrently-executing code is a Swift 6 error.
    let collector = Task<[Segment], Error> {
        var collected: [Segment] = []
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
            collected.append(Segment(start: s, end: e,
                                     text: plain.trimmingCharacters(in: .whitespacesAndNewlines)))
        }
        return collected
    }

    // Feed the audio file straight into the analyzer.
    let audioFile = try AVAudioFile(forReading: url)
    if let last = try await analyzer.analyzeSequence(from: audioFile) {
        try await analyzer.finalizeAndFinish(through: last)
    } else {
        try await analyzer.finalizeAndFinishThroughEndOfInput()
    }

    let segments = try await collector.value.filter { !$0.text.isEmpty }
    let full = segments.map { $0.text }.joined(separator: " ")
    // Report the locale actually used, not the one requested.
    return Output(engine: "speechtranscriber", locale: wanted, segments: segments, text: full)
}

// --- entry point ---
let args = CommandLine.arguments
let usage = "usage: transcribe <audio-file> [locale]  |  transcribe --locales"
guard args.count >= 2 else { fail(usage, code: .usage) }
if args[1] == "-h" || args[1] == "--help" {
    print(usage)
    exit(ExitCode.ok.rawValue)
}
guard #available(macOS 26.0, *) else { fail("requires macOS 26+", code: .modelUnavailable) }

let listLocales = args[1] == "--locales"
let audioPath = args[1]
let localeID = args.count >= 3 ? args[2] : "en-US"
if !listLocales && !FileManager.default.fileExists(atPath: audioPath) {
    fail("no such file: \(audioPath)", code: .usage)
}

// Detached: top-level code is main-actor-isolated in Swift 6 mode, and the
// main thread blocks on the semaphore below, so the work must not inherit it.
let sem = DispatchSemaphore(value: 0)
Task.detached {
    do {
        if listLocales {
            try emitJSON(await supportedLocaleIDs())
        } else {
            try emitJSON(try await transcribe(path: audioPath, localeID: localeID))
        }
        sem.signal()
    } catch let e as TranscribeError {
        fail(e.description, code: .modelUnavailable)
    } catch {
        fail("transcription failed: \(error)", code: .failure)
    }
}
sem.wait()
exit(ExitCode.ok.rawValue)
