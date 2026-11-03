import Foundation

/// How a device's connection stands.
enum FleetLink: Equatable {
    case connecting
    /// Receiving reports through this SSH alias (nil for this Mac).
    case live(via: String?)
    /// Not reachable right now. Retrying on its own.
    case offline(reason: String)
}

/// Why a command on a machine did not do what was asked. Text comes from the
/// view, in the user's language.
enum FleetCommandFailure: Equatable {
    case timedOut
    case noAnswer
    case exit(Int32, String)
    case launch(String)
    /// The agent said no, for this reason.
    case refused(String)
    case readOnly
    case inputIncomplete
    case unavailable
}

/// The models a machine's providers list through their APIs.
struct FleetModelListing: Equatable {
    var models: [String: [String]] = [:]
    /// Why a provider's list could not be read.
    var errors: [String: String] = [:]

    /// From the last line of the agent's answer that parses. Nil if none does.
    init?(output: Data) {
        struct Entry: Decodable { var models: [String]?; var error: String? }
        struct Answer: Decodable { var ok: Bool; var providers: [String: Entry]? }
        let answer = String(decoding: output, as: UTF8.self)
            .split(whereSeparator: \.isNewline)
            .reversed()
            .lazy
            .compactMap { try? JSONDecoder().decode(Answer.self, from: Data($0.utf8)) }
            .first
        guard let answer, answer.ok else { return nil }
        for (provider, entry) in answer.providers ?? [:] {
            if let list = entry.models { models[provider] = list }
            if let error = entry.error { errors[provider] = error }
        }
    }
}

/// What a machine answered to a change of rules.
enum FleetRulesAnswer: Equatable {
    case saved(revision: Int)
    /// The file has moved on. Its current revision, if the agent gave it.
    case conflict(revision: Int?)
    case failed(FleetCommandFailure)
}

/// Collects a command's result from the threads feeding and reading it.
private final class CommandState: @unchecked Sendable {
    private let lock = NSLock()
    private var value = FleetConnection.CommandResult()

    func update(_ change: (inout FleetConnection.CommandResult) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        change(&value)
    }

    var result: FleetConnection.CommandResult {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

/// Keeps one `fleet-agent.py` running for a device and passes on what it reports.
///
/// The agent is piped in through stdin, so remote machines need only Python.
/// `-I` keeps Python from importing anything from the remote home directory.
final class FleetConnection: @unchecked Sendable {
    /// Called on the main queue.
    var onLink: ((FleetLink) -> Void)?
    var onSnapshot: ((FleetSnapshot) -> Void)?

    /// A silent agent is a dead link: it reports at least every 5 seconds, so
    /// this long without a line means a connection that has quietly gone away.
    static let silenceLimit: TimeInterval = 35

    private let device: FleetDevice
    private let script: Data
    private let queue: DispatchQueue
    private var process: Process?
    private var stopped = false
    private var aliasIndex = 0
    private var failures = 0
    private var buffer = Data()
    private var lastLine = Date()
    private var gotLine = false
    private var watchdog: DispatchSourceTimer?
    private var errorTail = Data()

    init(device: FleetDevice, script: Data) {
        self.device = device
        self.script = script
        queue = DispatchQueue(label: "fleet.\(device.id)")
    }

    func start() {
        queue.async { [self] in
            stopped = false
            launch()
        }
    }

    func stop() {
        queue.async { [self] in
            stopped = true
            watchdog?.cancel()
            watchdog = nil
            process?.terminate()
            process = nil
        }
    }

    /// A pipe for feeding a child process. A child that dies early, as ssh does
    /// when a host is unreachable, must give a write error, not a SIGPIPE that takes
    /// the whole app with it. The agent is larger than a pipe holds, so the write is
    /// still in progress when ssh gives up.
    static func inputPipe() -> Pipe {
        let pipe = Pipe()
        _ = fcntl(pipe.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1)
        return pipe
    }

    /// The command that runs the agent. Pure, so it can be tested.
    static func command(for device: FleetDevice, alias: String?,
                        action: [String] = ["watch"]) -> (path: String, arguments: [String]) {
        guard let alias else {
            return ("/usr/bin/env", ["python3", "-I", "-u", "-"] + action)
        }
        var remote = (["python3 -I -u -"] + action.map { $0 == "watch" ? $0 : shellQuote($0) })
            .joined(separator: " ")
        if let user = device.sudoUser {
            remote = "sudo -n -u \(shellQuote(user)) -H \(remote)"
        }
        return ("/usr/bin/ssh", sshArguments(alias: alias, remote: remote))
    }

    private static func sshArguments(alias: String, remote: String) -> [String] {
        [
            "-T",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            alias, remote,
        ]
    }

    static func shellQuote(_ text: String) -> String {
        "'" + text.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// A run id as mm-worker writes it. Anything else never reaches a shell.
    static func isRunID(_ text: String) -> Bool {
        text.range(of: "^[0-9]+-[0-9]+-[0-9]+$", options: .regularExpression) != nil
    }

    /// What the agent answered, or why it could not be asked.
    static func cancelError(fromOutput output: Data) -> String? {
        struct Answer: Decodable { var ok: Bool; var error: String? }
        guard let line = String(decoding: output, as: UTF8.self)
                .split(whereSeparator: \.isNewline).last,
              let answer = try? JSONDecoder().decode(Answer.self, from: Data(line.utf8))
        else { return "No answer from the machine" }
        return answer.ok ? nil : (answer.error ?? "Failed")
    }

    /// Asks the machine to stop one worker. Nil means it did.
    static func cancel(runID: String, on device: FleetDevice, alias: String?,
                       script: Data) async -> String? {
        guard isRunID(runID) else { return "Bad run id" }
        return await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let (path, arguments) = command(for: device, alias: alias, action: ["cancel", runID])
                let process = Process()
                process.executableURL = URL(fileURLWithPath: path)
                process.arguments = arguments
                var environment = ProcessInfo.processInfo.environment
                environment["HOME"] = NSHomeDirectory()
                environment["PATH"] = TokscaleCLI.searchPath
                process.environment = environment
                let input = inputPipe(), output = Pipe()
                process.standardInput = input
                process.standardOutput = output
                process.standardError = FileHandle.nullDevice
                do {
                    try process.run()
                } catch {
                    continuation.resume(returning: error.localizedDescription)
                    return
                }
                try? input.fileHandleForWriting.write(contentsOf: script)
                try? input.fileHandleForWriting.close()
                let limit = DispatchWorkItem { process.terminate() }
                DispatchQueue.global().asyncAfter(deadline: .now() + 30, execute: limit)
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                limit.cancel()
                continuation.resume(returning: cancelError(fromOutput: data))
            }
        }
    }

    // MARK: Rules and install

    /// Rules go base64, which keeps them out of shell quoting.
    static func rulesSetAction(_ rules: FleetRules, expectedRevision: Int) throws -> [String] {
        ["rules-set", try rules.encoded().base64EncodedString(), String(expectedRevision)]
    }

    static func rulesResetAction(expectedRevision: Int) -> [String] {
        ["rules-reset", String(expectedRevision)]
    }

    /// From `docs/FLEET-RULES.md`. The rename means a running dispatcher keeps its
    /// old file. The size check is there because a dropped connection can look like
    /// a normal end of input to `cat`, which would install half a file.
    static func installShellCommand(byteCount: Int) -> String {
        let temp = "~/.local/bin/.mm-worker.tmp"
        return "mkdir -p ~/.local/bin && cat > \(temp)"
            + " && { [ \"$(wc -c < \(temp) | tr -d ' ')\" = \(byteCount) ] || { rm -f \(temp); exit 1; }; }"
            + " && chmod 755 \(temp) && mv \(temp) ~/.local/bin/mm-worker"
    }

    /// The command that installs the dispatcher from stdin. Nil for an account
    /// read through someone else's sudo, which stays read-only.
    static func installCommand(for device: FleetDevice, alias: String?,
                               byteCount: Int) -> (path: String, arguments: [String])? {
        guard device.sudoUser == nil else { return nil }
        let shell = installShellCommand(byteCount: byteCount)
        guard let alias else { return ("/bin/sh", ["-c", shell]) }
        return ("/usr/bin/ssh", sshArguments(alias: alias, remote: shell))
    }

    static let rulesTimeout: TimeInterval = 30
    /// Three providers at 10 seconds each, and the connection.
    static let modelsTimeout: TimeInterval = 45
    /// Roomy: stopping a transfer halfway is worse than waiting for a slow link.
    static let installTimeout: TimeInterval = 120

    /// How a command ended, and whether every byte of its input was taken.
    struct CommandResult: Equatable {
        var status: Int32 = -1
        var output = Data()
        var errors = Data()
        var timedOut = false
        var inputComplete = false
        var launchError: String?

        /// What went wrong, if the command did not run to a clean finish. A
        /// failed write counts: the other end may have finished with half a file.
        var failure: FleetCommandFailure? {
            if let launchError { return .launch(launchError) }
            if timedOut { return .timedOut }
            if status != 0 { return .exit(status, FleetConnection.reason(from: errors)) }
            if !inputComplete { return .inputIncomplete }
            return nil
        }
    }

    /// What `rules-set` or `rules-reset` came to.
    static func rulesAnswer(from result: CommandResult) -> FleetRulesAnswer {
        if let failure = result.failure { return .failed(failure) }
        struct Answer: Decodable { var ok: Bool; var revision: Int?; var error: String? }
        // Shell profiles print things, so the last line that parses is the answer.
        let answer = String(decoding: result.output, as: UTF8.self)
            .split(whereSeparator: \.isNewline)
            .reversed()
            .lazy
            .compactMap { try? JSONDecoder().decode(Answer.self, from: Data($0.utf8)) }
            .first
        guard let answer else { return .failed(.noAnswer) }
        if answer.ok {
            guard let revision = answer.revision else { return .failed(.noAnswer) }
            return .saved(revision: revision)
        }
        if answer.error == "conflict" { return .conflict(revision: answer.revision) }
        return .failed(.refused(answer.error ?? ""))
    }

    static func setRules(_ rules: FleetRules, expectedRevision: Int, on device: FleetDevice,
                         alias: String?, script: Data) async -> FleetRulesAnswer {
        guard device.sudoUser == nil else { return .failed(.readOnly) }
        guard let action = try? rulesSetAction(rules, expectedRevision: expectedRevision)
        else { return .failed(.noAnswer) }
        let (path, arguments) = command(for: device, alias: alias, action: action)
        return rulesAnswer(from: await run(path: path, arguments: arguments, input: script,
                                           timeout: rulesTimeout))
    }

    static func resetRules(expectedRevision: Int, on device: FleetDevice,
                           alias: String?, script: Data) async -> FleetRulesAnswer {
        guard device.sudoUser == nil else { return .failed(.readOnly) }
        let (path, arguments) = command(for: device, alias: alias,
                                        action: rulesResetAction(expectedRevision: expectedRevision))
        return rulesAnswer(from: await run(path: path, arguments: arguments, input: script,
                                           timeout: rulesTimeout))
    }

    /// Asks the machine what its providers offer. Nil if it could not be asked.
    static func listModels(on device: FleetDevice, alias: String?, script: Data) async -> FleetModelListing? {
        let (path, arguments) = command(for: device, alias: alias, action: ["models"])
        let result = await run(path: path, arguments: arguments, input: script, timeout: modelsTimeout)
        return result.failure == nil ? FleetModelListing(output: result.output) : nil
    }

    /// Replaces the machine's `mm-worker` with `file`. Nil means it did.
    static func install(_ file: Data, on device: FleetDevice, alias: String?) async -> FleetCommandFailure? {
        guard !file.isEmpty else { return .unavailable }
        guard let (path, arguments) = installCommand(for: device, alias: alias, byteCount: file.count)
        else { return .readOnly }
        return await run(path: path, arguments: arguments, input: file, timeout: installTimeout).failure
    }

    /// Runs one command to its end, feeding it `input`.
    ///
    /// The timeout starts before any input is written, as a stalled connection
    /// blocks the write once the pipe is full. Waits on the process, not on the
    /// pipes: an ssh ControlPersist master inherits them and can hold them open
    /// long after the command is over.
    static func run(path: String, arguments: [String], input: Data,
                    timeout: TimeInterval) async -> CommandResult {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: path)
                process.arguments = arguments
                var environment = ProcessInfo.processInfo.environment
                environment["HOME"] = NSHomeDirectory()
                environment["PATH"] = TokscaleCLI.searchPath
                process.environment = environment
                let stdin = inputPipe(), stdout = Pipe(), stderr = Pipe()
                process.standardInput = stdin
                process.standardOutput = stdout
                process.standardError = stderr

                let state = CommandState()
                do {
                    try process.run()
                } catch {
                    var result = CommandResult()
                    result.launchError = error.localizedDescription
                    continuation.resume(returning: result)
                    return
                }
                let limit = DispatchWorkItem {
                    state.update { $0.timedOut = true }
                    process.terminate()
                }
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: limit)

                let group = DispatchGroup()
                DispatchQueue.global(qos: .userInitiated).async(group: group) {
                    let written = (try? stdin.fileHandleForWriting.write(contentsOf: input)) != nil
                    try? stdin.fileHandleForWriting.close()
                    state.update { $0.inputComplete = written }
                }
                DispatchQueue.global(qos: .userInitiated).async(group: group) {
                    let data = stdout.fileHandleForReading.readDataToEndOfFile()
                    state.update { $0.output = data }
                }
                DispatchQueue.global(qos: .userInitiated).async(group: group) {
                    let data = stderr.fileHandleForReading.readDataToEndOfFile()
                    state.update { $0.errors = data.suffix(2000) }
                }
                process.waitUntilExit()
                limit.cancel()
                _ = group.wait(timeout: .now() + 2)
                state.update { $0.status = process.terminationStatus }
                continuation.resume(returning: state.result)
            }
        }
    }

    /// Doubling from 5 seconds, capped at a minute.
    static func retryDelay(afterFailures failures: Int) -> TimeInterval {
        min(60, 5 * pow(2, Double(max(0, failures - 1))))
    }

    private var currentAlias: String? {
        device.ssh.isEmpty ? nil : device.ssh[aliasIndex % device.ssh.count]
    }

    private func launch() {
        guard !stopped else { return }
        let alias = currentAlias
        let (path, arguments) = Self.command(for: device, alias: alias)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        var environment = ProcessInfo.processInfo.environment
        environment["HOME"] = NSHomeDirectory()
        // An app launched from Finder has a bare PATH, without Homebrew's python3.
        environment["PATH"] = TokscaleCLI.searchPath
        process.environment = environment

        let input = Self.inputPipe()
        let output = Pipe()
        let errors = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors

        buffer = Data()
        errorTail = Data()
        gotLine = false
        lastLine = Date()

        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            self?.queue.async { self?.receive(data, alias: alias) }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            self?.queue.async {
                guard let self else { return }
                self.errorTail.append(data)
                self.errorTail = self.errorTail.suffix(2000)
            }
        }
        process.terminationHandler = { [weak self] finished in
            self?.queue.async { self?.ended(finished) }
        }

        publish(link: .connecting)
        do {
            try process.run()
        } catch {
            ended(nil)
            return
        }
        self.process = process
        armWatchdog(for: process)

        // Python runs what it read from stdin once stdin closes.
        let script = script
        DispatchQueue.global(qos: .utility).async {
            try? input.fileHandleForWriting.write(contentsOf: script)
            try? input.fileHandleForWriting.close()
        }
    }

    private func receive(_ data: Data, alias: String?) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<newline]
            buffer = Data(buffer[buffer.index(after: newline)...])
            guard let snapshot = FleetSnapshot.decode(line: Data(line)) else { continue }
            lastLine = Date()
            if !gotLine {
                gotLine = true
                failures = 0
                publish(link: .live(via: alias))
            }
            DispatchQueue.main.async { [weak self] in self?.onSnapshot?(snapshot) }
        }
    }

    private func armWatchdog(for process: Process) {
        watchdog?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 10, repeating: 10)
        timer.setEventHandler { [weak self, weak process] in
            guard let self, let process, process.isRunning else { return }
            if Date().timeIntervalSince(self.lastLine) > Self.silenceLimit {
                process.terminate()
            }
        }
        timer.resume()
        watchdog = timer
    }

    private func ended(_ finished: Process?) {
        // A stale handler from a process already replaced has nothing to say.
        if let finished, finished !== process { return }
        process = nil
        watchdog?.cancel()
        watchdog = nil
        guard !stopped else { return }

        failures += 1
        if !device.ssh.isEmpty { aliasIndex += 1 }
        let reason = Self.reason(from: errorTail)
        publish(link: .offline(reason: reason))
        // Go straight to the next alias once, so a machine that has only left the
        // LAN comes back over NetBird right away rather than after a backoff.
        let triedAll = device.ssh.isEmpty || failures % device.ssh.count == 0
        let delay = triedAll ? Self.retryDelay(afterFailures: failures / max(1, device.ssh.count)) : 0
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in self?.launch() }
    }

    /// The last line ssh or Python wrote, without ssh's routine warnings.
    static func reason(from errors: Data) -> String {
        String(decoding: errors, as: UTF8.self)
            .split(whereSeparator: \.isNewline)
            .map(String.init)
            .last { !$0.hasPrefix("Warning: Permanently added") } ?? ""
    }

    private func publish(link: FleetLink) {
        DispatchQueue.main.async { [weak self] in self?.onLink?(link) }
    }
}
