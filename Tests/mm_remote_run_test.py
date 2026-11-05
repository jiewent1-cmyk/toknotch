#!/usr/bin/env python3
"""Tests for Sources/Resources/mm-remote-run. Run: python3 Tests/mm_remote_run_test.py

Hermetic: each test gets a temporary HOME and a fake `claude` first on PATH that
records what it was given. Nothing real is run and nothing reaches the network.
"""
import importlib.util, json, os, re, shutil, stat, subprocess, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "Sources/Resources/mm-remote-run")
WORKER = os.path.join(ROOT, "Sources/Resources/mm-worker.py")
SYSTEM_PATH = "/usr/bin:/bin"

# Records its arguments, folder, prompt and the environment that matters, then
# sleeps or exits as told.
FAKE_CLAUDE = r'''#!@PYTHON@
import json, os, sys, time
keep = ("ANTHROPIC_", "API_", "CLAUDE_", "MM_", "MINIMAX_", "ZHIPU_", "QWEN_", "QW_", "ZP_", "FAKE_X")
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": sys.stdin.read(),
          "env": {k: v for k, v in os.environ.items() if k.startswith(keep)}}
with open(os.path.join(os.environ["FAKE_LOG"], "record.json"), "w") as handle:
    json.dump(record, handle)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    os.chmod(path, mode)


def load_worker():
    spec = importlib.util.spec_from_file_location("mm_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MW = load_worker()

ENV_FILES = {
    "minimax": 'export MINIMAX_API_HOST="https://mm.example"\nMINIMAX_API_KEY=\'key-minimax\'  # the key\n'
               "MM_OPUS=opus-override\n",
    "zhipu": "# Zhipu\r\nZHIPU_API_HOST=https://zp.example/api\r\nexport ZHIPU_API_KEY=\"key-\\\"zp\\\" \\$x\"\r\n",
    "qwen": "QWEN_API_HOST=https://qw.example\nQWEN_API_KEY=key\\ qwen ; echo ignored\n"
            "QW_SUBAGENT=haiku\nQWEN_API_KEY=key-qwen-last",  # no final newline, and the last one wins
}


class RemoteRun(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mmrr-")
        self.home = os.path.join(self.root, "home")
        self.bin = os.path.join(self.root, "bin")
        self.log = os.path.join(self.root, "log")
        self.work = os.path.join(self.root, "my proj")
        for folder in (self.home, self.bin, self.log, self.work):
            os.makedirs(folder)
        write(os.path.join(self.bin, "claude"), FAKE_CLAUDE.replace("@PYTHON@", sys.executable), 0o755)
        for provider, text in ENV_FILES.items():
            write(os.path.join(self.home, ".config", provider, "env"), text, 0o600)
        self.env = {"HOME": self.home, "PATH": self.bin + ":" + SYSTEM_PATH, "LANG": "C",
                    "FAKE_LOG": self.log}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_runner(self, provider, limit="60", folder=None, args=("--model", "sonnet", "-p"), env=None,
                   prompt="the prompt\n", timeout=60):
        environment = dict(self.env)
        environment.update(env or {})
        return subprocess.run([RUNNER, provider, limit, folder or self.work] + list(args),
                              input=prompt.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=environment, timeout=timeout)

    def record(self):
        with open(os.path.join(self.log, "record.json")) as handle:
            return json.load(handle)

    def ran(self):
        return os.path.exists(os.path.join(self.log, "record.json"))

    def reference(self, provider, base):
        """What provider_env() in mm-worker.py gives, filtered as the fake records it."""
        saved = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        try:
            env = MW.provider_env(provider, base)
        finally:
            os.environ["HOME"] = saved
        keep = ("ANTHROPIC_", "API_", "CLAUDE_", "MM_", "MINIMAX_", "ZHIPU_", "QWEN_", "QW_", "ZP_", "FAKE_X")
        return {k: v for k, v in env.items() if k.startswith(keep)}

    def assert_matches_reference(self, provider):
        done = self.run_runner(provider)
        self.assertEqual(done.returncode, 0, done.stderr)
        got = self.record()
        self.assertEqual(got["argv"], ["--model", "sonnet", "-p"])
        self.assertEqual(os.path.realpath(got["cwd"]), os.path.realpath(self.work))
        self.assertEqual(got["prompt"], "the prompt\n")
        env = dict(got["env"])
        self.assertEqual(env.pop("MM_WORKER_ACTIVE"), "1")
        self.assertEqual(env, self.reference(provider, {}))
        return got["env"]

    def test_minimax_environment(self):
        env = self.assert_matches_reference("minimax")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://mm.example/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "key-minimax")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "opus-override")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "MiniMax-M2.7")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "MiniMax-M2.7-highspeed")
        self.assertEqual(env["API_TIMEOUT_MS"], "3000000")

    def test_zhipu_environment(self):
        env = self.assert_matches_reference("zhipu")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://zp.example/api")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], 'key-"zp" $x')
        self.assertNotIn("ZHIPU_API_KEY", env)
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "glm-5.3-flash")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "sonnet")

    def test_qwen_environment(self):
        env = self.assert_matches_reference("qwen")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://qw.example")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "key-qwen-last")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "qwen3.8-max[1m]")
        self.assertEqual(env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "65536")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "haiku")

    def test_quoted_key_with_a_space(self):
        write(os.path.join(self.home, ".config/qwen/env"), "QWEN_API_HOST=https://qw.example\nQWEN_API_KEY=key\\ qwen ; x\n")
        env = self.assert_matches_reference("qwen")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "key qwen")

    def test_env_file_is_parsed_not_run(self):
        marker = os.path.join(self.root, "ran")
        write(os.path.join(self.home, ".config/minimax/env"),
              "MINIMAX_API_HOST=https://mm.example\nMINIMAX_API_KEY=\"$(touch %s)\"\ntouch %s\n" % (marker, marker))
        env = self.assert_matches_reference("minimax")
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "$(touch %s)" % marker)

    def test_missing_env_file_still_runs_without_a_key(self):
        os.unlink(os.path.join(self.home, ".config/zhipu/env"))
        env = self.assert_matches_reference("zhipu")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "")

    def test_claude_uses_the_token_file(self):
        write(os.path.join(self.home, ".claude/oauth-token"), "  tok-abc123\n", 0o600)
        done = self.run_runner("claude")
        self.assertEqual(done.returncode, 0, done.stderr)
        env = self.record()["env"]
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-abc123")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("tok-abc123", done.stdout.decode() + done.stderr.decode())

    def test_claude_without_a_token_file_or_with_an_empty_one(self):
        done = self.run_runner("claude")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.record()["env"])
        write(os.path.join(self.home, ".claude/oauth-token"), "\n \n")
        done = self.run_runner("claude")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.record()["env"])

    def test_claude_login_never_goes_to_another_provider(self):
        write(os.path.join(self.home, ".claude/oauth-token"), "tok-abc123\n")
        self.run_runner("minimax", env={"CLAUDE_CODE_OAUTH_TOKEN": "inherited"})
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.record()["env"])

    def test_inherited_anthropic_variables_do_not_leak(self):
        inherited = {"ANTHROPIC_BASE_URL": "https://wrong.example", "ANTHROPIC_AUTH_TOKEN": "wrong",
                     "ANTHROPIC_API_KEY": "wrong", "ANTHROPIC_MODEL": "wrong",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL": "wrong", "ANTHROPIC_SMALL_FAST_MODEL": "wrong",
                     "API_TIMEOUT_MS": "1", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1", "FAKE_X": "kept"}
        for provider in ("claude", "minimax", "zhipu", "qwen"):
            done = self.run_runner(provider, env=inherited)
            self.assertEqual(done.returncode, 0, done.stderr)
            env = self.record()["env"]
            self.assertNotIn("wrong", env.values(), provider)
            self.assertEqual(env["FAKE_X"], "kept")  # everything else passes through
            if provider == "claude":
                self.assertFalse([k for k in env if k.startswith("ANTHROPIC_")])
                self.assertNotIn("API_TIMEOUT_MS", env)
            else:
                self.assertNotIn("ANTHROPIC_API_KEY", env)
                self.assertNotIn("ANTHROPIC_MODEL", env)
                self.assertEqual(env["API_TIMEOUT_MS"], "3000000")

    def test_unknown_provider_exits_2(self):
        done = self.run_runner("openai")
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown provider", done.stderr.decode())
        self.assertFalse(self.ran())

    def test_bad_usage_exits_2(self):
        for limit in ("", "0", "-5", "ten", "1.5"):
            self.assertEqual(self.run_runner("minimax", limit=limit).returncode, 2, limit)
        done = subprocess.run([RUNNER, "minimax", "60"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=self.env, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertFalse(self.ran())

    def test_missing_folder_fails(self):
        done = self.run_runner("minimax", folder=os.path.join(self.root, "nowhere"))
        self.assertEqual(done.returncode, 2)
        self.assertIn("no such folder", done.stderr.decode())
        self.assertFalse(self.ran())

    def test_exit_code_passes_through(self):
        for code in (0, 1, 7):
            done = self.run_runner("claude", env={"FAKE_EXIT": str(code)})
            self.assertEqual(done.returncode, code)

    def test_timeout_ends_the_run(self):
        started = time.time()
        done = self.run_runner("claude", limit="1", env={"FAKE_SLEEP": "120"}, timeout=100)
        self.assertEqual(done.returncode, 124)
        self.assertLess(time.time() - started, 60)
        self.assertTrue(self.ran())

    def without_timeout(self):
        """A PATH with perl and no timeout, as on macOS. The runner needs nothing else."""
        perl = shutil.which("perl", path=SYSTEM_PATH)
        if not perl:
            self.skipTest("no perl")
        tools = os.path.join(self.root, "tools")
        os.makedirs(tools)
        os.symlink(perl, os.path.join(tools, "perl"))
        return {"PATH": self.bin + ":" + tools}

    def test_timeout_falls_back_to_perl(self):
        env = self.without_timeout()
        env["FAKE_SLEEP"] = "120"
        started = time.time()
        done = self.run_runner("claude", limit="1", env=env, timeout=100)
        self.assertEqual(done.returncode, 124, done.stderr)
        self.assertLess(time.time() - started, 60)
        self.assertTrue(self.ran())

    def test_perl_fallback_passes_the_exit_code_and_input(self):
        env = self.without_timeout()
        env["FAKE_EXIT"] = "5"
        done = self.run_runner("minimax", env=env)
        self.assertEqual(done.returncode, 5, done.stderr)
        self.assertEqual(self.record()["prompt"], "the prompt\n")
        self.assertEqual(self.record()["env"]["ANTHROPIC_AUTH_TOKEN"], "key-minimax")

    def test_finds_claude_outside_path(self):
        os.makedirs(os.path.join(self.home, ".local/bin"))
        shutil.move(os.path.join(self.bin, "claude"), os.path.join(self.home, ".local/bin/claude"))
        done = self.run_runner("claude", env={"PATH": SYSTEM_PATH})
        if not self.ran() and shutil.which("claude", path=SYSTEM_PATH):
            self.skipTest("a real claude is on the system PATH")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_file_is_executable_and_has_nothing_personal(self):
        self.assertEqual(stat.S_IMODE(os.stat(RUNNER).st_mode), 0o755)
        with open(RUNNER, "rb") as handle:
            data = handle.read()
        text = data.decode("ascii")  # fails on anything non-ASCII
        self.assertTrue(text.startswith("#!/bin/bash\n"))
        self.assertNotIn("/home/", text)
        self.assertNotIn("/Users/", text)
        self.assertIsNone(re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text))
        self.assertIsNone(re.search(r"[0-9a-fA-F]{1,4}(:[0-9a-fA-F]{0,4}){4,7}", text))  # IPv6
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text))


if __name__ == "__main__":
    unittest.main()
