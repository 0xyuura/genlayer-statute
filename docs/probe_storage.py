# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""Throwaway probe. Isolates one question: does a nested DynArray inside an
@allow_storage dataclass, itself held in a DynArray, construct and persist?

No model call, so a failure here cannot be a leader timeout. Not part of the
Statute submission; kept only as the record of how the storage shape was
settled.
"""

import json
from dataclasses import dataclass

from genlayer import *


@allow_storage
@dataclass
class Atom:
    fact: str
    op: str
    value: str


@allow_storage
@dataclass
class Rule:
    outcome: str
    atoms: DynArray[Atom]


class ProbeStorage(gl.Contract):
    rules: DynArray[Rule]
    note: str

    def __init__(self):
        self.note = "empty"

    @gl.public.write
    def variant_inmem(self) -> None:
        rule = Rule(outcome="deny",
                    atoms=gl.storage.inmem_allocate(DynArray[Atom]))
        rule.atoms.append(Atom(fact="age", op="<", value="18"))
        self.rules.append(rule)
        self.note = "inmem"

    @gl.public.write
    def variant_call(self) -> None:
        rule = Rule(outcome="approve", atoms=DynArray[Atom]())
        rule.atoms.append(Atom(fact="age", op=">=", value="18"))
        self.rules.append(rule)
        self.note = "call"

    @gl.public.write
    def variant_append_then_fill(self) -> None:
        self.rules.append(Rule(outcome="review",
                               atoms=gl.storage.inmem_allocate(DynArray[Atom])))
        self.rules[len(self.rules) - 1].atoms.append(
            Atom(fact="tier", op="==", value="gold"))
        self.note = "append_then_fill"

    @gl.public.view
    def dump(self) -> str:
        out = []
        for rule in self.rules:
            out.append({"outcome": str(rule.outcome),
                        "atoms": [{"fact": str(a.fact), "op": str(a.op),
                                   "value": str(a.value)} for a in rule.atoms]})
        return json.dumps({"note": str(self.note), "rules": out}, sort_keys=True)
