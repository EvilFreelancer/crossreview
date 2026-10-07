"""Tests for the crossreview helper and the detection scripts.

Run from the repository root:  python3 -m unittest discover -s tests -v

No reviewer CLI is called: the agents are stub executables on a private PATH,
and the reviewers in the run tests are a stub script driven by its arguments.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
HELPER = SCRIPTS / "crossreview.py"
IS_WINDOWS = os.name == "nt"
POWERSHELLS = [p for p in (shutil.which("pwsh"), shutil.which("powershell") if IS_WINDOWS else None) if p]

sys.path.insert(0, str(SCRIPTS))
import crossreview as cr  # noqa: E402

STUB_REVIEWER = textwrap.dedent('''
    import os, sys, time
    mode = sys.argv[1]
    brief = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    if mode == "ok":
        sys.stdout.write("1. **severity**: high\\n   **where**: `app/x.py:3`\\n   **problem**: boom\\n\\nVERDICT: approve with changes\\n")
    elif mode == "echo":
        sys.stdout.buffer.write(brief)
    elif mode == "depth":
        sys.stdout.write("depth=%s\\n" % os.environ.get("CROSSREVIEW_DEPTH"))
    elif mode == "pong":
        sys.stdout.write("PONG\\n")
    elif mode == "fail":
        sys.stderr.write("Error: You've hit your usage limit. Try again later.\\n")
        sys.exit(2)
    elif mode == "soft-fail":
        sys.stdout.write("Error code: 401 - invalid api key\\n")
    elif mode == "empty":
        pass
    elif mode == "slow":
        time.sleep(60)
        sys.stdout.write("too late\\n")
    elif mode == "short-401":
        sys.stdout.write("1. medium: app/auth.py:12 returns 401 for an expired token instead of 403.\\n")
    elif mode == "stubborn":
        import subprocess, signal
        child = subprocess.Popen([sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"])
        open(sys.argv[2], "w").write(str(child.pid))
        time.sleep(120)
''')


# Antigravity's side of the stream-json exchange, with the event shapes agy 1.3.0
# prints: one user event in, events out. The model name picks the behaviour.
STUB_AGY = textwrap.dedent('''
    import json, sys, time
    args = sys.argv[1:]
    model = args[args.index("--model") + 1]
    assert "-p=" in args and args[args.index("--input-format") + 1] == "stream-json", args
    content = json.loads(sys.stdin.buffer.readline().decode("ascii"))["message"]["content"]
    def emit(event, **body):
        sys.stdout.write(json.dumps(dict({"event": event}, **body)) + "\\n")
        sys.stdout.flush()
    def delta(index, text):
        emit("step_update", step_update={"conversation_id": "c1", "step_index": index, "state": "ACTIVE",
                                         "step_type": "agent_response", "text_delta": text})
    emit("init", conversation_id="c1", init={"model": model, "permission_mode": "request-review"})
    emit("step_update", step_update={"conversation_id": "c1", "step_index": 0, "state": "DONE",
                                     "step_type": "user_input"})
    if model == "echo":
        delta(1, content[:4])
        emit("result", result={"conversation_id": "c1", "status": "SUCCESS", "response": content, "num_turns": 1})
    elif model == "quota":
        emit("result", result={"status": "ERROR", "response": "", "error": "RESOURCE_EXHAUSTED: quota exceeded"})
        sys.exit(1)
    elif model == "cut-off":
        emit("result", result={"status": "ERROR", "response": "1. high: half a review", "error": "turn aborted"})
    elif model == "denied":
        delta(1, "Let me run a command first.")
        emit("result", result={"status": "SUCCESS", "response": "",
                               "denied_actions": [{"action": "command", "display_name": "RunCommand"}]})
    elif model == "died":
        delta(1, "thinking aloud")
        delta(2, "partial ")
        delta(2, "answer")
        sys.exit(3)
    elif model == "slow":
        time.sleep(60)
''')


def write_agy(bin_dir: Path) -> None:
    # A stub `agy` in bin_dir: an executable that runs STUB_AGY.
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "stub_agy.py"
    stub.write_text(STUB_AGY, encoding="utf-8")
    if IS_WINDOWS:
        (bin_dir / "agy.cmd").write_text('@"%s" "%s" %%*\r\n' % (sys.executable, stub), encoding="ascii")
    else:
        shim = bin_dir / "agy"
        shim.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, stub), encoding="utf-8")
        shim.chmod(0o755)


def write_stub(bin_dir: Path, name: str, version: str = "", help_text: str = "", rc: int = 0,
               models: str = "", models_out: str = "") -> None:
    """An executable that prints `version` for --version and `help_text` for --help."""
    if IS_WINDOWS:
        lines = ["@echo off",
                 'if "%~1"=="--version" goto version',
                 'if "%~1"=="--help" goto help']
        if models:
            lines.append('if "%%*"=="%s" goto models' % models)
        lines += ["exit /b 0", ":version"]
        lines += ["echo %s" % version if version else "rem", "exit /b %d" % rc, ":help"]
        lines += ["echo %s" % help_text if help_text else "rem", "exit /b %d" % rc]
        if models:
            lines += [":models", "echo %s" % models_out, "exit /b 0"]
        (bin_dir / (name + ".cmd")).write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
        return
    body = ["#!/bin/sh", 'case "$*" in']
    body.append("  --version) %s exit %d ;;" % ("echo '%s';" % version if version else "", rc))
    body.append("  --help) %s exit %d ;;" % ("echo '%s';" % help_text if help_text else "", rc))
    if models:
        body.append("  '%s') echo '%s'; exit 0 ;;" % (models, models_out))
    body += ["esac", "exit 0", ""]
    path = bin_dir / name
    path.write_text("\n".join(body), encoding="ascii")
    path.chmod(0o755)


def tools_path(tmp: Path) -> str:
    """A directory with the few system tools the detection scripts use, and nothing else."""
    tools = tmp / "tools"
    tools.mkdir()
    if IS_WINDOWS:
        return str(tools)
    for name in ("tr", "timeout", "gtimeout", "sleep", "sh", "cat"):
        found = shutil.which(name)
        if found:
            os.symlink(found, tools / name)
    return str(tools)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crossreview-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.env_backup = dict(os.environ)
        self.addCleanup(self._restore_env)
        for var in ("CROSSREVIEW_HOME", "CROSSREVIEW_HOST", "CLAUDECODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                    "XDG_CONFIG_HOME"):
            os.environ.pop(var, None)
        self.home = self.tmp / "userhome"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        # Runs without --run-dir land under the temp dir: keep them in ours.
        for var in ("TMPDIR", "TEMP", "TMP"):
            os.environ[var] = str(self.tmp)
        tempfile.tempdir = None
        self.addCleanup(setattr, tempfile, "tempdir", None)
        os.environ["CODDY_HOME"] = str(self.tmp / "coddy")
        os.environ.pop("CROSSREVIEW_ROSTER", None)
        os.environ.pop("CROSSREVIEW_DEPTH", None)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self.env_backup)

    def helper(self, *args, env=None, cwd=None, timeout=120):
        proc = subprocess.run([sys.executable, str(HELPER)] + [str(a) for a in args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env or os.environ.copy(),
                              cwd=str(cwd or self.tmp), timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


class TableTest(unittest.TestCase):
    def test_every_row_is_complete_and_keeps_the_brief_out_of_argv(self):
        table = cr.load_table()
        self.assertIn("claude", table)
        self.assertIn("cursor", table)
        for spec in table.values():
            for template in (spec.posix, spec.powershell):
                for placeholder in ("{bin}", "{brief}", "{out}"):
                    self.assertIn(placeholder, template, "%s: %s" % (spec.agent, template))
                if spec.agent != "koda":
                    self.assertIn("{model}", template, spec.agent)
                    self.assertNotIn("$(", template, "%s passes the brief as an argument" % spec.agent)
            self.assertTrue(spec.marker and spec.marker == spec.marker.lower(), spec.agent)

    def test_a_row_that_goes_through_a_script_names_one_that_exists(self):
        for windows in (False, True):
            template = cr.load_table()["antigravity"].template(windows)
            self.assertNotIn("{scripts}", template)
            self.assertIn('"%s' % cr.HERE, template)
        self.assertTrue((cr.HERE / "agy-stream.py").is_file())

    def test_cursor_prefers_the_agent_alias(self):
        self.assertEqual(cr.load_table()["cursor"].binaries[0], "agent")


class PlanCommandTest(unittest.TestCase):
    def test_posix_template(self):
        plan = cr.plan_command("claude -p --model sonnet --output-format text < {brief} > {out}")
        self.assertEqual(plan["mode"], "exec")
        self.assertEqual(plan["argv"], ["claude", "-p", "--model", "sonnet", "--output-format", "text"])
        self.assertEqual((plan["stdin"], plan["stdout"]), ("{brief}", "{out}"))

    def test_powershell_template(self):
        plan = cr.plan_command("Get-Content -Raw {brief} | codex exec -m gpt-5.6-sol - > {out}")
        self.assertEqual(plan["mode"], "exec")
        self.assertEqual(plan["argv"], ["codex", "exec", "-m", "gpt-5.6-sol", "-"])
        self.assertEqual(plan["stdin"], "{brief}")

    def test_brief_as_an_argument(self):
        plan = cr.plan_command('agent -p --trust --model auto "$(cat {brief})" > {out}')
        self.assertEqual(plan["mode"], "exec")
        self.assertIn("$(cat {brief})", plan["argv"])

    def test_cursor_agent_subcommand_and_dev_null(self):
        plan = cr.plan_command("devin -p --prompt-file {brief} < /dev/null > {out}")
        self.assertEqual(plan["stdin"], "/dev/null")

    def test_anything_else_goes_to_a_shell(self):
        for command in ("a < {brief} > {out}; rm -rf x", "a 2>/dev/null > {out}", "a && b > {out}",
                        "a < {brief} | b > {out}", "a >> {out}", "a &> {out}"):
            self.assertEqual(cr.plan_command(command)["mode"], "shell", command)


class ModelsParsingTest(unittest.TestCase):
    def test_codex_catalog_keeps_visible_slugs_by_priority(self):
        text = json.dumps({"models": [{"slug": "b", "visibility": "list", "priority": 5},
                                      {"slug": "hidden", "visibility": "hide", "priority": 1},
                                      {"slug": "a", "visibility": "list", "priority": 2}]})
        self.assertEqual(cr.parse_codex_models(text), ["a", "b"])

    def test_cursor_list(self):
        text = "Available models\n\nauto - Auto (current, default)\ngrok-4.7-low-fast - Grok 4.7  Low Fast​​\n" \
               "claude-opus-5-5-high - Claude Opus 5.5 1M High\n"
        self.assertEqual(cr.parse_cursor_models(text), ["auto", "grok-4.7-low-fast", "claude-opus-5-5-high"])

    def test_devin_list(self):
        text = ("Available models (2 families)\n\nSWE-2 (swe-2)\n  aliases: swe\n"
                "  swe-2-high                         SWE-2 High  [262K context, Free]\n"
                "  swe-2-medium                       SWE-2 Medium  [262K context, Free]\n")
        self.assertEqual(cr.parse_devin_models(text), ["swe-2-high", "swe-2-medium"])

    def test_antigravity_list(self):
        text = ("Fetching available models...\n\x1b[Kgemini-3.8-flash-high\tGemini 3.8 Flash (High)\r\n"
                "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\ngpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n")
        self.assertEqual(cr.parse_antigravity_models(text),
                         ["gemini-3.8-flash-high", "claude-sonnet-4-6", "gpt-oss-120b-medium"])
        self.assertEqual(cr.parse_antigravity_models("\x1b[1mgemini-3.1-pro-low\x1b[0m\tGemini 3.1 Pro (Low)\n"),
                         ["gemini-3.1-pro-low"])

    def test_opencode_list(self):
        self.assertEqual(cr.parse_opencode_models("opencode/big-pickle\nrpa/gpt-oss:120b\n\nnoise line\n"),
                         ["opencode/big-pickle", "rpa/gpt-oss:120b"])

    def test_coddy_config(self):
        text = textwrap.dedent("""\
            agent:
              model: codex/gpt-5.6-sol
            models:
              - max_context_tokens: 1000
                model: neuraldeep/qwen3.8-27b
              - model: "codex/gpt-5.6-sol"
                reasoning_default: high
            providers:
              - name: codex
                model: not-a-model-entry
            """)
        self.assertEqual(cr.parse_coddy_models(text), ["neuraldeep/qwen3.8-27b", "codex/gpt-5.6-sol"])


class ModelsCommandTest(TempCase):
    def test_a_malformed_codex_catalog_is_a_failed_listing(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        write_stub(bin_dir, "codex", version="codex-cli 1", models="debug models", models_out="null")
        env = dict(os.environ, PATH=os.pathsep.join([str(bin_dir), tools_path(self.tmp)]))
        rc, out, err = self.helper("models", "codex", env=env)
        self.assertEqual(rc, cr.EXIT_ERROR, out + err)
        self.assertIn("failed", err)
        self.assertNotIn("Traceback", err)


class DetectTest(TempCase):
    def stub_path(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        write_stub(bin_dir, "claude", version="2.1.285 (Claude Code)")
        write_stub(bin_dir, "codex", version="codex-cli 0.157.1", models="debug models", models_out='{"models": []}')
        write_stub(bin_dir, "devin", rc=1)                                  # on PATH, but not the agent
        write_stub(bin_dir, "agent", version="1.0", help_text="some other tool")  # unrelated `agent`
        write_stub(bin_dir, "cursor-agent", version="2026.10.01", help_text="Start the Cursor Agent")
        write_stub(bin_dir, "agy", version="1.3.0", help_text="Usage of agy:", models="models", models_out="m1")
        return os.pathsep.join([str(bin_dir), tools_path(self.tmp)])

    def test_python_detection(self):
        os.environ["PATH"] = self.stub_path()
        found = {d.agent: d for d in cr.detect(cr.load_table())}
        self.assertEqual(sorted(found), ["antigravity", "claude", "codex", "cursor"])
        self.assertEqual(found["antigravity"].models_cmd, "agy models")
        self.assertIn("agy-stream.py", found["antigravity"].template)
        self.assertEqual(found["claude"].models_cmd, "")
        self.assertEqual(found["codex"].models_cmd, "codex debug models")
        self.assertEqual(found["cursor"].binary, "cursor-agent")
        self.assertTrue(found["cursor"].template.startswith(
            ("cmd /d /c --% " if IS_WINDOWS else "") + "cursor-agent -p --trust"), found["cursor"].template)
        for d in found.values():
            self.assertNotIn("{bin}", d.template)
            self.assertNotIn("{scripts}", d.template)
            self.assertIn("{brief}", d.template)

    @unittest.skipIf(IS_WINDOWS, "detect-agents.sh is the POSIX twin")
    def test_shell_detection_matches_python(self):
        path = self.stub_path()
        env = dict(os.environ, PATH=path)
        sh = subprocess.run(["sh", str(SCRIPTS / "detect-agents.sh")], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(sh.returncode, 0, sh.stderr)
        rc, out, err = self.helper("detect", env=env)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sh.stdout.decode(), out)
        self.assertEqual(len(out.strip().splitlines()), 4)

    @unittest.skipUnless(POWERSHELLS, "no PowerShell")
    def test_powershell_detection_matches_python(self):
        path = self.stub_path()
        env = dict(os.environ, PATH=path)
        table = cr.load_table()
        for exe in POWERSHELLS:
            with self.subTest(powershell=exe):
                ps = subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                     str(SCRIPTS / "detect-agents.ps1")], env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=180)
                self.assertEqual(ps.returncode, 0, ps.stderr)
                lines = sorted(ps.stdout.decode().strip().splitlines())
                self.assertEqual([ln.split("\t")[0] for ln in lines],
                                 ["antigravity", "claude", "codex", "cursor"], lines)
                for ln in lines:
                    agent, _, models_cmd, template = ln.split("\t")
                    self.assertEqual(template, cr.fill(table[agent].template(True), bin={
                        "cursor": "cursor-agent", "antigravity": "agy"}.get(agent, agent)))
                    if agent == "codex":
                        self.assertEqual(models_cmd, "codex debug models")


@unittest.skipUnless(IS_WINDOWS, "cmd.exe redirection is the Windows half")
class PowerShellTemplateTest(TempCase):
    # What Coddy wraps around every command it runs through PowerShell.
    PROLOGUE = ("$ProgressPreference = 'SilentlyContinue'\r\n"
                "try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }\r\n"
                "$OutputEncoding = New-Object System.Text.UTF8Encoding $false\r\n")
    EPILOGUE = ("\r\n$__ok = $?\r\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }\r\n"
                "if (-not $__ok) { exit 1 }\r\nexit 0\r\n")

    def test_brief_and_review_bytes_survive(self):
        bin_dir = self.tmp / "bin dir"
        bin_dir.mkdir()
        echo = bin_dir / "echo_stdin.py"
        echo.write_text("import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8")
        (bin_dir / "claude.cmd").write_text('@"%s" "%s" %%*\r\n' % (sys.executable, echo), encoding="ascii")
        work = self.tmp / "run dir"
        work.mkdir()
        brief = work / "brief.md"
        payload = "Brief: значение, 値, café\nline two\n".encode("utf-8")
        brief.write_bytes(payload)
        table = cr.load_table()
        env = dict(os.environ, PATH=os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
        for exe in POWERSHELLS:
            with self.subTest(powershell=exe):
                out = work / ("review-%s.md" % Path(exe).stem)
                command = cr.fill(table["claude"].powershell, bin="claude", model="sonnet",
                                  brief='"%s"' % brief, out='"%s"' % out)
                proc = subprocess.run([exe, "-NoProfile", "-Command", self.PROLOGUE + command + self.EPILOGUE],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(out.read_bytes(), payload)

    def test_the_antigravity_adapter_runs_from_a_plain_roster_command(self):
        bin_dir = self.tmp / "bin dir"
        write_agy(bin_dir)
        work = self.tmp / "run dir"
        work.mkdir()
        brief = work / "brief.md"
        payload = "Brief: значение, 値, café\nline two\n".encode("utf-8")
        brief.write_bytes(payload)
        env = dict(os.environ, PATH=os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
        for exe in POWERSHELLS:
            with self.subTest(powershell=exe):
                out = work / ("review-%s.md" % Path(exe).stem)
                command = cr.fill(cr.load_table()["antigravity"].template(True), bin="agy", model="echo",
                                  brief='"%s"' % brief, out='"%s"' % out)
                proc = subprocess.run([exe, "-NoProfile", "-Command", self.PROLOGUE + command + self.EPILOGUE],
                                      env=env, cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(out.read_bytes(), payload)


class RosterTest(TempCase):
    def make_repo(self) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(repo), check=True,
                                   stdout=subprocess.PIPE).stdout.decode().strip())

    def stub_env(self, *agents) -> dict:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        banners = {"claude": ("claude", "Claude Code 2", ""), "cursor": ("agent", "2026.10.01", "Cursor Agent")}
        for agent in agents:
            name, version, help_text = banners[agent]
            write_stub(bin_dir, name, version=version, help_text=help_text)
        return dict(os.environ, PATH=os.pathsep.join([str(bin_dir), tools_path(self.tmp)]))

    def test_the_agent_says_who_it_is(self):
        repo = self.make_repo()
        rc, _, err = self.helper("roster", cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("--host", err)
        rc, out, _ = self.helper("roster", cwd=repo, env=dict(os.environ, CLAUDECODE="1"))
        self.assertEqual(rc, cr.EXIT_NO_ROSTER, out)
        self.assertIn(os.path.join(".claude", "crossreview.json"), out)
        rc, out, _ = self.helper("roster", cwd=repo, env=dict(os.environ, CROSSREVIEW_HOST="cursor-agent"))
        self.assertIn(os.path.join(".cursor", "crossreview.json"), out)

    def test_each_agent_keeps_its_rosters_in_its_own_folders(self):
        repo = self.make_repo()
        os.environ["CODDY_HOME"] = str(self.tmp / "coddy-home")
        expected = {"claude": self.home / ".claude", "cursor": self.home / ".cursor", "codex": self.home / ".codex",
                    "coddy": self.tmp / "coddy-home", "kimi": self.home / ".kimi",
                    "opencode": self.home / ".config" / "opencode", "windsurf": self.home / ".agents",
                    "antigravity": self.home / ".gemini" / "antigravity-cli"}
        for host, folder in expected.items():
            self.assertEqual(cr.global_roster_path(host), folder / "crossreview.json", host)
        self.assertEqual(cr.local_roster_path("claude", repo), repo / ".claude" / "crossreview.json")
        self.assertEqual(cr.local_roster_path("coddy", repo), repo / ".coddy" / "crossreview.json")
        self.assertEqual(cr.local_roster_path("windsurf", repo), repo / ".agents" / "crossreview.json")
        self.assertEqual(cr.local_roster_path("antigravity", repo), repo / ".agents" / "crossreview.json")
        self.assertEqual(cr.resolve_host("agy"), "antigravity")
        os.environ["CODEX_HOME"] = str(self.tmp / "cx")
        self.assertEqual(cr.global_roster_path("codex"), self.tmp / "cx" / "crossreview.json")
        os.environ["CROSSREVIEW_HOME"] = str(self.tmp / "shared")
        self.assertEqual(cr.global_roster_path("claude"), self.tmp / "shared" / "crossreview.json")

    def test_setup_writes_global_or_local_and_the_project_wins(self):
        repo = self.make_repo()
        env = self.stub_env("claude", "cursor")
        rc, out, err = self.helper("init", "--host", "claude", "--scope", "global", "cursor:auto", env=env, cwd=repo)
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.home / ".claude" / "crossreview.json").is_file())
        rc, out, _ = self.helper("roster", "--host", "claude", cwd=repo)
        self.assertEqual(rc, 0, out)
        self.assertIn("global: every project, for claude", out)
        rc, out, err = self.helper("init", "--host", "claude", "--scope", "local", "claude:sonnet", env=env, cwd=repo)
        self.assertEqual(rc, 0, err)
        self.assertTrue((repo / ".claude" / "crossreview.json").is_file())
        rc, out, _ = self.helper("roster", "--host", "claude", cwd=repo)
        self.assertEqual(rc, 0, out)
        self.assertIn("local: this project, for claude", out)
        self.assertIn("--model sonnet", out)
        rc, out, _ = self.helper("roster", "--host", "claude", "--scope", "global", cwd=repo)
        self.assertIn("--model auto", out)
        rc, out, _ = self.helper("roster", "--host", "coddy", cwd=repo)
        self.assertEqual(rc, cr.EXIT_NO_ROSTER, "claude's rosters are not coddy's")

    def test_init_needs_a_scope(self):
        repo = self.make_repo()
        rc, _, err = self.helper("init", "--host", "claude", "cursor:auto", cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("--scope local", err)
        self.assertIn("--scope global", err)

    def test_coddy_keeps_its_original_places(self):
        repo = self.make_repo()
        coddy = self.tmp / "coddy-home"
        coddy.mkdir()
        os.environ["CODDY_HOME"] = str(coddy)
        (coddy / "crossreview.json").write_text(json.dumps({"version": 1, "reviewers": [
            {"kind": "cli", "agent": "cursor", "binary": "agent", "model": "auto",
             "command": 'agent -p --trust --mode plan --model auto "$(cat {brief})" > {out}'},
            {"kind": "internal", "definition": "explore", "model": "devin/swe-2"}]}))
        rc, out, _ = self.helper("roster", "--host", "coddy", cwd=repo)
        self.assertEqual(rc, 0, out)
        self.assertIn("global: every project, for coddy", out)
        self.assertIn("passes the brief as an argument", out)
        self.assertIn("internal reviewer of coddy", out)
        (repo / ".coddy").mkdir()
        (repo / ".coddy" / "crossreview.json").write_text(json.dumps({"reviewers": []}))
        rc, out, _ = self.helper("roster", "--host", "coddy", cwd=repo)
        self.assertEqual(rc, cr.EXIT_NEEDS_APPROVAL, out)

    def test_a_project_roster_from_a_clone_needs_approval(self):
        repo = self.make_repo()
        (repo / ".claude").mkdir()
        roster = repo / ".claude" / "crossreview.json"
        roster.write_text(json.dumps({"reviewers": [{"kind": "cli", "command": "evil < {brief} > {out}"}]}))
        rc, out, _ = self.helper("roster", "--host", "claude", cwd=repo)
        self.assertEqual(rc, cr.EXIT_NEEDS_APPROVAL, out)
        self.assertIn("evil", out)
        shown = out.split("--sha256 ")[1].split("`")[0].strip()
        brief = self.tmp / "brief.md"
        brief.write_text("x")
        rc, out, err = self.helper("run", "--host", "claude", "--brief", brief, cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("not approved", err)
        rc, out, err = self.helper("trust", "--host", "claude", "--sha256", shown, cwd=repo)
        self.assertEqual(rc, 0, out + err)
        rc, out, _ = self.helper("roster", "--host", "claude", cwd=repo)
        self.assertEqual(rc, 0, out)
        roster.write_text(json.dumps({"reviewers": [{"kind": "cli", "command": "worse < {brief} > {out}"}]}))
        rc, out, _ = self.helper("roster", "--host", "claude", cwd=repo)
        self.assertEqual(rc, cr.EXIT_NEEDS_APPROVAL, out)
        rc, out, _ = self.helper("roster", "--host", "claude", "--scope", "global", cwd=repo)
        self.assertEqual(rc, cr.EXIT_NO_ROSTER, out)

    def test_trust_refuses_a_file_changed_since_it_was_shown(self):
        repo = self.make_repo()
        (repo / ".claude").mkdir()
        roster = repo / ".claude" / "crossreview.json"
        roster.write_text(json.dumps({"reviewers": [{"kind": "cli", "command": "shown < {brief} > {out}"}]}))
        rc, out, _ = self.helper("roster", "--host", "claude", cwd=repo)
        shown = out.split("--sha256 ")[1].split("`")[0].strip()
        roster.write_text(json.dumps({"reviewers": [{"kind": "cli", "command": "swapped < {brief} > {out}"}]}))
        rc, _, err = self.helper("trust", "--host", "claude", "--sha256", shown, cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("changed since it was shown", err)

    def test_a_roster_named_inside_the_workspace_needs_approval(self):
        repo = self.make_repo()
        roster = repo / "review.json"
        roster.write_text(json.dumps({"reviewers": [{"kind": "cli", "command": "evil < {brief} > {out}"}]}))
        rc, out, _ = self.helper("roster", "--host", "claude", "--roster", roster, cwd=repo)
        self.assertEqual(rc, cr.EXIT_NEEDS_APPROVAL, out)
        env = dict(os.environ, CROSSREVIEW_ROSTER=str(roster), CROSSREVIEW_HOST="claude")
        rc, out, _ = self.helper("roster", cwd=repo, env=env)
        self.assertEqual(rc, cr.EXIT_NEEDS_APPROVAL, out)
        rc, out, err = self.helper("trust", "--host", "claude", "--roster", roster, cwd=repo)
        self.assertEqual(rc, 0, out + err)
        rc, out, _ = self.helper("roster", "--host", "claude", "--roster", roster, cwd=repo)
        self.assertEqual(rc, 0, out)
        outside = self.tmp / "mine.json"
        outside.write_text(json.dumps({"reviewers": []}))
        rc, out, _ = self.helper("roster", "--roster", outside, cwd=repo)
        self.assertEqual(rc, 0, out)

    def test_init_from_detected_agents_and_refresh_on_import(self):
        repo = self.make_repo()
        env = self.stub_env("claude", "cursor")
        rc, out, err = self.helper("init", "--host", "claude", "--scope", "global", "claude:sonnet", "cursor:auto",
                                   "internal/claude:opus", env=env, cwd=repo)
        self.assertEqual(rc, 0, err)
        target = self.home / ".claude" / "crossreview.json"
        data = json.loads(target.read_text())
        commands = [r.get("command") for r in data["reviewers"]]
        expected = "claude -p --model sonnet --output-format text --permission-mode plan < {brief} > {out}"
        self.assertIn(("cmd /d /c --% " + expected) if IS_WINDOWS else expected, commands)
        self.assertEqual(data["reviewers"][2], {"kind": "internal", "host": "claude", "model": "opus"})
        rc, _, err = self.helper("init", "--host", "claude", "--scope", "global", "claude:opus", env=env, cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("already exists", err)
        legacy = self.tmp / "legacy.json"
        legacy.write_text(json.dumps({"version": 1, "reviewers": [
            {"kind": "cli", "agent": "cursor", "binary": "agent", "model": "auto", "prompt_via": "arg",
             "command": 'agent -p --trust --mode plan --model auto "$(cat {brief})" > {out}'},
            {"kind": "internal", "definition": "explore", "model": "devin/swe-2"}]}))
        rc, out, err = self.helper("init", "--host", "claude", "--scope", "global", "--import", legacy, "--refresh",
                                   "--force", env=env, cwd=repo)
        self.assertEqual(rc, 0, err)
        data = json.loads(target.read_text())
        self.assertNotIn("$(cat", data["reviewers"][0]["command"])
        self.assertIn("--mode ask", data["reviewers"][0]["command"])
        self.assertNotIn("prompt_via", data["reviewers"][0])
        self.assertEqual(data["reviewers"][1]["host"], "coddy")

    def test_import_add_keeps_what_was_there(self):
        repo = self.make_repo()
        target = self.home / ".claude" / "crossreview.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"version": 1, "reviewers": [{"name": "mine", "command": "a < {brief} > {out}"}]}))
        legacy = self.tmp / "legacy.json"
        legacy.write_text(json.dumps({"version": 1, "reviewers": [{"name": "theirs", "command": "b < {brief} > {out}"}]}))
        rc, _, err = self.helper("init", "--host", "claude", "--scope", "global", "--import", legacy, "--add", cwd=repo)
        self.assertEqual(rc, 0, err)
        names = [r["name"] for r in json.loads(target.read_text())["reviewers"]]
        self.assertEqual(names, ["mine", "theirs"])

    def test_a_model_id_is_never_shell(self):
        repo = self.make_repo()
        env = self.stub_env("claude")
        for bad in ("sonnet; rm -rf ~", "x & y", "a$(id)", "m`id`", "a b"):
            rc, _, err = self.helper("init", "--host", "claude", "--scope", "global", "claude:%s" % bad, env=env,
                                     cwd=repo)
            self.assertEqual(rc, cr.EXIT_ERROR, bad)
            self.assertIn("characters a command line would interpret", err)
        rc, _, err = self.helper("init", "--host", "claude", "--scope", "global",
                                 "claude:claude-opus-4-8[context=1m,effort=high]", env=env, cwd=repo)
        self.assertEqual(rc, 0, err)

    @unittest.skipIf(IS_WINDOWS, "ownership and modes are the POSIX half")
    def test_the_run_root_is_private(self):
        root = cr.run_root()
        self.assertEqual(root.parent, self.tmp)
        self.assertEqual(os.stat(str(root)).st_mode & 0o777, 0o700)
        os.chmod(str(root), 0o755)
        cr.run_root()
        self.assertEqual(os.stat(str(root)).st_mode & 0o777, 0o700)
        run = cr.new_run_dir(None)
        self.assertTrue(cr.RUN_NAME.match(run.name), run.name)
        self.assertEqual(os.stat(str(run)).st_mode & 0o777, 0o700)
        shutil.rmtree(str(root))
        os.symlink(str(self.tmp / "elsewhere"), str(root))
        with self.assertRaises(cr.CrossreviewError):
            cr.run_root()

    def test_unknown_agent_and_missing_model(self):
        repo = self.make_repo()
        rc, _, err = self.helper("init", "--host", "claude", "--scope", "global", "nosuch:model", cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("unknown agent", err)
        rc, _, err = self.helper("init", "--host", "claude", "--scope", "global", "internal:opus", cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("internal/<host>:<model>", err)


class BriefTest(TempCase):
    def git(self, repo, *args):
        subprocess.run(["git", "-C", str(repo)] + list(args), check=True, stdout=subprocess.DEVNULL)

    def make_repo(self) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.email", "t@example.com")
        self.git(repo, "config", "user.name", "t")
        self.git(repo, "config", "core.autocrlf", "false")
        (repo / "app.py").write_text("def mean(v):\n    return sum(v) / len(v)\n")
        (repo / "README.md").write_text("# Demo\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "init")
        return repo

    def test_uncommitted_work_with_untracked_files(self):
        repo = self.make_repo()
        (repo / "app.py").write_text("def mean(v):\n    return sum(v) / (len(v) - 1)\n")
        (repo / "notes.md").write_text("Text with a fence:\n```\ncode\n```\nи кириллица\n", encoding="utf-8")
        (repo / "blob.bin").write_bytes(b"\0\1\2")
        out = self.tmp / "brief.md"
        rc, stdout, err = self.helper("brief", "--out", out, "--intent", "Fix the mean.", cwd=repo)
        self.assertEqual(rc, 0, err)
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("IMPORTANT: answer from this brief alone."))
        self.assertIn("Fix the mean.", text)
        self.assertIn("+    return sum(v) / (len(v) - 1)", text)
        self.assertIn("+++ b/notes.md", text)
        self.assertIn("+и кириллица", text)
        self.assertIn("blob.bin", text.split("left out")[-1])
        self.assertIn("````diff", text, "the fence must outgrow the ``` inside the change")
        self.assertIn("VERDICT: approve", text)
        self.assertIn("2 file(s)", stdout)

    def test_range_staged_doc_and_read_tools(self):
        repo = self.make_repo()
        (repo / "app.py").write_text("def mean(v):\n    return 0\n")
        self.git(repo, "add", "app.py")
        out = self.tmp / "b.md"
        rc, _, err = self.helper("brief", "--out", out, "--staged", "--tools", "read", "--language", "Russian",
                                 cwd=repo)
        self.assertEqual(rc, 0, err)
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("IMPORTANT: you may read"))
        self.assertIn("Write in Russian.", text)
        self.assertIn("+    return 0", text)
        self.git(repo, "commit", "-qm", "two")
        rc, _, err = self.helper("brief", "--out", out, "--range", "HEAD~1..HEAD", cwd=repo)
        self.assertEqual(rc, 0, err)
        self.assertIn("diff HEAD~1..HEAD", out.read_text(encoding="utf-8"))
        plan = self.tmp / "PLAN.md"
        plan.write_text("# Plan\nDo the thing.\n")
        rc, _, err = self.helper("brief", "--out", out, "--doc", plan, cwd=repo)
        self.assertEqual(rc, 0, err)
        self.assertIn("Do the thing.", out.read_text(encoding="utf-8"))

    def test_orchestrator_notes_and_exclude(self):
        repo = self.make_repo()
        (repo / "app.py").write_text("x = 1\n")
        (repo / "README.md").write_text("# Changed\n")
        notes = self.tmp / "notes.md"
        notes.write_text("- `x` is read only by the tests on purpose.\n")
        out = self.tmp / "b.md"
        rc, _, err = self.helper("brief", "--out", out, "--notes-file", notes, "--exclude", "README.md", cwd=repo)
        self.assertEqual(rc, 0, err)
        text = out.read_text(encoding="utf-8")
        self.assertIn("Notes from the orchestrator", text)
        self.assertIn("read only by the tests on purpose", text)
        self.assertIn("without seeing each other's answers", text)
        self.assertNotIn("# Changed", text)

    def test_exclude_belongs_to_a_git_scope(self):
        repo = self.make_repo()
        rc, _, err = self.helper("brief", "--out", self.tmp / "b.md", "--files", "app.py", "--exclude", "x",
                                 cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("--exclude filters a git scope", err)
        rc, _, err = self.helper("brief", "--out", self.tmp / "b.md", "--doc", self.tmp / "missing.md", cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("does not exist", err)

    def test_empty_scope_is_an_error(self):
        repo = self.make_repo()
        rc, _, err = self.helper("brief", "--out", self.tmp / "b.md", cwd=repo)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("nothing to review", err)


class RunTest(TempCase):
    def setUp(self):
        super().setUp()
        self.stub = self.tmp / "stub_reviewer.py"
        self.stub.write_text(STUB_REVIEWER, encoding="utf-8")
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("Review this: значение\n", encoding="utf-8")

    def command(self, mode: str, stdin: bool = True) -> str:
        cmd = '"%s" "%s" %s' % (Path(sys.executable).as_posix(), self.stub.as_posix(), mode)
        return cmd + (" < {brief}" if stdin else "") + " > {out}"

    def roster(self, entries, **top) -> Path:
        path = self.tmp / "roster.json"
        data = {"version": 1, "reviewers": entries}
        data.update(top)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_and_wait(self, roster: Path, *extra, wait_max=60):
        run_dir = self.tmp / "run"
        rc, out, err = self.helper("run", "--brief", self.brief, "--roster", roster, "--run-dir", run_dir, *extra)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.helper("wait", run_dir, "--max", wait_max, timeout=wait_max + 30)
        status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
        return rc, out, status, run_dir

    def test_outcomes_are_told_apart(self):
        roster = self.roster([
            {"name": "good", "command": self.command("ok")},
            {"name": "broken", "command": self.command("fail")},
            {"name": "soft", "command": self.command("soft-fail")},
            {"name": "silent", "command": self.command("empty")},
            {"name": "slow", "command": self.command("slow"), "timeout": 3},
            {"name": "gone", "command": "no-such-reviewer-binary < {brief} > {out}"},
            {"name": "off", "command": self.command("ok"), "enabled": False},
        ])
        rc, out, status, run_dir = self.run_and_wait(roster)
        self.assertEqual(rc, 0, out)
        by_name = {r["name"]: r for r in status["reviewers"]}
        self.assertNotIn("off", by_name)
        self.assertEqual(by_name["good"]["status"], "done")
        self.assertEqual(by_name["good"]["verdict"], "approve with changes")
        self.assertEqual(by_name["broken"]["status"], "failed")
        self.assertEqual(by_name["broken"]["rc"], 2)
        self.assertEqual(by_name["broken"]["note"], "usage or rate limit")
        self.assertEqual(by_name["soft"]["status"], "failed")
        self.assertEqual(by_name["soft"]["note"], "not signed in or invalid credentials")
        self.assertEqual(by_name["silent"]["status"], "empty")
        self.assertEqual(by_name["slow"]["status"], "timeout")
        self.assertEqual(by_name["gone"]["status"], "missing")
        self.assertIn("finished: 1 of 6 CLI reviewers answered", out)
        self.assertIn("INSUFFICIENT QUORUM", out)
        rc, collected, _ = self.helper("collect", run_dir)
        self.assertEqual(rc, 0)
        self.assertIn("**problem**: boom", collected)
        self.assertIn("usage limit", collected)

    def test_wait_is_bounded_and_stop_ends_a_reviewer(self):
        roster = self.roster([{"name": "slow", "command": self.command("slow")},
                              {"name": "good", "command": self.command("ok")}])
        run_dir = self.tmp / "run"
        rc, out, err = self.helper("run", "--brief", self.brief, "--roster", roster, "--run-dir", run_dir)
        self.assertEqual(rc, 0, out + err)
        started = time.time()
        rc, out, _ = self.helper("wait", run_dir, "--max", "3")
        self.assertEqual(rc, cr.EXIT_RUNNING, out)
        self.assertLess(time.time() - started, 30)
        self.assertIn("still running: slow", out)
        rc, out, _ = self.helper("stop", run_dir, "slow")
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.helper("wait", run_dir, "--max", "30")
        self.assertEqual(rc, 0, out)
        status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual({r["name"]: r["status"] for r in status["reviewers"]}, {"slow": "stopped", "good": "done"})
        self.assertIn("stopped on request", out)
        self.assertNotIn("rc=", out)

    def test_brief_bytes_reach_the_reviewer_and_depth_is_set(self):
        roster = self.roster([{"name": "echo", "command": self.command("echo")},
                              {"name": "depth", "command": self.command("depth", stdin=False)},
                              {"name": "arg", "command": '"%s" -c "import sys; sys.stdout.buffer.write('
                                                         'sys.argv[1].encode())" "$(cat {brief})" > {out}'
                                                         % Path(sys.executable).as_posix()}])
        rc, out, status, run_dir = self.run_and_wait(roster)
        self.assertEqual(rc, 0, out)
        reviews = run_dir / "reviews"
        self.assertEqual((reviews / "echo.md").read_bytes(), self.brief.read_bytes())
        self.assertEqual((reviews / "depth.md").read_text().strip(), "depth=1")
        self.assertIn("значение", (reviews / "arg.md").read_text(encoding="utf-8"))

    def test_retry_runs_a_reviewer_again_and_keeps_the_rest(self):
        flag = self.tmp / "second-try"
        script = self.tmp / "flaky.py"
        script.write_text(textwrap.dedent('''
            import os, sys
            flag = sys.argv[1]
            if not os.path.exists(flag):
                open(flag, "w").close()
                sys.stderr.write("stream error: connection reset by peer\\n")
                sys.exit(1)
            sys.stdout.write("1. low: fine\\nVERDICT: approve\\n")
        '''), encoding="utf-8")
        roster = self.roster([{"name": "good", "command": self.command("ok")},
                              {"name": "flaky", "command": '"%s" "%s" "%s" > {out}' % (
                                  Path(sys.executable).as_posix(), script.as_posix(), flag.as_posix())}])
        rc, out, status, run_dir = self.run_and_wait(roster)
        self.assertEqual({r["name"]: r["status"] for r in status["reviewers"]}, {"good": "done", "flaky": "failed"})
        rc, out, err = self.helper("retry", run_dir, "flaky", "--timeout", "60")
        self.assertEqual(rc, 0, out + err)
        rc, out, _ = self.helper("wait", run_dir, "--max", "60", timeout=90)
        self.assertEqual(rc, 0, out)
        status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
        by_name = {r["name"]: r for r in status["reviewers"]}
        self.assertEqual(by_name["flaky"]["status"], "done")
        self.assertEqual(by_name["flaky"]["verdict"], "approve")
        self.assertEqual(by_name["good"]["status"], "done")
        self.assertTrue((run_dir / "reviews" / "flaky.attempt1.err").exists())
        self.assertIn("finished: 2 of 2", out)

    def test_a_dead_supervisor_is_reported_not_waited_for(self):
        roster = self.roster([{"name": "slow", "command": self.command("slow")}])
        run_dir = self.tmp / "run"
        rc, out, err = self.helper("run", "--brief", self.brief, "--roster", roster, "--run-dir", run_dir)
        self.assertEqual(rc, 0, out + err)
        deadline = time.time() + 20
        status = {}
        while time.time() < deadline:
            status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            if status["reviewers"][0].get("pid"):
                break
            time.sleep(0.2)
        reviewer_pid = status["reviewers"][0]["pid"]
        cr.kill_tree(int(status["supervisor_pid"]))
        self.addCleanup(cr.kill_tree, int(reviewer_pid))
        rc, out, _ = self.helper("wait", run_dir, "--max", "40", timeout=90)
        self.assertEqual(rc, 0, out)
        self.assertIn("lost", out)
        self.assertIn("the supervisor is gone", out)
        deadline = time.time() + 10
        while time.time() < deadline and cr.pid_alive(int(reviewer_pid)):
            time.sleep(0.2)
        self.assertFalse(cr.pid_alive(int(reviewer_pid)), "a lost run's reviewers are stopped")
        self.assertTrue(json.loads((run_dir / "status.json").read_text(encoding="utf-8")).get("lost"))

    def test_a_short_review_about_401s_is_a_review(self):
        roster = self.roster([{"name": "short", "command": self.command("short-401")},
                              {"name": "soft", "command": self.command("soft-fail")}])
        rc, out, status, _ = self.run_and_wait(roster)
        by_name = {r["name"]: r for r in status["reviewers"]}
        self.assertEqual(by_name["short"]["status"], "done", out)
        self.assertEqual(by_name["soft"]["status"], "failed", out)

    @unittest.skipIf(IS_WINDOWS, "process groups are the POSIX half; taskkill /T covers Windows")
    def test_a_timeout_kills_children_that_ignore_sigterm(self):
        pidfile = self.tmp / "child.pid"
        roster = self.roster([{"name": "stubborn", "timeout": 3,
                               "command": '"%s" "%s" stubborn "%s" > {out}' % (
                                   Path(sys.executable).as_posix(), self.stub.as_posix(), pidfile.as_posix())}])
        rc, out, status, _ = self.run_and_wait(roster)
        self.assertEqual(status["reviewers"][0]["status"], "timeout", out)
        child = int(pidfile.read_text())
        deadline = time.time() + 10
        while time.time() < deadline and cr.pid_alive(child):
            time.sleep(0.2)
        self.assertFalse(cr.pid_alive(child), "the child that ignored SIGTERM must be gone")

    def test_a_brief_too_long_for_an_argument_is_refused_up_front(self):
        self.brief.write_text("x" * (cr.ARG_LIMIT + 10), encoding="utf-8")
        roster = self.roster([{"name": "arg", "command": '"%s" -c "print(1)" "$(cat {brief})" > {out}'
                               % Path(sys.executable).as_posix()},
                              {"name": "good", "command": self.command("ok")}])
        rc, out, status, _ = self.run_and_wait(roster)
        by_name = {r["name"]: r for r in status["reviewers"]}
        self.assertEqual(by_name["arg"]["status"], "failed")
        self.assertIn("command-line argument", by_name["arg"]["note"])
        self.assertEqual(by_name["good"]["status"], "done")

    def test_model_placeholder_and_unsafe_names(self):
        roster = self.roster([{"name": "../../evil", "model": "m1",
                               "command": '"%s" -c "import sys; print(sys.argv[1])" {model} > {out}'
                               % Path(sys.executable).as_posix()}])
        rc, out, status, run_dir = self.run_and_wait(roster)
        r = status["reviewers"][0]
        self.assertEqual(r["name"], "evil")
        self.assertEqual(Path(r["out"]).parent.resolve(), (run_dir / "reviews").resolve())
        self.assertEqual(Path(r["out"]).read_text().strip(), "m1")

    def test_a_reviewer_cannot_start_another_crossreview(self):
        roster = self.roster([{"name": "good", "command": self.command("ok")}])
        env = dict(os.environ, CROSSREVIEW_DEPTH="1")
        rc, _, err = self.helper("run", "--brief", self.brief, "--roster", roster, env=env)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("review the brief directly", err)

    def test_internal_reviewers_belong_to_their_host(self):
        roster = self.roster([{"name": "good", "command": self.command("ok")},
                              {"kind": "internal", "host": "claude", "model": "sonnet"},
                              {"kind": "internal", "host": "coddy", "model": "devin/swe-2"}], min_reviewers=2)
        rc, out, status, _ = self.run_and_wait(roster, "--host", "claude")
        self.assertEqual(rc, 0, out)
        by_name = {r["name"]: r for r in status["reviewers"]}
        self.assertEqual(by_name["claude-sonnet"]["status"], "host")
        self.assertEqual(by_name["coddy-devin-swe-2"]["status"], "skipped")
        self.assertIn("quorum met once the internal reviewers you run answer", out)

    def test_a_run_of_internal_reviewers_only(self):
        roster = self.roster([{"kind": "internal", "host": "claude", "model": "sonnet"}], min_reviewers=1)
        rc, out, status, _ = self.run_and_wait(roster, "--host", "claude")
        self.assertEqual(rc, 0, out)
        self.assertEqual(status["reviewers"][0]["status"], "host")

    def test_listing_shows_paths_relative_to_the_run(self):
        roster = self.roster([{"name": "good", "command": self.command("ok")}])
        run_dir = self.tmp / "run"
        rc, out, err = self.helper("run", "--brief", self.brief, "--roster", roster, "--run-dir", run_dir,
                                   "--foreground")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("< brief.md > reviews/good.md" if not IS_WINDOWS else "brief.md", out)
        self.assertIn("quorum", out.lower())

    def test_old_finished_runs_are_pruned_and_nothing_else(self):
        root = self.tmp / "root"
        root.mkdir()
        old_done, old_running, foreign = root / "20250101-000000-aaaa", root / "20250101-000000-bbbb", root / "keep-me"
        for path, done in ((old_done, True), (old_running, False), (foreign, True)):
            path.mkdir()
            (path / "status.json").write_text(json.dumps({"done": done, "updated": 1000}), encoding="utf-8")
            os.utime(str(path), (1000, 1000))
        cr.prune_old_runs(root)
        self.assertFalse(old_done.exists())
        self.assertTrue(old_running.exists())
        self.assertTrue(foreign.exists())

    def test_a_run_directory_holds_one_run(self):
        roster = self.roster([{"name": "good", "command": self.command("ok")}])
        rc, out, status, run_dir = self.run_and_wait(roster)
        self.assertEqual(rc, 0, out)
        rc, _, err = self.helper("run", "--brief", self.brief, "--roster", roster, "--run-dir", run_dir)
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("already holds a run", err)

    def test_internal_reviewers_keep_their_definition_and_reasoning(self):
        roster = self.roster([{"kind": "internal", "host": "coddy", "definition": "explore",
                               "model": "devin/swe-2", "reasoning": "high"}], min_reviewers=1)
        rc, out, status, _ = self.run_and_wait(roster, "--host", "coddy")
        r = status["reviewers"][0]
        self.assertEqual((r["status"], r["definition"], r["reasoning"]), ("host", "explore", "high"))
        self.assertIn("explore", r["note"])

    @unittest.skipUnless(IS_WINDOWS, "batch-file shims are the Windows half")
    def test_a_batch_file_reviewer_on_windows(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        echo = bin_dir / "echo_stdin.py"
        echo.write_text("import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8")
        (bin_dir / "fakecli.cmd").write_text('@"%s" "%s" %%*\r\n' % (sys.executable, echo), encoding="ascii")
        os.environ["PATH"] = os.pathsep.join([str(bin_dir), os.environ["PATH"]])
        roster = self.roster([{"name": "shim", "command": "cmd /d /c --% fakecli -p < {brief} > {out}"},
                              {"name": "shim-arg", "command": 'fakecli "$(Get-Content -Raw -Encoding UTF8 {brief})" > {out}'}])
        rc, out, status, run_dir = self.run_and_wait(roster)
        by_name = {r["name"]: r for r in status["reviewers"]}
        self.assertEqual(by_name["shim"]["status"], "done", out)
        self.assertEqual((run_dir / "reviews" / "shim.md").read_bytes(), self.brief.read_bytes())
        self.assertEqual(by_name["shim-arg"]["status"], "failed")
        self.assertIn("batch file", by_name["shim-arg"]["note"])

    def agy_command(self, model: str) -> str:
        """The table's own antigravity template, with a stub `agy` first on PATH."""
        bin_dir = self.tmp / "agy-bin"
        if not bin_dir.exists():
            write_agy(bin_dir)
            os.environ["PATH"] = os.pathsep.join([str(bin_dir), os.environ["PATH"]])
        return cr.fill(cr.load_table()["antigravity"].template(), bin="agy", model=model)

    def test_antigravity_gets_the_brief_as_json_and_leaves_a_review(self):
        # Longer than one argument may be anywhere, with line endings and scripts a code page would mangle.
        payload = ("Review this: значение, 値, café\r\nsecond line\n" * 4000).encode("utf-8")
        self.assertGreater(len(payload), 130_000)
        self.brief.write_bytes(payload)
        roster = self.roster([{"name": "agy", "agent": "antigravity", "binary": "agy", "model": "echo",
                               "command": self.agy_command("echo")}])
        rc, out, status, run_dir = self.run_and_wait(roster)
        self.assertEqual(rc, 0, out)
        self.assertEqual(status["reviewers"][0]["status"], "done", out)
        self.assertEqual((run_dir / "reviews" / "agy.md").read_bytes(), payload)
        self.assertIn("conversation c1", (run_dir / "reviews" / "agy.err").read_text(encoding="utf-8"))
        rc, listed, _ = self.helper("roster", "--roster", roster)
        self.assertNotIn("warning", listed)

    def test_antigravity_failures_say_why(self):
        roster = self.roster([{"name": name, "command": self.agy_command(name)}
                              for name in ("quota", "cut-off", "denied", "died")]
                             + [{"name": "slow", "command": self.agy_command("slow"), "timeout": 3}])
        rc, out, status, run_dir = self.run_and_wait(roster)
        self.assertEqual(rc, 0, out)
        by_name = {r["name"]: r for r in status["reviewers"]}
        self.assertEqual((by_name["quota"]["status"], by_name["quota"]["note"]), ("failed", "usage or rate limit"))
        # An error the CLI reports is a failure even when it exits 0, and what it wrote is kept.
        self.assertEqual(by_name["cut-off"]["status"], "failed")
        self.assertIn("turn aborted", by_name["cut-off"]["note"])
        self.assertEqual((run_dir / "reviews" / "cut-off.md").read_text(encoding="utf-8"), "1. high: half a review")
        self.assertEqual(by_name["denied"]["status"], "empty")
        self.assertIn("denied tool calls: RunCommand", by_name["denied"]["note"])
        self.assertEqual((by_name["died"]["status"], by_name["died"]["rc"]), ("failed", 3))
        self.assertEqual((run_dir / "reviews" / "died.md").read_text(encoding="utf-8"), "partial answer")
        self.assertEqual(by_name["slow"]["status"], "timeout")

    def test_antigravity_refuses_a_brief_that_is_not_utf8(self):
        self.brief.write_bytes("Review this: значение\n".encode("cp1251"))
        roster = self.roster([{"name": "agy", "command": self.agy_command("echo")}])
        rc, out, status, _ = self.run_and_wait(roster)
        self.assertEqual(status["reviewers"][0]["status"], "failed", out)
        self.assertIn("UTF-8", status["reviewers"][0]["note"])

    def test_one_off_reviewers_and_unknown_names(self):
        roster = self.roster([{"name": "good", "command": self.command("ok")}])
        rc, _, err = self.helper("run", "--brief", self.brief, "--roster", roster, "--only", "nobody")
        self.assertEqual(rc, cr.EXIT_ERROR)
        self.assertIn("no reviewer named nobody", err)

    def test_probe(self):
        roster = self.roster([{"name": "pong", "command": self.command("pong")},
                              {"name": "mute", "command": self.command("empty")}])
        rc, out, _ = self.helper("probe", "--roster", roster, "--timeout", "30")
        self.assertEqual(rc, cr.EXIT_ERROR, out)
        self.assertRegex(out, r"pong\s+ok")
        self.assertRegex(out, r"mute\s+FAIL")


if __name__ == "__main__":
    unittest.main()
