"""작업 묶음 5 (tools.py, memory.py) 검증.

설계서 4.2의 TS-01·TS-05 기대값과 2.5의 에러 처리 규약을 확인한다.
외부 API와 Store는 주입·InMemoryStore로 대체해 네트워크 없이 돌아간다.
"""

import json
import unittest
import urllib.error

import guards
from langchain.tools import ToolRuntime
from langgraph.store.memory import InMemoryStore

import memory
import tools
from state import RuntimeContext


def make_runtime(user_id="U001", store=None, messages=None):
    """Tool에 주입되는 ToolRuntime을 테스트용으로 만든다."""
    return ToolRuntime(
        state={"messages": list(messages or [])},
        context=RuntimeContext(user_id=user_id) if user_id else None,
        config={}, stream_writer=None, tool_call_id="test-call", store=store,
    )


class CheckUrlRiskTests(unittest.TestCase):
    def test_ts01_smishing_url_reports_three_signals(self):
        """4.2 TS-01-C001: 비정상 TLD·유사 도메인·단축형 경로 세 신호."""
        result = tools.analyze_url("http://vv-cj.top/x")
        joined = " ".join(result["signals"])
        self.assertIn("비정상 TLD .top", joined)
        self.assertIn("유사 도메인", joined)
        self.assertIn("단축형 경로", joined)
        self.assertTrue(result["blacklisted"])
        self.assertGreaterEqual(result["risk_score"], 50)

    def test_malformed_url_returns_zero_without_raising(self):
        """2.5: 형식 불명 URL이면 risk_score=0, signals=["형식 불명"] (예외 미발생)."""
        for bad in ("", "   ", "이게 뭐야", "http://", "https:///path"):
            with self.subTest(url=bad):
                result = tools.analyze_url(bad)
                self.assertEqual(result["risk_score"], 0)
                self.assertEqual(result["signals"], ["형식 불명"])
                self.assertFalse(result["blacklisted"])

    def test_ip_host_and_shortener_are_flagged(self):
        ip_result = tools.analyze_url("http://192.168.10.24/login")
        self.assertIn("IP 주소를 직접 사용", " ".join(ip_result["signals"]))
        self.assertEqual(ip_result["risk_score"], tools._DECISIVE_SCORES["ip_host"])

        short_result = tools.analyze_url("https://bit.ly/3abcd")
        self.assertIn("단축 URL", " ".join(short_result["signals"]))

    def test_ordinary_domain_has_no_false_alarm(self):
        result = tools.analyze_url("https://www.naver.com/news/article/123")
        self.assertFalse(result["blacklisted"])
        self.assertEqual(result["risk_score"], 0)
        self.assertEqual(result["signals"], ["알려진 위험 신호 없음"])

    def test_known_legit_domains_are_not_flagged(self):
        """정식 도메인을 사칭으로 잡으면 안 된다.

        브랜드 부분 문자열 매칭은 cjlogistics.com(CJ대한통운 정식 도메인)과
        kakaostory.com을 유사 도메인으로 잡았다. is_known_legit()이 앞에서 거른다.
        """
        for url in (
            "https://www.cjlogistics.com", "https://kakaostory.com",
            "https://story.kakao.com", "https://obank.kbstar.com",
            "https://www.cj.co.kr", "https://www.police.go.kr",
            "https://govtech.io", "https://toss.im", "https://blog.naver.com",
        ):
            with self.subTest(url=url):
                result = tools.analyze_url(url)
                self.assertEqual(result["risk_score"], 0)
                self.assertEqual(result["signals"], ["알려진 위험 신호 없음"])

    def test_decisive_signals_outrank_cumulative_ones(self):
        """단독으로 확정인 신호는 보강 신호 합계보다 높게 나와야 한다.

        @ 위장은 실제 접속지를 바꾸는 기법인데, 단순 합산에서는 .top 하나(20)와
        비슷한 20점이었다.
        """
        at_sign = tools.analyze_url("http://www.kbstar.com@evil.ru/login")
        self.assertEqual(at_sign["risk_score"], tools._DECISIVE_SCORES["at_sign"])

        weak = tools.analyze_url("http://unknown-site.top/ab")
        self.assertLess(weak["risk_score"], at_sign["risk_score"])

    def test_brand_in_subdomain_is_detected(self):
        """kakao.com.evil.ru처럼 브랜드를 하위 도메인에 넣은 위장을 잡는다."""
        result = tools.analyze_url("http://kakao.com.evil.ru/login")
        self.assertIn("유사 도메인", " ".join(result["signals"]))
        self.assertGreater(result["risk_score"], 0)

    def test_shortener_is_reported_as_unverifiable(self):
        """단축 URL은 위험이 아니라 목적지 확인 불가다 (설계서 1.5 안정성)."""
        result = tools.analyze_url("https://bit.ly/3xK9p")
        self.assertIn("확인 불가", " ".join(result["signals"]))
        self.assertLess(result["risk_score"], 50)

    def test_risk_score_never_exceeds_100(self):
        result = tools.analyze_url("http://vv-cj.top/x@evil")
        self.assertLessEqual(result["risk_score"], 100)


class VerifyCallerNumberTests(unittest.TestCase):
    @staticmethod
    def _companies(*_args):
        return [
            {"kor_co_nm": "국민은행", "cal_tel": "1588-9999"},
            {"kor_co_nm": "신한은행", "cal_tel": "1577-8000"},
        ]

    def test_official_number_matches(self):
        result = tools.verify_number("1588-9999", "국민은행", fetch=self._companies)
        self.assertIs(result["is_official"], True)
        self.assertEqual(result["company"], "국민은행")
        self.assertIn("15889999", result["official_numbers"])

    def test_unlisted_number_is_not_official(self):
        result = tools.verify_number("010-1234-5678", "국민은행", fetch=self._companies)
        self.assertIs(result["is_official"], False)

    def test_lookup_failure_yields_none_after_three_attempts(self):
        """2.5: 타임아웃 3회 재시도 후 is_official=None → unverified 기록."""
        attempts = []

        def failing(*_args):
            attempts.append(1)
            raise urllib.error.URLError("timeout")

        result = tools.verify_number("1588-9999", "국민은행", fetch=failing)
        self.assertIsNone(result["is_official"])
        self.assertEqual(result["official_numbers"], [])
        self.assertEqual(len(attempts), tools.FINLIFE_MAX_ATTEMPTS)


class MemoryMatchTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.past_text = "[택배] 주소 불일치로 반송 http://vv-cj.top/x 010-1111-2222"
        record = memory.build_record(
            "택배 사칭 스미싱 문자 신고",
            source_text=self.past_text,
            scam_type="smishing",
            reported=True,
        )
        memory.save_report(self.store, "U001", record)

    def test_ts05_same_domain_and_phrase_match(self):
        """4.2 TS-05-C001: 2주 뒤 다른 경로로 와도 도메인·문구가 일치."""
        history = memory.load_history(self.store, "U001")
        matches = memory.match_history(
            history, "[택배] 주소지 불일치 반송 예정(http://vv-cj.top/k2) 또 왔어요"
        )
        fields = {m["field"] for m in matches}
        self.assertIn("domain", fields)
        self.assertIn("phrase", fields)
        self.assertTrue(all(m["reported"] for m in matches))

    def test_unrelated_input_has_no_match(self):
        history = memory.load_history(self.store, "U001")
        self.assertEqual(memory.match_history(history, "오늘 점심 뭐 먹지"), [])

    def test_history_is_isolated_per_user(self):
        self.assertEqual(memory.load_history(self.store, "U002"), [])

    def test_record_keeps_no_pii_original(self):
        """3.1: 원문은 보관하지 않고 정규화된 값만 저장한다."""
        _, record = memory.load_history(self.store, "U001")[0]
        self.assertNotIn(self.past_text, str(record))
        self.assertEqual(record["domains"], ["vv-cj.top"])
        # 1.5 보안: 전화번호는 해시로만 저장하고 숫자 원문은 어디에도 남지 않는다.
        self.assertEqual(record["phones"], [memory.hash_identifier("01011112222")])
        self.assertNotIn("01011112222", str(record))
        self.assertNotIn("1111", record["phrase"])

    def test_prompt_block_masks_phone_number(self):
        history = memory.load_history(self.store, "U001")
        matches = memory.match_history(history, "010-1111-2222 에서 또 전화 왔어")
        block = memory.summarize_for_prompt(matches)
        self.assertIn("2222", block)
        self.assertNotIn("01011112222", block)

    def test_history_keeps_only_five_records(self):
        """3.1: report_history는 최근 5건."""
        for index in range(7):
            memory.save_report(
                self.store, "U002",
                memory.build_record(f"사건 {index}", source_text=f"case{index}.top"),
            )
        self.assertEqual(len(memory.load_history(self.store, "U002")),
                         memory.MAX_HISTORY_RECORDS)

    def test_empty_prompt_block_when_no_match(self):
        self.assertEqual(memory.summarize_for_prompt([]), "")


class ReportToAuthorityTests(unittest.TestCase):
    """4.2 TS-03-C004. 승인 자체는 HumanInTheLoopMiddleware(작업 묶음 2)가 담당하므로,
    여기서는 승인 이후 동작만 확인한다."""

    def test_accepted_report_is_saved_to_store_with_receipt(self):
        store = InMemoryStore()
        result = tools.report_to_authority.func(
            "smishing", "http://vv-cj.top/x", "택배 사칭 문자 신고",
            make_runtime("U001", store),
        )
        self.assertIs(result, True)
        history = memory.load_history(store, "U001")
        self.assertEqual(len(history), 1)
        _, record = history[0]
        self.assertTrue(record["reported"])
        self.assertTrue(record["receipt_no"].startswith("UH-SMI-"))

    def test_missing_user_id_raises_instead_of_guessing(self):
        """2.1: 모델이 임의로 다른 사용자의 ID를 선택하지 못하게 한다."""
        with self.assertRaises(ValueError):
            tools.report_to_authority.func(
                "smishing", "target", "summary", make_runtime(None, InMemoryStore())
            )

    def test_receipt_numbers_are_unique(self):
        first = tools.make_receipt_no("loan_scam")
        second = tools.make_receipt_no("loan_scam")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("UH-LOA-"))


class ToolReturnHygieneTests(unittest.TestCase):
    """AGENTS.md ⑤: 반환 데이터의 개인정보 제거.

    Tool 반환값은 모델을 거쳐 사용자에게 도달하므로, 입력을 반향하거나 외부 API
    문자열을 그대로 통과시키면 안 된다.
    """

    def test_url_check_does_not_echo_input(self):
        """URL 안에 개인정보가 있어도 반환값에 옮겨지지 않는다."""
        secrets = ("900101-1234567", "110-234-567890", "010-1111-2222")
        for url in (
            "http://evil.top/x?rrn=900101-1234567",
            "http://evil.top/pay?acc=110-234-567890",
            "http://evil.top/call/010-1111-2222",
            "http://900101-1234567@evil.top/x",
        ):
            with self.subTest(url=url):
                dumped = json.dumps(tools.analyze_url(url), ensure_ascii=False)
                for secret in secrets:
                    self.assertNotIn(secret, dumped)
                    self.assertNotIn(secret.replace("-", ""), dumped)

    def test_external_company_name_is_scrubbed(self):
        """외부 API 문자열 속 개인정보는 라벨로 가린다."""
        fetch = lambda *_a: [
            {"kor_co_nm": "국민은행 900101-1234567", "cal_tel": "1588-9999"}
        ]
        result = tools.verify_number("1588-9999", fetch=fetch)
        self.assertNotIn("900101", result["company"])
        self.assertIn("가림", result["company"])

    def test_official_numbers_survive_scrubbing(self):
        """공식 대표번호는 가리지 않는다. 가리면 대조가 불가능해진다."""
        fetch = lambda *_a: [{"kor_co_nm": "국민은행", "cal_tel": "1588-9999"}]
        result = tools.verify_number("1588-9999", "국민은행", fetch=fetch)
        self.assertIs(result["is_official"], True)
        self.assertEqual(result["official_numbers"], ["15889999"])
        self.assertEqual(result["company"], "국민은행")

    def test_oversized_external_string_is_truncated(self):
        """오염된 응답이 프롬프트를 밀어내지 못하게 길이를 자른다."""
        fetch = lambda *_a: [{"kor_co_nm": "가" * 5000, "cal_tel": "1588-9999"}]
        result = tools.verify_number("1588-9999", fetch=fetch)
        self.assertLessEqual(
            len(result["company"]), tools.MAX_EXTERNAL_FIELD_CHARS + 1
        )

    def test_control_characters_are_removed(self):
        fetch = lambda *_a: [
            {"kor_co_nm": "국민\x00은행\x1b[31m", "cal_tel": "1588-9999"}
        ]
        result = tools.verify_number("1588-9999", fetch=fetch)
        self.assertNotIn("\x00", result["company"])


class PhraseChannelTests(unittest.TestCase):
    """문구 대조 채널은 문장만 본다. URL·번호는 전용 채널이 따로 있다."""

    def test_url_and_number_leave_the_phrase_channel(self):
        phrase = memory.normalize_phrase(
            "[택배] 주소 불일치 반송 http://vv-cj.top/x 010-1111-2222"
        )
        self.assertEqual(phrase, "택배주소불일치반송")
        self.assertNotIn("vvcj", phrase)
        self.assertNotIn("http", phrase)
        self.assertNotIn("1111", phrase)

    def test_phrase_match_no_longer_reports_url_fragments(self):
        """근거 문장에 httpvvcjtop 같은 조각이 나오지 않아야 한다."""
        store = InMemoryStore()
        memory.save_report(store, "U100", memory.build_record(
            "택배 사칭 신고",
            source_text="[택배] 주소 불일치로 반송 http://vv-cj.top/x",
            scam_type="smishing", reported=True))
        matches = memory.match_history(
            memory.load_history(store, "U100"),
            "[택배] 주소 불일치로 반송 예정 http://vv-cj.top/k2")
        phrase_values = [m["value"] for m in matches if m["field"] == "phrase"]
        self.assertTrue(phrase_values)
        for value in phrase_values:
            self.assertNotIn("http", value)
            self.assertNotIn("vvcj", value)

    def test_reused_scam_text_matches_despite_edits(self):
        """같은 문구를 조금 고쳐 재사용해도 잡는다. 연속 부분문자열로는 놓친다."""
        for past, current in (
            ("[택배] 주소 불일치로 반송 http://vv-cj.top/x",
             "[택배] 주소지 불일치 반송 예정(http://vv-cj.top/k2) 또 왔어요"),
            ("[Web발신] 고객님 명의로 해외결제가 승인되었습니다",
             "[Web발신] 고객님 명의로 해외 결제가 승인 되었습니다 확인바랍니다"),
        ):
            with self.subTest(past=past):
                score = memory.phrase_similarity(
                    memory.normalize_phrase(past), memory.normalize_phrase(current))
                self.assertIsNotNone(score)

    def test_unrelated_text_does_not_match(self):
        """한국어 어미가 겹쳐도 무관한 문장은 일치로 보지 않는다."""
        for left, right in (
            ("[택배] 주소 불일치로 반송", "오늘 점심 뭐 먹지 날씨가 좋다"),
            ("[택배] 주소 불일치로 반송", "엄마 나 폰 액정 깨져서 이 번호로 연락해"),
            ("국민은행 대출 안내입니다", "카카오톡 인증번호 안내"),
            # 어미만 겹치는 짧은 인사말. 임계치를 올리기 전에는 70%로 잡혔다.
            ("안녕하세요 반갑습니다", "안녕히 가세요 고맙습니다"),
            ("계좌가 정지되었습니다 확인하세요", "택배가 도착했습니다 확인하세요"),
        ):
            with self.subTest(left=left):
                self.assertIsNone(memory.phrase_similarity(
                    memory.normalize_phrase(left), memory.normalize_phrase(right)))

    def test_phrase_value_is_similarity_not_raw_text(self):
        """근거 문장에 정규화된 원문을 그대로 노출하지 않는다."""
        store = InMemoryStore()
        memory.save_report(store, "U200", memory.build_record(
            "택배 사칭", source_text="[택배] 주소 불일치로 반송 예정입니다",
            scam_type="smishing", reported=True))
        matches = memory.match_history(
            memory.load_history(store, "U200"), "[택배] 주소 불일치로 반송 예정이래요")
        phrase = next(m for m in matches if m["field"] == "phrase")
        self.assertRegex(phrase["value"], r"^\d{1,3}%$")
        self.assertIn("문구가 유사합니다", memory.describe_matches([phrase])[0])

    def test_subject_particle_follows_final_consonant(self):
        """"도메인가"처럼 쓰지 않는다."""
        self.assertEqual(memory._with_subject_particle("도메인"), "도메인이")
        self.assertEqual(memory._with_subject_particle("발신번호"), "발신번호가")
        self.assertEqual(memory._with_subject_particle("문구"), "문구가")

    def test_description_reads_naturally(self):
        match = memory.HistoryMatch(
            key="r1", field="domain", value="vv-cj.top",
            summary="택배 사칭", scam_type="smishing", reported=True)
        line = memory.describe_matches([match])[0]
        self.assertIn("도메인이 일치합니다", line)
        self.assertNotIn("도메인가", line)


class ReportHistoryCaptureTests(unittest.TestCase):
    """4.2 TS-05는 "문구·도메인 패턴이 동일"을 요구한다.

    신고 대상(target)만 저장하면 문구 채널이 비어 경고가 절반만 나간다.
    """

    @staticmethod
    def _message(external):
        return guards.prepare_guarded_message(
            "이 문자 뭐야?", mask_text=lambda t: t, external_texts=[external])

    def test_pasted_text_reaches_report_history(self):
        store = InMemoryStore()
        message = self._message("[택배] 주소 불일치로 반송 http://vv-cj.top/x")
        tools.report_to_authority.func(
            "smishing", "http://vv-cj.top/x", "택배 사칭 신고",
            make_runtime("U001", store, messages=[message]))
        _, record = memory.load_history(store, "U001")[0]
        self.assertEqual(record["phrase"], "택배주소불일치로반송")
        self.assertEqual(record["domains"], ["vv-cj.top"])

    def test_ts05_reports_both_domain_and_phrase(self):
        store = InMemoryStore()
        memory.save_report(store, "U001", memory.build_record(
            "택배 사칭 신고",
            source_text="[택배] 주소 불일치로 반송 http://vv-cj.top/x",
            scam_type="smishing", reported=True))
        matches = memory.match_history(
            memory.load_history(store, "U001"),
            "[택배] 주소지 불일치 반송 예정(http://vv-cj.top/k2) 또 왔어요")
        self.assertEqual({m["field"] for m in matches}, {"domain", "phrase"})

    def test_malformed_messages_do_not_raise(self):
        """형식이 다른 메시지를 만나도 예외를 올리지 않는다."""
        for messages in (None, [], ["문자열"], [object()]):
            with self.subTest(messages=messages):
                self.assertEqual(memory.source_text_from_messages(messages), "")

    def test_pii_tokens_leave_the_phrase_channel(self):
        """PIIMiddleware가 남긴 토큰이 문구 비교에 섞이면 안 된다."""
        phrase = memory.normalize_phrase("[택배] 반송 <SCAM_PHONE_1> 확인")
        self.assertNotIn("scamphone", phrase)
        self.assertNotIn("<", phrase)


class ToolWiringTests(unittest.TestCase):
    def test_design_tool_set_is_registered(self):
        """2.5: Tool은 4종 (lookup_history는 제거됨)."""
        names = [t.name for t in tools.ALL_TOOLS]
        self.assertEqual(names, [
            "check_url_risk", "verify_caller_number",
            "get_scam_playbook", "report_to_authority",
        ])

    def test_docstrings_match_design_sentences(self):
        """AGENTS.md: docstring은 설계서 문장을 그대로 사용한다."""
        self.assertEqual(
            tools.check_url_risk.description,
            "문자에 포함된 URL이 알려진 피싱 사이트인지 확인하고, 단축 URL·유사 도메인·"
            "IP 직접 주소·비정상 TLD 등 위험 신호를 검사합니다.",
        )
        self.assertEqual(
            tools.verify_caller_number.description,
            "걸려온 전화번호가 해당 금융회사의 공식 대표번호인지 대조합니다. "
            "기관 사칭 판별에 사용합니다.",
        )

    def test_emergency_route_disables_only_lookup_tools(self):
        """3.2: 송금 턴에 비활성화되는 것은 조회형 Tool 2종뿐."""
        self.assertEqual(tools.LOOKUP_TOOL_NAMES,
                         ["check_url_risk", "verify_caller_number"])


if __name__ == "__main__":
    unittest.main()
