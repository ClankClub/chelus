---
name: systematic-debugging
description: Use when something fails and the cause is unknown.
version: 0.1.0
author: Chelus
---

# Systematic Debugging

Find the cause before changing anything. Guess-and-patch costs the owner an approval tap
per guess and often breaks something else. This covers scripts, commands, configs and
code on the phone; it doesn't replace asking the owner when the goal itself is unclear.

## When to Use

- An error, a crash, a failing test or command, "it stopped working", "why does X".
- A fix you already tried didn't work.

## Procedure

1. **Read the whole error.** The last line, the file and line number, the exit code. Find
   the message in the code with `search_files`, then open that spot with `read_file`.
2. **Reproduce it with one command.** Find the shortest `run_shell` command that shows the
   exact failure and would pass once it's fixed. If you can't make it fail, gather more
   facts (logs, `--version`, config) instead of guessing.
3. **Check what changed.** Recent edits, an upgraded package (`pkg list-installed`, `pip
   show`), a moved file, a changed setting, a full disk, a new Android restriction.
4. **Trace the bad value back.** Where does it first go wrong? Follow it upstream with
   `search_files` and fix it where it starts, not where it shows.
5. **Compare with something that works.** A similar script, the last good version, the
   documentation example. List every difference, even the small ones.
6. **Make two or three guesses and rank them** by how likely and how cheap to test. Test
   the top one with the smallest probe, changing one thing at a time.
7. **Fix the cause with one change**, using `edit_file`, then re-run the step 2 command
   and anything that depends on it.
8. **Report** the cause, the fix and the proof (the command and its new result).

## Pitfalls

- After three failed fixes, stop and tell the owner: the approach is probably wrong, and a
  fourth patch hides that.
- Don't bundle fixes; with two changes at once you can't tell which one worked.
- A command that fails with "not found" or "permission denied" on Termux is usually a
  missing package, a path outside `$PREFIX`, or Android storage permission; check those
  before rewriting code.
- Remove temporary debug output when done; mark each such line with a unique tag such as
  `DEBUG-x1` so one `search_files` finds them all.
- If you don't understand something, say so and ask; a confident wrong cause wastes more
  time than a question.

## Verification

The command from step 2 failed before the fix and passes after it, and you showed both
results to the owner.
