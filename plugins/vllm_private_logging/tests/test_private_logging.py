import io
import logging
import unittest

from vllm.entrypoints.serve.utils import request_logger
from vllm.sampling_params import SamplingParams

from vllm_private_logging import register

SECRET_PROMPT = "my credit card is 4111-1111-1111-1111"
SECRET_ANSWER = "the model answer with private data"


class PrivateLoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        register()
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.logger = request_logger.logger
        self.previous_level = self.logger.level
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.request_logger = request_logger.RequestLogger(max_log_len=None)

    def tearDown(self) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.previous_level)

    def output(self) -> str:
        self.handler.flush()
        return self.stream.getvalue()

    def test_inputs_are_not_logged_but_request_is(self) -> None:
        self.request_logger.log_inputs(
            "req-1",
            SECRET_PROMPT,
            [101, 102, 103],
            None,
            SamplingParams(max_tokens=16, temperature=0.5, stop=[SECRET_PROMPT]),
            None,
        )
        text = self.output()
        self.assertNotIn(SECRET_PROMPT, text)
        self.assertNotIn("101", text)
        self.assertIn("Received request req-1", text)
        self.assertIn("prompt_tokens=3", text)
        self.assertIn("max_tokens=16", text)

    def test_outputs_are_not_logged(self) -> None:
        self.request_logger.log_outputs("req-2", SECRET_ANSWER, [7, 8], "stop")
        self.request_logger.log_outputs(
            "req-2", SECRET_ANSWER, [9], None, is_streaming=True, delta=True
        )
        text = self.output()
        self.assertNotIn(SECRET_ANSWER, text)
        self.assertIn("Generated response req-2", text)
        self.assertIn("output_tokens=2", text)
        self.assertEqual(text.count("Generated response"), 1)

    def test_request_body_is_not_logged(self) -> None:
        class Body:
            request_id = "req-3"

            def model_dump_json(self, **kwargs: object) -> str:
                return SECRET_PROMPT

        self.request_logger.log_request_body(Body())
        self.assertNotIn(SECRET_PROMPT, self.output())

    def test_unmarked_records_are_dropped(self) -> None:
        self.logger.info("prompt: %r", SECRET_PROMPT)
        self.assertNotIn(SECRET_PROMPT, self.output())

    def test_register_is_idempotent(self) -> None:
        register()
        register()
        self.request_logger.log_outputs("req-4", SECRET_ANSWER, [1], "stop")
        self.assertEqual(self.output().count("Generated response req-4"), 1)


if __name__ == "__main__":
    unittest.main()
