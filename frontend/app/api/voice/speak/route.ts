import { NextRequest, NextResponse } from "next/server";

// POST /api/voice/speak  { text } -> audio/mpeg
// Server-side proxy to ElevenLabs TTS. The API key never reaches the browser.
// Fails closed with a setup hint when no key is configured.
const VOICE_ID = process.env.ELEVENLABS_VOICE_ID || "21m00Tcm4TlvDq8ikWAM"; // Rachel (preset)
const MODEL_ID = "eleven_flash_v2_5"; // low-latency model, good for live demo

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
  let text: string = "";
  try {
    text = (await req.json()).text ?? "";
  } catch {
    return NextResponse.json({ error: "text required" }, { status: 400 });
  }
  if (!text || typeof text !== "string") {
    return NextResponse.json({ error: "text required" }, { status: 400 });
  }
  const r = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${VOICE_ID}`, {
    method: "POST",
    headers: { "xi-api-key": key, "Content-Type": "application/json" },
    body: JSON.stringify({ text: text.slice(0, 1000), model_id: MODEL_ID }),
  });
  if (!r.ok) {
    const detail = (await r.text()).slice(0, 200);
    return NextResponse.json({ error: "tts_failed", status: r.status, detail }, { status: 502 });
  }
  return new NextResponse(await r.arrayBuffer(), {
    headers: { "Content-Type": "audio/mpeg" },
  });
}
