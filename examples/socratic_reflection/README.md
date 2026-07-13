# Socratic reflection layer (reference example)

A self-contained three-stage pipeline, originally built for the idle
`REFLECTING` cognitive state:

```
domain_classifier.classify_domain(trigger)
    -> question_generator.generate_question(trigger, domain)
        -> answer_router.answer_question(question_text, answer_source_hint)
```

1. **`domain_classifier.py`** — maps a trigger to `SELF` / `OPERATIONAL` /
   `EXTERNAL` (lens routing → keyword pre-filter → local-model fallback).
2. **`question_generator.py`** — asks the local model for one grounded,
   Socratic question about the trigger, with an `answer_source_hint`.
3. **`answer_router.py`** — resolves that hint to a real answer: `code_search`
   is a live `git grep`/`grep` over the repo, `file_read` is a stub, `none`
   is a no-op.

## Why this lives in `examples/`, not the product

This code is complete and was covered by its own unit tests when it lived
under `zugamind/cognition/workspace/` — but nothing in `stream/runner.py`
ever called it (tracked as
[issue #4](https://github.com/Zuga-Technologies/zugamind/issues/4)). The
natural wiring point is the `REFLECTING` idle state, where the runner
currently does nothing beyond the state transition.

It was moved here instead of wired in directly because wiring it changes the
live runner's idle-cycle behavior — extra local-model calls on every
`REFLECTING` cycle — and this repo currently has BugaPC's `bugapc-claude-
observer` deployment mid an observational experiment (EXP-005, "value of
wakes") that is deliberately not touched until its window closes. Landing a
runner behavior change mid-window would contaminate that measurement, so the
safe move now is: keep the code real, tested, and documented, don't flip it
on for a live deployment being scored.

## How to wire it in

```python
# in StreamRunner._transition_state, on the REFLECTING transition:
from examples.socratic_reflection.domain_classifier import classify_domain
from examples.socratic_reflection.question_generator import generate_question
from examples.socratic_reflection.answer_router import answer_question

domain = classify_domain(trigger)["domain"]
question = generate_question(trigger, domain)
if question:
    answer = answer_question(question["text"], question["answer_source_hint"])
```

Add a journal event for the question/answer pair, and consider gating it
behind its own idle-cycle cadence rather than every `REFLECTING` cycle.

## Running the example's tests

```
pytest tests/examples/test_socratic_reflection.py -q
```
