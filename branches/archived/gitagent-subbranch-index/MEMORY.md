# Branch Memory: gitagent-subbranch-index

## Branched From
main

## Goal
Stop giving nested branches their own STATE_TRACKER.md row; log them under their root branch's own Sub-Branches section instead, flattened across any nesting depth.

## Status
Active

## Decisions Log
- [2026-08-29 15:59 UTC] Replaced _branch_base (STATE_TRACKER.md lookup) with _branched_from, which reads a branch's own Branched From field via git show - works for any branch, open or closed, without needing it checked out.
- [2026-08-29 15:59 UTC] Added _root_branch, walking Branched From up until it hits a branch whose own base is main; used by both open_branch (to log Active) and close_branch (to log Completed) on the root's Sub-Branches section.
- [2026-08-29 15:59 UTC] Reverted STATE_TRACKER.md's Base column - it was always going to read main now, so it was dead weight. Migrated existing data: folded Branch-007 (the old nested row) into Branch-006's archived MEMORY.md as a Sub-Branches entry.

## Sub-Branches

## Open Questions
