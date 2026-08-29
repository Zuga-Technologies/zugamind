"""Socratic reflection: what the mind does with an idle cycle.

Three stages -- classify the trigger's domain, ask one grounded question
about it, resolve that question against a real source:

    domain_classifier.classify_domain(trigger)
        -> question_generator.generate_question(trigger, domain, grounding)
            -> answer_router.answer_question(text, answer_source_hint)

`engine.reflect_once()` is the orchestrator the runner calls on the
REFLECTING transition; the three stages stay independently callable.

This lived in examples/ as reference code that nothing called (issue #4).
It was parked there for one concrete reason: wiring it adds local-model
calls to every REFLECTING cycle, and BugaPC's dogfood deployment was mid
the EXP-005 observation window, which a runner behaviour change would have
contaminated. That window closed 2026-07-20 (see
docs/experiments/exp-005-value-of-wakes.md), so the reason expired and the
code moved back into the product on 2026-08-29.
"""
