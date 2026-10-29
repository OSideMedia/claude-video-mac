// On-device speech-to-text CLI wrapping macOS 26's SpeechAnalyzer + SpeechTranscriber.
//
//   transcribe <audio-file> [locale]     transcribe; JSON on stdout
//   transcribe --locales                 JSON array of supported BCP-47 locales
//   transcribe --sounds <audio-file>     label the sounds; JSON on stdout
//
// Emits timestamped JSON on stdout (stdout carries ONLY the JSON; every
// diagnostic goes to stderr):
//   {"engine":"speechtranscriber","locale":"en-US",
//    "segments":[{"start":0.0,"end":1.2,"text":"...",
//                 "words":[{"start":0.0,"end":0.3,"text":"Get"}, ...]}],
//    "text":"full transcript"}
// and for --sounds, every second of audio, half a second apart:
//   {"engine":"soundanalysis","classifier":"version1",
//    "windows":[{"start":0.0,"end":1.0,"labels":{"speech":0.97,"piano":0.41}}, ...]}
//
// Exit codes:
//   0  success
//   2  usage / bad input (missing args, no such file)
//   3  speech model not available for the locale (unsupported, or the one-time
//      model download needs network and failed)
//   4  transcription (or sound analysis) failed
//
// Everything runs on-device. No API key, no network model call at inference.

import AVFoundation
import Foundation
import SoundAnalysis
import Speech

struct Word: Codable { let start: Double; let end: Double; var text: String }
struct Segment: Codable { let start: Double; let end: Double; let text: String; let words: [Word] }
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

/// The supported locale to run for ANY requested tag: the exact tag; else the
/// language's likely-region locale (Foundation likely subtags of the language
/// and script ALONE — so a bare 'en' and an unlisted 'en-PH' both land on en-US,
/// not the sorted list's en-AU, a model that may not be installed; zh-Hant-HK
/// lands on zh-TW); else the first supported locale with the same language.
/// nil when no supported locale shares the language.
func resolveLocale(_ requested: String, _ supported: [String]) -> String? {
    func find(_ tag: String) -> String? {
        supported.first { $0.caseInsensitiveCompare(tag) == .orderedSame }
    }
    if let exact = find(requested) { return exact }
    let language = Locale.Language(identifier: requested)
    guard let code = language.languageCode?.identifier.lowercased() else { return nil }
    // A script-bearing tag whose language+region IS listed (zh-Hant-HK -> zh-HK).
    if let region = language.region?.identifier, let sameRegion = find("\(code)-\(region)") {
        return sameRegion
    }
    // Maximise the language (+ script) WITHOUT the requested region: en-PH's own
    // maximal form keeps PH, which is exactly the region the list lacks.
    var base = code
    if let script = language.script?.identifier { base += "-\(script)" }
    if let region = Locale.Language(identifier: Locale.Language(identifier: base).maximalIdentifier)
        .region?.identifier,
       let likely = find("\(code)-\(region)") {
        return likely
    }
    return supported.first { $0.split(separator: "-").first.map { $0.lowercased() } == code }
}

/// One result's words, from its runs in order: each timed run is one word
/// with its punctuation. Text the recogniser left untimed joins the word
/// before it, or, before any word, the first timed word after it, so no
/// word of the text is lost; a space joins them only where the text had one.
func timedWords(_ runs: [(text: String, time: (start: Double, end: Double)?)]) -> [Word] {
    var words: [Word] = []
    var lead = ""     // untimed text before the first word
    var gap = false   // whitespace since the last text
    for run in runs {
        let piece = run.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if piece.isEmpty {
            gap = gap || !run.text.isEmpty
            continue
        }
        let sep = gap || run.text.first?.isWhitespace == true ? " " : ""
        if let t = run.time {
            words.append(Word(start: t.start, end: t.end, text: lead.isEmpty ? piece : lead + sep + piece))
            lead = ""
        } else if words.isEmpty {
            lead += (lead.isEmpty ? "" : sep) + piece
        } else {
            words[words.count - 1].text += sep + piece
        }
        gap = run.text.last?.isWhitespace == true
    }
    return words
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

    // Configure the transcriber to report audio time ranges: one per word.
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
            var runs: [(text: String, time: (start: Double, end: Double)?)] = []
            for run in attributed.runs {
                let text = String(attributed[run.range].characters)
                if let r = run.audioTimeRange {
                    if start == .invalid { start = r.start }
                    end = r.end
                    runs.append((text, (r.start.seconds, r.end.seconds)))
                } else {
                    runs.append((text, nil))
                }
            }
            let words = timedWords(runs)
            let s = start.isValid ? start.seconds : 0
            let e = end.isValid ? end.seconds : s
            collected.append(Segment(start: s, end: e,
                                     text: plain.trimmingCharacters(in: .whitespacesAndNewlines),
                                     words: words))
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

struct SoundWindow: Codable { let start: Double; let end: Double; let labels: [String: Double] }
struct SoundOutput: Codable {
    let engine: String
    let classifier: String
    let windows: [SoundWindow]
}

/// Keeps each window's labels at `floor` or more, to the hundredth. The
/// digest's own cut-off sits above it, so it can move without a new binary.
final class SoundObserver: NSObject, SNResultsObserving {
    static let floor = 0.3
    var windows: [SoundWindow] = []
    var failure: Error?

    func request(_ request: SNRequest, didProduce result: SNResult) {
        guard let r = result as? SNClassificationResult else { return }
        var labels: [String: Double] = [:]
        for c in r.classifications where c.confidence >= SoundObserver.floor {
            labels[c.identifier] = (c.confidence * 100).rounded() / 100
        }
        windows.append(SoundWindow(start: r.timeRange.start.seconds,
                                   end: CMTimeRangeGetEnd(r.timeRange).seconds, labels: labels))
    }

    func request(_ request: SNRequest, didFailWithError error: Error) { failure = error }
}

/// Apple's built-in sound classifier (303 kinds of sound, on-device) over the
/// whole file, one-second windows half a second apart. analyze() runs to the
/// end of the file and reports every window before it returns.
func classifySounds(path: String) throws -> SoundOutput {
    let request = try SNClassifySoundRequest(classifierIdentifier: .version1)
    request.windowDuration = CMTime(seconds: 1.0, preferredTimescale: 48000)
    request.overlapFactor = 0.5
    let analyzer = try SNAudioFileAnalyzer(url: URL(fileURLWithPath: path))
    let observer = SoundObserver()
    try analyzer.add(request, withObserver: observer)
    analyzer.analyze()
    if let failure = observer.failure { throw failure }
    return SoundOutput(engine: "soundanalysis", classifier: "version1", windows: observer.windows)
}

// --- entry point ---
let args = CommandLine.arguments
let usage = "usage: transcribe <audio-file> [locale]  |  transcribe --locales  |  transcribe --sounds <audio-file>"
guard args.count >= 2 else { fail(usage, code: .usage) }
if args[1] == "-h" || args[1] == "--help" {
    print(usage)
    exit(ExitCode.ok.rawValue)
}
if args[1] == "--sounds" {
    guard args.count >= 3 else { fail(usage, code: .usage) }
    guard FileManager.default.fileExists(atPath: args[2]) else { fail("no such file: \(args[2])", code: .usage) }
    do {
        try emitJSON(try classifySounds(path: args[2]))
    } catch {
        fail("sound analysis failed: \(error)", code: .failure)
    }
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
