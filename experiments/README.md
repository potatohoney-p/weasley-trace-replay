# Synthetic replay experiment

Run `python -m demos.experiment` from the checkout. The checked-in `result.json`
is the output of that command. Every run is deterministic and makes zero network
calls. Tests independently cover CLI exit codes, partial runs, JSON corruption,
cross-actor tool attribution, usage bounds and invalid terminal transitions.

Two actors each produce a start, tool call, tool result, checkpoint and step end.
There are `10 choose 5 = 252` order-preserving interleavings. All must have the
same observed state even though the exact trace hashes differ. Three controlled
mutations alter a tool result digest, checkpoint digest, or tool status; the
comparison must identify each. A duplicate tool-result mutation must be rejected.
The campaign also interrupts at all 11 nonempty prefixes before run completion.
Every prefix must remain incomplete, and replay after appending the remaining
events must exactly match the uninterrupted state and trace hash.

This demonstrates a differential oracle for recorded lifecycle evidence. It
does not establish correctness for arbitrary agent workflows, measure LLM
quality, or benchmark throughput. A next experiment should generate more actors,
random valid causal graphs and adversarial prefixes with a fixed recorded seed.

## Seeded causal-graph campaign

See [the independent oracle and failure-reduction design](causal-campaign.md)
and [the saved deterministic report](causal-result.json). This supplements the
exhaustive two-actor experiment; it does not replace it or claim exhaustive
coverage of arbitrary concurrent executions.
