<!-- CEKURA-REPORT-START -->
# Cekura Quality Report — Field & Flower (flower-bot)

## 1. Header

| Field | Value |
|---|---|
| Result | **Smoke test v2 — post Daily-transport fix** (ID `591109`) |
| Agent | **Field & Flower (flower-bot)** (ID `18024`) |
| Project | `5878` (org Zolo) |
| Status | `completed` |
| Connection mode | **Pipecat Cloud WebRTC** (`pipecat-v2`) |
| Scenarios run | **3 of 7** generated (smoke-test subset) |
| Success rate | **0%** (0 / 3 fully successful) |
| Expected outcomes met | **1 / 3** |
| Date | 2026-05-30 |

> **Context:** this run is the **post-fix baseline**. The prior run (`591067`) had the agent **fully silent** (0 s audio, 0/3, pure infrastructure failure) because `bot()` didn't handle Pipecat Cloud's `DailySessionArguments`. After adding a `DailyTransport` branch + the `daily` dependency and redeploying (deployment `9488c490`), the agent now holds full multi-turn conversations. This report analyzes that recovered baseline.

---

## 2. Quick Summary of Issues

With the transport bug fixed, the agent performs **well on happy-path booking and out-of-scope deflection**, fully passing one of three scenarios. The remaining failures are **not** connection/config issues (Tool Call Success = 100%, no handshake errors) — they cluster into three model/prompt-level causes: (1) a **tool loop** on unavailable items where the agent repeatedly announces intent to "check availability" instead of relaying the sold-out result; (2) a **missed final step** — failing to read back the order confirmation number; and (3) **Nemotron model latency** producing >10 s response gaps.

| Issue category | Result | What's going wrong | Affected runs |
|---|---|---|---|
| Sold-out item not surfaced (tool loop) | ❌ (1 run) | Asked for Lily Elegance (sold out), the agent looped "let me check availability" **7×** and never stated it was sold out or offered an alternative; the flow never advanced. Model/prompt tool-use issue. | [3199434](https://dashboard.cekura.ai/5878/results/591109?call_id=3199434) Unavailable Bouquet Persistence |
| Missing order confirmation number | ❌ (1 run) | Agent completed the whole booking correctly but ended without reading back the `place_order` confirmation number. Prompt issue (final-step omission). | [3199433](https://dashboard.cekura.ai/5878/results/591109?call_id=3199433) Birthday Order Confirmation Flow |
| High latency / >10 s response gaps | ⚠️ (2 runs) | Nemotron responses up to **8.3 s** (p95 ≈ 6.9 s) tripped the ">10 s no-response" infrastructure check. Model/serving latency, not connectivity. | [3199434](https://dashboard.cekura.ai/5878/results/591109?call_id=3199434) Unavailable Bouquet · [3199435](https://dashboard.cekura.ai/5878/results/591109?call_id=3199435) Out-of-Scope Services |
| Over-confirmation (repetition) | ⚠️ (2 runs) | Agent re-confirmed delivery details 2–3× per call, padding the conversation. Minor prompt issue. | [3199433](https://dashboard.cekura.ai/5878/results/591109?call_id=3199433) Birthday · [3199435](https://dashboard.cekura.ai/5878/results/591109?call_id=3199435) Out-of-Scope |

---

## 3. Detailed Breakdown

### ❌ Sold-out item not surfaced — tool loop (1 run)

The agent fails the unavailable-item flow. When the caller asks for **Lily Elegance** (sold out today), the agent repeatedly announces intent to check availability but never relays a result, never states it's sold out, and never offers an in-stock alternative — so the caller's stated fallback (Rose Romance) is never reached. This is a model/prompt tool-use issue (a likely Nemotron tool-call loop), **not** a data or connection problem (Tool Call Success = 100%).

#### Run [3199434](https://dashboard.cekura.ai/5878/results/591109?call_id=3199434) — Unavailable Bouquet Persistence
- ❌ Expected Outcome **0/5** — evaluator: *"The agent never stated the bouquet was sold out or offered alternatives."*
- ❌ Unnecessary Repetition **1.5/5** — evaluator: *"Main Agent repeatedly states intent to check Lily Elegance Bouquet availability"* at **01:08, 01:34, 01:45, 01:53, 02:04, 02:23, 02:30** (7 repetitions in 10 turns, 70%).

### ❌ Missing order confirmation number (1 run)

The agent executed the full birthday-order flow correctly — occasion filtering, one-question-at-a-time capture, order confirmation — but ended the call without reading back the confirmation number from `place_order`. A single missing final step flips the expected outcome to a fail.

#### Run [3199433](https://dashboard.cekura.ai/5878/results/591109?call_id=3199433) — Birthday Order Confirmation Flow
- ✅ evaluator: *"Agent filtered by birthday occasion and presented five options"* (00:14); *"asked for recipient's name (01:26) then delivery address (01:37)"*; *"asked for the delivery date (02:05)"*; *"confirmed the full order including items and delivery details (02:35)."*
- ❌ evaluator: *"Agent did not provide a confirmation number, though they said goodbye"* (03:08).

### ⚠️ High latency / >10 s response gaps (2 runs)

Nemotron response latency is high — overall p95 ≈ 6.9 s, p99 ≈ 8.2 s, with single responses up to 8.27 s — enough to trip Cekura's ">10 s no agent response" infrastructure check. This is model/serving latency, not a connection failure (all calls connected; tools succeeded). It's intermittent: the Birthday run had **no** infra issue.

#### Run [3199434](https://dashboard.cekura.ai/5878/results/591109?call_id=3199434) — Unavailable Bouquet Persistence
- ⚠️ evaluator: *"Main agent didn't respond within 10 seconds after testing agent spoke at times: 00:11, 00:48, 01:14, 02:10."* Max single-response latency **8.27 s**.

#### Run [3199435](https://dashboard.cekura.ai/5878/results/591109?call_id=3199435) — Out-of-Scope Services & Pivot
- ⚠️ evaluator: *"Main agent didn't respond within 10 seconds … at times: 02:22."* (p95 ≈ 4.2 s on this run.)

### ⚠️ Over-confirmation / repetition (2 runs)

The agent re-confirms already-captured details multiple times, lengthening calls.

#### Run [3199433](https://dashboard.cekura.ai/5878/results/591109?call_id=3199433) — Birthday Order Confirmation Flow
- ⚠️ evaluator: *"re-confirmed the recipient name and delivery address for the second time"* (02:14); *"re-confirmed the bouquet details … recipient name and delivery address for the third time, and the delivery date for the second time"* (02:35).

#### Run [3199435](https://dashboard.cekura.ai/5878/results/591109?call_id=3199435) — Out-of-Scope Services & Pivot
- ⚠️ evaluator: *"confirmed the delivery address twice in the same turn"* (03:09); *"for the second time"* (03:46); *"for the third time … and the recipient name and delivery date for the second time"* (04:07).

### ✅ Passes — what validated cleanly

#### ✅ Out-of-scope deflection + complete booking (1 run)
- [3199435](https://dashboard.cekura.ai/5878/results/591109?call_id=3199435) **Out-of-Scope Services & Pivot** — Expected Outcome **5/5 (100%)**. Declined pizza (00:14) and weather (00:33), pivoted to flowers (00:49), listed birthday bouquets with prices in words (01:14), read back the full order (04:07), and provided a confirmation number + ETA (04:41). 0 interruptions, 0 self-interruptions.

#### ✅ Core booking mechanics (1 run)
- [3199433](https://dashboard.cekura.ai/5878/results/591109?call_id=3199433) **Birthday Order** — occasion filtering, one-question-at-a-time capture, and full order confirmation all worked; only the confirmation-number readback was missing. No infrastructure issues on this run.

---

## 4. Performance

| Metric | Value | Notes |
|---|---|---|
| Agent latency (mean / p50 / p95 / p99) | 3171 ms / ~2.7 s / **6.9 s** / 8.2 s | 🔴 p95 far above 2 s — primary perf issue (Nemotron) |
| Worst single response | **8.27 s** ([3199434](https://dashboard.cekura.ai/5878/results/591109?call_id=3199434)) | tripped the >10 s no-response check on 2/3 runs |
| Tool Call Success | **100%** (5.0/5) | all tool calls connected/succeeded |
| Interruption Score | 4.67/5 avg | [3199434](https://dashboard.cekura.ai/5878/results/591109?call_id=3199434) = 4.0 (2 interruptions) |
| AI interrupting user | 0.67 avg/call | [3199434](https://dashboard.cekura.ai/5878/results/591109?call_id=3199434): 2 (01:25, 02:21); others 0 |
| Talk-time ratio (agent share) | 0.81 avg (0.75–0.85) | 🟠 agent-dominant; trim read-backs |
| Unnecessary Repetition | 3.15/5 avg | 🔴 [3199434](https://dashboard.cekura.ai/5878/results/591109?call_id=3199434) = 1.5 (the loop); Birthday 4.09; Out-of-Scope 3.85 |
| Transcription Accuracy (test-agent STT WER) | 0% WER (5.0/5) | clean — measures the simulated caller's STT, not the bot |
| Average Pitch | 182 Hz avg | real audio confirmed (was 0 Hz pre-fix) |

**Outlier:** [3199434](https://dashboard.cekura.ai/5878/results/591109?call_id=3199434) Unavailable Bouquet is the worst run across latency (max 8.3 s), repetition (1.5), and interruptions (2).

---

## 5. What Works Well

- **Out-of-scope handling is excellent** — [3199435](https://dashboard.cekura.ai/5878/results/591109?call_id=3199435) cleanly declined pizza and weather and redirected to flowers without inventing capabilities.
- **End-to-end booking works** — both [3199435](https://dashboard.cekura.ai/5878/results/591109?call_id=3199435) and [3199433](https://dashboard.cekura.ai/5878/results/591109?call_id=3199433) captured occasion → recipient → address → date one step at a time and confirmed the order.
- **Prices spoken in words** as instructed (birthday bouquets listed with word-prices, 01:14 in [3199435](https://dashboard.cekura.ai/5878/results/591109?call_id=3199435)).
- **Tooling is solid** — 100% Tool Call Success; the internally-mocked catalog/order tools resolve correctly.
- **Clean audio + transcription** — 182 Hz pitch, 0% test-side WER, zero connection errors (the pre-fix silence is fully resolved).

---

## 6. Next Steps (ordered by impact)

1. **Fix the sold-out tool loop (highest impact).** Prompt addition: *"Call `check_availability`/`list_bouquets` at most once per item; once you have the result, state it. If an item is sold out, say so plainly and immediately offer one or two in-stock alternatives. Never re-announce intent to check."* This loop smells like Nemotron tool-call flakiness — worth A/B-testing the **GPT-4.1** variant (`bot-gpt.py`).
2. **Always read back the `place_order` confirmation number** before goodbye: *"After `place_order` succeeds, read the confirmation number and ETA aloud, then close."*
3. **Reduce latency.** Nemotron p95 ≈ 6.9 s is the main perf drag — tune the serving endpoint / cap response tokens, or evaluate GPT-4.1 for latency.
4. **Trim over-confirmation:** *"Confirm each detail once; do a single final read-back before placing the order."*

**Mock data:** not applicable — this Pipecat agent mocks all backend tools internally (`mock_backend.py`), so no Cekura hosted mock layer or seed records are needed.

**Re-running:** after applying fixes + redeploying, re-run only the failed scenarios with `results_rerun_create`, or re-run the 3-scenario smoke set. The other **4 scenarios** in the suite (Specials Filter, Sympathy Occasion, Mid-Order Change, Multi-Item Order) have **not** been executed yet — run the full 7 for complete coverage.

> **Coverage caveat:** this report covers the **3 smoke-tested scenarios** of the 7-scenario suite in folder `Auto-Generated - flower-bot v1`. Success rate is computed over those 3.
<!-- CEKURA-REPORT-END -->
