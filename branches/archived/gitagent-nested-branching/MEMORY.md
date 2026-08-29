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
- [2026-08-29 15:09 UTC] Added _ensure_clean_worktree(), scoped to tracked-file changes only (git itself already protects against clobbering untracked files on checkout).

## Sub-Branches
- **gitagent-notebook-sync** — Completed — The branch updates the GitAgent_Overview.ipynb notebook to document nested branches and a new diverging model, rewriting the tool table and directory diagram to reflect the changes. The key decision made was to adopt a checkout-based approach, rather than the previous model. The outcome is a clearer understanding of how nested branches interact with the STATE_TRACKER.

## Open Questions
