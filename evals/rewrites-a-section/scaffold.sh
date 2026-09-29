#!/bin/sh
. "$(dirname "$0")/../lib/demo-tracker.sh"
# A Carry forward grown past its limit: notes of the build, not facts a later ticket needs.
for fact in "Ran the migration locally twice" "Tried a composite key first, dropped it" \
    "TODO: look at the email index later" "created_at defaults to now()" "Migration file is 0001_users.sql"; do
  tracker add DEMO-1 carry "$fact"
done
