"use client";

import { useCallback, useEffect, useState } from "react";
import { api, Msg } from "@/lib/api";

interface CallRecord {
  at: string; booking_id: string | null; handoff: boolean; transcript: Msg[];
}

export default function TodayPage() {
  const [calls, setCalls] = useState<CallRecord[]>([]);

  const load = useCallback(async () => {
    const { data } = await api<{ calls: CallRecord[] }>("GET", "/api/calls");
    setCalls(data.calls ?? []);
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="tab">
      <h3>Today&apos;s calls</h3>
      <div className="row"><button className="ghost" onClick={load}>Refresh</button></div>
      {calls.length === 0 && <p className="muted">No calls yet today. Make one in the Text Call tab.</p>}
      {calls.map((c, i) => (
        <div className="card" key={i}>
          <div className="muted">
            {c.at} {c.booking_id && <>· booking <b>{c.booking_id}</b></>} {c.handoff && <span className="pill warn">handoff</span>}
          </div>
          <div className="chat">
            {c.transcript.map((m, j) => (
              <div className={`msg ${m.speaker}`} key={j}>
                <b>{m.speaker === "agent" ? "Front Desk" : "Caller"}:</b> {m.text}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
