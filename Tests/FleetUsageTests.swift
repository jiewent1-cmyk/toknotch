import XCTest
@testable import TokNotch

final class FleetUsageTests: XCTestCase {
    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        return calendar
    }()

    private var now: Date {
        calendar.date(from: DateComponents(year: 2026, month: 11, day: 1, hour: 12))!
    }

    private func daysAgo(_ days: Int) -> Date {
        calendar.date(byAdding: .day, value: -days, to: now)!
    }

    private func key(_ days: Int) -> String {
        FleetUsageDay.key(daysAgo(days), calendar: calendar)
    }

    private func tokens(_ input: Int64, cacheRead: Int64 = 0) -> FleetUsage {
        FleetUsage(input: input, output: 0, cacheRead: cacheRead, cacheWrite: 0)
    }

    /// A device as the store would hold it. `past` is by days ago; nil means no
    /// history has arrived.
    private func device(_ id: String, host: String, user: String? = "me", email: String? = nil,
                        today: [String: FleetUsage] = [:], past: [Int: [String: FleetUsage]]? = [:],
                        complete: Bool = true, reportedDaysAgo: Int = 0,
                        link: FleetLink = .live(via: nil)) -> FleetDeviceState {
        var days: [String: [String: FleetUsage]] = [:]
        for (ago, models) in past ?? [:] { days[key(ago)] = models }
        let record = FleetUsageRecord(
            hostname: host, user: user,
            claudeAccount: email.map { FleetClaudeAccount(email: $0, plan: "Claude Max", organization: nil) },
            todayByModel: today, reportedAt: daysAgo(reportedDaysAgo),
            history: past == nil ? nil : FleetUsageHistory(complete: complete, progress: complete ? 1 : 0.4,
                                                          days: days))
        return FleetDeviceState(device: FleetDevice(id: id, label: id, ssh: [id]), link: link, usage: record)
    }

    private func report(_ devices: [FleetDeviceState], _ range: FleetUsageRange = .week,
                        _ grouping: FleetUsageGrouping = .machines,
                        _ metric: FleetUsageMetric = .all) -> FleetUsageReport {
        FleetUsageReport.compute(devices: devices, range: range, grouping: grouping, metric: metric,
                                 now: now, calendar: calendar)
    }

    /// Two accounts on one machine, a Claude subscription shared with a second
    /// machine, a third machine with no known email, and other vendors.
    private var fleet: [FleetDeviceState] {
        [device("studio", host: "studio", user: "me", email: "me@example.com",
                today: ["claude-opus-5-5": tokens(100, cacheRead: 900)],
                past: [1: ["claude-opus-5-5": tokens(80)], 9: ["claude-sonnet-5-5": tokens(40)],
                       20: ["claude-opus-5-5": tokens(500)]]),
         device("studio-ci", host: "studio", user: "ci",
                today: ["MiniMax-M2.7": tokens(60)],
                past: [2: ["glm-5.2": tokens(30), "MiniMax-M2.7": tokens(10)]]),
         device("gpu", host: "gpu-box", email: "ME@example.com",
                today: ["claude-sonnet-5-5": tokens(50), "MiniMax-M2.7": tokens(25)],
                past: [3: ["claude-sonnet-5-5": tokens(70, cacheRead: 300)]]),
         device("air", host: "air", today: ["claude-haiku-5-5": tokens(5)], past: [4: ["claude-haiku-5-5": tokens(15)]])]
    }

    private func sum(_ rows: [FleetUsageRow]) -> Int64 { rows.reduce(0) { $0 + $1.tokens } }

    // MARK: Decoding

    private let oldLine = """
    {"agentVersion":4,"host":{"hostname":"box","system":"Linux","machine":"x86_64","cpus":16},\
    "sessions":[],"todayByModel":{"m":{"input":1,"output":2,"cacheRead":3,"cacheWrite":4}},"workers":[]}
    """

    private func line(host: String = "\"user\":\"ci\"", extra: String) -> Data {
        Data("""
        {"agentVersion":5,"host":{"hostname":"box","system":"Linux","machine":"x86_64","cpus":16,\(host)},\
        "sessions":[],"todayByModel":{},"workers":[]\(extra)}
        """.utf8)
    }

    func testAnOlderAgentStillDecodes() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(oldLine.utf8)))
        XCTAssertNil(snapshot.host.user)
        XCTAssertNil(snapshot.claudeAccount)
        XCTAssertNil(snapshot.usageHistory)
        XCTAssertEqual(snapshot.todayTotal, 10)
    }

    func testDecodesAccountAndHistory() throws {
        let extra = """
        ,"claudeAccount":{"email":"a@example.com","plan":"Claude Max","organization":null},\
        "usageHistory":{"complete":false,"progress":0.25,\
        "days":{"2026-10-31":{"claude-opus-5-5":{"input":1,"output":2,"cacheRead":3,"cacheWrite":4}}}}
        """
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: line(extra: extra)))
        XCTAssertEqual(snapshot.host.user, "ci")
        XCTAssertEqual(snapshot.claudeAccount, FleetClaudeAccount(email: "a@example.com", plan: "Claude Max",
                                                                  organization: nil))
        let history = try XCTUnwrap(snapshot.usageHistory)
        XCTAssertFalse(history.complete)
        XCTAssertEqual(history.progress, 0.25)
        XCTAssertEqual(history.days["2026-10-31"]?["claude-opus-5-5"]?.total, 10)
    }

    /// `null` means not known; anything unreadable is treated the same, and the report survives.
    func testAnUnreadableAccountOrHistoryDoesNotLoseTheReport() throws {
        for extra in [",\"claudeAccount\":null,\"usageHistory\":null",
                      ",\"claudeAccount\":{\"plan\":\"Claude Max\"},\"usageHistory\":\"nope\"",
                      ",\"claudeAccount\":[],\"usageHistory\":{\"days\":[]}"] {
            let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: line(extra: extra)), extra)
            XCTAssertNil(snapshot.claudeAccount, extra)
            XCTAssertNil(snapshot.usageHistory, extra)
            XCTAssertEqual(snapshot.host.hostname, "box")
        }
    }

    // MARK: Grouping

    func testTotalsAgreeAcrossViews() {
        for range in FleetUsageRange.allCases {
            for metric in FleetUsageMetric.allCases {
                let totals = FleetUsageGrouping.allCases.map { report(fleet, range, $0, metric).total }
                XCTAssertEqual(Set(totals).count, 1, "\(range) \(metric): \(totals)")
                for grouping in FleetUsageGrouping.allCases {
                    let result = report(fleet, range, grouping, metric)
                    XCTAssertEqual(sum(result.rows), result.total, "\(range) \(grouping) \(metric)")
                    for row in result.rows where !row.children.isEmpty {
                        XCTAssertEqual(sum(row.children), row.tokens, "\(row.id) in \(grouping)")
                    }
                }
            }
        }
        // Today: 1000 + 60 + 75 + 5. A week adds days 1 to 4: 80 + 40 + 370 + 15.
        XCTAssertEqual(report(fleet, .today).total, 1140)
        XCTAssertEqual(report(fleet, .week).total, 1645)
        XCTAssertEqual(report(fleet, .month).total, 2185)
    }

    func testMachinesHaveTheirAccountsAsChildren() throws {
        let rows = report(fleet, .week, .machines).rows
        XCTAssertEqual(rows.map(\.id), ["machine:studio", "machine:gpu-box", "machine:air"])
        let studio = try XCTUnwrap(rows.first)
        XCTAssertEqual(studio.name, .text("studio"))
        XCTAssertEqual(studio.detail, .machine(users: ["me", "ci"]))
        XCTAssertEqual(studio.deviceIDs, ["studio", "studio-ci"])
        XCTAssertEqual(studio.children.map(\.id), ["device:studio", "device:studio-ci"])
        XCTAssertEqual(studio.children.map(\.name), [.text("me"), .text("ci")])
        XCTAssertEqual(studio.children.last?.detail, .device(user: nil))
        // A machine with one account goes by its label and has nothing to unfold.
        XCTAssertEqual(rows[1].name, .text("gpu"))
        XCTAssertTrue(rows[1].children.isEmpty)
    }

    func testTheSameLogsListedTwiceCountOnce() {
        let original = device("a", host: "box", user: "me", today: ["glm-5.2": tokens(10)])
        let again = device("b", host: "box", user: "me", today: ["glm-5.2": tokens(10)])
        let other = device("c", host: "box", user: "ci", today: ["glm-5.2": tokens(5)])
        XCTAssertEqual(report([original, again], .today).total, 10)
        let rows = report([original, again, other], .today).rows
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].tokens, 15)
        XCTAssertEqual(rows[0].children.map(\.id), ["device:a", "device:c"])
    }

    func testAccountsFollowWhoPays() throws {
        let rows = report(fleet, .week, .accounts).rows
        // Emails are matched without case; one subscription on two machines is one row.
        let shared = try XCTUnwrap(rows.first { $0.id == "claude:me@example.com" })
        XCTAssertEqual(shared.tokens, 1000 + 80 + 50 + 370)
        XCTAssertEqual(shared.detail, .claudeAccount(plan: "Claude Max", machines: 2))
        XCTAssertEqual(shared.children.map(\.id), ["machine:studio", "machine:gpu-box"])

        let unknown = try XCTUnwrap(rows.first { $0.id == "unknown:air" })
        XCTAssertEqual(unknown.name, .unknownClaudeAccount)
        XCTAssertEqual(unknown.detail, .unknownClaudeAccount(device: "air"))
        XCTAssertEqual(unknown.tokens, 20)
        XCTAssertTrue(unknown.children.isEmpty)

        // Other vendors' keys can't be told apart, so each vendor is one row.
        let minimax = try XCTUnwrap(rows.first { $0.id == "vendor:minimax" })
        XCTAssertEqual(minimax.name, .vendor(.minimax))
        XCTAssertEqual(minimax.tokens, 60 + 10 + 25)
        XCTAssertEqual(minimax.detail, .vendorAccount(machines: 2))
        let zhipu = try XCTUnwrap(rows.first { $0.id == "vendor:zhipu" })
        XCTAssertEqual(zhipu.tokens, 30)
        XCTAssertTrue(zhipu.children.isEmpty, "one machine has nothing to unfold")
        XCTAssertEqual(rows.count, 4)
    }

    func testModelsByVendor() throws {
        let rows = report(fleet, .week, .models).rows
        let anthropic = try XCTUnwrap(rows.first { $0.id == "vendor:anthropic" })
        XCTAssertEqual(anthropic.color, .vendor(.anthropic))
        XCTAssertEqual(anthropic.detail, .vendor(models: 3))
        XCTAssertEqual(anthropic.children.map(\.id),
                       ["model:claude-opus-5-5", "model:claude-sonnet-5-5", "model:claude-haiku-5-5"])
        XCTAssertEqual(Set(rows.map(\.id)), ["vendor:anthropic", "vendor:minimax", "vendor:zhipu"])
    }

    // MARK: Days and ranges

    func testTodayCountsOnlyWhatArrivedToday() {
        let stale = device("old", host: "old", today: ["glm-5.2": tokens(7)], past: [3: ["glm-5.2": tokens(2)]],
                           reportedDaysAgo: 1, link: .offline(reason: ""))
        let record = stale.usage!
        XCTAssertEqual(record.days(calendar: calendar)[key(1)]?["glm-5.2"], tokens(7))
        XCTAssertNil(record.days(calendar: calendar)[key(0)])
        XCTAssertEqual(report([stale], .today).total, 0)
        XCTAssertEqual(report([stale], .week).total, 9)
        // Still on the page, just not today.
        XCTAssertTrue(report([stale], .today).hasData)
    }

    /// History never holds the machine's today, so where a time zone puts both
    /// on one date they are different tokens and both count.
    func testTodayAndHistoryOnTheSameDateAreAdded() {
        let ahead = device("east", host: "east", today: ["glm-5.2": tokens(4)], past: [0: ["glm-5.2": tokens(6)]])
        XCTAssertEqual(report([ahead], .today).total, 10)
    }

    func testRangesAndTrend() throws {
        let result = report(fleet, .week)
        XCTAssertEqual(result.dayKeys, (0..<7).reversed().map { key($0) })
        XCTAssertEqual(report(fleet, .today).dayKeys, [key(0)])
        XCTAssertEqual(report(fleet, .month).dayKeys.count, 30)
        let studio = try XCTUnwrap(result.rows.first)
        XCTAssertEqual(studio.trend.count, 30)
        XCTAssertEqual(studio.trend.last, 1060)
        XCTAssertEqual(studio.trend[29 - 20], 500)
        XCTAssertEqual(result.series.first?.values, Array(studio.trend.suffix(7)))
    }

    func testExcludingCacheReads() throws {
        let all = report(fleet, .today)
        let without = report(fleet, .today, .machines, .withoutCacheRead)
        XCTAssertEqual(all.total - without.total, 900)
        XCTAssertEqual(without.parts, all.parts, "the four parts are shown either way")
        XCTAssertEqual(without.rows.first { $0.id == "machine:studio" }?.tokens, 160)
        // A row with nothing but cache reads drops out.
        let onlyCache = device("c", host: "c", today: ["glm-5.2": tokens(0, cacheRead: 50)])
        XCTAssertTrue(report([onlyCache], .today, .machines, .withoutCacheRead).rows.isEmpty)
    }

    // MARK: Change

    func testChangeAgainstThePeriodBefore() throws {
        let box = device("box", host: "box", today: ["glm-5.2": tokens(120)],
                         past: [1: ["glm-5.2": tokens(100)], 6: ["glm-5.2": tokens(30)],
                                7: ["glm-5.2": tokens(200)], 13: ["glm-5.2": tokens(100)],
                                14: ["glm-5.2": tokens(999)]])
        XCTAssertEqual(try XCTUnwrap(report([box], .today).change), 0.2, accuracy: 1e-9)
        // 250 against 300: day 14 is outside both weeks.
        XCTAssertEqual(try XCTUnwrap(report([box], .week).change), -50.0 / 300, accuracy: 1e-9)
        XCTAssertNil(report([box], .month).change, "there is nothing before 30 days")
    }

    func testNoChangeWhileAScanRuns() {
        let done = device("a", host: "a", today: ["glm-5.2": tokens(10)], past: [1: ["glm-5.2": tokens(10)]])
        let scanning = device("b", host: "b", today: ["glm-5.2": tokens(10)], past: [:], complete: false)
        let result = report([done, scanning], .today)
        XCTAssertNil(result.change)
        XCTAssertEqual(result.scanning, ["b"])
        XCTAssertEqual(result.scanProgress, 0.4, accuracy: 1e-9)
        XCTAssertEqual(result.total, 20)
    }

    func testMissingHistoryIsNamedAndLeftOut() throws {
        let done = device("a", host: "a", today: ["glm-5.2": tokens(10)], past: [1: ["glm-5.2": tokens(5)]])
        let noHistory = device("b", host: "b", today: ["glm-5.2": tokens(1000)], past: nil)
        let stale = device("c", host: "c", today: ["glm-5.2": tokens(3)], past: [1: ["glm-5.2": tokens(3)]],
                           reportedDaysAgo: 1, link: .offline(reason: ""))
        var never = FleetDeviceState(device: FleetDevice(id: "d", label: "d", ssh: ["d"]))
        never.link = .offline(reason: "")
        let result = report([done, noHistory, stale, never], .today)
        XCTAssertEqual(try XCTUnwrap(result.change), 1.0, accuracy: 1e-9)
        XCTAssertEqual(result.withoutHistory, ["b"])
        XCTAssertEqual(result.leftOutOfChange, ["c"])
        XCTAssertEqual(result.withoutData, ["d"])
        // Its today still counts in the total.
        XCTAssertEqual(result.total, 1010)
    }

    func testNoChangeFromNothing() {
        let new = device("a", host: "a", today: ["glm-5.2": tokens(10)], past: [:])
        XCTAssertNil(report([new], .today).change)
        XCTAssertNil(report([], .today).change)
        XCTAssertFalse(report([], .today).hasData)
    }

    // MARK: Series and colour

    func testFoldsIntoOtherAndColourFollowsTheMachine() throws {
        // Nine machines; the busiest today is the quietest over the week.
        var devices = (1...8).map { index in
            device("m\(index)", host: "m\(index)", today: ["glm-5.2": tokens(Int64(index))],
                   past: [2: ["glm-5.2": tokens(Int64(index) * 1000)]])
        }
        devices.append(device("m9", host: "m9", today: ["glm-5.2": tokens(1000)], past: [:]))

        let today = report(devices, .today)
        let week = report(devices, .week)
        XCTAssertEqual(today.series.count, FleetUsageReport.seriesLimit + 1)
        XCTAssertEqual(today.series.last?.id, "other")
        XCTAssertEqual(today.series.last?.color, .other)
        XCTAssertEqual(today.series.first?.id, "machine:m9")
        XCTAssertEqual(week.series.first?.id, "machine:m8")
        XCTAssertEqual(today.series.reduce(0) { $0 + $1.total }, today.total)

        func colors(_ result: FleetUsageReport) -> [String: FleetUsageColor] {
            Dictionary(uniqueKeysWithValues: result.rows.map { ($0.id, $0.color) })
        }
        XCTAssertEqual(colors(today), colors(week))
        // fleet.json order, whatever the ranking.
        XCTAssertEqual(colors(week)["machine:m1"], .slot(0))
        XCTAssertEqual(colors(week)["machine:m9"], .slot(8))
        XCTAssertEqual(colors(report(devices, .week, .machines, .withoutCacheRead)), colors(week))
    }

    func testAccountColoursPutClaudeFirstInFleetOrder() {
        let colors = Dictionary(uniqueKeysWithValues: report(fleet, .month, .accounts).rows.map { ($0.id, $0.color) })
        XCTAssertEqual(colors["claude:me@example.com"], .slot(0))
        XCTAssertEqual(colors["unknown:air"], .slot(1))
        XCTAssertEqual(colors["vendor:zhipu"], .slot(2))
        XCTAssertEqual(colors["vendor:minimax"], .slot(3))
        XCTAssertEqual(report(fleet, .today, .accounts).rows.first { $0.id == "vendor:minimax" }?.color, .slot(3))
    }

    // MARK: Record and cache

    private func snapshot(host: String, user: String, history: FleetUsageHistory?) -> FleetSnapshot {
        FleetSnapshot(agentVersion: 5,
                      host: FleetSnapshot.Host(hostname: host, system: "Linux", machine: "x86_64", cpus: 4, user: user),
                      sessions: [], todayByModel: ["glm-5.2": tokens(3)], workers: [],
                      claudeAccount: nil, usageHistory: history)
    }

    func testARecordKeepsItsHistoryUntilReplaced() {
        let history = FleetUsageHistory(complete: true, progress: 1, days: [key(1): ["glm-5.2": tokens(9)]])
        let first = FleetUsageRecord.updated(nil, with: snapshot(host: "a", user: "me", history: history), at: now)
        let later = FleetUsageRecord.updated(first, with: snapshot(host: "a", user: "me", history: nil),
                                             at: now.addingTimeInterval(5))
        XCTAssertEqual(later.history, history)
        XCTAssertEqual(later.reportedAt, now.addingTimeInterval(5))
        let replaced = FleetUsageHistory(complete: true, progress: 1, days: [:])
        XCTAssertEqual(FleetUsageRecord.updated(later, with: snapshot(host: "a", user: "me", history: replaced),
                                                at: now).history, replaced)
        // Pointed at other logs, the old history is someone else's.
        XCTAssertNil(FleetUsageRecord.updated(later, with: snapshot(host: "b", user: "me", history: nil),
                                              at: now).history)
    }

    func testARescanKeepsWhatIsKnownUntilItFinishes() {
        let known = FleetUsageHistory(complete: true, progress: 1, days: [key(1): ["glm-5.2": tokens(9)]])
        let first = FleetUsageRecord.updated(nil, with: snapshot(host: "a", user: "me", history: known), at: now)
        let partial = FleetUsageHistory(complete: false, progress: 0.4, days: [key(1): ["glm-5.2": tokens(2)]])
        let scanning = FleetUsageRecord.updated(first, with: snapshot(host: "a", user: "me", history: partial), at: now)
        XCTAssertEqual(scanning.history?.days, known.days)
        XCTAssertEqual(scanning.history?.complete, false)
        XCTAssertEqual(scanning.history?.progress, 0.4)
        let finished = FleetUsageHistory(complete: true, progress: 1, days: [key(1): ["glm-5.2": tokens(11)]])
        XCTAssertEqual(FleetUsageRecord.updated(scanning, with: snapshot(host: "a", user: "me", history: finished),
                                                at: now).history, finished)
        // A first scan has nothing to keep, so it shows what it has counted.
        XCTAssertEqual(FleetUsageRecord.updated(nil, with: snapshot(host: "a", user: "me", history: partial),
                                                at: now).history, partial)
    }

    private func temporaryPath() throws -> String {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        return folder.appendingPathComponent("TokNotch/fleet-usage.json").path
    }

    @MainActor
    func testCacheRoundTrip() throws {
        let path = try temporaryPath()
        let records = Dictionary(uniqueKeysWithValues: fleet.map { ($0.id, $0.usage!) })
        try FleetUsageCache.save(records, to: path)
        XCTAssertEqual(FleetUsageCache.load(from: path), records)

        let store = FleetStore(devices: [FleetDevice(id: "gpu", label: "gpu"), FleetDevice(id: "new", label: "new")],
                               script: nil, dispatcherFile: nil, configPath: "/nonexistent/fleet.json",
                               usageCachePath: path)
        XCTAssertEqual(store.devices.first?.usage, records["gpu"])
        XCTAssertNil(store.devices.last?.usage)
    }

    func testACorruptCacheIsIgnored() throws {
        let path = try temporaryPath()
        try FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent,
                                                withIntermediateDirectories: true)
        for bad in ["{not json", "[]", "{\"version\":99,\"devices\":{}}", ""] {
            try Data(bad.utf8).write(to: URL(fileURLWithPath: path))
            XCTAssertEqual(FleetUsageCache.load(from: path), [:], bad)
        }
        XCTAssertEqual(FleetUsageCache.load(from: "/nonexistent/fleet-usage.json"), [:])
    }

    func testWritesAtMostOnceAMinuteAndOnlyWhenChanged() throws {
        let saved = try XCTUnwrap(fleet.first?.usage)
        var changed = saved
        changed.todayByModel["glm-5.2"] = tokens(1)
        func needs(_ record: FleetUsageRecord, _ savedAt: Date?, _ at: Date) -> Bool {
            FleetUsageCache.needsWrite(record, saved: saved, savedAt: savedAt, now: at, calendar: calendar)
        }
        XCTAssertTrue(FleetUsageCache.needsWrite(saved, saved: nil, savedAt: nil, now: now, calendar: calendar))
        XCTAssertFalse(needs(saved, nil, now), "unchanged")
        XCTAssertFalse(needs(changed, now.addingTimeInterval(-59), now), "within the minute")
        XCTAssertTrue(needs(changed, now.addingTimeInterval(-60), now))

        // A later report time alone waits, unless it starts a new day.
        var seen = saved
        seen.reportedAt = saved.reportedAt.addingTimeInterval(120)
        XCTAssertFalse(needs(seen, nil, now))
        seen.reportedAt = saved.reportedAt.addingTimeInterval(FleetUsageCache.lastSeenStep)
        XCTAssertTrue(needs(seen, nil, now))
        var tomorrow = saved
        tomorrow.reportedAt = calendar.startOfDay(for: now).addingTimeInterval(86_400 + 1)
        tomorrow.todayByModel = saved.todayByModel
        var lateTonight = saved
        lateTonight.reportedAt = calendar.startOfDay(for: now).addingTimeInterval(86_400 - 60)
        XCTAssertTrue(FleetUsageCache.needsWrite(tomorrow, saved: lateTonight, savedAt: nil,
                                                 now: now, calendar: calendar))
    }

    func testDayKeys() {
        XCTAssertEqual(key(0), "2026-11-01")
        XCTAssertEqual(FleetUsageDay.keys(3, now: now, calendar: calendar), ["2026-10-30", "2026-10-31", "2026-11-01"])
        XCTAssertEqual(FleetUsageDay.keys(2, endingDaysAgo: 2, now: now, calendar: calendar),
                       ["2026-10-29", "2026-10-30"])
        XCTAssertEqual(FleetUsageDay.date("2026-10-31", calendar: calendar).map { FleetUsageDay.key($0, calendar: calendar) },
                       "2026-10-31")
        XCTAssertNil(FleetUsageDay.date("yesterday", calendar: calendar))
    }
}
