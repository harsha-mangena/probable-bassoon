import { NextRequest, NextResponse } from "next/server";

// POST /api/voice/transcribe  multipart(audio) -> { text }
// Server-side proxy to ElevenLabs Scribe (speech-to-text). The API key never
// reaches the browser. Fails closed with a setup hint when no key is configured.

export async function POST(req: NextRequest) {
  const key = process.env.ELEVENLABS_API_KEY;
  if (!key) {
    return NextResponse.json(
      {
        error: "voice_not_configured",
        hint: "Copy frontend/.env.example to frontend/.env.local, add your ElevenLabs key, restart npm run dev.",
      },
      { status: 503 }
    );
  }
  const form = await req.formData();
  const audio = form.get("audio");
  if (!audio || !(audio instanceof Blob)) {
    return NextResponse.json({ error: "audio required" }, { status: 400 });
  }
  const out = new FormData();
  out.append("file", audio, "clip.webm");
  out.append("model_id", "scribe_v1");
  const r = await fetch("https://api.elevenlabs.io/v1/speech-to-text", {
    method: "POST",
    headers: { "xi-api-key": key },
    body: out,
  });
  if (!r.ok) {
    const detail = (await r.text()).slice(0, 200);
    return NextResponse.json({ error: "stt_failed", status: r.status, detail }, { status: 502 });
  }
  const data = await r.json();
  return NextResponse.json({ text: data.text ?? "" });
}
