#!/usr/bin/env python3
"""Runs a team of role-playing agents in one agentroom, using any OpenAI-compatible chat endpoint.

    python agent_runner.py --config roles.json

Each agent is just a role prompt plus a model. They share the room's chat and its workspace (text files).
Standard library only. See roles.example.json.

Turn-taking rules (this is what keeps agents from chattering forever):
  * A human message with no @mention is answered by the lead agent (first in the config, or "lead": true).
  * A human message that @mentions an agent is answered only by that agent.
  * An agent message is answered only by the agents it @mentions.  Handing work over = mentioning someone.
  * After `max_auto_turns` agent messages in a row with no human message, the lead says so and everyone stops
    until a human speaks again.
"""
import argparse, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request

BLOCK_RE = re.compile(r"<<<(WRITE|APPEND)[ \t]+([^\s>]+)[ \t]*>>>[ \t]*\n?(.*?)\n?[ \t]*<<<END>>>", re.S)
THINK_RE = re.compile(r"<think>.*?</think>", re.S)
WORKSPACE_BUDGET = 60_000  # characters of file content shown to the model per turn
SYSTEM_NAMES = {"workspace"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def http(method, url, body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json", **(headers or {})},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class Room:
    def __init__(self, url, room, token):
        self.base = f"{url.rstrip('/')}/api/rooms/{room}"
        self.token = token

    def _u(self, endpoint, **params):
        if self.token:
            params["token"] = self.token
        return f"{self.base}{endpoint}?{urllib.parse.urlencode(params)}"

    def messages(self, timeout=60, **params):
        return http("GET", self._u("/messages", **params), timeout=timeout)

    def say(self, sender, text):
        return http("POST", self._u("/messages"), {"sender": sender, "text": text})

    def files(self):
        return http("GET", self._u("/files"))

    def file(self, path):
        return http("GET", self._u("/file", path=path))

    def write(self, author, path, content, mode):
        return http("POST", self._u("/file"), {"author": author, "path": path, "content": content, "mode": mode})


def first_line(text):
    return text.strip().splitlines()[0] if text.strip() else ""


class Agent(threading.Thread):
    def __init__(self, cfg, spec, room, names, is_lead):
        super().__init__(daemon=True, name=spec["name"])
        d = {**cfg.get("defaults", {}), **spec}
        self.name_, self.role = spec["name"], spec["role"]
        self.base_url = d.get("base_url") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = d.get("model") or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.api_key = os.environ.get(d.get("api_key_env", "OPENAI_API_KEY"), "")
        self.temperature = d.get("temperature")
        self.listen = d.get("listen", "mentions")
        self.room, self.names, self.is_lead = room, names, is_lead
        self.brief = cfg.get("brief", "")
        self.max_auto = int(cfg.get("max_auto_turns", 10))
        self.team = cfg["agents"]
        self.mention_me = re.compile(rf"(?<![\w.-])@{re.escape(self.name_)}(?![\w.-])", re.I)
        self.mention_any = re.compile(r"(?<![\w.-])@(" + "|".join(re.escape(n) for n in names) + r")(?![\w.-])", re.I)
        self.last_pause_note = 0

    # ---- deciding whether to act
    def should_act(self, m):
        if m["sender"] == self.name_ or m["sender"] in SYSTEM_NAMES:
            return False
        if self.listen == "all":
            return True
        if self.mention_me.search(m["text"]):
            return True
        human = m["sender"] not in self.names
        return human and self.is_lead and not self.mention_any.search(m["text"])

    def over_budget(self):
        run = 0
        for m in reversed(self.room.messages(tail=40)):
            if m["sender"] in SYSTEM_NAMES:
                continue
            if m["sender"] in self.names:
                run += 1
            else:
                break
        return run >= self.max_auto, run

    # ---- building the prompt
    def system_prompt(self):
        team = "\n".join(f"- {a['name']}: {first_line(a['role'])}" for a in self.team)
        return f"""You are "{self.name_}", a member of a small team working on one project in a shared chat room. Humans and other AI agents are in the room.

Team:
{team}

Your role:
{self.role.strip()}

Project brief:
{self.brief.strip() or '(none yet; the humans will tell you)'}

How the room works:
- Messages arrive as "sender: text". Reply with the message you want to post: plain text, short and concrete.
- To hand work to someone, mention them with @name. Agents only act when mentioned (humans can speak any time), so end your message by mentioning whoever should act next. Mention nobody if you need nothing more.
- The shared workspace is a set of text files everyone can see. Their current contents are shown to you every turn. To create or replace a file, put this in your reply:
<<<WRITE folder/name.md>>>
file content
<<<END>>>
  To add to the end of a file use <<<APPEND folder/name.md>>> ... <<<END>>>. These blocks are not posted to the room; only the text outside them is. Always add a short message that says what you changed and hands off.
- Put long content in files, not in chat. Never invent facts or sources; say what you could not verify.
- If you have nothing useful to add, reply with exactly PASS."""

    def workspace_text(self):
        files = self.room.files()
        if not files:
            return "(the workspace is empty)"
        out, used = [], 0
        out.append("Files: " + ", ".join(f"{f['path']} (rev {f['rev']}, by {f['author']})" for f in files))
        for f in files:
            body = self.room.file(f["path"])["content"]
            room_left = WORKSPACE_BUDGET - used
            note = ""
            if len(body) > room_left:
                body, note = body[:max(room_left, 0)], "\n[...truncated to fit; the full file is in the workspace]"
            used += len(body)
            out.append(f"=== {f['path']} (rev {f['rev']}, last edited by {f['author']}) ===\n{body}{note}\n=== end {f['path']} ===")
        return "\n".join(out)

    def user_prompt(self, trigger):
        history = self.room.messages(tail=30)
        convo = "\n".join(f"{m['sender']}: {m['text']}" for m in history)
        return (f"WORKSPACE\n{self.workspace_text()}\n\nROOM (oldest first)\n{convo}\n\n"
                f"It is your turn because {trigger['sender']} just wrote the last message addressed to you. "
                f"Respond as {self.name_}.")

    # ---- calling the model
    def ask_model(self, system, user):
        body = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                  {"role": "user", "content": user}]}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        for attempt in range(3):
            try:
                out = http("POST", f"{self.base_url.rstrip('/')}/chat/completions", body, headers, timeout=600)
                return out["choices"][0]["message"]["content"] or ""
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
                log(f"[{self.name_}] model call failed ({e}); attempt {attempt + 1}/3")
                time.sleep(3 * (attempt + 1))
        return None

    def apply(self, reply):
        reply = THINK_RE.sub("", reply).strip()
        for kind, path, content in BLOCK_RE.findall(reply):
            try:
                r = self.room.write(self.name_, path, content, "append" if kind == "APPEND" else "write")
                log(f"[{self.name_}] {kind.lower()} {path} -> rev {r['rev']}{' (unchanged)' if r.get('unchanged') else ''}")
            except urllib.error.HTTPError as e:
                err = json.loads(e.read().decode() or "{}").get("error", e.reason)
                self.room.say(self.name_, f"(could not write {path}: {err})")
        text = BLOCK_RE.sub("", reply).strip()
        text = re.sub(rf"^{re.escape(self.name_)}\s*:\s*", "", text)
        if not text or text.upper().strip(". ") == "PASS":
            return
        self.room.say(self.name_, text)

    # ---- main loop
    def run(self):
        try:
            tail = self.room.messages(tail=1)
            cursor = tail[-1]["id"] if tail else 0
        except Exception as e:
            log(f"[{self.name_}] cannot reach room: {e}")
            cursor = 0
        log(f"[{self.name_}] joined (model={self.model}, lead={self.is_lead}, listen={self.listen})")
        while True:
            try:
                msgs = self.room.messages(timeout=60, after=cursor, wait=25, **{"as": self.name_})
                if not msgs:
                    continue
                cursor = msgs[-1]["id"]
                trigger = next((m for m in msgs if self.should_act(m)), None)
                if not trigger:
                    continue
                over, run = self.over_budget()
                if over:
                    if self.is_lead and self.last_pause_note != cursor:
                        self.last_pause_note = cursor
                        self.room.say(self.name_, f"(paused: {run} agent messages in a row with no human. "
                                                  "Say anything to continue.)")
                    continue
                reply = self.ask_model(self.system_prompt(), self.user_prompt(trigger))
                if reply is not None:
                    self.apply(reply)
                time.sleep(1)
            except Exception as e:  # keep the agent alive whatever happens
                log(f"[{self.name_}] error: {type(e).__name__}: {e}")
                time.sleep(3)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("ROLES_CONFIG", "roles.json"))
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    url = os.environ.get("AGENTROOM_URL") or cfg.get("room_url", "http://localhost:8765")
    token = os.environ.get(cfg.get("token_env", "ROOM_TOKEN"), "")
    room = Room(url, cfg.get("room", "lobby"), token)
    names = [s["name"] for s in cfg["agents"]]
    lead = next((s["name"] for s in cfg["agents"] if s.get("lead")), names[0])
    log(f"room #{cfg.get('room', 'lobby')} at {url}; agents: {', '.join(names)}; lead: {lead}")
    for _ in range(60):  # wait for the server to come up
        try:
            room.messages(tail=1)
            break
        except Exception:
            time.sleep(1)
    threads = [Agent(cfg, s, room, names, s["name"] == lead) for s in cfg["agents"]]
    for t in threads:
        t.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
