from __future__ import annotations

from sherpa.eval import ocr_engine_poc


def test_edit_distance_and_normalization_keep_identifier_errors_visible():
    assert ocr_engine_poc.normalize(" OLD_CODE \n ７桁 ") == "OLD_CODE7桁"
    assert ocr_engine_poc.edit_distance("OLD_CODE", "0LD_CODE") == 1


def test_parse_tesseract_tsv_uses_word_boxes_and_confidence():
    payload = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "4\t1\t1\t1\t1\t0\t10\t20\t40\t12\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t10\t20\t40\t12\t98.5\t廃止\n"
    )
    assert ocr_engine_poc.parse_tesseract_tsv(payload) == [{
        "text": "廃止",
        "confidence": 0.985,
        "bbox": [10, 20, 50, 32],
        "line_id": "1:1:1:1",
    }]


def test_parse_paddle_result_accepts_rectangles_and_polygons():
    rectangle = {"res": {"rec_texts": ["現行"], "rec_scores": [0.9], "rec_boxes": [[1, 2, 5, 8]]}}
    assert ocr_engine_poc.parse_paddle_result(rectangle)[0]["bbox"] == [1.0, 2.0, 5.0, 8.0]
    polygon = {"res": {"rec_texts": ["廃止"], "rec_scores": [0.8], "rec_polys": [[[1, 2], [5, 2], [5, 8], [1, 8]]]}}
    assert ocr_engine_poc.parse_paddle_result(polygon)[0]["bbox"] == [1.0, 2.0, 5.0, 8.0]


def test_evaluate_case_requires_term_inside_its_source_region():
    case = {
        "case_id": "x",
        "input": "x.png",
        "input_sha256": "0" * 64,
        "purpose": "test",
        "reference_text": "OLD_CODE 廃止",
        "regions": [{
            "check_id": "x:row",
            "label": "row",
            "bbox": [0, 0, 100, 20],
            "terms": [{"text": "OLD_CODE", "kind": "identifier"}, {"text": "廃止", "kind": "status"}],
        }],
    }
    observations = [
        {"text": "OLD_CODE", "bbox": [1, 1, 20, 10], "confidence": 1.0},
        {"text": "廃止", "bbox": [1, 30, 20, 40], "confidence": 1.0},
    ]
    result = ocr_engine_poc.evaluate_case(case, observations)
    assert result["term_recall"] == {"passed": 1, "total": 2, "rate": 0.5}
    assert result["region_checks"][0]["status"] == "fail"


def test_checked_in_oracle_and_external_template_share_the_same_cases():
    oracle = ocr_engine_poc.load_oracle(ocr_engine_poc.DEFAULT_ORACLE)
    external = ocr_engine_poc.evaluate_external(
        ocr_engine_poc.ROOT / "fixtures/eval/ocr_ja/external_observations.example.json", oracle,
    )
    assert [case["case_id"] for case in external["cases"]] == [
        "office_screen", "scan_pdf", "hybrid_pdf", "deprecated_stamp",
    ]
    assert external["summary"]["term_recall"] == {"passed": 0, "total": 65, "rate": 0.0}
