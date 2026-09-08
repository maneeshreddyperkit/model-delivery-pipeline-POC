# About this pipeline

This application takes engineering models from design tools through to a 3D
viewer automatically, and refuses to publish any model whose data is not
good enough to be used. It is a working proof of concept: the conversions
are real, the quality gates are real, and the fleet-scale history behind
the dashboard is simulated and labelled as such.

## What problem it solves

Engineering models are produced continuously by design teams and consumed
continuously by everyone else: planners, superintendents, commissioning,
cost. Getting a model from the design tool to the people who need it means
converting it, checking it, shrinking it enough to open in a browser, and
publishing it, and doing that for every revision of every model on every
project without a person in the loop.

The part that is usually missing is the check. It is straightforward to
automate a conversion. It is harder to automate the judgement of whether
the model that came out the other end is fit to be used, and if you skip
that, the automation becomes a fast way to distribute bad data.

So the pipeline is built around a quality gate, and the rest of the
application exists to show what that gate is worth.

## The argument it makes

Work packaging, commissioning and progress tracking all depend on the
attributes carried by the 3D model being complete and consistent. That
dependency is usually assumed rather than measured. This pipeline measures
it at the gate and then spends it: the same attributes that pass or fail a
model are what drive the work package readiness verdicts and the handover
feeds.

The sharpest version of the argument is on the Work Packages page. A model
can pass an aggregate attribute check and still contain a handful of
components with nothing recorded against them, and if those components
happen to fall inside one installation work package, that package cannot be
planned, costed or reported against even though nothing physical is
stopping it.

## The Operations page

The state of the pipeline: throughput, how long each stage takes, what
failed and why, and how the delivered payload compares to a naive
conversion of the same models.

## The Jobs page

Per-run detail. Every job has a stage-by-stage trace with timings, and
failures carry a classified error rather than a stack trace. You can switch
between the jobs this machine actually ran and the whole simulated fleet.

## The Catalog page

What is currently live and what is blocked, with the reason for each block.

## The Work packages page

Applies four readiness constraints to every installation work package:
engineering issued, materials delivered, the preceding package installed,
and model attributes complete. Three of those describe the physical world.
The fourth describes the data, and it is the only one this pipeline can
guarantee on its own. Any package can be opened in the 3D viewer with the
rest of the plant ghosted around it.

## The Handover page

What a Common Data Environment would consume: structured feeds for cost,
planning and completions systems, attribute completeness per discipline,
and the list of components that a receiving system would reject. The feeds
are generated on request from the live catalog, so a download is never a
stale export.

## The Viewer page

Opens the actual published file. It can colour the model by install status,
by package readiness, by discipline, by work package or by commissioning
system, and it has a 4D scrubber that plays planned install dates against
the status each component actually reports, so anything due but not
installed lights up.

## The SQL page

The catalog itself, queryable, read-only.

## How a model gets through

Seven stages, in order: intake, validate, extract, convert, optimise, QA,
publish. Intake registers the file and makes a re-drop of an identical file
a no-op. Validate checks the manifest and the tag conventions. Extract
pulls the component tree and its attributes into the catalog. Convert
produces glTF. Optimise welds vertices, deduplicates repeated geometry into
instances and generates levels of detail. QA compares the output against
the input on bounding box, volume, tag coverage and attribute coverage.
Publish writes the delivery folder and points the viewer at it.

A model that fails QA is not published, and the previous revision stays
live. That is the whole point: the failure mode of this pipeline is that a
model does not update, not that a bad model reaches 3,000 people.

## What is real and what is simulated

The models are procedurally generated. No Kiewit or client data is used
anywhere in this system.

Nine models are genuinely processed by this machine: read, validated,
converted, optimised, quality-gated and published, with every timing
measured rather than invented. The fleet-scale history behind the
dashboard, which puts the numbers at the order of magnitude the real
problem operates at, is backfilled and flagged as simulated in the
database. Any page that mixes the two says which is which, and the Jobs and
Catalog pages let you switch between measured and fleet scope.

Defects in the models are deliberate. A pipeline demonstrated only on
models that pass tells you nothing about the part that matters.

## Where it runs

Entirely on the machine serving it. SQLite for the catalog, Flask for the
web application, three.js vendored locally for the viewer, and the language
model, when one is installed, runs locally through Ollama. Nothing in the
running system makes a network call off the machine.
