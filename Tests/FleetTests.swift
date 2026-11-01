import XCTest
@testable import TokNotch

final class FleetTests: XCTestCase {
    private let sample = """
    {"agentVersion":1,"host":{"hostname":"box","system":"Linux","machine":"x86_64","cpus":16,"load":1.5,"cpuPercent":42.5,"temperature":61.2},
    "sessions":[{"id":"a","title":"t","kind":"interactive","model":"m","cwd":"~/x","lastTool":"Bash","idleSeconds":5,
    "usage":{"input":1,"output":2,"cacheRead":3,"cacheWrite":4}},
    {"id":"b","title":"","kind":"worker","model":"m","cwd":"","lastTool":"","idleSeconds":500,
    "usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0}}],
    "todayByModel":{"m":{"input":1,"output":2,"cacheRead":3,"cacheWrite":4},"n":{"input":10,"output":0,"cacheRead":0,"cacheWrite":0}},
    "workers":[{"id":"1","provider":"minimax","state":"running","title":"x","elapsedSeconds":3,"steps":6,"lastTool":"Edit","lastFile":"foo.swift","model":"m","sessionId":"b","idleSeconds":400},
    {"id":"2","provider":"claude","state":"running","title":"y","elapsedSeconds":3,"remoteHost":"other-box"},
    {"id":"3","provider":"zhipu","state":"done","title":"z","elapsedSeconds":3,"usage":{"input":1,"output":1,"cacheRead":0,"cacheWrite":0}}]}
    """.replacingOccurrences(of: "\n", with: "")

    func testAWorkersSessionIsNotListedOrCountedTwice() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.sessions.map(\.id), ["a", "b"])
        XCTAssertEqual(snapshot.ownSessions.map(\.id), ["a"])
    }

    func testWorkerProgress() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        let worker = try XCTUnwrap(snapshot.workers.first { $0.id == "1" })
        XCTAssertEqual(worker.steps, 6)
        XCTAssertEqual(worker.lastFile, "foo.swift")
        XCTAssertTrue(worker.isQuiet)
        XCTAssertTrue(worker.isCancellableHere)
    }

    /// One sent elsewhere is stopped on the machine running it; finished ones are done.
    func testOnlyRunningLocalWorkersCanBeCancelledHere() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.workers.filter(\.isCancellableHere).map(\.id), ["1"])
    }

    func testOnlyRealRunIDsAreAccepted() {
        XCTAssertTrue(FleetConnection.isRunID("20261101-012605-1064"))
        for bad in ["", "1-2", "1-2-3; rm -rf ~", "../x", "1-2-3\n", "$(id)-1-2"] {
            XCTAssertFalse(FleetConnection.isRunID(bad), bad)
        }
    }

    func testCancelCommandAndAnswers() {
        let device = FleetDevice(id: "p", label: "p", ssh: ["box"])
        XCTAssertEqual(FleetConnection.command(for: device, alias: "box", action: ["cancel", "1-2-3"]).arguments.last,
                       "python3 -I -u - 'cancel' '1-2-3'")
        XCTAssertNil(FleetConnection.cancelError(fromOutput: Data("noise\n{\"ok\": true}\n".utf8)))
        XCTAssertEqual(FleetConnection.cancelError(fromOutput: Data("{\"ok\": false, \"error\": \"not running\"}".utf8)),
                       "not running")
        XCTAssertNotNil(FleetConnection.cancelError(fromOutput: Data()))
    }

    func testDecodesAgentLine() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.todayTotal, 20)
        XCTAssertEqual(snapshot.activeSessions.map(\.id), ["a"])
    }

    func testReadsCPUAndTemperature() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.host.cpuPercent, 42.5)
        XCTAssertEqual(snapshot.host.temperature, 61.2)
    }

    /// A machine with no readable sensor still reports, and shows a dash rather than 0.
    func testMissingVitalsAreUnknownNotZero() throws {
        let without = sample.replacingOccurrences(of: ",\"cpuPercent\":42.5,\"temperature\":61.2", with: "")
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(without.utf8)))
        XCTAssertNil(snapshot.host.cpuPercent)
        XCTAssertNil(snapshot.host.temperature)
        XCTAssertEqual(FleetText.percent(snapshot.host.cpuPercent), "—")
        XCTAssertEqual(FleetText.degrees(snapshot.host.temperature), "—")
    }

    func testVitalLevels() {
        XCTAssertEqual(FleetVitalLevel.cpu(69.9), .normal)
        XCTAssertEqual(FleetVitalLevel.cpu(70), .warm)
        XCTAssertEqual(FleetVitalLevel.cpu(90), .hot)
        XCTAssertEqual(FleetVitalLevel.temperature(74.9), .normal)
        XCTAssertEqual(FleetVitalLevel.temperature(75), .warm)
        XCTAssertEqual(FleetVitalLevel.temperature(90), .hot)
    }

    func testVitalsRoundForDisplay() {
        XCTAssertEqual(FleetText.percent(42.4), "42%")
        XCTAssertEqual(FleetText.percent(42.5), "43%")
        XCTAssertEqual(FleetText.degrees(61.6), "62°C")
    }

    /// A worker sent to another machine belongs to that machine's count.
    func testRunningWorkersLeaveOutOnesSentElsewhere() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.runningWorkers.map(\.id), ["1"])
    }

    /// Shell profiles print things. They must not count as a report.
    func testIgnoresOtherOutput() {
        XCTAssertNil(FleetSnapshot.decode(line: Data("Welcome to Ubuntu".utf8)))
    }

    func testCommandsKeepPythonIsolatedAndUseSudoOnlyWhenAsked() {
        let local = FleetConnection.command(for: FleetDevice(id: "m", label: "m"), alias: nil)
        XCTAssertEqual(local.arguments, ["python3", "-I", "-u", "-", "watch"])

        let plain = FleetDevice(id: "p", label: "p", ssh: ["box"])
        XCTAssertEqual(FleetConnection.command(for: plain, alias: "box").arguments.last,
                       "python3 -I -u - watch")

        let other = FleetDevice(id: "c", label: "c", ssh: ["box"], sudoUser: "someone")
        XCTAssertEqual(FleetConnection.command(for: other, alias: "box").arguments.last,
                       "sudo -n -u 'someone' -H python3 -I -u - watch")
    }

    func testRetryBacksOffToAMinute() {
        XCTAssertEqual(FleetConnection.retryDelay(afterFailures: 1), 5)
        XCTAssertEqual(FleetConnection.retryDelay(afterFailures: 2), 10)
        XCTAssertEqual(FleetConnection.retryDelay(afterFailures: 20), 60)
    }

    func testReasonSkipsRoutineSSHWarning() {
        let errors = Data("ssh: connect to host x port 22: Operation timed out\nWarning: Permanently added 'x' to known hosts.\n".utf8)
        XCTAssertEqual(FleetConnection.reason(from: errors),
                       "ssh: connect to host x port 22: Operation timed out")
    }

    func testMissingConfigMeansThisMac() {
        XCTAssertEqual(FleetConfig.load(from: "/nonexistent/fleet.json"), [FleetConfig.thisMac])
    }
}
