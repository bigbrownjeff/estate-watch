---
name: ship-pr
description: Safely take an existing local change through validation, adversarial review, an intentional commit, push, and pull-request creation. Use only when the user explicitly asks to ship/publish changes or open a PR; invocation does not authorize merging the PR.
---

# Ship a pull request

1. Confirm that the user authorized the external actions in scope: commit, push, and PR creation. Do not infer merge permission.
2. Verify the repository, worktree, branch, upstream, and exact dirty paths. Preserve unrelated changes and stop if the requested scope overlaps work you cannot safely separate.
3. Run the repository's full relevant tests, typecheck, lint, build, and generated-file checks. Record any pre-existing failures separately; do not call a failed suite clean.
4. Run an adversarial review of the final diff. Resolve material findings and rerun affected tests.
5. Stage only the intended paths. Review the staged diff and secret scan before committing with an accurate message.
6. Push the exact branch without force. If non-fast-forward, stop and reconcile deliberately; never overwrite remote work.
7. Open a draft pull request with the outcome, risk notes, tests, and any intentionally deferred work. Return the PR URL and commit SHA.
8. Merge or clean up worktrees/branches only when separately authorized and after rechecking the exact targets.
