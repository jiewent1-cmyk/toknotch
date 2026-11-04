#!/usr/bin/env python3
"""Tests for Sources/Resources/fleet-agent.py. Run: python3 Tests/fleet_agent_test.py"""
import base64, copy, fcntl, importlib.util, json, os, re, shutil, stat, subprocess, sys, tempfile, time, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "Sources/Resources/fleet-agent.py")
SECRET = "SECRET-BODY-TEXT"
FAKE_KEY = "sk-FAKE-KEY-7f3a9c0d1e2b"


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


class OpencodeStream(unittest.TestCase):
    """fleet-agent follows opencode's events as it does claude's."""

    def test_progress_comes_from_opencode_events(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        agent = load_agent(home)
        path = os.path.join(home, "stream.json")
        lines = [
            {"type": "system", "subtype": "init", "model": "MiniMax-M3"},
            {"type": "tool_use", "sessionID": "ses_1", "part": {"tool": "read", "state": {"input": {"filePath": "/a/b/secret.py"}}}},
            {"type": "step_finish", "sessionID": "ses_1", "part": {"messageID": "m1", "reason": "tool-calls"}},
            {"type": "text", "part": {"text": SECRET}},
        ]
        with open(path, "w") as handle:
            handle.write("".join(json.dumps(line) + "\n" for line in lines))
        report = agent.WorkerStreams().progress(path, time.time())
        self.assertEqual(report["model"], "MiniMax-M3")
        self.assertEqual(report["lastTool"], "read")
        self.assertEqual(report["lastFile"], "secret.py")
        self.assertEqual(report["sessionId"], "ses_1")
        self.assertEqual(report["steps"], 1)
        self.assertNotIn(SECRET, json.dumps(report))


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


def encode(rules):
    return base64.b64encode(json.dumps(rules).encode()).decode()


class DispatcherBase(Base):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.home, ".cache/mm-worker")
        self.rules_path = os.path.join(self.home, ".config/mm-worker/rules.json")
        self.no_claude()

    def no_claude(self):
        """A PATH and install folders with no claude in them, whatever this machine has."""
        empty = os.path.join(self.home, "empty-bin")
        os.makedirs(empty, exist_ok=True)
        self.agent.CLAUDE_DIRS = (empty,)
        patch = mock.patch.dict(os.environ, {"PATH": empty})
        patch.start()
        self.addCleanup(patch.stop)
        return empty

    def put(self, relative, text):
        path = os.path.join(self.home, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def dispatcher(self, now=None):
        return self.agent.Dispatcher().read(time.time() if now is None else now)

    def cli(self, *args):
        """Run the agent as the app does: the script on stdin, arguments after `-`."""
        with open(SCRIPT, "rb") as script:
            done = subprocess.run([sys.executable, "-I", "-u", "-"] + list(args), stdin=script,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env={"HOME": self.home, "PATH": os.environ["PATH"]}, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, b"")
        lines = done.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1, done.stdout)
        return json.loads(lines[0]), done.stdout.decode() + done.stderr.decode()


class DispatcherReport(DispatcherBase):
    def test_a_bare_machine_reports_defaults_and_nothing_configured(self):
        report = self.report()["dispatcher"]
        self.assertEqual(report["installed"], False)
        self.assertEqual(report["version"], 0)
        self.assertEqual(report["claudeInstalled"], False)
        self.assertEqual(report["rules"], self.agent.DEFAULT_RULES)
        self.assertEqual(report["rulesValid"], True)
        self.assertEqual(sorted(report["providers"]), ["claude", "minimax", "qwen", "zhipu"])
        for provider in report["providers"].values():
            self.assertEqual(provider, {"configured": False, "used": 0, "cooling": False, "coolingReason": ""})
        self.assertEqual(self.report()["agentVersion"], 5)

    def test_version_comes_from_reading_the_file(self):
        marker = os.path.join(self.home, "ran")
        self.put(".local/bin/mm-worker", "#!/usr/bin/env python3\nimport os\nopen(%r, 'w')\n"
                 "MM_WORKER_VERSION = 3  # bumped on changes\n" % marker)
        self.assertEqual((self.dispatcher()["installed"], self.dispatcher()["version"]), (True, 3))
        self.assertFalse(os.path.exists(marker))

    def test_the_shell_dispatcher_is_version_0(self):
        self.put(".local/bin/mm-worker", "#!/bin/bash\nMM_WORKER_VERSION_NOTE=1\necho hi\n")
        self.assertEqual((self.dispatcher()["installed"], self.dispatcher()["version"]), (True, 0))

    def test_only_the_top_of_the_file_is_read_for_the_version(self):
        self.put(".local/bin/mm-worker", "#" * 5000 + "\nMM_WORKER_VERSION = 2\n")
        self.assertEqual(self.dispatcher()["version"], 0)

    def configured(self, text):
        self.put(".config/minimax/env", text)
        return self.dispatcher()["providers"]["minimax"]["configured"]

    def test_env_files_are_parsed_like_a_shell_without_running_them(self):
        marker = os.path.join(self.home, "ran")
        cases = [
            ("MINIMAX_API_KEY=abc\n", True),
            ('export MINIMAX_API_KEY="abc def"\n', True),
            ("export  MINIMAX_API_KEY='abc'\r\n", True),
            ("MINIMAX_API_KEY=abc # note\n", True),
            ("MINIMAX_API_KEY=$(touch %s)\n" % marker, True),
            ("MINIMAX_API_KEY=\n", False),
            ('MINIMAX_API_KEY=""\n', False),
            ("export MINIMAX_API_KEY=''\n", False),
            ('MINIMAX_API_KEY="   "\n', False),
            ("MINIMAX_API_KEY= # nothing set\n", False),
            ("# MINIMAX_API_KEY=abc\n", False),
            ("OTHER_MINIMAX_API_KEY=abc\nMINIMAX_API_KEYS=abc\n", False),
            ("MINIMAX_API_KEY = abc\n", False),
            ('MINIMAX_API_KEY="abc\n', False),
            ("MINIMAX_API_KEY=abc\nMINIMAX_API_KEY=\n", False),
            ("export MINIMAX_API_KEY\n", False),
        ]
        for text, expected in cases:
            self.assertEqual(self.configured(text), expected, text)
        self.assertFalse(os.path.exists(marker))

    def test_each_provider_has_its_own_variable(self):
        self.put(".config/zhipu/env", "MINIMAX_API_KEY=abc\n")
        self.put(".config/qwen/env", "QWEN_API_KEY=abc\n")
        providers = self.dispatcher()["providers"]
        self.assertEqual((providers["zhipu"]["configured"], providers["qwen"]["configured"]), (False, True))

    def fake_claude(self, folder):
        path = os.path.join(folder, "claude")
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\ntouch %s\n" % os.path.join(self.home, "ran"))
        os.chmod(path, 0o755)

    def test_claude_is_found_on_path_or_in_the_usual_folders(self):
        on_path = os.path.join(self.home, "path-bin")
        os.makedirs(on_path)
        self.fake_claude(on_path)
        with mock.patch.dict(os.environ, {"PATH": on_path}):
            report = self.dispatcher()
        self.assertTrue(report["claudeInstalled"])
        self.assertTrue(report["providers"]["claude"]["configured"])
        usual = os.path.join(self.home, ".local/bin")
        os.makedirs(usual)
        self.fake_claude(usual)
        self.agent.CLAUDE_DIRS = (usual,)  # PATH is still the empty folder
        self.assertTrue(self.dispatcher()["claudeInstalled"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "ran")))

    def test_a_claude_that_cannot_run_does_not_count(self):
        folder = self.no_claude()
        self.put(os.path.join(folder, "claude"), "not executable")
        self.assertFalse(self.dispatcher()["claudeInstalled"])

    def slot(self, provider, name, pid=None, age=0):
        folder = os.path.join(self.state, "slots", provider, name)
        os.makedirs(folder)
        if pid is not None:
            with open(os.path.join(folder, "pid"), "w") as handle:
                handle.write("%s\n" % pid)
        then = time.time() - age
        os.utime(folder, (then, then))

    def test_used_counts_live_slots_and_new_ones_waiting_for_a_pid(self):
        self.slot("minimax", "1", pid=os.getpid())
        self.slot("minimax", "2", pid=999999)  # dead
        self.slot("minimax", "3")  # just taken
        self.slot("minimax", "4", age=60)  # abandoned before its pid
        self.slot("minimax", "5", pid=0)  # never a real pid
        self.slot("zhipu", "1", pid=os.getpid())
        providers = self.dispatcher()["providers"]
        self.assertEqual(providers["minimax"]["used"], 2)
        self.assertEqual(providers["zhipu"]["used"], 1)
        self.assertEqual(providers["qwen"]["used"], 0)

    def test_cooling_until_a_future_time_with_its_reason(self):
        self.put(".cache/mm-worker/minimax_unavailable_until", "%d\n" % (time.time() + 600))
        self.put(".cache/mm-worker/minimax_unavailable_reason", "rate limited:\n  try later\n")
        self.put(".cache/mm-worker/zhipu_unavailable_until", "%d\n" % (time.time() - 1))
        self.put(".cache/mm-worker/zhipu_unavailable_reason", "old news\n")
        self.put(".cache/mm-worker/qwen_unavailable_until", "soon\n")
        providers = self.dispatcher()["providers"]
        self.assertEqual((providers["minimax"]["cooling"], providers["minimax"]["coolingReason"]),
                         (True, "rate limited: try later"))
        self.assertEqual((providers["zhipu"]["cooling"], providers["zhipu"]["coolingReason"]), (False, ""))
        self.assertFalse(providers["qwen"]["cooling"])

    def test_cooling_ends_on_time_between_file_reads(self):
        self.put(".cache/mm-worker/claude_unavailable_until", "1000\n")
        dispatcher = self.agent.Dispatcher()
        self.assertTrue(dispatcher.read(995.0)["providers"]["claude"]["cooling"])
        self.assertFalse(dispatcher.read(1001.0)["providers"]["claude"]["cooling"])

    def test_valid_rules_are_reported(self):
        rules = copy.deepcopy(self.agent.DEFAULT_RULES)
        rules.update(revision=5, balance="priority")
        self.put(".config/mm-worker/rules.json", json.dumps(rules))
        report = self.dispatcher()
        self.assertEqual((report["rules"], report["rulesValid"]), (rules, True))

    def test_broken_or_invalid_rules_report_the_defaults(self):
        rules = copy.deepcopy(self.agent.DEFAULT_RULES)
        rules["providers"]["minimax"]["slots"] = 99
        for text in ("{not json", "[]", "", "\xff", "[" * 60000, "[" * 70000, json.dumps(rules), json.dumps(rules)[:-1]):
            with open(self.put(".config/mm-worker/rules.json", ""), "wb") as handle:
                handle.write(text.encode("latin-1"))
            report = self.report()["dispatcher"]
            self.assertEqual((report["rules"], report["rulesValid"]), (self.agent.DEFAULT_RULES, False), text[:20])

    def test_unreadable_rules_report_the_defaults(self):
        os.makedirs(self.rules_path)  # a folder where the file should be
        report = self.dispatcher()
        self.assertEqual((report["rules"], report["rulesValid"]), (self.agent.DEFAULT_RULES, False))

    def test_files_are_read_at_most_every_interval_but_slots_every_time(self):
        dispatcher = self.agent.Dispatcher()
        self.assertEqual(dispatcher.read(100.0)["rulesValid"], True)
        self.put(".config/mm-worker/rules.json", "broken")
        self.put(".config/minimax/env", "MINIMAX_API_KEY=abc\n")
        self.slot("minimax", "1", pid=os.getpid())
        report = dispatcher.read(105.0)
        self.assertEqual((report["rulesValid"], report["providers"]["minimax"]["configured"]), (True, False))
        self.assertEqual(report["providers"]["minimax"]["used"], 1)
        report = dispatcher.read(100.0 + self.agent.DISPATCHER_INTERVAL)
        self.assertEqual((report["rulesValid"], report["providers"]["minimax"]["configured"]), (False, True))

    def test_used_alone_is_not_news_but_other_dispatcher_changes_are(self):
        first = self.report()
        busier = copy.deepcopy(first)
        busier["dispatcher"]["providers"]["minimax"]["used"] = 3
        stable = self.agent._without_clock
        self.assertEqual(stable(first), stable(busier))
        broken = copy.deepcopy(first)
        broken["dispatcher"]["rulesValid"] = False
        self.assertNotEqual(stable(first), stable(broken))
        cooling = copy.deepcopy(first)
        cooling["dispatcher"]["providers"]["zhipu"]["cooling"] = True
        self.assertNotEqual(stable(first), stable(cooling))
        self.assertEqual(first["dispatcher"]["providers"]["minimax"]["used"], 0)  # not changed in place

    def test_once_from_stdin_prints_a_snapshot_with_the_dispatcher(self):
        report, _ = self.cli("--once")
        self.assertEqual(report["agentVersion"], 5)
        self.assertTrue(report["usageHistory"]["complete"])
        self.assertIsNone(report["claudeAccount"])
        self.assertEqual(report["dispatcher"]["rules"], self.agent.DEFAULT_RULES)
        self.assertIn("used", report["dispatcher"]["providers"]["minimax"])


class Rules(DispatcherBase):
    def rules(self, **changes):
        rules = copy.deepcopy(self.agent.DEFAULT_RULES)
        rules.update(changes)
        return rules

    def error(self, change):
        rules = copy.deepcopy(self.agent.DEFAULT_RULES)
        change(rules)
        return self.agent.rules_error(rules)

    def test_the_defaults_match_the_document_and_are_valid(self):
        with open(os.path.join(ROOT, "docs/FLEET-RULES.md")) as handle:
            block = re.search(r"## rules\.json\s+```json\n(.*?)```", handle.read(), re.DOTALL).group(1)
        self.assertEqual(self.agent.DEFAULT_RULES, json.loads(block))
        self.assertIsNone(self.agent.rules_error(self.agent.DEFAULT_RULES))

    @unittest.skipUnless(os.path.exists(os.path.join(ROOT, "Sources/Resources/mm-worker.py")), "no mm-worker.py here")
    def test_the_defaults_match_mm_worker(self):
        with open(os.path.join(ROOT, "Sources/Resources/mm-worker.py")) as handle:
            source = handle.read()
        # Read the literal rather than importing a script that may act on import.
        import ast
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DEFAULT_RULES" for t in node.targets):
                self.assertEqual(ast.literal_eval(node.value), self.agent.DEFAULT_RULES)
                return
        self.fail("mm-worker.py has no DEFAULT_RULES")

    def test_a_subset_of_providers_is_fine(self):
        def drop_qwen(rules):
            del rules["providers"]["qwen"]
        self.assertIsNone(self.error(drop_qwen))

    def test_every_rule_is_checked_and_named(self):
        def set_path(*path):
            *parents, last = path[:-1]
            value = path[-1]
            def change(rules):
                target = rules
                for key in parents:
                    target = target[key]
                target[last] = value
            return change

        def delete(*path):
            def change(rules):
                target = rules
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
            return change

        def without_zhipu(rules):
            del rules["providers"]["zhipu"]
            rules["order"].remove("zhipu")

        cases = [
            (set_path("version", 2), "version"),
            (set_path("version", True), "version"),
            (set_path("version", 1.0), "version"),
            (set_path("revision", -1), "revision"),
            (set_path("revision", False), "revision"),
            (set_path("balance", "random"), "balance"),
            (set_path("extra", 1), 'unknown key "extra" in the rules'),
            (delete("defaults"), "defaults is missing from the rules"),
            (set_path("providers", {}), "providers"),
            (set_path("providers", "openai", {"enabled": True, "slots": 1}), '"openai"'),
            (set_path("providers", "minimax", "slots", 0), "providers.minimax.slots"),
            (set_path("providers", "minimax", "slots", 33), "providers.minimax.slots"),
            (set_path("providers", "minimax", "slots", 4.0), "providers.minimax.slots"),
            (set_path("providers", "minimax", "slots", True), "providers.minimax.slots"),
            (set_path("providers", "minimax", "slots", "4"), "providers.minimax.slots"),
            (set_path("providers", "minimax", "enabled", 1), "providers.minimax.enabled"),
            (set_path("providers", "minimax", "speed", 1), 'unknown key "speed" in providers.minimax'),
            (delete("providers", "zhipu", "slots"), "slots is missing from providers.zhipu"),
            (set_path("providers", "zhipu", None), "providers.zhipu"),
            (set_path("order", ["minimax", "claude", "qwen"]), "order"),
            (set_path("order", ["minimax", "zhipu", "claude", "qwen", "qwen"]), "order"),
            (set_path("order", ["minimax", "zhipu", "zhipu", "claude", "qwen"]), "order"),
            (set_path("order", ["qwen", "minimax", "zhipu", "claude"]), None),
            (set_path("order", ["minimax", "zhipu", "claude", "openai"]), "order"),
            (set_path("order", []), "order"),
            (set_path("providers", "qwen", "enabled", True), "order"),  # enabled but not in order
            (set_path("order", "minimax"), "order"),
            (set_path("order", [1, 2, 3, 4]), "order"),
            (delete("levels", "hard"), "hard is missing from levels"),
            (set_path("levels", "expert", {"claude": "sonnet"}), 'unknown key "expert" in levels'),
            (set_path("levels", "hard", {}), "levels.hard"),
            (set_path("levels", "hard", ["claude"]), "levels.hard"),
            (set_path("levels", "hard", "claude", "m3"), "levels.hard.claude"),
            (set_path("levels", "simple", "zhipu", "glm"), "levels.simple.zhipu"),
            (set_path("levels", "hard", "qwen", "max"), None),  # disabled is still allowed
            (set_path("levels", "hard", "openai", "gpt"), 'levels.hard names "openai"'),
            (without_zhipu, 'levels.simple names "zhipu", which is not in providers'),
            (set_path("defaults", "timeoutSeconds", 29), "defaults.timeoutSeconds"),
            (set_path("defaults", "timeoutSeconds", 86401), "defaults.timeoutSeconds"),
            (set_path("defaults", "timeoutSeconds", True), "defaults.timeoutSeconds"),
            (set_path("defaults", "cooldownSeconds", -1), "defaults.cooldownSeconds"),
            (set_path("defaults", "cooldownSeconds", 0), None),
            (set_path("defaults", "retries", 1), 'unknown key "retries" in defaults'),
        ]
        for change, expected in cases:
            error = self.error(change)
            if expected is None:
                self.assertIsNone(error)
            else:
                self.assertIsNotNone(error, expected)
                self.assertIn(expected, error)
        self.assertEqual(self.agent.rules_error([]), "the rules must be a JSON object")

    def read_rules(self):
        with open(self.rules_path) as handle:
            return json.load(handle)

    def test_set_conflict_and_reset_through_stdin(self):
        rules = self.rules(balance="priority", revision=41)  # the sent revision is replaced
        self.assertEqual(self.cli("rules-set", encode(rules), "0")[0], {"ok": True, "revision": 1})
        self.assertEqual(self.read_rules(), dict(rules, revision=1))
        self.assertEqual(stat.S_IMODE(os.stat(self.rules_path).st_mode), 0o600)
        self.assertEqual(self.cli("rules-set", encode(rules), "0")[0],
                         {"ok": False, "error": "conflict", "revision": 1})
        self.assertEqual(self.cli("rules-reset", "1")[0], {"ok": True, "revision": 2})
        self.assertEqual(self.read_rules(), dict(self.agent.DEFAULT_RULES, revision=2))
        self.assertEqual(self.cli("rules-reset", "1")[0], {"ok": False, "error": "conflict", "revision": 2})
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.rules_path))), [".rules.lock", "rules.json"])
        report = self.dispatcher()
        self.assertEqual((report["rules"]["revision"], report["rulesValid"]), (2, True))

    def test_a_missing_folder_is_created(self):
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config")))
        self.assertTrue(self.agent.rules_reset("0")["ok"])
        self.assertTrue(os.path.isfile(self.rules_path))

    def test_bad_input_gets_an_answer_and_writes_nothing(self):
        good = encode(self.rules())
        cases = [
            ("rules-set", "!!!not base64", "0"),
            ("rules-set", "abc", "0"),  # bad padding
            ("rules-set", base64.b64encode(b"\xff\xfe").decode(), "0"),
            ("rules-set", base64.b64encode(b"{not json").decode(), "0"),
            ("rules-set", base64.b64encode(b"[" * 50000).decode(), "0"),
            ("rules-set", encode([1, 2]), "0"),
            ("rules-set", encode(self.rules(version=2)), "0"),
            ("rules-set", "", "0"),
            ("rules-set", good, "one"),
            ("rules-set", good, "-1"),
            ("rules-set", good, "1.5"),
            ("rules-set", good, ""),
            ("rules-set", good),
            ("rules-set", good, "0", "extra"),
            ("rules-reset", "x"),
            ("rules-reset",),
            ("rules-set",),
        ]
        for args in cases:
            answer, _ = self.cli(*args)
            self.assertIs(answer["ok"], False, args)
            self.assertIsInstance(answer["error"], str)
            self.assertNotIn("Traceback", answer["error"])
        self.assertFalse(os.path.exists(self.rules_path))

    def test_errors_name_the_field(self):
        rules = self.rules()
        rules["providers"]["claude"]["slots"] = True
        answer, _ = self.cli("rules-set", encode(rules), "0")
        self.assertEqual(answer, {"ok": False, "error": "providers.claude.slots must be a whole number from 1 to 32"})

    def test_a_corrupt_or_invalid_file_counts_as_revision_0(self):
        for text in ("garbage", json.dumps(self.rules(revision=7, balance="nope"))):
            self.put(".config/mm-worker/rules.json", text)
            self.assertEqual(self.agent.rules_set(encode(self.rules()), "0"), {"ok": True, "revision": 1})
            self.assertEqual(self.agent.rules_set(encode(self.rules()), "0")["error"], "conflict")

    def test_a_failed_write_is_an_answer(self):
        self.put(".config/mm-worker", "a file where the folder should be")
        answer, _ = self.cli("rules-reset", "0")
        self.assertIs(answer["ok"], False)
        self.assertIn("could not open the rules folder", answer["error"])

    def test_a_held_lock_is_waited_for(self):
        self.agent.LOCK_TIMEOUT = 0.2
        os.makedirs(os.path.dirname(self.rules_path))
        with open(self.agent.RULES_LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            answer = self.agent.rules_reset("0")
        self.assertEqual(answer["ok"], False)
        self.assertIn("still being written", answer["error"])
        self.assertFalse(os.path.exists(self.rules_path))

    def test_concurrent_writers_cannot_both_win(self):
        os.makedirs(os.path.dirname(self.rules_path))
        env = {"HOME": self.home, "PATH": os.environ["PATH"]}
        writers = []
        # Holding the lock lines every writer up behind it, so all of them
        # go for revision 0 at the same moment once it is let go.
        with open(self.agent.RULES_LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for index in range(8):
                rules = self.rules(defaults={"timeoutSeconds": 100 + index, "cooldownSeconds": 0})
                with open(SCRIPT, "rb") as script:
                    writers.append(subprocess.Popen(
                        [sys.executable, "-I", "-u", "-", "rules-set", encode(rules), "0"],
                        stdin=script, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env))
            time.sleep(1.0)
            self.assertEqual([w.poll() for w in writers], [None] * 8)  # all waiting
        answers = []
        for writer in writers:
            out, err = writer.communicate(timeout=30)
            self.assertEqual((writer.returncode, err), (0, b""))
            answers.append(json.loads(out))
        winners = [a for a in answers if a["ok"]]
        self.assertEqual(winners, [{"ok": True, "revision": 1}])
        self.assertEqual([a for a in answers if not a["ok"]],
                         [{"ok": False, "error": "conflict", "revision": 1}] * 7)
        self.assertEqual(self.read_rules()["revision"], 1)


class KeyPrivacy(DispatcherBase):
    def test_a_key_never_appears_in_any_output(self):
        self.put(".config/minimax/env", 'export MINIMAX_API_KEY="%s"\n' % FAKE_KEY)
        self.put(".config/zhipu/env", "ZHIPU_API_KEY=%s\n" % FAKE_KEY)
        self.put(".config/qwen/env", "QWEN_API_KEY='%s'\n" % FAKE_KEY)
        # A provider's error message can quote the key back.
        self.put(".cache/mm-worker/minimax_unavailable_until", "%d\n" % (time.time() + 600))
        self.put(".cache/mm-worker/minimax_unavailable_reason", "401: bad key %s\n" % FAKE_KEY)
        outputs = []

        report = self.report()
        outputs.append(json.dumps(report))
        self.assertTrue(report["dispatcher"]["providers"]["minimax"]["configured"])
        self.assertEqual(report["dispatcher"]["providers"]["minimax"]["coolingReason"], "401: bad key [hidden]")
        outputs.append(self.cli("--once")[1])

        pasted = self.rules_with_model(FAKE_KEY)
        for args in [("rules-set", encode(pasted), "0"),
                     ("rules-set", FAKE_KEY, "0"),
                     ("rules-set", encode(self.agent.DEFAULT_RULES), FAKE_KEY),
                     ("rules-reset", FAKE_KEY),
                     ("rules-set", FAKE_KEY)]:
            answer, text = self.cli(*args)
            self.assertIs(answer["ok"], False)
            outputs.append(text)

        # Failures: a folder that can't be made, then a conflict.
        blocker = self.put(".config/mm-worker", "not a folder")
        outputs.append(self.cli("rules-reset", "0")[1])
        os.remove(blocker)
        self.assertTrue(self.cli("rules-reset", "0")[0]["ok"])
        outputs.append(self.cli("rules-reset", "0")[1])
        outputs.append(self.cli("--once")[1])

        for text in outputs:
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn(FAKE_KEY[3:], text)
        with open(self.rules_path) as handle:
            self.assertNotIn(FAKE_KEY, handle.read())

    def test_rules_take_a_full_name_for_its_own_provider_only(self):
        self.write_dispatcher(3)
        rules = self.rules_with_model("MiniMax-M2.5")
        self.assertIs(self.cli("rules-set", encode(rules), "0")[0]["ok"], True)
        for bad in ("glm-5.1", "m3 ; rm -rf x", "MiniMax-" + "x" * 90):
            answer = self.cli("rules-set", encode(self.rules_with_model(bad)), "1")[0]
            self.assertIs(answer["ok"], False, bad)
            self.assertIn("levels.simple.minimax", answer["error"])

    def test_full_names_wait_for_a_dispatcher_that_takes_them(self):
        # The old one would call the file invalid and go back to its defaults.
        self.write_dispatcher(1)
        answer = self.cli("rules-set", encode(self.rules_with_model("MiniMax-M2.5")), "0")[0]
        self.assertIs(answer["ok"], False)
        self.assertIn("too old", answer["error"])
        self.assertFalse(os.path.exists(self.rules_path))
        self.assertIs(self.cli("rules-set", encode(self.rules_with_model("m27")), "0")[0]["ok"], True)

    def test_the_cli_rule_waits_for_a_dispatcher_and_for_opencode(self):
        rules = copy.deepcopy(self.agent.DEFAULT_RULES)
        rules["cli"] = "opencode"
        self.write_dispatcher(2)
        self.assertIn("too old", self.cli("rules-set", encode(rules), "0")[0]["error"])
        self.write_dispatcher(3)
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            if not self.agent.opencode_installed():
                answer = self.cli("rules-set", encode(rules), "0")[0]
                self.assertIs(answer["ok"], False)
        self.put(".local/bin/opencode", "#!/bin/sh\n")
        os.chmod(os.path.join(self.home, ".local/bin/opencode"), 0o755)
        self.assertIs(self.cli("rules-set", encode(rules), "0")[0]["ok"], True)
        self.assertEqual(self.cli("--once")[0]["dispatcher"]["rules"]["cli"], "opencode")
        rules["cli"] = "nope"
        self.assertIn("cli must be", self.cli("rules-set", encode(rules), "1")[0]["error"])

    def write_dispatcher(self, version):
        self.put(".local/bin/mm-worker", "#!/usr/bin/env python3\nMM_WORKER_VERSION = %d\n" % version)

    def rules_with_model(self, model):
        rules = copy.deepcopy(self.agent.DEFAULT_RULES)
        rules["levels"]["simple"]["minimax"] = model
        return rules


def days_ago(now, days, hour=12):
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday - days, hour, 0, 0, 0, 0, -1))


def day_of(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


class History(Base):
    def scanned(self, now=None):
        now = self.now if now is None else now
        log = self.agent.SessionLog()
        history = self.agent.UsageHistory()
        log.scan(now)
        history.finish(now)
        return log, history, self.agent.snapshot(log, now, history=history)

    def days(self):
        return self.scanned()[2]["usageHistory"]["days"]

    def test_a_past_day_is_in_days_and_not_in_today(self):
        old = days_ago(self.now, 3)
        self.write("s1.jsonl", assistant("old", old, 5), assistant("new", self.now, 7))
        report = self.scanned()[2]
        self.assertEqual(report["usageHistory"]["complete"], True)
        self.assertEqual(report["usageHistory"]["progress"], 1.0)
        self.assertEqual(report["usageHistory"]["days"],
                         {day_of(old): {"m1": {"input": 1, "output": 5, "cacheRead": 10, "cacheWrite": 100}}})
        self.assertEqual(report["todayByModel"]["m1"]["output"], 7)
        self.assertEqual(report["sessions"][0]["usage"]["output"], 12)  # sessions keep their meaning

    def test_one_message_in_two_files_counts_once_with_the_latest_usage(self):
        when = days_ago(self.now, 2)
        first = self.write("s1.jsonl", assistant("a", when, 5), assistant("b", when, 1))
        self.write("s2.jsonl", assistant("a", when + 10, 50, session="s2"))
        os.utime(first, (self.now - 3600, self.now - 3600))  # the resumed copy is newer
        self.assertEqual(self.days()[day_of(when)]["m1"],
                         {"input": 2, "output": 51, "cacheRead": 20, "cacheWrite": 200})

    def test_synthetic_and_unusable_lines_are_left_out(self):
        when = days_ago(self.now, 2)
        self.write("s1.jsonl", assistant("a", when, 5, model="<synthetic>"), '{"usage": nope', "[]",
                   json.dumps({"type": "user", "message": {"id": "u", "usage": {"output_tokens": 9}},
                               "timestamp": stamp(when)}))
        self.assertEqual(self.days(), {})

    def test_only_the_29_days_before_today_are_reported(self):
        self.write("s1.jsonl", *[assistant("d%d" % n, days_ago(self.now, n), n) for n in (1, 29, 30, 35)])
        self.assertEqual(sorted(self.days()), [day_of(days_ago(self.now, 29)), day_of(days_ago(self.now, 1))])

    def test_a_file_with_an_old_mtime_is_not_opened(self):
        stale = self.write("old.jsonl", assistant("a", days_ago(self.now, 5), 5))
        self.write("new.jsonl", assistant("b", days_ago(self.now, 5), 7))
        then = days_ago(self.now, 31, 0)
        os.utime(stale, (then, then))
        real_open = open
        opened = []

        def spy(path, *args, **kwargs):
            opened.append(path)
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", spy):
            days = self.days()
        self.assertNotIn(stale, opened)
        self.assertIn(os.path.join(self.project, "new.jsonl"), opened)
        self.assertEqual(days[day_of(days_ago(self.now, 5))]["m1"]["output"], 7)

    def test_each_pass_reads_at_most_the_limit(self):
        limit = 64 << 10
        self.agent.HISTORY_PASS_BYTES = limit
        for index in range(3):
            self.write("s%d.jsonl" % index, *[assistant("m%d-%d" % (index, n), days_ago(self.now, 1 + n % 20), 1,
                                                        padding="x" * 900) for n in range(700)])
        history = self.agent.UsageHistory()
        history.step(self.now)
        first = history.report(self.now)
        self.assertFalse(first["complete"])
        self.assertTrue(0 < first["progress"] < 1, first["progress"])
        self.assertEqual(history.read_bytes, limit)
        passes = 1
        while not history.complete:
            before = history.read_bytes
            history.step(self.now)
            self.assertLessEqual(history.read_bytes - before, limit)
            passes += 1
            self.assertLess(passes, 200)
        self.assertGreater(passes, 20)
        final = history.report(self.now)
        self.assertEqual((final["complete"], final["progress"]), (True, 1.0))
        self.assertEqual(final["days"], self.days())
        self.assertEqual(sum(m["m1"]["output"] for m in final["days"].values()), 2100)

    def test_partial_reports_are_sent_at_most_every_interval(self):
        self.agent.HISTORY_PASS_BYTES = 1 << 10
        self.write("s1.jsonl", *[assistant("m%d" % n, days_ago(self.now, 2), 1) for n in range(40)])
        history = self.agent.UsageHistory()
        history.step(self.now)
        self.assertFalse(history.news(self.now)["complete"])
        history.step(self.now + 2)
        self.assertIsNone(history.news(self.now + 2))
        self.assertFalse(history.news(self.now + 2 + self.agent.HISTORY_INTERVAL)["complete"])
        history.finish(self.now + 20)
        self.assertTrue(history.news(self.now + 20)["complete"])  # the finished one waits for nothing

    def test_history_is_sent_only_when_a_past_day_changes(self):
        self.agent.WALK_INTERVAL = 0
        past = days_ago(self.now, 2)
        self.write("s1.jsonl", assistant("a", past, 5))
        log, history, first = self.scanned()
        self.assertIn("usageHistory", first)
        self.assertNotIn("usageHistory", self.agent.snapshot(log, self.now, history=history))
        self.write("s1.jsonl", assistant("today", self.now, 9), assistant("a", past, 5))  # not past news
        history.step(self.now)
        self.assertNotIn("usageHistory", self.agent.snapshot(log, self.now, history=history))
        self.write("s2.jsonl", assistant("b", past, 6, session="s2"))
        history.step(self.now)
        changed = self.agent.snapshot(log, self.now, history=history)
        self.assertEqual(changed["usageHistory"]["days"][day_of(past)]["m1"]["output"], 11)
        self.assertNotIn("usageHistory", self.agent.snapshot(log, self.now, history=history))

    def test_without_clock_ignores_usage_history(self):
        report = self.scanned()[2]
        bare = dict(report)
        del bare["usageHistory"]
        self.assertEqual(self.agent._without_clock(report), self.agent._without_clock(bare))

    def test_the_heartbeat_does_not_repeat_usage_history(self):
        self.write("s1.jsonl", assistant("a", days_ago(self.now, 2), 5))
        clock = [time.time()]
        lines = []

        class Out:
            def write(self, text):
                if len(lines) >= 15:
                    raise BrokenPipeError
                lines.append(json.loads(text))

            def flush(self):
                pass

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(self.agent.time, "time", lambda: clock[0]), \
                mock.patch.object(self.agent.time, "sleep", sleep), \
                mock.patch.object(self.agent.os, "nice", lambda n: 0), \
                mock.patch.object(self.agent.sys, "stdout", Out()):
            self.assertEqual(self.agent.main(["watch"]), 0)
        sent = [line["usageHistory"] for line in lines if "usageHistory" in line]
        self.assertEqual([h["complete"] for h in sent], [False, True])
        self.assertEqual(sent[-1]["days"][day_of(days_ago(self.now, 2))]["m1"]["output"], 5)
        self.assertGreater(clock[0] - self.now, 60)  # many heartbeats went by

    def test_at_midnight_today_moves_into_days_once(self):
        midnight = self.agent.local_midnight(self.now)
        before, after = midnight - 120, midnight + 60
        self.write("s1.jsonl", assistant("late", before - 60, 5), assistant("early", after - 30, 7))
        log = self.agent.SessionLog()
        history = self.agent.UsageHistory()
        log.scan(before)
        history.finish(before)
        report = self.agent.snapshot(log, before, history=history)
        self.assertEqual(report["todayByModel"]["m1"]["output"], 12)
        self.assertNotIn(day_of(before), report["usageHistory"]["days"])
        log.scan(after)
        history.step(after)
        report = self.agent.snapshot(log, after, history=history)
        self.assertEqual(report["todayByModel"]["m1"]["output"], 7)
        self.assertEqual(report["usageHistory"]["days"],
                         {day_of(before): {"m1": {"input": 1, "output": 5, "cacheRead": 10, "cacheWrite": 100}}})

    def test_old_days_keep_totals_and_ignore_repeats(self):
        self.agent.WALK_INTERVAL = 0
        old = days_ago(self.now, 5)
        self.write("s1.jsonl", assistant("a", old, 5), assistant("b", days_ago(self.now, 1), 6))
        log, history, report = self.scanned()
        self.assertEqual(set(history.detail), {"b"})  # only yesterday and today keep detail
        self.write("s2.jsonl", assistant("a", old, 5, session="s2"))  # a resumed session's copy
        history.step(self.now)
        self.assertEqual(history.report(self.now)["days"], report["usageHistory"]["days"])

    def test_a_message_the_history_dates_earlier_is_not_today_too(self):
        # Seen today in today's file, but earlier in an older file.
        older = self.write("old.jsonl", assistant("a", days_ago(self.now, 2), 5))
        os.utime(older, (self.now - 2 * 86400, self.now - 2 * 86400))
        self.write("s1.jsonl", assistant("a", self.now, 5), assistant("b", self.now, 7))
        report = self.scanned()[2]
        self.assertEqual(report["todayByModel"]["m1"]["output"], 7)
        self.assertEqual(report["usageHistory"]["days"][day_of(days_ago(self.now, 2))]["m1"]["output"], 5)


class Account(DispatcherBase):
    def config(self, value):
        text = value if isinstance(value, str) else json.dumps(value)
        return self.put(".claude.json", text)

    def account(self):
        return self.report()["claudeAccount"]

    def test_email_plan_and_organization(self):
        self.config({"numStartups": 3, "oauthAccount": {
            "emailAddress": "a@example.com", "planDisplayName": "Claude Max", "organizationName": "Acme",
            "accountUuid": "u-1", "displayName": "A"}})
        self.assertEqual(self.account(), {"email": "a@example.com", "plan": "Claude Max", "organization": "Acme"})

    def test_fields_other_than_email_may_be_null(self):
        self.config({"oauthAccount": {"emailAddress": "a@example.com", "planDisplayName": 7}})
        self.assertEqual(self.account(), {"email": "a@example.com", "plan": None, "organization": None})

    def test_unknown_is_null(self):
        self.assertIsNone(self.account())  # no file
        for value in ({}, {"oauthAccount": None}, {"oauthAccount": {"planDisplayName": "Claude Max"}},
                      {"oauthAccount": {"emailAddress": ""}}, "{not json", "[]", "\xff", "[" * 100000):
            self.config(value)
            self.assertIsNone(self.account(), str(value)[:30])

    def test_an_oversized_file_is_skipped(self):
        self.assertEqual(self.agent.ACCOUNT_LIMIT, 32 << 20)
        self.agent.ACCOUNT_LIMIT = 200
        good = {"oauthAccount": {"emailAddress": "a@example.com"}}
        self.config(good)
        self.assertEqual(self.account()["email"], "a@example.com")
        self.config(dict(good, padding="x" * 300))
        self.assertIsNone(self.account())

    def test_read_at_most_every_interval(self):
        reader = self.agent.ClaudeAccount()
        self.assertIsNone(reader.read(100.0))
        self.config({"oauthAccount": {"emailAddress": "a@example.com"}})
        self.assertIsNone(reader.read(150.0))
        self.assertEqual(reader.read(100.0 + self.agent.ACCOUNT_INTERVAL)["email"], "a@example.com")

    def test_credentials_in_the_file_never_appear(self):
        self.config({"primaryApiKey": FAKE_KEY, "customApiKeyResponses": {"approved": [FAKE_KEY]},
                     "oauthAccount": {"emailAddress": "a@example.com", "accessToken": FAKE_KEY,
                                      "refreshToken": FAKE_KEY, "planDisplayName": "Claude Max"}})
        report = self.report()
        self.assertEqual(report["claudeAccount"]["email"], "a@example.com")
        for text in (json.dumps(report), self.cli("--once")[1]):
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn(FAKE_KEY[3:], text)

    def test_host_names_the_os_account(self):
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        self.assertEqual(self.report()["host"]["user"], user)
        self.assertEqual(self.cli("--once")[0]["host"]["user"], user)


if __name__ == "__main__":
    unittest.main()
