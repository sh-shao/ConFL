import io
import os
import unittest
import urllib.error
from unittest.mock import patch
from confl.llm import APIModel, ModelError

class RetryTest(unittest.TestCase):
    def model(self):
        with patch.dict(os.environ, {'TEST_KEY':'test'}):
            return APIModel(dict(provider='openai',model='test',api_key_env='TEST_KEY'))

    def test_quota_does_not_retry(self):
        err=urllib.error.HTTPError('https://example.com',429,'limited',{},io.BytesIO(b'{"error":{"code":"insufficient_quota"}}'))
        with patch('urllib.request.build_opener') as opener, patch('confl.llm.time.sleep') as sleep:
            opener.return_value.open.side_effect=err
            with self.assertRaisesRegex(ModelError,'API_QUOTA_EXHAUSTED'):
                self.model()._post('https://example.com',{}, {})
            sleep.assert_not_called()

    def test_rate_limit_is_bounded(self):
        errors=[urllib.error.HTTPError('https://example.com',429,'limited',{'Retry-After':'1'},io.BytesIO(b'{}')) for _ in range(3)]
        with patch('urllib.request.build_opener') as opener, patch('confl.llm.time.sleep') as sleep:
            opener.return_value.open.side_effect=errors
            with self.assertRaisesRegex(ModelError,'retry budget exhausted'):
                self.model()._post('https://example.com',{}, {})
            self.assertEqual(opener.return_value.open.call_count,3)
            self.assertEqual(sleep.call_count,2)
