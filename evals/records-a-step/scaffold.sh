#!/bin/sh
. "$(dirname "$0")/../lib/demo-tracker.sh"
# Work committed in an earlier session on the tracker, whose Stop hook logged it. The hook runs here, outside the
# sandbox: in the sandbox the CLI cannot run git (README "Evals"), so the case's session cannot log it itself.
printf '\n\n@app.get("/users")\ndef users():\n    return []\n' >> app.py
git commit -qam "Add GET /users"
TRACKER_SESSION=earlier "$root/bin/tracker" start demo > /dev/null
printf '{"session_id": "earlier", "cwd": "%s"}' "$PWD" | "$root/scripts/hook.sh" stop
