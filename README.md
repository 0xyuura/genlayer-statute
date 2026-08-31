# Statute

**Natural language policy, compiled once under consensus into an on chain
decision table, then decided deterministically forever.**

Live on Testnet Bradbury at
[`0xB7255423EaF0A021Df3551BEaaafDbC3bdaa1868`](https://explorer-bradbury.genlayer.com/)

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
then compares what the two tables **decide** on a probe set both sides derive
deterministically:

| Fact kind | Probe values |
| --- | --- |
| `int` | the declared bounds, plus `c-1`, `c`, `c+1` around every integer constant appearing in **either** table |
| `enum` | every declared member |
| `bool` | both values |

Boundary errors are the errors this class of compilation actually makes, so
that is where the probes are placed. The probe set is bounded by construction,
and shrinks deterministically if a schema and table pair would otherwise
produce too many points.

This is the lesson from an earlier contract of ours, stated as a rule: **validate
the decision the contract will actually store, not the intermediate reading that
produced it.**

### Two more checks, because a table can be behaviourally right and still be junk

| Check | What it refuses |
| --- | --- |
| Closed vocabulary | an atom naming an undeclared fact, an operator illegal for that fact's kind (ordering on an enum), a value of the wrong type, an outcome outside the declared set, a rule with no atoms, a missing default |
| No dead rules | a rule no probe point can ever reach, because earlier rules already cover it. A dead rule means the compiler was sloppy, and a sloppy table is a bad thing to freeze into storage |

The dead rule check runs on **both** tables, so the rule is symmetric: junk is
refused whichever side produced it, and refusing rotates the leader rather than
freezing bad state.

### Error classification

Deterministic errors carry `[EXPECTED]` and must match exactly between leader
and validator. Model errors carry `[LLM_ERROR]` and always make validators
disagree, which forces a leader rotation instead of locking a broken table into
storage.

## What you get that an off chain model call cannot give you

The canonical probe grid is fixed by the schema alone, so every compiled version
has a stable **behavioural fingerprint**. That makes `diff` possible: when a
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

Notice how lean the rules are. Rule 1 carries no `kyc == true`, and rule 2 no
`track != content`, because earlier rules already shadow those cases. The
compilation is minimal rather than literal, and it is still exactly right.

### The claim, demonstrated across two independent deployments

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
canonical grid, not just at the probed ones.

## Honest limitations

- **Fingerprint resolution is the schema's declared step.** With `age` declared
  at step 10, a threshold at 18 and one at 19 decide identically at every grid
  point, so they share a fingerprint and `diff` reports nothing between them.
  Declare a finer step when a policy really turns on single units. Consensus is
  unaffected, because agreement runs on boundary probes, which do carry
  `c-1, c, c+1`. This is pinned by a test rather than left to drift.
- **Dead rule detection is probe based.** A rule that is live only at a point no
  probe visits would not be caught. The probes cover every declared enum member,
  both booleans, and every integer constant boundary, so the gap is narrow, but
  it is a gap and not a proof.
- **The grammar is deliberately small**: conjunctions of atoms, first match
  wins. No disjunction inside a rule, no arithmetic between facts. Disjunction
  is expressible as separate rules. This is what keeps the probe set finite and
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
