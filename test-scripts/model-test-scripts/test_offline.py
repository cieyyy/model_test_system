import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_basic_functionality as basic
from test_basic_functionality import PERSON_SCHEMA, body, parse_stream, usage_values


class OfflineTests(unittest.TestCase):
    def test_sse_usage_and_done(self):
        sse = '\n'.join((
            'data: {"model":"test-model","choices":[{"delta":{"reasoning_content":"思考"}}]}',
            'data: {"model":"test-model","choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":301,"completion_tokens":12,"prompt_tokens_details":{"cached_tokens":256}}}',
            'data: [DONE]'))
        result = parse_stream(sse)
        self.assertEqual(result['content'], 'OK')
        self.assertEqual(result['reasoning_content'], '思考')
        self.assertTrue(result['done'])
        values = usage_values(result['usage'])
        self.assertEqual(values[2], 256)
        self.assertEqual(values[4], '')
        self.assertFalse(parse_stream(sse.replace('data: [DONE]', ''))['done'])

    def test_body_no_fixed_parameters(self):
        basic.MODEL_NAME = 'test-model'
        request = body([{'role': 'user', 'content': '你好'}], reasoning_effort='low')
        self.assertEqual(request['model'], 'test-model')
        self.assertNotIn('temperature', request)
        self.assertNotIn('thinking', request)
        self.assertEqual(PERSON_SCHEMA['additionalProperties'], False)


if __name__ == '__main__':
    unittest.main()
