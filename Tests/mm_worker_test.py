#!/usr/bin/env python3
"""Tests for Sources/Resources/mm-worker.py. Run: python3 Tests/mm_worker_test.py

Hermetic: each test gets a temporary HOME, a fake `claude` and a fake `ssh` on
PATH, and probes go to a local HTTP server. Nothing reaches the network or a
real model. The fake ssh runs commands locally against a second temporary home.
"""
import ast, copy, glob, http.server, importlib.util, json, os, re, shutil, signal, subprocess, sys
import tempfile, threading, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "Sources/Resources/mm-worker.py")
AGENT = os.path.join(ROOT, "Sources/Resources/fleet-agent.py")
RULES_DOC = os.path.join(ROOT, "docs/FLEET-RULES.md")
SYSTEM_PATH = "/usr/bin:/bin"
PY = sys.executable

# Loads the dispatcher, shortens its waits, then runs it. The command line still
# names mm-worker.py, which fleet-agent's cancel looks for.
LOADER = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("mm_worker", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for key, value in json.loads(sys.argv[2]).items():
    setattr(module, key, value)
sys.exit(module.main(sys.argv[3:]))
"""

FAKE_CLAUDE = r'''#!@PYTHON@
import glob, json, os, signal, subprocess, sys, time
env = os.environ
TOKENS = {"key-minimax": "minimax", "key-zhipu": "zhipu", "key-qwen": "qwen"}
if env.get("ANTHROPIC_BASE_URL"):
    p = TOKENS.get(env.get("ANTHROPIC_AUTH_TOKEN", ""), "unknown")
else:
    p = "claude"
prompt = sys.stdin.read()
mode = env.get("MODE_" + p, "ok")
logdir = env.get("FAKE_LOG")
if logdir:
    keep = ("ANTHROPIC_", "API_", "CLAUDE_", "MM_WORKER", "MINIMAX_", "ZHIPU_", "QWEN_")
    record = {"provider": p, "argv": sys.argv[1:], "prompt": prompt, "pid": os.getpid(),
              "cwd": os.getcwd(), "time": time.time(),
              "env": {k: v for k, v in env.items() if k.startswith(keep)}}
    with open(os.path.join(logdir, "%d-%f.json" % (os.getpid(), time.time())), "w") as f:
        json.dump(record, f)
mine = None
peak = env.get("FAKE_PEAK")
if peak:
    os.makedirs(os.path.join(peak, p), exist_ok=True)
    mine = os.path.join(peak, p, str(os.getpid()))
    open(mine, "w").close()
    with open(os.path.join(peak, p + ".log"), "a") as f:
        f.write("%d\n" % len(os.listdir(os.path.join(peak, p))))

def ev(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()

ev({"type": "system", "subtype": "init", "session_id": "sess-" + p, "model": "model-" + p})
ev({"type": "assistant", "session_id": "sess-" + p, "message": {"id": "m1", "model": "model-" + p,
    "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/y/foo.swift"}}],
    "usage": {"input_tokens": 0, "output_tokens": 0}}})
ev({"type": "user", "message": {"content": "tool output says usage limit exceeded quota insufficient"}})
if mode == "slow":
    time.sleep(float(env.get("FAKE_SLOW", "30")))
elif mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(300)
elif mode == "die":
    sys.exit(1)
elif mode == "nofinal":
    sys.exit(0)
elif mode == "quota":
    ev({"type": "result", "subtype": "error", "is_error": True, "api_error_status": 429,
        "result": "API Error: 429 usage limit reached (2056)", "usage": {"input_tokens": 1, "output_tokens": 0}})
    sys.exit(1)
elif mode == "apierror":
    ev({"type": "result", "subtype": "error", "is_error": True, "api_error_status": None,
        "result": "API Error: 500 internal server error", "usage": {"input_tokens": 1, "output_tokens": 0}})
    sys.exit(1)
elif mode == "error":
    ev({"type": "result", "subtype": "error", "is_error": True, "api_error_status": None,
        "result": "ERROR-from-" + p, "usage": {"input_tokens": 1, "output_tokens": 1}})
    sys.exit(1)
elif mode == "bg":
    subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakesleep"), "300"])
elif mode == "selfcancel":
    runs = os.path.join(env["HOME"], ".cache/mm-worker/runs")
    run = max(glob.glob(os.path.join(runs, "*-%d.prompt.md" % os.getppid())))
    open(run[:-len(".prompt.md")] + ".cancelled", "w").close()
    sys.exit(1)
time.sleep(float(env.get("FAKE_SLEEP", "0")))
ev({"type": "result", "subtype": "success", "is_error": False, "api_error_status": None, "num_turns": 2,
    "result": "RESULT-from-" + p, "session_id": "sess-" + p,
    "usage": {"input_tokens": 100, "output_tokens": 7, "cache_read_input_tokens": 50}})
if mine:
    os.unlink(mine)
'''

FAKE_SLEEP = "#!@PYTHON@\nimport sys, time\ntime.sleep(float(sys.argv[1]))\n"

FAKE_SSH = r'''#!/bin/bash
while [ $# -gt 0 ]; do
  case $1 in -o|-p|-i|-l) shift 2 ;; -*) shift ;; *) break ;; esac
done
host=$1; shift
printf '%s\n' "$*" >> "$FAKE_SSH_LOG"
[ -e "$FAKE_SSH_OFFLINE" ] && exit 255
cd "$FAKE_REMOTE_HOME" || exit 255
if [ -e "$FAKE_SSH_FLAKY" ] && [[ "$*" == *setsid* ]]; then
  rm -f "$FAKE_SSH_FLAKY"
  HOME="$FAKE_REMOTE_HOME" bash -c "$*"
  exit 255
fi
HOME="$FAKE_REMOTE_HOME" exec bash -c "$*"
'''

FAKE_REMOTE_RUN = r'''#!/bin/bash
provider=$1 timeout_s=$2 workdir=$3; shift 3
export MM_WORKER_ACTIVE=1
unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN
case $provider in
  minimax) export ANTHROPIC_BASE_URL=fake://remote/anthropic ANTHROPIC_AUTH_TOKEN=key-minimax ;;
  zhipu) export ANTHROPIC_BASE_URL=fake://remote ANTHROPIC_AUTH_TOKEN=key-zhipu ;;
  qwen) export ANTHROPIC_BASE_URL=fake://remote ANTHROPIC_AUTH_TOKEN=key-qwen ;;
  claude) ;;
  *) exit 2 ;;
esac
cd "$workdir" || exit 2
exec timeout -k 15 "$timeout_s" "$FAKE_BIN/claude" "$@"
'''


def load_worker():
    spec = importlib.util.spec_from_file_location("mm_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MW = load_worker()


class ProbeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        http.server.ThreadingHTTPServer.__init__(self, ("127.0.0.1", 0), ProbeHandler)
        self.behaviour = {}
        self.requests = []
        self.guard = threading.Lock()


class ProbeHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        key = self.headers.get("x-api-key", "")
        server = self.server
        with server.guard:
            server.requests.append({"path": self.path, "key": key, "body": json.loads(body or b"{}"),
                                    "version": self.headers.get("anthropic-version")})
            answers = server.behaviour.get(key) or [(200, '{"type": "message"}')]
            code, text = answers.pop(0) if len(answers) > 1 else answers[0]
        data = text.replace("@KEY@", key).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


SERVER = None


def setUpModule():
    global SERVER
    for name in ("claude",):
        if shutil.which(name, path=SYSTEM_PATH):
            raise RuntimeError("a real %s is on %s; refusing to run" % (name, SYSTEM_PATH))
    SERVER = ProbeServer()
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def tearDownModule():
    SERVER.shutdown()
    SERVER.server_close()


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    if mode:
        os.chmod(path, mode)


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="mmw-"))
        self.home = os.path.join(self.root, "home")
        self.bin = os.path.join(self.root, "bin")
        self.sshbin = os.path.join(self.root, "sshbin")
        self.log = os.path.join(self.root, "log")
        self.remote = os.path.join(self.root, "remote")
        self.work = os.path.join(self.home, "proj")
        self.state = os.path.join(self.home, ".cache/mm-worker")
        for folder in (self.log, self.work, os.path.join(self.remote, "proj")):
            os.makedirs(folder)
        write(os.path.join(self.bin, "claude"), FAKE_CLAUDE.replace("@PYTHON@", PY), 0o755)
        write(os.path.join(self.bin, "fakesleep"), FAKE_SLEEP.replace("@PYTHON@", PY), 0o755)
        write(os.path.join(self.bin, "ssh"), FAKE_SSH, 0o755)
        write(os.path.join(self.sshbin, "ssh"), FAKE_SSH, 0o755)
        write(os.path.join(self.remote, ".local/bin/mm-remote-run"), FAKE_REMOTE_RUN, 0o755)
        base = "http://127.0.0.1:%d" % SERVER.server_address[1]
        write(os.path.join(self.home, ".config/minimax/env"),
              'export MINIMAX_API_HOST="%s/mm"\nMINIMAX_API_KEY=\'key-minimax\'  # the key\n' % base)
        write(os.path.join(self.home, ".config/zhipu/env"),
              "# Zhipu\nZHIPU_API_HOST=%s/zp\nexport ZHIPU_API_KEY=\"key-zhipu\"\n" % base)
        write(os.path.join(self.home, ".config/qwen/env"), "QWEN_API_HOST=%s/qw\nQWEN_API_KEY=key-qwen\n" % base)
        SERVER.behaviour = {}
        SERVER.requests = []
        self.env = {
            "HOME": self.home, "PATH": self.bin + ":" + SYSTEM_PATH, "PWD": self.work, "LANG": "C.UTF-8",
            "no_proxy": "*", "NO_PROXY": "*", "FAKE_LOG": self.log, "FAKE_BIN": self.bin,
            "FAKE_REMOTE_HOME": self.remote, "FAKE_SSH_LOG": os.path.join(self.root, "ssh.log"),
            "FAKE_SSH_OFFLINE": os.path.join(self.root, "offline"),
            "FAKE_SSH_FLAKY": os.path.join(self.root, "flaky"), "MM_WORKER_POLL": "0.3",
        }
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for pid in self.leftovers():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    # --- running things

    def command(self, args, patch):
        if patch:
            return [PY, "-c", LOADER, WORKER, json.dumps(patch)] + list(args)
        return [PY, WORKER] + list(args)

    def environment(self, extra):
        env = dict(self.env)
        for key, value in (extra or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def mw(self, *args, env=None, input=None, patch=None, timeout=90):
        return subprocess.run(self.command(args, patch), env=self.environment(env), cwd=self.work,
                              input=(input or "").encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)

    def start(self, *args, env=None, patch=None):
        tag = "p%d" % len(self.procs)
        out = open(os.path.join(self.root, tag + ".out"), "wb")
        err = open(os.path.join(self.root, tag + ".err"), "wb")
        proc = subprocess.Popen(self.command(args, patch), env=self.environment(env), cwd=self.work,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        out.close()
        err.close()
        proc.out_path, proc.err_path = out.name, err.name
        self.procs.append(proc)
        return proc

    def finish(self, proc, timeout=60):
        proc.wait(timeout=timeout)
        return proc.returncode, read(proc.out_path), read(proc.err_path)

    def ag(self, *args):
        with open(AGENT, "rb") as source:
            done = subprocess.run([PY, "-I", "-u", "-"] + list(args), stdin=source, stdout=subprocess.PIPE,
                                  env={"HOME": self.home, "PATH": SYSTEM_PATH}, timeout=30)
        return done.stdout.decode()

    def snapshot(self):
        return json.loads(self.ag("--once"))

    def running_worker(self, need_steps=True):
        def find():
            for worker in self.snapshot()["workers"]:
                if worker["state"] == "running" and (worker.get("steps") or not need_steps):
                    return worker
            return None
        return self.wait_for(find, 15, "a running worker")

    # --- looking at the results

    def wait_for(self, check, timeout=10, what="a condition"):
        end = time.time() + timeout
        while time.time() < end:
            value = check()
            if value:
                return value
            time.sleep(0.1)
        self.fail("timed out waiting for " + what)

    def records(self, provider=None):
        found = []
        for path in glob.glob(os.path.join(self.log, "*.json")):
            try:
                found.append(json.loads(read(path)))
            except ValueError:
                continue
        found.sort(key=lambda record: record["time"])
        return [record for record in found if provider in (None, record["provider"])]

    def ledger(self, count=1):
        lines = read(os.path.join(self.state, "ledger.tsv")).splitlines()[-count:]
        return [" ".join(line.split("\t")[i] for i in (1, 3, 5, 6)) for line in lines]

    def leftovers(self):
        done = subprocess.run(["pgrep", "-f", self.root], stdout=subprocess.PIPE)
        return [int(pid) for pid in done.stdout.split() if int(pid) != os.getpid()]

    def assert_no_leftovers(self):
        end = time.time() + 1.0
        while self.leftovers() and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(self.leftovers(), [], "processes outlived the dispatcher")

    def live_slots(self, provider=None):
        return glob.glob(os.path.join(self.state, "slots", provider or "*", "*"))

    def cooling_files(self, provider="*"):
        return glob.glob(os.path.join(self.state, provider + "_unavailable_until"))

    def until(self, provider):
        return int(read(os.path.join(self.state, provider + "_unavailable_until"))) - time.time()

    def rules(self, change):
        rules = copy.deepcopy(MW.DEFAULT_RULES)
        change(rules)
        write(os.path.join(self.home, ".config/mm-worker/rules.json"), json.dumps(rules))

    def occupy(self, provider, count):
        for index in range(1, count + 1):
            write(os.path.join(self.state, "slots", provider, str(index), "pid"), "%d\n" % os.getpid())


# ---------- the scenarios of the old end-to-end script ----------

class EndToEnd(Base):
    def test_1_normal_run_with_limit_words_in_the_stream(self):
        started = time.time()
        done = self.mw("-l", "normal", "-b", "minimax", "-n", "demo", "hello")
        self.assertEqual(done.stdout.decode(), "RESULT-from-minimax\n")
        self.assertEqual(done.returncode, 0)
        self.assertLess(time.time() - started, 5, "returned only after a watchdog")
        self.assertEqual(self.ledger(), ["minimax ok in=150 out=7"])
        self.assertEqual(self.cooling_files(), [])
        stream = glob.glob(os.path.join(self.state, "runs/*.minimax.json"))[0]
        self.assertEqual(len(read(stream).splitlines()), 1)
        self.assert_no_leftovers()

    def test_2_json_prints_one_result_line(self):
        done = self.mw("-l", "normal", "-b", "zhipu", "--json", "hi")
        lines = done.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["type"], "result")

    def test_3_a_crash_is_not_a_success_and_fails_over(self):
        done = self.mw("-l", "normal", "x", env={"MODE_minimax": "die"})
        self.assertEqual(done.stdout.decode(), "RESULT-from-zhipu\n")
        self.assertEqual(done.returncode, 0)
        first, second = self.ledger(2)
        self.assertTrue(first.startswith("minimax "))
        self.assertNotEqual(first.split()[1], "ok")
        self.assertTrue(second.startswith("zhipu ok"))
        self.assertEqual(done.stderr.decode().count("trying the next provider"), 1)

    def test_4_only_a_real_quota_error_cools_down(self):
        done = self.mw("-l", "normal", "x", env={"MODE_minimax": "quota"})
        self.assertEqual(done.returncode, 0)
        self.assertEqual(len(self.cooling_files("minimax")), 1)
        self.assertGreater(self.until("minimax"), 1700)

    def test_5_progress_is_visible_and_the_title_comes_from_n(self):
        proc = self.start("-l", "normal", "-b", "minimax", "-n", "我的任务", "-t", "60", "body-secret",
                          env={"MODE_minimax": "slow"})
        self.running_worker()
        text = self.ag("--once")
        worker = [w for w in json.loads(text)["workers"] if w["state"] == "running"][0]
        self.assertEqual((worker["title"], worker["provider"], worker.get("steps"), worker.get("lastTool"),
                          worker.get("lastFile"), worker.get("model"), worker.get("sessionId")),
                         ("我的任务", "minimax", 1, "Read", "foo.swift", "model-minimax", "sess-minimax"))
        self.assertIn("idleSeconds", worker)
        self.assertNotIn("body-secret", text)
        proc.kill()

    def test_6_cancelling_a_cheap_worker(self):
        proc = self.start("-l", "normal", "-b", "minimax", "-t", "60", "x", env={"MODE_minimax": "slow"})
        worker = self.running_worker()
        self.assertEqual(self.ag("cancel", worker["id"]).strip(), '{"ok": true}')
        rc, out, err = self.finish(proc, 10)
        self.assertEqual(rc, 130)
        self.assertEqual(out, "")
        self.assert_no_leftovers()
        self.assertEqual(self.live_slots("minimax"), [])
        self.assertEqual(self.ledger(), ["minimax cancelled in=0 out=0"])

    def test_7_cancelling_a_claude_worker(self):
        proc = self.start("-l", "hard", "-t", "60", "x", env={"MODE_claude": "slow"})
        worker = self.running_worker()
        self.assertEqual(self.ag("cancel", worker["id"]).strip(), '{"ok": true}')
        rc, _, err = self.finish(proc, 10)
        self.assertEqual(rc, 130)
        self.assertNotIn("trying the next provider", err)
        self.assert_no_leftovers()

    def test_8_cancelling_starts_no_other_provider(self):
        proc = self.start("-l", "normal", "-t", "60", "x",
                          env={"MODE_minimax": "slow", "MODE_zhipu": "slow", "MODE_claude": "slow"})
        worker = self.running_worker()
        self.ag("cancel", worker["id"])
        rc, _, _ = self.finish(proc, 10)
        self.assertEqual(rc, 130)
        time.sleep(0.5)
        self.assertEqual(len(self.records()), 1)
        self.assert_no_leftovers()

    def test_9_timeout_is_124(self):
        started = time.time()
        done = self.mw("-l", "normal", "-b", "zhipu", "-t", "2", "x", env={"MODE_zhipu": "slow"})
        self.assertEqual(done.returncode, 124)
        self.assertEqual(done.stdout, b"")
        self.assertLess(time.time() - started, 10)
        self.assertEqual(self.ledger(), ["zhipu timeout in=0 out=0"])
        self.assertIn("too big", done.stderr.decode())
        self.assert_no_leftovers()


# ---------- processes ----------

class Processes(Base):
    def test_a_finished_worker_leaves_nothing_behind_and_the_pipe_closes(self):
        started = time.time()
        done = self.mw("-b", "minimax", "x", env={"MODE_minimax": "bg"}, timeout=20)
        self.assertLess(time.time() - started, 5)
        self.assertEqual(done.stdout.decode(), "RESULT-from-minimax\n")
        self.assert_no_leftovers()

    def test_timeout_escalates_to_kill(self):
        started = time.time()
        done = self.mw("-b", "zhipu", "-t", "1", "x", env={"MODE_zhipu": "stubborn"},
                       patch={"KILL_GRACE": 1})
        self.assertEqual(done.returncode, 124)
        self.assertLess(time.time() - started, 10)
        self.assert_no_leftovers()

    def test_the_real_grace_periods(self):
        self.assertEqual((MW.KILL_GRACE, MW.QUEUE_SLEEP, MW.LOCK_STALE, MW.SLOT_GRACE, MW.RETRY_SLEEP,
                          MW.SUBMIT_GIVE_UP), (15, 5, 30, 5, 30, 600))

    def test_signals_stop_the_worker_and_free_the_slot(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=sig):
                proc = self.start("-b", "minimax", "x", env={"MODE_minimax": "slow"})
                self.wait_for(lambda: self.records("minimax"), 10, "the worker")
                count = len(self.records())
                proc.send_signal(sig)
                rc, out, _ = self.finish(proc, 10)
                self.assertEqual(rc, 130)
                self.assertEqual(out, "")
                self.assert_no_leftovers()
                self.assertEqual(self.live_slots(), [])
                self.assertEqual(self.ledger(), ["minimax interrupted in=0 out=0"])
                self.assertEqual(len(self.records()), count)
                for path in glob.glob(os.path.join(self.log, "*")):
                    os.unlink(path)

    def test_a_cancel_marker_after_the_run_stops_failover(self):
        done = self.mw("x", env={"MODE_minimax": "selfcancel"})
        self.assertEqual(done.returncode, 130)
        self.assertEqual(len(self.records()), 1)
        self.assertIn("cancelled", done.stderr.decode())

    def test_slots_hold_under_concurrency(self):
        peak = os.path.join(self.root, "peak")
        os.makedirs(peak)
        env = {"FAKE_PEAK": peak, "FAKE_SLEEP": "4"}
        procs = [self.start("x%d" % i, env=env, patch={"QUEUE_SLEEP": 0.5}) for i in range(20)]
        results = [self.finish(proc, 90) for proc in procs]
        self.assertEqual([rc for rc, _, _ in results], [0] * 20)
        self.assertTrue(all(out.startswith("RESULT-from-") for _, out, _ in results))
        peaks = {}
        for provider in ("minimax", "zhipu", "claude"):
            counts = [int(line) for line in read(os.path.join(peak, provider + ".log")).split()]
            peaks[provider] = max(counts)
        self.assertEqual(peaks, {"minimax": 8, "zhipu": 4, "claude": 4})
        self.assertEqual(len(self.records()), 20)
        self.assertEqual(self.live_slots(), [])
        self.assert_no_leftovers()

    def test_a_held_slot_makes_others_queue(self):
        self.occupy("zhipu", 4)
        proc = self.start("-b", "zhipu", "x", patch={"QUEUE_SLEEP": 0.2})
        time.sleep(1.5)
        self.assertIsNone(proc.poll())
        self.assertIn("all slots are busy", read(proc.err_path))
        shutil.rmtree(os.path.join(self.state, "slots/zhipu/2"))
        rc, out, _ = self.finish(proc, 10)
        self.assertEqual((rc, out), (0, "RESULT-from-zhipu\n"))

    def test_dead_slots_are_reaped_and_a_stale_lock_is_broken(self):
        write(os.path.join(self.state, "slots/minimax/1/pid"), "999999\n")
        os.makedirs(os.path.join(self.state, "select.lock"))
        old = time.time() - 60
        os.utime(os.path.join(self.state, "select.lock"), (old, old))
        done = self.mw("-b", "minimax", "x")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.live_slots(), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "select.lock")))

    def test_runs_directly_from_its_shebang(self):
        if not shutil.which("python3", path=SYSTEM_PATH):
            self.skipTest("no python3 on " + SYSTEM_PATH)
        copy_path = os.path.join(self.root, "inst", "mm-worker")
        os.makedirs(os.path.dirname(copy_path))
        shutil.copy(WORKER, copy_path)
        os.chmod(copy_path, 0o755)
        done = subprocess.run([copy_path, "-b", "claude", "x"], env=self.environment(None), cwd=self.work,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(done.stdout.decode(), "RESULT-from-claude\n")


# ---------- failover, classification and stdout ----------

class Failover(Base):
    def test_stdout_holds_only_the_final_result(self):
        for mode in ("die", "quota", "apierror", "nofinal"):
            with self.subTest(mode=mode):
                for path in self.cooling_files():
                    os.unlink(path)
                done = self.mw("x", env={"MODE_minimax": mode})
                self.assertEqual(done.stdout.decode(), "RESULT-from-zhipu\n")
                self.assertEqual(done.returncode, 0)
                done = self.mw("--json", "x", env={"MODE_minimax": mode})
                lines = done.stdout.decode().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertEqual(json.loads(lines[0])["result"], "RESULT-from-zhipu")

    def test_failed_attempt_text_goes_to_stderr(self):
        done = self.mw("x", env={"MODE_minimax": "apierror"})
        self.assertIn("API Error: 500", done.stderr.decode())
        self.assertNotIn("API Error", done.stdout.decode())
        self.assertEqual(self.cooling_files(), [], "a 500 is not a quota error")

    def test_the_note_is_prepended_once(self):
        done = self.mw("do it", env={"MODE_minimax": "die", "MODE_zhipu": "die"})
        self.assertEqual(done.stdout.decode(), "RESULT-from-claude\n")
        prompts = [record["prompt"] for record in self.records()]
        self.assertEqual(prompts[0], "do it\n")
        self.assertTrue(prompts[1].startswith(MW.FAILOVER_NOTE))
        self.assertEqual(prompts[2], MW.FAILOVER_NOTE + "do it\n")

    def test_a_forced_backend_does_not_fail_over(self):
        done = self.mw("-b", "minimax", "x", env={"MODE_minimax": "die"})
        self.assertEqual(done.returncode, 75)
        self.assertEqual(done.stdout, b"")
        self.assertEqual(len(self.records()), 1)

    def test_an_error_result_without_api_status_is_a_task_failure(self):
        done = self.mw("x", env={"MODE_minimax": "error"})
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.decode(), "ERROR-from-minimax\n")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.ledger(), ["minimax error in=1 out=1"])

    def test_claude_without_a_final_result_fails(self):
        done = self.mw("-b", "claude", "x", env={"MODE_claude": "nofinal"})
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, b"")
        self.assertEqual(self.ledger(), ["claude fail(1) in=0 out=0"])

    def test_a_429_result_without_quota_words_cools_briefly(self):
        line = {"type": "result", "is_error": True, "api_error_status": 429, "result": "slow down"}
        out = os.path.join(self.root, "r.json")
        write(out, json.dumps(line) + "\n")
        info = MW.inspect(out)
        self.assertEqual((info["result"], info["error"], info["status"]), (True, True, "429"))
        self.assertIsNone(MW.QUOTA_RE.search(info["line"]))

    def test_api_error_status_from_the_text(self):
        out = os.path.join(self.root, "r.json")
        write(out, "noise\n" + json.dumps({"type": "result", "is_error": True, "result": "API Error: 402 pay"}))
        self.assertEqual(MW.inspect(out)["status"], "402")
        write(out, json.dumps({"type": "assistant"}) + "\n")
        self.assertFalse(MW.inspect(out)["result"])
        write(out, "")
        self.assertFalse(MW.inspect(out)["result"])

    def test_timeout_with_spent_quota_fails_over(self):
        SERVER.behaviour = {"key-zhipu": [(200, "{}"), (402, '{"error": "insufficient balance"}')]}
        self.rules(lambda r: r.update(balance="priority", order=["zhipu", "claude", "minimax", "qwen"]))
        done = self.mw("-t", "2", "x", env={"MODE_zhipu": "slow"})
        self.assertEqual(done.stdout.decode(), "RESULT-from-claude\n")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(len(self.cooling_files("zhipu")), 1)
        self.assertEqual(self.ledger(2), ["zhipu timeout in=0 out=0", "claude ok in=150 out=7"])


# ---------- probes ----------

class Probes(Base):
    def reason(self, provider):
        return read(os.path.join(self.state, provider + "_unavailable_reason")).strip()

    def test_one_probe_per_provider_with_the_right_request(self):
        self.mw("-b", "minimax", "x")
        self.assertEqual(len(SERVER.requests), 1)
        request = SERVER.requests[0]
        self.assertEqual(request["path"], "/mm/anthropic/v1/messages")
        self.assertEqual(request["key"], "key-minimax")
        self.assertEqual(request["version"], "2023-06-01")
        self.assertEqual(request["body"]["model"], "MiniMax-M2.7-highspeed")
        self.assertEqual(request["body"]["max_tokens"], 1)
        SERVER.requests = []
        self.mw("-b", "zhipu", "x")
        self.assertEqual([(r["path"], r["body"]["model"]) for r in SERVER.requests],
                         [("/zp/v1/messages", "glm-5.3-flash")])
        SERVER.requests = []
        self.mw("-b", "claude", "x")
        self.assertEqual(SERVER.requests, [])

    def test_classification(self):
        cases = [
            ((402, "{}"), 1800, "quota used up (HTTP 402)"),
            ((200, '{"base_resp": {"status_code":1008, "status_msg": "insufficient balance"}}'), 1800,
             "quota used up (HTTP 200)"),
            ((429, "{}"), 120, "rate limited (HTTP 429)"),
            ((401, "{}"), 600, "authentication failed (HTTP 401), check ~/.config/minimax/env"),
            ((403, "{}"), 600, "authentication failed (HTTP 403)"),
            ((500, '{"error": "bad key @KEY@"}'), 300, "probe failed (HTTP 500)"),
        ]
        for answer, seconds, reason in cases:
            with self.subTest(answer=answer):
                for path in self.cooling_files():
                    os.unlink(path)
                SERVER.behaviour = {"key-minimax": [answer]}
                done = self.mw("x")
                self.assertEqual(done.stdout.decode(), "RESULT-from-zhipu\n")
                self.assertAlmostEqual(self.until("minimax"), seconds, delta=10)
                self.assertTrue(self.reason("minimax").startswith(reason), self.reason("minimax"))
                self.assertNotIn("key-minimax", done.stderr.decode())
                self.assertNotIn("key-minimax", self.reason("minimax"))

    def test_an_unreachable_host_is_a_probe_failure(self):
        write(os.path.join(self.home, ".config/minimax/env"),
              "MINIMAX_API_HOST=http://127.0.0.1:9\nMINIMAX_API_KEY=key-minimax\n")
        done = self.mw("x")
        self.assertEqual(done.stdout.decode(), "RESULT-from-zhipu\n")
        self.assertTrue(self.reason("minimax").startswith("probe failed (HTTP none)"))
        self.assertNotIn("key-minimax", done.stderr.decode())

    def test_a_cooling_provider_is_skipped_without_a_probe(self):
        write(os.path.join(self.state, "minimax_unavailable_until"), "%d\n" % (time.time() + 600))
        done = self.mw("x")
        self.assertEqual(done.stdout.decode(), "RESULT-from-zhipu\n")
        self.assertEqual([r["key"] for r in SERVER.requests], ["key-zhipu"])

    def test_a_good_probe_clears_old_cooling(self):
        write(os.path.join(self.state, "minimax_unavailable_until"), "%d\n" % (time.time() - 5))
        self.mw("-b", "minimax", "x")
        self.assertEqual(self.cooling_files(), [])

    def test_all_cooling_is_a_failure(self):
        for provider in ("minimax", "zhipu"):
            write(os.path.join(self.state, provider + "_unavailable_until"), "%d\n" % (time.time() + 600))
        self.assertEqual(self.mw("-b", "minimax", "x").returncode, 1)
        done = self.mw("x")
        self.assertEqual(done.stdout.decode(), "RESULT-from-claude\n")


# ---------- rules ----------

class RulesValidation(unittest.TestCase):
    def check(self, change):
        rules = copy.deepcopy(MW.DEFAULT_RULES)
        change(rules)
        return MW.validate_rules(rules)

    def test_defaults_are_valid(self):
        self.assertIsNone(MW.validate_rules(copy.deepcopy(MW.DEFAULT_RULES)))

    def test_valid_variations(self):
        def without_qwen(r):
            del r["providers"]["qwen"]
        cases = [
            without_qwen,
            lambda r: r.update(balance="priority", revision=7),
            lambda r: r.update(order=["qwen", "claude", "zhipu", "minimax"]),
            lambda r: r["levels"].update(hard={"qwen": "glm", "claude": "claude-opus-5-5"}),
            lambda r: r["providers"]["minimax"].update(slots=32, enabled=False),
            lambda r: r["defaults"].update(timeoutSeconds=30, cooldownSeconds=0),
            lambda r: r["defaults"].update(timeoutSeconds=86400, cooldownSeconds=86400),
        ]
        for change in cases:
            rules = copy.deepcopy(MW.DEFAULT_RULES)
            change(rules)
            if "qwen" not in rules["providers"]:
                rules["order"] = ["minimax", "zhipu", "claude"]
            self.assertIsNone(MW.validate_rules(rules), rules)

    def test_each_field_is_checked(self):
        cases = {
            "unknown top-level key": lambda r: r.update(extra=1),
            "missing key": lambda r: r.pop("defaults"),
            "version 2": lambda r: r.update(version=2),
            "version as text": lambda r: r.update(version="1"),
            "version true": lambda r: r.update(version=True),
            "negative revision": lambda r: r.update(revision=-1),
            "revision as float": lambda r: r.update(revision=1.0),
            "unknown balance": lambda r: r.update(balance="random"),
            "order missing an enabled one": lambda r: r.update(order=["minimax", "claude"]),
            "enabled but not in order": lambda r: r["providers"]["qwen"].update(enabled=True),
            "order with a repeat": lambda r: r.update(order=["minimax", "zhipu", "claude", "claude"]),
            "order with a stranger": lambda r: r.update(order=["minimax", "zhipu", "claude", "local"]),
            "order not a list": lambda r: r.update(order="minimax"),
            "unknown provider": lambda r: r["providers"].update(local={"enabled": True, "slots": 1}),
            "empty providers": lambda r: r.update(providers={}),
            "provider extra key": lambda r: r["providers"]["zhipu"].update(weight=2),
            "provider missing slots": lambda r: r["providers"]["zhipu"].pop("slots"),
            "enabled as text": lambda r: r["providers"]["zhipu"].update(enabled="yes"),
            "zero slots": lambda r: r["providers"]["zhipu"].update(slots=0),
            "33 slots": lambda r: r["providers"]["zhipu"].update(slots=33),
            "slots true": lambda r: r["providers"]["zhipu"].update(slots=True),
            "slots as text": lambda r: r["providers"]["zhipu"].update(slots="4"),
            "missing level": lambda r: r["levels"].pop("hard"),
            "unknown level": lambda r: r["levels"].update(extreme={"claude": "sonnet"}),
            "empty level": lambda r: r["levels"].update(hard={}),
            "level not an object": lambda r: r["levels"].update(hard=["claude"]),
            "level names unknown provider": lambda r: r["levels"]["simple"].update(local="x"),
            "model of another provider": lambda r: r["levels"]["simple"].update(zhipu="m3"),
            "opus under a typo": lambda r: r["levels"]["hard"].update(claude="opus"),
            "defaults extra key": lambda r: r["defaults"].update(retries=1),
            "timeout too short": lambda r: r["defaults"].update(timeoutSeconds=29),
            "timeout too long": lambda r: r["defaults"].update(timeoutSeconds=86401),
            "timeout as float": lambda r: r["defaults"].update(timeoutSeconds=60.0),
            "negative cooldown": lambda r: r["defaults"].update(cooldownSeconds=-1),
            "cooldown too long": lambda r: r["defaults"].update(cooldownSeconds=86401),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.assertIsInstance(self.check(change), str)

    def test_a_level_may_not_name_a_provider_the_rules_leave_out(self):
        def change(r):
            del r["providers"]["qwen"]
            r["order"] = ["minimax", "zhipu", "claude"]
            r["levels"]["hard"]["qwen"] = "glm"
        self.assertIsInstance(self.check(change), str)

    def test_not_an_object(self):
        for rules in ([], "x", None, 1):
            self.assertIsInstance(MW.validate_rules(rules), str)

    def test_load_returns_a_copy_and_reports_problems(self):
        folder = tempfile.mkdtemp()
        try:
            path = os.path.join(folder, "rules.json")
            rules, problem = MW.load_rules(path)
            self.assertIsNone(problem)
            rules["providers"]["minimax"]["slots"] = 1
            self.assertEqual(MW.DEFAULT_RULES["providers"]["minimax"]["slots"], 8)
            for text in ("{", "\xff", "[]", '{"version": 1}'):
                with open(path, "w", encoding="latin-1") as handle:
                    handle.write(text)
                rules, problem = MW.load_rules(path)
                self.assertIsInstance(problem, str)
                self.assertEqual(rules, MW.DEFAULT_RULES)
        finally:
            shutil.rmtree(folder)

    def test_defaults_match_the_contract(self):
        text = read(RULES_DOC)
        block = re.search(r"## rules\.json\s+```json\n(.*?)```", text, re.S).group(1)
        self.assertEqual(json.loads(block), MW.DEFAULT_RULES)

    def test_defaults_match_fleet_agent(self):
        home = tempfile.mkdtemp()
        saved = os.environ.get("HOME")
        try:
            os.environ["HOME"] = home
            spec = importlib.util.spec_from_file_location("fleet_agent", AGENT)
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)
        finally:
            os.environ["HOME"] = saved
            shutil.rmtree(home)
        if not hasattr(agent, "DEFAULT_RULES"):
            self.skipTest("fleet-agent.py has no DEFAULT_RULES yet")
        self.assertEqual(agent.DEFAULT_RULES, MW.DEFAULT_RULES)


FAKE_OPENCODE = r'''#!@PYTHON@
import json, os, sys
env = os.environ
config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
provider = next(iter(config["provider"]))
prompt = sys.stdin.read()
log = env.get("FAKE_LOG")
if log:
    with open(os.path.join(log, "opencode-%d.json" % os.getpid()), "w") as f:
        json.dump({"argv": sys.argv[1:], "config": config, "prompt": prompt,
                   "keys": {k: v for k, v in env.items() if k.endswith("_API_KEY")}}, f)
mode = env.get("MODE_OC_" + provider, "ok")
def ev(o):
    print(json.dumps(o), flush=True)
ev({"type": "step_start", "sessionID": "s", "part": {}})
ev({"type": "text", "part": {"text": "thinking about usage limit exceeded"}})
if mode == "cut":
    ev({"type": "step_finish", "part": {"reason": "tool-calls", "tokens": {"input": 1, "output": 1}}})
    sys.exit(0)
if mode == "error":
    ev({"type": "error", "error": {"name": "APIError", "data": {"message": "login fail", "statusCode": 401}}})
    sys.exit(1)
ev({"type": "step_finish", "part": {"reason": "tool-calls", "tokens": {"input": 10, "output": 2, "cache": {"read": 5, "write": 1}}}})
ev({"type": "step_start", "part": {}})
ev({"type": "text", "part": {"text": "RESULT-from-" + provider}})
ev({"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 3, "output": 4, "cache": {"read": 0, "write": 0}}}})
'''


class Opencode(Base):
    def provider_of(self, done):
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        return done.stdout.decode().strip().replace("RESULT-from-", "")

    def install(self):
        write(os.path.join(self.bin, "opencode"), FAKE_OPENCODE.replace("@PYTHON@", PY), 0o755)

    def runs(self):
        found = []
        for name in sorted(os.listdir(self.log)):
            if name.startswith("opencode-"):
                with open(os.path.join(self.log, name)) as handle:
                    found.append(json.load(handle))
        return found

    def test_the_answer_comes_from_the_last_step_and_is_counted(self):
        self.install()
        done = self.mw("--cli", "opencode", "-l", "simple", "x")
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertEqual(done.stdout.decode().strip(), "RESULT-from-mm")
        run = self.runs()[-1]
        self.assertEqual(run["argv"][:5], ["run", "--format", "json", "-m", "mm/MiniMax-M2.7-highspeed"])
        self.assertEqual(run["prompt"].strip(), "x")
        self.assertEqual(run["keys"], {"MINIMAX_API_KEY": "key-minimax"})
        self.assertIn("MINIMAX_API_KEY", run["config"]["provider"]["mm"]["options"]["apiKey"])
        self.assertNotIn("key-minimax", json.dumps(run["config"]))

    def test_with_no_tools_named_a_worker_can_only_read(self):
        self.install()
        self.mw("--cli", "opencode", "x")
        permission = self.runs()[-1]["config"]["permission"]
        self.assertEqual(permission["*"], "deny")
        for name in ("edit", "bash", "webfetch", "websearch", "task"):
            self.assertEqual(permission[name], "deny", name)
        self.assertEqual(permission["read"]["*"], "allow")
        self.assertEqual(permission["read"]["*.env"], "deny")

    def test_allowed_tools_open_exactly_what_they_name(self):
        permission = MW.opencode_permission(["--allowedTools", "Read,Edit,Bash(python3:*),Bash(git status),mcp__x__y"])
        self.assertEqual(permission["edit"], "allow")
        rules = list(permission["bash"].items())
        self.assertEqual(rules[:4], [("*", "deny"), ("python3", "allow"), ("python3 *", "allow"), ("git status", "allow")])
        # The last match wins, so a redirect after an allowed command is still refused.
        self.assertEqual([name for name, value in rules[4:] if value != "deny"], [])
        for escape in ("*>*", "*$(*", "*;*", "*&*", "*|*"):
            self.assertIn(escape, permission["bash"])
        self.assertEqual(permission["webfetch"], "deny")
        self.assertEqual(MW.opencode_permission(["--allowedTools=Bash"])["bash"], "allow")

    def test_a_flag_with_no_equivalent_stops_the_task(self):
        self.install()
        done = self.mw("--cli", "opencode", "x", "--", "--dangerously-skip-permissions")
        self.assertEqual(done.returncode, 2)
        self.assertIn("no equivalent", done.stderr.decode())
        self.assertEqual(self.runs(), [])

    def test_an_error_moves_to_the_next_provider(self):
        self.install()
        done = self.mw("--cli", "opencode", "x", env={"MODE_OC_mm": "error"})
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertEqual(done.stdout.decode().strip(), "RESULT-from-zp")

    def test_a_stream_that_just_stops_is_a_failure(self):
        self.install()
        done = self.mw("--cli", "opencode", "x", env={"MODE_OC_mm": "cut"})
        self.assertEqual(done.stdout.decode().strip(), "RESULT-from-zp")
        self.assertNotIn("usage limit", done.stderr.decode())

    def test_claude_is_never_a_candidate(self):
        self.install()
        done = self.mw("--cli", "opencode", "-b", "claude", "x")
        self.assertEqual(done.returncode, 2)
        done = self.mw("--cli", "opencode", "-l", "hard", "x")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no enabled provider", done.stderr.decode())

    def test_the_rule_chooses_the_cli_and_the_flag_overrides_it(self):
        self.install()
        self.rules(lambda r: r.update(cli="opencode"))
        self.assertEqual(self.mw("x").stdout.decode().strip(), "RESULT-from-mm")
        self.assertEqual(self.provider_of(self.mw("--cli", "claude", "x")), "minimax")

    def test_a_task_for_another_machine_stays_with_claude(self):
        self.install()
        done = self.mw("--cli", "opencode", "-H", "somewhere", "x")
        self.assertEqual(done.returncode, 2)
        self.assertIn("this machine only", done.stderr.decode())


class Rules(Base):
    def provider_of(self, done):
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        return done.stdout.decode().strip().replace("RESULT-from-", "")

    def test_an_invalid_file_warns_and_work_goes_on(self):
        for text in ("{not json", json.dumps(dict(MW.DEFAULT_RULES, version=2)),
                     json.dumps(dict(MW.DEFAULT_RULES, balance="random"))):
            with self.subTest(text=text[:20]):
                write(os.path.join(self.home, ".config/mm-worker/rules.json"), text)
                done = self.mw("x")
                self.assertEqual(self.provider_of(done), "minimax")
                self.assertIn("using the default rules", done.stderr.decode())

    def test_a_full_model_name_reaches_claude_unchanged(self):
        self.rules(lambda r: r["levels"]["normal"].update(minimax="MiniMax-M2.5"))
        done = self.mw("x")
        self.assertEqual(self.provider_of(done), "minimax")
        self.assertNotIn("using the default rules", done.stderr.decode())
        self.assertEqual(self.records("minimax")[-1]["argv"][:2], ["--model", "MiniMax-M2.5"])

    def test_another_providers_model_is_invalid(self):
        self.rules(lambda r: r["levels"]["normal"].update(minimax="glm-5.1"))
        self.assertIn("using the default rules", self.mw("x").stderr.decode())

    def test_a_missing_file_is_silent(self):
        done = self.mw("x")
        self.assertNotIn("rules", done.stderr.decode())

    def test_proportional_picks_the_least_loaded(self):
        self.occupy("minimax", 4)
        self.assertEqual(self.provider_of(self.mw("x")), "zhipu")
        self.occupy("zhipu", 2)
        self.occupy("claude", 3)
        self.assertEqual(self.provider_of(self.mw("x")), "minimax")  # 4/8 against 2/4 and 3/4

    def test_proportional_ties_follow_order(self):
        self.rules(lambda r: r.update(order=["zhipu", "claude", "minimax", "qwen"]))
        self.assertEqual(self.provider_of(self.mw("x")), "zhipu")

    def test_priority_takes_the_first_free_in_order(self):
        self.rules(lambda r: r.update(balance="priority"))
        self.occupy("minimax", 7)
        self.assertEqual(self.provider_of(self.mw("x")), "minimax")
        self.occupy("minimax", 8)
        self.assertEqual(self.provider_of(self.mw("x")), "zhipu")
        self.rules(lambda r: r.update(balance="priority", order=["claude", "zhipu", "minimax", "qwen"]))
        self.assertEqual(self.provider_of(self.mw("x")), "claude")

    def test_slots_come_from_the_rules_and_the_environment_overrides(self):
        self.rules(lambda r: (r.update(balance="priority"), r["providers"]["minimax"].update(slots=2)))
        self.occupy("minimax", 2)
        self.assertEqual(self.provider_of(self.mw("x")), "zhipu")
        self.assertEqual(self.provider_of(self.mw("x", env={"MM_WORKER_SLOTS_MINIMAX": "3"})), "minimax")

    def test_a_disabled_provider_gets_no_automatic_work_but_b_reaches_it(self):
        self.rules(lambda r: r["providers"]["minimax"].update(enabled=False))
        self.assertEqual(self.provider_of(self.mw("x")), "zhipu")
        self.assertEqual(self.provider_of(self.mw("-b", "minimax", "x")), "minimax")
        self.assertEqual(self.provider_of(self.mw("-b", "qwen", "x")), "qwen")

    def test_levels_choose_providers_and_models(self):
        self.rules(lambda r: r["levels"].update(normal={"minimax": "m27", "claude": "haiku"}))
        self.assertEqual(self.provider_of(self.mw("x")), "minimax")
        self.assertEqual(self.records("minimax")[-1]["argv"][:2], ["--model", "sonnet"])
        self.occupy("minimax", 8)
        self.assertEqual(self.provider_of(self.mw("x")), "claude")
        self.assertEqual(self.records("claude")[-1]["argv"][:2], ["--model", "haiku"])
        self.assertEqual(self.ledger(), ["claude ok in=150 out=7"])

    def test_default_level_models(self):
        cases = [(["-l", "simple", "-b", "minimax"], "minimax", "haiku"),
                 (["-l", "normal", "-b", "minimax"], "minimax", "opus[1m]"),
                 (["-l", "hard"], "claude", "claude-opus-5-5"),
                 (["-l", "normal", "-b", "zhipu"], "zhipu", "sonnet"),
                 (["-l", "normal", "-b", "claude"], "claude", "sonnet"),
                 (["-l", "simple", "-b", "qwen"], "qwen", "deepseek-v4-pro-0813[1m]"),
                 (["-l", "hard", "-b", "qwen"], "qwen", "glm-5.3[1m]"),
                 (["-b", "minimax", "-m", "m27"], "minimax", "sonnet"),
                 (["-b", "qwen", "-q", "max"], "qwen", "opus"),
                 (["-b", "zhipu", "-z", "flash"], "zhipu", "sonnet")]
        for args, provider, alias in cases:
            with self.subTest(args=args):
                self.assertEqual(self.provider_of(self.mw(*(args + ["x"]))), provider)
                self.assertEqual(self.records(provider)[-1]["argv"][:2], ["--model", alias])

    def test_m_fast_means_simple(self):
        done = self.mw("-m", "fast", "-b", "minimax", "x")
        self.assertIn("level=simple", done.stderr.decode())
        self.assertEqual(self.records("minimax")[-1]["argv"][:2], ["--model", "haiku"])

    def test_opus_is_refused_outside_hard(self):
        done = self.mw("-b", "claude", "-c", "claude-opus-5-5", "x")
        self.assertIn("opus is only for hard", done.stderr.decode())
        self.assertEqual(self.records("claude")[-1]["argv"][:2], ["--model", "sonnet"])
        self.mw("-b", "claude", "x", env={"MM_WORKER_CLAUDE_MODEL": "opus"})
        self.assertEqual(self.records("claude")[-1]["argv"][:2], ["--model", "sonnet"])
        self.mw("-l", "hard", "-c", "haiku", "x")
        self.assertEqual(self.records("claude")[-1]["argv"][:2], ["--model", "haiku"])
        self.mw("-l", "hard", "x", env={"MM_WORKER_CLAUDE_MODEL": "claude-opus-5-5"})
        self.assertEqual(self.records("claude")[-1]["argv"][:2], ["--model", "claude-opus-5-5"])

    def test_a_level_with_no_enabled_provider(self):
        self.rules(lambda r: r["providers"]["claude"].update(enabled=False))
        done = self.mw("-l", "hard", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("no enabled provider takes hard tasks", done.stderr.decode())
        self.assertEqual(self.provider_of(self.mw("-l", "hard", "-b", "claude", "x")), "claude")

    def test_cooldown_from_the_rules_and_the_environment(self):
        self.rules(lambda r: r["defaults"].update(cooldownSeconds=300))
        self.mw("x", env={"MODE_minimax": "quota"})
        self.assertAlmostEqual(self.until("minimax"), 300, delta=10)
        os.unlink(self.cooling_files("minimax")[0])
        self.mw("x", env={"MODE_minimax": "quota", "MM_WORKER_COOLDOWN": "1200"})
        self.assertAlmostEqual(self.until("minimax"), 1200, delta=10)

    def test_timeout_from_the_rules(self):
        self.rules(lambda r: r["defaults"].update(timeoutSeconds=45))
        rules, _ = MW.load_rules(os.path.join(self.home, ".config/mm-worker/rules.json"))
        self.assertEqual(MW.Dispatcher(MW.Options(), rules).timeout, 45)
        options = MW.parse_args(["-t", "7"])
        self.assertEqual(MW.Dispatcher(options, rules).timeout, 7)
        self.assertEqual(MW.Dispatcher(MW.Options(), MW.DEFAULT_RULES).timeout, 900)
        if not shutil.which("setsid"):
            return  # the part below runs the remote side here, which macOS can't do
        done = self.mw("-H", "box", "-b", "minimax", "x", env={"PATH": self.sshbin + ":" + SYSTEM_PATH})
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertIn(".local/bin/mm-remote-run minimax 45 proj ", read(self.env["FAKE_SSH_LOG"]))


# ---------- environment, arguments, prompt and ledger ----------

class MissingKeys(Base):
    def test_a_provider_without_a_key_gets_no_automatic_work(self):
        os.unlink(os.path.join(self.home, ".config/minimax/env"))
        done = self.mw("-l", "normal", "x")
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertEqual(self.records("minimax"), [])
        state = os.listdir(os.path.join(self.home, ".cache/mm-worker"))
        self.assertNotIn("minimax_unavailable_until", state)

    def test_an_empty_key_counts_as_none(self):
        with open(os.path.join(self.home, ".config/minimax/env"), "w") as handle:
            handle.write("MINIMAX_API_HOST=http://127.0.0.1:9\nMINIMAX_API_KEY=\n")
        done = self.mw("-l", "normal", "x")
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertEqual(self.records("minimax"), [])

    def test_with_no_keys_at_all_claude_still_runs(self):
        for provider in ("minimax", "zhipu", "qwen"):
            path = os.path.join(self.home, ".config", provider, "env")
            if os.path.exists(path):
                os.unlink(path)
        done = self.mw("-l", "simple", "x")
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertEqual(len(self.records("claude")), 1)

    def test_a_forced_provider_without_a_key_still_fails_clearly(self):
        os.unlink(os.path.join(self.home, ".config/minimax/env"))
        done = self.mw("-b", "minimax", "x")
        self.assertNotEqual(done.returncode, 0)


class Environment(Base):
    def test_provider_environments(self):
        self.mw("-b", "minimax", "x")
        self.mw("-b", "zhipu", "x")
        self.mw("-b", "qwen", "x")
        base = "http://127.0.0.1:%d" % SERVER.server_address[1]
        mm = self.records("minimax")[0]["env"]
        self.assertEqual(mm["ANTHROPIC_BASE_URL"], base + "/mm/anthropic")
        self.assertEqual(mm["ANTHROPIC_AUTH_TOKEN"], "key-minimax")
        self.assertEqual((mm["ANTHROPIC_DEFAULT_OPUS_MODEL"], mm["ANTHROPIC_DEFAULT_SONNET_MODEL"],
                          mm["ANTHROPIC_DEFAULT_HAIKU_MODEL"]),
                         ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"))
        self.assertEqual(mm["API_TIMEOUT_MS"], "3000000")
        self.assertEqual(mm["CLAUDE_CODE_MODEL_CAPABILITIES"], "minimax*=-mid_conv_tool_change")
        self.assertEqual(mm["MM_WORKER_ACTIVE"], "1")
        zp = self.records("zhipu")[0]["env"]
        self.assertEqual(zp["ANTHROPIC_BASE_URL"], base + "/zp")
        self.assertEqual(zp["ANTHROPIC_AUTH_TOKEN"], "key-zhipu")
        self.assertNotIn("ZHIPU_API_KEY", zp)
        self.assertEqual(zp["ANTHROPIC_DEFAULT_OPUS_MODEL"], "glm-5.3-flash")
        self.assertEqual(zp["CLAUDE_CODE_SUBAGENT_MODEL"], "sonnet")
        qw = self.records("qwen")[0]["env"]
        self.assertEqual(qw["ANTHROPIC_BASE_URL"], base + "/qw")
        self.assertEqual(qw["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "65536")
        self.assertEqual(qw["ANTHROPIC_DEFAULT_SONNET_MODEL"], "qwen3.8-flash[1m]")

    def test_env_file_overrides_of_the_model_names(self):
        path = os.path.join(self.home, ".config/minimax/env")
        write(path, read(path) + "MM_SONNET=custom-sonnet\n")
        self.mw("-b", "minimax", "x")
        self.assertEqual(self.records("minimax")[0]["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"], "custom-sonnet")

    def test_inherited_provider_variables_are_scrubbed(self):
        env = {"ANTHROPIC_BASE_URL": "http://elsewhere", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k",
               "ANTHROPIC_MODEL": "m", "ANTHROPIC_DEFAULT_OPUS_MODEL": "o", "API_TIMEOUT_MS": "1",
               "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1", "CLAUDE_QWEN_SESSION": "1"}
        done = self.mw("-b", "claude", "x", env=env)
        self.assertEqual(done.stdout.decode(), "RESULT-from-claude\n")
        seen = self.records("claude")[0]["env"]
        for name in env:
            self.assertNotIn(name, seen)

    def test_nesting_is_refused(self):
        done = self.mw("x", env={"MM_WORKER_ACTIVE": "1"})
        self.assertEqual(done.returncode, 3)
        self.assertIn("[mm-worker] refused", done.stderr.decode())
        self.assertEqual(self.records(), [])

    def test_arguments_after_double_dash_pass_through(self):
        self.mw("-b", "claude", "x", "--", "--allowedTools", "Read,Bash(git log:*)", "-x")
        self.assertEqual(self.records()[0]["argv"],
                         ["--model", "sonnet", "-p", "--output-format", "stream-json", "--verbose",
                          "--allowedTools", "Read,Bash(git log:*)", "-x"])
        self.assertEqual(self.records()[0]["cwd"], self.work)

    def test_env_files_are_parsed_not_run(self):
        path = os.path.join(self.root, "env")
        marker = os.path.join(self.root, "ran")
        write(path, "\n".join([
            "# comment", 'export A="x y"', "B='q\"z'", "C=plain # note", 'D="a\\"b\\$c"',
            "E=$(touch %s)" % marker, "F=", "touch %s" % marker, "  export G=g", "H=`touch %s`" % marker]))
        values = MW.read_env_file(path)
        self.assertEqual(values["A"], "x y")
        self.assertEqual(values["B"], 'q"z')
        self.assertEqual(values["C"], "plain")
        self.assertEqual(values["D"], 'a"b$c')
        self.assertEqual(values["F"], "")
        self.assertEqual(values["G"], "g")
        self.assertNotIn("touch", values)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(MW.read_env_file(os.path.join(self.root, "missing")), {})

    def test_claude_is_found_in_the_fallback_place(self):
        write(os.path.join(self.home, ".local/bin/claude"), FAKE_CLAUDE.replace("@PYTHON@", PY), 0o755)
        done = self.mw("-b", "claude", "x", env={"PATH": self.sshbin + ":" + SYSTEM_PATH})
        self.assertEqual(done.stdout.decode(), "RESULT-from-claude\n")

    def test_missing_claude_is_a_clear_error(self):
        if any(os.path.exists(path) for path in MW.CLAUDE_FALLBACKS if path.startswith("/")):
            self.skipTest("this machine has claude in a fallback place")
        done = self.mw("-b", "claude", "x", env={"PATH": self.sshbin + ":" + SYSTEM_PATH})
        self.assertEqual(done.returncode, 127)
        self.assertIn("claude not found", done.stderr.decode())


class Arguments(Base):
    def test_prompt_sources(self):
        prompt_file = os.path.join(self.root, "prompt.md")
        write(prompt_file, "from a file\n")
        self.mw("-b", "claude", "-f", prompt_file)
        self.mw("-b", "claude", input="from stdin\n")
        self.mw("-b", "claude", "from an argument")
        self.assertEqual([r["prompt"] for r in self.records()],
                         ["from a file\n", "from stdin\n", "from an argument\n"])

    def test_bad_input(self):
        cases = [(["-b", "claude"], "the prompt is empty"),
                 (["-b", "claude", "-f", os.path.join(self.root, "nothing")], "cannot read the prompt"),
                 (["-b", "local", "x"], "the local model backend has been removed"),
                 (["-b", "ollama", "x"], "unknown backend"),
                 (["-l", "extreme", "x"], "unknown level"),
                 (["-t", "soon", "x"], "timeout must be a number"),
                 (["-t", "0", "x"], "timeout must be above 0"),
                 (["x", "-l"], "-l needs a value")]
        for args, message in cases:
            with self.subTest(args=args):
                done = self.mw(*args)
                self.assertEqual(done.returncode, 2)
                self.assertIn("[mm-worker] " + message, done.stderr.decode())
                self.assertEqual(done.stdout, b"")
        self.assertEqual(self.records(), [])

    def test_name_and_run_files(self):
        self.mw("-b", "minimax", "-n", "my task", "x")
        names = glob.glob(os.path.join(self.state, "runs/*.name"))
        self.assertEqual(len(names), 1)
        self.assertEqual(read(names[0]), "my task\n")
        run_id = os.path.basename(names[0])[:-len(".name")]
        self.assertRegex(run_id, r"^\d{8}-\d{6}-\d+$")
        for suffix in (".prompt.md", ".stderr", ".minimax.json"):
            self.assertTrue(os.path.exists(os.path.join(self.state, "runs", run_id + suffix)), suffix)

    def test_ledger_format(self):
        prompt = "line one\nline\ttwo " + "x" * 80
        self.mw("-b", "minimax", prompt)
        fields = read(os.path.join(self.state, "ledger.tsv")).rstrip("\n").split("\t")
        self.assertEqual(len(fields), 9)
        self.assertRegex(fields[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(fields[1:4], ["minimax", "m3", "ok"])
        self.assertRegex(fields[4], r"^\d+s$")
        self.assertEqual(fields[5:8], ["in=150", "out=7", "~/proj"])
        self.assertEqual(fields[8], prompt.encode()[:60].decode().replace("\n", " ").replace("\t", " "))

    def test_help(self):
        done = self.mw("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("usage: mm-worker", done.stdout.decode())

    def test_status(self):
        self.rules(lambda r: r["providers"]["minimax"].update(slots=6))
        self.mw("-b", "minimax", "first")
        self.mw("-b", "claude", "second")
        write(os.path.join(self.state, "zhipu_unavailable_until"), "%d\n" % (time.time() + 1500))
        write(os.path.join(self.state, "zhipu_unavailable_reason"), "quota used up (HTTP 402)\n")
        self.occupy("claude", 1)
        done = self.mw("--status", env={"MM_WORKER_SLOTS_CLAUDE": "2"})
        err, out = done.stderr.decode(), done.stdout.decode()
        self.assertEqual(done.returncode, 0)
        self.assertIn("[mm-worker] automatic dispatch uses: minimax zhipu claude (only with -b: qwen)", err)
        self.assertIn("[mm-worker] minimax available (not cooling down)", err)
        self.assertRegex(err, r"\[mm-worker\] zhipu unavailable, 2[45] minutes left \(quota used up \(HTTP 402\)\)")
        self.assertIn("[mm-worker] slots in use: minimax 0/6  zhipu 0/4  claude 1/2  qwen 0/4", err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Last 15 runs:")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1].index("ok"), lines[2].index("ok"), "columns line up")
        self.assertIn("first", lines[1])


# ---------- remote mode ----------

@unittest.skipUnless(shutil.which("setsid") and shutil.which("timeout") and shutil.which("bash"),
                     "remote tests need setsid, timeout and bash")
class Remote(Base):
    FAST = {"RETRY_SLEEP": 0.2}

    def setUp(self):
        Base.setUp(self)
        # No local claude: a remote task must not need one.
        self.env["PATH"] = self.sshbin + ":" + SYSTEM_PATH

    def run_id(self):
        path = self.wait_for(lambda: glob.glob(os.path.join(self.state, "runs/*.remote")), 15, "submission")[0]
        return os.path.basename(path)[:-len(".remote")]

    def job(self, run_id, provider="minimax"):
        return os.path.join(self.remote, ".cache/mm-worker/jobs/%s-%s" % (run_id, provider))

    def test_end_to_end_with_awkward_text(self):
        os.makedirs(os.path.join(self.remote, "my proj"))
        name = "it's \"named\" $HOME `x`"
        prompt = "say 'hi' and $(touch %s/pwned) \"now\"" % self.root
        done = self.mw("-H", "box", "-b", "minimax", "-C", "my proj", "-n", name, prompt, "--",
                       "--allowedTools", "Bash(git log:*),Read", patch=self.FAST)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertEqual(done.stdout.decode(), "RESULT-from-minimax\n")
        record = self.records()[0]
        self.assertEqual(record["argv"], ["--model", "opus[1m]", "-p", "--output-format", "stream-json",
                                          "--verbose", "--allowedTools", "Bash(git log:*),Read"])
        self.assertEqual(record["cwd"], os.path.join(self.remote, "my proj"))
        self.assertEqual(record["prompt"], prompt + "\n")
        self.assertFalse(glob.glob(os.path.join(self.root, "**", "pwned"), recursive=True))
        run_id = os.path.basename(glob.glob(os.path.join(self.state, "runs/*.remote"))[0])[:-len(".remote")]
        job = self.job(run_id)
        self.assertEqual(read(os.path.join(job, "name")), name + "\n")
        self.assertEqual(read(os.path.join(job, "rc")).strip(), "0")
        self.assertEqual(len(read(os.path.join(job, "out.json")).splitlines()), 1)
        for name_ in ("prompt.md", "pid", "started", "stderr"):
            self.assertTrue(os.path.exists(os.path.join(job, name_)), name_)
        self.assertEqual(read(os.path.join(self.state, "runs", run_id + ".remote")),
                         "box .cache/mm-worker/jobs/%s-minimax minimax\n" % run_id)
        self.assertEqual(self.ledger(), ["minimax@box ok in=150 out=7"])
        self.assertEqual(self.live_slots(), [])
        launch = [line for line in read(self.env["FAKE_SSH_LOG"]).splitlines() if "setsid" in line]
        self.assertEqual(len(launch), 1)
        self.assertIn("mm-remote-run minimax 900 'my proj' --model 'opus[1m]' -p --output-format stream-json "
                      "--verbose --allowedTools 'Bash(git log:*),Read'", shlex_inner(launch[0]))

    def test_started_guard_survives_a_dropped_connection(self):
        write(self.env["FAKE_SSH_FLAKY"], "")
        done = self.mw("-H", "box", "-b", "zhipu", "x", patch=self.FAST)
        self.assertEqual(done.stdout.decode(), "RESULT-from-zhipu\n")
        launches = [line for line in read(self.env["FAKE_SSH_LOG"]).splitlines() if "setsid" in line]
        self.assertEqual(len(launches), 2)
        self.assertEqual(len(self.records()), 1)

    def test_waits_through_a_lost_connection(self):
        proc = self.start("-H", "box", "-b", "minimax", "x", env={"MODE_minimax": "slow", "FAKE_SLOW": "3"},
                          patch=self.FAST)
        self.run_id()
        write(self.env["FAKE_SSH_OFFLINE"], "")
        time.sleep(1.5)
        os.unlink(self.env["FAKE_SSH_OFFLINE"])
        rc, out, err = self.finish(proc, 30)
        self.assertEqual((rc, out), (0, "RESULT-from-minimax\n"))
        self.assertIn("cannot reach box", err)
        self.assertIn("connection to box is back", err)

    def test_gives_up_waiting_then_fetch_gets_the_result(self):
        proc = self.start("-H", "box", "-b", "minimax", "x",
                          env={"MODE_minimax": "slow", "FAKE_SLOW": "2", "MM_WORKER_OFFLINE_WAIT": "1"},
                          patch=self.FAST)
        run_id = self.run_id()
        write(self.env["FAKE_SSH_OFFLINE"], "")
        rc, out, err = self.finish(proc, 30)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("mm-worker --fetch " + run_id, err)
        self.assertEqual(len(self.records()), 1, "giving up must not hand the task to another provider")
        os.unlink(self.env["FAKE_SSH_OFFLINE"])
        done = self.mw("--fetch", run_id, patch=self.FAST)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertEqual(done.stdout.decode(), "RESULT-from-minimax\n")
        self.assertEqual(self.ledger(), ["minimax@box fetched(0) in=150 out=7"])
        self.assertEqual(read(os.path.join(self.state, "ledger.tsv")).split("\t")[2], "fetched")

    def test_kill_cancels_without_failover(self):
        proc = self.start("-H", "box", "-t", "60", "x", env={"MODE_minimax": "slow"}, patch=self.FAST)
        run_id = self.run_id()
        self.wait_for(lambda: self.records(), 10, "the remote worker")
        done = self.mw("--kill", run_id)
        self.assertEqual(done.returncode, 0)
        self.assertIn("cancelled " + run_id, done.stderr.decode())
        rc, out, err = self.finish(proc, 15)
        self.assertEqual((rc, out), (130, ""))
        self.assertNotIn("trying the next provider", err)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(read(os.path.join(self.job(run_id), "rc")).strip(), "cancelled")
        self.assert_no_leftovers()
        self.assertEqual(self.mw("--kill", run_id).returncode, 0, "still cancelled, as before")

    def test_kill_after_the_end_changes_nothing(self):
        done = self.mw("-H", "box", "-b", "minimax", "x", patch=self.FAST)
        self.assertEqual(done.returncode, 0)
        run_id = self.run_id()
        done = self.mw("--kill", run_id)
        self.assertEqual(done.returncode, 1)
        self.assertIn("already finished", done.stderr.decode())
        self.assertEqual(read(os.path.join(self.job(run_id), "rc")).strip(), "0")

    def test_an_interrupt_leaves_the_remote_task_running(self):
        proc = self.start("-H", "box", "-b", "minimax", "x", env={"MODE_minimax": "slow", "FAKE_SLOW": "3"},
                          patch=self.FAST)
        run_id = self.run_id()
        self.wait_for(lambda: self.records(), 10, "the remote worker")
        proc.send_signal(signal.SIGTERM)
        rc, _, err = self.finish(proc, 10)
        self.assertEqual(rc, 130)
        self.assertIn("mm-worker --fetch " + run_id, err)
        self.assertIn("--kill " + run_id, err)
        self.assertEqual(self.live_slots(), [])
        self.assertFalse(os.path.exists(os.path.join(self.job(run_id), "rc")))
        self.assertTrue(self.leftovers(), "the remote task should still run")
        done = self.mw("--fetch", run_id, patch=self.FAST)
        self.assertEqual(done.stdout.decode(), "RESULT-from-minimax\n")

    def test_remote_timeout_is_124(self):
        done = self.mw("-H", "box", "-b", "zhipu", "-t", "1", "x", env={"MODE_zhipu": "slow"}, patch=self.FAST)
        self.assertEqual(done.returncode, 124)
        self.assertEqual(self.ledger(), ["zhipu@box timeout in=0 out=0"])

    def test_remote_failure_fails_over_to_the_next_provider(self):
        done = self.mw("-H", "box", "x", env={"MODE_minimax": "die"}, patch=self.FAST)
        self.assertEqual(done.stdout.decode(), "RESULT-from-zhipu\n")
        self.assertTrue(self.records("zhipu")[0]["prompt"].startswith(MW.FAILOVER_NOTE))

    def test_bad_target_and_unknown_runs(self):
        done = self.mw("-H", "box", "-C", "nowhere", "x")
        self.assertEqual(done.returncode, 2)
        self.assertIn("lacks mm-remote-run or the folder nowhere", done.stderr.decode())
        for flag in ("--fetch", "--kill"):
            for run_id in ("20261101-000000-1", "../../etc"):
                done = self.mw(flag, run_id)
                self.assertEqual(done.returncode, 2)
                self.assertIn("no remote task recorded", done.stderr.decode())

    def test_fleet_cancel_refuses_a_remote_run_here(self):
        proc = self.start("-H", "box", "-b", "minimax", "x", env={"MODE_minimax": "slow", "FAKE_SLOW": "3"},
                          patch=self.FAST)
        run_id = self.run_id()
        self.assertEqual(json.loads(self.ag("cancel", run_id))["error"], "runs on another machine")
        worker = [w for w in self.snapshot()["workers"] if w.get("remoteHost")][0]
        self.assertEqual((worker["id"], worker["provider"]), (run_id, "minimax"))
        rc, out, _ = self.finish(proc, 30)
        self.assertEqual((rc, out), (0, "RESULT-from-minimax\n"))


def shlex_inner(launch):
    """The command inside `bash -c '...'` of the launch text."""
    import shlex
    words = shlex.split(launch)
    return words[words.index("-c") + 1]


# ---------- the shipped file ----------

class Source(unittest.TestCase):
    def setUp(self):
        self.text = read(WORKER)

    def test_header(self):
        lines = self.text.splitlines()
        self.assertEqual(lines[0], "#!/usr/bin/env python3")
        self.assertEqual(lines[1], "MM_WORKER_VERSION = 3")

    def test_python_3_9_grammar(self):
        ast.parse(self.text, feature_version=(3, 9))
        ast.parse(read(os.path.abspath(__file__)), feature_version=(3, 9))
        for newer in ("removesuffix(", "strict=", "bit_count", "pairwise", "tomllib", "ExceptionGroup",
                      "except*", "kw_only", "TypeAlias", "| None", "datetime.UTC"):
            self.assertNotIn(newer, self.text)

    def test_standard_library_only(self):
        allowed = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else None
        tree = ast.parse(self.text)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module]
            for name in names:
                if allowed is not None:
                    self.assertIn(name.split(".")[0], allowed)

    def test_nothing_personal(self):
        self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", self.text))
        for word in ("/home/", "/Users/", "tailscale", "miniconda"):
            self.assertNotIn(word, self.text)
        # Non-ASCII text appears only in the providers' own quota messages.
        self.assertTrue(set(ch for ch in self.text if ord(ch) > 127) <= set("余额度上限到期"))


if __name__ == "__main__":
    unittest.main()
