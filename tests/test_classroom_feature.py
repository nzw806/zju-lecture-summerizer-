import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app
import classroom_client as cc
import pipeline

TOKEN = "test-token-not-real"
COOKIE = (
    "_token=%3A2%3A%7Bi%3A0%3Bs%3A6%3A%22_token%22%3Bi%3A1%3Bs%3A11%3A%22"
    + TOKEN + "%22%3B%7D"
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class ClassroomClientTests(unittest.TestCase):
    def test_extract_token(self):
        self.assertEqual(cc.extract_bearer_token(COOKIE), TOKEN)
        with self.assertRaises(cc.ClassroomAuthError):
            cc.extract_bearer_token("not-a-classroom-cookie")

    def test_group_my_course_day_and_authorization(self):
        payload = {"list": {"day": {"course": [
            {"id": "101", "title": "算法", "sub_id": "201",
             "sub_title": "第 1 讲", "realname": "张老师"},
            {"id": "101", "title": "算法", "sub_id": "202",
             "sub_title": "第 2 讲", "realname": "张老师"},
        ]}}}
        with mock.patch.object(cc.urllib.request, "urlopen",
                               return_value=FakeResponse(payload)) as opened:
            courses = cc.get_my_course_day("2026-11-05", COOKIE)
        self.assertEqual(courses[0]["id"], "101")
        self.assertEqual(len(courses[0]["sessions"]), 2)
        request = opened.call_args.args[0]
        self.assertIn("day=2026-11-05", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)

    def test_replay_status_available_empty_and_unknown(self):
        cases = [
            ({"data": {"content": {"save_playback": {"contents": "url"}}}},
             "available"),
            ({"data": {"content": {"save_playback": {"contents": ""}}}},
             "unavailable"),
            ({"data": {"content": {}}}, "unknown"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(
                    cc.urllib.request, "urlopen",
                    return_value=FakeResponse(payload)):
                result = cc.get_sub_info("101", "201", COOKIE)
                self.assertEqual(result["status"], expected)
                expected_count = (1 if expected == "available"
                                  else 0 if expected == "unavailable" else None)
                self.assertEqual(result["content_count"], expected_count)

    def test_pipeline_rejects_non_numeric_direct_selection(self):
        with self.assertRaises(pipeline.PipelineError):
            pipeline.resolve_selection(course_id="abc", sub_id="201", tenant_code="112")

    def test_http_401_is_auth_error_without_body_leak(self):
        error = urllib.error.HTTPError(
            "https://classroom.zju.edu.cn", 401, "unauthorized", {}, io.BytesIO())
        with mock.patch.object(cc.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(cc.ClassroomAuthError):
                cc.get_my_course_day("2026-11-05", COOKIE)


class DirectSelectionTests(unittest.TestCase):
    def test_pipeline_accepts_course_and_sub_id(self):
        with tempfile.TemporaryDirectory() as output, \
                mock.patch.object(pipeline, "fetch_transcript",
                                  return_value=[(0, "课程内容")]), \
                mock.patch.object(
                    pipeline, "llm_chat",
                    return_value=("# note", {"prompt_tokens": 1,
                                             "completion_tokens": 1})) as chat:
            result = pipeline.run_pipeline(
                api_key="test-ai-key", out_root=output,
                course_id="101", sub_id="201", tenant_code="112",
                course_name="算法", session_name="第 1 讲",
                base_url="https://proxy.invalid/v1", model="test-model")
        self.assertEqual(result["course_id"], "101")
        self.assertEqual(result["sub_id"], "201")
        self.assertIn("课程：算法；课堂：第 1 讲", chat.call_args.args[2])


class LocalRouteTests(unittest.TestCase):
    def test_course_route_saves_cookie_only_locally(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            request = urllib.request.Request(
                origin + "/api/classroom/courses",
                data=json.dumps({"date": "2026-11-05", "cookie": COOKIE,
                                 "save": True}).encode(),
                headers={"Content-Type": "application/json"})
            with mock.patch.object(cc, "get_my_course_day", return_value=[]), \
                    mock.patch.object(app, "save_config") as save:
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response), {"courses": []})
                save.assert_called_once()
                self.assertEqual(save.call_args.args[0]["cookie"], COOKIE)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_course_route_requires_cookie(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/classroom/courses",
                data=json.dumps({"date": "2026-11-05", "cookie": ""}).encode(),
                headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
