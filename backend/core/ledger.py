"""
Pillar 4 tamper-evident ledger. Every Green Credit Points event (verified donation, recycle,
upgrade delivered, points redeemed) is one entry. Each entry stores the SHA-256 hash of the
previous one, so changing any old entry breaks every hash after it. Verify() re-computes the chain.
"""
import copy
import hashlib
import json

GENESIS = "0" * 64
FIELDS = ("seq", "at", "kind", "text", "points", "co2_kg", "ref", "prev")


def digest(entry):
    return hashlib.sha256(json.dumps({k: entry[k] for k in FIELDS}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def add(chain, at, kind, text, points=0, co2_kg=0.0, ref=None):
    e = {"seq": len(chain) + 1, "at": at, "kind": kind, "text": text, "points": int(points),
         "co2_kg": round(float(co2_kg), 1), "ref": ref, "prev": chain[-1]["hash"] if chain else GENESIS}
    e["hash"] = digest(e)
    chain.append(e)
    return e


def verify(chain):
    """(ok, first broken seq or None, message)."""
    prev = GENESIS
    for e in chain:
        if e["prev"] != prev:
            return False, e["seq"], f"Entry #{e['seq']} does not point to the hash of entry #{e['seq'] - 1}."
        if digest(e) != e["hash"]:
            return False, e["seq"], f"Entry #{e['seq']} was changed after it was written (hash mismatch)."
        prev = e["hash"]
    return True, None, f"All {len(chain)} entries verified: every SHA-256 hash matches."


def tamper_test(chain):
    """Demo: quietly add 500 points to a copy of an old entry and show the chain breaks.
    The real ledger is never changed."""
    if not chain:
        return {"ok": True, "msg": "The ledger is empty."}
    fake = copy.deepcopy(chain)
    victim = fake[0]
    victim["points"] += 500
    ok, seq, msg = verify(fake)
    return {"tampered_seq": victim["seq"], "detected": not ok, "broken_at": seq,
            "msg": f"Tamper test: added 500 fake points to entry #{victim['seq']} in a copy. "
                   + ("Caught: " + msg if not ok else "NOT caught!"),
            "real_ledger_ok": verify(chain)[0]}
