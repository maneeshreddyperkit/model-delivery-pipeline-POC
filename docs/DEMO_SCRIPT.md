# Demo script

Twenty minutes, two interviewers, seven tabs. You will not show all seven.

**Before the call**

```
python -m modelops demo        # full pipeline run, about 30 seconds
python -m modelops serve       # dashboard on http://127.0.0.1:8765
```

Then open these four tabs in the browser, in this order, and leave them open:
Operations, Work packages, Viewer, Handover. Everything else is answered from
those four or opened only if asked.

Run `python scripts/demo_facts.py` if you want to re-check any number in this
script against the current database. Every figure below came from that script,
so if a number here looks wrong it is because you re-seeded, not because the
script is lying.

---

# Who you are talking to

They want different things from the same twenty minutes, and the demo has to
serve both without visibly switching gears.

**Mathew Miller** built the nightly model conversion automation, the Databricks
platform and the UPV web apps, and is currently prototyping an MCP server for
driving the UPV 3D model through its API. He was a piping superintendent before
that. He will recognise every shortcut, he will ask what is real, and he is the
only person in the room who could rebuild this himself. Give him depth,
measurements, and the honest boundary of what works.

**Amy Lewis** founded Digital Project Execution and owns the Common Data
Environment that aggregates Hexagon Smart Suite and InEight into an integrated
model. She is a SmartPlant expert and cares about master data governance,
commodity codes and structured turnover. She will not care how decimation works.
Give her lifecycle, conformance and the cost of bad data.

**The rule for every screen:** one sentence for the engineer, one for the owner.
If a view only has one of those, do not show it.

---

# The argument, in one breath

Say this early and come back to it at the end.

> "Work packaging, commissioning and progress reporting all sit on top of model
> attribute integrity. Today that integrity is mostly assumed. This measures it,
> enforces it before a model reaches anybody, and then proves what it is worth
> by running work packaging and handover off the same data."

That is one claim, and the rest of the demo is evidence for it. The Construction
Industry Institute makes the same point in its AWP procedure: work packaging
software is "critically dependent upon the structure, attributes and integrity
of the 3D model." You are not proposing a new idea. You are showing what the
existing one unlocks when someone actually measures it.

---

# How to open (2 minutes)

Roughly word for word. It sets expectations honestly, which matters more than
sounding like an expert.

> "Quick context, because it cuts both ways.
>
> I am at Kiewit today, on Union Station, as a requirements analyst. Day to day
> that means allocating attributes to requirements: owner, type, discipline,
> system, sub-system, and keeping all of that traceable. So the question this
> proof of concept is built around, whether the attributes on an object are
> complete enough to do anything downstream with, is a problem I already work on
> here. Just against requirements rather than against geometry.
>
> The 3D half I have done elsewhere. Before Kiewit I built a pipeline that
> validated, converted and optimised mechanical CAD for an inspection platform,
> cutting model size around 70% without losing engineering fidelity, with
> revision tracking, retries and lineage around it.
>
> What I have not done is plant design or nuclear. I have not used
> UniversalPlantViewer, Navisworks or SmartPlant. I would rather say that now
> than have you find it out in twenty minutes.
>
> So I put the two halves together on my own time and built something, partly to
> check I had understood the problem and partly because I would rather show you
> something than assert I could do it. The most useful thing you can do is tell
> me where it is wrong."

**Then, immediately, the honesty slide.** Do this before Mathew asks, because he
will ask.

> "One thing up front so you can calibrate everything else. Fifteen of the 1,247
> jobs on that dashboard actually ran on this laptop: real geometry converted,
> real bytes weighed, real quality gates enforced. The other 1,232 are fifteen
> days of backfilled nightly history, so the throughput and reliability numbers
> read at the scale you actually run at. Those rows are flagged `is_simulated =
> 1` in the database and you can filter them out yourself in the SQL tab. Every
> payload figure and every per-stage timing is measured, never sampled."

Volunteering this is worth more than any feature in the demo.

---

# The route: four tabs, twenty minutes

| Time | Tab | For |
|---|---|---|
| 2:00 | Operations | Amy first, then Mathew |
| 5:00 | Work packages | Both. This is the payoff |
| 10:00 | Viewer | Mathew, driven from Work packages |
| 13:30 | Handover | Amy |
| 16:30 | Assistant and Tools | Mathew |
| 19:00 | Close | Both |

---

## Operations, 3 minutes

**Amy's sentence:** one pipeline serves four districts with different project
sizes and different service levels, and it reports per district rather than as
one blended number.

**Mathew's sentence:** every failure is classified as transient or permanent
before anything decides whether to retry it.

**Point at the district table.**

> "Same pipeline, four districts. Nuclear Solutions is 62 models and 3,465 of
> the 3,765 users. Power Delivery is 18 models and 130 users. Their success
> rates are 95.6 and 95.4, which is close, but the consequence of a bad night is
> not close at all. One global success rate hides that. This is the split I
> would want if I owned the platform."

**Point at the throughput chart, then the 31st.**

> "Fifteen nights. Weekdays are around 110 models a night, weekends drop to
> about 40 because engineering is not issuing. And then there is the 31st: 100
> models, 18 of them failed, success rate 82%. At fleet scale that is the whole
> point. One bad night is visible as a bad night instead of being averaged into
> noise, and 82% against a 96% baseline is something you would want a call
> about."

The last bar is always short. That is the run in progress, not a cliff. Say so
before anyone asks.

**Point at "Payload reduction, 91.0%, measured on 9 models."**

> "That is deliberately labelled. The 91% is measured on the nine models this
> machine actually converted, not on the backfilled thousand. The pipeline
> writes a second file on every run, a flat conversion with no instancing, and
> weighs it. So the reduction is measured against a real file rather than
> estimated against a baseline I made up."

**If Mathew asks about timings:** read them off the page rather than quoting
this document. They are one to two seconds end to end on average and two to four
at p95 across the fifteen measured jobs, but they move with the machine, so the
number on screen during the call is the true one. The per-stage table underneath
says which step to optimise first, and it is the optimisation step, which is
correct because it is the only step doing real work.

---

## Work packages, 5 minutes

This is the centre of the demo. Do not rush it.

> "Everything so far is pipeline plumbing. This is the page that says why the
> plumbing matters."

**Read the tiles left to right.** 206 packages. 10 ready to release. 129
blocked. 18 carry incomplete attributes. 67 complete.

**Then stop on the fourth tile and slow down.**

> "Eighteen packages contain components whose attributes were never populated in
> the source model. Three of them are blocked on nothing else. Look at what that
> means: the steel is up, the materials are on site, the package ahead of them is
> finished, and a planner still cannot schedule, cost or report against them.
> Nothing is physically wrong. The model is wrong.
>
> And a further fifteen were installed and handed over with those gaps still in
> them. That is the same defect, found too late to fix cheaply."

**Click the DATA filter, then open `IWP-BOP-200-STL-01-001`.**

Structural, 44 components, planned for 10 May, blocked because three components
are missing material.

> "Three components out of forty-four. No material. Which means no weight
> take-off, no cost, no procurement check, and the package cannot be released.
>
> Here is the part I would want you to push on. The model this came from passed
> the quality gate. Attribute coverage across the whole model was above the 90%
> publishing threshold, so it published, correctly. The gap only becomes a
> blocker when you slice the same data by work package, because three missing
> components spread thinly across a model are invisible and three missing
> components concentrated in one package stop that package dead.
>
> That is the argument for measuring integrity at the level work actually gets
> planned at, not just at the model level."

**Amy's sentence, say it here:** every one of these is attributable to a named
component in a named model, which is exactly what turnover needs and what a
site walk will never find.

**Mathew's sentence:** the four constraints are engineering issued, materials on
site, the preceding package installed, and attributes complete. The first three
are what any AWP tool checks. The fourth is the one the model can answer and
usually is not asked.

**Then use the search box.** Type `HVAC`. 206 rows becomes 22, instantly.

> "The verdict and project filters are server-side because they belong in a URL
> you can send someone. This one is client side, because it is the narrowing you
> do while somebody is watching and a round trip per keystroke would show."

Click the Hours column to sort. Point out that 30,961 craft hours are sitting
behind blocked packages against 1,894 ready to release.

---

## Viewer, 3 minutes

Get here by clicking **Isolate in 3D** on the package you just opened. Arriving
here from a blocked package is the whole point; do not navigate to the tab.

> "Same package, in the model. Everything outside it is ghosted. The 44
> components are the ones a crew would actually be handed."

**Point at the HUD, bottom left.**

> "1,521 components in this model, 160,648 triangles, and nineteen geometries.
> Nineteen. A plant is the same elbow, flange and valve thousands of times over,
> so the file stores each shape once and a list of where to stamp it. That is
> where the size reduction comes from. It is not compression, it is just not
> repeating yourself. 15.7 megabytes of naive conversion becomes 1.2 megabytes
> published and 133 kilobytes over the wire."

**Change "Colour by" to Readiness.**

> "Now the same geometry is coloured by whether the package each component
> belongs to can be released. This is the same data as the table, but nobody has
> ever asked me to read a table in a coordination meeting."

**Change it to Lifecycle, then open the 4D sequence and press play.**

> "Eight lifecycle states per component, from Designed through to Commissioned.
> The scrubber plays planned install dates against what is actually installed.
> Anything still ghosted when the cursor passes its planned date is late, and it
> goes amber. So the same model answers 'what does it look like' and 'are we
> behind', and you did not have to build a separate 4D deliverable to ask."

**The warning that has not changed.** Never compare this to
UniversalPlantViewer. You will be compared to a mature product by the people who
own it and you will lose on every axis: clash detection, measurement, markup,
section planes, saved viewpoints, streaming. If it comes up:

> "This is not meant to be a viewer, you already have one. I built enough of one
> to prove the file the pipeline produces actually opens and that the attributes
> are attached to it correctly. If this were real, the last stage would hand off
> to yours."

That reframes the 3D tab from a product you are pitching into a test you wrote
for your own output, which is both honest and a stronger position.

---

## Handover, 3 minutes

This tab is Amy's. Lead with the feeds, not the chart.

> "The viewer is one consumer of this catalog and it is not the interesting one.
> Cost, planning and completions all need to know what is in the plant, and they
> need it in a form a machine reads."

Five feeds, generated on request from the same published revisions the viewer is
served from, so a download is never a stale export:

| Feed | Records | Goes to |
|---|---|---|
| Component register | 6,595 | InEight, CDE |
| Work package status | 206 | InEight |
| Commissioning index | 57 | Hexagon Smart Suite |
| Model index | 9 | Universal Plant Viewer, CDE |
| Attribute exceptions | 139 | Data governance |

> "The last one is the one I would actually send. It is 139 rows, and every row
> is one component in one model missing one attribute a receiving system needs.
> That is a work list, not a report."

**Then the completeness grid.**

> "Every attribute against every discipline, across 6,444 published components.
> Grey is fully populated. The brighter the cell, the bigger the gap.
>
> And the point of the grid is that the gaps are not spread evenly. HVAC is
> missing material on 18 components. Electrical is missing commissioning system
> on 19. CWA, IWP and weight are at 100% everywhere. So this is not a data
> quality programme, it is two conversations with two disciplines."

**Point at the integration table.**

> "Inbound and outbound. The connectors I did not build are listed rather than
> left out, each one naming the interface it would use. Hexagon Smart Suite and
> InEight are feed-only right now. I would rather show you the boundary than
> imply it is not there."

---

## Assistant and Tools, 2.5 minutes

This tab is Mathew's, and it directly mirrors what he is prototyping.

**Open the assistant, bottom right. Ask, in the panel:**

`which packages are blocked by missing data?`

It answers with the three, as a table.

Then: `isolate IWP-BOP-200-STL-01-001`

The viewer changes.

Then: `colour the model by readiness`

The viewer changes again.

> "The point is not the chat. The point is that it changes what is on screen. It
> is calling the same functions the pages call, against the same catalog, and
> the viewer state it produces is just a URL, so anything the assistant can do I
> can also type into the address bar or send you in a link. The viewer stays
> testable without a language model anywhere near it."

**Now go to the Tools page and be straight about the model.**

> "Eight tools, two of which change the 3D view. These are the exact JSON
> schemas handed to the model, which is the same shape an MCP server advertises,
> so this tool layer could sit behind an MCP endpoint and be pointed at a real
> viewer API without any of these definitions changing.
>
> And the header says 'Not installed'. Ollama is not on this machine, so there
> is no language model in the loop right now. Questions are routed to the same
> tools by pattern matching instead, and the panel says which of the two answered
> every time. What a model would add is tolerance for how a question is phrased.
> The tools, the numbers and the viewer control are identical either way.
>
> The other thing I would point at is that the read-only boundary is in the code,
> not in the prompt. There is no tool that requeues a job, edits an attribute or
> writes a file. A boundary a model is asked to respect is not a boundary."

That last paragraph is the single most valuable thing you say to Mathew. Do not
skip it to save time.

---

## Close, 1 minute

> "So, back to where I started. Work packaging, commissioning and progress
> reporting all depend on model attribute integrity, and that integrity is
> usually assumed rather than measured. Three packages on that board cannot be
> planned, and nothing is physically wrong with any of them. Fifteen more were
> handed over with the same defect still in them.
>
> None of the data behind that is new. The pipeline already had it. The only new
> thing is that something is now counting it, and refusing to publish when the
> count is bad.
>
> It is a proof of concept built on evenings and weekends, on my own equipment,
> with no Kiewit data in it. Tell me where it is wrong."

---

# Tabs you did not show

Open these only if asked. Each one has a single sentence.

**Jobs.** Where you go when someone says their model is missing. Fifteen
measured jobs, five deliberately broken. Job #3 failed on a transient error,
backed off, retried and succeeded on attempt 2, and nobody was told because
nobody needed to be. Job #7 was a truncated upload and stopped dead on attempt 1,
because retrying a corrupt file cannot fix it and only delays everything queued
behind it. Knowing the difference between "try again" and "stop and get a human"
is most of what keeps a queue moving.

**Catalog.** What is live right now and what is not reaching users. Fourteen
models physically converted, nine live. `TB2-PIPE-U20` explains the gap: it was
submitted twice, RevA and RevB, both processed, RevB is live and RevA is still
there. That is the undo button, and it is one command.

**SQL.** The whole catalog is queryable, read-only twice over: the endpoint only
accepts a SELECT and the connection cannot write. Run the attribute completeness
query. A demo that can delete its own data is not a demo.

**How it works.** Configuration, quality gate thresholds, stage design. Only if
someone asks how a gate is tuned.

---

# The five deliberate failures

You broke these on purpose. Know all five cold, because "why did that fail" is
the most likely question of the whole call.

| Job | Model | Stopped by | Why it matters |
|---|---|---|---|
| 1 | `BOP-ELEC-U30` | Exported in inches | Never converted. Publishing is in millimetres |
| 2 | `BOP-EQUIP-U30` | `Area` on 0% of components | Below the 90% gate. Filtering would be silently wrong |
| 7 | `NI1-HVAC-U10` | Truncated archive | Permanent. Quarantined on attempt 1, never retried |
| 11 | `TB2-EQUIP-U20` | Manifest missing 3 fields | Cannot be routed or placed in coordinate space |
| 15 | `TB2-STEEL-U20` | 6 duplicate tags | Attribute joins are ambiguous. Looks fine in a viewer |

**The units one is the story to tell.**

> "This arrived in inches, we publish in millimetres, and it was stopped at the
> door. NASA lost a Mars orbiter exactly this way in 1999: one team in pounds,
> the other in newtons, nobody caught it, and a 125 million dollar spacecraft
> broke up in the atmosphere. Here it is a two line check that runs in about a
> millisecond.
>
> That is the whole argument for automating it. The check is trivial. Remembering
> to do it on model 300 of the day at six in the evening is the part people are
> bad at."

**The duplicate tag one is the one Mathew will like.**

> "Two different steel members with the same tag. Everything downstream looks
> components up by tag, so the moment a tag is ambiguous the attributes attach to
> the wrong thing. That model would have looked completely fine in a viewer while
> quietly giving people wrong information. I think stopping it was right, and I
> would want to know if you disagree."

---

# Failure modes, and what to say

Rehearse these. A demo that breaks gracefully is more convincing than one that
does not break.

**The assistant says "Not installed" and you wanted the model.**
Nothing to fix. This is the expected state and the panel already says so. Say:
"Ollama is not installed on this machine, so this is the deterministic fallback.
The tools are the same." Then carry on. The three scripted questions all work on
the fallback; they are the ones in this script for exactly that reason.

**The assistant picks the wrong tool.**
It will occasionally route a question to `search_docs` when you wanted data. Do
not fight it. Say: "That went to the documentation tool rather than the catalog,
let me ask it the other way", and rephrase using a word from the tool list, for
example "packages", "components", "attribute gaps". Then add: "That is the
tradeoff of the rule based fallback, and it is the one thing a real model fixes."
This lands as honesty rather than as a bug.

**The viewer does not load.**
Refresh once. If it still fails, the model files are on disk and the catalog
page proves it: point at the Delivered and Over the wire columns and say the
viewer is a client of those files, not the source of truth. Then move to
Handover, which needs no WebGL. Do not spend more than thirty seconds on it.

**The 3D is slow or the frame rate is bad.**
Switch the detail level dropdown to a coarser LOD and use it as a feature: "This
is the same model decimated. The tag tree does not change, so you can still find
everything, which is the point of generating levels of detail rather than
separate files."

**Somebody asks for a number that is not on screen.**
`python scripts/demo_facts.py` prints all of them. Better: ask the assistant, in
front of them. Being able to answer an unplanned question from the catalog live
is worth more than having memorised it.

**You lose the thread.**
Go back to the one sentence: attribute integrity is what makes a package
plannable, and this measures it. Everything in the demo is evidence for that.

---

# Questions you will get

Answer short. Do not oversell.

**"Where did these models come from?"**
> "I generated them procedurally, and the generator is in the repo. There is no
> Kiewit data and nothing from a previous employer in here. I deliberately made
> it produce messy geometry, because clean input would have given the
> optimisation stage nothing to do and made the numbers meaningless."

**"Is any of this ours?"**
> "None of it. That was deliberate, so I could hand this to anyone without a
> conversation about it."

**"How would this handle Navisworks, or SmartPlant, or our real formats?"**
> "I do not know those formats, so I did not pretend to. Only the intake stage
> knows what kind of file it is reading; everything after that works off one
> internal representation. So a new format is one adapter, not a change to the
> pipeline. Whether that holds up against a real Navisworks export is exactly
> what I would want you to tell me."

**"Could this handle hundreds a day?"**
> "This version, no. It is a single file SQLite database, which is right for a
> demo and wrong for real volume. The SQL Server schema is in the repo as the
> target variant because the job description names Azure SQL, and the queries are
> written to port. It does run jobs in parallel and the storage layer is
> contained, but I would not claim it is production ready."

**"Why local? Why not just use Azure?"**
> "Corporate policy, and it also made the demo stronger. There is no network call
> at runtime anywhere in this, including the fonts, the charting library and the
> 3D library, which are all vendored. The language model would have been Ollama
> on the same machine. Nothing about the design needs the cloud, and nothing
> about it prevents moving there."

**"How long did this take?"**
> "Evenings and weekends, on my own equipment, outside Union Station hours. I
> wanted to show I had understood the problem well enough to build something
> around it, not to hand you finished software."

**"What would you do next?"**
> "Three things, in order. Real source formats. A proper database. And then the
> one I think is most interesting: when a model is revised, publish only what
> changed instead of the whole thing again. At a few hundred models a day that
> seems like it would matter, and it is the same delta calculation the attribute
> exceptions feed already does, just applied to geometry."

**If you do not know something, say so.**
> "I do not know. I have not worked with that. How do you handle it today?"

You are already inside the company, so the question is not whether you can be
trusted with the work, it is whether you can do this particular work. Curiosity
reads well. Bluffing about plant design in front of an ex piping superintendent
does not.
