# The reviewer roster

The roster is a JSON file listing the reviewers. `crossreview.py init` writes it, and it can be
edited by hand: it is ordinary configuration, read fresh on every run.

```json
{
  "version": 1,
  "min_reviewers": 2,
  "timeout": 2700,
  "reviewers": [
    {"kind": "cli", "agent": "cursor", "binary": "agent", "model": "auto",
     "command": "agent -p --trust --mode ask --model auto --output-format text < {brief} > {out}"},
    {"kind": "cli", "agent": "coddy", "binary": "coddy", "model": "codex/gpt-5.6-sol",
     "command": "coddy --model codex/gpt-5.6-sol --mode ask --no-stdin -i {brief} > {out}",
     "timeout": 2400},
    {"kind": "cli", "agent": "devin", "binary": "devin", "model": "swe-2-high",
     "command": "devin -p --model swe-2-high --prompt-file {brief} --permission-mode auto --respect-workspace-trust false < /dev/null > {out}",
     "enabled": false},
    {"kind": "internal", "host": "claude", "model": "sonnet"},
    {"kind": "internal", "host": "coddy", "definition": "explore", "model": "devin/swe-2", "reasoning": "high"}
  ]
}
```

| Field | Meaning |
|-------|---------|
| `version` | Always `1`. |
| `min_reviewers` | The quorum: fewer answers than this and the report says **insufficient quorum**. Default 2. |
| `timeout` | Seconds each reviewer gets, default 2700. A reviewer's own `timeout` wins. |
| `reviewers[].kind` | `cli` (an external CLI started by the helper) or `internal` (a subagent of a host agent). Default `cli`. |
| `reviewers[].name` | Label in the status and the report, also the file name of the review. Default `<agent>-<model>`. |
| `reviewers[].agent` | Row of `scripts/agents.tsv` the entry came from; used for warnings and `init --refresh`. |
| `reviewers[].binary` | The binary detection found (`agent`, `cursor-agent`, a path). Informational: the command is what runs. |
| `reviewers[].model` | The model written into the command. |
| `reviewers[].command` | What runs, with `{brief}` and `{out}` left as placeholders. Authoritative: it runs exactly as written, so a wrapper, a proxy alias or an extra flag (`-c model_reasoning_effort=high`) belongs here. |
| `reviewers[].enabled` | `false` keeps the entry without running it. |
| `reviewers[].host` | For `internal`: the agent that runs it as its own subagent (`claude`, `coddy`, `opencode`). Other hosts skip it. |
| `reviewers[].definition`, `reasoning` | For Coddy's internal reviewers: the subagent definition (only `explore` is honoured) and the reasoning level. |

The helper runs a command without a shell when it has the template shape: arguments, then an
optional `< file` and `> {out}`, or the PowerShell form `Get-Content -Raw {brief} | ...`. Anything
else (pipes, `;`, `&&`, `2>`) goes to `sh -c`, or to PowerShell on Windows, with the paths quoted.

## Where it lives

The helper takes the first of these that exists:

1. `--roster FILE` on the command line;
2. `$CROSSREVIEW_ROSTER`;
3. the user roster, `$CROSSREVIEW_HOME/roster.json`, by default `~/.config/crossreview/roster.json`
   (`$XDG_CONFIG_HOME` is honoured) or `%APPDATA%\crossreview\roster.json` on Windows. Every agent
   on the machine shares this one;
4. Coddy's roster, `${CODDY_HOME:-~/.coddy}/crossreview.json`, where the first version of this skill
   kept it. `crossreview.py init --import <that file> --refresh` copies it to the user roster and
   moves its commands to the current templates;
5. a workspace roster, `.agents/crossreview.json` or `.coddy/crossreview.json` at the repository
   root, used only when none of the above exists and only once the user has approved it.

## Workspace rosters and trust

A roster inside a repository arrived with the checkout and runs commands, so opening a repository
must not be enough to run them. It is honoured only after the user approves that exact file:
`crossreview.py roster` exits 4 and prints the commands and the file's SHA-256, the agent shows
them to the user, and on a clear yes runs `crossreview.py trust`, which records

```json
{"workspace": "/abs/repo", "roster": "/abs/repo/.agents/crossreview.json", "sha256": "...", "approved": "2026-10-03T01:20:00"}
```

in `trust.json` next to the user roster. Any change to the file changes its digest and asks again.
The user roster needs no approval: the user wrote it, or asked for it to be written.
