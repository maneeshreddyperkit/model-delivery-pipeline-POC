# Demo script

Ten minutes. Run `python -m modelops demo`, then `python -m modelops serve`.

---

## How to open

Say this more or less word for word. It sets expectations honestly, and that
matters more than sounding like an expert.

> "Before I show you anything — I have not worked in plant design or BIM. I have
> not used UniversalPlantViewer or Navisworks. So I want to be upfront that I am
> coming at this from the outside.
>
> What I have done is the 3D side of it. In manufacturing I built tooling that
> processed 3D models and used them on an assembly line to cut inspection time
> and catch defects. Different industry, same underlying problem: take a heavy
> engineering model, make it usable, and make sure a bad one never reaches the
> people relying on it.
>
> After reading the job description I picked out one problem that seemed
> central, and spent a weekend building a small working thing around it — partly
> to see if I actually understood the problem, and partly because I would rather
> show you something than just tell you I could do it.
>
> It is a proof of concept. It is small on purpose. And I would genuinely like
> to know where I have got it wrong."

## The one problem I picked

> "The job description talks about delivering hundreds of models a day to
> thousands of users, and keeping quality consistent. What struck me is that
> those two goals fight each other.
>
> If a person does the work — export, convert, check, upload — then quality is
> good but you cannot do hundreds a day. If you automate it naively you get
> volume, but a broken model reaches thousands of people before anyone notices.
>
> So the one problem I picked is: **can you automate the whole path from
> engineering file to viewer, and still refuse to publish a bad model?**
>
> That is all this does. It is not a BIM platform. It does one path, end to
> end."

## What I built, in one picture

A model goes through seven steps. Every step exists to stop one specific thing
going wrong:

| Step | Plain English | Stops this |
|---|---|---|
| Intake | Open the file | Unknown file formats |
| Validate | Is this even usable? | Wrong units, missing information |
| Extract | Pull out the parts list | Model you can look at but not search |
| Convert | Convert it, badly, on purpose | Nothing — this is the "before" measurement |
| Optimise | Make it small | Files too heavy to open in a browser |
| QA | Is the result good enough? | A broken model reaching users |
| Publish | Make it live, all at once | Users seeing a half-finished model |

> "The fourth step is the odd one. It converts the model with no optimisation at
> all and measures the result. That file gets thrown away. It exists only so
> that when I claim the next step made things smaller, I am comparing against a
> real measured number instead of guessing."

## Numbers to quote

From a clean run — these are on screen, you do not need to memorise them:

- **14 files in. 9 went through, 5 were rejected.** 64% success rate.
- The 5 rejections are deliberate. I broke those files on purpose.
- One file failed, retried itself, and succeeded. No human involved.
- **39 MB becomes 350 KB** by the time it reaches a browser.
- The whole run takes about 9 seconds.

**Watch this one.** The dashboard says **8 models live**, not 9. That is not a
mistake and you should be ready for it: one model was submitted twice, as two
different versions, and the newer one replaced the older. Nine successful runs,
eight models live. If they notice before you explain it, that is actually a good
moment — it is proof that version tracking works.

> "The 64% is the number I would want you to look at, not the 39 MB. Anyone can
> make a file smaller. The interesting question is what happens to the five that
> were broken — and the answer is they stopped, they were labelled with a reason
> a person can act on, and none of them reached a user."

---

# Walking the tabs

Left to right. Five tabs, about two minutes each.

## 1. Operations — "here's what happened"

**What they see.** Five numbers across the top, then a size comparison, then
which models failed and why.

> "This is the page I would leave up on a screen.
>
> Fourteen models went in. Nine made it. Five did not, and I broke those five on
> purpose so you could see what happens when things go wrong — which at hundreds
> a day is most of the interesting behaviour.
>
> This block is the size comparison. Top row is the model converted with no
> optimisation. Middle row is what we actually publish. Bottom row is what a
> browser downloads. Same models measured three times, so it is a fair
> comparison rather than a claim. Thirty-nine megabytes down to three hundred
> and fifty kilobytes.
>
> And this shows where the time goes, per step. Right now the optimisation step
> is the slow one, which is correct — it is the only step doing real work. If
> this got too slow, this table tells me where to look."

**Point at this — the model that was drawn in inches.**

In the failed list, `BOP-ELEC-U30`. It was rejected because it came in measured
in inches, and everything published here is in millimetres.

> "This one never even got converted. It arrived in inches, we publish in
> millimetres, and the pipeline stopped it at the door.
>
> NASA lost a Mars orbiter this exact way in 1999. One team worked in pounds,
> the other in newtons, nobody caught it, and a 125-million-dollar spacecraft
> flew into the atmosphere and broke up. Here it is a two-line check that runs
> in about a millisecond.
>
> That is really the whole argument for doing this automatically. The check is
> trivial. Remembering to do it, on model number 300 of the day, at 6pm — that
> is the part humans are bad at."

**Point at the size numbers.** 39 MB down to 350 KB is **about 115 times
smaller**.

> "Same model. That is the difference between something you email and something
> that just opens."

**Why it exists.** You cannot run something you cannot see.

## 2. Jobs — "here's what went wrong"

**What they see.** Every model that went through, with its result. Click one for
the full story.

> "This is where you would go when someone says their model is not showing up.
>
> Open the HVAC one. The file was cut off partway through upload. The pipeline
> spotted that, and — this is the part I thought about most — it did **not** try
> again. Retrying a corrupt file cannot fix it, and it delays everything queued
> behind it. So it stops, gets set aside for a person, and everything else keeps
> moving.
>
> Compare that to a model that failed for a temporary reason. That one retried
> on its own and went through.
>
> Knowing the difference between 'try again' and 'stop and get help' is most of
> what keeps a queue moving.
>
> And the message says what is actually wrong, in English. Not a stack trace."

**Point at this — job #3, the one that fixed itself.**

`BOP-PIPE-U30`. Look at the attempt counter: it says **2**. It failed, waited,
tried again, and went through. Nobody was told, because nobody needed to be.

Then compare it to `NI1-HVAC-U10`, which stopped dead on attempt 1.

> "These two failed for completely different reasons and the pipeline treated
> them completely differently.
>
> Think about your wifi. If it drops, rebooting the router usually works — worth
> trying again. If your screen is cracked, rebooting does nothing. You need a
> person and a new screen.
>
> The first model was a dropped connection, so it retried and recovered on its
> own. The second was a file that got cut off halfway through upload — like a
> phone call ending mid-sentence. No amount of retrying invents the missing
> half. So it stopped immediately, got set aside for a human, and everything
> else in the queue kept moving.
>
> Getting that distinction wrong is how a queue quietly grinds to a halt — one
> broken file being retried forever while three hundred good ones wait behind
> it."

**Why it exists.** Cuts the time between "my model is missing" and knowing why.

## 3. Catalog — "here's what's live"

**What they see.** Every model, which version is live, how big, how old. Then
the blocked ones.

> "Simple question this answers: for any model, what is live right now and when
> did it go live.
>
> Versions are tracked, so if a bad one gets through there is a one-line command
> to put the previous version back.
>
> The list underneath is the one I would care about. These models exist,
> engineering thinks they were delivered, and they are not live. Without this
> list nobody finds out until a user complains."

**Point at this — `TB2-PIPE-U20`, the model with two versions.**

This is the one that explains the 9-versus-8 thing. It was submitted twice, as
RevA and RevB. Both processed fine. RevB is live; RevA is still there, just not
the one being served.

> "This is the undo button. If someone opens RevB tomorrow and says the pipe run
> is wrong, one command puts RevA back and it is live again in about a second.
> The old version was never deleted, it was just moved out of the way.
>
> Without this, 'we published a bad model' means finding the old file, hoping
> someone still has it, and redoing the whole thing under pressure."

**Point at the blocked list underneath.**

> "These five are the ones I would actually worry about. Engineering finished
> them. Somebody thinks they were delivered. And they are not live.
>
> That is the failure I would least like to have — not a loud one, a quiet one.
> Nobody finds out until a user goes looking for something that was never
> there."

**Why it exists.** Stops models going quietly stale.

## 4. Viewer — "here's the output"

**What they see.** A 3D plant model in the browser. Parts list on the left,
details of whatever you click on the right.

> "Everything so far has been numbers in a table. This opens the actual file the
> pipeline produced.
>
> This went in as an engineering file and came out as something that opens in a
> browser in under a second. Nobody touched it in between.
>
> Click a part. The panel on the right fills in — the tag, what it is, what it
> is made of. That information is not inside the 3D file. It is in a database,
> and the pipeline put it there. That is the bit I think matters: you can search
> this model, not just look at it.
>
> This dropdown changes the detail level. Lower detail is a lighter version of
> the same model, for when you are on a laptop or a tablet. The parts list does
> not change — you can still find everything."

**Point at this — component `20-C-1001`.**

Load the steel model (`NI1-STEEL-U10`, the biggest one, 3,215 parts) and click
that column. The panel fills in with real engineering data:

| | |
|---|---|
| Section | W14x90 |
| Material | A992-GR50 |
| Weight | 2,171.6 kg |
| Surface treatment | Galvanised |
| **Erection sequence** | **5** |

> "My favourite one is the last row. This column weighs two and a bit tonnes,
> and the model knows it is the fifth thing you put up.
>
> None of that is in the 3D file. It is in a database, and the pipeline put it
> there on the way through. That is the difference between a picture of a plant
> and something you can actually plan work from — you can ask 'show me
> everything erected in week 5' instead of just looking at it."

**Point at the draw-call number in the corner.**

> "A plant is the same valve, the same flange, the same bolt, thousands of times
> over. So rather than storing every copy, this stores the shape once and a list
> of where to stamp it — like a stencil. That is where most of the size saving
> comes from. It is not clever compression, it is just not repeating yourself."

**Point at the detail-level dropdown.**

> "Same idea as zooming out on Google Maps. When you are looking at the whole
> plant you do not need every bolt modelled — you need it to move smoothly.
> Zoom in and the detail comes back. The parts list never changes, so you can
> still find anything at any level."

**Why it exists.** Proves the output is real and usable, not just a file size in
a report.

**Do not oversell this one.** See the note at the end of this document.

## 5. SQL — "here's the data underneath"

**What they see.** A query box with a few saved questions.

> "The job description mentions SQL, so rather than say I know it I made the
> whole thing queryable.
>
> Run this saved one — 'attribute completeness'. It answers: across everything
> live, how much of the engineering information is actually filled in? That is a
> real question someone would ask, and it is answerable because the pipeline
> puts that information in a database as it goes.
>
> It is read-only, twice over. It only accepts a SELECT, and the connection
> cannot write. A demo that can delete its own data is not a demo."

**Point at this — the gap the query finds.**

Run "Required-attribute completeness, by discipline". Four fields are supposed
to be on every single component. Top row of the results:

> "Equipment is at 90%. Fifty components out of five hundred are missing three
> fields they are all supposed to have. Structural is at 99.8%. Everything else
> is clean.
>
> So if you asked me 'which team needs a nudge about tagging discipline', the
> answer is the equipment team, and I did not need to ask anybody — that took
> one query and about ten milliseconds.
>
> Right now I would guess finding that out means opening models one at a time
> and looking."

**Point at this — the duplicate tags.**

The `TB2-STEEL-U20` model was rejected for having six tags used twice, including
`20-B-3007` and `20-C-1010`.

> "Two different steel members, same tag. It is two houses on the same street
> with the same house number — the post arrives and there is no way to know
> which one it belongs to.
>
> Everything downstream looks parts up by tag. So the moment a tag is ambiguous,
> the attributes attach to the wrong thing. That model got stopped, and I think
> correctly — it would have looked completely fine in a viewer while quietly
> giving people wrong information."

**Why it exists.** Turns reporting questions into a query instead of a request
to a developer.

*(There is a sixth page, "How it works," with all the settings and the design
detail. Only open it if they ask.)*

---

# Questions you will get

Answer short. Do not oversell.

**"Where did these models come from?"**
> "I generated them. I did not have real plant files, and I would not put
> anything from a previous employer in a demo anyway. I made the generator
> produce deliberately messy geometry, because I read that real exports are
> messy — if I had generated clean files the optimisation step would have had
> nothing to do and the numbers would have been meaningless."

**"How would this work with Navisworks or our actual formats?"**
> "Honestly — I do not know those formats, so I did not pretend to. Only the
> first step knows what kind of file it is reading; everything after that works
> off one common format. So supporting a new file type means writing one small
> piece, not changing the pipeline. Whether that holds up against a real
> Navisworks export is exactly the kind of thing I would want you to tell me."

**"Could this handle hundreds a day?"**
> "This version, no. It stores everything in a single file database, which is
> fine for a demo and wrong for real volume. It does process models in parallel,
> and it is built so swapping in a proper database is a contained change — but I
> would not claim it is production-ready. It is a weekend proof of concept."

**"How long did this take?"**
> "A weekend. Which is the point — I wanted to show I had understood the problem
> well enough to build something around it, not to hand you finished software."

**"What would you do next?"**
> "Support real file formats. Move to a proper database. And then the one I
> think is most interesting: when a model gets updated, only publish what
> actually changed instead of the whole thing again. At a few hundred models a
> day that seems like it would matter."

**If you do not know something — say so.**
> "I don't know. I have not worked with that. How do you handle it today?"

That answer is better than a guess. You are applying as someone who is close to
this, not someone who has already done it.

---

# One warning: do not compare the viewer to theirs

The 3D tab is the most impressive-looking part of the demo and the weakest part
of the argument. Their team already has a plant viewer. They are not hiring
someone to build one.

So never say "this is like UniversalPlantViewer." You will be compared against a
mature commercial product by people who use it daily, and you will lose that
comparison on every axis — clash detection, measurement, markup, section planes,
saved viewpoints, document links, huge-model streaming. None of that is here.

Say this instead if it comes up:

> "This is not meant to be a viewer — you already have one. I only built enough
> of one to prove the file the pipeline produces actually opens and that the
> part information is attached to it correctly. If this were real, the last step
> would hand off to your viewer instead."

That reframes the 3D tab from *a product I am pitching* to *a test I wrote for my
own output*, which is both more honest and a stronger position.
