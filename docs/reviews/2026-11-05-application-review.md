# Final focused verdict — HEAD 0fc35bf

Verdict: **Approved within the agreed local/demo delivery scope. All four original findings are closed; no remaining actionable finding in the reviewed fixes.**

The final rereview was limited to `final-residual.patch` and the corresponding `final-fix-report.md` appendix. It inspected the `current_items()` projection and five added regression cases; it did not broaden the audit or rerun the passing full suite. Root independently reports **133 pytest tests passed in 7.49 seconds**. Earlier scoped acceptance of the canonical identity/migration, item-specific scope, midnight guard fixes and verified process-tree stop correction remains in force.

Finding 4 residual is now closed: a latest proposal blocked only by `pending_submission`, without its own execution, switches to the actual terminal execution record only after every local execution for the target is resolved. The temporal dependency check requires that execution to have existed at evaluation and to have completed at/after that evaluation. This fixes both successful verification and explicit rejection, while preserving genuine newer mapping/business exceptions, external pending reported after an already-finished local execution, all unresolved executions, and immutable historical batch responses. Global status filtering/counts consume the same projection and do not initiate any submission.

The regression additions cover the original unknown -> newer blocked -> terminal sequence for verified and failed outcomes, independent newer mapping exceptions, multiple unresolved dependencies, and newer external pending after an older terminal result. They substantiate the correction without changing execution semantics or requiring a further migration.

This verdict concerns code and the supplied local verification evidence. It does not claim production data integration, real repricing or production deployment. Root owns the final post-restart UI verification and delivery statement. No source code, runtime state, credentials or production systems were accessed or changed by this final rereview; only this review document was updated.

Prior findings and interim verdicts below are preserved as review history and are superseded by this final verdict.

---
# Scoped rereview of consolidated fix — HEAD 4f2d628

Verdict: **Changes requested — findings 1–3 closed; finding 4 has one remaining P2 branch.**

Reviewed `final-fix.patch`, `final-fix-report.md`, affected owning source and the additional uncommitted `deploy/stop-local.ps1` lifecycle correction. Existing evidence supplied by root: sequential full pytest 128 passed and frontend build passed; actual local stop freed port 18765, backup/migration/restart succeeded. This reviewer did not rerun the passing full suite, perform process control, read credentials, or contact production. Post-restart UI verification remains root-owned.

- Finding 1 closed: canonical target identity protects claims/history independently of SKU source identity; simultaneous aliases block instead of mixing losses. Migration preserves original JSON/Item identity, merges most recent effective time, fails closed on unmappable protected rows, and retains every unresolved execution until separately reconciled.
- Finding 2 closed: Worker carries selected item to the service; full source refresh remains, but result/submission scope is restricted to its canonical target. The scope survives rename and rollover.
- Finding 3 closed: day guards cover collection/evaluation/loop/intent-to-upload boundaries, and durable fresh-day jobs preserve selected scope. Only a provably unsent new intent is released on rollover.
- Lifecycle fix accepted in scope: stop script retains project executable/command ownership checks before killing that verified process tree, addressing the Windows virtualenv launcher child interpreter. Root supplied actual stop/restart evidence.

## Remaining P2 — Resolved older execution is hidden behind its newer blocked proposal

Location: `backend/app/services/store.py:77–96` (`current_items`), together with the terminal-state update in `backend/app/services/execution.py` reconciliation.

The new current view selects the latest Batch-created item per target and additionally retains only unresolved executions. A normal sequence is (1) submit and receive unknown, (2) run/re-evaluate again, producing a newer `pending_submission` blocked item, (3) reconcile the original execution successfully. At step 3 the older verified item is no longer included and the newer blocked item is still displayed as 待处理 with “已有未完成核验的提交”. Thus successful recovery leaves the exact stale actionable exception the original finding sought to resolve.

Independent narrow reproduction against an in-memory SQLite DB and synthetic receipts: `run_batch('unknown')`, `run_batch('blocked-while-unknown')`, set lookup ready, then `reconcile()`. Result: **4 verified execution targets; all 4 current target records remain 待处理 / pending_submission**. No external systems or production data were used.

Resolve this relationship in the operational projection or mark execution-dependent blocked proposals superseded when their dependency terminates. Preserve genuine newer business/data exceptions and immutable batch history. Add regression coverage for this specific unknown -> newer blocked -> verified sequence (and rejection as appropriate).

The original review and evidence follow for audit; the latest verdict above takes precedence.

---
# Final application review — 2026-11-05

Verdict: **Changes requested — 4 actionable findings (2 P1, 2 P2).**

Scope: approved design including the execution-time USER SCOPE UPDATE, AGENTS.md, API contract, ledger, final-review.patch and its current owning source files. The prior core currency correction was already reviewed and accepted. This review covers the local working demo, persistence/recovery, API/UI and reserved adapter boundaries; missing production deployment, credentials, live data capture and real trial repricing are explicitly deferred and are not findings. No production calls, credential-file reads, source edits or repeat of the already-passing full test suite were performed. Three isolated in-memory SQLite reproductions used synthetic data only.

## Findings

### 1. P1 — Execution lock must follow the effective upload target, not a mutable source SKU

Location: `backend/app/domain/types.py:28–31`; consumers `backend/app/services/execution.py:28,149–175`; actual upload target `backend/app/adapters/excel.py:32–41`.

`VariantKey.identity()` includes `sku`, although the uploaded workbook identifies its target by shop label, product ID and item ID (and selected price column). If the ERP/source mapping changes the SKU text for the same online item, both `_enrich()` and `_claim()` look up a different guard. The original unknown execution stays locked under the old hash while a second upload is allowed against the same online target. The same issue bypasses the confirmed-effective 72-hour cooldown; aliases present simultaneously can also yield independent proposals for a single target.

Confirmed synthetic reproduction: run four eligible demo targets with submit returning `unknown`, change only every source/online `key.sku` to the original plus `-renamed`, run another batch. Result: `first_calls=4, total_calls=8, unique_effective_targets=4`. No known rejection or unlock occurred between these calls.

Fix: define a stable canonical identity matching the effective upload target, use it for guard/history protection, and reject ambiguous source aliases during rule grouping. Do not simply remove SKU from grouping and silently combine conflicting aliases. Existing persisted guards/executions need a conservative reconciliation or migration; unknown locks must survive the identity change.

### 2. P1 — Reevaluate one item currently executes the entire candidate population

Location: `backend/app/worker.py:61–62`; request creation `backend/app/api/routes.py:160–169`; UI action `frontend/src/ItemDrawer.vue:104–109`.

The item-specific endpoint records and enqueues `payload.item_id`, but Worker discards that payload and calls unrestricted `run_batch(job.id)`. Pressing “重新评估” in one item's drawer can therefore upload every unrelated ready item in the enabled pilot scope. Refreshing the whole source is appropriate for complete rules and is required by the API contract; authorizing uploads for unrelated targets is a separate scope expansion. This remains a real side-effect problem at the reserved live adapter boundary even though this delivery runs demo mode.

Fix: carry the selected canonical target through the job, refresh the complete sources, then limit evaluated-result persistence/submission to that target. Keep the explicit batch-run endpoint as the all-candidate operation. Add a focused test where two unrelated targets become eligible and the item-specific job submits only the chosen target.

### 3. P2 — Cached proposals can be submitted after the Beijing business day changes

Location: `backend/app/services/execution.py:84,104–118,137`.

`business_date` is captured once and a ready decision is cached for up to 300 seconds. The loop does not recheck the Beijing date before `_submit()`. When a batch straddles midnight, later items can use yesterday's loss snapshots, FX date and seven-day sales window. This directly violates the accepted requirement that unsubmitted items crossing the day boundary must be refetched and recalculated.

Confirmed synthetic reproduction: start at Beijing 23:59:59, advance the clock two seconds after each submit, return unknown lookups. The service collected twice at the beginning, then submitted four items: first decision/submission date November 5; remaining three submitted November 6 with November 5 decision traces. The cache age remained below 300 seconds, so there was no refresh.

Fix: check the business day immediately before each new intent. On rollover stop yesterday's unsubmitted work and enqueue/restart on a fresh current-day batch (or equivalently refetch/recalculate with the new date), while continuing reconciliation of already-submitted work. Retain the five-minute refresh policy within a single business day.

### 4. P2 — Current exceptions keep showing resolved historical proposals as actionable

Location: `backend/app/api/routes.py:99–100,145–150`; persistence behavior `backend/app/services/execution.py:62–80`.

Each reevaluation/new job creates another batch. Original blocked item rows intentionally remain as historical records, but `/items?status=待处理` and overview counts aggregate every historical row without a superseded/current distinction. A target verified in a later batch remains listed as 待处理 from the earlier batch, still with an enabled reevaluation action. The global exception list therefore cannot be cleared through successful reevaluation and repeated runs inflate contradictory status totals.

Confirmed synthetic reproduction: first run with automatic execution disabled, then enable pilot scope and run again. Four targets verify successfully, but there are still four historical 待处理 rows for those same verified targets returned by the global query.

Fix: preserve historical batch records, but make global operational lists/counts represent current per-target state or explicitly track superseded proposals and link their newer results. Do not hide unresolved execution records merely because a newer proposal exists; active unknown/mismatch executions remain operational exceptions. Batch history can continue showing its immutable original outcomes.

## Other reviewed areas

- Execution intent is committed before external upload; unknown/crashed submissions retain a persistent guard; lookup exceptions do not cause retransmission; explicit rejection releases the lock; actual-price mismatch pauses new submissions while reconciliation continues.
- The live adapter distinguishes transport acceptance from price evidence, checks returned composite identity and source time, and gates execution on configured contracts/sites. File mode cannot upload and demo receipts are labelled synthetic.
- Admin cookie authentication, HTTP-only/Strict cookie settings, origin checks on writes and production configuration gates were inspected; no additional actionable auth/CSRF defect was identified within the local delivery scope.
- Worker lease/heartbeat, source-unavailable retry, SQLite/MySQL setup, launcher ownership checks, Vue request/error handling and settings boundaries were inspected. No production validation is claimed. The all-tests/build/browser results cited by the root remain the root's existing evidence; this reviewer did not rerun them.

After the four corrections, request a focused rereview of the affected code and regression results. Current verdict does not imply the broader application is unimplemented; these are specific safety/operational gaps in an otherwise coherent local delivery.
