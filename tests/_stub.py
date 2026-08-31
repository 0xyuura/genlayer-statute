"""Minimal `genlayer` SDK stub so the contract module imports under plain CPython.

The contract itself only runs inside GenVM. Everything worth unit testing in
Statute is a module level pure function, so a stub is enough to reach them.
"""
import sys
import types


def install() -> None:
    if "genlayer" in sys.modules:
        return

    class UserError(Exception):
        def __init__(self, message: str = ""):
            super().__init__(message)
            self.message = message

    class Return:
        def __init__(self, calldata):
            self.calldata = calldata

    def identity(fn):
        return fn

    write = identity
    write.payable = identity

    vm = types.SimpleNamespace(
        UserError=UserError,
        Return=Return,
        Result=object,
        run_nondet_unsafe=lambda leader, validator: leader(),
    )
    nondet = types.SimpleNamespace(
        exec_prompt=lambda *a, **k: {},
        web=types.SimpleNamespace(render=lambda *a, **k: ""),
    )
    storage = types.SimpleNamespace(
        copy_to_memory=lambda x: x,
        inmem_allocate=lambda t, *a, **k: list(a[0]) if a else [],
    )
    gl = types.SimpleNamespace(
        Contract=object,
        vm=vm,
        nondet=nondet,
        storage=storage,
        public=types.SimpleNamespace(view=identity, write=write),
        message=types.SimpleNamespace(sender_address="0x" + "11" * 20),
    )

    class _Generic:
        def __class_getitem__(cls, item):
            return list

    mod = types.ModuleType("genlayer")
    mod.gl = gl
    mod.allow_storage = identity
    mod.Address = str
    for _name in ("u8", "u16", "u32", "u64", "u128", "u256",
                  "i8", "i16", "i32", "i64", "i128", "i256"):
        setattr(mod, _name, int)
    mod.DynArray = _Generic
    mod.TreeMap = _Generic
    mod.__all__ = ["gl", "allow_storage", "Address", "DynArray", "TreeMap",
                   "u8", "u16", "u32", "u64", "u128", "u256",
                   "i8", "i16", "i32", "i64", "i128", "i256"]
    sys.modules["genlayer"] = mod
