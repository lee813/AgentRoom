# agentroom

A self-hosted room where a small team of AI agents works on a project together, with a shared workspace and a
web view so you can watch, step in, and edit files yourself. Standard library only; two containers.

```
                    ┌─ web view: chat + files (you)
roles.json ─► agents ─► room server ◄─┤
 (worker, reviewer,     chat + workspace └─ agentroom CLI (optional: Claude Code, Codex, scripts)
  any OpenAI-compatible model)
```

## Run

```bash
cp roles.example.json roles.json      # edit: the brief, the roles, your model endpoint
export ROOM_TOKEN=$(openssl rand -hex 16)
docker compose up -d --build
# open http://<server-lan-ip>:8765/?token=<that token>
```

History and files live in `./data/room.db`. Without a token the room is open to your whole LAN.

## How a project runs

1. You post a task in the room. A message with no `@mention` goes to the **lead** agent (the first one in
   `roles.json`, or the one with `"lead": true`). A message that mentions `@reviewer` goes only to the reviewer.
2. Agents work in the **workspace**: text files shared by the whole room. Each turn they see every file in full.
   They create or edit files by writing a block in their reply, and the block is saved as a new revision:

   ```
   <<<WRITE report.md>>>
   file content
   <<<END>>>
   ```

   (`<<<APPEND path>>>` adds to the end.) Only the text outside the blocks is posted to the chat.
3. **Handing work over means mentioning someone.** The worker finishes a draft and writes `@reviewer`; the reviewer
   writes feedback and `@worker`; when satisfied the reviewer says APPROVED and mentions nobody, so the chain ends.
4. **Loop guard:** after `max_auto_turns` (default 10) agent messages in a row with no human message, the lead says
   it has paused. Anything you type continues.

The Files tab lists the workspace, lets you switch revisions, highlights what changed, and lets you edit or
create files (your edit becomes a revision too). "Download all" gives a zip.

## Roles

A role is just text in `roles.json`; see `roles.example.json` for a worker and a reviewer. Per agent you can set
`model`, `base_url`, `api_key_env`, `temperature`, and `"listen": "all"` (react to every message, not only mentions).
Different agents can use different models or different servers. Add more roles (a fact-checker, a skeptic, an editor)
by adding entries and telling the others when to hand work to them.

Any OpenAI-compatible chat endpoint works (OpenAI, vLLM, Ollama, LM Studio, ...). Function calling is not needed, so
small local models work too. Models that print `<think>...</think>` are handled.

## Adding Claude Code, Codex or any other CLI agent

Agents that run in a terminal join through the `agentroom` command and learn the rules from a skill.

1. Put `cli/agentroom` on the agent machine's PATH (it needs only Python 3).
2. Set the environment before launching the agent. Use a different name per agent:

   ```bash
   export AGENTROOM_URL=http://<server-lan-ip>:8765 AGENTROOM_TOKEN=<token> AGENTROOM_ROOM=research
   export AGENTROOM_NAME=claude-1 AGENTROOM_ROLE="Worker: draft report.md and revise it after review"
   ```
3. Give it the instructions:
   - **Claude Code:** copy `skills/agentroom/` to `~/.claude/skills/agentroom/` (or `.claude/skills/` in a project).
   - **Codex and anything else:** paste `AGENTS-snippet.md` into the project's `AGENTS.md` (or `CLAUDE.md`).
     It is generated from the same skill, so the rules are identical. I did not verify whether Codex has its own
     skills folder; `AGENTS.md` is the safe route.
4. Tell it to start: for example "Join the agentroom and wait for tasks from the humans."

The skill teaches the loop (read, work in the workspace, post a short handoff with `@name`, then `agentroom wait`),
the room rules, and the worker and reviewer roles. CLI agents are not woken automatically, so they only see a reply
when they `wait` or `read`. Configured agents (from `roles.json`) answer on their own. A CLI agent counts as "not a
configured agent", so the loop guard treats its messages like human messages.

## HTTP API

All routes accept `Authorization: Bearer <token>` or `?token=`. A room is created by its first message or file.

| Route | |
|---|---|
| `POST /api/rooms/{room}/messages` `{sender, text}` | post a message |
| `GET /api/rooms/{room}/messages?after=ID&wait=SECONDS` | long-poll for new messages |
| `GET /api/rooms/{room}/messages?tail=N` | last N messages |
| `GET /api/rooms/{room}/stream?after=ID` | server-sent events |
| `GET /api/rooms/{room}/files` | list files (latest revision of each) |
| `GET /api/rooms/{room}/file?path=P[&rev=N]` | file content |
| `GET /api/rooms/{room}/file/history?path=P` | revisions of a file |
| `POST /api/rooms/{room}/file` `{path, content, author, mode: "write"\|"append"}` | create or change a file |
| `GET /api/rooms/{room}/workspace.zip` | everything as a zip |

Names and rooms: 1-32 characters of letters, digits, `_ . -`. File paths: up to 4 segments of the same
characters. Text files only, 200 KB each, 200 files per room.

## Files

- `server/` room server and web view
- `agents/agent_runner.py` role agents that use an OpenAI-compatible endpoint
- `cli/agentroom` command for CLI agents
- `skills/agentroom/SKILL.md` instructions for CLI agents (and `AGENTS-snippet.md`, generated from it)
- `roles.example.json`, `Dockerfile`, `docker-compose.yml`

## Limits

- Agents see up to about 60,000 characters of workspace per turn (later files are truncated). Keep deliverables
  in a few focused files.
- The workspace holds text only. Agents do not run code or browse the web; they work from the model and the files.
- One workspace per room; one project per room. For another project, run another `agents` container with its own `roles.json` and room name.
- No per-agent tokens, no message deleting, no TLS. Put it behind Caddy or nginx for HTTPS.
