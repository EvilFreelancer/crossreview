# Authoring and maintaining the `crossreview` skill

Agent guide for working **inside this skill repository**. Read it before editing `SKILL.md`, the
scripts, the references or any plugin manifest.

> Naming (Russian): the adjective for "agent / agentic" is **"агентный"**, never "агентский".

## Repository layout

The skill is packaged for **Claude Code**, **Codex** and **Cursor**, ships inside **Coddy** (vendored
by `make skills-vendor` in coddy-agent), and installs as a plain skill folder for any other agent.
The layout is flat: `SKILL.md` and its assets live at the repo root and the plugin manifests point
their skill source at `./`.

```
crossreview/
├─ .claude-plugin/plugin.json
├─ .cursor-plugin/plugin.json
├─ .codex-plugin/plugin.json
├─ .github/workflows/test.yml   # unit tests on Linux, macOS, Windows; shellcheck; PSScriptAnalyzer
├─ SKILL.md                     # CANONICAL skill: YAML frontmatter + instructions for the orchestrator
├─ README.md                    # human-facing documentation
├─ AGENTS.md                    # this file
├─ references/                  # agents.md, roster.md, brief.md: loaded on demand from SKILL.md
├─ scripts/
│  ├─ agents.tsv                # THE table of reviewer CLIs: binaries, marker, models command, templates
│  ├─ crossreview.py            # the helper: detect, models, roster, init, trust, brief, run, wait, ...
│  ├─ detect-agents.sh          # detection without Python, POSIX sh
│  └─ detect-agents.ps1         # detection without Python, Windows PowerShell 5.1 and PowerShell 7
└─ tests/test_crossreview.py    # python -m unittest discover -s tests
```

## Hard rules for this skill

1. **Reviewers are blind to each other.** Nothing a reviewer wrote ever reaches another reviewer's
   brief, in any round. The `--notes-file` of the brief is for the orchestrator's own words only;
2. **The orchestrator has the last word.** SKILL.md must keep the verify-and-decide step: every
   finding is checked against the code and decided (fix, not worth fixing, rejected, open). Do not
   turn it into a vote;
3. **The brief never travels as an argument** (`MAX_ARG_STRLEN` is 128 KB on Linux, a Windows
   command line 32 767 characters), and a reviewer's stdin is the brief or `/dev/null`;
4. **Reviewers stay read-only**: the CLI's own read-only mode in the template, the tool ban in the
   brief, the empty working directory of the run by default;
5. **`agents.tsv` is the single source of the templates.** The helper and both detection scripts
   read it; never hard-code a template in one of them;
6. **The three platforms stay equal.** Anything that runs a reviewer must work on Linux, macOS and
   Windows. The PowerShell column hands redirection to `cmd.exe` because PowerShell's own `|` and `>`
   re-encode text (Windows PowerShell 5.1 reads the system code page and writes UTF-16);
7. **No dependency beyond the Python standard library** (3.8+) in the helper, and none beyond a
   POSIX shell (plus `tr`, `sleep`, and `timeout` when present) in `detect-agents.sh`.

## Adding or changing a reviewer CLI

1. Edit its row in `scripts/agents.tsv`: candidate binaries in preference order, a marker word its
   `--version` or `--help` prints, the arguments that list its models (or `-`), the POSIX template
   and the PowerShell template (`cmd /d /c --% <the same command>`, `< NUL` instead of
   `< /dev/null`).
2. If it can list models, add a parser in `crossreview.py` (`parse_<agent>_models`) and a test with
   a captured sample of its output.
3. Document it in `references/agents.md`: the template, how it stays read-only, the model listing,
   the version you checked, the pitfalls, and the resume command that recovers an answer.
4. Check it for real: `python3 scripts/crossreview.py probe --reviewer <agent>:<model>`, then a small
   review. Only then call it verified in the README table.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

The suite never calls a real reviewer: agents are stub executables on a private `PATH`, reviewers
are a stub script. It covers detection (Python, sh and PowerShell agree), model-list parsing, the
roster lookup order and the workspace trust receipts, the brief builder, the run supervisor (done,
failed with a hint, empty, timeout, missing, stopped, retried), byte-exact brief delivery, the
recursion guard and, on Windows, the PowerShell templates run the way Coddy runs them. CI repeats it
on Linux, macOS and Windows; push and read the run before calling a change done.

## Versioning

`version` must be identical in `SKILL.md` (`metadata.version`), `.claude-plugin/plugin.json`,
`.cursor-plugin/plugin.json` and `.codex-plugin/plugin.json`, and the `description` the same in the
three manifests (the CI lint job checks both). Coddy replaces the copy in a user's home only when the
release carries a higher version, so bump it on every change that should reach Coddy users.

Every release is mirrored into the [rpa-skills](https://github.com/EvilFreelancer/rpa-skills)
catalog (plugin `version` and `description` in `.claude-plugin/marketplace.json`, `description` in
`.agents/plugins/marketplace.json`, catalog `metadata.version` patch-bumped in both, the README row
when the description changed) and re-vendored into coddy-agent (`make skills-vendor`).

## Commit checklist

- [ ] `python3 -m unittest discover -s tests` green locally, CI green on all three systems.
- [ ] `agents.tsv`, `references/agents.md` and the README table agree.
- [ ] `version` bumped and identical everywhere; `description` consistent.
- [ ] Release mirrored into rpa-skills and coddy-agent.
- [ ] Conventional commit message, e.g. `feat(crossreview): ...` / `fix(crossreview): ...`.

Part of the **[rpa-skills](https://github.com/EvilFreelancer/rpa-skills)** collection.
