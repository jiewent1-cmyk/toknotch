import Foundation

/// Pulls usage from other machines onto this one, so tokscale counts it too.
///
/// The work itself is a script the user keeps at `~/bin/tokscale-remote-sync`:
/// it slims each remote host's session logs over ssh and rsyncs them into a
/// folder tokscale already scans through `scanner.extraScanPaths`. Which hosts,
/// which keys and where the files land are all the script's business — this
/// only runs it and reads back one line per host:
///
///     2026-10-29 13:44:39 ROG: ok (9.9M)
///     2026-10-29 13:44:42 pi4: 连不上 pi4，跳过
///
/// A Mac without the script never sees the button.
enum RemoteSync {
    struct Outcome: Equatable {
        var succeeded: [String] = []
        var failed: [String] = []
        var total: Int { succeeded.count + failed.count }
    }

    static var script: String { NSHomeDirectory() + "/bin/tokscale-remote-sync" }
    /// Shared with the LaunchAgent that runs the same script every half hour,
    /// so both kinds of run end up in one history.
    static var logFile: String { NSHomeDirectory() + "/.local/share/tokscale-remote/sync.log" }

    static var isAvailable: Bool { FileManager.default.isExecutableFile(atPath: script) }

    /// When any sync last finished, by hand or on the timer.
    static var lastRun: Date? {
        (try? FileManager.default.attributesOfItem(atPath: logFile))?[.modificationDate] as? Date
    }

    /// A first sync of a busy host copies hundreds of megabytes; later ones are
    /// incremental and take seconds.
    static func run(timeout: TimeInterval = 20 * 60) async throws -> Outcome {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: script)
                var environment = ProcessInfo.processInfo.environment
                environment["HOME"] = NSHomeDirectory()
                environment["PATH"] = TokscaleCLI.searchPath
                process.environment = environment

                let output = Pipe()
                process.standardOutput = output
                process.standardError = FileHandle.nullDevice

                do {
                    try process.run()
                } catch {
                    continuation.resume(throwing: TokscaleCLI.Failure.notInstalled)
                    return
                }
                var timedOut = false
                let deadline = DispatchWorkItem {
                    if process.isRunning {
                        timedOut = true
                        process.terminate()
                    }
                }
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout,
                                                               execute: deadline)
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                deadline.cancel()
                append(data)

                guard !timedOut else {
                    continuation.resume(throwing: TokscaleCLI.Failure.timedOut)
                    return
                }
                guard process.terminationStatus == 0 else {
                    continuation.resume(throwing: TokscaleCLI.Failure.exited(code: process.terminationStatus))
                    return
                }
                continuation.resume(returning: parse(String(decoding: data, as: UTF8.self)))
            }
        }
    }

    /// `<date> <time> <host>: ok (...)` is a success; any other host line is not.
    static func parse(_ text: String) -> Outcome {
        var outcome = Outcome()
        for line in text.split(separator: "\n") {
            let words = line.split(separator: " ", maxSplits: 3)
            guard words.count == 4, words[2].hasSuffix(":") else { continue }
            let host = String(words[2].dropLast())
            if words[3].hasPrefix("ok") {
                outcome.succeeded.append(host)
            } else {
                outcome.failed.append(host)
            }
        }
        return outcome
    }

    private static func append(_ data: Data) {
        guard !data.isEmpty else { return }
        if let handle = FileHandle(forWritingAtPath: logFile) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            FileManager.default.createFile(atPath: logFile, contents: data)
        }
    }
}
