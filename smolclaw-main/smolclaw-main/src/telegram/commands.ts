import type { Context } from "grammy";
import type { Config } from "../config.ts";
import type { TelegramBot } from "./bot.ts";
import { getDatabase } from "../db/connection.ts";
import { escapeHtml } from "./formatting.ts";

/**
 * Handle slash commands. Returns true if the command was handled (no agent call needed).
 */
export async function handleCommand(
  ctx: Context,
  config: Config,
  bot: TelegramBot
): Promise<boolean> {
  const text = ctx.message?.text ?? "";
  const chatId = ctx.chat?.id;
  if (!chatId) return false;

  const [command, ...args] = text.split(" ");
  const cmd = command!.toLowerCase().replace("@" + (await ctx.api.getMe()).username?.toLowerCase(), "");

  switch (cmd) {
    case "/start":
    case "/help":
      await bot.sendMessage(
        chatId,
        `<b>smolclaw</b> — AI Agent via Telegram\n\n` +
          `<b>Commands:</b>\n` +
          `/status — System overview\n` +
          `/ps — List managed processes\n` +
          `/cron — List cron jobs\n` +
          `/loops — List learning loops\n` +
          `/memory &lt;query&gt; — Search memory\n` +
          `/memory add &lt;text&gt; — Add a memory\n` +
          `/reset — Clear conversation history\n` +
          `/help — This message\n\n` +
          `Or just send any message to chat with the agent.`,
        "HTML"
      );
      return true;

    case "/status":
      return await handleStatus(chatId, bot);

    case "/ps":
      return await handlePs(chatId, bot);

    case "/cron":
      return await handleCron(chatId, bot);

    case "/loops":
      return await handleLoops(chatId, bot);

    case "/memory":
      return await handleMemory(chatId, args, bot, config);

    case "/reset":
      return await handleReset(chatId, bot);

    default:
      return false; // Not a recognized command — pass to agent
  }
}

async function handleStatus(chatId: number, bot: TelegramBot): Promise<boolean> {
  const db = getDatabase();

  const sessionCount =
    db.query<{ count: number }, []>("SELECT COUNT(*) as count FROM sessions").get()?.count ?? 0;
  const processCount =
    db
      .query<{ count: number }, []>("SELECT COUNT(*) as count FROM processes WHERE status = 'running'")
      .get()?.count ?? 0;
  const cronCount =
    db.query<{ count: number }, []>("SELECT COUNT(*) as count FROM cron_jobs WHERE enabled = 1").get()
      ?.count ?? 0;
  const loopCount =
    db
      .query<{ count: number }, []>("SELECT COUNT(*) as count FROM loops WHERE status = 'active'")
      .get()?.count ?? 0;
  const memoryCount =
    db.query<{ count: number }, []>("SELECT COUNT(*) as count FROM memories").get()?.count ?? 0;

  const uptime = process.uptime();
  const hours = Math.floor(uptime / 3600);
  const mins = Math.floor((uptime % 3600) / 60);

  await bot.sendMessage(
    chatId,
    `<b>smolclaw status</b>\n\n` +
      `Uptime: ${hours}h ${mins}m\n` +
      `Sessions: ${sessionCount}\n` +
      `Running processes: ${processCount}\n` +
      `Active cron jobs: ${cronCount}\n` +
      `Active loops: ${loopCount}\n` +
      `Memories: ${memoryCount}`,
    "HTML"
  );
  return true;
}

async function handlePs(chatId: number, bot: TelegramBot): Promise<boolean> {
  const db = getDatabase();
  const processes = db
    .query<
      { name: string; status: string; pid: number | null; restart_count: number; command: string },
      []
    >("SELECT name, status, pid, restart_count, command FROM processes ORDER BY name")
    .all();

  if (processes.length === 0) {
    await bot.sendMessage(chatId, "No managed processes.");
    return true;
  }

  const lines = processes.map((p) => {
    const statusIcon = p.status === "running" ? "🟢" : p.status === "crashed" ? "🔴" : "⚪";
    const pidStr = p.pid ? ` (PID ${p.pid})` : "";
    const restartStr = p.restart_count > 0 ? ` [restarts: ${p.restart_count}]` : "";
    return `${statusIcon} <b>${escapeHtml(p.name)}</b> — ${p.status}${pidStr}${restartStr}\n   ${escapeHtml(p.command)}`;
  });

  await bot.sendMessage(chatId, `<b>Managed Processes</b>\n\n${lines.join("\n\n")}`, "HTML");
  return true;
}

async function handleCron(chatId: number, bot: TelegramBot): Promise<boolean> {
  const db = getDatabase();
  const jobs = db
    .query<
      { name: string; cron_expr: string; action_type: string; enabled: number; last_run_at: string | null },
      []
    >("SELECT name, cron_expr, action_type, enabled, last_run_at FROM cron_jobs ORDER BY name")
    .all();

  if (jobs.length === 0) {
    await bot.sendMessage(chatId, "No cron jobs.");
    return true;
  }

  const lines = jobs.map((j) => {
    const icon = j.enabled ? "✅" : "⏸";
    const lastRun = j.last_run_at ? `Last: ${j.last_run_at}` : "Never run";
    return `${icon} <b>${escapeHtml(j.name)}</b> [${escapeHtml(j.action_type)}]\n   ${escapeHtml(j.cron_expr)} — ${lastRun}`;
  });

  await bot.sendMessage(chatId, `<b>Cron Jobs</b>\n\n${lines.join("\n\n")}`, "HTML");
  return true;
}

async function handleLoops(chatId: number, bot: TelegramBot): Promise<boolean> {
  const db = getDatabase();
  const loops = db
    .query<
      {
        name: string;
        status: string;
        cycle_count: number;
        review_cron: string;
        last_cycle_at: string | null;
        apply_mode: string;
      },
      []
    >(
      "SELECT name, status, cycle_count, review_cron, last_cycle_at, apply_mode FROM loops ORDER BY name"
    )
    .all();

  if (loops.length === 0) {
    await bot.sendMessage(chatId, "No learning loops.");
    return true;
  }

  const lines = loops.map((l) => {
    const icon = l.status === "active" ? "🔄" : l.status === "paused" ? "⏸" : "✅";
    const lastCycle = l.last_cycle_at ? `Last: ${l.last_cycle_at}` : "Never run";
    return `${icon} <b>${escapeHtml(l.name)}</b> [${l.apply_mode}]\n   ${escapeHtml(l.review_cron)} — Cycles: ${l.cycle_count} — ${lastCycle}`;
  });

  await bot.sendMessage(chatId, `<b>Learning Loops</b>\n\n${lines.join("\n\n")}`, "HTML");
  return true;
}

async function handleMemory(
  chatId: number,
  args: string[],
  bot: TelegramBot,
  config: Config
): Promise<boolean> {
  if (args.length === 0) {
    await bot.sendMessage(
      chatId,
      "Usage:\n/memory <query> — Search memory\n/memory add <text> — Add a memory"
    );
    return true;
  }

  if (args[0] === "add" && args.length > 1) {
    const content = args.slice(1).join(" ");
    try {
      const { addMemory } = await import("../memory/store.ts");
      await addMemory(content, "manual", undefined, []);
      await bot.sendMessage(chatId, "Memory saved.");
    } catch (err) {
      await bot.sendMessage(chatId, `Failed to save memory: ${err instanceof Error ? err.message : String(err)}`);
    }
    return true;
  }

  // Search
  const query = args.join(" ");
  try {
    const { searchMemory } = await import("../memory/search.ts");
    const results = await searchMemory(query, 5);
    if (results.length === 0) {
      await bot.sendMessage(chatId, "No memories found.");
    } else {
      const lines = results.map(
        (r, i) =>
          `<b>${i + 1}.</b> ${escapeHtml(r.content.slice(0, 200))}${r.content.length > 200 ? "..." : ""}\n   <i>${r.source} — ${r.created_at}</i>`
      );
      await bot.sendMessage(chatId, `<b>Memory Search Results</b>\n\n${lines.join("\n\n")}`, "HTML");
    }
  } catch (err) {
    await bot.sendMessage(chatId, `Memory search failed: ${err instanceof Error ? err.message : String(err)}`);
  }
  return true;
}

async function handleReset(chatId: number, bot: TelegramBot): Promise<boolean> {
  const db = getDatabase();
  db.prepare("DELETE FROM sessions WHERE id = ?").run(String(chatId));
  await bot.sendMessage(chatId, "Conversation history cleared.");
  return true;
}
