import { getDatabase } from "../db/connection.ts";

export interface ProcessRecord {
  id: string;
  name: string;
  command: string;
  cwd: string | null;
  status: string;
  pid: number | null;
  restart_policy: string;
  max_restarts: number;
  restart_count: number;
  last_exit_code: number | null;
  created_at: string;
  updated_at: string;
}

export function getProcessRecord(name: string): ProcessRecord | null {
  const db = getDatabase();
  return db.query<ProcessRecord, [string]>("SELECT * FROM processes WHERE name = ?").get(name) ?? null;
}

export function upsertProcess(record: Partial<ProcessRecord> & { name: string; id: string }): void {
  const db = getDatabase();
  const existing = getProcessRecord(record.name);

  if (existing) {
    const updates: string[] = [];
    const values: unknown[] = [];

    for (const [key, value] of Object.entries(record)) {
      if (key === "name" || key === "id" || key === "created_at") continue;
      updates.push(`${key} = ?`);
      values.push(value);
    }
    updates.push("updated_at = datetime('now')");
    values.push(record.name);

    db.prepare(`UPDATE processes SET ${updates.join(", ")} WHERE name = ?`).run(...(values as any[]));
  } else {
    db.prepare(
      `INSERT INTO processes (id, name, command, cwd, status, pid, restart_policy, max_restarts, restart_count)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).run(
      record.id,
      record.name,
      record.command ?? "",
      record.cwd ?? null,
      record.status ?? "stopped",
      record.pid ?? null,
      record.restart_policy ?? "none",
      record.max_restarts ?? 3,
      record.restart_count ?? 0
    );
  }
}

export function updateProcessStatus(name: string, status: string, pid?: number | null, exitCode?: number | null): void {
  const db = getDatabase();
  db.prepare(
    "UPDATE processes SET status = ?, pid = ?, last_exit_code = ?, updated_at = datetime('now') WHERE name = ?"
  ).run(status, pid ?? null, exitCode ?? null, name);
}

export function incrementRestartCount(name: string): number {
  const db = getDatabase();
  db.prepare(
    "UPDATE processes SET restart_count = restart_count + 1, updated_at = datetime('now') WHERE name = ?"
  ).run(name);
  const record = getProcessRecord(name);
  return record?.restart_count ?? 0;
}

export function listProcessRecords(): ProcessRecord[] {
  const db = getDatabase();
  return db.query<ProcessRecord, []>("SELECT * FROM processes ORDER BY name").all();
}

export function deleteProcessRecord(name: string): void {
  const db = getDatabase();
  db.prepare("DELETE FROM processes WHERE name = ?").run(name);
}
