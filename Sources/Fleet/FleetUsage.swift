import Foundation

// MARK: - What the agent sends

/// Who a machine's Claude tokens are billed to, from `oauthAccount` in
/// `~/.claude.json`. Nil in a snapshot means not known, never "not logged in":
/// a machine can log in with a token instead.
struct FleetClaudeAccount: Codable, Equatable {
    var email: String
    var plan: String?
    var organization: String?
}

/// The 29 local days before today on one machine, by day and then model. Sent
/// only when it changes, and each one replaces the last in full.
struct FleetUsageHistory: Codable, Equatable {
    /// False while the first scan runs, when `days` holds what is counted so far.
    var complete: Bool
    /// 0 to 1, by bytes.
    var progress: Double
    /// Keyed `yyyy-MM-dd`, in the machine's own time zone.
    var days: [String: [String: FleetUsage]]
}

extension FleetUsage {
    static let zero = FleetUsage(input: 0, output: 0, cacheRead: 0, cacheWrite: 0)

    static func + (lhs: FleetUsage, rhs: FleetUsage) -> FleetUsage {
        FleetUsage(input: lhs.input + rhs.input, output: lhs.output + rhs.output,
                   cacheRead: lhs.cacheRead + rhs.cacheRead, cacheWrite: lhs.cacheWrite + rhs.cacheWrite)
    }

    static func += (lhs: inout FleetUsage, rhs: FleetUsage) {
        lhs = lhs + rhs
    }
}

// MARK: - Days

/// Days as the agent writes them, `yyyy-MM-dd`.
enum FleetUsageDay {
    static func key(_ date: Date, calendar: Calendar) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04ld-%02ld-%02ld", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    /// `count` days, oldest first, ending `ago` days before the day of `now`.
    static func keys(_ count: Int, endingDaysAgo ago: Int = 0, now: Date, calendar: Calendar) -> [String] {
        let today = calendar.startOfDay(for: now)
        return (0..<count).reversed().compactMap { back in
            calendar.date(byAdding: .day, value: -(back + ago), to: today)
                .map { FleetUsageDay.key($0, calendar: calendar) }
        }
    }

    static func date(_ key: String, calendar: Calendar) -> Date? {
        let parts = key.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2]))
    }
}

// MARK: - What the app keeps

/// Everything the Usage page needs from one device, kept apart from the
/// snapshot because most snapshots carry no history, and cached on disk so an
/// offline machine still counts.
struct FleetUsageRecord: Codable, Equatable {
    var hostname: String
    var user: String?
    var claudeAccount: FleetClaudeAccount?
    /// The machine's own today, as of `reportedAt`.
    var todayByModel: [String: FleetUsage]
    /// When the last report arrived here.
    var reportedAt: Date
    var history: FleetUsageHistory?

    /// The record after one more report. Most carry no history, so the last
    /// one stays, unless the device now points at other logs.
    static func updated(_ old: FleetUsageRecord?, with snapshot: FleetSnapshot,
                        at date: Date) -> FleetUsageRecord {
        let sameLogs = old?.hostname == snapshot.host.hostname && old?.user == snapshot.host.user
        let known = sameLogs ? old?.history : nil
        var history = snapshot.usageHistory ?? known
        // The agent scans again on every connection. Until it finishes, what it
        // has counted is less than what is already known, and the totals would
        // fall and climb back each time the window opens.
        if let incoming = snapshot.usageHistory, !incoming.complete, let known, !known.days.isEmpty {
            history = FleetUsageHistory(complete: false, progress: incoming.progress, days: known.days)
        }
        return FleetUsageRecord(hostname: snapshot.host.hostname,
                                user: snapshot.host.user,
                                claudeAccount: snapshot.claudeAccount,
                                todayByModel: snapshot.todayByModel,
                                reportedAt: date,
                                history: history)
    }

    /// Tokens by day and model.
    ///
    /// History days are the machine's own local days, taken as they are: a
    /// machine in another time zone files a message under its own date, and
    /// moving it would need the machine's offset, which isn't reported.
    ///
    /// `todayByModel` belongs to the day it arrived here, which is today only
    /// if it arrived today. A device last heard from on Tuesday has Tuesday's
    /// tokens in it, and they stay on Tuesday. They never overlap the history,
    /// which holds only days before the machine's today, so where a time zone
    /// puts both on one date they are added.
    func days(calendar: Calendar) -> [String: [String: FleetUsage]] {
        var days = history?.days ?? [:]
        let day = FleetUsageDay.key(reportedAt, calendar: calendar)
        for (model, usage) in todayByModel {
            days[day, default: [:]][model, default: .zero] += usage
        }
        return days
    }
}

/// `~/Library/Application Support/TokNotch/fleet-usage.json`. Connections run
/// only while the window is open, so without this the page would open empty
/// and lose a machine as soon as it went offline.
enum FleetUsageCache {
    static var path: String {
        NSHomeDirectory() + "/Library/Application Support/TokNotch/fleet-usage.json"
    }

    /// The least time between two writes of one device's record.
    static let minimumInterval: TimeInterval = 60
    /// A later report time alone is worth writing only this much later, so an
    /// idle machine doesn't rewrite the file every minute just to say it's there.
    static let lastSeenStep: TimeInterval = 300
    private static let version = 1

    private struct File: Codable {
        var version: Int
        var devices: [String: FleetUsageRecord]
    }

    /// By device id. A missing or unreadable file is no cache at all.
    static func load(from path: String) -> [String: FleetUsageRecord] {
        guard let data = FileManager.default.contents(atPath: path),
              let file = try? JSONDecoder().decode(File.self, from: data),
              file.version == version
        else { return [:] }
        return file.devices
    }

    static func save(_ records: [String: FleetUsageRecord], to path: String) throws {
        let url = URL(fileURLWithPath: path)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(File(version: version, devices: records)).write(to: url, options: .atomic)
    }

    /// Whether one device's record should be written now: changed, and not
    /// within a minute of its last write.
    static func needsWrite(_ record: FleetUsageRecord, saved: FleetUsageRecord?, savedAt: Date?,
                           now: Date, calendar: Calendar) -> Bool {
        guard let saved else { return true }
        if let savedAt, now.timeIntervalSince(savedAt) < minimumInterval { return false }
        var content = record
        content.reportedAt = saved.reportedAt
        if content != saved { return true }
        // A new day matters: it decides which day `todayByModel` is counted on.
        return record.reportedAt.timeIntervalSince(saved.reportedAt) >= lastSeenStep
            || FleetUsageDay.key(record.reportedAt, calendar: calendar)
                != FleetUsageDay.key(saved.reportedAt, calendar: calendar)
    }
}

// MARK: - What the page asks for

enum FleetUsageRange: String, CaseIterable, Identifiable {
    case today, week, month

    var id: String { rawValue }

    var dayCount: Int {
        switch self {
        case .today: return 1
        case .week: return 7
        case .month: return FleetUsageReport.windowDays
        }
    }

    /// 30 days is all the history there is, so there is nothing earlier to compare with.
    var comparesWithPrevious: Bool { self != .month }
}

enum FleetUsageGrouping: String, CaseIterable, Identifiable {
    case machines, accounts, models

    var id: String { rawValue }
}

enum FleetUsageMetric: String, CaseIterable, Identifiable {
    case all, withoutCacheRead

    var id: String { rawValue }

    func tokens(_ usage: FleetUsage) -> Int64 {
        switch self {
        case .all: return usage.total
        case .withoutCacheRead: return usage.total - usage.cacheRead
        }
    }
}

/// A series colour. It follows the entity, never its rank, so nothing is
/// repainted when the range changes and the order with it.
enum FleetUsageColor: Hashable {
    /// A place in the categorical palette.
    case slot(Int)
    case vendor(Vendor)
    case other
}

// MARK: - What the page shows

struct FleetUsageRow: Identifiable, Equatable {
    enum Name: Equatable {
        case text(String)
        case vendor(Vendor)
        case unknownClaudeAccount
        case other
    }

    enum Detail: Equatable {
        case machine(users: [String])
        case device(user: String?)
        case claudeAccount(plan: String?, machines: Int)
        /// The one device it was seen on.
        case unknownClaudeAccount(device: String)
        case vendorAccount(machines: Int)
        case vendor(models: Int)
        case model
    }

    var id: String
    var name: Name
    var color: FleetUsageColor
    var detail: Detail
    /// The devices behind this row, in `fleet.json` order.
    var deviceIDs: [String]
    /// In the range, all four kinds whatever the metric.
    var usage: FleetUsage
    /// In the range, by the metric.
    var tokens: Int64
    /// Of the page's total.
    var share: Double
    /// The last 30 days by the metric, oldest first, whatever the range.
    var trend: [Int64]
    var children: [FleetUsageRow]
}

struct FleetUsageSeries: Identifiable, Equatable {
    var id: String
    var name: FleetUsageRow.Name
    var color: FleetUsageColor
    /// One per day of the range, oldest first.
    var values: [Int64]
    var total: Int64
}

/// One device whose tokens are counted: the first in `fleet.json` for its logs.
struct FleetUsageEntry {
    var state: FleetDeviceState
    var record: FleetUsageRecord
    var days: [String: [String: FleetUsage]]

    func usage(on keys: [String]) -> FleetUsage {
        keys.reduce(FleetUsage.zero) { sum, key in
            sum + (days[key]?.values.reduce(FleetUsage.zero, +) ?? .zero)
        }
    }

    /// Two devices with the same hostname and user are the same logs listed
    /// twice, so only the first counts. Devices that have never reported come
    /// back by label.
    static func entries(_ devices: [FleetDeviceState],
                        calendar: Calendar) -> (counted: [FleetUsageEntry], withoutData: [String]) {
        var counted: [FleetUsageEntry] = []
        var withoutData: [String] = []
        var seen = Set<String>()
        for state in devices {
            guard let record = state.usage else {
                withoutData.append(state.device.label)
                continue
            }
            guard seen.insert(record.hostname + "\n" + (record.user ?? "")).inserted else { continue }
            counted.append(FleetUsageEntry(state: state, record: record, days: record.days(calendar: calendar)))
        }
        return (counted, withoutData)
    }
}

struct FleetUsageReport: Equatable {
    static let windowDays = 30
    /// Series of their own in the chart. The rest are folded into one.
    static let seriesLimit = 7

    var range: FleetUsageRange
    var grouping: FleetUsageGrouping
    var metric: FleetUsageMetric
    /// The days of the range, oldest first, in this Mac's calendar.
    var dayKeys: [String]
    var total: Int64
    /// The range's tokens of each kind, whatever the metric.
    var parts: FleetUsage
    var rows: [FleetUsageRow]
    var series: [FleetUsageSeries]
    /// Against the period before, as a fraction. Nil where there's nothing to
    /// compare against or the comparison would be false.
    var change: Double?
    /// Live devices whose first scan is running, by label.
    var scanning: [String]
    /// Their average progress, 0 to 1.
    var scanProgress: Double
    /// Devices that report but have sent no history: only their today counts.
    var withoutHistory: [String]
    /// Devices that have never reported.
    var withoutData: [String]
    /// Devices with tokens in either period but no report today, so not in `change`.
    var leftOutOfChange: [String]
    /// Anything at all in the last 30 days.
    var hasData: Bool
}

extension FleetUsageReport {
    static func compute(devices: [FleetDeviceState], range: FleetUsageRange,
                        grouping: FleetUsageGrouping, metric: FleetUsageMetric,
                        now: Date = Date(), calendar: Calendar = .current) -> FleetUsageReport {
        let (entries, withoutData) = FleetUsageEntry.entries(devices, calendar: calendar)
        let window = FleetUsageDay.keys(windowDays, now: now, calendar: calendar)
        let rangeKeys = Array(window.suffix(range.dayCount))
        let previousKeys = range.comparesWithPrevious
            ? FleetUsageDay.keys(range.dayCount, endingDaysAgo: range.dayCount, now: now, calendar: calendar)
            : []

        let buckets = UsageBucket.fill(entries, grouping: grouping, window: Set(window))
        let colors = colorsByID(buckets, grouping: grouping)
        let parts = entries.reduce(FleetUsage.zero) { $0 + $1.usage(on: rangeKeys) }
        let total = metric.tokens(parts)

        let builder = RowBuilder(entries: entries, window: window, rangeKeys: rangeKeys,
                                 metric: metric, total: total)
        let rows = buckets
            .compactMap { bucket in builder.row(bucket, color: colors[bucket.place.id] ?? .other) }
            .sorted(by: FleetUsageRow.ordered)

        var report = FleetUsageReport(
            range: range, grouping: grouping, metric: metric, dayKeys: rangeKeys,
            total: total, parts: parts, rows: rows,
            series: series(rows, days: rangeKeys.count),
            change: nil, scanning: [], scanProgress: 0, withoutHistory: [],
            withoutData: withoutData, leftOutOfChange: [],
            hasData: entries.contains { metric.tokens($0.usage(on: window)) > 0 })
        report.settleChange(entries, rangeKeys: rangeKeys, previousKeys: previousKeys,
                            today: window.last ?? "", calendar: calendar)
        return report
    }

    /// The change, and which devices it can't speak for.
    ///
    /// A device counts only if it reported today with a finished history.
    /// Without history its past is missing rather than zero; with an old report
    /// its recent days are. While any live device is still scanning, there is
    /// no change at all: its earlier days would look like a fall.
    private mutating func settleChange(_ entries: [FleetUsageEntry], rangeKeys: [String],
                                       previousKeys: [String], today: String, calendar: Calendar) {
        var progress: [Double] = []
        var current: Int64 = 0, previous: Int64 = 0, counted = 0
        for entry in entries {
            let label = entry.state.device.label
            guard let history = entry.record.history else {
                withoutHistory.append(label)
                continue
            }
            if !history.complete && entry.state.isLive {
                scanning.append(label)
                progress.append(min(1, max(0, history.progress)))
                continue
            }
            let isCurrent = FleetUsageDay.key(entry.record.reportedAt, calendar: calendar) == today
            guard isCurrent && history.complete else {
                if metric.tokens(entry.usage(on: rangeKeys + previousKeys)) > 0 {
                    leftOutOfChange.append(label)
                }
                continue
            }
            current += metric.tokens(entry.usage(on: rangeKeys))
            previous += metric.tokens(entry.usage(on: previousKeys))
            counted += 1
        }
        scanProgress = progress.isEmpty ? 0 : progress.reduce(0, +) / Double(progress.count)
        if range.comparesWithPrevious, scanning.isEmpty, counted > 0, previous > 0 {
            change = Double(current - previous) / Double(previous)
        } else {
            leftOutOfChange = []
        }
    }

    /// Slots in `fleet.json` order: the first device with a machine or a Claude
    /// account decides its place, and other vendors follow in a fixed order.
    /// Taken from the whole 30 days at all tokens, so neither the range nor the
    /// metric can move them. Past eight entities two can share a colour.
    private static func colorsByID(_ buckets: [UsageBucket],
                                   grouping: FleetUsageGrouping) -> [String: FleetUsageColor] {
        var colors: [String: FleetUsageColor] = [:]
        if grouping == .models {
            for bucket in buckets {
                if case .vendor(let vendor) = bucket.place.name { colors[bucket.place.id] = .vendor(vendor) }
            }
            return colors
        }
        func rank(_ index: Int, _ bucket: UsageBucket) -> (Int, Int) {
            if case .vendor(let vendor) = bucket.place.name {
                return (1, Vendor.allCases.firstIndex(of: vendor) ?? 0)
            }
            return (0, index)
        }
        let ordered = buckets.enumerated().sorted { rank($0.offset, $0.element) < rank($1.offset, $1.element) }
        for (slot, item) in ordered.enumerated() {
            colors[item.element.place.id] = .slot(slot)
        }
        return colors
    }

    private static func series(_ rows: [FleetUsageRow], days: Int) -> [FleetUsageSeries] {
        func values(_ row: FleetUsageRow) -> [Int64] { Array(row.trend.suffix(days)) }
        let folds = rows.count > seriesLimit
        var series = rows.prefix(folds ? seriesLimit : rows.count).map { row in
            FleetUsageSeries(id: row.id, name: row.name, color: row.color,
                             values: values(row), total: row.tokens)
        }
        if folds {
            let rest = rows.dropFirst(seriesLimit)
            var summed = Array(repeating: Int64(0), count: days)
            for row in rest {
                for (index, value) in values(row).enumerated() where index < days { summed[index] += value }
            }
            series.append(FleetUsageSeries(id: "other", name: .other, color: .other, values: summed,
                                           total: rest.reduce(0) { $0 + $1.tokens }))
        }
        return series
    }
}

extension FleetUsageRow {
    /// Most first; ties by id so the order doesn't flicker between reports.
    static func ordered(_ a: FleetUsageRow, _ b: FleetUsageRow) -> Bool {
        a.tokens != b.tokens ? a.tokens > b.tokens : a.id < b.id
    }
}

// MARK: - Grouping

private struct UsagePlace {
    enum Kind { case machine, device, claudeAccount, unknownClaudeAccount, vendorAccount, vendor, model }

    var id: String
    var name: FleetUsageRow.Name
    var kind: Kind
    /// The machine, for a machine; the device, for a device or unknown account.
    var hostname: String? = nil
    var deviceID: String? = nil
}

/// One row's tokens by day, with its children.
private final class UsageBucket {
    let place: UsagePlace
    private(set) var byDay: [String: FleetUsage] = [:]
    private(set) var deviceIDs: [String] = []
    private(set) var children: [UsageBucket] = []
    private var childrenByID: [String: UsageBucket] = [:]

    init(_ place: UsagePlace) { self.place = place }

    func add(_ usage: FleetUsage, day: String, device: String) {
        byDay[day, default: .zero] += usage
        if !deviceIDs.contains(device) { deviceIDs.append(device) }
    }

    func child(_ place: UsagePlace) -> UsageBucket {
        if let found = childrenByID[place.id] { return found }
        let made = UsageBucket(place)
        childrenByID[place.id] = made
        children.append(made)
        return made
    }

    func sum(_ keys: [String]) -> FleetUsage {
        keys.reduce(FleetUsage.zero) { $0 + (byDay[$1] ?? .zero) }
    }

    /// Top-level buckets in the order they were first seen, which follows
    /// `fleet.json` because the entries do.
    static func fill(_ entries: [FleetUsageEntry], grouping: FleetUsageGrouping,
                     window: Set<String>) -> [UsageBucket] {
        var byID: [String: UsageBucket] = [:]
        var order: [UsageBucket] = []
        let onMachine = Dictionary(grouping: entries, by: { $0.record.hostname })

        for entry in entries {
            let hostname = entry.record.hostname
            let shared = (onMachine[hostname]?.count ?? 0) > 1
            // A machine with several accounts goes by the first one's label,
            // which is the name the user gave it. The accounts go by their users.
            let machine = UsagePlace(id: "machine:" + hostname,
                                     name: .text(onMachine[hostname]?.first?.state.device.label ?? hostname),
                                     kind: .machine, hostname: hostname)
            for (day, models) in entry.days where window.contains(day) {
                for (model, usage) in models {
                    let (top, child) = places(grouping, entry: entry, model: model,
                                              machine: machine, shared: shared)
                    let target: UsageBucket
                    if let found = byID[top.id] {
                        target = found
                    } else {
                        target = UsageBucket(top)
                        byID[top.id] = target
                        order.append(target)
                    }
                    target.add(usage, day: day, device: entry.state.id)
                    if let child { target.child(child).add(usage, day: day, device: entry.state.id) }
                }
            }
        }
        return order
    }

    /// Where one model's tokens on one device go, by the rules in
    /// `docs/FLEET-USAGE.md`.
    private static func places(_ grouping: FleetUsageGrouping, entry: FleetUsageEntry, model: String,
                               machine: UsagePlace, shared: Bool) -> (UsagePlace, UsagePlace?) {
        let vendor = Vendor.inferred(fromModelID: model)
        switch grouping {
        case .machines:
            let device = UsagePlace(id: "device:" + entry.state.id,
                                    name: .text(entry.record.user ?? entry.state.device.label),
                                    kind: .device, deviceID: entry.state.id)
            return (machine, shared ? device : nil)
        case .accounts:
            guard vendor == .anthropic else {
                return (UsagePlace(id: "vendor:" + vendor.rawValue, name: .vendor(vendor),
                                   kind: .vendorAccount), machine)
            }
            if let email = entry.record.claudeAccount?.email, !email.isEmpty {
                return (UsagePlace(id: "claude:" + email.lowercased(), name: .text(email),
                                   kind: .claudeAccount), machine)
            }
            return (UsagePlace(id: "unknown:" + entry.state.id, name: .unknownClaudeAccount,
                               kind: .unknownClaudeAccount, deviceID: entry.state.id), nil)
        case .models:
            return (UsagePlace(id: "vendor:" + vendor.rawValue, name: .vendor(vendor), kind: .vendor),
                    UsagePlace(id: "model:" + model, name: .text(UsageFormat.modelName(model)), kind: .model))
        }
    }
}

/// Turns buckets into rows for one range and metric.
private struct RowBuilder {
    let entries: [FleetUsageEntry]
    let window: [String]
    let rangeKeys: [String]
    let metric: FleetUsageMetric
    let total: Int64

    /// Nil when there is nothing in the range.
    func row(_ bucket: UsageBucket, color: FleetUsageColor) -> FleetUsageRow? {
        let usage = bucket.sum(rangeKeys)
        let tokens = metric.tokens(usage)
        guard tokens > 0 else { return nil }
        var children = bucket.children
            .compactMap { row($0, color: color) }
            .sorted(by: FleetUsageRow.ordered)
        let detail = self.detail(bucket, children: children.count)
        // A subscription or vendor seen on one machine has nothing to unfold.
        if bucket.place.kind == .claudeAccount || bucket.place.kind == .vendorAccount, children.count < 2 {
            children = []
        }
        return FleetUsageRow(id: bucket.place.id, name: bucket.place.name, color: color, detail: detail,
                             deviceIDs: bucket.deviceIDs, usage: usage, tokens: tokens,
                             share: Double(tokens) / Double(max(1, total)),
                             trend: window.map { metric.tokens(bucket.byDay[$0] ?? .zero) },
                             children: children)
    }

    private func detail(_ bucket: UsageBucket, children: Int) -> FleetUsageRow.Detail {
        let place = bucket.place
        switch place.kind {
        case .machine:
            var users: [String] = []
            for entry in entries where entry.record.hostname == place.hostname {
                if let user = entry.record.user, !user.isEmpty, !users.contains(user) { users.append(user) }
            }
            return .machine(users: users)
        case .device:
            // The row is already named after the user.
            return .device(user: nil)
        case .claudeAccount:
            let plan = bucket.deviceIDs.compactMap { entry($0)?.record.claudeAccount?.plan }.first
            return .claudeAccount(plan: plan, machines: children)
        case .unknownClaudeAccount:
            return .unknownClaudeAccount(device: entry(place.deviceID)?.state.device.label ?? "")
        case .vendorAccount:
            return .vendorAccount(machines: children)
        case .vendor:
            return .vendor(models: children)
        case .model:
            return .model
        }
    }

    private func entry(_ id: String?) -> FleetUsageEntry? {
        entries.first { $0.state.id == id }
    }
}
