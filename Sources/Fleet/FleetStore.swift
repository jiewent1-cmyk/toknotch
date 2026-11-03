import Combine
import Foundation

/// A device with whatever is currently known about it.
struct FleetDeviceState: Identifiable, Equatable {
    var device: FleetDevice
    var link: FleetLink = .connecting
    /// The last report. Kept while offline, marked stale by `link`.
    var snapshot: FleetSnapshot?
    var updatedAt: Date?
    /// What the Usage page knows about this device's tokens. Outlives the
    /// snapshot, as it is cached on disk and most snapshots carry no history.
    var usage: FleetUsageRecord?

    var id: String { device.id }
    var isLive: Bool {
        if case .live = link { return true }
        return false
    }
}

/// Something to tell the user about a device's rules, until the next action.
enum FleetRulesNotice: Equatable {
    /// A save or reset was refused, and the machine's own rules are now showing.
    case conflict
    case saved
    case saveFailed(FleetCommandFailure)
    case installed
    case installFailed(FleetCommandFailure)
}

/// Publishes every device's state. Connections run only while something is
/// looking, so a closed window costs the remote machines nothing.
@MainActor
final class FleetStore: ObservableObject {
    @Published private(set) var devices: [FleetDeviceState]
    /// Workers asked to stop that still report as running, as "device/run".
    @Published private(set) var cancelling: Set<String> = []
    /// Why the last cancellation failed, until somebody has read it.
    @Published var cancelError: String?
    /// Rules being edited, by device id. Survives a closed window and a lost link.
    @Published private(set) var rulesEditors: [String: FleetRulesEditor] = [:]
    @Published private(set) var rulesNotices: [String: FleetRulesNotice] = [:]
    /// Devices whose dispatcher is being replaced.
    @Published private(set) var installing: Set<String> = []
    /// By device. Read when a device's rules are shown, not with every report.
    @Published private(set) var modelListings: [String: FleetModelListing] = [:]
    @Published private(set) var loadingModels: Set<String> = []
    @Published private(set) var modelListFailed: Set<String> = []
    /// By provider, from `fleet.json`.
    @Published private(set) var accountLimits: [String: Int]
    /// The last limit could not be written.
    @Published var accountLimitFailed = false
    private var connections: [String: FleetConnection] = [:]
    private var users = 0
    private let script: Data?
    private let dispatcherFile: Data?
    /// Nil for a fixture, which has no file to write to.
    private let configPath: String?
    private let isFixture: Bool
    /// Nil for a fixture, which keeps nothing.
    private let usageCachePath: String?
    /// What the cache file holds for each device, and when that was written.
    private var usageSaved: [String: FleetUsageRecord] = [:]
    private var usageSavedAt: [String: Date] = [:]

    init(devices: [FleetDevice] = FleetConfig.load(),
         script: Data? = FleetStore.bundledScript(),
         dispatcherFile: Data? = FleetStore.bundledDispatcher(),
         configPath: String = FleetConfig.path,
         usageCachePath: String = FleetUsageCache.path) {
        let cached = FleetUsageCache.load(from: usageCachePath)
        self.devices = devices.map { FleetDeviceState(device: $0, usage: cached[$0.id]) }
        self.script = script
        self.dispatcherFile = dispatcherFile
        self.configPath = configPath
        accountLimits = FleetConfig.loadAccountLimits(from: configPath)
        isFixture = false
        self.usageCachePath = usageCachePath
        usageSaved = cached
    }

    /// Already holds the given states and makes no connections. For previews and
    /// rendering checks. Giving a script and a file shows the controls as enabled.
    init(fixture: [FleetDeviceState], script: Data? = nil, dispatcherFile: Data? = nil,
         accountLimits: [String: Int] = [:]) {
        // A fixture that gives only snapshots still shows on the Usage page.
        devices = fixture.map { state in
            var state = state
            if state.usage == nil, let snapshot = state.snapshot {
                state.usage = FleetUsageRecord.updated(nil, with: snapshot, at: state.updatedAt ?? Date())
            }
            return state
        }
        self.script = script
        self.dispatcherFile = dispatcherFile
        configPath = nil
        self.accountLimits = accountLimits
        isFixture = true
        usageCachePath = nil
        for state in fixture {
            if let dispatcher = state.snapshot?.dispatcher { receive(dispatcher, for: state.id) }
        }
    }

    nonisolated static func bundledScript() -> Data? {
        Bundle.main.url(forResource: "fleet-agent", withExtension: "py")
            .flatMap { try? Data(contentsOf: $0) }
    }

    nonisolated static func bundledDispatcher() -> Data? {
        Bundle.main.url(forResource: "mm-worker", withExtension: "py")
            .flatMap { try? Data(contentsOf: $0) }
    }

    func start() {
        users += 1
        guard users == 1, !isFixture, let script else { return }
        for state in devices {
            let connection = FleetConnection(device: state.device, script: script)
            let id = state.id
            connection.onLink = { [weak self] link in
                MainActor.assumeIsolated { self?.update(id) { $0.link = link } }
            }
            connection.onSnapshot = { [weak self] snapshot in
                MainActor.assumeIsolated {
                    let now = Date()
                    self?.update(id) {
                        $0.snapshot = snapshot
                        $0.updatedAt = now
                        $0.usage = FleetUsageRecord.updated($0.usage, with: snapshot, at: now)
                    }
                    self?.saveUsage(now: now)
                    self?.settleCancelling(device: id, snapshot: snapshot)
                    if let dispatcher = snapshot.dispatcher { self?.receive(dispatcher, for: id) }
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
        // Whatever the last minute held, so a reopened window starts from it.
        saveUsage(now: Date(), closing: true)
    }

    // MARK: Usage cache

    /// Writes the records that are due. One not yet due keeps what the file
    /// last held for it, so no device is written more than once a minute.
    private func saveUsage(now: Date, closing: Bool = false) {
        guard let usageCachePath else { return }
        var records = usageSaved
        var written: [String] = []
        for state in devices {
            guard let record = state.usage, record != usageSaved[state.id] else { continue }
            if closing || FleetUsageCache.needsWrite(record, saved: usageSaved[state.id],
                                                     savedAt: usageSavedAt[state.id],
                                                     now: now, calendar: .current) {
                records[state.id] = record
                written.append(state.id)
            }
        }
        guard !written.isEmpty else { return }
        // A device taken out of fleet.json leaves the cache with it.
        let ids = Set(devices.map(\.id))
        records = records.filter { ids.contains($0.key) }
        do {
            try FleetUsageCache.save(records, to: usageCachePath)
        } catch {
            return
        }
        usageSaved = records
        for id in written { usageSavedAt[id] = now }
    }

    // MARK: Cancelling

    static func cancelKey(device: String, worker: String) -> String { "\(device)/\(worker)" }

    /// Whether this device's workers can be stopped from here: it has to be
    /// reachable, and an account read through someone else's sudo stays read-only.
    func canCancel(on state: FleetDeviceState) -> Bool {
        state.isLive && state.device.sudoUser == nil && script != nil
    }

    func cancel(_ worker: FleetWorker, on state: FleetDeviceState) {
        guard canCancel(on: state), worker.isCancellableHere, let script,
              case .live(let alias) = state.link
        else { return }
        let key = Self.cancelKey(device: state.id, worker: worker.id)
        cancelling.insert(key)
        let device = state.device
        Task {
            let failure = await FleetConnection.cancel(runID: worker.id, on: device,
                                                       alias: alias, script: script)
            if let failure {
                cancelling.remove(key)
                cancelError = failure
            }
        }
    }

    /// The agent reporting it as no longer running is what ends "cancelling".
    private func settleCancelling(device: String, snapshot: FleetSnapshot) {
        guard !cancelling.isEmpty else { return }
        let prefix = device + "/"
        let running = Set(snapshot.workers.filter { $0.state == .running }.map {
            Self.cancelKey(device: device, worker: $0.id)
        })
        for key in cancelling where key.hasPrefix(prefix) && !running.contains(key) {
            cancelling.remove(key)
        }
    }

    private func update(_ id: String, _ change: (inout FleetDeviceState) -> Void) {
        guard let index = devices.firstIndex(where: { $0.id == id }) else { return }
        change(&devices[index])
    }

    // MARK: Rules

    /// Rules can be changed only on a reachable machine that has reported them,
    /// and an account read through someone else's sudo stays read-only.
    func canEditRules(on state: FleetDeviceState) -> Bool {
        canCancel(on: state) && state.snapshot?.dispatcher != nil
    }

    /// Whether this build carries a dispatcher to install at all.
    var hasDispatcherFile: Bool { dispatcherFile?.isEmpty == false }

    func canInstall(on state: FleetDeviceState) -> Bool {
        state.isLive && state.device.sudoUser == nil && hasDispatcherFile
    }

    private func receive(_ dispatcher: FleetDispatcher, for id: String) {
        if rulesEditors[id] == nil {
            rulesEditors[id] = FleetRulesEditor(rules: dispatcher.rules, valid: dispatcher.rulesValid)
        } else {
            rulesEditors[id]?.receive(rules: dispatcher.rules, valid: dispatcher.rulesValid)
        }
    }

    /// Changes the unsaved rules. Ignored once a save is under way or if this
    /// device can't be edited, so a late click can't alter what is being sent.
    func editRules(on state: FleetDeviceState, _ change: (inout FleetRules) -> Void) {
        guard canEditRules(on: state), rulesEditors[state.id]?.isSaving == false else { return }
        change(&rulesEditors[state.id]!.draft)
        rulesNotices[state.id] = nil
    }

    func revertRules(on state: FleetDeviceState) {
        guard rulesEditors[state.id]?.isSaving == false else { return }
        rulesEditors[state.id]?.revert()
        rulesNotices[state.id] = nil
    }

    func saveRules(on state: FleetDeviceState) {
        guard canEditRules(on: state), let script, case .live(let alias) = state.link,
              rulesEditors[state.id]?.canSave == true,
              let submitted = rulesEditors[state.id]?.beginSave()
        else { return }
        let id = state.id, device = state.device
        rulesNotices[id] = nil
        Task {
            let answer = await FleetConnection.setRules(submitted, expectedRevision: submitted.revision,
                                                        on: device, alias: alias, script: script)
            switch answer {
            case .saved(let revision):
                rulesEditors[id]?.saved(submitted, revision: revision)
                rulesNotices[id] = .saved
            default:
                settle(answer, for: id)
            }
        }
    }

    /// Writes the defaults, dropping any unsaved edits. The view asks first.
    func resetRules(on state: FleetDeviceState) {
        guard canEditRules(on: state), let script, case .live(let alias) = state.link,
              let editor = rulesEditors[state.id], !editor.isSaving
        else { return }
        let id = state.id, device = state.device
        rulesEditors[id]?.isSaving = true
        rulesNotices[id] = nil
        Task {
            let answer = await FleetConnection.resetRules(expectedRevision: editor.expectedRevision,
                                                          on: device, alias: alias, script: script)
            switch answer {
            case .saved(let revision):
                rulesEditors[id]?.wasReset(revision: revision)
                rulesNotices[id] = .saved
            default:
                settle(answer, for: id)
            }
        }
    }

    private func settle(_ answer: FleetRulesAnswer, for id: String) {
        switch answer {
        case .conflict(let revision):
            rulesEditors[id]?.conflicted(fileRevision: revision)
            rulesNotices[id] = .conflict
        case .failed(let failure):
            rulesEditors[id]?.isSaving = false
            rulesNotices[id] = .saveFailed(failure)
        case .saved:
            break
        }
    }

    /// Replaces the machine's `mm-worker`. The view asks first. Only a clean exit
    /// counts as installed; the next report shows the version it found.
    func installDispatcher(on state: FleetDeviceState) {
        guard canInstall(on: state), let dispatcherFile, case .live(let alias) = state.link,
              !installing.contains(state.id)
        else { return }
        let id = state.id, device = state.device
        installing.insert(id)
        rulesNotices[id] = nil
        Task {
            let failure = await FleetConnection.install(dispatcherFile, on: device, alias: alias)
            installing.remove(id)
            rulesNotices[id] = failure.map(FleetRulesNotice.installFailed) ?? .installed
        }
    }

    // MARK: Model lists

    /// Whether this machine's `mm-worker` takes full model names.
    func acceptsFullModelNames(on state: FleetDeviceState) -> Bool {
        (state.snapshot?.dispatcher?.version ?? 0) >= FleetRules.fullNamesDispatcherVersion
    }

    /// Asks the machine for its providers' models. Once a session unless `again`.
    func loadModels(on state: FleetDeviceState, again: Bool = false) {
        guard canEditRules(on: state), acceptsFullModelNames(on: state), let script,
              case .live(let alias) = state.link, !loadingModels.contains(state.id),
              again || modelListings[state.id] == nil
        else { return }
        let id = state.id, device = state.device
        loadingModels.insert(id)
        modelListFailed.remove(id)
        Task {
            let listing = await FleetConnection.listModels(on: device, alias: alias, script: script)
            loadingModels.remove(id)
            if let listing {
                modelListings[id] = listing
            } else {
                modelListFailed.insert(id)
            }
        }
    }

    // MARK: Account limits

    func setAccountLimit(_ limit: Int?, provider: String) {
        guard accountLimits[provider] != limit else { return }
        if let configPath {
            do {
                try FleetConfig.setAccountLimit(limit, provider: provider, at: configPath)
            } catch {
                accountLimitFailed = true
                return
            }
        }
        accountLimits[provider] = limit
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
    var providerTotals: [FleetProviderTotal] {
        FleetProviderTotal.compute(devices: devices, limits: accountLimits)
    }
}
