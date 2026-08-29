# Branch Memory: gitagent-nested-branching

## Branched From
main

## Goal
Add a base parameter to open_branch so branches can nest under other branches instead of always coming off main; make branches actually diverge (checkout-based) so close_branch's merge is real.

## Status
Active

## Decisions Log
- [2026-08-29 15:09 UTC] Switched update_branch/close_branch to checkout the target branch instead of staying on the caller's branch, since MEMORY.md now only lives on its own branch's history.
- [2026-08-29 15:09 UTC] Added MAIN_BRANCH constant and a Base column in STATE_TRACKER.md so close_branch knows whether to merge into main or into a parent branch.

## Open Questions
