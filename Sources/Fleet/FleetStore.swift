import Combine
import Foundation

/// A device with whatever is currently known about it.
struct FleetDeviceState: Identifiable, Equatable {
    var device: FleetDevice
    var link: FleetLink = .connecting
    /// The last report. Kept while offline, marked stale by `link`.
    var snapshot: FleetSnapshot?
    var updatedAt: Date?

    var id: String { device.id }
    var isLive: Bool {
        if case .live = link { return true }
        return false
    }
}

/// Publishes every device's state. Connections run only while something is
/// looking, so a closed window costs the remote machines nothing.
@MainActor
final class FleetStore: ObservableObject {
    @Published private(set) var devices: [FleetDeviceState]
    private var connections: [String: FleetConnection] = [:]
    private var users = 0
    private let script: Data?

    init(devices: [FleetDevice] = FleetConfig.load(),
         script: Data? = FleetStore.bundledScript()) {
        self.devices = devices.map { FleetDeviceState(device: $0) }
        self.script = script
    }

    /// Already holds the given states and makes no connections. For previews and
    /// rendering checks.
    init(fixture: [FleetDeviceState]) {
        devices = fixture
        script = nil
    }

    nonisolated static func bundledScript() -> Data? {
        Bundle.main.url(forResource: "fleet-agent", withExtension: "py")
            .flatMap { try? Data(contentsOf: $0) }
    }

    func start() {
        users += 1
        guard users == 1, let script else { return }
        for state in devices {
            let connection = FleetConnection(device: state.device, script: script)
            let id = state.id
            connection.onLink = { [weak self] link in
                MainActor.assumeIsolated { self?.update(id) { $0.link = link } }
            }
            connection.onSnapshot = { [weak self] snapshot in
                MainActor.assumeIsolated {
                    self?.update(id) {
                        $0.snapshot = snapshot
                        $0.updatedAt = Date()
                    }
                }
            }
            connections[id] = connection
            connection.start()
        }
    }

    func stop() {
        users = max(0, users - 1)
        guard users == 0 else { return }
        connections.values.forEach { $0.stop() }
        connections.removeAll()
    }

    private func update(_ id: String, _ change: (inout FleetDeviceState) -> Void) {
        guard let index = devices.firstIndex(where: { $0.id == id }) else { return }
        change(&devices[index])
    }

    // MARK: Totals

    var liveCount: Int { devices.filter(\.isLive).count }
    var todayTotal: Int64 { devices.reduce(0) { $0 + ($1.snapshot?.todayTotal ?? 0) } }
    var activeSessionCount: Int {
        devices.reduce(0) { $0 + ($1.snapshot?.activeSessions.count ?? 0) }
    }
    var runningWorkerCount: Int {
        devices.reduce(0) { $0 + ($1.snapshot?.runningWorkers.count ?? 0) }
    }
}
