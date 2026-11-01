import Foundation

/// How a device's connection stands.
enum FleetLink: Equatable {
    case connecting
    /// Receiving reports through this SSH alias (nil for this Mac).
    case live(via: String?)
    /// Not reachable right now. Retrying on its own.
    case offline(reason: String)
}

/// Keeps one `fleet-agent.py` running for a device and passes on what it reports.
///
/// The agent is piped in through stdin, so remote machines need only Python.
/// `-I` keeps Python from importing anything from the remote home directory.
final class FleetConnection: @unchecked Sendable {
    /// Called on the main queue.
    var onLink: ((FleetLink) -> Void)?
    var onSnapshot: ((FleetSnapshot) -> Void)?

    /// A silent agent is a dead link: it reports at least every 10 seconds, so
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

    /// The command that runs the agent. Pure, so it can be tested.
    static func command(for device: FleetDevice, alias: String?) -> (path: String, arguments: [String]) {
        guard let alias else {
            return ("/usr/bin/env", ["python3", "-I", "-u", "-", "watch"])
        }
        var remote = "python3 -I -u - watch"
        if let user = device.sudoUser {
            remote = "sudo -n -u \(shellQuote(user)) -H \(remote)"
        }
        return ("/usr/bin/ssh", [
            "-T",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            alias, remote,
        ])
    }

    static func shellQuote(_ text: String) -> String {
        "'" + text.replacingOccurrences(of: "'", with: "'\\''") + "'"
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

        let input = Pipe()
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
