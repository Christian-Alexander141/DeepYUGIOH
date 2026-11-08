"""Low-level ctypes bindings for ygopro-core (ocgapi.h, API version 11)."""

from __future__ import annotations

import ctypes as C
from pathlib import Path

from deepyugioh import paths


class OCGCardData(C.Structure):
    _fields_ = [
        ("code", C.c_uint32),
        ("alias", C.c_uint32),
        ("setcodes", C.POINTER(C.c_uint16)),
        ("type", C.c_uint32),
        ("level", C.c_uint32),
        ("attribute", C.c_uint32),
        ("race", C.c_uint64),
        ("attack", C.c_int32),
        ("defense", C.c_int32),
        ("lscale", C.c_uint32),
        ("rscale", C.c_uint32),
        ("link_marker", C.c_uint32),
    ]


class OCGPlayer(C.Structure):
    _fields_ = [
        ("startingLP", C.c_uint32),
        ("startingDrawCount", C.c_uint32),
        ("drawCountPerTurn", C.c_uint32),
    ]


DataReader = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32, C.POINTER(OCGCardData))
DataReaderDone = C.CFUNCTYPE(None, C.c_void_p, C.POINTER(OCGCardData))
ScriptReader = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_char_p)
LogHandler = C.CFUNCTYPE(None, C.c_void_p, C.c_char_p, C.c_int)


class OCGDuelOptions(C.Structure):
    _fields_ = [
        ("seed", C.c_uint64 * 4),
        ("flags", C.c_uint64),
        ("team1", OCGPlayer),
        ("team2", OCGPlayer),
        ("cardReader", DataReader),
        ("payload1", C.c_void_p),
        ("scriptReader", ScriptReader),
        ("payload2", C.c_void_p),
        ("logHandler", LogHandler),
        ("payload3", C.c_void_p),
        ("cardReaderDone", DataReaderDone),
        ("payload4", C.c_void_p),
        ("enableUnsafeLibraries", C.c_uint8),
    ]


class OCGNewCardInfo(C.Structure):
    _fields_ = [
        ("team", C.c_uint8),
        ("duelist", C.c_uint8),
        ("code", C.c_uint32),
        ("con", C.c_uint8),
        ("loc", C.c_uint32),
        ("seq", C.c_uint32),
        ("pos", C.c_uint32),
    ]


class OCGQueryInfo(C.Structure):
    _fields_ = [
        ("flags", C.c_uint32),
        ("con", C.c_uint8),
        ("loc", C.c_uint32),
        ("seq", C.c_uint32),
        ("overlay_seq", C.c_uint32),
    ]


EXPECTED_API = (11, 0)

_lib: C.CDLL | None = None


def load_library(path: Path | None = None) -> C.CDLL:
    """Load libocgcore once and declare the function signatures."""
    global _lib
    if _lib is not None:
        return _lib
    path = path or paths.ocgcore_library()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found - build the engine first with scripts/build_core.sh"
        )
    lib = C.CDLL(str(path))
    vp = C.c_void_p
    sigs = {
        "OCG_GetVersion": (None, [C.POINTER(C.c_int), C.POINTER(C.c_int)]),
        "OCG_CreateDuel": (C.c_int, [C.POINTER(vp), C.POINTER(OCGDuelOptions)]),
        "OCG_DestroyDuel": (None, [vp]),
        "OCG_DuelNewCard": (None, [vp, C.POINTER(OCGNewCardInfo)]),
        "OCG_StartDuel": (None, [vp]),
        "OCG_DuelProcess": (C.c_int, [vp]),
        "OCG_DuelGetMessage": (vp, [vp, C.POINTER(C.c_uint32)]),
        "OCG_DuelSetResponse": (None, [vp, C.c_char_p, C.c_uint32]),
        "OCG_LoadScript": (C.c_int, [vp, C.c_char_p, C.c_uint32, C.c_char_p]),
        "OCG_DuelQueryCount": (C.c_uint32, [vp, C.c_uint8, C.c_uint32]),
        "OCG_DuelQuery": (vp, [vp, C.POINTER(C.c_uint32), C.POINTER(OCGQueryInfo)]),
        "OCG_DuelQueryLocation": (vp, [vp, C.POINTER(C.c_uint32), C.POINTER(OCGQueryInfo)]),
        "OCG_DuelQueryField": (vp, [vp, C.POINTER(C.c_uint32)]),
    }
    for name, (res, args) in sigs.items():
        fn = getattr(lib, name)
        fn.restype = res
        fn.argtypes = args
    major, minor = C.c_int(), C.c_int()
    lib.OCG_GetVersion(C.byref(major), C.byref(minor))
    if (major.value, minor.value) != EXPECTED_API:
        raise RuntimeError(
            f"ocgcore API {major.value}.{minor.value}, bindings expect {EXPECTED_API}"
        )
    _lib = lib
    return lib


def read_buffer(ptr: int | None, length: int) -> bytes:
    if not ptr or length == 0:
        return b""
    return C.string_at(ptr, length)
