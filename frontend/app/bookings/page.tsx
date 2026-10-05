"use client";

import { useCallback, useEffect, useState } from "react";
import { api, Booking } from "@/lib/api";

export default function BookingsPage() {
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [moves, setMoves] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    const { data } = await api<{ bookings: Booking[] }>("GET", "/api/bookings");
    setBookings(data.bookings ?? []);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function move(id: string) {
    const v = moves[id];
    if (!v) { alert("Pick a new date/time first."); return; }
    const { status, data } = await api("POST", "/api/bookings/move",
      { booking_id: id, new_start: v }) as { status: number; data: { error?: string } };
    if (status !== 200) { alert("Move failed: " + (data.error ?? status)); return; }
    load();
  }

  async function cancel(id: string) {
    if (!confirm(`Cancel booking ${id}?`)) return;
    await api("POST", "/api/bookings/cancel", { booking_id: id, reason: "cancelled in panel" });
    load();
  }

  return (
    <div className="tab">
      <h3>Bookings</h3>
      <div className="row"><button className="ghost" onClick={load}>Refresh</button></div>
      {bookings.length === 0 && <p className="muted">No confirmed bookings.</p>}
      {bookings.length > 0 && (
        <table>
          <thead><tr><th>When</th><th>Service</th><th>Customer</th><th>Reference</th><th></th></tr></thead>
          <tbody>
            {bookings.map((b) => (
              <tr key={b.id}>
                <td>{b.label}</td>
                <td>{b.service}</td>
                <td>{b.customer_name}</td>
                <td><b>{b.id}</b></td>
                <td>
                  <input type="datetime-local"
                    value={moves[b.id] ?? ""}
                    onChange={(e) => setMoves({ ...moves, [b.id]: e.target.value })} />{" "}
                  <button className="ghost" onClick={() => move(b.id)}>Move</button>{" "}
                  <button className="danger" onClick={() => cancel(b.id)}>Cancel</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
