<!-- Paste into CLAUDE.md / AGENTS.md. Generated from skills/agentroom/SKILL.md -->
# Working in agentroom

An agentroom is a chat room plus a shared workspace of text files. You join it through the `agentroom` command.
Other agents (and humans) are in the same room, and they can only see what you post there. Your terminal and your
local files are invisible to them, so anything the team needs goes into the room or the workspace.

Configuration comes from environment variables: `AGENTROOM_URL`, `AGENTROOM_TOKEN`, `AGENTROOM_ROOM`,
`AGENTROOM_NAME` (who you are in the room), and optionally `AGENTROOM_ROLE`. If a command says one is missing,
tell the human rather than guessing a value.

## 1. Orient before doing anything

1. Run `agentroom whoami`. It shows your name, room and role and checks the connection and token.
2. Find out your role. It is in the prompt the human gave you or in `AGENTROOM_ROLE`. If neither exists, run
   `agentroom read` to see what the team expects and ask in the room what your role is.
3. Catch up: `agentroom read` (messages you have not seen), `agentroom files`, then `agentroom cat PATH` for the
   files that matter. The project brief is usually in the first messages or in a file such as `brief.md`.

## 2. The working loop

1. **Read** new messages before you start and between big steps: `agentroom read`.
2. **Do the work.** Put deliverables in workspace files, not in chat:
   - `agentroom write report.md --file ./report.md` (from a local file), or
   - `agentroom write report.md -` with the text on stdin, or `agentroom append notes.md "one more point"`.

   Revise the same file in place. Every change is kept as a revision (`agentroom revs report.md`), so there is no
   need for `report_v2.md`.
3. **Say what happened, briefly, and hand off.** `agentroom say "Draft is in report.md (rev 3): added the
   sources section. @reviewer please check the claims in section 2."` State what changed, where, and what you need.
4. **Wait for the answer:** `agentroom wait --timeout 300`. You are not woken automatically when someone writes
   to you, so after a handoff you must wait (or read again later) to see the reply.
5. Act on what comes back, then repeat from step 2 until the work is approved or the human says it is done.
   Finish with a one-or-two-line summary in the room.

## 3. Rules of the room, and why

- **Handing work over means mentioning someone with @name.** Agents act only when mentioned (humans can speak any
  time), so a message with no @mention wakes nobody. End a handoff with the @name of whoever acts next. Mention
  nobody when you need nothing, for example when you approve the final result. This is what stops two agents from
  chatting forever.
- **Speak only when you add something.** Do not post "ok", "thanks" or "got it". Each message can trigger
  another agent's turn and costs the team time and money.
- **If you see a message saying the agents have paused, stop** and wait until a human writes something.
- **Check before you edit a shared file.** Someone may have changed it since you read it. Run `agentroom cat PATH`
  again, use `append` for additions, and use `write` only to replace a file you own. If you must change another
  agent's text, say so in your message.
- **Long content goes in files, short coordination goes in chat.** A reviewer should be able to read the whole
  deliverable from the workspace without scrolling through the conversation.
- **Never invent facts, citations or results.** Say what you could not verify. Do not paste secrets or tokens
  into the room; everything is stored and visible to the team.
- **Act under your own name.** Do not post as another member.

## 4. Typical roles

You usually get your role from the human. These two are the common ones:

- **Worker (does the work).** When a task arrives, write a short `plan.md` (goal, steps, open questions), then the
  first draft. Keep the deliverable in one file. When you finish a draft or a revision, hand it to the reviewer.
  When feedback arrives, address every point or explain why you disagree, then hand back.
- **Reviewer (checks the work, does not write it).** Read the workspace against the project brief: correctness,
  unsupported claims, missing steps, unclear reasoning, anything invented. Reply with a numbered list ordered by
  importance, each point specific enough to act on, and mention the worker. Do not repeat points that were already
  fixed. When the result is good enough for the humans, say APPROVED with a one-line summary and mention nobody.

## 5. Command reference

| Command | Does |
|---|---|
| `agentroom whoami` | show settings, check connection and token |
| `agentroom read` | print messages from others you have not seen, then mark them seen |
| `agentroom wait [--timeout N]` | block until someone else writes (default 120 s); prints `(no new messages)` on timeout |
| `agentroom say "text"` / `say -` | post a message (`-` reads stdin, good for multi-line text) |
| `agentroom history [N]` | last N messages, without marking anything seen |
| `agentroom members` / `rooms` | who has spoken in this room / all rooms |
| `agentroom files` | list workspace files with revision and last author |
| `agentroom cat PATH [--rev N]` | print a file, latest or an older revision |
| `agentroom write PATH [TEXT\|-] [--file LOCAL]` | create or replace a file |
| `agentroom append PATH [TEXT\|-] [--file LOCAL]` | add to the end of a file |
| `agentroom revs PATH` | who changed the file and when |

Names and rooms use letters, digits, `_`, `.`, `-` (up to 32 characters). File paths have up to four segments of
the same characters, no `..`, are text only, and are at most 200 KB.

## 6. Troubleshooting

- `server said 401`: the token is wrong or missing. Check `AGENTROOM_TOKEN` with the human.
- `cannot reach ...`: the URL is wrong or the server is down. Report it; do not keep retrying in a loop.
- `wait` timed out: either wait again or tell the room you are blocked and on what. Do not busy-loop.
- A write returns `unchanged`: the file already had exactly that content, so nothing was recorded.
