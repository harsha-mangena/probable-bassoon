"""Optional Twilio trial/PSTN adapter. Never enabled without signature validation."""

import base64
import hashlib
import hmac
import json
from xml.etree.ElementTree import Element, SubElement, tostring
from engine import Problem, digest


def valid_signature(token, url, params, signature):
    payload = url + "".join(
        k + v for k in sorted(params) for v in sorted(set(params[k]))
    )
    expected = base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature or "")


def handle(calls, params, step=None):
    values = {k: v[0] for k, v in params.items()}
    cid = values.get("CallSid", "")
    if not cid.startswith("CA") or len(cid) > 100:
        raise Problem("Missing call identifier")
    if step is None:
        result = calls.start("phone-" + cid)
    else:
        try:
            seq = int(step)
        except ValueError:
            raise Problem("Invalid call step")
        text = values.get("SpeechResult") or values.get("Digits") or ""
        rid = "phone-" + digest(json.dumps([cid, step, text]))
        result = calls.turn("phone-" + cid, text, rid, seq)
    root = Element("Response")
    message = result["message"]
    # Speak long references as individual digits so callers can write them down.
    import re

    message = re.sub(r"\b\d{10,12}\b", lambda m: " ".join(m[0]), message)
    if result["stage"] == "done":
        SubElement(root, "Say").text = message
        SubElement(root, "Hangup")
    else:
        gather = SubElement(
            root,
            "Gather",
            {
                "input": "dtmf speech",
                "action": f"/api/voice/twilio?step={result['seq']}",
                "method": "POST",
                "timeout": "8",
                "speechTimeout": "auto",
                "actionOnEmptyResult": "true",
                "finishOnKey": "#",
            },
        )
        SubElement(gather, "Say").text = (
            message + " Use the keypad followed by pound, or speak your response."
        )
    return tostring(root, encoding="utf-8", xml_declaration=True)
