import XCTest
@testable import TokNotch

/// Installing `mm-remote-run` as part of a machine's setup, short of a machine.
final class FleetSetupApplyTests: XCTestCase {
    private let target = "~/.local/bin/mm-remote-run"
    private let temp = "~/.local/bin/.mm-remote-run.tmp"

    private func result(_ output: String = "", status: Int32 = 0,
                        inputComplete: Bool = true) -> FleetConnection.CommandResult {
        var result = FleetConnection.CommandResult()
        result.status = status
        result.output = Data(output.utf8)
        result.inputComplete = inputComplete
        return result
    }

    /// The same safe path as the dispatcher: a temp file beside the target, the
    /// byte count checked, and only then put in place.
    func testRunnerCommandChecksTheByteCountBeforePlacingTheFile() throws {
        let shell = FleetConnection.runnerInstallShellCommand(byteCount: 123)
        XCTAssertTrue(shell.hasPrefix("mkdir -p ~/.local/bin && "))
        XCTAssertTrue(shell.contains("[ \"$(wc -c < \(temp) | tr -d ' ')\" = 123 ] || { rm -f \(temp); exit 1; }"))
        XCTAssertTrue(shell.contains("chmod 755 \(temp)"))
        let read = try XCTUnwrap(shell.range(of: "cat > \(temp)"))
        let check = try XCTUnwrap(shell.range(of: "wc -c"))
        let place = try XCTUnwrap(shell.range(of: "ln \(temp) \(target)"))
        XCTAssertLessThan(read.lowerBound, check.lowerBound)
        XCTAssertLessThan(check.lowerBound, place.lowerBound)
    }

    /// Existing runners are tuned by hand. The script looks before reading any
    /// input, and places the file with `ln`, which refuses an existing name,
    /// rather than `mv`, which would replace it.
    func testRunnerCommandNeverOverwrites() {
        let shell = FleetConnection.runnerInstallShellCommand(byteCount: 123)
        let present = "if [ -e \(target) ] || [ -L \(target) ]; then echo '\(FleetConnection.runnerPresentMarker)'; exit 0; fi"
        XCTAssertTrue(shell.contains(present))
        if let look = shell.range(of: present), let read = shell.range(of: "cat > ") {
            XCTAssertLessThan(look.lowerBound, read.lowerBound)
        }
        XCTAssertFalse(shell.contains("mv "))
        XCTAssertFalse(shell.contains("ln -f"))
        XCTAssertTrue(shell.contains("if ln \(temp) \(target); then rm -f \(temp);"))
        // One that appeared during the transfer is reported as there, anything else fails.
        XCTAssertTrue(shell.hasSuffix(
            "else rm -f \(temp); [ -e \(target) ] || [ -L \(target) ] || exit 1; echo '\(FleetConnection.runnerPresentMarker)'; fi"))
    }

    /// The dispatcher's own command did not change in sharing its first steps.
    func testDispatcherCommandIsUnchanged() {
        XCTAssertEqual(FleetConnection.installShellCommand(byteCount: 123),
                       "mkdir -p ~/.local/bin && cat > ~/.local/bin/.mm-worker.tmp"
                       + " && { [ \"$(wc -c < ~/.local/bin/.mm-worker.tmp | tr -d ' ')\" = 123 ] || { rm -f ~/.local/bin/.mm-worker.tmp; exit 1; }; }"
                       + " && chmod 755 ~/.local/bin/.mm-worker.tmp && mv ~/.local/bin/.mm-worker.tmp ~/.local/bin/mm-worker")
    }

    func testRunnerCommandRunsWhereTheDispatchersDoesAndNeverForAReadOnlyAccount() throws {
        let exact = FleetConnection.runnerInstallShellCommand(byteCount: 9)
        let box = FleetDevice(id: "p", label: "p", ssh: ["box"])
        let remote = try XCTUnwrap(FleetConnection.runnerInstallCommand(for: box, alias: "box", byteCount: 9))
        XCTAssertEqual(remote.path, "/usr/bin/ssh")
        XCTAssertEqual(remote.arguments.last, exact)
        XCTAssertTrue(remote.arguments.contains("BatchMode=yes"))
        let local = try XCTUnwrap(FleetConnection.runnerInstallCommand(for: FleetDevice(id: "m", label: "m"),
                                                                       alias: nil, byteCount: 9))
        XCTAssertEqual(local.path, "/bin/sh")
        XCTAssertEqual(local.arguments, ["-c", exact])
        let other = FleetDevice(id: "c", label: "c", ssh: ["box"], sudoUser: "someone")
        XCTAssertNil(FleetConnection.runnerInstallCommand(for: other, alias: "box", byteCount: 9))
    }

    /// One already there is not a failure, even though the script left its
    /// input unread, which can make the write fail.
    func testAnExistingRunnerIsReportedAsThere() {
        let marker = FleetConnection.runnerPresentMarker
        XCTAssertEqual(FleetConnection.runnerInstallOutcome(from: result("profile noise\n\(marker)\n")),
                       .alreadyThere)
        XCTAssertEqual(FleetConnection.runnerInstallOutcome(from: result(marker, inputComplete: false)),
                       .alreadyThere)
    }

    /// A short file makes the script remove it and exit 1, which is a failure.
    func testAByteCountMismatchIsAFailure() {
        XCTAssertEqual(FleetConnection.runnerInstallOutcome(from: result(status: 1)),
                       .failed(.exit(1, "")))
        // The marker counts only on a clean exit.
        XCTAssertEqual(FleetConnection.runnerInstallOutcome(
            from: result(FleetConnection.runnerPresentMarker, status: 1)), .failed(.exit(1, "")))
        var timedOut = result(FleetConnection.runnerPresentMarker)
        timedOut.timedOut = true
        XCTAssertEqual(FleetConnection.runnerInstallOutcome(from: timedOut), .failed(.timedOut))
    }

    func testOnlyACleanFinishWithEveryByteSentIsInstalled() {
        XCTAssertEqual(FleetConnection.runnerInstallOutcome(from: result()), .installed)
        XCTAssertEqual(FleetConnection.runnerInstallOutcome(from: result(inputComplete: false)),
                       .failed(.inputIncomplete))
        // A line that merely mentions the marker is not it.
        XCTAssertEqual(FleetConnection.runnerInstallOutcome(
            from: result("note: \(FleetConnection.runnerPresentMarker)")), .installed)
    }

    func testNothingToSendOrAReadOnlyAccountFailsWithoutRunning() async {
        let box = FleetDevice(id: "p", label: "p", ssh: ["box"])
        let empty = await FleetConnection.installRunner(Data(), on: box, alias: "box")
        XCTAssertEqual(empty, .failed(.unavailable))
        let other = FleetDevice(id: "c", label: "c", ssh: ["box"], sudoUser: "someone")
        let readOnly = await FleetConnection.installRunner(Data("x".utf8), on: other, alias: "box")
        XCTAssertEqual(readOnly, .failed(.readOnly))
    }
}
