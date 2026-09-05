# Putting the brain on the server

Written for whoever does the deploy, including a session that has a shell and
this one does not. Everything here is a button or one line of `.env`; nothing
needs a code change.

## What it is, and what it is not

It is a **bandit**, not a neural network. About 480 numbers — twenty model
families times eight recipes times (wins, losses, samples) — held in memory and
flushed to the `settings` table on the existing autosave. There are no weights,
no gradients and **no training step**: it fills from the group's own traffic.
"Deploying" it is turning a switch on and waiting.

What it decides is one thing: **which prompt weight this model family answers
to.** The full prompt is ~4,600 tokens over 52 rules, the compact one ~1,080,
and the tight one ~295. Which of them a model can actually hold is a fact about
that model, and it is what the bot has never known.

## Before

```
BRAIN=0            # the default, and what production is running now
BRAIN_WRITES=0     # stays 0 for this rollout
PROMPT_TIER=auto   # or tight, if that is what is being tested by hand
```

With `BRAIN=0` the rendered prompt is byte for byte what it has always been, and
a test asserts it. Anything that goes wrong from here is the switch, not the
merge.

## The rollout

1. **Update and confirm nothing moved.** Run for an hour on `BRAIN=0`. Take
   **panel → data → 🩺 diagnostics**. The brain section should read
   `selecting off` with arms already accumulating — it learns with the switch
   off, which is why turning it on is not starting from nothing.
2. **Turn it on** in **panel → 🧩 brain → selecting on**. Stored, so it survives
   a restart.
3. **Watch for a day.** Two screens tell you everything: **panel → 🧩 brain**
   (which recipe each family runs, on how much evidence) and
   **panel → data → 🩺 diagnostics** (the same, plus what each model produced).

## What "working" looks like

- Families below 30 samples say `still watching` and run the factory recipe.
- Above it, one recipe pulls ahead and the `mean` column separates.
- About one turn in ten goes off the winner on purpose; half of those go to the
  factory recipe, because that is what the breaker measures everything against.
- `command-r7b` picking `tight` over `compact` would be the hypothesis
  confirmed. `tight` losing would be it refuted, which is just as useful.

## What "going wrong" looks like, and the button for it

| symptom | button |
|---|---|
| replies got worse since the switch | **🧩 brain → back to factory** — every family reverts, counters kept |
| a family looks stuck on something bad | it says `sent home by the breaker`; leave it, that is the breaker working |
| you want the whole thing gone | **🧩 brain → forget everything**, then **selecting off** |
| you want today back and nothing else | **back to factory** is enough; it is a pause, not an erasure |

`BRAIN=0` in the `.env` and a restart is always the floor: it is the code path
production runs today.

## What it can never do

The locked layers are Python constants, emitted on every render, and a recipe
can only choose among *mutable* ones — voice, mood, how many examples. If the
whole recipe store were emptied the prompt would still carry every safety rule,
and there is a test per locked layer that a render losing one is refused.

Free mode never explores the layered prompt: a free model is a small model, and
spending one turn in ten on a 4,600-token prompt is the failure this exists to
fix.

## The one thing to send back

**panel → data → 🩺 diagnostics**, as a file. It carries no credentials, no chat
text and nobody's name, so it can be pasted anywhere — and it has the brain's
whole state in it.
