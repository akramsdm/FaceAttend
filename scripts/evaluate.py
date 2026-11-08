
r"""Evaluate the SAME Recognizer pipeline on eval_data (no invented results).

Usage:
  .\.venv\Scripts\python.exe -m scripts.evaluate [--data eval_data] [--out eval_results] [--steps 101]

Input layout:
  eval_data/<person>/enrol/*.jpg                 (enrolment / gallery for that person)
  eval_data/<person>/<condition>/*.jpg           (probes; conditions e.g. normal,dim,backlit,glasses,angle,other_day)
  eval_data/spoof/<person>/*.jpg                 (optional spoof probes)

Notes:
- "spoof" is a reserved top-level folder name (not a person). All other top-level
  folders are treated as person identities.
- Uses app.recognition.Recognizer (same analyse_frame/embed path as the app).
- Genuine scores = probe vs its own person (best of that person's enrol embeddings).
  Impostor scores = probe vs every other enrolled person (each reduced to that
  person's best enrol embedding). Images failing quality checks are listed
  separately and excluded from scoring.
- Writes eval_results/<timestamp>/ : results.csv, sweep.csv, metrics.json,
  score_histogram.png, far_frr_curve.png. metrics.json includes the lowest
  threshold at which FAR is 0 on this sample and the FRR at that point.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

# Headless plotting for CLI use.
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import rules  # noqa: E402
from app.eval_metrics import far_frr_at_threshold, far_zero_operating_point, sweep_thresholds  # noqa: E402
from app.recognition import Recognizer  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
RESERVED_TOP = {"spoof"}


def _images(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def _rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _load_embedding(recognizer: Recognizer, path: Path) -> tuple[np.ndarray | None, str | None]:
    data = path.read_bytes()
    try:
        analysis = recognizer.analyse_frame(data)
    except ValueError as exc:
        return None, f"decode_error:{exc}"
    if analysis.status != "ok" or analysis.face is None or analysis.frame is None:
        return None, str(analysis.status)
    try:
        emb = recognizer.embed(analysis.frame, analysis.face)
    except ValueError as exc:
        return None, f"embed_error:{exc}"
    return emb.astype(np.float32, copy=False), None


def build_gallery(recognizer: Recognizer, data_dir: Path) -> tuple[dict[str, list[np.ndarray]], list[dict[str, str]]]:
    gallery: dict[str, list[np.ndarray]] = {}
    rejected: list[dict[str, str]] = []
    for person_dir in sorted(p for p in data_dir.iterdir() if p.is_dir() and p.name not in RESERVED_TOP):
        enrol_dir = person_dir / "enrol"
        vecs: list[np.ndarray] = []
        for img in _images(enrol_dir):
            emb, reason = _load_embedding(recognizer, img)
            if emb is None:
                rejected.append({"kind": "enrol", "person": person_dir.name, "path": _rel(img, data_dir), "reason": reason or "unknown"})
            else:
                vecs.append(emb)
        if vecs:
            gallery[person_dir.name] = vecs
        else:
            # keep person visible in reporting? no gallery entry means cannot score as enrolled identity.
            pass
    return gallery, rejected


def score_probe(probe: np.ndarray, person: str, gallery: dict[str, list[np.ndarray]]) -> tuple[float | None, float | None, dict[str, float]]:
    """Return (genuine_best, best_impostor_across_others, per_person_best)."""
    per_person: dict[str, float] = {}
    for pid, vecs in gallery.items():
        if not vecs:
            continue
        per_person[pid] = max(rules._cosine_similarity(probe, v) for v in vecs)  # uses same rule math
    genuine = per_person.get(person)
    impostors = [s for pid, s in per_person.items() if pid != person]
    best_impostor = max(impostors) if impostors else None
    return genuine, best_impostor, per_person


def evaluate(recognizer: Recognizer, data_dir: Path) -> dict[str, Any]:
    gallery, enrol_rejected = build_gallery(recognizer, data_dir)
    probe_rows: list[dict[str, Any]] = []
    probe_rejected: list[dict[str, str]] = list(enrol_rejected)
    genuine_scores: list[float] = []
    impostor_scores: list[float] = []
    condition_genuine: defaultdict[str, list[float]] = defaultdict(list)
    condition_impostor: defaultdict[str, list[float]] = defaultdict(list)

    # Person probes
    for person_dir in sorted(p for p in data_dir.iterdir() if p.is_dir() and p.name not in RESERVED_TOP):
        person = person_dir.name
        for cond_dir in sorted(p for p in person_dir.iterdir() if p.is_dir() and p.name != "enrol"):
            condition = cond_dir.name
            for img in _images(cond_dir):
                emb, reason = _load_embedding(recognizer, img)
                if emb is None:
                    probe_rejected.append({"kind": "probe", "person": person, "condition": condition, "path": _rel(img, data_dir), "reason": reason or "unknown"})
                    probe_rows.append({"path": _rel(img, data_dir), "person": person, "condition": condition, "kind": "probe", "status": reason or "unknown"})
                    continue
                genuine, best_impostor, per_person = score_probe(emb, person, gallery)
                row: dict[str, Any] = {
                    "path": _rel(img, data_dir),
                    "person": person,
                    "condition": condition,
                    "kind": "probe",
                    "status": "ok",
                    "genuine_best": genuine,
                    "best_impostor": best_impostor,
                }
                # record per-person columns for transparency
                for pid, s in per_person.items():
                    row[f"score__{pid}"] = s
                probe_rows.append(row)
                if genuine is not None:
                    genuine_scores.append(float(genuine))
                    condition_genuine[condition].append(float(genuine))
                if best_impostor is not None:
                    impostor_scores.append(float(best_impostor))
                    condition_impostor[condition].append(float(best_impostor))

    # Spoof probes
    spoof_rows: list[dict[str, Any]] = []
    spoof_dir = data_dir / "spoof"
    if spoof_dir.exists():
        for person_dir in sorted(p for p in spoof_dir.iterdir() if p.is_dir()):
            target = person_dir.name
            for img in _images(person_dir):
                emb, reason = _load_embedding(recognizer, img)
                if emb is None:
                    probe_rejected.append({"kind": "spoof", "person": target, "condition": "spoof", "path": _rel(img, data_dir), "reason": reason or "unknown"})
                    spoof_rows.append({"path": _rel(img, data_dir), "person": target, "condition": "spoof", "kind": "spoof", "status": reason or "unknown"})
                    continue
                # score against claimed/target person and all enrolled
                genuine, best_impostor, per_person = score_probe(emb, target, gallery)
                target_score = genuine  # similarity to target identity
                spoof_rows.append({
                    "path": _rel(img, data_dir),
                    "person": target,
                    "condition": "spoof",
                    "kind": "spoof",
                    "status": "ok",
                    "target_best": target_score,
                    "best_overall": max(per_person.values()) if per_person else None,
                })

    sweep = sweep_thresholds(genuine_scores, impostor_scores, steps=101)
    far_zero = far_zero_operating_point(sweep)
    # per-condition at a reference point: use FAR=0 point if exists else 0.5
    ref_t = float(far_zero["threshold"]) if far_zero else 0.5
    per_condition = {}
    for cond in sorted(set(condition_genuine) | set(condition_impostor)):
        per_condition[cond] = far_frr_at_threshold(condition_genuine.get(cond, []), condition_impostor.get(cond, []), ref_t)

    # spoof pass rate at reference threshold: a spoof "passes" if its target_best >= ref_t
    spoof_ok = [r for r in spoof_rows if r.get("status") == "ok"]
    spoof_pass = [r for r in spoof_ok if r.get("target_best") is not None and float(r["target_best"]) >= ref_t]
    spoof_summary = {
        "reference_threshold": ref_t,
        "total": len(spoof_rows),
        "scored": len(spoof_ok),
        "passed_as_target": len(spoof_pass),
        "pass_rate": (len(spoof_pass) / len(spoof_ok)) if spoof_ok else None,
    }

    metrics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "enrolled_people": sorted(gallery.keys()),
        "enrol_embedding_counts": {k: len(v) for k, v in sorted(gallery.items())},
        "genuine_count": len(genuine_scores),
        "impostor_count": len(impostor_scores),
        "genuine_scores_summary": _summary(genuine_scores),
        "impostor_scores_summary": _summary(impostor_scores),
        "far_zero_point": far_zero,
        "reference_threshold": ref_t,
        "reference_metrics_overall": far_frr_at_threshold(genuine_scores, impostor_scores, ref_t),
        "per_condition_at_reference": per_condition,
        "spoof": spoof_summary,
        "quality_rejected_count": len(probe_rejected),
        "quality_rejected": probe_rejected,
        "notes": "No images or scores invented. FAR=0 point is the lowest threshold on the sweep grid (steps=101 over observed score range) where FAR==0 on this sample; None means FAR never reached 0 on-grid.",
    }
    return {
        "metrics": metrics,
        "sweep": sweep,
        "probe_rows": probe_rows,
        "spoof_rows": spoof_rows,
        "genuine_scores": genuine_scores,
        "impostor_scores": impostor_scores,
    }


def _summary(vals: list[float]) -> dict[str, float] | None:
    if not vals:
        return None
    a = np.asarray(vals, dtype=np.float64)
    return {"min": float(a.min()), "max": float(a.max()), "mean": float(a.mean()), "std": float(a.std(ddof=0)), "count": int(a.size)}


def write_outputs(result: dict[str, Any], out_root: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / ts
    out.mkdir(parents=True, exist_ok=True)

    # results.csv
    rows = list(result["probe_rows"]) + list(result["spoof_rows"])
    if rows:
        # stable column order: common first, then per-person score__* sorted
        common = ["path", "person", "condition", "kind", "status", "genuine_best", "best_impostor", "target_best", "best_overall"]
        extra = sorted({k for r in rows for k in r.keys() if k not in common})
        cols = [c for c in common if any(c in r for r in rows)] + extra
        with (out / "results.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
    else:
        (out / "results.csv").write_text("path,person,condition,kind,status\n", encoding="utf-8")

    # sweep.csv
    sweep = result["sweep"]
    if sweep:
        with (out / "sweep.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(sweep[0].keys()))
            w.writeheader()
            w.writerows(sweep)

    # metrics.json
    (out / "metrics.json").write_text(json.dumps(result["metrics"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # plots
    g = result["genuine_scores"]
    i = result["impostor_scores"]
    if g or i:
        fig, ax = plt.subplots(figsize=(7, 4))
        if g:
            ax.hist(g, bins=30, alpha=0.6, label=f"genuine (n={len(g)})")
        if i:
            ax.hist(i, bins=30, alpha=0.6, label=f"impostor (n={len(i)})")
        ax.set_xlabel("Cosine similarity")
        ax.set_ylabel("Count")
        ax.set_title("Genuine vs impostor score distribution")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out / "score_histogram.png", dpi=150)
        plt.close(fig)

    if sweep:
        th = [r["threshold"] for r in sweep]
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.plot(th, [r["far"] for r in sweep], label="FAR")
        ax.plot(th, [r["frr"] for r in sweep], label="FRR")
        ax.set_xlabel("Threshold")
        ax.set_ylabel("Rate")
        ax.set_title("FAR / FRR curve")
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(out / "far_frr_curve.png", dpi=150)
        plt.close(fig)

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the face recognizer on eval_data using the same Recognizer path as the app."
    )
    parser.add_argument("--data", type=Path, default=ROOT / "eval_data", help="Eval data root (default: eval_data)")
    parser.add_argument("--out", type=Path, default=ROOT / "eval_results", help="Output root (default: eval_results)")
    parser.add_argument("--steps", type=int, default=101, help="Threshold sweep steps (default: 101)")
    args = parser.parse_args(argv)

    data_dir = args.data
    if not data_dir.exists():
        print(f"eval data directory not found: {data_dir}")
        print("No images or results invented. Create eval_data/<person>/enrol/*.jpg and probe folders, then re-run.")
        return 2

    recognizer = Recognizer()  # SAME code path as the app
    result = evaluate(recognizer, data_dir)
    # allow overriding sweep steps from CLI (evaluate() uses 101 internally; re-sweep for consistency)
    if args.steps != 101:
        result["sweep"] = sweep_thresholds(result["genuine_scores"], result["impostor_scores"], steps=args.steps)
        result["metrics"]["far_zero_point"] = far_zero_operating_point(result["sweep"])

    out = write_outputs(result, args.out)
    m = result["metrics"]
    print(f"Wrote evaluation outputs to: {out}")
    print(f"  enrolled={len(m['enrolled_people'])} genuine={m['genuine_count']} impostor={m['impostor_count']} rejected={m['quality_rejected_count']}")
    fz = m.get("far_zero_point")
    if fz:
        print(f"  FAR=0 point (lowest on-grid): threshold={fz['threshold']:.6f} FRR={fz['frr']:.6f} FAR={fz['far']:.6f}")
    else:
        print("  FAR=0 point: none on the sweep grid for this sample")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
