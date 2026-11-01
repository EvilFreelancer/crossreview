# Reviewer CLIs

Every reviewer is a console code agent run once, non-interactively, on the brief. The templates
live in `scripts/agents.tsv`, the single table the helper and both detection scripts read; this page
explains them. `{bin}` is the detected binary, `{model}` is written into the roster, `{brief}` and
`{out}` are filled in for every run.

Rules every template follows, and why:

- **The brief never travels as an argument** (Koda is the one exception, see below). Linux caps one
  argument at 128 KB (`MAX_ARG_STRLEN`),
  Windows a whole command line at 32 767 characters. A 138 KB brief passed as `"$(cat brief)"`
  fails with `Argument list too long` before the model is called (Cursor Agent and Coddy, both
  checked on 2026-10-03). Templates read the brief from stdin or from a file flag.
- **Stdin is closed unless the template feeds it.** Coddy, Devin and OpenCode read stdin when it is
  not a terminal, and an inherited open pipe hangs them. The helper gives every reviewer `/dev/null`.
- **Read-only.** Each template picks the CLI's own read-only mode (plan, ask, a read-only sandbox, a
  permission mode that approves reads only). The brief's first line forbids tools as well, and by
  default the reviewers run in an empty directory of the run, so even a CLI that auto-approves
  everything has nothing of yours to touch.
- **The answer is stdout.** `> {out}` captures the final message; progress and logs go to stderr,
  which the helper keeps next to the review (`reviews/<name>.err`).

Status: **verified** means a real review ran through the template on the version named;
**unverified** means the flags come from the CLI's `--help` and no signed-in run was possible.

## claude (Claude Code)

```
claude -p --model {model} --output-format text --permission-mode plan < {brief} > {out}
```

- Read-only: `--permission-mode plan`. The workspace trust dialog is skipped in `-p` mode.
- Models: no listing command. Aliases `fable`, `opus`, `sonnet`, `haiku` follow the newest model of
  each family; full ids such as `claude-opus-5-5` work too.
- Verified on 2.1.285: `haiku` reviewed a 3 KB brief in 32 s.
- Recover a verdict from a session that ran out of time: in the run's `work` directory,
  `claude -p --continue "Do not call tools. Write your final review now."`.

## codex (OpenAI Codex CLI)

```
codex exec -m {model} --sandbox read-only --skip-git-repo-check - < {brief} > {out}
```

- `-` reads the prompt from stdin. `--skip-git-repo-check` because the reviewer runs outside a
  repository. Approvals are off in `exec`; the sandbox keeps it read-only.
- Models: `codex debug models` prints the raw catalog as JSON (half a megabyte); `crossreview.py
  models codex` reduces it to the visible slugs. Reasoning effort is not a flag: append
  `-c model_reasoning_effort=high` to the stored command by hand.
- A usage limit is per account: when it is hit, every Codex model fails at once, and so do the
  `codex/*` models inside Coddy, which share the login.
- Verified on 0.157.1 (one-word probe, 16 s).
- Recover: in the `work` directory, `codex exec resume --last "Write your final review now."`.

## coddy (Coddy Agent)

```
coddy --model {model} --mode ask --no-stdin -i {brief} > {out}
```

- `-i` reads the prompt from a file; `--no-stdin` keeps whatever is on stdin out of the prompt.
  Both need Coddy 1.1.62 or newer; an older build reads `-p -i` as the prompt `-i`. If you write a
  `-p` form by hand, put `--model` before `-p`, which takes the next argument as its value.
- Read-only: `--mode ask`.
- Models: `provider/model` ids from `models[].model` of `~/.coddy/config.yaml` (the helper reads
  them). One Coddy reaches several vendors at once: `codex/gpt-5.6-sol` (ChatGPT sign-in),
  `devin/swe-2`, `neuraldeep/qwen3.8-27b`, Anthropic or any OpenAI-compatible endpoint, which makes
  it the most flexible single reviewer CLI. The reasoning level comes from `reasoning_default` in
  the config, not from a flag; for a review, point `--config` at a copy with the level raised.
- Local models: a reply cut off at `max_tokens` arrives as an empty review. Raise `max_tokens`
  (32768 to 60000) in a config copy, prefer the `-noreason` variants, and keep their briefs short.
- Verified on 1.2.52: `codex/gpt-5.6-luna` reviewed a 3 KB brief in 32 s.
- Recover: `coddy sessions list` in the `work` directory gives the session id, then
  `coddy --model {model} --mode ask --session-id <id> -p "Do not call tools. Write your final review now."`.

## cursor (Cursor Agent)

```
agent -p --trust --mode ask --model {model} --output-format text < {brief} > {out}
```

- The binary is tried as `agent` first (it can be a wrapper that carries a proxy), then
  `cursor-agent`, then `cursor agent`. Whichever answered is kept in the roster's `binary` and
  `command`; the command is authoritative.
- `--trust` is required in print mode in a directory the CLI has not seen. Read-only: `--mode ask`.
- Models: `agent --list-models`; `auto` lets Cursor choose.
- Verified on 2026.10.01: on a 138 KB brief through stdin, `--mode ask` and `--mode plan` both
  answered in 58 s; the same brief as an argument failed with `Argument list too long`. The reply
  sometimes ends with a stray `<|eos|>`, and it may follow the language of the user's Cursor rules.
- Recover: in the `work` directory, `agent -p --trust --continue "Write your final review now."`.

## devin (Devin CLI)

```
devin -p --model {model} --prompt-file {brief} --permission-mode auto --respect-workspace-trust false < /dev/null > {out}
```

- `--prompt-file` reads the brief. `--permission-mode auto` approves read-only tools only.
  `--respect-workspace-trust false` is required: print mode cannot show the trust prompt and fails
  in an untrusted directory. `< /dev/null` because Devin reads stdin otherwise.
- Models: `devin models list`. `swe-2-high`, `swe-2-medium` and `swe-2-max` are free.
- With repository access `swe-2-high` can spend a whole hour checking hypotheses and never write
  the answer; from the brief alone it answers in minutes. When `high` stalls, `medium` usually does
  not.
- Verified on 3000.11.3: `swe-2-medium` reviewed a 3 KB brief in 10 s.
- Recover: the session id is in `~/.local/share/devin/cli/logs/` (the line `for session <id>`);
  `devin -r <id> --model swe-2-high -p "Do not call tools. Write your final review now." --permission-mode auto --respect-workspace-trust false < /dev/null`.

## opencode (OpenCode)

```
opencode run -m {model} --agent plan < {brief} > {out}
```

- Without a message argument `opencode run` reads the prompt from stdin. `--agent plan` is the
  read-only agent.
- Always pass `-m`: the configured default may be a model the account cannot use, and the run then
  hangs without output.
- In a checkout that has a `.opencode/` directory, OpenCode installs its plugin dependencies there
  and rewrites `.opencode/package.json`. Another reason to keep reviewers in the empty `work`
  directory.
- Models: `opencode models` (`provider/model`).
- Verified on 1.18.16: `neuraldeep/qwen3.8-27b-noreason` reviewed a 3 KB brief in 32 s.
- Recover: `opencode run --continue "Write your final review now."` in the `work` directory.

## qwen (Qwen Code)

```
qwen -m {model} --approval-mode plan -o text < {brief} > {out}
```

- Stdin is the prompt when no positional prompt is given. `--approval-mode plan` is read-only.
- Needs an auth type configured first (`No auth type is selected` otherwise).
- Unverified (0.10.6 installed, not signed in).

## gemini (Gemini CLI)

```
gemini -m {model} --approval-mode plan -o text < {brief} > {out}
```

- Same flags as Qwen Code, its fork. Unverified; if your version rejects `--approval-mode plan`,
  drop it from the stored command: headless Gemini CLI refuses tools that need approval anyway.

## kimi (Kimi Code CLI)

```
kimi --quiet --plan -m {model} < {brief} > {out}
```

- `--quiet` is `--print --output-format text --final-message-only`. Print mode implies `--yolo`
  (every action approved), so `--plan` and the empty `work` directory are what keep it harmless.
- Stdin is read as the prompt in print mode (checked).
- Unverified (1.30.0 installed, the key on this machine is rejected with 401).

## zcode (ZCode)

```
zcode --prompt "$(cat {brief})" --mode plan --no-color > {out}
```

ZCode ships as a desktop app, but the app carries its whole agent CLI: `resources/glm/zcode.cjs`
(checked in `%LOCALAPPDATA%\Programs\ZCode`, version 0.17.0), which runs under Electron's own Node
when `ELECTRON_RUN_AS_NODE=1` is set. The template wants a `zcode` launcher on PATH; it only has to
forward its arguments to that bundle:

- POSIX: a shell script that sets `ELECTRON_RUN_AS_NODE=1` and `exec`s `ZCode <install
  dir>/resources/glm/zcode.cjs "$@"` (the app executable and the bundle are next to each other).
- Windows: it must be a real `.exe`, never a `.cmd`/`.bat` — the brief travels as one argument and
  `cmd.exe` would re-parse its text (the runner refuses batch shims for exactly that reason). The
  compiler that ships with Windows builds this launcher:

  ```csharp
  using System; using System.Diagnostics; using System.IO; using System.Text;
  class ZCodeShim {
    static int Main(string[] args) {
      var root = Environment.GetEnvironmentVariable("ZCODE_DESKTOP_DIR") ?? Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "ZCode");
      var psi = new ProcessStartInfo(Path.Combine(root, "ZCode.exe"));
      psi.UseShellExecute = false;
      psi.EnvironmentVariables["ELECTRON_RUN_AS_NODE"] = "1";
      var sb = new StringBuilder(Quote(Path.Combine(root, "resources", "glm", "zcode.cjs")));
      foreach (var a in args) sb.Append(' ').Append(Quote(a));
      psi.Arguments = sb.ToString();
      var p = Process.Start(psi); p.WaitForExit(); return p.ExitCode;
    }
    static string Quote(string s) {  // the inverse of MSVCRT argv parsing
      bool need = s.Length == 0;
      foreach (char c in s) if (char.IsWhiteSpace(c) || c == '"') { need = true; break; }
      if (!need) return s;
      var sb = new StringBuilder("\""); int bs = 0;
      foreach (char c in s) {
        if (c == '\\') { bs++; continue; }
        if (c == '"') { sb.Append('\\', bs * 2 + 1).Append('"'); bs = 0; continue; }
        sb.Append('\\', bs).Append(c); bs = 0;
      }
      sb.Append('\\', bs * 2).Append('"'); return sb.ToString();
    }
  }
  ```

  `csc /nologo /out:zcode.exe zcode-shim.cs` (the compiler is
  `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`). The quoting was checked to deliver
  multi-line briefs with `"`, `&`, `%`, `<`, `|` and trailing backslashes unchanged.

Three setup steps the desktop app hides from a standalone CLI:

1. `provider/zcode-builtin.json` next to `zcode.cjs` — the bundle looks there, the desktop keeps it
   in `resources/config/provider/`. One copy fixes it.
2. Sign in once with `zcode login`: the desktop's login is not shared, and a signed-out CLI has an
   empty model registry.
3. Pick the model. Headless runs take `config.defaultModelSelection` from `~/.zcode/v2/provider_config.json`,
   and since the account route may fail (see below), the reliable setup is a personal provider
   under `config.providerConfigRules.providerRules` with an API key from the Z.AI console
   (personal rules use `personalModelIds`, not `builtinModelIds`):

   ```json
   {
     "providerId": "zai-api-key",
     "providerName": "Z.AI API Key",
     "config": {
       "group": "standard-personal",
       "access": {"type": "zhipu-coding-plan-api-key", "apiKey": "<your key>"},
       "api": {"type": "anthropic-messages", "baseUrl": "https://api.z.ai/api/anthropic"},
       "personalModelIds": ["GLM-5.3", "GLM-5.3-Flash", "GLM-5.2"]
     }
   }
   ```

   with `"defaultModelSelection": {"providerId": "zai-api-key", "modelId": "GLM-5.3",
   "options": {"reasoningLevel": "high"}}` next to it. There is no `--model` flag, so the roster's
   model is a label only, like Koda's.

- The brief travels as an argument (Koda's exception, `ARG_LIMIT`): `--prompt` takes text only,
  `--attach` accepts images and video, not text files.
- Read-only: `--mode plan` restricts writes and command execution; it is not an OS-level sandbox, so
  the brief's tool ban and the empty working directory are what keep it harmless. `--prompt` defaults
  to `--mode yolo`, never omit `--mode plan`.
- Models: no listing command. The builtin catalog covers GLM-5.3, GLM-5.3-Flash, GLM-5.2 and others;
  `zcode --help` lists the account plans.
- A run can fail with `project_token_login_failed` even when `zcode login` says `Login successful`:
  before the first model call the CLI exchanges the OAuth token for a coding-plan API key at
  `POST https://api.z.ai/api/auth/z/login`, and that endpoint may answer
  `{"code":500,"msg":"Z.ai user information is invalid"}` (checked 2026-11-01 with a valid token,
  direct and through a proxy). The desktop app does not use this exchange — it injects its own
  tokens into the agent — so ZCode can chat in its GUI and still fail as a reviewer. A personal
  API-key provider (step 3) bypasses the exchange; API-key calls bill from the account's balance
  (`[1113][Insufficient balance or no resource package. Please recharge.]` means recharge, not a
  CLI problem).
- Verified on 0.17.0 (the CLI bundle inside the ZCode 3.16.3 desktop app): `GLM-5.3` answered a
  one-word probe in 9 s and reviewed a 19 KB brief in 4m29s through the template.
- Recover: in the `work` directory, `zcode -c --prompt "Do not call tools. Write your final review now."`
  resumes the latest session of that directory, `zcode --resume <sess_...>` an earlier one.

## koda

```
koda "$(cat {brief})" > {out}
```

- Kept from Coddy's original list, and the one exception to the rule above: Koda takes the prompt
  only as an argument. The helper refuses it up front, with the reason in the status, when the brief
  is longer than one argument may be (120 KB here, 30 KB on Windows). There is no model placeholder:
  put the CLI's model flag into the stored command by hand.
- No read-only mode is known, so rely on the brief's tool ban and the empty working directory.
- Unverified.

## Adding an agent

Add a row to `scripts/agents.tsv` (agent, candidate binaries, a word its `--version` or `--help`
prints, the arguments that list its models or `-`, the POSIX template, the PowerShell template),
then a section here, then run the tests. Keep the brief out of argv, close stdin, pick the
read-only mode, and verify with `crossreview.py probe --reviewer <agent>:<model>` before calling it
verified.
