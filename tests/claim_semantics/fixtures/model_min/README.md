# model_min -- a labelled-synthetic Shen domain model for the Stage D checker's tests

Implements the interface the checker asks of a model in the delete-issue family
(atlas, step, effects, as-is effects, admissible-as-is, declared writes, the
divergence registry, the rule index) in the smallest form that is well formed.
The tests derive ill-formed mutants from it by text replacement.  It is not a
model of any application and is never used as a fact producer.
