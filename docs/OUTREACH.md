# Teams message

The interview is already booked, so this is no longer a cold outreach. Its job
is narrower: get the link in front of them beforehand so the call is a
conversation about the thing rather than a live discovery of it.

## Send this

> Hi Mathew, hi Amy, looking forward to Thursday.
>
> Ahead of it, here is a small proof of concept I built on my own time around
> one problem I think sits under the role: work packaging and handover both
> depend on model attribute integrity, and that tends to be assumed rather than
> measured. So this automates the path from engineering export to viewer, but
> refuses to publish a model whose attributes are not good enough, and then runs
> work package readiness and CDE feeds off the same data to show what that is
> worth.
>
> [link]
>
> Two notes so nothing is misleading. Everything in it is generated synthetic
> data, no Kiewit or client models, and several of those models fail on purpose
> so you can see what the pipeline does with a bad one rather than only the
> happy path. It also runs locally by design, no cloud services at runtime.
>
> It is hosted on a free tier that sleeps after 15 minutes, so give it up to a
> minute to wake up on first load.
>
> Happy to walk through it on the call, or happy to skip it entirely if you have
> other ground to cover.

## Why it is worded that way

**"on my own time"** heads off the obvious internal question before it is asked.
You are a Kiewit employee building something adjacent to a Kiewit product; say
where the time came from and it is a non-issue.

**"one problem I think sits under the role"** rather than *the problem the job
description describes*. The JD does not state this problem explicitly, it is
your reading of it. Asserting otherwise invites a correction in the first two
minutes.

**The thesis sentence is the same one you open the demo with.** If the message
frames the work one way and the walkthrough leads with another, the first two
minutes of the call are spent reconciling them. Keep this paragraph and the
opening of [DEMO_SCRIPT.md](DEMO_SCRIPT.md) saying the same thing.

**The failures are declared up front.** Mathew will click into the failed jobs
within a minute either way. Naming them first turns a possible "your demo is
broken" into "the failures are the interesting part," which is the actual
argument.

**"happy to skip it"** matters. They own the agenda. Offering to drop it costs
nothing and avoids reading as though you are trying to run their interview.

## Do not

- Do not compare it to UniversalPlantViewer. Mathew owns UPV.
- Do not call it an MVP or a platform. It is a proof of concept.
- Do not send it the morning of. Two or three days ahead gives them the option
  to look, without it feeling like homework due tomorrow.
