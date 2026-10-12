"""Unit tests for the MIREVA adapter (mock-based, no claim fixtures required)."""

from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase, TestCase, override_settings

from claim.models import Claim
from mireva import schema, signals
from mireva.mappers import OpenIMISToMirevaMapper
from mireva.models import MirevaClaimResult, MirevaEvaluation
from mireva.services import MirevaService


def _info(authenticated=True, has_perms=True):
    user = mock.Mock(is_authenticated=authenticated)
    user.has_perms.return_value = has_perms
    return SimpleNamespace(context=SimpleNamespace(user=user))


class MapperTests(SimpleTestCase):
    def test_policy_period_spans_all_lines(self):
        early = SimpleNamespace(start_date=date(2025, 1, 1), effective_date=None, expiry_date=date(2025, 12, 31))
        late = SimpleNamespace(start_date=date(2025, 3, 1), effective_date=date(2025, 4, 1), expiry_date=date(2026, 3, 1))
        claim = SimpleNamespace(
            items=SimpleNamespace(all=lambda: [SimpleNamespace(policy=early)]),
            services=SimpleNamespace(all=lambda: [SimpleNamespace(policy=late), SimpleNamespace(policy=None)]),
        )
        self.assertEqual(
            OpenIMISToMirevaMapper._policy_period(claim), (date(2025, 1, 1), date(2026, 3, 1))
        )

    def test_policy_period_empty(self):
        claim = SimpleNamespace(
            items=SimpleNamespace(all=lambda: []), services=SimpleNamespace(all=lambda: [])
        )
        self.assertEqual(OpenIMISToMirevaMapper._policy_period(claim), (None, None))

    def test_deceased_date_only_when_status_dead(self):
        dead = SimpleNamespace(status="DE", status_date=date(2025, 5, 1))
        alive = SimpleNamespace(status="AC", status_date=date(2025, 5, 1))
        self.assertEqual(OpenIMISToMirevaMapper._deceased_date(dead), date(2025, 5, 1))
        self.assertIsNone(OpenIMISToMirevaMapper._deceased_date(alive))
        self.assertIsNone(
            OpenIMISToMirevaMapper._deceased_date(SimpleNamespace(status="DE", status_date=None))
        )

    def test_normalize_code_strips_icd_dots_and_case(self):
        n = OpenIMISToMirevaMapper.normalize_code
        self.assertEqual(n("ICD-10", " a00.0 "), "A000")
        self.assertEqual(n("openIMIS-ITEM", "0001"), "0001")
        self.assertEqual(n("ICD-10", None), "")

    def test_local_concept_id_is_stable_distinct_and_in_reserved_range(self):
        f = OpenIMISToMirevaMapper.local_concept_id
        a, b = f("ICD-10", "A000"), f("ICD-10", "A001")
        self.assertEqual(a, f("ICD-10", "A000"))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, f("openIMIS-ITEM", "A000"))
        for value in (a, b):
            self.assertGreaterEqual(value, 2_000_000_000)
            self.assertLess(value, 2**31)

    def test_visit_type_labels(self):
        self.assertEqual(OpenIMISToMirevaMapper.VISIT_TYPE_LABELS["I"], "inpatient")


class ServiceTests(SimpleTestCase):
    def _service(self):
        with mock.patch("mireva.services.MirevaClient"):
            return MirevaService.__new__(MirevaService)

    def test_normalize_modern_payload(self):
        out = self._service()._normalize_prediction(
            {
                "final_decision": "REVIEW",
                "fraud_score": 0.6,
                "explanation": {"summary": "s", "reasons": [{"reason_id": "r"}]},
                "hard_rules": {"rules_fired": [{"rule_id": "h"}]},
                "soft_rules": {"rules_fired": [{"rule_id": "s"}]},
            }
        )
        self.assertEqual(out["decision"], "REVIEW")
        self.assertEqual(out["summary"], "s")
        self.assertEqual([f["rule_id"] for f in out["flags"]], ["h", "s"])

    def test_rejection_code_defaults(self):
        extract = MirevaService._extract_rejection_code
        self.assertEqual(extract({}), 20)  # "invalid claim"
        self.assertEqual(extract({"rejection_code": "x"}), 20)
        self.assertEqual(extract({"rejection_code": "7"}), 7)

    def test_rejection_code_maps_first_known_hard_rule(self):
        extract = MirevaService._extract_rejection_code
        self.assertEqual(extract({"hard_rule_ids": ["visit_before_policy"]}), 21)
        self.assertEqual(extract({"hard_rule_ids": ["visit_after_policy"]}), 21)
        self.assertEqual(extract({"hard_rule_ids": ["something_new", "amount_non_positive"]}), 20)
        self.assertEqual(extract({"hard_rule_ids": ["visit_before_policy"], "rejection_code": 5}), 5)

    def test_rejection_explanation_lists_hard_rule_details(self):
        text = MirevaService._rejection_explanation(
            {"reasons": [
                {"source": "hard_rule", "detail": "Visit date before policy start"},
                {"source": "soft_rule", "detail": "ignored"},
            ]},
            "summary",
        )
        self.assertEqual(text, "Rejected by MIREVA hard rules: Visit date before policy start")
        self.assertEqual(MirevaService._rejection_explanation({}, "summary"), "summary")
        self.assertIn("MIREVA", MirevaService._rejection_explanation({}, ""))

    def test_skip_existing_short_circuits_before_remote_call(self):
        service = self._service()
        service.client = mock.Mock()
        existing = object()
        with mock.patch("mireva.services.MirevaClaimResult") as result_model:
            result_model.objects.filter.return_value.first.return_value = existing
            self.assertIs(service.evaluate_claim("u", skip_existing=True), existing)
        service.client.predict.assert_not_called()


class SignalTests(SimpleTestCase):
    def _claim(self, status):
        return SimpleNamespace(status=status, uuid="abc")

    def test_queues_after_commit_with_skip_existing(self):
        from claim.models import Claim

        with mock.patch.object(signals, "MirevaClaimResult") as res, mock.patch.object(
            signals.transaction, "on_commit", side_effect=lambda fn: fn()
        ), mock.patch.object(signals, "evaluate_claim_async") as task:
            res.objects.filter.return_value.exists.return_value = False
            signals.on_claim_saved(Claim, self._claim(Claim.STATUS_CHECKED), False)
        task.delay.assert_called_once_with("abc", skip_existing=True)

    def test_broker_failure_is_swallowed(self):
        from claim.models import Claim

        with mock.patch.object(signals, "MirevaClaimResult") as res, mock.patch.object(
            signals.transaction, "on_commit", side_effect=lambda fn: fn()
        ), mock.patch.object(signals, "evaluate_claim_async") as task:
            res.objects.filter.return_value.exists.return_value = False
            task.delay.side_effect = ConnectionError("broker down")
            signals.on_claim_saved(Claim, self._claim(Claim.STATUS_CHECKED), False)

    def test_ignores_non_checked(self):
        from claim.models import Claim

        with mock.patch.object(signals.transaction, "on_commit") as on_commit:
            signals.on_claim_saved(Claim, self._claim(Claim.STATUS_ENTERED), False)
        on_commit.assert_not_called()


class SchemaPermissionTests(SimpleTestCase):
    def test_queries_require_permission(self):
        for fn, args in (
            (schema.Query.resolve_mireva_claim_results, ()),
            (schema.Query.resolve_mireva_claim_result, ("u",)),
        ):
            with self.assertRaises(PermissionDenied):
                fn(None, _info(has_perms=False), *args)
            with self.assertRaises(PermissionDenied):
                fn(None, _info(authenticated=False), *args)

    def test_mutation_requires_permission_and_valid_decision(self):
        with self.assertRaises(PermissionDenied):
            schema.SubmitMirevaReview.mutate(None, _info(has_perms=False), "u", "APPROVE")
        with self.assertRaises(ValueError):
            schema.SubmitMirevaReview.mutate(None, _info(), "u", "MAYBE")


class DatabaseTests(TestCase):
    """Run the real service against a database; only the MIREVA HTTP call is mocked."""

    @classmethod
    def setUpTestData(cls):
        from claim.test_helpers import create_test_claim

        cls.claim = create_test_claim({"status": Claim.STATUS_CHECKED})

    def _service(self, decision="APPROVE", extra=None, **settings_overrides):
        with mock.patch("mireva.services.MirevaClient") as client_cls:
            client_cls.return_value.predict.return_value = {
                "final_decision": decision,
                "fraud_score": 0.4,
                "risk_level": "MEDIUM",
                "confidence": 0.9,
                "model_version": "mireva-origin",
                "explanation": {"summary": "because"},
                **(extra or {}),
            }
            with override_settings(
                MIREVA_URL="http://mireva.test", MIREVA_API_KEY="k", **settings_overrides
            ):
                service = MirevaService()
        return service

    def test_approve_persists_result_and_updates_claim(self):
        service = self._service("APPROVE")
        result = service.evaluate_claim(str(self.claim.uuid))
        self.claim.refresh_from_db()
        self.assertEqual(result.decision, "APPROVE")
        self.assertEqual(self.claim.status, Claim.STATUS_PROCESSED)
        payload = service.client.predict.call_args.args[0]
        self.assertEqual(payload["claim_id"], str(self.claim.uuid))
        self.assertEqual(payload["provider_id"], str(self.claim.health_facility.uuid))
        self.assertIn(payload["visit"]["visit_type"], {"outpatient", "inpatient", "emergency"})

    def test_hard_rule_reject_is_applied_automatically(self):
        self._service(
            "REJECT", extra={"hard_rules": {"decision": "REJECT"}, "skipped_model": True}
        ).evaluate_claim(str(self.claim.uuid))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, Claim.STATUS_REJECTED)
        self.assertEqual(self.claim.rejection_reason, 20)
        self.assertEqual(self.claim.explanation, "because")  # no hard-rule detail supplied

    def test_model_only_reject_goes_to_review(self):
        self._service("REJECT", extra={"hard_rules": {"decision": "PASS"}}).evaluate_claim(
            str(self.claim.uuid)
        )
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, Claim.STATUS_CHECKED)
        self.assertEqual(self.claim.review_status, Claim.REVIEW_SELECTED)
        # The stored result still records what MIREVA decided.
        self.assertEqual(MirevaClaimResult.objects.get(claim=self.claim).decision, "REJECT")

    def test_auto_reject_always_setting_restores_model_rejections(self):
        self._service(
            "REJECT", extra={"hard_rules": {"decision": "PASS"}}, MIREVA_AUTO_REJECT="always"
        ).evaluate_claim(str(self.claim.uuid))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, Claim.STATUS_REJECTED)

    def test_invalid_auto_reject_setting_is_refused(self):
        with self.assertRaises(ValueError):
            self._service(MIREVA_AUTO_REJECT="sometimes")

    def test_review_marks_claim_selected(self):
        self._service("REVIEW").evaluate_claim(str(self.claim.uuid))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.review_status, Claim.REVIEW_SELECTED)

    def test_skip_existing_does_not_call_mireva_again_or_duplicate(self):
        service = self._service("REVIEW")
        first = service.evaluate_claim(str(self.claim.uuid))
        second = service.evaluate_claim(str(self.claim.uuid), skip_existing=True)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(service.client.predict.call_count, 1)
        self.assertEqual(MirevaClaimResult.objects.filter(claim=self.claim).count(), 1)

    def test_locked_path_skips_when_result_appears_during_remote_call(self):
        service = self._service("APPROVE")

        def racing_predict(_payload):
            MirevaClaimResult.objects.create(
                claim=self.claim, decision="REVIEW", fraud_score=0.9,
                risk_level="HIGH", confidence=0.5, model_version="other",
            )
            return {"final_decision": "APPROVE", "fraud_score": 0.1}

        service.client.predict.side_effect = racing_predict
        result = service.evaluate_claim(str(self.claim.uuid), skip_existing=True)
        self.claim.refresh_from_db()
        self.assertEqual(result.decision, "REVIEW")
        self.assertEqual(self.claim.status, Claim.STATUS_CHECKED)

    def test_review_mutation_updates_claim(self):
        self._service("REVIEW").evaluate_claim(str(self.claim.uuid))
        info = _info()
        info.context.user = mock.Mock(is_authenticated=True, has_perms=lambda p: True)
        # reviewed_by is a FK to core.User, so use a real user row.
        from core.models import User

        info.context.user = User.objects.first() or mock.Mock()
        with mock.patch.object(schema, "_require_perms"):
            if not isinstance(info.context.user, User):
                self.skipTest("no core.User available in test database")
            out = schema.SubmitMirevaReview.mutate(
                None, info, str(self.claim.uuid), "REJECT", "fraudulent"
            )
        self.claim.refresh_from_db()
        self.assertEqual(out.review_decision, "REJECT")
        self.assertEqual(self.claim.status, Claim.STATUS_REJECTED)

    def test_review_mutation_unknown_claim(self):
        with mock.patch.object(schema, "_require_perms"), self.assertRaises(ValueError):
            schema.SubmitMirevaReview.mutate(None, _info(), "no-such-uuid", "APPROVE")

    def test_provider_history_excludes_current_claim_and_own_rejections(self):
        from claim.test_helpers import create_test_claim

        hf = self.claim.health_facility
        other = create_test_claim({"health_facility": hf, "status": Claim.STATUS_REJECTED})
        mireva_rejected = create_test_claim({"health_facility": hf, "status": Claim.STATUS_REJECTED})
        MirevaClaimResult.objects.create(
            claim=mireva_rejected, decision="REJECT", fraud_score=0.9,
            risk_level="HIGH", confidence=0.9, model_version="x",
        )
        mapper = OpenIMISToMirevaMapper()
        without_self = mapper.fetch_provider_history(hf.id, exclude_claim_id=self.claim.id)
        # 3 claims total: current(CHECKED), other(REJECTED by humans), mireva_rejected.
        # Excluding the current claim leaves 2; only the human rejection counts -> 0.5.
        self.assertEqual(without_self["rejection_rate"], 0.5)
        self.assertEqual(other.status, Claim.STATUS_REJECTED)

    def test_model_version_setting_is_passed_to_client(self):
        with mock.patch("mireva.services.MirevaClient") as client_cls, override_settings(
            MIREVA_URL="http://mireva.test", MIREVA_API_KEY="k", MIREVA_MODEL_VERSION="mireva-concord"
        ):
            MirevaService()
        self.assertEqual(client_cls.call_args.kwargs["model_version"], "mireva-concord")

    def test_payload_includes_submission_date(self):
        payload = OpenIMISToMirevaMapper().map_claim(self.claim, [], None)
        self.assertEqual(payload["submission_date"], self.claim.date_claimed.isoformat())


class MappingDatabaseTests(TestCase):
    """OMOP mapping lookup, CSV loading and coverage reporting."""

    def _csv(self, text):
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(__import__("os").unlink, handle.name)
        return handle.name

    def test_mapped_code_wins_and_unmapped_gets_local_id(self):
        from mireva.models import OmopConceptMapping

        OmopConceptMapping.objects.create(
            source_code="A000", source_vocabulary="ICD-10",
            concept_id=4116491, concept_name="Cholera", domain="condition",
        )
        mapper = OpenIMISToMirevaMapper()
        self.assertEqual(mapper.concept_id("ICD-10", "A00.0"), 4116491)
        unmapped = mapper.concept_id("ICD-10", "Z999")
        self.assertGreaterEqual(unmapped, OpenIMISToMirevaMapper.LOCAL_CONCEPT_BASE)
        self.assertEqual(mapper.concept_id("ICD-10", ""), 0)

    def test_mapper_loads_each_vocabulary_once(self):
        mapper = OpenIMISToMirevaMapper()
        with self.assertNumQueries(1):
            for code in ("A1", "A2", "A3"):
                mapper.concept_id("ICD-10", code)

    def test_loader_creates_updates_and_skips_blank(self):
        from django.core.management import call_command
        from mireva.models import OmopConceptMapping

        path = self._csv(
            "source_code,vocabulary,concept_id,concept_name\n"
            "A00.0,ICD-10,4116491,Cholera\n"
            "0001,openIMIS-ITEM,1112807,Aspirin\n"
            "M1,openIMIS-SERVICE,,\n"
        )
        call_command("mireva_load_omop_mappings", path, stdout=__import__("io").StringIO())
        self.assertEqual(OmopConceptMapping.objects.count(), 2)
        self.assertEqual(OmopConceptMapping.objects.get(source_code="A000").domain, "condition")
        self.assertEqual(OmopConceptMapping.objects.get(source_code="0001").domain, "drug")
        path = self._csv("source_code,vocabulary,concept_id\nA00.0,ICD-10,999\n")
        call_command("mireva_load_omop_mappings", path, stdout=__import__("io").StringIO())
        self.assertEqual(OmopConceptMapping.objects.get(source_code="A000").concept_id, 999)

    def test_loader_rejects_bad_rows_atomically(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from mireva.models import OmopConceptMapping

        path = self._csv(
            "source_code,vocabulary,concept_id\n"
            "A00.0,ICD-10,4116491\n"
            "A01,ICD-10,abc\n"
            "A02,ICD-10,2000000005\n"
            "A03,NOPE,5\n"
        )
        with self.assertRaises(CommandError) as ctx:
            call_command("mireva_load_omop_mappings", path)
        self.assertIn("line 3", str(ctx.exception))
        self.assertIn("reserved local range", str(ctx.exception))
        self.assertEqual(OmopConceptMapping.objects.count(), 0)

    def test_coverage_reports_and_exports_unmapped(self):
        import csv
        import io
        import os
        import tempfile

        from django.core.management import call_command
        from claim.test_helpers import create_test_claim

        create_test_claim({"status": Claim.STATUS_CHECKED})
        out_path = os.path.join(tempfile.mkdtemp(), "unmapped.csv")
        out = io.StringIO()
        call_command("mireva_mapping_coverage", export=out_path, stdout=out)
        self.assertIn("ICD-10: 0/", out.getvalue())
        with open(out_path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertTrue(all(r["concept_id"] == "" for r in rows))
        self.assertEqual({r["vocabulary"] for r in rows} & {"ICD-10"}, {"ICD-10"})


class AuditSweepExportTests(TestCase):
    """Evaluation audit rows, the unevaluated-claims sweep and the training export."""

    @classmethod
    def setUpTestData(cls):
        from claim.test_helpers import create_test_claim

        cls.claim = create_test_claim({"status": Claim.STATUS_CHECKED})

    def _service(self, decision="APPROVE", **settings_overrides):
        return DatabaseTests._service(self, decision, **settings_overrides)

    def test_every_evaluation_is_audited_even_when_result_is_overwritten(self):
        service = self._service("APPROVE")
        service.evaluate_claim(str(self.claim.uuid))
        service.evaluate_claim(str(self.claim.uuid))
        self.assertEqual(MirevaClaimResult.objects.filter(claim=self.claim).count(), 1)
        rows = list(MirevaEvaluation.objects.filter(claim=self.claim))
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first.action, MirevaEvaluation.ACTION_APPROVED)
        self.assertEqual(len(first.payload_hash), 64)
        self.assertEqual(first.request_payload["claim_id"], str(self.claim.uuid))
        self.assertEqual(first.payload_hash, MirevaService._payload_hash(first.request_payload))

    def test_audit_records_downgraded_rejection(self):
        self._service("REJECT").evaluate_claim(str(self.claim.uuid))
        evaluation = MirevaEvaluation.objects.get(claim=self.claim)
        self.assertEqual(evaluation.decision, "REJECT")
        self.assertEqual(evaluation.action, MirevaEvaluation.ACTION_REVIEW_DOWNGRADED)

    def test_payload_storage_can_be_disabled(self):
        self._service("APPROVE", MIREVA_STORE_PAYLOAD=False).evaluate_claim(str(self.claim.uuid))
        evaluation = MirevaEvaluation.objects.get(claim=self.claim)
        self.assertIsNone(evaluation.request_payload)
        self.assertEqual(len(evaluation.payload_hash), 64)

    def test_unsupported_decision_rolls_back_everything(self):
        service = self._service("MAYBE")
        with self.assertRaises(Exception):
            service.evaluate_claim(str(self.claim.uuid))
        self.assertFalse(MirevaClaimResult.objects.filter(claim=self.claim).exists())
        self.assertFalse(MirevaEvaluation.objects.filter(claim=self.claim).exists())

    def test_sweep_queues_only_claims_without_result(self):
        from claim.test_helpers import create_test_claim
        from mireva import tasks

        evaluated = create_test_claim({"status": Claim.STATUS_CHECKED})
        MirevaClaimResult.objects.create(
            claim=evaluated, decision="APPROVE", fraud_score=0.1,
            risk_level="LOW", confidence=0.9, model_version="x",
        )
        with mock.patch.object(tasks, "evaluate_claim_async") as task:
            queued = tasks.queue_unevaluated_claims(limit=10_000)
        queued_uuids = {call.args[0] for call in task.delay.call_args_list}
        self.assertIn(str(self.claim.uuid), queued_uuids)
        self.assertNotIn(str(evaluated.uuid), queued_uuids)
        self.assertEqual(queued, len(queued_uuids))
        self.assertTrue(all(c.kwargs == {"skip_existing": True} for c in task.delay.call_args_list))

    def test_sweep_stops_when_broker_is_down(self):
        from mireva import tasks

        with mock.patch.object(tasks, "evaluate_claim_async") as task:
            task.delay.side_effect = ConnectionError("broker down")
            self.assertEqual(tasks.queue_unevaluated_claims(limit=50), 0)
        self.assertEqual(task.delay.call_count, 1)

    def test_export_writes_labelled_rows_and_reports_agreement(self):
        import io
        import json
        import os
        import tempfile

        from django.core.management import call_command
        from core.models import User

        self._service("REJECT").evaluate_claim(str(self.claim.uuid))
        result = MirevaClaimResult.objects.get(claim=self.claim)
        result.review_decision = "APPROVE"  # reviewer overrides MIREVA's REJECT
        result.reviewed_by = User.objects.first()
        result.reviewed_at = __import__("django.utils.timezone", fromlist=["now"]).now()
        result.save()

        out_path = os.path.join(tempfile.mkdtemp(), "labels.jsonl")
        out = io.StringIO()
        call_command("mireva_export_training_data", output=out_path, stdout=out)
        self.assertIn("1 overridden", out.getvalue())
        with open(out_path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle]
        mine = [r for r in rows if r["claim_id"] == str(self.claim.uuid)]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["label"], 0)
        self.assertEqual(mine[0]["mireva_decision"], "REJECT")
        self.assertEqual(mine[0]["payload"]["claim_id"], str(self.claim.uuid))

    def test_export_skips_undecided_review_rows(self):
        import io

        from django.core.management import call_command

        self._service("REVIEW").evaluate_claim(str(self.claim.uuid))
        MirevaClaimResult.objects.filter(claim=self.claim).update(review_decision="REVIEW")
        out = io.StringIO()
        call_command("mireva_export_training_data", stdout=out)
        self.assertIn("undecided", out.getvalue())


@override_settings(CHANNEL_LAYERS={})  # no websocket push: tests must not need RabbitMQ
class ReviewerNotificationTests(TestCase):
    """Reviewers are found by role right and notified once, after commit."""

    @classmethod
    def setUpTestData(cls):
        from claim.test_helpers import create_test_claim
        from core.models import Role, RoleRight
        from core.test_helpers import create_test_interactive_user

        cls.claim = create_test_claim({"status": Claim.STATUS_CHECKED})

        def role_with(name, right_id, blocked=False):
            role = Role.objects.create(name=name, is_system=0, is_blocked=blocked, audit_user_id=-1)
            RoleRight.objects.create(role=role, right_id=right_id, audit_user_id=-1)
            return role

        reviewer_role = role_with("mireva_reviewer", 111010)
        other_role = role_with("mireva_other", 111001)
        blocked_role = role_with("mireva_blocked", 111010, blocked=True)
        cls.reviewer = create_test_interactive_user("mireva_rev", roles=[reviewer_role.id])
        cls.other = create_test_interactive_user("mireva_oth", roles=[other_role.id])
        cls.blocked = create_test_interactive_user("mireva_blk", roles=[blocked_role.id])

    def test_find_reviewers_uses_valid_unblocked_roles_with_the_right(self):
        from mireva.notifications import find_reviewers

        names = {u.username for u in find_reviewers()}
        self.assertIn("mireva_rev", names)
        self.assertNotIn("mireva_oth", names)
        self.assertNotIn("mireva_blk", names)

    def test_rights_can_be_overridden(self):
        from mireva.notifications import find_reviewers

        with override_settings(MIREVA_REVIEWER_RIGHTS=[111001]):
            names = {u.username for u in find_reviewers()}
        self.assertIn("mireva_oth", names)
        self.assertNotIn("mireva_rev", names)

    def test_notify_creates_one_deduplicated_notification_per_reviewer(self):
        from mireva.notifications import notify_reviewers
        from notification.models import Notification

        prediction = {
            "risk_level": "HIGH", "fraud_score": 0.87,
            "reasons": [{"title": "late_submission_proxy", "source": "soft_rule"},
                        {"title": "Model scores", "source": "ai"}],
        }
        first = notify_reviewers(str(self.claim.uuid), self.claim.code, prediction, downgraded=True)
        again = notify_reviewers(str(self.claim.uuid), self.claim.code, prediction, downgraded=True)
        self.assertGreaterEqual(first, 1)
        self.assertEqual(again, 0)  # dedupe_key: no repeat for the same claim
        row = Notification.objects.get(recipient=self.reviewer, source="mireva")
        self.assertIn(self.claim.code, row.title)
        self.assertIn("HIGH", row.body)
        self.assertIn("0.87", row.body)
        self.assertIn("late_submission_proxy", row.body)
        self.assertNotIn("Model scores", row.body)
        self.assertEqual(row.severity, "error")
        self.assertFalse(Notification.objects.filter(recipient=self.other).exists())

    def test_notification_failure_never_raises(self):
        from mireva import notifications

        with mock.patch.object(notifications, "find_reviewers", side_effect=RuntimeError("db")):
            self.assertEqual(
                notifications.notify_reviewers(str(self.claim.uuid), "C1", {}, downgraded=False), 0
            )

    def test_evaluation_sends_notification_after_commit_only_for_review(self):
        from notification.models import Notification

        service = DatabaseTests._service(self, "REVIEW")
        with self.captureOnCommitCallbacks(execute=True):
            service.evaluate_claim(str(self.claim.uuid))
        self.assertTrue(Notification.objects.filter(recipient=self.reviewer, source="mireva").exists())

    def test_approved_claim_sends_no_notification(self):
        from notification.models import Notification

        service = DatabaseTests._service(self, "APPROVE")
        with self.captureOnCommitCallbacks(execute=True):
            service.evaluate_claim(str(self.claim.uuid))
        self.assertFalse(Notification.objects.filter(source="mireva").exists())
