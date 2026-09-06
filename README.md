# Statute

**Natural language policy, compiled once under consensus into an on chain
decision table, then decided deterministically forever.**

Live on Testnet Bradbury at
[`0x54378836847a768Db56918E8Fa38149c7db780c1`](https://explorer-bradbury.genlayer.com/address/0x54378836847a768Db56918E8Fa38149c7db780c1)

This is the revised contract. The first submission was rejected for a real
consensus defect, and what changed is set out immediately below.

---

## Revision after review

The first submission of this contract was rejected, and the review was right.
It is quoted here in full because the fix is best judged against it:

> The policy compiler is substantial, but its behavioral consensus can miss a
> real decision difference. Integer boundary candidates are thinned to 12
> values, so with several thresholds an adjacent `<50` versus `<51` difference
> can lose both 50 and 51, pass every retained probe and dead-rule check, yet
> decide `x=50` differently. Please make agreement exhaustive over the declared
> canonical domain or use a proof-preserving interval/decision-diagram
> comparison that cannot discard behavior-changing boundaries.

That was reproducible. With eight thresholds declared, the thinning kept 49 and
51 and dropped 50, and two tables that genuinely decide `x = 50` differently
agreed. `tests/test_deterministic.py::RejectionRegression` builds exactly that
pair and now fails agreement, and sweeps every adjacent threshold pair besides.

**What changed.** Agreement is now exhaustive over the declared canonical
domain, the first of the two options offered. `_shrink` and `boundary_probes`
are gone; there is no sampling step left in the contract to get wrong.

**A second hole, found while fixing the first, and closed with it.** Agreement
being exhaustive over the domain is only worth anything if the domain is
everything the contract will decide. It was not. `evaluate` accepted any
integer within `lo..hi` while the canonical grid visited only the declared
`step` points, so with `age` at step 10 the tables `age < 18` and `age < 15`
carried the **same behavioural fingerprint** and `diff` reported no change,
while `evaluate` cheerfully accepted `age = 16` and decided it differently.
That falsified the headline claim of this README. `evaluate` now refuses values
between declared points with `FACT_OFF_GRID`, so the set it accepts and the set
consensus checks are one set. The old test that pinned this as a documented
limitation has been replaced by one that pins its removal.

**Cost.** The worst case check is `MAX_GRID` points, which is 4096 and is
enforced at schema parse time. The old bound was 20000 probe points, so this is
strictly less work as well as strictly more coverage.

---

## The problem

Putting a model in the decision seat is expensive and unrepeatable. Every
evaluation pays for inference, every evaluation can drift, and nobody can prove
afterwards why a particular case was decided the way it was. That is a poor fit
for the thing policies exist to do, which is decide the same case the same way
twice.

Statute moves the model out of the decision seat. The fuzzy step runs exactly
once, at compile time, and what it returns is not a verdict. It is a decision
table: an ordered list of conjunctive rules over a declared fact schema, plus a
default. Consensus is reached on that table. From then on `evaluate` is a plain
deterministic view.

No model. No inference cost. No drift. Anyone can replay every decision the
contract has ever made from the stored table alone.

## How consensus is used

There is exactly one nondeterministic entry point, `compile_policy`. Everything
else is ordinary deterministic code.

### Validators agree on behaviour, not on text

Two correct compilations of the same policy will almost never be textually
equal. "deny when age < 18, otherwise approve" and "approve when age >= 18,
otherwise deny" are the same policy written the other way round. A validator
that diffed the JSON would reject both honest peers forever.

So validators do not compare tables. Each validator compiles the policy itself,
then compares what the two tables **decide** at every point of the canonical
domain:

| Fact kind | Values in the domain |
| --- | --- |
| `int` | every point the declared `lo:hi:step` visits, ending on `hi` |
| `enum` | every declared member |
| `bool` | both values |

Nothing is sampled and nothing is thinned. The check visits the entire product,
which by construction is exactly the set of fact combinations `evaluate` will
ever accept, so two tables that agree cannot decide any admissible case
differently. That is a proof, not a heuristic.

What makes it affordable is that the schema declares a **finite** domain and
`parse_schema` refuses any schema whose domain exceeds `MAX_GRID`. A policy
whose behaviour this contract could not enumerate in full is rejected before it
is ever compiled, rather than approximated afterwards.

This is the lesson from an earlier contract of ours, stated as a rule: **validate
the decision the contract will actually store, not the intermediate reading that
produced it.**

### Two more checks, because a table can be behaviourally right and still be junk

| Check | What it refuses |
| --- | --- |
| Closed vocabulary | an atom naming an undeclared fact, an operator illegal for that fact's kind (ordering on an enum), a value of the wrong type, an outcome outside the declared set, a rule with no atoms, a missing default |
| No dead rules | a rule no admissible fact combination can reach, because earlier rules already cover it. Checked over the whole domain, so a rule reported dead really is unreachable. A dead rule means the compiler was sloppy, and a sloppy table is a bad thing to freeze into storage |

The dead rule check runs on **both** tables, so the rule is symmetric: junk is
refused whichever side produced it, and refusing rotates the leader rather than
freezing bad state.

### Error classification

Deterministic errors carry `[EXPECTED]` and must match exactly between leader
and validator. Model errors carry `[LLM_ERROR]` and always make validators
disagree, which forces a leader rotation instead of locking a broken table into
storage.

## What you get that an off chain model call cannot give you

The canonical grid is fixed by the schema alone, and it is the whole of what
`evaluate` accepts, so every compiled version has a stable **behavioural
fingerprint** that covers every case the contract can be asked. That makes `diff` possible: when a
policy is amended, the contract reports, deterministically and on chain, exactly
which fact combinations change outcome.

"We reworded clause 4" and "we changed who gets approved" become
distinguishable. That is the question anyone governing by policy actually needs
answered, and a text diff cannot answer it.

## Worked example, live on chain

Schema and outcomes, set at construction:

```
requested_gen:int:0:10000:500
track:enum:app,content,contract,infra
prior_accepted:int:0:10:1
kyc:bool

outcomes: auto_approve, committee_review, reject
```

The policy text compiled into version 0:

> Applications without completed KYC are rejected. Content track applications
> are never approved automatically, they always go to the committee. A team that
> already has three or more accepted contributions and has completed KYC is
> approved automatically when it requests 2000 GEN or less. Any request above
> 5000 GEN goes to the committee. Everything else goes to the committee.

What consensus agreed on and stored, read back from the live contract:

```
0. IF kyc == false                                                 -> reject
1. IF track == content                                             -> committee_review
2. IF prior_accepted >= 3 AND kyc == true AND requested_gen <= 2000 -> auto_approve
3. IF requested_gen > 5000                                         -> committee_review
   default                                                         -> committee_review
```

Behavioural fingerprint: `2165931a275a8437f38050a5b8552cba6dd1346c762ede8b1a9606c7195b831f`

The same fingerprint came back from the deployment made **after** the agreement
check was rewritten (`0x54378836847a768Db56918E8Fa38149c7db780c1`), compiling the same policy
from scratch through a fresh consensus round. The rewrite closed two holes
without moving a single on-grid decision, which is the strongest evidence
available that the fix is a fix and not a rewrite of the behaviour.

Notice how lean the rules are. Rule 1 carries no `kyc == true`, and rule 2 no
`track != content`, because earlier rules already shadow those cases. The
compilation is minimal rather than literal, and it is still exactly right.

### The claim, demonstrated across three independent deployments

An earlier deployment of this same contract and policy
(`0xd04c71A33eE301Cce192918a6755aA60B8ECEEEc`) compiled a **textually different**
table. Its rule 2 spelled the track condition out:

```
2. IF kyc == true AND track != content AND prior_accepted >= 3
        AND requested_gen <= 2000                                  -> auto_approve
```

Its behavioural fingerprint is
`2165931a275a8437f38050a5b8552cba6dd1346c762ede8b1a9606c7195b831f`.

The same value. Two separate compilations, two different tables, one identical
behaviour. That is the property the agreement rule exists to recognise, observed
on chain rather than argued for.

`evaluate`, with no model involved:

| Facts | Outcome |
| --- | --- |
| no KYC, contract, 5 prior, 1500 | `reject` |
| KYC, contract, 5 prior, 1500 | `auto_approve` |
| KYC, content, 5 prior, 1500 | `committee_review` |
| KYC, contract, 1 prior, 1000 | `committee_review` |
| KYC, infra, 9 prior, 8000 | `committee_review` |

`explain` returns the rule that fired and the atoms that had to hold:

```json
{"matched_rule": 2, "outcome": "auto_approve", "version": 0,
 "reason": [{"fact": "prior_accepted", "op": ">=", "value": "3"},
            {"fact": "kyc", "op": "==", "value": "true"},
            {"fact": "requested_gen", "op": "<=", "value": "2000"}]}
```

### The diff

Version 1 is the same policy with the automatic approval ceiling raised from
2000 GEN to 3000 GEN. `diff(0, 1)` reports **48 changed fact combinations**:

```
requested_gen  : 2500, 3000
track          : app, contract, infra          (content correctly excluded)
prior_accepted : 3, 4, 5, 6, 7, 8, 9, 10
kyc            : true
direction      : committee_review -> auto_approve
```

3 tracks x 8 prior values x 2 request sizes = 48. The amendment stopped being a
sentence and became a list of cases.

## API

| Method | Kind | Purpose |
| --- | --- | --- |
| `compile_policy(policy)` | write, nondet | the only model call. Compiles, reaches consensus on behaviour, appends and activates a version |
| `evaluate(facts)` | view | the outcome. Pure deterministic |
| `explain(facts)` | view | outcome, matched rule index, and the atoms that held |
| `diff(before, after)` | view | fact combinations whose outcome changed between two versions |
| `record_decision(id, facts)` | write | deterministic. Writes an audit entry, no model |
| `activate(version)` | write | owner only |
| `get_version(index)` | view | policy hash, behavioural fingerprint, compiler, full table |
| `get_decision(id)` | view | a recorded decision |
| `version_count()` | view | how many versions exist |

Facts may be sent as a JSON string or as an already decoded mapping. The CLI
decodes a `{...}` argument before it reaches the contract, so refusing one shape
would be an ergonomics bug. Every value is validated either way.

The fact schema accepts `;` as well as newlines as a separator, so a whole
schema can travel as one argument through any ABI or command line.

## State

| Field | Type | Holds |
| --- | --- | --- |
| `facts_spec`, `outcomes_spec` | `str` | the declared vocabulary, fixed at construction |
| `versions` | `DynArray[Version]` | every compiled policy |
| `active` | `u32` | which version `evaluate` uses |
| `decisions`, `decision_ids` | `TreeMap` + `DynArray` | the audit log |
| `owner` | `Address` | may call `activate` |

A `Version` stores the policy text hash, the default outcome, the behavioural
fingerprint, the compiler's address, and the agreed table serialised with
`sort_keys`. That serialisation is a canonical form, so two nodes that agreed
write byte identical state.

A nested `DynArray` of rule structs would read more nicely. It does not survive
this runner: three separate construction variants all failed on chain with
nothing persisted. That was settled by a controlled experiment, and the probe
contract and its result are recorded in [docs/DEPLOY.md](docs/DEPLOY.md).

## Tests

50 deterministic unit tests, no network and no model:

```bash
python -m unittest discover -s tests -v
```

Everything that decides anything is a module level pure function, so it is all
reachable from tests. That placement is deliberate. In an earlier contract the
agreement rule lived inside a closure, out of reach of tests, and a consensus
defect shipped because of it. Nothing that votes hides in a closure any more.

The two tests that matter most are a matched pair:

- two textually different tables that decide identically **must** agree
- two tables differing at a single boundary point **must not** agree

and one property test over 225 table pairs asserts the thing the primitive
claims: if two tables agree, they decide identically at **every** point of the
canonical domain.

A dedicated `RejectionRegression` class pins the defect this contract was
rejected for the first time it was submitted, described below. It checks the
reported `<50` versus `<51` pair, checks that neither table has a dead rule so
the disagreement has to be caught on outcomes rather than by accident, and then
sweeps **every** adjacent threshold pair from 20 to 100.

## Honest limitations

- **The schema's declared step is the resolution of everything.** With `age`
  declared at step 10 the contract decides ages 0, 10, 20 and so on, and
  `evaluate` **refuses** age 16 with `FACT_OFF_GRID` rather than deciding a case
  it never enumerated. Declare `step 1` when a policy turns on single units.
  This is a real constraint on the caller, and it is the price of the agreement
  check being a proof.
- **A schema whose domain exceeds `MAX_GRID` is refused outright.** `age` at
  step 1 over 0 to 100 with a three member enum and a boolean is 606 points and
  fine; an unbounded income range is not. Coarsen the step or narrow the range.
  The contract will not accept a policy whose behaviour it cannot enumerate.
- **The grammar is deliberately small**: conjunctions of atoms, first match
  wins. No disjunction inside a rule, no arithmetic between facts. Disjunction
  is expressible as separate rules. This is what keeps the domain finite and
  the equivalence check honest.
- **Compilation quality is still the model's.** Consensus proves that
  independent validators derived the same behaviour from the same text. It does
  not prove that behaviour is what the author meant. `diff` and `explain` exist
  so a human can check that cheaply.
- **Bradbury is intermittent.** Two `compile_policy` calls in this session came
  back `LEADER_TIMEOUT` and one reverted at the consensus contract before
  reaching the contract at all. Retrying landed them. The transaction records
  are in [docs/DEPLOY.md](docs/DEPLOY.md).

## Layout

```
contracts/statute.py          the contract
tests/test_deterministic.py   50 unit tests
tests/_stub.py                minimal SDK stub so the module imports under CPython
docs/DEPLOY.md                deployment record, and the storage experiment
docs/example-policy.txt       the policy compiled into version 0
docs/example-policy-v2.txt    the amendment compiled into version 1
docs/probe_storage.py         throwaway probe that settled the storage shape
```

## Licence

MIT.
