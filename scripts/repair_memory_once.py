#!/usr/bin/env python3
"""Explicit historical targets only; defaults to read-only dry-run.

Manifest: {"owner_id": "...", "items": [{"episode_id": "...",
"job_ids": ["..."], "mode": "full|summary-only"}]}.
Stage accepts engine-verified results keyed by episode ID, each containing
{\"source_fingerprint\": \"hash read before generation\", \"result\": {\"episode\": ...,
\"memories\": [...]}}. It does not accept unbound model candidates.
Generate calls the real Dreamer/Verifier with current config and a read-only source
snapshot, then stages its result directly to --output (0600, atomic, no overwrite).
Run generate/stage on train-A. Transfer the frozen output back for apply; never
regenerate it during a retry. Default dry-run never invokes providers.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.memory_repair import RepairConflict, check_target, fingerprint, stage_repair
from backend.memory_store import MemoryStore


def load(path):
    return json.loads(Path(path).read_text())


def save_new(path, value):
    # Serialize first, then atomically publish without replacing prior output.
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".memory-repair-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


async def generate(store, source, target):
    from backend.memory_engine import MemoryEngine

    engine = MemoryEngine(store)
    try:
        return await engine.prepare_episode_repair(
            source, mode=target["mode"], job_ids=target["job_ids"])
    finally:
        await engine.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--action", choices=("dry-run", "generate", "stage", "apply"), default="dry-run")
    parser.add_argument("--results", type=Path, help="engine-verified results keyed by episode ID")
    parser.add_argument("--prepared", type=Path, help="frozen stage output (required for apply)")
    parser.add_argument("--output", type=Path, help="new private stage output file")
    args = parser.parse_args(argv)
    manifest = load(args.manifest)
    owner, items = manifest["owner_id"], manifest["items"]
    if not isinstance(owner, str) or not owner.strip() or not items:
        parser.error("explicit owner and nonempty target list required")
    if len({item["episode_id"] for item in items}) != len(items):
        parser.error("duplicate episode targets")
    manifest_hash = fingerprint(manifest)
    # Even stage opens in SQLite mode=ro: no migrations, permissions or WAL writes.
    store = MemoryStore(args.db, read_only=True)
    if args.action == "apply":
        if not args.prepared:
            parser.error("apply requires --prepared")
        bundle = load(args.prepared)
        if bundle["manifest_fingerprint"] != manifest_hash:
            raise RepairConflict("manifest changed")
        prepared = bundle["items"]
        if len(prepared) != len(items):
            raise RepairConflict("prepared target count mismatch")
        for target, result in zip(items, prepared):
            if (result["owner_id"] != owner or result["episode_id"] != target["episode_id"]
                    or result["mode"] != target["mode"]
                    or result["job_ids"] != sorted(target["job_ids"])):
                raise RepairConflict("prepared target differs from explicit manifest")
        # Reject missing paths before writable construction could create an empty DB.
        if not args.db.is_file():
            raise RepairConflict("database missing")
        for result in prepared:
            value = dict(result)
            digest = value.pop("prepared_fingerprint", None)
            if value.get("version") != 1 or fingerprint(value) != digest:
                raise RepairConflict("frozen result fingerprint mismatch")
        store = MemoryStore.open_existing_for_repair(args.db)
        for result in prepared:
            # Each receipt is independently durable; stop on first conflict.
            print(json.dumps(store.apply_memory_repair(result), ensure_ascii=False), flush=True)
        return 0
    if args.action == "stage" and (not args.results or not args.output):
        parser.error("stage requires --results and --output")
    if args.action == "generate" and (not args.output or args.results):
        parser.error("generate requires --output and does not accept --results")
    if args.action in {"stage", "generate"} and os.path.lexists(args.output):
        raise RepairConflict("output already exists; refusing to regenerate or overwrite")
    results = load(args.results) if args.action == "stage" else {}
    reports, staged = [], []
    for target in items:
        episode_id = target["episode_id"]
        report = {"owner_id": owner, **target}
        try:
            source = store.repair_snapshot(owner, episode_id)
            report.update(source_fingerprint=fingerprint(source),
                          jobs=[{"id": j["id"], "status": j["status"], "kind": j["kind"]}
                                for j in source["jobs"]],
                          memory_ids=[m["id"] for m in source["memories"]],
                          artifact_ids=[a["id"] for a in source["artifacts"]],
                          orphan_memory_ids=[m["id"] for m in source["orphans"]],
                          active_jobs=source["active_jobs"])
            check_target(source, target["mode"], target["job_ids"])
            report["status"] = "eligible"
            if args.action in {"stage", "generate"}:
                if args.action == "generate":
                    try:
                        generated = asyncio.run(generate(store, source, target))
                    except RepairConflict:
                        raise
                    except Exception as exc:
                        # Provider text may contain credentials or source evidence.
                        raise RepairConflict(
                            f"generation failed ({type(exc).__name__})") from None
                    report["duplicates"] = generated["duplicates"]
                else:
                    generated = results[episode_id]
                if generated.get("source_fingerprint") != fingerprint(source):
                    raise RepairConflict("generation source snapshot changed")
                staged.append(stage_repair(source, mode=target["mode"],
                                           job_ids=target["job_ids"], result=generated["result"]))
        except RepairConflict as exc:
            report.update(status="manual_review", reason=str(exc))
        reports.append(report)
    print(json.dumps({"manifest_fingerprint": manifest_hash, "items": reports},
                     ensure_ascii=False, indent=2))
    if any(r["status"] != "eligible" for r in reports):
        return 2
    if args.action in {"stage", "generate"}:
        # Recheck every target after the last generation, before publishing any.
        for target, prepared in zip(items, staged):
            current = store.repair_snapshot(owner, target["episode_id"])
            if fingerprint(current) != prepared["source_fingerprint"]:
                raise RepairConflict("source snapshot changed before publishing")
        save_new(args.output, {"manifest_fingerprint": manifest_hash, "items": staged,
                               "generation_reports": reports})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
