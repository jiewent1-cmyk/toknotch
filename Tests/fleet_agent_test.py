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
            json.dump({"result": SECRET, "usage": {"input_tokens": 3, "output_tokens": 4}}, handle)
        worker = self.report()["workers"][0]
        self.assertNotIn(SECRET, json.dumps(worker))
        self.assertEqual(worker["state"], "done")
        self.assertEqual(worker["usage"]["output"], 4)
        self.assertEqual(worker["provider"], "minimax")


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


class Timestamps(Base):
    def test_matches_calendar(self):
        import calendar
        for text in ("2026-11-01T07:25:36.123Z", "2024-02-29T00:00:00Z", "1999-12-31T23:59:59Z"):
            expect = calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
            self.assertEqual(self.agent.parse_timestamp(text), expect)


if __name__ == "__main__":
    unittest.main()
