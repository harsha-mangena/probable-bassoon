"""Isolated browser-test server with disposable data and explicit test accounts."""

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import App, ClinicServer, make_engine

e = make_engine(sys.argv[1])
a = App(e)
a.auth.create_user("browser-admin", "browser-test-password-123", "admin")
a.auth.create_user("browser-manager", "browser-test-password-123", "manager")
srv = ClinicServer(("127.0.0.1", 0), a)
day = e.clock().astimezone(e.tz).date() + timedelta(days=2)
while day.weekday() > 4:
    day += timedelta(days=1)
print(
    json.dumps(
        {
            "port": srv.server_address[1],
            "day": day.isoformat(),
            "override_day": (day + timedelta(days=1)).isoformat(),
        }
    ),
    flush=True,
)
srv.serve_forever()
