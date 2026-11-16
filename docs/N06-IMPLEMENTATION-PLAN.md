# N06 implementation plan: safe booking creation with simulated payments

Updated: 2026-11-13.

## Execution status and remaining work

N06 is **Partially Completed**. The current code contains simulation and booking changes. It does not yet meet the full simulation acceptance requirements. Use [the N06 completion checklist](N06-COMPLETION-CHECKLIST.md) to finish the existing work in order. This document remains the detailed design reference. The checklist records execution and evidence; it does not replace the ticket requirements.

Apply the checklist's **Keep the implementation small** rules. Reuse current services and dependencies. The detailed steps describe required behavior and evidence; they do not require new infrastructure, separate services, or a general workflow framework.

The 2026-11-13 source check still finds these gaps: the simulation launcher does not register its network guard; checkout ownership is missing; commit does not require exact payment/accepted-quote binding; operation creation is not atomic across concurrent requests; current prices are not fully compared; new-booking recovery depends on final linkage; BRS test readback compares an echoed result; the UI derives booking confirmation from payment status; and email remains inside the new-booking commit. Payment event persistence, run binding, and restart behavior also need the checklist's verification.

The previous isolated review ran 21 N06 backend tests and five focused frontend tests successfully. Those results are historical evidence for those cases only. They do not close the gaps above. No runtime tests were rerun for this plan update. Preserve existing uncommitted implementation and README changes. Production integrations, acceptance, and activation remain separate gates.

## 1. Purpose and authority

This is an execution guide for a smaller coding model. Follow the steps in order. Complete each check before the next step. Record an unresolved input instead of guessing. Recheck source paths before editing. Proposed names below are implementation decisions, not claims that code already exists.

The goal is to create one logical hotel stay and all accepted golf rounds after a verified deposit. Confirm the booking only after each required write, independent read, and local link is verified. Preserve the payment record if a later step fails.

The user requires all payment operations in development and test to use local simulation. A simulation is a local stand-in for an external service. No real money moves. No real Prommt account or dashboard is used. All hotel, golf, and notification effects in this development journey must also use test adapters. A simulated payment must never authorize a live reservation.

This document updates the earlier N06 plan. It preserves quote checks, guest acceptance, durable progress, duplicate protection, honest guest status, and recovery. It adds strict payment isolation and executable safety checks.

Source order:

1. Direct user instructions and applicable `AGENTS.md` files.
2. [N06 ticket](../../knowledge-vault/wiki/projects/rosapenna-booking/tickets/n06-paid-booking-creation.md), [F14 ticket](../../knowledge-vault/wiki/projects/rosapenna-booking/tickets/f14-safe-booking-operation-engine.md), [F11 ticket](../../knowledge-vault/wiki/projects/rosapenna-booking/tickets/f11-payment-links-and-verified-ledger.md), and their source requirements.
3. [Immutable booking scope](../../knowledge-vault/raw/Rosapenna-Updated-Build-and-Manage-My-Booking-Scope-v1.1.md).
4. Current code and verified provider contracts. Code comments and historical completion labels do not prove behavior.

The root [AGENTS.md](../../AGENTS.md) stores the user's definition of a detailed plan. Update this document when the user changes N06 scope. Log only actual code changes in the vault. Do not log this plan as implemented work.

## 2. Scope and completion boundary

Implement the N06 journey and the F11/F14 changes required to make that journey safe. Reuse the existing booking operation engine. Preserve amendment behavior with focused regression checks. Do not create a second booking engine.

Included: payment simulation, exact payment-to-intent binding, checkout ownership, accepted quote snapshots, pre-write checks, durable hotel and golf steps, complete linkage, recovery, safe status, and isolated tests.

Excluded: live payments, live bookings, guest email delivery, deployment, MMB activation, automatic refunds, automatic cancellations, new payment policies, balance collection, and reminder schedules. N07 owns confirmation delivery. N08 owns later automation. N08 can consume confirmed bookings from other channels; do not invent a dependency on N06.

The implementation may satisfy local acceptance checks. Under the vault's status rules, a verified simulation is **Partially Completed**. It is not **Completed** production work. Record missing authorized provider capabilities and production acceptance separately. Do not weaken these definitions to close the ticket.

## 3. Inspect before changes

From the workspace root, read root and nested instructions. Read the vault index before linked notes. Inspect both Git working trees. Preserve existing changes.

```powershell
git -C rosapenna-backend status --short
git -C rospaenna-frontend status --short
git -C rosapenna-backend log -1 --oneline
git -C rospaenna-frontend log -1 --oneline
```

Inspect these existing paths. Backend paths are relative to `rosapenna-backend/`; frontend paths are relative to `rospaenna-frontend/`.

| Area | Existing files | Required inspection |
| --- | --- | --- |
| Configuration | `rosapenna_backend/config.py`, `api/dependencies.py`, `app.py` | Startup order, environment loading, dependency factories, background jobs. Resolve shorthand paths relative to `rosapenna_backend/`. |
| Payment | `rosapenna_backend/integrations/prommt/client.py`, `integrations/prommt/schemas.py`, `services/package_pricing.py`, `api/routes/packages.py`, `api/schemas/packages.py` | Every create, link, details, callback, signature, expiry, and ledger path. |
| Intent and operation | `rosapenna_backend/services/booking_intents.py`, `services/booking_operations.py`, `api/routes/booking_intents.py`, `api/routes/booking_operations.py`, corresponding `api/schemas/` files | Ownership, revision checks, duplicate protection, recovery, saved evidence. |
| Vendor and linkage | `rosapenna_backend/integrations/opera/`, `integrations/brs/booking_client.py`, `services/booking_linkage.py`, `api/schemas/booking_linkage.py` | Actual writes, independent reads, lookup, room topology, safe financial totals. |
| Frontend | `app/api/packages/payment-callback/route.ts`, `components/home/BespokePackage.tsx`, `app/(main)/booking/page.tsx`, `lib/bookingBackend.ts`, `lib/packagesClient.ts`, existing booking-intent API routes | Browser ownership, server keys, status wording, simulator UI, error handling. |
| Tests | `tests/test_prommt.py`, `test_packages.py`, `test_booking_operations.py`, `test_booking_intents.py`, `test_brs_writes.py`, `test_opera.py`, `test_booking_linkage.py`; frontend `scripts/` | Imports that load `.env`, direct network clients, fake databases, current assertions. |

At initial plan preparation on 2026-11-12, both Git trees were clean. At the 2026-11-13 update, both contain uncommitted N06 and documentation changes. Preserve them. The original frontend callback signed unsigned input; that signing behavior has since been removed. Verify all remaining backend callback paths and their credentials independently. The payment client still has default `.env` loading and a live-call flag. Neither a source comment nor that flag alone proves safe test isolation.

Check: list the actual call path from guest selection to payment record to booking operation. Find every public route that can set payment, reservation, or verification state. A browser-submitted action must not become authoritative evidence.

## 4. Build the safety boundary first

### Environment contract

Use server configuration as authority. Reuse `BOOKING_ENVIRONMENT`. Add a validated `BOOKING_PAYMENT_MODE` with values `simulation` and `disabled`, default `disabled`, for this ticket. A live mode is not implemented or enabled by N06. Add `BOOKING_SIMULATION_ENABLED`, default false. An explicit development launcher sets it true. Keep these settings server-only. Do not put secrets in `NEXT_PUBLIC_*` variables.

| Environment | Payment mode | Simulator route | Allowed booking adapters |
| --- | --- | --- | --- |
| development | simulation, explicitly enabled; otherwise payment operations disabled | Protected local route only | Test OPERA, BRS, availability, and notification adapters |
| test | simulation, explicitly enabled; otherwise disabled | Test fixture or protected local route | Test adapters only |
| staging | disabled in this ticket | Absent or 404 | Booking commit disabled until separate verified integration work |
| production | disabled in this ticket | Absent or 404 | Simulated payment cannot authorize any write |

Reject unknown values and conflicting settings during startup. Reject simulation enabled in staging or production. Reject `PROMMT_ALLOW_LIVE_CALLS=true` in development/test before a client is constructed. A `PROMMT_MODE=TEST` value alone does not make a live account safe. A frontend `NODE_ENV` value alone does not authorize simulation. A local Next.js production build may still use an explicit application development profile.

Do not change production account settings, stored credentials, or unrelated working deployments. Report deployment impact before release. No deployment is part of this work.

### Independent protections

1. Add an injected payment adapter. In simulation mode, construct the simulator directly. Do not construct a live client and hope its boolean prevents calls. In disabled mode, reject payment operations with a safe unavailable response.
2. Add a payment transport guard to any retained real client path. Recheck policy at the send boundary. Reject real outbound payment calls for this development/test profile, even if a direct method or legacy route bypasses the dependency factory.
3. Generate an isolated child-process environment for development and tests. Allow only required OS, path, and local application settings. Do not inherit live `PROMMT_*`, OPERA/BRS secrets, SMTP credentials, cloud database URLs, or public webhook destinations. Disable all relevant settings classes' default `.env` reads in this profile, including import-time singletons.
4. Use an outbound network deny policy for the test process and any child process that can send requests. Permit only explicit local simulator and isolated database addresses/ports. The policy must cover actual transports, not just one mocked `fetch`. Test denied destinations through a fake socket/transport; do not send a request to prove denial.
5. Use a local simulator URL, such as a relative website path. No simulated link points to `pay.prommt.com`, `gw.prommt.com`, `app.prommt.com`, or another public provider host. Reject redirects that leave the local allowlist. Use only fixed server-side destinations.
6. Disable background tee-sheet refresh, vendor session jobs, SMTP, and reminder dispatch in the simulation launcher before application startup. No startup hook may reach real services.

Use one-port localhost simulation instead of a proxy that can forward unmatched traffic. Verify each configured dependency points to the local route. If using Postman simulation, read its installed CLI help, member schema, and context schema. If the actual CLI is missing, report the limit and use a local HTTP stand-in with the same safety checks. The discovered `postman.exe` is a Windows app alias; it is not evidence that the required CLI or simulation feature is installed. Do not launch the GUI as a CLI test.

Check: incompatible configuration fails before network initialization. The full dev/test journey works without live credentials. A test of the blocked transport observes zero external sends. A real payment record cannot be consumed in simulation mode, and a simulated record cannot be consumed in production mode.

## 5. Test records and payment evidence

### Generate only synthetic data

Create a fresh `simulation_run_id` for each run. Generate random service, checkout, and simulation signing secrets in the launcher. Keep them in child-process memory or restricted temporary local files. Exclude these files from Git and logs. Do not use committed example passwords or default signing keys. A worker restart within the same owned run retains the run identity and keys through the launcher. A new run must not consume the previous run's records.

Use synthetic names, `example.invalid` email addresses, and validated synthetic contact fixtures. Use isolated local MongoDB or in-memory test data. Add `environment`, `evidence_source`, `simulation_run_id`, and `is_test` to relevant records and vendor references. Use a distinct prefix for simulator IDs. The server sets these fields from the injected adapter; ignore client claims about their origin.

A development database must be explicitly local, dedicated, and owned by this test run. Use a unique database name and an ownership marker. If a namespace is nonempty or unowned, stop. Cleanup may remove only the marked run namespace after the name and local destination checks pass. Do not delete shared databases or historical records.

### Required payment fields

Bind each payment attempt to: payment ID, booking intent ID, booking reference, accepted quote version and hash, purpose (`deposit`), exact expected amount, currency, provider/simulator request ID, environment, source, and run ID. Store amounts as exact decimal strings or minor units with explicit currency scale. Do not compare floating-point values.

For request creation, use a unique active attempt identity scoped to environment, intent, deposit purpose, and accepted quote version. Repeated creation returns the existing attempt/link. If the intent already has a verified deposit, return that state instead of creating another payable attempt. Do not automatically invalidate a paid attempt when reaccepting an offer. Hash a documented canonical serialization of accepted facts, including normalized decimal strings and stable room/round keys; a rules version alone is not a quote content hash.

A verified event must match every required binding. If an identifier or amount conflicts, do not mark paid. A valid signature proves the sender only; it does not prove that the event belongs to this intent or amount. This follows the [OWASP payment integration guidance](https://cheatsheetseries.owasp.org/cheatsheets/Third_Party_Payment_Gateway_Integration_Cheat_Sheet.html).

Keep payment evidence separate from booking completion. In simulation, record `evidence_source=simulation` and expose an explicit simulated payment label. If reusing `verified_paid`, require source/environment checks at every consumer. That value alone cannot authorize writes.

Use a unique event identity scoped by source and environment. Store a payload hash. A repeated event with the same identity and hash returns the existing result. A repeated identity with different content returns a conflict and causes no write. A transaction/request identity cannot pay two intents. Use conditional state transitions. Do not regress a settled payment to failed when a delayed failure arrives. A refund fixture must not delete deposit evidence or silently trigger a new charge.

Persist the accepted event and durable booking work marker together in one document update where practical. Acknowledge only after durable storage. Workers retry failed work markers. A crash after event receipt must not lose the booking task. Never depend on the browser staying open.

Check: wrong purpose, intent, source, environment, request ID, amount, currency, signature, and replay evidence each fail safely. Cache contents cannot override durable evidence or bypass isolation.

## 6. Simulator operations and contracts

The simulator replaces the external service. The real application, validation, ledger, intent store, booking engine, and HTTP serialization still run. An HTTP stand-in is needed to test transport behavior. Unit fixtures alone do not prove HTTP wiring.

| Operation to implement or generate | Safe behavior and result |
| --- | --- |
| Create payment request | Use accepted server quote and guest consent. Generate one stable request for the same attempt. Return pending, exact amount/currency, local URL, and test source. No network to Prommt. |
| Get payment link/details/status | Read simulator-owned records. Return only checkout-scoped data. Unknown IDs do not reveal other records. No fallback to a provider URL or live details request. |
| Select a simulation result | Accept an allowlisted scenario, not arbitrary payment facts. Scenarios include success, failure, cancellation, pending, expiry, delayed success, repeated delivery, and timeout. |
| Generate and deliver a local event | Derive payment fields from the saved request. Use a stable event ID for replay. Send only to the configured local application callback. Sign exact local raw bytes with a fresh simulation-only key. |
| Signature and correlation failures | Generate altered raw bytes, bad signature, stale timestamp, wrong currency/amount/request/intent, and reused event identity in a private test fixture. These inputs are not ordinary guest controls. |
| Ambiguous write timeout | Save the stand-in's result before dropping/delaying the response. A recovery lookup finds the existing result. Also test a timeout before any write. |
| Refund or charge retry requests | Reject unsupported financial actions. A booking retry does not generate a second payment. |
| Reset | Remove only the owned run data. Keep a sanitized test report. Never expose a guest reset endpoint. |

Use a simulation event envelope with `schema_version`, `event_id`, `event_type`, `request_id`, `payment_id`, `intent_id`, `purpose`, `amount`, `currency`, `occurred_at`, and run identity. Reject unknown fields where appropriate. Set bounded body size and timestamp tolerance. Use constant-time signature comparison over the exact raw bytes, before parsing trusted fields. Resolve the expected key from server configuration, not an event field.

This local envelope is not the Prommt contract. Keep its verifier and credentials separate. Verify retained provider parsing only against authoritative saved Prommt specifications and synthetic fixtures. Do not describe the current custom colon-separated callback signature as an official Prommt signature. If provider details are missing, leave that production integration blocked.

### Proposed routes

Finalize these routes in schemas and OpenAPI before implementation. Reuse existing route modules and avoid competing paths.

| Route | Caller and protection | Contract |
| --- | --- | --- |
| Existing package payment creation/status routes | Website server plus scoped checkout credential | Add intent binding and source fields. Derive money server-side. Preserve compatible callers. |
| `POST /dev/payments/{payment_id}/simulate` | Local website server/test runner; dev/test policy, service credential, checkout ownership, run-bound simulation credential, rate limit | Body contains allowlisted scenario and command ID. Derive payment facts on server. Return accepted task/current state. |
| Proposed website `POST /api/dev/payments/{paymentId}/simulate` | Owned checkout session; strict Origin/CSRF check; server application profile | Forward only permitted scenario. Keep service/simulation secrets on server. Hidden controls alone are insufficient. |
| Separate local event receiver | Private local simulator; dev/test policy and raw-body signature/replay checks | Apply only simulation events to simulation records. Define its exact path after checking current package callback wiring. |
| `POST /booking-intents/{intent_id}/commit` | Authorized checkout or internal worker | Body includes expected intent revision, accepted quote identity/hash, stable command key. Payment ID comes from saved intent. Return 202 for pending work, 200 for an existing terminal result, 409 for consent/revision conflict. |
| `POST /booking-intents/{intent_id}/reaccept-offer` | Authorized checkout, CSRF/Origin checks | Accept exact server-issued offer hash/version, terms version, expected revision, and explicit consent. Reject stale offers. Never accept client prices as authority. |
| Existing intent GET | Authorized checkout | Return allowlisted state, revision, safe summary, test source, and allowed next action. Add `Cache-Control: no-store`. |
| Existing operation/recovery routes | Scoped server/staff authority | Require access to this intent/operation. Browser use of a service key alone is not ownership. Audit retries on the original operation. |

Return 400 for malformed input, 401/403 for missing/invalid authorization as appropriate, 404 for unavailable simulation routes and non-owned object lookups, 409 for conflicts, 422 for schema errors, 429 for rate limits, and safe 503 for disabled capability. Do not return raw exception text or full internal operation documents to guests.

Remove unsigned-message signing from `app/api/packages/payment-callback/route.ts`. Do not keep it as a compatibility bypass. Reject or retire unsafe legacy simulation paths. The production webhook path must never accept browser-generated or simulation evidence. Keep it disabled in this development deliverable if the verified provider boundary is unavailable.

Check: production/staging cannot reach simulation behavior even with valid test credentials. A second checkout cannot read, simulate, commit, append verification actions, or recover the first checkout's records. A request cannot set `payment_verified`, `readback_verified`, source, or final status directly.

## 7. Checkout ownership and accepted intent

Use a random checkout capability held in a server-issued HttpOnly, SameSite cookie. Use Secure cookies on HTTPS. The isolated HTTP localhost profile may use a narrowly defined non-Secure cookie so local tests work; never carry that exception to deployment. Keep tokens out of local storage, query strings, response bodies, and logs.

Validate ownership at the website route and backend object boundary. Forward a server-side checkout credential to the backend in a protected header, alongside service authentication. Store only its hash and scope it to the intent/run. A known intent ID or booking reference is not authorization. Use explicit request/response allowlists and scoped database filters. Protect cookie-authenticated writes with strict trusted Origin checks and an appropriate CSRF mechanism. Rate limit create, simulate, commit, status polling, and recovery.

Persist one canonical intent and booking reference. Reuse an existing valid intent ID; do not silently generate another during payment creation. Its immutable accepted snapshot contains all guest/contact inputs, room quantities, occupants, dates, rates, rounds, player counts, itemized money, consent time, terms/rules versions, source, and quote hash. Preserve rejected and superseded versions as history.

The current quote TTL and longer payment-link expiry are different. A later paid event keeps its payment evidence. Before writes, obtain a fresh valid offer. If price, inventory, or accepted terms changed, pause for explicit renewed acceptance. Do not extend quote expiry silently. Do not charge the difference automatically. If the new deposit no longer matches the existing payment and no approved adjustment policy exists, require staff review.

Check: consent is tied to exact server terms and offer content. Changing accepted facts after commit begins is rejected. Reacceptance before writes updates a versioned snapshot on the same intent and operation.

## 8. Durable booking execution

### State and duplicate protection

Keep distinct payment, booking, and notification states. Add or map booking states to: `awaiting_payment`, `ready_to_commit`, `awaiting_reacceptance`, `committing`, `paid_unconfirmed`, and `confirmed`. Map existing `unconfirmed_reconciliation` consistently. Do not break amendment schemas by replacing shared enums without migration.

Use a unique key for `(environment, intent_id, operation_type=new_booking)`, not only a caller's idempotency key. An idempotency key means repeating the same command returns the same result. Store its payload fingerprint; reject reuse with different data. Add unique source/event and stable component keys. Validate existing documents for duplicates before adding indexes. Never auto-delete conflicting records. Use partial indexes when legacy amendment records lack new fields.

Use a revision counter and conditional updates with expected state/revision. Check matched results. Do not overwrite an entire stale operation record. A lease can allow one worker to advance, but lease expiry is not proof that an external write failed. Database atomic writes do not make external providers one transaction. See [MongoDB atomicity guidance](https://www.mongodb.com/docs/manual/core/write-operations-atomicity/).

Persist a per-component step before each network write: stable component key, request hash, external idempotency/reference key where supported, state, attempts, timestamps, response reference, and independent read evidence. Use `planned`, `write_started`, `write_unknown`, `reference_saved`, `verified`, and explicit failure states or equivalent existing states.

### Ordered worker flow

1. Load the intent, payment, accepted snapshot, and canonical operation through scoped queries. Validate schemas and environment/source/run binding. Acquire the revision/lease claim. If durable storage fails, make no vendor call.
2. Verify deposit evidence and consent. In this profile, allow only simulation payment plus test vendor adapters. If payment is pending/failed, return the correct state and stop.
3. Recheck all accepted rooms, quantities, dates, category, occupancy, rate plan, restrictions, and price. Recheck every course, date, time, holes, player count, rate, and capacity. Use fresh simulator data in development. The future live adapter must do authorized live reads. Do not count unrelated `RESERVED_ONLINE` slots as free.
4. Compare exact itemized totals, currency, policies, and inventory with the accepted offer. A changed/unavailable offer sets `awaiting_reacceptance` before any write. If a provider later rejects due to a race, retain payment and set a visible exception.
5. Persist the hotel step. Create the exact accepted logical stay with real validated guest inputs from the intent. Use hotel subtotal only; do not include golf in the hotel amount. Remove placeholder guest/rate/phone fallbacks. Validate multi-room topology; unsupported shapes fail before writes rather than forcing quantity one.
6. Save the hotel result reference. Independently read the stored reservation and compare all required fields. The simulator must store state and implement a separate read request. A boolean or request echo is not independent evidence.
7. For each accepted golf round, persist its stable step. Use an immutable round key, not its position in a mutable list. Write exact tenant/course/date/time/holes/players/rate. Save both round and booking IDs. Read it independently and compare. Do not replay already verified steps.
8. Persist and verify complete linkage: intent, operation, payment evidence, hotel IDs, room/group allocation, every golf ID, correct room/golf/total amounts, and evidence source. A linkage failure blocks confirmation. Remove catch-and-continue success behavior.
9. After all evidence is present, atomically set final `confirmed` and a stable durable confirmed-event marker in the operation document. Emit an event for future N07 work. No inline guest email or real dispatch runs in N06. Repeated execution returns the saved verified outcome.

Use bounded timeouts on every external call. Retry safe reads within limits. Do not retry an ambiguous write unless the provider's supported idempotency or authoritative lookup resolves its outcome. Do not hold database transactions or locks across network calls. Reuse clients and indexes. Poll with bounded delays. Do not add a queue framework unless existing durable worker patterns cannot meet the requirement.

The inspected BRS live branch constructs a successful object locally. Replace that behavior with an explicit unsupported/blocked result until authorized create/read/lookup contracts exist. Do not invent BRS endpoints or claim live readback from a local echo. Reuse the OPERA adapter, but verify the exact Property API/version and multi-room mapping before future live activation.

Check: every requested component has a durable reference and independent matching read before final confirmation. A second submit, new caller key, duplicate event, process restart, or worker race creates no second logical stay or round in tested scenarios. If this guarantee cannot be established for a vendor, keep its write capability disabled.

## 9. Failure, recovery, and guest behavior

Before any vendor step fails, retain the accepted intent and payment evidence. After a partial or unknown write, set `paid_unconfirmed`. Save a structured reason code, known references, unknown outcomes, and the allowed next action. Persist an actionable staff alert in the same recovery state. Delivery to staff tools may retry; guest booking status remains truthful. No guest email is needed to prove the alert exists.

Recovery must work before final linkage exists. Load the original intent/operation rather than require a completed linked booking. A retry uses the original payment and component keys. Read known references first. An unknown hotel or round write requires supported idempotency/lookup or manual review. Never issue a blind create because a lease expired or the previous process crashed.

A read-only recovery action performs reads only. It must not call an amendment method that can write. Production staff actions remain gated by verified identity, role, and record scope. Shared staff keys are not proof of individual authority. Test this boundary with injected test identities; do not claim F06 production identity is resolved.

Use these website messages, with a visible test-mode label during simulation:

- Pending: `Test payment is pending.`
- Simulated paid, creating: `Test payment received. We are creating your test booking.`
- Changed offer: `The offer changed. Review it before we continue.`
- Exception: `Test payment received. The booking needs staff review.`
- Complete: `Test booking confirmed.` Only show this from verified backend completion.

Separate test source from ordinary payment state in types and UI. Refreshing the page or returning from a payment URL only reads saved status. It does not mark paid or confirmed. Hide simulation controls when disabled, and enforce the same restriction on the server. Keep honest payment and reservation wording on every affected page, including `BespokePackage.tsx`.

## 10. Files to change or add

Change only files that the verified call path needs. These names are a bounded starting map, not a requirement to create unnecessary modules.

| Files | Intended work |
| --- | --- |
| Existing backend configuration, dependencies, app startup | Add validated simulation policy, injected adapters, safe env loading, disabled side-effect jobs, and fail-closed setup. |
| Existing Prommt client and schemas | Guard retained real transport. Keep authoritative provider parsing separate from simulation. Remove real-looking mock links. |
| Existing package pricing/routes/schemas | Bind exact quote/intent/payment, safe callback state changes, source-aware ledger, protected simulator entry, durable work marker. |
| Existing intent and operation services/routes/schemas | Add checkout ownership, immutable snapshots, canonical operation key, conditional updates, per-step progress, safe recovery, and response allowlists. |
| Existing OPERA/BRS and linkage code | Stateful test adapters with independent reads, correct quantities/totals/IDs, explicit blocked live BRS capability, required linkage. |
| Existing frontend files in section 3 | Remove automatic callback signing. Add protected simulation action, test labels, scoped requests, honest final status, and offer acceptance. |
| Proposed `rosapenna_backend/integrations/payments/simulation.py` | Payment simulator adapter and local event generation. Keep local envelope separate from provider schemas. Use existing module location instead if it gives a clearer small change. |
| Proposed `scripts/run_n06_simulation.py` | Safe launcher, explicit settings injection, fresh run data/secrets, local dependency wiring, network guard, and scoped teardown. |
| Proposed `scripts/run_n06_tests.py` | Install safety policy before application imports or test discovery. Run only audited offline tests. Fail on external network attempts. |
| Proposed `tests/test_n06_simulation.py`, `tests/test_n06_commit.py`, `tests/test_n06_recovery.py` | Boundary, workflow, concurrency, independent read, and recovery tests described below. |
| Proposed frontend `scripts/n06-simulation.test.cjs` | Server route behavior and status checks. Exercise handlers/HTTP where practical; source-text matching alone does not prove authorization. |
| Proposed local HTTP stand-in artifacts | Reuse `postman/simulations/` and mocks if available. Record contracts, limitations, failure modes, and exercised calls in context files. Otherwise use a small local test server and report that choice. |
| This plan and root `AGENTS.md` | Keep instructions consistent with the final implemented commands and boundaries. |

Read Next.js docs under `node_modules/next/dist/docs/` before frontend code. Load the React best practices skill for React changes. Use pinned existing dependencies. Do not add a new framework to implement a small simulator.

## 11. Build order and exit checks

1. **Safety configuration and launcher.** Add the isolated settings factories, adapter selection, network policy, and import-safe runner. Check: forbidden live flags, inherited secrets, public URLs, redirects, or wrong environments cannot send traffic. Stop all payment work until this passes.
2. **Simulator and evidence.** Add request/status/scenario/event operations, fresh secrets, strict raw-body verification, correlation, and durable idempotency. Check: repeated success creates one payment fact; conflicting identity, amount, currency, or environment changes none.
3. **Checkout and snapshot.** Add ownership enforcement to every reachable mutation/read/action route and accepted content snapshots. Check: two independent checkout sessions cannot affect each other. Client actions cannot fabricate verification.
4. **Canonical operation and persistence.** Add safe indexes, revisions, work markers, and component steps. Check: parallel submit and crash/restart use the same operation. Duplicate legacy data is reported without destructive migration.
5. **Revalidation and test writes.** Implement complete room/round comparison, hotel/golf test adapters, independent reads, and strict linkage. Check: quantity, price, expired offer, unavailable inventory, and mismatched readback cannot confirm.
6. **Recovery and UI.** Resume from original intent, preserve payment, surface alerts, remove unsafe callback path, and add truthful simulated status. Check: each failure state has a safe next action; a retry does not charge or book again.
7. **Verification and documentation.** Run the matrix and appropriate regressions under the safety runner. Update OpenAPI examples and actual launcher instructions. Ask the user to review the isolated browser journey. Update vault code evidence through the specified delegate only after code changes. Check: each claim links to an actual command/result or remains explicitly unverified.

## 12. Required test matrix

For each case, inspect the HTTP response, saved payment/event/intent/operation, stand-in request log, hotel/round counts, linkage, and alerts as applicable. Assert external send count is zero for every run.

| Cases | Required observation |
| --- | --- |
| Valid deposit; one room/round; multiple accepted rooms/rounds | Exact quantities/prices and independent reads; one final test booking and complete IDs. Unsupported topology blocks before writes. |
| Failed, cancelled, expired, or pending payment | No hotel/golf write; honest status. Delayed valid success preserves evidence and runs fresh offer checks. |
| Browser redirect, unsigned callback, fabricated intent action | No verified payment or booking from browser claims. |
| Bad signature, changed raw bytes, stale timestamp, wrong request/intent/purpose/amount/currency | Rejected event; unchanged ledger and zero booking writes. |
| Same event twice; same event ID with different content; out-of-order failure after success | One payment fact; conflict on changed payload; paid evidence retained. |
| Simulation event against real record; real record in simulation; production receives simulation | No state change and no vendor write. |
| Two checkout sessions, missing service/simulation credential, CSRF/Origin failure, rate limit | Denied at server boundary. No record details leaked. |
| New caller key for same intent; concurrent workers; duplicate payment deliveries | One canonical operation, one logical hotel stay, one record per round. |
| Price/rules/terms change, expired quote, room quantity shortage, golf slot taken | Paused fresh offer; no silent acceptance or substitute. Preserve payment. |
| Reaccept current offer; stale/altered reaccept; deposit no longer matches | Only exact current version can proceed; unresolved financial difference requires staff review. |
| Hotel rejection; timeout before write; timeout after write | Visible exception. Recover known result without duplicate. Unknown unresolvable result requires manual action. |
| Golf fails after hotel; later round fails; independent read mismatch/unavailable | Paid-unconfirmed with all known IDs; no false confirmation. |
| Database fails before write; after vendor result; during linkage; during final event persistence | No write if progress cannot first persist. Later uncertainty retains recovery state; linkage/final persistence failure cannot confirm. |
| Crash after accepted event, hotel write, saved reference, or one round | Durable pending work survives. Recovery uses same component identities and finds existing effects. |
| Recovery before linkage; read-only recovery; repeated confirmed retry | Recovery is possible without final linkage; read-only issues zero writes; confirmed retry does not repeat effects/events. |
| SMTP/notification unavailable | No live send. N06 final state depends on verified booking, not inline mail. Durable confirmed event remains available to N07. |
| Wrong configuration, public host, redirect, inherited live flag, malicious endpoint override | Failure before transport. Denial test uses captured fake transport, never an actual production request. |
| Process restart with owned local database; index uniqueness and revision races | Results persist and conditional writes hold. In-memory/mongomock evidence is labeled separately from local Mongo evidence. |

Use targeted regression tests for changed F11/F14/N05 and shared amendment behavior. Audit imports before running old tests. A comment that says offline is not proof that `.env` or external traffic is blocked.

## 13. Commands to implement and verification steps

The two runner scripts below now exist. The test runner installs its audit guard, but the simulation launcher still lacks guard registration at this source check. Complete checklist task N06-01 before running the full simulation launcher. Do not replace the safe runner with broad test discovery against the current `.env`.

From `rosapenna-backend/`, after safe runners exist:

```powershell
uv run --offline --frozen --no-sync python scripts/run_n06_tests.py
uv run --offline --frozen --no-sync python scripts/run_n06_simulation.py
```

The test runner must return nonzero for any denied network attempt, failed assertion, or unsafe setup. It must summarize executed/skipped cases. The simulation launcher must bind local services to `127.0.0.1`, choose/check ports, print sanitized local URLs and run identity, and show `SIMULATION - NO REAL PAYMENT OR BOOKING`. It must not print credentials. Stop only processes it started. A readiness failure must stop the journey.

Proposed backend local port: 4040, checked before use. Existing frontend development port: 3030. Select another port explicitly if occupied; do not attach to an unknown existing service. Wire the website backend origin to the owned local service.

From `rospaenna-frontend/`, in the isolated simulation child environment:

```powershell
npm run lint
npx --no-install tsc --noEmit
node --test scripts/n06-simulation.test.cjs
npm run build
```

Run existing affected tests once the safety policy is in place. If dependencies need installation, use lockfile-preserving commands in a separate setup stage. Do not silently download tools during an offline test run. Report dependency advisory checks separately from offline behavior checks. Do not claim the full suite passes from selected tests.

Generate the actual OpenAPI document from the isolated app and inspect affected request/response/security declarations. Check local `/docs` at the launcher's printed backend URL with simulation dependencies only. Reuse existing collections if available. Add runnable requests and negative cases if this helps review; do not publish a Postman workspace, webhook URL, or simulation without applicable authorization. Run `postman status` only with a verified CLI, and report unavailable tooling accurately.

### User browser review after implementation

The user performs the browser review unless they explicitly ask for an agent. Follow the root instructions for delegated browser work if asked.

1. Start the safe launcher after all boundary checks pass. Use its printed local website URL, normally `http://127.0.0.1:3030`. Confirm the visible simulation label. Confirm external services are blocked.
2. Select a synthetic package and valid synthetic guest details. Accept the displayed terms. Create a test payment link. Confirm it remains local and states that no real payment occurs.
3. Choose simulated success. Expect creating status, then `Test booking confirmed` only after all independent reads and linkage. Compare test hotel/round IDs and exact quantities with the accepted quote.
4. Refresh and repeat submit. Expect the same booking/reference and unchanged hotel/round counts.
5. Create a separate test intent. Choose failed/pending payment. Expect no hotel/golf records.
6. Use the private scenario runner to change price/inventory before commit. Expect the changed offer and a new acceptance action. Expect no automatic repricing acceptance.
7. Inject a golf timeout after the hotel step. Expect staff-review status and preserved payment/hotel evidence. Recover the original operation. Expect no duplicate test stay, round, or payment.
8. Open an independent browser session. Confirm it cannot access the first session's intent or payment. Use automated handler tests for production route denial; do not deploy to check it.

A build or simulated browser run does not prove live Prommt, OPERA, or BRS behavior.

## 14. Security review and final evidence

Apply the root security checklist to changed boundaries. Verify NoSQL query values and record scope; do not accept user-supplied query operators. Use framework text escaping. Do not introduce HTML rendering for guest/provider values. Validate redirect targets, body size, identifiers, enum values, decimal amounts, and input limits. Keep CORS explicit. Configure trusted proxies before using forwarded identity for rate limits. Keep secrets out of response bodies, URLs, bundles, source maps, and logs. Return safe errors with correlation IDs. Log minimal state/reference metadata, not guest payloads, signatures, tokens, or card data.

Passwords, uploads, SQL, and RLS require no new infrastructure if they are not part of the changed architecture. Mark them not applicable with a reason. Do not claim a checklist item passed without server-side evidence.

Before reporting implementation done, provide:

- Every added, changed, or deleted file and its purpose.
- Commands run, actual counts/results, failures/skips, and safe local URLs.
- Matrix evidence for zero live traffic, ownership, source separation, duplicate protection, independent reads, strict linkage, and recovery.
- Remaining production gates: authoritative Prommt contract/evidence and authorized future activation; actual BRS create/read/lookup capability; exact OPERA Property API permissions and supported room topology; verified staff authorization and acceptance.
- Correct ticket status. N06 is currently **Partially Completed**, with local implementation gaps still open. Even after all simulation tasks pass, it remains **Partially Completed** until the production gates are met. Planning checkboxes are not test evidence.
- User review steps and the next eligible task. Keep MMB disabled. Commit/push only after confirmation and through the requested delegate. Do not start another ticket without authorization.

If an input or provider capability is missing, preserve completed local work and name the exact blocker and owner. A simulated success cannot replace missing production evidence.
