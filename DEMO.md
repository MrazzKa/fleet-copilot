# Demo plan

Two laptops. The Zenbook shows the slides. The ROG runs AgOpenWeb and Fleet Copilot.

Talk time is about 2 to 3 minutes, then questions.

## Before we start (10 minutes before)

On the ROG, in WSL:

```bash
sudo systemctl status agopenweb      # should be active
cd ~/fleet-copilot && ./run.sh       # wait for "Fleet Copilot running"
```

1. Open `http://localhost:8000`. The top left should say **AgOpenWeb (live GPS)** and the GPS twin badge should say **LIVE · control**.
2. Press the reset button, then **Start day**, speed **1x**.
3. Close any other AgOpenWeb tabs. Only our app should hold control.
4. Check the LLM pill in the header. It should show Claude. If it shows "rules only", the demo still works.
5. Optional: start the tunnel for the QR code (`cloudflared tunnel --url http://localhost:8000`) and paste the URL into the Judges dialog.

## Pitch script (about 2.5 minutes)

**Slide 1. Title.**
"Hi, I'm Timur. Our project is Fleet Copilot. It's a team of AI agents that keeps a tractor fleet working when something goes wrong."

**Slide 2. Problem.**
"One autonomous tractor can follow a plan. But a farm day changes all the time. A tractor stops, a bridge is closed, the wind gets too strong to spray. Right now a person has to notice it, fix the schedule and tell every machine. At 3 AM nobody is watching."

**Slide 3. How it works.**
"We have seven agents. Each one has one job. Fault Detection notices the problem. The Planner makes a new plan in milliseconds. Safety checks the plan before anything moves. Execution sends it to the tractors. Then an LLM, Claude, looks again in the background and can find a better plan. If it does, the plan is checked again and we save it as a reflex for next time."

**Switch to the ROG. Live demo.**
"This is real AgOpenWeb, the open-source AgOpenGPS in the browser. Our GPS agent drives its vehicle as tractor T1.
Now I break it." (Press **GPS fault**.)
"I didn't tell our app anything. The vehicle just stopped in AgOpenWeb. After four seconds the GPS agent sees zero speed and reports it."
(Point at the Incident story at the bottom.)
"Here is the story of this incident. Detected. New plan in about 20 milliseconds. Safety passed. Orders sent. Then Claude gave a second opinion a few seconds later."
(Press **Resume**.)
"Now the tractor moves again. The agent sees it and puts T1 back in the plan. That's the full loop: what broke, how we noticed, how we fixed it, and how it went back to normal."

**Slide 4. Results.**
"On the demo day with four problems we finish all 10 jobs and all high-priority jobs on time. A simple first-come-first-served schedule finishes 8 and misses every high-priority job. On 1000 random farms with random faults we had zero safety violations and zero crashes."

**Slide 5. Next steps.**
"We built it so the GPS adapter can be swapped for real machine data. Next we would connect to the tractor controller, use more signals than GPS, import real field maps and run it in shadow mode on one farm for a season."

"Thank you. You can scan the QR code and break a tractor yourself."

## If a judge asks to break something

- Use the buttons at the bottom: **Break T2**, **Fix T2**, **N bridge**, **Gust**, **Rush**.
- Or type: `T2 broke down`, `close north bridge`, `F8 urgent by 15:00`, `why not T3 for J5`.
- Repeat the same fault twice. The second time the story shows a learned reflex: no waiting for the LLM.

## Backup plans

| Problem | What to do |
|---|---|
| No internet | Click the LLM pill. It switches to offline mode and the agents use rules. Everything else works the same. |
| Claude API error | Same as above. The failover goes to the next provider or to rules by itself. |
| AgOpenWeb down | Switch to **Farm map**. Use **Break T2** and **Fix T2**. The incident story works the same way. If the AgOpenWeb link drops during a run, that is also a fault the agents report ("GPS telemetry lost"). |
| Browser shows AgOpenWeb as "observer" | Close other AgOpenWeb tabs and press **Take control** in the GPS twin box. |
| Everything frozen | Stop the server (Ctrl+C) and run `./run.sh` again. It takes about 5 seconds. |
| Public QR link fails | Skip it. Show the buttons on the ROG instead. |

## Questions we might get

**Why use an LLM at all?** The fast plan comes from an optimizer, so the tractors never wait. The LLM is good at the part that needs judgment: comparing strategies, reading operator rules from memory, explaining the change. It only acts through tools, and Safety checks its plan.

**What if the LLM is wrong?** Its plan goes through the same Safety check. If it fails, we keep the fast plan.

**How would this connect to real tractors?** The GPS Twin agent only needs position, speed and link status. For a real machine we would write an adapter for its controller instead of AgOpenWeb. The rest stays the same.

**Is it tested on real machines?** No. We use AgOpenWeb's built-in simulator and our own farm simulator.
