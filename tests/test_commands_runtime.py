import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import asset
from commands_runtime import CommandModelError, NativeClient, Runtime


class NativeProtocolTests(unittest.TestCase):
    def test_native_completion_has_no_grammar_and_preserves_empty_list(self):
        client = NativeClient('http://127.0.0.1:1')
        client.prompt_tokens = Mock(return_value=[1, 2])
        client.post = Mock(side_effect=[{'tokens': [10, 8104, 11, 7], 'stop_type': 'eos',
                                        'generation_settings': {'grammar': ''}},
                                       {'content': '<|tool_call_start|>[]<|tool_call_end|><|im_end|>'}])
        self.assertEqual(client.decide('explain a command')['calls'], [])
        path, payload = client.post.call_args_list[0].args
        self.assertEqual(path, '/completion')
        self.assertEqual(payload['prompt'], [1, 2])
        self.assertNotIn('grammar', payload)
        self.assertNotIn('json_schema', payload)
        self.assertEqual(client.post.call_args_list[1].args, ('/detokenize', {'tokens': [10, 8104, 11, 7]}))

    def test_truncated_unfinished_or_constrained_completion_refuses_before_parsing(self):
        for update in ({'truncated': True}, {'stop_type': 'limit'}, {'generation_settings': None},
                       {'generation_settings': {'grammar': 'root ::= "x"'}}):
            with self.subTest(update=update):
                client = NativeClient('http://127.0.0.1:1')
                client.prompt_tokens = Mock(return_value=[1])
                client.post = Mock(return_value={'tokens': [10], 'stop_type': 'eos', **update})
                with self.assertRaises(CommandModelError):client.decide('start codex')
                self.assertEqual(client.post.call_count, 1)

    def test_tampered_bundle_never_starts_a_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'model.gguf').write_bytes(b'changed')
            chosen = {'model_member': 'model.gguf', 'files': {
                'model.gguf': {'sha256': hashlib.sha256(b'correct').hexdigest(), 'bytes': 7}}}
            with patch('commands_runtime.inspect_bundle', return_value=(chosen, root)), \
                    patch('commands_runtime.subprocess.Popen') as spawn:
                with self.assertRaises(asset.AssetError):Runtime(root)
                spawn.assert_not_called()

    def test_server_start_failure_releases_private_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ['model.gguf', 'runtime/llama-server']
            for name in names:
                target=root/name; target.parent.mkdir(exist_ok=True); target.write_bytes(b'fixture')
            chosen = {'model_member': 'model.gguf', 'files': {
                name: {'sha256': hashlib.sha256(b'fixture').hexdigest(), 'bytes': 7} for name in names}}
            process=Mock();process.poll.return_value=1
            with patch('commands_runtime.inspect_bundle', return_value=(chosen, root)), \
                    patch('commands_runtime.subprocess.Popen', return_value=process) as spawn:
                with self.assertRaisesRegex(CommandModelError, 'exited during startup'):Runtime(root)
            private_binary=Path(spawn.call_args.args[0][0])
            self.assertFalse(private_binary.parent.parent.exists())
