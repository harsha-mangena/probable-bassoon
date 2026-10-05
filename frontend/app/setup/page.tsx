"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

interface Svc { id: string; name: string; duration_min: number; buffer_after_min?: number }
interface Config {
  business: { name: string; timezone: string; transfer_phone: string };
  calendar: { provider: string; note: string };
  services: Svc[];
  hours_weekly: Record<string, string[][]>;
  policy: { min_notice_min: number; max_advance_days: number; slot_increment_min: number; hold_ttl_min: number };
  reminders: { confirm: string; remind_before_min: number[] };
}

export default function SetupPage() {
  const [cfg, setCfg] = useState<Config | null>(null);

  useEffect(() => {
    api<{ }>("GET", "/api/config").then(({ data }) => setCfg(data as Config));
  }, []);

  if (!cfg) return <div className="tab"><p className="muted">Loading…</p></div>;

  return (
    <div className="tab">
      <h3>Setup <span className="muted">— rules live in config; nobody edits prompts</span></h3>
      <div className="kv">
        <b>Business</b><span>{cfg.business.name}</span>
        <b>Timezone</b><span>{cfg.business.timezone}</span>
        <b>Transfer</b><span>{cfg.business.transfer_phone}</span>
        <b>Calendar</b><span>{cfg.calendar.provider} — {cfg.calendar.note}</span>
      </div>
      <h3>Services</h3>
      <table>
        <thead><tr><th>Service</th><th>Duration</th><th>Buffer after</th></tr></thead>
        <tbody>
          {cfg.services.map((s) => (
            <tr key={s.id}><td>{s.name}</td><td>{s.duration_min} min</td><td>{s.buffer_after_min ?? 0} min</td></tr>
          ))}
        </tbody>
      </table>
      <h3>Hours</h3>
      {Object.entries(cfg.hours_weekly).map(([d, h]) => (
        <div key={d}><b>{d}</b>: {h.length ? h.map((x) => x.join("–")).join(", ") : "closed"}</div>
      ))}
      <h3>Policy</h3>
      <div className="kv">
        <b>Min notice</b><span>{cfg.policy.min_notice_min} min</span>
        <b>Max advance</b><span>{cfg.policy.max_advance_days} days</span>
        <b>Slot grid</b><span>{cfg.policy.slot_increment_min} min</span>
        <b>Hold TTL</b><span>{cfg.policy.hold_ttl_min} min</span>
      </div>
      <h3>Reminders</h3>
      <div className="kv">
        <b>Channel</b><span>{cfg.reminders.confirm}</span>
        <b>Send before</b><span>{cfg.reminders.remind_before_min.join(", ")} min</span>
      </div>
    </div>
  );
}
