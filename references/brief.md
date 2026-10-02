# The review brief

`crossreview.py brief` writes this file. Its shape, for writing one by hand when Python is missing
or the scope is unusual (a design document plus the code it describes, two pull requests at once):

````markdown
IMPORTANT: answer from this brief alone. Do not call tools, do not read or search files, do not run commands, do not edit anything. Reply with one message.

# Code review brief

You are one of several independent reviewers in a cross-review: other agents on other models review
the same change, and the findings are merged and checked against the code afterwards. Look for real
defects: wrong behaviour, crashes, data loss, races, security holes, broken contracts, missing or
wrong tests, and documentation the change leaves stale. Skip style preferences unless they hide a
bug. In a diff, lines starting with `-` are the old code: judge the new version.

## What the change is meant to do

<two or three sentences from the conversation: the task, the constraints, what must not change>

## Scope

<the uncommitted changes in repo X / main..feature / the plan docs/plan.md>.

## Answer format

A numbered list of findings, most severe first. For each finding:

- **severity**: critical, high, medium or low;
- **where**: `path:line` in the new version (for a document, its section);
- **problem**: what goes wrong, with a concrete scenario (the input or state, then the wrong result);
- **fix**: the smallest change that fixes it.

Questions and doubts go in a separate list after the findings. No praise and no summary of the
change. If you find nothing, say so. End with one line: `VERDICT: approve`,
`VERDICT: approve with changes` or `VERDICT: needs rework`. Write in English.

## Already reviewed and rejected

<round two and later: each rejected finding with the evidence that disproved it>

## The change

```diff
<git diff, with untracked files as new-file hunks>
```
````

Why each part is there:

- **The first line.** Reviewers that start reading the repository on their own spend their time
  limit verifying hypotheses and never write the answer. A brief that holds everything they need,
  plus an explicit ban on tools, gets an answer in minutes. When the review does need the
  repository, the line says they may read but not change anything (`--tools read`), and the run
  gets a `--cwd` of a detached worktree.
- **The intent.** Without it a reviewer can only check that the code is self-consistent, not that it
  does what was asked.
- **`-` lines are old code.** Small models otherwise report a removed line as a current bug.
- **The answer format.** A numbered list with `path:line`, severity and a fix is what makes the
  answers of five different models mergeable; the `VERDICT:` line lets the helper show each
  reviewer's verdict in the status table.
- **The rejected list.** In a second round reviewers otherwise repeat the first round's false alarms.
- **The fence.** The helper picks a fence longer than any run of backticks inside the diff, so a
  Markdown file in the change cannot close the block early.

Size: the brief goes to every reviewer through stdin or a file, so there is no 128 KB argument
limit, but every model has a context window and slow local models stall on long input. Leave out
lockfiles, generated code, snapshots and vendored files (`--exclude`), and split a large change by
layer into separate runs.
