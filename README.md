# BYD Guided Walkaround

A choice-driven exterior walkaround of a BYD, presented by an AI consultant. Built as a leads-and-engagement pitch demo for BYD South Africa.

## Setup

```
byd-showroom/
├── server.py
├── index.html          ← the walkaround (main demo)
├── freewalk.html       ← the earlier free-roam showroom
├── mic-test.html       ← microphone diagnostic
└── models/
    ├── 2024_byd_seal.glb
    └── 2024_byd_sealion_7.glb
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # optional
python3 server.py
```

Open **http://localhost:8000** in Chrome or Edge.

## How it works

Pick a car, then Naledi walks you round it. Each stop frames a part of the car, she explains it, and offers **three choices** for where to go next. You can also type or hold `V` to ask her anything, and drag to look around at any time — she keeps talking.

**The stops are organised by objection, not by body panel.** That's the whole design:

| Stop | The objection it answers |
|---|---|
| Stance | Do I want to be seen in it |
| Charging | How do I actually live with this |
| Loadshedding | The big one, locally |
| Battery | Is it safe, will it last |
| Practical | Does it work for my week |
| The brand | Is a Chinese car a risk |
| Next step | The close |

## What's worth demoing

**Plain English toggle** (top right). Rewrites everything without jargon — "230 kW" becomes a comparison to a car people know. Every stop is authored twice. This is aimed at the buyer who won't admit to a salesperson that they don't know what kW means.

**The loadshedding moment.** Reaching the V2L stop drops the showroom lights, the car keeps glowing, and appliances light up one by one. Ten seconds, and it's the thing people will describe to a friend.

**Naledi concedes things.** The prompt tells her to admit when an EV is the wrong fit and to refuse to invent specs. A salesperson who occasionally talks you out of something is credible on everything else.

**Lead capture as a payoff, not a toll gate.** She offers to send a summary of what you covered. The lead is saved with the stops visited and the questions asked, so a dealer gets the objection, not just a name.

## The data

Everything writes to `./data/`:

- `leads.jsonl` — captured leads with their session context
- `sessions.jsonl` — stops visited, questions asked, where they dropped

**http://localhost:8000/api/report** gives a live summary: session count, lead conversion, which stops get visited, where people exit, and recent questions verbatim.

That last one is the pitch. Nobody can currently tell BYD SA what South African buyers are actually afraid of, in their own words, at scale. This can.

## If hotspots land on the wrong end of the car

Sketchfab models don't agree on which way is forward. Every hotspot assumes the nose points to **+Z**. If the charge port marker appears on the bonnet, press `[` or `]` to rotate the mesh 90° at a time. When it lines up, copy the value the toast shows into `modelYaw` in the `FLEET` object.

## Controls

| | |
|---|---|
| Drag | Orbit |
| Scroll / pinch | Zoom |
| `V` (hold) | Talk |
| Chips | Choose where to go |
| Left rail | Jump to any stop |

Mobile works — orbit-first by design, since SA traffic will be mostly phones.

## Known limits

- **Voice is the delightful path, typing is the reliable one.** Chrome and Edge only, needs internet (Chrome sends audio to Google). The tour works completely without ever speaking. Check `mic-test.html` if it misbehaves.
- **Fixed narration is instant, AI answers take a second or two.** Deliberate: the six stops never hallucinate and never lag; the AI handles only free questions.
- **46 MB of models.** Compress with gltf-transform before this goes anywhere public.
- **Exterior only.** No interior, no doors, no test drive — all need proper assets from BYD.

## Asking BYD for assets

Don't ask for "a 3D model". Ask for: separated door/boot/frunk geometry, a modelled interior with the rotating 15.6" screen as its own object, paint as a swappable material slot, and wheels as separate meshes. A single welded mesh kills half the roadmap.

## Licensing

Models by **Ddiaz Design** (Sketchfab), **CC BY-NC-SA 4.0**. Attribution required, no commercial use. Replace before any real deployment.

Prices and specs are BYD South Africa retail as last checked — verify before showing anyone.
