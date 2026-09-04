"""日本語Office/PDF合成画像をTesseract/PaddleOCRで同一採点する隔離PoC。

製品のMD生成経路へOCR結果を混入させない。入力はchecked-inの合成fixture、正解は独立oracle、
出力は再採点可能なObservation JSONである。外部エンジンの追加時も ``observations`` 契約へ合わせる。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ORACLE = ROOT / "fixtures/eval/ocr_ja/oracle.json"
SCHEMA = "sherpa-ocr-engine-evaluation-v1"
OBSERVATION_SCHEMA = "sherpa-ocr-observations-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(path: Path) -> dict[str, Any]:
    files = sorted(item for item in path.rglob("*") if item.is_file())
    digest = hashlib.sha256()
    size = 0
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        item_hash = _sha256(item).encode("ascii")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(item_hash)
        size += item.stat().st_size
    return {"sha256": digest.hexdigest(), "file_count": len(files), "size_bytes": size}


def normalize(text: str) -> str:
    """文字精度用。表の空白差を除き、識別子の文字自体は同値化しすぎない。"""
    return "".join(character for character in unicodedata.normalize("NFKC", text) if not character.isspace())


def edit_distance(reference: str, candidate: str) -> int:
    """O(min(n,m)) memoryのLevenshtein距離。"""
    if len(reference) < len(candidate):
        reference, candidate = candidate, reference
    previous = list(range(len(candidate) + 1))
    for row, left in enumerate(reference, start=1):
        current = [row]
        for column, right in enumerate(candidate, start=1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (left != right)))
        previous = current
    return previous[-1]


def _bbox_intersects(left: list[float], right: list[float]) -> bool:
    return left[0] < right[2] and left[2] > right[0] and left[1] < right[3] and left[3] > right[1]


def _region_text(observations: Iterable[dict], bbox: list[float]) -> str:
    return "\n".join(
        str(item.get("text", ""))
        for item in observations
        if isinstance(item.get("bbox"), list) and len(item["bbox"]) == 4 and _bbox_intersects(item["bbox"], bbox)
    )


def evaluate_case(case: dict, observations: list[dict]) -> dict:
    checks = []
    kind_counts: dict[str, list[int]] = {}
    for region in case["regions"]:
        region_text = _region_text(observations, region["bbox"])
        term_results = []
        for term in region["terms"]:
            passed = normalize(term["text"]) in normalize(region_text)
            term_results.append({**term, "status": "pass" if passed else "fail"})
            counts = kind_counts.setdefault(term["kind"], [0, 0])
            counts[1] += 1
            counts[0] += int(passed)
        checks.append({
            "check_id": region["check_id"],
            "label": region["label"],
            "bbox": region["bbox"],
            "status": "pass" if all(term["status"] == "pass" for term in term_results) else "fail",
            "terms": term_results,
            "observed_text": region_text,
        })
    reference = normalize(case["reference_text"])
    recognized = normalize("\n".join(item.get("text", "") for item in observations))
    distance = edit_distance(reference, recognized)
    passed_terms = sum(term[0] for term in kind_counts.values())
    total_terms = sum(term[1] for term in kind_counts.values())
    return {
        "case_id": case["case_id"],
        "input": case["input"],
        "input_sha256": case["input_sha256"],
        "purpose": case["purpose"],
        "reference_characters": len(reference),
        "recognized_characters": len(recognized),
        "edit_distance": distance,
        "character_error_rate": round(distance / max(1, len(reference)), 6),
        "term_recall": {"passed": passed_terms, "total": total_terms, "rate": round(passed_terms / max(1, total_terms), 6)},
        "term_recall_by_kind": {
            kind: {"passed": counts[0], "total": counts[1], "rate": round(counts[0] / counts[1], 6)}
            for kind, counts in sorted(kind_counts.items())
        },
        "region_checks": checks,
        "observation_count": len(observations),
        "observations": observations,
    }


def load_oracle(path: Path) -> dict:
    oracle = json.loads(path.read_text(encoding="utf-8"))
    if oracle.get("schema") != "sherpa-ocr-ja-oracle-v1" or not isinstance(oracle.get("cases"), list):
        raise ValueError(f"unsupported OCR oracle: {path}")
    for case in oracle["cases"]:
        source = ROOT / case["input"]
        if not source.is_file():
            raise FileNotFoundError(source)
        if _sha256(source) != case["input_sha256"]:
            raise ValueError(f"fixture hash mismatch: {case['case_id']}")
        from PIL import Image
        with Image.open(source) as image:
            if list(image.size) != case["image_size"]:
                raise ValueError(f"fixture size mismatch: {case['case_id']}")
    return oracle


def parse_tesseract_tsv(payload: str) -> list[dict]:
    observations = []
    for row in csv.DictReader(io.StringIO(payload), delimiter="\t"):
        text = row.get("text", "").strip()
        if not text or row.get("level") != "5":
            continue
        left, top = int(row["left"]), int(row["top"])
        width, height = int(row["width"]), int(row["height"])
        confidence = float(row["conf"])
        observations.append({
            "text": text,
            "confidence": None if confidence < 0 else round(confidence / 100, 6),
            "bbox": [left, top, left + width, top + height],
            "line_id": f"{row['page_num']}:{row['block_num']}:{row['par_num']}:{row['line_num']}",
        })
    return observations


class TesseractEngine:
    def __init__(
        self, binary: Path, tessdata: Path, library_dir: Path | None, *, psm: int = 11, scale: float = 1.0,
    ):
        if scale < 1:
            raise ValueError("Tesseract scale must be at least 1")
        self.binary = binary
        self.tessdata = tessdata
        self.library_dir = library_dir
        self.psm = psm
        self.scale = scale
        self.env = os.environ.copy()
        self.env["TESSDATA_PREFIX"] = str(tessdata)
        if library_dir:
            current = self.env.get("LD_LIBRARY_PATH")
            self.env["LD_LIBRARY_PATH"] = str(library_dir) + (f":{current}" if current else "")
        version = subprocess.run(
            [str(binary), "--version"], check=True, capture_output=True, text=True, env=self.env,
        ).stdout.splitlines()[0]
        languages = {}
        for name in ("jpn.traineddata", "eng.traineddata"):
            model = tessdata / name
            if not model.is_file():
                raise FileNotFoundError(model)
            languages[name] = {"sha256": _sha256(model), "size_bytes": model.stat().st_size}
        self.metadata = {
            "engine": "tesseract",
            "version": version,
            "languages": "jpn+eng",
            "page_segmentation_mode": psm,
            "input_scale": scale,
            "language_artifacts": languages,
        }

    def predict(self, image: Path) -> list[dict]:
        command = [str(self.binary), str(image), "stdout", "-l", "jpn+eng", "--psm", str(self.psm), "tsv"]
        input_bytes = None
        if self.scale != 1:
            from PIL import Image
            with Image.open(image) as source:
                resized = source.resize(
                    (round(source.width * self.scale), round(source.height * self.scale)), Image.Resampling.LANCZOS,
                )
                buffer = io.BytesIO()
                resized.save(buffer, format="PNG")
            command[1] = "stdin"
            input_bytes = buffer.getvalue()
        completed = subprocess.run(command, check=True, capture_output=True, input=input_bytes, env=self.env)
        observations = parse_tesseract_tsv(completed.stdout.decode("utf-8"))
        if self.scale != 1:
            for observation in observations:
                observation["bbox"] = [round(value / self.scale, 3) for value in observation["bbox"]]
        return observations


def parse_paddle_result(result: Any) -> list[dict]:
    payload = result.json if hasattr(result, "json") else result
    if isinstance(payload, str):
        payload = json.loads(payload)
    values = payload.get("res", payload)
    texts = values.get("rec_texts") or []
    scores = values.get("rec_scores") or []
    boxes = values.get("rec_boxes") or values.get("rec_polys") or []
    if not (len(texts) == len(scores) == len(boxes)):
        raise ValueError("PaddleOCR result arrays differ in length")
    observations = []
    for index, (text, score, box) in enumerate(zip(texts, scores, boxes, strict=True)):
        if len(box) == 4 and not isinstance(box[0], (list, tuple)):
            bbox = [float(value) for value in box]
        else:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            bbox = [min(xs), min(ys), max(xs), max(ys)]
        observations.append({"text": str(text), "confidence": round(float(score), 6), "bbox": bbox, "line_id": str(index)})
    return observations


class PaddleEngine:
    def __init__(self, cache_home: Path, *, detection_model: str, recognition_model: str):
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(cache_home)
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        import paddle
        from paddleocr import PaddleOCR
        self.ocr = PaddleOCR(
            device="cpu",
            enable_mkldnn=False,
            text_detection_model_name=detection_model,
            text_recognition_model_name=recognition_model,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        model_root = cache_home / "official_models"
        models = {}
        for name in (detection_model, recognition_model):
            model_dir = model_root / name
            if not model_dir.is_dir():
                raise FileNotFoundError(model_dir)
            models[name] = _tree_digest(model_dir)
        self.metadata = {
            "engine": "paddleocr",
            "paddleocr_version": importlib.metadata.version("paddleocr"),
            "paddlepaddle_version": paddle.__version__,
            "device": "cpu",
            "enable_mkldnn": False,
            "document_orientation": False,
            "textline_orientation": False,
            "models": models,
        }

    def predict(self, image: Path) -> list[dict]:
        results = list(self.ocr.predict(str(image)))
        if len(results) != 1:
            raise ValueError(f"PaddleOCR returned {len(results)} results for one image")
        return parse_paddle_result(results[0])


def _summarize(cases: list[dict]) -> dict:
    checks = [check for case in cases for check in case["region_checks"]]
    terms = [term for check in checks for term in check["terms"]]
    return {
        "case_count": len(cases),
        "region_checks": {
            "passed": sum(check["status"] == "pass" for check in checks),
            "total": len(checks),
        },
        "term_recall": {
            "passed": sum(term["status"] == "pass" for term in terms),
            "total": len(terms),
            "rate": round(sum(term["status"] == "pass" for term in terms) / max(1, len(terms)), 6),
        },
        "macro_character_error_rate": round(sum(case["character_error_rate"] for case in cases) / max(1, len(cases)), 6),
        "elapsed_seconds": round(sum(case["elapsed_seconds"] for case in cases), 3),
    }


def evaluate_engine(engine: Any, oracle: dict) -> dict:
    cases = []
    for case in oracle["cases"]:
        started = time.monotonic()
        observations = engine.predict(ROOT / case["input"])
        evaluated = evaluate_case(case, observations)
        evaluated["elapsed_seconds"] = round(time.monotonic() - started, 3)
        cases.append(evaluated)
    return {"metadata": engine.metadata, "summary": _summarize(cases), "cases": cases}


def evaluate_external(path: Path, oracle: dict) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != OBSERVATION_SCHEMA or not isinstance(payload.get("cases"), dict):
        raise ValueError(f"unsupported external observation file: {path}")
    cases = []
    for case in oracle["cases"]:
        source = payload["cases"].get(case["case_id"])
        if not isinstance(source, dict) or not isinstance(source.get("observations"), list):
            raise ValueError(f"external observations missing: {case['case_id']}")
        evaluated = evaluate_case(case, source["observations"])
        evaluated["elapsed_seconds"] = float(source.get("elapsed_seconds", 0))
        cases.append(evaluated)
    return {"metadata": payload.get("metadata", {"engine": "external"}), "summary": _summarize(cases), "cases": cases}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tesseract/PaddleOCR/Nemotron等を同じ日本語画像oracleで採点")
    parser.add_argument("--engine", action="append", choices=("tesseract", "paddle"), default=[])
    parser.add_argument("--external", action="append", type=Path, default=[], help=f"{OBSERVATION_SCHEMA}形式の外部観測")
    parser.add_argument("--oracle", type=Path, default=DEFAULT_ORACLE)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tesseract-bin", type=Path, default=Path("/usr/bin/tesseract"))
    parser.add_argument("--tessdata", type=Path, default=Path("/usr/share/tesseract-ocr/5/tessdata"))
    parser.add_argument("--tesseract-library-dir", type=Path)
    parser.add_argument("--tesseract-psm", type=int, default=11)
    parser.add_argument("--tesseract-scale", type=float, default=1.0)
    parser.add_argument("--paddle-cache", type=Path, default=Path.home() / ".paddlex")
    parser.add_argument("--paddle-detection-model", default="PP-OCRv6_medium_det")
    parser.add_argument("--paddle-recognition-model", default="PP-OCRv6_medium_rec")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    if not args.engine and not args.external:
        raise SystemExit("--engine or --external is required")
    oracle = load_oracle(args.oracle)
    engines = []
    for name in args.engine:
        if name == "tesseract":
            engine = TesseractEngine(
                args.tesseract_bin,
                args.tessdata,
                args.tesseract_library_dir,
                psm=args.tesseract_psm,
                scale=args.tesseract_scale,
            )
        else:
            engine = PaddleEngine(
                args.paddle_cache,
                detection_model=args.paddle_detection_model,
                recognition_model=args.paddle_recognition_model,
            )
        engines.append(evaluate_engine(engine, oracle))
    engines.extend(evaluate_external(path, oracle) for path in args.external)
    report = {
        "schema": SCHEMA,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "oracle": args.oracle.resolve().relative_to(ROOT).as_posix(),
        "oracle_sha256": _sha256(args.oracle),
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "cpu": platform.processor() or None,
        },
        "engines": engines,
    }
    _write_json(args.out, report)
    print(json.dumps({"out": str(args.out), "engines": [item["metadata"]["engine"] for item in engines]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
