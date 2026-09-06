# Deploying Statute to Testnet Bradbury

## Network

| Field | Value |
| --- | --- |
| Name | Genlayer Bradbury Testnet |
| Chain id | 4221 |
| RPC | `https://rpc-bradbury.genlayer.com` |
| Explorer | `https://explorer-bradbury.genlayer.com/` |
| CLI used | `genlayer` 0.39.2 |
| Runner pinned | `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` |

## Live deployment

| Field | Value |
| --- | --- |
| Contract | `0x54378836847a768Db56918E8Fa38149c7db780c1` |
| Deploy tx | `0x064e628fb6654cfd62b37e84c95c6fa2375d2b8aef9d485bac60b02dbdcf6b7b` |
| Deploy result | `ACCEPTED` / `AGREE` / `FINISHED_WITH_RETURN`, 3 validators |
| Versions compiled | 2, both through consensus on the rewritten agreement check |
| Deployer | `0xfb332ad96268a9974d32f87daa335f553478a67d` |
| Superseded | `0xB7255423EaF0A021Df3551BEaaafDbC3bdaa1868`, the rejected revision |

### Verified live on this deployment

| Call | Result |
| --- | --- |
| `compile_policy` v1 | `ACCEPTED` / `AGREE` / `FINISHED_WITH_RETURN` |
| `compile_policy` v2 | `ACCEPTED` / `AGREE` / `FINISHED_WITH_RETURN` (one `LEADER_TIMEOUT` first, retried) |
| `evaluate` on grid, `requested_gen` 1500 | `auto_approve` |
| `evaluate` off grid, `requested_gen` 1501 | refused, `[EXPECTED] FACT_OFF_GRID` |
| `diff(0, 1)` | 48 changed fact combinations |
| `get_version(0).behaviour` | `2165931a275a8437f38050a5b8552cba6dd1346c762ede8b1a9606c7195b831f` |
| `get_version(1).behaviour` | `8a38c8c9ccd694416e988b8af785c108fecf0aef32b810c6054d7cbc600eca0f` |

The v0 fingerprint is byte for byte the one the two earlier deployments
produced, so the rewrite closed the consensus holes without moving any on-grid
decision.

Constructor arguments:

```
facts:    requested_gen:int:0:10000:500;track:enum:app,content,contract,infra;prior_accepted:int:0:10:1;kyc:bool
outcomes: auto_approve,committee_review,reject
```

## Before deploying

```bash
pip install genvm-linter
genvm-lint check contracts/statute.py        # must print ok: true
genvm-lint typecheck contracts/statute.py    # must print no type errors
python -m unittest discover -s tests         # 61 tests
```

## Account safety

Use a throwaway testnet keystore. Never import a wallet holding real funds into
a development CLI. Unlock only for the deploy and lock again afterwards:

```bash
genlayer account unlock --account <name> --password <password>
genlayer deploy --contract contracts/statute.py --args "<facts>" "<outcomes>"
genlayer account lock --account <name>
```

The account used for this deployment was locked again immediately after the
session, and its private key removed from the OS keychain.

## Deploying and driving it

```bash
genlayer network set testnet-bradbury
genlayer deploy --contract contracts/statute.py --args "<facts>" "<outcomes>"

genlayer write <addr> compile_policy --args "$(cat docs/example-policy.txt)"
genlayer call  <addr> version_count
genlayer call  <addr> get_version --args 0
genlayer call  <addr> evaluate --args '{"requested_gen":1500,"track":"contract","prior_accepted":5,"kyc":true}'
genlayer call  <addr> explain  --args '{"requested_gen":1500,"track":"contract","prior_accepted":5,"kyc":true}'
genlayer write <addr> compile_policy --args "$(cat docs/example-policy-v2.txt)"
genlayer call  <addr> diff --args 0 1
```

## The storage experiment

The first build stored each version's rules as a nested `DynArray[Rule]`, where
`Rule` was an `@allow_storage` dataclass holding `atoms: DynArray[Atom]`. The
GenLayer storage docs show that shape, and it lints clean.

On chain, `compile_policy` came back `ACCEPTED` with `resultName: AGREE` but
`txExecutionResultName: FINISHED_WITH_ERROR`. Consensus had succeeded and every
validator had then hit the same deterministic error, which pointed at the write
path rather than at the nondeterministic block.

Rather than guess, a throwaway probe contract isolated the single variable. It
contains no model call, so a failure could not be a leader timeout.
`docs/probe_storage.py` was deployed to `0x0136f9ECa38bCbF2A78e2Ef41E6ee8867786ab9C`
and offers three construction variants of the same nested shape:

| Method | Construction | Result |
| --- | --- | --- |
| `variant_inmem` | `Rule(atoms=gl.storage.inmem_allocate(DynArray[Atom]))` | `FINISHED_WITH_ERROR` |
| `variant_call` | `Rule(atoms=DynArray[Atom]())` | `FINISHED_WITH_ERROR` |
| `variant_append_then_fill` | append the rule first, then fill `atoms` in place | `FINISHED_WITH_ERROR` |

`dump()` afterwards returned `{"note": "empty", "rules": []}`, confirming nothing
persisted in any variant.

Conclusion: a `DynArray` nested inside an `@allow_storage` dataclass that is
itself held in a `DynArray` does not work in this runner, whatever the docs show.
Versions now store their rules as a canonical `json.dumps(..., sort_keys=True)`
string, which is also the pattern the `write-contract` skill recommends for
nested structures. Two nodes that agreed therefore write byte identical state.

## Network conditions observed

Bradbury was intermittent during this session. The contract logic was not
involved in any of these:

| Symptom | What it means | Handling |
| --- | --- | --- |
| `LEADER_TIMEOUT` / `NOT_VOTED` | the leader did not answer in time | retry; the next attempt landed |
| `Transaction reverted: EVM tx ... to consensus contract` | rejected before reaching the contract. Balance was 19.97 GEN at the time, so not funds | retry |
| `genlayer receipt` hanging | it waits for `FINALIZED`, which is much slower than `ACCEPTED` | read `status_name` from the write output instead |

Both `compile_policy` calls that eventually landed returned `ACCEPTED` /
`AGREE` / `FINISHED_WITH_RETURN`.

## A note on the runner version

`genvm-lint` reports that a newer runner exists
(`1zr6nqk597d97kg0dyxg0shhrykx5v02zjgnyrajapy4wlqvfvwh`). Pinning it makes
validation fail with `E101 Failed to load SDK: No module named 'genlayer.py'`,
so this contract stays on the runner the tooling can actually load. It is a
pinned hash either way, never `latest` or `test`.
