# Multi-agent fault-injection test cases

**10 / 10 passed.** Every case runs the full agent loop (Fault Detection → Planner → Navigation → Safety → Execution → Liaison) with zero human interventions unless the case scripts an operator message.

| Case | Result | Incidents | ⚡ safe plan (avg) | Msgs / incident | Tool calls | On-time (agents vs FCFS) |
|---|---|---|---|---|---|---|
| 3 AM breakdown — tractor B takes over | ✅ PASS | 3 | 17.8 ms | 7.7 | 22 | 6 vs 4 |
| Only sprayer tractor breaks mid-spray | ✅ PASS | 2 | 16.7 ms | 10.5 | 16 | 6 vs 4 |
| Bridge closes under moving tractors | ✅ PASS | 2 | 21.4 ms | 10.5 | 17 | 10 vs 8 |
| Wind gust during spraying | ✅ PASS | 2 | 19.7 ms | 11.0 | 16 | 9 vs 6 |
| Operator rushes a job in plain English | ✅ PASS | 3 | 18.0 ms | 8.3 | 23 | 10 vs 8 |
| Two tractors fail within 90 minutes | ✅ PASS | 4 | 8.9 ms | 7.2 | 20 | 4 vs 3 |
| Same fault twice → learned reflex | ✅ PASS | 4 | 10.4 ms | 9.0 | 22 | 8 vs 4 |
| Flooded field closes and reopens | ✅ PASS | 5 | 6.0 ms | 5.8 | 21 | 10 vs 8 |
| Tractor starts nearly empty | ✅ PASS | 1 | 19.6 ms | 11.0 | 8 | 10 vs 8 |
| Chaos burst on a random farm | ✅ PASS | 10 | 1.6 ms | 6.6 | 49 | 2 vs 2 |

## ✅ 3 AM breakdown — tractor B takes over

Night shift. At 03:00 T3 Colt breaks down mid-mowing a HIGH-priority field. Agents must hand the rest to another tractor (via the spare mower at the depot) with no human.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ handled with 0 human interventions
- ✅ J9 finished by a tractor other than T3 — done by T2 at 07:13
- ✅ J9 partial progress kept — total worked area equals recorded area (no rework)
- ✅ safe re-plan dispatched in < 100 ms — 10.8 ms

## ✅ Only sprayer tractor breaks mid-spray

07:40: T2 Bison (the sprayer) breaks at 47% of a HIGH-priority spray. Remaining hectares must move to another tractor and still meet the 10:30 deadline.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ handled with 0 human interventions
- ✅ J5 finished by a tractor other than T2 — done by T1 at 09:48
- ✅ J5 on time (due 10:30) — finished 09:48
- ✅ J5 partial progress kept — total worked area equals recorded area (no rework)
- ✅ safe re-plan dispatched in < 100 ms — 11.0 ms

## ✅ Bridge closes under moving tractors

06:20: North Bridge closes while tractors are driving east. Navigation must re-path everyone; nobody may cross the closed bridge.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ planner → navigation message exchanged — 1 message(s)
- ✅ navigation → planner message exchanged — 1 message(s)
- ✅ all jobs completed by end of day

## ✅ Wind gust during spraying

07:30: forecast jumps to 14 mph for 2 h (drift risk). Spraying must stop immediately, progress kept, and resume only in a safe window.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ no spraying minute outside the safe wind window — 0 unsafe minutes
- ✅ all jobs completed by end of day

## ✅ Operator rushes a job in plain English

08:00: the operator types “Field 6 is urgent, we need it by 10:30”. The Liaison agent must understand it and the plan must move F6 earlier.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ Liaison understood the operator: J7 priority = high
- ✅ J7 finished earlier than without the operator message — 10:10 vs 11:02

## ✅ Two tractors fail within 90 minutes

T2 breaks at 07:40, T3 at 09:10. One tractor left — agents must triage by priority and still beat the baseline.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ handled with 0 human interventions
- ✅ priority-weighted on-time work ≥ FCFS baseline — 11 vs 4

## ✅ Same fault twice → learned reflex

T2 breaks at 07:40, is repaired at 09:00 and breaks again at 11:00. The first time the Planner deliberates; the second time it must reuse the learned reflex from shared memory instantly.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ 2nd identical fault solved by a learned reflex (no deliberation) — 1st: reflex + deliberation 30 ms; 2nd: learned 1.8 ms

## ✅ Flooded field closes and reopens

09:00: F5 floods. Its job must be postponed (never worked while closed) and rescheduled when F5 reopens at 13:00.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ J2 done after the field reopened (13:00) — started 13:18, done 15:40

## ✅ Tractor starts nearly empty

T2 starts at 18% fuel. A refuel stop must be planned automatically; it must never run dry.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ T2 refueled before running dry
- ✅ all jobs completed by end of day

## ✅ Chaos burst on a random farm

Random farm, 6+ random faults (breakdowns, closures, wind, rush jobs, closed fields). Every incident must be handled with zero violations.

- ✅ 0 invariant violations (no lost/duplicated work, fuel, routes, implements, wind)
- ✅ every incident got a safe plan decision — 10 incidents
- ✅ communication efficiency: < 16 messages per incident — 6.6