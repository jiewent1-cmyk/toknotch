import XCTest
@testable import TokNotch

final class FleetTests: XCTestCase {
    private let sample = """
    {"agentVersion":1,"host":{"hostname":"box","system":"Linux","machine":"x86_64","cpus":16,"load":1.5},
    "sessions":[{"id":"a","title":"t","kind":"interactive","model":"m","cwd":"~/x","lastTool":"Bash","idleSeconds":5,
    "usage":{"input":1,"output":2,"cacheRead":3,"cacheWrite":4}},
    {"id":"b","title":"","kind":"worker","model":"m","cwd":"","lastTool":"","idleSeconds":500,
    "usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0}}],
    "todayByModel":{"m":{"input":1,"output":2,"cacheRead":3,"cacheWrite":4},"n":{"input":10,"output":0,"cacheRead":0,"cacheWrite":0}},
    "workers":[{"id":"1","provider":"minimax","state":"running","title":"x","elapsedSeconds":3},
    {"id":"2","provider":"claude","state":"running","title":"y","elapsedSeconds":3,"remoteHost":"other-box"},
    {"id":"3","provider":"zhipu","state":"done","title":"z","elapsedSeconds":3,"usage":{"input":1,"output":1,"cacheRead":0,"cacheWrite":0}}]}
    """.replacingOccurrences(of: "\n", with: "")

    func testDecodesAgentLine() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.todayTotal, 20)
        XCTAssertEqual(snapshot.activeSessions.map(\.id), ["a"])
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
