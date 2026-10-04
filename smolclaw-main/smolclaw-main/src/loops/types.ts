export interface LoopRecord {
  id: string;
  name: string;
  description: string | null;
  target_process: string | null;
  review_cron: string;
  review_prompt: string;
  apply_mode: string;
  status: string;
  cycle_count: number;
  last_cycle_at: string | null;
  last_cycle_result: string | null;
  created_at: string;
}

export interface CycleResult {
  cycle: number;
  timestamp: string;
  findings: string;
  actions_taken: string[];
  recommendations: string[];
}
