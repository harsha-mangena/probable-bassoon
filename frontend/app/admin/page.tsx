"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

interface Hold { id: string; slot_label: string; expires_in_min: number | null }
interface Reminder { booking_id: string; channel: string; sent: number }
interface AdminData {
  engine: string; business: string; timezone: string; calendar_provider: string;
  counts: { bookings_confirmed: number; holds_active: number; reminders_pending: number; calls_today: number };
  holds: Hold[]; reminders: Reminder[];
}

export default function AdminPage() {
  const [data, setData] = useState<AdminData | null>(null);

  const load = useCallback(async () => {
    const { data } = await api<AdminData>("GET", "/api/admin");
    setData(data);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function action(path: string, confirmMsg?: string) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    const { data } = await api<any>("POST", path, {});
    alert(JSON.stringify(data));
    load();
  }

  if (!data) return <div className="tab"><p className="muted">Loading…</p></div>;
  const c = data.counts;

  return (
    <div className="tab">
      <h3>Admin <span className="muted">— control plane</span></h3>
      <div className="row">
        <button className="ghost" onClick={load}>Refresh</button>
        <button className="ghost" onClick={() => action("/api/admin/expire_holds")}>Expire holds now</button>
        <button className="ghost" onClick={() => action("/api/admin/sweep")}>Run reminder sweep</button>
        <button className="danger" onClick={() => action("/api/admin/reset", "Reset demo state? Clears bookings, holds, calls.")}>Reset demo</button>
      </div>
      <div className="row">
        <div className="card"><b>Engine</b><br /><span className="ok">{data.engine}</span></div>
        <div className="card"><b>Confirmed bookings</b><br />{c.bookings_confirmed}</div>
        <div className="card"><b>Active holds</b><br />{c.holds_active}</div>
        <div className="card"><b>Pending reminders</b><br />{c.reminders_pending}</div>
        <div className="card"><b>Calls today</b><br />{c.calls_today}</div>
      </div>
      <p className="muted">Calendar provider: {data.calendar_provider} · Timezone: {data.timezone}</p>
      <h3>Holds</h3>
      {data.holds.length === 0 && <p className="muted">No active holds.</p>}
      {data.holds.length > 0 && (
        <table><thead><tr><th>Hold</th><th>Slot</th><th>Expires in</th></tr></thead>
          <tbody>{data.holds.map((h) => (
            <tr key={h.id}><td>{h.id}</td><td>{h.slot_label}</td><td>{h.expires_in_min} min</td></tr>
          ))}</tbody></table>
      )}
      <h3>Reminders</h3>
      {data.reminders.length === 0 && <p className="muted">No reminders queued.</p>}
      {data.reminders.length > 0 && (
        <table><thead><tr><th>Booking</th><th>Channel</th><th>Sent</th></tr></thead>
          <tbody>{data.reminders.map((r, i) => (
            <tr key={i}><td>{r.booking_id}</td><td>{r.channel}</td><td>{r.sent ? "yes" : "no"}</td></tr>
          ))}</tbody></table>
      )}
    </div>
  );
}
