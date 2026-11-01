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
    }

    var agentVersion: Int
    var host: Host
    var sessions: [FleetSession]
    var todayByModel: [String: FleetUsage]
    var workers: [FleetWorker]

    /// Today's tokens across every model.
    var todayTotal: Int64 { todayByModel.values.reduce(0) { $0 + $1.total } }

    /// Workers running on this machine. One sent elsewhere is counted by the
    /// machine doing the work, so a fleet-wide sum sees it once.
    var runningWorkers: [FleetWorker] {
        workers.filter { $0.state == .running && $0.remoteHost == nil }
    }

    var activeSessions: [FleetSession] { sessions.filter(\.isActive) }
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

struct FleetUsage: Decodable, Equatable {
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

    /// With no file, or an unreadable one, the fleet is just this Mac.
    static func load(from path: String = FleetConfig.path) -> [FleetDevice] {
        guard let data = FileManager.default.contents(atPath: path),
              let file = try? JSONDecoder().decode(File.self, from: data),
              !file.devices.isEmpty
        else { return [thisMac] }
        return file.devices
    }
}

extension FleetSnapshot {
    /// Decodes one output line. Nil for anything else ssh or a shell profile
    /// may have printed, which must not end the connection.
    static func decode(line: Data) -> FleetSnapshot? {
        try? JSONDecoder().decode(FleetSnapshot.self, from: line)
    }
}
