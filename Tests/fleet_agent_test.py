#!/usr/bin/env python3
"""Tests for Sources/Resources/fleet-agent.py. Run: python3 Tests/fleet_agent_test.py"""
import importlib.util, json, os, shutil, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "SECRET-BODY-TEXT"


def load_agent(home):
    os.environ["HOME"] = home
    spec = importlib.util.spec_from_file_location("agent", os.path.join(ROOT, "Sources/Resources/fleet-agent.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def assistant(ident, when, output, session="s1", model="m1", **extra):
    return json.dumps({
        "type": "assistant", "sessionId": session, "timestamp": stamp(when), "cwd": "/work/proj",
        "entrypoint": "cli",
        "message": {"id": ident, "model": model, "content": [
            {"type": "text", "text": SECRET},
            {"type": "tool_use", "name": "Bash", "input": {"command": SECRET}}],
            "usage": {"input_tokens": 1, "output_tokens": output,
                      "cache_read_input_tokens": 10, "cache_creation_input_tokens": 100}},
        **extra})


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.saved = os.environ.get("HOME")
        self.agent = load_agent(self.home)
        self.now = time.time()
        self.project = os.path.join(self.home, ".claude/projects/p")
        os.makedirs(self.project)

    def tearDown(self):
        shutil.rmtree(self.home)
        os.environ["HOME"] = self.saved

    def write(self, name, *lines):
        path = os.path.join(self.project, name)
        with open(path, "a") as handle:
            handle.write("".join(line + "\n" for line in lines))
        return path

    def report(self):
        log = self.agent.SessionLog()
        log.scan(time.time())
        return self.agent.snapshot(log, time.time())


class Usage(Base):
    def test_repeated_message_ids_count_once_with_the_latest_usage(self):
        self.write("s1.jsonl", assistant("a", self.now, 5), assistant("a", self.now, 50))
        usage = self.report()["todayByModel"]["m1"]
        self.assertEqual(usage, {"input": 1, "output": 50, "cacheRead": 10, "cacheWrite": 100})

    def test_a_resumed_session_repeating_lines_does_not_double_count(self):
        self.write("s1.jsonl", assistant("a", self.now, 5))
        self.write("s2.jsonl", assistant("a", self.now, 5, session="s2"), assistant("b", self.now, 7, session="s2"))
        self.assertEqual(self.report()["todayByModel"]["m1"]["output"], 12)

    def test_yesterdays_messages_count_for_the_session_but_not_for_today(self):
        midnight = self.agent.local_midnight(self.now)
        self.write("s1.jsonl", assistant("old", midnight - 60, 5), assistant("new", self.now, 7))
        report = self.report()
        self.assertEqual(report["todayByModel"]["m1"]["output"], 7)
        self.assertEqual(report["sessions"][0]["usage"]["output"], 12)

    def test_synthetic_messages_are_ignored(self):
        self.write("s1.jsonl", assistant("a", self.now, 5, model="<synthetic>"))
        self.assertEqual(self.report()["todayByModel"], {})

    def test_a_half_written_line_waits_for_the_rest(self):
        line = assistant("a", self.now, 5)
        path = os.path.join(self.project, "s1.jsonl")
        with open(path, "w") as handle:
            handle.write(line[:40])
        log = self.agent.SessionLog()
        log.scan(time.time())
        self.assertEqual(log.report(time.time())[1], {})
        with open(path, "a") as handle:
            handle.write(line[40:] + "\n")
        log.scan(time.time())
        self.assertEqual(log.report(time.time())[1]["m1"]["output"], 5)

    def test_new_lines_are_picked_up_without_rereading(self):
        self.write("s1.jsonl", assistant("a", self.now, 5))
        log = self.agent.SessionLog()
        log.scan(time.time())
        self.write("s1.jsonl", assistant("b", self.now, 6))
        log.scan(time.time())
        self.assertEqual(log.report(time.time())[1]["m1"]["output"], 11)

    def test_garbage_lines_do_not_stop_the_read(self):
        self.write("s1.jsonl", '{"usage": not json', "[]", '"usage"', assistant("a", self.now, 5))
        self.assertEqual(self.report()["todayByModel"]["m1"]["output"], 5)


class Privacy(Base):
    def test_bodies_and_tool_inputs_never_leave(self):
        self.write("s1.jsonl", assistant("a", self.now, 5))
        text = json.dumps(self.report())
        self.assertNotIn(SECRET, text)
        self.assertIn("Bash", text)

    def test_worker_result_text_never_leaves(self):
        job = os.path.join(self.home, ".cache/mm-worker/jobs/20261101-000000-1-minimax")
        os.makedirs(job)
        open(os.path.join(job, "started"), "w").close()
        with open(os.path.join(job, "rc"), "w") as handle:
            handle.write("0\n")
        with open(os.path.join(job, "prompt.md"), "w") as handle:
            handle.write("# Task: do it\n" + SECRET)
        with open(os.path.join(job, "out.json"), "w") as handle:
            json.dump({"result": SECRET, "session_id": "s9", "usage": {"input_tokens": 3, "output_tokens": 4}}, handle)
        worker = self.report()["workers"][0]
        self.assertNotIn(SECRET, json.dumps(worker))
        self.assertEqual(worker["state"], "done")
        self.assertEqual(worker["usage"]["output"], 4)
        self.assertEqual(worker["provider"], "minimax")
        self.assertEqual(worker["sessionId"], "s9")


class Workers(Base):
    def job(self, name, pid=None, rc=None):
        folder = os.path.join(self.home, ".cache/mm-worker/jobs", name)
        os.makedirs(folder)
        open(os.path.join(folder, "started"), "w").close()
        if pid is not None:
            with open(os.path.join(folder, "pid"), "w") as handle:
                handle.write(str(pid))
        if rc is not None:
            with open(os.path.join(folder, "rc"), "w") as handle:
                handle.write(rc)

    def test_job_states(self):
        self.job("1-1-1-claude", pid=os.getpid())
        self.job("1-1-2-zhipu", pid=999999)
        self.job("1-1-3-minimax", rc="124\n")
        self.job("1-1-4-minimax", rc="cancelled\n")
        states = {w["id"]: w["state"] for w in self.report()["workers"]}
        self.assertEqual(states, {"1-1-1": "running", "1-1-2": "lost", "1-1-3": "timeout", "1-1-4": "cancelled"})

    def test_a_slot_finds_its_run_and_notes_where_it_went(self):
        state = os.path.join(self.home, ".cache/mm-worker")
        slot = os.path.join(state, "slots/claude/1")
        os.makedirs(slot)
        os.makedirs(os.path.join(state, "runs"))
        pid = str(os.getpid())
        with open(os.path.join(slot, "pid"), "w") as handle:
            handle.write(pid + "\n")
        with open(os.path.join(state, "runs", "20261101-1-%s.prompt.md" % pid), "w") as handle:
            handle.write("## Rules\n\n# 任务：改代码\n")
        with open(os.path.join(state, "runs", "20261101-1-%s.remote" % pid), "w") as handle:
            handle.write("other-box .cache/x claude\n")
        worker = self.report()["workers"][0]
        self.assertEqual(worker["title"], "任务：改代码")
        self.assertEqual(worker["remoteHost"], "other-box")

    def test_no_state_directories_is_fine(self):
        self.assertEqual(self.report()["workers"], [])


class Vitals(Base):
    def sys_file(self, relative, text):
        path = os.path.join(self.home, "sys", relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)

    def use_fixture(self):
        self.agent.SYS_CLASS = os.path.join(self.home, "sys")

    def test_cpu_percent_comes_from_the_interval_between_reads(self):
        reads = iter([(100, 1000), (150, 1100), (150, 1100), (10, 1200)])
        sampler = self.agent.CPUSampler(read=lambda: next(reads))
        self.assertEqual(sampler.percent(), 50.0)
        self.assertIsNone(sampler.percent())  # no time passed
        self.assertIsNone(sampler.percent())  # counter went backwards

    def test_cpu_with_no_source_is_unknown_not_zero(self):
        self.assertIsNone(self.agent.CPUSampler(read=lambda: None).percent())

    def test_proc_stat_counts_iowait_as_idle(self):
        path = os.path.join(self.home, "stat")
        with open(path, "w") as handle:
            handle.write("cpu  10 0 10 70 10 0 0 0 0 0\ncpu0 1 1 1 1 1\n")
        self.agent.PROC_STAT = path
        self.assertEqual(self.agent.linux_cpu_times(), (20, 100))

    def test_ryzen_uses_tctl_not_the_hottest_entry(self):
        self.sys_file("hwmon/hwmon0/name", "k10temp\n")
        self.sys_file("hwmon/hwmon0/temp1_input", "61250\n")
        self.sys_file("hwmon/hwmon0/temp1_label", "Tctl\n")
        self.sys_file("hwmon/hwmon0/temp3_input", "70000\n")
        self.sys_file("hwmon/hwmon0/temp3_label", "Tccd1\n")
        self.use_fixture()
        self.assertEqual(self.agent.linux_temperature(), 61.2)

    def test_gpu_and_disk_sensors_are_not_the_cpu(self):
        self.sys_file("hwmon/hwmon0/name", "nvme\n")
        self.sys_file("hwmon/hwmon0/temp1_input", "40000\n")
        self.sys_file("hwmon/hwmon1/name", "amdgpu\n")
        self.sys_file("hwmon/hwmon1/temp1_input", "80000\n")
        self.use_fixture()
        self.assertIsNone(self.agent.linux_temperature())

    def test_raspberry_pi_thermal_zone(self):
        self.sys_file("thermal/thermal_zone0/type", "cpu-thermal\n")
        self.sys_file("thermal/thermal_zone0/temp", "48700\n")
        self.use_fixture()
        self.assertEqual(self.agent.linux_temperature(), 48.7)

    def test_nonsense_sensor_values_are_ignored(self):
        self.sys_file("thermal/thermal_zone0/type", "cpu-thermal\n")
        self.sys_file("thermal/thermal_zone0/temp", "-1000\n")
        self.use_fixture()
        self.assertIsNone(self.agent.linux_temperature())

    def test_mac_picks_hottest_cpu_die_and_skips_calibration_and_battery(self):
        pick = self.agent.pick_darwin_temperature
        self.assertEqual(pick({"PMU tdie1": 45.5, "PMU tdie2": 52.04, "PMU tcal": 51.0, "gas gauge battery": 90.0}), 52.0)
        self.assertEqual(pick({"pACC MTR Temp Sensor1": 60.0, "NAND CH0 temp": 90.0}), 60.0)
        self.assertEqual(pick({"PMU tcal": 51.0}), None)
        self.assertEqual(pick({}), None)

    def test_a_failing_temperature_read_does_not_stop_the_report(self):
        def broken():
            raise OSError("no sensors")
        self.write("s1.jsonl", assistant("a", self.now, 5))
        log = self.agent.SessionLog()
        log.scan(time.time())
        host = self.agent.snapshot(log, time.time(), self.agent.Vitals(read_temperature=broken))["host"]
        self.assertIsNone(host["temperature"])
        self.assertIn("cpuPercent", host)

    def test_temperature_is_read_less_often_than_cpu(self):
        calls = []
        def read():
            calls.append(1)
            return 50.0
        vitals = self.agent.Vitals(read_temperature=read)
        vitals.read(100.0)
        vitals.read(102.0)
        self.assertEqual(len(calls), 1)
        vitals.read(100.0 + self.agent.TEMPERATURE_INTERVAL)
        self.assertEqual(len(calls), 2)


class Streams(Base):
    def stream(self, path, *events):
        with open(path, "a") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")

    def tool(self, ident, name, **inputs):
        return {"type": "assistant", "session_id": "s1", "message": {"id": ident, "model": "m1", "content": [
            {"type": "text", "text": SECRET}, {"type": "tool_use", "name": name, "input": inputs}]}}

    def running_job(self):
        folder = os.path.join(self.home, ".cache/mm-worker/jobs/20261101-000000-1-minimax")
        os.makedirs(folder)
        open(os.path.join(folder, "started"), "w").close()
        with open(os.path.join(folder, "pid"), "w") as handle:
            handle.write(str(os.getpid()))
        return folder

    def test_running_job_reports_steps_tool_and_file_name_only(self):
        folder = self.running_job()
        self.stream(os.path.join(folder, "out.json"),
                    {"type": "system", "subtype": "init", "session_id": "s1", "model": "m1", "cwd": SECRET},
                    self.tool("a", "Read", file_path="/secret/dir/one.py"),
                    {"type": "user", "message": {"content": SECRET}},
                    self.tool("a", "Edit", file_path="/secret/dir/two.py"),
                    self.tool("b", "Bash", command=SECRET))
        worker = self.report()["workers"][0]
        self.assertEqual(worker["steps"], 2)
        self.assertEqual(worker["lastTool"], "Bash")
        self.assertNotIn("lastFile", worker)
        self.assertEqual(worker["sessionId"], "s1")
        self.assertNotIn("stream", worker)
        self.assertNotIn(SECRET, json.dumps(worker))
        self.assertNotIn("/secret", json.dumps(worker))

    def test_file_name_without_its_folders(self):
        folder = self.running_job()
        self.stream(os.path.join(folder, "out.json"), self.tool("a", "Edit", file_path="/secret/dir/two.py"))
        worker = self.report()["workers"][0]
        self.assertEqual(worker["lastFile"], "two.py")

    def test_tokens_come_from_the_session_log(self):
        folder = self.running_job()
        self.stream(os.path.join(folder, "out.json"), {"type": "assistant", "session_id": "s1", "message": {
            "id": "a", "model": "m1", "content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}})
        self.write("s1.jsonl", assistant("a", self.now, 50))
        log = self.agent.SessionLog()
        log.scan(time.time())
        worker = self.agent.snapshot(log, time.time())["workers"][0]
        self.assertEqual(worker["usage"]["output"], 50)

    def test_progress_is_read_incrementally_and_survives_a_half_line(self):
        folder = self.running_job()
        path = os.path.join(folder, "out.json")
        line = json.dumps(self.tool("a", "Read"))
        with open(path, "w") as handle:
            handle.write(line[:30])
        streams = self.agent.WorkerStreams()
        self.assertNotIn("steps", streams.progress(path, time.time()))
        with open(path, "a") as handle:
            handle.write(line[30:] + "\n")
        self.assertEqual(streams.progress(path, time.time())["steps"], 1)
        self.stream(path, self.tool("b", "Edit"))
        self.assertEqual(streams.progress(path, time.time())["lastTool"], "Edit")

    def test_a_job_with_no_stream_yet_still_lists(self):
        self.running_job()
        worker = self.report()["workers"][0]
        self.assertEqual(worker["state"], "running")
        self.assertNotIn("steps", worker)

    def test_a_given_name_beats_the_prompt(self):
        folder = self.running_job()
        with open(os.path.join(folder, "prompt.md"), "w") as handle:
            handle.write("# Task: from prompt\n")
        with open(os.path.join(folder, "name"), "w") as handle:
            handle.write("given name\n")
        self.assertEqual(self.report()["workers"][0]["title"], "given name")


class Cancel(Base):
    def job(self, pid):
        folder = os.path.join(self.home, ".cache/mm-worker/jobs/20261101-000000-1-minimax")
        os.makedirs(folder)
        with open(os.path.join(folder, "pid"), "w") as handle:
            handle.write(str(pid))
        return folder

    def test_bad_ids_are_refused(self):
        for bad in ("", "../../etc", "1-2-3; rm -rf /", "*", "1-2"):
            self.assertEqual(self.agent.cancel(bad)["ok"], False, bad)

    def test_unknown_run_is_refused(self):
        os.makedirs(os.path.join(self.home, ".cache/mm-worker/runs"))
        self.assertFalse(self.agent.cancel("20261101-000000-999999")["ok"])

    def test_dead_job_is_refused_and_not_marked(self):
        folder = self.job(999999)
        self.assertFalse(self.agent.cancel("20261101-000000-1")["ok"])
        self.assertFalse(os.path.exists(os.path.join(folder, "rc")))

    def test_finished_job_keeps_its_result(self):
        folder = self.job(os.getpid())
        with open(os.path.join(folder, "rc"), "w") as handle:
            handle.write("0\n")
        result = self.agent.cancel("20261101-000000-1")
        self.assertFalse(result["ok"])
        with open(os.path.join(folder, "rc")) as handle:
            self.assertEqual(handle.read(), "0\n")

    def test_a_pid_that_is_not_a_worker_is_left_alone(self):
        state = os.path.join(self.home, ".cache/mm-worker")
        os.makedirs(os.path.join(state, "runs"))
        os.makedirs(os.path.join(state, "slots/claude/1"))
        pid = str(os.getpid())  # this test process: alive, but not mm-worker
        with open(os.path.join(state, "slots/claude/1/pid"), "w") as handle:
            handle.write(pid)
        result = self.agent.cancel("20261101-000000-" + pid)
        self.assertEqual(result, {"ok": False, "error": "not a worker"})

    def test_a_worker_sent_elsewhere_is_refused_here(self):
        state = os.path.join(self.home, ".cache/mm-worker")
        os.makedirs(os.path.join(state, "runs"))
        os.makedirs(os.path.join(state, "slots/claude/1"))
        pid = str(os.getpid())
        with open(os.path.join(state, "slots/claude/1/pid"), "w") as handle:
            handle.write(pid)
        open(os.path.join(state, "runs", "20261101-000000-%s.remote" % pid), "w").close()
        self.assertEqual(self.agent.cancel("20261101-000000-" + pid)["error"], "runs on another machine")


class Timestamps(Base):
    def test_matches_calendar(self):
        import calendar
        for text in ("2026-11-01T07:25:36.123Z", "2024-02-29T00:00:00Z", "1999-12-31T23:59:59Z"):
            expect = calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
            self.assertEqual(self.agent.parse_timestamp(text), expect)


if __name__ == "__main__":
    unittest.main()
