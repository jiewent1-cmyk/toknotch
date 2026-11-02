import Foundation

/// One line of `fleet-agent.py` output: what a machine says it is doing.
struct FleetSnapshot: Decodable, Equatable {
    struct Host: Decodable, Equatable {
        var hostname: String
        var system: String
        var machine: String
        var cpus: Int
        var load: Double?
        /// Share of all cores in use, 0–100. Nil if this machine can't say.
        var cpuPercent: Double?
        /// CPU temperature in °C. Nil where no sensor could be read.
        var temperature: Double?
        /// The OS account running the agent. Absent from an older agent.
        var user: String?
    }

    var agentVersion: Int
    var host: Host
    var sessions: [FleetSession]
    var todayByModel: [String: FleetUsage]
    var workers: [FleetWorker]
    /// This machine's dispatcher and rules. Absent from an older agent.
    var dispatcher: FleetDispatcher?
    /// Who this machine's Claude tokens are billed to. Nil means not known.
    var claudeAccount: FleetClaudeAccount?
    /// Past days, sent only when they change. See `FleetUsageRecord`.
    var usageHistory: FleetUsageHistory?

    /// Today's tokens across every model.
    var todayTotal: Int64 { todayByModel.values.reduce(0) { $0 + $1.total } }

    /// Workers running on this machine. One sent elsewhere is counted by the
    /// machine doing the work, so a fleet-wide sum sees it once.
    var runningWorkers: [FleetWorker] {
        workers.filter { $0.state == .running && $0.remoteHost == nil }
    }

    /// Sessions that are not simply a worker listed above, which would show the
    /// same task twice.
    var ownSessions: [FleetSession] {
        let workerSessions = Set(workers.compactMap(\.sessionId))
        return sessions.filter { !workerSessions.contains($0.id) }
    }

    var activeSessions: [FleetSession] { ownSessions.filter(\.isActive) }
}

/// How worrying a CPU or temperature reading is.
enum FleetVitalLevel: Equatable {
    case normal, warm, hot

    static func cpu(_ percent: Double) -> FleetVitalLevel {
        percent >= 90 ? .hot : percent >= 70 ? .warm : .normal
    }

    /// Most of these CPUs start to throttle somewhere in the 80s and 90s.
    static func temperature(_ degrees: Double) -> FleetVitalLevel {
        degrees >= 90 ? .hot : degrees >= 75 ? .warm : .normal
    }
}

struct FleetUsage: Codable, Equatable {
    var input: Int64
    var output: Int64
    var cacheRead: Int64
    var cacheWrite: Int64

    var total: Int64 { input + output + cacheRead + cacheWrite }
}

struct FleetSession: Decodable, Equatable, Identifiable {
    enum Kind: String, Decodable { case interactive, worker }

    /// A session that wrote to its log this recently is treated as working.
    /// Claude Code writes on every step, and a long tool call can go quiet for
    /// a while, so this leans towards calling it active.
    static let activeWindow = 90

    var id: String
    var title: String
    var kind: Kind
    var model: String
    var cwd: String
    var lastTool: String
    var idleSeconds: Int
    var usage: FleetUsage

    var isActive: Bool { idleSeconds <= Self.activeWindow }
}

struct FleetWorker: Decodable, Equatable, Identifiable {
    enum State: String, Decodable {
        case running, done, failed, timeout, cancelled, lost
    }

    var id: String
    var provider: String
    var state: State
    var title: String
    var elapsedSeconds: Int
    var usage: FleetUsage?
    /// Set on the dispatching machine when the worker runs on another one.
    var remoteHost: String?
    /// Progress, from the worker's event stream. Absent on a worker started
    /// before streams, or one that has not said anything yet.
    var steps: Int?
    var lastTool: String?
    var lastFile: String?
    var model: String?
    var sessionId: String?
    var idleSeconds: Int?

    /// Long enough to mention, though a long-running command looks just like this.
    static let quietAfter = 300

    var isQuiet: Bool { state == .running && (idleSeconds ?? 0) >= Self.quietAfter }
    /// Only the machine running it can stop it; a dispatcher finds out from there.
    var isCancellableHere: Bool { state == .running && remoteHost == nil }
}

/// A machine or account to watch, from `~/.config/toknotch/fleet.json`.
struct FleetDevice: Codable, Equatable, Identifiable {
    var id: String
    var label: String
    /// SSH aliases, tried in order. Empty means this Mac.
    var ssh: [String]
    /// Read this account's data through the SSH account's passwordless sudo.
    var sudoUser: String?

    var isLocal: Bool { ssh.isEmpty }

    init(id: String, label: String, ssh: [String] = [], sudoUser: String? = nil) {
        self.id = id
        self.label = label
        self.ssh = ssh
        self.sudoUser = sudoUser
    }

    private enum CodingKeys: String, CodingKey { case id, label, ssh, sudoUser }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        label = try container.decodeIfPresent(String.self, forKey: .label) ?? id
        ssh = try container.decodeIfPresent([String].self, forKey: .ssh) ?? []
        sudoUser = try container.decodeIfPresent(String.self, forKey: .sudoUser)
    }
}

/// The device list. Machine names live in this file on the user's Mac and never
/// in the app, so the source can be public.
enum FleetConfig {
    private struct File: Codable { var devices: [FleetDevice] }

    static var path: String { NSHomeDirectory() + "/.config/toknotch/fleet.json" }
    static let thisMac = FleetDevice(id: "local", label: "This Mac")
    private static let limitsKey = "accountLimits"

    /// With no file, or an unreadable one, the fleet is just this Mac.
    static func load(from path: String = FleetConfig.path) -> [FleetDevice] {
        guard let data = FileManager.default.contents(atPath: path),
              let file = try? JSONDecoder().decode(File.self, from: data),
              !file.devices.isEmpty
        else { return [thisMac] }
        return file.devices
    }

    /// What each account allows, by provider. Read apart from `devices`, so a
    /// problem with one can't take the other with it.
    static func loadAccountLimits(from path: String = FleetConfig.path) -> [String: Int] {
        guard let data = FileManager.default.contents(atPath: path),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let limits = object[limitsKey] as? [String: Any]
        else { return [:] }
        return limits.compactMapValues { value in
            // A JSON `true` also arrives as an NSNumber, but it is no limit.
            guard let number = value as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.intValue > 0
            else { return nil }
            return number.intValue
        }
    }

    enum WriteError: Error, Equatable {
        /// The file exists but isn't a JSON object, so writing would destroy it.
        case unreadable
    }

    /// Sets or, with nil, removes one provider's limit, leaving `devices` and
    /// every other key as they are. Reads the file afresh, as it may have been
    /// edited by hand since the app last looked.
    static func setAccountLimit(_ limit: Int?, provider: String,
                                at path: String = FleetConfig.path) throws {
        // Write through a symlink rather than replacing it, for managed dotfiles.
        let url = URL(fileURLWithPath: path).resolvingSymlinksInPath()
        let manager = FileManager.default
        var object: [String: Any] = [:]
        if manager.fileExists(atPath: url.path) {
            let data = try Data(contentsOf: url)
            guard let existing = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            else { throw WriteError.unreadable }
            object = existing
        }
        var limits = object[limitsKey] as? [String: Any] ?? [:]
        limits[provider] = limit
        object[limitsKey] = limits.isEmpty ? nil : limits

        let data = try JSONSerialization.data(withJSONObject: object,
                                              options: [.prettyPrinted, .sortedKeys])
        let permissions = (try? manager.attributesOfItem(atPath: url.path))?[.posixPermissions]
        try manager.createDirectory(at: url.deletingLastPathComponent(),
                                    withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
        if let permissions {
            try? manager.setAttributes([.posixPermissions: permissions], ofItemAtPath: url.path)
        }
    }
}

extension FleetSnapshot {
    private enum CodingKeys: String, CodingKey {
        case agentVersion, host, sessions, todayByModel, workers, dispatcher
        case claudeAccount, usageHistory
    }

    /// A `dispatcher`, `claudeAccount` or `usageHistory` that can't be read is
    /// treated as absent: the rest of the report is still good, and losing a
    /// whole line would look like a dead link.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        agentVersion = try container.decode(Int.self, forKey: .agentVersion)
        host = try container.decode(Host.self, forKey: .host)
        sessions = try container.decode([FleetSession].self, forKey: .sessions)
        todayByModel = try container.decode([String: FleetUsage].self, forKey: .todayByModel)
        workers = try container.decode([FleetWorker].self, forKey: .workers)
        dispatcher = (try? container.decodeIfPresent(FleetDispatcher.self, forKey: .dispatcher)) ?? nil
        claudeAccount = (try? container.decodeIfPresent(FleetClaudeAccount.self, forKey: .claudeAccount)) ?? nil
        usageHistory = (try? container.decodeIfPresent(FleetUsageHistory.self, forKey: .usageHistory)) ?? nil
    }

    /// Decodes one output line. Nil for anything else ssh or a shell profile
    /// may have printed, which must not end the connection.
    static func decode(line: Data) -> FleetSnapshot? {
        try? JSONDecoder().decode(FleetSnapshot.self, from: line)
    }
}
