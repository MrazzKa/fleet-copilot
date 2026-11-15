# Fleet Copilot vs simple baseline (first-come-first-served)

Same simulator, same farms, same events, same physics. Baseline = when a tractor is idle it takes the first compatible job in submission order.

## Demo scenario, no events

| Metric | Fleet Copilot | FCFS baseline |
|---|---|---|
| Jobs completed | **10** | 10 |
| Jobs completed on time | **10** | 8 |
| High-priority on time | **3** | 1 |
| Priority-weighted on-time score | **20** | 14 |
| Hectares worked | **166.0** | 166.0 |
| Travel time (tractor-min) | **222** | 247 |
| Idle time (min, work was available) | **0** | 0 |
| Waiting at field for safe wind (min) | **51** | 182 |
| Constraint violations | **0** | 0 |

## Demo scenario, scripted events (T2 breakdown 07:40, North Bridge closed 10:00, F8 rushed 11:00, wind 14 mph 12:30)

| Metric | Fleet Copilot | FCFS baseline |
|---|---|---|
| Jobs completed | **10** | 8 |
| Jobs completed on time | **6** | 4 |
| High-priority on time | **3** | 0 |
| Priority-weighted on-time score | **14** | 5 |
| Hectares worked | **166.0** | 132.0 |
| Travel time (tractor-min) | **337** | 282 |
| Idle time (min, work was available) | **0** | 194 |
| Waiting at field for safe wind (min) | **51** | 292 |
| Constraint violations | **0** | 0 |

## 100 random farms with random events

Copilot better on priority-weighted on-time work in **63** farms, tied in 31, worse in 6.

| Mean per farm | Fleet Copilot | FCFS baseline | Δ |
|---|---|---|---|
| Jobs on time | **5.01** | 4.09 | +22% |
| Priority-weighted on-time | **10.57** | 8.48 | +25% |
| High-priority on time | **1.50** | 1.13 | +33% |
| Jobs completed | **5.68** | 5.57 | +2% |
| Hectares worked | **86.03** | 84.35 | +2% |
| Travel (min) | **161.76** | 183.55 | -12% |
| Idle (min) | **45.74** | 42.90 | +7% |
| Waiting for wind (min) | **60.55** | 78.77 | -23% |
| Violations | **0.00** | 0.00 | n/a |

Travel per completed job: **28.5 min** (Copilot) vs 33.0 min (FCFS), -14%.
Unproductive time (idle + waiting) total: **10629** vs 12167 min, -13%.
Crashes: 0. Runtime: 7.8 s.

## Chaos test

1000 random farms × 6474 injected events (breakdowns, repairs, bridge closures, wind gusts, rushed jobs, new jobs, closed fields, operator pins).
Every invariant checked every simulated minute: **0 violations, 0 crashes**. The full agent loop handled **8026 incidents** with 57650 agent messages and 47287 tool calls; unhandled incidents: **0**.
