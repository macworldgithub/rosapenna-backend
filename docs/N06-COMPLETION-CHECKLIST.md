# N06 completion checklist

Updated: 2026-11-13. Current ticket status: **Partially Completed**.

## Use this checklist

Read [the detailed implementation plan](N06-IMPLEMENTATION-PLAN.md), [root instructions](../../AGENTS.md), and [N06 requirements](../../knowledge-vault/wiki/projects/rosapenna-booking/tickets/n06-paid-booking-creation.md) first. This checklist covers the remaining local implementation and verification work. Execute tasks in numeric order. Preserve existing uncommitted changes in both repositories, including the README edits. Do not restart the ticket from scratch.

Every box below starts unchecked. A file's existence or a passing unrelated test does not close a box. Check a box only after its implementation and named result are verified. Below each task, record changed files, test command, result, evidence scope, and remaining limit. If blocked, leave it unchecked and name the required input and owner. Planning edits are not code evidence and must not be logged as implementation in the vault.

Backend paths below are relative to `rosapenna-backend/`. Frontend paths are relative to `rospaenna-frontend/`. Inside backend file lists, paths without a project prefix are under `rosapenna_backend/` unless a path starts with `scripts/`, `tests/`, or `docs/`. All proposed fields, methods, and commands require implementation before use.

## Keep the implementation small

Use the smallest change that meets the named failure case. The task groups below divide review and verification; they do not require separate services or modules.

- Reuse FastAPI, Next.js, MongoDB, existing adapters, and existing test tools. Prefer extending current functions and schemas to adding a framework or generic abstraction.
- Implement checkout ownership with one scoped capability using established crypto and cookie primitives. Do not add accounts, passwords, JWT infrastructure, or a new authentication product for checkout.
- Use unique indexes, conditional updates, and stable component keys in the existing operation record. Reuse an existing worker/recovery pattern for saved pending work. If none fits, use a small local worker function/command. Do not introduce a message broker, distributed workflow engine, new microservice, or scheduler framework.
- Keep network restrictions local to the launched development/test processes. Do not change machine-wide firewall, DNS, or production configuration. Reuse the current test runner; add a small shared safety helper only if both launchers need it.
- Use existing stateful test adapters for business logic. Add local HTTP tests only where they verify real serialization, routing, signatures, timeouts, or retries. Do not build a general provider emulator or require Postman installation/publication to finish the ticket.
- Add no dependency unless an existing pinned dependency or standard library cannot meet a named requirement. Add no new feature, reminder policy, payment adjustment, automatic refund, or cancellation.
- Keep tests tied to the failure cases and acceptance criteria below. Run the affected tests and required lint/type/build checks. Repeat or broaden them only after a change or unresolved failure justifies it.

Ownership, exact payment matching, database duplicate protection, independent reads, recoverable failures, and truthful status are required controls. Simplify their implementation; do not remove the controls to reduce code size.

## Source check and existing work

The 2026-11-13 source check was against uncommitted work over backend `53bd75d` and frontend `57a0ab7`. The code already includes a payment simulator, development routes, a guarded test runner, incomplete commit/reaccept routes, removal of unsigned frontend callback signing, blocked live BRS writes, missing-guest rejection, rejection of unsupported multi-room writes, hotel-only totals in linkage, and failure on linkage errors. Reuse these changes and verify them rather than treating them as fully complete.

The previous isolated review passed 21 N06 backend tests and five focused frontend tests. No tests were rerun for this checklist. The historical 80 backend regressions, 111 frontend tests, build, and health-check claims require fresh evidence on the final implementation. A health endpoint does not prove a complete booking journey or blocked network traffic.

## N06-01 — Repair and prove the safe environment

**Files:** backend `scripts/run_n06_simulation.py`, `scripts/run_n06_tests.py`, `config.py`, `api/dependencies.py`; frontend startup configuration.

- [x] Register the launcher's generated Python audit hook before application imports. Add a startup self-check that invokes the policy through a synthetic audit event and proves denial before any real connection is attempted. A missing guard must stop startup.
- [ ] Start relevant services through an isolated launcher with an allowlisted environment. Prevent ordinary `.env` loading from reintroducing live credentials. Apply equivalent outbound restrictions to the website process and any child that can reach providers. A Python hook does not cover Node.js or browser subprocesses. Permit only the owned local simulator/database addresses and ports.
- [ ] Require explicit development/test simulation mode. Reject conflicting settings, live payment flags, public callback URLs, external redirects, and simulated evidence routed to live booking adapters. Keep production/staging simulation unavailable. Keep all actual live calls disabled.
- [ ] Disable external refresh jobs, SMTP, and other side effects. Verify local database ownership before writes or cleanup. Protect temporary secrets and stop only owned processes. Add safety preconditions to documented run/test commands.

**Pass result:** the application starts without live credentials; a safe denial probe fails before transport; one full local journey records only permitted local calls. Both the test runner and application launcher enforce the policy. Do not run a production request to test denial. Finish this task before full integration runs.

Evidence for the first box only: on 2026-11-13, `.venv\Scripts\python.exe scripts\run_n06_tests.py` passed 29 tests, including synthetic-denial and missing-guard startup checks. The database is prepared only after the guard self-check. The backend self-check also probes datagram sends and process creation. The launcher no longer instructs users to start an unguarded website or writes a frontend credentials file. The booking engine rejects test payments if either booking adapter permits live writes, the environment or run differs, or test provenance is missing. This does not verify website isolation, every conflicting setting, or the full journey; the other N06-01 boxes remain open.

Additional partial evidence: the same offline runner passed 65 tests on 2026-11-13. Startup and payment creation reject conflicting simulation settings. Payment creation also rejects simulation in staging or production. The tests use mocks and verify that rejected settings create no simulated payment request. The rest of N06-01 remains open.

## N06-02 — Enforce checkout ownership

**Files:** backend `api/routes/booking_intents.py`, `api/routes/simulation.py`, `api/routes/packages.py`, `api/routes/booking_operations.py`, `api/security.py`, `services/booking_intents.py`; frontend booking-intent/package routes, `app/api/dev/payments/[paymentId]/simulate/route.ts`, `lib/bookingBackend.ts`.

- [ ] Add or reuse a random checkout capability. Keep its browser value in an HttpOnly, SameSite cookie; use Secure on HTTPS. Store only its hash and scope it to intent/environment/run. Keep a narrowly defined HTTP localhost exception for local cookie testing.
- [ ] Enforce ownership on both website and backend boundaries for intent/payment reads, payment creation, simulation, commit, reaccept, action append, and recovery. A shared service API key, known ID, booking reference, or hidden button does not establish guest ownership. Keep staff recovery separately authorized.
- [ ] Protect cookie-authenticated writes with trusted Origin/CSRF checks. Add a run-scoped simulation credential that stays server-side. Require explicit dev/test enablement at both layers; reject unknown/staging profiles. Return safe, limited responses with `Cache-Control: no-store`, and rate limit abuse-prone operations.
- [ ] Reject malformed simulation JSON instead of defaulting to success. Derive amount, currency, source, and verification fields from saved server records. Audit legacy action/callback routes so they cannot fabricate paid or verified booking state.

**Pass result:** independent checkouts A and B cannot read, simulate, commit, reaccept, or recover each other's records. Missing/invalid credentials, wrong Origin, and production/staging simulation requests cause no state change.

Partial evidence (2026-11-13): both legacy HTTP payment callback paths return 503 before changing state; the custom callback service has no default signing secret. The isolated runner passed 64 tests, including N06, package, F14 operation, and M08 amendment regressions, with no blocked external connection attempts. Checkout ownership, all other protected routes, and the complete callback/provider contract remain unverified. All N06-02 boxes remain open.

## N06-03 — Bind payment to the exact accepted booking

**Files:** backend `api/schemas/booking_intents.py`, `api/schemas/packages.py`, `api/routes/booking_intents.py`, `services/booking_intents.py`, `services/package_pricing.py`, `services/booking_operations.py`.

- [ ] Reuse one persisted intent and booking reference from selection through payment and commit. Do not silently make a second identity at payment creation. Save immutable accepted offer content, canonical hash/version, terms/rules versions, consent time, guest details, room allocation, and round identities.
- [ ] Require the expected revision, accepted quote identity/hash, and terms identity on commit. Bind deposit purpose, request/transaction identity, amount, currency, source, environment, and test-run ID. Validate from durable records inside the engine as well as the route. Remove the optional-hash bypass and arbitrary caller quote selection.
- [ ] Reject a deposit for quote A used with quote B, a different intent, an altered amount/currency, or a different run/environment. Reject mismatched or missing test provenance. A `verified_paid` string alone must not authorize a write. Prevent the same payment transaction from funding multiple intents.
- [ ] Return an existing active payment attempt on repeated creation. An already paid intent must not get another payable attempt from a retry. Preserve prior acceptance/payment history. Unsupported booking shapes must be rejected before offering a payable path, or routed to staff before payment.

**Pass result:** each mismatch causes zero hotel/golf writes; repeated payment creation returns the same active attempt; the accepted guest/rooms/rounds used by commit are exactly those stored in the accepted snapshot.

Partial evidence (2026-11-13): the 64-test offline runner verifies the booking engine rejects mismatched quote ID, quote hash, amount, currency, purpose, and booking reference before availability or vendor writes. This does not establish a saved accepted snapshot, intent ownership, complete payment provenance, concurrent uniqueness, or the reaccept flow. All N06-03 boxes remain open.

## N06-04 — Make payment events durable and restart-safe

**Files:** backend `integrations/payments/simulation.py`, `api/routes/simulation.py`, `services/package_pricing.py`, `api/dependencies.py`.

- [ ] Require complete request/event/active-run identity matching. Reject a missing run ID when a run is required. Reject records from a previous run. Keep simulation envelope, signatures, and secrets separate from provider parsing. Preserve exact-raw-byte signature and timestamp checks.
- [ ] Make event identity, payload-conflict detection, monotonic payment state, and the booking-work marker durable. Use conditional writes and unique scoped identities. Avoid marking payment paid before its accepted event/work marker is safely recorded. If multiple documents are essential, use supported transactions or an explicit recoverable protocol; do not assume a standalone MongoDB transaction works.
- [ ] Process a repeated event idempotently. Reject the same event identity with different content. Prevent a delayed failure from overwriting paid evidence, including under concurrency. Do not overwrite the original payment transaction from a later ignored event.
- [ ] Save pending booking work before acknowledging an accepted payment. Add a bounded recoverable worker using existing patterns, so closing the browser or crashing after payment does not lose the booking task. The browser and simulator HTTP handler must not be the sole place that runs commit.

**Pass result:** duplicate/concurrent deliveries produce one payment fact and one booking operation. A crash between accepted payment and worker execution resumes from saved state. Database failure cannot acknowledge lost work.

Partial evidence: the 29-test offline run checks missing/different event run, a different stored payment run, request ID, source, test flag, event type/status and a non-test payment sharing a simulator ID. The simulator route rejects non-test and other-run records before event processing. An out-of-order failure does not replace the saved success transaction in the tested route case. This does not establish durable atomic event handling, concurrent replay protection, checkout ownership, or restart recovery. All N06-04 boxes remain open.

## N06-05 — Prevent duplicate bookings under concurrency

**Files:** backend `api/schemas/booking_operations.py`, `services/booking_operations.py`, `services/booking_intents.py`, `api/routes/booking_intents.py`.

- [ ] Add a database-enforced unique new-booking identity scoped to environment, intent, and operation type. Inspect legacy conflicts first and report them without deleting data. Use partial indexes where old amendment records lack new fields. A lookup followed by a separate insert is insufficient.
- [ ] Store command payload fingerprints and reject reuse of a key with different data. Use revisions and atomic worker claims for progress. Check matched update results; remove whole-document stale overwrites and the unused `server_info()` call in commit. Synchronize intent status through conditional updates rather than unconditional flags.
- [ ] Save a stable step identity and request hash before each provider write. Record started, unknown, saved-reference, independently-verified, and failed outcomes. Keep round keys stable across retries; do not use mutable list positions as their identity.
- [ ] Prevent an expired worker lease from permitting a blind replay. Resolve unknown writes through supported provider idempotency or authoritative lookup. If the result cannot be resolved, keep paid-unconfirmed and require staff review. Preserve amendment behavior with targeted regressions.

**Pass result:** simultaneous commit calls using different command keys, simultaneous retries of an incomplete operation, and worker restart produce one logical hotel stay and one record per required round. Conflict/update failures cannot overwrite newer progress.

Evidence: pending.

## N06-06 — Recheck the full offer and support renewed acceptance

**Files:** backend `services/booking_operations.py`, `services/package_pricing.py`, `api/routes/booking_intents.py`, corresponding schemas; local availability and pricing stand-ins.

- [ ] Fetch fresh test hotel and golf availability/prices just before writes. Compare dates, room quantities/categories, occupancy, rate plan, restrictions, course/time/holes/players, capacity, currency, exact itemized prices, and terms/rules. Cached tee slots or a log entry for rules drift are insufficient.
- [ ] On a changed or expired offer, preserve the deposit and set `awaiting_reacceptance` or an equivalent explicit state before writes. Return a server-generated refreshed offer with hash/version and safe next action. Never silently accept a substitute, extend expiry, or reprice.
- [ ] Make reaccept validate the current offer and terms; store consent and history. Check the conditional update result. Permit only before commit starts. Update the accepted snapshot on the same intent/operation. Reject a stale reaccept or a changed deposit requirement without an approved adjustment policy.
- [ ] Resume the original operation from the refreshed accepted offer. The existing unconditional reconciliation replay must not permanently block a valid pre-write reacceptance. After any write starts, use recovery rather than replacing its immutable request.

**Pass result:** independent price, rules, terms, room availability, golf capacity, and expiry changes all pause before writes. Only acceptance of the exact current offer can resume. No automatic extra charge or refund occurs.

Partial evidence (2026-11-13): the 64-test offline runner verifies that a changed rules version stops the new booking before vendor writes and reports staff review. Fresh itemized prices, an automatically refreshed offer, renewed consent, and resumption are not yet implemented or verified. All N06-06 boxes remain open.

## N06-07 — Independently read and verify every booking component

**Files:** backend `integrations/brs/booking_client.py`, `integrations/opera/client.py`, `services/booking_operations.py`, `services/booking_linkage.py`, `api/schemas/booking_linkage.py`.

- [ ] Make test adapters persist booking state and expose independent read/lookup operations that survive the worker restarts being tested. Create with verification false, then read saved state and compare every required field before setting verification true. Do not compare a request with its own constructed response.
- [ ] Verify the exact guest/stay/room details and every golf course/date/time/holes/player/rate/currency. Keep hotel subtotal separate from golf. Preserve current missing-guest rejection, unsupported multi-room blocking, and linkage-failure handling. Keep unsupported live BRS writes blocked.
- [ ] Persist and verify all hotel, confirmation, group/room-allocation, golf round, BRS booking, payment, and operation identifiers and cross-links. Scope records and evidence to the environment/run. Final confirmation requires durable matching evidence for every accepted component and complete linkage.

**Pass result:** a saved-state mismatch, missing component/ID, unavailable read, or linkage failure cannot confirm. Read and write call logs show that separate operations ran. A restart can find existing saved test reservations.

Evidence: pending.

## N06-08 — Complete safe new-booking recovery

**Files:** backend `services/booking_operations.py`, existing staff recovery routes/services and operation schemas.

- [ ] Recover directly from the original intent, payment, operation, and per-step references even if final linkage does not exist. Save a structured failure reason and actionable staff alert with known/unknown component outcomes. Preserve paid evidence.
- [ ] Re-read known bookings first. Resolve a timeout after write using supported lookup/idempotency, and execute only proven missing steps with the same stable keys. Keep unresolved outcomes manual. A read-only recovery command must perform zero booking, amendment, payment, or notification writes.
- [ ] Rebuild/verify linkage after all required components are verified. Keep the original operation identity and audit trail. Enforce recovery role/record scope using injected test identities; leave production staff identity gaps explicit.

**Pass result:** hotel success followed by golf failure, missing linkage, timeout after a saved write, and a second recovery all preserve one stay/round set and one deposit. Unknown unresolvable writes remain paid-unconfirmed with an allowed staff action.

Evidence: pending.

## N06-09 — Save completion independently of email delivery

**Files:** backend `services/booking_operations.py`, operation schema, existing durable-event/worker patterns.

- [ ] Remove inline email dispatch from new-booking commit. Atomically record final verified booking state and one durable confirmed-event marker only after all required readbacks and links. Keep actual confirmation delivery in N07 scope.
- [ ] Keep payment, booking, and notification states distinct. Repeated execution must not create a second event or change a failed notification into a failed verified reservation. Keep real email disabled throughout local work.

**Pass result:** an unavailable mail adapter produces zero send calls during N06. A verified booking and its single event survive process restart. Failed/incomplete booking state never creates a confirmed event.

Evidence: pending.

## N06-10 — Show truthful website states

**Files:** frontend `components/home/BespokePackage.tsx`, `app/(main)/booking/page.tsx`, `lib/packagesClient.ts`, relevant website API routes.

- [ ] Render booking status from durable backend booking state. Replace payment-only checks for confirmation headings, icons, success panels, and secured-room/golf claims. Preserve explicit simulation labels on test evidence.
- [ ] Display pending payment, paid/creating, changed-offer acceptance, paid/staff-review, and verified test-confirmed states. Fetch safe status after page refresh with bounded polling; a payment redirect or local UI state must not manufacture confirmation.
- [ ] Wire owned checkout requests, refreshed offer acceptance, and allowed next actions. Hide simulation controls when disabled and retain server enforcement. A guest retry reuses the same intent and operation.

**Pass result:** simulated paid plus golf failure shows staff review, including after refresh. Only a durable verified completion shows `Test booking confirmed`. A second session cannot see the first booking.

Evidence: pending.

## N06-11 — Verify the full local acceptance matrix

**Files:** backend `tests/test_n06_simulation.py`, `tests/test_n06_commit.py`, `tests/test_n06_recovery.py`, runner scripts; frontend `scripts/n06-simulation.test.cjs`; local stand-in artifacts as needed.

- [ ] Cover every row in detailed-plan section 12. Add real handler/HTTP tests for the new routes, including ownership and unauthorized state fabrication. Do not rely on source-text assertions or service-method tests alone.
- [ ] Exercise the real app against local stateful HTTP stand-ins only for changed dependency paths where serialization, timeouts, and retries matter. Reuse an available verified Postman simulation or a small local stand-in server. Record contract assumptions and actual requests; do not mock the application under test. Do not reproduce the providers' entire APIs.
- [ ] Run concurrent event/commit and crash/restart cases against an owned local MongoDB for unique-index/revision evidence. Label in-memory/mongomock tests separately. Test partial writes and failure at each persistence boundary. Use synthetic denial probes; confirm actual journey logs contain no external sends.
- [ ] Run affected backend regression suites through a verified isolated runner. The current runner only selects the three N06 modules; extend its safe selection for audited regressions rather than running broad discovery against `.env`. Run frontend tests, TypeScript, lint, build, and isolated OpenAPI checks. Report skipped/failed cases honestly.
- [ ] Let the user run the browser steps in detailed-plan section 13 after the safety checks pass. Record those as pending user review until performed. Agent browser testing requires the specified delegated model and browser tools under root instructions.

**Commands after safety is repaired:**

```powershell
# From rosapenna-backend, with dependencies already installed:
uv run --offline --frozen --no-sync python scripts/run_n06_tests.py
uv run --offline --frozen --no-sync python scripts/run_n06_simulation.py --check

# From rospaenna-frontend, inside the protected simulation environment:
npm test
npx --no-install tsc --noEmit
npm run lint
npm run build
```

**Pass result:** commands exit successfully on the final tree; each acceptance row has observed response/state/counts/log evidence, and zero live effects. A readiness response alone closes no workflow box. If the full journey cannot run without external services, local verification remains incomplete.

Evidence: pending.

## N06-12 — Finish evidence and scope reporting

- [ ] Update this checklist with actual commands, results, changed files, and limits. Update the detailed plan and affected developer instructions so safe commands match the implementation. Preserve the user's requested short README style; avoid unrelated documentation expansion.
- [ ] Review changed input, authorization, cookie/CSRF, rate-limit, signature/replay, outbound destination, logging, error response, dependency, and source-map controls. Map each applicable root security rule to server-side evidence. State not-applicable controls with reasons.
- [ ] After code changes, delegate vault code-evidence writing under the required model/settings. Review source links and status consistency. Do not mark simulation as production complete. No vault log is needed for writing this checklist alone.
- [ ] Give the user all changed-file links, test results, local URL, review steps, and remaining production blockers. Keep MMB disabled. Do not commit or push without confirmation. Do not start the next ticket without authorization.

**Pass result:** another agent can reproduce the safe journey from the documentation and tell which requirements are verified, blocked, or awaiting user review.

Evidence: pending.

## Production completion gates — remain unchecked during local work

These are release prerequisites, not permission to run live tests. Existing live credentials are protected; the missing requirement is verified capability/contract evidence and authorization, not an assumption that credentials do not exist. Keep development and test payments simulated permanently.

- [ ] **Prommt contract and payment evidence:** verify authoritative API/webhook schemas, event correlation, amount/currency semantics, signing, replay, and lookup behavior from official specifications and synthetic fixtures. Owner/input: integration owner and provider contract. Production execution needs separate written authorization; do not call the live account for this checklist.
- [ ] **OPERA capability:** confirm authorized Property API/version, field mapping, independent reads, timeout lookup/idempotency, and supported room topology. Owner/input: property integration owner and approved vendor contract. Keep unsupported room shapes blocked; do not invent a multi-room mapping.
- [ ] **BRS capability:** obtain authorized create/read/lookup and deduplication contracts for required courses/tenants. Owner/input: BRS/property integration owner. Keep live writes blocked until real capabilities are established.
- [ ] **Staff security and acceptance:** verify F06 production identity, role/record scope, and approved staff reconciliation procedure. Owner/input: security/property owner. Synthetic staff identities prove local controls only.
- [ ] **Release acceptance:** obtain the required acceptance evidence and explicit release authorization through a separately approved process. No production payment, reservation, email, deployment, or MMB activation is authorized by completion of local boxes. MMB has its own later activation gate.

After N06-01 through N06-12 pass, report **Partially Completed — local simulation verified**, with the production gates above listed. Only report **Completed** when the vault's production-ready definition and all ticket acceptance criteria are actually met through separately authorized work. The task of writing this checklist is complete; the ticket remains open.
