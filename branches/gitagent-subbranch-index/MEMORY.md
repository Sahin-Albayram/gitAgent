# Branch Memory: gitagent-subbranch-index

## Branched From
main

## Goal
Stop giving nested branches their own STATE_TRACKER.md row; log them under their root branch's own Sub-Branches section instead, flattened across any nesting depth.

## Status
Active

## Decisions Log
- [2026-08-29 15:59 UTC] Replaced _branch_base (STATE_TRACKER.md lookup) with _branched_from, which reads a branch's own Branched From field via git show - works for any branch, open or closed, without needing it checked out.

## Sub-Branches

## Open Questions
