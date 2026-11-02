import SwiftUI

/// The Fleet window: every machine on the left, the chosen one on the right.
struct FleetView: View {
    static let minWidth: CGFloat = 860
    static let minHeight: CGFloat = 560

    @ObservedObject var store: FleetStore
    @ObservedObject var preferences: Preferences
    /// A device id, `overview` or `usage`.
    @State var selection: String? = FleetView.overview

    private static let overview = "__overview__"
    static let usage = "__usage__"
    private var language: AppLanguage { preferences.appLanguage }

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 210, ideal: 230, max: 280)
        } detail: {
            ScrollView {
                detail
                    .padding(20)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
            }
        }
        .frame(minWidth: Self.minWidth, minHeight: Self.minHeight)
    }

    private var sidebar: some View {
        List(selection: $selection) {
            Label(language.t("fleet.overview"), systemImage: "square.grid.2x2")
                .tag(Self.overview)
            Label(language.t("fleet.usage.title"), systemImage: "chart.bar.xaxis")
                .tag(Self.usage)
            Section(language.t("fleet.devices")) {
                ForEach(store.devices) { state in
                    FleetDeviceRow(state: state, language: language)
                        .tag(state.id)
                }
            }
        }
        .listStyle(.sidebar)
    }

    @ViewBuilder
    private var detail: some View {
        if let state = store.devices.first(where: { $0.id == selection }) {
            FleetDeviceDetail(state: state, store: store, language: language)
        } else if selection == Self.usage {
            FleetUsageView(store: store, language: language) { selection = $0 }
        } else {
            FleetOverview(store: store, language: language) { selection = $0 }
        }
    }
}

// MARK: - Sidebar

private struct FleetDeviceRow: View {
    let state: FleetDeviceState
    let language: AppLanguage

    var body: some View {
        HStack(spacing: 8) {
            FleetLinkDot(link: state.link)
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: state.device.label)
                    .lineLimit(1)
                Text(verbatim: subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            if state.isLive, let host = state.snapshot?.host {
                VStack(alignment: .trailing, spacing: 1) {
                    FleetCPUText(percent: host.cpuPercent)
                    FleetTemperatureText(degrees: host.temperature)
                }
                .font(.caption.monospacedDigit())
            }
        }
    }

    private var subtitle: String {
        guard let snapshot = state.snapshot else {
            return language.t(state.isLive ? "fleet.link.live" : FleetText.linkKey(state.link))
        }
        return UsageFormat.tokens(snapshot.todayTotal, language) + " · "
            + language.t("fleet.activeCount", snapshot.activeSessions.count)
    }
}

private struct FleetLinkDot: View {
    let link: FleetLink

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 8, height: 8)
    }

    private var color: Color {
        switch link {
        case .live: return .green
        case .connecting: return .yellow
        case .offline: return .secondary
        }
    }
}

/// Live readings are never shown for a machine that has gone offline: an old
/// number there would look like a current one.
private struct FleetCPUText: View {
    let percent: Double?

    var body: some View {
        Text(verbatim: FleetText.percent(percent))
            .foregroundStyle(percent.map { FleetText.tint(FleetVitalLevel.cpu($0)) } ?? .secondary)
    }
}

private struct FleetTemperatureText: View {
    let degrees: Double?

    var body: some View {
        Text(verbatim: FleetText.degrees(degrees))
            .foregroundStyle(degrees.map { FleetText.tint(FleetVitalLevel.temperature($0)) } ?? .secondary)
    }
}

enum FleetText {
    static let unknown = "—"

    static func percent(_ value: Double?) -> String {
        // A big idle machine really does sit below 1%; "0%" would read as no data.
        value.map { $0 < 1 ? "<1%" : "\(Int($0.rounded()))%" } ?? unknown
    }

    static func degrees(_ value: Double?) -> String {
        value.map { "\(Int($0.rounded()))°C" } ?? unknown
    }

    static func tint(_ level: FleetVitalLevel) -> Color {
        switch level {
        case .normal: return .secondary
        case .warm: return .orange
        case .hot: return .red
        }
    }

    static func linkKey(_ link: FleetLink) -> String {
        switch link {
        case .live: return "fleet.link.live"
        case .connecting: return "fleet.link.connecting"
        case .offline: return "fleet.link.offline"
        }
    }

    /// `minimax · Running · Step 6 · Edit foo.swift`: what it is doing, as far as
    /// anyone can tell. There is no percentage, because the agent can't know one.
    static func workerDetail(_ worker: FleetWorker, language: AppLanguage) -> String {
        var parts = [worker.provider]
        if let model = worker.model, !model.isEmpty { parts.append(UsageFormat.modelName(model)) }
        parts.append(language.t("fleet.worker.\(worker.state.rawValue)"))
        if let host = worker.remoteHost { parts.append("→ \(host)") }
        if worker.state == .running {
            if let steps = worker.steps { parts.append(language.t("fleet.worker.step", steps)) }
            if let tool = worker.lastTool, !tool.isEmpty {
                let file = worker.lastFile ?? ""
                parts.append(file.isEmpty ? tool : "\(tool) \(file)")
            }
            if worker.isQuiet, let idle = worker.idleSeconds {
                parts.append(language.t("fleet.worker.quiet", duration(idle)))
            }
        }
        return parts.joined(separator: " · ")
    }

    /// `90s`, `14m`, `2h 5m`. Compact on purpose, as these sit in narrow columns.
    static func duration(_ seconds: Int) -> String {
        if seconds < 60 { return "\(max(0, seconds))s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        let minutes = (seconds % 3600) / 60
        return minutes == 0 ? "\(seconds / 3600)h" : "\(seconds / 3600)h \(minutes)m"
    }

    /// `~/src/toknotch` becomes `toknotch`: the last folder says which project.
    static func project(_ path: String) -> String {
        (path as NSString).lastPathComponent
    }
}

// MARK: - Overview

private struct FleetOverview: View {
    @ObservedObject var store: FleetStore
    let language: AppLanguage
    let onSelect: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text(language.t("fleet.overview"))
                .font(.title2.weight(.semibold))
            HStack(spacing: 12) {
                FleetStat(title: language.t("fleet.stat.online"),
                          value: "\(store.liveCount)/\(store.devices.count)")
                FleetStat(title: language.t("fleet.stat.activeSessions"),
                          value: "\(store.activeSessionCount)")
                FleetStat(title: language.t("fleet.stat.workers"),
                          value: "\(store.runningWorkerCount)")
                FleetStat(title: language.t("fleet.stat.today"),
                          value: UsageFormat.tokens(store.todayTotal, language))
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 250), spacing: 12)], spacing: 12) {
                ForEach(store.devices) { state in
                    Button { onSelect(state.id) } label: {
                        FleetDeviceCard(state: state, language: language)
                    }
                    .buttonStyle(.plain)
                }
            }
            FleetProviderTotalsSection(store: store, language: language)
        }
    }
}

private struct FleetDeviceCard: View {
    let state: FleetDeviceState
    let language: AppLanguage

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                FleetLinkDot(link: state.link)
                Text(verbatim: state.device.label)
                    .font(.headline)
                    .lineLimit(1)
                Spacer(minLength: 4)
                if state.isLive, let host = state.snapshot?.host {
                    HStack(spacing: 8) {
                        FleetCPUText(percent: host.cpuPercent)
                        FleetTemperatureText(degrees: host.temperature)
                    }
                    .font(.callout.monospacedDigit())
                }
            }
            if let snapshot = state.snapshot {
                HStack(alignment: .firstTextBaseline, spacing: 14) {
                    metric(UsageFormat.tokens(snapshot.todayTotal, language), "fleet.stat.today")
                    metric("\(snapshot.activeSessions.count)", "fleet.stat.activeSessions")
                    metric("\(snapshot.runningWorkers.count)", "fleet.stat.workers")
                }
            } else {
                Text(language.t(FleetText.linkKey(state.link)))
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10))
        .contentShape(RoundedRectangle(cornerRadius: 10))
        .opacity(state.isLive || state.snapshot == nil ? 1 : 0.65)
    }

    private func metric(_ value: String, _ key: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(verbatim: value)
                .font(.title3.weight(.medium).monospacedDigit())
            Text(language.t(key))
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }
}

private struct FleetStat: View {
    let title: String
    let value: String
    /// Colours the value when it is worth noticing.
    var level: FleetVitalLevel? = nil

    private var valueColor: Color {
        guard let level, level != .normal else { return .primary }
        return FleetText.tint(level)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(verbatim: title)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Text(verbatim: value)
                .font(.title2.weight(.semibold).monospacedDigit())
                .foregroundStyle(valueColor)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10))
    }
}

// MARK: - One device

private struct FleetDeviceDetail: View {
    let state: FleetDeviceState
    @ObservedObject var store: FleetStore
    let language: AppLanguage
    @State private var confirming: FleetWorker?

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            header
            if let snapshot = state.snapshot {
                HStack(spacing: 12) {
                    FleetStat(title: language.t("fleet.stat.cpu"),
                              value: FleetText.percent(state.isLive ? snapshot.host.cpuPercent : nil),
                              level: state.isLive ? snapshot.host.cpuPercent.map(FleetVitalLevel.cpu) : nil)
                    FleetStat(title: language.t("fleet.stat.temperature"),
                              value: FleetText.degrees(state.isLive ? snapshot.host.temperature : nil),
                              level: state.isLive ? snapshot.host.temperature.map(FleetVitalLevel.temperature) : nil)
                    FleetStat(title: language.t("fleet.stat.today"),
                              value: UsageFormat.tokens(snapshot.todayTotal, language))
                    FleetStat(title: language.t("fleet.stat.activeSessions"),
                              value: "\(snapshot.activeSessions.count)")
                    FleetStat(title: language.t("fleet.stat.workers"),
                              value: "\(snapshot.runningWorkers.count)")
                }
                models(snapshot)
                workers(snapshot)
                sessions(snapshot)
                FleetRulesSection(state: state, dispatcher: snapshot.dispatcher,
                                  store: store, language: language)
            }
        }
        // Stopping a task can't be undone and may leave its work half done.
        .alert(language.t("fleet.cancel.title"), isPresented: isConfirming, presenting: confirming) { worker in
            Button(language.t("fleet.cancel.confirm"), role: .destructive) {
                store.cancel(worker, on: state)
            }
            Button(language.t("fleet.cancel.keep"), role: .cancel) {}
        } message: { worker in
            Text(language.t("fleet.cancel.message", worker.title.isEmpty ? worker.id : worker.title))
        }
        .alert(language.t("fleet.cancel.failedTitle"), isPresented: hasCancelError) {
            Button(language.t("fleet.ok"), role: .cancel) {}
        } message: {
            Text(verbatim: store.cancelError ?? "")
        }
    }

    private var isConfirming: Binding<Bool> {
        Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } })
    }

    private var hasCancelError: Binding<Bool> {
        Binding(get: { store.cancelError != nil }, set: { if !$0 { store.cancelError = nil } })
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                FleetLinkDot(link: state.link)
                Text(verbatim: state.device.label)
                    .font(.title2.weight(.semibold))
            }
            Text(verbatim: headerDetail)
                .font(.callout)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
    }

    private var headerDetail: String {
        var parts: [String] = []
        if let host = state.snapshot?.host {
            parts.append("\(host.hostname) · \(host.system) \(host.machine)")
            if let load = host.load { parts.append(language.t("fleet.load") + " " + String(format: "%.1f", load)) }
        }
        switch state.link {
        case .live(let via):
            if let via { parts.append("ssh \(via)") }
        case .connecting:
            parts.append(language.t("fleet.link.connecting"))
        case .offline(let reason):
            parts.append(language.t("fleet.link.offline") + (reason.isEmpty ? "" : ": \(reason)"))
        }
        if !state.isLive, let updated = state.updatedAt {
            parts.append(language.t("fleet.lastSeen") + " "
                + updated.formatted(.relative(presentation: .named).locale(language.locale)))
        }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private func models(_ snapshot: FleetSnapshot) -> some View {
        let rows = snapshot.todayByModel.sorted { $0.value.total > $1.value.total }
        if !rows.isEmpty {
            FleetSection(title: language.t("fleet.section.models")) {
                ForEach(rows, id: \.key) { model, usage in
                    FleetModelRow(model: model, usage: usage,
                                  share: Double(usage.total) / Double(max(1, snapshot.todayTotal)),
                                  language: language)
                }
            }
        }
    }

    @ViewBuilder
    private func workers(_ snapshot: FleetSnapshot) -> some View {
        if !snapshot.workers.isEmpty {
            FleetSection(title: language.t("fleet.section.workers")) {
                ForEach(sortedWorkers(snapshot)) { worker in
                    FleetWorkerRow(
                        worker: worker,
                        language: language,
                        canCancel: store.canCancel(on: state) && worker.isCancellableHere,
                        isCancelling: store.cancelling.contains(
                            FleetStore.cancelKey(device: state.id, worker: worker.id)),
                        onCancel: { confirming = worker }
                    )
                }
            }
        }
    }

    private func sortedWorkers(_ snapshot: FleetSnapshot) -> [FleetWorker] {
        snapshot.workers.sorted {
            let (a, b) = ($0.state == .running, $1.state == .running)
            return a != b ? a : $0.elapsedSeconds < $1.elapsedSeconds
        }
    }

    @ViewBuilder
    private func sessions(_ snapshot: FleetSnapshot) -> some View {
        FleetSection(title: language.t("fleet.section.sessions")) {
            if snapshot.ownSessions.isEmpty {
                Text(language.t("fleet.noSessions"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 6)
            }
            ForEach(snapshot.ownSessions) { session in
                FleetSessionRow(session: session, language: language)
            }
        }
    }
}

struct FleetSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(verbatim: title)
                .font(.headline)
            VStack(spacing: 0) { content }
                .padding(.horizontal, 12)
                .padding(.vertical, 4)
                .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
        }
    }
}

private struct FleetModelRow: View {
    let model: String
    let usage: FleetUsage
    let share: Double
    let language: AppLanguage

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(Vendor.inferred(fromModelID: model).tint)
                .frame(width: 8, height: 8)
            Text(verbatim: model)
                .lineLimit(1)
                .frame(minWidth: 150, alignment: .leading)
            GeometryReader { proxy in
                Capsule()
                    .fill(Vendor.inferred(fromModelID: model).tint.opacity(0.7))
                    .frame(width: max(2, proxy.size.width * share), height: 5)
                    .frame(maxHeight: .infinity, alignment: .center)
            }
            .frame(height: 12)
            Text(verbatim: UsageFormat.tokens(usage.total, language))
                .font(.body.monospacedDigit())
                .frame(minWidth: 62, alignment: .trailing)
        }
        .padding(.vertical, 6)
        .help(language.t("fleet.usageDetail",
                         UsageFormat.tokens(usage.input, language),
                         UsageFormat.tokens(usage.output, language),
                         UsageFormat.tokens(usage.cacheRead, language),
                         UsageFormat.tokens(usage.cacheWrite, language)))
    }
}

private struct FleetWorkerRow: View {
    let worker: FleetWorker
    let language: AppLanguage
    var canCancel = false
    var isCancelling = false
    var onCancel: () -> Void = {}

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .foregroundStyle(tint)
                .frame(width: 16)
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: worker.title.isEmpty ? worker.id : worker.title)
                    .lineLimit(1)
                Text(verbatim: detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            if let usage = worker.usage {
                Text(verbatim: UsageFormat.tokens(usage.total, language))
                    .font(.body.monospacedDigit())
            }
            Text(verbatim: FleetText.duration(worker.elapsedSeconds))
                .font(.body.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(minWidth: 52, alignment: .trailing)
            if isCancelling {
                ProgressView()
                    .controlSize(.small)
                    .frame(width: 22)
                    .help(language.t("fleet.cancel.pending"))
            } else if canCancel {
                Button(action: onCancel) {
                    Image(systemName: "stop.circle")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .frame(width: 22)
                .help(language.t("fleet.cancel.help"))
            }
        }
        .padding(.vertical, 6)
        .opacity(worker.remoteHost == nil ? 1 : 0.65)
    }

    private var detail: String {
        FleetText.workerDetail(worker, language: language)
    }

    private var symbol: String {
        switch worker.state {
        case .running: return "play.circle.fill"
        case .done: return "checkmark.circle.fill"
        case .failed: return "xmark.circle.fill"
        case .timeout: return "clock.badge.exclamationmark"
        case .cancelled: return "minus.circle.fill"
        case .lost: return "questionmark.circle.fill"
        }
    }

    private var tint: Color {
        switch worker.state {
        case .running: return .blue
        case .done: return .green
        case .failed, .timeout: return .orange
        case .cancelled, .lost: return .secondary
        }
    }
}

private struct FleetSessionRow: View {
    let session: FleetSession
    let language: AppLanguage

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(session.isActive ? Color.green : Color.secondary.opacity(0.5))
                .frame(width: 7, height: 7)
                .frame(width: 16)
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: session.title.isEmpty ? FleetText.project(session.cwd) : session.title)
                    .lineLimit(1)
                Text(verbatim: detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            Text(verbatim: UsageFormat.tokens(session.usage.total, language))
                .font(.body.monospacedDigit())
            Text(verbatim: session.isActive ? language.t("fleet.session.active")
                : FleetText.duration(session.idleSeconds))
                .font(.body.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(minWidth: 52, alignment: .trailing)
        }
        .padding(.vertical, 6)
    }

    private var detail: String {
        var parts = [language.t("fleet.kind.\(session.kind.rawValue)")]
        if !session.cwd.isEmpty { parts.append(FleetText.project(session.cwd)) }
        if !session.model.isEmpty { parts.append(UsageFormat.modelName(session.model)) }
        if session.isActive, !session.lastTool.isEmpty { parts.append(session.lastTool) }
        return parts.joined(separator: " · ")
    }
}
