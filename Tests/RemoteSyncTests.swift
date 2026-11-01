import XCTest
@testable import TokNotch

final class RemoteSyncTests: XCTestCase {
    func testParsesOneLinePerHost() {
        let outcome = RemoteSync.parse("""
            2026-10-29 13:44:39 ROG: ok (9.9M)
            2026-10-29 13:44:42 pi4: 连不上 pi4，跳过
            2026-10-29 13:44:44 pi4-myadmin: ok (404M)
            rsync noise without a host
            """)
        XCTAssertEqual(outcome.succeeded, ["ROG", "pi4-myadmin"])
        XCTAssertEqual(outcome.failed, ["pi4"])
        XCTAssertEqual(outcome.total, 3)
    }

    func testEmptyOutputIsNoHosts() {
        XCTAssertEqual(RemoteSync.parse("").total, 0)
    }
}
