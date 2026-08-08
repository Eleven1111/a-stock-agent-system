import copy
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "daban-stock-picker" / "scripts" / "daban_candidate_api.py"
SPEC = importlib.util.spec_from_file_location("daban_candidate_api", SCRIPT)
daban = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daban)


def test_parse_time_minutes():
    assert daban.parse_time_minutes("09:35") == 575
    assert daban.parse_time_minutes("10:30") == 630
    assert daban.parse_time_minutes("bad") is None


def test_example_payload_passes_and_emits_record_payload():
    result = daban.evaluate_payload(daban.example_payload())

    assert result["blocked"] is False
    assert result["top_candidates"][0]["code"] == "600001"
    assert result["top_candidates"][0]["tradeability"]["status"] == "limit_up"
    assert result["top_candidates"][0]["record_payload"]["strategy_id"] == "daban:first_board_reseal_v2"
    assert result["top_candidates"][0]["event"]["schema"] == "daban_event_v2"
    assert result["top_candidates"][0]["event"]["evidence"]["primary_strategy_id"] == (
        "daban:first_board_reseal_v2"
    )


def test_six_question_two_no_blocks_candidate():
    payload = daban.example_payload()
    payload["market"]["sentiment_score"] = 6
    payload["candidates"][0]["is_leader"] = False
    payload["candidates"][0]["is_front_runner"] = False

    result = daban.evaluate_payload(payload)

    candidate = result["candidates"][0]
    assert result["blocked"] is True
    assert candidate["six_question_veto"]["no_count"] == 2
    assert any("六问否决" in reason for reason in candidate["block_reasons"])


def test_one_word_limit_up_is_not_tradeable():
    payload = daban.example_payload()
    payload["candidates"][0].update({"open": 11.0, "high": 11.0, "low": 11.0, "price": 11.0})

    result = daban.evaluate_payload(payload)

    candidate = result["candidates"][0]
    assert candidate["blocked"] is True
    assert candidate["tradeability"]["status"] == "limit_up_sealed"
    assert candidate["record_payload"] is None


def test_second_board_weak_to_strong_candidate_passes():
    payload = copy.deepcopy(daban.example_payload())
    payload["candidates"][0].update(
        {
            "pattern": "second_board_weak_to_strong",
            "prev_day_limitup_close": True,
            "auction_gap_pct": 1.2,
            "first_limitup_time": "09:42",
            "sector_companion_count": 2,
            "is_leader": False,
            "is_front_runner": True,
            "open": 10.8,
            "low": 10.7,
            "price": 11.0,
        }
    )

    result = daban.evaluate_payload(payload)

    candidate = result["top_candidates"][0]
    assert result["blocked"] is False
    assert candidate["pattern"] == "second_board_weak_to_strong"
    assert candidate["six_question_veto"]["no_count"] == 0
    assert candidate["record_payload"]["strategy_id"] == "daban:second_board_w2s_v2"


def test_six_question_time_window_is_pattern_specific():
    payload = daban.example_payload()
    candidate = payload["candidates"][0]
    candidate["first_limitup_time"] = "09:30"
    questions = daban._six_questions(candidate, payload["market"], 5)
    assert questions[4]["passed"] is False

    candidate["pattern"] = "second_board_weak_to_strong"
    candidate.update({"prev_day_limitup_close": True, "auction_gap_pct": 1.0})
    assert daban._six_questions(candidate, payload["market"], 5)[4]["passed"] is True


def test_reseal_quality_is_normalized_and_tail_seal_is_research_only():
    payload = daban.example_payload()
    payload["candidates"][0].update(
        {
            "first_seal": "09:52",
            "final_seal": "14:31",
            "open_board_count": 2,
            "cumulative_open_minutes": 23,
            "longest_open_minutes": 14,
            "reseal_volume": 123456,
            "final_seal_order_survival": 0.7,
        }
    )
    candidate = daban.evaluate_payload(payload)["candidates"][0]
    assert candidate["first_seal_time"] == "09:52"
    assert candidate["final_seal_time"] == "14:31"
    assert candidate["open_board_count"] == 2
    assert candidate["cumulative_open_minutes"] == 23
    assert candidate["longest_open_minutes"] == 14
    assert candidate["reseal_volume"] == 123456
    assert candidate["final_seal_survival"] == 0.7
    assert candidate["tail_seal_after_14_30"] is True
    assert candidate["research_only"] is True
    assert candidate["record_payload"] is None


def test_pending_t1_disposal_blocks_new_entry():
    payload = daban.example_payload()
    payload["portfolio"]["has_positions_to_dispose"] = True

    result = daban.evaluate_payload(payload)

    assert result["blocked"] is True
    assert any("T+1处置优先" in reason for reason in result["candidates"][0]["block_reasons"])
