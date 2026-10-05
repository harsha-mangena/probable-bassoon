"use client";

import { useState } from "react";
import { api, Msg, Slot } from "@/lib/api";

type Stage = "idle" | "slots" | "held" | "done";

export default function CallPage() {
  const [msgs, setMsgs] = useState<Msg[]>([
    { speaker: "agent", text: "Hi, thanks for calling Glow Studio. What can I book for you today?" },
  ]);
  const [want, setWant] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [stage, setStage] = useState<Stage>("idle");
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [bookingId, setBookingId] = useState<string | null>(null);
  const [retryMsg, setRetryMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const push = (m: Msg[]) => setMsgs((prev) => [...prev, ...m]);

  async function start() {
    const w = want.trim() || "I want a haircut";
    push([{ speaker: "caller", text: w }]);
    setBusy(true);
    const { data } = await api<any>("POST", "/api/call/start", { want: w });
    setBusy(false);
    if (data.handoff) {
      push(data.transcript.slice(-1));
      setStage("done");
      return;
    }
    setSessionId(data.session_id);
    setSlots(data.slots);
    push([{ speaker: "agent", text: `Got it — ${String(data.service).toLowerCase()}. Here are the next open times:` }]);
    setStage("slots");
  }

  async function choose(i: number) {
    setBusy(true);
    const { status, data } = await api<any>("POST", "/api/call/choose", { session_id: sessionId, pick: i });
    setBusy(false);
    if (data.transcript) push(data.transcript.slice(-2));
    if (data.handoff) { setStage("done"); return; }
    if (data.conflict && data.slots) {
      setSlots(data.slots); // that time just got taken — pick another
      return;
    }
    if (status === 200 && data.hold_id) setStage("held");
  }

  async function commit() {
    if (!name.trim()) { alert("Name is required."); return; }
    push([{ speaker: "caller", text: phone.trim() ? `${name.trim()}. ${phone.trim()}` : name.trim() }]);
    setBusy(true);
    const { data } = await api<any>("POST", "/api/call/commit",
      { session_id: sessionId, name: name.trim(), phone: phone.trim() });
    setBusy(false);
    if (data.handoff) {
      push(data.transcript.slice(-1));
      setStage("done");
      return;
    }
    push(data.transcript.slice(-1));
    setBookingId(data.booking_id);
    setStage("done");
  }

  async function retry() {
    const { data } = await api<any>("POST", "/api/call/retry", { session_id: sessionId });
    setRetryMsg(
      data.same_id && data.no_duplicate
        ? `Retry returned the same id (${data.booking_id}) — no second booking created. (before: ${data.bookings_before}, after: ${data.bookings_after}, replayed: ${String(data.replayed)})`
        : "PROBLEM: duplicate detected."
    );
  }

  function reset() {
    setSessionId(null); setSlots([]); setStage("idle");
    setName(""); setPhone(""); setBookingId(null); setRetryMsg(null); setWant("");
    setMsgs([{ speaker: "agent", text: "Hi, thanks for calling Glow Studio. What can I book for you today?" }]);
  }

  return (
    <div className="tab">
      <h3>Text call <span className="muted">— drives the real engine, nothing is mocked</span></h3>
      <div className="chat">
        {msgs.map((m, i) => (
          <div className={`msg ${m.speaker}`} key={i}>
            <b>{m.speaker === "agent" ? "Front Desk" : "Caller"}:</b> {m.text}
          </div>
        ))}
      </div>

      {stage === "idle" && (
        <div className="row">
          <input value={want} onChange={(e) => setWant(e.target.value)}
            placeholder='Try: "I want a haircut"' style={{ flex: 1, minWidth: 220 }} />
          <button className="action" disabled={busy} onClick={start}>Start call</button>
        </div>
      )}

      {stage === "slots" && (
        <div className="row">
          {slots.map((s) => (
            <button key={s.index} className="ghost slotbtn" disabled={busy} onClick={() => choose(s.index)}>
              {s.label}
            </button>
          ))}
        </div>
      )}

      {stage === "held" && (
        <>
          <div className="row">
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" />
            <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="Phone" />
            <button className="action" disabled={busy} onClick={commit}>Confirm booking</button>
          </div>
          <p className="muted">Slot held server-side. Nothing is confirmed until you press confirm.</p>
        </>
      )}

      {stage === "done" && bookingId && (
        <div className="card">
          <span className="ok">Booked.</span> Reference <b>{bookingId}</b> — issued by the server.<br /><br />
          <button className="action" onClick={retry}>Retry commit (same key)</button>{" "}
          <span className="muted">Replays the identical idempotency key. A correct engine returns the same id and creates nothing.</span>
          {retryMsg && <><br /><br />{retryMsg.startsWith("PROBLEM") ? <span className="err">{retryMsg}</span> : <span className="ok">{retryMsg}</span>}</>}
        </div>
      )}

      {stage === "done" && (
        <div className="row"><button className="ghost" onClick={reset}>Start a new call</button></div>
      )}
    </div>
  );
}
