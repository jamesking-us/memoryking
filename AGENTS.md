# MemoryKing contributor instructions

This is the product repository. Keep documentation focused on current features, installation, integration, configuration, design and operating limits. The README must remain understandable to nontechnical readers. Do not add internal phase diaries, personal paths, report-delivery credentials or raw development archives here.

Use the pinned Python and Node environments. Keep domain code independent of adapters/vendors. Commit generated schema/type/browser artifacts with their authoritative sources. Run `python tools/check.py` with the project Python, then use `tools/local_ci.py` at an immutable commit for final clean-checkout evidence. Jobs run locally; do not activate hosted workflows or persistent runners.

Exercise examples before calling them runnable. Preserve security, history, authorization and recovery invariants. Distinguish implementation tests from production performance or real-task quality evidence. No model/provider spending, public publication, live routing or deployment is implied by a code change.

Never commit real databases, credentials, local user paths, raw private logs or personal automation settings. Keep the README concise; put operational detail in the five focused guides linked from it. Update the applicable guide when behavior changes.
