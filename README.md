# Fleet Copilot

A team of software agents that keeps a fleet of autonomous tractors working when things go wrong.

Built for the AgX Hack Day, track "Run the Fleet" (ATC).

## The problem

One autonomous tractor can follow a plan. A fleet needs someone to change the plan when the day changes. A tractor breaks down at 3 AM, a bridge is closed, the wind picks up and spraying has to stop, or a manager says a field is urgent. Today a person has to notice that, rebuild the schedule by hand and tell every machine what to do next.

Fleet Copilot does that part. It notices the problem, makes a new plan, checks that the plan is safe, sends it to the tractors and tells the operator what changed and why. The operator only steps in when they want to.

## What it looks like

- **AgOpenWeb view.** The main screen shows a real AgOpenWeb (AgOpenGPS for the browser) instance. Our Telemetry agent holds control of its built-in vehicle simulator and drives it like tractor T1. When the vehicle stops in AgOpenWeb, the agents notice it from GPS alone.
- **Incident story.** Every incident gets a timeline: what broke, when it was detected, the new plan, the safety check, when the tractors got their orders, the second opinion from the LLM, and when things went back to normal. Each step has its time.
- **Agent network.** A live graph of the 7 agents and the messages between them.
- **Farm map and schedule.** A whole farm day (3 tractors, 10 fields, 10 jobs, a river with two bridges, changing wind) next to a simple first-come-first-served baseline that gets the same events.

## The agents

| Agent | Job |
|---|---|
| Fault Detection | Watches the simulator, GPS and deadlines. Opens an incident and classifies it. |
| Planner | Builds a new plan in milliseconds (reflex). Then asks the LLM for a second opinion in the background. |
| Navigation | Answers route questions: which fields are cut off, which tractor needs a detour. |
| Safety | Checks every plan on its own with 8 rules before anything moves. Can reject a plan. |
| Execution | Sends the approved plan to the tractors and watches that it is followed. |
| Operator Liaison | Talks to people. Understands typed or spoken commands in plain English. |
| GPS Twin (Telemetry) | Connects to AgOpenWeb, drives its vehicle and reports stalls or a lost link. |

They talk over a message bus. Every message has a sender, a receiver, an incident id and a time, so you can follow one incident from start to end. They share one memory: facts (for example the last GPS position), past incidents, learned reflexes and operator rules. Memory is saved to disk.

## How one incident works

1. **Something breaks.** For example the vehicle in AgOpenWeb stops while the plan says it should be working.
2. **Detection.** After 4 seconds of zero speed, the GPS Twin tells Fault Detection. It opens an incident. No one pressed a button in our app.
3. **Fast plan.** The Planner runs the optimizer and builds a plan without the broken tractor. This takes milliseconds.
4. **Safety check.** The Safety agent checks the plan: no job given twice, no lost work, no closed bridge, enough fuel, the right implement, spraying only in safe wind, and more. If it fails, the Planner falls back to a simpler plan.
5. **Execution.** The plan goes out. Each tractor gets its new task list.
6. **Second opinion.** In the background the LLM looks at the situation with tools (`get_situation`, `recall`, `simulate_strategy`, `check_route`, `remember`, `submit_strategy`). It tries different strategies on a copy of the world. If one is better, it replaces the plan. That plan is checked by Safety again.
7. **Learning.** The winning strategy is saved as a reflex for that kind of incident. Next time the same kind of problem is handled at once, without waiting for the LLM.
8. **Recovery.** When the vehicle moves again in AgOpenWeb, the GPS Twin reports it. The tractor goes back into the plan.

The LLM never drives a tractor. It only picks a strategy through tools, and the Safety agent checks every plan before it is sent.

## Results

All numbers come from the code in this repo. You can rerun them (see below).

- Demo day with events (breakdown, bridge closed, rush job, wind): **10 of 10 jobs done, 3 of 3 high-priority on time.** The first-come-first-served baseline finishes 8 jobs and 0 high-priority jobs on time.
- 100 random farms: about **25% better weighted on-time score** and about **14% less travel per job** than the baseline.
- Chaos test, 1000 random farms with random faults: **0 rule violations, 0 crashes.** See `reports/chaos.json`.
- 10 fault test cases (night breakdown, double fault, bridge closure, wind gust, low fuel, flooded field and more): **10 of 10 pass.** See `reports/test_cases.md`.
- Unit tests: `pytest` (12 tests).

## Run it

You need Python 3.10 or newer. Tested on Ubuntu (WSL2 on Windows 11).

```bash
cp .env.example .env   # optional: add an API key
./run.sh               # opens on http://localhost:8000
```

`run.sh` makes a virtual environment, installs the packages and starts the server.

### With Docker

```bash
docker compose up --build
# public link for phones (cloudflared):
docker compose --profile public up
```

The compose file uses host networking so the app can reach AgOpenWeb on `127.0.0.1:5174`. This works with Docker Engine on Linux and WSL2.

### With AgOpenWeb

1. Install and start AgOpenWeb (it serves on `http://localhost:5174`).
2. In `.env` set `FLEET_AGOPENWEB=ws://localhost:5174/ws`.
3. Start Fleet Copilot first, then open AgOpenWeb in the browser. The first client holds control, so our GPS Twin drives the vehicle and the browser just watches.
4. Check the connection: `python scripts/agow_probe.py`.

### LLM options

The agents try these in order and fall back to the next one if a call fails:

1. Anthropic Claude (`ANTHROPIC_API_KEY`)
2. Any OpenAI-compatible API, for example Groq (`OPENAI_API_KEY`, `OPENAI_BASE_URL`)
3. Local Ollama (`OLLAMA_URL`)
4. Plain rules, no LLM

Without any key the app still works. Planning, safety and execution never wait for the LLM.

### Tests and reports

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m fleet.agents.testcases      # 10 fault cases
.venv/bin/python -m fleet.bench --chaos 1000    # chaos test
.venv/bin/python -m fleet.bench --seeds 100     # benchmark vs baseline
```

## Judges' phones

The "Judges" button shows a QR code. People who open the public link can break or fix a tractor, close a bridge, call a wind gust or rush a job. Everything else (reset, memory, logs, test runner) only works on the presenter's laptop. Public viewers get a smaller live stream and are limited to one event every 2 seconds.

## API

| Endpoint | What it does |
|---|---|
| `GET /health` | Status for Docker and monitoring |
| `WS /ws` | Live state, 10 times a second |
| `POST /api/event` | Inject an event (breakdown, repair, block, wind, priority, add_job) |
| `POST /api/command` | Text command to the Liaison agent |
| `GET /api/stories` | Incident timelines |
| `GET /api/why?job=J5` | Why a job is planned the way it is |
| `GET /api/log.json` | Full run log for audit |

## Limitations

- We drive AgOpenWeb's built-in simulator, not a real tractor. Nothing here has been tested on real machines.
- One AgOpenWeb vehicle is mirrored (tractor T1). The other tractors are simulated.
- The farm is a grid model. Field shapes, slopes and soil are not modeled.
- The local Ollama fallback is supported in code but was not tested end to end on the demo laptop.
- The public link has rate limits and a short list of allowed actions, but no login.

## Next steps

1. **Connect to real machine data.** Replace the AgOpenWeb adapter with an adapter for the tractor's own controller (CAN bus, ISOBUS or the vendor API). The Telemetry agent already works on speed, position and link health, so the change is in one file.
2. **Use more signals for detection.** Engine and drive faults, camera health, fuel or battery level, not only GPS speed.
3. **Real field maps.** Import field boundaries and roads from AgOpenGPS or ISOXML instead of the grid.
4. **Voice in the cab and on the phone.** The Liaison agent already understands short spoken commands. The next step is push-to-talk for the person who supervises the fleet.
5. **Pilot.** Run it in shadow mode on one farm for a season: it suggests plans, a person approves them, and we compare the results with what really happened.
6. **Security.** Add login and roles before anything is exposed outside the farm network.

## Project layout

```
fleet/            simulator, planner, safety checks, explanations
fleet/agents/     agents, message bus, shared memory, LLM tools, test cases
fleet/integrations/agopenweb.py   AgOpenWeb client (binary WebSocket protocol)
static/           dashboard (plain HTML, CSS and JavaScript)
scenarios/        demo farm
reports/          benchmark, chaos test and test case results
scripts/          AgOpenWeb probe and test helpers
tests/            pytest
```

`scripts/fake_agopenweb.py` is a small stand-in for the AgOpenWeb protocol. It is only used by the automated tests so they can run without AgOpenWeb installed. The demo uses the real AgOpenWeb.

## Credits

AgOpenWeb and AgOpenGPS are open-source projects by their own authors. This project only talks to AgOpenWeb over its WebSocket protocol and does not include its code.
