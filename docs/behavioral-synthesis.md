# Behavioral synthesis

The experimental learner operates on finite, pure Boolean observations. A task supplies named Boolean inputs and an observed Boolean result. Its obligations apply only to the specified observation.

## What makes this DreamCoder-style?

DreamCoder learns two things that transfer to the next problem: **a language for expressing solutions** and **a policy for searching that language**. Its wake phase finds programs for tasks. During sleep it extracts reusable abstractions and trains a neural recognizer using solved and imagined tasks. Repeating the cycle lets later concepts build on earlier ones. [Ellis et al., DreamCoder](https://arxiv.org/abs/2006.08381)

Follow one operation through the fork to see which parts it implements. From the repository root, run:

```sh
python -m capcov reason run --operation invention-demo \
  --project examples/search-projects/invention.json --json > receipt.json
jq '.search | {inventions, synthetic_tasks: .recognition_policy.synthetic_tasks,
               baseline: .evaluation.baseline, assisted: .evaluation.assisted}' receipt.json
```

### Follow one invocation through the Python code

The caller supplies the measurement. For example, a test adapter could invoke a validator and project its errors to one Boolean, “did the required-field error occur?” In this public fixture the measurements are already in JSON. Running `reason run` reads them from disk. There is no application execution or LLM call during this search.

**1. Read the answer column and try expressions.** For sibling `a`, the eight rows have these columns (read each string left to right, one character per row):

```text
a_guard     01010101
a_right     00110011
a_left      00001111
want        01010111
```

`run_search` calls `boolean_search.search` with those rows and an empty library. Each column becomes a bit mask. Trying `a_guard` alone gives the wrong seventh answer. Trying `a_right AND a_left` gives `00000011`. OR that with `a_guard` and the result is `01010111`: all eight answers match. The enumerator reaches that expression on attempt 185 and saves its tree in `search.wake`. The target's answers play no part in this step.

**2. Turn repeated trees into a callable definition.** The same search on sibling `b` saves `b_guard OR (b_right AND b_left)`. In `learn_library`, Capcov walks the trees and substitutes positional parameters for field names. The two trees now have the same representation. Thirteen of the twenty solutions match this shape; the other seven fit a simpler two-field OR. Capcov tries rewriting each of the thirteen matches as a call. Under its size function the original corpus costs 172 units. Each replacement saves one unit, the function body costs ten, and declaring it costs two. The new total is 171, so Capcov keeps the definition. Its library entry stores both the body and the suffixes that repeatedly filled its parameters:

```text
invented_000(p0, p1, p2) = p0 OR (p1 AND p2)
p0 → guard      p1 → right      p2 → left
```

**3. Learn an ordering for later guesses.** In `recognition.sleep`, Capcov generates small programs using the primitive operators and the new function. For each program it creates its own examples by executing it across every Boolean assignment to two, three, or four inputs. It knows which operator produced each synthetic table because it constructed the program. It stores that label with a few measurements, such as the fraction of true answers and how often flipping an input changes an answer from false to true. At search time, similar tables vote for which operator to try first among expressions of equal cost. This changes the order of guesses. Every guess still has to match the target answers.

**4. Try the function on an unseen operation.** For the fresh target, the assisted enumerator tries the three individual fields and the constants false and true. Those five guesses fail. The library's suffixes match exactly one target field apiece, so guess six is `invented_000(target_guard, target_right, target_left)`. Capcov executes the saved body on the target's columns and compares the result with all eight target answers. They match. Here the learned call succeeds before the operator ranking is needed. With the original grammar, the target search has to assemble the AND and OR again.

The reported program is one Boolean predicate over the caller's features. For partially specified behavior, a separate step can take a caller-supplied reachable input, append the **opposite** of the fitted prediction as a temporary constraint, and run search again. If a second program fits, both satisfy every original answer; executing the distinguishing request or resolving product intent is now worthwhile. This particular eight-row fixture exhausts its three-Boolean domain and has no proposed probes.

**Which mechanism helped here?** With the fixture's bounds and example order, the primitive search took 185 attempts. Applying the learned ranking to primitives took 116. Giving the search the learned library took six attempts with either ranking: the bound call already fit before the enumerator needed to rank larger expressions. All four runs produced the same answers on the known cases. The example demonstrates transfer of a new instruction. Its attempt counts measure search work on this fixture, not port quality. [Enumerator](../src/capcov/boolean_search.py) · [Recognition policy](../src/capcov/recognition.py) · [End-to-end example test](../tests/test_search_examples.py)

**How far does the analogy go?** The fork performs one wake, compression, synthetic-training, and target-search cycle per operation with sibling training. Its programs are finite Boolean expressions over caller-supplied features. Its compression matches recurring syntax with renamed variables, and its recognizer uses similarity-weighted votes over hand-written task features. DreamCoder uses a richer program language, Bayesian library learning, a neural recognizer and repeated learning cycles. Reusing a frozen Capcov library transfers earlier work; it does not automatically consolidate new solutions into a growing hierarchy. [Search action](../src/capcov/search_action.py) · [Library learner](../src/capcov/synthesis.py)

**Where port quality enters.** Once a program fits every known requirement, Capcov can search for a second fitting program with the opposite prediction on a supplied reachable request. That disagreement gives the agent a concrete case to replay against PHP, Go, and an independent specification. A domain decision can then supply the desired output for the next search. This counterexample and intent workflow is Capcov's addition. The `reason run` path above exercises both library invention and synthetic recognition training; the standalone synthesis commands below expose narrower experiments.

## Invent a library from sibling workflows

    python -m capcov synthesize research.json --induce-training --library-out library.json --out report.json --max-cost 12 --max-terms 500000

Use a top-level `tasks` array of siblings containing `id` and `examples`. Each example is `{"input":{"a":true,"b":false},"output":true}`. With `--induce-training`, omit all training program ASTs. The wake search solves the sibling tasks from observations. Repeated variable-renamed syntax trees become parameterized definitions if they reduce the measured size of the rewritten corpus including the definition. Distinct suffixes shared by formal argument bindings supply a simple search prior when they uniquely match fields in a new target.

A program-free `target` with `train_examples` and `heldout_examples` runs an explicit sparse-prediction experiment. Baseline and library searches receive identical fitting evidence and budgets. Search selects the first fitting expression by grammar cost and deduplicates on observable outputs. A uniquely role-bound learned call is one grammar production. Both selected programs are checked by the reference interpreter before scoring. Saved reports include their ASTs, search attempts, stopping conditions and results. Supply `--emit-go policy.go` to emit the selected program.

## Fit a known contract and challenge it

    python -m capcov synthesize acceptance.json --library-in library.json --out result.json --max-cost 12 --max-terms 500000 --max-probes 32

For porting, give the target its full `examples` array and optional `reachable_inputs`. A probe is `{"id":"fixture-case","input":{"a":true,"b":false}}`. The producer owns reachability and independent adjudication. Keep the target operation and its aliases out of library invention. Search fits both baseline and assisted programs to every known answer. For each new projected input, it looks for another assisted program satisfying the entire contract and producing the opposite output. This produces a disagreement witness or a bounded search receipt. Previously covered feature vectors cannot distinguish programs in this DSL. Resolve raw witnesses against an independent oracle, retain exceptions and append the answers to the contract. A finite check only describes the exercised inputs and projection.

## Run an inventory

    python -m capcov synthesize-batch operations.json --out batch.json --max-families 100 --max-cost 9 --max-terms 50000

The input maps IDs under `operations` to `family` and Boolean `examples`. Per family, the final eligible ID in sorted order is isolated from invention. Search first solves the eligible siblings, then invents a library and fits the target's entire known contract. Inspect the budget stops, exclusions, actual call use and both fit results.

## Operation-scoped agent workflow

The implementation plan has three stages. Solve observation-only siblings and invent a reusable Boolean library. Fit the full target contract and search for reachable disagreements. Route the resulting obligation to replay or intent review and retain a revision-bound receipt.

Start with `capcov reason next --operation ID --project capcov.search.json --json`. Dispatch the returned action by its kind. A search action carries argv for `reason run`. A witness carries the concrete projected input, both predictions and a seed intent packet. Collect independent observations, attach the business sources and the packet under the operation's `intent`, then request review. Store the reviewer's cited response under `recommendation`. Record an independently accepted rule under `accepted` when available. A previously confirmed decision may be supplied directly.

The project's `target.examples` holds mandatory Boolean expectations for this projection. Keep descriptive PHP and Go observations in the intent packet while deciding which expectations apply. The search validates all examples plus accepted projected cases. For a richer observable, define a specific Boolean projection first. The caller supplies reachable probe inputs and is responsible for their reachability.

A replay action can carry argv supplied by the project. The orchestrator runs the adapter in its fixture environment. To check the result, attach `candidate_replay` with the operation's `revision`, an evidence `source` and `observations` of the form `{"input":{"field":true},"output":false}`. Rerun `reason next`. A changed revision invalidates the receipt. A passing projection covers the listed cases only. The operation can also specify a pinned intent packet without a synthesis target.

Search receipts live under `.capcov/search` next to the project by default. Pass `--state-dir` to share them between workers. Search resumes from completed receipts; an interrupted enumeration restarts from its declared bounds. Use `--emit-go PATH` to materialize a fitted candidate. See [the runnable projects](../examples/search-projects/README.md).

The sleep phase samples programs from the Boolean grammar including invented functions, executes bounded synthetic truth tables, and learns a non-neural production ranking from those tasks and solved siblings. Search compares features of the target's known examples to these training tasks. The ranking orders productions of the same cost; candidate acceptance still uses the interpreter on every example. The baseline uses the original grammar and ordering. Set `bounds.sleep_samples` (default 96, maximum 512) to control the number of generated tasks. The receipt includes the policy, library digest, wake results, and both searches.

For an existing whole-inventory Boolean projection in the `synthesize-batch` input format, set the project to `{"schema":"capcov/search-project/v1","revision":"<source revision>","corpus":"projection.json","max_siblings":32}`. Supply the census ID to `--operation`. Capcov selects siblings from the same family in stable ID order and reports how many fit the limit. Every selected target example is retained. With a corpus derived from incumbent observations, the fitted contract characterizes the incumbent. Product qualification requires a projection of the desired behavior.
