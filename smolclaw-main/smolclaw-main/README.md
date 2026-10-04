<p align="center">
  <img src="logo.svg" width="96" alt="smolclaw logo"/>
</p>

<h1 align="center">smolclaw</h1>

<p align="center">A personal AI agent that runs 24/7 on your machine, controlled via Telegram. Powered by Claude.</p>

**Think Claude Code, but over Telegram** — with persistent memory, process management, cron scheduling, web access, and self-improving learning loops.

## What It Does

Send a message on Telegram, get a Claude-powered agent that can:

- **Run shell commands** on your machine
- **Read/write files** anywhere on the filesystem
- **Search the web** and fetch URLs
- **Manage processes** — start bots, monitor them, auto-restart on crash
- **Schedule jobs** — cron-based recurring tasks with agent or shell actions
- **Remember everything** — persistent memory injected into every conversation
- **Learn over time** — learning loops that review, analyze, and improve

## Setup Guide

### 1. Install Bun

```bash
curl -fsSL https://bun.sh/install | bash
```

Restart your terminal after installing. Verify with `bun --version` (need v1.1+).

### 2. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a display name (e.g. "smolclaw")
4. Choose a username — must end in `bot` (e.g. `smolclaw_bot`)
5. BotFather gives you a token like `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz` — save this

**Optional but recommended:** Send `/mybots` → select your bot → **Bot Settings** → **Group Privacy** → Turn **OFF** (so the bot can read messages in groups, if you want that later).

### 3. Get Your Telegram User ID

1. Open Telegram and search for **@userinfobot**
2. Send it any message
3. It replies with your user ID (a number like `123456789`) — save this

This is your allowlist — only this user ID can talk to the bot. Everyone else gets silently ignored.

### 4. Clone and Install

```bash
git clone https://github.com/yourusername/smolclaw.git
cd smolclaw
bun install
```

### 5. Configure

```bash
mkdir -p ~/.smolclaw/memory
cp config.example.json ~/.smolclaw/config.json
```

Edit `~/.smolclaw/config.json` with your details:

```json
{
  "telegram": {
    "botToken": "PASTE_YOUR_BOT_TOKEN_HERE",
    "allowedUsers": [PASTE_YOUR_USER_ID_HERE],
    "allowedUsernames": ["your_telegram_username"]
  },
  "anthropic": {
    "model": "claude-opus-4-6",
    "maxTokens": 16384
  }
}
```

Replace:
- `PASTE_YOUR_BOT_TOKEN_HERE` with the token from BotFather (keep the quotes)
- `PASTE_YOUR_USER_ID_HERE` with your numeric user ID (no quotes — it's a number)
- `your_telegram_username` with your @ handle without the @

**Don't have a Claude API key?** That's fine — leave out the `anthropic.apiKey` field entirely. On first run, smolclaw will open your browser to authenticate with Claude (same OAuth flow as Claude Code). Your token gets stored securely in macOS Keychain.

**Have a Claude API key?** Add `"apiKey": "sk-ant-..."` inside the `anthropic` section.

### 6. Set Up the Agent's Personality (Optional)

Create `~/.smolclaw/memory/DIRECTIVES.md` — this is loaded into every single conversation automatically:

```markdown
# Core Directives

You are my personal AI agent.

## Identity
- Direct, concise, technical
- You have full system access

## Context
- Owner: Your Name
- Machine: Mac (Apple Silicon)
- Interests: whatever you want

## Standing Orders
- Save important learnings to memory
- Prefer TypeScript/Bun for code
```

Edit this anytime — changes take effect on the next message, no restart needed.

### 7. Run

```bash
cd smolclaw
bun start
```

**First run without API key:** You'll see a setup banner. Press Enter, your browser opens, authenticate with Claude, and you're in. The token is stored in your keychain for next time.

**Subsequent runs:** Starts immediately, says "Authenticated with Claude (OAuth)".

Now open Telegram, find your bot, and send it a message. You should get a response.

### 8. Auto-Start on Login (macOS)

```bash
chmod +x scripts/install-daemon.sh
./scripts/install-daemon.sh
```

This creates a launchd agent that:
- Starts smolclaw when you log in
- Restarts automatically if it crashes
- Logs to `~/.smolclaw/daemon-stdout.log`

Control it with:
```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.smolclaw.daemon.plist

# Start
launchctl load ~/Library/LaunchAgents/com.smolclaw.daemon.plist

# View logs
tail -f ~/.smolclaw/daemon-stdout.log
```

## Configuration Reference

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `telegram.botToken` | **Yes** | — | Bot token from BotFather |
| `telegram.allowedUsers` | **Yes** | — | Array of Telegram user IDs allowed to use the bot |
| `telegram.allowedUsernames` | No | `[]` | Additional allowed usernames (case-insensitive) |
| `anthropic.apiKey` | No | — | Claude API key. Omit to use OAuth |
| `anthropic.model` | No | `claude-opus-4-6` | Claude model ID |
| `anthropic.maxTokens` | No | `16384` | Max response tokens |
| `openai.apiKey` | No | — | OpenAI key for embedding-based memory search. Without it, memory uses keyword search only |
| `workspace.dir` | No | `~/` | Agent's working directory |
| `workspace.memoryDir` | No | `~/.smolclaw/memory` | Where DIRECTIVES.md and memory files live |
| `daemon.logLevel` | No | `info` | Log level: `trace` / `debug` / `info` / `warn` / `error` |
| `daemon.dataDir` | No | `~/.smolclaw` | Where the database and logs are stored |

## Telegram Commands

Quick commands that respond instantly (no LLM call):

| Command | Description |
|---------|-------------|
| `/status` | System overview — uptime, sessions, processes, jobs, memory count |
| `/ps` | List managed processes with status and PID |
| `/cron` | List scheduled jobs with next run time |
| `/loops` | List learning loops with cycle count |
| `/memory <query>` | Search persistent memory |
| `/memory add <text>` | Manually add a memory entry |
| `/reset` | Clear conversation history (fresh start) |
| `/help` | Show all commands |

Everything else you type goes to Claude.

## Tools

The agent has these tools available:

| Tool | Description |
|------|-------------|
| `bash` | Execute any shell command |
| `read_file` | Read file contents |
| `write_file` | Write/create files |
| `list_files` | Find files by glob pattern |
| `web_search` | Search the web |
| `web_fetch` | Fetch a URL and extract readable text |
| `memory_write` | Save info to persistent memory |
| `memory_search` | Search persistent memory |
| `process_manage` | Start/stop/restart/monitor processes |
| `cron_manage` | Create and manage scheduled jobs |
| `loop_manage` | Create learning loops |

Tool calls show up in your Telegram messages as expandable blocks — tap to see what the agent did.

## Architecture

```
Telegram message
    → Auth middleware (allowlist check)
    → Orchestrator
        → Memory injection (DIRECTIVES.md + relevant memories)
        → Claude API call with tools
        → Tool execution loop (max 20 iterations)
        → Combined response with expandable tool output
    → Single Telegram message back
```

**Memory is mandatory, not optional.** The orchestrator injects DIRECTIVES.md and query-relevant memories into the system prompt before every API call. The agent doesn't choose whether to check memory — it's already there.

## Tech Stack

- **Runtime:** [Bun](https://bun.sh)
- **AI:** [Anthropic SDK](https://docs.anthropic.com/en/docs/sdks) (Claude)
- **Telegram:** [grammY](https://grammy.dev)
- **Database:** bun:sqlite (WAL mode)
- **Embeddings:** OpenAI text-embedding-3-small (optional)
- **Vector search:** sqlite-vec (optional)
- **Cron:** croner
- **Logging:** pino

## Development

```bash
bun test          # Run all 75 tests
bun run dev       # Run with --watch (auto-restart on file changes)
bun start --reauth  # Force re-authentication
```

## License

MIT
