---
type: regex
target: { source: file, path: .trackers/demo/log.md }
pattern: '^(?![\s\S]*GET /users[\s\S]*GET /users)[\s\S]*\[DEMO-2\] Commits on feat/DEMO-2-users-api: [0-9a-f]+ Add GET /users'
flags: i
---
