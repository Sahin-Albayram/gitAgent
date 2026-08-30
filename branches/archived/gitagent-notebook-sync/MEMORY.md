# Branch Memory: gitagent-notebook-sync

## Branched From
gitagent-nested-branching

## Goal
Update GitAgent_Overview.ipynb to document nested branches and the new checkout-based, actually-diverging model.

## Status
Completed

## Decisions Log
- [2026-08-29 15:09 UTC] Rewrote the tool table and directory diagram to reflect the new base parameter and each tool's ending checkout.
- [2026-08-29 15:09 UTC] Added a nesting demo: parent branch, child branch opened with base=parent, close child (merges into parent), close parent (merges into main).
- [2026-08-29 15:09 UTC] Documented the STATE_TRACKER.md-is-only-authoritative-on-main gotcha found while testing nesting.

## Open Questions
