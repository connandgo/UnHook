"""작업 묶음 6 검증: 설계서 2.5 RAG 구성과 5절 공유 계약.

가짜 임베딩을 주입하므로 API 키 없이 실행된다. 유사도 자체는 검증 대상이 아니고,
필터 3단계·`step_order` 정렬·폴백·무예외·연락처 테이블 일치를 검증한다.
"""

import hashlib
import json
import logging
import math
import unittest
from typing import get_args

from langchain_core.embeddings import Embeddings

import rag
from schemas import DamageStage, ScamType
from tools import get_scam_playbook


class BagEmbedding(Embeddings):
    """문자 2-gram 해시 기반 결정적 임베딩. 겹치는 글자가 많을수록 유사도가 높다."""

    _DIM = 256

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self._DIM
        grams = [text[i:i + 2] for i in range(len(text) - 1) if not text[i:i + 2].isspace()]
        for gram in grams:
            vec[int(hashlib.md5(gram.encode()).hexdigest(), 16) % self._DIM] += 1
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class FailingEmbedding(Embeddings):
    def embed_documents(self, texts):
        raise RuntimeError("embedding api down")

    def embed_query(self, text):
        raise RuntimeError("embedding api down")


def step_keys(result):
    return [s.split(rag._STEP_SEPARATOR)[0] for s in result["steps"]]


class RagDataTests(unittest.TestCase):
    """data/ 파일의 정합성. 설계서 2.5 전처리 행."""

    @classmethod
    def setUpClass(cls):
        cls.docs = rag.load_playbook_documents()
        cls.fallback = json.loads(rag.PLAYBOOK_FALLBACK_PATH.read_text(encoding="utf-8"))
        cls.labels = rag._load_contact_labels()

    def test_chunks_use_canonical_step_keys_and_known_contacts(self):
        keys = set(self.fallback["step_keys"])
        types = set(get_args(ScamType)) | {"any"}
        stages = set(get_args(DamageStage))
        self.assertGreater(len(self.docs), 20)
        for doc in self.docs:
            meta = doc.metadata
            with self.subTest(source=meta["source"], step=meta["step_key"]):
                self.assertIn(meta["step_key"], keys)
                self.assertTrue(set(meta["scam_types"]) <= types)
                self.assertTrue(meta["damage_stages"] and set(meta["damage_stages"]) <= stages)
                self.assertTrue(set(meta["contacts"]) <= set(self.labels))
                for stage in meta["damage_stages"]:
                    self.assertIn(meta["step_key"], self.fallback["step_order"][stage])

    def test_every_combination_has_multiple_sources(self):
        # 조합당 출처가 1개면 유사도 검색이 dict 조회로 퇴화한다 (설계서 2.5 전처리).
        for scam_type in set(get_args(ScamType)) - {"unknown"}:
            for stage in get_args(DamageStage):
                sources = {
                    d.metadata["source"] for d in self.docs
                    if stage in d.metadata["damage_stages"]
                    and ("any" in d.metadata["scam_types"] or scam_type in d.metadata["scam_types"])
                }
                with self.subTest(scam_type=scam_type, stage=stage):
                    self.assertGreaterEqual(len(sources), 2)

    def test_fallback_covers_every_stage_in_step_order(self):
        for stage in get_args(DamageStage):
            entries = self.fallback["by_damage_stage"][stage]
            order = self.fallback["step_order"][stage]
            keys = [e["step_key"] for e in entries]
            self.assertTrue(keys)
            self.assertEqual(keys, sorted(keys, key=order.index))


class RagSearchTests(unittest.TestCase):
    """search_playbook 동작. 설계서 2.5 검색·출력 형식·폴백 행."""

    @classmethod
    def setUpClass(cls):
        cls._threshold = rag.PLAYBOOK_SCORE_THRESHOLD
        rag.PLAYBOOK_SCORE_THRESHOLD = 0.05  # 가짜 임베딩 점수 스케일에 맞춘다.
        cls.labels = set(rag._load_contact_labels().values())
        logging.getLogger("rag").setLevel(logging.ERROR)

    @classmethod
    def tearDownClass(cls):
        rag.PLAYBOOK_SCORE_THRESHOLD = cls._threshold
        rag.reset_index()

    def setUp(self):
        rag.reset_index()
        rag.load_playbook_index(embeddings=BagEmbedding())
        self.assertTrue(rag.is_index_loaded())

    def test_ts02_c003_money_sent_order(self):
        result = rag.search_playbook("smishing", "money_sent")
        self.assertEqual(step_keys(result)[:3], ["지급정지 요청", "112 신고", "개인정보노출자 등록"])
        self.assertIn("송금한 은행 콜센터", result["contacts"])
        self.assertIn("경찰청 112", result["contacts"])

    def test_ts03_c001_loan_scam_money_sent_first_step_is_stop_payment(self):
        result = rag.search_playbook("loan_scam", "money_sent")
        self.assertEqual(step_keys(result)[0], "지급정지 요청")

    def test_ts02_c001_app_installed_starts_with_malware_removal(self):
        result = rag.search_playbook("smishing", "app_installed")
        self.assertEqual(step_keys(result)[:2], ["악성앱 삭제", "112 신고"])
        self.assertIn("다른 사람", result["steps"][0])

    def test_ts04_gov_impersonation_none_verifies_via_official_number(self):
        result = rag.search_playbook("gov_impersonation", "none")
        self.assertEqual(step_keys(result)[0], "공식 번호로 직접 확인")
        self.assertIn("검찰청 1301", result["contacts"])

    def test_step_format_and_limits(self):
        for scam_type in get_args(ScamType):
            for stage in get_args(DamageStage):
                result = rag.search_playbook(scam_type, stage)
                with self.subTest(scam_type=scam_type, stage=stage):
                    self.assertTrue(1 <= len(result["steps"]) <= rag.PLAYBOOK_TOP_K)
                    for step in result["steps"]:
                        self.assertIn(rag._STEP_SEPARATOR, step)
                    self.assertEqual(len(step_keys(result)), len(set(step_keys(result))))
                    self.assertTrue(result["contacts"])
                    self.assertTrue(set(result["contacts"]) <= self.labels)

    def test_unknown_type_uses_common_chunks_only(self):
        result = rag.search_playbook("unknown", "money_sent")
        self.assertEqual(step_keys(result)[0], "지급정지 요청")
        for step in result["steps"]:
            self.assertNotIn("대출 심사", step)
            self.assertNotIn("투자", step)

    def test_threshold_miss_falls_back_to_json(self):
        rag.PLAYBOOK_SCORE_THRESHOLD = 0.99
        try:
            result = rag.search_playbook("smishing", "money_sent")
        finally:
            rag.PLAYBOOK_SCORE_THRESHOLD = 0.05
        fallback = json.loads(rag.PLAYBOOK_FALLBACK_PATH.read_text(encoding="utf-8"))
        expected = [e["step_key"] for e in fallback["by_damage_stage"]["money_sent"]]
        self.assertEqual(step_keys(result), expected[:rag.PLAYBOOK_TOP_K])

    def test_invalid_inputs_never_raise(self):
        self.assertTrue(rag.search_playbook("smishing", "not_a_stage")["steps"])
        self.assertTrue(rag.search_playbook("not_a_type", "app_installed")["steps"])
        self.assertTrue(rag.search_playbook("", "")["steps"])

    def test_query_embedding_failure_falls_back(self):
        rag._INDEX.embedding = FailingEmbedding()
        result = rag.search_playbook("smishing", "money_sent")
        self.assertEqual(step_keys(result)[0], "지급정지 요청")

    def test_tool_wrapper_delegates_to_rag(self):
        # tools.get_scam_playbook은 예외를 잡지 않으므로 여기서 결과가 나오면 연결이 성립한다.
        result = get_scam_playbook.invoke({"scam_type": "smishing", "damage_stage": "money_sent"})
        self.assertEqual(step_keys(result)[0], "지급정지 요청")


class RagLoadTests(unittest.TestCase):
    """사전 로딩·캐싱. 설계서 2.5 사전 로딩 행."""

    def tearDown(self):
        rag.reset_index()

    def test_index_load_failure_is_absorbed_and_search_still_works(self):
        rag.reset_index()
        logging.getLogger("rag").setLevel(logging.ERROR)
        rag.load_playbook_index(embeddings=FailingEmbedding())
        self.assertFalse(rag.is_index_loaded())
        result = rag.search_playbook("smishing", "money_sent")
        self.assertEqual(step_keys(result)[0], "지급정지 요청")

    def test_load_is_attempted_once_unless_forced(self):
        rag.reset_index()
        rag.load_playbook_index(embeddings=BagEmbedding())
        first = rag._INDEX
        rag.load_playbook_index(embeddings=BagEmbedding())
        self.assertIs(rag._INDEX, first)
        rag.load_playbook_index(embeddings=BagEmbedding(), force=True)
        self.assertIsNot(rag._INDEX, first)


if __name__ == "__main__":
    unittest.main()
