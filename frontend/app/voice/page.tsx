"use client";

import { useRef, useState } from "react";
import { api, Msg, Slot } from "@/lib/api";

type Phase = "idle" | "ready" | "slots" | "held" | "done";

const GREETING = "Hi, thanks for calling Glow Studio. What can I book for you today?";
const NO_KEY_HINT =
  "Voice needs an ElevenLabs key: copy frontend/.env.example to frontend/.env.local, add your key, restart npm run dev.";

export default function VoicePage() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [phase, setPhase] = useState<Phase>("idle");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [bookingId, setBookingId] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [retryMsg, setRetryMsg] = useState<string | null>(null);

  const mediaRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const phaseRef = useRef<Phase>("idle");
  phaseRef.current = phase;

  const push = (m: Msg[]) => setMsgs((p) => [...p, ...m]);

  async function speak(text: string): Promise<boolean> {
    try {
      const r = await fetch("/api/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        setNotice(d.error === "voice_not_configured" ? NO_KEY_HINT : `Speech failed (${r.status}).`);
        return false;
      }
      const url = URL.createObjectURL(await r.blob());
      await new Audio(url).play();
      return true;
    } catch {
      setNotice("Speech playback failed.");
      return false;
    }
  }

  async function startCall() {
    setNotice(null);
    setRetryMsg(null);
    push([{ speaker: "agent", text: GREETING }]);
    await speak(GREETING); // also unlocks audio on the click gesture
    setPhase("ready");
  }

  async function startRec() {
    setNotice(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream);
      chunksRef.current = [];
      mr.ondataavailable = (e) => {
        if (e.data.size) chunksRef.current.push(e.data);
      };
      mr.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        void transcribe(new Blob(chunksRef.current, { type: mr.mimeType || "audio/webm" }));
      };
      mr.start();
      mediaRef.current = mr;
      setRecording(true);
    } catch {
      setNotice("Microphone unavailable — allow mic access and use Chrome.");
    }
  }

  function stopRec() {
    mediaRef.current?.stop();
    setRecording(false);
  }

  async function transcribe(blob: Blob) {
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("audio", blob, "clip.webm");
      const r = await fetch("/api/voice/transcribe", { method: "POST", body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        setNotice(d.error === "voice_not_configured" ? NO_KEY_HINT : "Transcription failed.");
        return;
      }
      const text = (d.text || "").trim();
      if (!text) {
        setNotice("Didn't catch that — hold the button and try again.");
        return;
      }
      await onHeard(text);
    } finally {
      setBusy(false);
    }
  }

  async function onHeard(text: string) {
    push([{ speaker: "caller", text }]);
    const ph = phaseRef.current;
    if (ph === "ready") {
      const { data } = await api<any>("POST", "/api/call/start", { want: text });
      if (data.handoff) {
        const line = data.transcript.slice(-1)[0]?.text ?? "";
        push([{ speaker: "agent", text: line }]);
        await speak(line);
        setPhase("done");
        return;
      }
      setSessionId(data.session_id);
      setSlots(data.slots);
      const line = `Got it — ${String(data.service).toLowerCase()}. Here are the next open times. Tap the one you want.`;
      push([{ speaker: "agent", text: line }]);
      await speak(line);
      setPhase("slots");
    } else if (ph === "held") {
      const { data } = await api<any>("POST", "/api/call/commit",
        { session_id: sessionId, name: text, phone: "" });
      if (data.handoff) {
        const line = data.transcript.slice(-1)[0]?.text ?? "";
        push([{ speaker: "agent", text: line }]);
        await speak(line);
        setPhase("done");
        return;
      }
      const line = data.transcript.slice(-1)[0]?.text ?? "";
      push([{ speaker: "agent", text: line }]);
      await speak(line);
      setBookingId(data.booking_id);
      setPhase("done");
    }
  }

  async function choose(i: number) {
    const { status, data } = await api<any>("POST", "/api/call/choose",
      { session_id: sessionId, pick: i });
    if (data.transcript) push(data.transcript.slice(-2));
    if (data.handoff) { setPhase("done"); return; }
    if (data.conflict && data.slots) { setSlots(data.slots); return; }
    if (status === 200 && data.hold_id) {
      const line = data.transcript.slice(-1)[0]?.text ?? "What's your name?";
      await speak(line);
      setPhase("held");
    }
  }

  async function retry() {
    const { data } = await api<any>("POST", "/api/call/retry", { session_id: sessionId });
    setRetryMsg(
      data.same_id && data.no_duplicate
        ? `Retry returned the same id (${data.booking_id}) — no second booking created.`
        : "PROBLEM: duplicate detected."
    );
  }

  function reset() {
    setSessionId(null); setSlots([]); setPhase("idle");
    setBookingId(null); setRetryMsg(null); setMsgs([]); setNotice(null);
  }

  return (
    <div className="tab">
      <h3>Voice call <span className="muted">— same engine, spoken channel (ElevenLabs)</span></h3>
      {notice && <div className="card"><span className="err">{notice}</span></div>}

      <div className="chat">
        {msgs.map((m, i) => (
          <div className={`msg ${m.speaker}`} key={i}>
            <b>{m.speaker === "agent" ? "Front Desk" : "Caller"}:</b> {m.text}
          </div>
        ))}
        {msgs.length === 0 && (
          <p className="muted">Press “Start voice call”, then hold the talk button to speak. Everything the agent says is synthesized; every booking id comes from the server.</p>
        )}
      </div>

      {phase === "idle" && (
        <div className="row">
          <button className="action" onClick={startCall}>Start voice call</button>
        </div>
      )}

      {(phase === "ready" || phase === "held") && (
        <div className="row">
          <button
            className="action"
            disabled={busy}
            onMouseDown={startRec}
            onMouseUp={stopRec}
            onMouseLeave={() => recording && stopRec()}
            onTouchStart={(e) => { e.preventDefault(); void startRec(); }}
            onTouchEnd={(e) => { e.preventDefault(); stopRec(); }}
            style={{ padding: "14px 26px", fontSize: 16, background: recording ? "#b3261e" : undefined }}
          >
            {recording ? "● Listening… release to send" : busy ? "Working…" : "Hold to talk"}
          </button>
          {phase === "held" && <span className="muted">Say your name to confirm the booking.</span>}
        </div>
      )}

      {phase === "slots" && (
        <div className="row">
          {slots.map((s) => (
            <button key={s.index} className="ghost slotbtn" onClick={() => choose(s.index)}>
              {s.label}
            </button>
          ))}
        </div>
      )}

      {phase === "done" && bookingId && (
        <div className="card">
          <span className="ok">Booked.</span> Reference <b>{bookingId}</b> — issued by the server.<br /><br />
          <button className="action" onClick={retry}>Retry commit (same key)</button>
          {retryMsg && <><br /><br />{retryMsg.startsWith("PROBLEM") ? <span className="err">{retryMsg}</span> : <span className="ok">{retryMsg}</span>}</>}
        </div>
      )}

      {phase === "done" && (
        <div className="row"><button className="ghost" onClick={reset}>Start a new call</button></div>
      )}
    </div>
  );
}
