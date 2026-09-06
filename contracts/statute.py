# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
Statute: natural language policy compiled once into an on chain decision table.

The problem this primitive solves
---------------------------------
Putting a model in the decision seat is expensive and unrepeatable. Every
evaluation pays for inference, every evaluation can drift, and nobody can prove
after the fact why a particular case was decided the way it was. That is a poor
fit for the thing policies are actually for, which is deciding the same case the
same way twice.

Statute moves the model out of the decision seat. The fuzzy step runs exactly
once, at compile time, and its output is not a verdict. It is a decision table:
an ordered list of conjunctive rules over a declared fact schema, plus a
default. Consensus is reached on that table. From then on `evaluate` is a plain
deterministic view. No model, no inference cost, no drift, and anybody can
replay every decision the contract ever made from the stored table alone.

Validators agree on behaviour, not on text
------------------------------------------
Two correct compilations of the same policy will almost never be textually
equal. "deny when age < 18, otherwise approve" and "approve when age >= 18,
otherwise deny" are the same policy written the other way round. A validator
that diffs the JSON would reject both honest peers forever.

So validators do not compare tables. Each validator compiles the policy itself,
then compares what the two tables *decide* over the whole canonical domain: the
schema declares a finite set of admissible values per fact, and agreement is
checked at every point of their product. Nothing is sampled and nothing is
thinned, so two tables that agree cannot decide any admissible case
differently.

The schema is what makes that affordable. `parse_schema` refuses any schema
whose domain exceeds MAX_GRID, so a policy whose behaviour this contract could
not enumerate in full is rejected before it is ever compiled, rather than
approximated afterwards. `evaluate` accepts exactly the domain the grid visits
and refuses anything between two declared points, which is what keeps the
agreement check, the fingerprint and `diff` talking about the same set of
cases.

An earlier version of this contract sampled instead. It kept the declared
bounds plus c-1, c, c+1 around every integer constant, then thinned each fact
to at most twelve values, and a thinned list could drop a boundary that
mattered: a leader compiling `x < 50` against a validator compiling `x < 51`
could lose both 50 and 51, agree on every retained point, and still decide
x = 50 differently. That defect is why exhaustiveness is now the rule, and the
case is pinned in the tests.

This is the lesson from a previous contract of ours, stated as a rule: validate
the decision the contract will actually store, not the intermediate reading that
produced it.

Two further checks run over the same domain, because a table can be
behaviourally right and still be junk:

  * a table carrying a rule that can never fire, because earlier rules already
    cover it, is rejected. A dead rule means the compiler was sloppy, and a
    sloppy table is a bad thing to freeze into storage.
  * the outcome and fact vocabulary is closed. Every atom must name a declared
    fact, use an operator legal for that fact's kind, and carry a value of the
    right type. Ordering operators on an enum are refused rather than guessed at.

What you get that an off chain model call cannot give you
---------------------------------------------------------
Because the canonical grid is fixed by the schema alone, and because it is the
whole of what `evaluate` accepts, every compiled version has a stable
behavioural fingerprint that covers every case the contract can be asked. That makes `diff` possible: when a
policy is amended, the contract reports, deterministically and on chain, exactly
which fact combinations change outcome. "We reworded clause 4" and "we changed
who gets approved" become distinguishable, which is the question anyone
governing by policy actually needs answered.

Scope
-----
The contract owns the schema, the compiled versions, which version is active,
and the audit log. A frontend owns collecting facts and showing results. The
model owns nothing that is not immediately re-derived and voted on.
"""

import json
import hashlib
import typing
from dataclasses import dataclass

from genlayer import *


# --------------------------------------------------------------------------
# Error classes. Deterministic errors must match exactly between leader and
# validator; a model error must make validators disagree so the leader rotates.
# --------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"
ERROR_LLM = "[LLM_ERROR]"

# Bounds. MAX_GRID is the important one: it is the largest canonical domain
# this contract will accept, and therefore the most work an agreement check can
# ever do. A schema whose domain does not fit is refused at parse time rather
# than sampled, which is the whole point of the rewrite described above.
MAX_FACTS = 6
MAX_ENUM_MEMBERS = 12
MAX_OUTCOMES = 8
MAX_GRID = 4096
MAX_RULES = 64
MAX_ATOMS_PER_RULE = 6
MAX_POLICY_CHARS = 4000
MAX_DIFF_POINTS = 200

INT_OPS = ("==", "!=", "<", "<=", ">", ">=")
SET_OPS = ("==", "!=")


def _fail(prefix: str, code: str) -> typing.NoReturn:
    """Always raises. Declared NoReturn so callers narrow correctly."""
    raise gl.vm.UserError(prefix + " " + code)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

def parse_schema(spec: str) -> list:
    """Parse the fact schema.

    One fact per line, or separated by ";" so that a whole schema can travel
    as a single argument through any ABI or command line:
        name:int:lo:hi:step
        name:enum:a,b,c
        name:bool
    """
    facts = []
    seen = set()
    for raw in str(spec).replace(";", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(":")
        name = parts[0].strip()
        if not name or not name.replace("_", "").isalnum():
            _fail(ERROR_EXPECTED, "SCHEMA_FACT_NAME_INVALID")
        if name in seen:
            _fail(ERROR_EXPECTED, "SCHEMA_FACT_DUPLICATE")
        seen.add(name)
        kind = parts[1].strip() if len(parts) > 1 else ""

        if kind == "int":
            if len(parts) != 5:
                _fail(ERROR_EXPECTED, "SCHEMA_INT_ARITY")
            try:
                lo, hi, step = int(parts[2]), int(parts[3]), int(parts[4])
            except ValueError:
                _fail(ERROR_EXPECTED, "SCHEMA_INT_NOT_NUMERIC")
            if step <= 0 or hi < lo:
                _fail(ERROR_EXPECTED, "SCHEMA_INT_RANGE_INVALID")
            facts.append({"name": name, "kind": "int",
                          "lo": lo, "hi": hi, "step": step})
        elif kind == "enum":
            if len(parts) != 3:
                _fail(ERROR_EXPECTED, "SCHEMA_ENUM_ARITY")
            members = sorted({m.strip() for m in parts[2].split(",") if m.strip()})
            if not members:
                _fail(ERROR_EXPECTED, "SCHEMA_ENUM_EMPTY")
            if len(members) > MAX_ENUM_MEMBERS:
                _fail(ERROR_EXPECTED, "SCHEMA_ENUM_TOO_LARGE")
            facts.append({"name": name, "kind": "enum", "members": members})
        elif kind == "bool":
            if len(parts) != 2:
                _fail(ERROR_EXPECTED, "SCHEMA_BOOL_ARITY")
            facts.append({"name": name, "kind": "bool"})
        else:
            _fail(ERROR_EXPECTED, "SCHEMA_KIND_UNKNOWN")

    if not facts:
        _fail(ERROR_EXPECTED, "SCHEMA_EMPTY")
    if len(facts) > MAX_FACTS:
        _fail(ERROR_EXPECTED, "SCHEMA_TOO_MANY_FACTS")
    if grid_size(facts) > MAX_GRID:
        _fail(ERROR_EXPECTED, "SCHEMA_GRID_TOO_LARGE")
    return facts


def parse_outcomes(spec: str) -> list:
    labels = [s.strip() for s in str(spec).split(",") if s.strip()]
    uniq = sorted(set(labels))
    if len(uniq) != len(labels):
        _fail(ERROR_EXPECTED, "OUTCOMES_DUPLICATE")
    if len(uniq) < 2:
        _fail(ERROR_EXPECTED, "OUTCOMES_TOO_FEW")
    if len(uniq) > MAX_OUTCOMES:
        _fail(ERROR_EXPECTED, "OUTCOMES_TOO_MANY")
    return labels


def domain_of(fact: dict) -> list:
    """Every value of a fact that the canonical grid visits."""
    if fact["kind"] == "int":
        vals, v = [], fact["lo"]
        while v <= fact["hi"]:
            vals.append(v)
            v += fact["step"]
        if vals[-1] != fact["hi"]:
            vals.append(fact["hi"])
        return vals
    if fact["kind"] == "enum":
        return list(fact["members"])
    return [False, True]


def in_domain(fact: dict, value) -> bool:
    """True when `value` is one of the points the canonical grid visits.

    `evaluate` and the agreement check are held to this one definition on
    purpose. If `evaluate` accepted a value the grid never visits, two tables
    could agree everywhere the contract looked and still decide that value
    differently, which is exactly the hole this contract used to have.
    """
    if fact["kind"] == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if value < fact["lo"] or value > fact["hi"]:
            return False
        # domain_of walks lo, lo+step, ... and always ends on hi.
        return value == fact["hi"] or (value - fact["lo"]) % fact["step"] == 0
    if fact["kind"] == "enum":
        return isinstance(value, str) and value in fact["members"]
    return isinstance(value, bool)


def grid_size(schema: list) -> int:
    total = 1
    for fact in schema:
        total *= len(domain_of(fact))
        if total > MAX_GRID * 64:          # stop early on absurd schemas
            return total
    return total


def _product(schema: list, domains: list) -> list:
    rows = [{}]
    for fact, values in zip(schema, domains):
        nxt = []
        for row in rows:
            for value in values:
                item = dict(row)
                item[fact["name"]] = value
                nxt.append(item)
        rows = nxt
    return rows


def canonical_grid(schema: list) -> list:
    """Schema only, so it is identical for every version of a policy."""
    return _product(schema, [domain_of(f) for f in schema])


# --------------------------------------------------------------------------
# Decision table
# --------------------------------------------------------------------------

def _coerce_fact_value(fact: dict, raw) -> object:
    if fact["kind"] == "int":
        try:
            return int(str(raw).strip())
        except (ValueError, TypeError):
            _fail(ERROR_EXPECTED, "VALUE_NOT_INT")
    if fact["kind"] == "enum":
        value = str(raw).strip()
        if value not in fact["members"]:
            _fail(ERROR_EXPECTED, "VALUE_NOT_A_MEMBER")
        return value
    value = str(raw).strip().lower()
    if value not in ("true", "false"):
        _fail(ERROR_EXPECTED, "VALUE_NOT_BOOL")
    return value == "true"


def parse_table(raw, schema: list, outcomes: list) -> dict:
    """Strictly parse a candidate table. Anything unexpected is refused."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            _fail(ERROR_LLM, "TABLE_NOT_JSON")
    if not isinstance(raw, dict):
        _fail(ERROR_LLM, "TABLE_NOT_OBJECT")

    by_name = {f["name"]: f for f in schema}
    default = raw.get("default")
    if not isinstance(default, str) or default not in outcomes:
        _fail(ERROR_LLM, "TABLE_DEFAULT_INVALID")

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list):
        _fail(ERROR_LLM, "TABLE_RULES_NOT_LIST")
    if len(rules_raw) > MAX_RULES:
        _fail(ERROR_LLM, "TABLE_TOO_MANY_RULES")

    rules = []
    for item in rules_raw:
        if not isinstance(item, dict):
            _fail(ERROR_LLM, "RULE_NOT_OBJECT")
        outcome = item.get("outcome")
        if not isinstance(outcome, str) or outcome not in outcomes:
            _fail(ERROR_LLM, "RULE_OUTCOME_INVALID")
        atoms_raw = item.get("atoms")
        if not isinstance(atoms_raw, list) or not atoms_raw:
            _fail(ERROR_LLM, "RULE_ATOMS_EMPTY")
        if len(atoms_raw) > MAX_ATOMS_PER_RULE:
            _fail(ERROR_LLM, "RULE_TOO_MANY_ATOMS")

        atoms = []
        for atom in atoms_raw:
            if not isinstance(atom, dict):
                _fail(ERROR_LLM, "ATOM_NOT_OBJECT")
            name = atom.get("fact")
            if not isinstance(name, str) or name not in by_name:
                _fail(ERROR_LLM, "ATOM_FACT_UNKNOWN")
            fact = by_name[name]
            op = atom.get("op")
            legal = INT_OPS if fact["kind"] == "int" else SET_OPS
            if not isinstance(op, str) or op not in legal:
                _fail(ERROR_LLM, "ATOM_OP_ILLEGAL_FOR_KIND")
            value = _coerce_fact_value(fact, atom.get("value"))
            atoms.append({"fact": name, "op": op, "value": _encode(value)})
        rules.append({"outcome": outcome, "atoms": atoms})

    return {"rules": rules, "default": default}


def _encode(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _atom_holds(atom: dict, facts: dict, by_name: dict) -> bool:
    fact = by_name[atom["fact"]]
    actual = facts[atom["fact"]]
    expected = _coerce_fact_value(fact, atom["value"])
    op = atom["op"]
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == "<":
        return actual < expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    return actual >= expected


def _check_facts(facts: dict, schema: list) -> None:
    if not isinstance(facts, dict):
        _fail(ERROR_EXPECTED, "FACTS_NOT_OBJECT")
    if len(facts) != len(schema):
        _fail(ERROR_EXPECTED, "FACTS_INCOMPLETE")
    for fact in schema:
        name = fact["name"]
        if name not in facts:
            _fail(ERROR_EXPECTED, "FACTS_INCOMPLETE")
        value = facts[name]
        if fact["kind"] == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                _fail(ERROR_EXPECTED, "FACT_NOT_INT")
            if value < fact["lo"] or value > fact["hi"]:
                _fail(ERROR_EXPECTED, "FACT_OUT_OF_RANGE")
            if not in_domain(fact, value):
                # In range but between two declared points. Refused rather than
                # rounded: rounding would decide a case the fingerprint never
                # saw, and silently deciding unenumerated cases is the defect
                # this contract was rejected for.
                _fail(ERROR_EXPECTED, "FACT_OFF_GRID")
        elif fact["kind"] == "enum":
            if not isinstance(value, str) or value not in fact["members"]:
                _fail(ERROR_EXPECTED, "FACT_NOT_A_MEMBER")
        else:
            if not isinstance(value, bool):
                _fail(ERROR_EXPECTED, "FACT_NOT_BOOL")


def _first_match(table: dict, facts: dict, by_name: dict) -> int:
    """Hot path. Takes a prebuilt name index because the agreement check walks
    the whole canonical domain and rebuilding it per point is pure waste."""
    for index, rule in enumerate(table["rules"]):
        if all(_atom_holds(a, facts, by_name) for a in rule["atoms"]):
            return index
    return -1


def first_match(table: dict, facts: dict, schema: list) -> int:
    """Index of the rule that fires, or -1 when the default applies."""
    return _first_match(table, facts, {f["name"]: f for f in schema})


def _outcome_at(table: dict, index: int) -> str:
    return table["default"] if index < 0 else table["rules"][index]["outcome"]


def evaluate_table(table: dict, facts: dict, schema: list) -> str:
    _check_facts(facts, schema)
    return _outcome_at(table, first_match(table, facts, schema))


# --------------------------------------------------------------------------
# Agreement.
#
# There is no probe set here and no sampling of any kind. The check visits
# every point of the canonical domain, which by `in_domain` is exactly the set
# of fact combinations `evaluate` will ever accept. Two tables that pass it
# cannot decide any admissible case differently, and that is a proof rather
# than a hope.
#
# The previous version sampled: it kept the declared bounds plus c-1, c, c+1
# around every integer constant, then thinned each fact to at most twelve
# values. Thinning could discard a boundary that mattered. With several
# thresholds declared, a leader compiling `x < 50` and a validator compiling
# `x < 51` could lose both 50 and 51 from the kept values, agree on every
# retained point, pass the dead rule check, and still decide x = 50
# differently. tests/test_deterministic.py pins that exact case.
#
# Cost is bounded by MAX_GRID, which parse_schema enforces before a policy can
# ever be compiled, so a schema this contract cannot fully enumerate is refused
# up front instead of being approximated later.
# --------------------------------------------------------------------------

def dead_rules(table: dict, schema: list) -> list:
    """Rules that no admissible fact combination can reach, because earlier
    rules already cover them. Exhaustive, so a rule reported dead really is
    unreachable rather than merely unvisited."""
    by_name = {f["name"]: f for f in schema}
    reached = set()
    for facts in canonical_grid(schema):
        index = _first_match(table, facts, by_name)
        if index >= 0:
            reached.add(index)
    return [i for i in range(len(table["rules"])) if i not in reached]


def tables_agree(mine: dict, theirs: dict, schema: list) -> bool:
    """The whole consensus rule, in one testable place.

    Symmetric on purpose: a sloppy table is refused whichever side produced it,
    and refusing rotates the leader rather than freezing junk into storage.

    Outcome agreement and reachability are collected in a single walk of the
    domain, because walking it three times to answer three questions about the
    same points is the kind of thing that pushes a schema over the gas limit
    and tempts the next person into sampling again.
    """
    by_name = {f["name"]: f for f in schema}
    reached_mine = set()
    reached_theirs = set()
    try:
        for facts in canonical_grid(schema):
            i = _first_match(mine, facts, by_name)
            j = _first_match(theirs, facts, by_name)
            if _outcome_at(mine, i) != _outcome_at(theirs, j):
                return False
            if i >= 0:
                reached_mine.add(i)
            if j >= 0:
                reached_theirs.add(j)
        if len(reached_mine) != len(mine["rules"]):
            return False
        if len(reached_theirs) != len(theirs["rules"]):
            return False
    except (KeyError, TypeError, ValueError, gl.vm.UserError):
        return False
    return True


# --------------------------------------------------------------------------
# Behaviour identity
# --------------------------------------------------------------------------

def outcome_vector(table: dict, schema: list) -> list:
    return [evaluate_table(table, facts, schema) for facts in canonical_grid(schema)]


def fingerprint(table: dict, schema: list) -> str:
    """Identity of what a table decides, not of how it is written."""
    joined = "".join(outcome_vector(table, schema))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def diff_points(before: dict, after: dict, schema: list, limit: int) -> list:
    out = []
    for facts in canonical_grid(schema):
        was = evaluate_table(before, facts, schema)
        now = evaluate_table(after, facts, schema)
        if was != now:
            out.append({"facts": facts, "from": was, "to": now})
            if len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------
# Compilation prompt
# --------------------------------------------------------------------------

def schema_brief(schema: list) -> str:
    lines = []
    for fact in schema:
        if fact["kind"] == "int":
            if fact["step"] == 1:
                lines.append("- %s: integer from %d to %d" %
                             (fact["name"], fact["lo"], fact["hi"]))
            else:
                # The declared domain is coarser than every integer, and the
                # contract will only ever be asked about these points, so say
                # so rather than let the model aim at a value between them.
                lines.append("- %s: integer from %d to %d in steps of %d" %
                             (fact["name"], fact["lo"], fact["hi"],
                              fact["step"]))
        elif fact["kind"] == "enum":
            lines.append("- %s: one of %s" %
                         (fact["name"], ", ".join(fact["members"])))
        else:
            lines.append("- %s: boolean, true or false" % fact["name"])
    return "\n".join(lines)


def compile_prompt(policy: str, schema: list, outcomes: list) -> str:
    return (
        "Compile the policy below into an ordered decision table.\n\n"
        "Facts available:\n" + schema_brief(schema) + "\n\n"
        "Allowed outcomes: " + ", ".join(outcomes) + "\n\n"
        "Rules are evaluated in order and the first one that matches wins, so "
        "put the most specific rules first. Every atom in a rule must hold for "
        "the rule to match. Integer facts allow ==, !=, <, <=, >, >=. Enum and "
        "boolean facts allow only == and !=. Do not write a rule that an "
        "earlier rule already fully covers. Give a default outcome for cases "
        "no rule matches.\n\n"
        "Return JSON only, shaped exactly like this:\n"
        '{"rules": [{"outcome": "<outcome>", "atoms": '
        '[{"fact": "<fact>", "op": "<op>", "value": "<value>"}]}], '
        '"default": "<outcome>"}\n\n'
        "Policy:\n" + policy
    )


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

@allow_storage
@dataclass
class Version:
    """One compiled policy.

    `rules_json` is the agreed table serialised with sort_keys, which makes the
    stored bytes a canonical form: two nodes that agreed write byte identical
    state. A nested DynArray of rule structs would read more nicely, but it does
    not survive this runner. That was settled by experiment rather than by
    reading, and the probe and its result are recorded in docs/DEPLOY.md.
    """
    policy_sha: str
    default_outcome: str
    behaviour: str
    rules_json: str
    compiled_by: Address


@allow_storage
@dataclass
class Decision:
    facts: str
    outcome: str
    version: u32
    rule_index: i32
    decided_by: Address


class Statute(gl.Contract):
    facts_spec: str
    outcomes_spec: str
    versions: DynArray[Version]
    active: u32
    decisions: TreeMap[str, Decision]
    decision_ids: DynArray[str]
    owner: Address

    def __init__(self, facts_spec: str, outcomes_spec: str):
        parse_schema(facts_spec)          # refuse an unusable schema at birth
        parse_outcomes(outcomes_spec)
        self.facts_spec = facts_spec
        self.outcomes_spec = outcomes_spec
        self.active = u32(0)
        self.owner = gl.message.sender_address

    # -- helpers ---------------------------------------------------------
    def _schema(self) -> list:
        return parse_schema(str(self.facts_spec))

    def _outcomes(self) -> list:
        return parse_outcomes(str(self.outcomes_spec))

    def _table_at(self, index: int) -> dict:
        if index < 0 or index >= len(self.versions):
            _fail(ERROR_EXPECTED, "VERSION_NOT_FOUND")
        version = self.versions[index]
        return {"rules": json.loads(str(version.rules_json)),
                "default": str(version.default_outcome)}

    # -- the one nondeterministic entry point ----------------------------
    @gl.public.write
    def compile_policy(self, policy: str) -> None:
        text = str(policy).strip()
        if not text or len(text) > MAX_POLICY_CHARS:
            _fail(ERROR_EXPECTED, "POLICY_LENGTH")

        # Storage cannot be touched inside a nondeterministic block, so the
        # schema is pulled into plain Python before the closures are built.
        schema = self._schema()
        outcomes = self._outcomes()
        prompt = compile_prompt(text, schema, outcomes)

        def leader_fn() -> str:
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            table = parse_table(raw, schema, outcomes)
            return json.dumps(table, sort_keys=True)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _leader_errored(leaders_res, leader_fn)
            try:
                theirs = parse_table(_leader_payload(leaders_res),
                                     schema, outcomes)
                mine = parse_table(leader_fn(), schema, outcomes)
            except gl.vm.UserError:
                return False
            return tables_agree(mine, theirs, schema)

        agreed = parse_table(gl.vm.run_nondet_unsafe(leader_fn, validator_fn),
                             schema, outcomes)

        self.versions.append(Version(
            policy_sha=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            default_outcome=agreed["default"],
            behaviour=fingerprint(agreed, schema),
            rules_json=json.dumps(agreed["rules"], sort_keys=True),
            compiled_by=gl.message.sender_address,
        ))
        self.active = u32(len(self.versions) - 1)

    # -- everything below is deterministic, and needs no model -----------
    @gl.public.view
    def evaluate(self, facts_json: str) -> str:
        schema = self._schema()
        return evaluate_table(self._table_at(int(self.active)),
                              _load_facts(facts_json, schema), schema)

    @gl.public.view
    def explain(self, facts_json: str) -> str:
        schema = self._schema()
        table = self._table_at(int(self.active))
        facts = _load_facts(facts_json, schema)
        index = first_match(table, facts, schema)
        return json.dumps({
            "outcome": evaluate_table(table, facts, schema),
            "matched_rule": index,
            "reason": "default" if index < 0 else table["rules"][index]["atoms"],
            "version": int(self.active),
        }, sort_keys=True)

    @gl.public.write
    def record_decision(self, decision_id: str, facts_json: str) -> None:
        key = str(decision_id).strip()
        if not key or len(key) > 64:
            _fail(ERROR_EXPECTED, "DECISION_ID_LENGTH")
        if key in self.decisions:
            _fail(ERROR_EXPECTED, "DECISION_ID_TAKEN")
        schema = self._schema()
        table = self._table_at(int(self.active))
        facts = _load_facts(facts_json, schema)
        self.decisions[key] = Decision(
            facts=json.dumps(facts, sort_keys=True),
            outcome=evaluate_table(table, facts, schema),
            version=u32(int(self.active)),
            rule_index=i32(first_match(table, facts, schema)),
            decided_by=gl.message.sender_address,
        )
        self.decision_ids.append(key)

    @gl.public.write
    def activate(self, version: int) -> None:
        if gl.message.sender_address != self.owner:
            _fail(ERROR_EXPECTED, "NOT_OWNER")
        if version < 0 or version >= len(self.versions):
            _fail(ERROR_EXPECTED, "VERSION_NOT_FOUND")
        self.active = u32(version)

    @gl.public.view
    def diff(self, before: int, after: int) -> str:
        schema = self._schema()
        points = diff_points(self._table_at(before), self._table_at(after),
                             schema, MAX_DIFF_POINTS)
        return json.dumps({"changed": len(points), "points": points},
                          sort_keys=True)

    @gl.public.view
    def version_count(self) -> int:
        return len(self.versions)

    @gl.public.view
    def get_version(self, index: int) -> str:
        if index < 0 or index >= len(self.versions):
            _fail(ERROR_EXPECTED, "VERSION_NOT_FOUND")
        version = self.versions[index]
        return json.dumps({
            "policy_sha": str(version.policy_sha),
            "behaviour": str(version.behaviour),
            "compiled_by": str(version.compiled_by),
            "table": self._table_at(index),
        }, sort_keys=True)

    @gl.public.view
    def get_decision(self, decision_id: str) -> str:
        key = str(decision_id).strip()
        if key not in self.decisions:
            _fail(ERROR_EXPECTED, "DECISION_NOT_FOUND")
        record = self.decisions[key]
        return json.dumps({
            "facts": str(record.facts),
            "outcome": str(record.outcome),
            "version": int(record.version),
            "rule_index": int(record.rule_index),
            "decided_by": str(record.decided_by),
        }, sort_keys=True)


# --------------------------------------------------------------------------
# Small helpers kept at module level so tests can reach them
# --------------------------------------------------------------------------

def _load_facts(facts, schema: list) -> dict:
    """Accept the facts as a JSON string or as an already decoded mapping.

    Callers differ: the CLI decodes a `{...}` argument into calldata before it
    reaches the contract, while genlayer-js and cross contract callers may send
    either. Refusing one of them is an ergonomics bug, not a safety property,
    and `_check_facts` still validates every value either way.
    """
    if isinstance(facts, (bytes, bytearray)):
        facts = facts.decode("utf-8")
    if isinstance(facts, str):
        try:
            facts = json.loads(facts)
        except Exception:
            _fail(ERROR_EXPECTED, "FACTS_NOT_JSON")
    elif not isinstance(facts, dict):
        try:
            facts = dict(facts)
        except Exception:
            _fail(ERROR_EXPECTED, "FACTS_NOT_OBJECT")
    _check_facts(facts, schema)
    return facts


def _leader_payload(leaders_res):
    payload = leaders_res.calldata
    if isinstance(payload, (bytes, bytearray)):
        return payload.decode("utf-8")
    return payload


def _leader_errored(leaders_res, leader_fn) -> bool:
    """Deterministic errors must match; a model error must force rotation."""
    leader_msg = getattr(leaders_res, "message", "")
    try:
        leader_fn()
        return False
    except gl.vm.UserError as exc:
        mine = getattr(exc, "message", str(exc))
        if mine.startswith(ERROR_EXPECTED):
            return mine == leader_msg
        return False
    except Exception:
        return False
