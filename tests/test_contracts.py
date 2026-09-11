import unittest

from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter, ValidationError

from schemas import DamageFlags, ScamAssessment, URLRiskResult
from state import UnHookState, create_initial_state


def assessment_payload():
    return {
        "scam_type": "unknown",
        "risk_level": "insufficient_info",
        "damage_stage": "none",
        "confidence": 0.5,
        "evidence": ["The sender requests opening an unverified link."],
        "unverified": ["Sender identity"],
        "immediate_actions": [],
        "damage_flags": {},
        "injection_detected": False,
    }


class ContractTests(unittest.TestCase):
    def test_url_status_accepts_five_values_and_preserves_existing_fields(self):
        adapter = TypeAdapter(URLRiskResult)
        for status in ("confirmed", "suspicious", "unverifiable", "clean", "malformed"):
            with self.subTest(status=status):
                payload = {"status": status, "blacklisted": False, "risk_score": 0, "signals": []}
                self.assertEqual(adapter.validate_python(payload), payload)

    def test_url_status_is_required_and_rejects_unknown_values(self):
        adapter = TypeAdapter(URLRiskResult)
        legacy = {"blacklisted": False, "risk_score": 0, "signals": []}
        for payload in (legacy, {**legacy, "status": "safe"}, {**legacy, "status": None}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    adapter.validate_python(payload)
        self.assertEqual(URLRiskResult.__required_keys__, {"status", "blacklisted", "risk_score", "signals"})

    def test_unknown_and_denied_facts_are_distinct(self):
        self.assertIsNone(DamageFlags().money_sent)
        self.assertIs(DamageFlags(money_sent=False).money_sent, False)
        parsed = ScamAssessment.model_validate(assessment_payload())
        self.assertIsNone(parsed.next_question)

    def test_output_constraints(self):
        for field, value in (
            ("confidence", -0.1), ("confidence", 1.1),
            ("evidence", []), ("risk_level", "safe"),
            ("damage_stage", "unknown"),
            ("damage_flags", {"info_exposed": ["account"] * 11}),
            ("immediate_actions", [
                {"priority": 1, "action": "Check", "contact": None}
            ] * 6),
            ("immediate_actions", [
                {"priority": 2, "action": "Second", "contact": None},
                {"priority": 1, "action": "First", "contact": None},
            ]),
        ):
            with self.subTest(field=field, value=value):
                payload = assessment_payload()
                payload[field] = value
                with self.assertRaises(ValidationError):
                    ScamAssessment.model_validate(payload)

    def test_threads_do_not_share_mutable_defaults(self):
        first, second = create_initial_state(), create_initial_state()
        first["info_exposed"].append("account")
        first["checklist"]["example"] = True
        first["pii_vault"]["token"] = "synthetic"
        self.assertEqual(second["info_exposed"], [])
        self.assertEqual(second["checklist"], {})
        self.assertEqual(second["pii_vault"], {})

    def test_langgraph_preserves_message_reducer_and_partial_updates(self):
        graph = StateGraph(UnHookState)
        graph.add_node("reply", lambda state: {
            "messages": [("ai", "Acknowledged")], "money_sent": True,
        })
        graph.add_edge(START, "reply")
        graph.add_edge("reply", END)
        initial = create_initial_state()
        initial["messages"] = [("human", "Example input")]
        result = graph.compile().invoke(initial)
        self.assertEqual([m.type for m in result["messages"]], ["human", "ai"])
        self.assertIs(result["money_sent"], True)
        self.assertIsNone(result["link_clicked"])


if __name__ == "__main__":
    unittest.main()
